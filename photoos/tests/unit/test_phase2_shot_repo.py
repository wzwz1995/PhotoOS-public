"""Phase 2 · M2/M4 落库：`ShotRepo` 的测试。

本文件的**核心是决策 #4 的数据安全断言**：

> `selection.shot_id` 是 `ON DELETE CASCADE`。若重算导致 Shot 消失，
> 精选记录会被连带删掉 —— 那是不可接受的数据丢失。

因此每条重算路径都要验证 **`selection` 只增不减**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.repos import SelectionRepo, ShotRepo


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    _seed(database)
    return database


def _seed(db: Database, *, assets: int = 6) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库','t','ONLINE')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, created_at, updated_at)"
            " VALUES (1,'城\\时段','城','2026-01-01_1000-1100','t','t')"
        )
        for asset_id in range(1, assets + 1):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', 't')",
                (asset_id, f"城\\时段\\DSC_{asset_id:04d}.JPG"),
            )


def _row(shot_id, assets, *, captured_at="2026-01-01 10:00:00", confidence=0.95) -> dict:
    return {
        "shot_id": shot_id,
        "assets": list(assets),
        "captured_at": captured_at,
        "primary_asset_id": assets[0],
        "group_confidence": confidence,
    }


# ============================================================ 基本落库

def test_creates_shots_and_links_assets(db: Database) -> None:
    repo = ShotRepo(db)
    stats = repo.apply_regroup(shot_rows=[_row(None, [1, 2]), _row(None, [3])])
    assert stats["created"] == 2
    assert repo.count() == 2
    with db.read() as conn:
        linked = conn.execute(
            "SELECT shot_id FROM asset WHERE id = 1"
        ).fetchone()[0]
    assert linked is not None


def test_shot_carries_time_and_primary_asset(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [3, 4], captured_at="2026-01-01 12:34:56")])
    row = db.query_one("SELECT * FROM shot WHERE id = (SELECT MAX(id) FROM shot)")
    assert row["captured_at"] == "2026-01-01 12:34:56"
    assert row["capture_date_local"] == "2026-01-01"
    assert row["group_confidence"] == 0.95
    assert row["group_source"] == "auto"
    assert row["group_revision"] == 1


def test_reuses_existing_shot_and_bumps_revision(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    stats = repo.apply_regroup(shot_rows=[_row(shot_id, [1, 2])])
    assert stats["updated"] == 1 and stats["created"] == 0
    assert repo.get(shot_id)["group_revision"] == 2


# ============================================================ ★ 决策 #4：只增不删

def test_retire_marks_instead_of_deleting(db: Database) -> None:
    """★ 退役 = 标 `retired_at`，**绝不物理删除**。

    同时断言 `retired_reason` 记下了**资产继承者**（`regroup:into:<id>`）——
    这个记录是必要的：资产一旦改挂，退役 Shot 名下就没有任何反向引用，
    事后无法再推断"它的东西去哪了"（并发兜底要靠它追回搁浅的精选）。
    """
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]

    stats = repo.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2])], retired=[shot_id])
    assert stats["retired"] == 1
    assert repo.count(active_only=True) == 2
    assert repo.count(active_only=False) == 3          # 旧行还在
    assert repo.get(shot_id)["retired_at"] is not None
    reason = str(repo.get(shot_id)["retired_reason"])
    assert reason.startswith("regroup"), reason
    # 资产 1 落到了某个新 Shot 上 → 记录里带着它的 id
    if ":" in reason:
        successor = int(reason.rsplit(":", 1)[1])
        assert repo.get(successor)["retired_at"] is None, "继承者必须是活跃 Shot"


def test_selection_survives_regroup(db: Database) -> None:
    """★★ 本文件最重要的一条：重算**不得**让精选消失。

    先给一个 Shot 打上 FINAL，然后让重算把它的资产拆成两个新 Shot
    （原 Shot 必然"消失"）。

    ## ⚠️ 这里断言的是"**用户仍然看得见**"，而不只是"行还在"

    早期版本只断言 `selection.status_of(旧shot) == FINAL`，也就是**精选留在退役行上**。
    那满足"行还在"（`ON DELETE CASCADE` 没触发），但所有会话/场景/统计查询都带
    `retired_at IS NULL` —— 用户在工作台里**看不到自己标过的那张**，决策 #4 的意图落空。

    实测（真实库 21,400 资产、标 60 个多张资产 Shot 为 FINAL，再把 `--gap-seconds`
    从 3 改成 2）：**9 条精选落到退役行上**，界面上的 FINAL 从 60 变成 51，
    而"只增不减 60 → 60"的断言**照常通过**。所以判据必须是"活跃可见"。
    """
    shots = ShotRepo(db)
    selection = SelectionRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    selection.set_status(shot_id, sel.FINAL, reason="光最好")
    before = shots.assert_invariants()
    assert before["finals"] == 1

    shots.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2])], retired=[shot_id])

    after = shots.assert_invariants()
    assert after["finals"] == 1, "重算后 FINAL 记录丢失了 —— 违反决策 #4"
    assert after["selections"] >= before["selections"]

    # ① 行数口径：不变
    assert selection.final_count() == 1
    # ② **可见性口径**：那一条 FINAL 必须挂在一个**活跃** Shot 上
    live = db.query(
        "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
        " WHERE sh.retired_at IS NULL AND s.status = ?", (sel.FINAL,))
    assert len(live) == 1, "FINAL 跑到退役 Shot 上了 —— 用户会看不到它"
    # ③ 迁移必须留痕，能回答"这张为什么在这里"
    events = db.query(
        "SELECT from_status, to_status, reason FROM selection_event WHERE shot_id = ?",
        (int(live[0]["shot_id"]),))
    reasons = [str(r["reason"] or "") for r in events]
    assert any(r.startswith("migrated_from:") for r in reasons), reasons
    # ④ 旧行已删（"同一个决定搬家"，不是两个决定）
    assert selection.status_of(shot_id) == sel.UNREVIEWED


def test_frozen_shot_must_not_appear_in_the_plan(db: Database) -> None:
    """★★ 冻结的 Shot **本身**不得出现在重算方案里（与"资产被偷"是**两条**不同的守卫）。

    `apply_regroup()` 里有两道相关的检查，容易被当成一回事：

      ① 方案里出现了**冻结 Shot 的 id**（`shot_rows[*]["shot_id"] == 冻结id`）
         → 报错「冻结的 Shot 不允许出现在重算方案里」。**本测试守的就是这条。**
      ② 方案里出现了**冻结 Shot 的资产**（新 Shot，`shot_id=None`）
         → 报错「重算方案里包含属于冻结 Shot 的资产」。
         由 `test_regroup_must_not_steal_assets_from_frozen_shots` 守着。

    ② 是更隐蔽的那条（方案里全是**新** Shot，看不出问题），但 ① 同样必须有人守：
    调用方若直接把冻结 Shot 的 id 带进方案，等于**原地改写用户锁定的分组**。

    > 这条测试是**缺陷覆盖矩阵**（`venv/check_defect_coverage.py`）发现的缺口：
    > 矩阵里登记的 `test_frozen_shots_are_never_touched` 并不存在 ——
    > 于是回头确认"① 真的没有测试"，补上它。
    > **机械核对"每个守卫都有测试"确实能找出人肉漏掉的东西。**

    ⚠️ 断言时要注意两道守卫的**触发顺序**：如果方案行里既带冻结 id、又带**它的原资产**，
    那么先触发的是 ②（资产那条）—— 因为冻结 Shot 的资产本来就是那些。
    所以想验证 ① 必须构造"**冻结 id + 别的资产**"，也就是
    "**把用户锁定的那个组改挂到另一批资产上**"。第一版就是把两件事混在一起，报的却是 ②。
    """
    shots = ShotRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2]), _row(None, [3, 4])])
    first, second = [int(r["id"]) for r in db.query("SELECT id FROM shot ORDER BY id")]
    shots.mark_user_adjusted(first)          # 用户锁定了第一个组
    assert shots.protected_ids() == frozenset({first})

    # ① 方案里带**冻结 id**，且资产被换成**别的**（= 原地改写用户锁定的分组）→ 报 ① 那条
    with pytest.raises(ValueError, match="不允许出现在重算方案里"):
        shots.apply_regroup(shot_rows=[_row(first, [3, 4])], retired=[])
    # 不传 `frozen=` 也必须拦住 —— 冻结集合是**从库里查出来**的，不能只信调用方
    with pytest.raises(ValueError, match="不允许出现在重算方案里"):
        shots.apply_regroup(shot_rows=[_row(first, [3, 4])], retired=[], frozen=frozenset())

    # ② 方案里带**冻结 id + 它的原资产** → 由**守卫 ①**先拦下（① 在 ② 之前判断）。
    #    这道组合既"改写了冻结 Shot"又"碰了它的资产"，报 ① 是对的。
    with pytest.raises(ValueError, match="不允许出现在重算方案里"):
        shots.apply_regroup(shot_rows=[_row(first, [1, 2])], retired=[])

    # ③ 单独的"资产被偷"（**新** Shot，`shot_id=None`）→ 报 ② 那条
    with pytest.raises(ValueError, match="属于.{0,4}冻结 Shot.{0,4} 的资产"):
        shots.apply_regroup(shot_rows=[_row(None, [1, 2, 3, 4])], retired=[second])

    # 原状未动：分组还锁着，资产没挪窝
    assert shots.protected_ids() == frozenset({first})
    assert sorted(int(r["id"]) for r in db.query(
        "SELECT id FROM asset WHERE shot_id = ?", (first,))) == [1, 2]

    # 非冻结的 Shot 出现在方案里是**正常**的（别把守卫写成"谁都不许复用"）
    stats = shots.apply_regroup(shot_rows=[_row(second, [3, 4])])
    assert stats["updated"] == 1


def test_regroup_must_not_steal_assets_from_frozen_shots(db: Database) -> None:
    """★★ 决策 #10 的边界：用户锁定的 Shot，其**资产**不得被重算搬走。

    ## 为什么这条必须单独测

    只把冻结 Shot 从"可复用集合"里排除是**不够的** —— 那只是不去复用它们的 shot 行，
    它们名下的资产仍会进入新分组，`UPDATE asset SET shot_id` 就把资产搬走了。
    结果：冻结 Shot 变成**空壳**，而 `group_source='user'` 还在、`selection` 还挂着，
    用户点进"我锁定的那个组"看到的是空的。

    实测现场（真实库快照）：手工拆分一个 4 张资产的 Shot（两边冻结）后改阈值重算，
    两个冻结 Shot 的资产**全部被搬空**。

    这里断言仓库层会**报错**（而不是静默搬走）—— 与"冻结 Shot 不得出现在方案里"
    同一套哲学：静默做错比报错危险得多。
    """
    shots = ShotRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2]), _row(None, [3, 4])])
    first, second = [int(r["id"]) for r in db.query("SELECT id FROM shot ORDER BY id")]
    shots.mark_user_adjusted(first)          # 用户锁定了第一个组
    assert shots.protected_ids() == frozenset({first})
    assert shots.protected_asset_ids() == frozenset({1, 2})

    # 一个新方案想把资产 1、2 分到别处 → 必须被拒绝
    with pytest.raises(ValueError, match="冻结"):
        shots.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2, 3, 4])],
                            retired=[second])

    # 原状未动
    assert sorted(int(r["id"]) for r in db.query(
        "SELECT id FROM asset WHERE shot_id = ?", (first,))) == [1, 2]


def test_protected_asset_ids_is_empty_without_user_adjustments(db: Database) -> None:
    """没有人工调整时，`protected_asset_ids()` 必须为空 —— 否则重算会被无谓地挡住。"""
    shots = ShotRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2])])
    assert shots.protected_ids() == frozenset()
    assert shots.protected_asset_ids() == frozenset()


def test_physically_deleting_would_lose_selection(db: Database) -> None:
    """反证：**若**物理删除会怎样 —— 把"为什么必须只增不删"钉成可执行证据。"""
    shots = ShotRepo(db)
    selection = SelectionRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    selection.set_status(shot_id, sel.FINAL)
    with db.write() as conn:
        conn.execute("DELETE FROM shot WHERE id = ?", (shot_id,))
    assert selection.final_count() == 0, "级联删除确实会带走精选"


def test_invariants_snapshot_counts_everything(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    snap = repo.assert_invariants()
    assert set(snap) == {
        "shots_total", "shots_active", "shots_retired", "scenes",
        "selections", "finals", "assets_with_shot",
    }
    assert snap["assets_with_shot"] == 2


# ============================================================ ★ 决策 #10：用户调整冻结

def test_frozen_shot_is_never_touched(db: Database) -> None:
    """★ `group_source='user'` 的 Shot **永不被重算覆盖**（决策 #10）。

    正确的分工是：调用方用 `plan_regroup(protected=...)` 把冻结的 Shot 从新方案里
    排除掉，然后传进来的 `shot_rows` 里就不该有它。
    """
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2]), _row(None, [3, 4])])
    frozen_id = db.query_one("SELECT MIN(id) AS id FROM shot")["id"]
    repo.mark_user_adjusted(frozen_id)
    assert repo.protected_ids() == frozenset({frozen_id})

    # 新方案只处理"未被冻结"的部分：资产 3、4 合并，资产 5、6 各自成组。
    # 冻结的 frozen_id 既不出现在 shot_rows，也不在 retired 里。
    repo.apply_regroup(
        shot_rows=[_row(None, [3, 4]), _row(None, [5]), _row(None, [6])],
        retired=[],
        frozen=repo.protected_ids(),
    )
    row = repo.get(frozen_id)
    assert row["retired_at"] is None, "冻结的 Shot 被退役了"
    assert row["group_source"] == "user"
    # ★ 关键：冻结 Shot 的资产**没有被搬走**
    remaining = sorted(
        r["id"] for r in db.query("SELECT id FROM asset WHERE shot_id = ?", (frozen_id,))
    )
    assert remaining == [1, 2], f"冻结 Shot 的资产被搬走了：{remaining}"


def test_frozen_shot_in_plan_is_rejected_not_silently_skipped(db: Database) -> None:
    """★ 回归：冻结的 Shot 出现在 `shot_rows` 里必须**报错**，不能静默跳过。

    早期实现是 `continue` 跳过那一行，但那只跳过了 Shot 自身的 UPDATE，
    下面的 `UPDATE asset SET shot_id` 仍会执行 —— 结果是把用户手工锁定的 Shot
    的资产搬走，而它的 `group_source='user'` 还在，看起来"没被动过"。
    """
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    repo.mark_user_adjusted(shot_id)

    with pytest.raises(ValueError, match="冻结的 Shot"):
        repo.apply_regroup(
            shot_rows=[_row(shot_id, [1])],
            frozen=repo.protected_ids(),
        )

    # 报错之后数据完好：资产仍属于冻结的 Shot
    remaining = [r["id"] for r in db.query("SELECT id FROM asset WHERE shot_id = ?", (shot_id,))]
    assert sorted(remaining) == [1, 2]


def test_frozen_and_retired_conflict_is_rejected(db: Database) -> None:
    """冻结与退役同时指定 → 明确报错，而不是静默做一半。"""
    repo = ShotRepo(db)
    with pytest.raises(ValueError):
        repo.apply_regroup(shot_rows=[], retired=[1], frozen=frozenset({1}))


def test_mark_user_adjusted_bumps_revision(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    repo.mark_user_adjusted(shot_id)
    assert repo.get(shot_id)["group_revision"] == 2


# ============================================================ 资产归属

def test_assets_are_reassigned_on_regroup(db: Database) -> None:
    """重算会改 `asset.shot_id` —— 这是**派生线索**的变化，资产本身不动（铁律 #3）。"""
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    first = db.query_one("SELECT id FROM shot")["id"]
    repo.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2])], retired=[first])
    with db.read() as conn:
        pairs = {r["id"]: r["shot_id"] for r in conn.execute("SELECT id, shot_id FROM asset")}
    assert pairs[1] != pairs[2]
    assert len({pairs[1], pairs[2]}) == 2


def test_asset_count_is_never_reduced(db: Database) -> None:
    """★ 铁律 #3：重算绝不丢资产。"""
    repo = ShotRepo(db)
    with db.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
    repo.apply_regroup(shot_rows=[_row(None, [1, 2, 3]), _row(None, [4])])
    repo.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2, 3, 4])], retired=[1, 2])
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == before


def test_active_assets_and_unassigned_count(db: Database) -> None:
    repo = ShotRepo(db)
    assert repo.unassigned_asset_count() == 6
    repo.apply_regroup(shot_rows=[_row(None, [1, 2])])
    assert repo.unassigned_asset_count() == 4
    assert repo.active_assets() != {}


# ============================================================ Scene 落库

def test_replace_scenes_creates_and_links(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2]), _row(None, [3])])
    shot_ids = [r["id"] for r in db.query("SELECT id FROM shot ORDER BY id")]
    result = repo.replace_scenes(
        session_id=1,
        scenes=[
            {
                "name": "场景A", "captured_from": "2026-01-01 10:00:00",
                "captured_to": "2026-01-01 10:05:00", "shot_ids": shot_ids[:2],
                "evidence_json": '{"size":2}',
            },
            {
                "name": "场景B", "captured_from": "2026-01-01 11:00:00",
                "captured_to": "2026-01-01 11:30:00", "shot_ids": shot_ids[2:],
                "evidence_json": '{"size":1}',
            },
        ],
    )
    assert result["created"] == 2
    assert repo.scene_count() == 2
    with db.read() as conn:
        linked = conn.execute(
            "SELECT scene_id FROM shot WHERE id = ?", (shot_ids[0],)
        ).fetchone()[0]
    assert linked is not None


def test_replace_scenes_reuses_rows_by_name(db: Database) -> None:
    """同名 Scene 复用既有行（`group_revision` +1），不堆积重复行。"""
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    scene = {"name": "场景A", "captured_from": "t", "captured_to": "t2", "shot_ids": [shot_id]}
    repo.replace_scenes(session_id=1, scenes=[scene])
    result = repo.replace_scenes(session_id=1, scenes=[scene])
    assert result["updated"] == 1 and result["created"] == 0
    assert repo.scene_count() == 1
    assert db.query_one("SELECT group_revision FROM scene")["group_revision"] == 2


def test_replace_scenes_clears_old_links(db: Database) -> None:
    """重建时必须先把旧关联清空 —— 否则两个 Scene 会共享同一个 Shot。"""
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1]), _row(None, [2])])
    shot_ids = [r["id"] for r in db.query("SELECT id FROM shot ORDER BY id")]
    repo.replace_scenes(
        session_id=1,
        scenes=[{"name": "A", "captured_from": "t", "captured_to": "t", "shot_ids": shot_ids}],
    )
    repo.replace_scenes(
        session_id=1,
        scenes=[{"name": "B", "captured_from": "t", "captured_to": "t", "shot_ids": [shot_ids[0]]}],
    )
    with db.read() as conn:
        rows = conn.execute("SELECT id, scene_id FROM shot ORDER BY id").fetchall()
    linked = [r["scene_id"] for r in rows if r["scene_id"] is not None]
    assert len(linked) == 1, f"应只有一个 Shot 挂着 Scene，实际 {linked}"


def test_scene_rebuild_preserves_selections(db: Database) -> None:
    """重建 Scene 不得影响精选（Scene 是纯派生结构，精选挂在 Shot 上）。"""
    shots = ShotRepo(db)
    selection = SelectionRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    selection.set_status(shot_id, sel.FINAL)
    shots.replace_scenes(
        session_id=1,
        scenes=[{"name": "A", "captured_from": "t", "captured_to": "t", "shot_ids": [shot_id]}],
    )
    assert selection.final_count() == 1


def test_set_scene_and_clear(db: Database) -> None:
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    with db.write() as conn:
        conn.execute("INSERT INTO scene (id, session_id, name, created_at, updated_at) VALUES (1,1,'S','t','t')")
    assert repo.set_scene(shot_id, 1) is True
    assert repo.set_scene(shot_id, None) is True


# ============================================================ 多轮重算的稳定性

def test_repeated_regroup_is_stable(db: Database) -> None:
    """同一方案跑两次：第二次应当全部命中复用，不产生新行、不退役。"""
    repo = ShotRepo(db)
    repo.apply_regroup(shot_rows=[_row(None, [1, 2]), _row(None, [3, 4])])
    existing = repo.active_assets()
    reused = [
        _row(shot_id, sorted(assets)) for shot_id, assets in existing.items()
    ]
    stats = repo.apply_regroup(shot_rows=reused)
    assert stats["created"] == 0 and stats["retired"] == 0
    assert repo.count(active_only=False) == 2


def test_multi_round_regroup_never_loses_selection(db: Database) -> None:
    """★ 多轮重算的压力测试：每轮都改变分组方式，精选必须一直在。"""
    shots = ShotRepo(db)
    selection = SelectionRepo(db)
    shots.apply_regroup(shot_rows=[_row(None, [1, 2, 3, 4, 5, 6])])
    shot_id = db.query_one("SELECT id FROM shot")["id"]
    selection.set_status(shot_id, sel.FINAL)

    plans = [
        [[1, 2, 3], [4, 5, 6]],
        [[1], [2, 3], [4, 5, 6]],
        [[1, 2], [3, 4], [5], [6]],
        [[1, 2, 3, 4, 5, 6]],
    ]
    for plan in plans:
        retired = sorted(repo_id for repo_id in shots.active_assets())
        shots.apply_regroup(
            shot_rows=[_row(None, group) for group in plan], retired=retired
        )
        snap = shots.assert_invariants()
        assert snap["finals"] == 1, f"某一轮重算后 FINAL 丢失了：plan={plan}"
        assert snap["assets_with_shot"] == 6, "资产归属数变了"
