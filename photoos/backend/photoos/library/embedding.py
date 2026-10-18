"""视觉编码器（Phase 2 · M3）。

CLIP ViT-B/32 的 **vision tower**（ONNX，512 维，L2 归一化）。

## 本模块存在的一个主要理由：不要让 CUDA 静默降级

调研阶段实测（`PHASE2_RESEARCH.md` §2.3）：**CUDA 有三个必须同时正确的条件，错一个就静默跑在 CPU 上** ——

1. `onnxruntime-gpu` 必须是 **CUDA 12 构建**（1.25.1 是最后一个；1.27+ 要 CUDA 13，
   而 PyPI 上 CUDA 13 的 Windows 轮子只有 cuDNN 有）；
2. `nvidia-cudnn-cu12` 必须是 **9.10.2.21**（9.27.0.42 实测在第一次 Conv 就
   `CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED`）；
3. **必须显式调用 `ort.preload_dlls()`**（ORT ≥1.21 在 Windows 不再自动加载
   `nvidia-*` 站点包里的 DLL）。

而最阴的一点：这三种状态下 `get_available_providers()` 输出**完全相同**，
`session.get_providers()` 在其中一种下**也骗人**（provider 建得起来，推理时才炸）。
**唯一可靠的判据是跑一次真推理并计时** —— 这正是 `probe()` 做的事，
也是 `photoos ai doctor` 的核心。

## 与 PhotoOS 既有解码路径的关系

输入是**已生成的 `preview`**（WebP，900–1600 px）。实测（R3）：
preview→224 与 原片→224 的同图余弦 0.832–0.996，而"不同图"只有 0.463–0.652 ——
**两个分布不重叠**。因此用 preview 编码是等价的，而 IO 从 413 GB 降到 3.8 GB。

附带好处：`preview` **已经烘焙了 EXIF 方向**（实测 3,225 张 `orientation∈{6,8}`
的原片是 6016×4016，而它们的 preview 是 1068×1600 竖构图），
所以这里**不需要**再处理 orientation —— 若改用原片就必须自己转，否则会裁错构图。

> ⚠️ **上面那句推断是错的（2026-10 订正）。** "preview 是竖构图"只证明**转了 90°**，
> **不证明转对了** —— 180° 与 90° 一样改变宽高比。实测确实有一批 preview
> 是**颠倒**的（`media/adapter.py` 把 LibRaw 的 `sizes.flip` 用在了内嵌预览上，
> 二次旋转；3,861 张受影响）。详见 `docs/13-raw-orientation.md`。
>
> 对**本模块**（CLIP）的结论仍然成立，但理由是**实测出来的**、不是推断的：
> 同一张图转 90°/180°/270° 后与正立的余弦中位 0.980–0.988、最低 0.803，
> 没有一个掉进"不同图"区间（`_probe/clip_rotation.py`）。
> 对**人脸**（SFace）则完全不成立：倒转后同一张脸的余弦中位只有 0.105。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

from . import model_store
from .model_store import EmbeddingNotAvailable
from .vectorstore import VectorStoreError, l2_normalize

#: CLIP 官方预处理默认值。**仅作兜底**：正常路径一律从 `preprocessor_config.json` 读，
#: 这样"预处理口径"与"权重版本"始终配套（`prep_version` 记录的正是这个口径）。
FALLBACK_MEAN = (0.48145466, 0.4578275, 0.40821073)
FALLBACK_STD = (0.26862954, 0.26130258, 0.27577711)
FALLBACK_SIZE = 224

#: 预处理尺寸
DEFAULT_BATCH = 16
#: OOM 时依次降到的批大小（PRD §11.1「GPU 内存不足时支持降低 batch size」）
OOM_FALLBACK_BATCHES = (16, 8, 4, 2, 1)


#: `EmbeddingNotAvailable` 是 `model_store.EmbeddingNotAvailable` 的**同一个对象**
#: （上面已 import）。这里**不再定义同名类** ——
#: 早先本模块有过一个同名同义的独立类，后果是「按 A 捕获却抛出 B」：
#: 调用方写了 `except embedding.EmbeddingNotAvailable` 却漏掉 `model_store` 抛出的那条路径。


@dataclass
class PreprocessConfig:
    size: int = FALLBACK_SIZE
    mean: tuple[float, float, float] = FALLBACK_MEAN
    std: tuple[float, float, float] = FALLBACK_STD

    @classmethod
    def from_model_dir(cls, data_root: Path) -> "PreprocessConfig":
        raw = model_store.load_preprocessor_config(data_root)
        size = FALLBACK_SIZE
        declared = raw.get("size")
        if isinstance(declared, dict):
            size = int(declared.get("shortest_edge") or declared.get("height") or FALLBACK_SIZE)
        elif isinstance(declared, int):
            size = int(declared)
        mean = tuple(float(x) for x in raw.get("image_mean", FALLBACK_MEAN))[:3]
        std = tuple(float(x) for x in raw.get("image_std", FALLBACK_STD))[:3]
        if len(mean) != 3 or len(std) != 3 or any(s == 0 for s in std):
            raise EmbeddingNotAvailable("preprocessor_config.json 的 image_mean/image_std 不合法")
        return cls(size=size, mean=mean, std=std)


def preprocess_image(image, config: PreprocessConfig) -> "np.ndarray":
    """复刻 `CLIPImageProcessor`：resize 短边 → center crop → /255 → normalize → CHW。

    返回 `(3, H, W)` float32。
    """
    from PIL import Image

    size = int(config.size)
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("图像尺寸为 0")
    scale = size / min(width, height)
    new_w, new_h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
    resized = image.resize((new_w, new_h), Image.BICUBIC)
    left, top = (new_w - size) // 2, (new_h - size) // 2
    cropped = resized.crop((left, top, left + size, top + size)).convert("RGB")

    array = np.asarray(cropped, dtype=np.float32) / 255.0
    mean = np.asarray(config.mean, dtype=np.float32)
    std = np.asarray(config.std, dtype=np.float32)
    array = (array - mean) / std
    return np.ascontiguousarray(array.transpose(2, 0, 1))


@dataclass
class ProbeResult:
    """一次真实推理的探测结果 —— **CUDA 是否生效的唯一可靠判据**。"""

    providers_available: list[str] = field(default_factory=list)
    providers_session: list[str] = field(default_factory=list)
    device: str = "unknown"          # 'cuda' | 'cpu'
    ms_per_image: float = 0.0
    ort_version: str = ""
    cudnn_version: str = ""
    degraded: bool = False           # 请求了 CUDA 但实际跑在 CPU
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.degraded:
            return f"⚠️ 降级到 CPU（{self.ms_per_image:.1f} ms/张）"
        if self.device == "cuda":
            return f"✅ GPU 生效（{self.ms_per_image:.1f} ms/张）"
        return f"CPU（{self.ms_per_image:.1f} ms/张）"


@runtime_checkable
class Embedder(Protocol):
    """视觉编码器接口（可替换模型，PRD §9.3「可替换的 Embedding 模型」）。"""

    model: str
    model_version: str
    dim: int
    normalization: str
    prep_version: str

    def embed_images(self, images: Sequence[object]) -> "np.ndarray": ...

    def embed_paths(self, paths: Sequence[Path]) -> "np.ndarray": ...


class NullEmbedder:
    """未启用时的占位实现。

    **存在的主要理由是"绝不静默产出垃圾"**：如果运行时或模型缺失就返回零向量，
    相似度会全部变成 0，用户看到的是"所有照片都不像"——
    一个看起来正常、实际完全错误的结果。这里直接抛错。
    """

    model = model_store.MODEL_NAME
    model_version = model_store.MODEL_VERSION
    dim = model_store.MODEL_DIM
    normalization = model_store.MODEL_NORMALIZATION
    prep_version = model_store.PREP_VERSION

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _raise(self) -> None:
        raise EmbeddingNotAvailable(
            f"嵌入功能未启用：{self.reason}。"
            "安装运行时后重试：pip install -e \".[ai-gpu]\"，再运行 photoos ai fetch-model"
        )

    def embed_images(self, images: Sequence[object]) -> "np.ndarray":
        self._raise()

    def embed_paths(self, paths: Sequence[Path]) -> "np.ndarray":
        self._raise()


class ClipOnnxEmbedder:
    """CLIP ViT-B/32 vision tower 的 ONNX 实现。"""

    model = model_store.MODEL_NAME
    model_version = model_store.MODEL_VERSION
    dim = model_store.MODEL_DIM
    normalization = model_store.MODEL_NORMALIZATION
    prep_version = model_store.PREP_VERSION

    def __init__(
        self,
        *,
        session,
        config: PreprocessConfig,
        ort_module,
        input_name: str = "pixel_values",
        device: str = "cpu",
    ) -> None:
        self._session = session
        self._config = config
        self._ort = ort_module
        self._input_name = input_name
        self.device = device

    # ------------------------------------------------------------------ 构造
    @classmethod
    def create(cls, data_root: Path, *, device: str = "auto", threads: int | None = None) -> "ClipOnnxEmbedder":
        """按需创建；运行时或模型缺失时抛 `EmbeddingNotAvailable`（**不静默降级**）。"""
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise EmbeddingNotAvailable(
                "未安装 onnxruntime（可选依赖）。安装：pip install -e \".[ai-gpu]\""
            ) from exc

        model_path = model_store.vision_model_path(data_root)
        if not model_path.exists():
            raise EmbeddingNotAvailable(
                f"模型缺失：{model_path}。请先运行 `photoos ai fetch-model`"
            )

        # ★ 必须有这一步：ORT ≥1.21 在 Windows 不再自动加载 nvidia-* 站点包里的 DLL。
        #   不加它，CUDAExecutionProvider 会**静默**降级到 CPU（实测）。
        try:
            ort.preload_dlls()
        except Exception:  # noqa: BLE001 - 老版本没有这个函数，或非 Windows
            pass

        providers: list = []
        if device in ("auto", "cuda"):
            providers.append(("CUDAExecutionProvider", {"device_id": 0}))
        providers.append("CPUExecutionProvider")

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3
        if threads:
            options.intra_op_num_threads = int(threads)

        session = ort.InferenceSession(str(model_path), sess_options=options, providers=providers)
        actual = session.get_providers()
        resolved = "cuda" if "CUDAExecutionProvider" in actual else "cpu"
        return cls(
            session=session,
            config=PreprocessConfig.from_model_dir(data_root),
            ort_module=ort,
            input_name=session.get_inputs()[0].name,
            device=resolved,
        )

    # ------------------------------------------------------------------ 探测
    def probe(self, *, requested_device: str = "auto") -> ProbeResult:
        """跑一次**真实推理**并计时 —— 这是唯一能证明 CUDA 真的在用的方法。

        为什么不能用 `get_available_providers()`：实测三种状态下输出完全相同。
        为什么不能只看 `session.get_providers()`：cuDNN 版本不对时它**也报 CUDA**，
        直到第一次 Conv 才抛错并自己降级。
        """
        result = ProbeResult(
            providers_available=list(self._ort.get_available_providers()),
            providers_session=list(self._session.get_providers()),
            ort_version=str(getattr(self._ort, "__version__", "")),
        )
        try:
            from importlib.metadata import version as _v

            result.cudnn_version = _v("nvidia-cudnn-cu12")
        except Exception:  # noqa: BLE001
            result.cudnn_version = ""

        dummy = np.zeros((1, 3, self._config.size, self._config.size), dtype=np.float32)
        try:
            # 预热一次（首次调用含 BLAS 线程池唤醒与页错误，计时会偏高）
            self._session.run(None, {self._input_name: dummy})
            started = time.perf_counter()
            for _ in range(3):
                self._session.run(None, {self._input_name: dummy})
            result.ms_per_image = (time.perf_counter() - started) / 3 * 1000
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"推理探测失败：{type(exc).__name__}: {exc}")
            result.device = "cpu"
            result.degraded = requested_device in ("auto", "cuda")
            return result

        result.device = self.device
        if requested_device in ("auto", "cuda") and self.device != "cuda":
            result.degraded = True
            result.notes.append(
                "请求了 CUDA 但实际 provider 里没有 CUDAExecutionProvider ——"
                "检查 onnxruntime-gpu 是否 1.25.1（CUDA 12 构建）与 cuDNN 是否 9.10.2.21"
            )
        return result

    # ------------------------------------------------------------------ 编码
    def preprocess(self, image) -> "np.ndarray":
        """单张预处理（`(3, H, W)` CHW float32）。

        **单独暴露这一步是为了让调用方并行化它**：实测预处理占端到端耗时的 94%
        （推理只占 6%），而 Pillow/numpy 会释放 GIL，因此线程池能带来约 6× 加速。
        见 `library/embed_pipeline.run_embedding`。
        """
        return preprocess_image(image, self._config)

    def embed_arrays(self, arrays: Sequence["np.ndarray"]) -> "np.ndarray":
        """对**已预处理**的数组批量推理（无 IO、无图像解码）。

        与 `preprocess()` 配对使用：调用方在线程池里并行 `preprocess`，
        再在单线程里调用本方法 —— 这样 GPU 只被一个线程驱动（避免并发抢占显存）。
        """
        if not arrays:
            return np.zeros((0, self.dim), dtype="<f4")
        batch = np.stack([np.asarray(a, dtype="<f4") for a in arrays])
        outputs = self._session.run(None, {self._input_name: batch})
        raw = np.asarray(outputs[0], dtype="<f4")
        if raw.ndim != 2 or raw.shape[1] != self.dim:
            raise VectorStoreError(f"模型输出形状异常：{raw.shape}（期望 (N, {self.dim})）")
        return l2_normalize(raw)

    def embed_images(self, images: Sequence[object]) -> "np.ndarray":
        """对一批 PIL 图像编码，返回 `(N, dim)` **已 L2 归一化**的 float32。"""
        if not images:
            return np.zeros((0, self.dim), dtype="<f4")
        return self.embed_arrays([self.preprocess(image) for image in images])

    def embed_paths(self, paths: Sequence[Path]) -> "np.ndarray":
        from PIL import Image

        images = []
        for path in paths:
            with Image.open(path) as handle:
                handle.load()
                images.append(handle.convert("RGB"))
        return self.embed_images(images)

    def embed_paths_batched(
        self,
        paths: Sequence[Path],
        *,
        batch_size: int = DEFAULT_BATCH,
        on_progress=None,
    ) -> "np.ndarray":
        """分批编码，**OOM 时自动降 batch**（PRD §11.1）。

        降级是"降一次就记住"：一旦某批 OOM，后续都按更小的批跑，
        避免每批都先失败一次（GPU OOM 的代价是显存整理，很慢）。
        """
        from PIL import Image

        if not paths:
            return np.zeros((0, self.dim), dtype="<f4")

        current = max(1, int(batch_size))
        out: list["np.ndarray"] = []
        index = 0
        total = len(paths)
        while index < total:
            chunk = paths[index : index + current]
            images = []
            for path in chunk:
                with Image.open(path) as handle:
                    handle.load()
                    images.append(handle.convert("RGB"))
            try:
                out.append(self.embed_images(images))
            except Exception as exc:  # noqa: BLE001 - 只对 OOM 降级，其他异常照抛
                message = str(exc).lower()
                is_oom = "out of memory" in message or "cuda_error_out_of_memory" in message
                smaller = [b for b in OOM_FALLBACK_BATCHES if b < current]
                if not is_oom or not smaller:
                    raise
                current = smaller[0]
                continue
            index += len(chunk)
            if on_progress:
                on_progress(index, total)
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype="<f4")


def create_embedder(data_root: Path, *, device: str = "auto", threads: int | None = None) -> Embedder:
    """工厂：可用则返回 `ClipOnnxEmbedder`，否则返回 `NullEmbedder`（调用时才抛错）。

    为什么不在工厂里抛错：上层（界面、`ai doctor`）需要**能构造出来并展示原因**，
    而不是在装配阶段就崩掉。
    """
    try:
        return ClipOnnxEmbedder.create(data_root, device=device, threads=threads)
    except EmbeddingNotAvailable as exc:
        return NullEmbedder(str(exc))


__all__ = [
    "ClipOnnxEmbedder",
    "DEFAULT_BATCH",
    "Embedder",
    "EmbeddingNotAvailable",
    "FALLBACK_MEAN",
    "FALLBACK_SIZE",
    "FALLBACK_STD",
    "NullEmbedder",
    "OOM_FALLBACK_BATCHES",
    "PreprocessConfig",
    "ProbeResult",
    "create_embedder",
    "preprocess_image",
]
