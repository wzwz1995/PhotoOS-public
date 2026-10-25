"""场景挖掘：轮次目标、识别线、标签排除、状态推进。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from photoos.db import Database, apply_migrations
from photoos.library import scene_mining
from photoos.repos import SemanticRepo


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
        for shot_id, vector in (
            (1, (1.0, 0.0, 0.0, 0.0)),
            (2, (0.9, 0.1, 0.0, 0.0)),
            (3, (0.0, 1.0, 0.0, 0.0)),
        ):
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
                (shot_id, "embeddinggemma-2", "safetensors-fp32", 4,
                 "eg2-p1.0", _vec(*vector)),
            )
        conn.execute(
            "INSERT INTO semantic_query (id, query_text, prompt_text, text_kind, model,"
            " model_version, dim, normalization, vector, created_at, updated_at)"
            " VALUES (1,'月亮','moon','query','embeddinggemma-2','safetensors-fp32',4,"
            " 'l2',?,'2026-01-01','2026-01-01')",
            (_vec(1.0, 0.0, 0.0, 0.0),),
        )
    return database


def test_warmup_and_steady_sampling_targets() -> None:
    scores = np.asarray([0.80] * 20 + [0.70] * 40 + [0.50] * 140, dtype=np.float32)
    shot_ids = np.arange(1, len(scores) + 1, dtype=np.int64)
    warm, wc = scene_mining._select_indices(
        scores, threshold=0.70, round_no=1, shot_ids=shot_ids, feedback={})
    assert len(warm) == 60
    assert wc == {"certain": 20, "uncertain": 40}

    steady, sc = scene_mining._select_indices(
        scores, threshold=0.70, round_no=4, shot_ids=shot_ids, feedback={})
    assert len(steady) == 40
    assert sc == {"certain": 20, "uncertain": 20}, "确定不足 40 时必须用不确定补"


def test_recommended_line_separates_confirmed_labels() -> None:
    scores = np.asarray([0.82] * 10 + [0.28] * 10, dtype=np.float32)
    shot_ids = np.arange(1, 21, dtype=np.int64)
    feedback = {i: ("keep" if i <= 10 else "drop") for i in range(1, 21)}
    rec = scene_mining._recommend_threshold(scores, shot_ids, feedback)
    assert rec is not None
    assert 0.28 <= rec["threshold"] <= 0.82
    assert rec["precision"] >= 0.95
    assert rec["recall"] >= 0.95


def test_labels_are_excluded_from_discovery(db: Database) -> None:
    SemanticRepo(db).set_feedback(1, 3, "drop")
    status = scene_mining.scene_status(db, 1)
    assert status["confirmed"] == 1
    result = scene_mining.discover(db, 1)
    assert result["selected"] == 2
    assert {int(item["shot_id"]) for item in result["items"]} == {1, 2}
    assert result["round"] == 1
    assert result["phase"] == "warmup"


def test_round_advances_and_threshold_is_saved(db: Database) -> None:
    first = scene_mining.discover(db, 1, threshold=0.55)
    assert first["round"] == 1
    assert first["threshold"] == pytest.approx(0.55)
    second = scene_mining.discover(db, 1)
    assert second["round"] == 2
    assert second["threshold"] == pytest.approx(0.55)
    row = db.query_one("SELECT threshold, round FROM semantic_scene WHERE query_id = 1")
    assert row is not None
    assert float(row["threshold"]) == pytest.approx(0.55)
    assert int(row["round"]) == 2


def test_bad_threshold_rejected(db: Database) -> None:
    with pytest.raises(ValueError):
        scene_mining.discover(db, 1, threshold=1.5)
