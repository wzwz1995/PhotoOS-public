"""回归：**有 `shot_id` 的资产**，详情接口必须能正常返回。

## 这是一个"潜伏缺陷被新功能激活"的实例

`asset_detail()` 里有一段只在 `asset.shot_id` 非空时才跑的查询，它写的是：

```python
"SELECT s.id, s.name, … FROM shot AS s LEFT JOIN scene AS sc …"
```

而 **`shot` 表没有 `name` 列**。Phase 1 **从不写 `shot_id`**（Shot 是 Phase 2 才
落库的派生结构），所以这段代码**永远走不到**，两条测试线（Phase 1 与 Phase 2 的
1000+ 用例）都全绿。

Phase 2 把 `shot_id` 填上之后，它立刻变成
`OperationalError: no such column: s.name` —— **资产详情接口 500**。

实测现场（真实数据 + 真实浏览器）：在浏览视图点任意一张照片，
界面只弹一句「读取详情失败：服务器内部错误」，**抽屉打不开、看不到元数据、
也没有下载原片入口**。这是用户一眼就能看到的功能损坏。

## 教训

**改了数据形状，就要回头测那些"以前总是走 else 分支"的代码。**
本文件守住的正是那个交叉点：**Phase 2 的库 + 有 shot_id 的资产**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """1 Library / 1 Session / 1 Scene / 1 Shot / 1 Asset，且 **asset.shot_id 已填**。"""
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        stamp = "2026-01-01T00:00:00+08:00"
        with db.write() as conn:
            conn.execute(
                "INSERT INTO library (uuid, name, created_at, status, root_path_hint)"
                " VALUES ('lib','库',?,'ONLINE',?)", (stamp, str(data_root / "lib"))
            )
            conn.execute(
                "INSERT INTO session (id, name, city_hint, period_hint, captured_from,"
                " captured_to, created_at, updated_at)"
                " VALUES (1,'城\\2026-01-01_1000-1100','城','2026-01-01_1000-1100',"
                " '2026-01-01 10:00:00','2026-01-01 11:00:00',?,?)", (stamp, stamp)
            )
            conn.execute(
                "INSERT INTO scene (id, session_id, name, captured_from, captured_to,"
                " created_at, updated_at)"
                " VALUES (1,1,'场景','2026-01-01 10:00:00','2026-01-01 10:30:00',?,?)",
                (stamp, stamp),
            )
            conn.execute(
                "INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                " capture_date_local, camera_key, group_source, group_confidence,"
                " created_at, updated_at)"
                " VALUES (1,1,1,'2026-01-01 10:00:00','2026-01-01','NIKON Z 5','auto',0.95,?,?)",
                (stamp, stamp),
            )
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                " first_seen_at, shot_id)"
                " VALUES (1,1,'城\\2026-01-01_1000-1100\\DSC_0001.JPG','jpeg','OK',?,1)",
                (stamp,),
            )
            conn.execute(
                "INSERT INTO metadata (asset_id, capture_at, camera_make, camera_model,"
                " tz_offset, tz_source, iso, aperture)"
                " VALUES (1,'2026-01-01 10:00:00','NIKON CORPORATION','NIKON Z 5',"
                " '+08:00','exif-offset',400,2.8)"
            )
        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.db = db  # type: ignore[attr-defined]
            yield test_client


def test_asset_detail_works_when_asset_has_a_shot(client: TestClient) -> None:
    """★ 回归：有 `shot_id` 的资产，详情必须 **200**（不是 500）。

    修之前这里返回 500，消息是 `no such column: s.name`。
    """
    response = client.get("/api/assets/1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 1
    # 元数据仍然齐全（详情页靠它渲染那张表）
    assert body["metadata"]["camera_model"] == "NIKON Z 5"
    assert body["metadata"]["iso"] == 400


def test_asset_detail_reports_shot_and_scene(client: TestClient) -> None:
    """`shot` / `scene` 信息要**真的带上**，而且字段名不能是想当然的。

    `shot` 表**没有 `name` 列** —— 这正是原来那条 SQL 的错处。
    这里断言返回的是**真实存在的列**，并要求 scene 的名称来自 `scene` 表。
    """
    body = client.get("/api/assets/1").json()
    assert body["shot_id"] == 1
    shot = body["shot"]
    assert shot is not None, "有 shot_id 却没带上 shot 信息"
    assert shot["id"] == 1
    # 真实列：这些必须都在（否则模板/前端会拿到 undefined）
    for column in ("scene_id", "captured_at", "primary_asset_id", "group_source",
                   "group_confidence", "group_revision"):
        assert column in shot, f"shot 缺列 {column}：{sorted(shot)}"
    assert shot["group_source"] == "auto"
    assert shot["group_confidence"] == pytest.approx(0.95)
    # scene 的名称来自 scene 表（不是 shot 表的 name —— 那张表没有这个列）
    assert shot["scene_name"] == "场景"


def test_asset_detail_without_shot_still_works(client: TestClient) -> None:
    """对照组：没有 `shot_id` 的资产走 else 分支（Phase 1 的常态），也必须正常。

    这条保证修复没有把"Phase 1 的路径"弄坏。
    """
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE asset SET shot_id = NULL WHERE id = 1")
    body = client.get("/api/assets/1").json()
    assert body["shot_id"] is None
    assert body["shot"] is None


def test_asset_detail_survives_a_retired_shot(client: TestClient) -> None:
    """退役的 Shot 也要能查（详情页不该因为重算过就打不开）。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE shot SET retired_at = '2026-01-02T00:00:00+08:00'"
                     " WHERE id = 1")
    response = client.get("/api/assets/1")
    assert response.status_code == 200, response.text
    assert response.json()["shot"]["id"] == 1


def test_asset_detail_with_null_scene(client: TestClient) -> None:
    """Shot 没有 Scene（`replace_scenes` 会把旧链接清成 NULL）时也不能炸。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE shot SET scene_id = NULL WHERE id = 1")
    body = client.get("/api/assets/1").json()
    assert body["shot"] is not None
    assert body["shot"]["scene_id"] is None
    assert body["shot"]["scene_name"] is None
