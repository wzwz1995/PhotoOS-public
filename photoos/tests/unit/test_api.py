"""HTTP API 测试。

覆盖的关键承诺：
  * 浏览/筛选只查数据库，不访问原片（测试里把照片文件删掉后列表仍可用）。
  * 离线 Library 仍能返回缩略图与元数据（PRD 断点 #5）。
  * 缩略图路径不会被穿越攻击利用。
  * 任务可通过 API 创建/暂停/恢复/取消。
  * 破坏性操作没有 API 入口。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoos.api import create_app
from tests.unit.route_guard import destructive_hits
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library.store import LibraryStore, write_marker
from photoos.repos import AssetRepo


def _photo(path: Path, seed: int, size: tuple[int, int] = (800, 600)) -> Path:
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", size, (seed * 17 % 256, seed * 31 % 256, seed * 53 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5" if seed % 2 else "NIKON Z 50"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = f"2025:0{1 + seed % 9}:1{seed % 9} 10:{seed:02d}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    sub[0x829D] = IFDRational(28, 10)
    sub[0x920A] = IFDRational(500, 10)
    sub[0xA434] = "NIKKOR Z 24-70mm f/4 S" if seed % 2 else "NIKKOR Z 50mm f/1.8 S"
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def client(tmp_path: Path):
    """一个已完整索引的库 + 装配好的 TestClient。

    **关键隔离**：用 `use_config_path(None)` 禁用开发机上的 `data/config.json`。
    否则真实配置会覆盖这里的 `data_root`，导致测试静默作用在真实照片库上
    （这是本项目真实踩过的陷阱，已由 `test_config_isolation` 回归守护）。
    """
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        (root / "杭州市" / "2025-10-25_1000-1100").mkdir(parents=True)
        (root / "上海市" / "2025-10-26_1000-1100").mkdir(parents=True)
        for index in range(1, 6):
            _photo(root / "杭州市" / "2025-10-25_1000-1100" / f"DSC_{1000 + index}.JPG", index)
        for index in range(1, 4):
            _photo(root / "上海市" / "2025-10-26_1000-1100" / f"DSC_{2000 + index}.JPG", index + 5)

        write_marker(root, "uuid-api", "测试库")
        library = LibraryStore(db).create(
            name="测试库", root=root, library_uuid="uuid-api", marker_uuid="uuid-api"
        )
        queue = JobQueue(db)
        job = queue.create("full_index", library_id=library.id)
        queue.claim_next()
        report = JobRunner(db, settings).run(job)
        assert report.status == "COMPLETED", report.error

        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.library_id = library.id  # type: ignore[attr-defined]
            test_client.library_root = root  # type: ignore[attr-defined]
            test_client.settings = settings  # type: ignore[attr-defined]
            test_client.db = db  # type: ignore[attr-defined]
            yield test_client


def test_config_isolation(tmp_path: Path) -> None:
    """回归：显式传入的 data_root 不得被开发机的 config.json 覆盖。"""
    from photoos.config import active_config_path

    with use_config_path(None):
        assert Settings(data_root=tmp_path).data_root == tmp_path
    # 退出上下文后恢复默认路径
    assert active_config_path().name == "config.json"


# ===================================================================== 健康/概览
def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["schema_version"] == payload["schema_expected"]
    assert payload["version"]


def test_root_serves_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "PhotoOS" in response.text
    assert "照片库" in response.text
    # UI 不得包含任何删除原文件的入口
    lowered = response.text.lower()
    assert "delete original" not in lowered


def test_static_assets(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200
    js = client.get("/static/js/app.js")
    assert js.status_code == 200
    assert "/api/assets" in js.text


def test_stats(client: TestClient) -> None:
    payload = client.get("/api/stats").json()
    assert payload["totals"]["assets"] == 8
    assert payload["totals"]["bytes"] > 0
    assert payload["totals"]["hashed"] == 8
    assert payload["libraries"][0]["stats"]["assets"] == 8


def test_config_endpoint(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    assert payload["data_root"]
    assert payload["require_confirmation_for_destructive"] is True


# ===================================================================== Library
def test_libraries(client: TestClient) -> None:
    payload = client.get("/api/libraries").json()
    assert len(payload) == 1
    entry = payload[0]
    assert entry["uuid"] == "uuid-api"
    assert entry["has_marker"] is True
    assert entry["reachable"] is True
    assert entry["asset_count"] == 8
    assert entry["stats"]["hashed"] == 8


def test_probe_endpoint(client: TestClient, tmp_path: Path) -> None:
    new_dir = tmp_path / "另一个目录"
    new_dir.mkdir()
    payload = client.get("/api/libraries/probe", params={"path": str(new_dir)}).json()
    assert payload["exists"] is True
    assert payload["action"] == "create"
    assert payload["reason"]


def test_rebind_is_dry_run_by_default(client: TestClient, tmp_path: Path) -> None:
    """未显式 apply=true 时不得改动数据库（防止误操作）。"""
    library_id = client.library_id  # type: ignore[attr-defined]
    new_root = tmp_path / "新盘" / "library"
    shutil.copytree(client.library_root, new_root)  # type: ignore[attr-defined]

    response = client.post(f"/api/libraries/{library_id}/rebind", json={"path": str(new_root)})
    payload = response.json()
    assert payload["rebound"] is False
    assert payload["applied"] is False
    # 数据库仍指向旧路径
    assert LibraryStore(client.db).get(library_id).root_path_hint == str(client.library_root)  # type: ignore[attr-defined]


def test_rebind_apply(client: TestClient, tmp_path: Path) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    new_root = tmp_path / "新盘" / "library"
    shutil.copytree(client.library_root, new_root)  # type: ignore[attr-defined]

    payload = client.post(
        f"/api/libraries/{library_id}/rebind", json={"path": str(new_root), "apply": True}
    ).json()
    assert payload["rebound"] is True
    assert payload["found"] == 8
    assert payload["verified_by"] == "hash"
    assert LibraryStore(client.db).get(library_id).root_path_hint == str(new_root)  # type: ignore[attr-defined]


def test_rebind_missing_path_returns_error_status(client: TestClient, tmp_path: Path) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    payload = client.post(
        f"/api/libraries/{library_id}/rebind",
        json={"path": str(tmp_path / "根本不存在的盘"), "apply": True},
    ).json()
    assert payload["rebound"] is False
    assert payload["warnings"]


def test_rebind_requires_path(client: TestClient) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    response = client.post(f"/api/libraries/{library_id}/rebind", json={})
    assert response.status_code == 422


def test_library_verify_endpoint(client: TestClient) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    payload = client.get(f"/api/libraries/{library_id}/verify").json()
    assert payload["reachable"] is True
    assert payload["asset_count"] == 8


def test_unknown_library_returns_404(client: TestClient) -> None:
    response = client.get("/api/libraries/9999/verify")
    assert response.status_code == 404
    assert response.json()["code"] == "library_not_found"


# ===================================================================== 资产浏览
def test_assets_pagination(client: TestClient) -> None:
    first = client.get("/api/assets", params={"limit": 3}).json()
    assert first["total"] == 8
    assert len(first["items"]) == 3

    second = client.get("/api/assets", params={"limit": 3, "offset": 3}).json()
    assert len(second["items"]) == 3
    ids_first = {item["id"] for item in first["items"]}
    ids_second = {item["id"] for item in second["items"]}
    assert not (ids_first & ids_second), "分页不得重复返回同一行"


def test_photos_of_a_retired_shot_still_listed_exactly_once(client: TestClient) -> None:
    """★ 回归（Q-27）：照片库要显示**全量**照片 —— 包括仍挂在退役组上的那些，
    而且**不能重复**。

    背景：重算分组时"只增不删"（旧 shot 打 `retired_at` 而不是 DELETE，因为
    `selection.shot_id` 是 CASCADE，删了会连带删掉精选）。正常路径下资产会被搬到新组，
    但**遗留/搁浅**状态下仍可能有资产挂在退役组上 —— 那些照片不能凭空消失，
    也不能因为走了 shot 的 JOIN 而重复出现两次。

    故意破坏：给 asset 级查询加上 `AND sh.retired_at IS NULL`，本测试立刻变红。
    """
    db = client.db  # type: ignore[attr-defined]
    # 造一个"退役但资产还在名下"的组（正常路径下资产会被搬走，这里模拟**搁浅**状态）
    asset_id = int(db.query("SELECT id FROM asset ORDER BY id LIMIT 1")[0]["id"])
    db.execute(
        "INSERT INTO shot (id, captured_at, capture_date_local, created_at, updated_at,"
        " group_source, group_revision) VALUES (9001, '2025-10-25 10:00:00', '2025-10-25',"
        " '2026-10-17 00:00:00', '2026-10-17 00:00:00', 'auto', 1)"
    )
    db.execute("UPDATE asset SET shot_id = 9001 WHERE id = ?", (asset_id,))
    db.execute(
        "UPDATE shot SET retired_at = ?, retired_reason = 'regroup' WHERE id = 9001",
        ("2026-10-17 00:00:00",),
    )

    payload = client.get("/api/assets", params={"limit": 100}).json()
    ids = [item["id"] for item in payload["items"]]
    assert asset_id in ids, "退役组里的照片不该从照片库里消失"
    assert len(ids) == len(set(ids)), "同一张照片不得出现两次"
    assert payload["total"] == 8, "总数应当仍是 8（既不丢也不重复计数）"



def test_assets_filter_by_library_and_type(client: TestClient) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    payload = client.get("/api/assets", params={"library_id": library_id, "file_type": "jpeg"}).json()
    assert payload["total"] == 8
    assert all(item["file_type"] == "jpeg" for item in payload["items"])

    empty = client.get("/api/assets", params={"file_type": "raw"}).json()
    assert empty["total"] == 0


def test_assets_filter_by_camera_and_year(client: TestClient) -> None:
    # 机型筛选是**子串匹配**（用户会输入 "Z 5" 这种片段），
    # 因此 "Z 5" 同时命中 "NIKON Z 5" 与 "NIKON Z 50" —— 这是有意为之。
    z5 = client.get("/api/assets", params={"camera": "Z 5"}).json()
    assert z5["total"] == 8
    assert all("Z 5" in (item["camera_model"] or "") for item in z5["items"])

    # 精确片段可区分：只有 Z5 那批的型号以 "Z 5" 结尾
    exact = client.get("/api/assets", params={"q": "NIKON Z 5"}).json()
    assert exact["total"] == 8
    z50 = [item for item in exact["items"] if item["camera_model"] == "NIKON Z 50"]
    assert len(z50) == 4

    years = client.get("/api/assets", params={"year": 2025}).json()
    assert years["total"] == 8

    empty = client.get("/api/assets", params={"year": 1999}).json()
    assert empty["total"] == 0


def test_assets_search_matches_structured_fields(client: TestClient) -> None:
    payload = client.get("/api/assets", params={"q": "24-70"}).json()
    assert payload["total"] == 4
    assert all("24-70" in (item["lens_model"] or "") for item in payload["items"])


def test_assets_only_missing_metadata(client: TestClient) -> None:
    payload = client.get("/api/assets", params={"only_missing_metadata": "true"}).json()
    assert payload["total"] == 0, "完整索引后不应还有缺元数据的资产"


def test_assets_include_metadata_and_aspect(client: TestClient) -> None:
    payload = client.get("/api/assets", params={"limit": 1}).json()
    item = payload["items"][0]
    assert item["capture_at"]
    assert item["camera_model"]
    assert item["lens_model"]
    assert item["iso"]
    assert item["has_thumb"] is True
    assert item["aspect"] and item["aspect"] > 0


def test_assets_sorting(client: TestClient) -> None:
    by_size = client.get("/api/assets", params={"sort": "size", "limit": 8}).json()["items"]
    sizes = [item["size"] for item in by_size]
    assert sizes == sorted(sizes, reverse=True)

    by_name = client.get("/api/assets", params={"sort": "name", "limit": 8}).json()["items"]
    names = [item["relative_path"] for item in by_name]
    assert names == sorted(names)


def test_invalid_sort_falls_back_default(client: TestClient) -> None:
    """排序参数有白名单校验，非法值应被拒绝而不是拼进 SQL。"""
    response = client.get("/api/assets", params={"sort": "size; DROP TABLE asset"})
    assert response.status_code == 422


def test_asset_detail(client: TestClient) -> None:
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["id"] == asset_id
    assert detail["metadata"]["capture_at"]
    assert detail["metadata"]["source"] in {"pillow", "exifread", "libraw"}
    assert len(detail["previews"]) == 2
    assert detail["library"]["uuid"] == "uuid-api"
    assert detail["relative_path"]


def test_asset_detail_404(client: TestClient) -> None:
    assert client.get("/api/assets/999999").status_code == 404


def test_thumb_and_preview_served(client: TestClient) -> None:
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    for kind in ("thumb", "preview"):
        response = client.get(f"/api/assets/{asset_id}/{kind}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert len(response.content) > 0
        assert "max-age" in response.headers.get("cache-control", "")


def test_original_served_when_online(client: TestClient) -> None:
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    response = client.get(f"/api/assets/{asset_id}/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")


def test_original_returns_409_when_library_offline(client: TestClient) -> None:
    """库离线时原图返回 409（可预期的业务状态），而不是 500 或 404 混淆。"""
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    root = client.library_root  # type: ignore[attr-defined]
    offline_root = root.with_name("library-offline")
    shutil.move(str(root), str(offline_root))
    try:
        response = client.get(f"/api/assets/{asset_id}/original")
        assert response.status_code == 409
        assert "离线" in response.json()["detail"]
    finally:
        shutil.move(str(offline_root), str(root))


def test_browsing_works_without_photo_files(client: TestClient) -> None:
    """PRD 断点 #5：硬盘不可用时，索引/缩略图/EXIF 仍然可用（列表不依赖原片）。"""
    root = client.library_root  # type: ignore[attr-defined]
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    offline_root = root.with_name("library-away")
    shutil.move(str(root), str(offline_root))
    try:
        listing = client.get("/api/assets").json()
        assert listing["total"] == 8
        assert listing["items"][0]["capture_at"]
        assert client.get(f"/api/assets/{asset_id}/thumb").status_code == 200
        detail = client.get(f"/api/assets/{asset_id}").json()
        assert detail["metadata"]["camera_model"]
    finally:
        shutil.move(str(offline_root), str(root))


def test_thumb_404_when_not_generated(client: TestClient) -> None:
    """尚未生成预览时返回 404 且给出可操作提示。"""
    db: Database = client.db  # type: ignore[attr-defined]
    db.execute("DELETE FROM preview")
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    response = client.get(f"/api/assets/{asset_id}/thumb")
    assert response.status_code == 404
    assert "index" in response.json()["detail"]


def test_thumb_rejects_path_traversal(client: TestClient, tmp_path: Path) -> None:
    """即使 preview.path 被篡改成穿越路径，也必须拒绝。"""
    db: Database = client.db  # type: ignore[attr-defined]
    asset_id = client.get("/api/assets", params={"limit": 1}).json()["items"][0]["id"]
    db.execute(
        "UPDATE preview SET path = ? WHERE asset_id = ? AND kind = 'thumb'",
        ("../../../../Windows/System32/drivers/etc/hosts", asset_id),
    )
    response = client.get(f"/api/assets/{asset_id}/thumb")
    assert response.status_code == 404


# ===================================================================== facets
def test_facets(client: TestClient) -> None:
    payload = client.get("/api/facets").json()
    cameras = {row["value"]: row["count"] for row in payload["cameras"]}
    assert cameras.get("NIKON Z 5") == 4
    years = {row["value"]: row["count"] for row in payload["years"]}
    assert years.get("2025") == 8


# ===================================================================== 任务
def test_jobs_list(client: TestClient) -> None:
    payload = client.get("/api/jobs").json()
    assert "jobs" in payload
    assert any(job["type"] == "full_index" for job in payload["jobs"])


def test_create_job_and_actions(client: TestClient) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    created = client.post("/api/jobs", json={"type": "hash", "library_id": library_id}).json()
    assert created["status"] in {"PENDING", "PROCESSING"}
    job_id = created["id"]

    paused = client.post(f"/api/jobs/{job_id}/pause").json()
    assert paused["status"] == "PAUSED"
    resumed = client.post(f"/api/jobs/{job_id}/resume").json()
    assert resumed["status"] == "PENDING"
    cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert cancelled["status"] == "CANCELLED"


def test_job_detail_includes_items(client: TestClient) -> None:
    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["job"]["id"] == job_id
    assert isinstance(payload["items"], dict)
    assert "failures" in payload


def test_unknown_job_action_rejected(client: TestClient) -> None:
    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    response = client.post(f"/api/jobs/{job_id}/explode")
    assert response.status_code == 422


def test_unknown_job_type_rejected(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"type": "self_destruct"})
    assert response.status_code == 400
    assert response.json()["code"] == "job_error"


def test_job_404(client: TestClient) -> None:
    response = client.get("/api/jobs/999999")
    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


# ===================================================================== 维护
def test_integrity_endpoint(client: TestClient) -> None:
    payload = client.get("/api/integrity").json()
    assert payload["quick_check"] == "ok"
    assert payload["foreign_key_violations"] == 0
    assert payload["orphan_assets"] == 0
    assert payload["orphan_metadata"] == 0
    assert payload["orphan_previews"] == 0


def test_backup_endpoint(client: TestClient) -> None:
    payload = client.post("/api/maintenance/backup").json()
    assert Path(payload["path"]).exists()
    assert payload["bytes"] > 0

    listed = client.get("/api/maintenance/backups").json()
    assert any(entry["name"] == Path(payload["path"]).name for entry in listed)


def test_duplicates_endpoint_reports_only(client: TestClient) -> None:
    """重复报告必须是只读的：调用前后文件系统完全一致。"""
    library_id = client.library_id  # type: ignore[attr-defined]
    root = client.library_root  # type: ignore[attr-defined]
    before = {p.name: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    payload = client.post("/api/maintenance/duplicates", json={"library_id": library_id}).json()
    assert "groups" in payload and "note" in payload
    assert "不会自动隔离或删除" in payload["note"]

    after = {p.name: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_duplicates_detects_real_duplicates(client: TestClient) -> None:
    library_id = client.library_id  # type: ignore[attr-defined]
    root = client.library_root  # type: ignore[attr-defined]
    original = root / "杭州市" / "2025-10-25_1000-1100" / "DSC_1001.JPG"
    duplicate = root / "上海市" / "2025-10-26_1000-1100" / "DSC_1001__2.JPG"
    duplicate.write_bytes(original.read_bytes())

    db: Database = client.db  # type: ignore[attr-defined]
    settings: Settings = client.settings  # type: ignore[attr-defined]
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    JobRunner(db, settings).run(job)
    job = queue.create("hash", library_id=library_id)
    JobRunner(db, settings).run(job)

    payload = client.post("/api/maintenance/duplicates", json={"library_id": library_id}).json()
    assert payload["groups"] == 1
    assert payload["redundant_copies"] == 1
    assert payload["reclaimable_bytes"] > 0
    assert duplicate.exists(), "报告不得删除任何文件"


# ===================================================================== 安全
def test_import_plan_rejects_bad_source_with_readable_message(client: TestClient) -> None:
    """**回归**：源目录不可用时必须是可读的业务错误，而不是裸的 500。

    真实现场：`/api/import/plan` 的局部 `except` 子句里用了一个未导入的名字
    （`SafetyError`），于是 `except` 自身抛 NameError → 任何错误都变成
    `Internal Server Error`，界面上完全看不出原因。
    """
    response = client.post("/api/import/plan", json={"source": r"Z:\并不存在\DCIM"})
    assert response.status_code != 500, "错误必须被翻译成业务状态码"
    assert response.status_code == 400
    payload = response.json()
    assert "message" in payload and payload["message"]
    assert "不存在" in payload["message"] or "不可访问" in payload["message"]


def test_import_plan_explains_mtp_device_paths(client: TestClient) -> None:
    """相机 USB 直连的 MTP 路径要给出可操作指引（既有已知限制，但不能只报"不存在"）。"""
    response = client.post(
        "/api/import/plan", json={"source": r"此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5"}
    )
    assert response.status_code == 400
    message = response.json()["message"]
    assert "MTP" in message
    assert "读卡器" in message, "要告诉用户最快的替代做法"
    assert "不会被改动" in message, "要承诺不动相机与卡上的文件"


def test_import_plan_requires_source(client: TestClient) -> None:
    assert client.post("/api/import/plan", json={}).status_code == 422


def test_import_apply_requires_confirmation(client: TestClient, tmp_path: Path) -> None:
    """导入会写库，必须先确认；缺确认时是 428 而不是默默执行。"""
    source = tmp_path / "src"
    source.mkdir()
    response = client.post("/api/import/apply", json={"source": str(source)})
    assert response.status_code == 428
    assert "确认" in response.json()["detail"]


def test_import_apply_validates_source_before_creating_job(client: TestClient, tmp_path: Path) -> None:
    """源不可用时**不能**创建注定失败的任务 —— 用户应该在点击时就得到原因。"""
    before = len(client.get("/api/jobs").json()["jobs"])
    response = client.post(
        "/api/import/apply",
        json={"source": str(tmp_path / "并不存在"), "confirmed": True},
    )
    assert response.status_code == 400
    after = len(client.get("/api/jobs").json()["jobs"])
    assert after == before, "不该为坏源创建任务"


def test_import_apply_is_not_capped_by_preview_limit(client: TestClient, tmp_path: Path) -> None:
    """**核心回归**：预演的 `limit` 只是抽样上限，**绝不能截断真正的导入**。

    实测现场：界面上「预演上限 30」→ 点「确认执行」只导入了 30 个文件，
    用户以为会处理整张存储卡（5,254 个）。`limit` 的存在意义是"一个 HTTP 请求
    一次读多少 EXIF"，把它用在执行上就是把抽样当成了总量。
    """
    source = tmp_path / "src"
    source.mkdir()
    _photo(source / "a.JPG", 1)

    job = client.post(
        "/api/import/apply",
        json={"source": str(source), "confirmed": True, "limit": 30},
    ).json()

    row = client.db.query_one("SELECT scope_json FROM job WHERE id = ?", (job["id"],))  # type: ignore[attr-defined]
    assert row is not None
    scope = json.loads(row["scope_json"])
    assert scope.get("limit") is None, "执行不能被预演抽样上限截断"


def test_import_apply_honours_explicit_max_files(client: TestClient, tmp_path: Path) -> None:
    """想只导入一部分时，用显式的 `max_files` —— 语义清楚，不与预演抽样混淆。"""
    source = tmp_path / "src"
    source.mkdir()
    _photo(source / "a.JPG", 1)

    job = client.post(
        "/api/import/apply",
        json={"source": str(source), "confirmed": True, "max_files": 5},
    ).json()

    row = client.db.query_one("SELECT scope_json FROM job WHERE id = ?", (job["id"],))  # type: ignore[attr-defined]
    scope = json.loads(row["scope_json"])
    assert scope.get("limit") == 5


def test_import_plan_reports_source_total(client: TestClient, tmp_path: Path) -> None:
    """预演结果必须带上源里的文件总数（界面据此显示"源中共 N 个"）。

    ⚠️ 种子必须避开 fixture 里已经入库的那 8 张（种子 1..8）。用同样的种子会让生成的
    JPEG 与库里某张**逐字节相同**，从而被"库内已有同内容 → 跳过"正确地判成重复
    （2026-10-17 起分类导入也查全库同内容，见 20-reference Q-38），`total` 于是变 0。
    那不是回归，是测试构造撞了。
    """
    source = tmp_path / "src"
    source.mkdir()
    for index in range(4):
        _photo(source / f"p{index}.JPG", 900 + index)

    payload = client.post(
        "/api/import/plan", json={"source": str(source), "limit": 2}
    ).json()
    assert payload["source_total"] == 4
    assert payload["total"] == 2
    assert payload["preview_sampled"] is True


def test_no_destructive_endpoints(client: TestClient) -> None:
    """Phase 1 不得暴露删除/移动原文件的接口（PRD §11.1 / §14.7）。

    判据在 `route_guard.py`（按**词**比对，不是按子串）——
    `confirm-all` 里含 `rm` 这种假阳性会让守卫被"顺手放宽"（见模块说明）。
    """
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    for path in sorted(paths):
        hits = destructive_hits(path)
        assert not hits, f"发现了可疑的破坏性路由：{path}（{sorted(hits)}）"


def test_the_route_guard_still_catches_destructive_paths() -> None:
    """★ 反证：判据放宽之后，真正的破坏性路由仍然认得出来。

    永远为真的断言不是守卫。这里两个方向都钉住。
    """
    for path in ("/api/assets/{asset_id}/delete", "/api/library/purge",
                 "/api/people/{person_id}/remove", "/api/x/rm"):
        assert destructive_hits(path), f"漏掉了破坏性路由：{path}"
    for path in ("/api/people/{person_id}/confirm-all", "/api/faces/verdict",
                 "/api/collections", "/api/assets/query"):
        assert not destructive_hits(path), f"误伤了正常路由：{path}"


def test_openapi_documents_api(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "PhotoOS"
    assert "/api/assets" in schema["paths"]

def test_config_exposes_scene_thresholds(client: TestClient) -> None:
    """★ 回归（Q-11）：Scene 的五个阈值必须能从配置接口读到。

    用户没法从界面改 Scene 划分是此前的实际痛点；现在它们在 `data/config.json` 里，
    而 `/api/config` 要如实报出来（否则用户不知道有哪些可调项）。
    """
    payload = client.get("/api/config").json()
    for key in ("scene_gap_seconds", "scene_merge_similarity", "scene_dissimilar_run",
                "scene_max_shots", "scene_unrelated_similarity"):
        assert key in payload, f"/api/config 缺少 {key}"
    assert payload["scene_gap_seconds"] == 900.0
    assert payload["scene_merge_similarity"] == 0.85
    assert payload["scene_max_shots"] == 60