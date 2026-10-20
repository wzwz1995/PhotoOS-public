"""Phase 2 · M4：Scene 划分测试。

Scene 的存在理由是**让 PRD §9.2 的「20–50 张 Shot ≤10 分钟」可达成**：
实测库里有 866 张、2,239 个文件的时段，不切分用户面对的就是"一次筛 500+ 张 Shot"。
因此本文件的重点断言是 **③ 规模约束真的把段切到上限以内**。
"""

from __future__ import annotations

import pytest

from photoos.library import scene_grouping as sc
from photoos.library.shot_grouping import ShotAsset, ShotGroup


def _shot(aid: int, ts: str, *, size: int = 1, confidence: float = 0.95) -> ShotGroup:
    assets = [
        ShotAsset(
            asset_id=aid * 100 + k,
            file_type="jpeg",
            relative_path=f"城\\2026-01-01_1000-1100\\DSC_{aid:04d}_{k}.JPG",
            capture_at=ts,
        )
        for k in range(size)
    ]
    return ShotGroup(assets=assets, confidence=confidence)


def _series(*times: str) -> list[ShotGroup]:
    return [_shot(i + 1, t) for i, t in enumerate(times)]


def _params(**kw) -> sc.SceneParams:
    base = {"gap_seconds": 900.0}
    base.update(kw)
    return sc.SceneParams(**base)


# ============================================================ ① 时间断点

def test_single_segment_when_gaps_are_small() -> None:
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 10:10:00")
    scenes = sc.split_scenes(shots, params=_params())
    assert len(scenes) == 1
    assert scenes[0].size == 3


def test_gap_splits_into_two_scenes() -> None:
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 11:00:00")
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=900))
    assert [s.size for s in scenes] == [2, 1]


def test_gap_threshold_is_inclusive() -> None:
    """恰好等于阈值不切（与 shot_grouping 同口径，否则边界会差一）。"""
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:15:00")
    assert len(sc.split_scenes(shots, params=_params(gap_seconds=900))) == 1
    assert len(sc.split_scenes(shots, params=_params(gap_seconds=899))) == 2


def test_boundary_records_the_reason() -> None:
    """★ `evidence_json` 要能回答"为什么在这里切"（否则用户调了参数无从判断）。"""
    shots = _series("2026-01-01 10:00:00", "2026-01-01 11:30:00")
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=900))
    assert scenes[1].boundaries
    boundary = scenes[1].boundaries[0]
    assert boundary.kind == "gap"
    assert "分钟" in boundary.detail


def test_shot_without_time_is_split_conservatively() -> None:
    """无拍摄时间的 Shot 不能与别人"相邻"——保守切断而不是猜。"""
    shots = [
        _shot(1, "2026-01-01 10:00:00"),
        ShotGroup(assets=[ShotAsset(999, "jpeg", "城\\p\\x.JPG", capture_at=None)]),
        _shot(3, "2026-01-01 10:01:00"),
    ]
    scenes = sc.split_scenes(shots, params=_params())
    assert len(scenes) == 3
    assert any("无拍摄时间" in b.detail for s in scenes for b in s.boundaries)


def test_empty_input() -> None:
    assert sc.split_scenes([], params=_params()) == []


def test_bad_params_rejected() -> None:
    with pytest.raises(ValueError):
        sc.split_scenes([], params=_params(gap_seconds=0))
    with pytest.raises(ValueError):
        sc.split_scenes([], params=_params(max_shots=0))


# ============================================================ ② 相似度合并

def test_similarity_merges_across_a_gap() -> None:
    """★ 跨边界余弦 ≥ 阈值 → 合并（同一拍摄意图被时间断点切断了）。"""
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 11:00:00")
    by_gap = sc.split_scenes(shots, params=_params(gap_seconds=900))
    assert len(by_gap) == 2

    merged = sc.split_scenes(
        shots, params=_params(gap_seconds=900), similarity=lambda a, b: 0.93
    )
    assert len(merged) == 1
    assert merged[0].size == 3


def test_similarity_below_threshold_keeps_the_split() -> None:
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 11:00:00")
    scenes = sc.split_scenes(
        shots, params=_params(gap_seconds=900, merge_threshold=0.85),
        similarity=lambda a, b: 0.80,
    )
    assert len(scenes) == 2


def test_merge_threshold_is_inclusive() -> None:
    shots = _series("2026-01-01 10:00:00", "2026-01-01 11:00:00")
    assert len(sc.split_scenes(shots, params=_params(gap_seconds=900, merge_threshold=0.85),
                               similarity=lambda a, b: 0.85)) == 1
    assert len(sc.split_scenes(shots, params=_params(gap_seconds=900, merge_threshold=0.86),
                               similarity=lambda a, b: 0.85)) == 2


def test_similarity_returning_none_does_not_merge_or_split() -> None:
    """★ 没有嵌入时**静默跳过**相似度分支 —— 不猜、也不默认成 1.0。

    若把 None 当成"很相似"，整个时段会被并成一个巨大 Scene；
    若当成"不相似"，又会被切碎。两种猜测都错，正确做法是不参与判定。
    """
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 11:00:00")
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=900), similarity=lambda a, b: None)
    assert [s.size for s in scenes] == [2, 1]


def test_no_similarity_callback_still_works() -> None:
    """M3 未跑时 Scene 划分照常可用（只是边界更粗）—— 与 M2 的降级姿态一致。"""
    shots = _series("2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 11:00:00")
    assert [s.size for s in sc.split_scenes(shots, params=_params(gap_seconds=900))] == [2, 1]


# ============================================================ ② 相似度细分

def test_long_dissimilar_run_splits_a_segment() -> None:
    """段内连续 K 张与前一张互不相似 → 换了题材，继续细分。"""
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 20)])
    # 前 10 张彼此相似（0.9），第 11 张起连续互不相似（0.1）
    def sim(a: int, b: int) -> float:
        if a >= 9 and b >= 10:
            return 0.1
        return 0.9

    scenes = sc.split_scenes(
        shots, params=_params(dissimilar_run=3, unrelated_similarity=0.5), similarity=sim
    )
    assert len(scenes) >= 2
    assert any(b.kind == "dissimilar" for s in scenes for b in s.boundaries)


def test_short_dissimilar_run_does_not_split() -> None:
    """偶尔一张不相似不该切 —— 否则一个 Scene 会被切得粉碎。"""
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 10)])
    scenes = sc.split_scenes(
        shots,
        params=_params(dissimilar_run=5, unrelated_similarity=0.5),
        similarity=lambda a, b: 0.1 if b == 5 else 0.9,
    )
    assert len(scenes) == 1


# ============================================================ ③ 规模约束（断点 #2 的前提）

def test_oversized_scene_is_split_until_within_limit() -> None:
    """★ 核心：> max_shots 的段必须被切到上限以内，否则"≤10 分钟"无从谈起。"""
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 50)])
    scenes = sc.split_scenes(shots, params=_params(max_shots=20), similarity=None)
    assert all(s.size <= 20 for s in scenes)
    assert sum(s.size for s in scenes) == 50


def test_size_split_happens_at_the_largest_time_gap() -> None:
    """在**最大时间间隔**处切 —— 用最可靠的信号，且让规模与相似度阈值解耦。"""
    shots = _series(
        "2026-01-01 10:00:00",
        "2026-01-01 10:01:00",
        "2026-01-01 10:02:00",
        "2026-01-01 10:20:00",   # ← 最大间隔在这里
        "2026-01-01 10:21:00",
        "2026-01-01 10:22:00",
    )
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=99999, max_shots=3))
    assert [s.size for s in scenes] == [3, 3]
    assert scenes[1].captured_from == "2026-01-01 10:20:00"


def test_size_split_reason_is_recorded() -> None:
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 10)])
    scenes = sc.split_scenes(shots, params=_params(max_shots=4))
    assert any(b.kind == "size" for s in scenes for b in s.boundaries)


def test_exactly_at_limit_is_not_split() -> None:
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 10)])
    assert len(sc.split_scenes(shots, params=_params(max_shots=10))) == 1


def test_realistic_period_2239_assets_becomes_many_scenes() -> None:
    """真实规模：希腊市 `2026-10-06_1146_至_10-07_1908` 有 **2,239 个文件**。

    这里用 400 个 Shot（每 Shot 5 张资产 = 2,000 张）近似，验证会被切成多个 Scene，
    且每个都在上限内 —— 否则用户要一次面对 400 张 Shot。
    """
    shots = [
        _shot(i + 1, f"2026-10-0{6 + (i // 200)} {10 + (i % 12):02d}:{(i * 7) % 60:02d}:00", size=5)
        for i in range(400)
    ]
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=3600, max_shots=60))
    assert all(s.size <= 60 for s in scenes)
    assert len(scenes) >= 7
    assert sum(s.size for s in scenes) == 400


# ============================================================ 按时段划分

def test_split_all_never_crosses_periods() -> None:
    """Scene 不跨时段目录：时段是"一次出行"，跨时段合成 Scene 语义上说不通。"""
    by_period = {
        ("城", "2026-01-01_1000-1100"): _series("2026-01-01 10:00:00", "2026-01-01 10:05:00"),
        ("城", "2026-01-01_1100-1200"): _series("2026-01-01 11:00:00"),
    }
    result = sc.split_all(by_period, params=_params(gap_seconds=99999))
    assert set(result) == set(by_period)
    assert [len(v) for v in result.values()] == [1, 1]


def test_split_all_sorts_shots_by_time() -> None:
    shots = _series("2026-01-01 10:10:00", "2026-01-01 10:00:00", "2026-01-01 10:05:00")
    result = sc.split_all({("城", "p"): shots}, params=_params())
    assert result[("城", "p")][0].captured_from == "2026-01-01 10:00:00"


def test_split_all_passes_period_to_similarity_callback() -> None:
    seen: list[tuple] = []

    def sim(period, a, b):
        seen.append((period, a, b))
        return 0.9

    by_period = {("城", "p1"): _series("2026-01-01 10:00:00", "2026-01-01 10:05:00")}
    sc.split_all(by_period, params=_params(), similarity=sim)
    assert all(p == ("城", "p1") for p, _, _ in seen)


# ============================================================ 汇总与证据

def test_summarize_counts_prd_band() -> None:
    """`within_prd_band` 直接对应 PRD §9.2 的「20–50 张 Shot」—— 这是断点 #2 的可观测指标。"""
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 60)])
    scenes = sc.split_scenes(shots, params=_params(max_shots=30))
    stats = sc.summarize(scenes)
    assert stats["scenes"] == 2
    assert stats["shots"] == 60
    assert stats["max_shots"] <= 30
    assert stats["within_prd_band"] >= 1


def test_scene_draft_reports_asset_count_and_range() -> None:
    shots = [_shot(1, "2026-01-01 10:00:00", size=2), _shot(2, "2026-01-01 10:05:00", size=2)]
    scene = sc.split_scenes(shots, params=_params())[0]
    assert scene.asset_count == 4
    assert scene.captured_from == "2026-01-01 10:00:00"
    assert scene.captured_to == "2026-01-01 10:05:00"


def test_scene_needs_review_propagates_from_shots() -> None:
    shots = [_shot(1, "2026-01-01 10:00:00", confidence=0.40)]
    assert sc.split_scenes(shots, params=_params())[0].needs_review is True


def test_params_to_json_uses_the_planned_key_names() -> None:
    """`evidence_json` 里记的参数名要与 config / Session 覆盖里的键一致，便于对照排查。"""
    payload = _params().to_json()
    assert set(payload) == {
        "scene_gap_seconds",
        "scene_merge_similarity",
        "scene_dissimilar_run",
        "scene_max_shots",
        "scene_unrelated_similarity",
    }


def test_split_is_deterministic() -> None:
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 30)])
    a = [s.indices for s in sc.split_scenes(shots, params=_params(max_shots=7))]
    b = [s.indices for s in sc.split_scenes(shots, params=_params(max_shots=7))]
    assert a == b


def test_every_shot_lands_in_exactly_one_scene() -> None:
    """★ 守恒：切分不得丢 Shot、也不得重复（铁律 #3 精神：派生不丢事实）。"""
    shots = _series(*[f"2026-01-01 10:{m:02d}:00" for m in range(0, 37)])
    scenes = sc.split_scenes(shots, params=_params(gap_seconds=120, max_shots=8))
    collected = [i for s in scenes for i in s.indices]
    assert sorted(collected) == list(range(37))
    assert len(collected) == len(set(collected))
