"""基础物理分类导入的测试（M3）。

本文件守护的承诺：
  1. **源永远只读** —— 导入前后源文件的 size/mtime/SHA256 完全不变，且不存在
     move/rename/delete 的代码路径（只做复制）。这是铁律 #1 的落点。
  2. **照片与视频联合切段** —— 两者必须拿到同一个时段目录名，否则同一段拍摄
     会在两个盘上被切成不同目录（PHASE1_EXT_TECH_PLAN §1.3.6）。
  3. **不新建平行时段** —— 第二次导入必须并入既有 `<城市>\\<时段>` 目录。
  4. 预演**不写任何文件**。

关于 GPS 的处理：Pillow 写 GPS IFD 相当啰嗦，而本文件测的是**编排**（判定→落位→落库），
EXIF/GPS 的解析本身已由 `test_media.py` 覆盖。因此这里用 `gps_table` 夹具把 GPS
注入到真实抽取结果的**一个字段**上，其余（拍摄时间、时区、尺寸、机型）全部真实解析。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from photoos.config import Settings
from photoos.db import Database, apply_migrations
from photoos.errors import PhotoOSError, SafetyError
from photoos.library import classify_import
from photoos.library.classify import UNKNOWN_CITY
from photoos.library.geo import GeoCache, GeoEntry, cell_key
from photoos.library.store import LibraryStore
from photoos.media.metadata import extract_metadata as real_extract_metadata
from photoos.repos import AssetRepo, MetadataRepo, SessionRepo

#: 真实 geo-cache 中的一个万宁市网格（18.628868, 110.213232 落在这一格）
WANNING_CELL = cell_key(18.628868, 110.213232)
WANNING_LATLON = (18.628868, 110.213232)


# --------------------------------------------------------------------- 夹具
@pytest.fixture()
def gps_table(monkeypatch) -> dict[str, tuple[float, float]]:
    """按文件名注入 GPS。返回的表可写，用于指定哪些文件"有 GPS"。"""
    table: dict[str, tuple[float, float]] = {}
    counter = {"calls": 0}

    def patched(path):
        counter["calls"] += 1
        info = real_extract_metadata(path)
        hit = table.get(Path(path).name)
        if hit is not None:
            info.gps_lat, info.gps_lon = hit
        return info

    patched.calls = counter  # type: ignore[attr-defined]
    monkeypatch.setattr(classify_import, "extract_metadata", patched)
    table["__calls__"] = counter  # type: ignore[assignment]
    return table


@pytest.fixture()
def geo() -> GeoCache:
    return GeoCache(
        {
            WANNING_CELL: GeoEntry(
                cell=WANNING_CELL, lat=18.628868, lon=110.213232, city="萬寧市", src="bigdatacloud"
            )
        }
    )


@pytest.fixture()
def ctx(tmp_path: Path) -> tuple[Database, Settings, int, int, Path]:
    """照片库 + 视频库 + 空的源目录。"""
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    store = LibraryStore(db)
    photo_root = tmp_path / "照片"
    video_root = tmp_path / "视频"
    photo_root.mkdir()
    video_root.mkdir()
    photos = store.create(name="我的照片库", root=photo_root)
    videos = store.create(name="我的视频库", root=video_root)

    source = tmp_path / "src"
    source.mkdir()
    return db, settings, photos.id, videos.id, source


def _photo(
    path: Path,
    when: datetime,
    *,
    offset: str = "+08:00",
    size: tuple[int, int] = (320, 240),
    seed: int = 1,
) -> Path:
    """写一张带真实 EXIF（拍摄时间 + 时区偏移）的 JPEG。"""
    image = Image.new("RGB", size, (seed * 7 % 256, seed * 13 % 256, seed * 29 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x9011] = offset
    sub[0x8827] = 200
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


def _video(path: Path, utc: datetime) -> Path:
    """用 ffmpeg 生成一个**极小但真实**的 MP4，并注入容器 UTC 时间。"""
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    stamp = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(
        [
            exe, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt", "yuv420p", "-metadata", f"creation_time={stamp}",
            "-loglevel", "error", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    out: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return out


def _plan(db, source, photo_id, video_id=None, *, geo=None, **kwargs):
    return classify_import.build_plan(
        db,
        source,
        photo_library_id=photo_id,
        video_library_id=video_id,
        geo=geo,
        **kwargs,
    )


# ============================================================ 预演不写任何文件
def test_dry_run_writes_nothing(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON
    before = _snapshot(source)

    plan = _plan(db, source, photo_id, video_id, geo=geo)

    assert plan.total == 1
    assert _snapshot(source) == before, "预演绝不能改动源文件"
    assert AssetRepo(db).count(photo_id) == 0, "预演绝不能写数据库"
    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None and not any(root.rglob("*.JPG")), "预演绝不能往库里写文件"


# ============================================================ 执行：只复制
def test_apply_copies_byte_identical_and_leaves_source_untouched(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10), seed=1)
    _photo(source / "b.JPG", datetime(2025, 11, 29, 6, 20), seed=2)
    gps_table["a.JPG"] = WANNING_LATLON
    before = _snapshot(source)

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    report = classify_import.execute_plan(db, plan, geo=geo)

    assert report.copied == 2 and report.failed == 0
    assert _snapshot(source) == before, "导入后源文件的 size/mtime/SHA256 必须完全不变"

    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    for name in ("a.JPG", "b.JPG"):
        copied = root / "万宁市" / plan.plan.files[0].session / name
        assert copied.is_file()
        assert copied.read_bytes() == (source / name).read_bytes(), "副本必须与源逐字节相同"


def test_database_rows_are_complete_after_apply(ctx, geo, gps_table) -> None:
    """复制完就该有 asset + hash + metadata（不用等索引阶段再读一遍原片）。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    report = classify_import.execute_plan(db, plan, geo=geo)

    assert report.assets_registered == 1
    assert report.metadata_written == 1
    assets = AssetRepo(db)
    asset = assets.by_relative(photo_id, f"万宁市\\{plan.plan.files[0].session}\\a.JPG")
    assert asset is not None
    assert asset.content_hash, "复制的同一次读盘里就算出了哈希"
    assert asset.size == (source / "a.JPG").stat().st_size
    row = MetadataRepo(db).get(asset.id)
    assert row is not None
    assert row["capture_at"] == "2025-11-29 06:10:00"
    assert row["tz_offset"] == "+08:00"
    assert int(row["width"]) == 320


def test_metadata_is_read_once_not_twice(ctx, geo, gps_table) -> None:
    """规划阶段读到的 EXIF 必须被复用 —— RAW 上这能省掉一整遍原片读取。"""
    db, _s, photo_id, video_id, source = ctx
    for index in range(3):
        _photo(source / f"p{index}.JPG", datetime(2025, 11, 29, 6, 10 + index))
    before_calls = gps_table["__calls__"]["calls"]  # type: ignore[index]

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    classify_import.execute_plan(db, plan, geo=geo)

    after_calls = gps_table["__calls__"]["calls"]  # type: ignore[index]
    assert after_calls - before_calls == 3, "每个文件只应读取一次 EXIF（规划阶段）"


# ============================================================ 联合切段（核心）
def test_photos_and_videos_share_one_session_name_across_libraries(
    ctx, geo, gps_table
) -> None:
    """**最重要的行为**：同一段拍摄在照片库与视频库里必须落到同名时段目录。

    实测依据：既有库 71 个时段目录里，有 3 个只有把照片与视频的跨度合起来才能复现；
    只看照片只有 68/71（PHASE1_EXT_TECH_PLAN §1.3.6）。
    """
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "p1.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["p1.JPG"] = WANNING_LATLON
    # 视频 UTC 09:47 → 借照片的 +08:00 → 本地 17:47，把时段右边界推到 1747
    _video(source / "v1.MOV", datetime(2025, 11, 29, 9, 47))

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    classify_import.execute_plan(db, plan, geo=geo)

    photo_root = LibraryStore(db).get(photo_id).root_path
    video_root = LibraryStore(db).get(video_id).root_path
    assert photo_root is not None and video_root is not None

    photo_dirs = sorted(p.name for p in (photo_root / "万宁市").iterdir())
    video_dirs = sorted(p.name for p in (video_root / "万宁市").iterdir())
    assert photo_dirs == video_dirs == ["2025-11-29_0610-1747"], (
        "两个库必须得到同一个时段名；若不同，说明照片与视频没有被联合切段"
    )


def test_video_is_routed_to_video_library_not_photo_library(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "p1.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["p1.JPG"] = WANNING_LATLON
    _video(source / "v1.MOV", datetime(2025, 11, 29, 9, 47))

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    kinds = {f.name: f.kind for f in plan.plan.files}
    assert kinds == {"p1.JPG": "photo", "v1.MOV": "video"}
    assert plan.by_library() == {photo_id: 1, video_id: 1}

    classify_import.execute_plan(db, plan, geo=geo)
    assert AssetRepo(db).count(photo_id) == 1
    assert AssetRepo(db).count(video_id) == 1

    video_root = LibraryStore(db).get(video_id).root_path
    assert video_root is not None
    assert list(video_root.rglob("*.MOV")), "视频必须落在视频库"


def test_video_metadata_keeps_timezone_unknown(ctx, geo, gps_table) -> None:
    """视频的时区推断只用于**分类命名**，不得污染 metadata（仍然如实记 unknown）。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "p1.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["p1.JPG"] = WANNING_LATLON
    _video(source / "v1.MOV", datetime(2025, 11, 29, 9, 47))

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    classify_import.execute_plan(db, plan, geo=geo)

    assets = AssetRepo(db)
    video_asset = assets.by_relative(
        video_id, f"万宁市\\{plan.plan.files[0].session}\\v1.MOV"
    )
    assert video_asset is not None
    row = MetadataRepo(db).get(video_asset.id)
    assert row is not None
    assert row["tz_source"] == "unknown"
    assert row["capture_at"] is None, "视频没有本地时间，不得伪造"
    assert row["capture_at_utc"] == "2025-11-29 09:47:00"


# ============================================================ 锚点合并
def test_second_import_merges_into_existing_session(ctx, geo, gps_table) -> None:
    """**不新建平行时段**：第二次导入必须并入既有 `<城市>\\<时段>` 目录。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "early.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["early.JPG"] = WANNING_LATLON
    plan = _plan(db, source, photo_id, video_id, geo=geo)
    classify_import.execute_plan(db, plan, geo=geo)
    folder = plan.plan.files[0].session

    second = source.parent / "src2"
    second.mkdir()
    _photo(second / "late.JPG", datetime(2025, 11, 29, 17, 40))
    plan2 = _plan(db, second, photo_id, video_id, geo=geo)

    assert plan2.plan.files[0].session == folder, "必须沿用既有目录名"
    assert plan2.plan.files[0].city_how == "inherited-library"

    classify_import.execute_plan(db, plan2, geo=geo)
    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    assert sorted(p.name for p in (root / "万宁市").iterdir()) == [folder], "不得出现第二个时段目录"


def test_distant_import_creates_a_new_session(ctx, geo, gps_table) -> None:
    """相隔超过 12 小时的素材应当另建时段（锚点合并不能过度）。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON
    classify_import.execute_plan(db, _plan(db, source, photo_id, video_id, geo=geo), geo=geo)

    later = source.parent / "later"
    later.mkdir()
    _photo(later / "b.JPG", datetime(2025, 12, 20, 10, 0))
    gps_table["b.JPG"] = WANNING_LATLON
    plan = _plan(db, later, photo_id, video_id, geo=geo)
    assert plan.plan.files[0].session == "2025-12-20_1000-1000"


# ============================================================ session 落库
def test_session_row_is_created_then_updated_not_duplicated(ctx, geo, gps_table) -> None:
    """session 的身份是 `(city_hint, period_hint)`；重复导入不得堆积同名 Session。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON
    plan = _plan(db, source, photo_id, video_id, geo=geo)
    report = classify_import.execute_plan(db, plan, geo=geo)

    assert (report.sessions_created, report.sessions_updated) == (1, 0)
    repo = SessionRepo(db)
    assert repo.count() == 1
    row = repo.get_by_dir("万宁市", plan.plan.files[0].session)
    assert row is not None
    assert row["name"] == f"万宁市\\{plan.plan.files[0].session}"
    assert row["source"] == "auto"
    assert row["captured_from"] == "2025-11-29 06:10:00"

    # 再导一次同一时段（更晚的文件）→ 更新同一行，且 group_revision 递增
    second = source.parent / "src2"
    second.mkdir()
    _photo(second / "b.JPG", datetime(2025, 11, 29, 17, 40))
    plan2 = _plan(db, second, photo_id, video_id, geo=geo)
    report2 = classify_import.execute_plan(db, plan2, geo=geo)

    assert (report2.sessions_created, report2.sessions_updated) == (0, 1)
    assert repo.count() == 1, "不得新增重复的 Session 行"
    refreshed = repo.get_by_dir("万宁市", plan.plan.files[0].session)
    assert refreshed is not None
    assert refreshed["captured_to"] == "2025-11-29 17:40:00", "时间范围应被扩展"
    assert int(refreshed["group_revision"]) == 2


# ============================================================ 同名不覆盖
def test_name_collision_gets_suffix_and_never_overwrites(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10), seed=1)
    _photo(source / "nested", datetime(2025, 11, 29, 6, 20), seed=2) if False else None
    (source / "sub").mkdir()
    _photo(source / "sub" / "a.JPG", datetime(2025, 11, 29, 6, 20), seed=2)
    gps_table["a.JPG"] = WANNING_LATLON

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    names = sorted(f.name for f in plan.plan.files)
    assert names == ["a.JPG", "a__2.JPG"], "同一时段内的同名文件必须加后缀而不是互相覆盖"

    report = classify_import.execute_plan(db, plan, geo=geo)
    assert report.copied == 2 and report.failed == 0

    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    folder = root / "万宁市" / plan.plan.files[0].session
    assert sorted(p.name for p in folder.iterdir()) == ["a.JPG", "a__2.JPG"]
    assert (folder / "a.JPG").read_bytes() != (folder / "a__2.JPG").read_bytes()


def test_reimporting_same_source_skips_instead_of_duplicating(ctx, geo, gps_table) -> None:
    """**重复导入同一批素材必须跳过，而不是复制出 `__2` 副本。**

    实测现场：拿库内已有的照片当源做预演，计划里给出的是 `DSC_9005__2.JPG` ——
    也就是说重复导入一整张存储卡会把整个库再复制一遍（存储直接翻倍）。
    正确语义是"同内容 → 跳过"，这也是既有 `plan_import`（非分类路径）早就有的行为。
    """
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10), seed=7)
    gps_table["a.JPG"] = WANNING_LATLON
    classify_import.execute_plan(db, _plan(db, source, photo_id, video_id, geo=geo), geo=geo)

    before = _snapshot(source)
    again = _plan(db, source, photo_id, video_id, geo=geo)

    assert again.total == 0, "内容相同的重复导入不该产生任何待复制文件"
    assert len(again.duplicates) == 1
    assert any("不重复复制" in note for note in again.notes)

    report = classify_import.execute_plan(db, again, geo=geo)
    assert report.copied == 0 and report.skipped_duplicate == 1
    assert _snapshot(source) == before

    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    folder = next((root / "万宁市").iterdir())
    assert [item.name for item in folder.iterdir()] == ["a.JPG"], "不得出现 __2 副本"


def test_same_name_different_content_still_gets_suffix(ctx, geo, gps_table) -> None:
    """反方向：**同名但内容不同**必须保留（改名落位），不能被误判成重复而丢弃。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10), seed=7)
    gps_table["a.JPG"] = WANNING_LATLON
    classify_import.execute_plan(db, _plan(db, source, photo_id, video_id, geo=geo), geo=geo)

    # 同名、同尺寸范围但内容不同（种子不同 → 像素不同 → 体积也不同）
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 30), seed=99, size=(400, 300))
    again = _plan(db, source, photo_id, video_id, geo=geo)

    assert again.total == 1
    assert not again.duplicates
    assert again.plan.files[0].name == "a__2.JPG"
    assert again.plan.files[0].collision_with is not None

    classify_import.execute_plan(db, again, geo=geo)
    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    folder = next((root / "万宁市").iterdir())
    assert sorted(item.name for item in folder.iterdir()) == ["a.JPG", "a__2.JPG"]


def test_existing_target_file_is_refused_not_overwritten(ctx, geo, gps_table) -> None:
    """即使预演之后目标文件才出现，执行阶段也必须拒绝覆盖。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON
    plan = _plan(db, source, photo_id, video_id, geo=geo)

    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    target = root / Path(plan.plan.files[0].target_relative.replace("\\", "/"))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"someone else's file")

    report = classify_import.execute_plan(db, plan, geo=geo)
    assert report.copied == 0 and report.failed == 1
    assert target.read_bytes() == b"someone else's file", "既有文件绝不能被覆盖"


# ============================================================ 安全拒绝
def test_source_with_videos_requires_video_library(ctx, geo, gps_table) -> None:
    """源里有视频却没给视频库 → 必须明确报错，而不是把视频塞进照片库。

    照片与视频要一起切段；混进同一个库会破坏 `<城市>\\<时段>` 的约定。
    """
    db, _s, photo_id, _video_id, source = ctx
    _photo(source / "p1.JPG", datetime(2025, 11, 29, 6, 10))
    _video(source / "v1.MOV", datetime(2025, 11, 29, 9, 47))

    with pytest.raises(PhotoOSError) as excinfo:
        _plan(db, source, photo_id, None, geo=geo)
    assert "视频库" in excinfo.value.message


def test_source_inside_library_is_rejected(ctx, geo) -> None:
    """源位于目标库内部 → 会自我复制，必须拒绝。"""
    db, _s, photo_id, video_id, _source = ctx
    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    (root / "万宁市").mkdir()
    _photo(root / "万宁市" / "a.JPG", datetime(2025, 11, 29, 6, 10))

    with pytest.raises(SafetyError):
        _plan(db, root / "万宁市", photo_id, video_id, geo=geo)


# ============================================================ 未知地点 / 未命中网格
def test_photo_without_gps_lands_in_unknown_city(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))  # 无 GPS

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    assert plan.plan.files[0].city == UNKNOWN_CITY
    assert plan.plan.unknown_city_count == 1


def test_scan_source_reports_total_even_when_sampling(ctx, gps_table) -> None:
    """**抽样预演也必须知道源里一共有多少** —— 否则「确认执行」是没有刻度的按钮。

    实测踩到：预演上限 30，用户以为点确认执行会处理整张卡（实际只导入 30 个）。
    """
    db, _s, _photo_id, _video_id, source = ctx
    for index in range(7):
        _photo(source / f"p{index}.JPG", datetime(2025, 11, 29, 6, 10 + index))

    entries, _skipped, total, nbytes = classify_import.scan_source(source, limit=3, workers=1)
    assert len(entries) == 3, "抽样只读前 3 个的 EXIF"
    assert total == 7, "但总数必须是整个源的 7 个"
    assert nbytes == sum((source / f"p{i}.JPG").stat().st_size for i in range(7)), (
        "总字节数也要覆盖整个源（用于给复制耗时预估）"
    )

    all_entries, _, total_all, bytes_all = classify_import.scan_source(source, workers=1)
    assert len(all_entries) == 7 and total_all == 7 and bytes_all == nbytes


def test_build_plan_flags_sampled_preview(ctx, geo, gps_table) -> None:
    db, _s, photo_id, video_id, source = ctx
    for index in range(5):
        _photo(source / f"p{index}.JPG", datetime(2025, 11, 29, 6, 10 + index))
    gps_table["p0.JPG"] = WANNING_LATLON

    sampled = _plan(db, source, photo_id, video_id, geo=geo, limit=2)
    assert sampled.source_total == 5
    assert sampled.total == 2
    assert sampled.preview_sampled is True
    assert any("确认执行" in note and "全部" in note for note in sampled.notes), (
        "必须明确告诉用户确认执行会处理全部文件"
    )

    full = _plan(db, source, photo_id, video_id, geo=geo)
    assert full.source_total == 5 and full.total == 5
    assert full.preview_sampled is False


def test_scan_source_parallel_reads_give_same_result_as_serial(ctx, gps_table) -> None:
    """并发只影响速度，不影响结果（EXIF 抽取必须是纯的）。"""
    db, _s, _photo_id, _video_id, source = ctx
    for index in range(6):
        _photo(source / f"p{index}.JPG", datetime(2025, 11, 29, 6, 10 + index), seed=index + 1)

    serial, _, _n1, _b1 = classify_import.scan_source(source, workers=1)
    parallel, _, _n2, _b2 = classify_import.scan_source(source, workers=4)

    def shape(entries):
        return [
            (e.relative, e.size, e.item.local_time, e.item.lat, e.file_type) for e in entries
        ]

    assert shape(serial) == shape(parallel)


def test_build_plan_accepts_city_override(ctx, geo, gps_table) -> None:
    """整批强制城市要能一路走到导入计划（无 GPS 的素材由此才能归位）。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2026, 10, 2, 18, 58))
    _photo(source / "b.JPG", datetime(2026, 10, 2, 18, 59))
    gps_table.clear()  # 模拟"相机整段没有 GPS"

    plan = classify_import.build_plan(
        db, source, photo_library_id=photo_id, video_library_id=video_id,
        geo=geo, city_override="雅典",
    )
    assert plan.total == 2
    assert {f.city for f in plan.plan.files} == {"雅典市"}
    assert all(f.target_relative.startswith("雅典市\\") for f in plan.plan.files)
    assert plan.plan.unknown_city_count == 0
    assert any("雅典市" in note for note in plan.notes)


def test_build_plan_without_override_still_uses_gps(ctx, geo, gps_table) -> None:
    """回归：不指定城市时仍按 GPS 判定（默认路径不变）。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = WANNING_LATLON

    plan = classify_import.build_plan(
        db, source, photo_library_id=photo_id, video_library_id=video_id, geo=geo
    )
    assert {f.city for f in plan.plan.files} == {"万宁市"}
    assert plan.plan.city_override is None


def test_unresolved_gps_cell_is_reported(ctx, geo, gps_table) -> None:
    """GPS 落在缓存之外 → 列入未命中网格，供 `photoos geo assign` 人工指认。"""
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10))
    gps_table["a.JPG"] = (31.2304, 121.4737)  # 上海，缓存里没有，且超出就近匹配范围

    plan = _plan(db, source, photo_id, video_id, geo=geo)
    assert plan.plan.unresolved_cells == [cell_key(31.2304, 121.4737)]
    assert plan.plan.files[0].city == UNKNOWN_CITY


# ============================================================ 源路径不可用时的提示
def test_mtp_camera_path_gets_actionable_guidance(ctx, geo) -> None:
    """相机 USB 直连的 MTP 路径必须给出**可操作的**指引。

    实测现场：用户在界面填了 `此电脑\\Z 5\\可移动存储 10001\\DCIM\\101NCZ_5`，
    只得到「Internal Server Error」。这类路径没有盘符、普通文件 API 读不到
    （既有已知限制），但用户需要知道的是接下来该怎么做。
    """
    db, _s, photo_id, video_id, _source = ctx
    mtp = r"此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5"

    with pytest.raises(PhotoOSError) as excinfo:
        _plan(db, mtp, photo_id, video_id, geo=geo)
    message = excinfo.value.message
    assert "MTP" in message
    assert "读卡器" in message and "复制到本地" in message, "必须给出两条可操作的替代做法"
    assert "不会被改动" in message, "要明确承诺不动相机与卡上的文件"


def test_relative_source_path_says_it_must_be_absolute(ctx, geo, tmp_path) -> None:
    """相对路径会被解析到服务进程的工作目录，必须讲清楚这一点。"""
    db, _s, photo_id, video_id, _source = ctx
    with pytest.raises(PhotoOSError) as excinfo:
        _plan(db, r"我的照片\2026", photo_id, video_id, geo=geo)
    message = excinfo.value.message
    assert "绝对路径" in message
    assert "工作目录" in message


def test_missing_absolute_source_keeps_plain_message(ctx, geo, tmp_path) -> None:
    db, _s, photo_id, video_id, _source = ctx
    with pytest.raises(PhotoOSError) as excinfo:
        _plan(db, str(tmp_path / "并不存在"), photo_id, video_id, geo=geo)
    assert "不存在或不可访问" in excinfo.value.message


@pytest.mark.parametrize(
    "text,expected",
    [
        (r"此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5", True),
        (r"本机\iPhone\Internal Storage\DCIM", True),
        ("This PC\\Pixel\\Internal shared storage\\DCIM", True),
        (r"C:\Photos", False),
        (r"E:\_相机导入\20261011", False),
        (r"\\nas\photos", False),
        (r"我的照片\2026", False),
    ],
)
def test_looks_like_mtp_classifier(text: str, expected: bool) -> None:
    from photoos.library.paths import looks_like_mtp

    assert looks_like_mtp(text) is expected


# ============================================================ 视频锚点时区（回归）
#
# ★ 回归：`_video_local_from_utc` 曾经**把 UTC 当本地时间**返回，
#   第二个参数（照片行）传了却完全没用 —— 境外拍摄的视频会整体错几小时，
#   而且不报错。现在向"时间最邻近的照片"借 EXIF 偏移（与 `classify.infer_video_local_times`
#   同一策略）。故意破坏：把函数改回 `return utc.strftime(...)`，前两条立刻变红。

def test_video_local_time_borrows_the_nearest_photo_offset() -> None:
    """照片在 +03:00，视频 UTC 10:00 → 本地 13:00（不是 10:00）。"""
    photo_times = [
        (datetime(2026, 5, 1, 12, 30), 180),   # 本地 12:30，+03:00
        (datetime(2026, 5, 1, 13, 40), 180),
    ]
    assert classify_import._video_local_from_utc(
        "2026-05-01 10:10:00", photo_times
    ) == "2026-05-01 13:10:00"


def test_video_local_time_picks_the_closer_photo_when_offsets_mix() -> None:
    """同一时段内相机时钟换过：取**时间上更近**的那张照片的偏移，而不是众数。"""
    photo_times = [
        (datetime(2026, 10, 2, 16, 50), 180),   # +03:00，离视频 UTC 13:00 更近
        (datetime(2026, 10, 2, 21, 0), 480),    # +08:00
        (datetime(2026, 10, 2, 21, 10), 480),
    ]
    # UTC 13:00 → 粗估按众数 +08:00 = 21:00，但最近的照片是 21:00 那张（+08:00）
    assert classify_import._video_local_from_utc(
        "2026-10-02 13:00:00", photo_times
    ) == "2026-10-02 21:00:00"


def test_video_local_time_does_not_guess_without_photos() -> None:
    """没有任何带偏移的照片 → 返回 None（**不猜**），而不是把 UTC 当本地时间。"""
    assert classify_import._video_local_from_utc("2026-05-01 10:10:00", []) is None


def test_video_local_time_returns_none_for_unparsable_utc() -> None:
    assert classify_import._video_local_from_utc("not-a-time", [(datetime(2026, 5, 1), 480)]) is None
def test_same_content_under_a_different_name_is_still_skipped(ctx, geo, gps_table) -> None:
    """★ 回归（Q-38）：同一张照片换了**文件名/路径**再导入，也必须跳过。

    分类路径原先只跟"目标位置那一条"比内容，所以换个名字再导会多出一份 `a__2.JPG`
    副本（而通用 `plan_import` 早就会查"库内其它路径同内容"）。
    现在两条判据都查，成本靠"体积先筛"控制：体积撞不上就不读源文件。
    故意破坏：把 `drop_content_duplicates` 的判据 ② 去掉，本测试立刻变红。
    """
    db, _s, photo_id, video_id, source = ctx
    _photo(source / "a.JPG", datetime(2025, 11, 29, 6, 10), seed=7)
    gps_table["a.JPG"] = WANNING_LATLON
    classify_import.execute_plan(db, _plan(db, source, photo_id, video_id, geo=geo), geo=geo)

    root = LibraryStore(db).get(photo_id).root_path
    assert root is not None
    imported = next(iter((root / "万宁市").rglob("a.JPG")), None)
    assert imported is not None, "前置条件：a.JPG 应当已经导入"

    # 同内容、**不同文件名** → 目标路径不冲突，但库内已有相同内容
    (source / "a.JPG").unlink()
    shutil.copyfile(imported, source / "b.JPG")

    again = _plan(db, source, photo_id, video_id, geo=geo)
    assert again.total == 0, "同内容换名字再导入不该产生待复制文件"
    assert len(again.duplicates) == 1