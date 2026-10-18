"""Phase 4 · 搜索反馈与重排（`library/semantic_rank.py` + `SemanticRepo` 的反馈部分）。

这一组测试要回答三个问题，每个都用**故意的破坏**来验，而不是看代码写了什么：

1. 用户的表态**真的改变了排序**吗？（删掉的排除了吗？选中的升上来了吗？）
2. 学习**在什么条件下不生效**？（只有一条表态时不该重排 —— 否则退化成"找相似"）
3. 身份**对不上时会不会报错**？（文本 768 维配图像 512 维必须炸，不能广播）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from photoos.db import Database, apply_migrations
from photoos.library import semantic_rank as R
from photoos.repos import SemanticRepo

MODEL = "embeddinggemma-2"
MODEL_VERSION = "safetensors-fp32"
PREP = "eg2-p1.0"
DIM = 4


def _vec(*values: float) -> bytes:
    vec = np.asarray(values, dtype="<f4")
    return (vec / np.linalg.norm(vec)).astype("<f4").tobytes()


def _text_vec(*values: float) -> np.ndarray:
    vec = np.asarray(values, dtype="f4")
    return (vec / np.linalg.norm(vec)).astype("f4")


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    with database.write() as conn:
        conn.execute("INSERT INTO library (id, uuid, name, created_at, status)"
                     " VALUES (1,'lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO scene (id, name, created_at, updated_at)"
                     " VALUES (1,'S','2026-01-01','2026-01-01')")
        # 三张照片，向量分别朝向三个坐标轴 —— 这样"谁应该排在前面"是可以手算的
        vectors = {
            1: (1.0, 0.0, 0.0, 0.0),
            2: (0.0, 1.0, 0.0, 0.0),
            3: (0.0, 0.0, 1.0, 0.0),
        }
        for shot_id, values in vectors.items():
            conn.execute("INSERT INTO shot (id, scene_id, created_at, updated_at)"
                         " VALUES (?, 1, '2026-01-01', '2026-01-01')", (shot_id,))
            conn.execute("INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                         " first_seen_at, shot_id) VALUES (?, 1, ?, 'jpeg', 'OK',"
                         " '2026-01-01', ?)",
                         (shot_id, f"a\\b\\{shot_id}.jpg", shot_id))
            conn.execute(
                "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
                " input_kind, input_version, prep_version, vector, created_at)"
                " VALUES (?, ?, ?, ?, 'l2', 'preview', 'p1.0', ?, ?, '2026-01-01')",
                (shot_id, MODEL, MODEL_VERSION, DIM, PREP, _vec(*values)))
        # 词的文本向量朝向第 1 个轴 → 模型原始排序必须是 1, 2, 3
        conn.execute(
            "INSERT INTO semantic_query (id, query_text, prompt_text, text_kind, model,"
            " model_version, dim, normalization, vector, created_at, updated_at)"
            " VALUES (1, '日出', 'sunrise', 'query', ?, ?, ?, 'l2', ?, '2026-01-01',"
            " '2026-01-01')",
            (MODEL, MODEL_VERSION, DIM, _vec(1.0, 0.0, 0.0, 0.0)))
        for rank, shot_id in enumerate((1, 2, 3)):
            conn.execute(
                "INSERT INTO semantic_query_hit (query_id, shot_id, rank, score,"
                " computed_at) VALUES (1, ?, ?, ?, '2026-01-01')",
                (shot_id, rank, 1.0 - 0.1 * rank))
    return database


# ============================================================ 反馈本身

def test_feedback_is_idempotent_and_can_change_its_mind(db: Database) -> None:
    """同一个 (词, 照片) 改主意是 UPDATE，不是堆两行互相矛盾的记录。"""
    repo = SemanticRepo(db)
    repo.set_feedback(1, 2, "keep")
    repo.set_feedback(1, 2, "drop")
    mapping = repo.feedback_map(1)
    assert mapping == {2: "drop"}
    assert repo.feedback_counts(1) == {"keep": 0, "drop": 1}


def test_feedback_rejects_an_unknown_action(db: Database) -> None:
    with pytest.raises(ValueError):
        SemanticRepo(db).set_feedback(1, 2, "maybe")


def test_clear_feedback_can_target_specific_shots(db: Database) -> None:
    repo = SemanticRepo(db)
    repo.set_feedback(1, 1, "keep")
    repo.set_feedback(1, 2, "drop")
    assert repo.clear_feedback(1, [1]) == 1
    assert repo.feedback_map(1) == {2: "drop"}
    assert repo.clear_feedback(1) == 1
    assert repo.feedback_map(1) == {}


def test_feedback_disappears_with_the_shot(db: Database) -> None:
    """★ 反向验证：删掉 Shot，它的表态必须跟着走。

    否则 `semantic_tuning` 会拿着一个不存在的 Shot 算均值，
    重排结果里就永远混着一条幽灵。
    """
    repo = SemanticRepo(db)
    repo.set_feedback(1, 3, "keep")
    with db.write() as conn:
        conn.execute("DELETE FROM shot WHERE id = 3")
    assert repo.feedback_map(1) == {}


# ============================================================ 学习

def test_tuning_needs_at_least_two_signals(db: Database) -> None:
    """★ 只有一条表态时**不重排**。

    理由是数学的：一条反馈的"均值"就是那一张图本身，
    加上去等于"再找一张和它最像的"，那不是用户要的语义搜索。
    """
    repo = SemanticRepo(db)
    row = dict(db.query_one("SELECT * FROM semantic_query WHERE id = 1"))
    repo.set_feedback(1, 2, "keep")
    single = R.compute_tuning(db, query_row=row, feedback=repo.feedback_map(1))
    assert single.pos_count == 1 and not single.active

    repo.set_feedback(1, 3, "keep")
    pair = R.compute_tuning(db, query_row=row, feedback=repo.feedback_map(1))
    assert pair.pos_count == 2 and pair.active


def test_tuning_notes_say_when_there_is_nothing_to_lift(db: Database) -> None:
    repo = SemanticRepo(db)
    row = dict(db.query_one("SELECT * FROM semantic_query WHERE id = 1"))
    repo.set_feedback(1, 2, "drop")
    repo.set_feedback(1, 3, "drop")
    learned = R.compute_tuning(db, query_row=row, feedback=repo.feedback_map(1))
    assert learned.pos_vector is None
    assert any("选中" in note for note in learned.notes), \
        "没有正例时必须如实说明'只能往下压'，不能让用户以为学习是全向的"


def test_tuning_reports_shots_without_vectors(db: Database) -> None:
    """表态里混进一个没有向量的 Shot 时要说出来，而不是悄悄少算一张。"""
    repo = SemanticRepo(db)
    row = dict(db.query_one("SELECT * FROM semantic_query WHERE id = 1"))
    feedback = {2: "keep", 3: "keep", 99: "drop"}
    learned = R.compute_tuning(db, query_row=row, feedback=feedback)
    assert learned.pos_count == 2
    assert any("没有向量" in note for note in learned.notes)


# ============================================================ 重排

def _pool(db: Database) -> R.ShotPool:
    return R.build_pool(db, model=MODEL, model_version=MODEL_VERSION, prep_version=PREP)


def test_rerank_starts_from_the_model_order(db: Database) -> None:
    pool = _pool(db)
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=None,
                      feedback={}, limit=10)
    assert [r["shot_id"] for r in ranked] == [1, 2, 3]


def test_dropped_shots_are_excluded(db: Database) -> None:
    """★ 用户删掉的那张必须**从结果里消失**（不是只是排到后面）。"""
    pool = _pool(db)
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=None,
                      feedback={1: "drop"}, limit=10)
    assert 1 not in [r["shot_id"] for r in ranked]
    assert [r["shot_id"] for r in ranked] == [2, 3]


def test_learned_tuning_lifts_what_the_user_picked(db: Database) -> None:
    """★ 核心断言：学习项朝向某张照片时，它必须真的升到前面。

    这里把 `alpha` 放大到 1.2 才断言"翻盘"，因为**默认权重本来就翻不动大分差** ——
    这是刻意的设计（见下一条测试与 `semantic_rank` 的 docstring）：
    默认权重负责微调，真正把用户删掉的东西拿掉的是**硬排除**，不是打分。
    这条测试要证明的是"机制本身有效"，不是"默认参数下必然翻盘"。
    """
    pool = _pool(db)
    tuning = R.Tuning(pos_vector=_text_vec(0.0, 0.0, 1.0, 0.0), neg_vector=None,
                      pos_count=3, neg_count=0, alpha=1.2, beta=0.0)
    assert tuning.active
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=tuning,
                      feedback={}, limit=10)
    assert ranked[0]["shot_id"] == 3, "学习项足够大时，用户选中的方向必须排到前面"


def test_default_weight_decides_between_close_candidates(db: Database) -> None:
    """★ 默认权重的作用范围：**只有分差小于 α 的候选才可能被翻盘**。

    构造两个原始分完全相同的候选（文本向量落在第 1 与第 3 个轴的正中间），
    此时 0.35 的学习项足以把用户选中的那个顶上去。
    这条测试同时说明了默认权重的**边界** —— 它是微调，不是重写排序。
    """
    pool = _pool(db)
    middle = _text_vec(2 ** -0.5, 0.0, 2 ** -0.5, 0.0)
    before = R.rerank(pool, query_vector=middle, tuning=None, feedback={}, limit=10)
    assert before[0]["shot_id"] == 1, "前提：未学习时第 1 张在前（分数更高的并列项）"

    tuning = R.Tuning(pos_vector=_text_vec(0.0, 0.0, 1.0, 0.0), neg_vector=None,
                      pos_count=3, neg_count=0, alpha=R.DEFAULT_ALPHA, beta=0.0)
    after = R.rerank(pool, query_vector=middle, tuning=tuning, feedback={}, limit=10)
    assert after[0]["shot_id"] == 3, "分差小于 α 时，默认权重应当足以改变顺序"


def test_learned_tuning_cannot_override_a_large_base_gap(db: Database) -> None:
    """★ 反向验证：默认权重**翻不动**大分差。

    这是设计而不是缺陷：如果学习项能随意覆盖模型判断，
    用户误点一次就会让整页结果面目全非。真正"把删掉的东西拿掉"的是硬排除。
    这条断言把该边界钉住，避免有人日后悄悄调大 α 而破坏稳定性。
    """
    pool = _pool(db)
    tuning = R.Tuning(pos_vector=_text_vec(0.0, 0.0, 1.0, 0.0), neg_vector=None,
                      pos_count=3, neg_count=0, alpha=R.DEFAULT_ALPHA, beta=0.0)
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=tuning,
                      feedback={}, limit=10)
    assert ranked[0]["shot_id"] == 1, "原始分差远大于 α 时，顺序不该被学习项掀翻"


def test_learned_tuning_pushes_down_what_the_user_dropped(db: Database) -> None:
    pool = _pool(db)
    tuning = R.Tuning(pos_vector=None, neg_vector=_text_vec(0.0, 1.0, 0.0, 0.0),
                      pos_count=0, neg_count=3, alpha=0.0, beta=0.6)
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=tuning,
                      feedback={}, limit=10)
    assert ranked[-1]["shot_id"] == 2, "被否决的方向必须沉底"


def test_a_kept_shot_beyond_the_limit_is_still_returned(db: Database) -> None:
    """★ 用户亲手选进来的照片**必须出现**，哪怕它没排进前 N。

    否则会出现"我明明选了它，结果里却没有"这种最伤信任的表现。
    """
    pool = _pool(db)
    kept = {3: "keep"}
    ranked = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=None,
                      feedback=kept, limit=1)
    ids = [r["shot_id"] for r in ranked]
    assert 3 in ids, "用户选中的照片被 limit 截掉了"


def test_rerank_refuses_a_dimension_mismatch(db: Database) -> None:
    """★ 文本向量与图像向量维度不一致时必须报错。

    如果放任 numpy 广播，768 维的文本向量配上 512 维的图像矩阵会抛一个
    难以理解的 shape 错误，或者更糟 —— 在某些形状下**静默算出一个错的结果**。
    报错文案要能让人直接想到"两个模型身份串了"。
    """
    pool = _pool(db)
    with pytest.raises(ValueError, match="维度"):
        R.rerank(pool, query_vector=_text_vec(1.0, 0.0), tuning=None, feedback={}, limit=5)


def test_base_score_is_reported_alongside_the_new_score(db: Database) -> None:
    """重排后仍要能看到模型的原始分，用户才判断得出"学习改变了什么"。

    没有学习项时两个分**必须相等** —— 如果这里不等，说明重排在没有用户输入的情况下
    也动了分数，那"关掉学习看原样"这句话就是假的。
    """
    pool = _pool(db)
    # 真实运行时 base_scores 来自 `semantic_query_hit.score`，也就是同一批向量的点积；
    # 所以这里必须传**真实点积**，而不是随手编的数字（编的数字会让下面这条断言失去意义）。
    real = {1: 1.0, 2: 0.0, 3: 0.0}
    plain = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0), tuning=None,
                     feedback={}, limit=10, base_scores=real)
    assert plain[0]["base_score"] == 1.0
    assert plain[0]["score"] == plain[0]["base_score"], "没有学习项时不该改变分数"

    lifted = R.rerank(pool, query_vector=_text_vec(1.0, 0.0, 0.0, 0.0),
                      tuning=R.Tuning(pos_vector=_text_vec(0.0, 0.0, 1.0, 0.0),
                                      neg_vector=None, pos_count=3, neg_count=0,
                                      alpha=0.5, beta=0.0),
                      feedback={}, limit=10, base_scores=real)
    moved = {r["shot_id"]: r for r in lifted}
    assert moved[3]["base_score"] == 0.0
    assert moved[3]["score"] == pytest.approx(0.5), "被选中的方向应当被抬高 α 倍"
    assert moved[3]["score"] > moved[3]["base_score"]
    assert moved[1]["score"] == moved[1]["base_score"], "与学习项无关的照片分数不该变"


# ============================================================ 身份与持久化

def test_resolve_image_identity_reads_the_real_prep_version(db: Database) -> None:
    """不写死 `eg2-p1.0` —— 换预处理口径时重排要自动跟着走。"""
    row = dict(db.query_one("SELECT * FROM semantic_query WHERE id = 1"))
    assert R.resolve_image_identity(db, row) == (MODEL, MODEL_VERSION, PREP)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
            " input_kind, input_version, prep_version, vector, created_at)"
            " VALUES (1, ?, ?, ?, 'l2', 'preview', 'p1.0', 'eg2-p2.0', ?, '2026-02-01')",
            (MODEL, MODEL_VERSION, DIM, _vec(1.0, 0.0, 0.0, 0.0)))
    assert R.resolve_image_identity(db, row)[2] == "eg2-p2.0", \
        "新口径写入之后，重排必须用新的那一套"


def test_resolve_image_identity_fails_loudly_without_vectors(db: Database) -> None:
    row = dict(db.query_one("SELECT * FROM semantic_query WHERE id = 1"))
    row["model"] = "不存在的模型"
    with pytest.raises(ValueError, match="没有"):
        R.resolve_image_identity(db, row)


def test_save_tuning_upserts_one_row_per_query(db: Database) -> None:
    """`semantic_tuning` 每个词**只有一行**（口味是当前值），不是每次新增一行。"""
    repo = SemanticRepo(db)
    for alpha in (0.1, 0.5):
        repo.save_tuning(1, image_model=MODEL, image_model_version=MODEL_VERSION,
                         image_prep_version=PREP, dim=DIM, pos_vector=_vec(1.0, 0.0, 0.0, 0.0),
                         neg_vector=None, pos_count=2, neg_count=0, alpha=alpha, beta=0.25)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_tuning").fetchone()[0] == 1
    assert SemanticRepo(db).get_tuning(1)["alpha"] == 0.5


def test_tuning_disappears_with_the_query(db: Database) -> None:
    repo = SemanticRepo(db)
    repo.save_tuning(1, image_model=MODEL, image_model_version=MODEL_VERSION,
                     image_prep_version=PREP, dim=DIM, pos_vector=None, neg_vector=None,
                     pos_count=0, neg_count=0, alpha=0.35, beta=0.25)
    with db.write() as conn:
        conn.execute("DELETE FROM semantic_query WHERE id = 1")
    assert repo.get_tuning(1) is None


def test_pool_keeps_one_row_per_shot(db: Database) -> None:
    """一个 Shot 就是一个决策单元：**代表图**只有一行，不按 asset 重复计票。"""
    with db.write() as conn:
        conn.execute("INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                     " first_seen_at, shot_id) VALUES (99, 1, 'a\\b\\same.jpg', 'jpeg', 'OK',"
                     " '2026-01-01', 1)")
        conn.execute(
            "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
            " input_kind, input_version, prep_version, vector, created_at)"
            " VALUES (99, ?, ?, ?, 'l2', 'preview', 'p1.0', ?, ?, '2026-01-01')",
            (MODEL, MODEL_VERSION, DIM, PREP, _vec(1.0, 0.0, 0.0, 0.0)))
    pool = _pool(db)
    assert len(pool) == 3, "同一个 Shot 的多张 asset 不该让它在池子里出现两次"
    assert len(set(pool.shot_ids.tolist())) == 3


def test_pool_asks_for_vectors_it_can_decode(db: Database) -> None:
    """池子的维度必须来自库里的 `dim`，而不是某个写死的常量。"""
    pool = _pool(db)
    assert pool.dim == DIM


def test_shot_vectors_query_has_no_correlated_subquery(db: Database) -> None:
    """★ 回归（实测 41 秒的性能缺陷）：代表图查询不许用相关子查询。

    早先写的是 `JOIN asset a ON a.id = (SELECT ... WHERE a3.shot_id = s.id
    ORDER BY a3.id LIMIT 1)`。语义正确，但 SQLite 无法为"联接到子查询表达式"选索引，
    于是逐行重新求值 —— 实测 7,371 个 Shot 上要 **41 秒**，而它服务的重排只要 0.7 毫秒。
    "点一下等 40 秒"实际上等于功能不可用。

    这条测试**不看代码写了什么，只看执行计划**：只要计划里出现
    `CORRELATED`，就说明有人把它写回去了。
    功能测试抓不到这种退化（结果一模一样，只是慢 350 倍），所以必须由计划守住。
    """
    from photoos.repos import SHOT_VECTORS_SQL

    rows = db.query("EXPLAIN QUERY PLAN " + SHOT_VECTORS_SQL, (MODEL, MODEL_VERSION, PREP))
    plan = " | ".join(str(r["detail"]) for r in rows)
    assert "CORRELATED" not in plan.upper(), f"查询计划里出现了相关子查询：{plan}"


def test_the_query_plan_check_can_actually_fail(db: Database) -> None:
    """★ 反证：把相关子查询的写法喂给同一条判据，它必须认出来。

    没有这条，上面那条可能只是因为"SQLite 的计划文本里恰好没有 CORRELATED"而通过 ——
    那就是一个永远为真的空断言。
    """
    correlated = (
        "SELECT s.id FROM shot s"
        "  JOIN asset a ON a.id = (SELECT a3.id FROM asset a3 WHERE a3.shot_id = s.id"
        "                        ORDER BY a3.id LIMIT 1)"
        " WHERE s.retired_at IS NULL"
    )
    rows = db.query("EXPLAIN QUERY PLAN " + correlated)
    plan = " | ".join(str(r["detail"]) for r in rows)
    assert "CORRELATED" in plan.upper(), \
        f"这条反证失效了（SQLite 换了计划文本，判据要更新）：{plan}"
