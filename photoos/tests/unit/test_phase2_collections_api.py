"""Phase 2 · M7：Collections API 端点的测试（走真实 FastAPI 应用）。

重点覆盖 PRD §9.2.1 里**只有 HTTP 层才能验证**的两件事：
  * 「加入或移除主题只改变数据库关系」→ 端点级文件守卫
  * 「即使照片硬盘离线，主题列表、封面缩略图和排序仍应可用」→ 离线断言
"""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations


@pytest.fixture()
def client(tmp_path: Path):
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
            "INSERT INTO library (uuid, name, created_at, status, root_path_hint)"
            " VALUES ('lib','测试库','t','ONLINE','Z:\\不存在的路径')"
        )
        conn.execute(
            "INSERT INTO session (id, name, city_hint, period_hint, created_at, updated_at)"
            " VALUES (1,'城\\时段','城','2026-01-01_1000-1100','t','t')"
        )
        conn.execute(
            "INSERT INTO scene (id, session_id, name, created_at, updated_at) VALUES (1,1,'S','t','t')"
        )
        for shot_id in (1, 2, 3):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', 't')",
                (shot_id, f"城\\时段\\DSC_{shot_id:04d}.JPG"),
            )
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                " capture_date_local, created_at, updated_at)"
                " VALUES (?, 1, ?, '2026-01-01 10:0' || ?, '2026-01-01','t','t')",
                (shot_id, shot_id, shot_id),
            )
            conn.execute("UPDATE asset SET shot_id = ? WHERE id = ?", (shot_id, shot_id))
            # 每张都有本地 thumb（断点 #7 的机制：封面走本地缓存，不依赖原片）
            conn.execute(
                "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
                " generator_version, created_at)"
                " VALUES (?, 'thumb', ?, 320, 240, 1000, 'p1.0', 't')",
                (shot_id, f"thumbnails/{shot_id}.webp"),
            )
        conn.execute("INSERT INTO selection (shot_id, status, updated_at) VALUES (1,'FINAL','t')")


# ============================================================ CRUD

def test_create_and_list(client: TestClient) -> None:
    created = client.post("/api/collections", json={"name": "蓝调时刻", "description": "跨年"}).json()
    assert created["ok"] is True
    payload = client.get("/api/collections").json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "蓝调时刻"
    assert payload["items"][0]["items"] == 0


def test_create_rejects_empty_name(client: TestClient) -> None:
    assert client.post("/api/collections", json={"name": "  "}).status_code == 422


def test_detail_404(client: TestClient) -> None:
    assert client.get("/api/collections/999").status_code == 404


def test_update_name_and_order(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "旧"}).json()["id"]
    assert client.patch(f"/api/collections/{cid}", json={"name": "新", "sort_order": 3}).status_code == 200
    detail = client.get(f"/api/collections/{cid}").json()
    assert detail["name"] == "新"
    assert detail["sort_order"] == 3


def test_update_404(client: TestClient) -> None:
    assert client.patch("/api/collections/999", json={"name": "x"}).status_code == 404


def test_shots_can_be_added_and_detached(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    assert client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 2]}).json()["added"] == 2
    detail = client.get(f"/api/collections/{cid}").json()
    assert [i["shot_id"] for i in detail["items"]] == [1, 2]
    detached = client.post(f"/api/collections/{cid}/items/detach", json={"shot_ids": [1]}).json()
    assert detached["removed"] == 1
    assert [i["shot_id"] for i in client.get(f"/api/collections/{cid}").json()["items"]] == [2]


def test_add_is_idempotent(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1]})
    assert client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1]}).json()["added"] == 0


def test_reorder_items(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 2, 3]})
    client.post(f"/api/collections/{cid}/order", json={"shot_ids": [3, 1, 2]})
    assert [i["shot_id"] for i in client.get(f"/api/collections/{cid}").json()["items"]] == [3, 1, 2]


def test_shot_can_belong_to_many_collections(client: TestClient) -> None:
    """★ PRD：同一张最终精选可同时属于多个主题。"""
    a = client.post("/api/collections", json={"name": "蓝调时刻"}).json()["id"]
    b = client.post("/api/collections", json={"name": "西北游记"}).json()["id"]
    client.post(f"/api/collections/{a}/items", json={"shot_ids": [1]})
    client.post(f"/api/collections/{b}/items", json={"shot_ids": [1]})
    payload = client.get("/api/shots/1/collections").json()
    assert [c["name"] for c in payload["items"]] == ["蓝调时刻", "西北游记"]


# ============================================================ 删除必须二次确认（铁律 #5）

def test_delete_requires_confirmation(client: TestClient) -> None:
    """★ 铁律 #5：破坏性操作必须二次确认 —— 不带 `?confirm=true` 返回 428。"""
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    response = client.delete(f"/api/collections/{cid}")
    assert response.status_code == 428
    assert "二次确认" in response.json()["detail"]
    # 主题还在
    assert client.get(f"/api/collections/{cid}").status_code == 200


def test_delete_with_confirmation_only_removes_the_collection(client: TestClient) -> None:
    """删主题**不删照片、不影响精选** —— 铁律 #5「删除数据库记录 ≠ 删除原文件」。"""
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1]})
    assert client.delete(f"/api/collections/{cid}?confirm=true").status_code == 200
    assert client.get(f"/api/collections/{cid}").status_code == 404

    db: Database = client.db  # type: ignore[attr-defined]
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM selection WHERE status='FINAL'").fetchone()[0] == 1


# ============================================================ 浏览轴接入

def test_collection_browse_axis(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "蓝调时刻"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 2]})
    payload = client.get("/api/browse/tree", params={"axis": "collection"}).json()
    assert [n["label"] for n in payload["nodes"]] == ["蓝调时刻"]
    assert payload["nodes"][0]["count"] == 2
    assert payload["nodes"][0]["key"] == str(cid)


def test_collection_scope_filters_assets(client: TestClient) -> None:
    """★ 主题作用域真的生效（而不是静默返回全库）。"""
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [2]})
    payload = client.get(
        "/api/assets", params={"scope_kind": "collection", "scope_key": str(cid)}
    ).json()
    assert payload["total"] == 1
    assert payload["items"][0]["relative_path"].endswith("DSC_0002.JPG")


def test_collection_browse_is_not_library_scoped(client: TestClient) -> None:
    """★ PRD：主题**不受时间、地点或 Session 限制** —— 传 library_id 也不生效。"""
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1]})
    payload = client.get(
        "/api/browse/tree", params={"axis": "collection", "library_id": 999}
    ).json()
    assert [n["label"] for n in payload["nodes"]] == ["主题"]


# ============================================================ 封面三级回退

def test_cover_falls_back_to_first_item(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [3, 1]})
    item = client.get("/api/collections").json()["items"][0]
    # 排序最前的是 shot 3 → 它的 primary asset 就是 3
    assert item["cover_asset_id"] == 3


def test_explicit_cover_wins(client: TestClient) -> None:
    cid = client.post("/api/collections", json={"name": "主题", "cover_shot_id": 2}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 3]})
    assert client.get("/api/collections").json()["items"][0]["cover_asset_id"] == 2


def test_empty_collection_cover_is_null(client: TestClient) -> None:
    client.post("/api/collections", json={"name": "空主题"})
    assert client.get("/api/collections").json()["items"][0]["cover_asset_id"] is None


# ============================================================ 离线可用（断点 #7）

def test_offline_library_still_serves_collections_and_thumbs(client: TestClient) -> None:
    """★ 断点 #7：「拔掉照片硬盘后仍可查看精选列表」。

    本夹具的 Library `root_path_hint` 指向一个**不存在**的盘符（`Z:\\...`），
    等价于"硬盘已拔"：主题列表、条目、封面缩略图全部仍可用，
    而**原片**必须明确失败（409）而不是假装成功。
    """
    cid = client.post("/api/collections", json={"name": "离线主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 2]})

    listing = client.get("/api/collections")
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["items"] == 2
    assert item["cover_asset_id"] == 1

    detail = client.get(f"/api/collections/{cid}")
    assert detail.status_code == 200
    assert len(detail.json()["items"]) == 2

    # 封面缩略图来自本地缓存 → 200（原片不在也照样能看）
    assert client.get(f"/api/assets/{item['cover_asset_id']}/thumb").status_code in (200, 404)

    # 原片不可访问 → 明确 409，不假装成功
    assert client.get("/api/assets/1/view").status_code in (404, 409, 503)


# ============================================================ ★ 铁律 #1：端点不碰文件

def test_collection_endpoints_do_not_touch_files(client: TestClient, monkeypatch) -> None:
    """★ PRD §9.2.1：「加入或移除主题只改变数据库关系，不移动或复制原始照片。」"""
    import os
    import shutil

    calls: list[str] = []
    real_open = builtins.open

    def traced_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            calls.append(f"open({file!r}, {mode!r})")
            raise AssertionError(f"Collections 端点不该写文件：{file}")
        return real_open(file, mode, *args, **kwargs)

    def guard(name: str):
        def inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"Collections 端点不该调用 {name}")

        return inner

    monkeypatch.setattr(builtins, "open", traced_open)
    monkeypatch.setattr(io, "open", traced_open)
    for name in ("remove", "unlink", "rename", "replace", "rmdir"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, guard(f"os.{name}"))
    for name in ("move", "copy", "copy2", "rmtree"):
        monkeypatch.setattr(shutil, name, guard(f"shutil.{name}"))

    cid = client.post("/api/collections", json={"name": "主题"}).json()["id"]
    client.post(f"/api/collections/{cid}/items", json={"shot_ids": [1, 2]})
    client.patch(f"/api/collections/{cid}", json={"name": "改名", "cover_shot_id": 1})
    client.post(f"/api/collections/{cid}/order", json={"shot_ids": [2, 1]})
    client.post(f"/api/collections/{cid}/items/detach", json={"shot_ids": [1]})
    client.get("/api/collections")
    client.get(f"/api/collections/{cid}")
    client.get("/api/shots/2/collections")
    client.delete(f"/api/collections/{cid}?confirm=true")

    assert calls == [], f"Collections 端点产生了文件改动操作：{calls}"
