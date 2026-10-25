"""画面词工作台：覆盖统计、命中重算、词义重建。

## 为什么单独一层

`semantic_rank.py` 只负责线性代数（rerank）；`repos.SemanticRepo` 只负责表读写。
“某个词现在是否过期、要不要刷新、刷新哪些”属于**运营状态**，放在这里，
这样 `serve` 的 API、后台任务和测试共用同一套口径。

## 两个成本完全不同的动作

1. **重算命中**：已有词向量 × 已有图片向量，numpy 几秒完成，不需要 torch。
2. **补图片向量**：要 AI venv（EmbeddingGemma 2 + torch），6,737 张在 CPU 上约
   9 小时，CUDA 上约半小时；它**不是按词**分摊的，所有词共用同一套图片向量。

所以本模块只做 1，以及给 2 算清楚“还缺多少”。2 由现有 `embed_gemma` 动作执行。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

from ..db import Database
from ..repos import EmbeddingRepo, SemanticRepo
from . import model_store, scene_mining, semantic_rank

#: 一个词最多保留多少条命中。与界面 `semantic/results` 的 limit 上限（300）
#: 保持有余量；超过这个数的尾部对“缩小范围”没有实际价值，只会让表变大。
TOP_HITS = 200


def _identity(db: Database) -> tuple[str, str, str]:
    """取当前词表实际使用的图片向量身份。

    `semantic_query` 只存词的 `model/model_version`；**预处理口径**属于
    图片向量，必须像 `semantic_rank.resolve_image_identity()` 一样从
    `embedding` 取最新写入的那个 prep_version，不能假设等于 EG2 的发布常量。
    """
    row = db.query_one(
        "SELECT model, model_version FROM semantic_query"
        " ORDER BY updated_at DESC, id DESC LIMIT 1"
    )
    if row is None:
        return (model_store.GEMMA_MODEL_NAME, model_store.GEMMA_MODEL_VERSION,
                model_store.GEMMA_PREP_VERSION)
    model, version = str(row["model"]), str(row["model_version"])
    prep = db.query_one(
        "SELECT prep_version FROM embedding WHERE model = ? AND model_version = ?"
        " GROUP BY prep_version ORDER BY MAX(created_at) DESC LIMIT 1",
        (model, version),
    )
    return (model, version, str(prep["prep_version"]) if prep else model_store.GEMMA_PREP_VERSION)


def _coverage(db: Database, identity: tuple[str, str, str]) -> dict[str, Any]:
    model, model_version, prep = identity
    total = int(db.scalar("SELECT COUNT(*) FROM asset", default=0))
    embedded = int(db.scalar(
        "SELECT COUNT(*) FROM asset a WHERE EXISTS ("
        " SELECT 1 FROM embedding e WHERE e.asset_id = a.id AND e.model = ?"
        " AND e.model_version = ? AND e.prep_version = ?)",
        (model, model_version, prep), default=0))
    pending = EmbeddingRepo(db).missing_assets(
        model=model, model_version=model_version, prep_version=prep)
    with_preview = len(pending)
    missing = max(0, total - embedded)
    without_preview = max(0, missing - with_preview)

    def _missing_count(*, video: bool, with_preview_required: bool) -> int:
        """按“照片/视频 × 是否有 preview”拆分，避免把视频说成照片。"""
        sql = (
            "SELECT COUNT(*) FROM asset a WHERE NOT EXISTS ("
            " SELECT 1 FROM embedding e WHERE e.asset_id = a.id AND e.model = ?"
            " AND e.model_version = ? AND e.prep_version = ?)"
        )
        params: list[Any] = [model, model_version, prep]
        if video:
            sql += " AND a.file_type = 'video'"
        else:
            sql += " AND a.file_type != 'video'"
        if with_preview_required:
            sql += (" AND a.status = 'OK' AND EXISTS ("
                    " SELECT 1 FROM preview p WHERE p.asset_id = a.id AND p.kind = 'preview')")
        return int(db.scalar(sql, params, default=0))

    missing_photo = _missing_count(video=False, with_preview_required=False)
    missing_video = _missing_count(video=True, with_preview_required=False)
    with_preview_photo = _missing_count(video=False, with_preview_required=True)
    with_preview_video = _missing_count(video=True, with_preview_required=True)
    without_preview_photo = max(0, missing_photo - with_preview_photo)
    without_preview_video = max(0, missing_video - with_preview_video)
    last = db.scalar(
        "SELECT MAX(computed_at) FROM semantic_query_hit", default=None)
    newest = db.scalar(
        "SELECT MAX(first_seen_at) FROM asset", default=None)
    return {
        "model": model,
        "model_version": model_version,
        "prep_version": prep,
        "total_assets": total,
        "embedded": embedded,
        "missing": missing,
        "missing_photo": missing_photo,
        "missing_video": missing_video,
        "missing_with_preview": with_preview,
        "missing_without_preview": without_preview,
        "missing_with_preview_photo": with_preview_photo,
        "missing_with_preview_video": with_preview_video,
        "missing_without_preview_photo": without_preview_photo,
        "missing_without_preview_video": without_preview_video,
        "last_hit_at": str(last) if last else None,
        "newest_asset_at": str(newest) if newest else None,
    }


def manage_payload(db: Database) -> dict[str, Any]:
    """工作台的一次快照。**只读**，不触发任何嵌入。"""
    identity = _identity(db)
    coverage = _coverage(db, identity)
    repo = SemanticRepo(db)
    items = repo.list_queries()
    meta: dict[int, tuple[str | None, int]] = {}
    for row in db.query(
            "SELECT query_id, MAX(computed_at) AS last_at, COUNT(*) AS n"
            "  FROM semantic_query_hit GROUP BY query_id"):
        meta[int(row["query_id"])] = (
            str(row["last_at"]) if row["last_at"] else None,
            int(row["n"]),
        )
    newest = coverage.get("newest_asset_at")
    scene_states = scene_mining.list_scene_states(db)
    out: list[dict[str, Any]] = []
    for item in items:
        query_id = int(item["id"])
        counts = repo.feedback_counts(query_id)
        last_at, hit_count = meta.get(query_id, (None, 0))
        scene = scene_states.get(query_id) or {
            "query_id": query_id, "threshold": scene_mining.DEFAULT_THRESHOLD,
            "round": 0, "updated_at": None,
        }
        stale = bool(coverage["missing_with_preview"])
        if last_at is None:
            stale = True
        elif newest and last_at < newest:
            stale = True
        out.append({
            **item,
            "id": query_id,
            "hit_count": hit_count,
            "last_hit_at": last_at,
            "feedback_keep": int(counts.get("keep", 0)),
            "feedback_drop": int(counts.get("drop", 0)),
            "scene": scene,
            "stale": stale,
        })
    return {
        "coverage": coverage,
        "items": out,
        "top_hits": TOP_HITS,
    }


def refresh_hits(db: Database, query_ids: Sequence[int] | None = None) -> dict[str, Any]:
    """按**当前**图片向量重算选中词的命中。

    - 只读 `semantic_query.vector` 和 `embedding`，不读原片、不跑模型；
    - 只替换 `semantic_query_hit`，不动反馈、tuning、词向量；
    - 词与图片的模型身份不一致时**拒绝**，不混算。
    """
    selected = [int(value) for value in (query_ids or [])]
    if selected:
        marks = ",".join("?" for _ in selected)
        rows = [dict(r) for r in db.query(
            f"SELECT * FROM semantic_query WHERE id IN ({marks}) ORDER BY id", selected)]
    else:
        rows = [dict(r) for r in db.query("SELECT * FROM semantic_query ORDER BY id")]
    if not rows:
        return {"updated": 0, "words": 0, "hits": 0, "skipped": []}

    model, version, prep = _identity(db)
    identity = (model, version, prep)
    if any((str(r["model"]), str(r["model_version"])) != (model, version)
           for r in rows):
        raise ValueError("选中的词不属于同一个模型身份，拒绝混算")

    pool = semantic_rank.build_pool(
        db, model=identity[0], model_version=identity[1], prep_version=identity[2])
    if len(pool) == 0:
        raise ValueError("库里还没有该模型身份的图片向量")

    updated = 0
    written = 0
    skipped: list[str] = []
    for row in rows:
        dim = int(row["dim"])
        raw = bytes(row["vector"])
        if len(raw) != dim * 4:
            skipped.append(f"{row['query_text']}: 词向量长度不符")
            continue
        vector = np.frombuffer(raw, dtype="<f4").astype("f4")
        scores = pool.matrix @ vector
        order = np.argsort(-scores, kind="stable")
        hits: list[tuple[int, int, float, bool]] = []
        for rank, index in enumerate(order[:min(TOP_HITS, len(order))]):
            shot_id = int(pool.shot_ids[int(index)])
            hits.append((shot_id, rank, float(scores[int(index)]), True))
        count = SemanticRepo(db).save_hits(
            query_text=str(row["query_text"]),
            prompt_text=row.get("prompt_text"),
            text_kind=str(row.get("text_kind") or "query"),
            model=identity[0], model_version=identity[1], dim=dim,
            normalization=str(row.get("normalization") or "l2"),
            vector=raw, hits=hits,
            concept_ratio=row.get("concept_ratio"),
        )
        updated += 1
        written += count
    return {
        "updated": updated,
        "words": len(rows),
        "hits": written,
        "skipped": skipped,
        "identity": identity,
    }


def all_query_ids(db: Database) -> list[int]:
    return [int(r["id"]) for r in db.query("SELECT id FROM semantic_query ORDER BY id")]


__all__ = ["TOP_HITS", "all_query_ids", "manage_payload", "refresh_hits"]
