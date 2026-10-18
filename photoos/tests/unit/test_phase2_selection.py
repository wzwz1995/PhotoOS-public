"""Phase 2 · M0：精选状态机的纯逻辑测试（PRD §9.2）。

这些测试全部**不碰数据库**：状态机是纯函数，落库在 `repos.SelectionRepo`，
两者的边界要守住，否则"随时可退回"这种规则会被 SQL 细节淹没。
"""

from __future__ import annotations

import pytest

from photoos.library import selection as sel

# ============================================================ 状态集合

def test_four_statuses_match_prd_exactly() -> None:
    """PRD §9.2 的核心概念：UNREVIEWED/CANDIDATE/SELECTED/FINAL 四态，一个不多一个不少。"""
    assert sel.STATUSES == ("UNREVIEWED", "CANDIDATE", "SELECTED", "FINAL")


def test_reviewed_statuses_exclude_unreviewed() -> None:
    assert set(sel.REVIEWED_STATUSES) == {"CANDIDATE", "SELECTED", "FINAL"}
    assert "UNREVIEWED" not in sel.REVIEWED_STATUSES


def test_every_status_has_a_chinese_label() -> None:
    assert set(sel.STATUS_LABELS) == set(sel.STATUSES)
    assert sel.STATUS_LABELS[sel.CANDIDATE] == "候选"
    assert sel.STATUS_LABELS[sel.FINAL] == "最终精选"


# ============================================================ normalize_status

def test_none_and_blank_mean_unreviewed() -> None:
    """`selection` 表没有行 = 从未审阅（LEFT JOIN 拿到 NULL），归一化比报错更贴合调用方式。"""
    assert sel.normalize_status(None) == sel.UNREVIEWED
    assert sel.normalize_status("") == sel.UNREVIEWED
    assert sel.normalize_status("   ") == sel.UNREVIEWED


def test_status_is_case_and_whitespace_insensitive() -> None:
    assert sel.normalize_status(" final ") == sel.FINAL
    assert sel.normalize_status("candidate") == sel.CANDIDATE


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(sel.InvalidSelectionStatus):
        sel.normalize_status("BOGUS")
    with pytest.raises(sel.InvalidSelectionStatus):
        sel.normalize_status("SELECTED_FINAL")


def test_status_label_uses_prd_wording() -> None:
    assert sel.status_label(None) == "未审阅"
    assert sel.status_label("SELECTED") == "精选"


# ============================================================ normalize_reason

def test_reason_is_trimmed_and_blank_becomes_none() -> None:
    assert sel.normalize_reason("  光最好 ") == "光最好"
    assert sel.normalize_reason("") is None
    assert sel.normalize_reason("   ") is None
    assert sel.normalize_reason(None) is None


def test_overlong_reason_is_truncated_not_rejected() -> None:
    """理由只是备注，不该因为多打几个字就丢掉整次操作。"""
    long = "好" * (sel.MAX_REASON_LENGTH + 50)
    out = sel.normalize_reason(long)
    assert out is not None
    assert len(out) == sel.MAX_REASON_LENGTH


def test_reason_supports_prd_examples() -> None:
    for example in ("光最好", "构图最好", "最有氛围"):
        assert sel.normalize_reason(example) == example


# ============================================================ plan_transition

def test_transition_forward() -> None:
    t = sel.plan_transition(7, sel.UNREVIEWED, sel.CANDIDATE)
    assert (t.shot_id, t.from_status, t.to_status) == (7, "UNREVIEWED", "CANDIDATE")
    assert t.changed is True


def test_transition_from_missing_row_is_unreviewed() -> None:
    """该 Shot 还没有 selection 行时，from_status 应为 UNREVIEWED（而不是 NULL）。"""
    t = sel.plan_transition(3, None, sel.FINAL)
    assert t.from_status == sel.UNREVIEWED
    assert t.to_status == sel.FINAL


@pytest.mark.parametrize(
    "start,target",
    [
        ("UNREVIEWED", "FINAL"),   # 跳过中间态
        ("FINAL", "UNREVIEWED"),   # 退回起点
        ("FINAL", "SELECTED"),     # 退一级
        ("SELECTED", "CANDIDATE"),
        ("CANDIDATE", "SELECTED"),
        ("SELECTED", "FINAL"),
        ("FINAL", "CANDIDATE"),
    ],
)
def test_any_transition_is_allowed(start: str, target: str) -> None:
    """PRD §9.2：**允许用户随时退回**，因此不设转移白名单。

    这条测试故意覆盖"任意跳转"——如果将来有人加了转移限制，这里会立刻失败，
    从而迫使那次修改显式讨论（PRD 只要求"可退回"，没要求"只能相邻退回"）。
    """
    t = sel.plan_transition(1, start, target)
    assert (t.from_status, t.to_status) == (start, target)
    assert t.changed is True


def test_repeated_same_status_without_reason_is_a_noop() -> None:
    """连按同一个键不应刷出满屏事件。"""
    t = sel.plan_transition(1, sel.SELECTED, sel.SELECTED)
    assert t.changed is False


def test_same_status_with_new_reason_still_counts_as_an_action() -> None:
    """状态没变但补了理由 —— 这是用户的真实操作，必须记录。"""
    t = sel.plan_transition(1, sel.SELECTED, sel.SELECTED, reason="构图最好")
    assert t.changed is True
    assert t.reason == "构图最好"


def test_transition_carries_reason() -> None:
    t = sel.plan_transition(9, sel.CANDIDATE, sel.FINAL, reason="  光最好  ")
    assert t.reason == "光最好"


def test_transition_rejects_bad_target() -> None:
    with pytest.raises(sel.InvalidSelectionStatus):
        sel.plan_transition(1, sel.UNREVIEWED, "NOPE")


# ============================================================ is_reviewed

def test_is_reviewed() -> None:
    assert sel.is_reviewed(None) is False
    assert sel.is_reviewed("UNREVIEWED") is False
    assert sel.is_reviewed("CANDIDATE") is True
    assert sel.is_reviewed("SELECTED") is True
    assert sel.is_reviewed("FINAL") is True


# ============================================================ summarize

def test_summarize_reports_all_four_buckets_in_order() -> None:
    text = sel.summarize({"UNREVIEWED": 40, "CANDIDATE": 7, "SELECTED": 3, "FINAL": 2})
    assert text == "未审阅 40，候选 7，精选 3，最终精选 2"


def test_summarize_tolerates_missing_keys() -> None:
    assert sel.summarize({}) == "未审阅 0，候选 0，精选 0，最终精选 0"
