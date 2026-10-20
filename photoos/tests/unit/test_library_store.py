"""Library / Volume 身份识别测试（PRD §5.1 / §5.2 LOCKED）。

核心不变量：
  * 库标记 > 卷序列号 > （后续）卷指纹，**盘符永不参与判定**；
  * 盘符变化只能导致 "rebind"，绝不能导致 "丢失" 或"重新导入"；
  * `.photoos/library.json` 是唯一允许写入照片库的文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.errors import LibraryError, LibraryNotFound
from photoos.library import store as store_mod
from photoos.library.store import (
    LibraryStore,
    compute_fingerprint,
    marker_path,
    probe_path,
    read_marker,
    write_marker,
)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "data" / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    return database


@pytest.fixture()
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "照片"
    (root / "杭州市" / "2025-07-19_1341-1413").mkdir(parents=True)
    (root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.JPG").write_bytes(b"jpeg")
    (root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.NEF").write_bytes(b"nef")
    return root


# --------------------------------------------------------------------- 标记文件
def test_marker_round_trip(library_root: Path) -> None:
    assert read_marker(library_root) is None
    path = write_marker(library_root, "uuid-1234", "我的照片库")
    assert path == marker_path(library_root)
    assert path.is_file()

    payload = read_marker(library_root)
    assert payload is not None
    assert payload["library_uuid"] == "uuid-1234"
    assert payload["name"] == "我的照片库"
    assert payload["marker_schema"] == store_mod.MARKER_SCHEMA
    assert payload["created_by"] == "PhotoOS"


def test_marker_is_utf8_json_without_bom(library_root: Path) -> None:
    path = write_marker(library_root, "uuid-中文", "库名")
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    payload = json.loads(raw.decode("utf-8"))
    assert payload["library_uuid"] == "uuid-中文"


def test_marker_does_not_touch_photos(library_root: Path) -> None:
    """写标记只能新增 .photoos/library.json，不得改动任何照片。"""
    before = {
        p.relative_to(library_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in library_root.rglob("*")
        if p.is_file()
    }
    write_marker(library_root, "uuid-x", "x")
    after = {
        p.relative_to(library_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in library_root.rglob("*")
        if p.is_file()
    }
    # 所有原有文件必须逐字节、逐时间戳完全一致
    for name, snapshot in before.items():
        assert after[name] == snapshot, f"写标记不应改动 {name}"
    # 唯一新增文件就是标记本身，且不得留下 .tmp 残骸
    assert set(after) - set(before) == {".photoos/library.json"}
    assert not list(library_root.rglob("*.tmp"))


def test_write_marker_refuses_to_overwrite_existing(library_root: Path) -> None:
    """绝不静默改写既有库标记 —— 那是跨机器识别库身份的依据。"""
    write_marker(library_root, "uuid-original", "原始名称")
    with pytest.raises(LibraryError) as excinfo:
        write_marker(library_root, "uuid-attacker", "新名称")
    assert excinfo.value.detail["kind"] == "marker_exists"
    payload = read_marker(library_root)
    assert payload is not None
    assert payload["library_uuid"] == "uuid-original", "原标记必须保持不变"


def test_write_marker_dry_run_writes_nothing(library_root: Path) -> None:
    path = write_marker(library_root, "uuid-dry", "dry", dry_run=True)
    assert not path.exists()


def test_write_marker_requires_existing_dir(tmp_path: Path) -> None:
    with pytest.raises(LibraryError):
        write_marker(tmp_path / "nope", "uuid-x", "x")


def test_corrupt_marker_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / store_mod.MARKER_DIRNAME).mkdir(parents=True)
    marker_path(root).write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert read_marker(root) is None

    marker_path(root).write_text('{"no_uuid": true}', encoding="utf-8")
    assert read_marker(root) is None


# --------------------------------------------------------------------- 指纹
def test_fingerprint_is_order_independent() -> None:
    samples = [("a\\1.jpg", 100, 111), ("b\\2.nef", 200, 222), ("c\\3.jpg", 300, 333)]
    assert compute_fingerprint(samples) == compute_fingerprint(list(reversed(samples)))


def test_fingerprint_is_case_and_separator_normalized() -> None:
    assert compute_fingerprint([("A/B.JPG", 1, 2)]) == compute_fingerprint([("a\\b.jpg", 1, 2)])


def test_fingerprint_changes_with_content_metadata() -> None:
    base = compute_fingerprint([("a.jpg", 100, 111)])
    assert base != compute_fingerprint([("a.jpg", 101, 111)])
    assert base != compute_fingerprint([("a.jpg", 100, 112)])
    assert base != compute_fingerprint([("b.jpg", 100, 111)])


def test_fingerprint_has_no_path_or_drive_dependency() -> None:
    """指纹只由相对路径 + size + mtime 决定，因此换盘符/换机器后不变。"""
    samples = [("杭州市\\a.jpg", 100, 111)]
    assert compute_fingerprint(samples) == compute_fingerprint(samples)
    assert "E:" not in compute_fingerprint(samples)


def test_fingerprint_limit_is_applied() -> None:
    """`limit` 是"最多取排序后的前 N 条"：因此传入**相同样本集合**时结果必须一致，
    而样本集合不同（即使都超过 limit）就必须得到不同指纹，否则指纹失去了区分力。
    """
    broad = [(f"{i:04d}.jpg", i, i) for i in range(500)]
    # 排序后前 10 条相同 → 指纹相同
    assert compute_fingerprint(broad, limit=10) == compute_fingerprint(list(broad), limit=10)
    # 排序后前 10 条不同 → 指纹必须不同（证明 limit 真的在生效）
    shifted = [(f"{i:04d}.jpg", i, i) for i in range(100, 600)]
    assert compute_fingerprint(broad, limit=10) != compute_fingerprint(shifted, limit=10)


# --------------------------------------------------------------------- 探测
def test_probe_new_path_suggests_create(db: Database, library_root: Path) -> None:
    result = probe_path(db, library_root)
    assert result.action == "create"
    assert result.is_dir
    assert result.needs_marker_write
    assert result.volume_identity is not None


def test_probe_missing_path_is_offline(db: Database, tmp_path: Path) -> None:
    result = probe_path(db, tmp_path / "不存在的盘" / "照片")
    assert result.action == "offline"
    assert not result.exists


def test_probe_finds_marker_and_reuses(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    write_marker(library_root, "uuid-abc", "我的照片库")
    library = store.create(name="我的照片库", root=library_root, library_uuid="uuid-abc", marker_uuid="uuid-abc")

    result = probe_path(db, library_root)
    assert result.action == "reuse"
    assert result.library is not None
    assert result.library.id == library.id


def test_probe_detects_drive_letter_change_as_rebind(db: Database, tmp_path: Path) -> None:
    """模拟 PRD 断点 #10：同一库换到别的路径（盘符/根目录变化）→ rebind，不是丢失。"""
    original = tmp_path / "E盘" / "照片"
    original.mkdir(parents=True)
    (original / "杭州市").mkdir()
    (original / "杭州市" / "a.jpg").write_bytes(b"jpeg")

    store = LibraryStore(db)
    write_marker(original, "uuid-move", "旅行照片")
    library = store.create(name="旅行照片", root=original, library_uuid="uuid-move", marker_uuid="uuid-move")

    moved = tmp_path / "新盘" / "照片"
    moved.parent.mkdir(parents=True)
    original.rename(moved)

    result = probe_path(db, moved)
    assert result.action == "rebind", "路径变化必须判为重绑定，绝不能判为新库或丢失"
    assert result.library is not None
    assert result.library.id == library.id
    assert result.library.uuid == "uuid-move"


def test_probe_adopts_foreign_marker_without_library_row(db: Database, library_root: Path) -> None:
    """带 PhotoOS 标记但本机数据库没记录（换机后）→ adopt，沿用原 uuid。"""
    write_marker(library_root, "uuid-from-pc-a", "PC-A 的库")
    result = probe_path(db, library_root)
    assert result.action == "adopt"
    assert result.marker is not None
    assert result.marker["library_uuid"] == "uuid-from-pc-a"


@pytest.mark.skipif(os.name != "nt", reason="卷序列号依赖 Win32")
def test_probe_matches_same_path_without_marker(db: Database, library_root: Path) -> None:
    """没有库标记时，同一路径仍可命中（路径提示判据）。"""
    store = LibraryStore(db)
    library = store.create(name="库", root=library_root)
    identity = store_mod.volume.volume_identity(library_root)
    assert identity is not None
    volume_record = store.record_volume(identity, library_id=library.id)
    store.link_volume_library(volume_record.id, library.id, library_root)

    result = probe_path(db, library_root)
    assert result.action == "reuse"
    assert result.library is not None
    assert result.library.id == library.id


@pytest.mark.skipif(os.name != "nt", reason="卷序列号依赖 Win32")
def test_two_libraries_on_one_volume_are_not_confused(db: Database, tmp_path: Path) -> None:
    """回归测试（真实现场：`C:\\Photos` 与 `E:\\视频` 在同一块盘）。

    卷序列号相同**不足以**判定库身份 —— 否则第二个库会被误判成第一个库并改写其路径提示。
    """
    volume_root = tmp_path
    photos = volume_root / "照片"
    videos = volume_root / "视频"
    photos.mkdir()
    videos.mkdir()
    (photos / "杭州市").mkdir()
    (videos / "杭州市").mkdir()

    store = LibraryStore(db)
    identity = store_mod.volume.volume_identity(volume_root)
    assert identity is not None

    photo_lib = store.create(name="我的照片库", root=photos)
    photo_vol = store.record_volume(identity, library_id=photo_lib.id)
    store.link_volume_library(photo_vol.id, photo_lib.id, photos)

    # 同一卷上登记第二个库
    result = probe_path(db, videos)
    assert result.action == "create", "同一卷上的新目录绝不能被判成既有库的重绑定"
    assert result.library is None

    video_lib = store.create(name="我的视频库", root=videos)
    store.link_volume_library(photo_vol.id, video_lib.id, videos)

    # 两个库都必须能被独立识别
    assert store.get(photo_lib.id).root_path_hint == str(photos)
    assert store.get(video_lib.id).root_path_hint == str(videos)
    libraries = store.libraries_for_volume(identity.serial)
    assert {lib.id for lib, _ in libraries} == {photo_lib.id, video_lib.id}

    assert probe_path(db, photos).library.id == photo_lib.id  # type: ignore[union-attr]
    assert probe_path(db, videos).library.id == video_lib.id  # type: ignore[union-attr]


@pytest.mark.skipif(os.name != "nt", reason="卷结构比对依赖 Win32")
def test_structural_match_when_marker_lost(db: Database, tmp_path: Path) -> None:
    """库标记丢失但目录结构仍在时，应通过结构重合度恢复识别（而不是当成新库）。"""
    root = tmp_path / "照片"
    for city in ("杭州市", "上海市", "哈尔滨市"):
        (root / city / "2025-01-01_0000-0100").mkdir(parents=True)
    store = LibraryStore(db)
    library = store.create(name="我的照片库", root=root)
    identity = store_mod.volume.volume_identity(root)
    assert identity is not None
    volume_record = store.record_volume(identity, library_id=library.id)
    store.link_volume_library(volume_record.id, library.id, root)

    # 写入与该库结构吻合的 asset 索引（结构比对的依据）
    with db.write() as conn:
        for city in ("杭州市", "上海市", "哈尔滨市"):
            conn.execute(
                "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, ?, 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')",
                (library.id, f"{city}\\2025-01-01_0000-0100\\a.jpg"),
            )

    # 模拟"标记丢失 + 路径变化"：清空路径提示，但目录结构一致
    store.set_root_hint(library.id, tmp_path / "旧位置" / "照片")
    result = probe_path(db, root)
    assert result.action == "rebind"
    assert result.library is not None
    assert result.library.id == library.id


@pytest.mark.skipif(os.name != "nt", reason="卷结构比对依赖 Win32")
def test_no_guess_when_structure_is_ambiguous(db: Database, tmp_path: Path) -> None:
    """结构与候选库都不吻合时，必须拒绝猜测，而不是硬塞进某个库。"""
    root = tmp_path / "照片"
    (root / "杭州市").mkdir(parents=True)
    other = tmp_path / "别处"
    other.mkdir()
    store = LibraryStore(db)
    library = store.create(name="别的库", root=other)
    identity = store_mod.volume.volume_identity(root)
    assert identity is not None
    volume_record = store.record_volume(identity, library_id=library.id)
    store.link_volume_library(volume_record.id, library.id, other)

    result = probe_path(db, root)
    assert result.action == "create"
    assert result.library is None


# --------------------------------------------------------------------- 读写
def test_create_and_get(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    library = store.create(name="我的照片", root=library_root)
    assert library.id > 0
    assert library.status == "ONLINE"
    assert library.root_path_hint == str(library_root)
    assert len(library.uuid) == 36  # uuid4

    fetched = store.get(library.id)
    assert fetched.uuid == library.uuid
    assert store.find_by_uuid(library.uuid).id == library.id  # type: ignore[union-attr]


def test_create_rejects_missing_root(db: Database, tmp_path: Path) -> None:
    store = LibraryStore(db)
    with pytest.raises(LibraryError) as excinfo:
        store.create(name="x", root=tmp_path / "nope")
    assert excinfo.value.detail["kind"] == "root_missing"


def test_get_missing_raises(db: Database) -> None:
    with pytest.raises(LibraryNotFound):
        LibraryStore(db).get(999)


def test_list_all_and_counts(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    assert store.list_all() == []
    first = store.create(name="A", root=library_root)
    second = store.create(name="B", root=library_root)
    assert [lib.id for lib in store.list_all()] == [first.id, second.id]
    assert store.live_asset_count(first.id) == 0


def test_set_root_hint_does_not_change_uuid(db: Database, library_root: Path) -> None:
    """路径只是运行时提示，更新它绝不能改变资产身份。"""
    store = LibraryStore(db)
    library = store.create(name="A", root=library_root)
    other = library_root.parent / "另一个位置"
    other.mkdir()
    store.set_root_hint(library.id, other)
    updated = store.get(library.id)
    assert updated.uuid == library.uuid
    assert updated.root_path_hint == str(other)


def test_status_transitions(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    library = store.create(name="A", root=library_root)
    assert library.is_online
    store.set_status(library.id, "OFFLINE")
    offline = store.get(library.id)
    assert not offline.is_online
    assert offline.status == "OFFLINE"


# --------------------------------------------------------------------- 卷记录
@pytest.mark.skipif(os.name != "nt", reason="卷记录依赖 Win32")
def test_record_volume_and_lookup(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    library = store.create(name="A", root=library_root)
    identity = store_mod.volume.volume_identity(library_root)
    assert identity is not None

    record = store.record_volume(identity, library_id=library.id, fingerprint="fp-1")
    assert record.library_id == library.id
    assert record.status == "ONLINE"
    assert record.serial == identity.serial

    if identity.has_serial:
        found = store.find_by_volume_serial(identity.serial)
        assert found is not None
        assert found[0].id == library.id

    # 重复登记同一卷不得产生第二条记录（幂等）
    again = store.record_volume(identity, library_id=library.id)
    assert again.id == record.id
    assert len(store.volumes_for(library.id)) == 1


@pytest.mark.skipif(os.name != "nt", reason="卷记录依赖 Win32")
def test_record_volume_updates_mount_hint_only(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    library = store.create(name="A", root=library_root)
    identity = store_mod.volume.volume_identity(library_root)
    assert identity is not None
    record = store.record_volume(identity, library_id=library.id)

    store.mark_volumes_offline(library.id)
    assert store.get_volume(record.id).status == "OFFLINE"

    store.record_volume(identity, library_id=library.id)
    refreshed = store.get_volume(record.id)
    assert refreshed.status == "ONLINE"
    assert refreshed.serial == identity.serial


def test_find_by_volume_serial_ignores_zero(db: Database) -> None:
    assert LibraryStore(db).find_by_volume_serial(0) is None


def test_recorded_marker_uuid_makes_probe_reuse(db: Database, library_root: Path) -> None:
    """回归测试：PhotoOS 自己写入标记后，必须把 marker_uuid 记入库，
    否则下次探测会退回"卷序列号"判据而不是更强的"库标记"判据。
    """
    store = LibraryStore(db)
    library = store.create(name="我的照片库", root=library_root)
    write_marker(library_root, library.uuid, library.name)
    store.set_marker_uuid(library.id, library.uuid)

    record = store.get(library.id)
    assert record.marker_uuid == library.uuid
    assert store.find_by_marker(library.uuid) is not None

    result = probe_path(db, library_root)
    assert result.action == "reuse"
    assert result.library is not None
    assert result.library.id == library.id


def test_log_event(db: Database, library_root: Path) -> None:
    store = LibraryStore(db)
    library = store.create(name="A", root=library_root)
    store.log_event("scan", library_id=library.id, payload={"files": 10})
    rows = db.query("SELECT kind, payload_json FROM app_event")
    assert len(rows) == 1
    assert rows[0]["kind"] == "scan"
    assert "10" in rows[0]["payload_json"]
