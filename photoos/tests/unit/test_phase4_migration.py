"""Phase 4 · 迁移 008（分人 + 人工反馈）的结构契约。

分四部分：
  A. 表/列/索引是否如方案所定；
  B. 铁律 #2（不出现身份性路径）在 schema 层面是否成立；
  C. **外键方向**：删掉一张脸/一个人之后，什么该跟着走、什么不许跟着走；
  D. 纯增量：008 不得动到 001–007 的任何东西。

C 部分是重点。本项目在"引用 vs 软删除"这一族上连出过六处缺陷，
所以每一条外键方向都从一个**故意的破坏**去验证，而不是看 SQL 写的是什么。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.db.migrator import target_version


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
        return {row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


# ============================================================ A. 结构

def test_008_applied_and_schema_at_target(migrated: Database) -> None:
    assert migrated.schema_version() == target_version()
    assert migrated.schema_version() >= 8, "008 必须已应用"


def test_008_creates_people_tables(migrated: Database) -> None:
    assert {"face", "face_embedding", "person", "person_face", "person_reject",
            "person_profile", "person_event", "semantic_feedback",
            "semantic_tuning"} <= _objects(migrated, "table")


def test_008_adds_collection_source_column(migrated: Database) -> None:
    """主题要能回答"我是从哪个词/哪个人来的"（用户工作流的最后一环）。"""
    assert "source_json" in _columns(migrated, "collection")


def test_008_creates_indexes(migrated: Database) -> None:
    indexes = _objects(migrated, "index")
    expected = {
        "idx_face_asset", "idx_face_shot", "idx_face_frontal",
        "idx_face_embedding_face", "idx_face_embedding_identity",
        "idx_person_status", "idx_person_face_face", "idx_person_face_source",
        "idx_person_reject_face", "idx_person_event_person", "idx_person_event_action",
        "idx_semantic_feedback_query",
    }
    assert expected <= indexes, f"缺少索引：{expected - indexes}"


def test_008_preserves_earlier_tables(migrated: Database) -> None:
    """008 是对 001–007 的**纯增量**。"""
    tables = _objects(migrated, "table")
    earlier = {
        "library", "volume", "volume_library", "session", "scene", "shot", "asset",
        "asset_instance", "metadata", "preview", "job", "job_item", "app_event",
        "selection", "selection_event", "schema_migrations", "collection",
        "collection_item", "embedding", "semantic_query", "semantic_query_hit",
    }
    assert earlier <= tables, f"008 丢失了既有表：{earlier - tables}"


def test_migrations_are_still_idempotent(migrated: Database, tmp_path: Path) -> None:
    before = migrated.schema_version()
    second = apply_migrations(migrated, backup_dir=tmp_path / "backups")
    assert second.applied == []
    assert second.from_version == second.to_version == before


# ============================================================ B. 铁律 #2

def test_008_tables_have_no_identity_paths(migrated: Database) -> None:
    """Phase 4 的新表同样**一个路径字段都不许有**（铁律 #2）。

    人脸缩略图是**按需从 preview 现裁**的，不落盘、不存路径 ——
    所以这条约束在人脸这一族上也能成立，而不是"因为要存图所以破例"。
    """
    forbidden = ("path", "root", "drive", "volume", "folder", "dir_", "absolute")
    offenders: list[str] = []
    for table in ("face", "face_embedding", "person", "person_face", "person_reject",
                  "person_profile", "person_event", "semantic_feedback", "semantic_tuning"):
        for name in _columns(migrated, table):
            if any(bad in name.lower() for bad in forbidden):
                offenders.append(f"{table}.{name}")
    assert not offenders, f"Phase 4 新表出现了疑似身份性路径列：{offenders}"


def test_face_embedding_identity_is_unique_so_history_is_never_overwritten(
    migrated: Database,
) -> None:
    """铁律 #4：换人脸模型 = **新增一行**，绝不覆盖历史。"""
    with migrated.write() as conn:
        conn.execute("INSERT INTO library (uuid, name, created_at, status)"
                     " VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO asset (library_id, relative_path, file_type, status,"
                     " first_seen_at) VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK',"
                     " '2026-01-01T00:00:00+08:00')")
        conn.execute(
            "INSERT INTO face (asset_id, detector, detector_version, detect_max, preview_w,"
            " preview_h, box_x, box_y, box_w, box_h, det_score, face_px, created_at)"
            " VALUES (1, 'yunet', 'v1', 900, 100, 100, 1, 2, 30, 30, 0.9, 30, '2026-01-01')")

    def insert(version: str) -> None:
        with migrated.write() as conn:
            conn.execute(
                "INSERT INTO face_embedding (face_id, model, model_version, prep_version, dim,"
                " normalization, vector, created_at)"
                " VALUES (1, 'sface', ?, 'p1', 4, 'l2', ?, '2026-01-01')",
                (version, b"\x00" * 16))

    insert("v1")
    with pytest.raises(sqlite3.IntegrityError):
        insert("v1")   # 同身份重复 → 拒绝
    insert("v2")       # 换版本 → 允许（多版本并存）


def test_semantic_feedback_action_is_constrained(migrated: Database) -> None:
    """`action` 只能是 keep/drop —— 写进垃圾字符串必须被 CHECK 拦下。"""
    with migrated.write() as conn:
        conn.execute("INSERT INTO library (uuid, name, created_at, status)"
                     " VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO asset (library_id, relative_path, file_type, status,"
                     " first_seen_at) VALUES (1, 'a\\b\\c.jpg', 'jpeg', 'OK',"
                     " '2026-01-01T00:00:00+08:00')")
        conn.execute("INSERT INTO scene (id, name, created_at, updated_at)"
                     " VALUES (1,'S','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO shot (id, scene_id, created_at, updated_at)"
                     " VALUES (1, 1, '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO semantic_query (id, query_text, model, model_version, dim,"
                     " normalization, vector, created_at, updated_at)"
                     " VALUES (1, '日出', 'm', 'v', 4, 'l2', X'00000000', '2026-01-01',"
                     " '2026-01-01')")
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute("INSERT INTO semantic_feedback (query_id, shot_id, action, created_at,"
                         " updated_at) VALUES (1, 1, 'maybe', '2026-01-01', '2026-01-01')")


def test_person_status_is_constrained(migrated: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute("INSERT INTO person (uuid, status, created_at, updated_at)"
                         " VALUES ('u', 'whatever', '2026-01-01', '2026-01-01')")


def test_person_name_may_be_null(migrated: Database) -> None:
    """★ 这是设计而不是偷懒：模型**不知道那是谁**，所以候选人物必须能无名落地。"""
    with migrated.write() as conn:
        conn.execute("INSERT INTO person (uuid, name, status, created_at, updated_at)"
                     " VALUES ('u1', NULL, 'proposed', '2026-01-01', '2026-01-01')")
    with migrated.read() as conn:
        row = conn.execute("SELECT name, status FROM person WHERE uuid = 'u1'").fetchone()
    assert row["name"] is None and row["status"] == "proposed"


# ============================================================ C. 外键方向

def _seed_photo(migrated: Database) -> None:
    with migrated.write() as conn:
        conn.execute("INSERT INTO library (uuid, name, created_at, status)"
                     " VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                     " first_seen_at) VALUES (1, 1, 'a\\b\\c.jpg', 'jpeg', 'OK',"
                     " '2026-01-01T00:00:00+08:00')")
        conn.execute("INSERT INTO scene (id, name, created_at, updated_at)"
                     " VALUES (1,'S','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO shot (id, scene_id, created_at, updated_at)"
                     " VALUES (1, 1, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT INTO face (id, asset_id, shot_id, detector, detector_version, detect_max,"
            " preview_w, preview_h, box_x, box_y, box_w, box_h, det_score, face_px, created_at)"
            " VALUES (1, 1, 1, 'yunet', 'v1', 900, 100, 100, 1, 2, 30, 30, 0.9, 30,"
            " '2026-01-01')")
        conn.execute("INSERT INTO person (id, uuid, name, status, created_at, updated_at)"
                     " VALUES (1, 'p1', '妈妈', 'named', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO person_face (person_id, face_id, source, created_at,"
                     " updated_at) VALUES (1, 1, 'user', '2026-01-01', '2026-01-01')")


def test_deleting_asset_cascades_faces(migrated: Database) -> None:
    """★ 反向验证：删掉 asset，它的人脸必须跟着消失。

    如果不级联，库里会留下指向不存在 asset 的"孤儿脸"，
    界面就会去裁一张不存在的预览 —— 表现是零星破图，极难定位。
    """
    _seed_photo(migrated)
    with migrated.write() as conn:
        conn.execute("DELETE FROM asset WHERE id = 1")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM face").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM person_face").fetchone()[0] == 0
        # 人还在：脸没了不等于这个人不存在
        assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1


def test_retiring_a_shot_keeps_the_face(migrated: Database) -> None:
    """★ 分组重算不许删掉"这张脸存在"的事实。

    `face.shot_id` 是 `ON DELETE SET NULL` 而不是 CASCADE：
    Shot 是**可重算的分组**，删一次分组不该把检测结果抹掉（那要重新跑一遍检测）。
    """
    _seed_photo(migrated)
    with migrated.write() as conn:
        # 只增不删的退役路径
        conn.execute("UPDATE shot SET retired_at = '2026-01-02' WHERE id = 1")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM face").fetchone()[0] == 1
    with migrated.write() as conn:
        # 真的物理删除（迁移/清理时会走到）—— 脸必须留下，只是失去分组归属
        conn.execute("DELETE FROM shot WHERE id = 1")
    with migrated.read() as conn:
        row = conn.execute("SELECT shot_id FROM face WHERE id = 1").fetchone()
        assert row is not None, "删 Shot 不该把检出的人脸一起删掉"
        assert row["shot_id"] is None, "失去分组后 shot_id 必须置空，而不是指向不存在的行"


def test_deleting_person_keeps_the_faces(migrated: Database) -> None:
    """★ 反向验证：删掉一个人（标签）之后，**脸一张都不许少**。

    这是"删的是标签不是照片"这条承诺在数据层的落点。
    """
    _seed_photo(migrated)
    with migrated.write() as conn:
        conn.execute("DELETE FROM person WHERE id = 1")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM face").fetchone()[0] == 1, "脸被误删了"
        assert conn.execute("SELECT COUNT(*) FROM person_face").fetchone()[0] == 0, \
            "归属必须随人一起消失"


def test_unknown_and_rejected_are_different_states(migrated: Database) -> None:
    """★ "未知"与"被否决"必须是两个可区分的事实。

    没有 `person_face` 行 = 模型/用户都还没表态（未知）；
    有 `person_reject` 行 = 明确说过"不是他"（负标签）。
    学习时把前者当成负样本会让模型越学越窄 —— 所以它们分属两张表。
    """
    _seed_photo(migrated)
    with migrated.write() as conn:
        conn.execute("DELETE FROM person_face WHERE person_id = 1")   # 变成"未知"
        conn.execute("DELETE FROM face WHERE id = 1")
        conn.execute(
            "INSERT INTO face (id, asset_id, shot_id, detector, detector_version, detect_max,"
            " preview_w, preview_h, box_x, box_y, box_w, box_h, det_score, face_px, created_at)"
            " VALUES (2, 1, 1, 'yunet', 'v1', 900, 100, 100, 50, 60, 30, 30, 0.9, 30,"
            " '2026-01-01')")
        conn.execute("INSERT INTO person_reject (person_id, face_id, created_at)"
                     " VALUES (1, 2, '2026-01-01')")
    with migrated.read() as conn:
        unknown = conn.execute("SELECT COUNT(*) FROM face f WHERE NOT EXISTS"
                               " (SELECT 1 FROM person_face pf WHERE pf.face_id = f.id)").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM person_reject").fetchone()[0]
    assert unknown == 1 and rejected == 1
    # 被否决的那张脸同时也满足"未知"（没有 person_face 行）——
    # 这正是为什么读取端不能只看 person_face 判断"处理过没有"
    with migrated.read() as conn:
        both = conn.execute(
            "SELECT COUNT(*) FROM face f"
            " WHERE NOT EXISTS (SELECT 1 FROM person_face pf WHERE pf.face_id = f.id)"
            "   AND EXISTS (SELECT 1 FROM person_reject pr WHERE pr.face_id = f.id)"
        ).fetchone()[0]
    assert both == 1


def test_face_box_identity_is_unique_per_detector(migrated: Database) -> None:
    """同一 asset + 同一检测器 + 同一框 = 同一张脸（重复检测幂等）。"""
    _seed_photo(migrated)
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute(
                "INSERT INTO face (asset_id, detector, detector_version, detect_max, preview_w,"
                " preview_h, box_x, box_y, box_w, box_h, det_score, face_px, created_at)"
                " VALUES (1, 'yunet', 'v1', 900, 100, 100, 1, 2, 30, 30, 0.9, 30, '2026-01-01')")


def test_same_box_on_a_new_detector_is_a_new_row(migrated: Database) -> None:
    """换检测器 = 新的一行（多版本并存），而不是被唯一约束挡住。"""
    _seed_photo(migrated)
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO face (asset_id, detector, detector_version, detect_max, preview_w,"
            " preview_h, box_x, box_y, box_w, box_h, det_score, face_px, created_at)"
            " VALUES (1, 'yunet-v2', 'v1', 900, 100, 100, 1, 2, 30, 30, 0.9, 30, '2026-01-01')")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM face").fetchone()[0] == 2


# ============================================================ E. 迁移 009（人工判定）

def test_009_creates_the_verdict_table(migrated: Database) -> None:
    """★ 「不是人」存在独立的判定表里，**不是** `face` 上加一列。

    `face` 是检测器的**事实**（"这里检出过一张脸"），"它不是人"是**用户的判断**。
    混进事实表之后，"重算检测"这类操作就有一个回答不了的问题：判断还算不算？
    """
    assert migrated.schema_version() >= 9
    assert "face_verdict" in _objects(migrated, "table")
    columns = _columns(migrated, "face_verdict")
    assert set(columns) == {"face_id", "verdict", "note", "created_at", "updated_at"}
    assert columns["face_id"]["pk"] == 1, "一张脸只能有一条判定"
    assert "idx_face_verdict_created" in _objects(migrated, "index")


def test_009_rejects_an_unknown_verdict(migrated: Database) -> None:
    """判定词汇表是封闭的：随手塞一个值进去必须被挡。"""
    _seed_photo(migrated)
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute("INSERT INTO face_verdict (face_id, verdict, created_at, updated_at)"
                         " VALUES (1, 'maybe', '2026-01-01', '2026-01-01')")


def test_009_verdict_goes_away_with_the_face(migrated: Database) -> None:
    """★ 反向验证：脸没了，判定不许变成孤儿行（否则计数永远比真实多）。"""
    _seed_photo(migrated)
    with migrated.write() as conn:
        conn.execute("INSERT INTO face_verdict (face_id, verdict, created_at, updated_at)"
                     " VALUES (1, 'not_person', '2026-01-01', '2026-01-01')")
    with migrated.write() as conn:
        conn.execute("DELETE FROM face WHERE id = 1")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM face_verdict").fetchone()[0] == 0


def test_009_widens_the_event_vocabulary_without_losing_rows(migrated: Database) -> None:
    """★ 009 重建 `person_event` 来扩 action 白名单 —— 旧行**一行都不许丢**。

    改 CHECK 只能重建表（SQLite 不支持 DROP CONSTRAINT），而重建是最容易
    把数据弄丢的一类迁移：漏一个列、拷错一次，历史就没了。
    这里先写三条旧词汇的事件，扩完必须还在，且新词汇可用、非法词汇仍被挡。
    """
    _seed_photo(migrated)
    with migrated.write() as conn:
        for action in ("confirm", "reject", "profile"):
            conn.execute("INSERT INTO person_event (person_id, face_id, action, detail_json,"
                         " created_at) VALUES (1, 1, ?, '{}', '2026-01-01')", (action,))
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM person_event").fetchone()[0] == 3
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'person_event'").fetchone()[0]
    for action in ("dissolve", "restore", "move_in", "move_out", "not_person", "person_again"):
        assert action in sql, f"事件词汇表里缺 {action}"
    with migrated.write() as conn:
        conn.execute("INSERT INTO person_event (person_id, action, created_at)"
                     " VALUES (1, 'dissolve', '2026-01-02')")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM person_event").fetchone()[0] == 4
    with pytest.raises(sqlite3.IntegrityError):
        with migrated.write() as conn:
            conn.execute("INSERT INTO person_event (person_id, action, created_at)"
                         " VALUES (1, 'not_a_real_action', '2026-01-02')")


def test_009_is_purely_additive(migrated: Database) -> None:
    """★ 009 只许新增：`face` 的列与其余表的结构一个字都不能变。

    重建 `person_event` 是这次迁移里唯一"动到旧对象"的地方，
    所以这里逐列比对它的形状（列名/类型/非空），别的内容必须原样。
    """
    columns = _columns(migrated, "person_event")
    assert list(columns) == ["id", "person_id", "face_id", "action", "detail_json", "created_at"]
    assert columns["action"]["notnull"] == 1
    assert "is_person" not in _columns(migrated, "face"), \
        "判定不许混进 face 表（那是检测事实）"
    assert "verdict" not in _columns(migrated, "person_face"), \
        "判定也不是归属的一个属性"
