"""基础物理分类内核：把一批文件判定到 `<城市>\\<时段>\\`。

**这是纯计算模块** —— 不读原片、不写文件、不碰数据库。输入全部来自索引与调用方
提供的 EXIF 摘要，因此可以完整地单元测试，也便于先"预演"再执行（PRD §11.1）。

产出必须与既有库**逐字一致**，否则会切出重复或割裂的目录（例如同时存在 `万宁市`
与 `萬寧市`，或同一天被切成两个时段目录）。因此本模块严格复刻旧工具
`classify-photos.ps1` 的判定顺序与命名规则：

    pass 0  GPS → 城市（网格直查 → 就近匹配 → 未解析）
    pass 1  按拍摄时间切出「时间连续块」（间隔 > gap_hours 切段）
    pass 2  块内按**城市变化**再切分；段首无城市的成员由段内首个已知城市向后继承
    pass 3  整段无 GPS：**仅当**前后相邻已定位段城市一致且都在 24h 内才推断
    pass 4  与库内既有 `<城市>\\<时段>` **锚点合并**（间隔 ≤ gap_hours 且城市相符则沿用其目录）
    pass 5  生成目标相对路径，同名冲突加 `__N`（**绝不覆盖**）

与旧工具的**一处有意偏差**（见 `_pick_anchor`）：旧实现在同城多个锚点都落在时间窗口内时，
会选中**遍历顺序上最后一个**而不是时间最近的（`-or $cityMatches` 的副作用）。
本模块按"同城优先 + 时间最近"选择 —— 结果只会在"原本就会并入同城目录"的前提下更准确，
不会制造重复目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .geo import GeoCache

#: 无法判定城市时的目录名。既有库（16,105 张）里确实存在该目录，必须沿用同一字面量。
UNKNOWN_CITY = "未知地点"

#: 时段切分间隔（小时）。既有库就是用 12 小时建出来的；`Settings.session_gap_hours` 同值。
DEFAULT_GAP_HOURS = 12

#: `inferred-between` 推断允许的最大前后间距（小时），与旧工具一致。
INFERRED_BETWEEN_MAX_HOURS = 24

#: 繁 → 简 映射。**逐项复刻**旧工具 `ConvertTo-CityFolderName`，一项都不能少：
#: 缓存里存的 `萬寧市` / `嘉峪關市` 全靠它才能变成库内的 `万宁市` / `嘉峪关市`。
TRADITIONAL_TO_SIMPLIFIED: dict[str, str] = {
    "關": "关", "萬": "万", "寧": "宁", "東": "东", "區": "区", "縣": "县", "臺": "台", "廣": "广",
    "龍": "龙", "陽": "阳", "貴": "贵", "蘭": "兰", "門": "门", "灣": "湾", "島": "岛", "遼": "辽",
    "濟": "济", "齊": "齐", "樂": "乐", "興": "兴", "濱": "滨", "鶴": "鹤", "崗": "岗", "頭": "头",
    "廈": "厦", "澤": "泽", "蘇": "苏", "無": "无", "錫": "锡", "揚": "扬", "鎮": "镇", "麗": "丽",
    "雲": "云", "華": "华", "國": "国", "學": "学", "會": "会", "農": "农", "業": "业", "產": "产",
}

#: 末尾已带这些后缀时不再补「市」。
_CITY_SUFFIXES = ("市", "州", "盟", "地区", "县", "区", "旗")

#: `city_how` → 置信度。用于写入 `session.confidence`，也用于预演表格的排序提示。
CITY_CONFIDENCE: dict[str, float] = {
    "gps-cell": 1.0,
    "manual": 1.0,
    #: 整批人工指定（用户明确知道拍在哪）—— 与逐格指认同等可信
    "manual-batch": 1.0,
    "gps-near": 0.8,
    "inherited-session": 0.6,
    "inherited-library": 0.6,
    "inferred-between": 0.5,
    "unknown": 0.0,
    "none": 0.0,
}

#: `gps-near` 后面跟着**距离**（`geo.nearest()` 返回 `gps-near0.12`），
#: 所以查表前要把数值尾巴去掉。见 `confidence_for_how()`。
_NEAR_SUFFIX = re.compile(r"^(gps-near)\d+(?:\.\d+)?$")

#: 查不到时的兜底。**低于所有具名来源**（最低的具名是 inferred-between 0.5）——
#: 取 0.5 会让"未知来源"和"推断出来的"一样可信，那不对。
_CONFIDENCE_FALLBACK = 0.4


def confidence_for_how(city_how: str | None) -> float:
    """把 `city_how` 映射成置信度。

    ⚠️ 这里**必须**先归一化再查表。`geo.nearest()` 返回的 `how` 带着距离
    （`gps-near0.12`），而 `CITY_CONFIDENCE` 里的键是 `"gps-near"` ——
    直接 `CITY_CONFIDENCE.get(how, 0.5)` 会**永远匹配不上**，
    静默落到兜底值。

    这个缺陷在本机**从未触发**（真实地理缓存下所有 GPS 都能网格直查命中，
    `nearest()` 一次都没走到），所以它是潜伏的。但它是真的：
    一旦有人用了缓存边缘的坐标，得到的置信度就是兜底值而不是 0.8。

    距离仍然保留在 `city_how` 里供界面显示（用户能看懂"为什么归到这个城市"），
    只是不参与查表。
    """
    if not city_how:
        return _CONFIDENCE_FALLBACK
    key = city_how.strip()
    if key in CITY_CONFIDENCE:
        return CITY_CONFIDENCE[key]
    match = _NEAR_SUFFIX.match(key)
    if match:
        return CITY_CONFIDENCE[match.group(1)]
    return _CONFIDENCE_FALLBACK

#: `time_how` 取值（与 `metadata.tz_source` 的既有词汇表保持一致）
TIME_EXIF = "exif"                        # EXIF 本地时间（含时区偏移）→ 可靠
TIME_EXIF_WALLCLOCK = "exif-wallclock"    # EXIF 有本地时间但无偏移 → 时区未知
TIME_CONTAINER_UTC = "container-utc"      # 视频容器 UTC → 时区未知
TIME_VIDEO_INFERRED = "video-tz-from-photo"   # 视频 UTC + 邻近照片推断出的偏移
TIME_FILE_MTIME = "file-mtime"            # 退回文件修改时间
TIME_NONE = "none"


# --------------------------------------------------------------------------- 命名
def normalize_city_folder_name(name: str) -> str:
    """把缓存/EXIF 里的原始地名规范成**目录名**。

    两步（顺序不可换，否则 `萬寧` 会先被补成 `萬寧市` 再转简体，虽然结果相同，
    但换序在多字后缀判断上会不一致）：
      1. 繁体 → 简体
      2. 末尾若不以 市/州/盟/地区/县/区/旗 结尾 → 补「市」
    """
    if not name:
        return name
    out = name
    for traditional, simplified in TRADITIONAL_TO_SIMPLIFIED.items():
        out = out.replace(traditional, simplified)
    if not out.endswith(_CITY_SUFFIXES):
        out = out + "市"
    return out


def session_folder_name(start: datetime, end: datetime) -> str:
    """时段目录名。三种形态与既有库逐字一致：

    ==================  ==========================================  ==========================
    情况                格式                                        既有库实例
    ==================  ==========================================  ==========================
    同一日              ``yyyy-MM-dd_HHmm-HHmm``                    ``2025-11-29_0610-1747``
    同年跨日            ``yyyy-MM-dd_HHmm_至_MM-dd_HHmm``           ``2025-09-07_2212_至_09-08_0200``
    跨年                ``yyyy-MM-dd_HHmm_至_yyyy-MM-dd_HHmm``      ——
    ==================  ==========================================  ==========================
    """
    if start.date() == end.date():
        return f"{start:%Y-%m-%d}_{start:%H%M}-{end:%H%M}"
    if start.year == end.year:
        return f"{start:%Y-%m-%d_%H%M}_至_{end:%m-%d_%H%M}"
    return f"{start:%Y-%m-%d_%H%M}_至_{end:%Y-%m-%d_%H%M}"


def parse_wall_clock(value: str | None) -> datetime | None:
    """把索引里存的时间串解析成**naive 本地挂钟时间**。

    `metadata.capture_at` 存的本来就是本地时间（偏移另存于 `tz_offset`），
    因此这里**直接丢弃时区信息**而不做换算 —— 换算会把 06:10 变成 22:10 前一天。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def offset_minutes_from_text(value: str | None) -> int | None:
    """`'+08:00'` → `480`；无法解析返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    if len(text) < 3 or text[0] not in "+-":
        return None
    try:
        sign = 1 if text[0] == "+" else -1
        hours, _, minutes = text[1:].partition(":")
        return sign * (int(hours) * 60 + int(minutes or 0))
    except ValueError:
        return None


# --------------------------------------------------------------------------- 输入
@dataclass(frozen=True)
class ClassifyItem:
    """一个待归类文件的判定输入。"""

    key: str                                  # 源侧标识（批次内唯一，用于回填结果）
    name: str                                 # 文件名（含扩展名）
    kind: str = "photo"                       # photo | video
    size: int | None = None
    local_time: datetime | None = None        # 本地挂钟时间；视频通常为 None（需推断）
    utc_time: datetime | None = None          # 已知的精确 UTC（naive，语义为 UTC）
    offset_minutes: int | None = None         # 已知时区偏移
    time_how: str = TIME_NONE
    lat: float | None = None
    lon: float | None = None
    file_mtime: datetime | None = None        # 最后兜底（旧工具也这么做）

    @property
    def is_video(self) -> bool:
        return self.kind == "video"


@dataclass(frozen=True)
class SessionAnchor:
    """库内**既有**的 `<城市>\\<时段>` 目录及其时间范围。

    存在的意义：导入不能为库内已有的时段再建一个平行目录。新块若与锚点时间相邻
    （≤ gap_hours）且城市相符，就沿用它的目录名。
    """

    city: str
    folder: str
    start: datetime
    end: datetime

    def gap_to(self, start: datetime, end: datetime) -> timedelta:
        """两个时间区间之间的距离；重叠时为 0。"""
        if self.end < start:
            return start - self.end
        if end < self.start:
            return self.start - end
        return timedelta(0)


@dataclass(frozen=True)
class ClassifiedFile:
    """单个文件的判定结果。"""

    key: str
    name: str
    kind: str
    city: str
    city_how: str
    session: str
    target_relative: str          # `<城市>\\<时段>\\<文件名>`（已处理 __N 冲突）
    local_time: datetime
    time_how: str
    size: int | None = None
    time_unknown: bool = False
    #: 因为同名而被改名前，**原本要落的相对路径**。
    #: 调用方据此可以做内容级判定：若既有文件与源内容相同，正确行为是"跳过"而不是
    #: 复制出 `__2` 副本 —— 否则重复导入同一张存储卡会把整个库复制一遍。
    #: 纯计算模块拿不到文件内容，因此这里只如实报告冲突，判定交给调用方。
    collision_with: str | None = None


@dataclass(frozen=True)
class ClassifiedSession:
    """一个 `<城市>\\<时段>`（可直接写进 `session` 表）。"""

    city: str
    folder: str
    captured_from: datetime
    captured_to: datetime
    confidence: float
    members: int
    time_unknown: bool = False


@dataclass(frozen=True)
class ClassifyPlan:
    files: list[ClassifiedFile] = field(default_factory=list)
    sessions: list[ClassifiedSession] = field(default_factory=list)
    #: 未命中缓存、又超出就近匹配范围的网格键（建议人工指认后回写缓存）
    unresolved_cells: list[str] = field(default_factory=list)
    #: 无法判定城市的文件数（会落到 `未知地点`）
    unknown_city_count: int = 0
    #: 时区未知的文件数（视频未找到可参照的邻近照片）
    unknown_time_count: int = 0
    #: 本次使用的整批强制城市（None 表示没有强制）
    city_override: str | None = None
    #: 被整批强制城市压过的 GPS 判定：`(文件 key, GPS 原本判成的城市)`。
    #: 列出来是为了让用户在预演里**核对**"我指定的城市"和"GPS 说的"是否一致 ——
    #: 强制是人的决定，但人不该在看不到分歧的情况下决定。
    city_override_disagreements: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.files)

    def by_city(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.files:
            counts[item.city] = counts.get(item.city, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def by_city_how(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.files:
            counts[item.city_how] = counts.get(item.city_how, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def by_time_how(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.files:
            counts[item.time_how] = counts.get(item.time_how, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- 时间
def infer_video_local_times(
    items: Sequence[ClassifyItem],
) -> tuple[list[ClassifyItem], list[str]]:
    """给「有 UTC 但没有本地时间」的视频，从**邻近照片**借一个时区偏移。

    为什么不按固定 +08:00 折算：境外拍摄会整体算错，进而把视频切进错误的时段目录。
    为什么可以借：同一批次里时间相邻的照片带有 EXIF 偏移 —— 那是**证据**，不是猜测。
    找不到可参照的照片时不猜：保留时区未知，只在报告里标注。

    返回 `(更新后的 items, 说明列表)`。
    """
    photos_with_offset = [
        item for item in items if not item.is_video and item.offset_minutes is not None and item.utc_time
    ]
    if not photos_with_offset:
        return list(items), []

    resolved: list[ClassifyItem] = []
    borrowed = 0
    for item in items:
        if not item.is_video or item.local_time is not None or item.utc_time is None:
            resolved.append(item)
            continue
        nearest = min(
            photos_with_offset,
            key=lambda photo: abs((photo.utc_time - item.utc_time).total_seconds()),  # type: ignore[operator]
        )
        offset = nearest.offset_minutes
        assert offset is not None
        resolved.append(
            _replace_item(
                item,
                local_time=item.utc_time + timedelta(minutes=offset),
                offset_minutes=offset,
                time_how=TIME_VIDEO_INFERRED,
            )
        )
        borrowed += 1

    notes = []
    if borrowed:
        notes.append(
            f"{borrowed} 个视频的时区由同一批次中时间最邻近的照片推断"
            "（这些照片带有 EXIF 偏移，属于证据而非猜测）"
        )
    return resolved, notes


def _replace_item(item: ClassifyItem, **changes: object) -> ClassifyItem:
    from dataclasses import replace

    return replace(item, **changes)  # type: ignore[arg-type]


def _effective_time(item: ClassifyItem) -> tuple[datetime | None, str, bool]:
    """取用于排序/切段的本地挂钟时间，以及它是否"时区未知"。"""
    if item.local_time is not None:
        unknown = item.time_how in {TIME_EXIF_WALLCLOCK, TIME_CONTAINER_UTC}
        return item.local_time, item.time_how, unknown
    if item.file_mtime is not None:
        return item.file_mtime, TIME_FILE_MTIME, True
    return None, TIME_NONE, True


# --------------------------------------------------------------------------- 主流程
def plan_classification(
    items: Iterable[ClassifyItem],
    *,
    geo: GeoCache | None = None,
    anchors: Sequence[SessionAnchor] = (),
    gap_hours: float = DEFAULT_GAP_HOURS,
    unknown_city: str = UNKNOWN_CITY,
    reserved_relative: Iterable[str] = (),
    city_override: str | None = None,
) -> ClassifyPlan:
    """把一批文件判定到 `<城市>\\<时段>\\<文件名>`。

    `reserved_relative` 给出**目标库中已存在**的相对路径（大小写不敏感），
    用于在预演阶段就避免覆盖与重名。

    `city_override` 是**整批强制城市**。为什么需要它：相机常常整段没有 GPS
    （未连接手机定位），而境外拍摄即使有 GPS 也可能不在离线缓存里 ——
    这两种情况下"按坐标反查"根本无从下手，但**用户自己知道这批拍在哪**。
    一旦给出就压过 GPS，并在报告中列出"GPS 原本判成了什么"以供核对。
    """
    geo = geo or GeoCache()
    items, notes = infer_video_local_times(list(items))

    unresolved: set[str] = set()
    disagreed: set[tuple[str, str]] = set()
    #: 整批指定的城市（用户明确知道这批素材拍在哪）。一旦给出就**压过 GPS**，
    #: 因为它是"人已经确认过的事实"，而 GPS 可能整个缺失（例如相机没连手机定位）。
    override_city = normalize_city_folder_name(city_override) if city_override else None

    # ---- pass 0：逐项 GPS → 城市
    members: list[_Member] = []
    for order, item in enumerate(items):
        city: str | None = None
        city_how = "none"
        if item.lat is not None and item.lon is not None:
            hit = geo.lookup(item.lat, item.lon)
            if hit is not None:
                city = normalize_city_folder_name(hit.city)
                city_how = hit.how
            else:
                from .geo import cell_key

                unresolved.add(cell_key(item.lat, item.lon))
        local, time_how, time_unknown = _effective_time(item)
        if override_city is not None:
            # 记下"GPS 原本判成了别的城市"，便于调用方在预演里提示用户核对
            if city is not None and city != override_city:
                disagreed.add((item.key, city))
            city = override_city
            city_how = "manual-batch"
        members.append(
            _Member(
                item=item,
                order=order,
                city=city,
                city_how=city_how if city else ("unknown" if item.lat is not None else "none"),
                local=local,
                time_how=time_how,
                time_unknown=time_unknown,
            )
        )

    # 没有时间的文件无法参与按时间切段：单独兜到 `未知地点` 的一个稳定时段里
    timed = [m for m in members if m.local is not None]
    untimed = [m for m in members if m.local is None]

    timed.sort(key=lambda m: (m.local, m.item.name))  # type: ignore[arg-type,return-value]
    blocks = _time_blocks(timed, gap_hours=gap_hours)
    parts = _split_on_city_change(blocks)
    _inherit_city_in_part(parts)
    _infer_between(parts)
    _merge_into_anchors(parts, anchors, gap_hours=gap_hours, unknown_city=unknown_city)

    for part in parts:
        if part.city is None:
            part.city = unknown_city
            for member in part.members:
                if member.city is None:
                    member.city = unknown_city
                    member.city_how = "unknown"
        if part.folder is None:
            times = [m.local for m in part.members if m.local is not None]
            part.folder = session_folder_name(min(times), max(times))  # type: ignore[arg-type]

    if untimed:
        notes.append(
            f"{len(untimed)} 个文件没有可用拍摄时间（EXIF 与文件时间都取不到），"
            f"已归入 {unknown_city} 下按文件名派生的时段目录，便于人工处理"
        )
    if override_city:
        notes.append(
            f"已按你指定的城市「{override_city}」归类（整批强制，压过 GPS 判定）"
        )
        if unresolved:
            # 境外坐标几乎必然全部未命中：让用户明白"指定城市确实覆盖了这些有 GPS 的文件"，
            # 而不是以为它们本来就没坐标。
            notes.append(
                f"其中 {len(unresolved)} 个 GPS 网格不在离线缓存里（境外坐标通常如此），"
                "已一并归入你指定的城市"
            )
        if disagreed:
            sample = "、".join(f"{key}：GPS 说 {city}" for key, city in sorted(disagreed)[:5])
            notes.append(
                f"注意：{len(disagreed)} 个文件的 GPS 判定与指定城市不同 —— {sample}"
                "。若这批素材其实横跨多个地点，请拆开分别导入。"
            )

    files = _build_targets(parts, untimed, reserved_relative)
    sessions = _build_sessions(parts)
    unknown_count = sum(1 for f in files if f.city == unknown_city)
    unknown_time = sum(1 for f in files if f.time_unknown)

    return ClassifyPlan(
        files=files,
        sessions=sessions,
        unresolved_cells=sorted(unresolved),
        unknown_city_count=unknown_count,
        unknown_time_count=unknown_time,
        city_override=override_city,
        city_override_disagreements=sorted(disagreed),
        notes=notes,
    )


# --------------------------------------------------------------------------- 内部
@dataclass
class _Member:
    item: ClassifyItem
    order: int
    city: str | None
    city_how: str
    local: datetime | None
    time_how: str
    time_unknown: bool


@dataclass
class _Part:
    members: list[_Member] = field(default_factory=list)
    city: str | None = None
    city_how: str = "pending"
    folder: str | None = None


def _time_blocks(members: Sequence[_Member], *, gap_hours: float) -> list[list[_Member]]:
    """pass 1：间隔超过 `gap_hours` 即切新块。"""
    gap = timedelta(hours=gap_hours)
    blocks: list[list[_Member]] = []
    current: list[_Member] = []
    end: datetime | None = None
    for member in members:
        assert member.local is not None
        if end is None or (member.local - end) > gap:
            current = [member]
            blocks.append(current)
        else:
            current.append(member)
        end = member.local if end is None or member.local > end else end
    return blocks


def _split_on_city_change(blocks: Sequence[Sequence[_Member]]) -> list[_Part]:
    """pass 2：块内按城市变化再切分。段首无城市的成员由段内首个已知城市向后继承。"""
    parts: list[_Part] = []
    for block in blocks:
        current: _Part | None = None
        last_city: str | None = None
        for member in block:
            if member.city:
                if current is None or (last_city and member.city != last_city):
                    current = _Part(city=member.city, city_how=member.city_how)
                    parts.append(current)
                last_city = member.city
                current.city = member.city
            elif current is None:
                current = _Part(city=None, city_how="pending")
                parts.append(current)
            current.members.append(member)
    return parts


def _inherit_city_in_part(parts: Sequence[_Part]) -> None:
    """段内继承：段首那些没有 GPS 的成员跟随本段的城市。"""
    for part in parts:
        if not part.city:
            continue
        for member in part.members:
            if not member.city:
                member.city = part.city
                member.city_how = "inherited-session"


def _infer_between(parts: Sequence[_Part]) -> None:
    """pass 3：整段无 GPS 时，仅当**前后相邻已定位段城市一致**且都在 24h 内才推断。"""
    limit = timedelta(hours=INFERRED_BETWEEN_MAX_HOURS)
    for index, part in enumerate(parts):
        if part.city:
            continue
        previous = next((p for p in reversed(parts[:index]) if p.city), None)
        following = next((p for p in parts[index + 1 :] if p.city), None)
        if previous is None or following is None or previous.city != following.city:
            continue
        own = [m.local for m in part.members if m.local is not None]
        prev_times = [m.local for m in previous.members if m.local is not None]
        next_times = [m.local for m in following.members if m.local is not None]
        if not own or not prev_times or not next_times:
            continue
        if (min(own) - max(prev_times)) > limit or (min(next_times) - max(own)) > limit:
            continue
        part.city = previous.city
        part.city_how = "inferred-between"
        for member in part.members:
            if not member.city:
                member.city = previous.city
                member.city_how = "inferred-between"


def _pick_anchor(
    part: _Part, anchors: Sequence[SessionAnchor], *, gap_hours: float, unknown_city: str
) -> SessionAnchor | None:
    """选一个可合并的既有锚点。

    规则：锚点必须落在「时间距离 ≤ gap_hours」窗口内，且**城市相符**；
    当新块城市未知时，任何锚点都可以（借它的城市）。

    偏差说明：旧实现里 `-or $cityMatches` 会让"同城的较远锚点"覆盖掉"同城的最近锚点"，
    结果取决于索引里的遍历顺序。这里改为**同城优先 + 时间最近**，
    只会在"本来就会并入同城目录"的前提下选出更准确的那个，不会制造重复目录。
    """
    if not part.members:
        return None
    times = [m.local for m in part.members if m.local is not None]
    if not times:
        return None
    start, end = min(times), max(times)
    window = timedelta(hours=gap_hours)

    best: SessionAnchor | None = None
    best_key: tuple[int, timedelta] | None = None
    for anchor in anchors:
        gap = anchor.gap_to(start, end)
        if gap > window:
            continue
        city_matches = part.city is not None and part.city != unknown_city and anchor.city == part.city
        city_unknown = part.city is None or part.city == unknown_city
        if not (city_matches or city_unknown):
            continue
        key = (0 if city_matches else 1, gap)
        if best_key is None or key < best_key:
            best, best_key = anchor, key
    return best


def _merge_into_anchors(
    parts: Sequence[_Part],
    anchors: Sequence[SessionAnchor],
    *,
    gap_hours: float,
    unknown_city: str,
) -> None:
    """pass 4：能并入既有目录的就并入，避免为同一时段建平行目录。"""
    if not anchors:
        for part in parts:
            if part.city is None:
                part.city = unknown_city
        return
    for part in parts:
        anchor = _pick_anchor(part, anchors, gap_hours=gap_hours, unknown_city=unknown_city)
        if anchor is None:
            if part.city is None:
                part.city = unknown_city
            continue
        if (part.city is None or part.city == unknown_city) and anchor.city:
            part.city = anchor.city
            part.city_how = "inherited-library"
            for member in part.members:
                if not member.city or member.city == unknown_city:
                    member.city = anchor.city
                    member.city_how = "inherited-library"
        part.folder = anchor.folder


def _dedupe_target(
    directory: str, name: str, reserved: set[str], used: set[str]
) -> tuple[str, str, str | None]:
    """同名冲突 → `stem__2.ext`、`stem__3.ext`…（**绝不覆盖**，与旧工具一致）。

    返回 `(最终文件名, 最终相对路径, 冲突的既有相对路径或 None)`。
    第三个返回值让调用方能对"同名"做**内容级**判定（同内容应跳过而不是改名复制）。
    """
    from pathlib import PurePosixPath

    base = PurePosixPath(name)
    stem, suffix = base.stem, base.suffix
    original = f"{directory}\\{name}"
    if original.lower() not in reserved and original.lower() not in used:
        used.add(original.lower())
        return name, original, None
    collided_with = original
    index = 2
    while index < 1000:
        alternate = f"{stem}__{index}{suffix}"
        candidate = f"{directory}\\{alternate}"
        if candidate.lower() not in reserved and candidate.lower() not in used:
            used.add(candidate.lower())
            return alternate, candidate, collided_with
        index += 1
    raise ValueError(f"无法为 {name} 找到可用名（已尝试 999 次）")


def _build_targets(
    parts: Sequence[_Part],
    untimed: Sequence[_Member],
    reserved_relative: Iterable[str],
) -> list[ClassifiedFile]:
    """pass 5：生成目标相对路径，并处理同名冲突。"""
    reserved = {str(path).replace("/", "\\").lower() for path in reserved_relative}
    used: set[str] = set()
    out: list[ClassifiedFile] = []

    for part in parts:
        assert part.city and part.folder
        directory = f"{part.city}\\{part.folder}"
        for member in sorted(part.members, key=lambda m: (m.local, m.item.name)):  # type: ignore[arg-type,return-value]
            name, target, collided = _dedupe_target(directory, member.item.name, reserved, used)
            out.append(
                ClassifiedFile(
                    key=member.item.key,
                    name=name,
                    kind=member.item.kind,
                    city=part.city,
                    city_how=member.city_how,
                    session=part.folder,
                    target_relative=target,
                    local_time=member.local,  # type: ignore[arg-type]
                    time_how=member.time_how,
                    size=member.item.size,
                    time_unknown=member.time_unknown,
                    collision_with=collided,
                )
            )

    # 无时间的文件：按文件名分组落到 `未知地点\<按文件名的稳定时段>`
    for member in untimed:
        stamp = (member.item.file_mtime or datetime(1970, 1, 1)).strftime("%Y-%m-%d_0000")
        directory = f"{UNKNOWN_CITY}\\{stamp}"
        name, target, collided = _dedupe_target(directory, member.item.name, reserved, used)
        out.append(
            ClassifiedFile(
                key=member.item.key,
                name=name,
                kind=member.item.kind,
                city=UNKNOWN_CITY,
                city_how="unknown",
                session=stamp,
                target_relative=target,
                local_time=member.item.file_mtime or datetime(1970, 1, 1),
                time_how=TIME_NONE,
                size=member.item.size,
                time_unknown=True,
                collision_with=collided,
            )
        )
    return out


def _build_sessions(parts: Sequence[_Part]) -> list[ClassifiedSession]:
    """把每个段汇成一个 `session` 行（Phase 2 的 Scene 可直接挂上去）。"""
    out: list[ClassifiedSession] = []
    for part in parts:
        times = [m.local for m in part.members if m.local is not None]
        if not times or not part.city or not part.folder:
            continue
        member_counts: dict[str, int] = {}
        for member in part.members:
            member_counts[member.city_how] = member_counts.get(member.city_how, 0) + 1
        best_how = max(member_counts.items(), key=lambda kv: kv[1])[0]
        out.append(
            ClassifiedSession(
                city=part.city,
                folder=part.folder,
                captured_from=min(times),
                captured_to=max(times),
                confidence=confidence_for_how(best_how),
                members=len(part.members),
                time_unknown=any(m.time_unknown for m in part.members),
            )
        )
    return out


# --------------------------------------------------------------------------- 锚点
def anchors_from_rows(
    rows: Iterable[tuple[str, str | None]], *, min_depth: int = 2
) -> list[SessionAnchor]:
    """从库内既有 asset 的 `(relative_path, capture_at)` 派生时段锚点。

    不需要额外索引：`relative_path` 本身编码了 `<城市>\\<时段>`，直接聚合即可。
    """
    buckets: dict[tuple[str, str], list[datetime]] = {}
    for relative, captured in rows:
        parts = str(relative).replace("/", "\\").split("\\")
        if len(parts) < min_depth + 1:
            continue
        city, folder = parts[0], parts[1]
        if not city or not folder:
            continue
        when = parse_wall_clock(captured)
        if when is None:
            continue
        buckets.setdefault((city, folder), []).append(when)
    return [
        SessionAnchor(city=city, folder=folder, start=min(times), end=max(times))
        for (city, folder), times in buckets.items()
    ]


__all__ = [
    "CITY_CONFIDENCE",
    "confidence_for_how",
    "DEFAULT_GAP_HOURS",
    "INFERRED_BETWEEN_MAX_HOURS",
    "TIME_CONTAINER_UTC",
    "TIME_EXIF",
    "TIME_EXIF_WALLCLOCK",
    "TIME_FILE_MTIME",
    "TIME_NONE",
    "TIME_VIDEO_INFERRED",
    "TRADITIONAL_TO_SIMPLIFIED",
    "UNKNOWN_CITY",
    "ClassifiedFile",
    "ClassifiedSession",
    "ClassifyItem",
    "ClassifyPlan",
    "SessionAnchor",
    "anchors_from_rows",
    "infer_video_local_times",
    "normalize_city_folder_name",
    "offset_minutes_from_text",
    "parse_wall_clock",
    "plan_classification",
    "session_folder_name",
]
