"""浏览轴与作用域筛选的测试（M5）。

本文件守护的是"用户能按物理路径浏览"这件事（对应反馈 #3）：
  * 默认轴是 `path`（城市 → 时段），时段标签渲染成人话；
  * 选中节点 → `scope_kind=path` + `scope_key` 精确筛选；
  * `selection` / `collection` 两个 Phase 2 轴**已在本阶段实现**（Phase 1 时是 501）；
    `tag` 按决策 #6 本期不做，**仍明确返回 501**，而不是静默忽略 ——
    静默忽略会让用户以为筛选生效了，看到的却是全库；
  * 列表项的 `selection_status` 已从 Phase 1 的常量占位换成真实的 `LEFT JOIN selection`。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoos.api.app import create_app
from photoos.api.queries import prettify_session_folder
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library.store import LibraryStore, write_marker

HZ = "杭州市"
HZ_SESSION = "2025-10-25_1000-1100"
SH = "上海市"
SH_SESSION = "2025-10-26_1400-1530"


def _photo(path: Path, when: str, seed: int = 1) -> Path:
    image = Image.new("RGB", (160, 120), (seed * 17 % 256, seed * 31 % 256, seed * 43 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = when
    sub[0x9011] = "+08:00"
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def client(tmp_path: Path):
    """两个城市、三个时段的已索引库（含一个跨天时段名）。"""
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        plan = [
            (HZ, HZ_SESSION, 3, "2025:10:25 10:0{0}:00"),
            (HZ, "2025-10-24_2349_至_10-25_1807", 2, "2025:10:24 23:5{0}:00"),
            (SH, SH_SESSION, 4, "2025:10:26 14:0{0}:00"),
        ]
        for city, session, count, template in plan:
            folder = root / city / session
            folder.mkdir(parents=True)
            for index in range(1, count + 1):
                _photo(folder / f"DSC_{index:04d}.JPG", template.format(index), seed=index)

        write_marker(root, "uuid-browse", "测试库")
        library = LibraryStore(db).create(
            name="测试库", root=root, library_uuid="uuid-browse", marker_uuid="uuid-browse"
        )
        queue = JobQueue(db)
        job = queue.create("full_index", library_id=library.id)
        queue.claim_next()
        assert JobRunner(db, settings).run(job).status == "COMPLETED"

        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.library_id = library.id  # type: ignore[attr-defined]
            # 暴露 db 句柄：Phase 2 的精选作用域测试需要在同一库上造 Shot/selection 行
            test_client.db = db  # type: ignore[attr-defined]
            yield test_client


# ============================================================ 路径轴
def test_path_axis_lists_cities(client: TestClient) -> None:
    payload = client.get("/api/browse/tree", params={"axis": "path"}).json()
    assert payload["axis"] == "path"
    nodes = {node["key"]: node for node in payload["nodes"]}
    assert set(nodes) == {HZ, SH}
    assert nodes[HZ]["count"] == 5
    assert nodes[SH]["count"] == 4
    assert payload["total"] == 9
    assert all(node["has_children"] for node in payload["nodes"]), "城市层应可展开"


def test_path_axis_drills_into_sessions_with_readable_labels(client: TestClient) -> None:
    payload = client.get(
        "/api/browse/tree", params={"axis": "path", "parent": HZ}
    ).json()
    nodes = {node["key"]: node for node in payload["nodes"]}
    assert set(nodes) == {f"{HZ}\\{HZ_SESSION}", f"{HZ}\\2025-10-24_2349_至_10-25_1807"}
    assert nodes[f"{HZ}\\{HZ_SESSION}"]["label"] == "2025-10-25 10:00–11:00"
    assert nodes[f"{HZ}\\2025-10-24_2349_至_10-25_1807"]["label"] == (
        "2025-10-24 23:49 – 10-25 18:07"
    )
    assert not any(node["has_children"] for node in payload["nodes"]), (
        "时段层下面是文件而不是目录，不应显示为可展开"
    )


def test_session_folder_labels_fall_back_to_raw_name() -> None:
    """渲染失败时原样返回 —— 目录名是事实，不能因为渲染不了就藏起来。"""
    assert prettify_session_folder("2025-10-25_1000-1100") == "2025-10-25 10:00–11:00"
    assert prettify_session_folder("2025-10-24_2349_至_10-25_1807") == (
        "2025-10-24 23:49 – 10-25 18:07"
    )
    assert prettify_session_folder("2025-12-31_2200_至_2026-01-01_0300") == (
        "2025-12-31 22:00 – 2026-01-01 03:00"
    )
    assert prettify_session_folder("未知地点") == "未知地点"


# ============================================================ 时间轴
def test_time_axis_year_month_day(client: TestClient) -> None:
    years = client.get("/api/browse/tree", params={"axis": "time"}).json()
    assert [node["key"] for node in years["nodes"]] == ["2025"]
    assert years["nodes"][0]["label"] == "2025 年"
    assert years["nodes"][0]["count"] == 9

    months = client.get(
        "/api/browse/tree", params={"axis": "time", "parent": "2025"}
    ).json()
    assert [node["key"] for node in months["nodes"]] == ["2025-10"]
    assert months["nodes"][0]["count"] == 9

    days = client.get(
        "/api/browse/tree", params={"axis": "time", "parent": "2025-10"}
    ).json()
    assert [node["key"] for node in days["nodes"]] == ["2025-10-26", "2025-10-25", "2025-10-24"]
    assert not any(node["has_children"] for node in days["nodes"]), "日是最深一层"


# ============================================================ 作用域筛选
def test_scope_path_filters_exactly_one_session(client: TestClient) -> None:
    scope_key = f"{HZ}\\{HZ_SESSION}"
    payload = client.get(
        "/api/assets", params={"scope_kind": "path", "scope_key": scope_key, "limit": 50}
    ).json()
    assert payload["total"] == 3
    assert all(item["relative_path"].startswith(scope_key + "\\") for item in payload["items"])


def test_scope_path_does_not_leak_across_cities(client: TestClient) -> None:
    """前缀匹配必须带分隔符：`杭州市` 不能把 `杭州市西湖区` 之类的兄弟目录也带进来。"""
    payload = client.get(
        "/api/assets", params={"scope_kind": "path", "scope_key": SH, "limit": 50}
    ).json()
    assert payload["total"] == 4
    assert all(item["relative_path"].startswith(SH + "\\") for item in payload["items"])


def test_scope_time_filters_by_date_prefix(client: TestClient) -> None:
    payload = client.get(
        "/api/assets", params={"scope_kind": "time", "scope_key": "2025-10-26", "limit": 50}
    ).json()
    assert payload["total"] == 4
    assert all(item["capture_date_local"] == "2025-10-26" for item in payload["items"])


def test_scope_combines_with_other_filters(client: TestClient) -> None:
    payload = client.get(
        "/api/assets",
        params={
            "scope_kind": "path",
            "scope_key": f"{HZ}\\{HZ_SESSION}",
            "file_type": "raw",
            "limit": 50,
        },
    ).json()
    assert payload["total"] == 0, "该时段只有 JPEG，叠加类型筛选后应为 0"


def test_path_prefix_with_wildcards_is_escaped(client: TestClient) -> None:
    """目录名里若含 `%` 或 `_`，不能当成 LIKE 通配符（本项目踩过转义坑）。"""
    for tricky in ("%", "_"):
        payload = client.get(
            "/api/assets", params={"scope_kind": "path", "scope_key": tricky, "limit": 10}
        ).json()
        assert payload["total"] == 0, f"{tricky!r} 被当成通配符了"


# ============================================================ Phase 2：轴已启用
def test_tag_axis_still_returns_501(client: TestClient) -> None:
    """决策 #6：`tag` 本期**不做**（PRD §9.2 通篇无"标签"要求，Collections 已覆盖其用途）。

    保持 501 而不是静默返回全库 —— 形状已冻结，Phase 3 加实现分支即可，前端零改动。
    """
    response = client.get("/api/browse/tree", params={"axis": "tag"})
    assert response.status_code == 501
    assert "Phase 2" in response.json()["detail"]


def test_tag_scope_kind_still_returns_501(client: TestClient) -> None:
    response = client.get("/api/assets", params={"scope_kind": "tag", "scope_key": "x"})
    assert response.status_code == 501


@pytest.mark.parametrize("axis", ["selection", "collection"])
def test_phase2_axes_are_now_implemented(client: TestClient, axis: str) -> None:
    """Phase 2 已启用这两个轴 —— 不再 501，而是返回结构正确的节点。"""
    response = client.get("/api/browse/tree", params={"axis": axis})
    assert response.status_code == 200, response.text
    assert response.json()["axis"] == axis


def test_selection_axis_lists_all_four_statuses(client: TestClient) -> None:
    """四态**固定全部列出**（哪怕计数为 0）—— 缺一个会让"还没有最终精选"看起来像功能坏了。"""
    payload = client.get("/api/browse/tree", params={"axis": "selection"}).json()
    keys = [node["key"] for node in payload["nodes"]]
    assert keys == ["UNREVIEWED", "CANDIDATE", "SELECTED", "FINAL"]
    labels = {node["key"]: node["label"] for node in payload["nodes"]}
    assert labels["FINAL"] == "最终精选"


def test_selection_scope_filters_unreviewed(client: TestClient) -> None:
    """未审阅 = `selection` 表里没有行（Phase 1 建表时的语义）。

    该夹具里 9 张照片全部没有 `shot_id` → `LEFT JOIN selection` 得到 NULL →
    全部算未审阅。这同时验证了**用 INNER JOIN 会全丢**这一点（那正是不能用 INNER 的原因）。
    """
    payload = client.get(
        "/api/assets", params={"scope_kind": "selection", "scope_key": "UNREVIEWED"}
    ).json()
    assert payload["total"] == 9
    assert all(item["selection_status"] == "UNREVIEWED" for item in payload["items"])


def test_selection_scope_excludes_reviewed_shots(client: TestClient) -> None:
    """把某个 Shot 标为 FINAL 后，它必须从"未审阅"里消失、并出现在 FINAL 里。

    这是"作用域真的生效"的关键断言 —— 否则筛选看起来对、实际返回全库。
    """
    db = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute(
            "INSERT INTO scene (id, name, created_at, updated_at) VALUES (1,'S','t','t')"
        )
        conn.execute(
            "INSERT INTO shot (id, scene_id, created_at, updated_at) VALUES (900, 1, 't', 't')"
        )
        conn.execute("UPDATE asset SET shot_id = 900 WHERE id = (SELECT MIN(id) FROM asset)")

    client.post("/api/shots/900/selection", json={"status": "FINAL"})

    unreviewed = client.get(
        "/api/assets", params={"scope_kind": "selection", "scope_key": "UNREVIEWED"}
    ).json()
    finals = client.get(
        "/api/assets", params={"scope_kind": "selection", "scope_key": "FINAL"}
    ).json()
    assert unreviewed["total"] == 8
    assert finals["total"] == 1
    assert finals["items"][0]["selection_status"] == "FINAL"


def test_collection_axis_is_empty_without_collections(client: TestClient) -> None:
    payload = client.get("/api/browse/tree", params={"axis": "collection"}).json()
    assert payload["nodes"] == []


def test_unknown_axis_is_rejected(client: TestClient) -> None:
    assert client.get("/api/browse/tree", params={"axis": "nonsense"}).status_code == 422


def test_bad_selection_scope_key_is_rejected(client: TestClient) -> None:
    """非法状态值 → 明确报错，而不是静默返回全库。"""
    response = client.get(
        "/api/assets", params={"scope_kind": "selection", "scope_key": "BOGUS"}
    )
    assert response.status_code >= 400


def test_asset_items_carry_real_selection_status(client: TestClient) -> None:
    """Phase 1 这里断言的是常量占位；Phase 2 换成 `LEFT JOIN selection` 后

    行为不变（没有精选行时仍是 UNREVIEWED），但字段现在来自真实数据。
    """
    payload = client.get("/api/assets", params={"limit": 3}).json()
    assert payload["items"]
    assert all(item["selection_status"] == "UNREVIEWED" for item in payload["items"])


def test_browse_tree_is_library_scoped(client: TestClient) -> None:
    payload = client.get(
        "/api/browse/tree", params={"axis": "path", "library_id": 999}
    ).json()
    assert payload["nodes"] == [] and payload["total"] == 0
