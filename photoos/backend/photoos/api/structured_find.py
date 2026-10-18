"""Phase 3 · 结构化筛选（L4）：按时间/地点/场次精确取照片。

## 为什么需要它（实测结论，不是设计偏好）

用户问「我的拍摄博物馆能不能分出来」。实测答案是：

  * **语义检索分不出来**：`a marble statue of a person` 的 top-100 里，
    用户那次杭州博物馆只占 14 张，而**希腊的三个博物馆场次占了 72 张** ——
    因为语义检索只看得见"像不像博物馆内容"，**看不见"这是哪一趟"**。
    而且该场次自身的几何凝聚度只有 1.082（内部很杂：雕像/访客/建筑/户外混在一起）。

  * **结构化信息能 100% 分出来**：`session` / `scene` 早就把每一趟记得清清楚楚
    （78 个 session、303 个 scene，带城市、时段、时间范围）。
    你那次是 `session #45 杭州市\\2025-10-18_1454-1631`，
    下辖 `scene #180`（14:54–15:46，29 Shot）与 `scene #181`（15:55–16:31，41 Shot）。

**所以"分出哪一趟拍摄"该用结构化条件，不是语义。** 这一层是确定性的、可复算的、
不依赖任何模型 —— 与 `filter_query.py`（字段白名单 + 参数绑定）同一套安全纪律。

## 安全边界（照抄 filter_query.py 的纪律）

  * **绝不接受原始 SQL**：只接受白名单参数，值全部走参数绑定
  * **未知参数显式报错**，不静默忽略
  * **默认带上限**，不一次性把全库拉出来
  * 只读 `asset`/`metadata`/`shot`/`scene`/`session`/`selection`，**不碰原片**
"""

from __future__ import annotations

from typing import Any

#: 每个 session 最多列多少个 Scene（防止一次刷屏）
DEFAULT_LIMIT = 60


def find_shots(
    db: Any,
    *,
    city: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    session_like: str | None = None,
    scene_id: int | None = None,
    min_shots: int | None = None,
    library_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """按结构化条件找 Scene（一个 Scene = 一次可筛完的决策单元）。

    返回每个 Scene 的身份与规模，并**如实报告它的时间范围** ——
    时间范围是用户判断"这是不是我要找的那趟"的依据（与语义搜索显示拍摄时刻同一理由）。
    """
    where: list[str] = ["sh.retired_at IS NULL"]
    params: list[Any] = []

    if city:
        where.append("ss.city_hint = ?")
        params.append(city)
    if date_from:
        where.append("COALESCE(sc.captured_from, sh.captured_at, '') >= ?")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(sc.captured_from, sh.captured_at, '') <= ?")
        params.append(date_to + "T23:59:59")
    if session_like:
        # 用户给的值一律当**字面量**（转义 LIKE 通配符），与 filter_query 同纪律
        escaped = str(session_like).replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where.append("(ss.period_hint LIKE ? ESCAPE '!' OR ss.city_hint LIKE ? ESCAPE '!')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    if scene_id is not None:
        where.append("sc.id = ?")
        params.append(int(scene_id))
    if library_id is not None:
        where.append("a.library_id = ?")
        params.append(int(library_id))

    having = ""
    if min_shots is not None:
        having = " HAVING COUNT(sh.id) >= ?"
        params.append(int(min_shots))

    sql = (
        "SELECT sc.id AS scene_id, sc.name AS scene_name, sc.confidence,"
        "       ss.id AS session_id, ss.city_hint, ss.period_hint,"
        "       MIN(COALESCE(m.capture_at, sh.captured_at)) AS first_at,"
        "       MAX(COALESCE(m.capture_at, sh.captured_at)) AS last_at,"
        "       COUNT(DISTINCT sh.id) AS shots,"
        "       COUNT(DISTINCT CASE WHEN sel.status = 'FINAL' THEN sh.id END) AS finals,"
        "       COUNT(DISTINCT CASE WHEN sel.status IS NOT NULL THEN sh.id END) AS reviewed"
        "  FROM scene sc"
        "  JOIN session ss ON ss.id = sc.session_id"
        "  JOIN shot sh ON sh.scene_id = sc.id"
        "  JOIN asset a ON a.shot_id = sh.id"
        "  LEFT JOIN metadata m ON m.asset_id = a.id"
        "  LEFT JOIN selection sel ON sel.shot_id = sh.id"
        f" WHERE {' AND '.join(where)}"
        " GROUP BY sc.id" + having +
        " ORDER BY first_at"
    )
    params.append(int(limit))
    sql += " LIMIT ?"
    return [
        {
            "scene_id": int(r["scene_id"]),
            "scene_name": r["scene_name"],
            "session_id": int(r["session_id"]),
            "city": r["city_hint"],
            "period": r["period_hint"],
            "first_at": r["first_at"],
            "last_at": r["last_at"],
            "shots": int(r["shots"]),
            "finals": int(r["finals"]),
            "reviewed": int(r["reviewed"]),
        }
        for r in db.query(sql, params)
    ]


__all__ = ["DEFAULT_LIMIT", "find_shots"]
