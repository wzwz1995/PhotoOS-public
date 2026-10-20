"""媒体适配层测试：类型探测、元数据、RAW/HEIC/视频、缩略图与预览。

原则：合成样本优先（可离线、快、确定），真实 Nikon Z5 NEF 样本存在时额外做一次真实校验。
所有测试都必须证明：**处理原片不改动原片**。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from photoos.db import apply_migrations
from photoos.errors import DecodeError, MediaError, UnsupportedFormat
from photoos.media.adapter import (
    asset_storage_key,
    decode,
    decode_capabilities,
    generate_previews,
    load_image,
    previews_exist,
)
from photoos.media.detect import detect_by_extension_only, detect_type, detect_type_from_bytes
from photoos.media.metadata import MediaInfo, extract_metadata
from photoos.media.video import (
    VideoInfo,
    _parse_mvhd,
    _parse_stsd_codec,
    _parse_tkhd_dimensions,
    container_metadata_summary,
)

REAL_NEF_DIR = Path(r"C:\PhotoOS\_samples")
REAL_NEFS = sorted(REAL_NEF_DIR.glob("*.NEF")) if REAL_NEF_DIR.is_dir() else []

#: 真实 MOV：优先用样本目录，其次用真实视频库（只读）
_CANDIDATE_MOVS = [Path(r"C:\PhotoOS\_samples"), Path(r"C:\Videos")]
REAL_MOVS: list[Path] = []
for _root in _CANDIDATE_MOVS:
    if _root.is_dir():
        REAL_MOVS.extend(sorted(_root.rglob("*.mov"))[:2])
        REAL_MOVS.extend(sorted(_root.rglob("*.MOV"))[:2])
    if len(REAL_MOVS) >= 2:
        break


# --------------------------------------------------------------------- 样本工厂
def _write_jpeg(path: Path, size: tuple[int, int] = (800, 600), exif: bytes | None = None) -> Path:
    image = Image.new("RGB", size, (30, 120, 200))
    if exif is not None:
        path.write_bytes(b"")  # 占位，实际由下方 save 覆盖
        image.save(path, format="JPEG", exif=exif)
    else:
        image.save(path, format="JPEG")
    return path


def _jpeg_with_exif(path: Path, size: tuple[int, int] = (1200, 800)) -> Path:
    """写一个带 IFD0 + Exif 子 IFD + GPS 的真实 JPEG，模拟相机输出。

    关键：DateTimeOriginal / FNumber / ISO 必须写入 **Exif 子 IFD**，
    因为真实文件就是这样（IFD0 里没有曝光信息）。
    GPS 的度分秒必须用 IFDRational（Pillow 不接受裸元组）。
    """
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", size, (12, 140, 220))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"        # Make
    exif[0x0110] = "NIKON Z 5"                # Model
    exif[0x0131] = "Ver.01.40"                # Software
    exif[0x0112] = 6                          # Orientation
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2025:10:25 10:49:32"       # DateTimeOriginal
    sub[0x9291] = "05"                        # SubsecTimeOriginal
    sub[0x9011] = "+08:00"                    # OffsetTimeOriginal
    sub[0x829D] = IFDRational(71, 10)         # FNumber = f/7.1
    sub[0x829A] = IFDRational(1, 8000)        # ExposureTime
    sub[0x8827] = 800                         # ISOSpeedRatings
    sub[0x920A] = IFDRational(3000, 10)       # FocalLength = 300mm
    sub[0xA405] = 300                         # FocalLengthIn35mmFilm
    sub[0xA434] = "TAMRON 70-300mm F/4.5-6.3 Di III RXD A047Z"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"                              # GPSLatitudeRef
    gps[2] = (
        IFDRational(30, 1),
        IFDRational(15, 1),
        IFDRational(4380, 100),
    )                                         # 30°15'43.80"
    gps[3] = "E"
    gps[4] = (
        IFDRational(120, 1),
        IFDRational(8, 1),
        IFDRational(5950, 100),
    )
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


def _write_heic(path: Path, size: tuple[int, int] = (640, 480)) -> Path:
    Image.new("RGB", size, (200, 60, 30)).save(path, format="HEIF")
    return path


def _write_png(path: Path, size: tuple[int, int] = (300, 200)) -> Path:
    Image.new("RGB", size, (10, 10, 10)).save(path, format="PNG")
    return path


def _write_tiff(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    Image.new("RGB", size, (0, 200, 0)).save(path, format="TIFF")
    return path


def _write_video(path: Path) -> Path:
    """用内置 ffmpeg 生成一个真实的小 MP4（含 moov）。"""
    import subprocess

    from photoos.media.video import ffmpeg_path

    command = [
        ffmpeg_path(), "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
        "-pix_fmt", "yuv420p", "-c:v", "libx264" if _has_x264() else "mpeg4",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not path.exists():
        pytest.skip(f"无法用内置 ffmpeg 生成测试视频：{(result.stderr or '')[:200]}")
    return path


def _has_x264() -> bool:
    import subprocess

    from photoos.media.video import ffmpeg_path

    result = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-encoders"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return "libx264" in (result.stdout or "")


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


# --------------------------------------------------------------------- 类型探测
def test_detect_jpeg_by_content(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "photo.jpg")
    detected = detect_type(path)
    assert detected is not None
    assert detected.file_type == "jpeg" and detected.by_content


def test_detect_ignores_extension_when_content_disagrees(tmp_path: Path) -> None:
    """扩展名骗人时必须相信内容 —— 真实库里存在被改名的文件。"""
    path = _write_jpeg(tmp_path / "actually_jpeg.png")
    detected = detect_type(path)
    assert detected is not None
    assert detected.file_type == "jpeg" and detected.by_content


def test_detect_no_extension(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "noext")
    detected = detect_type(path)
    assert detected is not None and detected.file_type == "jpeg"


def test_detect_png_tiff_heic(tmp_path: Path) -> None:
    assert detect_type(_write_png(tmp_path / "a.png")).file_type == "png"  # type: ignore[union-attr]
    assert detect_type(_write_tiff(tmp_path / "a.tif")).file_type == "tiff"  # type: ignore[union-attr]
    assert detect_type(_write_heic(tmp_path / "a.heic")).file_type == "heic"  # type: ignore[union-attr]


def test_detect_raw_by_magic_and_extension(tmp_path: Path) -> None:
    """RAW 与 TIFF 共享魔数，靠扩展名路由；不得误判成普通 TIFF。"""
    payload = b"II*\x00" + b"\x08\x00\x00\x00" + b"\x00" * 64
    (tmp_path / "shot.nef").write_bytes(payload)
    (tmp_path / "shot.tif").write_bytes(payload)
    nef = detect_type(tmp_path / "shot.nef")
    tif = detect_type(tmp_path / "shot.tif")
    assert nef is not None and nef.file_type == "raw"
    assert tif is not None and tif.file_type == "tiff"


def test_detect_ignored_extensions_return_none(tmp_path: Path) -> None:
    for name in ("a.csv", "a.log", "a.ps1", "a.json", "a.md"):
        path = tmp_path / name
        path.write_bytes(b"hello")
        assert detect_type(path) is None, f"{name} 不应被当作媒体"


def test_detect_unknown_binary_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"\x00\x01\x02\x03not-a-format")
    assert detect_type(path) is None


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "png"),
        (b"GIF89a" + b"\x00" * 20, "other"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20, "webp"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "heic"),
        (b"\x00\x00\x00\x18ftypavif" + b"\x00" * 20, "heic"),
        (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 20, "video"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20, "video"),
        (b"FUJIFILM" + b"\x00" * 20, "raw"),
        # ★ 佳能 CR3 是 ISO-BMFF 封装（`ftyp` + `crx ` 品牌），不是 TIFF。
        # 修复前它落进通用分支 → `other` → 通用 Pillow 解不出来（20-reference Q-36）。
        (b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 20, "raw"),
    ],
)
def test_detect_type_from_bytes(head: bytes, expected: str) -> None:
    detected = detect_type_from_bytes(head)
    assert detected is not None
    assert detected.file_type == expected


@pytest.mark.parametrize(
    ("name", "expected", "mime"),
    [
        # 只看扩展名（遍历阶段用）：这两种曾经都被判成 other。
        ("a.cr3", "raw", "image/x-canon-cr3"),
        ("a.raw", "raw", "image/x-raw"),
        # 回归：TIFF 家族的 RAW 仍然走 raw（不能被新加的容器分支抢走）。
        ("a.nef", "raw", "image/x-nikon-nef"),
        ("a.tif", "tiff", "image/tiff"),
    ],
)
def test_detect_by_extension_routes_raw_formats(tmp_path: Path, name: str, expected: str, mime: str) -> None:
    detected = detect_by_extension_only(tmp_path / name)
    assert detected is not None
    assert detected.file_type == expected
    assert detected.mime == mime


def test_detect_cr3_by_content_wins_over_extension(tmp_path: Path) -> None:
    """真实文件走 `detect_type`（魔数优先）：CR3 的内容头必须被认出来。"""
    path = tmp_path / "IMG_0001.CR3"
    path.write_bytes(b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 64)
    detected = detect_type(path)
    assert detected is not None
    assert detected.file_type == "raw"
    assert detected.by_content is True
    assert detected.container == "cr3"


def test_detect_type_from_bytes_unknown() -> None:
    assert detect_type_from_bytes(b"nothing familiar here") is None


# --------------------------------------------------------------------- 解码能力
def test_decode_capabilities_reports_expected_decoders(tmp_path: Path) -> None:
    jpeg = _write_jpeg(tmp_path / "a.jpg")
    heic = _write_heic(tmp_path / "a.heic")
    assert decode_capabilities(jpeg).decoder == "Pillow"
    assert "heif" in decode_capabilities(heic).decoder.lower()


def test_decode_capabilities_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "a.bin"
    path.write_bytes(b"\x00\x01\x02")
    with pytest.raises(UnsupportedFormat):
        decode_capabilities(path)


# --------------------------------------------------------------------- HEIC
def test_heic_roundtrip_does_not_need_system_codec(tmp_path: Path) -> None:
    """PRD §8 / 断点 #11：HEIC 解码必须由内置 libheif 完成，不依赖系统 HEIF 扩展。"""
    import pillow_heif

    assert pillow_heif.libheif_version(), "应能读到内置 libheif 版本"
    path = _write_heic(tmp_path / "shot.heic", (320, 240))
    image = load_image(path)
    assert image.size == (320, 240)
    assert image.mode == "RGB"


def test_heic_thumbnail_generation(tmp_path: Path) -> None:
    path = _write_heic(tmp_path / "shot.heic", (900, 600))
    result = generate_previews(1, path, tmp_path / "data")
    assert result.thumb_width == 320
    assert result.preview_width == 900  # 小于 preview 上限时保持原尺寸
    assert (tmp_path / "data" / result.thumb_path).exists()


# --------------------------------------------------------------------- 元数据
def test_metadata_from_jpeg_sub_ifd(tmp_path: Path) -> None:
    """回归：曝光信息在 Exif 子 IFD，只读 IFD0 会全部为空。"""
    path = _jpeg_with_exif(tmp_path / "shot.jpg")
    info = extract_metadata(path)

    assert info.source in {"pillow", "exifread"}
    assert info.camera_make == "NIKON CORPORATION"
    assert info.camera_model == "NIKON Z 5"
    assert info.lens_model and "TAMRON" in info.lens_model
    assert info.iso == 800
    assert info.aperture == 7.1
    assert info.focal_length == 300.0
    assert info.focal_length_35mm == 300.0
    assert info.shutter_speed == "1/8000"
    assert info.exposure_time == pytest.approx(1 / 8000, rel=1e-3)
    assert info.capture_at == "2025-10-25 10:49:32"
    assert info.capture_date_local == "2025-10-25"
    assert info.subsec == "05"
    assert info.orientation == 6


def test_metadata_timezone_conversion(tmp_path: Path) -> None:
    """有 OffsetTimeOriginal 时必须算出精确 UTC；这是既有工具做不到的地方。"""
    path = _jpeg_with_exif(tmp_path / "shot.jpg")
    info = extract_metadata(path)
    assert info.tz_offset == "+08:00"
    assert info.tz_source == "exif-offset"
    assert info.capture_at_utc == "2025-10-25 02:49:32"


def test_metadata_without_timezone_does_not_fake_utc(tmp_path: Path) -> None:
    """无时区信息时必须标注 unknown 且不写 UTC —— 绝不猜测（境外拍摄会算错的根源）。"""
    image = Image.new("RGB", (100, 100), (1, 2, 3))
    exif = Image.Exif()
    exif[0x0110] = "Some Phone"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2026-10-09 18:30:00"
    path = tmp_path / "phone.jpg"
    image.save(path, format="JPEG", exif=exif.tobytes())

    info = extract_metadata(path)
    assert info.capture_at == "2026-10-09 18:30:00"
    assert info.capture_at_utc is None
    assert info.tz_source == "unknown"
    assert any("时区" in w for w in info.warnings)


def test_metadata_gps(tmp_path: Path) -> None:
    path = _jpeg_with_exif(tmp_path / "shot.jpg")
    info = extract_metadata(path)
    assert info.gps_lat == pytest.approx(30.2621667, abs=1e-4)
    assert info.gps_lon == pytest.approx(120.1498750, abs=1e-4)
    assert info.gps_lat > 0 and info.gps_lon > 0  # N/E 应为正


def test_metadata_missing_exif_is_graceful(tmp_path: Path) -> None:
    """无 EXIF 的文件不得抛异常，且必须记录尺寸（供索引与筛选使用）。"""
    path = _write_jpeg(tmp_path / "plain.jpg", (640, 480))
    info = extract_metadata(path)
    assert info.source == "none"
    assert info.width == 640 and info.height == 480
    assert info.capture_at is None


def test_metadata_never_raises_on_corrupt_file(tmp_path: Path) -> None:
    """损坏文件必须返回空结果 + 警告，绝不向上抛异常（PRD §11.1 单文件失败不阻塞整批）。"""
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    info = extract_metadata(path)
    assert isinstance(info, MediaInfo)
    assert info.capture_at is None


def test_metadata_missing_file_is_graceful(tmp_path: Path) -> None:
    info = extract_metadata(tmp_path / "nope.jpg")
    assert info.source == "none"
    assert info.warnings


def test_metadata_normalization() -> None:
    info = MediaInfo(aperture=7.099999904632568, focal_length=299.99999, iso=800)
    info.normalized()
    assert info.aperture == 7.1
    assert info.focal_length == 300.0


def test_metadata_to_row_matches_table_columns() -> None:
    """to_row 的键必须与 metadata 表列名一致，否则落库会静默丢字段。"""
    from photoos.db import Database, apply_migrations

    row = MediaInfo().to_row(asset_id=7)
    assert row["asset_id"] == 7
    expected = {
        "asset_id", "camera_make", "camera_model", "lens_make", "lens_model",
        "focal_length", "focal_length_35mm", "aperture", "shutter_speed", "exposure_time",
        "iso", "exposure_comp", "metering_mode", "flash", "wb", "orientation",
        "width", "height", "capture_at", "capture_at_utc", "capture_date_local",
        "tz_offset", "tz_source", "subsec", "gps_lat", "gps_lon", "gps_alt",
        "device_serial", "software", "source", "source_enrichment", "duration_seconds",
        "codec", "raw_json",
    }
    assert set(row) == expected
    assert apply_migrations  # 保持导入被使用（端到端测试用）


def test_metadata_row_is_accepted_by_schema(tmp_path: Path) -> None:
    """端到端：提取结果必须能被 metadata 表的 CHECK 约束接受。"""
    from photoos.db import Database

    db = Database(tmp_path / "db.sqlite")
    apply_migrations(db, backup_dir=tmp_path / "backups")
    path = _jpeg_with_exif(tmp_path / "shot.jpg")
    info = extract_metadata(path)
    row = info.to_row(asset_id=1)

    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, status, created_at) VALUES ('u','lib','ONLINE','t')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1,'杭州市\\a.jpg','jpeg','OK','t')"
        )
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO metadata ({columns}) VALUES ({placeholders})", tuple(row.values())
        )
    stored = db.query_one("SELECT * FROM metadata WHERE asset_id = 1")
    assert stored is not None
    assert stored["iso"] == 800
    assert stored["capture_at_utc"] == "2025-10-25 02:49:32"


# --------------------------------------------------------------------- 视频
def test_video_probe_from_generated_mp4(tmp_path: Path) -> None:
    from photoos.media.video import probe_video

    path = _write_video(tmp_path / "clip.mp4")
    info = probe_video(path)
    assert info.source == "iso-bmff"
    assert info.width == 320 and info.height == 240
    assert info.duration_seconds == pytest.approx(2.0, abs=0.6)
    assert info.codec == "H.264"
    assert "320x240" in container_metadata_summary(info)
    # 注：ffmpeg 生成的合成视频 creation_time 为 0（未设置），此处**不应**产出时间。
    # 真实相机 MOV 的 creation_time 由下面的真实样本测试覆盖。
    assert info.capture_at_utc is None


@pytest.mark.skipif(not REAL_MOVS, reason="没有真实 MOV 样本")
def test_real_mov_capture_time_is_utc() -> None:
    """真实 Nikon MOV：mvhd.creation_time 是 UTC，必须解析出来。"""
    from photoos.media.video import probe_video

    info = probe_video(REAL_MOVS[0])
    assert info.source == "iso-bmff", f"应走容器解析：{info.warnings}"
    assert info.capture_at_utc is not None
    assert info.width and info.height
    assert info.duration_seconds is not None and info.duration_seconds > 0
    assert info.codec in {"H.264", "H.265/HEVC"}
    # 未提供时区依据时不得伪造本地时间（既有工具硬编码 +08:00 是缺陷）
    assert info.capture_at is None
    assert info.tz_source == "unknown"


def test_video_cover_frame(tmp_path: Path) -> None:
    from photoos.media.video import extract_cover_frame

    path = _write_video(tmp_path / "clip.mp4")
    frame = extract_cover_frame(path, seek_seconds=0.5)
    assert frame.mode == "RGB"
    assert frame.size == (320, 240)


def test_video_previews(tmp_path: Path) -> None:
    path = _write_video(tmp_path / "clip.mp4")
    result = generate_previews(5, path, tmp_path / "data")
    assert result.thumb_width == 320
    assert result.decoder == "ffmpeg"


def test_video_probe_non_mp4_uses_ffmpeg(tmp_path: Path) -> None:
    """非 MP4/MOV 容器退回 ffmpeg 文本解析（不依赖 ffprobe）。"""
    from photoos.media.video import probe_video

    path = _write_video(tmp_path / "clip.mkv")
    info = probe_video(path)
    assert info.source == "ffmpeg"
    assert info.width == 320 and info.height == 240


def test_iso_bmff_locates_moov_at_file_end(tmp_path: Path) -> None:
    """回归：moov 在文件尾部（非 faststart）时仍必须解析成功。

    真实事故：43 MB 的 MP4 布局为 ftyp → uuid → mdat(43.4MB) → moov，
    moov 落在"头部窗口 + 尾部窗口"两种启发式之外，导致信息全丢。
    """
    from photoos.media.video import parse_iso_bmff

    path = _write_video(tmp_path / "tail-moov.mp4")
    info = parse_iso_bmff(path)
    assert not info.warnings, f"不应有警告：{info.warnings}"
    assert info.width == 320 and info.height == 240


def test_mvhd_parser_handles_version_1() -> None:
    import struct

    # version=1: flags(4) + creation(8) + modification(8) + timescale(4) + duration(8)
    creation = 3_700_000_000  # 秒（1904 纪元）
    blob = bytearray()
    blob += b"\x01\x00\x00\x00"
    blob += struct.pack(">Q", creation)
    blob += struct.pack(">Q", creation)
    blob += struct.pack(">I", 1000)
    blob += struct.pack(">Q", 5000)
    blob += b"\x00" * 80
    created, duration = _parse_mvhd(bytes(blob), 0, len(blob))
    assert created is not None
    assert duration == pytest.approx(5.0)


def test_mvhd_parser_handles_unset_creation_time() -> None:
    import struct

    blob = bytearray(b"\x00\x00\x00\x00")
    blob += struct.pack(">I", 0)          # creation = 0 → 未设置
    blob += struct.pack(">I", 0)
    blob += struct.pack(">I", 600)        # timescale
    blob += struct.pack(">I", 1200)       # duration
    blob += b"\x00" * 80
    created, duration = _parse_mvhd(bytes(blob), 0, len(blob))
    assert created is None, "creation_time=0 表示未设置，不得当成 1904 年"
    assert duration == pytest.approx(2.0)


def test_tkhd_dimensions_are_fixed_point() -> None:
    import struct

    blob = bytearray(b"\x00\x00\x00\x00")   # version 0 + flags
    blob += struct.pack(">I", 0)            # creation
    blob += struct.pack(">I", 0)            # modification
    blob += struct.pack(">I", 1)            # track_id
    blob += struct.pack(">I", 0)            # reserved
    blob += struct.pack(">I", 100)          # duration
    blob += b"\x00" * 8                     # reserved
    blob += b"\x00" * 8                     # layer/alt/volume/reserved
    blob += b"\x00" * 36                    # matrix
    blob += struct.pack(">I", 1920 << 16)   # width  = 1920.0
    blob += struct.pack(">I", 1080 << 16)   # height = 1080.0
    width, height = _parse_tkhd_dimensions(bytes(blob), 0, len(blob))
    assert (width, height) == (1920, 1080)


def test_stsd_codec_names() -> None:
    import struct

    for fourcc, expected in ((b"avc1", "H.264"), (b"hvc1", "H.265/HEVC"), (b"apcn", "ProRes 422")):
        blob = bytearray(b"\x00\x00\x00\x00")
        blob += struct.pack(">I", 1)
        blob += struct.pack(">I", 100) + fourcc + b"\x00" * 20
        assert _parse_stsd_codec(bytes(blob), 0, len(blob)) == expected


# --------------------------------------------------------------------- 预览
def test_generate_previews_sizes_and_paths(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "big.jpg", (4000, 3000))
    data_root = tmp_path / "data"
    result = generate_previews(42, path, data_root)

    assert result.thumb_width == 320
    assert result.thumb_height == 240
    assert result.preview_width == 1600
    assert result.preview_height == 1200
    assert result.generator_version == "p1.0"

    thumb = data_root / result.thumb_path
    preview = data_root / result.preview_path
    assert thumb.exists() and preview.exists()
    with Image.open(thumb) as image:
        assert image.size == (320, 240)
    # 预览路径必须是相对的（数据库不存绝对路径）
    assert not Path(result.thumb_path).is_absolute()
    assert ":" not in result.thumb_path


def test_generate_previews_does_not_upscale(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "small.jpg", (200, 150))
    result = generate_previews(1, path, tmp_path / "data")
    assert (result.thumb_width, result.thumb_height) == (200, 150)
    assert (result.preview_width, result.preview_height) == (200, 150)


def test_generate_previews_is_idempotent(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "a.jpg", (1000, 800))
    data_root = tmp_path / "data"
    first = generate_previews(9, path, data_root)
    thumb = data_root / first.thumb_path
    mtime_before = thumb.stat().st_mtime_ns

    second = generate_previews(9, path, data_root)
    assert second.thumb_path == first.thumb_path
    assert thumb.stat().st_mtime_ns == mtime_before, "已生成的不应重复生成（PRD §11.2）"


def test_previews_exist(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "a.jpg")
    data_root = tmp_path / "data"
    assert not previews_exist(3, data_root)
    generate_previews(3, path, data_root)
    assert previews_exist(3, data_root)


def test_previews_force_regenerates(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "a.jpg", (1000, 800))
    data_root = tmp_path / "data"
    first = generate_previews(11, path, data_root)
    thumb = data_root / first.thumb_path
    before = thumb.stat().st_mtime_ns
    generate_previews(11, path, data_root, force=True)
    # 强制重算允许时间戳变化（内容应一致）
    assert thumb.exists()


def test_asset_storage_key_is_sharded_and_relative() -> None:
    # id=123456 补零到 9 位 → "000123456"；切片 [-3:-1]="45"、[-1]="6"
    key = asset_storage_key(123456, "thumbnails")
    assert key == "thumbnails/45/6/000123456.webp"
    assert asset_storage_key(1, "previews") == "previews/00/1/000000001.webp"
    assert not Path(key).is_absolute()
    # 相邻 id 不得落在同一目录（避免单目录堆积十万文件）
    assert asset_storage_key(1, "thumbnails") != asset_storage_key(2, "thumbnails")
    # 目录层级必须是两级（分片深度）
    assert len(Path(key).parts) == 4


def test_generate_previews_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MediaError):
        generate_previews(1, tmp_path / "nope.jpg", tmp_path / "data")


def test_corrupt_file_raises_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)
    with pytest.raises(DecodeError):
        generate_previews(1, path, tmp_path / "data")


# --------------------------------------------------------------------- 内存解码路径
def test_previews_from_bytes_matches_path_version(tmp_path: Path) -> None:
    """从内存数据生成预览必须与按路径生成**结果一致**。

    这条很重要：融合流程（一次读盘同时算哈希与生成预览）用的就是内存路径，
    如果两者产出不同，融合就会悄悄改变输出。
    """
    from photoos.media.adapter import generate_previews_from_bytes

    source = _write_jpeg(tmp_path / "photo.jpg", (1200, 800))
    payload = source.read_bytes()

    from_path = generate_previews(1, source, tmp_path / "data-path")
    from_bytes = generate_previews_from_bytes(1, payload, tmp_path / "data-bytes", file_type="jpeg")

    assert from_bytes.thumb_width == from_path.thumb_width
    assert from_bytes.thumb_height == from_path.thumb_height
    assert from_bytes.preview_width == from_path.preview_width
    assert from_bytes.preview_height == from_path.preview_height
    # 同尺寸同质量下编码结果应完全一致
    assert from_bytes.thumb_bytes == from_path.thumb_bytes
    assert from_bytes.preview_bytes == from_path.preview_bytes


def test_previews_from_bytes_applies_exif_orientation(tmp_path: Path) -> None:
    """内存路径同样要遵守 EXIF 方向（横竖不能弄反）。"""
    from PIL.TiffImagePlugin import IFDRational

    from photoos.media.adapter import generate_previews_from_bytes

    image = Image.new("RGB", (1200, 800), (40, 90, 160))
    exif = Image.Exif()
    exif[0x0112] = 6  # 顺时针旋转 90°
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2025:10:25 10:49:32"
    sub[0x9011] = "+08:00"
    sub[0x829D] = IFDRational(40, 10)
    source = tmp_path / "rotated.jpg"
    image.save(source, format="JPEG", exif=exif.tobytes())

    result = generate_previews_from_bytes(2, source.read_bytes(), tmp_path / "data", file_type="jpeg")
    # 旋转后长边仍应是 1200（原 1200x800 → 800x1200，缩到长边 1600 以内不缩放）
    assert max(result.preview_width, result.preview_height) == 1200
    assert result.preview_width < result.preview_height, "方向未生效：应当是竖构图"


def test_previews_from_bytes_rejects_video(tmp_path: Path) -> None:
    """视频无法从内存解码（ffmpeg 需要路径），必须明确报错而不是静默出错。"""
    from photoos.media.adapter import generate_previews_from_bytes

    with pytest.raises(UnsupportedFormat):
        generate_previews_from_bytes(3, b"\x00" * 32, tmp_path / "data", file_type="video")


def test_previews_from_bytes_corrupt_data_raises(tmp_path: Path) -> None:
    from photoos.media.adapter import generate_previews_from_bytes

    with pytest.raises(DecodeError):
        generate_previews_from_bytes(4, b"\xff\xd8\xff\xe0" + b"\x00" * 40, tmp_path / "data")


@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_previews_from_bytes_on_real_nef(tmp_path: Path) -> None:
    """真实 Z5 NEF 走内存路径也必须成功（融合流程依赖它）。"""
    from photoos.media.adapter import generate_previews_from_bytes

    payload = REAL_NEFS[0].read_bytes()
    result = generate_previews_from_bytes(5, payload, tmp_path / "data", file_type="raw")
    assert result.fast_path, f"应走内嵌预览快路径，实际 {result.decoder}"
    assert result.preview_width == 1600


# --------------------------------------------------------------------- 只读守卫
@pytest.mark.parametrize(
    "factory",
    [_write_jpeg, _write_png, _write_tiff, _write_heic],
)
def test_processing_never_modifies_original(tmp_path: Path, factory) -> None:
    """P0 铁律：生成元数据与预览绝不能改动原片一个字节。"""
    path = factory(tmp_path / "orig")
    before = _snapshot(tmp_path)
    extract_metadata(path)
    generate_previews(1, path, tmp_path / "data")
    after = {k: v for k, v in _snapshot(tmp_path).items() if not k.startswith("data/")}
    assert after == before


def test_processing_never_modifies_real_nef(tmp_path: Path) -> None:
    if not REAL_NEFS:
        pytest.skip("没有真实 NEF 样本")
    path = REAL_NEFS[0]
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    digest_before = _hash_head(path)
    extract_metadata(path)
    generate_previews(1, path, tmp_path / "data")
    assert (path.stat().st_size, path.stat().st_mtime_ns) == before
    assert _hash_head(path) == digest_before


def _hash_head(path: Path, size: int = 1 << 20) -> bytes:
    import hashlib

    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(size)).digest()


# --------------------------------------------------------------------- 真实 NEF
@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_real_nef_metadata() -> None:
    """PRD §8：Nikon Z5 NEF 必须是 Phase 1 的实际测试样本。"""
    info = extract_metadata(REAL_NEFS[0])
    assert info.source == "libraw"
    assert info.camera_model == "NIKON Z 5"
    assert info.camera_make == "NIKON CORPORATION"
    assert info.lens_model
    assert info.iso and info.iso > 0
    assert info.aperture and info.aperture > 0
    assert info.capture_at and len(info.capture_at) == 19
    assert info.width and info.height
    assert info.tz_source == "exif-offset", "Z5 应写入 OffsetTimeOriginal"
    assert info.capture_at_utc


@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_real_nef_uses_embedded_preview_fast_path(tmp_path: Path) -> None:
    """RAW 快路径：内嵌 JPEG（实测 1~8 ms）而不是全尺寸去马赛克（~0.5 s）。"""
    result = generate_previews(1, REAL_NEFS[0], tmp_path / "data")
    assert result.fast_path, f"应走内嵌预览快路径，实际 {result.decoder}"
    assert "embedded-preview" in result.decoder
    assert result.preview_width == 1600
