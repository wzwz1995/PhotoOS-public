"""适配层统一入口：`decode_capabilities / extract_metadata / load_image / generate_previews`。

性能关键（PHASE1_TECH_PLAN §9）：
  * RAW **优先走相机内嵌 JPEG 预览**（实测每张 1~8 ms），只有内嵌预览不可用或尺寸不足时
    才做半尺寸去马赛克（实测 ~0.5 s/张）。10 万张规模下这是数量级差异。
  * 缩略图/预览一律转 WebP，路径按 asset_id 两级分片，避免单目录堆积十万文件。
  * 每张预览都记录 `generator_version`，未来升级可精确重算而**不动原片**。

纪律：任何单张失败都抛 `MediaError` 子类，由 Job 层记录后继续处理其他文件（PRD §11.1）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ..config import (
    GENERATOR_VERSION,
    PREVIEW_FORMAT,
    PREVIEW_MAX_EDGE,
    PREVIEW_QUALITY,
    THUMBNAIL_MAX_EDGE,
)
from ..errors import DecodeError, MediaError, UnsupportedFormat
from .detect import DetectedType, detect_type
from .metadata import MediaInfo, extract_metadata  # noqa: F401  (对外统一出口)

# 让 Pillow 能打开 HEIC（不依赖系统 HEIF 扩展）
try:  # pragma: no cover - 环境相关
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass

#: Pillow 安全上限，避免损坏文件声明超大尺寸导致内存爆炸
Image.MAX_IMAGE_PIXELS = 500_000_000


@dataclass(frozen=True)
class DecodeCapability:
    """某个文件能否解码、走哪条路径。`fast_path` 为 True 表示走了内嵌预览。"""

    file_type: str
    decoder: str
    fast_path: bool = False
    note: str = ""


@dataclass(frozen=True)
class Previews:
    """生成结果。`path` 均为**相对 data_root** 的路径（数据库不存绝对路径）。"""

    thumb_path: str
    thumb_width: int
    thumb_height: int
    thumb_bytes: int
    preview_path: str
    preview_width: int
    preview_height: int
    preview_bytes: int
    fast_path: bool
    decoder: str
    generator_version: str = GENERATOR_VERSION

    @property
    def kind_note(self) -> str:
        return f"{self.decoder}{'(内嵌快路径)' if self.fast_path else ''}"


# --------------------------------------------------------------------- 能力探测
def decode_capabilities(path: Path) -> DecodeCapability:
    """判断解码路径（不做实际解码，因此很快）。"""
    detected = detect_type(path)
    if detected is None:
        raise UnsupportedFormat(f"无法识别的文件类型：{Path(path).name}")
    mapping = {
        "raw": ("rawpy/LibRaw", True),
        "heic": ("pillow-heif/libheif", False),
        "jpeg": ("Pillow", False),
        "png": ("Pillow", False),
        "tiff": ("Pillow", False),
        "webp": ("Pillow", False),
        "video": ("ffmpeg", False),
        "other": ("Pillow", False),
    }
    decoder, fast = mapping.get(detected.file_type, ("Pillow", False))
    note = "由内容魔数判定" if detected.by_content else "由扩展名兜底判定"
    return DecodeCapability(detected.file_type, decoder, fast, note)


# --------------------------------------------------------------------- 缩放
def _fit(image: Image.Image, max_edge: int) -> tuple[Image.Image, bool]:
    """按长边缩放（只缩小不放大）。返回 (图像, 是否被缩小过)。"""
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image, False
    scale = max_edge / float(longest)
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS), True


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGB", "L"):
        return image.convert("RGB")
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _apply_orientation(image: Image.Image, orientation: int | None) -> Image.Image:
    """按 EXIF Orientation 摆正。EXIF 缺失时 PIL 的 exif_transpose 也会正确返回原图。"""
    if not orientation or orientation == 1:
        return image
    transposed = ImageOps.exif_transpose(image)
    return transposed if transposed is not None else image


# --------------------------------------------------------------------- 图像获取
def decode(path: Path, *, max_edge: int | None = None) -> tuple[Image.Image, str, bool]:
    """解码为 PIL 图像，返回 (图像, 解码器, 是否走了 RAW 内嵌快路径)。

    `max_edge` 给出时，尽量在解码阶段就缩小（RAW 用 half_size，视频用缩放滤镜）。
    """
    path = Path(path)
    capability = decode_capabilities(path)

    if capability.file_type == "raw":
        return _decode_raw_path(path, max_edge)
    if capability.file_type == "video":
        from .video import extract_cover_frame

        return _to_rgb(extract_cover_frame(path)), "ffmpeg", False
    return _decode_pillow_path(path, max_edge), capability.decoder, False


def load_image(path: Path) -> Image.Image:
    """解码为完整尺寸的 PIL 图像（按 EXIF 摆正）。供预览生成与后续阶段使用。"""
    image, _decoder, _fast = decode(path)
    return image


def _apply_raw_flip(image: Image.Image, flip: int) -> Image.Image:
    """按 LibRaw 报告的 flip 摆正（内嵌 JPEG 的 EXIF 方向未必可靠，以 LibRaw 为准）。"""
    if flip == 3:
        return image.rotate(180, expand=True)
    if flip == 5:
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).rotate(90, expand=True)
    if flip == 6:
        return image.rotate(-90, expand=True)
    return image


def _raw_embedded_preview(raw, max_edge: int | None, thumb) -> Image.Image | None:
    """取出并摆正相机内嵌预览；尺寸过小时返回 None（交由去马赛克慢路径）。"""
    import io

    longest = 0
    try:
        with Image.open(io.BytesIO(thumb.data)) as probe:
            longest = max(probe.size)
    except Exception:  # noqa: BLE001 - 内嵌 JPEG 损坏时走慢路径
        return None
    if not longest or (max_edge is not None and longest < max_edge // 2):
        return None

    # 用 draft 在**解码阶段**降采样：JPEG 支持 1/2、1/4、1/8 解码，
    # 避免把 6000x4000 全解进内存再缩放（10 万张规模下这是主要开销）。
    target = max_edge or longest
    embedded = Image.open(io.BytesIO(thumb.data))
    try:
        embedded.draft("RGB", (target, target))
    except Exception:  # noqa: BLE001 - 非 JPEG 不支持 draft
        pass
    embedded.load()
    embedded = _apply_raw_flip(embedded, int(getattr(raw.sizes, "flip", 0) or 0))
    image = _to_rgb(embedded)
    if max_edge:
        image, _ = _fit(image, max_edge)
    return image


def _decode_raw(source, max_edge: int | None) -> tuple[Image.Image, str, bool]:
    """RAW 解码（路径或文件对象）：优先内嵌 JPEG 预览；不可用时退回半尺寸去马赛克。"""
    import rawpy

    try:
        with rawpy.imread(source) as raw:
            try:
                thumb = raw.extract_thumb()
            except Exception:  # noqa: BLE001 - 某些 RAW 没有可用缩略图
                thumb = None

            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                image = _raw_embedded_preview(raw, max_edge, thumb)
                if image is not None:
                    return image, "rawpy/LibRaw(embedded-preview)", True

            # 慢路径：半尺寸去马赛克（无插值，明显快于全尺寸）
            rgb = raw.postprocess(
                half_size=True, use_camera_wb=True, output_bps=8, no_auto_bright=False
            )
        image = Image.fromarray(rgb)
        if max_edge:
            image, _ = _fit(image, max_edge)
        return image, "rawpy/LibRaw(demosaic-half)", False
    except DecodeError:
        raise
    except Exception as exc:  # noqa: BLE001 - LibRaw 异常类型繁多
        name = getattr(source, "name", None) or "（内存数据）"
        raise DecodeError(f"RAW 解码失败：{name}（{type(exc).__name__}: {exc}）") from exc


def _decode_raw_path(path: Path, max_edge: int | None) -> tuple[Image.Image, str, bool]:
    try:
        import rawpy
    except ImportError as exc:  # pragma: no cover
        raise DecodeError(f"rawpy 不可用：{exc}") from exc
    return _decode_raw(str(path), max_edge)


def _decode_raw_buffer(buffer, max_edge: int | None) -> tuple[Image.Image, str, bool]:
    try:
        import rawpy
    except ImportError as exc:  # pragma: no cover
        raise DecodeError(f"rawpy 不可用：{exc}") from exc
    return _decode_raw(buffer, max_edge)


def _decode_pillow_path(path: Path, max_edge: int | None) -> Image.Image:
    try:
        with Image.open(path) as image:
            return _prepare_raster(image, max_edge)
    except (OSError, ValueError) as exc:
        raise DecodeError(f"解码失败：{path.name}（{type(exc).__name__}: {exc}）") from exc


def _decode_pillow_buffer(buffer, max_edge: int | None) -> tuple[Image.Image, str, bool]:
    try:
        with Image.open(buffer) as image:
            return _prepare_raster(image, max_edge), "Pillow", False
    except (OSError, ValueError) as exc:
        raise DecodeError(f"解码失败（内存数据）：{type(exc).__name__}: {exc}") from exc


def _prepare_raster(image: Image.Image, max_edge: int | None) -> Image.Image:
    """摆正、按需降采样、转 RGB 并完成加载（使调用方可以安全关闭源）。"""
    orientation = None
    try:
        orientation = image.getexif().get(274)  # 274 = Orientation
    except Exception:  # noqa: BLE001 - EXIF 损坏不应阻止解码
        orientation = None
    image = _apply_orientation(image, orientation)
    if max_edge:
        # draft 让 JPEG 在解码阶段就降采样，显著降低内存与耗时
        try:
            image.draft("RGB", (max_edge * 2, max_edge * 2))
        except Exception:  # noqa: BLE001 - 非 JPEG 不支持 draft
            pass
        image, _ = _fit(image, max_edge)
    result = _to_rgb(image)
    result.load()
    return result


# --------------------------------------------------------------------- 落盘
def asset_storage_key(asset_id: int, kind: str, *, suffix: str = ".webp") -> str:
    """相对 data_root 的存储路径：`<kind>/ab/cd/<id>.webp` 两级分片。

    分片是为了避免单目录堆积十万级文件（NTFS 虽可承受，但目录列举会变慢）。
    """
    identifier = f"{asset_id:09d}"
    return f"{kind}/{identifier[-3:-1]}/{identifier[-1]}/{identifier}{suffix}"


def _write_webp(
    image: Image.Image, target: Path, *, max_edge: int, quality: int = PREVIEW_QUALITY
) -> tuple[int, int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    fitted, _ = _fit(image, max_edge)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        fitted.save(tmp, format=PREVIEW_FORMAT, quality=quality, method=4)
        size = tmp.stat().st_size
        os.replace(tmp, target)  # 原子替换，避免留下半截文件
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise MediaError(f"写入预览失败：{target}（{exc}）") from exc
    return fitted.width, fitted.height, size


def generate_previews(
    asset_id: int,
    path: Path,
    data_root: Path,
    *,
    thumb_edge: int = THUMBNAIL_MAX_EDGE,
    preview_edge: int = PREVIEW_MAX_EDGE,
    force: bool = False,
) -> Previews:
    """生成缩略图与预览。**只读原片**，输出全部落在 `data_root` 下。"""
    path = Path(path)
    if not path.is_file():
        raise DecodeError(f"原文件不存在：{path}")
    image, decoder, fast = decode(path, max_edge=preview_edge)
    return _write_previews(
        asset_id, image, data_root, decoder=decoder, fast=fast,
        thumb_edge=thumb_edge, preview_edge=preview_edge, force=force,
    )


def generate_previews_from_bytes(
    asset_id: int,
    payload: bytes,
    data_root: Path,
    *,
    file_type: str | None = None,
    thumb_edge: int = THUMBNAIL_MAX_EDGE,
    preview_edge: int = PREVIEW_MAX_EDGE,
    force: bool = False,
) -> Previews:
    """从**已在内存中的数据**生成缩略图与预览。

    用途：当同一次任务已经为了计算哈希而读取了文件内容时，可以复用这份数据，
    避免为预览再读一遍原片（对 270 GB 的库就是少读一遍）。
    视频仍需走 ffmpeg（它按路径读取），因此视频不适用本函数。
    """
    import io

    data_root = Path(data_root)
    buffer = io.BytesIO(payload)

    if file_type == "raw":
        image, decoder, fast = _decode_raw_buffer(buffer, preview_edge)
    elif file_type == "video":
        raise UnsupportedFormat("视频无法从内存解码（ffmpeg 需要按路径读取），请使用 generate_previews")
    else:
        image, decoder, fast = _decode_pillow_buffer(buffer, preview_edge)

    return _write_previews(
        asset_id, image, data_root, decoder=decoder, fast=fast,
        thumb_edge=thumb_edge, preview_edge=preview_edge, force=force,
    )


def _write_previews(
    asset_id: int,
    image: Image.Image,
    data_root: Path,
    *,
    decoder: str,
    fast: bool,
    thumb_edge: int,
    preview_edge: int,
    force: bool,
) -> Previews:
    """把已解码的图像写成缩略图与预览（原子写入 + 幂等复用）。"""
    data_root = Path(data_root)
    thumb_rel = asset_storage_key(asset_id, "thumbnails")
    preview_rel = asset_storage_key(asset_id, "previews")
    thumb_abs = data_root / thumb_rel
    preview_abs = data_root / preview_rel

    if not force and thumb_abs.exists() and preview_abs.exists():
        # 已生成且未要求强制重算 → 直接复用（PRD §11.2：已生成的不重复生成）。
        image.close()
        with Image.open(thumb_abs) as existing_thumb:
            thumb_size = existing_thumb.size
        with Image.open(preview_abs) as existing_preview:
            preview_size = existing_preview.size
        return Previews(
            thumb_path=thumb_rel,
            thumb_width=thumb_size[0],
            thumb_height=thumb_size[1],
            thumb_bytes=thumb_abs.stat().st_size,
            preview_path=preview_rel,
            preview_width=preview_size[0],
            preview_height=preview_size[1],
            preview_bytes=preview_abs.stat().st_size,
            fast_path=fast,
            decoder=decoder,
        )

    try:
        thumb_w, thumb_h, thumb_bytes = _write_webp(image, thumb_abs, max_edge=thumb_edge)
        preview_w, preview_h, preview_bytes = _write_webp(image, preview_abs, max_edge=preview_edge)
    finally:
        image.close()

    return Previews(
        thumb_path=thumb_rel,
        thumb_width=thumb_w,
        thumb_height=thumb_h,
        thumb_bytes=thumb_bytes,
        preview_path=preview_rel,
        preview_width=preview_w,
        preview_height=preview_h,
        preview_bytes=preview_bytes,
        fast_path=fast,
        decoder=decoder,
    )


def previews_exist(asset_id: int, data_root: Path) -> bool:
    """两个衍生文件是否都已存在（决定是否还需要解码原片）。"""
    data_root = Path(data_root)
    return (data_root / asset_storage_key(asset_id, "thumbnails")).exists() and (
        data_root / asset_storage_key(asset_id, "previews")
    ).exists()


__all__ = [
    "DecodeCapability",
    "MediaInfo",
    "Previews",
    "asset_storage_key",
    "decode",
    "decode_capabilities",
    "extract_metadata",
    "generate_previews",
    "generate_previews_from_bytes",
    "load_image",
    "previews_exist",
]
