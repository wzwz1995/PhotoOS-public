"""Phase 2 · M0：迁移 006 与"事务关键字判据"的回归测试。

分两部分：
  A. **迁移 006 的结构契约** —— 新增表/列/索引/触发器是否如方案所定，
     以及铁律 #2（不出现身份性路径）在 schema 层面是否成立。
  B. **事务关键字判据的修正**（`migrator._explicit_transaction`）——
     修复前的实现是"逐行看是否以 BEGIN 开头"，把 `CREATE TRIGGER ... BEGIN ... END;`
     误判成事务控制语句（真实误报：006 第一次应用就是这么失败的）。
     SQLite 的触发器**语法上必须含 BEGIN**（实测 BEGIN-less 触发器是语法错误），
     因此判据必须是"**独立语句**以事务关键字开头"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.db.migrator import _explicit_transaction, target_version

# ============================================================ 固定夹具

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "photoos.db")


@pytest.fixture()
def migrated(db: Database, tmp_path: Path) -> Database:
    apply_migrations(db, backup_dir=tmp_path / "backups")
    return db


def _columns(db: Database, table: str) -> dict[str, sqlite3.Row]:
    with db.read() as conn:
        return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _objects(db: Database, kind: str) -> set[str]:
    with db.read() as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
        }


# ============================================================ A. 迁移 006 结构

def test_006_applied_and_schema_is_at_target(migrated: Database) -> None:
    """006 已被应用，且库停在**当时的目标版本**。

    ⚠️ 这里刻意**不写死数字**：Phase 3 加了迁移 007（语义搜索）之后，
    原来那句 `assert schema_version() == 6` 就不再是"006 应用成功"的判据，
    而变成了"永远不许再加迁移" —— 那是把测试写成了绊脚石。
    真正该断言的是"迁移全部应用完毕、且正好等于迁移目录里的最高版本"。
    006 自身的结构契约由下面几条（表/列/索引/触发器）分别守着。
    """
    assert migrated.schema_version() == target_version()
    assert migrated.schema_version() >= 6, "006 必须已应用"


def test_006_creates_phase2_tables(migrated: Database) -> None:
    assert {"collection", "collection_item", "embedding"} <= _objects(migrated, "table")


def test_006_adds_phase2_columns(migrated: Database) -> None:
    assert "review_config_json" in _columns(migrated, "session")
    assert {"retired_at", "retired_reason"} <= set(_columns(migrated, "shot"))
    assert "evidence_json" in _columns(migrated, "scene")


def test_006_adds_indexes(migrated: Database) -> None:
    indexes = _objects(migrated, "index")
    expected = {
        "idx_collection_sort",
        "idx_collection_item_shot",
        "idx_collection_item_order",
        "idx_embedding_asset",
        "idx_embedding_identity",
        "idx_embedding_created",
        "idx_shot_active",
        "idx_shot_scene_rev",
        "idx_selection_status",
        "idx_selection_updated",
        "idx_sel_event_to",
    }
    assert expected <= indexes, f"缺少索引：{expected - indexes}"


def test_006_adds_status_guard_trigger(migrated: Database) -> None:
    assert "trg_selection_event_status_insert" in _objects(migrated, "trigger")


def test_007_semantic_tables_and_indexes(migrated: Database) -> None:
    """迁移 007：语义搜索的两张表与索引（Phase 3）。"""
    assert {"semantic_query", "semantic_query_hit"} <= _objects(migrated, "table")
    indexes = _objects(migrated, "index")
    assert {"idx_semantic_query_text", "idx_semantic_hit_query_rank",
            "idx_semantic_hit_shot"} <= indexes


def test_007_semantic_tables_have_no_identity_paths(migrated: Database) -> None:
    """铁律 #2 同样适用于 Phase 3 的新表：一个路径字段都不许有。

    与 Phase 2 那张表同一判据（列名含 path/root/drive/... 即违规）。
    """
    forbidden = ("path", "root", "drive", "volume", "folder", "dir_", "absolute")
    offenders: list[str] = []
    for table in ("semantic_query", "semantic_query_hit"):
        for name in _columns(migrated, table):
            if any(bad in name.lower() for bad in forbidden):
                offenders.append(f"{table}.{name}")
    assert not offenders, f"Phase 3 新表出现了疑似身份性路径列：{offenders}"


def test_007_hit_cascades_when_the_shot_disappears(migrated: Database) -> None:
    """★ 反向验证：删掉 Shot 必须**级联清掉**语义命中。

    为什么值得断言：`semantic_query_hit.shot_id` 是 `ON DELETE CASCADE`，
    与 `selection` / `collection_item` 对 shot 的处理**同一套语义**。
    如果哪天有人把它改成 SET NULL 或干脆去掉外键，
    界面就会去渲染一个不存在的 Shot —— 本项目在"引用 vs 软删除"这一族上
    已经连出过六处缺陷（PHASE2_DELIVERY §6.7–§6.12），所以这里从一开始就钉住。
    """
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (id, uuid, name, status, created_at)"
            " VALUES (1, 'u1', 'L', 'ONLINE', '2026-01-01')")
        conn.execute(
            "INSERT INTO session (id, name, source, group_revision, created_at, updated_at)"
            " VALUES (1, 'p', 'auto', 1, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT INTO scene (id, session_id, name, source, group_revision, created_at, updated_at)"
            " VALUES (1, 1, 's', 'auto', 1, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT INTO shot (id, scene_id, captured_at, group_source, group_revision,"
            " created_at, updated_at)"
            " VALUES (1, 1, '2026-01-01', 'auto', 1, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT INTO semantic_query (id, query_text, model, model_version, dim,"
            " normalization, vector, created_at, updated_at)"
            " VALUES (1, '日出', 'm', 'v', 4, 'l2', X'00000000', '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT INTO semantic_query_hit (query_id, shot_id, rank, score, computed_at)"
            " VALUES (1, 1, 0, 0.5, '2026-01-01')")
    with migrated.write() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_query_hit").fetchone()[0] == 1
        conn.execute("DELETE FROM shot WHERE id = 1")
    with migrated.read() as conn:
        left = conn.execute("SELECT COUNT(*) FROM semantic_query_hit").fetchone()[0]
        # 查询本身不该被删（它只是"被问过的词"），只有命中随 Shot 走
        queries = conn.execute("SELECT COUNT(*) FROM semantic_query").fetchone()[0]
    assert left == 0, "删 Shot 后命中必须级联消失，否则界面会渲染不存在的 Shot"
    assert queries == 1, "查询身份不该随 Shot 消失"


def test_012_adds_collection_hierarchy(migrated: Database) -> None:
    """迁移 012：主题上下级关系 —— collection.parent_id + 反查索引。"""
    cols = _columns(migrated, "collection")
    assert "parent_id" in cols
    assert "idx_collection_parent" in _objects(migrated, "index")


def test_012_parent_id_is_self_referential_fk_with_set_null(migrated: Database) -> None:
    """★ 删除父主题时，子主题**升为顶层**（SET NULL），不被级联删除。

    铁律 #5：删一个主题不该连带吞掉用户的其它主题。这条测试把"为什么用
    SET NULL 而不是 CASCADE"钉成可执行证据。
    """
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO collection (uuid, name, created_at, updated_at)"
            " VALUES ('u1', '世界树之巅', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
        parent = conn.execute("SELECT id FROM collection WHERE uuid = 'u1'").fetchone()[0]
        conn.execute(
            "INSERT INTO collection (uuid, name, parent_id, created_at, updated_at)"
            " VALUES ('u2', '奥尔梅克', ?, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')",
            (parent,),
        )
        conn.execute("DELETE FROM collection WHERE id = ?", (parent,))
    with migrated.read() as conn:
        row = conn.execute("SELECT name, parent_id FROM collection WHERE uuid = 'u2'").fetchone()
        assert row is not None, "删除父主题后子主题必须还在"
        assert row["name"] == "奥尔梅克"
        assert row["parent_id"] is None, "删除父主题后子主题应升为顶层"



    """006 是对 001-005 的**纯增量**：不得重建或丢失任何既有表。"""
    tables = _objects(migrated, "table")
    phase1 = {
        "library", "volume", "volume_library", "session", "scene", "shot", "asset",
        "asset_instance", "metadata", "preview", "job", "job_item", "app_event",
        "selection", "selection_event", "schema_migrations",
    }
    assert phase1 <= tables, f"006 丢失了 Phase 1 的表：{phase1 - tables}"


def test_migrations_are_idempotent(migrated: Database, tmp_path: Path) -> None:
    """重复 init 不应再次应用任何迁移，也不应报错。

    同样**不写死版本号**：判据是"没有任何新迁移被应用、版本不变"，
    而不是"版本恰好是 6"（见上面那条的说明）。
    """
    before = migrated.schema_version()
    second = apply_migrations(migrated, backup_dir=tmp_path / "backups")
    assert second.applied == []
    assert second.from_version == second.to_version == before


# ------------------------------------------------------------ 铁律 #2 的 schema 层断言

def test_new_tables_contain_no_identity_path_columns(migrated: Database) -> None:
    """铁律 #2：数据库内不出现身份性路径。

    Phase 2 新表**一个路径字段都不该有** —— 主题只引用 shot_id，嵌入只引用 asset_id。
    列名里出现 path/root/drive/volume/folder 都视为违规（需要显式讨论后才能放行）。
    """
    forbidden = ("path", "root", "drive", "volume", "folder", "dir_", "absolute")
    offenders: list[str] = []
    for table in ("collection", "collection_item", "embedding"):
        for name in _columns(migrated, table):
            lowered = name.lower()
            if any(bad in lowered for bad in forbidden):
                offenders.append(f"{table}.{name}")
    assert not offenders, f"Phase 2 新表出现了疑似身份性路径列：{offenders}"


def test_collection_uuid_is_unique(migrated: Database) -> None:
    """collection.uuid 是跨机迁移的稳定身份，必须唯一。"""
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO collection (uuid, name, created_at, updated_at)"
            " VALUES ('u1', '蓝调时刻', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute(
                "INSERT INTO collection (uuid, name, created_at, updated_at)"
                " VALUES ('u1', '重复', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
            )


def test_embedding_identity_is_unique_so_history_is_never_overwritten(
    migrated: Database,
) -> None:
    """铁律 #4 / PRD §9.3：模型升级**新增行**，绝不覆盖历史。

    同一 (asset, model, model_version, prep_version) 重复写入必须被拒绝；
    而换一个 model_version 则应当成功 —— 这就是"多版本并存"。
    """
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status) VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, '城市\\时段\\a.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )

    def insert(version: str) -> None:
        with migrated.write() as conn:
            conn.execute(
                "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
                " input_kind, input_version, prep_version, vector, created_at)"
                " VALUES (1, 'clip-vit-base-patch32', ?, 512, 'l2', 'preview', 'p1.0', 'p1.0', ?,"
                " '2026-01-01T00:00:00+08:00')",
                (version, b"\x00" * (512 * 4)),
            )

    insert("v1")
    with pytest.raises(sqlite3.IntegrityError):
        insert("v1")  # 同版本重复 -> 拒绝（不可覆盖历史）
    insert("v2")  # 换版本 -> 允许（多版本并存）


# ------------------------------------------------------------ 触发器：状态白名单

def test_trigger_rejects_unknown_selection_status(migrated: Database) -> None:
    """Phase 2 开始真正写 selection_event，因此状态必须是四态之一。"""
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status) VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (name, created_at, updated_at) VALUES ('S','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at) VALUES (1, 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO selection_event (shot_id, from_status, to_status, created_at)"
            " VALUES (1, 'UNREVIEWED', 'CANDIDATE', '2026-01-01T00:00:00+08:00')"
        )

    for bad_from, bad_to in (("BOGUS", "CANDIDATE"), ("UNREVIEWED", "BOGUS")):
        with pytest.raises(sqlite3.IntegrityError):
            with migrated.write() as conn:
                conn.execute(
                    "INSERT INTO selection_event (shot_id, from_status, to_status, created_at)"
                    " VALUES (1, ?, ?, '2026-01-01T00:00:00+08:00')",
                    (bad_from, bad_to),
                )


def test_trigger_allows_null_from_status(migrated: Database) -> None:
    """首次进入状态机时 from_status 为 NULL（PRD：从"未审阅"开始不必有前驱状态）。"""
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status) VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (name, created_at, updated_at) VALUES ('S','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at) VALUES (1, 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO selection_event (shot_id, from_status, to_status, created_at)"
            " VALUES (1, NULL, 'FINAL', '2026-01-01T00:00:00+08:00')"
        )
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM selection_event").fetchone()[0] == 1


# ------------------------------------------------------------ 只增不删：外键方向

def test_retiring_a_shot_does_not_delete_selection(migrated: Database) -> None:
    """决策 #4 的核心：重算时"标记退役"而不是删除，因此精选**不会**被级联删掉。

    这条测试同时说明"为什么不能物理删除"：selection.shot_id 是 ON DELETE CASCADE。
    """
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status) VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (name, created_at, updated_at) VALUES ('S','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at) VALUES (1, 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO selection (shot_id, status, updated_at) VALUES (1, 'FINAL', '2026-01-01T00:00:00+08:00')"
        )
        # 只增不删：标退役
        conn.execute(
            "UPDATE shot SET retired_at = ?, retired_reason = 'merged_into:2' WHERE id = 1",
            ("2026-01-02T00:00:00+08:00",),
        )

    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM selection").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM selection WHERE status='FINAL'").fetchone()[0] == 1


def test_physically_deleting_a_shot_would_cascade_selection(migrated: Database) -> None:
    """反证：**若**物理删除 Shot，精选会被级联删掉 —— 这正是决策 #4 要禁止的。

    这条测试把"为什么必须只增不删"钉成可执行的证据，而不是注释里的一句话。
    """
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status) VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (name, created_at, updated_at) VALUES ('S','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at) VALUES (1, 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO selection (shot_id, status, updated_at) VALUES (1, 'FINAL', '2026-01-01T00:00:00+08:00')"
        )
        conn.execute("DELETE FROM shot WHERE id = 1")

    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM selection").fetchone()[0] == 0


# ============================================================ B. 事务关键字判据

def test_finds_real_transaction_statements() -> None:
    assert _explicit_transaction("BEGIN;\nCREATE TABLE t (id INTEGER);\nCOMMIT;") == "BEGIN"
    assert _explicit_transaction("CREATE TABLE t (id INTEGER);\nCOMMIT;") == "COMMIT"
    assert _explicit_transaction("SAVEPOINT sp1;") == "SAVEPOINT"
    assert _explicit_transaction("  rollback  ;") == "ROLLBACK"


def test_allows_trigger_body_begin() -> None:
    """真实误报：SQLite 触发器语法上必须含 BEGIN...END;"""
    sql = (
        "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW\n"
        "WHEN NEW.x NOT IN ('A','B')\n"
        "BEGIN\n"
        "    SELECT RAISE(ABORT, 'bad');\n"
        "END;\n"
    )
    assert _explicit_transaction(sql) is None


def test_allows_case_begin_form() -> None:
    """SQLite 的 CASE 允许 BEGIN 形式（与 END 配对），也是合法用法。"""
    sql = "SELECT CASE WHEN 1 THEN BEGIN SELECT 1; END; END;"
    assert _explicit_transaction(sql) is None


def test_ignores_transaction_words_inside_comments() -> None:
    """注释里写 BEGIN 是说明文字，不是语句 —— 006 第一次失败就是这个原因。"""
    assert _explicit_transaction("-- 文件内不得出现 BEGIN/COMMIT\nCREATE TABLE t (id INTEGER);") is None
    assert _explicit_transaction("/* BEGIN; COMMIT; */ CREATE TABLE t (id INTEGER);") is None


def test_ignores_transaction_words_inside_string_literals() -> None:
    """字符串字面量里的 'BEGIN;' 是数据，不是语句。"""
    assert _explicit_transaction("INSERT INTO t (v) VALUES ('BEGIN;');") is None
    assert _explicit_transaction('INSERT INTO t (v) VALUES ("COMMIT;");') is None
    # '' 转义不应让扫描器提前闭合
    assert _explicit_transaction("INSERT INTO t (v) VALUES ('it''s BEGIN;');") is None


def test_rejects_transaction_statement_after_a_trigger() -> None:
    """允许触发器体，但不得因此放行真正的事务语句。"""
    sql = (
        "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW\n"
        "WHEN NEW.x NOT IN ('A')\n"
        "BEGIN\n    SELECT RAISE(ABORT, 'bad');\nEND;\n"
        "BEGIN;\n"
    )
    assert _explicit_transaction(sql) == "BEGIN"


def test_006_itself_passes_the_transaction_check() -> None:
    """回归：006 必须能通过判据（它就是触发这次修正的那个迁移）。"""
    from photoos.db.migrator import discover_migrations

    migration = next(m for m in discover_migrations() if m.version == 6)
    assert _explicit_transaction(migration.sql) is None
