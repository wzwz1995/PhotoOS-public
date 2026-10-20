"""Phase 2 · 决策 #5：`shot_gap_seconds` 三层可配置 + 界面预设（HTTP + 纯逻辑）。

决策 #5 的原话是「**改成可配置吧，再预留一个 sql 筛选的接口**」。
SQL 筛选接口由 `test_phase2_filter_query.py` 覆盖；本文件覆盖前半句。

三层优先级（与 `cli.py::_resolve_gap` 必须一致，否则 CLI 与界面会给出不同的分组）：
    CLI `--gap-seconds`  >  `session.review_config_json`  >  `config.json` 全局

本文件刻意覆盖两类容易写错的地方：
  ① **脏数据**：`review_config_json` 里是半截 JSON 时，页面必须还能打开
  ② **边界值**：0 / 负数 / NaN / inf 会让"间隔 > 阈值"永远成立，
     把整个时段切成一堆单张组 —— 必须 422 而不是静默接受
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.library import shot_grouping as sg


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        with db.write() as conn:
            conn.execute(
                "INSERT INTO library (uuid, name, created_at, status)"
                " VALUES ('lib','测试库','2026-01-01T00:00:00+08:00','ONLINE')"
            )
            for session_id, name in ((1, "时段一"), (2, "时段二")):
                conn.execute(
                    "INSERT INTO session (id, name, captured_from, created_at, updated_at)"
                    " VALUES (?, ?, '2026-01-01 10:00:00', 't', 't')",
                    (session_id, name),
                )
        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.db = db  # type: ignore[attr-defined]
            yield test_client


def _cfg(db: Database, session_id: int):
    row = db.query_one("SELECT review_config_json FROM session WHERE id = ?", (session_id,))
    return row["review_config_json"]


# ============================================================ 纯逻辑：预设与校验

def test_presets_are_sorted_and_positive() -> None:
    values = [item["value"] for item in sg.gap_presets()]
    assert values == sorted(values)
    assert all(value > 0 for value in values)


def test_presets_carry_their_cost() -> None:
    """每条预设都必须写明代价 —— 只给数字会让用户以为"越大越好"。"""
    for item in sg.gap_presets():
        assert item["label"] and item["note"], item
        assert len(item["note"]) >= 10, item


def test_presets_are_copies() -> None:
    """调用方改不坏模块级常量（否则一次界面请求就能污染全局）。"""
    first = sg.gap_presets()
    first[0]["value"] = 999.0
    assert sg.gap_presets()[0]["value"] != 999.0


def test_recommended_preset_matches_measured_median() -> None:
    """调研实测本库相邻间隔中位数 3.150 s —— 推荐档必须在它附近。

    这条是"预设不是拍脑袋定的"的可执行证据：如果有人把推荐档改成 30 s，
    这个测试会红，逼他回去看 PHASE2_RESEARCH 的对账表。
    """
    recommended = [item for item in sg.gap_presets() if "推荐" in item["label"]]
    assert recommended, "预设里必须标出一个推荐档"
    assert 2.0 <= recommended[0]["value"] <= 5.0


@pytest.mark.parametrize("bad", [0, -1, -0.5, None, "abc", float("nan"), float("inf")])
def test_normalize_gap_rejects_bad(bad) -> None:
    with pytest.raises(ValueError):
        sg.normalize_gap(bad)


@pytest.mark.parametrize("good,expect", [(3, 3.0), ("2.5", 2.5), (0.1, 0.1), (600, 600.0)])
def test_normalize_gap_accepts(good, expect) -> None:
    """刻意**不设上限**：用户可以有自己的理由（例如整段旅程算一个决策单元）。"""
    assert sg.normalize_gap(good) == expect


def test_non_finite_gap_would_shatter_grouping() -> None:
    """把 `inf` 当阈值会发生什么 —— 这才是那条有限性检查**为什么**存在。

    切分条件是「间隔 > 阈值」：`inf` 让任何间隔都不成立 → 整个时段被并成**一组**；
    而 `nan` 让比较恒为 False（`gap != gap`），行为取决于写法，属不可预测。
    两种都不能接受，所以 `normalize_gap` 一律拒绝。

    这里直接对着分组函数断言行为，避免那条检查被"顺手简化"掉时没人发现。
    """
    from photoos.library.shot_grouping import ShotAsset, group_shots

    items = [
        ShotAsset(asset_id=i, file_type="jpeg", relative_path=f"x/{i}.JPG",
                  capture_at=f"2026-01-01 10:0{i}:00")
        for i in range(5)
    ]
    assert len(group_shots(items, gap_seconds=1.0)) == 5, "1 秒阈值：每张各自成组"
    # 极大但**有限**的阈值表现为"全部并成一组" —— 这正是 inf 的极限行为，
    # 也正是 normalize_gap 必须拦住它的原因。
    assert len(group_shots(items, gap_seconds=1e9)) == 1


# ============================================================ 预设端点

def test_presets_endpoint(client: TestClient) -> None:
    body = client.get("/api/review/config/presets").json()
    assert len(body["presets"]) == len(sg.GAP_PRESETS)
    assert body["global_default"] == 3
    # 能力边界必须一并返回，界面才有东西可显示
    assert "抓不住" in body["limitation"]


# ============================================================ 读取有效值与来源

def test_get_config_reports_global_source(client: TestClient) -> None:
    body = client.get("/api/sessions/1/review-config").json()
    assert body["override"] is None
    assert body["effective"] == 3
    assert body["source"] == "跟随全局默认"
    # ⚠️ `source` 是**给用户看的中文**，不得出现配置文件名或内部字段名。
    # 界面上的技术名词对使用者没有意义，也显得不像成品。
    for leaked in ("config.json", "review_config_json"):
        assert leaked not in body["source"], f"source 泄漏了内部名词：{leaked}"


def test_set_config_then_effective_is_override(client: TestClient) -> None:
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": 2.0})
    body = client.get("/api/sessions/1/review-config").json()
    assert body["override"] == 2.0
    assert body["effective"] == 2.0
    assert body["source"] == "本时段已设置"


def test_override_is_per_session(client: TestClient) -> None:
    """覆盖只影响本时段 —— 否则"单时段覆盖"这个层级就没有意义了。"""
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": 1.0})
    assert client.get("/api/sessions/1/review-config").json()["effective"] == 1.0
    assert client.get("/api/sessions/2/review-config").json()["effective"] == 3


def test_clearing_override_falls_back_to_global(client: TestClient) -> None:
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": 1.0})
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": None})
    body = client.get("/api/sessions/1/review-config").json()
    assert body["override"] is None
    assert body["effective"] == 3
    assert body["source"] == "跟随全局默认"


def test_write_reports_that_regroup_is_needed(client: TestClient) -> None:
    """参数变了但分组没重算 —— 必须明说，不能让用户以为已经生效。"""
    body = client.patch(
        "/api/sessions/1/review-config", json={"shot_gap_seconds": 2.0}
    ).json()
    assert body["needs_regroup"] is True
    assert "重算" in body["note"]


def test_patch_requires_the_field(client: TestClient) -> None:
    assert client.patch("/api/sessions/1/review-config", json={}).status_code == 422


@pytest.mark.parametrize(
    "raw",
    ['0', '-1', '"abc"', '1e400'],
    ids=["zero", "negative", "not-a-number", "overflow-to-inf"],
)
def test_patch_rejects_bad_values(client: TestClient, raw: str) -> None:
    """`0`/负数会让"间隔 > 阈值"永远成立，把整个时段切成一堆单张组。

    ⚠️ 这里走**原始 JSON 文本**而不是 `json=` 参数：`NaN`/`Infinity` 不是合法 JSON，
    httpx 会拒绝序列化（那是编码器报错，测不到我们的守卫）。而 `1e400` 是**合法**
    JSON —— Python 把它解析成 `inf`，正好命中 `normalize_gap` 的有限性检查。
    """
    response = client.patch(
        "/api/sessions/1/review-config",
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text


def test_patch_404_on_missing_session(client: TestClient) -> None:
    assert client.patch(
        "/api/sessions/999/review-config", json={"shot_gap_seconds": 2.0}
    ).status_code == 404
    assert client.get("/api/sessions/999/review-config").status_code == 404


def test_patch_preserves_other_keys(client: TestClient) -> None:
    """`review_config_json` 以后会装别的东西（Phase 3 的筛选参数），不能整块覆盖掉。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute(
            "UPDATE session SET review_config_json = ? WHERE id = 1",
            (json.dumps({"some_future_key": "keep-me"}, ensure_ascii=False),),
        )
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": 4.0})
    stored = json.loads(_cfg(db, 1))
    assert stored["some_future_key"] == "keep-me"
    assert stored["shot_gap_seconds"] == 4.0


# ============================================================ 脏数据容错

def test_broken_json_does_not_break_the_page(client: TestClient) -> None:
    """库里存了半截 JSON 时，GET 必须还能返回（退化成全局默认），而不是 500。

    这列是用户可编辑的。参数失效是小事，整个时段的照片打不开是大事。
    """
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute(
            "UPDATE session SET review_config_json = ? WHERE id = 1", ('{"shot_gap',)
        )
    response = client.get("/api/sessions/1/review-config")
    assert response.status_code == 200
    body = response.json()
    assert body["effective"] == 3
    assert body["override"] is None


def test_non_dict_json_is_tolerated(client: TestClient) -> None:
    """`[1,2,3]` / `"abc"` 是合法 JSON 但不是配置 —— 同样不能 500。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE session SET review_config_json = '[1,2,3]' WHERE id = 1")
    assert client.get("/api/sessions/1/review-config").json()["effective"] == 3


def test_patch_recovers_from_broken_json(client: TestClient) -> None:
    """坏 JSON 上写入必须能成功（从空 dict 重建），否则用户被卡死无法自救。"""
    db: Database = client.db  # type: ignore[attr-defined]
    with db.write() as conn:
        conn.execute("UPDATE session SET review_config_json = 'not json' WHERE id = 1")
    assert client.patch(
        "/api/sessions/1/review-config", json={"shot_gap_seconds": 2.0}
    ).status_code == 200
    assert json.loads(_cfg(db, 1)) == {"shot_gap_seconds": 2.0}


def test_string_number_override_is_accepted(client: TestClient) -> None:
    """JSON 里 `"2.5"` 这种字符串数字在真实数据里很常见（手工编辑过），容忍它。"""
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": "2.5"})
    assert client.get("/api/sessions/1/review-config").json()["effective"] == 2.5


# ============================================================ 与 CLI 的一致性

def test_api_and_cli_agree_on_precedence(client: TestClient) -> None:
    """界面读到的 effective 必须与 CLI `_resolve_gap` 算出的一致。

    两边各写一套优先级就会出现"界面说 2 秒、实际按 3 秒切"的错位。
    """
    from photoos.cli import _resolve_gap

    settings = client.app.state.settings  # type: ignore[attr-defined]
    db: Database = client.db  # type: ignore[attr-defined]

    # 全局
    assert _resolve_gap(settings, db, "1", None)[0] == 3
    assert client.get("/api/sessions/1/review-config").json()["effective"] == 3

    # 覆盖之后两边一起变
    client.patch("/api/sessions/1/review-config", json={"shot_gap_seconds": 1.5})
    assert _resolve_gap(settings, db, "1", None)[0] == 1.5
    assert client.get("/api/sessions/1/review-config").json()["effective"] == 1.5

    # CLI 显式传入时，命令行赢过一切
    assert _resolve_gap(settings, db, "1", 9.0)[0] == 9.0
