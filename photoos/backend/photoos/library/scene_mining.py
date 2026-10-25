"""关键词场景挖掘（MVP）。

一个“场景”不是一次 top-N 检索，而是一个持续更新的集合：

* 人工标签存在 `semantic_feedback`（keep = 是，drop = 不是）；
* 场景分数由**已确认标签的均值向量**给出（正例均值，负例做减分）；
* 全库每个活跃 Shot 都打分；
* 「发现」按识别线抽出 60 张未确认候选：
  - 前 3 轮：40 不确定 + 20 确定；
  - 第 4 轮起：40 确定，确定不足用不确定补；
* 新照片不需要单独处理：它们本来就在全库池里，下一轮自然会被打分。

这是一条可复算的轻量模型，不依赖 torch；它只做候选挖掘，不碰精选/主题。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

from ..db import Database, utc_now
from ..repos import SemanticRepo
from . import semantic_rank

#: 60 张/轮（产品决定；接口仍保留 limit 但默认就是 60）
BATCH_SIZE = 60

#: 识别线上下这么宽的分数带算“不确定”。raw cosine 的尺度约 0.55–0.95。
LINE_BAND = 0.04

#: 前 3 轮（含冷启动）每轮 40 不确定 + 20 确定；之后 40 确定。
WARMUP_ROUNDS = 3
WARMUP_UNCERTAIN = 40
WARMUP_CERTAIN = 20
STEADY_CERTAIN = 40

DEFAULT_THRESHOLD = 0.70


def _query(db: Database, query_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM semantic_query WHERE id = ?", (int(query_id),))
    if row is None:
        raise LookupError(f"没有这个关键词：{query_id}")
    return dict(row)


def _state(db: Database, query_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM semantic_scene WHERE query_id = ?", (int(query_id),))
    if row is None:
        return {
            "query_id": int(query_id),
            "threshold": DEFAULT_THRESHOLD,
            "round": 0,
            "created_at": None,
            "updated_at": None,
        }
    return dict(row)


def _recommend_threshold(scores: np.ndarray, shot_ids: np.ndarray,
                         feedback: dict[int, str]) -> dict[str, Any] | None:
    """用已确认正负例估算一条平衡识别线（仅用于给建议，不自动覆盖用户滑杆）。

    只给指标和建议值：用户仍可把线拉低换召回。没有足够的正负例时返回 None。
    """
    pos = [
        float(scores[i]) for i, sid in enumerate(shot_ids)
        if feedback.get(int(sid)) == "keep"
    ]
    neg = [
        float(scores[i]) for i, sid in enumerate(shot_ids)
        if feedback.get(int(sid)) == "drop"
    ]
    if len(pos) < 5 or len(neg) < 5:
        return None
    pos_arr = np.asarray(pos, dtype=np.float32)
    neg_arr = np.asarray(neg, dtype=np.float32)
    candidates = np.unique(np.concatenate([pos_arr, neg_arr]))
    best: dict[str, Any] | None = None
    best_key = (-1.0, -1.0)
    fallback: dict[str, Any] | None = None
    fallback_key = (-1.0, -1.0)
    unlabeled_scores = np.asarray(
        [float(scores[i]) for i, sid in enumerate(shot_ids)
         if int(sid) not in feedback], dtype=np.float32)
    for threshold in candidates:
        tp = int((pos_arr >= threshold).sum())
        fp = int((neg_arr >= threshold).sum())
        if tp == 0:
            continue
        precision = tp / (tp + fp)
        recall = tp / len(pos_arr)
        if precision < 0.60:
            continue
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        unconfirmed_above = int((unlabeled_scores >= threshold).sum())
        item = {
            "threshold": round(float(threshold), 6),
            "precision": round(float(precision), 4),
            "recall": round(float(recall), 4),
            "unconfirmed_above": unconfirmed_above,
            "positive_labels": len(pos),
            "negative_labels": len(neg),
        }
        key = (f1, precision)
        if key > fallback_key:
            fallback_key = key
            fallback = item
        # 建议线不能高到“一个未确认候选都没有”：那会让复核页失去意义。
        # 至少留 20 个过线未确认；否则用户点“用建议线”只会得到空池。
        if unconfirmed_above < 20:
            continue
        if key > best_key:
            best_key = key
            best = item
    return best or fallback


def _default_threshold(scores: np.ndarray, shot_ids: np.ndarray,
                       feedback: dict[int, str]) -> float:
    """首次进入场景时的识别线：有标签时用建议线，否则用 70% 分位。"""
    recommended = _recommend_threshold(scores, shot_ids, feedback)
    if recommended is not None:
        return float(recommended["threshold"])
    values = [
        float(scores[i]) for i, sid in enumerate(shot_ids)
        if int(sid) not in feedback
    ]
    if not values:
        return DEFAULT_THRESHOLD
    return float(np.clip(np.quantile(np.asarray(values, dtype=np.float32), 0.70), 0.05, 0.99))


def _ensure_state(db: Database, query_id: int, threshold: float) -> dict[str, Any]:
    state = _state(db, query_id)
    if state["created_at"] is None:
        now = utc_now()
        with db.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO semantic_scene"
                " (query_id, threshold, round, created_at, updated_at)"
                " VALUES (?, ?, 0, ?, ?)",
                (int(query_id), float(threshold), now, now),
            )
        state = _state(db, query_id)
    return state


def _save_state(db: Database, query_id: int, *, threshold: float, round_no: int) -> None:
    now = utc_now()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO semantic_scene"
            " (query_id, threshold, round, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(query_id) DO UPDATE SET"
            " threshold = excluded.threshold, round = excluded.round, updated_at = excluded.updated_at",
            (int(query_id), float(threshold), int(round_no), now, now),
        )


def _decode_query_vector(row: dict[str, Any]) -> np.ndarray:
    raw = row.get("vector")
    dim = int(row.get("dim") or 0)
    if raw is None or dim <= 0:
        raise ValueError("这个关键词没有文本向量，无法初始化场景")
    data = bytes(raw)
    if len(data) != dim * 4:
        raise ValueError(f"关键词向量字节数不符：期望 {dim * 4}，实际 {len(data)}")
    vec = np.frombuffer(data, dtype="<f4").astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        raise ValueError("关键词向量为零向量")
    return vec / norm


def _scores(
    db: Database,
    *,
    pool: semantic_rank.ShotPool,
    query_row: dict[str, Any],
    feedback: dict[int, str],
) -> tuple[np.ndarray, str, semantic_rank.Tuning]:
    """返回 (每个 Shot 的场景分, 'scene'|'text', tuning)。

    有正例时用正例均值；反例作为减分项。没有正例时退回文本检索。
    这样冷启动不需要负样本，标签一出现就自动升级成场景模型。
    """
    tuning = semantic_rank.compute_tuning(db, query_row=query_row, feedback=feedback)
    if tuning.pos_vector is not None:
        score = pool.matrix @ tuning.pos_vector
        if tuning.neg_vector is not None:
            # 用固定的小系数，不把“不是”压得过狠；阈值由用户调。
            score = score - 0.35 * (pool.matrix @ tuning.neg_vector)
        return np.clip(score.astype(np.float32), 0.0, 1.0), "scene", tuning

    query_vec = _decode_query_vector(query_row)
    score = pool.matrix @ query_vec
    return np.clip(score.astype(np.float32), 0.0, 1.0), "text", tuning


def _take(sorted_indices: Sequence[int], n: int, used: set[int], out: list[int]) -> None:
    for index in sorted_indices:
        if n <= 0:
            return
        index = int(index)
        if index in used:
            continue
        used.add(index)
        out.append(index)
        n -= 1


def _select_indices(
    scores: np.ndarray,
    *,
    threshold: float,
    round_no: int,
    shot_ids: np.ndarray,
    feedback: dict[int, str],
) -> tuple[list[int], dict[str, int]]:
    """按轮次目标挑候选。返回 (pool 下标, 每类实际数量)。"""
    unlabeled = [
        int(i) for i, sid in enumerate(shot_ids)
        if int(sid) not in feedback
    ]
    if not unlabeled:
        return [], {"certain": 0, "uncertain": 0}
    certain = [i for i in unlabeled if float(scores[i]) >= threshold + LINE_BAND]
    uncertain = [i for i in unlabeled if abs(float(scores[i]) - threshold) <= LINE_BAND]
    certain.sort(key=lambda i: (-float(scores[i]), int(shot_ids[i])))
    uncertain.sort(key=lambda i: (abs(float(scores[i]) - threshold), -float(scores[i]), int(shot_ids[i])))

    warmup = round_no <= WARMUP_ROUNDS
    target_certain = WARMUP_CERTAIN if warmup else STEADY_CERTAIN
    target_uncertain = WARMUP_UNCERTAIN if warmup else 0
    target_total = BATCH_SIZE if warmup else STEADY_CERTAIN
    used: set[int] = set()
    picked: list[int] = []
    _take(certain, target_certain, used, picked)
    _take(uncertain, target_uncertain, used, picked)

    # 第一轮起就可能出现“确定/不确定池不够”的情况。用户明确要求：
    # 确定不足用不确定补；反方向也允许不确定不足时用确定补，避免整轮空转。
    if len(picked) < target_total:
        # 只补“不确定”池；不再从分数线以下很远的地方硬凑。宁可这一轮少于
        # 60/40，也不要为了凑数给用户一堆明显无关的照片。
        _take([i for i in uncertain if i not in used], target_total - len(picked), used, picked)

    picked.sort(key=lambda i: (
        0 if abs(float(scores[i]) - threshold) <= LINE_BAND else 1,
        -float(scores[i]),
        int(shot_ids[i]),
    ))
    selected = picked[:target_total]
    certain_count = sum(1 for i in selected if float(scores[i]) >= threshold + LINE_BAND)
    return selected, {
        "certain": certain_count,
        "uncertain": max(0, len(selected) - certain_count),
    }


def _band(score: float, threshold: float) -> str:
    if score >= threshold + LINE_BAND:
        return "certain"
    if abs(score - threshold) <= LINE_BAND:
        return "uncertain"
    return "near"


def _items_by_ids(
    db: Database,
    *,
    shot_ids: Sequence[int],
    scores: dict[int, float],
    threshold: float,
) -> list[dict[str, Any]]:
    """取候选的卡片元数据（代表图、路径、拍摄时刻、精选状态）。"""
    ids = [int(s) for s in shot_ids]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = db.query(
        "SELECT s.id AS shot_id, s.captured_at, s.camera_key,"
        "       sc.name AS scene_name,"
        "       a.id AS asset_id, a.relative_path,"
        "       m.capture_at, COALESCE(sel.status, 'UNREVIEWED') AS selection_status,"
        "       (SELECT COUNT(*) FROM asset a2 WHERE a2.shot_id = s.id) AS asset_count"
        "  FROM shot s"
        "  JOIN asset a ON a.id = ("
        "        SELECT a3.id FROM asset a3 WHERE a3.shot_id = s.id ORDER BY a3.id LIMIT 1)"
        "  LEFT JOIN scene sc ON sc.id = s.scene_id"
        "  LEFT JOIN metadata m ON m.asset_id = a.id"
        "  LEFT JOIN selection sel ON sel.shot_id = s.id"
        f" WHERE s.id IN ({marks})",
        ids,
    )
    by_id = {int(r["shot_id"]): dict(r) for r in rows}
    out: list[dict[str, Any]] = []
    for sid in ids:
        row = by_id.get(int(sid))
        if row is None:
            continue
        score = float(scores[int(sid)])
        row["score"] = round(score, 6)
        row["band"] = _band(score, threshold)
        out.append(row)
    return out


def _summary(
    db: Database,
    *,
    query_row: dict[str, Any],
    scores: np.ndarray,
    shot_ids: np.ndarray,
    feedback: dict[int, str],
    threshold: float,
    round_no: int,
    mode: str,
    tuning: semantic_rank.Tuning,
) -> dict[str, Any]:
    unlabeled = [
        int(i) for i, sid in enumerate(shot_ids)
        if int(sid) not in feedback
    ]
    above = [i for i in unlabeled if float(scores[i]) >= threshold]
    certain = [i for i in unlabeled if float(scores[i]) >= threshold + LINE_BAND]
    uncertain = [i for i in unlabeled if abs(float(scores[i]) - threshold) <= LINE_BAND]
    recommended = _recommend_threshold(scores, shot_ids, feedback)
    return {
        "query_id": int(query_row["id"]),
        "query_text": query_row["query_text"],
        "prompt_text": query_row.get("prompt_text"),
        "threshold": round(float(threshold), 6),
        "round": int(round_no),
        "phase": "warmup" if round_no <= WARMUP_ROUNDS else "steady",
        "mode": mode,
        "has_positive": tuning.pos_count > 0,
        "pos_count": tuning.pos_count,
        "neg_count": tuning.neg_count,
        "confirmed": len(feedback),
        "confirmed_keep": sum(1 for a in feedback.values() if a == "keep"),
        "confirmed_drop": sum(1 for a in feedback.values() if a == "drop"),
        "shots": len(shot_ids),
        "unconfirmed": len(unlabeled),
        "above_line": len(above),
        "certain_pool": len(certain),
        "uncertain_pool": len(uncertain),
        "targets": {
            "certain": WARMUP_CERTAIN if round_no <= WARMUP_ROUNDS else STEADY_CERTAIN,
            "uncertain": WARMUP_UNCERTAIN if round_no <= WARMUP_ROUNDS else 0,
        },
        "recommended": recommended,
    }


def scene_status(db: Database, query_id: int) -> dict[str, Any]:
    query_row = _query(db, int(query_id))
    state = _state(db, int(query_id))
    image_model, image_version, prep = semantic_rank.resolve_image_identity(db, query_row)
    pool = semantic_rank.build_pool(
        db, model=image_model, model_version=image_version, prep_version=prep)
    feedback = SemanticRepo(db).feedback_map(int(query_id))
    scores, mode, tuning = _scores(
        db, pool=pool, query_row=query_row, feedback=feedback)
    threshold = float(state["threshold"])
    if state["created_at"] is None:
        threshold = _default_threshold(scores, pool.shot_ids, feedback)
    summary = _summary(
        db, query_row=query_row, scores=scores, shot_ids=pool.shot_ids,
        feedback=feedback, threshold=threshold,
        round_no=int(state["round"]), mode=mode, tuning=tuning)
    return summary


def discover(
    db: Database,
    query_id: int,
    *,
    threshold: float | None = None,
    limit: int = BATCH_SIZE,
) -> dict[str, Any]:
    """挖一批候选，并把这一轮写入场景状态。

    这是**唯一**会推进 round 的入口；GET status 不会。
    """
    query_row = _query(db, int(query_id))
    state = _state(db, int(query_id))
    limit = max(1, min(int(limit), BATCH_SIZE))

    image_model, image_version, prep = semantic_rank.resolve_image_identity(db, query_row)
    pool = semantic_rank.build_pool(
        db, model=image_model, model_version=image_version, prep_version=prep)
    if len(pool) == 0:
        raise ValueError("库里还没有这个模型身份的图片向量")
    feedback = SemanticRepo(db).feedback_map(int(query_id))
    scores, mode, tuning = _scores(
        db, pool=pool, query_row=query_row, feedback=feedback)

    if threshold is None:
        threshold = (float(state["threshold"]) if state["created_at"] is not None
                     else _default_threshold(scores, pool.shot_ids, feedback))
    threshold = float(threshold)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("识别线必须在 0–1 之间")
    _ensure_state(db, int(query_id), threshold)

    round_no = int(state["round"]) + 1
    selected, counts = _select_indices(
        scores, threshold=threshold, round_no=round_no,
        shot_ids=pool.shot_ids, feedback=feedback)
    selected = selected[:limit]
    # limit 若被调用方调小，候选分类计数也必须跟着截断后的集合走。
    counts = {
        "certain": sum(1 for i in selected if float(scores[i]) >= threshold + LINE_BAND),
        "uncertain": sum(1 for i in selected if abs(float(scores[i]) - threshold) <= LINE_BAND),
    }
    score_by_id = {
        int(pool.shot_ids[i]): float(scores[i]) for i in selected
    }
    items = _items_by_ids(
        db, shot_ids=list(score_by_id.keys()), scores=score_by_id,
        threshold=threshold)

    _save_state(db, int(query_id), threshold=threshold, round_no=round_no)
    summary = _summary(
        db, query_row=query_row, scores=scores, shot_ids=pool.shot_ids,
        feedback=feedback, threshold=threshold, round_no=round_no,
        mode=mode, tuning=tuning)
    summary.update({
        "items": items,
        "selected": len(items),
        "selected_certain": counts["certain"],
        "selected_uncertain": counts["uncertain"],
        "next_round": round_no + 1,
    })
    return summary


def list_scene_states(db: Database) -> dict[int, dict[str, Any]]:
    """工作台用：一次读全部场景状态，避免每个词一次查询。"""
    rows = db.query("SELECT query_id, threshold, round, updated_at FROM semantic_scene")
    return {int(r["query_id"]): dict(r) for r in rows}


__all__ = [
    "BATCH_SIZE",
    "DEFAULT_THRESHOLD",
    "LINE_BAND",
    "STEADY_CERTAIN",
    "WARMUP_CERTAIN",
    "WARMUP_ROUNDS",
    "WARMUP_UNCERTAIN",
    "discover",
    "list_scene_states",
    "scene_status",
]
