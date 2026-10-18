"""前端资源的缓存契约（Phase 3 修复的真实故障）。

## 这条测试守的是什么故障

用户报「文字搜索下拉出不来」。排查结果是**前端资源被浏览器启发式缓存**：

  * `/`（index.html）原来是一个裸 `HTMLResponse`，**既没有 Cache-Control 也没有 ETag**
  * `/static/*` 由 `StaticFiles` 提供，**有 ETag 但没有 Cache-Control**

两者都让浏览器有机会**不重新取文件**，于是用户拿到「旧 JS + 新 HTML」——
新做的下拉框出不来，而按一次 `Ctrl+F5` 就好。这类问题最难查，
因为它**在服务端一切正常**（我自己用 headless 浏览器测永远是好的）。

## 判据（可复现，且破坏实现会立刻变红）

  1. `/` 与 `/static/**` 的响应必须带 `Cache-Control: no-cache`
     —— **允许缓存，但每次必须带 ETag 回服务端校验**（零构建、无内容哈希文件名，
     所以"校验"是唯一可靠的陈旧检测手段）
  2. `/` 与 `/static/**` 必须带 `ETag`（没有校验线索，`no-cache` 也无从谈起）
  3. 带 `If-None-Match` 的条件请求必须 **304**（证明校验真的在跑，而不是形同虚设）
  4. **不存在的静态文件仍然 404** —— 自定义的 `StaticFiles` 子类不能把错误吃掉

## 反向验证

把 `_NoCacheStatic` 换回普通 `StaticFiles`、或把 index 的 headers 去掉 →
第 1 条立刻失败。本文件存在的意义就是让那次修复**不会再被静默改回去**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api.app import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations

WEB_ROOT = Path(__file__).resolve().parents[2] / "backend" / "photoos" / "web"


@pytest.fixture()
def client(tmp_path: Path):
    """最小可用的库 + TestClient。

    **关键隔离**：与 `test_api.py` 一样用 `use_config_path(None)` 禁用开发机上的
    `data/config.json` —— 否则真实配置会覆盖 `data_root`，测试会静默作用在真实库上
    （本项目真实踩过的陷阱，已由 `test_config_isolation` 守护）。
    """
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        apply_migrations(Database(settings.db_path), backup_dir=settings.backups_dir)
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def _has_frontend() -> bool:
    return (WEB_ROOT / "index.html").is_file()


@pytest.mark.skipif(not _has_frontend(), reason="前端资源不在仓库里（打包形态）")
def test_index_and_static_disable_heuristic_caching(client: TestClient) -> None:
    """`/` 与静态资源必须声明 `no-cache`，否则浏览器可能一直用旧文件。

    ★ 这就是「下拉出不来」那个故障的守护断言。把 index 的 headers 去掉、
    或把 `_NoCacheStatic` 换回 `StaticFiles`，本条立刻变红。
    """
    for path in ("/", "/static/js/app.js", "/static/css/phase2.css"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} 取不到"
        assert response.headers.get("cache-control") == "no-cache", (
            f"{path} 缺少 Cache-Control: no-cache —— 浏览器会启发式缓存，"
            f"改了前端却看不到（实测踩过）"
        )


@pytest.mark.skipif(not _has_frontend(), reason="前端资源不在仓库里")
def test_frontend_assets_carry_an_etag(client: TestClient) -> None:
    """`no-cache` 的前提是有校验线索 —— 没有 ETag 就等于每次全量重下。"""
    for path in ("/", "/static/js/search.js"):
        response = client.get(path)
        assert response.headers.get("etag"), f"{path} 没有 ETag"


@pytest.mark.skipif(not _has_frontend(), reason="前端资源不在仓库里")
def test_conditional_request_returns_304(client: TestClient) -> None:
    """带 `If-None-Match` 时必须 304，证明校验真的在跑（而不是形同虚设）。"""
    first = client.get("/static/js/search.js")
    etag = first.headers.get("etag")
    assert etag
    again = client.get("/static/js/search.js", headers={"If-None-Match": etag})
    assert again.status_code == 304, (
        f"条件请求应当 304（未变），实际 {again.status_code} —— 校验没生效"
    )


@pytest.mark.skipif(not _has_frontend(), reason="前端资源不在仓库里")
def test_missing_static_file_is_still_404(client: TestClient) -> None:
    """自定义 StaticFiles 子类**不得吃掉错误路径**。

    为什么值得断言：我们为了让静态资源带 `Cache-Control` 而覆写了
    `file_response`。覆写很容易顺手把 404/206（Range）这些路径一起改坏，
    而"能取到文件"的测试**发现不了**这类破坏。
    """
    assert client.get("/static/js/definitely-not-here.js").status_code == 404
