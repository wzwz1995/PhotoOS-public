"""关键词工作台：覆盖口径、命中刷新、反馈保真。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from photoos.db import Database, apply_migrations
from photoos.library import semantic_admin
from photoos.repos import SemanticRepo

MODEL = "embeddinggemma-2"
VERSION = "safetensors-fp32"
PREP = "eg2-p1.0"
DIM = 4


def _vec(*values: float) -> bytes:
    vec = np.asarray(values, dtype="<f4")
    return (vec / np.linalg.norm(vec)).astype("<f4").tobytes()


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "photoos.db")
    apply_migrations(database, backup_dir=tmp_path / "backups")
    with database.write() as conn:
        conn.execute("INSERT INTO library (id, uuid, name, created_at, status)"
                     " VALUES (1,'lib','L','2026-01-01T00:00:00+08:00','ONLINE')")
        conn.execute("INSERT INTO scene (id, name, created_at, updated_at)"
                     " VALUES (1,'S','2026-01-01','2026-01-01')")
        for shot_id, vector in ((1, (1.0, 0.0, 0.0, 0.0)), (2, (0.0, 1.0, 0.0, 0.0))):
            conn.execute("INSERT INTO shot (id, scene_id, created_at, updated_at)"
                         " VALUES (?,1,'2026-01-01','2026-01-01')", (shot_id,))
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                " first_seen_at, shot_id) VALUES (?,1,?,'jpeg','OK','2026-01-01',?)",
                (shot_id, f"a\\{shot_id}.jpg", shot_id),
            )
            conn.execute(
                "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
                " input_kind, input_version, prep_version, vector, created_at)"
                " VALUES (?,?,?,?,'l2','preview','p1.0',?,?,'2026-01-01')",
                (shot_id, MODEL, VERSION, DIM, PREP, _vec(*vector)),
            )
        conn.execute(
            "INSERT INTO semantic_query (id, query_text, prompt_text, text_kind, model,"
            " model_version, dim, normalization, vector, created_at, updated_at)"
            " VALUES (1,'月亮','moon','query',?,?,?,'l2',?,'2026-01-01','2026-01-01')",
            (MODEL, VERSION, DIM, _vec(1.0, 0.0, 0.0, 0.0)),
        )
        # 旧命中只有 1，且 feedback 里有一条 drop（刷新不应该抹掉它）
        conn.execute(
            "INSERT INTO semantic_query_hit (query_id, shot_id, rank, score, computed_at)"
            " VALUES (1,1,0,1.0,'2025-12-31')"
        )
    SemanticRepo(database).set_feedback(1, 2, "drop")
    return database


def test_manage_payload_reports_coverage_and_staleness(db: Database) -> None:
    payload = semantic_admin.manage_payload(db)
    coverage = payload["coverage"]
    assert coverage["total_assets"] == 2
    assert coverage["embedded"] == 2
    assert coverage["missing"] == 0
    assert coverage["missing_photo"] == 0
    assert coverage["missing_video"] == 0
    assert coverage["last_hit_at"] == "2025-12-31"
    item = payload["items"][0]
    assert item["query_text"] == "月亮"
    assert item["feedback_drop"] == 1
    # 最后命中早于最新 asset；这是“快照可能过期”的保守判断
    assert item["stale"] is True


def test_refresh_hits_includes_new_shot_and_preserves_feedback(db: Database) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at)"
            " VALUES (3,1,'2026-01-01','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
            " first_seen_at, shot_id) VALUES (3,1,'a\\3.jpg','jpeg','OK','2026-01-02',3)"
        )
        conn.execute(
            "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
            " input_kind, input_version, prep_version, vector, created_at)"
            " VALUES (3,?,?,?,'l2','preview','p1.0',?,?,'2026-01-02')",
            (MODEL, VERSION, DIM, PREP, _vec(1.0, 0.0, 0.0, 0.0)),
        )
    result = semantic_admin.refresh_hits(db, [1])
    assert result["updated"] == 1
    assert result["words"] == 1
    assert result["hits"] == 3
    hits = SemanticRepo(db).hits(1, limit=10)
    assert {int(h["shot_id"]) for h in hits} == {1, 2, 3}
    assert SemanticRepo(db).feedback_map(1) == {2: "drop"}
    # 反馈没有被刷新改写
    assert SemanticRepo(db).feedback_counts(1)["drop"] == 1
