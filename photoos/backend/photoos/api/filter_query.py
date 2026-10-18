"""结构化筛选查询（Phase 2 · 方案 §11.2 / PRD §9.2.1）。

## 这是什么

用户/界面（以及将来的自然语言筛选）需要的"SQL 式筛选"，但**绝不接受原始 SQL 字符串** ——
而是接受一个**结构化的 filter 对象**，由服务端翻译成参数化查询。

```
POST /api/assets/query
{
  "filters": {
    "capture_date_local": {"gte": "2025-10-12", "lte": "2026-10-12"},
    "city":               {"in": ["杭州市", "希腊市"]},
    "camera_model":       {"eq": "NIKON Z 5"},
    "focal_length":       {"gte": 200},
    "file_type":          {"in": ["jpeg"]},
    "selection_status":   {"in": ["SELECTED", "FINAL"]},
    "collection_id":      {"eq": 3},
    "has_gps":            {"eq": true}
  },
  "sort": "capture_desc", "offset": 0, "limit": 300
}
```

## 安全边界（铁律 #2 / #5 的技术落点，逐条写死）

| 规则 | 实现 |
| --- | --- |
| **绝不接受原始 SQL** | 只接受结构化对象；`WHERE` 由服务端拼，值**全部**走参数绑定 |
| **字段白名单** | 只有 `FIELDS` 里列出的键合法；未知键 → **400**（不静默忽略） |
| **操作符白名单** | 只有 `OPERATORS` 里列出的合法；未知 → 400 |
| **`like` 不接受用户通配符** | 用户给的值一律当**字面量**，服务端自己拼 `%值%`（内部还会转义 `%`/`_`） |
| **绝不扫原片** | 生成的 SQL 只碰 `asset` / `metadata` / `selection` / `collection_item` 等结构化表 |
| **强制分页** | `limit` 上限 1000，缺省 300 |

## 为什么字段白名单要"报错"而不是"忽略"

静默忽略未知字段会让用户以为筛选生效了 —— 那和"浏览轴静默返回全库"是同一类错误
（Phase 1 已经明确拒绝过那种做法）。宁可 400 并告诉他哪个键不认识。
"""

from __future__ import annotations

from typing import Any

from ..db import Database

#: 字段 → 允许的操作符。**这张表就是白名单本身**，不在其中的一律拒绝。
FIELDS: dict[str, tuple[str, ...]] = {
    # 时间（字符串比较，YYYY-MM-DD 前缀可比）
    "capture_date_local": ("eq", "ne", "gte", "lte", "gt", "lt", "between"),
    "capture_at": ("eq", "gte", "lte", "is_null"),
    "year": ("eq", "in"),
    # 位置与来源
    "city": ("eq", "ne", "in", "like"),
    "period": ("eq", "ne", "in", "like"),
    "relative_path": ("like", "eq"),
    "library_id": ("eq", "in"),
    # 设备
    "camera_model": ("eq", "ne", "in", "like"),
    "lens_model": ("eq", "ne", "in", "like"),
    "focal_length": ("eq", "gte", "lte", "between"),
    "aperture": ("eq", "gte", "lte"),
    "iso": ("eq", "gte", "lte", "between"),
    # 文件
    "file_type": ("eq", "ne", "in"),
    "status": ("eq", "ne", "in"),
    # 布尔
    "has_gps": ("eq",),
    "is_video": ("eq",),
    # 精选
    "selection_status": ("eq", "ne", "in"),
    "has_reason": ("eq",),
    # 主题
    "collection_id": ("eq", "in"),
}

#: 允许的操作符全集（用于报错信息）
OPERATORS = ("eq", "ne", "in", "gte", "lte", "gt", "lt", "between", "like", "is_null")

#: 分页上限（PRD §11.2：不一次性加载全部）
MAX_LIMIT = 1000
DEFAULT_LIMIT = 300

#: 排序白名单
SORTS: dict[str, str] = {
    "capture_desc": "COALESCE(m.capture_at,'') DESC, a.relative_path",
    "capture_asc": "COALESCE(m.capture_at,'') ASC, a.relative_path",
    "name": "a.relative_path ASC",
    "size_desc": "a.size DESC",
    # ⚠️ 口径统一（2026-10-17）：`recent` = **首次入库时间**（`a.first_seen_at`）。
    # 曾经这里是 `m.extracted_at`，而 `compose.ASSET_ORDERS` 是 `a.first_seen_at` ——
    # 同一个词在两个入口含义不同，导致 `/api/filter?order=recent` 静默按拍摄时间排。
    # 为什么选 first_seen：重新提取元数据会刷新 extracted_at，那会让旧照片
    # 突然排到"最近"前面去；入库时间不会，且 `asset` 表里它 NOT NULL（实测 0 个空）。
    "recent": "a.first_seen_at DESC, a.id DESC",
}
DEFAULT_SORT = "capture_desc"


class QueryError(ValueError):
    """筛选条件不合法（字段/操作符/值）。消息面向用户，可直接展示。"""


def _escape_like(value: str) -> str:
    """转义 LIKE 的通配符 —— **用户给的值一律当字面量**。

    用 `!` 作 ESCAPE 字符（与 `library.paths.like_prefix_pattern` 同一约定）。
    """
    return str(value).replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _require_list(value: Any, field: str, op: str) -> list[Any]:
    if not isinstance(value, (list, tuple)) or not value:
        raise QueryError(f"字段 {field} 的 {op} 需要一个非空数组")
    return list(value)


def build_where(filters: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
    """把结构化 `filters` 翻译成 `(WHERE 子句列表, 参数列表)`。

    **所有值都进参数列表**，不做任何字符串插值（列名与操作符来自白名单常量）。
    """
    if filters is None:
        return [], []
    if not isinstance(filters, dict):
        raise QueryError("filters 必须是一个对象")

    clauses: list[str] = []
    params: list[Any] = []

    for field, condition in filters.items():
        if field not in FIELDS:
            raise QueryError(
                f"不支持的筛选字段：{field}（可用字段：{'、'.join(sorted(FIELDS))}）"
            )
        if condition is None:
            continue
        if not isinstance(condition, dict):
            # 允许简写 `"city": "杭州市"` 等价于 `{"eq": "杭州市"}`
            condition = {"eq": condition}
        allowed = FIELDS[field]
        for op, value in condition.items():
            if op not in OPERATORS:
                raise QueryError(f"不支持的操作符：{op}（可用：{'、'.join(OPERATORS)}）")
            if op not in allowed:
                raise QueryError(f"字段 {field} 不支持操作符 {op}（可用：{'、'.join(allowed)}）")
            clause, extra = _clause_for(field, op, value)
            clauses.append(clause)
            params.extend(extra)

    return clauses, params


def _as_int(value: Any, field: str) -> int:
    """把值转成整数；转不了就抛 `QueryError`（→ HTTP 400）。

    ⚠️ 为什么不直接 `int(value)`：`int("abc")` 抛的是 `ValueError`，
    而路由层只把 `QueryError` 翻译成 400，其它异常会变成 **500（服务器内部错误）** ——
    用户明明是参数填错，却看到"服务端错了"。这个函数把两类错误归一。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise QueryError(f"字段 {field} 需要整数，收到 {value!r}") from None


def _collection_clause(op: str, value: Any) -> tuple[str, list[Any]]:
    """主题筛选走 EXISTS —— 与 `scope_kind=collection` 同一语义。"""
    if op == "eq":
        return (
            "a.shot_id IN (SELECT ci.shot_id FROM collection_item ci WHERE ci.collection_id = ?)",
            [_as_int(value, "collection_id")],
        )
    if op == "in":
        items = _require_list(value, "collection_id", op)
        placeholders = ", ".join("?" for _ in items)
        return (
            "a.shot_id IN (SELECT ci.shot_id FROM collection_item ci"
            f" WHERE ci.collection_id IN ({placeholders}))",
            [_as_int(i, "collection_id") for i in items],
        )
    raise QueryError(f"collection_id 不支持操作符 {op}")


#: 字段 → SQL 列。`a` = asset，`m` = metadata，`s` = selection。
#: `city` / `period` 从 `relative_path` 派生（用 substr+instr，不需要额外列）。
_COLUMN: dict[str, str] = {
    "capture_date_local": "m.capture_date_local",
    "capture_at": "m.capture_at",
    "year": "substr(m.capture_date_local, 1, 4)",
    "city": "substr(a.relative_path, 1, instr(a.relative_path, CHAR(92)) - 1)",
    "period": "substr(a.relative_path, instr(a.relative_path, CHAR(92)) + 1,"
              " instr(substr(a.relative_path, instr(a.relative_path, CHAR(92)) + 1),"
              " CHAR(92)) - 1)",
    "relative_path": "a.relative_path",
    "library_id": "a.library_id",
    "camera_model": "m.camera_model",
    "lens_model": "m.lens_model",
    "focal_length": "m.focal_length",
    "aperture": "m.aperture",
    "iso": "m.iso",
    "file_type": "a.file_type",
    "status": "a.status",
    "has_gps": "m.gps_lat",
    "is_video": "CASE WHEN a.file_type = 'video' THEN 1 ELSE NULL END",
    "selection_status": "COALESCE(s.status, 'UNREVIEWED')",
    "has_reason": "s.reason",
    "collection_id": "",          # 走 `_collection_clause`，不直接映射列
}


def _coerce_year(value: Any) -> str:
    """把年份统一成 **4 位字符串**（与 `queries.py:134`、`compose.py:125` 同一口径）。

    ⚠️ 为什么必须强转：`_COLUMN["year"]` 是 `substr(capture_date_local,1,4)`，
    拿到的是**文本**。而 SQLite 的比较规则里"整数恒小于文本"，
    所以 `'2025' = 2025` 恒为**假** —— 用户传 `{"year": {"eq": 2025}}`（整数）
    会**静默返回 0 行**，不报错。实测该缺陷确实存在（见 20-reference Q-29）。
    """
    try:
        return f"{int(value):04d}"
    except (TypeError, ValueError):
        raise QueryError(f"year 需要四位年份（例如 2025），收到 {value!r}") from None


def _clause_for(field: str, op: str, value: Any) -> tuple[str, list[Any]]:
    """单个条件的 SQL 片段 + 参数。**只做白名单到列名的映射**，不拼接用户输入。"""
    if field == "collection_id":
        return _collection_clause(op, value)

    column = _COLUMN[field]

    if field == "year":
        # 白名单里 year 只允许 eq / in（见 FIELDS），标量与数组分别处理
        if op == "in":
            value = [_coerce_year(v) for v in _require_list(value, field, op)]
        else:
            value = _coerce_year(value)

    if op == "is_null":
        if not isinstance(value, bool):
            raise QueryError(f"字段 {field} 的 is_null 需要 true/false")
        return (f"{column} IS NULL" if value else f"{column} IS NOT NULL"), []

    if op == "eq":
        if field in ("has_gps", "is_video", "has_reason") and not isinstance(value, bool):
            raise QueryError(f"字段 {field} 需要 true/false")
        if isinstance(value, bool):
            return (f"{column} IS NOT NULL" if value else f"{column} IS NULL"), []
        return f"{column} = ?", [value]

    if op == "ne":
        if isinstance(value, bool):
            return (f"{column} IS NULL" if value else f"{column} IS NOT NULL"), []
        return f"({column} IS NULL OR {column} <> ?)", [value]

    if op == "in":
        items = _require_list(value, field, op)
        placeholders = ", ".join("?" for _ in items)
        return f"{column} IN ({placeholders})", items

    if op in ("gte", "lte", "gt", "lt"):
        symbol = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}[op]
        return f"{column} {symbol} ?", [value]

    if op == "between":
        items = _require_list(value, field, op)
        if len(items) != 2:
            raise QueryError(f"字段 {field} 的 between 需要恰好两个值")
        return f"{column} BETWEEN ? AND ?", [items[0], items[1]]

    if op == "like":
        # ★ 用户的值当**字面量**：服务端加两侧通配，用户写的 % 会被转义掉
        return f"{column} LIKE ? ESCAPE '!'", [f"%{_escape_like(value)}%"]

    raise QueryError(f"未实现的操作符：{op}")     # pragma: no cover - 白名单已挡住


def normalise_request(payload: dict[str, Any] | None) -> dict[str, Any]:
    """校验并规范化请求体，返回可直接使用的参数。"""
    payload = payload or {}
    if not isinstance(payload, dict):
        raise QueryError("请求体必须是一个对象")

    known = {"filters", "sort", "offset", "limit"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise QueryError(f"不支持的请求字段：{'、'.join(unknown)}（可用：{'、'.join(sorted(known))}）")

    sort = payload.get("sort") or DEFAULT_SORT
    if sort not in SORTS:
        raise QueryError(f"不支持的排序：{sort}（可用：{'、'.join(sorted(SORTS))}）")

    try:
        offset = max(0, int(payload.get("offset") or 0))
    except (TypeError, ValueError) as exc:
        raise QueryError("offset 必须是整数") from exc

    try:
        limit = int(payload.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise QueryError("limit 必须是整数") from exc
    limit = max(1, min(limit, MAX_LIMIT))

    filters = payload.get("filters")
    return {"filters": filters, "sort": sort, "offset": offset, "limit": limit}


def describe(filters: dict[str, Any] | None) -> str:
    """把筛选条件渲染成一句人话（界面回显用：让用户确认"我到底筛了什么"）。

    **自然语言筛选（Phase 3）也会用到它** —— 小模型把自然语言翻成 filter 对象后，
    用本函数把对象再翻回人话给用户核对，形成"自然语言 ⇄ 结构化"的闭环。
    """
    if not filters:
        return "全部照片"
    parts: list[str] = []
    for field, condition in filters.items():
        if not isinstance(condition, dict):
            condition = {"eq": condition}
        for op, value in condition.items():
            label = _FIELD_LABELS.get(field, field)
            parts.append(f"{label} {_OP_LABELS.get(op, op)} {value}")
    return "；".join(parts)


_FIELD_LABELS = {
    "capture_date_local": "拍摄日期",
    "capture_at": "拍摄时间",
    "year": "年份",
    "city": "城市",
    "period": "时段",
    "relative_path": "路径",
    "library_id": "照片库",
    "camera_model": "机型",
    "lens_model": "镜头",
    "focal_length": "焦距",
    "aperture": "光圈",
    "iso": "ISO",
    "file_type": "类型",
    "status": "状态",
    "has_gps": "有 GPS",
    "is_video": "是视频",
    "selection_status": "精选状态",
    "has_reason": "有精选理由",
    "collection_id": "主题",
}
_OP_LABELS = {
    "eq": "=", "ne": "≠", "in": "属于", "gte": "≥", "lte": "≤",
    "gt": ">", "lt": "<", "between": "在区间", "like": "包含", "is_null": "为空",
}


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_SORT",
    "FIELDS",
    "MAX_LIMIT",
    "OPERATORS",
    "QueryError",
    "SORTS",
    "build_where",
    "describe",
    "normalise_request",
]
