"""从既有库**派生 Session 行**（Phase 2 · M1）。

## 为什么需要这个模块

Phase 1 把 `<城市>\\<时段>` 只当作 `relative_path` 的前两段，**没有落 `session` 行**
（当时有意排除，见 `PHASE1_EXT_TECH_PLAN` §1.6 的"若你希望严守范围"）。Phase 2 的
Shot / Scene 需要稳定的 `session_id` 外键，因此要把这 71 个时段补成行。

## 为什么是"派生"而不是"重跑分类"

分类链（`library/classify.py`）需要读**源文件**的 EXIF 才能判定城市与时段；而库内文件的
元数据**已经在 `metadata` 表里了**。再跑一遍分类等于为了算一次 `MIN/MAX` 去读 21,400 个
文件——既慢又会碰原片。这里只读数据库。

`session` 的身份键是 `(city_hint, period_hint)`，由迁移 005 的部分唯一索引强制。
这两个值**就是目录的前两段**——不是"从路径猜语义"，而是"读取已经确定的身份"。

## 两个只有实测才会发现的时区陷阱

**① 视频的容器 UTC 不能直接当本地时间。**
视频的 `metadata.capture_at` 是 NULL（时区未知，Phase 1 有意为之），只有 `capture_at_utc`。
实测：直接用它会把 **15 个时段的起点拉歪 4.5–8 小时**
（例：`海北藏族自治州\\2025-09-20_0654-1623` 会被算成 `2025-09-19 22:54` 而不是 `06:54`）。

**② 偏移必须逐条视频向"时间最邻近的照片"借**（而不是给整个时段取一个众数）。

诚实说明：在**当前这个库上**，两种策略的结果相同 —— 实测 78 个时段里只有 1 个混用了偏移
（`希腊市\\2026-10-02_1645-2128`：`+08:00` 158 张 / `+03:00` 114 张），而**它恰好没有视频**；
其余时段都是单一偏移，"众数"与"逐条取最近"必然一致。因此**本库数据证明不了**这条选择的必要性。

仍然选择"逐条取最近"，理由有两条：

1. **语义正确**：视频的本地时间应当由**与它在时间上真正相邻的那张照片**决定，
   而不是由"整个时段里多数照片的偏移"决定。相机的时钟可能在同一时段中途被改过
   （`2026-10-02` 那个时段就是这个形态：16:45–17:29 是 `+03:00`、18:58–21:28 变成 `+08:00`），
   这时"众数"对边界上的视频就是错的那个。
2. **与导入链路同构**：`classify.infer_video_local_times` 用的就是"按 UTC 时间取最近的照片"。
   回填若换一套策略，就会出现"目录名按 A 算、session 行按 B 算"的隐性不一致。
   保持同一策略，才能用目录名当基准来校验（见 `verify_against_folder_names`）。

（`tests/unit/test_phase2_session_derive.py::test_borrows_from_nearest_photo_not_period_mode`
用一个构造出来的混用时段把这个差异钉住了 —— 即使当前真实库覆盖不到它。）

## 验证基准（写进测试）

既有目录名形如 `YYYY-MM-DD_HHMM-HHMM`，**就是**那一段的**本地**时间跨度
（旧工具 `classify-photos.ps1` 的命名规则，Phase 1 扩展已 71/71 复现验证过）。
于是"哪个算法正确"有一个**外部基准**可比，不需要自证：

    只数照片（不借偏移）：2026-10-12 实测 72 个可比时段中，不借偏移会错 15 个
    借偏移：             同上 72 个可比时段，**逐字相符 72、不符 0**

（66/67 那组数字出自调研阶段的较早快照；导入希腊照片后时段从 71 增至 78，
可比时段由 67 增至 72，结论不变：借偏移 100% 相符。）
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .classify import ClassifiedSession

#: 目录名 = 同一天：`YYYY-MM-DD_HHMM-HHMM`
_SAME_DAY_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<start>\d{4})-(?P<end>\d{4})$")
#: 目录名 = 跨天：`YYYY-MM-DD_HHMM_至_MM-DD_HHMM` 或 `..._至_YYYY-MM-DD_HHMM`
_CROSS_DAY_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<start>\d{4})_至_")
#: `+08:00` / `-03:30`
_TZ_RE = re.compile(r"^(?P<sign>[+-])(?P<h>\d{2}):(?P<m>\d{2})$")

#: 借不到偏移时的置信度（时段里全是没有偏移的照片/视频）
CONFIDENCE_NO_OFFSET = 0.8
#: 时段内混用多个 tz_offset（相机时钟没改之类）—— 决策 #11：不判断，但降置信度
CONFIDENCE_MIXED_OFFSET = 0.8
#: 目录名是**已存在的物理事实**（不是推导出来的），因此默认满信
CONFIDENCE_DEFAULT = 1.0


@dataclass(frozen=True)
class AssetTime:
    """派生 Session 所需的最小输入（一条 asset + 它的时间信息）。

    刻意只保留这几个字段：派生逻辑**不需要**文件路径以外的任何东西，
    更不需要打开文件（铁律 #1）。
    """

    library_id: int
    relative_path: str
    file_type: str
    #: 本地拍摄时间（`metadata.capture_at`）；视频为 None
    capture_at: str | None = None
    #: 容器 UTC（`metadata.capture_at_utc`）；照片通常为 None
    capture_at_utc: str | None = None
    #: `metadata.tz_offset`，形如 '+08:00'；无偏移为 None
    tz_offset: str | None = None

    @property
    def is_video(self) -> bool:
        return self.file_type == "video"

    @property
    def dir_key(self) -> tuple[str, str] | None:
        """`(城市, 时段)`；路径层数不足两段时返回 None。"""
        parts = str(self.relative_path).replace("/", "\\").split("\\")
        # 同 shot_grouping：平铺文件夹（<3 段）以前被丢弃 → 没有 session →
        # scenes rebuild 得到 0 个 Scene。这里退化成"(未分类)"，仍按时间切场次。
        if len(parts) >= 3:
            city, period = parts[0], parts[1]
        elif len(parts) == 2:
            city, period = "(未分类)", parts[0] or "(根目录)"
        else:
            city, period = "(未分类)", "(根目录)"
        if not city or not period:
            return None
        return city, period


@dataclass
class DerivedSession:
    """一条待写入 `session` 表的行 + 派生依据（供 dry-run 表格展示与审计）。"""

    city: str
    period: str
    captured_from: dt.datetime
    captured_to: dt.datetime
    confidence: float
    photo_count: int
    video_count: int
    borrowed_offsets: list[str] = field(default_factory=list)
    #: 时段内出现过的全部 tz_offset（多于一个 → 混用，见决策 #11）
    distinct_offsets: list[str] = field(default_factory=list)
    #: 视频未借到偏移的条数（时间未知，不猜）
    videos_without_offset: int = 0

    @property
    def name(self) -> str:
        return f"{self.city}\\{self.period}"

    @property
    def total(self) -> int:
        return self.photo_count + self.video_count

    @property
    def mixed_offsets(self) -> bool:
        return len(self.distinct_offsets) > 1

    def to_row(self) -> dict[str, object]:
        """转成 `SessionRepo.upsert_many` 接受的行（不含 name，由 repo 兜底）。"""
        return {
            "name": self.name,
            "city_hint": self.city,
            "period_hint": self.period,
            "captured_from": self.captured_from.strftime("%Y-%m-%d %H:%M:%S"),
            "captured_to": self.captured_to.strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": self.confidence,
            "source": "auto",
        }

    def as_classified(self) -> ClassifiedSession:
        """复用 Phase 1 已有的 `ClassifiedSession` 形状（便于与导入链路共用代码）。"""
        return ClassifiedSession(
            city=self.city,
            folder=self.period,
            captured_from=self.captured_from,
            captured_to=self.captured_to,
            confidence=self.confidence,
            members=self.total,
            time_unknown=self.videos_without_offset > 0,
        )


# ---------------------------------------------------------------- 解析

def parse_wall_clock(text: str | None) -> dt.datetime | None:
    """解析 `metadata` 里的时间文本（naive 本地时间或 UTC，两种写法都见得到）。"""
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


def offset_from_tz(tz_offset: str | None) -> dt.timedelta | None:
    """把 `'+08:00'` 解析成 `timedelta`；无法解析返回 None（**不猜**）。"""
    if not tz_offset:
        return None
    match = _TZ_RE.match(str(tz_offset).strip())
    if not match:
        return None
    sign = 1 if match.group("sign") == "+" else -1
    return sign * dt.timedelta(hours=int(match.group("h")), minutes=int(match.group("m")))


def folder_name_matches_span(period: str, captured_from: dt.datetime, captured_to: dt.datetime) -> bool | None:
    """目录名是否与派生出的本地时间跨度逐字相符。

    返回 None = 该目录名不符合"同日"格式（跨天命名），无法用这个基准检验。
    这是**唯一**能独立于本算法验证正确性的手段 —— 见模块 docstring。
    """
    match = _SAME_DAY_RE.match(str(period))
    if not match:
        return None
    return (
        captured_from.strftime("%Y-%m-%d") == match.group("date")
        and captured_from.strftime("%H%M") == match.group("start")
        and captured_to.strftime("%H%M") == match.group("end")
    )


def is_cross_day_name(period: str) -> bool:
    return bool(_CROSS_DAY_RE.match(str(period)))


# ---------------------------------------------------------------- 主流程

def derive_sessions(assets: Iterable[AssetTime]) -> list[DerivedSession]:
    """把资产时间信息聚合成 `session` 行（确定性、可重复）。

    **视频本地时间 = 容器 UTC + 同一时段内"按 UTC 时间最邻近的、带偏移的照片"的偏移**。
    借不到（该时段没有带偏移的照片）时**保留时区未知**，不猜 —— 与 Phase 1 决策 #3 一致。
    """
    buckets: dict[tuple[str, str], list[AssetTime]] = {}
    for item in assets:
        key = item.dir_key
        if key is None:
            continue
        buckets.setdefault(key, []).append(item)

    out: list[DerivedSession] = []
    for (city, period), items in sorted(buckets.items()):
        photos = [a for a in items if not a.is_video]
        videos = [a for a in items if a.is_video]

        photo_times = [t for t in (parse_wall_clock(a.capture_at) for a in photos) if t is not None]
        if not photo_times:
            # 全无照片（只有视频）→ 借不到偏移，记时区未知；本库当前没有这种时段，
            # 但导入链路允许它出现（纯视频批次），因此必须处理而不是假设不存在。
            utc_times = [t for t in (parse_wall_clock(a.capture_at_utc) for a in videos) if t is not None]
            if not utc_times:
                continue
            out.append(
                DerivedSession(
                    city=city,
                    period=period,
                    captured_from=min(utc_times),
                    captured_to=max(utc_times),
                    confidence=CONFIDENCE_NO_OFFSET,
                    photo_count=0,
                    video_count=len(videos),
                    distinct_offsets=[],
                    videos_without_offset=len(videos),
                )
            )
            continue

        # 可借偏移的"参照照片"：有 UTC 与偏移才能算时间距离
        refs: list[tuple[dt.datetime, dt.timedelta, str]] = []
        for a in photos:
            off = offset_from_tz(a.tz_offset)
            local = parse_wall_clock(a.capture_at)
            if off is None or local is None:
                continue
            refs.append((local - off, off, str(a.tz_offset)))

        distinct_offsets = sorted({str(a.tz_offset) for a in photos if a.tz_offset})
        borrowed: list[str] = []
        video_times: list[dt.datetime] = []
        videos_without_offset = 0
        for v in videos:
            utc = parse_wall_clock(v.capture_at_utc)
            if utc is None or not refs:
                videos_without_offset += 1
                continue
            # ★ 逐条取"时间最近"而不是取时段众数（实测：众数法会算错混用时区的时段）
            _, off, label = min(refs, key=lambda r: abs((r[0] - utc).total_seconds()))
            video_times.append(utc + off)
            borrowed.append(label)

        times = photo_times + video_times
        confidence = CONFIDENCE_DEFAULT
        if len(distinct_offsets) > 1:
            confidence = CONFIDENCE_MIXED_OFFSET
        elif videos_without_offset or (videos and not video_times):
            confidence = CONFIDENCE_NO_OFFSET

        out.append(
            DerivedSession(
                city=city,
                period=period,
                captured_from=min(times),
                captured_to=max(times),
                confidence=confidence,
                photo_count=len(photos),
                video_count=len(videos),
                borrowed_offsets=borrowed,
                distinct_offsets=distinct_offsets,
                videos_without_offset=videos_without_offset,
            )
        )
    return out


def verify_against_folder_names(
    sessions: Sequence[DerivedSession],
) -> tuple[int, int, list[tuple[str, str, str]], int]:
    """用目录名当基准校验派生结果。

    返回 `(可比对数, 相符数, 不符明细, 跨天跳过数)`。
    不符明细 = `(name, 目录名期望的跨度, 实际派生跨度)`。
    """
    comparable = matched = 0
    skipped = 0
    mismatches: list[tuple[str, str, str]] = []
    for s in sessions:
        verdict = folder_name_matches_span(s.period, s.captured_from, s.captured_to)
        if verdict is None:
            skipped += 1
            continue
        comparable += 1
        got = f"{s.captured_from.strftime('%Y-%m-%d')}_{s.captured_from.strftime('%H%M')}-{s.captured_to.strftime('%H%M')}"
        if verdict:
            matched += 1
        else:
            mismatches.append((s.name, s.period, got))
    return comparable, matched, mismatches, skipped


__all__ = [
    "AssetTime",
    "CONFIDENCE_DEFAULT",
    "CONFIDENCE_MIXED_OFFSET",
    "CONFIDENCE_NO_OFFSET",
    "DerivedSession",
    "derive_sessions",
    "folder_name_matches_span",
    "is_cross_day_name",
    "offset_from_tz",
    "parse_wall_clock",
    "verify_against_folder_names",
]
