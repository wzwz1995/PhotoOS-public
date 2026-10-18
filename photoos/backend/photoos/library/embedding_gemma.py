"""EmbeddingGemma 2 编码器（Phase 3 · 中文语义检索用的第二个引擎）。

## 为什么需要第二个引擎（实测依据，见 `PHASE3_PLAN.md` 附录 C）

Phase 2 的 CLIP ViT-B/32 在**视觉相似度**上很好（Scene 划分用它），但**中文查询几乎失效**：
实测 `日出` 的 dawn 富集 **0.00**、中文/英文富集比中位 **0.27**。
EmbeddingGemma 2 把中英跨语言对齐做到 **33–67%**（CLIP 只有 2–18%），代价是慢 7.4 倍。

所以两个引擎**并存**（PRD §9.3 断点 #6「模型升级后旧分析仍可追溯、可并存」）：
CLIP 继续做 Scene 划分，EG2 专门服务中文查询。

## 身份四元组（铁律 #4：派生数据必须可精确重算）

一行的身份是 `(asset_id, model, model_version, prep_version)`：

| 字段 | 值 |
| --- | --- |
| `model` | `embeddinggemma-2` |
| `model_version` | `safetensors-fp32`（权重来自官方 safetensors，**无 ONNX 产物**） |
| `prep_version` | `eg2-p1.0`（处理器默认 resize/normalize，mean pooling，L2 归一化） |
| `dim` | **768**（注意：与 CLIP 的 512 不同，这是并存的危险源，repo 层已按身份过滤） |

## 三个踩过的坑（都写进代码，不写进聊天）

1. **`transformers` 的高层 API 有 bug**：`get_image_features()` 会把 `attention_mask`
   错传给视觉塔（`TypeError: Gemma4VisionEncoder got multiple values for keyword
   argument 'attention_mask'`，实测于 transformers 5.19.0）。
   → 必须走 **`sentence-transformers`**（仓库自带 `modules.json` 指定 mean pooling + normalize）。
2. **FP16 的警告针对文本侧**：模型卡明示 FP16 出 NaN，但**视觉塔用 autocast 是安全的** ——
   实测 AMP fp16 与 fp32 的向量**余弦一致度 = 1.0000、零 NaN**，而吞吐从 3.15 → 7.73 张/秒
   （**2.5×**，全量 113 分 → 46 分）。本模块默认开 AMP。
3. **不做多线程预处理**：CLIP 那条路径靠"6 线程预处理 + 串行推理"拿到 6× 加速，
   但这里 **GPU 前向本身就是瓶颈**（320 ms/张），而且 torch 模型不是线程安全的
   → 本模块**只实现 `embed_paths_batched`**，让管线走串行回退路径（简单且安全）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .model_store import EmbeddingNotAvailable
from .vectorstore import VectorStoreError, l2_normalize

#: 写进 `embedding.model` / `model_version` —— **一旦发布不得更改语义**
MODEL_NAME = "embeddinggemma-2"
MODEL_VERSION = "safetensors-fp32"
MODEL_DIM = 768
MODEL_NORMALIZATION = "l2"
#: 预处理口径版本。任何预处理改动都必须改这个值，否则新旧向量会被混在一起比较。
PREP_VERSION = "eg2-p1.0"

#: 默认批大小。**实测：batch 8 是最优点** ——
#: 16/32 在 6 GB 卡上显存溢出到共享内存，慢 7 倍（2180 ms/张）。见附录 C.2。
DEFAULT_BATCH = 8


@dataclass
class GemmaProbeResult:
    """`ai doctor` / `embed` 用的自检结果（与 `clip` 的 ProbeResult 同形状的关键字段）。"""

    device: str
    ms_per_image: float
    dim: int
    notes: list[str]

    @property
    def summary(self) -> str:
        return f"{self.device}，实测 {self.ms_per_image:.1f} ms/张，{self.dim} 维"


class EmbeddingGemma2Embedder:
    """EmbeddingGemma 2 视觉塔（768 维，L2 归一化，mean pooling）。

    依赖 `sentence-transformers` + `torch`（**不是** onnxruntime）——
    因为官方仓库没有任何 ONNX/GGUF 产物。所以本模块**只在装了它们的 venv 里可用**；
    产品 venv 不装它们也不影响 CLIP 那条路径（这正是"并存"的前提）。
    """

    model = MODEL_NAME
    model_version = MODEL_VERSION
    dim = MODEL_DIM
    normalization = MODEL_NORMALIZATION
    prep_version = PREP_VERSION

    def __init__(self, *, st_model, device: str, amp: bool) -> None:
        self._model = st_model
        self.device = device
        self.amp = amp

    # ------------------------------------------------------------------ 构造
    @classmethod
    def create(cls, data_root: Path, *, device: str = "auto", amp: bool = True, batch_size: int | None = None):
        """按需创建；依赖或模型缺失时抛 `EmbeddingNotAvailable`（**不静默降级**）。"""
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise EmbeddingNotAvailable(
                "未安装 torch / sentence-transformers（可选依赖）。"
                "EmbeddingGemma 2 没有 ONNX 产物，无法走 onnxruntime 路径。"
            ) from exc

        from . import model_store as _ms

        directory = _ms.gemma_model_dir(data_root)
        if not (directory / "model.safetensors").exists():
            raise EmbeddingNotAvailable(
                f"模型缺失：{directory / 'model.safetensors'}。请先运行 `photoos ai fetch-model --engine gemma`"
            )

        resolved = device
        if device == "auto":
            resolved = "cuda" if torch.cuda.is_available() else "cpu"

        # dtype 固定 float32：模型卡明示 FP16 会出 NaN（**整个模型**加载成 fp16 会坏）。
        # 加速靠在推理处用 autocast（视觉塔实测安全，见模块 docstring 第 2 条）。
        st_model = SentenceTransformer(str(directory), device=resolved,
                                       model_kwargs={"dtype": torch.float32})
        return cls(st_model=st_model, device=resolved, amp=amp and resolved == "cuda")

    # ------------------------------------------------------------------ 探测
    def probe(self) -> GemmaProbeResult:
        """跑一次**真实推理**并计时 —— 与 CLIP 那边同一纪律：不许只看 provider 名字。"""
        import torch
        from PIL import Image

        dummy = Image.new("RGB", (640, 427), (128, 128, 128))
        notes: list[str] = []
        if self.device != "cuda":
            notes.append("未使用 CUDA（CPU 上全量 21,400 张会非常慢，实测约 114 小时）")
        if not self.amp:
            notes.append("AMP（fp16 autocast）未启用 → 吞吐约为启用时的 40%")
        with torch.no_grad():
            self._encode_images([dummy])               # 预热：首次前向含算法选择，不计时
            started = time.perf_counter()
            for _ in range(3):
                self._encode_images([dummy])
        ms = (time.perf_counter() - started) / 3 * 1000
        return GemmaProbeResult(device=self.device, ms_per_image=ms, dim=self.dim, notes=notes)

    # ------------------------------------------------------------------ 编码
    def _encode_images(self, images: Sequence[object]) -> "np.ndarray":
        """核心编码：**已 L2 归一化**的 (N, 768) float32。"""
        import torch

        ctx = (torch.autocast(device_type="cuda", dtype=torch.float16)
               if self.amp else _nullcontext())
        with ctx, torch.no_grad():
            emb = self._model.encode(
                list(images), batch_size=len(images), convert_to_numpy=True,
                show_progress_bar=False, normalize_embeddings=True,
            )
        out = np.asarray(emb, dtype="<f4")
        if out.ndim != 2 or out.shape[1] != self.dim:
            raise VectorStoreError(f"模型输出形状异常：{out.shape}（期望 (N, {self.dim})）")
        # `normalize_embeddings=True` 已经归一化过；这里再归一化一次是**幂等的**，
        # 但不省略 —— 归一化是整个检索的前提，宁可重复也不能漏。
        return l2_normalize(out)

    def encode_texts(self, texts: Sequence[str], *, kind: str = "query") -> "np.ndarray":
        """把查询文本编码成 (N, 768)。

        `kind="query"` 用官方前缀 `task: search result | query: `（`encode_query`）；
        `kind="document"` 用 `title: none | text: `。**前缀对检索排序的影响约 0.009 MRR**
        （外部实测），但为了与官方用法一致仍然保留 —— 换前缀属于**口径变更**，
        必须同时改 `PREP_VERSION`。
        """
        import torch

        if kind not in ("query", "document"):
            raise ValueError(f"kind 必须是 query 或 document，实际 {kind!r}")
        ctx = (torch.autocast(device_type="cuda", dtype=torch.float16)
               if self.amp else _nullcontext())
        with ctx, torch.no_grad():
            emb = (self._model.encode_query(list(texts), convert_to_numpy=True,
                                            show_progress_bar=False, normalize_embeddings=True)
                   if kind == "query" else
                   self._model.encode_document(list(texts), convert_to_numpy=True,
                                               show_progress_bar=False,
                                               normalize_embeddings=True))
        out = np.asarray(emb, dtype="<f4")
        if out.ndim == 1:
            out = out[None, :]
        if out.shape[1] != self.dim:
            raise VectorStoreError(f"文本向量维度异常：{out.shape}（期望 (N, {self.dim})）")
        return l2_normalize(out)

    def embed_images(self, images: Sequence[object]) -> "np.ndarray":
        """对一批 PIL 图像编码（与 `ClipOnnxEmbedder.embed_images` 同签名）。"""
        if not images:
            return np.zeros((0, self.dim), dtype="<f4")
        return self._encode_images(images)

    def embed_paths_batched(
        self, paths: Sequence[Path], *, batch_size: int = DEFAULT_BATCH, on_progress=None
    ) -> "np.ndarray":
        """分批读图 + 编码（管线串行分支调用的入口）。

        **不支持"预处理与推理分离"**：本类刻意不实现 `preprocess`，
        于是 `run_embedding` 会走 `_run_embedding_serial` —— 这是有意的，
        因为 (a) torch 模型不是线程安全的，(b) 这里 GPU 前向本身就是瓶颈。
        """
        from PIL import Image

        if not paths:
            return np.zeros((0, self.dim), dtype="<f4")
        batch = max(1, int(batch_size))
        out: list[np.ndarray] = []
        for start in range(0, len(paths), batch):
            chunk = paths[start : start + batch]
            images = []
            for path in chunk:
                with Image.open(path) as handle:
                    handle.load()
                    images.append(handle.convert("RGB"))
            out.append(self._encode_images(images))
            if on_progress:
                on_progress(min(start + len(chunk), len(paths)), len(paths))
        return np.vstack(out)

    def embed_paths(self, paths: Sequence[Path]) -> "np.ndarray":
        return self.embed_paths_batched(paths)


class _nullcontext:
    """标准库 `contextlib.nullcontext` 的极简替代（避免多一次 import 开销）。"""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def create_gemma_embedder(data_root: Path, *, device: str = "auto", amp: bool = True):
    """工厂：可用则返回实例，否则抛 `EmbeddingNotAvailable`。

    与 CLIP 那边的 `create_embedder` 不同，这里**不返回 Null 对象** ——
    因为调用方（`embed --engine gemma` / `search`）需要明确的失败原因。
    """
    return EmbeddingGemma2Embedder.create(data_root, device=device, amp=amp)


__all__ = [
    "DEFAULT_BATCH",
    "EmbeddingGemma2Embedder",
    "GemmaProbeResult",
    "MODEL_DIM",
    "MODEL_NAME",
    "MODEL_NORMALIZATION",
    "MODEL_VERSION",
    "PREP_VERSION",
    "create_gemma_embedder",
]
