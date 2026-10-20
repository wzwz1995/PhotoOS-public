"""Phase 2 · M5：精选状态机的落库与过程追溯测试。

PRD §9.2 的四条要求逐条对应到本文件：
  * 「候选、精选、最终精选三级」      → 状态迁移
  * 「允许用户随时退回，不删除原片」  → `clear()` 与"退回"事件的记录
  * 「记录选择过程：选了什么、放弃了什么、最终留下什么」 → `selection_event` 与 `abandoned()`
  * 「允许给精选添加简短理由」        → `reason`

**外加一条铁律 #1 的硬断言**：精选全过程**不产生任何文件系统调用**。
"""

from __future__ import annotations

import builtins
import sqlite3
from pathlib import Path

import pytest

from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.repos import SelectionRepo


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    _seed(database)
    return database


def _seed(db: Database) -> None:
    """一个 Library + 一个 Scene + 3 个 Shot（每个 Shot 一个 asset）。"""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO session (id, name, created_at, updated_at)"
            " VALUES (1,'城\\时段','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (id, session_id, name, created_at, updated_at)"
            " VALUES (1, 1, '城\\时段\\场景1','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO scene (id, session_id, name, created_at, updated_at)"
            " VALUES (2, 1, '城\\时段\\场景2','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        for shot_id, scene_id in ((1, 1), (2, 1), (3, 2)):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')",
                (shot_id, f"城\\时段\\DSC_{shot_id:04d}.JPG"),
            )
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at, capture_date_local,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, '2026-01-01 10:0' || ?, '2026-01-01',"
                " '2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')",
                (shot_id, scene_id, shot_id, shot_id),
            )
            conn.execute("UPDATE asset SET shot_id = ? WHERE id = ?", (shot_id, shot_id))


# ============================================================ 默认状态

def test_missing_row_means_unreviewed(db: Database) -> None:
    """`selection` 表没有行 = 从未审阅（Phase 1 建表时的语义，LEFT JOIN 拿到 NULL）。"""
    repo = SelectionRepo(db)
    assert repo.status_of(1) == sel.UNREVIEWED
    assert repo.get(1) is None
    assert repo.total() == 0


def test_status_map_defaults_to_unreviewed(db: Database) -> None:
    repo = SelectionRepo(db)
    assert repo.status_map([1, 2, 3]) == {1: "UNREVIEWED", 2: "UNREVIEWED", 3: "UNREVIEWED"}


def test_status_map_empty(db: Database) -> None:
    assert SelectionRepo(db).status_map([]) == {}


# ============================================================ 三级状态机

def test_forward_through_three_levels(db: Database) -> None:
    """PRD 的候选 → 精选 → 最终精选。"""
    repo = SelectionRepo(db)
    assert repo.set_status(1, sel.CANDIDATE) is True
    assert repo.status_of(1) == sel.CANDIDATE
    assert repo.set_status(1, sel.SELECTED) is True
    assert repo.set_status(1, sel.FINAL) is True
    assert repo.status_of(1) == sel.FINAL


def test_jump_directly_to_final(db: Database) -> None:
    """允许跳过中间态（PRD 没要求必须相邻推进）。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    assert repo.status_of(1) == sel.FINAL


def test_repeated_same_status_without_reason_is_a_noop(db: Database) -> None:
    """★ 连按同一个键不该刷出满屏事件（否则时间线会被噪声淹没）。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.SELECTED)
    before = len(repo.history(1))
    assert repo.set_status(1, sel.SELECTED) is False
    assert len(repo.history(1)) == before


def test_same_status_with_reason_is_recorded(db: Database) -> None:
    """状态没变但补了理由 —— 这是用户的真实操作，必须记录。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.SELECTED)
    assert repo.set_status(1, sel.SELECTED, reason="构图最好") is True
    row = repo.get(1)
    assert row["reason"] == "构图最好"
    assert len(repo.history(1)) == 2


def test_reason_is_stored_and_normalized(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE, reason="  光最好  ")
    assert repo.get(1)["reason"] == "光最好"


def test_blank_reason_is_not_stored_as_empty_string(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE, reason="   ")
    assert repo.get(1)["reason"] is None


def test_reason_survives_status_change_without_new_reason(db: Database) -> None:
    """升级状态时没给新理由 → **保留**原理由（COALESCE），不该被清空。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE, reason="光最好")
    repo.set_status(1, sel.SELECTED)
    assert repo.get(1)["reason"] == "光最好"


def test_unknown_status_is_rejected(db: Database) -> None:
    repo = SelectionRepo(db)
    with pytest.raises(sel.InvalidSelectionStatus):
        repo.set_status(1, "BOGUS")


def test_database_layer_also_rejects_bad_status(db: Database) -> None:
    """迁移 006 的触发器是第二道防线 —— 绕过 repo 直写也必须被拒。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO selection_event (shot_id, from_status, to_status, created_at)"
                " VALUES (1, 'UNREVIEWED', 'BOGUS', '2026-01-01T00:00:00+08:00')"
            )


# ============================================================ 随时退回

@pytest.mark.parametrize(
    "start,target",
    [
        ("FINAL", "UNREVIEWED"),
        ("FINAL", "SELECTED"),
        ("SELECTED", "CANDIDATE"),
        ("CANDIDATE", "UNREVIEWED"),
        ("FINAL", "CANDIDATE"),
    ],
)
def test_any_rollback_is_allowed(db: Database, start: str, target: str) -> None:
    """★ PRD：「允许用户随时退回」。本测试覆盖任意回退方向。"""
    repo = SelectionRepo(db)
    repo.set_status(1, start)
    repo.set_status(1, target)
    assert repo.status_of(1) == target


def test_clear_returns_to_unreviewed(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    assert repo.clear(1) is True
    assert repo.status_of(1) == sel.UNREVIEWED


# ============================================================ 过程追溯

def test_every_change_writes_exactly_one_event(db: Database) -> None:
    """★「记录选择过程」：事件条数 == 变更次数。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    repo.set_status(1, sel.SELECTED, reason="光最好")
    repo.set_status(1, sel.FINAL)
    repo.set_status(1, sel.UNREVIEWED)
    history = repo.history(1)
    assert len(history) == 4
    assert [(r["from_status"], r["to_status"]) for r in history] == [
        ("UNREVIEWED", "CANDIDATE"),
        ("CANDIDATE", "SELECTED"),
        ("SELECTED", "FINAL"),
        ("FINAL", "UNREVIEWED"),
    ]


def test_history_is_time_ordered(db: Database) -> None:
    repo = SelectionRepo(db)
    for status in (sel.CANDIDATE, sel.SELECTED, sel.FINAL):
        repo.set_status(1, status)
    stamps = [r["created_at"] for r in repo.history(1)]
    assert stamps == sorted(stamps)


def test_first_event_has_unreviewed_as_from_status(db: Database) -> None:
    """首次进入状态机时 `from_status` 记 UNREVIEWED（而不是 NULL）。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    assert repo.history(1)[0]["from_status"] == sel.UNREVIEWED


def test_abandoned_lists_rolled_back_shots(db: Database) -> None:
    """★「放弃了什么」：曾进入候选/精选、后来被退回的 Shot。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    repo.set_status(2, sel.SELECTED)
    repo.set_status(3, sel.FINAL)
    repo.set_status(1, sel.UNREVIEWED)      # 放弃 shot 1
    repo.set_status(2, sel.UNREVIEWED)      # 放弃 shot 2

    abandoned = repo.abandoned()
    assert {int(r["shot_id"]) for r in abandoned} == {1, 2}
    # shot 3 仍是 FINAL，不该出现在"被放弃"里
    assert 3 not in {int(r["shot_id"]) for r in abandoned}


def test_abandoned_logs_the_reason_for_giving_up(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    repo.set_status(1, sel.UNREVIEWED, reason="糊了")
    row = repo.abandoned()[0]
    assert row["reason"] == "糊了"


def test_abandoned_scoped_by_scene(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    repo.set_status(3, sel.CANDIDATE)
    repo.set_status(1, sel.UNREVIEWED)
    repo.set_status(3, sel.UNREVIEWED)
    assert {int(r["shot_id"]) for r in repo.abandoned(scene_id=1)} == {1}
    assert {int(r["shot_id"]) for r in repo.abandoned(scene_id=2)} == {3}


# ============================================================ 一个 Scene 可留 2~N 张

def test_scene_can_keep_multiple_finals(db: Database) -> None:
    """★ PRD：「一个 Scene 可以最终只留下 1 张，也可以留下 2~N 张」—— **不设任何上限**。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    repo.set_status(2, sel.FINAL)
    assert repo.final_count() == 2
    assert repo.counts(scene_id=1)[sel.FINAL] == 2


def test_scene_can_keep_zero_finals(db: Database) -> None:
    """也可以一张都不留（例如整段都糊了）—— 同样不该被阻止。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    assert repo.counts(scene_id=1)[sel.FINAL] == 0


# ============================================================ 计数

def test_counts_reports_all_four_buckets(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    repo.set_status(2, sel.SELECTED)
    repo.set_status(3, sel.FINAL)
    counts = repo.counts()
    assert counts == {"UNREVIEWED": 0, "CANDIDATE": 1, "SELECTED": 1, "FINAL": 1}


def test_counts_includes_unreviewed_shots_without_rows(db: Database) -> None:
    """未审阅的 Shot 在 `selection` 表里没有行，但必须被计入 —— 否则进度显示会少算。"""
    SelectionRepo(db).set_status(1, sel.CANDIDATE)
    counts = SelectionRepo(db).counts()
    assert counts["UNREVIEWED"] == 2
    assert counts["CANDIDATE"] == 1


def test_counts_scoped_by_scene(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    repo.set_status(3, sel.FINAL)
    assert repo.counts(scene_id=1)[sel.FINAL] == 1
    assert repo.counts(scene_id=2)[sel.FINAL] == 1


def test_retired_shots_are_excluded_from_counts(db: Database) -> None:
    """决策 #4：退役（重算后不再对应新分组）的 Shot 不该计入当前 Scene 的进度。"""
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 1")
    assert repo.counts(scene_id=1)[sel.FINAL] == 0
    # 但精选行本身**没有被删除**（只增不删）
    assert repo.final_count() == 1


# ============================================================ 批量

def test_set_many_marks_a_whole_group(db: Database) -> None:
    """工作台里"整组标为候选" —— 相似组是决策单元。"""
    repo = SelectionRepo(db)
    assert repo.set_many([1, 2], sel.CANDIDATE) == 2
    assert repo.status_map([1, 2]) == {1: "CANDIDATE", 2: "CANDIDATE"}


def test_set_many_returns_only_real_changes(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE)
    assert repo.set_many([1, 2], sel.CANDIDATE) == 1     # 只有 2 真变了


def test_set_many_on_empty_list(db: Database) -> None:
    assert SelectionRepo(db).set_many([], sel.CANDIDATE) == 0


# ============================================================ 精选视图查询

def test_finals_lists_final_shots_only(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    repo.set_status(2, sel.SELECTED)
    rows = repo.finals()
    assert [int(r["asset_id"]) for r in rows] == [1]


def test_finals_excludes_retired_shots(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.FINAL)
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 1")
    assert repo.finals() == []


def test_set_reason_only_touches_reason(db: Database) -> None:
    repo = SelectionRepo(db)
    repo.set_status(1, sel.SELECTED)
    assert repo.set_reason(1, "最有氛围") is True
    assert repo.status_of(1) == sel.SELECTED
    assert repo.get(1)["reason"] == "最有氛围"


# ============================================================ ★ 铁律 #1：绝不碰文件

def test_selection_never_touches_the_filesystem(db: Database, monkeypatch) -> None:
    """★ 铁律 #1 的硬断言：精选全过程**不产生任何改动文件的操作**。

    PRD §9.2：「保留所有原片；精选只是状态/关系，不产生破坏性文件操作。」
    PRD 第 137 行：「目录只是文件存储方式；Scene、Shot、精选…全部由数据库管理。」

    实现方式：把一切**改动文件**的能力（删除/移动/改名/复制/以写模式 open）
    替换成会抛异常的桩，然后跑一遍完整的精选流程。任何一次这样的调用都会让测试失败。

    刻意**不拦 `os.mkdir`/`makedirs`**：`Database.connect()` 为了容错会在打开连接前
    `mkdir(parents=True, exist_ok=True)` 数据库自己的父目录（`data/`）。
    那是连接初始化的固定开销，且只影响 PhotoOS 自己的数据目录，
    **与"是否碰了用户原片"是两件事** —— 拦它只会让断言变成对无关行为的断言。
    真正要证明的是"没有删除/移动/改名/复制/写入"，下面的桩覆盖了这些。
    """
    import io
    import os
    import shutil

    calls: list[str] = []

    def guard(name: str):
        def inner(*args, **kwargs):
            calls.append(f"{name}{args[:1]}")
            raise AssertionError(f"精选流程不该调用 {name}（铁律 #1）")

        return inner

    real_open = builtins.open

    def traced_open(file, mode="r", *args, **kwargs):
        # SQLite 自己会开数据库文件；只禁止**写**模式的 open
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            calls.append(f"open({file!r}, {mode!r})")
            raise AssertionError(f"精选流程不该以写模式打开文件（铁律 #1）：{file}")
        return real_open(file, mode, *args, **kwargs)

    # ⚠️ 必须同时拦 `builtins.open` 与 `io.open`：
    # `pathlib.Path.write_text()` 走的是 `io.open`，只补 builtins 会留下一个真实缺口
    # （反向验证时发现的 —— 故意写文件没被拦住，说明守卫当时是摆设）。
    monkeypatch.setattr(builtins, "open", traced_open)
    monkeypatch.setattr(io, "open", traced_open)
    # `Path.write_bytes` / `read_bytes` 直接走 io.open，已被上面覆盖
    for name in ("remove", "unlink", "rename", "replace", "rmdir", "removedirs"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, guard(f"os.{name}"))
    monkeypatch.setattr(shutil, "move", guard("shutil.move"))
    monkeypatch.setattr(shutil, "copy", guard("shutil.copy"))
    monkeypatch.setattr(shutil, "copy2", guard("shutil.copy2"))
    monkeypatch.setattr(shutil, "copyfile", guard("shutil.copyfile"))
    monkeypatch.setattr(shutil, "rmtree", guard("shutil.rmtree"))

    repo = SelectionRepo(db)
    repo.set_status(1, sel.CANDIDATE, reason="光最好")
    repo.set_status(1, sel.SELECTED)
    repo.set_status(1, sel.FINAL, reason="构图最好")
    repo.set_many([2, 3], sel.CANDIDATE)
    repo.clear(2)
    repo.set_reason(1, "最有氛围")
    repo.set_status(3, sel.FINAL)
    repo.history(1)
    repo.abandoned()
    repo.counts()
    repo.finals()
    repo.final_count()

    assert calls == [], f"精选流程产生了文件改动操作：{calls}"
    assert repo.final_count() == 2
