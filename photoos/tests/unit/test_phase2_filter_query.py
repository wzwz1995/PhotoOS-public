"""Phase 2 · 结构化筛选（`POST /api/assets/query`）测试。

这个端点是**用户明确要求预留的"SQL 筛选接口"**，也是将来"自然语言筛选"的落点。
因此测试的重点不是"能不能筛"，而是**安全边界有没有真的写死**：

  * 未知字段 → 400（**不静默忽略** —— 静默忽略会让用户以为筛选生效了）
  * 未知操作符 → 400
  * `like` 不接受用户写的通配符（值当字面量）
  * 值全部参数绑定（构造一个"看起来像 SQL 注入"的值，必须被当普通字符串）
  * 绝不扫描原片（只查结构化表）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app, filter_query as fq
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.repos import CollectionRepo, SelectionRepo
from tests.unit.route_guard import destructive_hits


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
    """两座城市、四个时段、6 张照片；机型/焦段/ISO/GPS 各有差异；2 张有精选状态。"""
    rows = [
        # (id, city, period, name, camera, focal, iso, gps, captured, selected)
        (1, "杭州市", "2025-10-25_1000-1100", "DSC_1001.JPG", "NIKON Z 5", 70.0, 400, 30.1, "2025-10-25 10:00:00", None),
        (2, "杭州市", "2025-10-25_1000-1100", "DSC_1002.JPG", "NIKON Z 5", 200.0, 800, 30.1, "2025-10-25 10:05:00", sel.FINAL),
        (3, "杭州市", "2025-10-26_0900-1000", "DSC_1003.JPG", "NIKON Z 5", 300.0, 1600, None, "2025-10-26 09:00:00", sel.SELECTED),
        (4, "上海市", "2026-02-19_0947-1749", "DSC_2001.JPG", "Apple iPhone 15 Pro", 24.0, 100, 31.2, "2026-02-19 09:47:00", None),
        (5, "希腊市", "2026-10-03_1030-1938", "DSC_3001.JPG", "NIKON Z 5", 85.0, 200, 37.9, "2026-10-03 10:30:00", sel.CANDIDATE),
        (6, "希腊市", "2026-10-03_1030-1938", "DSC_3002.JPG", "NIKON Z 5", 85.0, 200, 37.9, "2026-10-03 10:31:00", None),
    ]
    with db.write() as conn:
        conn.execute("INSERT INTO library (id,uuid,name,created_at,status,root_path_hint)"
                     " VALUES (1,'q','测试库','t','ONLINE','E:\\\\照片')")
        conn.execute("INSERT INTO session (id,name,city_hint,period_hint,created_at,updated_at)"
                     " VALUES (1,'S','多城','多时段','t','t')")
        conn.execute("INSERT INTO scene (id,session_id,name,created_at,updated_at) VALUES (1,1,'场景','t','t')")
        for i, city, period, name, camera, focal, iso, gps, when, status in rows:
            conn.execute(
                "INSERT INTO asset (id,library_id,relative_path,file_type,status,size,first_seen_at)"
                " VALUES (?,1,?,'jpeg','OK',?, 't')",
                (i, f"{city}\\{period}\\{name}", 1000 * i))
            conn.execute(
                "INSERT INTO shot (id,scene_id,primary_asset_id,captured_at,capture_date_local,"
                "created_at,updated_at) VALUES (?,1,?,?,?,'t','t')",
                (i, i, when, when[:10]))
            conn.execute("UPDATE asset SET shot_id=? WHERE id=?", (i, i))
            conn.execute(
                "INSERT INTO metadata (asset_id,camera_model,focal_length,iso,gps_lat,capture_at,"
                "capture_date_local) VALUES (?,?,?,?,?,?,?)",
                (i, camera, focal, iso, gps, when, when[:10]))
            if status:
                conn.execute("INSERT INTO selection (shot_id,status,updated_at) VALUES (?,?,'t')",
                             (i, status))


def _query(client: TestClient, filters=None, **extra) -> dict:
    body = dict(extra)
    if filters is not None:
        body["filters"] = filters
    response = client.post("/api/assets/query", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ============================================================ 基本筛选

def test_no_filters_returns_everything(client: TestClient) -> None:
    payload = _query(client)
    assert payload["total"] == 6
    assert payload["description"] == "全部照片"


def test_filter_by_city(client: TestClient) -> None:
    assert _query(client, {"city": {"eq": "杭州市"}})["total"] == 3


def test_filter_by_city_shorthand(client: TestClient) -> None:
    """允许简写 `"city": "杭州市"` 等价于 `{"eq": ...}`。"""
    assert _query(client, {"city": "杭州市"})["total"] == 3


def test_filter_by_city_in(client: TestClient) -> None:
    assert _query(client, {"city": {"in": ["杭州市", "希腊市"]}})["total"] == 5


def test_filter_by_period(client: TestClient) -> None:
    assert _query(client, {"period": {"eq": "2026-10-03_1030-1938"}})["total"] == 2


def test_filter_by_camera(client: TestClient) -> None:
    assert _query(client, {"camera_model": {"eq": "Apple iPhone 15 Pro"}})["total"] == 1


def test_filter_by_numeric_range(client: TestClient) -> None:
    assert _query(client, {"focal_length": {"gte": 200}})["total"] == 2
    assert _query(client, {"focal_length": {"between": [80, 250]}})["total"] == 3


def test_filter_by_date_range(client: TestClient) -> None:
    payload = _query(client, {"capture_date_local": {"gte": "2026-01-01"}})
    assert payload["total"] == 3


def test_filter_by_year(client: TestClient) -> None:
    assert _query(client, {"year": {"eq": "2025"}})["total"] == 3


def test_filter_by_year_accepts_an_integer(client: TestClient) -> None:
    """★ 回归：`year` 传**整数**必须和传字符串一样有效。

    `year` 的列是 `substr(capture_date_local,1,4)`（**文本**），而 SQLite 里
    "整数恒小于文本"，所以 `'2025' = 2025` 恒为假 —— 修复前这里会**静默返回 0 行**。
    故意破坏：把 `filter_query._coerce_year` 的调用删掉，本测试立刻变 0 != 3。
    """
    assert _query(client, {"year": {"eq": 2025}})["total"] == 3


def test_filter_by_year_in_accepts_integers(client: TestClient) -> None:
    assert _query(client, {"year": {"in": [2024, 2025]}})["total"] == 3


def test_bad_year_is_400_not_500(client: TestClient) -> None:
    """`year` 传非年份值 → 400（用户输入错误），不是 500。"""
    response = client.post("/api/assets/query", json={"filters": {"year": {"eq": "20xx"}}})
    assert response.status_code == 400, response.text
    assert "year" in response.json()["detail"]


def test_bad_collection_id_is_400_not_500(client: TestClient) -> None:
    """★ 回归：`collection_id` 传非数字曾经抛 `ValueError` → **500**。

    路由层只把 `QueryError` 翻译成 400，所以这里必须断言 **400**；
    修复点在 `filter_query._as_int`（故意破坏：把 `_as_int` 改回裸 `int()`，本测试变 500）。
    """
    response = client.post("/api/assets/query", json={"filters": {"collection_id": {"eq": "abc"}}})
    assert response.status_code == 400, response.text
    assert "collection_id" in response.json()["detail"]



def test_filter_by_gps_presence(client: TestClient) -> None:
    assert _query(client, {"has_gps": {"eq": True}})["total"] == 5
    assert _query(client, {"has_gps": {"eq": False}})["total"] == 1


def test_filter_by_selection_status(client: TestClient) -> None:
    assert _query(client, {"selection_status": {"eq": sel.FINAL}})["total"] == 1
    assert _query(client, {"selection_status": {"in": [sel.SELECTED, sel.CANDIDATE]}})["total"] == 2


def test_filter_unreviewed_uses_absent_rows(client: TestClient) -> None:
    """未审阅 = `selection` 表里没有行（Phase 1 建表时的语义）。"""
    assert _query(client, {"selection_status": {"eq": sel.UNREVIEWED}})["total"] == 3


def test_filters_are_combined_with_and(client: TestClient) -> None:
    payload = _query(client, {"city": {"eq": "杭州市"}, "focal_length": {"gte": 200}})
    assert payload["total"] == 2


def test_filter_by_collection(client: TestClient) -> None:
    db: Database = client.db  # type: ignore[attr-defined]
    cid = CollectionRepo(db).create("主题")
    CollectionRepo(db).add_shots(cid, [1, 4])
    assert _query(client, {"collection_id": {"eq": cid}})["total"] == 2


def test_filter_by_library(client: TestClient) -> None:
    assert _query(client, {"library_id": {"eq": 1}})["total"] == 6
    assert _query(client, {"library_id": {"eq": 999}})["total"] == 0


# ============================================================ 排序与分页

def test_sort_options(client: TestClient) -> None:
    """`capture_asc` 第一张是最早的（2025-10-25 10:00，asset 1）；
    `capture_desc` 第一张是**最晚**的（2026-10-03 10:31，asset 6 —— 与 asset 5 同一分钟，
    由 `relative_path` 做稳定的次序打破）。"""
    asc = _query(client, sort="capture_asc")
    desc = _query(client, sort="capture_desc")
    assert [item["id"] for item in asc["items"]] == [1, 2, 3, 4, 5, 6]
    assert [item["id"] for item in desc["items"]] == [6, 5, 4, 3, 2, 1]


def test_pagination(client: TestClient) -> None:
    page = _query(client, limit=2, offset=2)
    assert len(page["items"]) == 2
    assert page["total"] == 6
    assert page["offset"] == 2


def test_limit_is_capped(client: TestClient) -> None:
    """`limit` 上限 1000（PRD §11.2：不一次性加载全部）。"""
    assert _query(client, limit=99999)["limit"] == fq.MAX_LIMIT


def test_default_limit(client: TestClient) -> None:
    assert _query(client)["limit"] == fq.DEFAULT_LIMIT


def test_items_carry_the_same_shape_as_the_list_endpoint(client: TestClient) -> None:
    """筛选结果必须与 `/api/assets` 同形 —— 前端才能用同一套渲染。"""
    item = _query(client, limit=1)["items"][0]
    for key in ("id", "relative_path", "file_type", "selection_status", "has_thumb"):
        assert key in item


def test_description_is_human_readable(client: TestClient) -> None:
    payload = _query(client, {"camera_model": {"eq": "NIKON Z 5"}, "focal_length": {"gte": 200}})
    assert "机型" in payload["description"] and "焦距" in payload["description"]


# ============================================================ ★ 安全边界

def test_unknown_field_is_rejected(client: TestClient) -> None:
    """★ 未知字段 → 400。**绝不能静默忽略** —— 那会让用户以为筛选生效了。"""
    response = client.post("/api/assets/query", json={"filters": {"no_such_field": {"eq": 1}}})
    assert response.status_code == 400
    assert "不支持" in response.json()["detail"]


def test_unknown_operator_is_rejected(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"filters": {"city": {"regex": ".*"}}})
    assert response.status_code == 400
    assert "操作符" in response.json()["detail"]


def test_operator_not_allowed_for_field_is_rejected(client: TestClient) -> None:
    """`iso` 不允许 `like` —— 白名单是**逐字段**的，不是全局的。"""
    response = client.post("/api/assets/query", json={"filters": {"iso": {"like": "4"}}})
    assert response.status_code == 400


def test_unknown_request_key_is_rejected(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"raw_sql": "DROP TABLE asset"})
    assert response.status_code == 400
    assert "请求字段" in response.json()["detail"]


def test_unknown_sort_is_rejected(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"sort": "id; DROP TABLE asset"})
    assert response.status_code == 400


def test_in_sql_succeeds_and_injection_is_inert(client: TestClient) -> None:
    """★ 注入尝试：值里带 SQL 片段时必须被当**普通字符串**，不影响表结构。"""
    evil = "'; DROP TABLE asset; --"
    payload = _query(client, {"city": {"eq": evil}})
    assert payload["total"] == 0                 # 没有城市叫这个名字
    db: Database = client.db  # type: ignore[attr-defined]
    assert int(db.scalar("SELECT COUNT(*) FROM asset", default=0)) == 6   # 表还在、数据没少


def test_like_treats_the_value_as_a_literal(client: TestClient) -> None:
    """★ `like` 不接受用户写的通配符：`%` 被转义，只按字面量匹配。"""
    # 库里有 `DSC_1001.JPG`，但用户写 `%` 应当被当字面量，因此匹配不到
    assert _query(client, {"relative_path": {"like": "DSC_1001"}})["total"] == 1
    assert _query(client, {"relative_path": {"like": "%"}})["total"] == 0
    assert _query(client, {"relative_path": {"like": "DSC_1001%"}})["total"] == 0


def test_like_underscore_is_escaped(client: TestClient) -> None:
    """`_` 是 SQL LIKE 的单字符通配符 —— 必须转义，否则 `DSC_1` 会错配。"""
    assert _query(client, {"relative_path": {"like": "DSC_1001"}})["total"] == 1
    assert _query(client, {"relative_path": {"like": "DSCx1001"}})["total"] == 0


def test_between_requires_two_values(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"filters": {"iso": {"between": [100]}}})
    assert response.status_code == 400


def test_in_requires_non_empty_list(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"filters": {"city": {"in": []}}})
    assert response.status_code == 400


def test_boolean_field_requires_boolean(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"filters": {"has_gps": {"eq": "maybe"}}})
    assert response.status_code == 400


def test_filters_must_be_an_object(client: TestClient) -> None:
    response = client.post("/api/assets/query", json={"filters": ["city", "eq", "杭州"]})
    assert response.status_code == 400


def test_no_destructive_endpoint_added(client: TestClient) -> None:
    """本端点不得引入任何含 delete/remove 的路由。

    判据与 `test_api.py` 共用同一份（`route_guard.py`）—— 这里曾经自己写了一份
    子串匹配，于是 `confirm-all` 里的 `rm` 把两条断言一起打红。
    """
    paths = {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]
    assert "/api/assets/query" in paths
    for path in sorted(paths):
        hits = destructive_hits(path)
        assert not hits, f"{path} 看起来像破坏性路由（{sorted(hits)}）"


# ============================================================ 字段发现

def test_fields_endpoint_lists_the_contract(client: TestClient) -> None:
    """让界面/自然语言层能自己发现契约，而不是把字段名硬编码在两处、慢慢漂移。"""
    payload = client.get("/api/assets/query/fields").json()
    assert "city" in payload["fields"]
    assert "eq" in payload["fields"]["city"]
    assert payload["limit"]["max"] == fq.MAX_LIMIT
    assert "capture_desc" in payload["sorts"]


def test_fields_endpoint_is_not_shadowed_by_the_asset_route(client: TestClient) -> None:
    """★ 路由顺序回归：`/api/assets/query/fields` 不能被 `/api/assets/{asset_id}` 抢走。

    Starlette 按注册顺序匹配；若把参数化路由排在前面，这个端点会返回 422
    （它会把 "query" 当成 asset_id 解析）。
    """
    response = client.get("/api/assets/query/fields")
    assert response.status_code == 200, response.text
    assert "fields" in response.json()


# ============================================================ 纯函数层（不经 HTTP）

def test_build_where_binds_all_values() -> None:
    clauses, params = fq.build_where({"city": {"eq": "杭州市"}, "iso": {"gte": 400}})
    assert len(clauses) == 2 and params == ["杭州市", 400]
    assert "?" in clauses[0] and "杭州市" not in clauses[0]


def test_build_where_handles_none_condition() -> None:
    assert fq.build_where({"city": None}) == ([], [])


def test_describe_is_empty_for_no_filters() -> None:
    assert fq.describe(None) == "全部照片"


def test_normalise_rejects_non_dict_payload() -> None:
    with pytest.raises(fq.QueryError):
        fq.normalise_request(["not", "a", "dict"])


def test_normalise_clamps_negative_offset() -> None:
    assert fq.normalise_request({"offset": -5})["offset"] == 0
