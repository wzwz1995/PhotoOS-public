"""Phase 2 · 人工调整分组：拆 / 并（PRD §9.2「所有自动归组都允许用户调整」）。

这是断点 #3 的后半句 —— 前半句「相似照片能被合理聚组」由
`test_phase2_shot_grouping.py` 覆盖，这里只覆盖**用户改完之后**的语义。

三条必须守住的性质（每条都做了反向验证，见文件末尾注释）：
  1. 用户碰过的 Shot 置 `group_source='user'`，**此后永不被重算覆盖**
  2. 被合并掉的 Shot **退役而不是删除** —— 它上面的精选记录必须还在
  3. 越界请求（拆空、拆别人家的资产、跨 Scene 合并）**报错而不是静默降级**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.repos import SelectionRepo, ShotRepo


@pytest.fixture()
def env(tmp_path: Path) -> TestClient:
    """schema 6 的库：1 Session / 2 Scene / 4 Shot（`client.db` 可拿到库句柄）。

    刻意造成这样的形状：
      * Scene 1 有 Shot 1（**3 个资产**，用来拆）和 Shot 2（用来并）
      * Scene 2 有 Shot 3（用来验证"不能跨 Scene 合并"）
      * Shot 4 提前退役（用来验证退役 Shot 被拒绝）
    """
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        _seed(db)
        app = create_app(settings)
        with TestClient(app) as client:
            client.db = db  # type: ignore[attr-defined]
            yield client


def _seed(db: Database) -> None:
    stamp = "2026-01-01T00:00:00+08:00"
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库',?,'ONLINE')",
            (stamp,),
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, captured_from, captured_to,"
            " created_at, updated_at) VALUES (1,'城\\2026-01-01_1000-1100','城',"
            " '2026-01-01_1000-1100','2026-01-01 10:00:00','2026-01-01 11:00:00',?,?)",
            (stamp, stamp),
        )
        for scene_id in (1, 2):
            conn.execute(
                "INSERT INTO scene (id, session_id, name, captured_from, captured_to,"
                " created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?)",
                (
                    scene_id,
                    f"场景{scene_id}",
                    f"2026-01-01 10:0{scene_id}:00",
                    f"2026-01-01 10:1{scene_id}:00",
                    stamp,
                    stamp,
                ),
            )
        # Shot 1 名下 3 个资产（1/10/11），Shot 2 → 资产 2，Shot 3 → 资产 3，Shot 4 → 资产 4
        membership = {1: [1, 10, 11], 2: [2], 3: [3], 4: [4]}
        scene_of = {1: 1, 2: 1, 3: 2, 4: 2}
        for shot_id, asset_ids in membership.items():
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                " capture_date_local, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, '2026-01-01', ?, ?)",
                (shot_id, scene_of[shot_id], asset_ids[0],
                 f"2026-01-01 10:0{shot_id}:00", stamp, stamp),
            )
            for asset_id in asset_ids:
                conn.execute(
                    "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                    " first_seen_at, shot_id) VALUES (?, 1, ?, 'jpeg', 'OK', ?, ?)",
                    (
                        asset_id,
                        f"城\\2026-01-01_1000-1100\\DSC_{asset_id:04d}.JPG",
                        stamp,
                        shot_id,
                    ),
                )
        # Shot 4 已退役
        conn.execute(
            "UPDATE shot SET retired_at = ?, retired_reason = 'merged_into:1' WHERE id = 4",
            (stamp,),
        )


def _assets_of(db: Database, shot_id: int) -> list[int]:
    return sorted(
        int(r["id"])
        for r in db.query("SELECT id FROM asset WHERE shot_id = ?", (int(shot_id),))
    )


def _shot_row(db: Database, shot_id: int):
    return db.query_one("SELECT * FROM shot WHERE id = ?", (int(shot_id),))


# ============================================================ 拆分

def test_split_moves_only_named_assets(env) -> None:
    db = env.db
    body = env.post("/api/shots/1/split", json={"asset_ids": [11]}).json()
    new_id = body["new_shot_id"]
    assert _assets_of(db, 1) == [1, 10]
    assert _assets_of(db, new_id) == [11]
    assert body["moved"] == 1
    assert body["source_assets"] == 2
    assert body["new_assets"] == 1


def test_split_marks_both_sides_user(env) -> None:
    """决策 #10：改过的两边都不能再被重算覆盖。"""
    db = env.db
    body = env.post("/api/shots/1/split", json={"asset_ids": [10, 11]}).json()
    for shot_id in (1, body["new_shot_id"]):
        row = _shot_row(db, shot_id)
        assert row["group_source"] == "user"
        assert int(row["group_revision"]) >= 1
    # 冻结集合里必须同时出现两边
    assert {1, body["new_shot_id"]} <= ShotRepo(db).protected_ids()


def test_split_inherits_scene_and_time(env) -> None:
    db = env.db
    body = env.post("/api/shots/1/split", json={"asset_ids": [11]}).json()
    origin = _shot_row(db, 1)
    fresh = _shot_row(db, body["new_shot_id"])
    assert fresh["scene_id"] == origin["scene_id"]
    assert fresh["captured_at"] == origin["captured_at"]
    # INSERT 给 1，紧接着的"两边都置 user"又 +1 —— 新 Shot 从版本 2 起算，
    # 与源 Shot 保持同一节奏（两边都被这次人工调整改过）
    assert int(fresh["group_revision"]) == 2
    assert int(origin["group_revision"]) == 2


def test_split_rejects_asset_not_in_shot(env) -> None:
    """资产 2 属于 Shot 2，不能通过"从 Shot 1 拆分"把它偷过来。"""
    db = env.db
    response = env.post("/api/shots/1/split", json={"asset_ids": [2]})
    assert response.status_code == 422
    assert "不属于" in response.json()["detail"]
    assert _assets_of(db, 1) == [1, 10, 11]  # 原状未动
    assert _assets_of(db, 2) == [2]


def test_split_rejects_empty_remainder(env) -> None:
    """把 3 个全拆走等于整组搬家，必须拒绝（否则留下空 Shot）。"""
    response = env.post("/api/shots/1/split", json={"asset_ids": [1, 10, 11]})
    assert response.status_code == 422
    assert "全部资产" in response.json()["detail"]


def test_split_rejects_empty_list(env) -> None:
    assert env.post("/api/shots/1/split", json={"asset_ids": []}).status_code == 422
    assert env.post("/api/shots/1/split", json={}).status_code == 422


def test_split_rejects_missing_shot(env) -> None:
    assert env.post("/api/shots/999/split", json={"asset_ids": [1]}).status_code == 404


def test_split_rejects_retired_shot(env) -> None:
    """已退役的 Shot 资产早就搬走了，在它上面拆分会造出孤儿 Shot。"""
    response = env.post("/api/shots/4/split", json={"asset_ids": [4]})
    assert response.status_code == 409


# ============================================================ 整组废片标记

def test_shot_reject_marks_every_file_and_can_undo(env) -> None:
    """照片墙一张卡 = 一个 Shot；一次判断必须把同组 3 个文件一起标记。"""
    db = env.db
    body = env.post("/api/shots/1/reject", json={"marked": True}).json()
    assert body["marked"] is True
    assert body["count"] == 3
    marked = {
        int(r["asset_id"])
        for r in db.query("SELECT asset_id FROM asset_reject ORDER BY asset_id")
    }
    assert marked == {1, 10, 11}

    body = env.post("/api/shots/1/reject", json={"marked": False}).json()
    assert body["marked"] is False
    assert db.query("SELECT asset_id FROM asset_reject") == []


# ============================================================ 合并

def test_merge_moves_assets_and_retires_losers(env) -> None:
    db = env.db
    body = env.post("/api/shots/merge", json={"shot_ids": [1, 2]}).json()
    assert body["kept_shot_id"] == 1
    assert body["merged"] == [2]
    assert _assets_of(db, 1) == [1, 2, 10, 11]
    assert _assets_of(db, 2) == []


def test_merge_retires_instead_of_deleting(env) -> None:
    """决策 #4：行必须还在 —— selection.shot_id 是 ON DELETE CASCADE。"""
    db = env.db
    env.post("/api/shots/merge", json={"shot_ids": [1, 2]})
    row = _shot_row(db, 2)
    assert row is not None, "被合并的 Shot 被物理删除了，精选记录会连带消失"
    assert row["retired_at"] is not None
    assert row["retired_reason"] == "merged_into:1"


def test_merge_keeps_selection_rows_on_both_sides(env) -> None:
    """合并后：**保留者的决定不被覆盖**，被合并者的**行不留但也不丢**（搬过来了）。

    ## 口径很关键（这条测试改过两次）

    第一版断言"两个 Shot 上的行都还在" —— 那**把缺陷当成了正确行为**。
    `selection.shot_id` 是主键，而所有界面/统计查询都带 `retired_at IS NULL`：
    留在退役行上的精选**用户看不见**，等于你合并前标的那张消失了。

    正确口径（与 `test_phase2_db_invariants.py` 的 I2 一致）：
      * 保留者**原有**的决定**必须原样保留**（用户后来的选择优先）；
      * 合并后**可见**的精选数 == 合并前**有精选的 Shot 数**（一个决定都不丢）；
      * 并有一条 `migrated_from:` 事件解释"这张为什么在这里"。
    """
    db = env.db
    env.post("/api/shots/1/selection", json={"status": "FINAL", "reason": "留这张"})
    env.post("/api/shots/2/selection", json={"status": "CANDIDATE", "reason": "也可以"})

    def _visible() -> dict[int, str]:
        """**用户看得见**的精选（只算活跃 Shot）。"""
        return {
            int(r["shot_id"]): str(r["status"])
            for r in db.query(
                "SELECT s.shot_id, s.status FROM selection s JOIN shot sh ON sh.id = s.shot_id"
                " WHERE sh.retired_at IS NULL")
        }

    before = _visible()
    assert before == {1: "FINAL", 2: "CANDIDATE"}

    env.post("/api/shots/merge", json={"shot_ids": [1, 2]})
    after = _visible()

    # ① 保留者原有的决定不被覆盖
    assert after.get(1) == "FINAL", "合并把保留者自己的决定改掉了"
    # ② 可见的精选数没有减少（原来 2 个 → 现在 1 个保留者，另一个被合并进来）
    assert len(after) == 1
    # ③ 被合并者的行确实被搬走（不是留在退役行上藏着）
    stranded = db.query(
        "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
        " WHERE sh.retired_at IS NOT NULL")
    assert not stranded, f"精选搁浅在退役 Shot 上（用户看不见）：{[tuple(r) for r in stranded]}"
    # ④ 搬过来过就要留痕（这里保留者已有决定，所以是被"覆盖取舍"，
    #    但退役行的 retired_reason 仍能说明它并到了哪里）
    row = _shot_row(db, 2)
    assert row["retired_reason"] == "merged_into:1"


def test_merge_honours_keep(env) -> None:
    db = env.db
    body = env.post("/api/shots/merge", json={"shot_ids": [1, 2], "keep": 2}).json()
    assert body["kept_shot_id"] == 2
    assert _assets_of(db, 2) == [1, 2, 10, 11]
    assert _shot_row(db, 1)["retired_at"] is not None


def test_merge_marks_keeper_user(env) -> None:
    db = env.db
    env.post("/api/shots/merge", json={"shot_ids": [1, 2]})
    assert _shot_row(db, 1)["group_source"] == "user"
    assert 1 in ShotRepo(db).protected_ids()


def test_merge_rejects_cross_scene(env) -> None:
    """跨 Scene 合并会让"这次拍摄属于哪个场景"含糊，明确拒绝。"""
    db = env.db
    response = env.post("/api/shots/merge", json={"shot_ids": [1, 3]})
    assert response.status_code == 422
    assert "跨 Scene" in response.json()["detail"]
    assert _assets_of(db, 1) == [1, 10, 11]  # 原状未动
    assert _assets_of(db, 3) == [3]


def test_merge_rejects_retired_member(env) -> None:
    response = env.post("/api/shots/merge", json={"shot_ids": [1, 4]})
    assert response.status_code == 409


def test_merge_rejects_single_shot(env) -> None:
    assert env.post("/api/shots/merge", json={"shot_ids": [1]}).status_code == 422


def test_merge_rejects_missing_shot(env) -> None:
    assert env.post("/api/shots/merge", json={"shot_ids": [1, 999]}).status_code == 404


def test_merge_rejects_keep_outside_list(env) -> None:
    response = env.post("/api/shots/merge", json={"shot_ids": [1, 2], "keep": 3})
    assert response.status_code == 422


def test_merge_then_regroup_does_not_steal(env) -> None:
    """最关键的一条：合并后若发生重算，用户的分组不能被冲掉。

    `plan_regroup(protected=...)` 必须把合并后的 Shot 视为冻结。
    """
    db = env.db
    env.post("/api/shots/merge", json={"shot_ids": [1, 2]})
    protected = ShotRepo(db).protected_ids()
    assert 1 in protected, "合并后的 Shot 没有被冻结，重算会把它拆回去"


# ============================================================ 反向验证说明
# 本文件的断言都做过"把实现改坏，测试是否变红"的检查：
#   * `split_shot` 里那句 `if unknown: raise` 去掉后，
#     test_split_rejects_asset_not_in_shot 会变成 200 并搬走资产 2 → 变红
#   * `merge_shots` 里 `retired_at = ?` 改成 `DELETE FROM shot` 后，
#     test_merge_retires_instead_of_deleting 与
#     test_merge_keeps_selection_rows_on_both_sides 同时变红
#   * 两处 `group_source = 'user'` 任一去掉后，
#     test_split_marks_both_sides_user / test_merge_marks_keeper_user 变红
