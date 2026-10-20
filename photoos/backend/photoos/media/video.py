"""视频 Asset 处理（PRD §9.1 视频边界 LOCKED：入库 + 基础信息 + 封面，不做内容分析）。

两个关键实现决策：

1. **不使用 ffprobe**。`imageio-ffmpeg` 随包只带 `ffmpeg`，不含 `ffprobe`
   （实测：调用 `-select_streams` 会报 `Unrecognized option`）。因此 MP4/MOV 的
   时长/尺寸/编码/创建时间由本模块**直接解析 ISO-BMFF 容器**得到 —— 纯 Python、
   无外部依赖、且 `mvhd.creation_time` 按规范就是 UTC，时区语义比读文本输出可靠。
   其他容器（MKV/AVI 等）退回 ffmpeg 头部文本解析。

2. **时区不猜**。`mvhd` 是 UTC；本模块同时给出 `capture_at_utc`，并把本地挂钟时间
   标为未知时区（`tz_source='container-utc+assumed'` 需要调用方显式传入假定偏移）。
   既有 PowerShell 工具对视频硬编码 +08:00，境外拍摄会算错小时数，此处不复刻。
"""

from __future__ import annotations

import datetime as dt
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DecodeError, MediaError

#: MP4/MOV 的纪元是 1904-01-01 UTC
_MP4_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)

#: 顶层容器 box（用于限制递归范围，避免把 mdat 里的数据当 box 解析）
_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"meta"}
_FULL_BOXES = {b"meta"}  # meta 是 full box，多 4 字节 version+flags


@dataclass
class VideoInfo:
    """视频基础信息。字段与 metadata 表兼容。"""

    container: str | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    capture_at_utc: str | None = None
    capture_at: str | None = None
    tz_offset: str | None = None
    tz_source: str = "unknown"
    source: str = "none"
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- ffmpeg
_FFMPEG_EXE: str | None = None


def ffmpeg_path() -> str:
    """随包 ffmpeg 的绝对路径（由 `__file__` 推导，不依赖 cwd）。"""
    global _FFMPEG_EXE
    if _FFMPEG_EXE is None:
        try:
            import imageio_ffmpeg

            _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"内置 ffmpeg 不可用：{exc}") from exc
    return _FFMPEG_EXE


def _run_ffmpeg(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """调用 ffmpeg。**argv 列表传参**（绝不经过 shell），显式 UTF-8 解码以支持中文路径。"""
    command = [ffmpeg_path(), "-hide_banner", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DecodeError(f"ffmpeg 超时（{timeout}s）：{Path(args[-1]).name}") from exc
    except OSError as exc:
        raise DecodeError(f"无法执行 ffmpeg：{exc}") from exc


# --------------------------------------------------------------------- ISO-BMFF
def _iter_boxes(data: bytes, start: int, end: int):
    """遍历 [start, end) 内的 box，产出 (type, payload_start, payload_end, box_end)。"""
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield box_type, offset + header, offset + size, offset + size
        offset += size


def _find_box(data: bytes, path: list[bytes], start: int = 0, end: int | None = None):
    """按路径查找嵌套 box，返回 (payload_start, payload_end)；找不到返回 None。"""
    if end is None:
        end = len(data)
    if not path:
        return start, end
    head, *rest = path
    for box_type, payload_start, payload_end, _box_end in _iter_boxes(data, start, end):
        if box_type != head:
            continue
        if box_type in _FULL_BOXES:
            payload_start += 4
        if not rest:
            return payload_start, payload_end
        found = _find_box(data, rest, payload_start, payload_end)
        if found is not None:
            return found
    return None


def _parse_mvhd(data: bytes, start: int, end: int) -> tuple[dt.datetime | None, float | None]:
    """解析 mvhd：返回 (创建时间 UTC, 时长秒)。"""
    if start + 4 > end:
        return None, None
    version = data[start]
    if version == 1:
        if start + 4 + 8 + 8 + 4 + 8 > end:
            return None, None
        creation = struct.unpack_from(">Q", data, start + 4)[0]
        timescale = struct.unpack_from(">I", data, start + 20)[0]
        duration = struct.unpack_from(">Q", data, start + 24)[0]
    else:
        if start + 4 + 4 + 4 + 4 + 4 > end:
            return None, None
        creation = struct.unpack_from(">I", data, start + 4)[0]
        timescale = struct.unpack_from(">I", data, start + 12)[0]
        duration = struct.unpack_from(">I", data, start + 16)[0]

    created: dt.datetime | None = None
    # 0 或全 1 表示"未设置"
    if creation not in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        try:
            created = _MP4_EPOCH + dt.timedelta(seconds=int(creation))
        except OverflowError:
            created = None

    seconds: float | None = None
    if timescale and duration not in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        seconds = duration / timescale
    return created, seconds


def _parse_tkhd_dimensions(data: bytes, start: int, end: int) -> tuple[int | None, int | None]:
    """解析 tkhd 的显示宽高（16.16 定点）。"""
    if start + 4 > end:
        return None, None
    version = data[start]
    base = start + 4 + (8 if version == 1 else 4) * 2 + 4 + 4 + 4 + (8 if version == 1 else 4)
    # 布局：version/flags, creation, modification, track_id, reserved, duration,
    #       reserved(8), layer, alt_group, volume, reserved, matrix(36), width, height
    base = start + 4
    base += 8 if version == 1 else 4   # creation
    base += 8 if version == 1 else 4   # modification
    base += 4                          # track_id
    base += 4                          # reserved
    base += 8 if version == 1 else 4   # duration
    base += 8                          # reserved
    base += 2 + 2 + 2 + 2              # layer, alternate_group, volume, reserved
    base += 36                         # matrix
    if base + 8 > end:
        return None, None
    width = struct.unpack_from(">I", data, base)[0] / 65536.0
    height = struct.unpack_from(">I", data, base + 4)[0] / 65536.0
    return (int(round(width)) or None), (int(round(height)) or None)


def _parse_stsd_codec(data: bytes, start: int, end: int) -> str | None:
    """从 stsd 取出第一个 sample entry 的编码四字码（avc1/hvc1/…）。"""
    if start + 8 > end:
        return None
    entry_count = struct.unpack_from(">I", data, start + 4)[0]
    if entry_count < 1:
        return None
    entry_start = start + 8
    if entry_start + 8 > end:
        return None
    fmt = data[entry_start + 4 : entry_start + 8]
    try:
        text = fmt.decode("ascii")
    except UnicodeDecodeError:
        return None
    names = {
        "avc1": "H.264", "avc3": "H.264", "hvc1": "H.265/HEVC", "hev1": "H.265/HEVC",
        "mp4v": "MPEG-4", "jpeg": "Motion JPEG", "ap4h": "ProRes 4444", "apch": "ProRes 422 HQ",
        "apcn": "ProRes 422", "apcs": "ProRes 422 LT", "apco": "ProRes 422 Proxy",
        "av01": "AV1", "vp09": "VP9",
    }
    return names.get(text, text)


def _locate_top_level_box(path: Path, wanted: bytes, *, max_boxes: int = 256) -> tuple[int, int] | None:
    """按真实 box 布局定位一个**顶层** box，返回 (payload_start, size)。

    为什么不能用"读文件头 N MB + 读文件尾 N MB"的窗口启发式：`moov` 可能落在
    两个窗口之外。实测 `NSHM_VIDEO_*.mp4`（43 MB）的布局是
    `ftyp(24) → uuid(40) → mdat(43.4 MB) → moov(13 KB)`，`moov` 起点约在 41.4 MB，
    既超出 32 MB 的头部窗口，也不在尾部窗口起点（≈10.7 MB）之后 —— 窗口法必然漏掉它。

    正确做法：顺着顶层 box 链表逐跳 seek（每个 box 只读 16 字节头，不读 mdat 内容），
    因此即使文件有几个 GB，代价也只是几次 seek。
    """
    try:
        total = path.stat().st_size
    except OSError:
        return None

    offset = 0
    with open(path, "rb") as handle:
        for _ in range(max_boxes):
            if offset + 8 > total:
                return None
            handle.seek(offset)
            header = handle.read(16)
            if len(header) < 8:
                return None
            size = struct.unpack_from(">I", header, 0)[0]
            box_type = header[4:8]
            payload = 8
            if size == 1:
                if len(header) < 16:
                    return None
                size = struct.unpack_from(">Q", header, 8)[0]
                payload = 16
            elif size == 0:
                size = total - offset  # 延伸到文件末尾
            if size < payload or offset + size > total:
                return None
            if box_type == wanted:
                return offset + payload, size - payload
            offset += size
    return None


def parse_iso_bmff(path: Path, *, max_moov_bytes: int = 64 * 1024 * 1024) -> VideoInfo:
    """解析 MP4/MOV 容器头（时长/尺寸/编码/创建时间）。"""
    info = VideoInfo(container=path.suffix.lower().lstrip(".") or "mp4", source="iso-bmff")

    located = _locate_top_level_box(path, b"moov")
    if located is None:
        info.warnings.append("容器中未找到 moov box")
        return info
    moov_start, moov_size = located
    if moov_size > max_moov_bytes:
        info.warnings.append(f"moov 过大（{moov_size} 字节），跳过容器解析")
        return info

    with open(path, "rb") as handle:
        handle.seek(moov_start)
        data = handle.read(moov_size)
    if len(data) < 8:
        info.warnings.append("moov 读取不完整")
        return info

    # 以下所有 offset 都相对于 data（即 moov 的 payload 起点）
    mvhd = _find_box(data, [b"mvhd"])
    if mvhd is not None:
        created, duration = _parse_mvhd(data, *mvhd)
        info.duration_seconds = duration
        if created is not None:
            info.capture_at_utc = created.strftime("%Y-%m-%d %H:%M:%S")

    # 找到视频轨（hdlr == 'vide'），避免把音频轨的尺寸/编码当成视频信息
    video_trak: tuple[int, int] | None = None
    root = _find_box(data, [])
    if root is not None:
        for box_type, payload_start, payload_end, _ in _iter_boxes(data, *root):
            if box_type != b"trak":
                continue
            hdlr = _find_box(data, [b"mdia", b"hdlr"], payload_start, payload_end)
            if hdlr is None:
                continue
            handler = data[hdlr[0] + 8 : hdlr[0] + 12] if hdlr[0] + 12 <= hdlr[1] else b""
            if handler == b"vide":
                video_trak = (payload_start, payload_end)
                break

    scope = video_trak or root
    if scope is not None:
        tkhd = _find_box(data, [b"tkhd"], *scope)
        if tkhd is not None:
            info.width, info.height = _parse_tkhd_dimensions(data, *tkhd)
        stsd = _find_box(data, [b"mdia", b"minf", b"stbl", b"stsd"], *scope)
        if stsd is not None:
            info.codec = _parse_stsd_codec(data, *stsd)

    if info.duration_seconds is None and info.width is None:
        info.warnings.append("moov 中未解析出可用信息")
    return info


# --------------------------------------------------------------------- ffmpeg 兜底
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO_STREAM_RE = re.compile(
    r"Stream #\d+:\d+.*?: Video:\s*([A-Za-z0-9_\-]+).*?(\d{2,5})x(\d{2,5})"
)


def parse_with_ffmpeg(path: Path, *, timeout: int = 60) -> VideoInfo:
    """非 MP4/MOV 容器的兜底解析（读 ffmpeg 头部文本，不依赖 ffprobe）。"""
    info = VideoInfo(container=path.suffix.lower().lstrip(".") or None, source="ffmpeg")
    result = _run_ffmpeg(["-i", str(path)], timeout=timeout)
    text = (result.stderr or "") + (result.stdout or "")

    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        info.duration_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    match = _VIDEO_STREAM_RE.search(text)
    if match:
        info.codec = match.group(1)
        info.width = int(match.group(2))
        info.height = int(match.group(3))

    created = re.search(r"creation_time\s*:\s*([0-9T:\-\.Z]+)", text)
    if created:
        raw = created.group(1).replace("Z", "").split(".")[0]
        try:
            parsed = dt.datetime.fromisoformat(raw)
            info.capture_at_utc = parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    if info.duration_seconds is None and info.width is None:
        info.warnings.append("ffmpeg 未能解析出视频信息")
    return info


def probe_video(path: Path) -> VideoInfo:
    """视频探测入口：优先精确的容器解析，失败再退回 ffmpeg。"""
    path = Path(path)
    suffix = path.suffix.lower()
    info: VideoInfo
    if suffix in {".mp4", ".mov", ".m4v", ".3gp"}:
        try:
            info = parse_iso_bmff(path)
        except (OSError, struct.error) as exc:
            info = VideoInfo(container=suffix.lstrip("."), source="iso-bmff")
            info.warnings.append(f"容器解析失败：{type(exc).__name__}: {exc}")
        if info.duration_seconds is None and info.width is None:
            fallback = parse_with_ffmpeg(path)
            fallback.warnings = info.warnings + fallback.warnings
            info = fallback
    else:
        info = parse_with_ffmpeg(path)

    # 本地挂钟时间：容器给的是 UTC，没有时区依据时不伪造本地时间（PRD 纪律：不猜）
    if info.capture_at_utc and not info.capture_at:
        info.tz_source = "unknown"
    return info


# --------------------------------------------------------------------- 封面帧
def extract_cover_frame(path: Path, *, seek_seconds: float = 1.0, timeout: int = 120):
    """抽取一帧作为封面，返回 PIL.Image（RGB）。

    走临时文件而不是管道：`subprocess` 以文本模式捕获时无法安全承载二进制帧，
    而临时目录由系统清理，且照片库始终只读。
    """
    import tempfile

    from PIL import Image

    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="photoos-cover-") as tmpdir:
        target = Path(tmpdir) / "cover.png"
        result = _run_ffmpeg(
            [
                "-v", "error",
                "-ss", f"{max(0.0, seek_seconds):.3f}",
                "-i", str(path),
                "-frames:v", "1",
                "-y", str(target),
            ],
            timeout=timeout,
        )
        if not target.exists() or target.stat().st_size == 0:
            detail = (result.stderr or "").strip()[:200]
            raise DecodeError(
                f"无法抽取视频封面帧：{path.name}" + (f"（ffmpeg: {detail}）" if detail else "")
            )
        with Image.open(target) as image:
            return image.convert("RGB")


def container_metadata_summary(info: VideoInfo) -> str:
    """一行摘要，供 CLI/日志展示。"""
    parts: list[str] = []
    if info.codec:
        parts.append(info.codec)
    if info.width and info.height:
        parts.append(f"{info.width}x{info.height}")
    if info.duration_seconds is not None:
        minutes, seconds = divmod(int(info.duration_seconds), 60)
        parts.append(f"{minutes}:{seconds:02d}")
    if info.capture_at_utc:
        parts.append(f"UTC {info.capture_at_utc}")
    return "  ".join(parts) if parts else "（无可用信息）"

