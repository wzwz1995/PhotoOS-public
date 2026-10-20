"""精选状态机（PRD §9.2 核心概念）。

```
UNREVIEWED ──> CANDIDATE ──> SELECTED ──> FINAL
     ▲              ▲            ▲           │
     └──────────────┴────────────┴───────────┘     随时可退回
```

设计要点（与 PHASE2_TECH_PLAN §7 一致）：

  * **四态是"标签"不是"流程"**：PRD 要求"允许用户随时退回"，因此这里
    **不设转移白名单** —— 任意状态可以跳到任意状态，包括直接 UNREVIEWED。
    唯一被约束的是"状态值必须是这四个之一"，由迁移 006 的触发器在数据库层兜底。
  * **每一次变更都要留痕**：写入 `selection_event`（Phase 1 就建好的表），
    这样"记录选择过程：用户选了什么、放弃了什么、最终留下什么"（PRD §9.2）才有依据。
  * **`selection` 表只保存"当前状态"**，历史在 `selection_event` 里 —— 两者都不含
    任何文件路径（铁律 #2），也不触发任何文件操作（铁律 #1）。
  * **本模块是纯逻辑**：没有 IO、没有数据库访问，因此可以完全单测。
    持久化在 `repos.SelectionRepo`。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 精选状态（与迁移 001 的 CHECK 约束、006 的触发器**必须逐字一致**）
UNREVIEWED = "UNREVIEWED"
CANDIDATE = "CANDIDATE"
SELECTED = "SELECTED"
FINAL = "FINAL"

#: 有序列出四态 —— 顺序即 PRD 的"三级 + 未审阅初始态"，用于界面展示与排序。
STATUSES: tuple[str, ...] = (UNREVIEWED, CANDIDATE, SELECTED, FINAL)

#: 面向用户的中文名（PRD 原文用词：候选、精选、最终精选）
STATUS_LABELS: dict[str, str] = {
    UNREVIEWED: "未审阅",
    CANDIDATE: "候选",
    SELECTED: "精选",
    FINAL: "最终精选",
}

#: 界面上"已进入筛选流程"的状态（用于统计与"被放弃了什么"的反查）
REVIEWED_STATUSES: tuple[str, ...] = (CANDIDATE, SELECTED, FINAL)

#: 理由的长度上限。PRD 说"简短理由（例如'光最好''构图最好'）"，
#: 因此不设长文本能力；上限只用于防止异常输入撑爆界面，不是产品约束。
MAX_REASON_LENGTH = 200


class InvalidSelectionStatus(ValueError):
    """状态不是四态之一。"""


def normalize_status(value: str | None) -> str:
    """把可空/大小写不一的状态规范成四态之一；None / 空串视为 UNREVIEWED。

    为什么把 None 当 UNREVIEWED 而不是报错：`selection` 表里**没有行**就代表
    "从未审阅"（Phase 1 建表时就是这个语义，见 001_core.sql 的默认值），
    因此上层从 `LEFT JOIN` 拿到的必然是 NULL，归一化比报错更符合实际调用方式。
    """
    if value is None:
        return UNREVIEWED
    text = str(value).strip().upper()
    if not text:
        return UNREVIEWED
    if text not in STATUSES:
        raise InvalidSelectionStatus(
            f"未知的精选状态：{value!r}（只允许 {'/'.join(STATUSES)}）"
        )
    return text


def status_label(value: str | None) -> str:
    """状态的中文名（供 CLI 与界面复用，避免各处自己维护一份映射）。"""
    return STATUS_LABELS[normalize_status(value)]


def normalize_reason(value: str | None) -> str | None:
    """规范化理由：去首尾空白；空串归为 None（不写空字符串进库）。

    超长时**截断而不是报错** —— 理由只是备注，不该因为多打几个字就丢失整次操作。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_REASON_LENGTH:
        return text[:MAX_REASON_LENGTH]
    return text


@dataclass(frozen=True)
class Transition:
    """一次状态变更的意图（尚未落库）。"""

    shot_id: int
    from_status: str
    to_status: str
    reason: str | None = None

    @property
    def changed(self) -> bool:
        """状态是否真的变了。

        **状态相同、理由也相同 → 无需写库**（避免连按同一个键刷出满屏事件）；
        状态相同但理由变了 → 仍需记录（用户是在补充/修改理由，属于一次真实操作）。
        """
        return self.from_status != self.to_status or self.reason is not None


def plan_transition(
    shot_id: int,
    current_status: str | None,
    target_status: str,
    reason: str | None = None,
) -> Transition:
    """算出一次状态变更（纯函数，可单测）。

    `current_status=None` 表示该 Shot 还没有 `selection` 行（= 从未审阅）。
    """
    return Transition(
        shot_id=int(shot_id),
        from_status=normalize_status(current_status),
        to_status=normalize_status(target_status),
        reason=normalize_reason(reason),
    )


def is_reviewed(status: str | None) -> bool:
    """是否已经"审阅过"（进入过候选及以上）。

    用途：工作台按 `Space` 跳"下一张未审阅的"；以及 Scene 进度"还差几张"。
    """
    return normalize_status(status) in REVIEWED_STATUSES


def summarize(counts: dict[str, int]) -> str:
    """把"各状态各多少张"汇总成一行可读文本（CLI / 界面共用）。"""
    parts = [f"{STATUS_LABELS[s]} {int(counts.get(s, 0))}" for s in STATUSES]
    return "，".join(parts)


__all__ = [
    "CANDIDATE",
    "FINAL",
    "InvalidSelectionStatus",
    "MAX_REASON_LENGTH",
    "REVIEWED_STATUSES",
    "SELECTED",
    "STATUSES",
    "STATUS_LABELS",
    "Transition",
    "UNREVIEWED",
    "is_reviewed",
    "normalize_reason",
    "normalize_status",
    "plan_transition",
    "status_label",
    "summarize",
]
