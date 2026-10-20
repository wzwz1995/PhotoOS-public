"""重绑定与迁移包测试（PRD §5.1 / §5.2 / §12 LOCKED）。

这些测试守护三条最重要的承诺：
  1. 盘符/路径变化**不会**让照片"消失"，也**不会**触发重新导入。
  2. 迁移包能完整带走索引、元数据与派生缓存，且**照片原文件不在包内**。
  3. 迁移/重绑定全程只读原片，且失败时可回退。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from photoos.config import Settings
from photoos.db import Database, apply_migrations, target_version
from photoos.errors import MigrationPackageError
from photoos.jobs import JobQueue, JobRunner
from photoos.library import migration as migration_mod
from photoos.library import rebind as rebind_mod
from photoos.library.store import LibraryStore, write_marker
from photoos.repos import AssetRepo, MetadataRepo, PreviewRepo


# --------------------------------------------------------------------- fixtures
def _photo(path: Path, seed: int) -> Path:
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", (640, 480), (seed * 11 % 256, seed * 23 % 256, seed * 37 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = f"2025:10:25 10:{seed:02d}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    sub[0x829D] = IFDRational(71, 10)
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def indexed(tmp_path: Path):
    """一个已完整索引的小库（含元数据、预览、哈希）。返回 (db, settings, library_id, root)。"""
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    root = tmp_path / "E盘" / "照片"
    (root / "杭州市" / "2025-10-25_1000-1100").mkdir(parents=True)
    (root / "上海市" / "2025-10-26_1000-1100").mkdir(parents=True)
    for index in range(1, 6):
        _photo(root / "杭州市" / "2025-10-25_1000-1100" / f"DSC_{6000 + index}.JPG", index)
    for index in range(1, 3):
        _photo(root / "上海市" / "2025-10-26_1000-1100" / f"DSC_{7000 + index}.JPG", index)

    write_marker(root, "uuid-migrate", "我的照片库")
    store = LibraryStore(db)
    library = store.create(
        name="我的照片库", root=root, library_uuid="uuid-migrate", marker_uuid="uuid-migrate"
    )

    queue = JobQueue(db)
    job = queue.create("full_index", library_id=library.id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED", report.error
    return db, settings, library.id, root


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


# ===================================================================== 重绑定
def test_rebind_after_path_change(indexed, tmp_path: Path) -> None:
    """PRD 断点 #10：`D:\\PHOTO_LIBRARY` → `E:\\PHOTO_LIBRARY` 不得判定照片丢失。"""
    db, _settings, library_id, root = indexed
    before = _snapshot(root)

    new_root = tmp_path / "新盘" / "照片"
    new_root.parent.mkdir(parents=True)
    shutil.move(str(root), str(new_root))

    report = rebind_mod.rebind_library(db, library_id, new_root, verify_hashes=True)

    assert report.rebound, report.warnings
    assert report.verified_by == rebind_mod.VERIFY_HASH
    assert report.hash_mismatch == 0
    assert report.found == 7 and report.missing == 0
    assert report.confidence.startswith("高")

    library = LibraryStore(db).get(library_id)
    assert library.root_path_hint == str(new_root)
    assert library.status == "ONLINE"
    assert library.uuid == "uuid-migrate", "重绑定绝不能改变资产身份"

    # 原片一个字节都没动
    assert _snapshot(new_root) == before

    # Asset 数量不变，且没有产生新行（没有"重新导入"）
    assert AssetRepo(db).count(library_id) == 7


def test_rebind_dry_run_does_not_touch_database(indexed, tmp_path: Path) -> None:
    db, _settings, library_id, root = indexed
    new_root = tmp_path / "别处" / "照片"
    new_root.parent.mkdir(parents=True)
    shutil.move(str(root), str(new_root))

    report = rebind_mod.rebind_library(db, library_id, new_root, apply=False)
    assert not report.rebound
    assert any("预演" in item for item in report.warnings)
    # 数据库仍指向旧路径
    assert LibraryStore(db).get(library_id).root_path_hint == str(root)
    assert not root.exists()


def test_rebind_refuses_wrong_directory(indexed, tmp_path: Path) -> None:
    """选错目录时必须拒绝，而不是把整库标成丢失。"""
    db, _settings, library_id, _root = indexed
    wrong = tmp_path / "无关目录"
    wrong.mkdir()
    (wrong / "随便.txt").write_text("x", encoding="utf-8")

    report = rebind_mod.rebind_library(db, library_id, wrong)
    assert not report.rebound
    assert report.found == 0
    assert any("选错了目录" in item for item in report.warnings)
    # 索引与状态不得被破坏
    assert AssetRepo(db).count(library_id) == 7
    assert LibraryStore(db).get(library_id).root_path_hint != str(wrong)


def test_rebind_refuses_when_content_differs(indexed, tmp_path: Path) -> None:
    """新位置文件名相同但内容不同 → 拒绝重绑定（可能是另一棵树）。"""
    db, _settings, library_id, root = indexed
    new_root = tmp_path / "另一棵树" / "照片"
    shutil.copytree(root, new_root)
    # 篡改其中一个文件的内容（保持路径与大小结构）
    victim = new_root / "杭州市" / "2025-10-25_1000-1100" / "DSC_6001.JPG"
    _photo(victim, seed=200)

    report = rebind_mod.rebind_library(db, library_id, new_root, sample=6, verify_hashes=True)
    assert not report.rebound
    assert report.hash_mismatch >= 1
    assert any("不一致" in item for item in report.warnings)


def test_rebind_marks_missing_files_without_deleting(indexed, tmp_path: Path) -> None:
    """部分文件在新位置缺失 → 标 MISSING，行数不变。"""
    db, _settings, library_id, root = indexed
    new_root = tmp_path / "新盘" / "照片"
    shutil.copytree(root, new_root)
    (new_root / "上海市" / "2025-10-26_1000-1100" / "DSC_7001.JPG").unlink()

    report = rebind_mod.rebind_library(db, library_id, new_root, sample=2)
    assert report.rebound
    assert report.missing == 1 and report.found == 6
    assets = AssetRepo(db)
    assert assets.count(library_id) == 7
    missing = assets.by_relative(library_id, "上海市\\2025-10-26_1000-1100\\DSC_7001.JPG")
    assert missing is not None and missing.status == "MISSING"


def test_rebind_without_hashes_uses_metadata_verification(indexed, tmp_path: Path) -> None:
    """尚未哈希的库也能重绑定：退化为 size+mtime 校验，并如实标注置信度。"""
    db, _settings, library_id, root = indexed
    db.execute("UPDATE asset SET content_hash = NULL, hash_algo = NULL WHERE library_id = ?", (library_id,))

    new_root = tmp_path / "新盘" / "照片"
    shutil.copytree(root, new_root)
    report = rebind_mod.rebind_library(db, library_id, new_root, verify_hashes=True)
    assert report.rebound
    assert report.verified_by == rebind_mod.VERIFY_META
    assert report.confidence.startswith("中")


def test_rebind_logs_event(indexed, tmp_path: Path) -> None:
    db, _settings, library_id, root = indexed
    new_root = tmp_path / "新盘" / "照片"
    shutil.copytree(root, new_root)
    rebind_mod.rebind_library(db, library_id, new_root)

    events = db.query("SELECT kind, payload_json FROM app_event WHERE kind = 'rebind'")
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["new_root"] == str(new_root)
    assert payload["verified_by"] == rebind_mod.VERIFY_HASH


def test_rebind_offline_path(indexed, tmp_path: Path) -> None:
    """路径不可达（硬盘没插）→ 报告离线，不改数据库。"""
    db, _settings, library_id, _root = indexed
    report = rebind_mod.rebind_library(db, library_id, tmp_path / "根本没插的盘" / "照片")
    assert not report.rebound
    assert report.warnings


def test_mark_offline_keeps_index_usable(indexed) -> None:
    """PRD 断点 #5：拔盘后仍能浏览缩略图、EXIF 与基础元数据。"""
    db, settings, library_id, _root = indexed
    rebind_mod.mark_library_offline(db, library_id, reason="拔盘测试")

    assert LibraryStore(db).get(library_id).status == "OFFLINE"
    # 索引、元数据、预览都还在
    assert AssetRepo(db).count(library_id) == 7
    assert MetadataRepo(db).count(library_id) == 7
    assert PreviewRepo(db).count(kind="thumb", library_id=library_id) == 7

    thumb = PreviewRepo(db).get(AssetRepo(db).pending(library_id)[0].id, "thumb")
    assert thumb is not None
    assert (settings.data_root / str(thumb["path"])).exists()


def test_library_availability(indexed) -> None:
    db, _settings, library_id, root = indexed
    reachable, total, sampled = rebind_mod.library_availability(db, library_id)
    assert reachable and total == 7 and sampled > 0

    shutil.rmtree(root)
    reachable2, total2, sampled2 = rebind_mod.library_availability(db, library_id)
    assert not reachable2 and total2 == 7 and sampled2 == 0


# ===================================================================== 迁移包
def test_export_package_contents(indexed, tmp_path: Path) -> None:
    db, settings, library_id, _root = indexed
    target = tmp_path / "migration.zip"
    report = migration_mod.export_package(db, settings.data_root, target)

    assert target.exists()
    assert report.schema_version == target_version()
    assert report.libraries == 1 and report.assets == 7
    assert report.files > 0

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "photoos.db", "libraries.json"} <= names
        assert any(name.startswith("thumbnails/") for name in names)
        assert any(name.startswith("previews/") for name in names)

        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["package_format"] == migration_mod.PACKAGE_FORMAT
        assert manifest["photo_files_included"] is False, "照片原文件默认不得入包（PRD §5.1）"
        assert manifest["schema_version"] == target_version()
        assert manifest["stats"]["assets"] == 7
        # 每个成员都有校验和
        assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_export_does_not_include_photo_originals(indexed, tmp_path: Path) -> None:
    """照片原文件绝不能被收进迁移包。"""
    db, settings, library_id, root = indexed
    target = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, target)

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert not any(name.upper().endswith((".JPG", ".JPEG", ".NEF", ".MOV")) for name in names)
        assert not any("DSC_" in name for name in names)
        # 包内不得出现照片库路径
        assert not any(str(root).replace("\\", "/") in name for name in names)


def test_libraries_json_records_identity_not_drive_letter(indexed, tmp_path: Path) -> None:
    """迁移元数据以 uuid 为身份；盘符只作为"最后位置提示"。"""
    db, settings, library_id, root = indexed
    # 登记卷身份（真实流程由 `library add` / `rebind` 完成）
    from photoos.library.volume import volume_identity

    identity = volume_identity(root)
    if identity is not None:
        store = LibraryStore(db)
        record = store.record_volume(identity, library_id=library_id, fingerprint="fp-test")
        store.link_volume_library(record.id, library_id, root)

    target = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, target)

    with zipfile.ZipFile(target) as archive:
        payload = json.loads(archive.read("libraries.json").decode("utf-8"))
    assert len(payload["libraries"]) == 1
    entry = payload["libraries"][0]
    assert entry["uuid"] == "uuid-migrate"
    assert entry["asset_count"] == 7
    assert "last_root_hint" in entry, "路径只能作为提示存在"
    assert entry["volumes"], "应记录卷指纹以便整盘更换后定位"
    volume_entry = entry["volumes"][0]
    assert "serial_hex" in volume_entry and "fingerprint" in volume_entry
    assert "last_mount_hint" in volume_entry, "盘符只能作为提示字段出现"


def test_verify_package_ok(indexed, tmp_path: Path) -> None:
    db, settings, library_id, _root = indexed
    target = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, target)

    result = migration_mod.verify_package(target)
    assert result.ok
    assert result.checked > 0
    assert not result.missing and not result.corrupt
    assert result.manifest is not None


def test_verify_package_detects_corruption(indexed, tmp_path: Path) -> None:
    """篡改包内成员必须被检出（迁移包不能被静默损坏）。"""
    db, settings, library_id, _root = indexed
    good = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, good)

    with zipfile.ZipFile(good) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["libraries.json"] = b'{"libraries":[]}'

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    result = migration_mod.verify_package(tampered)
    assert not result.ok
    assert "libraries.json" in result.corrupt


def test_verify_package_detects_missing_member(indexed, tmp_path: Path) -> None:
    db, settings, library_id, _root = indexed
    good = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, good)

    with zipfile.ZipFile(good) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.pop("libraries.json")

    broken = tmp_path / "missing.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    result = migration_mod.verify_package(broken)
    assert not result.ok
    assert "libraries.json" in result.missing


def test_verify_package_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MigrationPackageError):
        migration_mod.verify_package(tmp_path / "不存在.zip")


def test_verify_package_rejects_non_zip(tmp_path: Path) -> None:
    bogus = tmp_path / "notazip.zip"
    bogus.write_bytes(b"this is not a zip")
    with pytest.raises(MigrationPackageError):
        migration_mod.verify_package(bogus)


def test_verify_rejects_newer_schema(indexed, tmp_path: Path) -> None:
    """包的 schema 版本高于本程序时必须拒绝 —— 绝不降级导入损坏数据。"""
    db, settings, library_id, _root = indexed
    good = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, good)

    with zipfile.ZipFile(good) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"].decode("utf-8"))
    manifest["schema_version"] = target_version() + 5
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    future = tmp_path / "future.zip"
    with zipfile.ZipFile(future, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    result = migration_mod.verify_package(future)
    assert not result.ok
    assert any("高于本程序支持" in item for item in result.warnings)


def test_export_package_rejects_no_libraries(tmp_path: Path) -> None:
    """空库导出不应崩溃，但必须给出明确警告。"""
    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    report = migration_mod.export_package(db, settings.data_root, tmp_path / "empty.zip")
    assert report.libraries == 0
    assert any("没有 Library" in item for item in report.warnings)


# ===================================================================== 导入
def test_import_round_trip_restores_index(indexed, tmp_path: Path) -> None:
    """PC-A 导出 → PC-B 导入：索引/元数据/预览全部保留，且不需要重新导入照片。"""
    db, settings, library_id, root = indexed
    package = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, package)

    # 模拟 PC-B：全新的数据根
    pc_b_root = tmp_path / "pc-b" / "data"
    report = migration_mod.import_package(package, pc_b_root)
    assert report.applied, report.warnings
    assert report.assets == 7 and report.libraries == 1

    new_db = Database(pc_b_root / "photoos.db")
    new_settings = Settings(data_root=pc_b_root)
    assert new_db.schema_version() == target_version()
    assert AssetRepo(new_db).count(library_id) == 7
    assert MetadataRepo(new_db).count(library_id) == 7
    assert PreviewRepo(new_db).count(library_id=library_id) == 14
    # 预览文件也被带回（重建代价高的派生数据）
    thumb = PreviewRepo(new_db).get(AssetRepo(new_db).pending(library_id)[0].id, "thumb")
    assert thumb is not None and (pc_b_root / str(thumb["path"])).exists()
    # 迁移后仍然只依赖 uuid 与相对路径
    assert LibraryStore(new_db).get(library_id).uuid == "uuid-migrate"
    del new_settings

    # 原数据根未被破坏
    assert (settings.data_root / "photoos.db").exists()


def test_import_backs_up_existing_data_root(indexed, tmp_path: Path) -> None:
    """导入到已有数据的机器时必须先备份，并写 restore.json 说明回退方法。"""
    db, settings, library_id, _root = indexed
    package = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, package)

    report = migration_mod.import_package(package, settings.data_root)
    assert report.applied
    assert report.backup_path is not None and report.backup_path.exists()
    assert (report.backup_path / "photoos.db").exists()

    restore = json.loads((settings.data_root / "restore.json").read_text(encoding="utf-8"))
    assert restore["previous_data_root_backup"] == str(report.backup_path)
    assert "回退步骤" in restore["how_to_rollback"]
    assert restore["next_steps"]


def test_import_dry_run_changes_nothing(indexed, tmp_path: Path) -> None:
    db, settings, library_id, _root = indexed
    package = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, package)
    before = _snapshot(settings.data_root)

    report = migration_mod.import_package(package, settings.data_root, apply=False)
    assert not report.applied
    assert _snapshot(settings.data_root) == before
    assert not (settings.data_root / "restore.json").exists()


def test_import_rejects_corrupt_package(indexed, tmp_path: Path) -> None:
    db, settings, library_id, _root = indexed
    good = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, good)

    with zipfile.ZipFile(good) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["photoos.db"] = members["photoos.db"] + b"corrupted"

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    before = _snapshot(settings.data_root)
    report = migration_mod.import_package(bad, settings.data_root)
    assert not report.applied
    assert _snapshot(settings.data_root) == before, "校验失败时不得改动数据"


def test_import_rejects_path_traversal(indexed, tmp_path: Path) -> None:
    """防目录穿越：包内不能出现 `..` 之类的成员名。"""
    db, settings, library_id, _root = indexed
    good = tmp_path / "migration.zip"
    migration_mod.export_package(db, settings.data_root, good)

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(good) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(evil, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        archive.writestr("../../../../evil.txt", b"pwned")

    # 让校验通过（把恶意成员补进 manifest）
    with zipfile.ZipFile(evil) as archive:
        members2 = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members2["manifest.json"].decode("utf-8"))
    import hashlib

    manifest["files"].append(
        {
            "name": "../../../../evil.txt",
            "size": len(b"pwned"),
            "sha256": hashlib.sha256(b"pwned").hexdigest(),
        }
    )
    members2["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(evil, "w") as archive:
        for name, payload in members2.items():
            archive.writestr(name, payload)

    with pytest.raises(MigrationPackageError, match="穿越|非法"):
        migration_mod.import_package(evil, settings.data_root)
    assert not (tmp_path / "evil.txt").exists()


# ===================================================================== 备份
def test_backup_and_list_and_prune(indexed) -> None:
    db, settings, library_id, _root = indexed
    first = migration_mod.backup_database(db, settings.backups_dir, label="test")
    assert first.exists()
    assert Database(first, read_only=True).quick_check() == "ok"

    # 造几份旧备份（mtime 靠后以便区分）
    import os
    import time as time_mod

    extra = []
    for index in range(3):
        path = settings.backups_dir / f"photoos_old{index}.db"
        path.write_bytes(b"x")
        os.utime(path, (time_mod.time() - 1000 + index, time_mod.time() - 1000 + index))
        extra.append(path)

    entries = migration_mod.list_backups(settings.backups_dir)
    assert len(entries) == 4  # test + old0..old2（另有一次 full_index 前的迁移备份？无）

    removed = migration_mod.prune_backups(settings.backups_dir, keep=2)
    assert len(removed) == 2
    assert len(migration_mod.list_backups(settings.backups_dir)) == 2


def test_prune_backups_ignores_foreign_files(indexed) -> None:
    """清理备份只能删本系统生成的 photoos_*.db，不能碰用户放进来的文件。"""
    db, settings, library_id, _root = indexed
    keeper = settings.backups_dir / "用户手动放的重要文件.db"
    keeper.write_bytes(b"keep me")
    other = settings.backups_dir / "migration.zip"
    other.write_bytes(b"zip")

    migration_mod.backup_database(db, settings.backups_dir, label="a")
    migration_mod.prune_backups(settings.backups_dir, keep=0)

    assert keeper.exists(), "不得删除用户文件"
    assert other.exists(), "不得删除迁移包"
