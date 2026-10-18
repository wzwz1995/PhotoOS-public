"""全库一致性不变式（把两类"意图落空"缺陷变成**通用**回归测试）。

## 为什么需要这组测试（而不是再多几个针对性测试）

§13.35（精选行还在但用户看不见）与 §13.36（用户锁定的组被搬空）是**同一个毛病**：
守卫检查的层次错了 —— 数"表里有多少行"，而不是数"用户实际看到什么"。
一个 bug 一个 bug 地补针对性测试，抓不住"下一处同类的错"。

所以这里把整类性质写成**九条全库不变式**，并在**经过对抗性操作之后**再验一次：

| # | 不变式 | 挡住哪类缺陷 |
| --- | --- | --- |
| I1 | 每个**活跃** Shot 至少有 1 个资产 | 空壳组（§13.36 的形态）|
| I2 | 每条精选都挂在**活跃** Shot 上 | 精选搁浅（§13.35 的形态）|
| I3 | `shot.primary_asset_id` 属于该 Shot | 封面指向已搬走的资产 |
| I4 | `asset.shot_id` 指向存在的 Shot | 悬空外键 |
| I5 | `shot.scene_id` 指向存在的 Scene | 悬空 Scene |
| I6 | 冻结（`group_source='user'`）的活跃 Shot 至少有 1 个资产 | 锁定组被搬空 |
| I7 | 主题里没有指向**退役** Shot 的条目 | 主题里出现用户看不见的成员 |
| I8 | 主题列表页条目数 == 详情页能看见的条目数 | 两个计数口径不一致 |
| I9 | 主题的**显式封面**指向活跃 Shot | 封面取不到图 |

> ⚠️ 这张表与下面 `INVARIANTS` 元组必须**同步改**。历史上前六条写完之后
> I7–I9 才加进来，注释里的"六条/八条"长期与代码的"九条"不一致
> （本文件里曾同时出现三种说法，见 20-reference 的 Q-7）。
> 以 `INVARIANTS` 元组的长度为准。

> **这组断言的价值在于口径**：它们不看"表里有多少行"，只看"引用关系是否自洽"。
> 早期那些全绿的测试恰恰是因为口径是"行数"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.library.shot_grouping import ShotAsset, ShotGroup
from photoos.repos import SelectionRepo, ShotRepo

#: 九条不变式（I1–I9，与模块 docstring 的表格同步），
#: `(编号, 说明, SQL, 是否只在有 retired_at 时适用)`
INVARIANTS: tuple[tuple[str, str, str, bool], ...] = (
    (
        "I1",
        "每个活跃 Shot 至少有 1 个资产",
        "SELECT sh.id FROM shot sh WHERE sh.retired_at IS NULL"
        "  AND NOT EXISTS (SELECT 1 FROM asset a WHERE a.shot_id = sh.id)",
        True,
    ),
    (
        "I2",
        "每条精选都挂在活跃 Shot 上（用户可见）",
        "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
        " WHERE sh.retired_at IS NOT NULL",
        True,
    ),
    (
        "I3",
        "shot.primary_asset_id 属于该 Shot",
        "SELECT sh.id FROM shot sh JOIN asset a ON a.id = sh.primary_asset_id"
        " WHERE sh.primary_asset_id IS NOT NULL AND a.shot_id IS NOT sh.id"
        "   AND sh.retired_at IS NULL",
        True,
    ),
    (
        "I4",
        "asset.shot_id 指向存在的 Shot",
        "SELECT a.id FROM asset a WHERE a.shot_id IS NOT NULL"
        "  AND NOT EXISTS (SELECT 1 FROM shot sh WHERE sh.id = a.shot_id)",
        False,
    ),
    (
        "I5",
        "shot.scene_id 指向存在的 Scene",
        "SELECT sh.id FROM shot sh WHERE sh.scene_id IS NOT NULL"
        "  AND NOT EXISTS (SELECT 1 FROM scene sc WHERE sc.id = sh.scene_id)"
        "  AND sh.retired_at IS NULL",
        True,
    ),
    (
        "I6",
        "人工锁定（group_source='user'）的 Shot 未被搬空",
        "SELECT sh.id FROM shot sh WHERE sh.retired_at IS NULL"
        "  AND sh.group_source = 'user'"
        "  AND NOT EXISTS (SELECT 1 FROM asset a WHERE a.shot_id = sh.id)",
        True,
    ),
    (
        "I7",
        "主题里没有指向**退役** Shot 的条目（否则用户看不见它）",
        "SELECT ci.collection_id, ci.shot_id FROM collection_item ci"
        "  JOIN shot sh ON sh.id = ci.shot_id"
        " WHERE sh.retired_at IS NOT NULL",
        True,
    ),
    (
        "I8",
        "主题列表页的条目数 == 详情页能看见的条目数",
        "SELECT c.id FROM collection c WHERE"
        "  (SELECT COUNT(*) FROM collection_item ci WHERE ci.collection_id = c.id)"
        "  <>"
        "  (SELECT COUNT(*) FROM collection_item ci2"
        "     JOIN shot sh2 ON sh2.id = ci2.shot_id AND sh2.retired_at IS NULL"
        "    WHERE ci2.collection_id = c.id)",
        True,
    ),
    (
        "I9",
        "主题的**显式封面**指向活跃 Shot（否则界面上封面取不到图）",
        "SELECT c.id FROM collection c JOIN shot sh ON sh.id = c.cover_shot_id"
        " WHERE c.cover_shot_id IS NOT NULL AND sh.retired_at IS NOT NULL",
        True,
    ),
)


def assert_invariants_hold(db: Database, *, where: str) -> None:
    """九条不变式全部为空集才算通过。失败时把**违规行**打进断言消息。"""
    problems: list[str] = []
    for key, title, sql, needs_retired in INVARIANTS:
        if needs_retired:
            has_column = any(
                str(r[1]) == "retired_at" for r in db.query("PRAGMA table_info(shot)")
            )
            if not has_column:
                continue
        rows = db.query(sql)
        if rows:
            samples = [tuple(r) for r in rows[:5]]
            problems.append(f"{key} {title}：{len(rows)} 条违规，样例 {samples}")
    assert not problems, f"[{where}] 全库不变式被违反：\n  " + "\n  ".join(problems)


def _seed(db: Database, *, shots: int = 6, assets_per_shot: int = 2) -> None:
    """造 `shots` 个 Shot，每个含 `assets_per_shot` 个资产（同一时段、时间相邻）。"""
    with db.write() as conn:
        conn.execute("INSERT INTO library (uuid,name,created_at,status)"
                     " VALUES ('l','库','t','ONLINE')")
        conn.execute("INSERT INTO session (id,name,city_hint,period_hint,created_at,updated_at)"
                     " VALUES (1,'城\\2026-01-01_1000-1100','城','2026-01-01_1000-1100','t','t')")
        conn.execute("INSERT INTO scene (id,session_id,name,captured_from,captured_to,"
                     "created_at,updated_at) VALUES (1,1,'场景','a','b','t','t')")
        asset_id = 1
        for shot_index in range(shots):
            shot_id = shot_index + 1
            minute = shot_index
            conn.execute(
                "INSERT INTO shot (id,scene_id,captured_at,capture_date_local,created_at,updated_at)"
                " VALUES (?,1,?,'2026-01-01','t','t')",
                (shot_id, f"2026-01-01 10:{minute:02d}:00"))
            for _ in range(assets_per_shot):
                conn.execute(
                    "INSERT INTO asset (id,library_id,relative_path,file_type,status,"
                    "first_seen_at,shot_id) VALUES (?,1,?,'jpeg','OK','t',?)",
                    (asset_id, f"城\\2026-01-01_1000-1100\\DSC_{asset_id:04d}.JPG", shot_id))
                asset_id += 1
            conn.execute("UPDATE shot SET primary_asset_id = ? WHERE id = ?",
                         (shot_id * assets_per_shot - assets_per_shot + 1, shot_id))


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    _seed(database)
    return database


# ============================================================ 干净状态

def test_invariants_hold_on_a_clean_library(db: Database) -> None:
    assert_invariants_hold(db, where="刚建好")


# ============================================================ 对抗性操作之后

def test_invariants_hold_after_retire_and_reselect(db: Database) -> None:
    """退役一个 Shot 并把精选迁到继承者之后，九条不变式仍须成立。"""
    shots, selection = ShotRepo(db), SelectionRepo(db)
    selection.set_status(1, sel.FINAL, reason="留下")
    # 1、2 号资产改挂到一个新 Shot，旧的 1 号 Shot 退役
    shots.apply_regroup(
        shot_rows=[{"shot_id": None, "assets": [1, 2, 3, 4],
                    "captured_at": "2026-01-01 10:00:00", "primary_asset_id": 1,
                    "group_confidence": 0.95}],
        retired=[1, 2],
    )
    assert_invariants_hold(db, where="退役并重算之后")


def test_invariants_hold_after_manual_split_and_regroup(db: Database) -> None:
    """★ 手工拆分（冻结两个组）之后再重算 —— §13.36 的完整场景。

    修之前这里会红：冻结 Shot 的资产被搬空（I1 + I6）。
    """
    shots, selection = ShotRepo(db), SelectionRepo(db)
    selection.set_status(1, sel.FINAL, reason="锁定的组")

    new_shot = shots.split_shot(1, [2])          # 1 号 Shot 拆出资产 2
    assert shots.protected_ids() == frozenset({1, new_shot})
    assert_invariants_hold(db, where="手工拆分之后")

    # 现在重算：任何包含锁定资产的方案都必须被拒绝（而不是静默搬走）
    with pytest.raises(ValueError, match="冻结"):
        shots.apply_regroup(
            shot_rows=[{"shot_id": None, "assets": [1, 2],
                        "captured_at": "2026-01-01 10:00:00", "primary_asset_id": 1,
                        "group_confidence": 0.95}],
            retired=[new_shot],
        )
    assert_invariants_hold(db, where="被拒绝的重算之后（库应原样）")
    # 锁定组及其精选都还在
    assert shots.protected_asset_ids() == frozenset({1, 2})
    assert selection.status_of(1) == sel.FINAL


def test_invariants_hold_after_scene_rebuild(db: Database) -> None:
    """Scene 重建（`replace_scenes` 只改 `shot.scene_id`）之后不变式仍成立。"""
    shots = ShotRepo(db)
    shots.replace_scenes(session_id=1, scenes=[
        {"name": "新场景", "captured_from": "2026-01-01 10:00:00",
         "captured_to": "2026-01-01 10:05:00", "shot_ids": [1, 2, 3]},
    ])
    assert_invariants_hold(db, where="Scene 重建之后")
    # 被移出方案的 Shot 应变成"没有 Scene"，而不是指向已消失的 Scene
    orphan = db.query("SELECT id FROM shot WHERE scene_id IS NULL")
    assert orphan, "重建后应有 Shot 不再属于任何 Scene"


def test_invariants_hold_after_merge(db: Database) -> None:
    """合并两个 Shot（一个退役）之后不变式仍成立。"""
    shots, selection = ShotRepo(db), SelectionRepo(db)
    selection.set_status(2, sel.CANDIDATE, reason="合并前留的")
    shots.merge_shots([1, 2], keep=1)
    assert_invariants_hold(db, where="合并之后")
    # 被合并掉的那个 Shot 上的精选必须迁到保留者（否则 I2 会红）
    visible = db.query(
        "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
        " WHERE sh.retired_at IS NULL")
    assert len(visible) == 1


def test_collection_members_survive_regroup(db: Database) -> None:
    """★ 主题（Collections）的成员必须随 Shot 重算一起搬迁。

    `collection_item.shot_id` 指向 Shot，而 `CollectionRepo.items()` 带
    `sh.retired_at IS NULL` —— 不搬的话用户整理好的主题会**悄悄少条目**。

    实测（真实库快照，主题里放 120 个 Shot，再改 `--gap-seconds` 3→2）：
    可看见 **120 → 102**（少 18 条），而列表页仍显示 120 —— 两个数字对不上（I7 + I8）。
    """
    from photoos.repos import CollectionRepo

    shots, collections = ShotRepo(db), CollectionRepo(db)
    cid = collections.create("我的精选集")
    collections.add_shots(cid, [1, 2, 3])
    assert len(collections.items(cid)) == 3

    # 1、2 号资产改挂到一个新 Shot，旧的 1 号 Shot 退役
    shots.apply_regroup(
        shot_rows=[{"shot_id": None, "assets": [1, 2, 3, 4],
                    "captured_at": "2026-01-01 10:00:00", "primary_asset_id": 1,
                    "group_confidence": 0.95}],
        retired=[1, 2],
    )
    assert_invariants_hold(db, where="主题成员搬迁之后")
    # ⚠️ 期望是 **2** 而不是 3：1 号与 2 号**合并成了同一个 Shot**，它们的两条主题条目
    # 也因此并成一条 —— 这是正确行为（`collection_item` 主键是
    # `(collection_id, shot_id)`，同一张作品在一个主题里只能有一条）。
    # 要紧的是"一条都没**丢**"：两个 Shot 都还在这张新 Shot 里。
    items = collections.items(cid)
    assert len(items) == 2, "主题里的东西被重算弄丢了"
    owner = db.query_one("SELECT shot_id FROM asset WHERE id = 1")["shot_id"]
    assert int(items[0]["shot_id"]) == int(owner), "迁移后应指向继承者"
    assert [int(i["shot_id"]) for i in items] == [int(owner), 3]

    # 列表页的数字必须与详情页一致（同一口径）
    listed = next(c for c in collections.list_all() if int(c["id"]) == cid)
    assert listed["items"] == len(collections.items(cid))


def test_collection_count_ignores_retired_shots(db: Database) -> None:
    """列表计数与详情条目数**必须同口径**（都只数能看见的）。"""
    from photoos.repos import CollectionRepo

    collections = CollectionRepo(db)
    cid = collections.create("集")
    collections.add_shots(cid, [1, 2, 3])
    with db.write() as conn:      # 直接退役一个，模拟"没有继承者"的情形
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 2")
    listed = next(c for c in collections.list_all() if int(c["id"]) == cid)
    assert listed["items"] == 2, "列表计数把看不见的条目也算进去了"
    assert listed["items"] == len(collections.items(cid))


# ============================================================ 自检：审计真的能抓到东西

def test_auditor_actually_detects_violations(db: Database) -> None:
    """★ 反向验证：人为制造违规，确认这九条**真的会红**。

    没有这一条，上面所有"全部通过"都可能是**空断言**（比如 SQL 写错导致永远查不出行）。
    """
    # I1 + I6：把 Shot 1 的资产搬走并冻结它
    with db.write() as conn:
        conn.execute("UPDATE asset SET shot_id = 2 WHERE shot_id = 1")
        conn.execute("UPDATE shot SET group_source = 'user' WHERE id = 1")
    with pytest.raises(AssertionError) as excinfo:
        assert_invariants_hold(db, where="人为制造空壳")
    message = str(excinfo.value)
    assert "I1" in message and "I6" in message, message

    # I3：封面指向别人的资产
    with db.write() as conn:
        conn.execute("UPDATE shot SET primary_asset_id = 3 WHERE id = 2")
    with pytest.raises(AssertionError, match="I3"):
        assert_invariants_hold(db, where="人为制造错封面")

    # I2：把有精选的 Shot 退役，让精选"看不见"
    SelectionRepo(db).set_status(2, sel.FINAL)
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 2")
    with pytest.raises(AssertionError, match="I2"):
        assert_invariants_hold(db, where="人为制造搁浅精选")


def test_auditor_detects_collection_violations(db: Database) -> None:
    """★ I7 / I8 / I9 也必须真的会红 —— 否则它们就是空断言。"""
    from photoos.repos import CollectionRepo

    collections = CollectionRepo(db)
    cid = collections.create("集", cover_shot_id=2)
    collections.add_shots(cid, [1, 2])
    # 直接改库把 Shot 2 退役但不搬引用 → I7（孤条目）/ I8（计数不一致）/ I9（封面退役）
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 2")
    with pytest.raises(AssertionError) as excinfo:
        assert_invariants_hold(db, where="人为制造主题孤条目与退役封面")
    message = str(excinfo.value)
    assert "I7" in message, message
    assert "I8" in message, message
    assert "I9" in message, message


def test_reconcile_fixes_stranded_selection(db: Database) -> None:
    """★ 并发兜底：把**已经搁浅**的精选追回到继承者上。

    为什么需要这个兜底（实测到的竞态）：`apply_regroup()` 的搬迁只能搬
    **那一刻已经存在**的引用。真实用法里界面开着、`serve` 是另一个进程：

        用户：标 FINAL(shot 52)        ← serve 进程
        重算：退役 shot 52 + 搬引用    ← CLI 进程

    "标"若发生在重算的守卫检查之后、提交之前，那条精选就落在已退役的 Shot 上，
    没有任何人再回头看它。实测：一个线程按 4 ms 间隔标 300 个 Shot、
    同时跑重算（1.5 s），出现静默搁浅（写入异常 0 条）。

    这里**人为构造**那个状态：照常跑一次重算（于是退役行上记下了
    `regroup:into:<继承者>`），然后**绕过 `set_status` 的退役守卫**直接写一条精选 ——
    正是并发下漏进来的那条。
    """
    shots, selection = ShotRepo(db), SelectionRepo(db)

    # 1、2 号资产并到一个新 Shot，旧的 1 号 Shot 退役（继承者是那个新 Shot）
    stats = shots.apply_regroup(
        shot_rows=[{"shot_id": None, "assets": [1, 2, 3, 4],
                    "captured_at": "2026-01-01 10:00:00", "primary_asset_id": 1,
                    "group_confidence": 0.95}],
        retired=[1, 2],
    )
    row = db.query_one("SELECT retired_reason FROM shot WHERE id = 1")
    assert str(row["retired_reason"]).startswith("regroup:into:"), row["retired_reason"]
    successor_id = int(str(row["retired_reason"]).rsplit(":", 1)[1])

    # 直接写库 = 并发下"漏进来"的那一条（守卫没拦住）
    with db.write() as conn:
        conn.execute("INSERT INTO selection (shot_id, status, reason, updated_at)"
                     " VALUES (1,'FINAL','并发下标的','2026-01-02T00:00:00+08:00')")
    with pytest.raises(AssertionError, match="I2"):
        assert_invariants_hold(db, where="人为构造搁浅")

    moved = shots.reconcile_stranded_references()
    assert moved["selection_migrated"] == 1, moved
    assert_invariants_hold(db, where="兜底追回之后")
    with db.read() as conn:
        got = conn.execute(
            "SELECT s.shot_id, s.status, s.reason FROM selection s"
            " JOIN shot sh ON sh.id = s.shot_id WHERE sh.retired_at IS NULL").fetchone()
    assert int(got["shot_id"]) == successor_id
    assert got["status"] == "FINAL" and got["reason"] == "并发下标的"
    # 幂等：再跑一次不该产生任何变化
    assert shots.reconcile_stranded_references()["selection_migrated"] == 0


def test_reconcile_needs_the_recorded_successor(db: Database) -> None:
    """★ 说明性测试：**为什么必须把继承者记在退役行上**。

    推断继承者的两种来源：
      ① **记录**：`retired_reason = 'regroup:into:<id>'` / `'merged_into:<id>'`
      ② **推断**：看"它原来的资产现在多数落在哪"

    但这个测试证明 ② 在退役场景下**必然失效**：退役之后资产已经改挂到新 Shot，
    `SELECT ... FROM asset WHERE shot_id = <退役 id>` 返回**空集** ——
    退役 Shot 名下**没有**任何反向引用可查。实测 `members = {}`。

    （② 只在"资产仍挂在退役 Shot 上"时才有效，那是手工改库或异常中断才会出现的状态。
      保留 ② 是为了兼容老库：早期的退役行只写 `'regroup'`，没有记 `<id>`。）
    """
    shots = ShotRepo(db)

    # 退役行上**没有**记录（模拟老库），且资产已经被搬走 → 推断不出继承者
    with db.write() as conn:
        conn.execute("UPDATE asset SET shot_id = 2 WHERE id IN (1, 2)")
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00',"
                     " retired_reason = 'regroup' WHERE id = 1")
        conn.execute("INSERT INTO selection (shot_id, status, updated_at)"
                     " VALUES (1,'FINAL','2026-01-02T00:00:00+08:00')")
    # 退役 Shot 名下已无资产 —— 没有任何东西可以反查
    assert db.query("SELECT id FROM asset WHERE shot_id = 1") == []
    moved = shots.reconcile_stranded_references()
    assert moved["selection_migrated"] == 0
    assert moved["stranded_shots"] == 1, "应当**如实报告**有引用搁浅、而不是假装修好了"

    # 补上记录之后（等价于"现在的重算会写下 regroup:into:<id>"），就能追回
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_reason = 'regroup:into:2' WHERE id = 1")
    moved = shots.reconcile_stranded_references()
    assert moved["selection_migrated"] == 1, moved
    assert_invariants_hold(db, where="按记录追回之后")
    with db.read() as conn:
        got = conn.execute(
            "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
            " WHERE sh.retired_at IS NULL").fetchone()
    assert int(got["shot_id"]) == 2


def test_set_status_refuses_retired_shot(db: Database) -> None:
    """仓库层也**必须**拒绝给退役 Shot 标精选（否则会静默产生看不见的决定）。

    API 层早就返回 409，但 CLI 与并发写入走的是仓库层 —— 那里原本没有守卫。
    """
    shots, selection = ShotRepo(db), SelectionRepo(db)
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 1")
    with pytest.raises(ValueError, match="退役"):
        selection.set_status(1, sel.FINAL)
    with pytest.raises(ValueError, match="不存在"):
        selection.set_status(999, sel.FINAL)


def test_collection_cover_survives_regroup(db: Database) -> None:
    """★ 主题的**显式封面**必须随 Shot 重算搬到继承者上。

    不搬的话 `list_all()` 的 `COALESCE(cover_shot_id, 自动首条)` 会优先取那个
    已退役的封面 —— 界面上主题封面取不到图（I9）。

    实测（真实库快照）：选一个"必然会被 gap 3→2 拆散"的 Shot 当封面，
    重算后 `explicit_cover` 指向**已退役** Shot —— 命中这个缺陷。
    """
    from photoos.repos import CollectionRepo

    shots, collections = ShotRepo(db), CollectionRepo(db)
    cid = collections.create("封面集", cover_shot_id=1)
    collections.add_shots(cid, [1, 2, 3])

    # 1、2 号资产并到一个新 Shot，旧的 1 号 Shot 退役
    shots.apply_regroup(
        shot_rows=[{"shot_id": None, "assets": [1, 2, 3, 4],
                    "captured_at": "2026-01-01 10:00:00", "primary_asset_id": 1,
                    "group_confidence": 0.95}],
        retired=[1, 2],
    )
    assert_invariants_hold(db, where="封面搬迁之后")
    row = dict(collections.get(cid))
    owner = int(db.query_one("SELECT shot_id FROM asset WHERE id = 1")["shot_id"])
    assert int(row["cover_shot_id"]) == owner, "封面没有跟着继承者走"
    listed = next(c for c in collections.list_all() if int(c["id"]) == cid)
    assert int(listed["effective_cover_shot_id"]) == owner


def test_merge_moves_collection_members_and_cover(db: Database) -> None:
    """手工合并也必须搬主题成员与封面（内联版本曾漏掉这两处）。"""
    from photoos.repos import CollectionRepo

    shots, collections = ShotRepo(db), CollectionRepo(db)
    cid = collections.create("合并集", cover_shot_id=2)
    collections.add_shots(cid, [1, 2, 3])

    shots.merge_shots([1, 2], keep=1)

    assert_invariants_hold(db, where="手工合并之后")
    row = dict(collections.get(cid))
    assert int(row["cover_shot_id"]) == 1, "封面没跟着合并走"
    ids = [int(i["shot_id"]) for i in collections.items(cid)]
    assert ids == [1, 3], f"主题成员没正确合并：{ids}"
