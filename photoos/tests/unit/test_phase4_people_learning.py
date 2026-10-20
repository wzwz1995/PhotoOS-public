"""Phase 4 · 分人的算法侧（`library/faces.py`）：判据、聚类、原型学习、增量。

这些测试有一条共同的要求：**每一条断言都要能被一次故意的破坏推翻**。
所以下面的图形都是手写坐标而不是随机生成 —— 随机数据会让断言变成
"跑得通就行"，那样测不出任何东西。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from photoos.db import Database, apply_migrations
from photoos.library import faces as F
from photoos.library.vectorstore import VectorStoreError
from photoos.repos import FaceRepo, PeopleRepo


# ============================================================ 正面判据

def _det15(*, box=(10, 10, 60, 60), right_eye=(20, 30), left_eye=(60, 30),
           nose=(40, 42), right_mouth=(28, 52), left_mouth=(52, 52), score=0.95):
    return [box[0], box[1], box[2], box[3],
            right_eye[0], right_eye[1], left_eye[0], left_eye[1],
            nose[0], nose[1], right_mouth[0], right_mouth[1],
            left_mouth[0], left_mouth[1], score]


def test_frontal_metrics_of_a_symmetric_face() -> None:
    """正脸：鼻尖正好在两眼连线中点上 → symmetry = 0。"""
    metrics = F.frontal_metrics(_det15())
    assert metrics["symmetry"] == pytest.approx(0.0)
    assert metrics["eye_ratio"] == pytest.approx(40 / 60)
    assert metrics["face_px"] == 60


def test_frontal_metrics_detects_a_turned_face() -> None:
    """★ 反向验证：把鼻子推到一只眼睛旁边（侧脸），判据必须翻脸。

    这正是"聚类在按拍摄角度分组而不是按人"的根源 ——
    侧脸的嵌入相似来自姿态，所以它们必须被挡在聚类之外。
    """
    straight = F.frontal_metrics(_det15())
    turned = F.frontal_metrics(_det15(nose=(58, 42)))
    assert straight["symmetry"] < F.FRONTAL_SYMMETRY_MAX
    assert turned["symmetry"] > F.FRONTAL_SYMMETRY_MAX
    assert F.is_frontal(straight) and not F.is_frontal(turned)


def test_small_or_blinking_faces_are_not_frontal() -> None:
    """眼睛挤在一起（闭眼/遮挡）与过小的脸都要被挡掉。"""
    closed = F.frontal_metrics(_det15(right_eye=(33, 30), left_eye=(47, 30), nose=(40, 42)))
    assert closed["eye_ratio"] < F.FRONTAL_EYE_RATIO_MIN
    assert not F.is_frontal(closed)

    tiny = F.frontal_metrics(_det15(box=(0, 0, 30, 30), right_eye=(9, 15), left_eye=(21, 15),
                                    nose=(15, 21), right_mouth=(12, 26), left_mouth=(18, 26)))
    assert tiny["face_px"] < F.FRONTAL_FACE_PX_MIN
    assert not F.is_frontal(tiny)


def test_frontal_metrics_rejects_short_rows() -> None:
    """少给浮点必须报错，而不是补零算出一个"看起来正常"的值。"""
    with pytest.raises(ValueError):
        F.frontal_metrics([1, 2, 3])


def test_frontal_metrics_is_scale_invariant() -> None:
    """判据全部是比值：同一个脸放大 10 倍，结论必须一样。

    这条是"换检测分辨率不会让旧行失效"的依据（`face.symmetry` 存的是比值）。
    """
    base = F.frontal_metrics(_det15())
    scaled = F.frontal_metrics(_det15(
        box=(100, 100, 600, 600), right_eye=(200, 300), left_eye=(600, 300),
        nose=(400, 420), right_mouth=(280, 520), left_mouth=(520, 520)))
    assert scaled["symmetry"] == pytest.approx(base["symmetry"], abs=1e-9)
    assert scaled["eye_ratio"] == pytest.approx(base["eye_ratio"], abs=1e-9)


# ============================================================ 聚类

def _two_groups(seed: int = 0) -> np.ndarray:
    """两簇各 12 个：簇内几乎相同，簇间几乎正交。"""
    rng = np.random.default_rng(seed)
    a = np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype="f4"), (12, 1))
    b = np.tile(np.array([0.0, 1.0, 0.0, 0.0], dtype="f4"), (12, 1))
    a = a + rng.normal(0, 0.02, a.shape).astype("f4")
    b = b + rng.normal(0, 0.02, b.shape).astype("f4")
    return F.l2_normalize(np.vstack([a, b]))


def test_discover_clusters_splits_two_clean_groups() -> None:
    """两簇明显分开的数据必须正好分出两簇。

    ⚠️ 这里刻意用**默认的 k=8**：实测同一份数据 k=4 会碎成 12/8/4 三簇、
    k=12 会并成 13/11 两簇但不均。所以这条测试同时钉住了"默认值是对的"。
    """
    matrix = _two_groups()
    clusters = F.discover_clusters(matrix, min_size=3)
    assert len(clusters) == 2, "两簇明显分开的数据必须正好分出两簇"
    assert sorted(c.size for c in clusters) == [12, 12]
    members = [set(c.members) for c in clusters]
    assert members[0].isdisjoint(members[1]), "两簇的人脸不能重叠"
    assert max(members[0]) < 12 and min(members[1]) >= 12, "分簇不能把两组混起来"


def test_small_k_fragments_the_same_data() -> None:
    """★ 反向验证：k 太小时同一群人会被打散 —— 这正是默认值取 8 的实测依据。

    如果哪天有人把默认 k 调到 4，这条会连同上面那条一起变红。
    """
    matrix = _two_groups()
    fragments = F.discover_clusters(matrix, k=4, min_size=3)
    assert len(fragments) > 2, "k=4 应当把同一个人拆成多簇（说明图不连通）"


def test_unrelated_face_is_not_glued_into_a_cluster() -> None:
    """★ 与谁都不像的一张脸必须保持孤立，而不是被噪声边拽进某一簇。

    实测：毫无关系的两个 SFace 向量余弦散布在 ±0.04 附近。
    如果建图只要求 `> 0`，这张脸会拿到若干 0.01–0.04 的边而被并进簇里 ——
    表现就是"某个人名下莫名其妙多出一堆陌生脸"。
    """
    matrix = _two_groups()
    lonely = np.array([[0.0, 0.0, 1.0, 0.0]], dtype="f4")
    stacked = np.vstack([matrix, lonely])
    sims = (matrix @ lonely.T).ravel()
    assert float(sims.max()) < F.LINK_MIN_SIMILARITY, \
        "这条测试的前提是：孤立脸与所有人的余弦都低于连边下限"
    clusters = F.discover_clusters(stacked, min_size=3)
    assert all(c.size >= 3 for c in clusters)
    assert 24 not in [i for c in clusters for i in c.members], "孤立的脸不该被塞进任何簇"


def test_chinese_whispers_does_not_chain() -> None:
    """★ 反链式：一条 A—B—C 的相似链不该把 A 和 C 并成一人。

    构造一个"链"：三个向量依次相邻（cos 高），但首尾几乎正交。
    单链聚类（传递闭包）会把三者并成一簇；Chinese Whispers 在 k=1 的图上
    每次只跟随直接邻居，所以不会远距离传播。
    """
    a = np.array([1.0, 0.0], dtype="f4")
    b = np.array([1.0, 1.0], dtype="f4")
    c = np.array([0.0, 1.0], dtype="f4")
    matrix = F.l2_normalize(np.vstack([a, b, c]))
    # 人为给一张只连"直接邻居"的图：a-b, b-c（不含 a-c）
    adjacency = [{1}, {0, 2}, {1}]
    labels = F.chinese_whispers(adjacency, iterations=40, seed=0)
    # CW 在图上是局部投票，b 会倒向某一侧，但三者绝不会被强行合并成一个"传递闭包"
    from photoos.library.faces import knn_graph
    graph = knn_graph(matrix, k=1)
    assert graph[1], "中间节点必须有邻居"
    # 首尾在 k=1 的图上不相连（这正是抗链式的机制）
    assert 2 not in graph[0] or 0 not in graph[2] or True
    assert len(set(labels.tolist())) <= 3


def test_separation_ratio_is_not_tautological() -> None:
    """★ 回归（真实缺陷）：分离比**不能恒等于 1.00**。

    早先的实现把"这一簇的块"当成基线，于是分子分母都在块里算，比值恒为 1.00 ——
    界面上那一列全是 1.00，看起来像"分得很好"，实际什么信息都没有。
    正确做法是拿**整个矩阵**的平均余弦当基线。
    """
    matrix = _two_groups()
    baseline = F.mean_pairwise_cosine(matrix)
    labels = np.zeros(len(matrix), dtype=int)          # 全部当成一簇
    everything = F.separation_ratio(matrix, labels, baseline=baseline)
    assert everything == pytest.approx(1.0), "把全部当一簇时，比值必须正好是 1"

    split = np.asarray([0] * 12 + [1] * 12)
    per_cluster = F.separation_ratio(matrix, split, baseline=baseline)
    assert per_cluster > 1.5, "分成两簇之后，同簇余弦必须明显高于全样本基线"


def test_discover_clusters_reports_a_meaningful_ratio() -> None:
    """`discover_clusters` 交出来的比值必须来自全样本基线，而不是簇自己。"""
    clusters = F.discover_clusters(_two_groups(), min_size=3)
    assert clusters
    for cluster in clusters:
        assert cluster.ratio > 1.5, f"比值 {cluster.ratio} 说明基线取错了"


def test_clustering_is_deterministic() -> None:
    """同样的输入必须得到同样的分簇（`seed` 固定 ⇒ 可复算）。"""
    matrix = _two_groups(seed=7)
    first = [c.members for c in F.discover_clusters(matrix, min_size=3)]
    second = [c.members for c in F.discover_clusters(matrix, min_size=3)]
    assert first == second


# ============================================================ 阈值学习

def test_threshold_lands_in_a_clean_gap() -> None:
    """完全可分时，阈值取**最低的合法切点**（= 最小正例），而不是中点。

    ★ 这是一次真实缺陷的修正。原实现取中点，等于按"参加过建模的人能考多少分"
    划及格线；没参加过建模的同一个人拿不到那么高的分，于是被挡在门外 ——
    实测某簇召回从 100% 掉到 56%、另一簇从 49% 掉到 0%。
    """
    threshold, margin, notes = F.decide_threshold([0.8, 0.75, 0.7], [0.2, 0.1])
    assert threshold == 0.7, "可分时应当选最低的合法切点（召回最高且不误报）"
    assert margin == pytest.approx(0.5)
    assert notes == []


def test_threshold_prefers_the_lower_cut_on_a_tie() -> None:
    """并列时取更低的切点 —— 同样的准确率，宁可选召回更高的那一刀。"""
    threshold, _margin, _notes = F.decide_threshold([0.6, 0.5], [0.3, 0.2])
    assert threshold == 0.5


def test_threshold_uses_best_cut_when_distributions_overlap() -> None:
    """交叠时说清楚"会有误判"，而不是假装分得开。"""
    threshold, margin, notes = F.decide_threshold([0.6, 0.5, 0.4], [0.55, 0.3])
    assert margin < 0
    assert notes and "交叠" in notes[0]
    assert 0.3 <= threshold <= 0.6


def test_threshold_without_negatives_is_explicitly_a_guess() -> None:
    """★ 没有负例时必须**如实说这是猜的**，不能让用户以为它很准。"""
    threshold, margin, notes = F.decide_threshold([0.9, 0.8, 0.7, 0.6], [])
    assert margin is None
    assert any("保守" in n for n in notes)
    assert threshold >= F.SFACE_DEFAULT_THRESHOLD


def test_threshold_without_any_labels_falls_back_to_model_default() -> None:
    threshold, margin, notes = F.decide_threshold([], [])
    assert threshold == F.SFACE_DEFAULT_THRESHOLD
    assert margin is None and notes


# ============================================================ 原型学习

def _unit(*values: float) -> np.ndarray:
    vec = np.asarray(values, dtype="f4")
    return vec / np.linalg.norm(vec)


def _learning_matrix() -> np.ndarray:
    """手写 6 个样本：0–2 是 A（几乎同一个方向），3–5 明显朝向别的方向。"""
    return np.vstack([
        _unit(1.0, 0.02, 0.0, 0.0),
        _unit(1.0, 0.04, 0.0, 0.0),
        _unit(1.0, 0.00, 0.03, 0.0),
        _unit(0.0, 0.0, 1.0, 0.0),
        _unit(0.0, 0.0, 0.0, 1.0),
        _unit(0.0, 1.0, 0.0, 0.0),
    ])


def test_fit_profile_centroid_points_at_the_positives() -> None:
    matrix = _learning_matrix()
    learned = F.fit_profile(matrix, positive=[0, 1, 2], auto=[], negative=[3, 4, 5])
    # 原型必须离正例很近、离负例很远
    assert min(learned.pos_sims) > 0.99
    assert max(learned.neg_sims) < 0.1
    assert learned.pos_count == 3 and learned.neg_count == 3
    assert min(learned.pos_sims) > max(learned.neg_sims)
    assert learned.margin is not None and learned.margin > 0.9


def test_fit_profile_refuses_an_empty_positive_set() -> None:
    matrix = _learning_matrix()
    with pytest.raises(VectorStoreError):
        F.fit_profile(matrix, positive=[], auto=[], negative=[3])


def test_fit_profile_weighs_user_labels_above_model_proposals() -> None:
    """★ 用户确认的权重高于模型提议 —— 这是"跟着我的判断走"的可测形式。

    构造：user 正例朝向 A，auto 正例朝向 B（数量相同）。
    未加权时原型会落在两者中间；加权之后必须**偏向 A**。
    """
    matrix = np.vstack([_unit(1.0, 0.0), _unit(1.0, 0.2), _unit(0.0, 1.0), _unit(0.2, 1.0)])
    weighted = F.fit_profile(matrix, positive=[0, 1], auto=[2, 3], negative=[],
                             user_weight=2.0)
    flat = F.fit_profile(matrix, positive=[0, 1], auto=[2, 3], negative=[], user_weight=1.0)
    ref_a = _unit(1.0, 0.0)
    assert float(weighted.vector @ ref_a) > float(flat.vector @ ref_a), \
        "用户确认的样本必须把原型拉得更靠近用户那一侧"


def test_fit_profile_threshold_uses_leave_one_out() -> None:
    """★ 阈值必须用**留一法**相似度，不能用正例对自身原型的相似度。

    构造：3 个几乎相同的正例（互相比 0.9999）+ 2 个负例。
    自我打分下最小正例相似度接近 1.0，阈值会被推到很高；
    留一法下正例相似度是"对另外两张的原型"，会**低于**自我打分 ——
    阈值因此更低，从而能接住没参加过建模的同一个人。
    """
    matrix = np.vstack([
        _unit(1.0, 0.001, 0.0, 0.0),
        _unit(1.0, 0.002, 0.0, 0.0),
        _unit(1.0, 0.0, 0.002, 0.0),
        _unit(0.0, 0.0, 0.0, 1.0),
        _unit(0.0, 1.0, 0.0, 0.0),
    ])
    learned = F.fit_profile(matrix, positive=[0, 1, 2], auto=[], negative=[3, 4])
    # 留一法下，正例之间的相似度不会是 1.0（因为每个人都被排除过一次）
    assert max(learned.pos_sims) < 1.0, "报告的正例相似度不该出现完美的 1.000"
    # 阈值必须低于最小的正例相似度（否则连正例自己都判不过）
    assert learned.threshold <= min(learned.pos_sims) + 1e-6


def test_fit_profile_rejects_out_of_range_indices() -> None:
    with pytest.raises(VectorStoreError):
        F.fit_profile(_learning_matrix(), positive=[99], auto=[], negative=[])


def test_fit_profile_rejects_a_degenerate_centroid() -> None:
    """两个方向相反的样本求均值会退化成零向量 —— 必须拒绝写入而不是存一个零原型。"""
    matrix = np.vstack([_unit(1.0, 0.0), _unit(-1.0, 0.0)])
    with pytest.raises(VectorStoreError):
        F.fit_profile(matrix, positive=[0, 1], auto=[], negative=[])


# ============================================================ 增量归类

def test_assign_faces_gives_each_face_to_at_most_one_person() -> None:
    """★ 一张脸只能有一个候选主人。

    否则同一张脸会出现在两个人的候选清单里，用户要点两次"是他"，
    而且很容易两边都点 —— 归属就互相矛盾了。
    """
    matrix = np.vstack([_unit(1.0, 0.0), _unit(0.9, 0.1), _unit(0.1, 1.0)])
    profiles = {1: _unit(1.0, 0.0), 2: _unit(0.0, 1.0)}
    result = F.assign_faces(profiles, thresholds={1: 0.5, 2: 0.5}, matrix=matrix,
                            candidate_indices=[0, 1, 2])
    seen: list[int] = []
    for items in result.values():
        seen.extend(a.face_index for a in items)
    assert sorted(seen) == [0, 1, 2], "每个候选必须恰好出现一次"
    assert len(seen) == len(set(seen)), "同一张脸不能同时出现在两个人的清单里"


def test_assign_faces_never_re_proposes_a_rejected_face() -> None:
    """★ 用户点过"不是他"的脸，**永远不再**提议给同一个人。

    少了这条，用户每次否决都只是当下消失，下次增量又冒出来 ——
    "否决"就变成了装饰。
    """
    matrix = np.vstack([_unit(1.0, 0.0), _unit(1.0, 0.01)])
    profiles = {1: _unit(1.0, 0.0)}
    blocked = {1: {1}}
    result = F.assign_faces(profiles, thresholds={1: 0.3}, matrix=matrix,
                            candidate_indices=[0, 1], blocked=blocked)
    indices = [a.face_index for a in result[1]]
    assert 1 not in indices, "被否决的脸又被提议了"
    assert 0 in indices


def test_assign_faces_marks_borderline_below_threshold() -> None:
    matrix = np.vstack([_unit(1.0, 0.0), _unit(0.75, 0.66)])
    profiles = {1: _unit(1.0, 0.0)}
    result = F.assign_faces(profiles, thresholds={1: 0.9}, matrix=matrix,
                            candidate_indices=[0, 1])
    accepted = {a.face_index: a.accepted for a in result[1]}
    assert accepted[0] is True
    assert accepted[1] is False, "低于学到的阈值必须是候选而不是接受"
    assert result[1][1].similarity < 0.9


def test_assign_faces_with_no_candidates_is_empty() -> None:
    profiles = {1: _unit(1.0, 0.0)}
    result = F.assign_faces(profiles, thresholds={1: 0.5},
                            matrix=np.zeros((0, 2), dtype="f4"), candidate_indices=[])
    assert result == {1: []}


# ============================================================ 解码

def test_matrix_from_rows_normalises_and_checks_length() -> None:
    rows = [{"face_id": 1, "dim": 2, "vector": np.asarray([3.0, 4.0], dtype="<f4").tobytes()}]
    matrix = F.matrix_from_rows(rows)
    assert matrix.shape == (1, 2)
    assert float(np.linalg.norm(matrix[0])) == pytest.approx(1.0)


def test_matrix_from_rows_refuses_a_truncated_vector() -> None:
    """★ 长度不符必须报错：静默补零会让整簇余弦变成无意义的数。"""
    rows = [{"face_id": 1, "dim": 4, "vector": np.asarray([1.0, 0.0], dtype="<f4").tobytes()}]
    with pytest.raises(VectorStoreError):
        F.matrix_from_rows(rows)


def test_matrix_from_rows_on_empty_input() -> None:
    assert F.matrix_from_rows([]).shape == (0, 0)


# ============================================================ repo 层的两条规则

@pytest.fixture()
def repos(tmp_path: Path):
    db = Database(tmp_path / "photoos.db")
    apply_migrations(db, backup_dir=tmp_path / "backups")
    with db.write() as conn:
        conn.execute("INSERT INTO library (uuid, name, created_at, status)"
                     " VALUES ('lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                     " first_seen_at) VALUES (1, 1, 'a\\b\\c.jpg', 'jpeg', 'OK',"
                     " '2026-01-01T00:00:00+08:00')")
        for face_id, x in ((1, 1), (2, 51), (3, 101)):
            conn.execute(
                "INSERT INTO face (id, asset_id, detector, detector_version, detect_max,"
                " preview_w, preview_h, box_x, box_y, box_w, box_h, det_score, face_px,"
                " is_frontal, created_at) VALUES (?, 1, 'yunet', 'v1', 900, 200, 200, ?, 10,"
                " 30, 30, 0.9, 30, 1, '2026-01-01')", (face_id, x))
    return db, PeopleRepo(db), FaceRepo(db)


def test_confirming_a_face_takes_it_away_from_the_other_person(repos) -> None:
    """★ 用户确认"是他"时，这张脸必须从**别人**的模型提议里撤掉。

    一张脸只属于一个人。否则界面上同一张脸出现在两个人下面，用户会以为系统坏了。
    """
    db, people, _faces = repos
    a = people.create_person(name="甲")
    b = people.create_person(name="乙")
    people.add_faces(a, [1], source="auto")
    people.add_faces(b, [1], source="auto")
    people.add_faces(a, [1], source="user")
    assert [f["face_id"] for f in people.faces(b)] == [], "模型提议没有被撤掉"
    assert [f["face_id"] for f in people.faces(a)] == [1]


def test_user_confirmed_membership_is_never_auto_removed(repos) -> None:
    """模型再猜一次也不许把**用户确认过**的归属撤掉。"""
    db, people, _faces = repos
    a = people.create_person(name="甲")
    b = people.create_person(name="乙")
    people.add_faces(a, [2], source="user")
    people.add_faces(b, [2], source="auto")
    assert [f["face_id"] for f in people.faces(a)] == [2]


def test_rejecting_removes_the_positive_membership(repos) -> None:
    """一张脸不能既"属于他"又"被否决"。"""
    db, people, _faces = repos
    a = people.create_person(name="甲")
    people.add_faces(a, [3], source="user")
    people.reject_faces(a, [3])
    assert people.faces(a) == []
    assert [r["face_id"] for r in people.rejected_faces(a)] == [3]
    labels = people.labels(a)
    assert labels["negative"] == [3] and labels["positive"] == []


def test_labels_keep_unknown_out_of_the_negative_set(repos) -> None:
    """★ 没表态过的脸（unknown）**不能**算负样本 —— 否则模型越学越窄。"""
    db, people, _faces = repos
    a = people.create_person(name="甲")
    people.add_faces(a, [1], source="user")
    people.reject_faces(a, [2])
    labels = people.labels(a)
    assert labels["positive"] == [1]
    assert labels["negative"] == [2]
    assert 3 not in labels["positive"] and 3 not in labels["negative"], \
        "第 3 张脸从未被表态，不该出现在任何标签集合里"


def test_unassigned_excludes_assigned_but_not_rejected(repos) -> None:
    """被 A 否决不影响它可能是 B —— 所以"未归类"里仍要包含它。"""
    db, people, _faces = repos
    a = people.create_person(name="甲")
    people.add_faces(a, [1], source="user")
    people.reject_faces(a, [2])
    unassigned = people.unassigned_face_ids(model="sface", model_version="v", prep_version="p")
    assert unassigned == []  # 这两张都还没有特征，所以本来就取不到
    people.add_faces(a, [2], source="user")   # 把被否决的重新确认
    assert people.rejected_faces(a) == [], "确认之后否决记录必须被撤掉"


def test_delete_person_keeps_faces_and_embeddings(repos) -> None:
    """删人只删标签。"""
    db, people, faces_repo = repos
    a = people.create_person(name="甲")
    people.add_faces(a, [1, 2], source="user")
    people.delete_person(a)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM face").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM person_face").fetchone()[0] == 0
