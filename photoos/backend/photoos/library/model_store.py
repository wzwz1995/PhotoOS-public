"""嵌入模型的获取与校验（Phase 2 · M3）。

## 模型身份（可复现下载）

| 文件 | 字节 | sha256 |
| --- | ---: | --- |
| `onnx/vision_model.onnx` | 351,685,709 | `fd6e1402a588279d1723c7534d4bcba5bc0b14b47dfab0e46f8c47b8270d7d40` |
| `preprocessor_config.json` | 520 | `6f638fb9401a6d6296feff533ee7efe657b787c49f954f82f5906b36ef2a1b1f` |

仓库：`Xenova/clip-vit-base-patch32`（transformers.js 官方转出的 ONNX 版本，
下载量 20 万+）。注意 **`openai/clip-vit-base-patch32` 本身没有 onnx 目录**（实测 404）。

**只下载 vision tower**：本期不需要文本塔，省下 242.3 MB。

## 两个实测踩出来的坑（写在代码里，不写在文档里）

1. **必须带 `User-Agent`**：hf-mirror 对无 UA 的**小文件**请求返回 **403**
   （实测：335 MB 的 onnx 一次成功，520 B 的 json 稳定 403）。
2. **不能用 ETag 当校验和**：实测 ETag（`581a113c…`）与 sha256（`fd6e1402…`）**不同**。

## 为什么模型要落在 `data_root/ai_cache/models/`

铁律 #4 要求派生数据「能**精确重算**」。能精确重算的前提是**模型本身可得且有校验和**。
放在 `data_root` 下的好处：随数据根整体备份/迁移（`ai_cache` 已在
`library/migration.py` 的 `DEFAULT_INCLUDE_DIRS` 里），且不依赖 venv 的生死。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: 模型标识（写进 `embedding.model` / `model_version`，一旦发布不得更改语义）
MODEL_NAME = "clip-vit-base-patch32"
MODEL_VERSION = "xenova-onnx-fp32"
MODEL_DIM = 512
MODEL_NORMALIZATION = "l2"
#: 预处理口径版本。**任何**预处理改动（含 resize 算法）都必须改这个值，
#: 否则新旧向量会被混在一起比较。
PREP_VERSION = "p1.0"

#: 参与下载的文件：[仓库内路径, 落地文件名, 字节数, sha256]
MODEL_FILES: tuple[tuple[str, str, int, str], ...] = (
    (
        "onnx/vision_model.onnx",
        "vision_model.onnx",
        351_685_709,
        "fd6e1402a588279d1723c7534d4bcba5bc0b14b47dfab0e46f8c47b8270d7d40",
    ),
    (
        "preprocessor_config.json",
        "preprocessor_config.json",
        520,
        "6f638fb9401a6d6296feff533ee7efe657b787c49f954f82f5906b36ef2a1b1f",
    ),
)

#: 镜像基址（hf-mirror 实测 30.8 MB/s；huggingface.co 本机不可达）
DEFAULT_BASE_URL = "https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/main/"

#: 实测：不带 UA 会被 hf-mirror 以 403 拒绝小文件
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PhotoOS/0.2"


class EmbeddingNotAvailable(RuntimeError):
    """嵌入功能不可用（运行时缺失 / 模型缺失 / 模型损坏 / 预处理配置损坏）。

    **只有这一个异常类型**，并且在 `embedding.py` 里被 re-export ——
    调用方只需要 `except EmbeddingNotAvailable` 就能覆盖全部缺依赖的情形。

    合并成一个类型是刻意的：早先这里叫 `ModelNotAvailable`，
    `embedding.py` 里另有一个语义相同的类，导致「按 A 捕获却抛出 B」——
    调用方写了一处 except 却漏掉另一条路径。
    """


@dataclass(frozen=True)
class ModelFile:
    remote: str
    local: str
    size: int
    sha256: str


@dataclass
class CheckResult:
    path: Path
    ok: bool
    detail: str


def model_dir(data_root: Path) -> Path:
    """模型落地目录：`<data_root>/ai_cache/models/<model>-<version>/`。"""
    return Path(data_root) / "ai_cache" / "models" / f"{MODEL_NAME}-{MODEL_VERSION}"


# --------------------------------------------------------------------------- EmbeddingGemma 2
#
# Phase 3 的第二个引擎（中文语义检索）。**与 CLIP 完全独立**：
#   * CLIP 走 `onnxruntime` + 单个 335 MB 的 ONNX 文件
#   * EG2 **没有 ONNX/GGUF 产物**，只能走 `torch` + `sentence-transformers`
#     （仓库里只有 `model.safetensors`）
# 所以两者**互不影响**：产品 venv 不装 torch 也能继续用 CLIP 那条路径。

GEMMA_MODEL_NAME = "embeddinggemma-2"
GEMMA_MODEL_VERSION = "safetensors-fp32"   # safetensors 是 fp32；fp16 会出 NaN（模型卡明示）
GEMMA_MODEL_DIM = 768
GEMMA_MODEL_NORMALIZATION = "l2"
GEMMA_PREP_VERSION = "eg2-p1.0"

#: EG2 权重（`model.safetensors`）的**实测**字节数与 sha256。
#: 来源：ModelScope `google/embeddinggemma-2`（本机实测 11.7 MB/s；hf-mirror 同有该仓库）。
#: 官方 `huggingface.co` 在本机不可达，所以默认基址指向 ModelScope。
GEMMA_MODEL_FILE = ("model.safetensors", 1_488_915_288,
                    "197a32965d4b1105faf060417baa899e193fb73cd401f42ec9295234d5553d79")
#: 随模型一起落地的配置文件（tokenizer / processor / sentence-transformers 模块图）
GEMMA_EXTRA_FILES = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json",
                     "config.json", "preprocessor_config.json", "processor_config.json",
                     "sentence_bert_config.json", "config_sentence_transformers.json",
                     "modules.json", "chat_template.jinja",
                     # ⚠️ 少了这两个，sentence-transformers 6.x 建 Pooling 时会报
                     # `Pooling.__init__() missing 'embedding_dimension'`（实测踩到）
                     "1_Pooling/config.json", "2_Normalize/config.json")
GEMMA_DEFAULT_BASE_URL = (
    "https://modelscope.cn/api/v1/models/google/embeddinggemma-2/repo?Revision=master&FilePath="
)


def gemma_model_dir(data_root: Path) -> Path:
    """EG2 的落地目录：`<data_root>/ai_cache/models/embeddinggemma-2-safetensors-fp32/`。"""
    return Path(data_root) / "ai_cache" / "models" / f"{GEMMA_MODEL_NAME}-{GEMMA_MODEL_VERSION}"


def verify_gemma(data_root: Path, *, deep: bool = False) -> list[CheckResult]:
    """检查 EG2 模型是否就位。

    `deep=True` 时算 sha256（1.49 GB，约 4 秒）。默认只比字节数 —— 因为
    `photoos search` 每次启动都调用本函数，算 sha256 会白白多花 4 秒；
    显式校验留给 `ai fetch-model --engine gemma` 与 `ai doctor`。
    """
    directory = gemma_model_dir(data_root)
    local, size, want = GEMMA_MODEL_FILE
    target = directory / local
    if not target.exists():
        return [CheckResult(target, False, "文件缺失")]
    actual_size = target.stat().st_size
    if actual_size != size:
        return [CheckResult(target, False, f"字节数不符：期望 {size}，实际 {actual_size}")]
    if deep:
        actual = sha256_file(target)
        if actual != want:
            return [CheckResult(target, False,
                                f"sha256 不符：期望 {want[:16]}…，实际 {actual[:16]}…")]
        return [CheckResult(target, True, f"{size:,} 字节，sha256 一致")]
    return [CheckResult(target, True, f"{size:,} 字节")]


def is_gemma_ready(data_root: Path, *, deep: bool = False) -> bool:
    return all(item.ok for item in verify_gemma(data_root, deep=deep))



def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- 人脸模型（Phase 4）
#
# YuNet（检测）+ SFace（识别），都是 OpenCV Zoo 的官方 ONNX 产物，
# 由 `cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF` 直接加载。
#
# ## 为什么走 GitHub 而不是模型镜像
#
# 这两个文件的官方托管在 `opencv/opencv_zoo` 仓库里（LFS 大文件）。
# 实测两个坑：
#   1. `githubusercontent.com` 上的 LFS 文件返回的是 **131 字节的指针文本**，
#      不是权重 —— 下载"成功"但模型加载失败，且大小对不上时才会被发现；
#   2. 必须走 `media.githubusercontent.com/media/...` 才是真实字节。
# 所以这里的 sha256 是**实测值**（下载下来算的），URL 也用 media 子域。
#
# 为什么人脸模型与 CLIP/EG2 放在**不同子目录**：三者生命周期完全独立
# （换人脸模型不该动到 Scene 划分），混在一起会让 `ai doctor` 的分项报告失去意义。

FACE_DETECTOR = "yunet-2023mar"
FACE_MODEL_NAME = "sface-2021dec"
#: 人脸模型目录名。两个模型同属"分人"这一个功能，所以共用一个目录。
FACE_DIR_NAME = "face-yunet2023mar-sface2021dec"

#: [落地文件名, 仓库内相对路径, 字节数, sha256]
FACE_MODEL_FILES: tuple[tuple[str, str, int, str], ...] = (
    ("face_detection_yunet_2023mar.onnx",
     "face_detection_yunet/face_detection_yunet_2023mar.onnx", 232_589,
     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    ("face_recognition_sface_2021dec.onnx",
     "face_recognition_sface/face_recognition_sface_2021dec.onnx", 38_696_353,
     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
)

FACE_DEFAULT_BASE_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
)


def face_model_dir(data_root: Path) -> Path:
    """人脸模型的落地目录：`<data_root>/ai_cache/models/face-yunet2023mar-sface2021dec/`。"""
    return Path(data_root) / "ai_cache" / "models" / FACE_DIR_NAME


def verify_face(data_root: Path, *, deep: bool = False) -> list[CheckResult]:
    """检查人脸模型是否就位（默认只比字节数；`deep=True` 才算 sha256）。"""
    directory = face_model_dir(data_root)
    out: list[CheckResult] = []
    for local, _remote, size, want in FACE_MODEL_FILES:
        target = directory / local
        if not target.exists():
            out.append(CheckResult(target, False, "文件缺失"))
            continue
        actual_size = target.stat().st_size
        if actual_size != size:
            out.append(CheckResult(target, False, f"字节数不符：期望 {size}，实际 {actual_size}"))
            continue
        if deep:
            actual = sha256_file(target)
            if actual != want:
                out.append(CheckResult(
                    target, False, f"sha256 不符：期望 {want[:16]}…，实际 {actual[:16]}…"))
                continue
        out.append(CheckResult(target, True,
                               f"{size:,} 字节" + ("，sha256 一致" if deep else "")))
    return out


def is_face_ready(data_root: Path, *, deep: bool = False) -> bool:
    return all(item.ok for item in verify_face(data_root, deep=deep))


def fetch_face(
    data_root: Path,
    *,
    base_url: str = FACE_DEFAULT_BASE_URL,
    force: bool = False,
    local_dir: Path | None = None,
    on_progress=None,
) -> list[CheckResult]:
    """下载人脸模型并逐个校验 sha256。

    `local_dir` 是**离线导入**通道：已经手工下载好的目录（例如项目里
    `scratch/models/face/`）可以就地复制，**字节校验仍然照做** ——
    "本地已有"绝不等于"跳过校验"，否则校验形同虚设。
    """
    directory = face_model_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    out: list[CheckResult] = []
    for local, remote, size, want in FACE_MODEL_FILES:
        target = directory / local
        if not force and target.exists() and target.stat().st_size == size \
                and sha256_file(target) == want:
            out.append(CheckResult(target, True, "已存在且校验通过（跳过下载）"))
            if on_progress:
                on_progress(local, "skip")
            continue

        if on_progress:
            on_progress(local, "start")
        source = (Path(local_dir) / local) if local_dir else None
        if source is not None and source.exists():
            shutil.copyfile(source, target)
        else:
            try:
                _download(base_url + remote, target)
            except urllib.error.HTTPError as exc:
                out.append(CheckResult(target, False, f"下载失败 HTTP {exc.code}（{local}）"))
                continue
            except Exception as exc:  # noqa: BLE001
                out.append(CheckResult(target, False, f"下载失败：{type(exc).__name__}: {exc}"))
                continue

        actual = sha256_file(target)
        if actual != want:
            target.unlink(missing_ok=True)
            out.append(CheckResult(
                target, False,
                f"sha256 校验失败（已删除）：期望 {want[:16]}…，实际 {actual[:16]}…"))
            if on_progress:
                on_progress(local, "fail")
            continue
        out.append(CheckResult(target, True, f"已就位并校验通过（{size:,} 字节）"))
        if on_progress:
            on_progress(local, "done")
    return out


#: 文本塔：把查询词编码成 512 维向量。没有它就只能"以图搜图"，
#: 不能"用文字找照片" —— 所以它是语义搜索的必需件，单独一个组件。
TEXT_MODEL_FILES: tuple[tuple[str, str], ...] = (
    ("onnx/text_model.onnx", "text_model.onnx"),
)


def text_model_path(data_root: Path) -> Path:
    return model_dir(data_root) / "text_model.onnx"


def is_text_ready(data_root: Path) -> bool:
    """文本塔是否就位。只比文件存在 + 能被 onnxruntime 打开（不跑推理）。"""
    path = text_model_path(data_root)
    if not path.is_file() or path.stat().st_size < 100 * 1024 * 1024:
        return False
    try:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        return len(session.get_inputs()) >= 1
    except Exception:  # noqa: BLE001
        return False


def fetch_text(data_root: Path, *, base_url: str = DEFAULT_BASE_URL, on_progress=None) -> list[CheckResult]:
    """下载文本塔。**没有官方 sha256**，所以用"能否被 onnxruntime 打开"作为校验。"""
    directory = model_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    out: list[CheckResult] = []
    for remote, local in TEXT_MODEL_FILES:
        target = directory / local
        if target.is_file() and target.stat().st_size > 100 * 1024 * 1024:
            out.append(CheckResult(target, True, "已存在（跳过下载）"))
            if on_progress:
                on_progress(local, "skip")
            continue
        if on_progress:
            on_progress(local, "start")
        try:
            _download(base_url + remote, target)
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(target, False, f"下载失败：{type(exc).__name__}: {exc}"))
            continue
        if not is_text_ready(data_root):
            target.unlink(missing_ok=True)
            out.append(CheckResult(target, False, "下载后无法被 onnxruntime 打开（已删除）"))
            if on_progress:
                on_progress(local, "fail")
            continue
        out.append(CheckResult(target, True, f"已就位（{target.stat().st_size:,} 字节）"))
        if on_progress:
            on_progress(local, "done")
    return out


def verify(data_root: Path, *, deep: bool = True) -> list[CheckResult]:
    """检查模型文件是否就位。

    `deep=True` 时**逐文件算 sha256**（335 MB 约 1 秒，值得）——
    只比字节数会漏掉「文件被截断/损坏但大小凑巧一致」的情况。
    """
    directory = model_dir(data_root)
    out: list[CheckResult] = []
    for remote, local, size, want in MODEL_FILES:
        target = directory / local
        if not target.exists():
            out.append(CheckResult(target, False, "文件缺失"))
            continue
        actual_size = target.stat().st_size
        if actual_size != size:
            out.append(CheckResult(target, False, f"字节数不符：期望 {size}，实际 {actual_size}"))
            continue
        if deep:
            actual = sha256_file(target)
            if actual != want:
                out.append(
                    CheckResult(target, False, f"sha256 不符：期望 {want[:16]}…，实际 {actual[:16]}…")
                )
                continue
        out.append(CheckResult(target, True, f"{size:,} 字节" + ("，sha256 一致" if deep else "")))
    return out


def is_ready(data_root: Path, *, deep: bool = False) -> bool:
    return all(item.ok for item in verify(data_root, deep=deep))


def vision_model_path(data_root: Path) -> Path:
    return model_dir(data_root) / "vision_model.onnx"


def preprocessor_config_path(data_root: Path) -> Path:
    return model_dir(data_root) / "preprocessor_config.json"


def load_preprocessor_config(data_root: Path) -> dict:
    """读 `preprocessor_config.json`。

    **不硬编码 mean/std**：它们属于模型的一部分，读文件才能保证
    「预处理口径」与「权重版本」始终配套（`prep_version` 记录的正是这个口径）。
    """
    path = preprocessor_config_path(data_root)
    if not path.exists():
        raise EmbeddingNotAvailable(f"缺少 {path.name}，请先运行 `photoos ai fetch-model`")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EmbeddingNotAvailable(f"{path.name} 无法解析：{exc}") from exc


def _download(url: str, target: Path, *, timeout: float = 180.0) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".photoos-part")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(part, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)
        part.replace(target)
    except Exception:
        part.unlink(missing_ok=True)
        raise


def fetch(
    data_root: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    force: bool = False,
    on_progress=None,
) -> list[CheckResult]:
    """下载缺失（或校验失败的）模型文件，并**逐个校验 sha256**。

    下载到 `.photoos-part` 再原子改名 —— 中途失败不留半成品
    （与 Phase 1 的导入落位同一套纪律）。
    """
    directory = model_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    out: list[CheckResult] = []
    for remote, local, size, want in MODEL_FILES:
        target = directory / local
        if target.exists() and not force:
            actual_size = target.stat().st_size
            if actual_size == size and sha256_file(target) == want:
                out.append(CheckResult(target, True, "已存在且校验通过（跳过下载）"))
                if on_progress:
                    on_progress(local, "skip")
                continue
        if on_progress:
            on_progress(local, "start")
        try:
            _download(base_url + remote, target)
        except urllib.error.HTTPError as exc:
            # 403 的成因基本只有一个：没带 UA（见模块 docstring）
            hint = "（403：请检查请求是否带 User-Agent）" if exc.code == 403 else ""
            out.append(CheckResult(target, False, f"下载失败 HTTP {exc.code} {hint}"))
            continue
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(target, False, f"下载失败：{type(exc).__name__}: {exc}"))
            continue
        actual = sha256_file(target)
        if actual != want:
            target.unlink(missing_ok=True)
            out.append(
                CheckResult(
                    target, False, f"sha256 校验失败（已删除）：期望 {want[:16]}…，实际 {actual[:16]}…"
                )
            )
        else:
            out.append(CheckResult(target, True, f"已下载并校验通过（{size:,} 字节）"))
        if on_progress:
            on_progress(local, "done")
    return out


def fetch_gemma(
    data_root: Path,
    *,
    base_url: str = GEMMA_DEFAULT_BASE_URL,
    force: bool = False,
    on_progress=None,
) -> list[CheckResult]:
    """下载 EG2 权重与配置，**逐个校验**；失败不留残骸。

    与 `fetch()`（CLIP）是**两套独立实现**，因为两者的 URL 形状不同：
    ModelScope 的直链是 `.../repo?Revision=master&FilePath=<相对路径>`
    （文件路径是**查询参数**，不是路径段）。

    **人工离线导入也支持**：只要文件已经躺在 `gemma_model_dir()` 里且字节数正确，
    本函数会跳过下载（`--force` 可强制重下）。
    """
    directory = gemma_model_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    out: list[CheckResult] = []

    targets = [GEMMA_MODEL_FILE] + [(name, None, None) for name in GEMMA_EXTRA_FILES]
    for local, size, want in targets:
        target = directory / local
        if target.exists() and not force:
            if size is None or target.stat().st_size == size:
                out.append(CheckResult(target, True, "已存在（跳过下载）"))
                if on_progress:
                    on_progress(local, "skip")
                continue
        if on_progress:
            on_progress(local, "start")
        try:
            _download(base_url + local, target)
        except urllib.error.HTTPError as exc:
            out.append(CheckResult(target, False, f"下载失败 HTTP {exc.code}（{local}）"))
            continue
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(target, False, f"下载失败：{type(exc).__name__}: {exc}"))
            continue
        if want is not None:
            actual = sha256_file(target)
            if actual != want:
                target.unlink(missing_ok=True)
                out.append(CheckResult(
                    target, False,
                    f"sha256 校验失败（已删除）：期望 {want[:16]}…，实际 {actual[:16]}…"))
                if on_progress:
                    on_progress(local, "fail")
                continue
        out.append(CheckResult(target, True, f"已下载（{target.stat().st_size:,} 字节）"))
        if on_progress:
            on_progress(local, "done")
    return out


__all__ = [
    "CheckResult",
    "DEFAULT_BASE_URL",
    "EmbeddingNotAvailable",
    "FACE_DEFAULT_BASE_URL",
    "FACE_DETECTOR",
    "FACE_DIR_NAME",
    "FACE_MODEL_FILES",
    "FACE_MODEL_NAME",
    "GEMMA_DEFAULT_BASE_URL",
    "GEMMA_EXTRA_FILES",
    "GEMMA_MODEL_DIM",
    "GEMMA_MODEL_FILE",
    "GEMMA_MODEL_NAME",
    "GEMMA_MODEL_NORMALIZATION",
    "GEMMA_MODEL_VERSION",
    "GEMMA_PREP_VERSION",
    "MODEL_DIM",
    "MODEL_FILES",
    "MODEL_NAME",
    "MODEL_NORMALIZATION",
    "MODEL_VERSION",
    "PREP_VERSION",
    "face_model_dir",
    "fetch",
    "fetch_face",
    "fetch_gemma",
    "gemma_model_dir",
    "is_face_ready",
    "is_gemma_ready",
    "is_ready",
    "load_preprocessor_config",
    "model_dir",
    "preprocessor_config_path",
    "sha256_file",
    "verify",
    "verify_face",
    "verify_gemma",
    "vision_model_path",
]
