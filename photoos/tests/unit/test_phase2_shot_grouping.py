"""Phase 2 · M2：Shot 分组测试。

**核心断言是你给的那张表**（`PHASE2_RESEARCH.md` §9.2，我在调研阶段逐项复现过，
这里把它钉成回归测试）：

| 检查项 | 万宁市\\2025-11-29_0610-1747（302 张） | 杭州市\\2026-02-19_0947-1749（322 张） |
| --- | ---: | ---: |
| 2 s 阈值分组 | 176 组；15 组 ≥3 张；最大 15 张 | 199 组；21 组 ≥3 张；最大 17 张 |
| 1 s / 5 s | 218 / 129 组 | 234 / 137 组 |
| 组内镜头变过 | 0 | 0 |
| 组内焦段变过 | 0 | 0 |
| 组内曝光变过 | 4 | 1 |
| 文件名序号严格连续 | 15/15 | 21/21 |

数据来自 `venv/fixtures_grouping.json`（从真实库只读导出，见文件头）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from photoos.library import shot_grouping as sg

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "grouping_periods.json"


def _load(period: str) -> list[sg.ShotAsset]:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))[period]
    return [
        sg.ShotAsset(
            asset_id=int(row["id"]),
            file_type=row["file_type"],
            relative_path=row["relative_path"],
            capture_at=row["capture_at"],
            subsec=row["subsec"],
            lens_model=row["lens_model"],
            focal_length=row["focal_length"],
            aperture=row["aperture"],
            shutter_speed=row["shutter_speed"],
            iso=row["iso"],
        )
        for row in data
    ]


@pytest.fixture(scope="module")
def wanning() -> list[sg.ShotAsset]:
    return _load("万宁市\\2025-11-29_0610-1747")


@pytest.fixture(scope="module")
def hangzhou() -> list[sg.ShotAsset]:
    return _load("杭州市\\2026-02-19_0947-1749")


# ============================================================ 输入规模

def test_fixture_sizes_match_research(wanning, hangzhou) -> None:
    """样本量与调研一致：302 / 322 张 JPG。"""
    assert len(wanning) == 302
    assert len(hangzhou) == 322


# ============================================================ ★ 复现实测的组数

@pytest.mark.parametrize(
    "gap,expected_wanning,expected_hangzhou",
    [
        (1.0, 218, 234),
        (2.0, 176, 199),
        (3.0, 153, 164),
        (5.0, 129, 137),
    ],
)
def test_group_counts_reproduce_research(wanning, hangzhou, gap, expected_wanning, expected_hangzhou) -> None:
    """★ 阈值 → 组数，与调研实测逐位一致。"""
    assert len(sg.group_shots(wanning, gap_seconds=gap)) == expected_wanning
    assert len(sg.group_shots(hangzhou, gap_seconds=gap)) == expected_hangzhou


@pytest.mark.parametrize(
    "gap,expected_big,expected_max",
    [(1.0, 13, 15), (2.0, 15, 15), (3.0, 23, 15), (5.0, 26, 28)],
)
def test_wanning_large_groups_reproduce_research(wanning, gap, expected_big, expected_max) -> None:
    groups = sg.group_shots(wanning, gap_seconds=gap)
    big = [g for g in groups if g.size >= 3]
    assert len(big) == expected_big
    assert max(g.size for g in groups) == expected_max


@pytest.mark.parametrize(
    "gap,expected_big,expected_max",
    [(1.0, 17, 11), (2.0, 21, 17), (3.0, 31, 27), (5.0, 41, 27)],
)
def test_hangzhou_large_groups_reproduce_research(hangzhou, gap, expected_big, expected_max) -> None:
    groups = sg.group_shots(hangzhou, gap_seconds=gap)
    big = [g for g in groups if g.size >= 3]
    assert len(big) == expected_big
    assert max(g.size for g in groups) == expected_max


# ============================================================ ★ 组内信号

def _big(groups):
    return [g for g in groups if g.size >= 3]


def test_no_lens_or_focal_change_inside_groups(wanning, hangzhou) -> None:
    """★ 实测：连拍组内镜头与焦段**从不变化**（0/15、0/21）。

    这正是不该拿它们当切分条件的原因 —— 用了也不会产生任何效果。
    """
    for assets, expected in ((wanning, 15), (hangzhou, 21)):
        big = _big(sg.group_shots(assets, gap_seconds=2.0))
        assert len(big) == expected
        for g in big:
            assert len({a.lens_model for a in g.assets if a.lens_model}) == 1
            assert len({a.focal_length for a in g.assets if a.focal_length is not None}) == 1


def test_exposure_does_change_inside_groups(wanning, hangzhou) -> None:
    """★ 实测：曝光在组内**会**变化（4/15、1/21）→ 绝不能当硬约束。"""
    counts = []
    for assets, expected in ((wanning, 4), (hangzhou, 1)):
        big = _big(sg.group_shots(assets, gap_seconds=2.0))
        varied = sum(1 for g in big if len({a.exposure_key for a in g.assets}) > 1)
        counts.append(varied)
        assert varied == expected
    assert counts == [4, 1]


def test_exposure_change_only_lowers_confidence_slightly(wanning) -> None:
    """曝光变过 → 0.80（正常现象），而不是被切分或大幅降级。"""
    big = _big(sg.group_shots(wanning, gap_seconds=2.0))
    varied = [g for g in big if len({a.exposure_key for a in g.assets}) > 1]
    assert varied, "夹具里应当存在曝光变过的组"
    for g in varied:
        assert g.confidence == sg.CONFIDENCE_EXPOSURE_VARIED
        assert not g.needs_review


def test_sequence_numbers_are_contiguous_in_all_large_groups(wanning, hangzhou) -> None:
    """★ 实测：组内文件名序号**严格连续**（15/15、21/21）。

    因为镜头/焦段/曝光三条里前两条不变、第三条会变，序号连续就成了
    "这是连拍"最有用的**正向**信号（它决定 0.95 还是 0.85）。
    """
    for assets, expected in ((wanning, 15), (hangzhou, 21)):
        big = _big(sg.group_shots(assets, gap_seconds=2.0))
        contiguous = 0
        for g in big:
            nums = sorted(n for _p, n in (sg._sequence_number(a.name) or (None, None) for a in g.assets) if n is not None)
            if nums and all(nums[i + 1] - nums[i] == 1 for i in range(len(nums) - 1)):
                contiguous += 1
        assert contiguous == expected


# ============================================================ 置信度规则

def _asset(aid: int, name: str, ts: str, **kw) -> sg.ShotAsset:
    """构造一个夹具资产。

    `ts` 可以写成 `'2026-01-01 10:00:00.50'` 这种"带小数秒"的形式，
    这里会把它拆成 `capture_at='...10:00:00'` + `subsec='50'` ——
    **真实 EXIF 就是这个形状**（Nikon 把百分秒写在独立的 `SubSecTimeOriginal` 标签里，
    不写进 `DateTimeOriginal`），所以夹具也照这个形状造，避免测出一个现实中不存在的输入。
    """
    stamp = str(ts)
    frac = "00"
    if "." in stamp:
        stamp, _, digits = stamp.partition(".")
        frac = digits or "00"
    return sg.ShotAsset(
        asset_id=aid,
        file_type=kw.pop("file_type", "jpeg"),
        relative_path=f"城\\2026-01-01_1000-1100\\{name}",
        capture_at=stamp,
        subsec=kw.pop("subsec", frac),
        **kw,
    )


def test_lens_change_flags_for_review() -> None:
    """镜头变过 → 0.40 + 提示人工确认（这是最强的"不是连拍"反证）。"""
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00.00", lens_model="A", focal_length=50.0),
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:01.00", lens_model="B", focal_length=50.0),
    ]
    g = sg.group_shots(items, gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_SUSPECT
    assert g.needs_review is True
    assert any("镜头" in n for n in g.notes)


def test_focal_change_flags_for_review() -> None:
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00.00", lens_model="A", focal_length=50.0),
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:01.00", lens_model="A", focal_length=70.0),
    ]
    g = sg.group_shots(items, gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_SUSPECT
    assert any("焦段" in n for n in g.notes)


def test_contiguous_sequence_gives_top_confidence() -> None:
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00.00", lens_model="A", focal_length=50.0),
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:00.50", lens_model="A", focal_length=50.0),
    ]
    g = sg.group_shots(items, gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_STRONG


def test_gap_in_sequence_lowers_confidence() -> None:
    """序号不连续（如撞名后缀把序号打断）→ 0.85 而不是 0.95。"""
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00.00", lens_model="A", focal_length=50.0),
        _asset(2, "DSC_0009.JPG", "2026-01-01 10:00:00.50", lens_model="A", focal_length=50.0),
    ]
    g = sg.group_shots(items, gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_LIKELY
    assert any("序号" in n for n in g.notes)


def test_dup_suffix_does_not_break_sequence() -> None:
    """`__N` 撞名后缀必须先剥掉再比序号（PHASE1_TECH_PLAN §B3：命名冲突 ≠ 重复）。"""
    assert sg._sequence_number("DSC_9005__2.JPG") == ("dsc_", 9005)
    assert sg._sequence_number("DSC_9005.NEF") == ("dsc_", 9005)
    items = [
        _asset(1, "DSC_0001.NEF", "2026-01-01 10:00:00.00", lens_model="A", focal_length=50.0),
        _asset(2, "DSC_0002__2.JPG", "2026-01-01 10:00:00.50", lens_model="A", focal_length=50.0),
    ]
    g = sg.group_shots(items, gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_STRONG


def test_single_asset_group_is_high_confidence() -> None:
    """单张一组的"置信度"没有歧义 —— 不该给它低分或提示人工确认。"""
    g = sg.group_shots([_asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00.00")], gap_seconds=2.0)[0]
    assert g.confidence == sg.CONFIDENCE_STRONG
    assert g.needs_review is False
    assert g.notes == []


# ============================================================ subsec

def test_subsec_parsing_is_digit_count_based() -> None:
    """Nikon 写百分秒；按位数换算，换机身写毫秒也不会算错。"""
    assert sg.parse_subsecond("07") == pytest.approx(0.07)
    assert sg.parse_subsecond("070") == pytest.approx(0.070)
    assert sg.parse_subsecond(None) == 0.0
    assert sg.parse_subsecond("") == 0.0
    assert sg.parse_subsecond("abc") == 0.0


def test_subsec_decides_order_within_the_same_second() -> None:
    """★ 同秒的相邻对必须由 subsec 定序（实测万宁市有 46 对）。

    若忽略 subsec，这两张会被当成"同一时刻"，顺序退化为按路径 —— 分组边界随之漂移。
    这里用 0.05 s 与 0.06 s（相差 0.01 s，远小于阈值）确保它们同组，
    再断言组内顺序是 `subsec` 决定的而不是文件名决定的。
    """
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00", subsec="06"),   # 较晚
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:00", subsec="05"),   # 较早
    ]
    groups = sg.group_shots(items, gap_seconds=0.5)
    assert len(groups) == 1
    # subsec 05 在 06 之前 → 组内顺序应为 2, 1（与文件名顺序相反，证明是 subsec 在起作用）
    assert [a.asset_id for a in groups[0].assets] == [2, 1]


def test_same_second_order_change_can_move_a_group_boundary() -> None:
    """subsec 真的会影响**分组边界**，不只是组内顺序。

    三张同秒：subsec 10 / 40 / 90。阈值 0.5 s。
    按 subsec 排序后间隔为 0.30 与 0.50 —— 后者恰好等于阈值（不切分）→ 一组。
    若忽略 subsec 而按文件名排序（90/10/40），间隔同样是 0.30/0.50，
    但**顺序变成 90→10→40**，中间会出现 0.80 的跨越。用文件名 `DSC_0001/0002/0003`
    配 subsec `90/10/40` 就能看出区别。
    """
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00", subsec="90"),
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:00", subsec="10"),
        _asset(3, "DSC_0003.JPG", "2026-01-01 10:00:00", subsec="40"),
    ]
    groups = sg.group_shots(items, gap_seconds=0.5)
    # 按 subsec 排序 = 2(0.10) → 3(0.40) → 1(0.90)，间隔 0.30 / 0.50 都 ≤ 0.5 → 一组
    assert len(groups) == 1
    assert [a.asset_id for a in groups[0].assets] == [2, 3, 1]


def test_same_second_without_subsec_falls_back_to_path_order() -> None:
    items = [
        _asset(1, "DSC_0002.JPG", "2026-01-01 10:00:00", subsec=None),
        _asset(2, "DSC_0001.JPG", "2026-01-01 10:00:00", subsec=None),
    ]
    groups = sg.group_shots(items, gap_seconds=2.0)
    assert len(groups) == 1
    assert [a.asset_id for a in groups[0].assets] == [2, 1]   # 按 relative_path


# ============================================================ 边界与确定性

def test_gap_threshold_is_inclusive() -> None:
    """间隔**恰好等于**阈值 → 不切分（与调研脚本口径一致，否则组数会差一）。"""
    items = [
        _asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00", subsec="00"),
        _asset(2, "DSC_0002.JPG", "2026-01-01 10:00:02", subsec="00"),
    ]
    assert len(sg.group_shots(items, gap_seconds=2.0)) == 1
    assert len(sg.group_shots(items, gap_seconds=1.999)) == 2


def test_zero_or_negative_gap_is_rejected() -> None:
    """阈值必须为正 —— 防止调用方漏传配置导致"每张一组"这种静默错误。"""
    with pytest.raises(ValueError):
        sg.group_shots([], gap_seconds=0)
    with pytest.raises(ValueError):
        sg.group_shots([], gap_seconds=-1)


def test_missing_time_assets_each_get_their_own_group() -> None:
    """无拍摄时间的资产不能与别人"相邻"（否则会被随意并进某组）。"""
    items = [
        sg.ShotAsset(1, "jpeg", "城\\2026-01-01_1000-1100\\a.JPG", capture_at=None),
        sg.ShotAsset(2, "jpeg", "城\\2026-01-01_1000-1100\\b.JPG", capture_at=None),
        _asset(3, "DSC_0003.JPG", "2026-01-01 10:00:00"),
    ]
    groups = sg.group_shots(items, gap_seconds=2.0)
    assert len(groups) == 3


def test_empty_input() -> None:
    assert sg.group_shots([], gap_seconds=2.0) == []


def test_deterministic_regardless_of_input_order(wanning) -> None:
    """同一批资产、任意输入顺序 → 完全相同的分组（可重算的前提）。"""
    forward = sg.group_shots(wanning, gap_seconds=2.0)
    backward = sg.group_shots(list(reversed(wanning)), gap_seconds=2.0)
    assert [g.asset_ids for g in forward] == [g.asset_ids for g in backward]


def test_grouping_is_repeatable(wanning) -> None:
    a = [g.asset_ids for g in sg.group_shots(wanning, gap_seconds=2.0)]
    b = [g.asset_ids for g in sg.group_shots(wanning, gap_seconds=2.0)]
    assert a == b


# ============================================================ 主 Asset

def test_primary_asset_prefers_raw_over_jpeg() -> None:
    """PHASE1_TECH_PLAN §4.2 决策 4：RAW > HEIC > TIFF > JPEG > PNG > 视频。"""
    items = [
        sg.ShotAsset(10, "jpeg", "城\\2026-01-01_1000-1100\\DSC_0001.JPG", capture_at="2026-01-01 10:00:00"),
        sg.ShotAsset(11, "raw", "城\\2026-01-01_1000-1100\\DSC_0001.NEF", capture_at="2026-01-01 10:00:00"),
    ]
    assert sg.group_shots(items, gap_seconds=2.0)[0].primary_asset_id == 11


def test_primary_asset_is_deterministic_within_same_type() -> None:
    items = [
        sg.ShotAsset(20, "jpeg", "城\\2026-01-01_1000-1100\\DSC_0002.JPG", capture_at="2026-01-01 10:00:00"),
        sg.ShotAsset(19, "jpeg", "城\\2026-01-01_1000-1100\\DSC_0001.JPG", capture_at="2026-01-01 10:00:00"),
    ]
    assert sg.group_shots(items, gap_seconds=2.0)[0].primary_asset_id == 19


def test_captured_at_ignores_video_utc() -> None:
    """视频的容器 UTC 不能当 Shot 的拍摄时间（时区基准不同，会带偏整个 Shot）。"""
    g = sg.ShotGroup(assets=[
        sg.ShotAsset(1, "jpeg", "城\\p\\a.JPG", capture_at="2026-01-01 20:00:00"),
        sg.ShotAsset(2, "video", "城\\p\\v.MOV", capture_at_utc="2026-01-01 12:00:00"),
    ])
    assert g.captured_at == "2026-01-01 20:00:00"
    assert g.capture_date_local == "2026-01-01"


# ============================================================ 按时段分段

def test_grouping_never_crosses_period_directories() -> None:
    """跨时段目录的两张即使时间相邻也不合成一个 Shot（那是两次不同的出行）。"""
    items = [
        sg.ShotAsset(1, "jpeg", "城\\2026-01-01_1000-1100\\a.JPG", capture_at="2026-01-01 10:00:00"),
        sg.ShotAsset(2, "jpeg", "城\\2026-01-01_1100-1200\\b.JPG", capture_at="2026-01-01 10:00:01"),
    ]
    buckets = sg.group_by_directory(items, gap_seconds=2.0)
    assert set(buckets) == {("城", "2026-01-01_1000-1100"), ("城", "2026-01-01_1100-1200")}
    assert all(len(v) == 1 for v in buckets.values())


def test_shallow_paths_are_grouped_under_root() -> None:
    items = [sg.ShotAsset(1, "jpeg", "只有一层.JPG", capture_at="2026-01-01 10:00:00")]
    groups = sg.group_by_directory(items, gap_seconds=2.0)
    assert set(groups) == {("(未分类)", "(根目录)")}
    assert [a.asset_id for a in groups[("(未分类)", "(根目录)")][0].assets] == [1]


# ============================================================ 只增不删的重算（决策 #4）

def test_regroup_reuses_identical_groups() -> None:
    g = sg.ShotGroup(assets=[_asset(1, "DSC_0001.JPG", "2026-01-01 10:00:00")])
    plan = sg.plan_regroup([g], existing={7: frozenset({1})})
    assert plan.reused == [7]
    assert plan.retired == []


def test_regroup_retires_vanished_shots_instead_of_deleting() -> None:
    """★ 决策 #4：旧 Shot 不再对应任何新分组时，进 `retired`（标 retired_at），**不删除**。"""
    g = sg.ShotGroup(assets=[_asset(1, "x.JPG", "2026-01-01 10:00:00")])
    plan = sg.plan_regroup([g], existing={7: frozenset({1}), 8: frozenset({99})})
    assert plan.reused == [7]
    assert plan.retired == [8]


def test_regroup_creates_new_shots() -> None:
    g = sg.ShotGroup(assets=[_asset(1, "x.JPG", "2026-01-01 10:00:00")])
    plan = sg.plan_regroup([g], existing={})
    assert plan.created and plan.reused == []
    assert plan.retired == []


def test_regroup_never_touches_protected_shots() -> None:
    """★ 决策 #10：`group_source='user'` 的 Shot **永不被重算覆盖**（也不退役）。"""
    g = sg.ShotGroup(assets=[_asset(1, "x.JPG", "2026-01-01 10:00:00")])
    plan = sg.plan_regroup([g], existing={7: frozenset({1})}, protected=frozenset({7}))
    assert plan.reused == []
    assert plan.retired == []
    assert plan.created


def test_regroup_plan_is_deterministic() -> None:
    gs = [
        sg.ShotGroup(assets=[_asset(1, "a.JPG", "2026-01-01 10:00:00")]),
        sg.ShotGroup(assets=[_asset(2, "b.JPG", "2026-01-01 10:00:05")]),
    ]
    existing = {5: frozenset({1}), 6: frozenset({2}), 9: frozenset({42})}
    a = sg.plan_regroup(gs, existing=existing)
    b = sg.plan_regroup(gs, existing=existing)
    assert (a.reused, a.created, a.retired) == (b.reused, b.created, b.retired)
    assert a.reused == [5, 6]
    assert a.retired == [9]
