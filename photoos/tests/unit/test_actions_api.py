"""`/api/actions` 与 `/api/actions/{id}/run` 的 HTTP 层契约。

## 为什么单独有这个文件

这两个端点是**界面唯一的命令执行通道**。`actions/catalog.py` 自己有单测，
但端点层此前**一条 HTTP 断言都没有**（2026-10 审计发现：
`grep api/actions tests/` 零命中）。也就是说——
命令模板写错、只接受 id 的边界被放宽、缺 `query` 时返回 500 而不是 422，
这些都不会被任何测试拦住。

## 这里断言什么（以及故意破坏什么会让它变红）

| 断言 | 故意破坏 |
| --- | --- |
| 浏览器拿到的是**说明 + 一行渲染好的预览**，不是 `argv` 数组 | 让 `catalog_payload` 返回 `argv` |
| 未知 action id → **404**（不静默忽略） | 把 404 改成 200/空任务 |
| 动作缺 `query` → **422** | 把 `validate_query` 的 ValueError 改成不抛 |
| `query` 含命令行元字符 → **422** | 放宽 `_QUERY_OK` 正则 |

**不**在这里触发真正入队：那会拉起 subprocess（`cli_action` 任务），
既慢又依赖本机环境。入队逻辑的正确性由 `jobs` 的测试与手工验收覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations


@pytest.fixture()
def client(tmp_path: Path):
    """最小可用的应用：只要迁移过的库 + 装配好的 app（不需要照片）。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def test_actions_list_is_readable(client: TestClient) -> None:
    response = client.get("/api/actions")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert isinstance(payload["actions"], list) and payload["actions"]
    assert isinstance(payload["environment"], dict)

    first = payload["actions"][0]
    for key in ("id", "label", "description", "group", "needs_query",
                "long_running", "requires", "missing_requires", "available"):
        assert key in first, f"动作条目缺少 {key}"


def test_actions_list_never_leaks_an_argv_array(client: TestClient) -> None:
    """★ 安全边界：浏览器**不能**拿到可以直接执行的参数数组。

    它只该看到一行供显示的 `command_preview`（字符串）。
    故意破坏：让 `catalog_payload()` 把 `argv` 也塞进条目，本测试立刻变红。
    """
    payload = client.get("/api/actions").json()
    for item in payload["actions"]:
        assert "argv" not in item, f"动作 {item['id']} 泄露了 argv"
        assert isinstance(item["command_preview"], str)
        assert item["command_preview"], f"动作 {item['id']} 的 command_preview 是空的"


def test_every_action_has_a_unique_id(client: TestClient) -> None:
    ids = [item["id"] for item in client.get("/api/actions").json()["actions"]]
    assert len(ids) == len(set(ids)), f"动作 id 重复：{ids}"


def test_unknown_action_id_is_404(client: TestClient) -> None:
    """未知 id 必须明确报错 —— 静默接受会让界面以为点了什么。"""
    response = client.post("/api/actions/definitely-not-an-action/run", json={})
    assert response.status_code == 404, response.text
    assert "definitely-not-an-action" in response.json()["detail"]


def test_action_needing_query_rejects_missing_query(client: TestClient) -> None:
    """需要搜索词的动作缺 `query` → **422**（用户输入问题），不是 500。"""
    actions = {item["id"]: item for item in client.get("/api/actions").json()["actions"]}
    needs_query = [aid for aid, item in actions.items() if item["needs_query"]]
    assert needs_query, "目录里应当至少有一个需要搜索词的动作"

    response = client.post(f"/api/actions/{needs_query[0]}/run", json={})
    assert response.status_code == 422, response.text


def test_action_needing_prompt_rejects_missing_or_unsafe_prompt(client: TestClient) -> None:
    """改词义动作：query 合法但 prompt 缺失/带命令行元字符 → 422。"""
    actions = {
        item["id"]: item for item in client.get("/api/actions").json()["actions"]
    }
    needs_prompt = [aid for aid, item in actions.items() if item.get("needs_prompt")]
    assert needs_prompt, "目录里应当至少有一个需要 prompt 的动作"
    action_id = needs_prompt[0]

    missing = client.post(f"/api/actions/{action_id}/run", json={"query": "月亮"})
    assert missing.status_code == 422
    unsafe = client.post(
        f"/api/actions/{action_id}/run",
        json={"query": "月亮", "prompt": "moon; rm -rf /"},
    )
    assert unsafe.status_code == 422


def test_action_query_rejects_shell_metacharacters(client: TestClient) -> None:
    """★ `query` 是**唯一**能进入命令行的用户输入，元字符必须被白名单挡住。

    这条同时证明"用户输入拼不出命令行"：`;` / `|` / `$` 都不在白名单里。
    故意破坏：放宽 `_QUERY_OK` 正则，本测试变红。
    """
    needs_query = [
        item["id"] for item in client.get("/api/actions").json()["actions"]
        if item["needs_query"]
    ][0]
    for bad in ("月亮; rm -rf /", "月亮 | cat /etc/passwd", "$(whoami)", "`id`"):
        response = client.post(f"/api/actions/{needs_query}/run", json={"query": bad})
        assert response.status_code == 422, f"{bad!r} 竟然被接受了：{response.text}"
