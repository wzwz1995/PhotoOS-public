"""迁移器与数据库连接测试。

覆盖 PHASE1_TECH_PLAN §5 的每条规则：单事务应用、幂等、checksum 防篡改、
版本兼容检查、迁移前在线备份、只前进不后退。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from photoos.db import (
    Database,
    apply_migrations,
    discover_migrations,
    pending_migrations,
    target_version,
    verify_checksums,
)
from photoos.db.migrator import MIGRATION_RE
from photoos.errors import MigrationError, NotInitializedError, SchemaVersionMismatch


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "photoos.db")


# --------------------------------------------------------------------- 迁移清单
def test_migrations_are_well_formed() -> None:
    migrations = discover_migrations()
    assert migrations, "至少需要一个迁移文件"
    assert migrations[0].version == 1
    versions = [m.version for m in migrations]
    assert versions == sorted(versions), "迁移必须按版本升序"
    assert len(versions) == len(set(versions)), "迁移版本号不得重复"
    for migration in migrations:
        assert MIGRATION_RE.match(migration.path.name)
        assert len(migration.checksum) == 64
        assert migration.sql.strip(), f"{migration.label} 内容为空"


def test_target_version_matches_last_migration() -> None:
    assert target_version() == discover_migrations()[-1].version


# --------------------------------------------------------------------- 应用迁移
def test_apply_creates_schema_and_sets_user_version(db: Database, tmp_path: Path) -> None:
    result = apply_migrations(db, backup_dir=tmp_path / "backups")
    assert result.from_version == 0
    assert result.to_version == target_version()
    assert result.applied == [m.label for m in discover_migrations()]
    assert result.backup_path is None, "首次建库无需备份"
    assert db.schema_version() == target_version()

    with db.read() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    expected = {
        "library", "volume", "session", "scene", "shot", "asset", "asset_instance",
        "metadata", "preview", "job", "job_item", "app_event", "schema_migrations",
        # Phase 2 预留（仅建表）
        "selection", "selection_event",
    }
    assert expected <= tables, f"缺少表：{expected - tables}"


def test_apply_is_idempotent(db: Database, tmp_path: Path) -> None:
    first = apply_migrations(db, backup_dir=tmp_path / "backups")
    second = apply_migrations(db, backup_dir=tmp_path / "backups")
    assert first.applied
    assert second.applied == []
    assert second.backup_path is None, "无待应用迁移时不应产生备份"
    assert second.from_version == second.to_version == target_version()
    assert pending_migrations(db) == []


def test_rollback_on_bad_migration(db: Database, tmp_path: Path) -> None:
    """迁移 SQL 出错时必须整体回滚，库保持原版本、不留下半截 schema。"""
    good = tmp_path / "migrations"
    good.mkdir()
    (good / "001_ok.sql").write_text("CREATE TABLE alpha (id INTEGER PRIMARY KEY);", encoding="utf-8")

    apply_migrations(db, directory=good)
    assert db.schema_version() == 1

    (good / "002_bad.sql").write_text(
        "CREATE TABLE beta (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE alpha (id INTEGER PRIMARY KEY);\n",  # 表已存在 -> 报错
        encoding="utf-8",
    )
    with pytest.raises(MigrationError):
        apply_migrations(db, directory=good)

    assert db.schema_version() == 1, "失败迁移不得推进版本"
    with db.read() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "beta" not in names, "失败迁移创建的表必须被回滚"


# --------------------------------------------------------------------- 防篡改
def test_checksum_mismatch_is_rejected(db: Database, tmp_path: Path) -> None:
    """篡改已应用的迁移文件必须被拒绝（且报"篡改"而非"缺失"）。"""
    apply_migrations(db, backup_dir=tmp_path / "backups")
    migrations = discover_migrations()

    # 逐字节复制全部迁移文件后篡改其中一个：
    # 必须用 copy2（不能用 write_text(sql)，那会改变换行等字节从而"误篡改"所有文件），
    # 否则会先命中"文件缺失"或"全部不匹配"判据，测不到目标行为。
    tampered = tmp_path / "migrations"
    tampered.mkdir()
    for migration in migrations:
        shutil.copy2(migration.path, tampered / migration.path.name)
    victim = migrations[-1]
    (tampered / victim.path.name).write_text(
        victim.sql + "\n-- 事后篡改\n", encoding="utf-8"
    )

    with pytest.raises(MigrationError) as excinfo:
        verify_checksums(db, tampered)
    assert excinfo.value.detail["kind"] == "checksum_mismatch"
    # 只有被篡改的那个迁移应被报告，且未篡改的迁移必须被视为一致
    labels = [item["label"] for item in excinfo.value.detail["migrations"]]
    assert labels == [victim.label]

    with pytest.raises(MigrationError):
        apply_migrations(db, directory=tampered, backup_dir=tmp_path / "backups")


def test_missing_migration_file_is_rejected(db: Database, tmp_path: Path) -> None:
    """已应用的迁移文件整体消失时，报「缺失」而不是「checksum 不匹配」。"""
    full = tmp_path / "full"
    full.mkdir()
    (full / "001_a.sql").write_text("CREATE TABLE t1 (id INTEGER);", encoding="utf-8")
    (full / "002_b.sql").write_text("CREATE TABLE t2 (id INTEGER);", encoding="utf-8")
    apply_migrations(db, directory=full)
    assert db.schema_version() == 2

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "001_a.sql").write_text("CREATE TABLE t1 (id INTEGER);", encoding="utf-8")

    with pytest.raises(MigrationError) as excinfo:
        verify_checksums(db, partial)
    assert excinfo.value.detail["kind"] == "missing_migration_file"
    assert excinfo.value.detail["versions"] == [2]

    with pytest.raises(MigrationError) as excinfo2:
        apply_migrations(db, directory=partial)
    assert excinfo2.value.detail["kind"] == "missing_migration_file"


# --------------------------------------------------------------------- 备份
def test_backup_before_upgrade(db: Database, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_first.sql").write_text(
        "CREATE TABLE thing (id INTEGER PRIMARY KEY, note TEXT);", encoding="utf-8"
    )
    apply_migrations(db, directory=migrations)

    with db.write() as conn:
        conn.execute("INSERT INTO thing (note) VALUES ('before-upgrade')")

    (migrations / "002_second.sql").write_text(
        "ALTER TABLE thing ADD COLUMN extra TEXT;", encoding="utf-8"
    )
    backups = tmp_path / "backups"
    result = apply_migrations(db, directory=migrations, backup_dir=backups)

    assert result.applied == ["002_second"]
    assert result.backup_path is not None and result.backup_path.exists()

    # 备份必须是升级前的可用快照
    with sqlite3.connect(result.backup_path) as backup_conn:
        rows = list(backup_conn.execute("SELECT note FROM thing"))
        columns = {r[1] for r in backup_conn.execute("PRAGMA table_info(thing)")}
    assert rows == [("before-upgrade",)]
    assert "extra" not in columns


def test_vacuum_into_does_not_disturb_open_db(db: Database, tmp_path: Path) -> None:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    target = tmp_path / "copy.db"
    db.vacuum_into(target)
    assert target.exists()
    assert Database(target).schema_version() == target_version()


# --------------------------------------------------------------------- 版本检查
def test_newer_db_is_rejected(db: Database, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_a.sql").write_text("CREATE TABLE t1 (id INTEGER);", encoding="utf-8")
    apply_migrations(db, directory=migrations)

    with db.write() as conn:  # 伪造成“来自更新版本”的库
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(SchemaVersionMismatch):
        apply_migrations(db, directory=migrations)


def test_bad_migration_filename_is_rejected(db: Database, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "not-numbered.sql").write_text("CREATE TABLE t (id INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError) as excinfo:
        discover_migrations(migrations)
    assert excinfo.value.detail["kind"] == "bad_migration_filename"


def test_explicit_transaction_in_migration_is_rejected(db: Database, tmp_path: Path) -> None:
    """迁移文件不得自带事务控制语句（否则嵌套事务，原子性无法保证）。"""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_txn.sql").write_text(
        "BEGIN;\nCREATE TABLE t (id INTEGER);\nCOMMIT;\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(db, directory=migrations)
    assert excinfo.value.detail["kind"] == "explicit_transaction"


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path / "nope")


# --------------------------------------------------------------------- 连接行为
def test_foreign_keys_are_enforced(db: Database, tmp_path: Path) -> None:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    with pytest.raises(sqlite3.IntegrityError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (999, 'x.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
            )


def test_wal_mode_enabled(db: Database, tmp_path: Path) -> None:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    with db.read() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_unique_library_relative_path_is_enforced(db: Database, tmp_path: Path) -> None:
    """同一 Library 内同一相对路径只能有一个 asset —— 重复扫描天然幂等。"""
    apply_migrations(db, backup_dir=tmp_path / "backups")
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, status, created_at)"
            " VALUES ('u-1', 'lib', 'ONLINE', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, 'a/b.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (1, 'a/b.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
            )


def test_query_helpers(db: Database, tmp_path: Path) -> None:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    assert db.scalar("SELECT COUNT(*) FROM library", default=-1) == 0
    assert db.query("SELECT * FROM library") == []
    assert db.query_one("SELECT * FROM library") is None
    assert db.integrity_check() == "ok"
    assert db.foreign_key_check() == []


def test_require_exists_raises(tmp_path: Path) -> None:
    db = Database(tmp_path / "missing.db")
    with pytest.raises(NotInitializedError):
        db.require_exists()


def test_read_only_connection_rejects_writes(db: Database, tmp_path: Path) -> None:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    ro = Database(db.path, read_only=True)
    assert ro.query("SELECT COUNT(*) AS n FROM library")[0]["n"] == 0
    with pytest.raises(sqlite3.OperationalError):
        with ro.write() as conn:
            conn.execute(
                "INSERT INTO library (uuid, name, status, created_at)"
                " VALUES ('u-x', 'x', 'ONLINE', '2026-01-01T00:00:00+08:00')"
            )
