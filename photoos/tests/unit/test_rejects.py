"""废片标记 + 可还原删除（回收站）的单元测试。

覆盖：
  * 标记/取消标记只写库；
  * 汇总（数量/大小/命中精选主题的"保护"计数）；
  * 清理只处理废片、拒绝越界路径、跳过受保护照片；
  * 清理必须显式确认（428）；
  * 回收站调用是**可注入**的 —— 测试里绝不真删文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations, utc_now
from photoos.library import rejects as rejects_mod
from photoos.library.store import LibraryStore, write_marker
from photoos.repos import AssetRepo


@pytest.fixture()
def env(tmp_path: Path):
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        root = tmp_path / "library"
        root.mkdir()
        write_marker(root, "uuid-reject", "废片测试库")
        library = LibraryStore(db).create(
            name="废片测试库", root=root, library_uuid="uuid-reject", marker_uuid="uuid-reject"
        )
        rows = []
        for name in ("a.JPG", "b.JPG"):
            path = root / name
            path.write_bytes(name.encode())
            stat = path.stat()
            rows.append((name, stat.st_size, stat.st_mtime_ns, "jpeg", "image/jpeg"))
        AssetRepo(db).upsert_discovered(library.id, rows, now=utc_now())
        assets = {
            str(row["relative_path"]): int(row["id"])
            for row in db.query("SELECT id, relative_path FROM asset")
        }
        app = create_app(settings)
        with TestClient(app) as client:
            client.settings = settings  # type: ignore[attr-defined]
            client.db = db  # type: ignore[attr-defined]
            client.library = library  # type: ignore[attr-defined]
            client.library_root = root  # type: ignore[attr-defined]
            client.assets = assets  # type: ignore[attr-defined]
            yield client


def test_mark_and_unmark_only_touch_the_database(env: TestClient) -> None:
    asset_id = env.assets["a.JPG"]  # type: ignore[attr-defined]
    marked = env.post(f"/api/assets/{asset_id}/reject", json={"marked": True}).json()
    assert marked == {"asset_id": asset_id, "marked": True}
    assert rejects_mod.is_marked(env.db, asset_id)  # type: ignore[attr-defined]
    # 原片还在，没被移动/删除
    assert (env.library_root / "a.JPG").is_file()  # type: ignore[attr-defined]

    unmarked = env.post(f"/api/assets/{asset_id}/reject", json={"marked": False}).json()
    assert unmarked["marked"] is False
    assert not rejects_mod.is_marked(env.db, asset_id)  # type: ignore[attr-defined]


def test_summary_reports_count_bytes_and_protection(env: TestClient, monkeypatch) -> None:
    a = env.assets["a.JPG"]  # type: ignore[attr-defined]
    b = env.assets["b.JPG"]  # type: ignore[attr-defined]
    env.post(f"/api/assets/{a}/reject", json={"marked": True})
    env.post(f"/api/assets/{b}/reject", json={"marked": True})
    # 假装 a 的 Shot 进了精选（不依赖 shot/selection 的真实构造）
    original = rejects_mod.items
    monkeypatch.setattr(
        rejects_mod,
        "items",
        lambda db: [
            rejects_mod.RejectItem(item.asset_id, item.library_id, item.relative_path,
                                   item.size, item.shot_id, item.marked_at,
                                   selected=item.asset_id == a, collected=False)
            for item in original(db)
        ],
    )
    payload = env.get("/api/rejects").json()
    assert payload["count"] == 2
    assert payload["bytes"] == len(b"a.JPG") + len(b"b.JPG")
    assert payload["protected"] == 1
    assert {item["asset_id"] for item in payload["items"]} == {a, b}


def test_cleanup_requires_confirmation(env: TestClient) -> None:
    response = env.post("/api/rejects/cleanup", json={})
    assert response.status_code == 428


def test_cleanup_rejects_empty_mark_set(env: TestClient) -> None:
    response = env.post("/api/rejects/cleanup", json={"confirmed": True})
    assert response.status_code == 400
    assert "没有已标记" in response.json()["message"]


def test_cleanup_skips_protected_and_recycles_the_rest(env: TestClient, monkeypatch) -> None:
    a = env.assets["a.JPG"]  # type: ignore[attr-defined]
    b = env.assets["b.JPG"]  # type: ignore[attr-defined]
    rejects_mod.mark(env.db, a)  # type: ignore[attr-defined]
    rejects_mod.mark(env.db, b)  # type: ignore[attr-defined]

    original_items = rejects_mod.items
    rows = original_items(env.db)  # type: ignore[attr-defined]
    crafted = []
    for item in rows:
        if item.asset_id == a:
            crafted.append(rejects_mod.RejectItem(
                item.asset_id, item.library_id, item.relative_path, item.size,
                item.shot_id, item.marked_at, selected=True))
        else:
            crafted.append(item)
    monkeypatch.setattr(rejects_mod, "items", lambda db: crafted)

    recycled: list[Path] = []
    report = rejects_mod.cleanup(env.db, recycle=lambda path: recycled.append(path))  # type: ignore[attr-defined]
    assert report.skipped == 1 and report.recycled == 1
    assert [path.name for path in recycled] == ["b.JPG"]
    assert any("a.JPG" in item for item in report.skipped_items)
    # 受保护的 a 仍可查询，b 的 asset 行已删
    remaining = {str(row["relative_path"]) for row in env.db.query("SELECT relative_path FROM asset")}  # type: ignore[attr-defined]
    assert remaining == {"a.JPG"}


def test_cleanup_refuses_paths_outside_library(env: TestClient, monkeypatch) -> None:
    asset_id = env.assets["a.JPG"]  # type: ignore[attr-defined]
    rejects_mod.mark(env.db, asset_id)  # type: ignore[attr-defined]
    original_items = rejects_mod.items
    rows = original_items(env.db)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        rejects_mod, "items",
        lambda db: [rejects_mod.RejectItem(
            item.asset_id, item.library_id, "..\\outside.JPG", item.size,
            item.shot_id, item.marked_at) for item in rows],
    )
    recycled: list[Path] = []
    report = rejects_mod.cleanup(env.db, recycle=lambda path: recycled.append(path))  # type: ignore[attr-defined]
    assert report.failed == 1 and not recycled
    assert "越出库根" in report.errors[0]


def test_cleanup_job_recycles_via_injected_function(env: TestClient, monkeypatch) -> None:
    # TestClient 的 lifespan 会启动 BackgroundRunner；全量套件变慢时它可能先领走
    # 这条任务，导致手工 claim_next() 返回 None（单独跑时又恰好手工先领）。
    # 本测试要自己控制 claim/run，所以先停掉后台线程。
    env.app.state.runner.stop()  # type: ignore[attr-defined]
    a = env.assets["a.JPG"]  # type: ignore[attr-defined]
    rejects_mod.mark(env.db, a)  # type: ignore[attr-defined]
    recycled: list[Path] = []
    monkeypatch.setattr(rejects_mod, "move_to_recycle_bin", lambda path: recycled.append(path))
    job = env.post("/api/rejects/cleanup", json={"confirmed": True}).json()["job"]
    from photoos.jobs import JobQueue, JobRunner

    queue = JobQueue(env.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(env.db, env.settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    assert [path.name for path in recycled] == ["a.JPG"]
    assert not rejects_mod.is_marked(env.db, a)  # type: ignore[attr-defined]
