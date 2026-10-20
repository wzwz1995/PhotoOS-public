"""分类内核单元测试（M2）。

本文件守护的核心承诺只有一句：**新照片必须落进与既有库一致的目录**。
既有库是旧工具建出来的（18 个城市 / 71 个时段 / 16,105 张），因此这里
用它的**真实取值**做数据驱动断言（城市名、时段名、网格键都取自真实缓存与目录）。

最容易出事的两种失配：
  * 城市名不统一 → 同时存在 `万宁市` 与 `萬寧市`，同一座城市被劈成两半
  * 时段边界不一致 → 同一天被切成两个平行时段目录
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from photoos.library.classify import (
    TIME_EXIF,
    TIME_VIDEO_INFERRED,
    UNKNOWN_CITY,
    ClassifyItem,
    SessionAnchor,
    anchors_from_rows,
    infer_video_local_times,
    normalize_city_folder_name,
    offset_minutes_from_text,
    parse_wall_clock,
    plan_classification,
    session_folder_name,
)
from photoos.library.geo import CELL_DEG, GeoCache, GeoEntry, angular_distance_deg, cell_key

# 真实缓存里的原始地名（繁体/缺后缀）→ 既有库里的目录名
REAL_CITY_RENAMES = [
    ("萬寧市", "万宁市"),
    ("嘉峪關市", "嘉峪关市"),
    ("海口", "海口市"),
    ("鹰潭市", "鹰潭市"),          # 已是简体且带后缀 → 原样
    ("海北藏族自治州", "海北藏族自治州"),  # 以「州」结尾 → 不补「市」
    ("上饶市", "上饶市"),
    ("保定市", "保定市"),
]

# 真实缓存中的 (Cell, Lat, Lon)
REAL_CELLS = [
    ("18.650,110.200", 18.627962, 110.211597),
    ("20.050,110.300", 20.037407, 110.312801),
    ("28.050,117.050", 28.045022, 117.042444),
]


def _geo(*cells: tuple[str, float, float, str]) -> GeoCache:
    return GeoCache(
        {
            cell: GeoEntry(cell=cell, lat=lat, lon=lon, city=city, src="bigdatacloud")
            for cell, lat, lon, city in cells
        }
    )


def _item(
    key: str,
    when: str,
    *,
    lat: float | None = None,
    lon: float | None = None,
    kind: str = "photo",
    utc: str | None = None,
    offset: int | None = None,
) -> ClassifyItem:
    return ClassifyItem(
        key=key,
        name=f"{key}.JPG" if kind == "photo" else f"{key}.MOV",
        kind=kind,
        local_time=parse_wall_clock(when) if when else None,
        utc_time=parse_wall_clock(utc) if utc else None,
        offset_minutes=offset,
        time_how=TIME_EXIF if when else "none",
        lat=lat,
        lon=lon,
    )


# ============================================================ 网格：与旧工具逐位一致
@pytest.mark.parametrize("cell,lat,lon", REAL_CELLS)
def test_cell_key_matches_legacy_grid_math(cell: str, lat: float, lon: float) -> None:
    """旧缓存 62 条都与 `round(deg/0.05)*0.05` 一致；公式一改缓存命中率就会掉。"""
    assert cell_key(lat, lon) == cell
    assert CELL_DEG == 0.05


def test_geo_cache_exact_then_nearest(tmp_path) -> None:
    cache = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))

    exact = cache.lookup(18.627962, 110.211597)
    assert exact is not None and exact.how == "gps-cell"

    # 同一网格附近 → 仍然命中同一格
    near = cache.lookup(18.63, 110.21)
    assert near is not None and near.city == "萬寧市"

    # 远离缓存（> 0.6°）→ 拒绝判定，而不是硬猜一个城市
    assert cache.lookup(31.23, 121.47) is None


def test_geo_cache_ignores_zero_coordinates() -> None:
    """(0,0) 是「没有 GPS」的常见占位写法，不是几内亚湾。"""
    cache = _geo(("0.000,0.000", 0.0, 0.0, "某市"))
    assert cache.lookup(0.0, 0.0) is None


def test_geo_cache_roundtrip_is_format_compatible(tmp_path) -> None:
    """写出的文件必须能被旧工具/旧缓存同样的方式读回（含 BOM 与列名）。"""
    path = tmp_path / "geo-cache.csv"
    cache = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    cache.save(path)

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "旧工具导出带 BOM，保持兼容"
    assert b"Cell,Lat,Lon,City,Province,Locality,Src" in raw

    reloaded = GeoCache.load(path)
    assert len(reloaded) == 1
    assert reloaded.lookup(18.627962, 110.211597).city == "萬寧市"  # type: ignore[union-attr]


def test_geo_cache_load_missing_file_is_empty_not_error(tmp_path) -> None:
    assert len(GeoCache.load(tmp_path / "nope.csv")) == 0


# ============================================================ 区域指认
def test_assign_radius_fills_a_whole_area_in_one_command() -> None:
    """网格边长 0.05°（约 5.5 km），一趟旅行会覆盖几十上百格 —— 逐个指认不现实。

    "以某点为中心、半径 N 公里内都算这座城市"才是人的思维方式。
    """
    cache = GeoCache()
    written, skipped = cache.assign_radius(37.9838, 23.7275, 25, "雅典")

    assert written > 50, "25 km 半径应当覆盖几十个网格"
    assert skipped == 0
    assert len(cache) == written
    # 中心与边缘都要能命中
    assert cache.lookup(37.9838, 23.7275).city == "雅典"  # type: ignore[union-attr]
    assert cache.lookup(37.95, 23.80).city == "雅典"  # type: ignore[union-attr]
    # 半径之外**没有被写入**（用 exact 查，因为 lookup 还带 0.6° 就近匹配）
    assert cache.exact(38.60, 23.7275) is None
    assert cache.exact(37.20, 23.7275) is None


def test_assign_radius_covers_no_gps_free_lunch_but_reports_skips() -> None:
    """原有**机器反查**的条目不能被一次粗粒度区域指认悄悄抹掉。"""
    from photoos.library.geo import GeoEntry

    machine_cell = cell_key(37.9838, 23.7275)
    cache = GeoCache(
        {machine_cell: GeoEntry(cell=machine_cell, lat=37.9838, lon=23.7275, city="雅典", src="bigdatacloud")}
    )
    written, skipped = cache.assign_radius(37.9838, 23.7275, 5, "比雷埃夫斯")

    assert skipped >= 1, "机器反查的条目应被跳过而不是覆盖"
    assert cache.lookup(37.9838, 23.7275).city == "雅典"  # type: ignore[union-attr]
    assert written > 0, "其余空格子应当被写入"

    # 明确要覆盖时才覆盖（纠正单个格子的正确做法）
    written2, skipped2 = cache.assign_radius(
        37.9838, 23.7275, 5, "比雷埃夫斯", overwrite_machine=True
    )
    assert skipped2 == 0 and written2 > 0
    assert cache.lookup(37.9838, 23.7275).city == "比雷埃夫斯"  # type: ignore[union-attr]


def test_assign_radius_rejects_bad_input() -> None:
    cache = GeoCache()
    with pytest.raises(ValueError):
        cache.assign_radius(37.98, 23.73, 0, "雅典")
    with pytest.raises(ValueError):
        cache.assign_radius(37.98, 23.73, 10, "  ")


def test_angular_distance_compresses_longitude_by_latitude() -> None:
    """经度距离要按纬度余弦压缩，否则高纬度地区会误判到很远的城市。"""
    same_lat = angular_distance_deg(45.0, 0.0, 45.0, 1.0)
    assert same_lat == pytest.approx(0.7071, abs=1e-3)
    assert angular_distance_deg(45.0, 0.0, 46.0, 0.0) == pytest.approx(1.0)


# ============================================================ 城市名规范化
@pytest.mark.parametrize("raw,expected", REAL_CITY_RENAMES)
def test_normalize_city_folder_name(raw: str, expected: str) -> None:
    assert normalize_city_folder_name(raw) == expected


def test_normalize_keeps_unknown_placeholder_shape() -> None:
    # 「未知地点」以「点」结尾，不在后缀表里 → 会被补成「未知地点市」。
    # 我们**不**用它做规范化输入（UNKNOWN_CITY 是独立常量），这里把行为钉住以防误用。
    assert normalize_city_folder_name(UNKNOWN_CITY) == UNKNOWN_CITY + "市"


# ============================================================ 时段命名
def test_session_folder_name_same_day() -> None:
    start, end = datetime(2025, 11, 29, 6, 10), datetime(2025, 11, 29, 17, 47)
    assert session_folder_name(start, end) == "2025-11-29_0610-1747"


def test_session_folder_name_cross_day_same_year() -> None:
    start, end = datetime(2025, 9, 7, 22, 12), datetime(2025, 9, 8, 2, 0)
    assert session_folder_name(start, end) == "2025-09-07_2212_至_09-08_0200"


def test_session_folder_name_cross_year() -> None:
    start, end = datetime(2025, 12, 31, 22, 0), datetime(2026, 1, 1, 3, 0)
    assert session_folder_name(start, end) == "2025-12-31_2200_至_2026-01-01_0300"


def test_parse_wall_clock_drops_offset_without_shifting() -> None:
    """`capture_at` 本身就是本地时间，丢掉时区即可 —— 换算会把 06:10 变成前一天 22:10。"""
    assert parse_wall_clock("2025-11-29 06:10:01") == datetime(2025, 11, 29, 6, 10, 1)
    assert parse_wall_clock("2025-11-29T06:10:01+08:00") == datetime(2025, 11, 29, 6, 10, 1)
    assert parse_wall_clock(None) is None
    assert parse_wall_clock("not a time") is None


def test_offset_minutes_parsing() -> None:
    assert offset_minutes_from_text("+08:00") == 480
    assert offset_minutes_from_text("-05:30") == -330
    assert offset_minutes_from_text("+00:00") == 0
    assert offset_minutes_from_text(None) is None
    assert offset_minutes_from_text("08:00") is None


# ============================================================ 切段
def test_time_blocks_keep_exactly_gap_hours_together() -> None:
    """边界必须是「> 12h 才切」：正好 12 小时仍算同一段。"""
    base = datetime(2025, 11, 29, 6, 0)
    items = [
        _item("a", "2025-11-29 06:00"),
        _item("b", (base + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")),
    ]
    plan = plan_classification(items)
    assert len({f.session for f in plan.files}) == 1

    items = [
        _item("a", "2025-11-29 06:00"),
        _item("b", (base + timedelta(hours=12, seconds=1)).strftime("%Y-%m-%d %H:%M:%S")),
    ]
    plan = plan_classification(items)
    assert len({f.session for f in plan.files}) == 2


def test_city_change_splits_within_one_time_block() -> None:
    """同一时间块内城市变化也要切段（真实库规则：间隔 > 12h **或** 城市变化）。"""
    geo = _geo(
        ("18.650,110.200", 18.627962, 110.211597, "萬寧市"),
        ("20.050,110.300", 20.037407, 110.312801, "海口"),
    )
    items = [
        _item("a", "2025-11-29 06:00", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-29 06:30", lat=20.037407, lon=110.312801),
    ]
    plan = plan_classification(items, geo=geo)
    assert {f.city for f in plan.files} == {"万宁市", "海口市"}
    assert len({(f.city, f.session) for f in plan.files}) == 2


def test_gps_less_members_inherit_session_city() -> None:
    """段内其他照片有 GPS → 整段继承（既有库里大量无 GPS 照片就是这样落位的）。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    items = [
        _item("a", "2025-11-29 06:00"),
        _item("b", "2025-11-29 06:10", lat=18.627962, lon=110.211597),
        _item("c", "2025-11-29 06:20"),
    ]
    plan = plan_classification(items, geo=geo)
    assert {f.city for f in plan.files} == {"万宁市"}
    hows = {f.key: f.city_how for f in plan.files}
    assert hows["b"] == "gps-cell"
    assert hows["a"] == "inherited-session" and hows["c"] == "inherited-session"


def test_member_sandwiched_inside_located_block_inherits_session() -> None:
    """块内夹着的无 GPS 成员走 `inherited-session`（不是 inferred-between）。

    旧工具的语义：`inferred-between` 只用于**整个时间块都没有 GPS** 的情况
    （"blocks with no GPS at all"）。块内已有定位成员时，无 GPS 成员直接跟随本块城市。
    """
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    items = [
        _item("a", "2025-11-29 06:00", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-29 06:30"),
        _item("c", "2025-11-29 07:00", lat=18.627962, lon=110.211597),
    ]
    plan = plan_classification(items, geo=geo)
    assert {f.city for f in plan.files} == {"万宁市"}
    assert next(f for f in plan.files if f.key == "b").city_how == "inherited-session"


def test_inferred_between_requires_both_neighbour_blocks_to_agree() -> None:
    """整块无 GPS：仅当前后两个**已定位块**城市一致且都在 24h 内才推断。

    时间刻意让三块两两间隔 > 12h（因此是三个独立块），但都 ≤ 24h（因此允许推断）。
    """
    geo = _geo(
        ("18.650,110.200", 18.627962, 110.211597, "萬寧市"),
        ("20.050,110.300", 20.037407, 110.312801, "海口"),
    )
    agree = [
        _item("a", "2025-11-29 06:00", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-29 20:00"),                       # 距上一块 14h → 独立块
        _item("c", "2025-11-30 09:00", lat=18.627962, lon=110.211597),  # 距上一块 13h → 独立块
    ]
    plan = plan_classification(agree, geo=geo)
    assert {f.city for f in plan.files} == {"万宁市"}
    assert next(f for f in plan.files if f.key == "b").city_how == "inferred-between"

    disagree = [
        _item("a", "2025-11-29 06:00", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-29 20:00"),
        _item("c", "2025-11-30 09:00", lat=20.037407, lon=110.312801),
    ]
    plan = plan_classification(disagree, geo=geo)
    assert next(f for f in plan.files if f.key == "b").city == UNKNOWN_CITY


def test_inferred_between_respects_24h_limit() -> None:
    """前后间隔超过 24 小时就不该推断（旧工具的 `-near` 判断）。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    items = [
        _item("a", "2025-11-27 06:00", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-28 20:00"),                       # 距上一块 38h → 超限
        _item("c", "2025-11-30 09:00", lat=18.627962, lon=110.211597),
    ]
    plan = plan_classification(items, geo=geo)
    assert next(f for f in plan.files if f.key == "b").city == UNKNOWN_CITY


def test_unknown_city_lands_in_unknown_folder() -> None:
    plan = plan_classification([_item("a", "2025-11-29 06:00")])
    assert plan.files[0].city == UNKNOWN_CITY
    assert plan.files[0].target_relative.startswith(UNKNOWN_CITY + "\\")
    assert plan.unknown_city_count == 1


# ============================================================ 锚点合并（最关键）
def _anchor(city: str, folder: str, start: str, end: str) -> SessionAnchor:
    return SessionAnchor(
        city=city, folder=folder, start=parse_wall_clock(start), end=parse_wall_clock(end)  # type: ignore[arg-type]
    )


def test_anchor_merge_reuses_existing_folder_instead_of_creating_parallel_one() -> None:
    """**最重要的行为**：库里已有同日时段时，新照片必须并入它，不能新建平行目录。

    否则同一天会出现 `2025-11-29_0610-1747` 与 `2025-11-29_1800-1900` 两个目录，
    既有的 71 个时段会被逐渐打碎。
    """
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    anchors = [_anchor("万宁市", "2025-11-29_0610-1747", "2025-11-29 06:10", "2025-11-29 17:47")]

    late = [_item("a", "2025-11-29 18:00", lat=18.627962, lon=110.211597)]
    plan = plan_classification(late, geo=geo, anchors=anchors)

    assert plan.files[0].session == "2025-11-29_0610-1747", "必须沿用既有目录名"
    assert plan.files[0].target_relative == "万宁市\\2025-11-29_0610-1747\\a.JPG"
    assert len(plan.sessions) == 1
    assert plan.sessions[0].folder == "2025-11-29_0610-1747"


def test_anchor_merge_does_not_reuse_across_cities() -> None:
    """城市不同的锚点即使时间相邻也不能借用 —— 那会把照片放进错误的城市目录。"""
    geo = _geo(("20.050,110.300", 20.037407, 110.312801, "海口"))
    anchors = [_anchor("万宁市", "2025-11-29_0610-1747", "2025-11-29 06:10", "2025-11-29 17:47")]

    plan = plan_classification(
        [_item("a", "2025-11-29 18:00", lat=20.037407, lon=110.312801)],
        geo=geo,
        anchors=anchors,
    )
    assert plan.files[0].city == "海口市"
    assert plan.files[0].session != "2025-11-29_0610-1747"


def test_anchor_merge_prefers_nearest_same_city_anchor() -> None:
    """同城有多个候选锚点时取**时间最近**的那个。

    这是与旧工具的**有意偏差**：旧实现里 `-or $cityMatches` 会让遍历顺序上最后一个
    同城锚点覆盖掉更近的那个，结果依赖索引顺序。按最近选择只会更准，不会造重复目录。
    """
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    anchors = [
        _anchor("万宁市", "2025-11-28_0600-0800", "2025-11-28 06:00", "2025-11-28 08:00"),
        _anchor("万宁市", "2025-11-29_0610-1747", "2025-11-29 06:10", "2025-11-29 17:47"),
    ]
    plan = plan_classification(
        [_item("a", "2025-11-29 18:00", lat=18.627962, lon=110.211597)],
        geo=geo,
        anchors=anchors,
    )
    assert plan.files[0].session == "2025-11-29_0610-1747"


def test_unknown_city_block_borrows_city_from_nearby_anchor() -> None:
    """无 GPS 的新照片，若时间紧邻某个既有锚点，可借用它的城市（旧工具 `inherited-library`）。"""
    anchors = [_anchor("万宁市", "2025-11-29_0610-1747", "2025-11-29 06:10", "2025-11-29 17:47")]
    plan = plan_classification([_item("a", "2025-11-29 18:00")], anchors=anchors)

    assert plan.files[0].city == "万宁市"
    assert plan.files[0].city_how == "inherited-library"
    assert plan.files[0].session == "2025-11-29_0610-1747"


def test_anchor_far_in_time_is_not_merged() -> None:
    anchors = [_anchor("万宁市", "2025-11-29_0610-1747", "2025-11-29 06:10", "2025-11-29 17:47")]
    plan = plan_classification([_item("a", "2025-12-20 10:00")], anchors=anchors)
    assert plan.files[0].session == "2025-12-20_1000-1000"


def test_anchors_from_rows_derives_ranges_from_relative_paths() -> None:
    rows = [
        ("万宁市\\2025-11-29_0610-1747\\DSC_1.JPG", "2025-11-29 06:10:01"),
        ("万宁市\\2025-11-29_0610-1747\\DSC_2.JPG", "2025-11-29 17:47:00"),
        ("杭州市\\2025-10-01_0840-1115\\DSC_3.JPG", "2025-10-01 08:40:00"),
        ("松散文件.JPG", "2025-10-01 09:00:00"),  # 不在 <城市>\<时段> 下 → 忽略
    ]
    anchors = {a.folder: a for a in anchors_from_rows(rows)}
    assert set(anchors) == {"2025-11-29_0610-1747", "2025-10-01_0840-1115"}
    assert anchors["2025-11-29_0610-1747"].start == datetime(2025, 11, 29, 6, 10, 1)
    assert anchors["2025-11-29_0610-1747"].end == datetime(2025, 11, 29, 17, 47, 0)


# ============================================================ 同名冲突
def test_collision_gets_suffix_and_never_overwrites() -> None:
    """同一时段内两个同名文件 → 第二个加 `__2`（既有库里有 6,443 个这样的文件）。"""
    items = [_item("a", "2025-11-29 06:00"), _item("a", "2025-11-29 06:01")]
    plan = plan_classification(items)
    assert sorted(f.target_relative for f in plan.files) == [
        "未知地点\\2025-11-29_0600-0601\\a.JPG",
        "未知地点\\2025-11-29_0600-0601\\a__2.JPG",
    ]


def test_collision_against_existing_library_paths() -> None:
    """目标库里已经有同名文件时，预演阶段就要改名，不能等到复制时才发现。"""
    plan = plan_classification(
        [_item("a", "2025-11-29 06:00")],
        reserved_relative={"未知地点\\2025-11-29_0600-0600\\a.JPG"},
    )
    assert plan.files[0].name == "a__2.JPG"
    assert plan.files[0].target_relative.endswith("a__2.JPG")


# ============================================================ 视频时区
def test_video_timezone_borrowed_from_nearest_photo() -> None:
    """视频只有容器 UTC；偏移必须取自同批次时间最近的**带偏移照片**（证据，不是猜测）。"""
    items = [
        ClassifyItem(
            key="p1", name="p1.JPG", local_time=parse_wall_clock("2025-11-29 06:10"),
            utc_time=parse_wall_clock("2025-11-28 22:10"), offset_minutes=480, time_how=TIME_EXIF,
        ),
        ClassifyItem(
            key="v1", name="v1.MOV", kind="video",
            utc_time=parse_wall_clock("2025-11-28 22:48"), time_how="container-utc",
        ),
    ]
    resolved, notes = infer_video_local_times(items)
    video = next(i for i in resolved if i.kind == "video")
    assert video.local_time == datetime(2025, 11, 29, 6, 48)
    assert video.time_how == TIME_VIDEO_INFERRED
    assert notes


def test_video_without_reference_photo_stays_time_unknown() -> None:
    """纯视频批次不猜时区：保持在 UTC，并在报告里标为时区未知（用户已确认此策略）。"""
    items = [
        ClassifyItem(
            key="v1", name="v1.MOV", kind="video",
            utc_time=parse_wall_clock("2025-11-28 22:48"), time_how="container-utc",
        )
    ]
    resolved, notes = infer_video_local_times(items)
    assert resolved[0].local_time is None
    assert not notes


def test_video_tz_inference_lands_video_in_same_session_as_its_photos() -> None:
    """端到端：视频与同段照片必须落进同一个时段目录（否则一次拍摄被劈成两半）。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    items = [
        _item("p1", "2025-11-29 06:10", lat=18.627962, lon=110.211597),
        ClassifyItem(
            key="v1", name="v1.MOV", kind="video",
            utc_time=parse_wall_clock("2025-11-28 22:48"), time_how="container-utc",
        ),
    ]
    # 把偏移挂在照片上（真实数据里来自 EXIF）
    items[0] = ClassifyItem(**{**items[0].__dict__, "offset_minutes": 480,
                               "utc_time": parse_wall_clock("2025-11-28 22:10")})
    plan = plan_classification(items, geo=geo)

    sessions = {f.key: (f.city, f.session) for f in plan.files}
    assert sessions["v1"] == sessions["p1"] == ("万宁市", "2025-11-29_0610-0648")


def test_session_span_covers_both_photos_and_videos() -> None:
    """**联合切段**：时段的时间跨度必须覆盖照片与视频两者。

    实测证据：既有库 71 个时段目录里有 3 个只用照片无法复现 ——
    例如 `杭州市\\2025-09-07_2212_至_09-08_0200` 的起点 22:12 来自视频
    （照片只到 01:29）。照片+视频合并跨度才能 71/71 逐字复现既有目录名。

    失配的代价：同一段拍摄会在照片盘与视频盘上拿到不同目录名，
    下次导入时还会再切出平行时段。
    """
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    photo_early = _item("p1", "2025-11-29 06:10", lat=18.627962, lon=110.211597)
    photo_late = _item("p2", "2025-11-29 07:00", lat=18.627962, lon=110.211597)
    photo_early = ClassifyItem(**{**photo_early.__dict__, "offset_minutes": 480,
                                  "utc_time": parse_wall_clock("2025-11-28 22:10")})
    # 视频时间晚于所有照片：只看照片会得到 0610-0700，带上视频才是 0610-1747
    video = ClassifyItem(
        key="v1", name="v1.MOV", kind="video",
        utc_time=parse_wall_clock("2025-11-29 09:47"), time_how="container-utc",
    )
    plan = plan_classification([photo_early, photo_late, video], geo=geo)

    assert {f.session for f in plan.files} == {"2025-11-29_0610-1747"}
    assert plan.sessions[0].captured_to == datetime(2025, 11, 29, 17, 47)
    # 视频借到了 +08:00，因此与照片同段而不是自成一段
    assert next(f for f in plan.files if f.key == "v1").time_how == TIME_VIDEO_INFERRED


def test_unknown_time_videos_are_flagged_not_silently_placed() -> None:
    """时区未知的文件必须被计数并标注，不能悄悄放进一个看起来正常的时间段。"""
    items = [
        ClassifyItem(
            key="v1", name="v1.MOV", kind="video",
            utc_time=parse_wall_clock("2025-11-28 22:48"), time_how="container-utc",
        )
    ]
    plan = plan_classification(items)
    assert plan.unknown_time_count == 1
    assert plan.files[0].time_unknown is True


# ============================================================ session 行
def test_sessions_are_emitted_for_phase2_handoff() -> None:
    """每个 `<城市>\\<时段>` 都要产出一行 session（Phase 2 的 Scene 直接挂上去）。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    items = [
        _item("a", "2025-11-29 06:10", lat=18.627962, lon=110.211597),
        _item("b", "2025-11-29 07:00"),
    ]
    plan = plan_classification(items, geo=geo)
    assert len(plan.sessions) == 1
    session = plan.sessions[0]
    assert session.city == "万宁市"
    assert session.folder == "2025-11-29_0610-0700"
    assert session.captured_from == datetime(2025, 11, 29, 6, 10)
    assert session.captured_to == datetime(2025, 11, 29, 7, 0)
    assert session.members == 2
    assert 0.0 < session.confidence <= 1.0


def test_unresolved_cells_are_reported_for_manual_assignment() -> None:
    """未命中的 GPS 网格要列出来，供 `photoos geo assign` 人工指认后回写缓存。"""
    plan = plan_classification([_item("a", "2025-11-29 06:00", lat=31.2304, lon=121.4737)])
    assert plan.unresolved_cells == [cell_key(31.2304, 121.4737)]
    assert plan.files[0].city == UNKNOWN_CITY


# ============================================================ 整批强制城市
def test_city_override_handles_files_without_any_gps() -> None:
    """**相机没连手机定位时整段没有 GPS** —— 这时任何坐标反查都无从下手。

    实测现场：用户从存储卡导入 30 张希腊拍的照片，卡上确实有 74% 带 GPS（雅典一带），
    但走查顺序最前的那 30 张恰好整段没有 GPS，于是预演全部落到「未知地点」。
    `geo assign` 帮不上忙（没有坐标可指认），只能由人指定城市。
    """
    items = [_item("a", "2026-10-02 18:58"), _item("b", "2026-10-02 18:59")]
    plan = plan_classification(items, geo=_geo(), city_override="雅典")

    assert {f.city for f in plan.files} == {"雅典市"}, "无 GPS 的文件也必须归到指定城市"
    assert plan.files[0].city_how == "manual-batch"
    assert plan.city_override == "雅典市"
    assert plan.unknown_city_count == 0


def test_city_override_beats_gps_judgement() -> None:
    """境外坐标不在离线缓存里 —— 指定城市应当直接生效，而不是继续落到未知地点。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    athens = _item("a", "2026-10-02 18:58", lat=37.5, lon=23.5)
    plan = plan_classification([athens], geo=geo, city_override="雅典")
    assert {f.city for f in plan.files} == {"雅典市"}

    # 不加指定时：缓存里最近的也在数千公里外 → 未知地点（这就是用户看到的现象）
    plain = plan_classification([athens], geo=geo)
    assert plain.files[0].city == UNKNOWN_CITY
    assert plain.unresolved_cells == [cell_key(37.5, 23.5)]


def test_city_override_normalizes_name_like_other_folders() -> None:
    """目录命名约定是「以 市/州/… 结尾」，指定值走同一条规范化，保持库内一致。"""
    for raw, expected in (("雅典", "雅典市"), ("雅典市", "雅典市"), ("希腊", "希腊市")):
        plan = plan_classification([_item("a", "2026-10-02 18:58")], city_override=raw)
        assert plan.files[0].city == expected


def test_city_override_reports_disagreements_with_gps() -> None:
    """强制是人的决定，但**人不该在看不到分歧的情况下决定** —— 分歧必须列出来。"""
    geo = _geo(
        ("18.650,110.200", 18.627962, 110.211597, "萬寧市"),
        ("20.050,110.300", 20.037407, 110.312801, "海口"),
    )
    items = [
        _item("a", "2026-10-02 18:58", lat=18.627962, lon=110.211597),  # GPS: 万宁市
        _item("b", "2026-10-02 18:59", lat=20.037407, lon=110.312801),  # GPS: 海口市
    ]
    plan = plan_classification(items, geo=geo, city_override="雅典")

    assert {f.city for f in plan.files} == {"雅典市"}, "指定城市压过 GPS"
    assert len(plan.city_override_disagreements) == 2
    assert any("GPS 说 万宁市" in note for note in plan.notes), "要提示用户核对"
    assert any("拆开分别导入" in note for note in plan.notes), "要给出应对办法"


def test_city_override_absent_keeps_gps_behaviour_unchanged() -> None:
    """回归：不指定城市时行为**完全不变**（不能因为加了这个开关就影响默认路径）。"""
    geo = _geo(("18.650,110.200", 18.627962, 110.211597, "萬寧市"))
    plan = plan_classification(
        [_item("a", "2025-11-29 06:00", lat=18.627962, lon=110.211597)], geo=geo
    )
    assert plan.files[0].city == "万宁市"
    assert plan.files[0].city_how == "gps-cell"
    assert plan.city_override is None
    assert plan.city_override_disagreements == []


def test_city_override_does_not_break_time_blocking() -> None:
    """同一城市的相邻拍摄仍应切成一个时段（强制城市不该让切段失效）。"""
    items = [_item("a", "2026-10-02 18:58"), _item("b", "2026-10-02 19:30")]
    plan = plan_classification(items, city_override="雅典")
    assert len({f.session for f in plan.files}) == 1
    assert all(f.session.startswith("2026-10-02_1858") for f in plan.files)


# ====================================================== 置信度查表（修过的缺陷）

def test_confidence_for_how_handles_the_distance_suffix() -> None:
    """★ `gps-near` 带距离后缀时也要查到 0.8，不能落到兜底值。

    `geo.nearest()` 返回的 `how` 是 `gps-near0.12`（**带距离**，供界面显示
    "为什么归到这个城市"），而 `CITY_CONFIDENCE` 的键是 `"gps-near"`。
    早先直接 `CITY_CONFIDENCE.get(how, 0.5)` 查表 —— 永远匹配不上，
    静默落到兜底值。

    这个缺陷在真实数据上从未触发（本机所有 GPS 都能网格直查命中，
    `nearest()` 一次都没走到），所以它是**潜伏**的：一旦有人用了缓存边缘的坐标，
    置信度就从 0.8 变成兜底值。测试比真实数据更能守住它。
    """
    from photoos.library.classify import confidence_for_how

    assert confidence_for_how("gps-near0.12") == 0.8
    assert confidence_for_how("gps-near") == 0.8
    assert confidence_for_how("gps-near12.90") == 0.8
    # 其余来源照常
    assert confidence_for_how("gps-cell") == 1.0
    assert confidence_for_how("manual-batch") == 1.0
    assert confidence_for_how("inherited-session") == 0.6
    assert confidence_for_how("unknown") == 0.0


def test_confidence_fallback_is_below_every_named_source() -> None:
    """未知来源的兜底值必须**低于**所有具名来源。

    取 0.5 会让"来源不明"和"推断出来的"（inferred-between = 0.5）一样可信，
    那会让用户在预演表格里分不出两者。
    """
    from photoos.library.classify import CITY_CONFIDENCE, confidence_for_how

    fallback = confidence_for_how("something-unknown")
    named = [v for v in CITY_CONFIDENCE.values() if v > 0]
    assert fallback < min(named), (
        f"兜底值 {fallback} 不低于最低的具名来源 {min(named)}"
    )
    assert confidence_for_how(None) == fallback
    assert confidence_for_how("") == fallback
