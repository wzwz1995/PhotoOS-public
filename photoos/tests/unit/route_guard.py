"""三条"不许有破坏性端点"守卫共用的判据。

## 为什么要有这个模块

同一个判断（"这条路由是不是一个删东西的入口"）本来写在三个测试文件里，
三份实现迟早漂移 —— 本项目已经因为"同一个口径写两处"栽过几次。
放在这里，改一次三处一起生效。

## 判据为什么不能是"路径里包含这些字母"

第一版三处都是 `word in path.lower()`，于是 Phase 4 交付后新增的
`POST /api/people/{person_id}/confirm-all` 被三条断言同时判成破坏性路由 ——
`confirm` 里恰好有 `rm` 两个字母。假阳性比漏报更危险：修它的人会顺手把
关键词表删掉几个，守卫就这么没了。

正确的判据是**路径段拆出来的词**：
  * `confirm-all` → {confirm, all}      → 通过
  * `assets/{id}/delete` → {assets, delete} → 命中
所以这里按 `/` 切段、再按 `-`/`_` 切词，逐个比对。
"""
from __future__ import annotations

import re

#: 出现这些词（作为**独立词**）就认为这条路由在删东西。
FORBIDDEN_ROUTE_WORDS = frozenset({
    "delete", "remove", "purge", "erase", "rm", "unlink", "trash", "destroy",
})


def destructive_tokens(path: str) -> set[str]:
    """把路由路径拆成动作词（路径参数 `{...}` 跳过）。"""
    tokens: set[str] = set()
    for segment in str(path or "").split("/"):
        if not segment or segment.startswith("{"):
            continue
        tokens.update(t for t in re.split(r"[-_]", segment.lower()) if t)
    return tokens


def destructive_hits(path: str) -> set[str]:
    """这条路由命中了哪些禁忌词（空集 = 通过）。"""
    return destructive_tokens(path) & FORBIDDEN_ROUTE_WORDS
