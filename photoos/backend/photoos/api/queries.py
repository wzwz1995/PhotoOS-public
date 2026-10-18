"""只读查询层（供 API 使用）。

纪律（PRD §11.2）：
  * 搜索与筛选**只访问数据库索引、Metadata、Selection、Collection 等结构化数据**，
    **绝不扫描原始照片文件**。
  * 所有分页使用 `LIMIT/OFFSET` + 覆盖索引；10 万级规模下仍由索引命中。
  * 任何用户输入都走参数绑定，不做字符串拼接（避免注入与引号问题）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import Database

#: 排序白名单（绝不把用户输入直接拼进 SQL）
SORT_SQL: dict[str, str] = {
    # 默认：拍摄时间倒序（没有拍摄时间的排最后）
    "capture": "COALESCE(m.capture_at, '') DESC, a.relative_path",
    "captured_desc": "COALESCE(m.capture_at, '') DESC, a.relative_path",
    "name": "a.relative_path ASC",
    "size": "a.size DESC",
    "recent": "a.first_seen_at DESC, a.id DESC",
}

#: 浏览列表需要的列（含缩略图尺寸，便于前端预留占位，避免布局抖动）
_ASSET_COLUMNS = """
    a.id, a.library_id, a.relative_path, a.file_type, a.status, a.size, a.mime,
    a.content_hash, a.shot_id, a.status_reason,
    m.capture_at, m.capture_date_local, m.camera_make, m.camera_model,
    m.lens_model, m.focal_length, m.aperture, m.shutter_speed, m.iso,
    m.width, m.height, m.gps_lat, m.gps_lon, m.tz_offset, m.orientation,
    t.width AS thumb_width, t.height AS thumb_height,
    CASE WHEN t.id IS NULL THEN 0 ELSE 1 END AS has_thumb,
    CASE WHEN p.id IS NULL THEN 0 ELSE 1 END AS has_preview,
    COALESCE(s.status, 'UNREVIEWED') AS selection_status,
    s.reason AS selection_reason,
    (SELECT GROUP_CONCAT(DISTINCT pf.person_id)
       FROM face f JOIN person_face pf ON pf.face_id = f.id
      WHERE f.asset_id = a.id
        AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id)
    ) AS person_ids
"""

#: 精选状态来自**该 asset 所属 Shot** 的 `selection` 行。
#: Phase 1 这里是一个常量 `'UNREVIEWED'`（并注明"Phase 2 换成 JOIN"）—— 现在换成真 JOIN。
#: 用 LEFT JOIN 而不是 INNER：未审阅的 Shot 在 `selection` 里没有行，用 INNER 会把它们整批丢掉。
#
#: `person_ids` 是"这张照片里有谁"（逗号分隔的 id，没有则 NULL）。
#: 它是给**界面显示标签**用的（弹窗上要能点人名），不是筛选条件 ——
#: 筛选走 `_build_filters` 的 EXISTS（见 `compose.person_clause`）。
#: 代价：`face` 有 `idx_face_asset`，实测按 asset 查是 0.005 ms。
_ASSET_JOINS = """
    FROM asset AS a
    LEFT JOIN metadata AS m ON m.asset_id = a.id
    LEFT JOIN preview  AS t ON t.asset_id = a.id AND t.kind = 'thumb'
    LEFT JOIN preview  AS p ON p.asset_id = a.id AND p.kind = 'preview'
    LEFT JOIN selection AS s ON s.shot_id = a.shot_id
"""


def _build_filters(
    *,
    library_id: int | None,
    query: str | None,
    file_type: str | None,
    status: str | None,
    camera: str | None,
    lens: str | None,
    date_from: str | None,
    date_to: str | None,
    year: int | None,
    only_missing_metadata: bool,
    # 焦距范围。给默认值而不做成必填：本函数有多个调用方（列表分页、浏览树计数），
    # 它们大多不关心焦距 —— 做成必填会逼每个调用点都写一遍 `focal_min=None`，
    # 而且漏一个就是 TypeError（实测：加完必填参数后 `test_browse.py` 挂了 6 个用例）。
    focal_min: float | None = None,
    focal_max: float | None = None,
    scope_kind: str | None = None,
    scope_key: str | None = None,
    person_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if library_id is not None:
        clauses.append("a.library_id = ?")
        params.append(int(library_id))
    if file_type:
        # 允许逗号分隔的多选
        types = [item.strip() for item in file_type.split(",") if item.strip()]
        if types:
            clauses.append(f"a.file_type IN ({', '.join('?' for _ in types)})")
            params.extend(types)
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        if statuses:
            clauses.append(f"a.status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
    if camera:
        clauses.append("COALESCE(m.camera_model, '') LIKE ?")
        params.append(f"%{camera}%")
    if lens:
        clauses.append("COALESCE(m.lens_model, '') LIKE ?")
        params.append(f"%{lens}%")
    # 焦距范围。
    #
    # ⚠️ `focal_length = 0` 是"**没有这个数据**"，不是"0 毫米的超广角"——
    # 实测本库里有 2,262 张（视频与缺失 EXIF 的照片）。所以一旦按范围筛选，
    # 必须显式排除 0 与 NULL，否则"≤35 广角"会把它们全捞进来。
    # 只在**确实传了范围**时才加这个条件：不筛焦距时不该因此改变结果集。
    #
    # ⚠️ 上界是**开区间**（`<` 而不是 `<=`）。这不是随意的：界面上的快捷区间
    # 是"连续四段"（广角 / 标准 / 中长 / 长焦），要让四段的张数**加起来等于总数**，
    # 边界值就只能属于一段。半开区间是唯一能做到这点的写法。
    if focal_min is not None or focal_max is not None:
        clauses.append("m.focal_length IS NOT NULL AND m.focal_length > 0")
        if focal_min is not None:
            clauses.append("m.focal_length >= ?")
            params.append(float(focal_min))
        if focal_max is not None:
            clauses.append("m.focal_length < ?")
            params.append(float(focal_max))
    if date_from:
        clauses.append("m.capture_date_local >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("m.capture_date_local <= ?")
        params.append(date_to)
    if year is not None:
        clauses.append("substr(m.capture_date_local, 1, 4) = ?")
        params.append(f"{int(year):04d}")
    if only_missing_metadata:
        clauses.append("m.asset_id IS NULL")
    if query:
        # 关键词只用于匹配结构化字段（文件名/机型/镜头），不触发任何文件访问
        needle = f"%{query}%"
        clauses.append(
            "(a.relative_path LIKE ? OR COALESCE(m.camera_model,'') LIKE ?"
            " OR COALESCE(m.lens_model,'') LIKE ?)"
        )
        params.extend([needle, needle, needle])
    if scope_kind and scope_key:
        from ..library.paths import like_prefix_pattern

        # 作用域：把"浏览树里选中的节点"翻译成查询条件。
        # `path` 与 `time` 是**前缀匹配**；`selection` 与 `collection` 是 JOIN。
        # **参数形状完全相同**，因此前端的树组件、面包屑、网格都不用改写 ——
        # 这正是 Phase 1 把 `scope_kind` + `scope_key` 冻结下来的目的。
        if scope_kind == "path":
            clauses.append("a.relative_path LIKE ? ESCAPE '!'")
            params.append(like_prefix_pattern(f"{scope_key}\\"))
        elif scope_kind == "time":
            clauses.append("COALESCE(m.capture_date_local, '') LIKE ? ESCAPE '!'")
            params.append(like_prefix_pattern(scope_key))
        elif scope_kind == "selection":
            from ..library import selection as sel

            # 四态之一；未审阅 = `selection` 里没有行（Phase 1 建表时的语义）。
            # 非法值必须**明确报错**而不是静默返回全库 —— 否则用户以为筛选生效了。
            status = sel.normalize_status(scope_key)
            if status == sel.UNREVIEWED:
                clauses.append("s.shot_id IS NULL")
            else:
                clauses.append("s.status = ?")
                params.append(status)
        elif scope_kind == "collection":
            clauses.append(
                "a.shot_id IN (SELECT ci.shot_id FROM collection_item ci WHERE ci.collection_id = ?)"
            )
            params.append(int(scope_key))
        else:
            raise ValueError(f"未实现的作用域类型：{scope_kind}")

    # 人物：**每一个**指定的人都必须出现在这张照片里（AND，所以是合影）。
    # 谓词复用 `compose.person_clause`，只是把判据从 shot 级换成 asset 级 ——
    # 脸本来就挂在 asset 上（`face.asset_id`），这里更直接、不用绕 shot。
    # 两边共用一份实现是刻意的：同一件事写两遍必然漂移（这个项目已经付过学费）。
    if person_ids:
        from .compose import person_clause

        clause, person_params = person_clause(list(person_ids), face_ref="f.asset_id = a.id")
        clauses.append(clause)
        params.extend(person_params)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def asset_page(
    db: Database,
    *,
    library_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    query: str | None = None,
    file_type: str | None = None,
    status: str | None = None,
    camera: str | None = None,
    lens: str | None = None,
    focal_min: float | None = None,
    focal_max: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    year: int | None = None,
    only_missing_metadata: bool = False,
    sort: str = "capture",
    scope_kind: str | None = None,
    scope_key: str | None = None,
    person_ids: list[int] | None = None,
) -> dict[str, Any]:
    """分页查询资产列表。返回 `{total, offset, limit, items}`。"""
    where, params = _build_filters(
        library_id=library_id,
        query=query,
        file_type=file_type,
        status=status,
        camera=camera,
        lens=lens,
        focal_min=focal_min,
        focal_max=focal_max,
        date_from=date_from,
        date_to=date_to,
        year=year,
        only_missing_metadata=only_missing_metadata,
        scope_kind=scope_kind,
        scope_key=scope_key,
        person_ids=person_ids,
    )
    order = SORT_SQL.get(sort, SORT_SQL["capture"])
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))

    total = int(
        db.scalar(f"SELECT COUNT(*) {_ASSET_JOINS} {where}", params, default=0)
    )
    rows = db.query(
        f"SELECT {_ASSET_COLUMNS} {_ASSET_JOINS} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_asset_row(row) for row in rows],
    }


def asset_detail(db: Database, asset_id: int) -> dict[str, Any] | None:
    """单个资产的完整信息（含全部元数据、预览、实例与所属 Library）。"""
    row = db.query_one(
        f"SELECT {_ASSET_COLUMNS} {_ASSET_JOINS} WHERE a.id = ?", (int(asset_id),)
    )
    if row is None:
        return None
    payload = _asset_row(row)

    meta = db.query_one("SELECT * FROM metadata WHERE asset_id = ?", (int(asset_id),))
    payload["metadata"] = dict(meta) if meta is not None else None

    previews = db.query(
        "SELECT kind, path, width, height, bytes, generator_version, created_at"
        " FROM preview WHERE asset_id = ? ORDER BY kind",
        (int(asset_id),),
    )
    payload["previews"] = [dict(item) for item in previews]

    instances = db.query(
        "SELECT ai.relative_path, ai.size, ai.mtime_ns, ai.in_library, v.serial, v.label,"
        " v.mount_hint, v.status AS volume_status"
        " FROM asset_instance AS ai JOIN volume AS v ON v.id = ai.volume_id"
        " WHERE ai.asset_id = ? ORDER BY ai.id",
        (int(asset_id),),
    )
    payload["instances"] = [dict(item) for item in instances]

    library = db.query_one("SELECT id, name, uuid, status, root_path_hint FROM library WHERE id = ?", (int(row["library_id"]),))
    payload["library"] = dict(library) if library is not None else None

    if payload.get("shot_id"):
        # ⚠️ 这里原来写的是 `SELECT s.id, s.name, …`，把 `shot AS s` 当成了
        # Session/Scene 那种"有 name 的表" —— 但 **`shot` 表没有 `name` 列**。
        #
        # 为什么 Phase 1 一直没暴露：这段代码只在 `asset.shot_id` 非空时才跑，
        # 而 Phase 1 **从不写 `shot_id`**（Shot 是 Phase 2 才落库的派生结构）。
        # Phase 2 把 `shot_id` 填上之后，这条 SQL 立刻变成
        # `OperationalError: no such column: s.name` ——
        # 资产详情接口 **500**，抽屉打不开（实测：点网格里的照片，
        # 界面只弹一句"读取详情失败：服务器内部错误"）。
        #
        # 这是"**潜伏缺陷被新功能激活**"的典型：Phase 1 的测试全绿、
        # Phase 2 的 1000+ 测试也全绿（它们没覆盖"有 shot_id 时查详情"）。
        # 记住：**改了数据形状，就要回头测那些"以前总是走 else 分支"的代码。**
        shot = db.query_one(
            "SELECT s.id, s.scene_id, s.captured_at, s.capture_date_local,"
            "       s.primary_asset_id, s.camera_key, s.group_source, s.group_confidence,"
            "       s.group_revision,"
            "       sc.name AS scene_name, sc.dir_hint,"
            "       sc.captured_from, sc.captured_to, sc.source AS scene_source"
            "  FROM shot AS s LEFT JOIN scene AS sc ON sc.id = s.scene_id"
            " WHERE s.id = ?",
            (int(payload["shot_id"]),),
        )
        payload["shot"] = dict(shot) if shot is not None else None
    else:
        payload["shot"] = None
    return payload


def camera_histogram(db: Database, library_id: int | None = None) -> list[dict[str, Any]]:
    """按机型分组计数。

    ⚠️ `library_id is None`（"全部库"）时**不 join `asset`** ——
    机型信息全在 metadata 里，而 join 实测要 7 ms（21,400 行）。
    筛选面板每次进照片库都要拉这个接口，不该为一个不需要的 join 付钱。
    指定了库才必须 join（库归属在 asset 上）。
    """
    if library_id is None:
        rows = db.query(
            "SELECT COALESCE(m.camera_model, '(未知)') AS value, COUNT(*) AS n"
            " FROM metadata AS m GROUP BY value ORDER BY n DESC LIMIT 50"
        )
    else:
        rows = db.query(
            "SELECT COALESCE(m.camera_model, '(未知)') AS value, COUNT(*) AS n"
            " FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE a.library_id = ? GROUP BY value ORDER BY n DESC LIMIT 50",
            (int(library_id),),
        )
    return [{"value": str(r["value"]), "count": int(r["n"])} for r in rows]


def capture_year_histogram(db: Database, library_id: int | None = None) -> list[dict[str, Any]]:
    """按年份分组计数。同 `camera_histogram`：不指定库时不 join。"""
    if library_id is None:
        rows = db.query(
            "SELECT substr(m.capture_date_local, 1, 4) AS value, COUNT(*) AS n"
            " FROM metadata AS m WHERE m.capture_date_local IS NOT NULL"
            " GROUP BY value ORDER BY value DESC"
        )
    else:
        rows = db.query(
            "SELECT substr(m.capture_date_local, 1, 4) AS value, COUNT(*) AS n"
            " FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE m.capture_date_local IS NOT NULL AND a.library_id = ?"
            " GROUP BY value ORDER BY value DESC",
            (int(library_id),),
        )
    return [{"value": str(r["value"]), "count": int(r["n"])} for r in rows]


def lens_histogram(db: Database, library_id: int | None = None) -> list[dict[str, Any]]:
    """按镜头分组计数（给筛选面板的下拉框）。

    与 `camera_histogram` 同一形状。实测本库只有 4 支镜头，所以下拉框
    完全放得下；即便镜头多一些，也让 SQL 兜住（LIMIT 50）。

    ⚠️ 只查 `metadata`，**不 join `asset`**：镜头信息全在 metadata 里，
    而 join 本身实测要 7 ms（21,400 行）。筛选面板每次进照片库都要拉这个接口，
    不该为一个不需要的 join 付钱。
    """
    if library_id is None:
        rows = db.query(
            "SELECT COALESCE(m.lens_model, '(未知)') AS value, COUNT(*) AS n"
            " FROM metadata AS m GROUP BY value ORDER BY n DESC LIMIT 50"
        )
    else:
        rows = db.query(
            "SELECT COALESCE(m.lens_model, '(未知)') AS value, COUNT(*) AS n"
            " FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE a.library_id = ? GROUP BY value ORDER BY n DESC LIMIT 50",
            (int(library_id),),
        )
    return [{"value": str(r["value"]), "count": int(r["n"])} for r in rows]


def focal_range(db: Database, library_id: int | None = None) -> dict[str, Any]:
    """焦距的实际取值范围 + 快捷区间计数。

    **排除 0 与 NULL**：`focal_length = 0` 是"没有这个数据"（视频、缺 EXIF），
    不是"0 毫米的超广角"。本库里有 2,262 张这种 —— 如果把 0 算进范围，
    滑块的下界会变成 0，而区间计数也会被它们污染。

    快捷区间按本库真实分布挑选（见 `10-import-audit.md` 同一套实测方法）：
    广角 ≤35 / 标准 35–70 / 中长 70–135 / 长焦 >135。

    ⚠️ 这里**只用一次聚合**算出全部数字（min/max/计数/四个区间/无焦距计数）。
    第一版是 5 条独立查询、每条都把 `asset ⋈ metadata` 扫一遍 ——
    实测 `/api/facets` 因此要 **120 ms**，而照片库的筛选面板每次都要拉它。
    合并成一条带 `SUM(CASE WHEN …)` 的聚合之后是一次扫描。
    """
    where = "WHERE m.focal_length IS NOT NULL AND m.focal_length > 0"
    params: list[Any] = []
    if library_id is not None:
        where += " AND a.library_id = ?"
        params.append(int(library_id))

    # 边界写死在 SQL 里（不是用户输入）。与下面 presets 的边界必须一致。
    row = db.query_one(
        "SELECT MIN(m.focal_length) AS lo, MAX(m.focal_length) AS hi, COUNT(*) AS n,"
        " SUM(CASE WHEN m.focal_length < 35 THEN 1 ELSE 0 END) AS b1,"
        " SUM(CASE WHEN m.focal_length >= 35 AND m.focal_length < 70 THEN 1 ELSE 0 END) AS b2,"
        " SUM(CASE WHEN m.focal_length >= 70 AND m.focal_length < 135 THEN 1 ELSE 0 END) AS b3,"
        " SUM(CASE WHEN m.focal_length >= 135 THEN 1 ELSE 0 END) AS b4"
        f" FROM asset AS a JOIN metadata AS m ON m.asset_id = a.id {where}",
        params,
    )
    lo = float(row["lo"]) if row and row["lo"] is not None else 0.0
    hi = float(row["hi"]) if row and row["hi"] is not None else 0.0
    counted = int(row["n"]) if row else 0
    bucket = {
        ("广角 ≤35"): int(row["b1"] or 0) if row else 0,
        ("标准 35–69"): int(row["b2"] or 0) if row else 0,
        ("中长 70–134"): int(row["b3"] or 0) if row else 0,
        ("长焦 ≥135"): int(row["b4"] or 0) if row else 0,
    }

    # ⚠️ 四个区间必须是**无重叠的划分**，否则张数加起来会超过总数 ——
    # 实测第一版就是这样：35 / 70 / 135 三个边界值同时落在相邻两个区间里，
    # 四个数加起来 29,404，而实际只有 21,335 张有焦距。用户会以为统计错了。
    #
    # 现在按**左闭右开**切：[lo, hi)。只有一个例外是最后一段（无上界）。
    # 界面的区间是"从上一个边界到这一个边界"，读作连续的一段。
    presets = [
        {"label": label, "min": low, "max": high, "count": bucket[label]}
        for label, low, high in (
            ("广角 ≤35", None, 35.0),
            ("标准 35–69", 35.0, 70.0),
            ("中长 70–134", 70.0, 135.0),
            ("长焦 ≥135", 135.0, None),
        )
    ]

    # 有多少张是"没有焦距数据"的 —— 界面上要说清楚它们会被排除，否则用户会以为丢照片。
    #
    # ⚠️ 这里**不用 `asset LEFT JOIN metadata`**（那要扫两表并做外连接，实测 20+ ms）。
    # 等价算法：`总数 − 有焦距的`。前提是"每张 asset 都有 metadata 行"——
    # 这在正常库里成立（索引阶段对没有元数据的文件也写一行空记录，
    # 否则 `needing_metadata` 会反复挑出它们）。若将来允许缺 metadata 行，
    # 这个减法会少算，需要改回外连接。
    if library_id is None:
        total_assets = int(db.scalar("SELECT COUNT(*) FROM asset", default=0))
        total_meta = int(db.scalar("SELECT COUNT(*) FROM metadata", default=0))
    else:
        total_assets = int(db.scalar(
            "SELECT COUNT(*) FROM asset WHERE library_id = ?", (int(library_id),), default=0))
        total_meta = int(db.scalar(
            "SELECT COUNT(*) FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE a.library_id = ?", (int(library_id),), default=0))
    # 没有焦距 = 没有 metadata 行 + 有行但焦距为 0/NULL
    missing = max(0, total_assets - total_meta) + max(0, total_meta - counted)

    return {
        "min": lo,
        "max": hi,
        "count_with_focal": counted,
        "count_without_focal": int(missing),
        "presets": presets,
    }


def library_overview(db: Database) -> dict[str, Any]:
    """全局概览：各 Library 统计 + 任务与存储占用。"""
    from ..jobs import JobQueue
    from ..jobs.pipelines import library_stats
    from ..library.store import LibraryStore

    store = LibraryStore(db)
    libraries = []
    for library in store.list_all():
        libraries.append(
            {
                "id": library.id,
                "name": library.name,
                "status": library.status,
                "root_path_hint": library.root_path_hint,
                "stats": library_stats(db, library.id),
            }
        )

    queue = JobQueue(db)
    jobs = queue.list_recent(limit=5)
    return {
        "libraries": libraries,
        "totals": {
            "assets": int(db.scalar("SELECT COUNT(*) FROM asset", default=0)),
            "bytes": int(db.scalar("SELECT COALESCE(SUM(size),0) FROM asset", default=0)),
            "metadata": int(db.scalar("SELECT COUNT(*) FROM metadata", default=0)),
            "previews": int(db.scalar("SELECT COUNT(*) FROM preview", default=0)),
            "preview_bytes": int(db.scalar("SELECT COALESCE(SUM(bytes),0) FROM preview", default=0)),
            "hashed": int(
                db.scalar(
                    "SELECT COUNT(*) FROM asset WHERE content_hash IS NOT NULL AND content_hash != ''",
                    default=0,
                )
            ),
        },
        "jobs": [
            {
                "id": record.id,
                "type": record.type,
                "status": record.status,
                "percent": round(record.percent, 2),
                "note": record.progress_note,
            }
            for record in jobs
        ],
    }


def list_jobs(db: Database, *, limit: int = 20, active_only: bool = False) -> list[dict[str, Any]]:
    from ..jobs import JobQueue

    queue = JobQueue(db)
    records = queue.list_active() if active_only else queue.list_recent(limit=limit)
    return [
        {
            "id": record.id,
            "type": record.type,
            "status": record.status,
            "progress_done": record.progress_done,
            "progress_total": record.progress_total,
            "percent": round(record.percent, 2),
            "note": record.progress_note,
            "error": record.error,
            "retry_count": record.retry_count,
            "library_id": record.scope.get("library_id"),
            "created_at": record.created_at,
            "finished_at": record.finished_at,
        }
        for record in records
    ]


def integrity_summary(db: Database) -> dict[str, Any]:
    """完整性摘要。`integrity_check` 在大库上会读全库，因此这里用 `quick_check`。"""
    violations = db.foreign_key_check()
    return {
        "schema_version": db.schema_version(),
        "quick_check": db.quick_check(),
        "foreign_key_violations": len(violations),
        "orphan_assets": int(
            db.scalar(
                "SELECT COUNT(*) FROM asset WHERE library_id NOT IN (SELECT id FROM library)",
                default=0,
            )
        ),
        "orphan_metadata": int(
            db.scalar(
                "SELECT COUNT(*) FROM metadata WHERE asset_id NOT IN (SELECT id FROM asset)",
                default=0,
            )
        ),
        "orphan_previews": int(
            db.scalar(
                "SELECT COUNT(*) FROM preview WHERE asset_id NOT IN (SELECT id FROM asset)",
                default=0,
            )
        ),
        "json_each_supported": _supports_json1(db),
    }


def _supports_json1(db: Database) -> bool:
    try:
        row = db.query_one("SELECT json_valid('{}') AS ok")
        return bool(row and row["ok"])
    except sqlite3.Error:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- 映射
# ============================================================ Phase 2：Session / Scene / 精选

def session_list(db: Database, *, limit: int = 500) -> dict[str, Any]:
    """Session 列表（含城市/时段/时间范围/Shot 数）。

    Shot 数用**相关子查询**而不是 JOIN + GROUP BY：`shot` 表可能有几万行，
    子查询走 `idx_shot_scene` 的代价远小于分组聚合，而且不会因为 Scene 为空而丢行。
    """
    rows = db.query(
        "SELECT s.id, s.name, s.city_hint, s.period_hint, s.captured_from, s.captured_to,"
        "       s.confidence, s.group_revision, s.source,"
        "       (SELECT COUNT(*) FROM scene sc WHERE sc.session_id = s.id) AS scenes,"
        "       (SELECT COUNT(*) FROM shot sh JOIN scene sc2 ON sc2.id = sh.scene_id"
        "         WHERE sc2.session_id = s.id AND sh.retired_at IS NULL) AS shots"
        "  FROM session s"
        " ORDER BY COALESCE(s.captured_from, '') DESC, s.id"
        " LIMIT ?",
        (int(limit),),
    )
    return {
        "total": int(db.scalar("SELECT COUNT(*) FROM session", default=0)),
        "items": [
            {
                "id": int(r["id"]),
                "name": r["name"],
                "city": r["city_hint"],
                "period": r["period_hint"],
                "captured_from": r["captured_from"],
                "captured_to": r["captured_to"],
                "confidence": r["confidence"],
                "group_revision": int(r["group_revision"]),
                "source": r["source"],
                "scenes": int(r["scenes"]),
                "shots": int(r["shots"]),
            }
            for r in rows
        ],
    }


def scene_list(
    db: Database, *, session_id: int | None = None, limit: int = 500
) -> dict[str, Any]:
    """Scene 列表，附每个 Scene 的精选进度（各状态计数）。

    进度是 PRD §9.2「一个 Scene 可以最终只留下 1 张，也可以留下 2~N 张」的可视化前提 ——
    用户需要看到"这个 Scene 还剩几张没审"。
    """
    # `retired_at` 是迁移 006 加的列。升级窗口期内（库仍是 schema 5）不引用它，
    # 让接口在两种 schema 下都能用，而不是整个端点 500。
    live = " AND sh.retired_at IS NULL" if _has_column(db, "shot", "retired_at") else ""

    where = ""
    params: list[Any] = []
    if session_id is not None:
        where = " WHERE sc.session_id = ?"
        params.append(int(session_id))
    params.append(int(limit))

    rows = db.query(
        "SELECT sc.id, sc.session_id, sc.name, sc.captured_from, sc.captured_to,"
        "       sc.source, sc.confidence, sc.group_revision, sc.evidence_json,"
        "       ses.city_hint, ses.period_hint,"
        f"       (SELECT COUNT(*) FROM shot sh WHERE sh.scene_id = sc.id{live}) AS shots,"
        "       (SELECT COUNT(*) FROM shot sh JOIN selection sel2 ON sel2.shot_id = sh.id"
        f"         WHERE sh.scene_id = sc.id{live}"
        "           AND sel2.status IN ('CANDIDATE','SELECTED','FINAL')) AS reviewed,"
        "       (SELECT COUNT(*) FROM shot sh JOIN selection sel3 ON sel3.shot_id = sh.id"
        f"         WHERE sh.scene_id = sc.id{live}"
        "           AND sel3.status = 'FINAL') AS finals"
        "  FROM scene sc LEFT JOIN session ses ON ses.id = sc.session_id"
        + where
        + " ORDER BY COALESCE(sc.captured_from, '') , sc.id LIMIT ?",
        params,
    )
    return {
        "total": len(rows),
        "items": [
            {
                "id": int(r["id"]),
                "session_id": r["session_id"],
                "name": r["name"],
                "city": r["city_hint"],
                "period": r["period_hint"],
                "captured_from": r["captured_from"],
                "captured_to": r["captured_to"],
                "shots": int(r["shots"]),
                "reviewed": int(r["reviewed"]),
                "finals": int(r["finals"]),
                "needs_review": int(r["shots"]) - int(r["reviewed"]),
                "source": r["source"],
                "confidence": r["confidence"],
            }
            for r in rows
        ],
    }


def scene_detail(db: Database, scene_id: int) -> dict[str, Any] | None:
    """Scene 详情：Shot 列表（含精选状态与理由）+ 该 Scene 的选择过程时间线。

    这是断点 #8「从最终精选反查完整拍摄组」的数据来源：
    FINAL → 它所属的 Shot → 该 Shot 的相似组 → 该 Scene 的全部 Shot。
    """
    scene = db.query_one(
        "SELECT sc.*, ses.city_hint, ses.period_hint"
        "  FROM scene sc LEFT JOIN session ses ON ses.id = sc.session_id WHERE sc.id = ?",
        (int(scene_id),),
    )
    if scene is None:
        return None

    # `retired_at` 是迁移 006 加的列；升级窗口期内不引用它（见 `scene_list` 的同类处理）
    live_shot = " AND sh.retired_at IS NULL" if _has_column(db, "shot", "retired_at") else ""
    shots = db.query(
        "SELECT sh.id, sh.primary_asset_id, sh.captured_at, sh.capture_date_local,"
        "       sh.camera_key, sh.group_source, sh.group_confidence, sh.group_revision,"
        "       COALESCE(sel.status, 'UNREVIEWED') AS selection_status, sel.reason,"
        "       (SELECT COUNT(*) FROM asset a WHERE a.shot_id = sh.id) AS assets,"
        "       a.relative_path AS primary_path, a.file_type AS primary_type,"
        "       CASE WHEN p.id IS NULL THEN 0 ELSE 1 END AS has_thumb"
        "  FROM shot sh"
        "  LEFT JOIN selection sel ON sel.shot_id = sh.id"
        "  LEFT JOIN asset a ON a.id = sh.primary_asset_id"
        "  LEFT JOIN preview p ON p.asset_id = a.id AND p.kind = 'thumb'"
        " WHERE sh.scene_id = ?"
        + live_shot
        + " ORDER BY COALESCE(sh.captured_at, ''), sh.id",
        (int(scene_id),),
    )

    events = db.query(
        "SELECT e.shot_id, e.from_status, e.to_status, e.reason, e.created_at"
        "  FROM selection_event e JOIN shot sh ON sh.id = e.shot_id"
        " WHERE sh.scene_id = ?"
        " ORDER BY e.created_at, e.id",
        (int(scene_id),),
    )

    counts = {status: 0 for status in ("UNREVIEWED", "CANDIDATE", "SELECTED", "FINAL")}
    for row in shots:
        key = str(row["selection_status"])
        counts[key] = counts.get(key, 0) + 1

    return {
        "id": int(scene["id"]),
        "session_id": scene["session_id"],
        "name": scene["name"],
        "city": scene["city_hint"],
        "period": scene["period_hint"],
        "captured_from": scene["captured_from"],
        "captured_to": scene["captured_to"],
        "source": scene["source"],
        "confidence": scene["confidence"],
        "evidence": json.loads(scene["evidence_json"]) if scene["evidence_json"] else None,
        "counts": counts,
        "shots": [
            {
                "id": int(r["id"]),
                "primary_asset_id": r["primary_asset_id"],
                "primary_path": r["primary_path"],
                "primary_type": r["primary_type"],
                "captured_at": r["captured_at"],
                "asset_count": int(r["assets"]),
                "has_thumb": bool(r["has_thumb"]),
                "group_source": r["group_source"],
                "group_confidence": r["group_confidence"],
                "selection_status": r["selection_status"],
                "reason": r["reason"],
            }
            for r in shots
        ],
        "events": [
            {
                "shot_id": int(r["shot_id"]),
                "from_status": r["from_status"],
                "to_status": r["to_status"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in events
        ],
    }


def structured_query(db: Database, payload: dict[str, Any] | None) -> dict[str, Any]:
    """结构化筛选查询（`POST /api/assets/query`，方案 §11.2）。

    与 `asset_page` 共用同一套列与 JOIN，因此**分页、缩略图占位、`selection_status`
    的行为完全一致**；区别只是筛选条件来自结构化对象而不是固定的 query 参数。

    安全：条件由 `filter_query.build_where` 生成，**值全部参数绑定**，
    字段与操作符都过白名单；这里不做任何字符串插值。
    """
    from . import filter_query as fq

    request = fq.normalise_request(payload)
    clauses, params = fq.build_where(request["filters"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order = fq.SORTS[request["sort"]]

    total = int(db.scalar(f"SELECT COUNT(*) {_ASSET_JOINS} {where}", params, default=0))
    rows = db.query(
        f"SELECT {_ASSET_COLUMNS} {_ASSET_JOINS} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, request["limit"], request["offset"]],
    )
    return {
        "total": total,
        "offset": request["offset"],
        "limit": request["limit"],
        "sort": request["sort"],
        # 回显"到底筛了什么"的人话 —— 让用户能核对，也是将来自然语言筛选的闭环一半
        "description": fq.describe(request["filters"]),
        "items": [_asset_row(row) for row in rows],
    }


def _has_column(db: Database, table: str, column: str) -> bool:
    """schema 兼容探测：Phase 1 的库没有 006 加的列时不要炸（升级窗口期）。"""
    try:
        with db.read() as conn:
            return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:  # noqa: BLE001
        return False


def _asset_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    payload: dict[str, Any] = {
        "id": int(row["id"]),
        "library_id": int(row["library_id"]),
        "relative_path": row["relative_path"],
        "file_type": row["file_type"],
        "status": row["status"],
        "size": int(row["size"]) if row["size"] is not None else None,
        "mime": row["mime"],
        "content_hash": row["content_hash"],
        "shot_id": int(row["shot_id"]) if "shot_id" in keys and row["shot_id"] is not None else None,
        "status_reason": row["status_reason"] if "status_reason" in keys else None,
        "capture_at": row["capture_at"],
        "capture_date_local": row["capture_date_local"],
        "camera_make": row["camera_make"],
        "camera_model": row["camera_model"],
        "lens_model": row["lens_model"],
        "focal_length": row["focal_length"],
        "aperture": row["aperture"],
        "shutter_speed": row["shutter_speed"],
        "iso": row["iso"],
        "width": row["width"],
        "height": row["height"],
        "gps_lat": row["gps_lat"],
        "gps_lon": row["gps_lon"],
        "tz_offset": row["tz_offset"],
        "orientation": row["orientation"],
        "thumb_width": row["thumb_width"],
        "thumb_height": row["thumb_height"],
        "has_thumb": bool(row["has_thumb"]),
        "has_preview": bool(row["has_preview"]),
    }
    # 缩略图比例：前端用它预留占位框，避免滚动时布局抖动
    if payload["thumb_width"] and payload["thumb_height"]:
        payload["aspect"] = round(payload["thumb_width"] / payload["thumb_height"], 4)
    else:
        payload["aspect"] = None
    # 精选状态：**字段位先占住**。Phase 1 一律 UNREVIEWED（selection 表恒为空），
    # Phase 2 只需把 SELECT 里的常量换成对 selection 表的 JOIN —— 前端与查询参数都不用改。
    payload["selection_status"] = (
        row["selection_status"] if "selection_status" in keys else "UNREVIEWED"
    )
    # 「这张照片里有谁」—— 给弹窗显示可点的人名标签用。空数组而不是 None，
    # 免得前端每处都要判空。
    raw = row["person_ids"] if "person_ids" in keys else None
    payload["person_ids"] = [int(x) for x in str(raw).split(",")] if raw else []
    return payload


# ============================================================ 浏览轴（browse axis）
#: 本版本实现的浏览轴。Phase 2 追加 selection / collection 时**只需增加实现分支**，
#: `axis` + `parent` 的请求形状与 `scope_kind` + `scope_key` 的查询形状都保持不变。
BROWSE_AXES_IMPLEMENTED = ("path", "time", "selection", "collection")
#: 已冻结但**仍不实现**的轴 —— 明确拒绝，而不是静默返回全库。
#: `tag` 按决策 #6 本期不做（PRD §9.2 通篇无"标签"要求；Collections 已覆盖其用途），
#: 保持 501 不动，Phase 3 加实现分支即可、前端零改动。
BROWSE_AXES_RESERVED = ("tag",)

SEG = "CHAR(92)"  # SQL 里的反斜杠：用 CHAR(92) 避免 Python/SQL 双重转义（踩过这个坑）


def _path_browse(
    db: Database, *, library_id: int | None, parent: str, limit: int
) -> list[dict[str, Any]]:
    """物理路径轴：第 0 层是城市，第 1 层是时段目录。

    直接从 `asset.relative_path` 聚合 —— 不需要额外的索引，也不碰任何原片
    （PRD §11.2：浏览只访问数据库索引）。
    """
    where, params = _build_filters(
        library_id=library_id, query=None, file_type=None, status=None, camera=None,
        lens=None, date_from=None, date_to=None, year=None, only_missing_metadata=False,
        scope_kind="path" if parent else None, scope_key=parent or None,
    )
    # 跳过 parent 前缀后取下一段
    offset = len(parent) + 2 if parent else 1
    segment = (
        f"CASE WHEN instr(substr(a.relative_path, {offset}), {SEG}) > 0"
        f" THEN substr(substr(a.relative_path, {offset}), 1,"
        f"           instr(substr(a.relative_path, {offset}), {SEG}) - 1)"
        f" ELSE substr(a.relative_path, {offset}) END"
    )
    depth = parent.count("\\") + 1
    # `has_children` 表示"这是树里还能展开的一层"。**时段节点下面是文件、不是目录**，
    # 因此不能简单地用"库里还有更深的路径"判断：要再深**两层**才算存在子目录节点。
    # 同时把最深层的查询提到循环外做一次（18 个城市节点不该查 18 次库）。
    deepest = _deepest_segments(db, library_id)
    node_segments = (parent.count("\\") + 2) if parent else 1
    has_children = deepest >= node_segments + 2
    rows = db.query(
        f"SELECT {segment} AS key, COUNT(*) AS n FROM asset AS a {where}"
        f" GROUP BY key ORDER BY n DESC, key LIMIT ?",
        [*params, int(limit)],
    )
    return [
        {
            "key": f"{parent}\\{row['key']}" if parent else str(row["key"]),
            "label": _node_label(parent, str(row["key"])),
            "count": int(row["n"]),
            "has_children": has_children,
        }
        for row in rows
        if str(row["key"])
    ]


def _deepest_segments(db: Database, library_id: int | None) -> int:
    """库内最深的路径层数（`城市\\时段\\文件` → 3）。一次聚合，零成本。"""
    where, params = _build_filters(
        library_id=library_id, query=None, file_type=None, status=None, camera=None,
        lens=None, date_from=None, date_to=None, year=None, only_missing_metadata=False,
    )
    deepest = db.scalar(
        f"SELECT MAX(LENGTH(a.relative_path) - LENGTH(REPLACE(a.relative_path, {SEG}, '')))"
        f" FROM asset AS a {where}",
        params,
        default=0,
    )
    return int(deepest or 0) + 1


def _has_deeper(db: Database, library_id: int | None, depth: int) -> bool:
    """保留给调试/测试的便捷函数：该层之下是否还有目录层。"""
    return _deepest_segments(db, library_id) > depth + 1


def _time_browse(
    db: Database, *, library_id: int | None, parent: str, limit: int
) -> list[dict[str, Any]]:
    """时间轴：年 → 月 → 日（按 `capture_date_local` 的字符前缀聚合）。"""
    level = 0 if not parent else len([p for p in parent.split("-") if p])
    width = (4, 7, 10)[min(level, 2)]
    like_parent = parent if level else None

    where, params = _build_filters(
        library_id=library_id, query=None, file_type=None, status=None, camera=None,
        lens=None, date_from=None, date_to=None, year=None, only_missing_metadata=False,
        scope_kind="time" if like_parent else None, scope_key=like_parent,
    )
    rows = db.query(
        f"SELECT substr(m.capture_date_local, 1, {width}) AS key, COUNT(*) AS n"
        f" FROM asset AS a LEFT JOIN metadata AS m ON m.asset_id = a.id {where}"
        f" AND m.capture_date_local IS NOT NULL AND m.capture_date_local <> ''"
        f" GROUP BY key ORDER BY key DESC LIMIT ?",
        [*params, int(limit)],
    )
    return [
        {
            "key": str(row["key"]),
            "label": _time_label(str(row["key"])),
            "count": int(row["n"]),
            "has_children": level < 2,
        }
        for row in rows
        if str(row["key"])
    ]


def _selection_browse(
    db: Database, *, library_id: int | None, parent: str, limit: int
) -> list[dict[str, Any]]:
    """精选轴：四个状态各一个节点（PRD §9.2「候选、精选、最终精选」+ 未审阅）。

    `parent` 未被使用（这一层就是叶子）—— 保留参数以维持与其它轴一致的签名。
    """
    from ..library import selection as sel

    del parent, limit
    where, params = _build_filters(
        library_id=library_id, query=None, file_type=None, status=None, camera=None,
        lens=None, date_from=None, date_to=None, year=None, only_missing_metadata=False,
    )
    rows = db.query(
        "SELECT COALESCE(s.status, 'UNREVIEWED') AS key, COUNT(*) AS n"
        "  FROM asset AS a"
        "  LEFT JOIN selection AS s ON s.shot_id = a.shot_id"
        f" {where}"
        " GROUP BY key",
        params,
    )
    tally = {str(r["key"]): int(r["n"]) for r in rows}
    # 四个状态**固定全部列出**（哪怕是 0）—— 界面左上角的状态栏靠这个渲染，
    # 缺一个会让"这个库还没有最终精选"看起来像是功能坏了。
    return [
        {
            "key": status,
            "label": sel.STATUS_LABELS[status],
            "count": tally.get(status, 0),
            "has_children": False,
        }
        for status in sel.STATUSES
    ]


def _collection_browse(
    db: Database, *, library_id: int | None, parent: str, limit: int
) -> list[dict[str, Any]]:
    """主题轴：一层，列出全部 Collection（PRD §9.2.1）。

    注意主题**不受 Library 限制**（PRD：「不受时间、地点或 Session 限制」），
    因此这里**不按 library_id 过滤** —— 传了也不生效，这是刻意的。
    """
    del parent, library_id
    rows = db.query(
        "SELECT c.id, c.name, COALESCE(c.description, '') AS description,"
        "       (SELECT COUNT(*) FROM collection_item ci"
        "         JOIN shot sh ON sh.id = ci.shot_id AND sh.retired_at IS NULL"
        "        WHERE ci.collection_id = c.id) AS items"
        "  FROM collection c ORDER BY c.sort_order, c.id LIMIT ?",
        (int(limit),),
    )
    return [
        {
            "key": str(r["id"]),
            "label": str(r["name"]),
            "count": int(r["items"]),
            "has_children": False,
            "description": r["description"],
        }
        for r in rows
    ]


def browse_tree(
    db: Database,
    *,
    axis: str = "path",
    library_id: int | None = None,
    parent: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """可插拔的浏览树。返回 `{axis, parent, nodes, total}`。"""
    parent = (parent or "").strip().strip("\\")
    if axis == "path":
        nodes = _path_browse(db, library_id=library_id, parent=parent, limit=limit)
    elif axis == "time":
        nodes = _time_browse(db, library_id=library_id, parent=parent, limit=limit)
    elif axis == "selection":
        nodes = _selection_browse(db, library_id=library_id, parent=parent, limit=limit)
    elif axis == "collection":
        nodes = _collection_browse(db, library_id=library_id, parent=parent, limit=limit)
    else:
        raise ValueError(f"未实现的浏览轴：{axis}")
    return {
        "axis": axis,
        "parent": parent,
        "nodes": nodes,
        "total": sum(node["count"] for node in nodes),
    }


def _node_label(parent: str, key: str) -> str:
    """路径轴的显示名：时段目录名渲染成人话，城市名原样。"""
    if not parent:
        return key
    return prettify_session_folder(key)


def _time_label(key: str) -> str:
    parts = key.split("-")
    if len(parts) == 1:
        return f"{key} 年"
    if len(parts) == 2:
        return f"{key}"
    return key


def prettify_session_folder(folder: str) -> str:
    """把 `2025-11-29_0610-1747` / `2025-09-07_2212_至_09-08_0200` 渲染成人话。

    解析失败时**原样返回** —— 目录名是事实，不能因为渲染失败就把它藏起来。
    """
    import re

    same_day = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})-(\d{2})(\d{2})", folder)
    if same_day:
        date, h1, m1, h2, m2 = same_day.groups()
        return f"{date} {h1}:{m1}–{h2}:{m2}"

    cross = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})_至_(\d{2}-\d{2})_(\d{2})(\d{2})", folder
    )
    if cross:
        date, h1, m1, date2, h2, m2 = cross.groups()
        return f"{date} {h1}:{m1} – {date2} {h2}:{m2}"

    cross_year = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})_至_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})", folder
    )
    if cross_year:
        date, h1, m1, date2, h2, m2 = cross_year.groups()
        return f"{date} {h1}:{m1} – {date2} {h2}:{m2}"
    return folder


__all__ = [
    "BROWSE_AXES_IMPLEMENTED",
    "BROWSE_AXES_RESERVED",
    "SORT_SQL",
    "asset_detail",
    "asset_page",
    "browse_tree",
    "camera_histogram",
    "capture_year_histogram",
    "integrity_summary",
    "library_overview",
    "list_jobs",
    "prettify_session_folder",
]
