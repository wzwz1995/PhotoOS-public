"""Phase 2 · M5：精选 API 端点的测试（走真实 FastAPI 应用 + 临时库）。

覆盖 PRD §9.2 的端到端行为：
  * 三级状态机 + 随时退回（HTTP 层）
  * 理由、过程追溯、被放弃了什么
  * 一个 Scene 留 1 张 / 2~N 张都合法
  * 退役 Shot 的操作被明确拒绝（409）而不是静默成功
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.repos import SelectionRepo


@pytest.fixture()
def client(tmp_path: Path):
    """一个 schema 6 的库 + 1 个 Session / 2 个 Scene / 3 个 Shot（不涉及任何照片文件）。"""
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        _seed(db)
        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.db = db  # type: ignore[attr-defined]
            yield test_client


def _seed(db: Database) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, captured_from, captured_to,"
            " created_at, updated_at)"
            " VALUES (1,'城\\2026-01-01_1000-1100','城','2026-01-01_1000-1100',"
            " '2026-01-01 10:00:00','2026-01-01 11:00:00','2026-01-01T00:00:00+08:00',"
            " '2026-01-01T00:00:00+08:00')"
        )
        for scene_id, name in ((1, "场景A"), (2, "场景B")):
            conn.execute(
                "INSERT INTO scene (id, session_id, name, captured_from, captured_to,"
                " created_at, updated_at) VALUES (?, 1, ?, ?, ?,"
                " '2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')",
                (scene_id, name, f"2026-01-01 10:0{scene_id}:00", f"2026-01-01 10:1{scene_id}:00"),
            )
        for shot_id, scene_id, minute in ((1, 1, 0), (2, 1, 1), (3, 2, 2)):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', '2026-01-01T00:00:00+08:00')",
                (shot_id, f"城\\2026-01-01_1000-1100\\DSC_{shot_id:04d}.JPG"),
            )
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                " capture_date_local, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, '2026-01-01',"
                " '2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')",
                (shot_id, scene_id, shot_id, f"2026-01-01 10:0{minute}:00"),
            )
            conn.execute("UPDATE asset SET shot_id = ? WHERE id = ?", (shot_id, shot_id))


# ============================================================ Session / Scene 列表

def test_sessions_endpoint(client: TestClient) -> None:
    payload = client.get("/api/sessions").json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["city"] == "城"
    assert item["period"] == "2026-01-01_1000-1100"
    assert item["scenes"] == 2
    assert item["shots"] == 3


def test_scenes_endpoint_lists_both(client: TestClient) -> None:
    payload = client.get("/api/scenes").json()
    assert [i["id"] for i in payload["items"]] == [1, 2]
    first = payload["items"][0]
    assert first["shots"] == 2
    assert first["reviewed"] == 0
    assert first["finals"] == 0


def test_scenes_endpoint_filters_by_session(client: TestClient) -> None:
    assert len(client.get("/api/scenes?session_id=1").json()["items"]) == 2
    assert len(client.get("/api/scenes?session_id=999").json()["items"]) == 0


def test_scene_detail_includes_shots_and_counts(client: TestClient) -> None:
    payload = client.get("/api/scenes/1").json()
    assert payload["id"] == 1
    assert len(payload["shots"]) == 2
    assert payload["counts"] == {
        "UNREVIEWED": 2, "CANDIDATE": 0, "SELECTED": 0, "FINAL": 0,
    }


def test_scene_detail_404(client: TestClient) -> None:
    assert client.get("/api/scenes/999").status_code == 404


# ============================================================ 设置状态

def test_set_selection_forward(client: TestClient) -> None:
    for status in (sel.CANDIDATE, sel.SELECTED, sel.FINAL):
        response = client.post("/api/shots/1/selection", json={"status": status})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status


def test_set_selection_with_reason(client: TestClient) -> None:
    body = client.post(
        "/api/shots/1/selection", json={"status": sel.SELECTED, "reason": "光最好"}
    ).json()
    assert body["reason"] == "光最好"


def test_set_selection_rollback(client: TestClient) -> None:
    client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    body = client.post("/api/shots/1/selection", json={"status": sel.UNREVIEWED}).json()
    assert body["status"] == sel.UNREVIEWED


def test_set_selection_rejects_bad_status(client: TestClient) -> None:
    response = client.post("/api/shots/1/selection", json={"status": "BOGUS"})
    assert response.status_code == 422


def test_set_selection_on_missing_shot(client: TestClient) -> None:
    assert client.post("/api/shots/999/selection", json={"status": sel.FINAL}).status_code == 404


def test_set_selection_on_retired_shot_is_rejected(client: TestClient) -> None:
    """★ 退役的 Shot 不能被操作 —— 明确 409，而不是静默成功写进一条孤儿状态。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00' WHERE id = 1")
    response = client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    assert response.status_code == 409
    assert "退役" in response.json()["detail"]


def test_repeated_same_status_reports_not_changed(client: TestClient) -> None:
    client.post("/api/shots/1/selection", json={"status": sel.SELECTED})
    body = client.post("/api/shots/1/selection", json={"status": sel.SELECTED}).json()
    assert body["changed"] is False


def test_counts_returned_with_update(client: TestClient) -> None:
    body = client.post("/api/shots/1/selection", json={"status": sel.FINAL}).json()
    assert body["counts"][sel.FINAL] == 1
    assert body["counts"][sel.UNREVIEWED] == 2


# ============================================================ 历史与摘要

def test_shot_history_records_every_change(client: TestClient) -> None:
    client.post("/api/shots/1/selection", json={"status": sel.CANDIDATE})
    client.post("/api/shots/1/selection", json={"status": sel.SELECTED, "reason": "构图最好"})
    client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    payload = client.get("/api/shots/1/history").json()
    assert payload["status"] == sel.FINAL
    assert [e["to_status"] for e in payload["events"]] == [
        sel.CANDIDATE, sel.SELECTED, sel.FINAL,
    ]
    assert payload["events"][1]["reason"] == "构图最好"


def test_shot_history_404(client: TestClient) -> None:
    assert client.get("/api/shots/999/history").status_code == 404


def test_selection_summary_lists_finals_and_abandoned(client: TestClient) -> None:
    """PRD：「记录选择过程：用户选了什么、放弃了什么、最终留下什么」。"""
    client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    client.post("/api/shots/2/selection", json={"status": sel.CANDIDATE})
    client.post("/api/shots/2/selection", json={"status": sel.UNREVIEWED, "reason": "糊了"})

    payload = client.get("/api/selection/summary").json()
    assert payload["counts"][sel.FINAL] == 1
    assert [f["asset_id"] for f in payload["finals"]] == [1]
    assert [a["shot_id"] for a in payload["abandoned"]] == [2]
    assert payload["abandoned"][0]["reason"] == "糊了"
    assert payload["labels"][sel.FINAL] == "最终精选"


def test_scene_detail_carries_the_timeline(client: TestClient) -> None:
    """断点 #8：从最终精选反查完整拍摄组，需要 Scene 级别的过程时间线。"""
    client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    client.post("/api/shots/2/selection", json={"status": sel.CANDIDATE})
    payload = client.get("/api/scenes/1").json()
    assert [e["to_status"] for e in payload["events"]] == [sel.FINAL, sel.CANDIDATE]
    assert payload["counts"][sel.FINAL] == 1


# ============================================================ 一个 Scene 留 N 张

def test_scene_can_hold_two_finals(client: TestClient) -> None:
    """PRD：一个 Scene 可以留 1 张，也可以留 2~N 张 —— 不设上限。"""
    client.post("/api/shots/1/selection", json={"status": sel.FINAL})
    client.post("/api/shots/2/selection", json={"status": sel.FINAL})
    payload = client.get("/api/scenes/1").json()
    assert payload["counts"][sel.FINAL] == 2
    assert payload["finals"] if "finals" in payload else True


def test_scene_progress_reflects_review_state(client: TestClient) -> None:
    client.post("/api/shots/1/selection", json={"status": sel.SELECTED})
    scene = client.get("/api/scenes").json()["items"][0]
    assert scene["shots"] == 2
    assert scene["reviewed"] == 1
    assert scene["needs_review"] == 1


# ============================================================ 铁律 #1：端点不碰文件

def test_selection_endpoints_do_not_touch_files(client: TestClient, monkeypatch) -> None:
    """★ 铁律 #1：精选全过程**不产生任何改动文件的操作**。

    在 API 层再断言一次（repo 层已有同样断言）—— 因为端点是"用户能触达的边界"，
    如果这里偷偷调用了文件操作，前面的 repo 断言是拦不住的。
    """
    import builtins
    import io
    import os
    import shutil

    calls: list[str] = []
    real_open = builtins.open

    def traced_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            calls.append(f"open({file!r}, {mode!r})")
            raise AssertionError(f"精选端点不该写文件：{file}")
        return real_open(file, mode, *args, **kwargs)

    def guard(name: str):
        def inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"精选端点不该调用 {name}")

        return inner

    monkeypatch.setattr(builtins, "open", traced_open)
    monkeypatch.setattr(io, "open", traced_open)
    for name in ("remove", "unlink", "rename", "replace", "rmdir"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, guard(f"os.{name}"))
    for name in ("move", "copy", "copy2", "rmtree"):
        monkeypatch.setattr(shutil, name, guard(f"shutil.{name}"))

    assert client.post("/api/shots/1/selection", json={"status": sel.FINAL}).status_code == 200
    assert client.post("/api/shots/2/selection", json={"status": sel.CANDIDATE}).status_code == 200
    assert client.get("/api/shots/1/history").status_code == 200
    assert client.get("/api/selection/summary").status_code == 200
    assert client.get("/api/scenes/1").status_code == 200
    assert calls == [], f"精选端点产生了文件改动操作：{calls}"
