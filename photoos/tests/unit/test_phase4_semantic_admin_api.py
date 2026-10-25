"""关键词工作台 HTTP 契约：覆盖、只读状态、按词刷新。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations


def _vec(*values: float) -> bytes:
    vec = np.asarray(values, dtype="<f4")
    return (vec / np.linalg.norm(vec)).astype("<f4").tobytes()


@pytest.fixture()
def client(tmp_path: Path):
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        with db.write() as conn:
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
                    "INSERT INTO embedding (asset_id, model, model_version, dim,"
                    " normalization, input_kind, input_version, prep_version, vector, created_at)"
                    " VALUES (?, 'embeddinggemma-2','safetensors-fp32',4,'l2','preview',"
                    " 'p1.0','eg2-p1.0',?,'2026-01-01')",
                    (shot_id, _vec(*vector)),
                )
            conn.execute(
                "INSERT INTO semantic_query (id, query_text, prompt_text, text_kind, model,"
                " model_version, dim, normalization, vector, created_at, updated_at)"
                " VALUES (1,'月亮','moon','query','embeddinggemma-2','safetensors-fp32',4,"
                " 'l2',?,'2026-01-01','2026-01-01')",
                (_vec(1.0, 0.0, 0.0, 0.0),),
            )
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def test_manage_endpoint_reports_coverage(client: TestClient) -> None:
    response = client.get("/api/semantic/manage")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["coverage"]["total_assets"] == 2
    assert data["coverage"]["embedded"] == 2
    assert data["coverage"]["missing"] == 0
    assert data["items"][0]["query_text"] == "月亮"


def test_refresh_endpoint_recomputes_selected(client: TestClient) -> None:
    response = client.post("/api/semantic/refresh", json={"query_ids": [1]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == 1
    assert body["hits"] == 2


def test_refresh_rejects_bad_query_ids(client: TestClient) -> None:
    response = client.post("/api/semantic/refresh", json={"query_ids": ["x"]})
    assert response.status_code == 422


def test_scene_endpoints_status_and_discover(client: TestClient) -> None:
    status = client.get("/api/semantic/scenes/1")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["query_text"] == "月亮"
    assert body["round"] == 0
    assert body["threshold"] >= 0

    first = client.post("/api/semantic/scenes/1/discover", json={"limit": 60, "threshold": 0.0})
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["round"] == 1
    assert payload["phase"] == "warmup"
    assert len(payload["items"]) == 2
    assert all(item["band"] in {"certain", "uncertain", "near"} for item in payload["items"])

    second = client.post("/api/semantic/scenes/1/discover", json={"threshold": 0.55})
    assert second.status_code == 200, second.text
    assert second.json()["round"] == 2
    assert second.json()["threshold"] == 0.55


def test_scene_discover_rejects_bad_threshold(client: TestClient) -> None:
    response = client.post("/api/semantic/scenes/1/discover", json={"threshold": 2})
    assert response.status_code == 422
