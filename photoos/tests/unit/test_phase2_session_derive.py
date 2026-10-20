"""Phase 2 · M1：Session 回填的派生逻辑测试。

核心断言来自 `PHASE2_RESEARCH.md` §8 的**实测**：
  * 借偏移：同日命名时段 **67/67 逐字相符**
  * 不借偏移：**53/67**（14 个不符）
  * 希腊市混用时区的时段：逐条取最近才对，取众数会错

这些测试用**真实库的数字与真实目录名**做夹具，因此它们是"算法对不对"的证据，
而不只是"代码能不能跑"。
"""

from __future__ import annotations

import datetime as dt

import pytest

from photoos.library import session_derive as sd


def _photo(path: str, capture_at: str, tz: str | None = "+08:00") -> sd.AssetTime:
    return sd.AssetTime(1, path, "jpeg", capture_at=capture_at, tz_offset=tz)


def _video(path: str, utc: str) -> sd.AssetTime:
    return sd.AssetTime(2, path, "video", capture_at_utc=utc)


# ============================================================ 解析

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2025-11-29 06:10:01", dt.datetime(2025, 11, 29, 6, 10, 1)),
        ("2025-11-29T06:10:01", dt.datetime(2025, 11, 29, 6, 10, 1)),
        (None, None),
        ("", None),
        ("   ", None),
        ("垃圾", None),
    ],
)
def test_parse_wall_clock(text: str | None, expected: dt.datetime | None) -> None:
    assert sd.parse_wall_clock(text) == expected


@pytest.mark.parametrize(
    "text,minutes",
    [("+08:00", 480), ("-03:30", -210), ("+00:00", 0), (None, None), ("UTC", None), ("", None)],
)
def test_offset_from_tz(text: str | None, minutes: int | None) -> None:
    out = sd.offset_from_tz(text)
    if minutes is None:
        assert out is None
    else:
        assert out == dt.timedelta(minutes=minutes)


def test_tz_offset_is_never_guessed() -> None:
    """决策：借不到偏移就记时区未知，**绝不默认 +08:00**（Phase 1 决策 #3 的延续）。"""
    assert sd.offset_from_tz("UTC+8") is None
    assert sd.offset_from_tz("+8:00") is None  # 必须两位小时


# ============================================================ 目录名基准

def test_folder_name_matches_span_same_day() -> None:
    ok = sd.folder_name_matches_span(
        "2025-11-29_0610-1747",
        dt.datetime(2025, 11, 29, 6, 10, 1),
        dt.datetime(2025, 11, 29, 17, 47, 22),
    )
    assert ok is True


def test_folder_name_matches_span_detects_mismatch() -> None:
    bad = sd.folder_name_matches_span(
        "2025-11-29_0610-1747",
        dt.datetime(2025, 11, 28, 22, 48, 26),   # 这正是"不借偏移"时的错误起点
        dt.datetime(2025, 11, 29, 17, 47, 22),
    )
    assert bad is False


def test_folder_name_matches_span_returns_none_for_cross_day() -> None:
    """跨天命名（含 `_至_`）无法用同日基准检验，必须返回 None 而不是 False。"""
    assert sd.folder_name_matches_span(
        "2025-09-07_2212_至_09-08_0200",
        dt.datetime(2025, 9, 7, 22, 12),
        dt.datetime(2025, 9, 8, 2, 0),
    ) is None
    assert sd.is_cross_day_name("2025-09-07_2212_至_09-08_0200") is True
    assert sd.is_cross_day_name("2025-11-29_0610-1747") is False


# ============================================================ ★ 借偏移 vs 不借（真实案例）

#: 真实数据：万宁市该时段 606 个文件（602 照片 + 4 视频）。
#: 视频容器 UTC 最早 2025-11-28 22:48:26；借到 +08:00 后应为 2025-11-29 06:10:26。
#: 目录名 `2025-11-29_0610-1747` 是 ground truth。
_WANNING = [
    _photo("万宁市\\2025-11-29_0610-1747\\DSC_4816.JPG", "2025-11-29 06:10:01"),
    _photo("万宁市\\2025-11-29_0610-1747\\DSC_5100.JPG", "2025-11-29 17:47:22"),
    _video("万宁市\\2025-11-29_0610-1747\\DSC_4858.MOV", "2025-11-28 22:48:26"),
]


def test_borrowing_offset_reproduces_folder_name() -> None:
    """★ 本模块的核心断言：借偏移后，派生的时间跨度与目录名逐字相符。"""
    sessions = sd.derive_sessions(_WANNING)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.captured_from == dt.datetime(2025, 11, 29, 6, 10, 1)
    assert s.captured_to == dt.datetime(2025, 11, 29, 17, 47, 22)
    assert sd.folder_name_matches_span(s.period, s.captured_from, s.captured_to) is True


def test_not_borrowing_offset_would_be_wrong() -> None:
    """反证：把视频 UTC 当本地时间会得到 2025-11-28 22:48，与目录名不符。

    这条测试把"为什么必须借偏移"钉成可执行证据（研究阶段实测：15 个时段受影响）。
    """
    naive_from = dt.datetime(2025, 11, 28, 22, 48, 26)
    assert sd.folder_name_matches_span(
        "2025-11-29_0610-1747", naive_from, dt.datetime(2025, 11, 29, 17, 47, 22)
    ) is False
    # 而借偏移后的起点恰好是照片最早时间（因为视频借完落在照片区间之内）
    assert naive_from != dt.datetime(2025, 11, 29, 6, 10, 1)


def test_video_extends_range_when_it_is_genuinely_later() -> None:
    """视频确实晚于所有照片时，它应当把 captured_to 推后 —— 借偏移不是"忽略视频"。"""
    items = [
        _photo("城\\2026-05-02_0929-1115\\a.jpg", "2026-05-02 09:29:42"),
        _video("城\\2026-05-02_0929-1115\\b.MOV", "2026-05-02 03:15:02"),  # +8h -> 11:15:02
    ]
    s = sd.derive_sessions(items)[0]
    assert s.captured_to == dt.datetime(2026, 5, 2, 11, 15, 2)
    assert sd.folder_name_matches_span(s.period, s.captured_from, s.captured_to) is True


# ============================================================ ★ 逐条取最近 vs 取众数

def test_borrows_from_nearest_photo_not_period_mode() -> None:
    """★ 希腊市混用时区的真实情形：必须逐条取"时间最近"，不能取时段众数。

    构造：时段内 3 张 +03:00 的照片（众数）与 1 张 +08:00 的照片。
    视频的 UTC 时间**最接近那张 +08:00 的照片** —— 因此正确的偏移是 +08:00。
    若按众数（+03:00）换算，结果会差 5 小时。
    """
    items = [
        # +03:00 的三张（在时段里占多数）
        _photo("希腊市\\2026-10-03_1030-1938\\a.jpg", "2026-10-03 10:30:33", "+03:00"),
        _photo("希腊市\\2026-10-03_1030-1938\\b.jpg", "2026-10-03 11:00:00", "+03:00"),
        _photo("希腊市\\2026-10-03_1030-1938\\c.jpg", "2026-10-03 11:30:00", "+03:00"),
        # 一张 +08:00（少数派，但时间上与视频最近）
        _photo("希腊市\\2026-10-03_1030-1938\\d.jpg", "2026-10-03 19:38:14", "+08:00"),
        # 视频：UTC 11:38:14 → +08:00 得 19:38:14（贴近那张少数派照片的本地时间）
        _video("希腊市\\2026-10-03_1030-1938\\v.MOV", "2026-10-03 11:38:14"),
    ]
    s = sd.derive_sessions(items)[0]
    assert s.borrowed_offsets == ["+08:00"], (
        f"应借到 +08:00（时间最近），实际借到 {s.borrowed_offsets} —— 像是取了众数"
    )
    assert s.captured_to == dt.datetime(2026, 10, 3, 19, 38, 14)


def test_mixed_offsets_lower_confidence() -> None:
    """决策 #11：混用多个 tz_offset 时**不判断**，但必须降置信度让界面能标注。"""
    items = [
        _photo("希腊市\\2026-10-02_1645-2128\\a.jpg", "2026-10-02 16:45:30", "+03:00"),
        _photo("希腊市\\2026-10-02_1645-2128\\b.jpg", "2026-10-02 21:28:01", "+08:00"),
    ]
    s = sd.derive_sessions(items)[0]
    assert s.mixed_offsets is True
    assert s.distinct_offsets == ["+03:00", "+08:00"]
    assert s.confidence == sd.CONFIDENCE_MIXED_OFFSET


def test_single_offset_keeps_full_confidence() -> None:
    s = sd.derive_sessions(_WANNING)[0]
    assert s.mixed_offsets is False
    assert s.confidence == sd.CONFIDENCE_DEFAULT


# ============================================================ 边界情形

def test_video_without_reference_photo_is_left_unknown() -> None:
    """时段里没有带偏移的照片 → 视频借不到偏移，**不猜**，并降置信度。"""
    items = [
        _photo("城\\2026-01-01_1000-1100\\a.jpg", "2026-01-01 10:00:00", tz=None),
        _photo("城\\2026-01-01_1000-1100\\b.jpg", "2026-01-01 11:00:00", tz=None),
        _video("城\\2026-01-01_1000-1100\\v.MOV", "2026-01-01 02:00:00"),
    ]
    s = sd.derive_sessions(items)[0]
    assert s.videos_without_offset == 1
    assert s.borrowed_offsets == []
    assert s.confidence == sd.CONFIDENCE_NO_OFFSET
    # ★ 视频**没有**被计入时间范围 —— 否则会把它 UTC 的 02:00 当成本地时间，
    #   让 captured_from 凭空前移 8 小时（这正是研究阶段实测到的 15 个时段的错误）
    assert s.captured_from == dt.datetime(2026, 1, 1, 10, 0, 0)
    assert s.captured_to == dt.datetime(2026, 1, 1, 11, 0, 0)


def test_period_with_only_videos_uses_utc_and_marks_unknown() -> None:
    """纯视频时段（导入链路允许出现）：用 UTC 记范围但标未知，且不崩。"""
    items = [_video("城\\2026-01-01_1000-1100\\v.MOV", "2026-01-01 02:00:00")]
    s = sd.derive_sessions(items)[0]
    assert s.photo_count == 0
    assert s.video_count == 1
    assert s.videos_without_offset == 1
    assert s.confidence == sd.CONFIDENCE_NO_OFFSET


def test_shallow_paths_are_grouped_under_root() -> None:
    """单层路径也要派生 Session：平铺文件夹是支持的，不能被静默丢弃。"""
    items = [sd.AssetTime(1, "只有一层.jpg", "jpeg", capture_at="2026-01-01 10:00:00")]
    sessions = sd.derive_sessions(items)
    assert len(sessions) == 1
    assert sessions[0].city == "(未分类)"
    assert sessions[0].period == "(根目录)"
    assert sessions[0].total == 1


def test_empty_input() -> None:
    assert sd.derive_sessions([]) == []


def test_photos_and_videos_share_one_session_across_libraries() -> None:
    """照片与视频**共享同一个时段名**（Phase 1 §1.3.6 的联合切段模型）。

    `session` 表是全局的（没有 library_id），本函数也按 `(city, period)` 聚合，
    因此一个 `<城市>\\<时段>` 只会产出一行 —— 这正是断点 #5 的前提。
    """
    items = [
        _photo("万宁市\\2025-11-29_0610-1747\\a.JPG", "2025-11-29 06:10:01"),
        _video("万宁市\\2025-11-29_0610-1747\\v.MOV", "2025-11-28 22:48:26"),
    ]
    # library_id 分别是 1 和 2
    assert items[0].library_id == 1 and items[1].library_id == 2
    sessions = sd.derive_sessions(items)
    assert len(sessions) == 1
    assert sessions[0].total == 2


def test_derivation_is_repeatable() -> None:
    """跑两次结果必须完全一致（回填的可重复性是方案 §6.4 的硬要求）。"""
    first = [s.to_row() for s in sd.derive_sessions(_WANNING)]
    second = [s.to_row() for s in sd.derive_sessions(_WANNING)]
    assert first == second


def test_to_row_shape_matches_session_repo_expectations() -> None:
    row = sd.derive_sessions(_WANNING)[0].to_row()
    assert row["city_hint"] == "万宁市"
    assert row["period_hint"] == "2025-11-29_0610-1747"
    assert row["name"] == "万宁市\\2025-11-29_0610-1747"
    assert row["source"] == "auto"
    assert row["captured_from"] == "2025-11-29 06:10:01"
    assert row["captured_to"] == "2025-11-29 17:47:22"
    assert isinstance(row["confidence"], float)


def test_as_classified_reuses_phase1_shape() -> None:
    """复用 Phase 1 的 `ClassifiedSession`，避免两套几乎相同的结构。"""
    s = sd.derive_sessions(_WANNING)[0].as_classified()
    assert s.city == "万宁市"
    assert s.folder == "2025-11-29_0610-1747"
    assert s.members == 3
    assert s.time_unknown is False


# ============================================================ 汇总校验

def test_verify_against_folder_names_counts_correctly() -> None:
    sessions = sd.derive_sessions(
        _WANNING
        + [_photo("城B\\2026-01-01_0800-0900\\x.jpg", "2026-01-01 08:00:00")]
        + [_photo("城C\\2026-01-01_2200_至_01-02_0200\\y.jpg", "2026-01-01 22:00:00")]
    )
    comparable, matched, mismatches, skipped = sd.verify_against_folder_names(sessions)
    assert comparable == 2      # 万宁市 与 城B
    assert matched == 1         # 万宁市（借偏移后相符）
    assert skipped == 1         # 城C 是跨天命名
    # 城B 的目录名写的是 0800-0900 但只有一张 08:00 的照片 → 跨度 0800-0800，不符
    assert len(mismatches) == 1
    assert mismatches[0][0].startswith("城B")
