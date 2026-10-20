"""Phase 2 · M7：Collections（自定义精选主题）测试。

PRD §9.2.1 逐条对应：
  * 「精选主题不是 Scene 的别名，也不受时间、地点或 Session 限制」→ `test_collection_spans_sessions_and_cities`
  * 「主题支持自定义名称、封面、描述和排序」                    → CRUD + 排序
  * 「同一张最终精选可以同时属于多个主题」                      → `test_shot_can_belong_to_many_collections`
  * 「主题与物理文件目录完全无关；加入或移除只改变数据库关系」    → **铁律 #1 的文件守卫**
  * 「即使照片硬盘离线，主题列表、封面缩略图和排序仍应可用」      → 封面三级回退 + 不碰原片
"""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.repos import CollectionRepo


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    _seed(database)
    return database


def _seed(db: Database) -> None:
    """两个 Session（不同城市、不同年份）+ 3 个 Scene + 4 个 Shot，其中 2 个 FINAL。"""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, captured_from, created_at, updated_at)"
            " VALUES (1,'杭州\\2025-01-01_1000-1100','杭州','2025-01-01_1000-1100',"
            " '2025-01-01 10:00:00','t','t')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, captured_from, created_at, updated_at)"
            " VALUES (2,'雅典\\2026-06-01_1800-1900','雅典','2026-06-01_1800-1900',"
            " '2026-06-01 18:00:00','t','t')"
        )
        conn.execute("INSERT INTO scene (id, session_id, name, created_at, updated_at) VALUES (1,1,'S1','t','t')")
        conn.execute("INSERT INTO scene (id, session_id, name, created_at, updated_at) VALUES (2,1,'S2','t','t')")
        conn.execute("INSERT INTO scene (id, session_id, name, created_at, updated_at) VALUES (3,2,'S3','t','t')")
        for shot_id, scene_id in ((1, 1), (2, 2), (3, 3), (4, 3)):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', 't')",
                (shot_id, f"c\\p\\DSC_{shot_id:04d}.JPG"),
            )
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                " capture_date_local, created_at, updated_at)"
                " VALUES (?, ?, ?, '2025-01-01 10:00:00', '2025-01-01', 't', 't')",
                (shot_id, scene_id, shot_id),
            )
            conn.execute("UPDATE asset SET shot_id = ? WHERE id = ?", (shot_id, shot_id))
        # 两个最终精选
        conn.execute("INSERT INTO selection (shot_id, status, updated_at) VALUES (1,'FINAL','t')")
        conn.execute("INSERT INTO selection (shot_id, status, updated_at) VALUES (3,'FINAL','t')")


# ============================================================ 创建与读取

def test_create_and_get(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("蓝调时刻", description="不同年份不同地点的蓝调")
    row = repo.get(cid)
    assert row["name"] == "蓝调时刻"
    assert row["description"] == "不同年份不同地点的蓝调"
    assert row["uuid"]


def test_uuid_is_stable_identity_for_migration(db: Database) -> None:
    """★ `uuid` 是跨机迁移的稳定身份（与 library.uuid 同思路）。

    没有它，PC-B 上导入迁移包后主题 id 会被重新分配，逐项校验就失去基准。
    """
    repo = CollectionRepo(db)
    first = repo.create("主题A", uuid="fixed-uuid-1")
    assert repo.get(first)["uuid"] == "fixed-uuid-1"
    assert repo.get_by_uuid("fixed-uuid-1")["id"] == first


def test_uuid_must_be_unique(db: Database) -> None:
    import sqlite3

    repo = CollectionRepo(db)
    repo.create("主题A", uuid="dup")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create("主题B", uuid="dup")


def test_empty_name_is_rejected(db: Database) -> None:
    with pytest.raises(ValueError):
        CollectionRepo(db).create("   ")


def test_get_by_name(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("西北游记")
    assert repo.get_by_name("西北游记")["id"] == cid
    assert repo.get_by_name("不存在") is None


# ============================================================ 更新与删除

def test_update_fields(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("旧名", description="旧描述", sort_order=5)
    assert repo.update(cid, name="新名", description="新描述", sort_order=1) is True
    row = repo.get(cid)
    assert (row["name"], row["description"], row["sort_order"]) == ("新名", "新描述", 1)


def test_update_with_no_fields_is_noop(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    assert repo.update(cid) is False


def test_cover_can_be_set_and_cleared(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.update(cid, cover_shot_id=1)
    assert repo.get(cid)["cover_shot_id"] == 1
    repo.update(cid, clear_cover=True)
    assert repo.get(cid)["cover_shot_id"] is None


def test_delete_removes_collection_and_items(db: Database) -> None:
    """删除主题只删数据库关系 —— 由外键级联清掉 collection_item。"""
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1, 3])
    assert repo.delete(cid) is True
    assert repo.get(cid) is None
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_item").fetchone()[0] == 0
    # ★ 精选与 Shot 完全不受影响（删除数据库记录 ≠ 删除原文件，也不动精选）
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM selection WHERE status='FINAL'").fetchone()[0] == 2


# ============================================================ 作品归属

def test_shot_can_belong_to_many_collections(db: Database) -> None:
    """★ PRD §9.2.1：「同一张最终精选可以同时属于多个主题」。"""
    repo = CollectionRepo(db)
    a = repo.create("蓝调时刻")
    b = repo.create("西北游记")
    repo.add_shots(a, [1])
    repo.add_shots(b, [1])
    assert [c["name"] for c in repo.collections_of_shot(1)] == ["蓝调时刻", "西北游记"]


def test_add_shots_is_idempotent(db: Database) -> None:
    """同一主题里重复加入同一张不该产生第二行（主键天然保证）。"""
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    assert repo.add_shots(cid, [1, 2]) == 2
    assert repo.add_shots(cid, [1, 2]) == 0
    assert repo.list_all()[0]["items"] == 2


def test_add_shots_to_missing_collection_raises(db: Database) -> None:
    with pytest.raises(ValueError):
        CollectionRepo(db).add_shots(999, [1])


def test_remove_shots(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1, 2, 3])
    assert repo.remove_shots(cid, [2]) == 1
    assert [i["shot_id"] for i in repo.items(cid)] == [1, 3]


def test_collection_spans_sessions_and_cities(db: Database) -> None:
    """★ PRD：「不受时间、地点或 Session 限制」。

    一个主题同时收录 2025 年杭州与 2026 年雅典的作品 —— 结构上就无法限制
    （`collection_item` 只引用 `shot_id`，没有时间/地点/Session 外键）。
    """
    repo = CollectionRepo(db)
    cid = repo.create("跨年跨地")
    repo.add_shots(cid, [1, 3])
    assert sorted(repo.audit_scene_ids(cid)) == [1, 3]
    scenes = db.query(
        "SELECT DISTINCT sh.scene_id, ses.city_hint FROM collection_item ci"
        " JOIN shot sh ON sh.id = ci.shot_id"
        " JOIN scene sc ON sc.id = sh.scene_id"
        " JOIN session ses ON ses.id = sc.session_id"
        " WHERE ci.collection_id = ?",
        (cid,),
    )
    assert {r["city_hint"] for r in scenes} == {"杭州", "雅典"}


# ============================================================ 排序

def test_collection_level_ordering(db: Database) -> None:
    repo = CollectionRepo(db)
    repo.create("第三", sort_order=30)
    repo.create("第一", sort_order=10)
    repo.create("第二", sort_order=20)
    assert [c["name"] for c in repo.list_all()] == ["第一", "第二", "第三"]


def test_item_level_ordering(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1, 2, 3])
    assert [i["shot_id"] for i in repo.items(cid)] == [1, 2, 3]
    repo.reorder(cid, [3, 1, 2])
    assert [i["shot_id"] for i in repo.items(cid)] == [3, 1, 2]


def test_item_note(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1], note="这张是当天最好的光")
    assert repo.items(cid)[0]["note"] == "这张是当天最好的光"
    repo.set_note(cid, 1, "改主意了")
    assert repo.items(cid)[0]["note"] == "改主意了"


# ============================================================ 封面三级回退（断点 #7）

def test_cover_falls_back_to_first_item(db: Database) -> None:
    """★ 未指定封面 → 取主题内排序最前的一条（断点 #7 要求离线也能看封面）。"""
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [3, 1])
    assert repo.list_all()[0]["effective_cover_shot_id"] == 3


def test_explicit_cover_wins(db: Database) -> None:
    repo = CollectionRepo(db)
    cid = repo.create("主题", cover_shot_id=2)
    repo.add_shots(cid, [1, 3])
    assert repo.list_all()[0]["effective_cover_shot_id"] == 2


def test_empty_collection_has_no_cover(db: Database) -> None:
    """空主题 → 无封面（前端用占位图，**不发请求**）。"""
    repo = CollectionRepo(db)
    repo.create("空主题")
    assert repo.list_all()[0]["effective_cover_shot_id"] is None


def test_items_expose_local_thumb_availability(db: Database) -> None:
    """封面走本地 `thumb` 缓存 —— 这是"拔盘后主题列表与封面仍可用"的机制。

    `has_thumb` 来自 `preview` 表（`D:` 本地盘的派生文件），**与原片是否在线无关**。
    """
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1])
    assert repo.items(cid)[0]["has_thumb"] is False      # 本夹具没造 preview 行
    with db.write() as conn:
        conn.execute(
            "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
            " generator_version, created_at) VALUES (1,'thumb','thumbnails/x.webp',"
            " 320,240,1000,'p1.0','t')"
        )
    assert repo.items(cid)[0]["has_thumb"] is True


def test_retired_shots_disappear_from_items(db: Database) -> None:
    """决策 #4：退役 Shot 不在主题里显示（但 collection_item 行不被删）。"""
    repo = CollectionRepo(db)
    cid = repo.create("主题")
    repo.add_shots(cid, [1, 2])
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = 't2' WHERE id = 1")
    assert [i["shot_id"] for i in repo.items(cid)] == [2]


# ============================================================ ★ 铁律 #1：只改关系

def test_collections_never_touch_the_filesystem(db: Database, monkeypatch) -> None:
    """★ PRD §9.2.1：「加入或移除主题只改变数据库关系，不移动或复制原始照片。」

    与 M5 的同名断言一样，这里也拦 `io.open`（`Path.write_text` 走的是它，
    只补 `builtins.open` 会留下缺口 —— 那是 M5 反向验证时发现的）。
    """
    import os
    import shutil

    calls: list[str] = []
    real_open = builtins.open

    def traced_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            calls.append(f"open({file!r}, {mode!r})")
            raise AssertionError(f"Collections 不该写文件：{file}")
        return real_open(file, mode, *args, **kwargs)

    def guard(name: str):
        def inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"Collections 不该调用 {name}")

        return inner

    monkeypatch.setattr(builtins, "open", traced_open)
    monkeypatch.setattr(io, "open", traced_open)
    for name in ("remove", "unlink", "rename", "replace", "rmdir"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, guard(f"os.{name}"))
    for name in ("move", "copy", "copy2", "rmtree"):
        monkeypatch.setattr(shutil, name, guard(f"shutil.{name}"))

    repo = CollectionRepo(db)
    cid = repo.create("蓝调时刻", description="跨年跨地")
    repo.add_shots(cid, [1, 3])
    repo.update(cid, name="改名了", cover_shot_id=1, sort_order=2)
    repo.reorder(cid, [3, 1])
    repo.set_note(cid, 3, "备注")
    repo.remove_shots(cid, [1])
    repo.items(cid)
    repo.list_all()
    repo.collections_of_shot(3)
    repo.audit_scene_ids(cid)
    repo.delete(cid)

    assert calls == [], f"Collections 产生了文件改动操作：{calls}"


def test_collections_do_not_require_photo_files_to_exist(db: Database) -> None:
    """主题引用的是 `shot_id`，**与照片文件是否存在无关** —— 断点 #7 的离线语义。"""
    repo = CollectionRepo(db)
    cid = repo.create("离线主题")
    repo.add_shots(cid, [1, 3])
    with db.write() as conn:
        conn.execute("UPDATE asset SET status = 'MISSING'")
    # 主题列表与条目照样可用
    assert repo.list_all()[0]["items"] == 2
    assert len(repo.items(cid)) == 2
