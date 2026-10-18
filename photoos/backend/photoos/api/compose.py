"""多维筛选的组合层：把**内容 / 元数据 / 人物**三类条件叠在一次查询上。

## 为什么需要单独一个模块

改造前，这三个维度在代码里**从来没有相遇过**：

| 维度 | 原本住在哪 | 能不能叠加 |
|---|---|---|
| 元数据（机型 / 镜头 / 焦距 / 年份 / 路径…） | `queries.py::_build_filters()` | 能，但它**完全不认识"人"** |
| 内容（"像日落"这类） | `/api/semantic/results` | 独立端点，不接受任何元数据条件 |
| 人物 | `person_face → face.shot_id` | 独立，只在人物页 |

三者都落在 **`shot`** 上，所以"同时满足"本来是能表达的 —— 缺的只是这个 JOIN。
本模块就是那个 JOIN，其余地方一行不改。

## 三类条件怎么合

- **内容是"排序"，不是"筛选"。** 用户搜"日落"时想要的是"最像日落的排前面"，
  而不是"只要像日落就都留下"。所以内容命中全库都算分数，按分数降序 ——
  再叠加机型/人物时，是**在这个排序里挑出符合的**。
- **元数据与人物是"筛选"**，全部 AND（同时满足）。
- **没有内容词时不排相似度**：那种情况下相似度没有意义，
  按拍摄时间/文件名排（由调用方通过 `order` 指定）。

## 与既有实现的口径一致

"可见的 Shot" 沿用 `repos/__init__.py::SemanticRepo.hits()` 的写法：
`s.retired_at IS NULL`，代表照片取 `MIN(asset.id)`。
**代表照片的取法必须两边一样**，否则同一个 Shot 在两条路径下会指向不同文件。

## 为什么不在 SQL 里做向量点积

21,400 张 × 768 维的点积用 numpy 是几十毫秒，而把向量从 BLOB 解到 SQL 里做
只会更慢。所以分工是：**向量算分在 `library/semantic_rank.py`，
筛选与排序在 SQL**。本模块负责把两者接起来。
"""

from __future__ import annotations

from typing import Any

from ..db import Database

# 代表照片：与 `SemanticRepo.hits()` 完全相同的取法。改这里必须同时改那边。
_REP_ASSET = (
    "JOIN asset rep ON rep.id = ("
    "  SELECT a3.id FROM asset a3 WHERE a3.shot_id = s.id ORDER BY a3.id LIMIT 1)"
)

#: 三种排序。`similarity` 只在有内容词时可用（没有内容词时相似度无意义）。
ORDERS = {
    "similarity": "score_pairs.score DESC, s.captured_at DESC",
    "capture": "s.captured_at DESC",
    "name": "rep.relative_path ASC",
    "size": "rep.size DESC",
}


def metadata_clauses(
    *,
    asset_alias: str = "rep",
    library_id: int | None = None,
    file_type: str | None = None,
    status: str | None = None,
    camera: str | None = None,
    lens: str | None = None,
    focal_min: float | None = None,
    focal_max: float | None = None,
    year: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
    path_prefix: str | None = None,
    only_missing_metadata: bool = False,
) -> tuple[list[str], list[Any]]:
    """元数据条件 → SQL 片段与参数。

    `asset_alias` 是**代表照片/目标照片**在调用方查询里的别名：
    `compose.combine()` 用 `rep`（shot 的代表图），`queries.asset_page()` 用 `a`。
    参数化是为了让两边**共用同一份谓词**，而不是各写一遍再慢慢漂移。

    ⚠️ 语义必须与 `queries.py::_build_filters()` 保持一致 —— 那边的注释解释了
    为什么焦距要排除 0、为什么上界是开区间、为什么关键词只匹配结构化字段。
    这里不重复那些论证，但**结论照搬**。`_probe/bench_compose.py` 有一条
    对拍检查：同一组条件在 asset 层两边总数必须相等。
    """
    a = asset_alias
    clauses: list[str] = []
    params: list[Any] = []

    if library_id is not None:
        clauses.append(f"{a}.library_id = ?")
        params.append(int(library_id))
    if file_type:
        types = [t.strip() for t in str(file_type).split(",") if t.strip()]
        if types:
            clauses.append(f"{a}.file_type IN ({', '.join('?' for _ in types)})")
            params.extend(types)
    if status:
        states = [t.strip() for t in str(status).split(",") if t.strip()]
        if states:
            clauses.append(f"{a}.status IN ({', '.join('?' for _ in states)})")
            params.extend(states)
    if camera:
        clauses.append("COALESCE(m.camera_model, '') LIKE ?")
        params.append(f"%{camera}%")
    if lens:
        clauses.append("COALESCE(m.lens_model, '') LIKE ?")
        params.append(f"%{lens}%")

    # 焦距：`0` 与 NULL 都是"没有这个数据"，一旦按范围筛就必须排除，
    # 否则"≤35 广角"会把视频与缺 EXIF 的照片全捞进来。上界是**开区间**，
    # 这样界面上的四段快捷区间加起来正好等于总数。
    if focal_min is not None or focal_max is not None:
        clauses.append("m.focal_length IS NOT NULL AND m.focal_length > 0")
        if focal_min is not None:
            clauses.append("m.focal_length >= ?")
            params.append(float(focal_min))
        if focal_max is not None:
            clauses.append("m.focal_length < ?")
            params.append(float(focal_max))

    if year is not None:
        clauses.append("substr(m.capture_date_local, 1, 4) = ?")
        params.append(f"{int(year):04d}")
    if date_from:
        clauses.append("m.capture_date_local >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("m.capture_date_local <= ?")
        params.append(date_to)
    if only_missing_metadata:
        clauses.append("m.asset_id IS NULL")
    if keyword:
        needle = f"%{keyword}%"
        clauses.append(
            f"({a}.relative_path LIKE ? OR COALESCE(m.camera_model,'') LIKE ?"
            " OR COALESCE(m.lens_model,'') LIKE ?)"
        )
        params.extend([needle, needle, needle])
    if path_prefix:
        from ..library.paths import like_prefix_pattern

        clauses.append(f"{a}.relative_path LIKE ? ESCAPE '!'")
        params.append(like_prefix_pattern(f"{path_prefix}\\"))
    return clauses, params


def person_clause(
    person_ids: list[int], *, face_ref: str = "f.shot_id = s.id"
) -> tuple[str, list[Any]]:
    """**每一个**指定的人都必须出现在这张照片里（AND）。

    为什么是 AND 而不是 OR：用户勾"妈妈"和"我"时，想要的是"我俩的合影"。
    OR 会把单独一张妈妈的照片也捞进来 —— 那不是他的意思。

    ⚠️ 被标成「不是人」的脸**不参与**人物条件（`face_verdict` 里那行就是撤销）。
    这与人物页的口径一致：判定为"不是人"的脸不属于任何人。

    `face_ref` 是"这张脸属于当前这张照片"的判据：
      · shot 级（`compose.combine`）用 `f.shot_id = s.id`
      · asset 级（`queries.asset_page`）用 `f.asset_id = a.id`
    **脸本来就是挂在 asset 上的**，所以 asset 级更直接，不需要绕 shot。
    """
    if not person_ids:
        return "", []
    clause = " AND ".join(
        ["EXISTS (SELECT 1 FROM face f JOIN person_face pf ON pf.face_id = f.id"
         f" WHERE {face_ref} AND pf.person_id = ?"
         " AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id))"]
        * len(person_ids)
    )
    return clause, [int(p) for p in person_ids]


def combine(
    db: Database,
    *,
    ranked: list[dict[str, Any]] | None = None,
    shot_ids: list[int] | None = None,
    person_ids: list[int] | None = None,
    exclude_shot_ids: set[int] | None = None,
    order: str | None = None,
    limit: int = 300,
    offset: int = 0,
    **metadata: Any,
) -> dict[str, Any]:
    """把内容排序与元数据/人物筛选合成一次查询。

    - `ranked`：`semantic_rank.rerank()` 的输出（每项含 `shot_id` / `score`）。
    - `shot_ids`：等价的轻量形式（只关心集合、不关心分数时用）。
      两者都不给 = 纯元数据/人物筛选。
    - `exclude_shot_ids`：用户对内容词表过态"删掉"的那些（语义与
      `semantic_rank.rerank` 里的 `dropped` 一致：**直接排除**）。
    - `order`：`None`（默认）= **有内容词就按相似度，没有才按拍摄时间**。
      这是刻意的：相似度只在有内容词时才有意义，而用户搜了词就是想按"像不像"看。
      显式传 `capture` / `name` / `size` 可以压过它（他想按时间翻这批结果）。
    """
    score_rows = ranked if ranked is not None else (
        [{"shot_id": int(sid), "score": None} for sid in (shot_ids or [])] or None
    )
    has_content = score_rows is not None
    # 默认顺序取决于有没有内容词 —— 不能把 `order` 默认成 `capture`，
    # 否则"有内容词时按相似度"永远被下面那句覆盖掉（实测踩过：搜出来的结果按时间排了）。
    effective_order = order or ("similarity" if has_content else "capture")

    clauses: list[str] = ["s.retired_at IS NULL"]
    params: list[Any] = []

    cte = ""
    if has_content:
        # 用 CTE 传 id+score：避免 `IN (?,?,…)` 在 7 千个 id 时撞上
        # SQLite 的变量上限（老版本 999）。VALUES 行数没有这个限制。
        values = ",".join(
            "(?,?)" for _ in score_rows
        )
        cte = f"WITH score_pairs(shot_id, score) AS (VALUES {values}) "
        for row in score_rows:
            params.append(int(row["shot_id"]))
            params.append(row.get("score"))
        clauses.append("s.id IN (SELECT shot_id FROM score_pairs)")
    else:
        clauses.append("1=1")

    meta_clauses, meta_params = metadata_clauses(**metadata)
    clauses.extend(meta_clauses)
    params.extend(meta_params)

    person_where, person_params = person_clause(list(person_ids or []))
    if person_where:
        clauses.append(person_where)
        params.extend(person_params)

    if exclude_shot_ids:
        marks = ",".join("?" for _ in exclude_shot_ids)
        clauses.append(f"s.id NOT IN ({marks})")
        params.extend(sorted(int(x) for x in exclude_shot_ids))

    where = " AND ".join(clauses)
    order_sql = ORDERS.get(effective_order, ORDERS["capture"])

    join_scores = "JOIN score_pairs ON score_pairs.shot_id = s.id" if has_content else ""
    select_score = "score_pairs.score AS score" if has_content else "NULL AS score"

    base_from = (
        f"FROM shot s {join_scores} "
        f"JOIN selection sel ON sel.shot_id = s.id "
        f"{_REP_ASSET} "
        "LEFT JOIN metadata m ON m.asset_id = rep.id "
    )
    # `selection` 用 JOIN 会漏掉"未审阅"（没有行）—— 必须 LEFT JOIN。
    base_from = base_from.replace("JOIN selection sel ON sel.shot_id = s.id",
                                  "LEFT JOIN selection sel ON sel.shot_id = s.id")

    count_row = db.query_one(
        f"{cte}SELECT COUNT(*) AS n {base_from} WHERE {where}", tuple(params)
    )
    total = int(count_row["n"]) if count_row is not None else 0
    rows = db.query(
        f"""
        {cte}
        SELECT s.id AS shot_id, s.captured_at, s.scene_id,
               rep.id AS asset_id, rep.relative_path, rep.file_type, rep.size,
               COALESCE(sel.status, 'UNREVIEWED') AS selection_status,
               {select_score},
               (SELECT COUNT(*) FROM asset a2 WHERE a2.shot_id = s.id) AS asset_count,
               (SELECT GROUP_CONCAT(DISTINCT pf2.person_id)
                  FROM face f2 JOIN person_face pf2 ON pf2.face_id = f2.id
                 WHERE f2.shot_id = s.id
                   AND NOT EXISTS (SELECT 1 FROM face_verdict fv2 WHERE fv2.face_id = f2.id)
               ) AS person_ids
        {base_from}
        WHERE {where}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (int(limit), int(offset)),
    )

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("person_ids", None)
        item["person_ids"] = [int(x) for x in str(raw).split(",")] if raw else []
        items.append(item)

    return {
        "total": total,
        "offset": int(offset),
        "items": items,
        # 如实说明这次是怎么排的 —— 用户要能知道"为什么这张在前面"
        "ordered_by": effective_order,
        "has_content": has_content,
    }


# --------------------------------------------------------------------- asset 级
#
# 照片库列的是**文件**（一个拍摄组含 JPG+NEF 等多个文件），所以筛选也要按文件给结果。
# 上面那套是 shot 级（一个组一行），这里复用它的**条件与打分**，只换输出粒度。
#
# ⚠️ 分数是**组级**的（`build_pool` 一组一个向量），所以同组各文件分数相同。
# 这是诚实的写法：模型就是在组粒度算的，不编造每帧的独立分数。
# 界面靠"文件组"标记让人理解"为什么这几张一样"（见 app.js 的 `fileGroup`）。

#: asset 级的排序。与 `queries.SORT_SQL` 的取值对齐（capture/name/size/recent）。
#:
#: ⚠️ `similarity` 用的是**列别名 `score`**，不是 `score_pairs.score` ——
#: 那个 CTE 已经被"求交集再传小集合"的做法取代了（见 `combine_assets` 里的说明）。
#: 别名在 ORDER BY 里可用（SQLite 支持按结果列别名排序）。
ASSET_ORDERS = {
    "similarity": "score DESC, m.capture_at DESC, a.id DESC",
    "capture": "m.capture_at DESC, a.id DESC",
    "captured_desc": "m.capture_at DESC, a.id DESC",
    "name": "a.relative_path ASC",
    "size": "a.size DESC",
    "recent": "a.first_seen_at DESC, a.id DESC",
}


def _candidate_shot_ids(
    db: Database,
    *,
    library_id: int | None = None,
    file_type: str | None = None,
    status: str | None = None,
    camera: str | None = None,
    lens: str | None = None,
    focal_min: float | None = None,
    focal_max: float | None = None,
    year: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
    path_prefix: str | None = None,
    only_missing_metadata: bool = False,
    person_ids: list[int] | None = None,
    scope_kind: str | None = None,
    scope_key: str | None = None,
) -> list[int]:
    """满足元数据/人物/作用域条件的 **shot id 集合**（不涉及内容）。

    为什么要这一步：内容相似度是"全库排序"，而其它条件是"筛选"。
    两者要在 SQL 里合流，最自然的写法是把 7,371 个 shot 的分数做成 CTE 再 JOIN ——
    **但那个写法在带 `library_id` 时会退化成 1.6 亿次比较**（见 `combine_assets` 里的说明）。

    改成"先求候选集合、再与相似度求交集"之后，实测 9–20 ms，
    而且传进主查询的 `IN (...)` 只包含**真的可能有结果的**那些 shot。
    """
    clauses, params = metadata_clauses(
        asset_alias="a", library_id=library_id, file_type=file_type, status=status,
        camera=camera, lens=lens, focal_min=focal_min, focal_max=focal_max, year=year,
        date_from=date_from, date_to=date_to, keyword=keyword, path_prefix=path_prefix,
        only_missing_metadata=only_missing_metadata)

    if person_ids:
        clause, person_params = person_clause(list(person_ids), face_ref="f.asset_id = a.id")
        clauses.append(clause)
        params.extend(person_params)

    if scope_kind and scope_key:
        from .queries import _build_filters

        scope_where, scope_params = _build_filters(
            library_id=None, query=None, file_type=None, status=None, camera=None,
            lens=None, date_from=None, date_to=None, year=None,
            only_missing_metadata=False, scope_kind=scope_kind, scope_key=scope_key)
        stripped = scope_where.strip()
        if stripped.upper().startswith("WHERE "):
            stripped = stripped[6:]
        if stripped:
            clauses.append(stripped)
            params.extend(scope_params)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        "SELECT DISTINCT a.shot_id FROM asset a"
        " LEFT JOIN metadata m ON m.asset_id = a.id" + where,
        tuple(params),
    )
    return [int(r["shot_id"]) for r in rows if r["shot_id"] is not None]


def combine_assets(
    db: Database,
    *,
    ranked: list[dict[str, Any]] | None = None,
    person_ids: list[int] | None = None,
    exclude_shot_ids: set[int] | None = None,
    order: str | None = None,
    limit: int = 300,
    offset: int = 0,
    scope_kind: str | None = None,
    scope_key: str | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    """和 `combine()` 同一套条件，但**一行一个文件**。

    `scope_kind` / `scope_key` 是浏览树的作用域（选中某个文件夹/时间/精选状态/主题），
    与 `queries._build_filters` 的语义一致 —— 复用那边的实现，不重写一遍。
    """
    has_content = ranked is not None
    effective_order = order or ("similarity" if has_content else "capture")

    clauses: list[str] = []
    params: list[Any] = []

    join_scores = ""
    score_expr = "NULL AS score"
    score_map: dict[int, Any] = {}
    content_ids: list[int] | None = None

    meta_clauses, meta_params = metadata_clauses(asset_alias="a", **metadata)
    clauses.extend(meta_clauses)
    params.extend(meta_params)

    # 人物条件复用同一份实现（shot 级与 asset 级只差一个判据）
    if person_ids:
        clause, person_params = person_clause(list(person_ids), face_ref="f.asset_id = a.id")
        clauses.append(clause)
        params.extend(person_params)

    # 浏览树作用域：直接用 `queries._build_filters` 的语义，避免第二套写法。
    # 只把人物/元数据之外的那几种（path/time/selection/collection）拿过来。
    if scope_kind and scope_key:
        from .queries import _build_filters

        scope_where, scope_params = _build_filters(
            library_id=None, query=None, file_type=None, status=None, camera=None,
            lens=None, date_from=None, date_to=None, year=None,
            only_missing_metadata=False, scope_kind=scope_kind, scope_key=scope_key)
        # `_build_filters` 返回的是**带 WHERE 前缀**的整段；剥掉前缀再拼。
        stripped = scope_where.strip()
        if stripped.upper().startswith("WHERE "):
            stripped = stripped[6:]
        if stripped:
            clauses.append(stripped)
            params.extend(scope_params)

    if has_content:
        # ⚠️ 这里**刻意不用 CTE `VALUES` 去 JOIN**。
        #
        # 原来写成 `WITH score_pairs(...) AS (VALUES (?,?),...) JOIN asset a ON a.shot_id = ...`，
        # 一旦同时给了 `library_id`，SQLite 会选一个**灾难性的连接顺序**：
        #     SEARCH a USING INDEX idx_asset_library   ← 先扫全库
        #     SCAN score_pairs                          ← 每行再扫 7,371 行 CTE
        # 实测 7,371 个 shot 时 **4.6–10.4 秒**（不带 library_id 只要 0.3 秒 ——
        # 计划碰巧对了，所以这个坑很隐蔽）。
        #
        # 改成：**先在 SQL 里求"候选 shot 集合"（9–20 ms），在 Python 里与相似度求交集，
        # 只把交集传进查询**。实测 `IN(7311)` 计数 17 ms、取 300 行 24 ms。
        # 分数用 CASE 查表，不再需要那个 CTE 被扫两遍。
        score_map = {int(r["shot_id"]): r.get("score") for r in ranked}
        content_ids = _candidate_shot_ids(
            db, library_id=metadata.get("library_id"),
            file_type=metadata.get("file_type"), status=metadata.get("status"),
            camera=metadata.get("camera"), lens=metadata.get("lens"),
            focal_min=metadata.get("focal_min"), focal_max=metadata.get("focal_max"),
            year=metadata.get("year"), date_from=metadata.get("date_from"),
            date_to=metadata.get("date_to"), keyword=metadata.get("keyword"),
            path_prefix=metadata.get("path_prefix"),
            only_missing_metadata=bool(metadata.get("only_missing_metadata")),
            person_ids=list(person_ids or []),
            scope_kind=scope_kind, scope_key=scope_key,
        )
        content_ids = [sid for sid in content_ids if sid in score_map]
        if not content_ids:
            # 内容命中与其它条件没有交集 —— 直接返回空，不必再查库
            return {"total": 0, "offset": int(offset), "limit": int(limit), "items": [],
                    "ordered_by": effective_order, "has_content": True}
        marks = ",".join("?" for _ in content_ids)
        clauses.append(f"a.shot_id IN ({marks})")
        params.extend(content_ids)
        # ⚠️ 分数**不走 `CASE a.shot_id WHEN ... END`**：实测 7,300 个 WHEN 要 530 ms，
        # 而不取分数只要 15 ms —— 那是这一步最大的单项开销。
        # 改成**临时表 + 索引**（下面 `db.read()` 里建），连接查询。
        score_expr = "sp.score AS score"

    if exclude_shot_ids:
        marks = ",".join("?" for _ in exclude_shot_ids)
        clauses.append(f"(a.shot_id IS NULL OR a.shot_id NOT IN ({marks}))")
        params.extend(sorted(int(x) for x in exclude_shot_ids))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = ASSET_ORDERS.get(effective_order, ASSET_ORDERS["capture"])

    base_from = f"""
    FROM asset AS a
    {join_scores}
    LEFT JOIN metadata  AS m ON m.asset_id = a.id
    LEFT JOIN preview   AS t ON t.asset_id = a.id AND t.kind = 'thumb'
    LEFT JOIN preview   AS p ON p.asset_id = a.id AND p.kind = 'preview'
    LEFT JOIN selection AS s ON s.shot_id = a.shot_id
    """

    rows_sql = f"""
        SELECT a.id, a.library_id, a.relative_path, a.file_type, a.status, a.size, a.mime,
               a.content_hash, a.shot_id, a.status_reason,
               m.capture_at, m.capture_date_local, m.camera_make, m.camera_model,
               m.lens_model, m.focal_length, m.aperture, m.shutter_speed, m.iso,
               m.width, m.height, m.gps_lat, m.gps_lon, m.tz_offset, m.orientation,
               t.width AS thumb_width, t.height AS thumb_height,
               CASE WHEN t.id IS NULL THEN 0 ELSE 1 END AS has_thumb,
               CASE WHEN p.id IS NULL THEN 0 ELSE 1 END AS has_preview,
               COALESCE(s.status, 'UNREVIEWED') AS selection_status,
               s.reason AS selection_reason,
               {score_expr},
               (SELECT GROUP_CONCAT(DISTINCT pf.person_id)
                  FROM face f JOIN person_face pf ON pf.face_id = f.id
                 WHERE f.asset_id = a.id
                   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id)
               ) AS person_ids
        {base_from}
        {where}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """

    if has_content:
        # 临时表必须与查询**共用同一个连接** —— `db.read()` 是"用完关掉"的语义，
        # 分成两次 `db.query()` 调用的话临时表已经不存在了。
        with db.read() as conn:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _score_pairs "
                "(shot_id INTEGER PRIMARY KEY, score REAL)")
            conn.execute("DELETE FROM _score_pairs")
            conn.executemany(
                "INSERT INTO _score_pairs(shot_id, score) VALUES (?, ?)",
                [(int(sid), float(score_map[sid] or 0.0)) for sid in content_ids])
            # 临时表上的主键已经够了；不额外建索引（建索引反而更慢，实测 7 千行不值得）
            count_row = conn.execute(
                f"SELECT COUNT(*) AS n {base_from} {where}", tuple(params)).fetchone()
            total = int(count_row["n"]) if count_row is not None else 0
            joined_from = base_from.replace(
                "FROM asset AS a",
                "FROM asset AS a JOIN _score_pairs AS sp ON sp.shot_id = a.shot_id")
            joined_sql = rows_sql.replace(base_from, joined_from)
            rows = [dict(r) for r in conn.execute(
                joined_sql, tuple(params) + (int(limit), int(offset)))]
    else:
        count_row = db.query_one(
            f"SELECT COUNT(*) AS n {base_from} {where}", tuple(params))
        total = int(count_row["n"]) if count_row is not None else 0
        rows = [dict(r) for r in db.query(
            rows_sql, tuple(params) + (int(limit), int(offset)))]

    # 逐行装配复用 `queries._asset_row`：列名完全一致，不重写第二份。
    from .queries import _asset_row

    items = []
    for row in rows:
        item = _asset_row(row)
        # 相似度只在有内容词时存在（无内容词时是 SQL 里的 `NULL AS score`）。
        # 它是**可核对的原始数**，不是给照片打的分 —— 界面按硬规则只显示小数，
        # 不做星级/"匹配度"那类表达（视觉纪律见 phase2.css 顶部）。
        if has_content:
            item["score"] = row.get("score")
        items.append(item)

    return {
        "total": total,
        "offset": int(offset),
        "limit": int(limit),
        "items": items,
        "ordered_by": effective_order,
        "has_content": has_content,
    }


__all__ = ["ASSET_ORDERS", "ORDERS", "combine", "combine_assets",
           "metadata_clauses", "person_clause"]
