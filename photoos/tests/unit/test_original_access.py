"""M1 回归测试：原片访问（下载 / 内联查看）。

实测故障现场：详情抽屉里的「打开原图」把 `/api/assets/<id>/original` 用
`target="_blank"` 打开，而服务端 `FileResponse(path)` **没有传 `filename`**，
于是没有 `Content-Disposition`；`.NEF` 在 `mimetypes` 里无映射又让 Starlette
回退成 `application/octet-stream`。浏览器只能下载，并从 **URL 末段**取名 ——
文件名就是 `original`，**没有扩展名**（`.MOV` 因为 MIME 已知被补成 `original.mov`）。

因此本文件盯住两件事：
  1. 下载端点必须给出**真实文件名**（含中文名走 RFC 5987 的 `filename*`）。
  2. 查看端点对浏览器渲染不了的格式（RAW）必须回退到派生预览，并如实标注来源。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoos.api.app import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library.store import LibraryStore, write_marker

REAL_NEF_DIR = Path(r"C:\PhotoOS\_samples")
REAL_NEFS = sorted(REAL_NEF_DIR.glob("*.NEF")) if REAL_NEF_DIR.is_dir() else []

JPEG_REL = "杭州市\\2025-10-25_1000-1100\\DSC_1001.JPG"
CHINESE_REL = "杭州市\\2025-10-25_1000-1100\\海边日落 01.JPG"
NEF_REL = "杭州市\\2025-10-25_1000-1100\\DSC_2001.NEF"


def _photo(path: Path, seed: int = 1, size: tuple[int, int] = (320, 240)) -> Path:
    image = Image.new("RGB", size, (seed * 11 % 256, seed * 23 % 256, seed * 37 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2025:10:25 10:05:00"
    sub[0x9011] = "+08:00"
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def client(tmp_path: Path):
    """一个已完整索引的库：普通 JPEG + **中文名** JPEG + 真实 NEF（若样本存在）。"""
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        session = root / "杭州市" / "2025-10-25_1000-1100"
        session.mkdir(parents=True)
        _photo(session / "DSC_1001.JPG", seed=1)
        _photo(session / "海边日落 01.JPG", seed=2)
        if REAL_NEFS:
            shutil.copy2(REAL_NEFS[0], session / "DSC_2001.NEF")

        write_marker(root, "uuid-orig", "测试库")
        library = LibraryStore(db).create(
            name="测试库", root=root, library_uuid="uuid-orig", marker_uuid="uuid-orig"
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


def _asset_id(client: TestClient, relative: str) -> int:
    row = client.db.query_one(  # type: ignore[attr-defined]
        "SELECT id FROM asset WHERE library_id = ? AND relative_path = ?",
        (client.library_id, relative),  # type: ignore[attr-defined]
    )
    assert row is not None, f"测试库中不存在 {relative}"
    return int(row["id"])


# ============================================================ 下载端点：真实文件名
def test_original_download_declares_real_filename(client: TestClient) -> None:
    """核心回归：没有 Content-Disposition 时浏览器会把文件存成无扩展名的 `original`。"""
    asset_id = _asset_id(client, JPEG_REL)
    response = client.get(f"/api/assets/{asset_id}/original")

    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("attachment"), disposition
    assert "DSC_1001.JPG" in disposition, disposition
    assert response.content == (client.library_root / JPEG_REL).read_bytes()  # type: ignore[attr-defined]


def test_original_download_encodes_chinese_filename(client: TestClient) -> None:
    """中文文件名必须走 RFC 5987（`filename*=UTF-8''…`），否则会乱码或被截断。"""
    asset_id = _asset_id(client, CHINESE_REL)
    response = client.get(f"/api/assets/{asset_id}/original")

    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    # Starlette 输出小写 `utf-8`；RFC 5987 中 charset 大小写不敏感
    assert "filename*=utf-8''" in disposition.lower(), disposition
    assert "%E6%B5%B7%E8%BE%B9%E6%97%A5%E8%90%BD" in disposition, "应为“海边日落”的百分号编码"


@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_raw_download_keeps_extension_and_mime(client: TestClient) -> None:
    """RAW 的 MIME 在 `mimetypes` 里查不到，必须由 asset 记录提供，否则退化成 octet-stream。"""
    asset_id = _asset_id(client, NEF_REL)
    response = client.get(f"/api/assets/{asset_id}/original")

    assert response.status_code == 200
    assert "DSC_2001.NEF" in response.headers.get("content-disposition", "")
    assert response.headers["content-type"] == "image/x-nikon-nef"


# ============================================================ 查看端点：能渲染给原片
def test_view_serves_original_bytes_for_jpeg(client: TestClient) -> None:
    """JPEG 能内联渲染 → 必须给真正的原片数据，而不是退回预览。"""
    asset_id = _asset_id(client, JPEG_REL)
    response = client.get(f"/api/assets/{asset_id}/view")

    assert response.status_code == 200
    assert response.headers["x-photoos-source"] == "original"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == (client.library_root / JPEG_REL).read_bytes()  # type: ignore[attr-defined]


@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_view_falls_back_to_preview_for_raw(client: TestClient) -> None:
    """RAW 浏览器渲染不了 → 回退到派生预览，并如实标注来源。

    否则用户点「查看」会得到一个下载下来的二进制文件，而且误以为看到的是原图。
    """
    asset_id = _asset_id(client, NEF_REL)
    response = client.get(f"/api/assets/{asset_id}/view")

    assert response.status_code == 200
    assert response.headers["x-photoos-source"] == "preview"
    assert response.headers["content-type"] == "image/webp"
    assert len(response.content) > 0

    # 预览确实是一张可解码的图（而不是碰巧返回了别的字节）
    import io

    with Image.open(io.BytesIO(response.content)) as image:
        assert image.width > 0 and image.height > 0


@pytest.mark.skipif(not REAL_NEFS, reason="没有真实 NEF 样本")
def test_view_preview_is_larger_than_thumb_for_raw(client: TestClient) -> None:
    """回退的应当是 `preview`（大图）而不是 `thumb`，否则「查看大图」反而更小。"""
    asset_id = _asset_id(client, NEF_REL)
    preview_bytes = len(client.get(f"/api/assets/{asset_id}/view").content)
    thumb_bytes = len(client.get(f"/api/assets/{asset_id}/thumb").content)
    assert preview_bytes > thumb_bytes


def test_view_reports_missing_preview_clearly(client: TestClient) -> None:
    """RAW 且还没有预览时，错误信息必须说清楚原因，而不是含糊的 404。"""
    asset_id = _asset_id(client, JPEG_REL)
    db = client.db  # type: ignore[attr-defined]
    db.execute("DELETE FROM preview WHERE asset_id = ?", (asset_id,))
    # 把类型改成 raw，模拟"浏览器渲染不了且缺预览"
    db.execute("UPDATE asset SET file_type = 'raw' WHERE id = ?", (asset_id,))

    response = client.get(f"/api/assets/{asset_id}/view")
    assert response.status_code == 404
    assert "无法直接渲染" in response.json()["detail"]


# ============================================================ 错误路径
def test_original_and_view_404_for_unknown_asset(client: TestClient) -> None:
    for suffix in ("original", "view"):
        response = client.get(f"/api/assets/999999/{suffix}")
        assert response.status_code == 404


def test_view_returns_409_when_library_offline(client: TestClient) -> None:
    """离线时两个端点都必须是 409（可预期业务状态），而不是 500。"""
    asset_id = _asset_id(client, JPEG_REL)
    root = client.library_root  # type: ignore[attr-defined]
    offline_root = root.with_name("library-offline")
    shutil.move(str(root), str(offline_root))
    try:
        for suffix in ("original", "view"):
            response = client.get(f"/api/assets/{asset_id}/{suffix}")
            assert response.status_code == 409
            assert "离线" in response.json()["detail"]
    finally:
        shutil.move(str(offline_root), str(root))
