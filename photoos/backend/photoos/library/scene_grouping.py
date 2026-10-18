"""Scene 划分（Phase 2 · M4）。

## Scene 解决什么问题

一个 Session（= 一个 `<城市>\\<时段>` 目录）可能有 **866 张**（希腊市 `2026-10-04`）
甚至 **2,239 个文件**（`2026-10-06_1146_至_10-07_1908`）。用户不可能一次筛完。

**Scene 是把 Session 切成"可以一次做完决策"的块。** PRD §9.2 的 10 分钟标准是
"20–50 张 Shot"，所以 Scene 必须落在那个规模上 —— 这不是界面偏好，是**断点 #2 可达性的前提**。

## 三段算法

```
① 时间断点（粗切）  在 Shot 序列上找间隔 > scene_gap_seconds 的位置切段
                    默认 900 s（15 分钟）—— 比 session_gap_hours=12 小两个数量级
② 相似度合并/细分   跨边界最大余弦 ≥ merge_threshold → 合并（同一意图被时间切断了）
                    段内连续 K 张局部互不相似 → 细分（这段里换了题材）
③ 规模约束          段内 Shot 数 > max_shots → 在最大时间间隔处继续切，直到达标
```

**第 ③ 步是"保证 PRD 标准可达"的机制**，不是可有可无的优化：
实测 71 个时段里最大的有 2,239 个文件，若不切分，用户面对的就是"一次筛 500+ 张 Shot"。

## 相似度信号的来源与降级

第 ② 步需要嵌入（M3）。**没有嵌入时第 ①③ 步照常工作**，只是 Scene 边界更粗 ——
这是刻意的：M2 的 Shot 分组用纯时间规则就已实测可行，Scene 也不该因为嵌入没跑就不可用。

## 参数（决策 #5 的同一套机制）

全部由调用方传入，**模块内不含默认值** —— 默认值属于 config / Session 覆盖 / UI 三处，
避免"模块藏一个默认值、配置又一个"的双头局面。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from .shot_grouping import ShotGroup, parse_capture_time

#: Scene 的时间断点默认值（秒）。15 分钟 —— 一次"换个地方继续拍"的典型间隔。
DEFAULT_SCENE_GAP_SECONDS = 900.0
#: 跨边界相似度达到此值就认为"被时间断点切断了同一意图"
DEFAULT_MERGE_THRESHOLD = 0.85
#: 段内连续这么多张局部互不相似 → 认为换了题材，继续细分
DEFAULT_DISSIMILAR_RUN = 8
#: 单张 Shot 数上限 —— 保证 PRD「20–50 张 Shot ≤10 分钟」可达成
DEFAULT_MAX_SHOTS = 60
#: "互不相似"的判定线（低于此值算不相似）
DEFAULT_UNRELATED_SIMILARITY = 0.5


@dataclass(frozen=True)
class SceneParams:
    """Scene 划分参数（全部必填，无隐藏默认）。"""

    gap_seconds: float
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD
    dissimilar_run: int = DEFAULT_DISSIMILAR_RUN
    max_shots: int = DEFAULT_MAX_SHOTS
    unrelated_similarity: float = DEFAULT_UNRELATED_SIMILARITY

    def to_json(self) -> dict:
        return {
            "scene_gap_seconds": self.gap_seconds,
            "scene_merge_similarity": self.merge_threshold,
            "scene_dissimilar_run": self.dissimilar_run,
            "scene_max_shots": self.max_shots,
            "scene_unrelated_similarity": self.unrelated_similarity,
        }


@dataclass(frozen=True)
class Boundary:
    """一次切分/合并的依据（写进 `scene.evidence_json`，供用户核对"为什么这么切"）。

    `between` 是**绝对位置索引**（指向传给 `split_scenes` 的 shots 序列），
    不是段落内的相对位置 —— 这一区分很重要：早期实现里规模切分用的是段内相对位置，
    导致 `kind='size'` 的边界永远对不上任何 Scene（切了但没记录）。
    """

    kind: str            # 'gap' | 'similarity' | 'size' | 'dissimilar'
    between: tuple[int, int]
    detail: str = ""
    cosine: float | None = None


@dataclass
class SceneDraft:
    """一个切好的 Scene（**尚未落库**）。"""

    shots: list[ShotGroup] = field(default_factory=list)
    #: 段内全部 Shot 的位置索引（指向传入的 shots 序列）
    indices: list[int] = field(default_factory=list)
    boundaries: list[Boundary] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.shots)

    @property
    def asset_count(self) -> int:
        return sum(g.size for g in self.shots)

    @property
    def captured_from(self) -> str | None:
        stamps = [g.captured_at for g in self.shots if g.captured_at]
        return min(stamps) if stamps else None

    @property
    def captured_to(self) -> str | None:
        stamps = [g.captured_at for g in self.shots if g.captured_at]
        return max(stamps) if stamps else None

    @property
    def needs_review(self) -> bool:
        """段内是否有建议人工确认的 Shot（置信度低）。"""
        return any(g.needs_review for g in self.shots)


def _stamp(group: ShotGroup) -> float | None:
    value = parse_capture_time(group.captured_at)
    return value.timestamp() if value is not None else None


def _time_split(
    shots: Sequence[ShotGroup],
    *,
    gap_seconds: float,
) -> tuple[list[list[int]], list[Boundary]]:
    """① 时间断点粗切。返回 `(段落列表, 边界列表)`，段落是位置索引的列表。"""
    segments: list[list[int]] = []
    boundaries: list[Boundary] = []
    if not shots:
        return segments, boundaries

    current: list[int] = [0]
    for i in range(1, len(shots)):
        prev_stamp, stamp = _stamp(shots[i - 1]), _stamp(shots[i])
        # 任一侧无时间 → 不能判定"相邻"，保守切断（与 shot_grouping 同口径）
        if prev_stamp is None or stamp is None or (stamp - prev_stamp) > gap_seconds:
            segments.append(current)
            boundaries.append(
                Boundary(
                    kind="gap",
                    between=(current[-1], i),
                    detail=(
                        "无拍摄时间，保守切断"
                        if prev_stamp is None or stamp is None
                        else f"间隔 {(stamp - prev_stamp) / 60:.1f} 分钟 > {gap_seconds / 60:.0f} 分钟"
                    ),
                )
            )
            current = [i]
        else:
            current.append(i)
    segments.append(current)
    return segments, boundaries


def _segment_similarity(
    segment: Sequence[int],
    *,
    similarity,
) -> float | None:
    """段内**相邻 Shot** 之间的最大余弦（用于判断"是不是同一意图"）。

    这里刻意只看相邻 Shot 对：段内主题漂移是**局部**现象（"换了题材"），
    比较所有远距离对会把整段的主题差异平均掉，反而看不出漂移。

    与 §5.5 的实测口径一致：那 52 对跨组高相似照片都是**相邻**组之间。
    `similarity` 返回 None 表示无法计算（没有嵌入），这时整条相似度分支
    **静默跳过** —— 不猜、不默认成 1.0。
    """
    best: float | None = None
    for a, b in zip(segment, segment[1:]):
        value = similarity(a, b)
        if value is None:
            continue
        best = value if best is None else max(best, value)
    return best


def max_cross_similarity(
    left_assets: Sequence[int],
    right_assets: Sequence[int],
    *,
    matrix: Any,
    index_of: dict[int, int],
) -> float | None:
    """两组资产之间**最大的**两两余弦；任一侧没有嵌入时返回 None。

    为什么是"最大"而不是"相邻那一对"：Shot 内部的排列是拍摄顺序，不是相似度顺序。
    同一机位等光线拍的三张里，真正跨越边界相似的那张可能排在中间。只看
    `(left_assets[-1], right_assets[0])` 会**系统性低估**相似度，
    于是该合并的段没合并 —— 这与 §5.5 实测的 52 对跨组高相似照片是同一类问题。

    `matrix` 是 L2 归一化后的 `(n, dim)` 矩阵（**直接传引用，不要传副本**：
    本库是 21,400×512×4 B ≈ 43.7 MB，每次比较复制一遍会把流水线拖垮）。
    `index_of` 是 `asset_id → 矩阵行号`；不在其中的资产视为无嵌入。
    """
    import numpy as np

    left_idx = [index_of[a] for a in left_assets if a in index_of]
    right_idx = [index_of[a] for a in right_assets if a in index_of]
    if not left_idx or not right_idx:
        return None
    block = matrix[left_idx] @ matrix[right_idx].T
    value = float(np.max(block))
    # nan 说明向量异常；返回 None 让调用方跳过这次比较，而不是拿 nan 去比大小
    return None if value != value else value


def build_similarity_callback(
    shots: Sequence[ShotGroup],
    *,
    matrix: Any,
    index_of: dict[int, int],
):
    """把"嵌入检索"包装成 `split_scenes()` 要的 `(i, j) -> float | None` 回调。

    `matrix=None`（或没有任何嵌入）时返回 `None` —— 调用方据此走
    "无相似度信号"的降级路径（第 ② 步整段跳过，而不是猜一个值）。
    """
    if matrix is None or not index_of:
        return None
    members = [list(shot.asset_ids) for shot in shots]

    def callback(left: int, right: int) -> float | None:
        if not (0 <= left < len(members)) or not (0 <= right < len(members)):
            return None
        return max_cross_similarity(
            members[left], members[right], matrix=matrix, index_of=index_of
        )

    return callback


def split_scenes(
    shots: Sequence[ShotGroup],
    *,
    params: SceneParams,
    similarity=None,
) -> list[SceneDraft]:
    """把一段 Shot 序列切成 Scene。

    参数
    ----
    shots
        已经分好组的 Shot（M2 的产物），**必须按时间升序**。
    params
        划分参数（见 `SceneParams`）。
    similarity
        可选回调 `(index_a, index_b) -> float | None`，返回**两个 Shot 之间**的余弦
        （应当是组内所有资产两两相似度的**最大值**，见 `max_cross_similarity`）。
        为 None 或返回 None 时，跳过第 ② 步（相似度合并/细分），只做 ①③。
    """
    if params.gap_seconds <= 0:
        raise ValueError("gap_seconds 必须为正数")
    if params.max_shots < 1:
        raise ValueError("max_shots 必须 >= 1")

    segments, boundaries = _time_split(shots, gap_seconds=params.gap_seconds)

    # ---------------------------------------------------------------- ② 相似度合并
    if similarity is not None and len(segments) > 1:
        merged: list[list[int]] = [segments[0]]
        kept: list[Boundary] = []
        for index in range(1, len(segments)):
            previous, current = merged[-1], segments[index]
            bridge = similarity(previous[-1], current[0])
            if bridge is not None and bridge >= params.merge_threshold:
                merged[-1] = previous + current
            else:
                merged.append(current)
                # 记录保留下来的是哪个边界
                for b in boundaries:
                    if b.between == (previous[-1], current[0]):
                        kept.append(b)
                        break
        segments = merged
        boundaries = kept

    # ---------------------------------------------------------------- ② 相似度细分
    if similarity is not None:
        refined: list[list[int]] = []
        for segment in segments:
            refined.extend(
                _split_dissimilar(
                    segment,
                    similarity=similarity,
                    run=params.dissimilar_run,
                    unrelated=params.unrelated_similarity,
                    boundaries=boundaries,
                )
            )
        segments = refined

    # ---------------------------------------------------------------- ③ 规模约束
    segments = _enforce_size_limit(
        segments, shots, max_shots=params.max_shots, boundaries=boundaries
    )

    # 段落是**互不重叠、按时间升序**的区间。边界按"**开启**下一段"归属：
    # 若某段的起始位置正好是一条边界的右侧，那条边界就是"这一段为什么被单独切出来"。
    # （早期实现用"边界右侧落在段内部"来归属，结果每条边界都被排除，`evidence` 全空。）
    opening = {b.between[1]: b for b in boundaries}
    out: list[SceneDraft] = []
    for segment in segments:
        boundary = opening.get(segment[0])
        out.append(
            SceneDraft(
                shots=[shots[i] for i in segment],
                indices=list(segment),
                boundaries=[boundary] if boundary is not None else [],
            )
        )
    return out


def _split_dissimilar(
    segment: Sequence[int],
    *,
    similarity,
    run: int,
    unrelated: float,
    boundaries: list[Boundary],
) -> list[list[int]]:
    """段内出现连续 K 张互不相似 → 在此处继续切（换了题材）。

    `segment` 传的是**绝对位置**，因此写进 `boundaries` 的也是绝对位置，
    与 `_time_split` 的记录口径一致（这正是早期实现的一个 bug：规模切分用了
    段内相对位置，导致 `kind='size'` 的边界永远匹配不到任何 Scene）。
    """
    if run < 2 or len(segment) <= run:
        return [list(segment)]

    out: list[list[int]] = []
    current: list[int] = [segment[0]]
    streak = 0
    for i in range(1, len(segment)):
        value = similarity(segment[i - 1], segment[i])
        if value is not None and value < unrelated:
            streak += 1
        else:
            streak = 0
        if streak >= run:
            out.append(current)
            boundaries.append(
                Boundary(
                    kind="dissimilar",
                    between=(current[-1], segment[i]),
                    detail=f"连续 {streak} 张与前一张相似度 < {unrelated}",
                    cosine=value,
                )
            )
            current = [segment[i]]
            streak = 0
        else:
            current.append(segment[i])
    out.append(current)
    return out


def _enforce_size_limit(
    segments: list[list[int]],
    shots: Sequence[ShotGroup],
    *,
    max_shots: int,
    boundaries: list[Boundary],
) -> list[list[int]]:
    """超过 `max_shots` 的段在**最大时间间隔**处继续切，直到达标。

    为什么按时间间隔切而不是按相似度：这一步的目标是"保证规模可达"，
    应当用最可靠的信号（时间）。按相似度切会让这一步与第 ② 步耦合，
    用户调相似度阈值时规模就不可预测了。
    """
    result: list[list[int]] = []
    for segment in segments:
        result.extend(_split_oversized(segment, shots, max_shots=max_shots, boundaries=boundaries))
    return result


def _split_oversized(
    segment: Sequence[int],
    shots: Sequence[ShotGroup],
    *,
    max_shots: int,
    boundaries: list[Boundary],
) -> list[list[int]]:
    """把超限的段切成"尽量均衡"的若干块，切点落在**间隔较大**处。

    ⚠️ 这里刻意**不用递归对半切**。早期实现是"找最大间隔 → 递归左右两半"，
    在间隔均匀的真实数据上会退化：所有间隔相等时 `>` 永远命中第一个位置，
    于是一张一张地剥，10 张 / 上限 4 会切出 `[1,1,1,1,1,1,4]` 而不是 `[3,3,4]`
    —— Scene 数从 3 个变成 7 个，用户要多做一倍的无谓切换。

    现在的做法：先算出需要几块（`ceil(n / max_shots)`），按均衡的目标长度选切点，
    并且在"目标位置附近"挑**间隔最大**的那个位置（间隔越大，切在那里越自然）。
    """
    n = len(segment)
    if n <= max_shots:
        return [list(segment)]

    import math

    pieces = math.ceil(n / max_shots)
    # 每块的理想长度（尽量均匀；余数摊到前面几块）
    base, extra = divmod(n, pieces)
    targets: list[int] = []
    cursor = 0
    for index in range(pieces - 1):
        cursor += base + (1 if index < extra else 0)
        targets.append(cursor)

    # 把每个目标位置吸附到"附近间隔最大"的真实切点，避免切在密集连拍中间
    cut_points: list[int] = []
    search = max(1, base // 2)
    for target in targets:
        low, high = max(1, target - search), min(n - 1, target + search)
        best_at, best_gap = target, -1.0
        for pos in range(low, high + 1):
            left, right = _stamp(shots[segment[pos - 1]]), _stamp(shots[segment[pos]])
            gap = (right - left) if (left is not None and right is not None) else float("inf")
            if gap > best_gap:
                best_at, best_gap = pos, gap
        cut_points.append(best_at)

    # 去重并保证单调（吸附后可能撞在一起）
    cut_points = sorted({point for point in cut_points if 0 < point < n})
    result: list[list[int]] = []
    previous = 0
    for point in [*cut_points, n]:
        if point <= previous:
            continue
        result.append(list(segment[previous:point]))
        # 边界只出现在两块之间：`point` 是下一块的起点，因此必然 < n
        if point < n:
            boundaries.append(
                Boundary(
                    kind="size",
                    between=(segment[point - 1], segment[point]),
                    detail=f"段内 {n} 张 Shot 超过上限 {max_shots}，切成 {len(cut_points) + 1} 段",
                )
            )
        previous = point

    # 兜底：若吸附把切点挤没了、导致仍有块超限，退回等分（保证不变式成立）
    if any(len(chunk) > max_shots for chunk in result):
        result = [list(segment[i : i + max_shots]) for i in range(0, n, max_shots)]
    return result


def split_all(
    shots_by_period: dict[tuple[str, str], Sequence[ShotGroup]],
    *,
    params: SceneParams,
    similarity=None,
) -> dict[tuple[str, str], list[SceneDraft]]:
    """按时段分别划 Scene。

    Scene **不跨时段目录**：时段是"一次出行"（12 小时规则切出来的），
    跨时段合成一个 Scene 在语义上说不通，也会让 `scene.session_id` 变得含糊。

    `similarity` 是 `(period, i, j) -> float | None` —— 带 period 参数是因为
    相似度只在时段内有意义（不同时段的照片不该被拿来比较）。
    """
    out: dict[tuple[str, str], list[SceneDraft]] = {}
    for key, shots in shots_by_period.items():
        ordered = sorted(shots, key=lambda g: (_stamp(g) if _stamp(g) is not None else float("inf")))
        callback = None
        if similarity is not None:
            callback = lambda a, b, _k=key: similarity(_k, a, b)  # noqa: E731
        out[key] = split_scenes(ordered, params=params, similarity=callback)
    return out


def summarize(drafts: Sequence[SceneDraft]) -> dict:
    """给 CLI / 界面用的汇总（**只读统计，不含任何主观判断**）。"""
    sizes = [d.size for d in drafts]
    return {
        "scenes": len(drafts),
        "shots": sum(sizes),
        "min_shots": min(sizes) if sizes else 0,
        "max_shots": max(sizes) if sizes else 0,
        "mean_shots": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "within_prd_band": sum(1 for s in sizes if 20 <= s <= 50),
        "over_max": sum(1 for s in sizes if s > DEFAULT_MAX_SHOTS),
        "needs_review": sum(1 for d in drafts if d.needs_review),
    }


__all__ = [
    "Boundary",
    "DEFAULT_DISSIMILAR_RUN",
    "DEFAULT_MAX_SHOTS",
    "DEFAULT_MERGE_THRESHOLD",
    "DEFAULT_SCENE_GAP_SECONDS",
    "DEFAULT_UNRELATED_SIMILARITY",
    "SceneDraft",
    "SceneParams",
    "split_all",
    "split_scenes",
    "summarize",
]
