"""Scene 的人工调整：合并 / 拆分 / 解除标记，以及**重算不会冲掉它**。

## 为什么这组测试重要

Scene 是按"时间断点 + 相似度"自动切的，实拍里必然切错。用户手工合并/拆分之后，
如果下一次 `scenes rebuild` 把结果覆盖掉，这个功能就是假的。
所以这里除了测三个动作本身，还专门测 `replace_scenes()` 的**跳过语义**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.repos import ShotRepo


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    with database.write() as conn:
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, source, group_revision,"
            " created_at, updated_at) VALUES (1, '万宁市\\2025-11-29_0610-1747', '万宁市',"
            " '2025-11-29_0610-1747', 'auto', 1, 't', 't')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, source, group_revision,"
            " created_at, updated_at) VALUES (2, '杭州市\\2025-12-01_1000-1100', '杭州市',"
            " '2025-12-01_1000-1100', 'auto', 1, 't', 't')"
        )
    return database


def _scene(db: Database, scene_id: int, session_id: int, name: str,
           start: str, end: str, source: str = "auto") -> None:
    db.execute(
        "INSERT INTO scene (id, session_id, name, captured_from, captured_to, source,"
        " group_revision, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 1, 't', 't')",
        (scene_id, session_id, name, start, end, source),
    )


def _shot(db: Database, shot_id: int, scene_id: int, captured_at: str) -> None:
    db.execute(
        "INSERT INTO shot (id, scene_id, captured_at, capture_date_local, created_at,"
        " updated_at, group_source, group_revision)"
        " VALUES (?, ?, ?, ?, 't', 't', 'auto', 1)",
        (shot_id, scene_id, captured_at, captured_at[:10]),
    )


def _shots_in(db: Database, scene_id: int) -> list[int]:
    return [int(r["id"]) for r in db.query(
        "SELECT id FROM shot WHERE scene_id = ? AND retired_at IS NULL ORDER BY captured_at, id",
        (scene_id,))]


def test_merge_moves_shots_and_pins_the_scene(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", "2025-11-29 06:10:00", "2025-11-29 07:00:00")
    _scene(db, 11, 1, "B", "2025-11-29 07:10:00", "2025-11-29 08:00:00")
    _shot(db, 1, 10, "2025-11-29 06:10:00")
    _shot(db, 2, 11, "2025-11-29 07:10:00")
    _shot(db, 3, 11, "2025-11-29 07:20:00")

    result = repo.merge_scenes(10, 11)

    assert result["moved_shots"] == 2
    assert _shots_in(db, 10) == [1, 2, 3]
    assert db.scalar("SELECT COUNT(*) FROM scene WHERE id = 11", default=0) == 0
    row = db.query_one("SELECT source, captured_from, captured_to FROM scene WHERE id = 10")
    assert row["source"] == "user", "合并过的 Scene 必须被标记，否则重算会覆盖它"
    assert row["captured_from"] == "2025-11-29 06:10:00"
    assert row["captured_to"] == "2025-11-29 08:00:00", "合并后时间范围要取两边并集"


def test_merge_refuses_cross_session(db: Database) -> None:
    """跨时段合并会让目录名与时间范围对不上 —— 目录名是唯一能独立校验的基准。"""
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None)
    _scene(db, 20, 2, "B", None, None)
    with pytest.raises(ValueError):
        repo.merge_scenes(10, 20)


def test_merge_refuses_self(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None)
    with pytest.raises(ValueError):
        repo.merge_scenes(10, 10)


def test_split_moves_the_tail_into_a_new_scene(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", "2025-11-29 06:10:00", "2025-11-29 08:00:00")
    _shot(db, 1, 10, "2025-11-29 06:10:00")
    _shot(db, 2, 10, "2025-11-29 07:10:00")
    _shot(db, 3, 10, "2025-11-29 07:20:00")

    result = repo.split_scene(10, after_shot_id=1)

    assert result["head_shots"] == 1
    assert result["moved_shots"] == 2
    assert _shots_in(db, 10) == [1]
    assert _shots_in(db, result["new_scene_id"]) == [2, 3]
    head = db.query_one("SELECT source, captured_to FROM scene WHERE id = 10")
    assert head["source"] == "user"
    assert head["captured_to"] == "2025-11-29 06:10:00", "前段的范围要收到切点"
    new = db.query_one("SELECT source, name, captured_from FROM scene WHERE id = ?",
                       (result["new_scene_id"],))
    assert new["source"] == "user"
    assert new["captured_from"] == "2025-11-29 07:10:00"
    assert "拆出" in str(new["name"])


def test_split_refuses_the_last_shot(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None)
    _shot(db, 1, 10, "2025-11-29 06:10:00")
    with pytest.raises(ValueError):
        repo.split_scene(10, after_shot_id=1)


def test_split_refuses_a_foreign_shot(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None)
    _scene(db, 11, 1, "B", None, None)
    _shot(db, 1, 10, "2025-11-29 06:10:00")
    _shot(db, 2, 11, "2025-11-29 07:10:00")
    with pytest.raises(ValueError):
        repo.split_scene(10, after_shot_id=2)


def test_rebuild_skips_a_session_with_manual_edits(db: Database) -> None:
    """★ 核心语义：含人工调整的时段，重算**整段跳过**（否则手工调整等于白做）。

    故意破坏：去掉 `replace_scenes()` 开头的 pinned 检查，本测试立刻变红
    （重建会把 shot.scene_id 清空、然后按方案重新指派）。
    """
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None, source="user")
    _shot(db, 1, 10, "2025-11-29 06:10:00")

    result = repo.replace_scenes(session_id=1, scenes=[
        {"name": "全新建的", "shot_ids": [1], "captured_from": None, "captured_to": None},
    ])

    assert result.get("skipped_pinned") == 1
    assert result["created"] == 0 and result["cleared"] == 0
    assert _shots_in(db, 10) == [1], "手工调整过的 Scene 及其 Shot 必须原样保留"
    assert db.scalar("SELECT COUNT(*) FROM scene WHERE name = '全新建的'", default=0) == 0


def test_unpin_allows_rebuild_again(db: Database) -> None:
    repo = ShotRepo(db)
    _scene(db, 10, 1, "A", None, None, source="user")
    _shot(db, 1, 10, "2025-11-29 06:10:00")

    repo.unpin_scene(10)
    assert db.scalar("SELECT source FROM scene WHERE id = 10") == "auto"

    result = repo.replace_scenes(session_id=1, scenes=[
        {"name": "重算后的", "shot_ids": [1], "captured_from": None, "captured_to": None},
    ])
    assert not result.get("skipped_pinned")
    assert db.scalar("SELECT COUNT(*) FROM scene WHERE name = '重算后的'", default=0) == 1


# ============================================================ HTTP 层（界面要走这三个端点）

@pytest.fixture()
def client(db: Database):
    from fastapi.testclient import TestClient

    from photoos.api import create_app
    from photoos.config import Settings, use_config_path

    settings = Settings(data_root=db.path.parent)
    settings.ensure_dirs()
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def test_api_merge_and_split_and_unpin(client, db: Database) -> None:
    """三个端点都要能用，并且**错误输入是 400/422 而不是 500**。"""
    _scene(db, 10, 1, "A", "2025-11-29 06:10:00", "2025-11-29 07:00:00")
    _scene(db, 11, 1, "B", "2025-11-29 07:10:00", "2025-11-29 08:00:00")
    _shot(db, 1, 10, "2025-11-29 06:10:00")
    _shot(db, 2, 11, "2025-11-29 07:10:00")
    _shot(db, 3, 11, "2025-11-29 07:20:00")

    merged = client.post("/api/scenes/merge", json={"primary_id": 10, "secondary_id": 11})
    assert merged.status_code == 200, merged.text
    assert merged.json()["moved_shots"] == 2
    assert _shots_in(db, 10) == [1, 2, 3]

    split = client.post("/api/scenes/10/split", json={"after_shot_id": 2})
    assert split.status_code == 200, split.text
    new_id = split.json()["new_scene_id"]
    assert _shots_in(db, new_id) == [3]

    unpin = client.post(f"/api/scenes/{new_id}/unpin")
    assert unpin.status_code == 200, unpin.text
    assert db.scalar("SELECT source FROM scene WHERE id = ?", (new_id,)) == "auto"


def test_api_scene_adjust_rejects_bad_input(client, db: Database) -> None:
    _scene(db, 10, 1, "A", None, None)
    # 非数字 id → 422；不存在的 Scene → 400；跨时段 → 400
    assert client.post("/api/scenes/merge",
                       json={"primary_id": "abc", "secondary_id": 11}).status_code == 422
    assert client.post("/api/scenes/merge",
                       json={"primary_id": 10, "secondary_id": 999}).status_code == 400
    _scene(db, 20, 2, "B", None, None)
    assert client.post("/api/scenes/merge",
                       json={"primary_id": 10, "secondary_id": 20}).status_code == 400
    assert client.post("/api/scenes/10/split", json={"after_shot_id": "x"}).status_code == 422
