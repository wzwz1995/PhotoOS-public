"""文件类型探测 —— **以内容为准，扩展名只做兜底**（PRD §8）。

为什么不能只看扩展名：照片库常出现被改名、无扩展名、扩展名与实际内容不符的文件
（真实库里就有 `DSC_4051 - 副本 (2).NEF` 这类中文后缀文件）。只看扩展名会让这些文件
要么被跳过、要么被送进错误的解码器。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import IGNORED_EXTENSIONS, PHOTO_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS

#: 探测时最多读取多少字节（足够覆盖所有魔数）
SNIFF_BYTES = 512


@dataclass(frozen=True)
class DetectedType:
    """探测结果。`file_type` 取值与 `asset.file_type` 的 CHECK 约束一致。"""

    file_type: str          # raw|jpeg|heic|png|tiff|webp|video|other
    mime: str
    extension: str
    by_content: bool        # True = 由魔数判定，False = 仅由扩展名判定
    container: str | None = None   # RAW 家族名或视频容器，便于诊断


#: TIFF 家族（RAW 与普通 TIFF 共享魔数，需要靠扩展名/内部标签区分）
_TIFF_RAW_HINTS: dict[str, tuple[str, str]] = {
    ".nef": ("raw", "image/x-nikon-nef"),
    ".nrw": ("raw", "image/x-nikon-nrw"),
    ".cr2": ("raw", "image/x-canon-cr2"),
    ".crw": ("raw", "image/x-canon-crw"),
    ".arw": ("raw", "image/x-sony-arw"),
    ".srf": ("raw", "image/x-sony-srf"),
    ".sr2": ("raw", "image/x-sony-sr2"),
    ".raf": ("raw", "image/x-fuji-raf"),
    ".orf": ("raw", "image/x-olympus-orf"),
    ".rw2": ("raw", "image/x-panasonic-rw2"),
    ".pef": ("raw", "image/x-pentax-pef"),
    ".dng": ("raw", "image/x-adobe-dng"),
    ".srw": ("raw", "image/x-samsung-srw"),
    ".3fr": ("raw", "image/x-hasselblad-3fr"),
    ".iiq": ("raw", "image/x-phaseone-iiq"),
    ".rwl": ("raw", "image/x-leica-rwl"),
    # `.raw` 是若干厂商共用的裸扩展名（松下/徕卡等），TIFF 头的那些靠这里路由；
    # 非 TIFF 头的 `.raw` 由 `_detect_by_extension` 兜底（同为 raw）。
    ".raw": ("raw", "image/x-raw"),
}

#: **非 TIFF 容器**的 RAW：`.cr3` 是 ISO-BMFF（`ftyp` + `crx ` 品牌），和 mp4 同一套封装。
#: 它曾经只在 `RAW_EXTENSIONS` 白名单里、却没有路由 —— 于是被判成 `other`，
#: 通用 Pillow 解不出来，照片能进库但缩略图/预览是空的（见 20-reference Q-36）。
_CONTAINER_RAW_HINTS: dict[str, tuple[str, str, str]] = {
    ".cr3": ("raw", "image/x-canon-cr3", "cr3"),
}


def _sniff(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(SNIFF_BYTES)
    except OSError:
        return b""


def detect_type_from_bytes(
    head: bytes, *, filename: str = "", declared_size: int = 0
) -> DetectedType | None:
    """仅凭前若干字节 + 文件名判定；无法判定返回 None。"""
    ext = Path(filename).suffix.lower()

    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        # 佳能 CR3：ISO-BMFF 封装，品牌 `crx `。必须在 heic/video 分支之前认出来，
        # 否则会落到下面的通用分支再退回扩展名，最终被判成 `other`。
        if brand == b"crx ":
            return DetectedType("raw", "image/x-canon-cr3", ext or ".cr3", True, "cr3")
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"):
            return DetectedType("heic", "image/heic", ext or ".heic", True, brand.decode("ascii"))
        if brand in (b"qt  ", b"isom", b"mp42", b"mp41", b"M4V ", b"M4A ", b"3gp4", b"avc1"):
            return DetectedType("video", "video/quicktime" if brand == b"qt  " else "video/mp4",
                                ext or ".mov", True, brand.decode("ascii", "replace").strip())

    if head.startswith(b"\xff\xd8\xff"):
        return DetectedType("jpeg", "image/jpeg", ext or ".jpg", True)

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedType("png", "image/png", ext or ".png", True)

    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return DetectedType("other", "image/gif", ext or ".gif", True)

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return DetectedType("webp", "image/webp", ext or ".webp", True)

    if head.startswith(b"BM"):
        return DetectedType("other", "image/bmp", ext or ".bmp", True)

    if head.startswith((b"II*\x00", b"MM\x00*")):
        # TIFF 家族：RAW 与普通 TIFF 共用魔数。扩展名是唯一可用的区分线索；
        # 真正的"是不是 RAW"由解码器（LibRaw）确认，这里只做路由。
        if ext in _TIFF_RAW_HINTS:
            file_type, mime = _TIFF_RAW_HINTS[ext]
            return DetectedType(file_type, mime, ext, True, ext.lstrip("."))
        if ext in (".tif", ".tiff"):
            return DetectedType("tiff", "image/tiff", ext, True, "tiff")
        return DetectedType("tiff", "image/tiff", ext or ".tif", True, "tiff-unknown-ext")

    if head.startswith(b"FUJIFILM"):
        return DetectedType("raw", "image/x-fuji-raf", ext or ".raf", True, "raf")

    if head.startswith(b"8BPS"):
        return DetectedType("other", "image/vnd.adobe.photoshop", ext or ".psd", True)

    if head[:4] in (b"\x1aE\xdf\xa3",) or head.startswith(b"\x1a\x45\xdf\xa3"):
        return DetectedType("video", "video/x-matroska", ext or ".mkv", True, "matroska")

    if head.startswith(b"AVI ") or (len(head) > 12 and head[8:12] == b"AVI "):
        return DetectedType("video", "video/x-msvideo", ext or ".avi", True, "avi")

    return None


def _detect_by_extension(ext: str) -> DetectedType | None:
    if ext in _TIFF_RAW_HINTS:
        file_type, mime = _TIFF_RAW_HINTS[ext]
        return DetectedType(file_type, mime, ext, False, ext.lstrip("."))
    if ext in _CONTAINER_RAW_HINTS:
        file_type, mime, container = _CONTAINER_RAW_HINTS[ext]
        return DetectedType(file_type, mime, ext, False, container)
    if ext in (".tif", ".tiff"):
        return DetectedType("tiff", "image/tiff", ext, False, "tiff")
    if ext in {".jpg", ".jpeg", ".jpe"}:
        return DetectedType("jpeg", "image/jpeg", ext, False)
    if ext in {".heic", ".heif", ".avif"}:
        return DetectedType("heic", "image/heic", ext, False)
    if ext == ".png":
        return DetectedType("png", "image/png", ext, False)
    if ext == ".webp":
        return DetectedType("webp", "image/webp", ext, False)
    if ext in VIDEO_EXTENSIONS:
        return DetectedType("video", "video/mp4" if ext in {".mp4", ".m4v"} else "video/quicktime",
                            ext, False)
    if ext in IGNORED_EXTENSIONS:
        return None
    if ext in PHOTO_EXTENSIONS:
        return DetectedType("other", "application/octet-stream", ext, False)
    return None


def detect_type(path: Path) -> DetectedType | None:
    """探测一个文件的类型（会读取文件头，因此**每个文件一次随机读**）。

    适用场景：元数据/预览阶段 —— 那里本来就要读文件。
    **不适用于遍历阶段**（16k 次随机读在外置盘上会拖成十几分钟），
    遍历请用 `detect_by_extension_only()`。
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in IGNORED_EXTENSIONS:
        return None

    head = _sniff(path)
    if head:
        detected = detect_type_from_bytes(head, filename=path.name)
        if detected is not None:
            return detected
    if ext in RAW_EXTENSIONS:
        # RAW 可能不使用标准 TIFF 头（例如部分厂商封装）；扩展名在此作为兜底，
        # 最终由 LibRaw 确认能否解码。
        return _detect_by_extension(ext)
    return _detect_by_extension(ext)


def detect_by_extension_only(path: Path) -> DetectedType | None:
    """仅凭扩展名推断类型，**不打开文件**。

    用于遍历/发现阶段：`walker` 已经按扩展名筛出了媒体文件，此时再为每个文件做一次
    随机读毫无收益，只会把 1 秒的遍历变成十几分钟。真实类型在元数据阶段由
    `detect_type()`（魔数优先）确认；即使扩展名不准，也只会晚一点被发现，不会丢文件。
    """
    ext = Path(path).suffix.lower()
    if ext in IGNORED_EXTENSIONS:
        return None
    return _detect_by_extension(ext)


def is_media(path: Path) -> bool:
    return detect_type(path) is not None
