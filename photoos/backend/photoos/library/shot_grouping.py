"""Shot 分组（Phase 2 · M2）。

## Shot 是什么

> PRD §9.2：Shot = **场景中的一次拍摄**（可含多个 Asset），**精选以 Shot 为单位**。

因此 Shot 不是一个"文件"，而是"一次按快门"。RAW+JPEG 成对存在时，它们是**同一个 Shot 的两个 Asset**
（本库实测：7,997 个 Shot 里 5,351 个是 `jpeg+raw` 组合，平均 2.67 个 Asset/Shot）。

## 为什么只有"时间 + subsec"参与切分（实测依据）

调研阶段在真实库上逐项核对过（`PHASE2_RESEARCH.md` §9.2，我说过要复核的数字**全部复现**）：

| 信号 | 连拍组内是否变化 | 结论 |
| --- | --- | --- |
| 镜头型号 | **0/15、0/21** | 用它切分**在任何真实数据上都不产生效果**（死代码） |
| 焦段 | **0/15、0/21** | 同上 |
| 文件名序号严格连续 | **15/15、21/21** | 同上 |
| **曝光（光圈/快门/ISO）** | **4/15、1/21** | 连拍中途自动曝光会变 → **绝不能当硬约束** |

于是这三项**改为置信度与人类可读解释**：它们一旦变化，就是"这**不是**连拍"的**强反证**，
应该**暴露给用户**而不是被算法偷偷用掉。这与 PRD §9.2「所有自动归组都允许用户调整」以及
铁律 #3「分组只是可重算的派生线索」一致。

## `subsec` 必须参与排序

本机是 Nikon Z 5，`subsec` 100% 有值（实测 302/302、322/322，**位数恒为 2**，即百分秒）。
实测万宁市那个时段有 **46 对同秒相邻** —— 只按 `capture_at`（秒精度）排序，
这 46 对的先后顺序是不确定的，分组边界会随机漂移。

## 阈值必须可调（决策 #5）

实测同一时段，阈值 1 s→12 s 的组数是 **218 → 101**（万宁）、**234 → 86**（杭州），差 2 倍以上。
因此本模块**不含任何默认值**：`gap_seconds` 一律由调用方传入，
默认值只在 config / Session 覆盖 / UI 三处定义（见 `PHASE2_TECH_PLAN` §4.3），
避免"模块里藏一个默认值、配置里又一个默认值"的双头局面。

## 本模块不碰数据库、不碰文件

纯函数进纯函数出。落库与"只增不删"的重算策略在 `repos`/`jobs` 层（M2 后半 + M5）。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- 置信度

#: 时间紧邻 + 镜头同 + 焦段同 + 序号连续（几乎确定是连拍）
CONFIDENCE_STRONG = 0.95
#: 时间紧邻 + 镜头同 + 焦段同，但序号不连续（如 `DSC_9005__2` 的撞名后缀）
CONFIDENCE_LIKELY = 0.85
#: 时间紧邻 + 曝光变过 —— 连拍中途自动曝光变化属正常现象，不是问题
CONFIDENCE_EXPOSURE_VARIED = 0.80
#: 时间紧邻，但**镜头或焦段变了** → 大概率不是连拍，界面应高亮"建议人工确认"
CONFIDENCE_SUSPECT = 0.40

#: 低于此值视为"建议人工确认"
CONFIDENCE_REVIEW_FLOOR = 0.5

#: 状态机允许的值（与迁移 001 的 CHECK 一致）
GROUP_SOURCE_AUTO = "auto"
GROUP_SOURCE_USER = "user"

#: 界面上可选的 `shot_gap_seconds` 预设（决策 #5 的"UI 预设"那一层）。
#:
#: 每个值都由**实测**锚定，而不是拍脑袋定的 —— 实测依据见 `PHASE2_RESEARCH.md`：
#:   * 本库相邻拍摄间隔**中位数 3.150 s**（n=301，§R2/§8 对账表）
#:   * 2.0 s 阈值在万宁 302 张上切出 176 组，其中只有 15 组 ≥3 张
#:   * **时间规则永远看不见"构图高度相似"**：实测有一对相隔 **360.5 s** 却余弦
#:     **0.9846** 的照片（§5.5），调大阈值到 360 s 会把整段旅程并成一组，不可用
#:
#: 所以预设是"用多少时间跨度当一个决策单元"的取舍，不是"越大越好"。界面必须
#: 把 `note` 一并显示出来，否则用户会以为调大阈值就能把相似照片找全。
GAP_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "value": 1.0,
        "label": "1 秒 · 只并真正的连拍",
        "note": "只把高速连拍并成一组；同一机位等光线拍的几张仍会分开。",
    },
    {
        "value": 2.0,
        "label": "2 秒 · 偏紧（调研基准）",
        "note": "调研里用来衡量相似度的基准阈值；实测会漏掉间隔 5.1 s 但余弦 0.986 的那类竞争照片。",
    },
    {
        "value": 3.0,
        "label": "3 秒 · 贴近本库中位数（推荐）",
        "note": "本库相邻间隔实测中位数 3.150 s，因此这个值把「约一半的相邻对」并进同组。",
    },
    {
        "value": 10.0,
        "label": "10 秒 · 偏松",
        "note": "同一机位反复取景基本能并进来；代价是会把「本来是两次独立拍摄」也合成一组。",
    },
)


def gap_presets() -> list[dict[str, Any]]:
    """给界面用的预设清单（深拷贝，调用方改不坏模块级常量）。"""
    return [dict(item) for item in GAP_PRESETS]


def normalize_gap(value: Any) -> float:
    """校验并归一化 `shot_gap_seconds`。

    刻意**不设上限**（用户可以有自己的理由），但必须为正数：0 或负数会让
    `group_shots()` 里的比较永远成立，把整个时段的照片切成一张一组。
    """
    try:
        gap = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"shot_gap_seconds 必须是数字，收到 {value!r}") from exc
    if not (gap > 0) or gap != gap or gap == float("inf"):
        raise ValueError(f"shot_gap_seconds 必须是有限正数，收到 {value!r}")
    return gap


#: 主 Asset 的类型优先级（PHASE1_TECH_PLAN §4.2 决策 4：RAW > HEIC > TIFF > JPEG > PNG > 视频）
_PRIMARY_PRIORITY: dict[str, int] = {
    "raw": 0,
    "heic": 1,
    "tiff": 2,
    "jpeg": 3,
    "png": 4,
    "webp": 5,
    "other": 6,
    "video": 7,
}


def parse_subsecond(text: str | None) -> float:
    """把 `subsec` 解析成秒的小数部分。

    Nikon 写的是**百分秒**（实测位数恒为 2，如 `'07'` → 0.07 s）。
    按位数换算而不是固定除以 100 —— 若将来换机身写了毫秒（3 位），也不会算错。
    """
    if text is None:
        return 0.0
    value = str(text).strip()
    if not value or not value.isdigit():
        return 0.0
    return int(value) / (10 ** len(value))


def parse_capture_time(text: str | None) -> dt.datetime | None:
    """解析 `metadata.capture_at`（naive 本地时间）。"""
    if not text:
        return None
    value = str(text).strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- 输入

@dataclass(frozen=True)
class ShotAsset:
    """参与分组的一个资产（一条 `asset` + 它的分组线索）。

    刻意只带分组真正需要的字段 —— 让"分组不需要读文件"在类型上就成立。
    """

    asset_id: int
    file_type: str
    relative_path: str
    #: `metadata.capture_at`（本地时间）；无则为 None
    capture_at: str | None = None
    #: `metadata.subsec`（Nikon 为百分秒字符串）
    subsec: str | None = None
    lens_model: str | None = None
    focal_length: float | None = None
    aperture: float | None = None
    shutter_speed: str | None = None
    iso: int | None = None
    #: 视频没有本地时间时，用容器 UTC 做**粗略排序键**（不参与"同一次按快门"的判定）
    capture_at_utc: str | None = None

    @property
    def is_video(self) -> bool:
        return self.file_type == "video"

    @property
    def name(self) -> str:
        return str(self.relative_path).replace("/", "\\").rsplit("\\", 1)[-1]

    @property
    def order_key(self) -> dt.datetime | None:
        """用于排序的时间键。

        照片用本地时间；视频没有本地时间（时区未知，Phase 1 有意为之），
        退回容器 UTC —— **只用于排序**，不用于"是否同一次按快门"的判定，
        因为它的绝对基准与照片不同（差一个时区）。视频因此基本总是自成一组，
        这与 PRD §9.2「视频不做视频精选」的边界一致。
        """
        return parse_capture_time(self.capture_at) or parse_capture_time(self.capture_at_utc)

    @property
    def sort_stamp(self) -> float:
        base = self.order_key
        if base is None:
            return float("inf")     # 无时间的排最后，不参与任何"相邻"判定
        return base.timestamp() + parse_subsecond(self.subsec)

    @property
    def exposure_key(self) -> tuple:
        """曝光三要素（用于判断"组内曝光是否变过"）。"""
        return (self.aperture, self.shutter_speed, self.iso)


# --------------------------------------------------------------------------- 输出

@dataclass
class ShotGroup:
    """一个分好的 Shot。"""

    assets: list[ShotAsset] = field(default_factory=list)
    confidence: float = CONFIDENCE_STRONG
    #: 人类可读的解释（"为什么给它这个置信度"），供界面显示
    notes: list[str] = field(default_factory=list)

    @property
    def primary_asset_id(self) -> int:
        """主 Asset：RAW > HEIC > TIFF > JPEG > PNG > 视频（Phase 1 §4.2 决策 4）。

        同一优先级内按 `asset_id` 升序取第一个，保证**确定性**（同样的输入永远得同一个结果）。
        """
        return min(
            self.assets,
            key=lambda a: (_PRIMARY_PRIORITY.get(a.file_type, 99), a.asset_id),
        ).asset_id

    @property
    def captured_at(self) -> str | None:
        """Shot 的拍摄时间 = 组内最早的照片时间（视频的 UTC 不参与，避免把 Shot 时间带偏）。"""
        times = [
            a.capture_at for a in self.assets
            if a.capture_at and not a.is_video
        ]
        if times:
            return min(times)
        for a in self.assets:
            if a.capture_at:
                return a.capture_at
        return None

    @property
    def capture_date_local(self) -> str | None:
        stamp = self.captured_at
        return stamp[:10] if stamp else None

    @property
    def camera_key(self) -> str | None:
        """机型线索（PRD §4.3：不建 Device 实体，信息留在 metadata）。"""
        return None

    @property
    def asset_ids(self) -> list[int]:
        return [a.asset_id for a in self.assets]

    @property
    def size(self) -> int:
        return len(self.assets)

    @property
    def needs_review(self) -> bool:
        return self.confidence < CONFIDENCE_REVIEW_FLOOR

    @property
    def file_types(self) -> list[str]:
        return sorted({a.file_type for a in self.assets})


# --------------------------------------------------------------------------- 分组

def _sequence_number(name: str) -> tuple[str, int] | None:
    """从文件名取出 `(前缀, 序号)`，用于"序号是否连续"这个置信度信号。

    `DSC_9005__2.JPG` → `('dsc_', 9005)`：**先剥掉 `__N` 撞名后缀**。
    这一步是必须的 —— PHASE1_TECH_PLAN §B3 用实测证明 `__N` 有两种互斥语义
    （真重复 / 撞名但内容不同），因此它不代表身份；但**序号本身**仍是有效的连拍线索。
    """
    stem = str(name).rsplit(".", 1)[0]
    match = re.match(r"^(?P<prefix>.*?)(?P<num>\d+?)(?:__(?P<dup>\d+))?$", stem)
    if not match:
        return None
    return match.group("prefix").lower(), int(match.group("num"))


def _assess(group: Sequence[ShotAsset]) -> tuple[float, list[str]]:
    """给一个时间连续的组打置信度 + 生成人类可读解释。

    规则表（PHASE2_TECH_PLAN §4.2）：
      * 镜头变 或 焦段变 → 0.40（强反证，建议人工确认）
      * 否则曝光变        → 0.80（正常现象）
      * 否则序号不连续    → 0.85（例如撞名后缀把序号打断）
      * 否则              → 0.95
    """
    if len(group) == 1:
        return CONFIDENCE_STRONG, []

    notes: list[str] = []
    lenses = {a.lens_model for a in group if a.lens_model}
    focals = {a.focal_length for a in group if a.focal_length is not None}
    exposures = {a.exposure_key for a in group}
    seqs = [_sequence_number(a.name) for a in group]

    lens_changed = len(lenses) > 1
    focal_changed = len(focals) > 1

    if lens_changed:
        notes.append("组内镜头变过 → 可能不是连拍，建议人工确认")
    if focal_changed:
        notes.append("组内焦段变过 → 可能不是连拍，建议人工确认")
    if lens_changed or focal_changed:
        return CONFIDENCE_SUSPECT, notes

    if len(exposures) > 1:
        notes.append("组内曝光变过（连拍中途自动曝光属正常现象）")
        return CONFIDENCE_EXPOSURE_VARIED, notes

    # 序号连续才给最高置信度。`_sequence_number()` 对没有数字的文件名返回 None，
    # 不能直接解包 —— 2026-10-21 的真机导入里有 6,586 张手机照片（含中文名、
    # `DOCUMENT_PICTURE` 等），整批 regroup 就是在这一行 `TypeError` 崩掉的。
    parsed = [seq for seq in seqs if seq is not None]
    prefixes = {p for p, _ in parsed}
    numbers = [n for _p, n in parsed]
    complete = len(parsed) == len(seqs) and len(prefixes) == 1
    contiguous = False
    if complete:
        ordered = sorted(numbers)
        contiguous = all(ordered[i + 1] - ordered[i] == 1 for i in range(len(ordered) - 1))
    if not (complete and contiguous):
        notes.append("文件名序号不连续 → 置信度略降")
        return CONFIDENCE_LIKELY, notes

    return CONFIDENCE_STRONG, notes


def group_shots(
    assets: Iterable[ShotAsset],
    *,
    gap_seconds: float,
) -> list[ShotGroup]:
    """把一批资产按"时间 + subsec"切成 Shot（**确定性、可重算**）。

    参数
    ----
    gap_seconds
        相邻两张之间超过这个间隔就切新 Shot。**必填、无默认值** —— 见模块 docstring：
        默认值属于配置层（决策 #5），不属于算法层。

    排序键
    ------
    `(capture_at, subsec, relative_path)`。`subsec` 必须参与：实测有大量同秒相邻对
    （万宁市 302 张里 46 对），只按秒排序会让分组边界随机漂移。

    切分条件（**唯一**的硬条件）
    --------------------------
    相邻两张的间隔 `> gap_seconds`。镜头/焦段/曝光/序号**一律不参与切分**，
    只影响 `confidence` 与 `notes`。
    """
    if gap_seconds <= 0:
        raise ValueError("gap_seconds 必须为正数")

    ordered = sorted(assets, key=lambda a: (a.sort_stamp, a.relative_path))

    groups: list[list[ShotAsset]] = []
    current: list[ShotAsset] = []
    for item in ordered:
        if not current:
            current = [item]
            continue
        prev = current[-1]
        gap = item.sort_stamp - prev.sort_stamp
        # 任一侧无时间（sort_stamp = inf）→ 不能判定"相邻"，各自成组
        if gap > gap_seconds or gap != gap:      # gap != gap 处理 inf - inf = nan
            groups.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        groups.append(current)

    out: list[ShotGroup] = []
    for members in groups:
        confidence, notes = _assess(members)
        out.append(ShotGroup(assets=list(members), confidence=confidence, notes=notes))
    return out


def group_by_directory(
    assets: Iterable[ShotAsset],
    *,
    gap_seconds: float,
) -> dict[tuple[str, str], list[ShotGroup]]:
    """按 `<城市>\\<时段>` 分段后各自分组。

    Shot 只在**同一个时段目录内**成立：跨时段的两张照片即使时间相邻，
    也是两次不同的出行，不该合成一次按快门。
    """
    buckets: dict[tuple[str, str], list[ShotAsset]] = {}
    for item in assets:
        parts = str(item.relative_path).replace("/", "\\").split("\\")
        # ⚠️ 以前这里 `if len(parts) < 3: continue` 会**静默丢弃**所有不在
        # `<城市>\<时段>\文件` 结构里的照片 —— 平铺的文件夹（用户随手放一批照片）
        # 于是得到"参与 225 张 / Shot 0 个"，接着 scenes rebuild 直接失败。
        # 现在按层级退化到"(未分类)"分组：仍然按时间间隔切 Shot，只是不细分城市/时段。
        if len(parts) >= 3:
            key = (parts[0], parts[1])
        elif len(parts) == 2:
            key = ("(未分类)", parts[0] or "(根目录)")
        else:
            key = ("(未分类)", "(根目录)")
        buckets.setdefault(key, []).append(item)
    return {key: group_shots(items, gap_seconds=gap_seconds) for key, items in sorted(buckets.items())}


# --------------------------------------------------------------------------- 只增不删的重算（决策 #4）

@dataclass
class RegroupPlan:
    """一次 Shot 重算的计划（**尚未落库**）。

    决策 #4 的落点：重算**只增不删**。`selection.shot_id` 是 `ON DELETE CASCADE`，
    物理删除一个被精选引用的 Shot 会**连带删掉精选记录**。因此：
      * `reused`   = 资产集合与新方案完全一致，原地更新即可
      * `created`  = 新出现的 Shot（需要 INSERT）
      * `retired`  = 旧方案里有、新方案里没有 → **标 retired_at，不删除**
                     （若它还有 selection 行，调用方必须把 selection 迁到新 Shot）
    """

    reused: list[int] = field(default_factory=list)
    created: list[int] = field(default_factory=list)
    retired: list[int] = field(default_factory=list)


def plan_regroup(
    new_groups: Sequence[ShotGroup],
    *,
    existing: dict[int, frozenset[int]],
    protected: frozenset[int] = frozenset(),
) -> RegroupPlan:
    """算出"哪些 Shot 可复用、哪些要新建、哪些该退役"。

    参数
    ----
    new_groups
        新算出的分组。
    existing
        `{shot_id: frozenset(asset_id)}` —— 库内现有的 Shot 及其资产集合。
    protected
        **必须冻结**的 Shot id（`group_source='user'`，或用户手工调整过的分组）。
        它们不参与匹配，因此不会被复用也不会被退役 —— 决策 #10：
        "用户一改即置 user 并**永不被后续重算覆盖**"。

    匹配策略：按**资产集合完全相等**匹配。
    刻意不做"部分重合也算复用"的模糊匹配 —— 那会让"复用"变得不可预测，
    而退役 + 新建的代价只是多几行（`shot` 表可忽略），
    **可预测性比省几行重要**（这正是决策 #4 选择"只增不删"的前提）。
    """
    plan = RegroupPlan()
    remaining: dict[frozenset[int], list[int]] = {}
    for shot_id, assets in existing.items():
        if shot_id in protected:
            continue
        remaining.setdefault(assets, []).append(shot_id)

    for group in new_groups:
        key = frozenset(group.asset_ids)
        candidates = remaining.get(key)
        if candidates:
            plan.reused.append(candidates.pop())
            continue
        plan.created.append(0)      # 0 = 占位，真实 id 由落库层分配

    for shot_ids in remaining.values():
        plan.retired.extend(sorted(shot_ids))
    return plan


__all__ = [
    "CONFIDENCE_EXPOSURE_VARIED",
    "CONFIDENCE_LIKELY",
    "CONFIDENCE_REVIEW_FLOOR",
    "CONFIDENCE_STRONG",
    "CONFIDENCE_SUSPECT",
    "GROUP_SOURCE_AUTO",
    "GROUP_SOURCE_USER",
    "RegroupPlan",
    "ShotAsset",
    "ShotGroup",
    "group_by_directory",
    "group_shots",
    "parse_capture_time",
    "parse_subsecond",
    "plan_regroup",
]
