"""派生缓存（缩略图/预览）的路径校验：必须**容忍目录联接**，同时**挡住穿越**。

## 这个文件补的是一个真实的 404

`_serve_preview()` 里原本写的是：

```python
candidate = (data_root / row["path"]).resolve()   # ← 会解析联接
root      = data_root.resolve()
if root not in candidate.parents:                 # ← 于是这条永远 False
    return None                                   # → 404
```

一侧 `resolve()`、另一侧没有，导致**任何把 data_root（或其子目录）做成
目录联接/符号链接的部署**都会被误判成"路径穿越"，`/api/assets/{id}/thumb`
一律 404。实测现场：把真实 `thumbnails/` 用 `mklink /J` 联接进隔离数据根做
真实规模界面演练时，`candidate.is_file()` 为 `True` 而校验为 `False`，
界面上的缩略图**全部**是坏图。

**为什么原来的测试没抓到**：隔离测试里的 `thumbnails/` 都是**真实目录**，
不是联接，所以 `resolve()` 前后一致。

本文件同时守住两件事：
  ① 联接场景**必须能取到**（否则隔离演练和"把缓存放别的盘"都不可用）
  ② 穿越场景**必须挡住**（这是安全边界，不能被修 ① 的时候顺手删掉）
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _make_library(root: Path, *, thumb_rel: str, thumb_file: Path) -> Settings:
    """建一个最小的库：1 asset + 1 preview 行，`preview.path` 指向 `thumb_rel`。"""
    settings = Settings(data_root=root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','库','2026-01-01T00:00:00+08:00','ONLINE')"
        )
        conn.execute(
            "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (1,1,'城\\\\2026-01-01_1000-1100\\\\DSC_0001.JPG','jpeg','OK',"
            " '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
            " generator_version, created_at)"
            " VALUES (1,'thumb',?,1,1,?, 'p1.0','2026-01-01T00:00:00+08:00')",
            (thumb_rel, len(PNG_1PX)),
        )
    return settings


def _junction(link: Path, target: Path) -> bool:
    """建 NTFS 目录联接（不需要管理员权限）。失败返回 False。

    ⚠️ 必须**确认联接真的生效**：`mklink /J` 在跨盘时会静默创建失败或
    创建成别的东西，所以这里回读 `link.resolve()` 与目标比对，
    而不是只看 `link.exists()`（后者在失败时也可能为真）。
    """
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return False
    try:
        return link.resolve() == target.resolve()
    except OSError:
        return False


def test_serves_thumbnail_through_a_junction(tmp_path: Path) -> None:
    """★ 回归：data_root 的子目录是**目录联接**时，缩略图必须能取到。

    真实部署里这就是"把缓存目录挪到另一个盘、然后用联接接回来"的场景。
    修 ① 之前，这里返回 404。
    """
    external = tmp_path / "external_thumbs"
    external.mkdir()
    (external / "t.png").write_bytes(PNG_1PX)

    data_root = tmp_path / "data"
    data_root.mkdir()
    link = data_root / "thumbnails"
    if not _junction(link, external):
        pytest.skip("本机无法创建目录联接（需要 NTFS）")

    settings = _make_library(data_root, thumb_rel="thumbnails/t.png",
                             thumb_file=external / "t.png")
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/api/assets/1/thumb")
    assert response.status_code == 200, response.text
    assert response.content == PNG_1PX


def test_serves_thumbnail_from_a_plain_directory(tmp_path: Path) -> None:
    """对照组：普通目录（非联接）本来就该能取到 —— 别把 ① 修成"只有联接能用"。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "thumbnails").mkdir()
    (data_root / "thumbnails" / "t.png").write_bytes(PNG_1PX)

    settings = _make_library(data_root, thumb_rel="thumbnails/t.png",
                             thumb_file=data_root / "thumbnails" / "t.png")
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/api/assets/1/thumb")
    assert response.status_code == 200, response.text
    assert response.content == PNG_1PX


def test_still_blocks_path_traversal(tmp_path: Path) -> None:
    """★ 安全边界不能被 ① 顺手删掉：`preview.path` 跑到 data_root 之外必须拒绝。

    这里把一个真实存在的文件放在 data_root **外面**，再用 `..` 指过去 ——
    校验必须仍然拦下（否则客户端能借这个接口读任意文件）。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG_1PX)

    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "thumbnails").mkdir()

    settings = _make_library(data_root, thumb_rel="../outside/secret.png",
                             thumb_file=outside / "secret.png")
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/api/assets/1/thumb")
    assert response.status_code == 404, "穿越没被挡住 —— 安全边界破了"
    assert PNG_1PX not in response.content


def test_missing_file_is_404(tmp_path: Path) -> None:
    """记录在、但文件不在 → 404（且不能 500）。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "thumbnails").mkdir()
    settings = _make_library(data_root, thumb_rel="thumbnails/gone.png",
                             thumb_file=data_root / "thumbnails" / "gone.png")
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/assets/1/thumb").status_code == 404


def test_absolute_path_in_db_is_rejected(tmp_path: Path) -> None:
    """`preview.path` 里塞绝对路径（手工改库/旧版本写出）不能读到任意文件。

    这是"库内不得出现身份路径"（铁律 #2）在派生缓存这一侧的边界。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG_1PX)

    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "thumbnails").mkdir()

    settings = _make_library(data_root, thumb_rel=str(outside / "secret.png"),
                             thumb_file=outside / "secret.png")
    with use_config_path(None):
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/api/assets/1/thumb")
    assert response.status_code == 404
