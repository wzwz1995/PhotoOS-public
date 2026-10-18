"""元数据提取（EXIF / LibRaw / 容器）。

分层策略（PHASE1_TECH_PLAN §6.2 Tier A，全部为随包依赖，无外部可执行文件）：
  * **RAW**：rawpy（LibRaw）—— 机型、镜头、焦段、光圈、快门、ISO、时间由 LibRaw 直接给出，
    比自行解析 Nikon MakerNote 可靠得多。
  * **JPEG / PNG / TIFF / HEIC**：Pillow `getexif()`（pillow-heif 注册后 HEIC 同样适用）。
  * **兜底**：`exifread`（纯 Python）。注意其 HEIC 解析器含裸 assert，必须捕获异常。

时区纪律（**不猜**）：
  * 有 `OffsetTimeOriginal` → 记录 `tz_offset` 与 `tz_source='exif-offset'`。
  * 无时区信息 → `tz_source='unknown'`，只保存本地挂钟时间，**不写** `capture_at_utc`。
    （既有 PowerShell 工具对视频硬编码 +08:00，境外拍摄会算错 6 小时；此处不复刻该缺陷。）
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

from ..errors import MetadataError
from .detect import detect_type

# 注册 HEIC 打开器，使 Pillow 能直接处理 HEIC（不依赖系统 HEIF 扩展）
try:  # pragma: no cover - 环境相关
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_REGISTERED = True
except Exception:  # noqa: BLE001
    HEIF_REGISTERED = False


@dataclass
class MediaInfo:
    """与 `metadata` 表一一对应的提取结果。字段名与列名保持一致，便于落库。"""

    source: str = "none"            # libraw | pillow | exifread | ffmpeg | none —— 记录**主**来源
    source_enrichment: str | None = None   # 补充来源（RAW 用 Pillow 补 GPS/OffsetTime 等）
    camera_make: str | None = None
    camera_model: str | None = None
    lens_make: str | None = None
    lens_model: str | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    aperture: float | None = None
    shutter_speed: str | None = None
    exposure_time: float | None = None
    iso: int | None = None
    exposure_comp: float | None = None
    metering_mode: str | None = None
    flash: str | None = None
    wb: str | None = None
    orientation: int | None = None
    width: int | None = None
    height: int | None = None
    capture_at: str | None = None          # 'YYYY-MM-DD HH:MM:SS'（本地挂钟）
    capture_at_utc: str | None = None      # 仅在时区已知时写入
    tz_offset: str | None = None
    tz_source: str = "unknown"             # exif-offset | container-utc+assumed | unknown | file-mtime
    subsec: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_alt: float | None = None
    device_serial: str | None = None
    software: str | None = None
    raw_json: str | None = None
    #: 视频专用（Phase 1 只入库，不做内容分析）
    duration_seconds: float | None = None
    codec: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def capture_date_local(self) -> str | None:
        if not self.capture_at:
            return None
        return self.capture_at[:10]

    def normalized(self) -> MediaInfo:
        """规整数值：光圈/焦距保留 1 位小数（f/7.099999… → f/7.1），ISO 取整。

        EXIF 里这些是 rational，转换后会有浮点尾巴；直接展示很难看，
        也影响"按光圈筛选"这类功能的分组。此处统一规整，`raw_json` 仍保留原始值。
        """
        if self.aperture is not None:
            self.aperture = round(float(self.aperture), 1)
        if self.focal_length is not None:
            self.focal_length = round(float(self.focal_length), 1)
        if self.focal_length_35mm is not None:
            self.focal_length_35mm = round(float(self.focal_length_35mm), 1)
        if self.exposure_comp is not None:
            self.exposure_comp = round(float(self.exposure_comp), 2)
        if self.gps_lat is not None:
            self.gps_lat = round(float(self.gps_lat), 7)
        if self.gps_lon is not None:
            self.gps_lon = round(float(self.gps_lon), 7)
        if self.gps_alt is not None:
            self.gps_alt = round(float(self.gps_alt), 1)
        return self

    def to_row(self, asset_id: int) -> dict[str, Any]:
        """转换为 metadata 表的一行（含派生字段）。"""
        payload = {
            "asset_id": asset_id,
            "camera_make": self.camera_make,
            "camera_model": self.camera_model,
            "lens_make": self.lens_make,
            "lens_model": self.lens_model,
            "focal_length": self.focal_length,
            "focal_length_35mm": self.focal_length_35mm,
            "aperture": self.aperture,
            "shutter_speed": self.shutter_speed,
            "exposure_time": self.exposure_time,
            "iso": self.iso,
            "exposure_comp": self.exposure_comp,
            "metering_mode": self.metering_mode,
            "flash": self.flash,
            "wb": self.wb,
            "orientation": self.orientation,
            "width": self.width,
            "height": self.height,
            "capture_at": self.capture_at,
            "capture_at_utc": self.capture_at_utc,
            "capture_date_local": self.capture_date_local,
            "tz_offset": self.tz_offset,
            "tz_source": self.tz_source,
            "subsec": self.subsec,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "gps_alt": self.gps_alt,
            "device_serial": self.device_serial,
            "software": self.software,
            "source": self.source,
            "source_enrichment": self.source_enrichment,
            "duration_seconds": self.duration_seconds,
            "codec": self.codec,
            "raw_json": self.raw_json,
        }
        return payload


# --------------------------------------------------------------------- 工具
def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("\x00")
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            result = float(value.num) / float(value.den)  # IFDRational
        except (AttributeError, TypeError, ZeroDivisionError):
            return None
    if result != result:  # NaN
        return None
    return result


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


def _ratio_str(value: Any) -> tuple[str | None, float | None]:
    """把曝光时间转成 (显示串, 秒数)。支持 rational 与 IFDRational。"""
    seconds = _to_float(value)
    if seconds is None or seconds <= 0:
        return None, None
    if seconds >= 1:
        text = f"{seconds:g}s"
    else:
        denominator = round(1 / seconds)
        text = f"1/{denominator}"
    return text, seconds


def _parse_exif_datetime(value: Any) -> str | None:
    """EXIF 时间形如 '2025:10:25 10:49:32' —— 转成标准 'YYYY-MM-DD HH:MM:SS'。"""
    text = _clean_str(value)
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _normalize_offset(value: Any) -> str | None:
    """把 '+08:00' / '08:00' / 480 之类规整为 '+08:00'。"""
    text = _clean_str(value)
    if not text:
        return None
    text = text.strip()
    if text in {"Z", "z", "+00:00", "-00:00", "UTC", "utc"}:
        return "+00:00"
    if len(text) >= 3 and text[0] in "+-" and ":" not in text:
        text = f"{text[:3]}:{text[3:5]}" if len(text) >= 5 else f"{text[:3]}00"
    if len(text) == 6 and text[0] in "+-" and text[3] == ":":
        return text
    if len(text) == 5 and text[0] in "+-" and text[3] == ":":
        return f"{text}:00"
    return None


def _apply_offset(capture_at: str | None, offset: str | None) -> str | None:
    """把本地挂钟时间 + 偏移换算为 UTC ISO 串（仅在偏移已知时）。"""
    if not capture_at or not offset:
        return None
    try:
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = (int(part) for part in offset[1:].split(":"))
    except (ValueError, IndexError):
        return None
    local = dt.datetime.strptime(capture_at, "%Y-%m-%d %H:%M:%S")
    utc = local - sign * dt.timedelta(hours=hours, minutes=minutes)
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def _gps_decimal(values: Any, ref: Any) -> float | None:
    try:
        degrees = _to_float(values[0])
        minutes = _to_float(values[1])
        seconds = _to_float(values[2])
    except (TypeError, IndexError, KeyError):
        return None
    if degrees is None or minutes is None or seconds is None:
        return None
    result = degrees + minutes / 60.0 + seconds / 3600.0
    ref_text = (_clean_str(ref) or "").upper()
    if ref_text in {"S", "W"}:
        result = -result
    return round(result, 7)


# --------------------------------------------------------------------- RAW
def _extract_raw(path: Path, info: MediaInfo) -> bool:
    """RAW 元数据：**标准 EXIF 优先**，LibRaw 只补它独有的字段。

    为什么不让 LibRaw 当主力（实测对比，真实 Nikon Z5 NEF）：

    | 字段 | Pillow(EXIF) | rawpy(LibRaw) |
    | --- | --- | --- |
    | Make / Model / LensModel / ISO / FNumber / ExposureTime / FocalLength | ✅ | ✅ |
    | **OffsetTimeOriginal（时区）** | ✅ `+08:00` | ❌ 不提供 |
    | **SubsecTimeOriginal（亚秒）** | ✅ | ❌ 不提供 |
    | **BodySerialNumber（机身序列号）** | ✅ | ❌ 不提供 |
    | ImageWidth/Height | ⚠️ 只给**内嵌预览**尺寸（160×120） | ✅ 真实 RAW 尺寸（6040×4032） |
    | flip（方向） | ⚠️ 语义与 LibRaw 不同 | ✅ |

    且成本差异巨大：`Pillow.getexif()` 约 **2 ms/文件**（只读容器头），
    而 `rawpy.imread()+extract_thumb()` 约 **137 ms/文件**（必须读完整 30 MB RAW 才能取到内嵌 JPEG）。

    结论：元数据阶段用 Pillow 读 EXIF（快且更全），用 rawpy 只补尺寸与 flip（`imread` 本身只要 1 ms）。
    `extract_thumb()` 只在**生成预览**时才需要，不在本阶段调用。
    """
    exif_ok = _exif_via_pillow(path, info)
    libraw_ok = _extract_raw_dimensions(path, info)

    if exif_ok or libraw_ok:
        info.source = "libraw" if libraw_ok else "exifread"
        info.source_enrichment = "pillow" if (libraw_ok and exif_ok) else None
        return True

    # EXIF 与 LibRaw 都没读出东西 → 最后退回纯 Python 解析器
    return _exif_via_exifread(path, info)


def _needs_raw_dimensions(info: MediaInfo) -> bool:
    """内嵌预览尺寸（如 160×120）会远小于真实 RAW 尺寸，需要由 LibRaw 纠正。"""
    if info.width is None or info.height is None:
        return True
    return max(info.width, info.height) < 1024


def _extract_raw_dimensions(path: Path, info: MediaInfo) -> bool:
    """用 LibRaw 补齐真实尺寸与方向。`imread` 只解析头部，实测约 1 ms。"""
    try:
        import rawpy
    except ImportError as exc:  # pragma: no cover
        info.warnings.append(f"rawpy 不可用：{exc}")
        return False

    need_size = _needs_raw_dimensions(info)
    need_orientation = not info.orientation or info.orientation == 1
    if not need_size and not need_orientation:
        return False

    try:
        with rawpy.imread(str(path)) as raw:
            sizes = getattr(raw, "sizes", None)
            if sizes is None:
                return False
            if need_size:
                info.width = _to_int(getattr(sizes, "iwidth", None)) or _to_int(
                    getattr(sizes, "width", None)
                )
                info.height = _to_int(getattr(sizes, "iheight", None)) or _to_int(
                    getattr(sizes, "height", None)
                )
            if need_orientation:
                flip = _to_int(getattr(sizes, "flip", None))
                if flip:
                    # LibRaw flip 语义：3/5/6 需要旋转，映射到 EXIF Orientation 值
                    info.orientation = {0: 1, 3: 6, 5: 8, 6: 5}.get(flip, 1)
            # 兜底：EXIF 缺失时用 LibRaw 的时间/曝光（精度略低但好过没有）
            other = getattr(raw, "other", None)
            if other is not None:
                if not info.capture_at:
                    timestamp = getattr(other, "timestamp", None)
                    if isinstance(timestamp, dt.datetime):
                        info.capture_at = timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if info.iso is None:
                    info.iso = _to_int(getattr(other, "iso_speed", None))
                if info.aperture is None:
                    info.aperture = _to_float(getattr(other, "aperture", None))
                if info.exposure_time is None:
                    info.shutter_speed, info.exposure_time = _ratio_str(
                        getattr(other, "shutter_speed", None)
                    )
                if info.focal_length is None:
                    info.focal_length = _to_float(getattr(other, "focal_length", None))
            if info.lens_model is None:
                lens = getattr(raw, "lens", None)
                if lens is not None:
                    info.lens_model = _clean_str(getattr(lens, "model", None))
                    info.lens_make = info.lens_make or _clean_str(getattr(lens, "make", None))
            return True
    except Exception as exc:  # noqa: BLE001 - LibRaw 异常类型繁多
        info.warnings.append(f"LibRaw 读取尺寸失败：{type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------------- Pillow
def _exif_via_pillow(path: Path, info: MediaInfo) -> bool:
    try:
        with Image.open(path) as image:
            if info.width is None:
                info.width, info.height = image.size
            exif = image.getexif()
            if not exif:
                return False
            _fill_from_exif_dict(exif, info)
            return True
    except Exception as exc:  # noqa: BLE001 - Pillow 对损坏文件会抛多种异常
        info.warnings.append(f"Pillow 读取失败：{type(exc).__name__}: {exc}")
        return False


def _fill_from_exif_dict(exif: Any, info: MediaInfo) -> None:
    """从 Pillow 的 Exif 中填充字段。

    **关键**：`Image.getexif()` 只返回 IFD0；`DateTimeOriginal`、`FNumber`、`ISOSpeedRatings`、
    `ExposureTime`、`LensModel`、`OffsetTimeOriginal` 等都在 **Exif 子 IFD** 里。
    实测：真实相机 JPEG 的 IFD0 只有 13 个标签且不含曝光信息，只读 IFD0 会导致
    整库"曝光信息全空"。因此这里先读 IFD0，再用子 IFD 覆盖。
    """
    base = ExifTags.Base

    def from_ifd(ifd: Any, name: str) -> Any:
        try:
            return ifd.get(getattr(base, name))
        except (AttributeError, KeyError, TypeError):
            return None

    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif) or {}
    except (AttributeError, KeyError, TypeError):
        sub = {}

    def first(name: str) -> Any:
        """子 IFD 优先，其次 IFD0（不同厂商写入位置不一致）。"""
        value = from_ifd(sub, name)
        return value if value is not None else from_ifd(exif, name)

    info.camera_make = info.camera_make or _clean_str(first("Make"))
    info.camera_model = info.camera_model or _clean_str(first("Model"))
    info.lens_make = info.lens_make or _clean_str(first("LensMake"))
    info.lens_model = info.lens_model or _clean_str(first("LensModel"))
    info.software = info.software or _clean_str(first("Software"))
    info.orientation = info.orientation or _to_int(first("Orientation")) or 1
    info.focal_length = info.focal_length or _to_float(first("FocalLength"))
    info.focal_length_35mm = info.focal_length_35mm or _to_float(first("FocalLengthIn35mmFilm"))
    info.aperture = info.aperture or _to_float(first("FNumber"))
    info.iso = (
        info.iso
        or _to_int(first("ISOSpeedRatings"))
        or _to_int(first("PhotographicSensitivity"))
        or _to_int(first("StandardOutputSensitivity"))
    )
    info.exposure_comp = info.exposure_comp or _to_float(first("ExposureBiasValue"))
    info.metering_mode = info.metering_mode or _clean_str(first("MeteringMode"))
    info.flash = info.flash or _clean_str(first("Flash"))
    info.wb = info.wb or _clean_str(first("WhiteBalance"))
    info.device_serial = (
        info.device_serial
        or _clean_str(first("BodySerialNumber"))
        or _clean_str(first("CameraSerialNumber"))
    )
    info.subsec = (
        info.subsec
        or _clean_str(first("SubsecTimeOriginal"))
        or _clean_str(first("SubsecTimeDigitized"))
        or _clean_str(first("SubsecTime"))
    )
    if info.exposure_time is None:
        info.shutter_speed, info.exposure_time = _ratio_str(first("ExposureTime"))

    if not info.capture_at:
        info.capture_at = (
            _parse_exif_datetime(first("DateTimeOriginal"))
            or _parse_exif_datetime(first("DateTimeDigitized"))
            or _parse_exif_datetime(first("DateTime"))
        )
    offset = _normalize_offset(first("OffsetTimeOriginal")) or _normalize_offset(
        first("OffsetTime")
    )
    if offset:
        info.tz_offset = offset
        info.tz_source = "exif-offset"
        info.capture_at_utc = _apply_offset(info.capture_at, offset)

    if info.gps_lat is None:
        try:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        except (AttributeError, KeyError, TypeError):
            gps = None
        if gps:
            info.gps_lat = _gps_decimal(
                gps.get(ExifTags.GPS.GPSLatitude), gps.get(ExifTags.GPS.GPSLatitudeRef)
            )
            info.gps_lon = _gps_decimal(
                gps.get(ExifTags.GPS.GPSLongitude), gps.get(ExifTags.GPS.GPSLongitudeRef)
            )
            altitude = _to_float(gps.get(ExifTags.GPS.GPSAltitude))
            if altitude is not None:
                ref = _to_int(gps.get(ExifTags.GPS.GPSAltitudeRef))
                info.gps_alt = -altitude if ref == 1 else altitude


# --------------------------------------------------------------------- exifread
def _exif_via_exifread(path: Path, info: MediaInfo) -> bool:
    """兜底：纯 Python 解析。HEIC 分支含裸 assert，必须捕获异常。"""
    try:
        import exifread
    except ImportError:  # pragma: no cover
        return False
    try:
        with open(path, "rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except Exception as exc:  # noqa: BLE001 - 含 AssertionError / KeyError
        info.warnings.append(f"exifread 失败：{type(exc).__name__}: {exc}")
        return False

    if not tags:
        return False

    def value(name: str) -> Any:
        return tags.get(name)

    info.camera_make = info.camera_make or _clean_str(value("Image Make"))
    info.camera_model = info.camera_model or _clean_str(value("Image Model"))
    info.lens_model = info.lens_model or _clean_str(value("EXIF LensModel"))
    info.software = info.software or _clean_str(value("Image Software"))
    info.orientation = info.orientation or _to_int(value("Image Orientation")) or 1
    info.focal_length = info.focal_length or _to_float(value("EXIF FocalLength"))
    info.focal_length_35mm = info.focal_length_35mm or _to_float(
        value("EXIF FocalLengthIn35mmFilm")
    )
    info.aperture = info.aperture or _to_float(value("EXIF FNumber"))
    info.iso = info.iso or _to_int(value("EXIF ISOSpeedRatings"))
    if info.exposure_time is None:
        info.shutter_speed, info.exposure_time = _ratio_str(value("EXIF ExposureTime"))
    if not info.capture_at:
        info.capture_at = _parse_exif_datetime(value("EXIF DateTimeOriginal")) or _parse_exif_datetime(
            value("Image DateTime")
        )
    offset = _normalize_offset(value("EXIF OffsetTimeOriginal"))
    if offset and not info.tz_offset:
        info.tz_offset = offset
        info.tz_source = "exif-offset"
        info.capture_at_utc = _apply_offset(info.capture_at, offset)

    if info.source in {"none", "exifread"}:
        info.source = "exifread"
    return True

# --------------------------------------------------------------------- 入口
def extract_metadata(path: Path) -> MediaInfo:
    """提取元数据。**永不抛异常给调用方**（失败信息写进 warnings），
    以符合 PRD §11.1「单文件失败不影响整批」。
    """
    path = Path(path)
    info = MediaInfo()
    detected = detect_type(path)
    if detected is None:
        info.warnings.append("无法识别的文件类型")
        return info

    if detected.file_type == "raw":
        # `_extract_raw` 内部已是「EXIF 优先 + LibRaw 补尺寸」，无需再额外调用解析器
        _extract_raw(path, info)
    elif detected.file_type == "video":
        from .video import probe_video

        video = probe_video(path)
        info.source = "ffmpeg"
        info.width, info.height = video.width, video.height
        info.capture_at = video.capture_at
        info.tz_offset = video.tz_offset
        info.tz_source = video.tz_source
        info.capture_at_utc = video.capture_at_utc
        info.duration_seconds = video.duration_seconds
        info.codec = video.codec
        info.warnings.extend(video.warnings)
    else:
        if _exif_via_pillow(path, info):
            info.source = "pillow"
        elif _exif_via_exifread(path, info):
            info.source = "exifread"

    if info.tz_source == "unknown" and info.capture_at:
        # 明确标注：时间是本地挂钟，时区未知，不伪造 UTC
        info.warnings.append("无时区信息，capture_at 视为本地挂钟时间")
    return info.normalized()


def dump_tags(path: Path) -> str:
    """诊断用：把可用标签序列化为 JSON（在 `photoos inspect` 中使用）。"""
    info = extract_metadata(path)
    payload = {k: v for k, v in info.to_row(0).items() if k != "asset_id"}
    payload["warnings"] = info.warnings
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
