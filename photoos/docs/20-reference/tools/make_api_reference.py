#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate docs/20-reference/40-HTTP接口参考.md from the FastAPI app itself.

Inputs
------
* `photoos.api.create_app()` 的路由表（本脚本 import 后直接枚举，**权威**）
* 可选：运行中服务的 `/openapi.json`（用来交叉核对源码与线上是否一致）

Output
------
* `photoos/docs/20-reference/40-HTTP接口参考.md`

Why generated
-------------
83 个 operation 手抄一定会抄错，而且会随着代码漂移。本脚本让这份参考成为
**路由表的转写**：代码改了重跑一次即可。

Safety
------
* 用 `use_config_path(None)` **禁用开发机的 `data/config.json`**，
  并指向一个**临时 data_root**，因此不会碰真实库
  （这是本项目 `tests/unit/test_api.py:52-58` 记录的陷阱：不禁用配置时，
  测试会静默作用在真实照片库上）。
* 只读取源码与可选的 HTTP GET，不写任何产品数据。

Usage
-----
    python docs/20-reference/tools/make_api_reference.py
    python docs/20-reference/tools/make_api_reference.py --check-local   # 顺带比对线上
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1]
REPO = DOCS.parents[2]
BACKEND = REPO / "photoos" / "backend"
OUT = DOCS / "40-HTTP接口参考.md"
BASE_URL = "http://127.0.0.1:8760"
ROUTES_JSON = REPO / "_probe" / "docwork" / "routes.json"

sys.path.insert(0, str(BACKEND))

#: 分组顺序与显示名（键是路径里 `/api/<key>` 的那一段；`""` = 顶层与前端）
GROUP_ORDER: list[tuple[str, str]] = [
    ("health", "健康与自述"),
    ("config", "配置"),
    ("libraries", "库与卷"),
    ("browse", "浏览轴"),
    ("facets", "分面（筛选下拉项）"),
    ("assets", "资产（列表 / 详情 / 媒体）"),
    ("stats", "统计"),
    ("filter", "多维筛选"),
    ("find", "确定性取片（按时间地点）"),
    ("scenes", "场景"),
    ("sessions", "时段"),
    ("shots", "拍摄组（含精选与分组调整）"),
    ("selection", "精选"),
    ("collections", "主题"),
    ("semantic", "语义搜索与反馈"),
    ("people", "人物"),
    ("faces", "人脸"),
    ("jobs", "后台任务"),
    ("actions", "界面可触发的命令"),
    ("import", "导入（预演 / 执行）"),
    ("maintenance", "维护（备份 / 查重）"),
    ("exports", "打包导出"),
    ("integrity", "完整性"),
    ("review", "精选页配置"),
    ("cli", "命令直跑"),
]

#: 需要二次确认的端点（判据在源码里，这里只做**标注**）
CONFIRM_HINT = "confirm"

#: FastAPI 自动挂载的文档路由——**不是**我们的接口，必须排除，
#: 否则 operation 数会比 `/openapi.json` 报的多 4 个（实测 87 vs 83）。
AUTO_DOC_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def is_auto_doc(path: str) -> bool:
    return path in AUTO_DOC_PATHS


def build_routes() -> list[dict]:
    """装配 app 并枚举路由（不启动服务、不连真实库）。"""
    from photoos.api.app import create_app
    from photoos.config import Settings, use_config_path

    with tempfile.TemporaryDirectory(prefix="photoos-routes-") as tmp:
        with use_config_path(None):
            settings = Settings(data_root=Path(tmp) / "data")
            settings.ensure_dirs()
            app = create_app(settings)
            routes = []
            for route in app.routes:
                path = getattr(route, "path", None)
                methods = sorted(m for m in (getattr(route, "methods", None) or [])
                                 if m not in ("HEAD", "OPTIONS"))
                if not path or not methods:
                    continue
                if is_auto_doc(path):
                    continue
                fn = getattr(route, "endpoint", None)
                doc = inspect.getdoc(fn) or ""
                summary = doc.splitlines()[0].strip() if doc else ""
                params = []
                try:
                    for name, param in inspect.signature(fn).parameters.items():
                        if name in ("request", "self"):
                            continue
                        ann = param.annotation
                        ann_name = getattr(ann, "__name__", None) or str(ann)
                        if "Request" in ann_name or "Response" in ann_name:
                            continue
                        required = param.default is inspect.Parameter.empty
                        # FastAPI 注入的依赖项（如 settings）跳过
                        if "Depends" in repr(param.default):
                            continue
                        params.append((name, ann_name,
                                       "必填" if required else "可选"))
                except (TypeError, ValueError):
                    pass
                routes.append({
                    "path": path,
                    "methods": methods,
                    "summary": summary,
                    "doc": doc,
                    "module": getattr(fn, "__module__", ""),
                    "qualname": getattr(fn, "__qualname__", ""),
                    "params": params,
                })
            return routes


def source_line_index() -> dict[str, int]:
    """函数名 → 定义所在行号（用于给出 `file:line` 引用）。"""
    index: dict[str, int] = {}
    for py in (BACKEND / "photoos" / "api").glob("*.py"):
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        rel = py.relative_to(REPO).as_posix()
        for i, line in enumerate(lines, 1):
            m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", line)
            if m:
                # 同名函数以最后一次出现为准（后定义覆盖）
                index[m.group(1)] = i
                index[f"{rel}::{m.group(1)}"] = i
    return index


def group_of(path: str) -> str:
    m = re.match(r"^/api/([a-z_]+)", path)
    if m:
        return m.group(1)
    return ""


def classify(routes: list[dict]) -> tuple[int, int]:
    ops = sum(len(r["methods"]) for r in routes)
    gets = sum(1 for r in routes if "GET" in r["methods"])
    return ops, gets


def check_local(routes: list[dict]) -> str | None:
    """与运行中服务的 /openapi.json 比对；服务没跑就返回 None。"""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=5) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    live = {(p, m.upper()) for p, ops in spec.get("paths", {}).items()
            for m in ops if m.lower() in ("get", "post", "put", "patch", "delete")}
    src = {(r["path"], m) for r in routes for m in r["methods"]}
    only_src = sorted(src - live)
    only_live = sorted(live - src)
    lines = [f"线上 operation 数：{len(live)}；源码 operation 数：{len(src)}"]
    if only_src:
        lines.append(f"⚠️ 只在源码里（服务是旧的？）：{only_src}")
    if only_live:
        lines.append(f"⚠️ 只在线上的（源码里没有？）：{only_live}")
    if not only_src and not only_live:
        lines.append("✅ 双向零差异 —— 8760 上跑的就是这份路由表")
    return "\n".join(lines)


def main() -> int:
    routes = build_routes()
    line_index = source_line_index()
    ops, gets = classify(routes)
    crosscheck = check_local(routes) if "--check-local" in sys.argv else None

    ROUTES_JSON.parent.mkdir(parents=True, exist_ok=True)
    ROUTES_JSON.write_text(json.dumps(routes, ensure_ascii=False, indent=1),
                           encoding="utf-8", newline="\n")

    groups: dict[str, list[dict]] = {}
    for r in routes:
        groups.setdefault(group_of(r["path"]), []).append(r)

    out: list[str] = []
    add = out.append
    add("# 40 · HTTP 接口参考")
    add("")
    add("> **本篇由脚本生成，不要手改** —— 它是 FastAPI 路由表的转写。")
    add("> 重新生成：`python docs/20-reference/tools/make_api_reference.py`")
    add(">")
    add(f"> 【实测】源码共 **{ops} 个 operation**（{len(routes)} 条路由，"
        f"其中 GET {gets} 条）。路径参数写作 `{{name}}`。")
    if crosscheck:
        add(">")
        add("> **与运行中服务的交叉核对**：")
        for line in crosscheck.splitlines():
            add(f"> {line}")
    add("")
    add("---")
    add("")
    add("## 0. 调用纪律（先读这五条）")
    add("")
    add("1. **只有回环地址、没有鉴权** —— 单用户本地优先，这是有意的"
        "（`api/app.py:4`）。你在本机 `curl` 它就是全部权限。")
    add("2. **同步 DB 逻辑必须走 `_read()`**（`anyio.to_thread.run_sync`，"
        "`api/app.py:3395-3404`）。在 async 路由里直接调 sqlite3 会阻塞整个服务。")
    add("3. **错误响应有三种形状**（兜底 500 / 校验 422 / 领域 `{code,message,detail}`），"
        "另有路由内专用码 428（需二次确认）、501（保留的浏览轴）、409（离线/已退役）。"
        "详见 [`20-后端架构与模块地图.md`](20-后端架构与模块地图.md) §3.1。")
    add("4. **服务端没有删除原片的入口** —— 全仓有守卫测试断言路由里不出现"
        "破坏性动词（`tests/unit/route_guard.py`）。")
    add("5. **`/docs` 与 `/openapi.json` 永远比本篇新** —— 本篇的价值是"
        "“分组 + 读写标注 + 源码位置”，不是替代交互式文档。")
    add("")
    add("---")
    add("")
    add("## 1. 速查：按分组的接口数")
    add("")
    add("| 分组 | 端点数 | 分组 | 端点数 |")
    add("| --- | --- | --- | --- |")
    ordered = [(k, len(v)) for k, v in groups.items()]
    ordered.sort(key=lambda kv: -kv[1])
    half = (len(ordered) + 1) // 2
    left, right = ordered[:half], ordered[half:]
    for i in range(half):
        lk, lc = left[i]
        if i < len(right):
            rk, rc = right[i]
            add(f"| `{lk or '(顶层/前端)'}` | {lc} | `{rk or '(顶层/前端)'}` | {rc} |")
        else:
            add(f"| `{lk or '(顶层/前端)'}` | {lc} | | |")
    add("")
    add("---")
    add("")
    add("## 2. 全部接口")
    add("")

    seen: set[str] = set()
    for key, title in GROUP_ORDER:
        if key not in groups:
            continue
        seen.add(key)
        n = len(groups[key])
        add(f"### {title} · `{n}` 个端点" if key else f"### {title}")
        add("")
        add("| 方法 | 路径 | 说明 | 关键参数 | 源码 |")
        add("| --- | --- | --- | --- | --- |")
        for r in sorted(groups[key], key=lambda x: x["path"]):
            methods = "/".join(r["methods"])
            summary = (r["summary"] or "—").replace("|", "\\|")
            if len(summary) > 90:
                summary = summary[:88] + "…"
            params = "、".join(f"`{n}`({k})" for n, _t, k in r["params"][:6])
            if len(r["params"]) > 6:
                params += " …"
            line = line_index.get(r["qualname"].split(".")[-1], 0)
            src = f"`api/app.py:{line}`" if line else "—"
            add(f"| `{methods}` | `{r['path']}` | {summary} | {params or '—'} | {src} |")
        add("")
    leftover = [k for k in groups if k not in seen]
    if leftover:
        add("### 其它")
        add("")
        add("| 方法 | 路径 | 说明 |")
        add("| --- | --- | --- |")
        for k in leftover:
            for r in sorted(groups[k], key=lambda x: x["path"]):
                add(f"| `{'/'.join(r['methods'])}` | `{r['path']}` | "
                    f"{(r['summary'] or '—')[:80]} |")
        add("")
    add("---")
    add("")
    add("## 3. 写操作清单（改动数据的那些）")
    add("")
    add("【实测】非 GET 的 operation 一共 "
        f"**{ops - gets}** 个。它们按风险分三档：")
    add("")
    add("| 档 | 判据 | 端点 |")
    add("| --- | --- | --- |")
    mutating = [r for r in routes if "GET" not in r["methods"]]
    destructive = [r for r in mutating
                   if re.search(r"(delete|remove|detach|dissolve|purge)", r["path"])]
    confirming = [r for r in mutating if r not in destructive and CONFIRM_HINT in r["path"]]
    other = [r for r in mutating if r not in destructive and r not in confirming]

    def fmt(rs: list[dict]) -> str:
        if not rs:
            return "—"
        return "、".join(f"`{'/'.join(r['methods'])} {r['path']}`" for r in rs[:14]) + \
               ("…" if len(rs) > 14 else "")

    add(f"| 删除类（路径含 delete/remove/detach/dissolve） | 需二次确认 | {fmt(destructive)} |")
    add(f"| 确认类（路径含 confirm） | 表单里带确认参数 | {fmt(confirming)} |")
    add(f"| 其余写操作 | 直接生效 | {fmt(other)} |")
    add("")
    # 用项目自己的守卫判据复核一遍（同一口径，不另写一份）
    try:
        sys.path.insert(0, str(REPO / "photoos" / "tests"))
        from unit.route_guard import FORBIDDEN_ROUTE_WORDS, destructive_hits  # type: ignore
    except Exception:  # pragma: no cover
        FORBIDDEN_ROUTE_WORDS, destructive_hits = frozenset(), None  # type: ignore
    if destructive_hits is not None:
        hits = [(r["path"], sorted(destructive_hits(r["path"]))) for r in routes
                if destructive_hits(r["path"])]
        add(f"### 用项目自己的守卫判据复核（`tests/unit/route_guard.py`）")
        add("")
        add(f"禁忌词表：{'、'.join('`' + w + '`' for w in sorted(FORBIDDEN_ROUTE_WORDS))}")
        add("")
        if hits:
            add("命中禁忌词的路由：")
            add("")
            for path, words in hits:
                add(f"* `{path}` → {', '.join(words)}")
        else:
            add("【实测】**没有任何一条路由在路径里出现破坏性动作词**。")
            add("")
            add("⚠️ 这句必须读准 —— 它**不等于**“这个服务没有任何删除操作”。"
                "事实是：")
            add("")
            add("| 有删除操作的 | 没有的 |")
            add("| --- | --- |")
            add("| `DELETE /api/collections/{id}`（删**主题**，"
                "要 `?confirm=true`）、`DELETE /api/semantic/feedback`"
                "（撤销一条反馈） | **任何删/移/改名原片的入口** |")
            add("")
            add("判据是「路径段里有没有 delete/remove/purge 这类**动作词**」"
                "（`tests/unit/route_guard.py:31-43`），"
                "所以 `DELETE /api/collections/{id}`（词是 `collections`）合法通过，"
                "而 `/api/assets/{id}/delete` 会被拦下 —— 后者才是"
                "「动原片」的入口。产品级的说法是 PRD §11.1 / §14.7：")
            add("**Phase 1 不暴露删除/移动原文件的接口**"
                "（`tests/unit/test_api.py:600-609`）。")
        add("")
        add("> ⚠️ 【推断】注意判据是**按路径段切词**而不是「路径里包含这些字母」："
            "`confirm-all` 里恰好有 `rm` 两个字母，第一版守卫因此把"
            "`/api/people/{id}/confirm-all` 误判成破坏性路由"
            "（`tests/unit/route_guard.py:9-19` 记着这个现场）。")
    add("")
    add("> ⚠️ 【推断】把端点按路径词分档只是**给人看的提示**，不是权限边界 ——")
    add("> 真正的边界是「服务端没有删除原片的代码路径」（见 "
        "[`70-安全边界与硬规则.md`](70-安全边界与硬规则.md)）。")
    add("")
    add("---")
    add("")
    add("返回 [`00-文档规范与索引.md`](00-文档规范与索引.md) · "
        "相关：[`20-后端架构与模块地图.md`](20-后端架构与模块地图.md)、"
        "[`31-任务引擎与流水线.md`](31-任务引擎与流水线.md)")
    add("")

    OUT.write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    print(f"operations={ops} routes={len(routes)} gets={gets} groups={len(groups)}")
    if crosscheck:
        print(crosscheck)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
