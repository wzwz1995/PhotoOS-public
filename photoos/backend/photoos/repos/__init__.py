"""数据访问：Library / Asset / Metadata / Preview / Job。

设计要点（PHASE1_TECH_PLAN §7）：
  * SQLite 是单写者：写入集中在"每个 job item 写一次"的粒度上（16,105 文件 → 约 16k 次写），
    而不是每文件多次写。批量提交 + `busy_timeout` 让多线程 worker 可以安全并发。
  * 增量扫描的依据是 `(size, mtime_ns)` 是否变化，而不是"文件是否出现过" ——
    这样重扫既幂等又便宜（16,105 条 SELECT 约 0.1 秒，不需要读文件内容）。
  * **永远不删除 asset 行来表示文件消失**：只把 `status` 置为 MISSING（PRD：删除数据库记录
    ≠ 删除原文件；反过来，文件暂时不可见也绝不能丢索引）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

from ..db import Database, utc_now


def _escape_like(value: str) -> str:
    """把 LIKE 的通配符转义成字面量（`ESCAPE '\\'`）。

    Phase 3 的 `structured_find` 已经确立这条纪律：用户输入的"场次关键词"
    一律当**字面量**，绝不让 `%` / `_` 变成通配符把结果放大。
    本函数是同一判据在 `repos` 层的副本 —— 两处必须一致，
    所以谁都不许自己手写 `replace('%', '')` 之类的近似做法。
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _load_source(raw: Any) -> dict[str, Any] | None:
    """把 `collection.source_json` 解回对象。

    **解析失败返回 None 而不抛异常**：来源只是解释性信息
    （"这个主题是从哪个词来的"），它坏掉不该让整个主题列表打不开。
    这与身份字段的处理相反 —— 身份对不上必须报错（宁可不显示也不能显示错的）。
    """
    if not raw:
        return None
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


#: 对外名称。API 层读 `collection.source_json` 时用它，
#: 保证"解析失败不报错"这条规则**只有一份实现**。
load_source = _load_source


# --------------------------------------------------------------------------- 模型
ASSET_STATUSES = ("PENDING", "OK", "MISSING", "UNREADABLE", "QUARANTINED", "SKIPPED")


@dataclass(frozen=True)
class AssetRecord:
    id: int
    library_id: int
    relative_path: str
    file_type: str
    status: str
    size: int | None
    mtime_ns: int | None
    content_hash: str | None
    hash_algo: str | None
    shot_id: int | None
    #: 文件 MIME。**不要用 `mimetypes.guess_type` 代替**：RAW（NEF/CR3/DNG…）
    #: 在那里查不到，会退化成 `application/octet-stream`，进而让浏览器把原片
    #: 当成未知二进制下载（实测故障现场：文件被存成无扩展名的 `original`）。
    mime: str | None = None
    #: 状态原因（例如为何被降级为 PENDING），便于界面与排查直接看到原因。
    status_reason: str | None = None


@dataclass(frozen=True)
class ScanOutcome:
    """一次发现阶段的结果统计。"""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: int = 0
    total_seen: int = 0

    @property
    def summary(self) -> str:
        return (
            f"新增 {self.added}，变化 {self.updated}，未变 {self.unchanged}，"
            f"标记缺失 {self.missing}，遍历错误 {self.errors}（共见 {self.total_seen}）"
        )


# --------------------------------------------------------------------------- Asset
class AssetRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 写
    def upsert_discovered(
        self, library_id: int, entries: Iterable[tuple[str, int, int, str, str | None]], *, now: str | None = None
    ) -> ScanOutcome:
        """批量写入"发现阶段"的结果。

        `entries` 每项为 `(relative_path, size, mtime_ns, file_type, mime)`。

        行为：
          * 新文件 → 插入，status='PENDING'（等待后续 hash/元数据/预览）
          * 已存在且 size/mtime 变化 → 更新 size/mtime/type，并把 hash 置空（内容可能已变）
          * 已存在且未变 → 只刷新 last_verified_at，不做多余写入
        """
        stamp = now or utc_now()
        added = updated = unchanged = 0
        rows = list(entries)

        with self.db.write() as conn:
            existing = {
                str(row["relative_path"]): row
                for row in conn.execute(
                    "SELECT id, relative_path, size, mtime_ns, file_type, status, content_hash"
                    " FROM asset WHERE library_id = ?",
                    (library_id,),
                )
            }
            for relative, size, mtime_ns, file_type, mime in rows:
                current = existing.get(relative)
                if current is None:
                    conn.execute(
                        "INSERT INTO asset (library_id, relative_path, size, mtime_ns, file_type,"
                        " mime, status, first_seen_at, last_verified_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
                        (library_id, relative, size, mtime_ns, file_type, mime, stamp, stamp),
                    )
                    added += 1
                    continue

                if int(current["size"] or -1) == size and int(current["mtime_ns"] or -1) == mtime_ns:
                    # 未变化：仅刷新校验时间；若先前被标记 MISSING/UNREADABLE 则复活
                    reviving = current["status"] in ("MISSING", "UNREADABLE")
                    if reviving:
                        conn.execute(
                            "UPDATE asset SET status = ?, status_reason = NULL, last_verified_at = ?"
                            " WHERE library_id = ? AND relative_path = ?",
                            ("OK" if current["content_hash"] else "PENDING", stamp, library_id, relative),
                        )
                    else:
                        conn.execute(
                            "UPDATE asset SET last_verified_at = ? WHERE library_id = ? AND relative_path = ?",
                            (stamp, library_id, relative),
                        )
                    unchanged += 1
                    continue

                # 内容可能已变：清空 hash 让后续重新计算，但**保留 shot 关联**
                asset_id = int(current["id"])
                conn.execute(
                    "UPDATE asset SET size = ?, mtime_ns = ?, file_type = ?, mime = ?,"
                    " content_hash = NULL, hash_algo = NULL, status = 'PENDING',"
                    " status_reason = NULL, last_verified_at = ?"
                    " WHERE id = ?",
                    (size, mtime_ns, file_type, mime, stamp, asset_id),
                )
                # **陈旧派生数据必须一并清除**，否则文件内容变了而派生数据还是旧的：
                #   * `needing_metadata` 要求 metadata 行不存在 → 旧 EXIF 永不刷新
                #   * 融合阶段只处理「既无哈希也无预览」者（pipelines.run_fused_hash_previews）
                #     → 旧预览还在，于是它既不被重算哈希、也不被重算预览
                # 后果：元数据与缩略图永久陈旧、状态卡在 PENDING、`index --wait` 永不收敛（缺陷 #13）。
                # 预览文件路径由 asset_id 确定性派生（media.adapter.asset_storage_key），
                # 重建时会覆盖同一路径，因此只需删记录、不必删磁盘文件。
                conn.execute("DELETE FROM metadata WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM preview WHERE asset_id = ?", (asset_id,))
                updated += 1

        return ScanOutcome(added=added, updated=updated, unchanged=unchanged, total_seen=len(rows))

    def mark_missing(self, library_id: int, seen: set[str]) -> int:
        """把库内未被本次遍历看到的 asset 标记为 MISSING（**不删除行**）。"""
        stamp = utc_now()
        with self.db.write() as conn:
            rows = conn.execute(
                "SELECT id, relative_path, status FROM asset WHERE library_id = ?"
                " AND status NOT IN ('MISSING','QUARANTINED')",
                (library_id,),
            ).fetchall()
            targets = [int(r["id"]) for r in rows if str(r["relative_path"]) not in seen]
            if targets:
                conn.executemany(
                    "UPDATE asset SET status = 'MISSING', status_reason = ?, last_verified_at = ?"
                    " WHERE id = ?",
                    [("本次扫描未发现该文件", stamp, asset_id) for asset_id in targets],
                )
        return len(targets)

    def set_hash(self, asset_id: int, content_hash: str, algo: str) -> None:
        self.db.execute(
            "UPDATE asset SET content_hash = ?, hash_algo = ?, last_verified_at = ? WHERE id = ?",
            (content_hash, algo, utc_now(), asset_id),
        )

    def set_status(self, asset_id: int, status: str, reason: str | None = None) -> None:
        self.db.execute(
            "UPDATE asset SET status = ?, status_reason = ?, last_verified_at = ? WHERE id = ?",
            (status, reason, utc_now(), asset_id),
        )

    def mark_ok(self, asset_id: int) -> None:
        self.db.execute(
            "UPDATE asset SET status = 'OK', status_reason = NULL, last_verified_at = ? WHERE id = ?",
            (utc_now(), asset_id),
        )

    def reconcile_status(self, library_id: int) -> tuple[int, int]:
        """把 `status` 与「派生数据是否齐备」对齐，返回 `(升为 OK 数, 降为 PENDING 数)`。

        `status='OK'` 的定义应当是**可推导的不变式**：文件在库、哈希已算、元数据已提取、
        缩略图与预览都已生成。它不该是某条代码路径的副作用 —— 否则会朝两个方向撒谎：

        * **漏置**：由「哈希（兜底）」补上哈希的资产，没有任何阶段会去置 OK
          → 数据齐全却永远停在 PENDING（真实踩到）。
        * **错置**：元数据阶段会乐观地 `mark_ok`，而随后的预览阶段失败
          （原片损坏 / 格式不可解码）→ 资产带着 OK 状态却没有缩略图。

        因此这里**双向**对齐：齐备的 PENDING 升为 OK，不齐备的 OK 降回 PENDING。
        两个条件互斥（以"是否齐备"划分），不会来回翻转。
        """
        complete = (
            "content_hash IS NOT NULL AND content_hash <> ''"
            " AND EXISTS (SELECT 1 FROM metadata AS m WHERE m.asset_id = asset.id)"
            " AND EXISTS (SELECT 1 FROM preview AS p WHERE p.asset_id = asset.id AND p.kind = 'thumb')"
            " AND EXISTS (SELECT 1 FROM preview AS p WHERE p.asset_id = asset.id AND p.kind = 'preview')"
        )
        with self.db.write() as conn:
            promoted = conn.execute(
                "UPDATE asset SET status = 'OK', status_reason = NULL"
                f" WHERE library_id = ? AND status = 'PENDING' AND {complete}",
                (int(library_id),),
            ).rowcount
            demoted = conn.execute(
                "UPDATE asset SET status = 'PENDING',"
                " status_reason = COALESCE(status_reason, '派生数据不完整：预览或元数据缺失')"
                f" WHERE library_id = ? AND status = 'OK' AND NOT ({complete})",
                (int(library_id),),
            ).rowcount
        return int(promoted or 0), int(demoted or 0)

    # ------------------------------------------------------------------ 读
    def get(self, asset_id: int) -> AssetRecord:
        row = self.db.query_one("SELECT * FROM asset WHERE id = ?", (asset_id,))
        if row is None:
            raise KeyError(f"asset 不存在：{asset_id}")
        return _to_asset(row)

    def by_relative(self, library_id: int, relative: str) -> AssetRecord | None:
        row = self.db.query_one(
            "SELECT * FROM asset WHERE library_id = ? AND relative_path = ?",
            (library_id, relative),
        )
        return _to_asset(row) if row else None

    def count(self, library_id: int | None = None) -> int:
        if library_id is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM asset", default=0))
        return int(
            self.db.scalar("SELECT COUNT(*) FROM asset WHERE library_id = ?", (library_id,), default=0)
        )

    def status_histogram(self, library_id: int | None = None) -> dict[str, int]:
        if library_id is None:
            rows = self.db.query("SELECT status, COUNT(*) AS n FROM asset GROUP BY status")
        else:
            rows = self.db.query(
                "SELECT status, COUNT(*) AS n FROM asset WHERE library_id = ? GROUP BY status",
                (library_id,),
            )
        return {str(r["status"]): int(r["n"]) for r in rows}

    def type_histogram(self, library_id: int | None = None) -> dict[str, int]:
        if library_id is None:
            rows = self.db.query("SELECT file_type, COUNT(*) AS n FROM asset GROUP BY file_type")
        else:
            rows = self.db.query(
                "SELECT file_type, COUNT(*) AS n FROM asset WHERE library_id = ? GROUP BY file_type",
                (library_id,),
            )
        return {str(r["file_type"]): int(r["n"]) for r in rows}

    def pending(
        self,
        library_id: int,
        *,
        limit: int = 100_000,
        only_unhashed: bool = False,
        scope_prefix: str | None = None,
    ) -> list[AssetRecord]:
        sql = (
            "SELECT * FROM asset WHERE library_id = ? AND status IN ('PENDING','OK')"
            " AND status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
        )
        params: list[Any] = [library_id]
        if only_unhashed:
            sql += " AND (content_hash IS NULL OR content_hash = '')"
        sql, params = _apply_scope(sql, params, scope_prefix)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [_to_asset(r) for r in self.db.query(sql, params)]

    def unhashed(
        self, library_id: int, *, limit: int = 100_000, scope_prefix: str | None = None
    ) -> list[AssetRecord]:
        sql = (
            "SELECT * FROM asset WHERE library_id = ? AND status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
            " AND (content_hash IS NULL OR content_hash = '')"
        )
        params: list[Any] = [library_id]
        sql, params = _apply_scope(sql, params, scope_prefix)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [_to_asset(r) for r in self.db.query(sql, params)]

    def needing_metadata(
        self, library_id: int, *, limit: int = 100_000, scope_prefix: str | None = None
    ) -> list[AssetRecord]:
        sql = (
            "SELECT a.* FROM asset AS a LEFT JOIN metadata AS m ON m.asset_id = a.id"
            " WHERE a.library_id = ? AND a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
            " AND m.asset_id IS NULL"
        )
        params: list[Any] = [library_id]
        sql, params = _apply_scope(sql, params, scope_prefix, column="a.relative_path")
        sql += " ORDER BY a.id LIMIT ?"
        params.append(limit)
        return [_to_asset(r) for r in self.db.query(sql, params)]

    def needing_previews(
        self, library_id: int, *, limit: int = 100_000, scope_prefix: str | None = None
    ) -> list[AssetRecord]:
        sql = (
            "SELECT a.* FROM asset AS a"
            " LEFT JOIN preview AS t ON t.asset_id = a.id AND t.kind = 'thumb'"
            " LEFT JOIN preview AS p ON p.asset_id = a.id AND p.kind = 'preview'"
            " WHERE a.library_id = ? AND a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
            " AND (t.id IS NULL OR p.id IS NULL)"
        )
        params: list[Any] = [library_id]
        sql, params = _apply_scope(sql, params, scope_prefix, column="a.relative_path")
        sql += " ORDER BY a.id LIMIT ?"
        params.append(limit)
        return [_to_asset(r) for r in self.db.query(sql, params)]

    def total_bytes(self, library_id: int | None = None) -> int:
        if library_id is None:
            return int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM asset", default=0))
        return int(
            self.db.scalar(
                "SELECT COALESCE(SUM(size),0) FROM asset WHERE library_id = ?", (library_id,), default=0
            )
        )

    # ------------------------------------------------------------------ 去重
    def duplicate_groups(self, library_id: int, *, limit: int = 500) -> list[tuple[str, list[AssetRecord]]]:
        """按内容 hash 分组，返回有多个成员的组（PRD §9.1 基于 Hash 去重）。

        **只报告，不自动隔离、不删除** —— 破坏性操作必须由用户显式确认（PRD §11.1）。
        """
        rows = self.db.query(
            "SELECT content_hash, COUNT(*) AS n FROM asset WHERE library_id = ?"
            " AND content_hash IS NOT NULL AND content_hash != ''"
            " AND status NOT IN ('QUARANTINED','SKIPPED')"
            " GROUP BY content_hash HAVING n > 1 ORDER BY n DESC, content_hash LIMIT ?",
            (library_id, limit),
        )
        groups: list[tuple[str, list[AssetRecord]]] = []
        for row in rows:
            digest = str(row["content_hash"])
            members = self.db.query(
                "SELECT * FROM asset WHERE library_id = ? AND content_hash = ? ORDER BY relative_path",
                (library_id, digest),
            )
            groups.append((digest, [_to_asset(m) for m in members]))
        return groups


# --------------------------------------------------------------------------- Metadata
class MetadataRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["extracted_at"] = utc_now()
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{name} = excluded.{name}" for name in row if name != "asset_id")
        with self.db.write() as conn:
            conn.execute(
                f"INSERT INTO metadata ({columns}) VALUES ({placeholders})"
                f" ON CONFLICT(asset_id) DO UPDATE SET {updates}",
                tuple(row.values()),
            )

    def get(self, asset_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM metadata WHERE asset_id = ?", (asset_id,))

    def count(self, library_id: int | None = None) -> int:
        if library_id is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM metadata", default=0))
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
                " WHERE a.library_id = ?",
                (library_id,),
                default=0,
            )
        )

    def capture_time_coverage(self, library_id: int) -> tuple[int, int]:
        """(有拍摄时间的数量, 有元数据的总数)。

        **同时统计 `capture_at` 与 `capture_at_utc`**：照片有本地时间，
        视频只有容器给的精确 UTC（本地时区未知，见 media/video.py 的时区纪律）。
        两者都算"知道拍摄时间"，否则视频会永远显示成 0% 覆盖。
        """
        total = self.count(library_id)
        with_time = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
                " WHERE a.library_id = ? AND (m.capture_at IS NOT NULL OR m.capture_at_utc IS NOT NULL)",
                (library_id,),
                default=0,
            )
        )
        return with_time, total

    def time_quality(self, library_id: int) -> dict[str, int]:
        """拍摄时间的质量分布，供断点报告与 UI 展示"数据可信度"。"""
        base = "FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id WHERE a.library_id = ?"
        return {
            "total": self.count(library_id),
            "any_time": int(
                self.db.scalar(
                    "SELECT COUNT(*) " + base
                    + " AND (m.capture_at IS NOT NULL OR m.capture_at_utc IS NOT NULL)",
                    (library_id,),
                    default=0,
                )
            ),
            "local_time": int(
                self.db.scalar(
                    "SELECT COUNT(*) " + base + " AND m.capture_at IS NOT NULL",
                    (library_id,),
                    default=0,
                )
            ),
            "utc_only": int(
                self.db.scalar(
                    "SELECT COUNT(*) " + base
                    + " AND m.capture_at IS NULL AND m.capture_at_utc IS NOT NULL",
                    (library_id,),
                    default=0,
                )
            ),
            "tz_known": int(
                self.db.scalar(
                    "SELECT COUNT(*) " + base + " AND m.tz_offset IS NOT NULL",
                    (library_id,),
                    default=0,
                )
            ),
            "no_time": int(
                self.db.scalar(
                    "SELECT COUNT(*) " + base
                    + " AND m.capture_at IS NULL AND m.capture_at_utc IS NULL",
                    (library_id,),
                    default=0,
                )
            ),
        }

    def camera_histogram(self, library_id: int) -> list[tuple[str, int]]:
        rows = self.db.query(
            "SELECT COALESCE(m.camera_model, '(未知)') AS model, COUNT(*) AS n"
            " FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE a.library_id = ? GROUP BY model ORDER BY n DESC",
            (library_id,),
        )
        return [(str(r["model"]), int(r["n"])) for r in rows]


# --------------------------------------------------------------------------- Preview
class PreviewRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        asset_id: int,
        *,
        thumb: tuple[str, int, int, int],
        preview: tuple[str, int, int, int],
        generator_version: str,
    ) -> None:
        """写入/更新缩略图与预览记录。`thumb`/`preview` = (path, width, height, bytes)。"""
        stamp = utc_now()
        rows = [
            (asset_id, "thumb", *thumb, generator_version, stamp),
            (asset_id, "preview", *preview, generator_version, stamp),
        ]
        with self.db.write() as conn:
            conn.executemany(
                "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
                " generator_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(asset_id, kind) DO UPDATE SET path = excluded.path,"
                " width = excluded.width, height = excluded.height, bytes = excluded.bytes,"
                " generator_version = excluded.generator_version, created_at = excluded.created_at",
                rows,
            )

    def get(self, asset_id: int, kind: str = "thumb") -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM preview WHERE asset_id = ? AND kind = ?", (asset_id, kind)
        )

    def count(self, kind: str | None = None, library_id: int | None = None) -> int:
        """统计 preview 行数。**按 Library 统计时必须 join asset** ——
        否则会把所有库的预览都算进来（真实现场：视频库显示照片库的预览数）。"""
        sql = "SELECT COUNT(*) FROM preview"
        params: list[Any] = []
        joins = ""
        where: list[str] = []
        if library_id is not None:
            joins = " JOIN asset AS a ON a.id = preview.asset_id"
            where.append("a.library_id = ?")
            params.append(library_id)
        if kind is not None:
            where.append("preview.kind = ?")
            params.append(kind)
        if where:
            sql += joins + " WHERE " + " AND ".join(where)
        return int(self.db.scalar(sql, params, default=0))

    def total_bytes(self, library_id: int | None = None) -> int:
        if library_id is None:
            return int(self.db.scalar("SELECT COALESCE(SUM(bytes),0) FROM preview", default=0))
        return int(
            self.db.scalar(
                "SELECT COALESCE(SUM(p.bytes),0) FROM preview AS p"
                " JOIN asset AS a ON a.id = p.asset_id WHERE a.library_id = ?",
                (library_id,),
                default=0,
            )
        )

    def generator_versions(self, library_id: int | None = None) -> dict[str, int]:
        if library_id is None:
            rows = self.db.query(
                "SELECT generator_version, COUNT(*) AS n FROM preview GROUP BY generator_version"
            )
        else:
            rows = self.db.query(
                "SELECT p.generator_version AS generator_version, COUNT(*) AS n FROM preview AS p"
                " JOIN asset AS a ON a.id = p.asset_id WHERE a.library_id = ?"
                " GROUP BY p.generator_version",
                (library_id,),
            )
        return {str(r["generator_version"]): int(r["n"]) for r in rows}

    def sample_with_preview(
        self, kind: str = "thumb", *, library_id: int | None = None
    ) -> sqlite3.Row | None:
        """取一条**确实有该类型预览记录**的样例（用于健康检查）。

        不能用"随便取一个 asset"来检查缩略图：库通常是增量索引的，
        很大一部分 asset 合法地还没有预览，随机取样会误报失败。
        """
        sql = "SELECT * FROM preview WHERE kind = ?"
        params: list[Any] = [kind]
        if library_id is not None:
            sql = (
                "SELECT p.* FROM preview AS p JOIN asset AS a ON a.id = p.asset_id"
                " WHERE p.kind = ? AND a.library_id = ?"
            )
            params.append(library_id)
        sql += " ORDER BY id LIMIT 1"
        return self.db.query_one(sql, params)

    def broken_records(
        self, data_root: Path, library_id: int | None = None, *, limit: int = 5000
    ) -> list[tuple[int, str, str]]:
        """找出**数据库有记录但磁盘文件已不存在**的预览。

        为什么需要它：`record()` 只写数据库，删除磁盘上的 thumbnails/previews 目录
        （清理缓存、迁移包未包含、误删）不会让记录消失，于是
        `needing_previews` 会认为"已生成"而永远不重算 —— 界面就一直是空白缩略图。
        因此必须能主动对账，并据此触发重建。

        **只统计原文件可访问的资产**：照片库离线时原片与预览都不可读，
        那不是"派生数据损坏"，不应被当成不一致（PRD §9.1：离线时索引仍应可用）。

        返回 `[(asset_id, kind, path), ...]`。
        """
        sql = (
            "SELECT p.asset_id, p.kind, p.path FROM preview AS p"
            " JOIN asset AS a ON a.id = p.asset_id"
            " WHERE a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
        )
        params: list[Any] = []
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(library_id)
        sql += " ORDER BY p.asset_id LIMIT ?"
        params.append(int(limit))

        data_root = Path(data_root)
        broken: list[tuple[int, str, str]] = []
        for row in self.db.query(sql, params):
            relative = str(row["path"])
            if not (data_root / relative).is_file():
                broken.append((int(row["asset_id"]), str(row["kind"]), relative))
        return broken

    def clear_for_assets(self, asset_ids: list[int]) -> int:
        """删除指定 asset 的预览记录（用于触发重建）。只动数据库，不动任何原片。"""
        if not asset_ids:
            return 0
        removed = 0
        with self.db.write() as conn:
            for chunk_start in range(0, len(asset_ids), 400):
                chunk = asset_ids[chunk_start : chunk_start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"DELETE FROM preview WHERE asset_id IN ({placeholders})", chunk
                )
                removed += cursor.rowcount or 0
        return removed


# --------------------------------------------------------------------------- 映射
def _apply_scope(
    sql: str, params: list[Any], scope_prefix: str | None, *, column: str = "relative_path"
) -> tuple[str, list[Any]]:
    """把"只处理某个子树"的条件加进查询（断点测试的分层策略需要）。

    `scope_prefix` 是相对路径前缀（例如 `杭州市\\2025-10-25_1000-1100`）。
    转义规则见 `library.paths.like_prefix_pattern`（用 `!` 做 ESCAPE 字符）。
    """
    if not scope_prefix:
        return sql, params
    from ..library.paths import like_prefix_pattern

    return sql + f" AND {column} LIKE ? ESCAPE '!'", [*params, like_prefix_pattern(scope_prefix)]


class CollectionRepo:
    """`collection` / `collection_item` 的读写（Phase 2 · M7，PRD §9.2.1）。

    设计要点：

      * **与物理目录完全无关**：只引用 `shot_id`，**不存任何路径**（铁律 #2 自动满足）。
      * **加入/移除只改数据库关系**：本类不产生任何文件系统调用（铁律 #1/#5）。
      * **一张最终精选可属多个主题**：主键是 `(collection_id, shot_id)`。
      * **封面三级回退**：自定义 → 主题内排序最前的一条 → 无（前端占位图）。
        三级都走**本地 `thumb` 缓存**，因此拔盘后主题列表与封面仍可用（断点 #7）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读
    def list_all(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """主题列表（含条目数与**可用的封面 shot_id**）。

        ★ `items` 数的是**用户看得见**的条目（`retired_at IS NULL`），不是表里的行数。

        为什么必须这样（实测到的缺陷）：`items()` 详情查询带
        `JOIN shot sh ON sh.id = ci.shot_id AND sh.retired_at IS NULL`，
        而这里原本数的是 `COUNT(*) FROM collection_item` —— 一个过滤、一个不过滤。
        于是 Shot 重算之后**列表页显示 120、详情页只有 102**，用户会以为丢了东西。
        两处口径必须一致，而且应当是"看得见的那个口径"。
        """
        rows = self.db.query(
            "SELECT c.id, c.uuid, c.name, c.description, c.cover_shot_id, c.sort_order,"
            "       c.parent_id, c.created_at, c.updated_at, c.source_json,"
            "       (SELECT COUNT(*) FROM collection_item ci"
            "          JOIN shot sh ON sh.id = ci.shot_id AND sh.retired_at IS NULL"
            "         WHERE ci.collection_id = c.id) AS items,"
            "       (SELECT COUNT(*) FROM collection ch WHERE ch.parent_id = c.id) AS children,"
            "       COALESCE(c.cover_shot_id,"
            "                (SELECT ci2.shot_id FROM collection_item ci2"
            "                   JOIN shot sh2 ON sh2.id = ci2.shot_id AND sh2.retired_at IS NULL"
            "                  WHERE ci2.collection_id = c.id"
            "                  ORDER BY ci2.sort_order, ci2.shot_id LIMIT 1)) AS effective_cover"
            "  FROM collection c"
            " ORDER BY c.sort_order, c.id LIMIT ?",
            (int(limit),),
        )
        return [
            {
                "id": int(r["id"]),
                "uuid": r["uuid"],
                "name": r["name"],
                "description": r["description"],
                "sort_order": int(r["sort_order"]),
                "parent_id": None if r["parent_id"] is None else int(r["parent_id"]),
                "children": int(r["children"]),
                "items": int(r["items"]),
                "cover_shot_id": r["cover_shot_id"],
                # 三级回退的最终结果（前端的封面取这张 Shot 的 primary asset 的 thumb）
                "effective_cover_shot_id": r["effective_cover"],
                # 主题的来源（来自哪个词 / 哪个人 / 哪次筛选）—— 解析失败不报错，
                # 因为来源只是解释性信息，不该让主题列表打不开。
                "source": _load_source(r["source_json"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def get(self, collection_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM collection WHERE id = ?", (int(collection_id),))

    def get_by_uuid(self, uuid: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM collection WHERE uuid = ?", (str(uuid),))

    def get_by_name(self, name: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM collection WHERE name = ?", (str(name),))

    def items(self, collection_id: int, *, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """主题内的作品（按主题内排序）。"""
        rows = self.db.query(
            "SELECT ci.shot_id, ci.sort_order, ci.note, ci.added_at,"
            "       sh.primary_asset_id, sh.captured_at, sh.group_source,"
            "       COALESCE(sel.status, 'UNREVIEWED') AS selection_status,"
            "       a.relative_path, a.file_type,"
            "       CASE WHEN p.id IS NULL THEN 0 ELSE 1 END AS has_thumb"
            "  FROM collection_item ci"
            "  JOIN shot sh ON sh.id = ci.shot_id AND sh.retired_at IS NULL"
            "  LEFT JOIN selection sel ON sel.shot_id = sh.id"
            "  LEFT JOIN asset a ON a.id = sh.primary_asset_id"
            "  LEFT JOIN preview p ON p.asset_id = a.id AND p.kind = 'thumb'"
            " WHERE ci.collection_id = ?"
            " ORDER BY ci.sort_order, ci.added_at, ci.shot_id LIMIT ? OFFSET ?",
            (int(collection_id), int(limit), int(offset)),
        )
        return [
            {
                "shot_id": int(r["shot_id"]),
                "sort_order": int(r["sort_order"]),
                "note": r["note"],
                "added_at": r["added_at"],
                "primary_asset_id": r["primary_asset_id"],
                "captured_at": r["captured_at"],
                "selection_status": r["selection_status"],
                "relative_path": r["relative_path"],
                "file_type": r["file_type"],
                "has_thumb": bool(r["has_thumb"]),
            }
            for r in rows
        ]

    def children(self, collection_id: int) -> list[dict[str, Any]]:
        """直接子主题（父主题详情里的「统合目录」区块）。

        每个子主题只给「封面 + 名字 + 条目数」三样，够画一张可点开的目录卡。
        条目数口径与 `list_all` 一致：数的是**用户看得见**的条目（`retired_at IS NULL`）。
        """
        rows = self.db.query(
            "SELECT c.id, c.name, c.description, c.cover_shot_id,"
            "       (SELECT COUNT(*) FROM collection_item ci"
            "          JOIN shot sh ON sh.id = ci.shot_id AND sh.retired_at IS NULL"
            "         WHERE ci.collection_id = c.id) AS items,"
            "       COALESCE(c.cover_shot_id,"
            "                (SELECT ci2.shot_id FROM collection_item ci2"
            "                   JOIN shot sh2 ON sh2.id = ci2.shot_id AND sh2.retired_at IS NULL"
            "                  WHERE ci2.collection_id = c.id"
            "                  ORDER BY ci2.sort_order, ci2.shot_id LIMIT 1)) AS effective_cover"
            "  FROM collection c"
            " WHERE c.parent_id = ?"
            " ORDER BY c.sort_order, c.id",
            (int(collection_id),),
        )
        return [
            {
                "id": int(r["id"]),
                "name": r["name"],
                "description": r["description"],
                "items": int(r["items"]),
                "cover_shot_id": r["cover_shot_id"],
                "effective_cover_shot_id": r["effective_cover"],
            }
            for r in rows
        ]

    def _would_create_cycle(self, collection_id: int, new_parent_id: int | None) -> bool:
        """把 `collection_id` 挂到 `new_parent_id` 下是否会成环。

        判据：从 `new_parent_id` 沿 parent_id 一路向上，若碰到 `collection_id`
        （即 `collection_id` 是 `new_parent_id` 的祖先）或 `new_parent_id == collection_id`，
        就成环。深度防御：途中若撞进一个既有的环（本不该出现），也一律当环处理。
        """
        if new_parent_id is None:
            return False
        current: int | None = int(new_parent_id)
        seen: set[int] = set()
        while current is not None:
            if current in seen or current == int(collection_id):
                return True
            seen.add(current)
            row = self.db.query_one("SELECT parent_id FROM collection WHERE id = ?", (current,))
            if row is None or row["parent_id"] is None:
                return False
            current = int(row["parent_id"])
        return False

    def _validate_parent(self, parent_id: int | None) -> None:
        if parent_id is None:
            return
        if self.get(int(parent_id)) is None:
            raise ValueError(f"上级主题 {parent_id} 不存在")

    def collections_of_shot(self, shot_id: int) -> list[dict[str, Any]]:
        """反查"这张作品属于哪些主题"（详情抽屉要用；PRD 允许一张属多个主题）。"""
        rows = self.db.query(
            "SELECT c.id, c.name, ci.sort_order"
            "  FROM collection_item ci JOIN collection c ON c.id = ci.collection_id"
            " WHERE ci.shot_id = ? ORDER BY c.sort_order, c.id",
            (int(shot_id),),
        )
        return [{"id": int(r["id"]), "name": r["name"], "sort_order": int(r["sort_order"])} for r in rows]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM collection", default=0))

    def audit_scene_ids(self, collection_id: int) -> list[int]:
        """该主题覆盖到的 Scene id（"跨多个 Session"的可视化证据）。"""
        rows = self.db.query(
            "SELECT DISTINCT sh.scene_id FROM collection_item ci"
            "  JOIN shot sh ON sh.id = ci.shot_id"
            " WHERE ci.collection_id = ? AND sh.scene_id IS NOT NULL",
            (int(collection_id),),
        )
        return [int(r["scene_id"]) for r in rows]

    # ------------------------------------------------------------------ 写
    def create(
        self,
        name: str,
        *,
        description: str | None = None,
        cover_shot_id: int | None = None,
        sort_order: int = 0,
        uuid: str | None = None,
        source: dict[str, Any] | None = None,
        parent_id: int | None = None,
    ) -> int:
        """新建主题。返回 id。

        `uuid` 是**跨机迁移的稳定身份**（与 `library.uuid` 同思路）——
        没有它，PC-B 上导入迁移包后主题的 id 会被重新分配，逐项校验就失去基准。
        不传时自动生成。

        `source` 记录**这个主题是怎么来的**（用户的工作流是"精选 → 按关键词或
        时间地点建主题"，所以主题必须能回答"我是从哪个词/哪个人来的"）。
        形状见迁移 008 末尾。

        `parent_id` 让新主题直接成为某个主题的子主题（上下级关系）。
        新建不可能成环（新行还没有 id），只需确认父主题存在。
        """
        from ..db import utc_now

        if not str(name).strip():
            raise ValueError("主题名称不能为空")
        self._validate_parent(parent_id)
        stamp = utc_now()
        identity = uuid or __import__("uuid").uuid4().hex
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO collection (uuid, name, description, cover_shot_id, sort_order,"
                " parent_id, source_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    str(name).strip(),
                    description,
                    cover_shot_id,
                    int(sort_order),
                    None if parent_id is None else int(parent_id),
                    None if source is None else json.dumps(source, ensure_ascii=False),
                    stamp,
                    stamp,
                ),
            )
            return int(cursor.lastrowid)

    def set_source(self, collection_id: int, source: dict[str, Any] | None) -> None:
        """记/改主题的来源（`list_all`/`get` 会把它解回对象返回）。"""
        self.db.execute(
            "UPDATE collection SET source_json = ?, updated_at = ? WHERE id = ?",
            (None if source is None else json.dumps(source, ensure_ascii=False),
             utc_now(), int(collection_id)),
        )

    def update(
        self,
        collection_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        cover_shot_id: int | None = None,
        clear_cover: bool = False,
        sort_order: int | None = None,
        parent_id: int | None = None,
        clear_parent: bool = False,
    ) -> bool:
        """改名称/描述/封面/排序/上级主题。只更新传入的字段。"""
        from ..db import utc_now

        fields: list[str] = []
        params: list[Any] = []
        if name is not None:
            if not str(name).strip():
                raise ValueError("主题名称不能为空")
            fields.append("name = ?")
            params.append(str(name).strip())
        if description is not None:
            fields.append("description = ?")
            params.append(description)
        if clear_cover:
            fields.append("cover_shot_id = NULL")
        elif cover_shot_id is not None:
            fields.append("cover_shot_id = ?")
            params.append(int(cover_shot_id))
        if sort_order is not None:
            fields.append("sort_order = ?")
            params.append(int(sort_order))
        if clear_parent:
            fields.append("parent_id = NULL")
        elif parent_id is not None:
            self._validate_parent(parent_id)
            if self._would_create_cycle(collection_id, parent_id):
                raise ValueError("不能把主题挂到它自己或它的子主题下面（会形成环）")
            fields.append("parent_id = ?")
            params.append(int(parent_id))
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(utc_now())
        params.append(int(collection_id))
        with self.db.write() as conn:
            cursor = conn.execute(
                f"UPDATE collection SET {', '.join(fields)} WHERE id = ?", params
            )
        return bool(cursor.rowcount)

    def delete(self, collection_id: int) -> bool:
        """删除**主题本身**（`collection_item` 由外键级联删除）。

        ⚠️ 这只删数据库关系，**不碰任何照片文件**（铁律 #5：删除数据库记录 ≠ 删除原文件）。
        """
        with self.db.write() as conn:
            cursor = conn.execute("DELETE FROM collection WHERE id = ?", (int(collection_id),))
        return bool(cursor.rowcount)

    def add_shots(
        self, collection_id: int, shot_ids: Iterable[int], *, note: str | None = None
    ) -> int:
        """把作品加进主题（幂等：已在主题里的不重复插入）。返回**新增**条数。"""
        from ..db import utc_now

        if self.get(collection_id) is None:
            raise ValueError(f"主题 {collection_id} 不存在")
        stamp = utc_now()
        added = 0
        with self.db.write() as conn:
            next_order = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM collection_item"
                    " WHERE collection_id = ?",
                    (int(collection_id),),
                ).fetchone()[0]
            )
            for offset, shot_id in enumerate(shot_ids):
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO collection_item"
                    " (collection_id, shot_id, sort_order, note, added_at) VALUES (?, ?, ?, ?, ?)",
                    (int(collection_id), int(shot_id), next_order + offset, note, stamp),
                )
                added += cursor.rowcount or 0
        return added

    def remove_shots(self, collection_id: int, shot_ids: Iterable[int]) -> int:
        """从主题移除作品（**只删关系**）。返回移除条数。"""
        removed = 0
        with self.db.write() as conn:
            for shot_id in shot_ids:
                cursor = conn.execute(
                    "DELETE FROM collection_item WHERE collection_id = ? AND shot_id = ?",
                    (int(collection_id), int(shot_id)),
                )
                removed += cursor.rowcount or 0
        return removed

    def reorder(self, collection_id: int, ordered_shot_ids: Sequence[int]) -> int:
        """按给定顺序重排主题内的作品。返回更新条数。"""
        updated = 0
        with self.db.write() as conn:
            for position, shot_id in enumerate(ordered_shot_ids):
                cursor = conn.execute(
                    "UPDATE collection_item SET sort_order = ? WHERE collection_id = ? AND shot_id = ?",
                    (position, int(collection_id), int(shot_id)),
                )
                updated += cursor.rowcount or 0
        return updated

    def set_note(self, collection_id: int, shot_id: int, note: str | None) -> bool:
        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE collection_item SET note = ? WHERE collection_id = ? AND shot_id = ?",
                (note, int(collection_id), int(shot_id)),
            )
        return bool(cursor.rowcount)


class SelectionRepo:
    """`selection` / `selection_event` 的读写（Phase 2 · M5）。

    设计要点（PHASE2_TECH_PLAN §7）：

      * **状态机在 `library/selection.py`**（纯逻辑），本类只负责落库 ——
        这样"随时可退回"这类产品规则不会被 SQL 细节淹没。
      * **每次变更都写一行 `selection_event`**：PRD §9.2 要求
        「记录选择过程：用户选了什么、放弃了什么、最终留下什么」。
        `selection` 表只保存**当前状态**，历史全在事件表里。
      * **状态没变且没给理由 → 不写库**（连按同一个键不该刷出满屏事件）；
        状态没变但补了理由 → 仍要记录（那是用户的真实操作）。
      * **本类不产生任何文件系统调用** —— 精选只是数据库状态/关系（PRD 第 137 行）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读
    def get(self, shot_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM selection WHERE shot_id = ?", (int(shot_id),))

    def status_of(self, shot_id: int) -> str:
        """某 Shot 的当前状态；**没有行 = 从未审阅**（Phase 1 建表时的语义）。"""
        from ..library import selection as sel

        row = self.get(shot_id)
        return sel.normalize_status(row["status"] if row else None)

    def status_map(self, shot_ids: Iterable[int]) -> dict[int, str]:
        """批量取状态（列表页要一次拿到整页，避免 N+1 查询）。"""
        ids = [int(i) for i in shot_ids]
        out = {i: "UNREVIEWED" for i in ids}
        if not ids:
            return out
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start : chunk_start + 400]
            placeholders = ", ".join("?" for _ in chunk)
            for row in self.db.query(
                f"SELECT shot_id, status FROM selection WHERE shot_id IN ({placeholders})", chunk
            ):
                out[int(row["shot_id"])] = str(row["status"])
        return out

    def counts(self, *, scene_id: int | None = None) -> dict[str, int]:
        """各状态各多少张。`scene_id` 给定时只统计该 Scene。"""
        from ..library import selection as sel

        sql = (
            "SELECT COALESCE(s.status, 'UNREVIEWED') AS status, COUNT(*) AS n"
            "  FROM shot sh LEFT JOIN selection s ON s.shot_id = sh.id"
        )
        params: list[Any] = []
        if scene_id is not None:
            sql += " WHERE sh.scene_id = ? AND sh.retired_at IS NULL"
            params.append(int(scene_id))
        out = {status: 0 for status in sel.STATUSES}
        for row in self.db.query(sql + " GROUP BY 1", params):
            key = str(row["status"])
            if key in out:
                out[key] = int(row["n"])
        return out

    def history(self, shot_id: int, *, limit: int = 100) -> list[sqlite3.Row]:
        """某个 Shot 的选择过程时间线（断点 #8 的反查要用）。"""
        return self.db.query(
            "SELECT * FROM selection_event WHERE shot_id = ? ORDER BY created_at, id LIMIT ?",
            (int(shot_id), int(limit)),
        )

    def abandoned(self, *, scene_id: int | None = None, limit: int = 500) -> list[sqlite3.Row]:
        """**被放弃了什么** —— 曾经进入候选/精选但后来退回 UNREVIEWED 的 Shot。

        PRD §9.2 要求"记录用户放弃了什么"。回退操作会留下
        `to_status='UNREVIEWED'` 且 `from_status IN (CANDIDATE, SELECTED)` 的事件，
        这就是"放弃"的定义。只保留每个 Shot 最近一次这样的记录。
        """
        from ..library import selection as sel

        reviewed = ", ".join(f"'{s}'" for s in sel.REVIEWED_STATUSES)
        sql = (
            "SELECT e.*, sh.scene_id FROM selection_event e"
            "  JOIN shot sh ON sh.id = e.shot_id"
            f" WHERE e.to_status = '{sel.UNREVIEWED}' AND e.from_status IN ({reviewed})"
        )
        params: list[Any] = []
        if scene_id is not None:
            sql += " AND sh.scene_id = ?"
            params.append(int(scene_id))
        sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, params)

    def finals(self, *, limit: int = 1000, offset: int = 0) -> list[sqlite3.Row]:
        """最终精选列表（精选视图的基础查询）。"""
        from ..library import selection as sel

        return self.db.query(
            "SELECT a.id AS asset_id, a.relative_path, a.library_id, m.capture_at,"
            "       m.camera_model, m.lens_model, s.status AS selection_status,"
            "       s.reason, s.updated_at AS selected_at"
            "  FROM selection s"
            "  JOIN shot sh ON sh.id = s.shot_id AND sh.retired_at IS NULL"
            "  LEFT JOIN asset a ON a.id = sh.primary_asset_id"
            "  LEFT JOIN metadata m ON m.asset_id = a.id"
            " WHERE s.status = ?"
            " ORDER BY m.capture_at DESC, a.id"
            " LIMIT ? OFFSET ?",
            (sel.FINAL, int(limit), int(offset)),
        )

    # ------------------------------------------------------------------ 写
    def set_status(
        self, shot_id: int, target_status: str, *, reason: str | None = None
    ) -> bool:
        """设置某个 Shot 的精选状态，并记录事件。返回**是否真的产生了变更**。

        行为（与 `library.selection.plan_transition` 一致）：
          * 状态变了 → 更新 `selection` + 写一条 `selection_event`
          * 状态没变但给了理由 → 只更新理由 + 写一条事件（理由变更也是操作）
          * 状态没变也没理由 → 什么都不做，返回 False

        ⚠️ **不接受已退役的 Shot**（抛 `ValueError`）。这与 API 层的行为一致
        （`POST /api/shots/{id}/selection` 对退役 Shot 返回 409），
        但仓库层原本没有这层保护 —— 而 CLI 与并发写入走的是仓库层。

        为什么必须拒绝：写到退役行上的精选**用户看不见**（所有查询都带
        `retired_at IS NULL`），于是"标了但没生效"会静默发生。
        宁可让调用方拿到明确错误，也不要产生一条看不见的决定。
        """
        from ..library import selection as sel
        from ..db import utc_now

        row = self.db.query_one("SELECT retired_at FROM shot WHERE id = ?", (int(shot_id),))
        if row is None:
            raise ValueError(f"Shot {shot_id} 不存在，不能标记精选")
        if row["retired_at"] is not None:
            raise ValueError(
                f"Shot {shot_id} 已因分组重算而退役，不能标记精选 ——"
                "写到退役行上的决定用户看不见。请对当前分组下的 Shot 操作。"
            )

        transition = sel.plan_transition(shot_id, self.status_of(shot_id), target_status, reason)
        if not transition.changed:
            return False

        stamp = utc_now()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO selection (shot_id, status, reason, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(shot_id) DO UPDATE SET"
                "   status = excluded.status,"
                "   reason = COALESCE(excluded.reason, selection.reason),"
                "   updated_at = excluded.updated_at",
                (int(shot_id), transition.to_status, transition.reason, stamp),
            )
            conn.execute(
                "INSERT INTO selection_event (shot_id, from_status, to_status, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    int(shot_id),
                    transition.from_status,
                    transition.to_status,
                    transition.reason,
                    stamp,
                ),
            )
        return True

    def set_many(
        self, shot_ids: Iterable[int], target_status: str, *, reason: str | None = None
    ) -> int:
        """批量设置（工作台里"整组标为候选"要用）。返回实际变更的条数。"""
        changed = 0
        for shot_id in shot_ids:
            if self.set_status(int(shot_id), target_status, reason=reason):
                changed += 1
        return changed

    def clear(self, shot_id: int) -> bool:
        """退回"未审阅"（PRD：允许随时退回）。"""
        from ..library import selection as sel

        return self.set_status(shot_id, sel.UNREVIEWED)

    def set_reason(self, shot_id: int, reason: str | None) -> bool:
        """只改理由、不动状态。"""
        return self.set_status(shot_id, self.status_of(shot_id), reason=reason)

    # ------------------------------------------------------------------ 不变式
    def total(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM selection", default=0))

    def final_count(self) -> int:
        from ..library import selection as sel

        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM selection WHERE status = ?", (sel.FINAL,), default=0
            )
        )


class EmbeddingRepo:
    """`embedding` 表的读写（Phase 2 · M3 落库）。

    ## 本类最重要的约束：多版本并存，绝不覆盖历史（铁律 #4 / PRD §9.3）

    一行的身份是 `(asset_id, model, model_version, prep_version)` 四元组。
    其中任何一项变化（模型升级、预处理口径变化）都**新增一行**，绝不 UPDATE 旧行 ——
    PRD §9.3 明文要求「模型升级后可以重新分析，**不覆盖历史结果**」。

    `input_version`（= preview 的 `generator_version`）不参与身份四元组，
    而是**随行记录**：输入图变了意味着缓存失效，调用方应当换一个 `prep_version`
    或显式 `--rebuild`，本类只负责如实存储当时的输入身份。

    本类不产生任何文件系统调用（唯一例外是注入的 `vector_store` 读 preview 图像 ——
    那是只读的）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读
    def get(
        self, asset_id: int, *, model: str, model_version: str, prep_version: str
    ) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM embedding WHERE asset_id = ? AND model = ? AND model_version = ?"
            " AND prep_version = ?",
            (int(asset_id), model, model_version, prep_version),
        )

    def count(
        self, *, model: str | None = None, model_version: str | None = None,
        prep_version: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if model:
            clauses.append("model = ?")
            params.append(model)
        if model_version:
            clauses.append("model_version = ?")
            params.append(model_version)
        if prep_version:
            clauses.append("prep_version = ?")
            params.append(prep_version)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM embedding{where}", params, default=0))

    def identities(self) -> list[dict[str, Any]]:
        """库内出现过的全部 (model, model_version, prep_version) 组合及其计数。

        用途：`ai doctor` / `embed --dry-run` 报告"当前库里有几个版本的向量"——
        多版本并存是**特性**而不是问题，但用户需要看得见。
        """
        rows = self.db.query(
            "SELECT model, model_version, prep_version, dim, normalization,"
            "       COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at"
            "  FROM embedding GROUP BY model, model_version, prep_version, dim, normalization"
            " ORDER BY last_at DESC"
        )
        return [
            {
                "model": r["model"],
                "model_version": r["model_version"],
                "prep_version": r["prep_version"],
                "dim": int(r["dim"]),
                "normalization": r["normalization"],
                "count": int(r["n"]),
                "first_at": r["first_at"],
                "last_at": r["last_at"],
            }
            for r in rows
        ]

    def missing_assets(
        self,
        *,
        model: str,
        model_version: str,
        prep_version: str,
        library_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """还需要算向量的资产：`status='OK'`、**有 preview**、且该版本尚无向量。

        三个条件缺一不可：
          * 没有 preview 就无法编码（`input_kind='preview'`）
          * `status != 'OK'` 的资产（MISSING/UNREADABLE）不该被反复尝试
          * 已有该版本向量的跳过 —— 这是**增量**的核心，也是"已生成的不重复生成"
        """
        sql = (
            "SELECT a.id, a.library_id, a.relative_path, p.path AS preview_path,"
            "       p.generator_version AS preview_version"
            "  FROM asset a"
            "  JOIN preview p ON p.asset_id = a.id AND p.kind = 'preview'"
            "  LEFT JOIN embedding e ON e.asset_id = a.id AND e.model = ?"
            "       AND e.model_version = ? AND e.prep_version = ?"
            " WHERE a.status = 'OK' AND e.id IS NULL"
        )
        params: list[Any] = [model, model_version, prep_version]
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(int(library_id))
        sql += " ORDER BY a.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [
            {
                "asset_id": int(r["id"]),
                "library_id": int(r["library_id"]),
                "relative_path": r["relative_path"],
                "preview_path": r["preview_path"],
                "preview_version": r["preview_version"],
            }
            for r in self.db.query(sql, params)
        ]

    def load_store(
        self,
        *,
        model: str,
        model_version: str,
        prep_version: str,
        library_id: int | None = None,
        dim: int | None = None,
        fallback_dim: int | None = None,
    ):
        """把某个版本的向量装进 `NumpyVectorStore`（缺库时返回空 store）。

        本库规模（21,400 × 512 × 4 B = 43.7 MB）实测可全放内存，
        因此**不需要任何外部向量库**（PHASE2_RESEARCH §7）。

        ## 维度是怎么定的（Phase 3 多模型并存后新增，`fallback_dim`）

        Phase 3 起 `embedding` 表可以同时存多个模型的向量（CLIP 512 维 / EG2 768 维），
        所以"没有匹配行时用哪个维度建空 store"**不能靠猜** —— 猜错会让一个
        "还没有向量"的情形悄悄变成后面某处的维度不匹配。
        解析顺序：① 显式 `dim` ② 实际行的 `dim` ③ `fallback_dim`
        ④ 该身份**历史行**的 `dim`（同一个模型版本即使当前为空，维度是确定的）
        ⑤ 都没有则报错（不静默）。
        """
        from ..library.vectorstore import NumpyVectorStore, VectorStoreError

        sql = (
            "SELECT e.asset_id, e.vector, e.dim FROM embedding e"
            "  JOIN asset a ON a.id = e.asset_id"
            " WHERE e.model = ? AND e.model_version = ? AND e.prep_version = ?"
        )
        params: list[Any] = [model, model_version, prep_version]
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(int(library_id))
        rows = self.db.query(sql, params)

        resolved = int(dim) if dim else (int(rows[0]["dim"]) if rows else 0)
        if resolved <= 0 and fallback_dim:
            resolved = int(fallback_dim)
        if resolved <= 0:
            # 同一身份的历史行能告诉我们维度（例如"这一版算过、但被删空了"）
            hist = self.db.scalar(
                "SELECT dim FROM embedding WHERE model = ? AND model_version = ?"
                " AND prep_version = ? LIMIT 1",
                (model, model_version, prep_version),
                default=0,
            )
            resolved = int(hist or 0)
        if resolved <= 0:
            raise VectorStoreError(
                f"load_store：库里没有 {model}/{model_version}/{prep_version} 的向量，"
                "且无法确定维度 —— 多模型并存时不许猜（请传 dim 或 fallback_dim）"
            )
        if not rows:
            return NumpyVectorStore(dim=resolved)
        return NumpyVectorStore.from_rows(
            [(int(r["asset_id"]), bytes(r["vector"])) for r in rows], dim=resolved
        )

    # ------------------------------------------------------------------ 写
    def store_many(self, rows: Iterable[dict[str, Any]]) -> int:
        """批量写入向量。**已存在同身份的行会被忽略**（不覆盖）。

        为什么用 `INSERT OR IGNORE` 而不是 `ON CONFLICT DO UPDATE`：
        覆盖就等于"模型升级污染历史结果"，正是 PRD §9.3 禁止的。
        要重算请换 `prep_version` 或 `model_version` —— 那是**新的一行**。
        """
        payload = [
            (
                int(r["asset_id"]),
                str(r["model"]),
                str(r["model_version"]),
                r.get("model_sha256"),
                int(r["dim"]),
                str(r["normalization"]),
                str(r["input_kind"]),
                str(r["input_version"]),
                str(r["prep_version"]),
                bytes(r["vector"]),
                r.get("created_at") or utc_now(),
            )
            for r in rows
        ]
        if not payload:
            return 0
        written = 0
        with self.db.write() as conn:
            for start in range(0, len(payload), 200):
                chunk = payload[start : start + 200]
                placeholders = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO embedding (asset_id, model, model_version,"
                    " model_sha256, dim, normalization, input_kind, input_version,"
                    " prep_version, vector, created_at) VALUES " + placeholders,
                    [value for row in chunk for value in row],
                )
                written += cursor.rowcount or 0
        return written

    def delete_identity(self, *, model: str, model_version: str, prep_version: str) -> int:
        """删掉**某一个版本**的全部向量（回退用）。

        ⚠️ 这是数据库行的删除，**不涉及任何照片文件**；而且只删指定版本，
        其他版本的向量原样保留（这正是多版本并存的意义）。
        """
        with self.db.write() as conn:
            cursor = conn.execute(
                "DELETE FROM embedding WHERE model = ? AND model_version = ? AND prep_version = ?",
                (model, model_version, prep_version),
            )
        return cursor.rowcount or 0


class SemanticRepo:
    """`semantic_query` / `semantic_query_hit` 的读写（Phase 3 · 文字搜索，迁移 007）。

    ## 架构位置（「方案 a」的关键一环）

    文本塔要 PyTorch，而产品 venv（跑 `serve` 的那个）**故意不装它**。
    所以分工是：

    * **写**（CLI，在装了 torch 的 venv 里）：算文本向量 → 落 `semantic_query`，
      同时跑一遍检索 → 落 `semantic_query_hit`
    * **读**（界面，产品 venv）：只读这两张表，**一次点积都不用做**

    ## 快照会过期，读取端必须过滤退役 Shot

    重算分组之后，老的 hit 行可能指向**已退役**的 Shot。这与 `selection` /
    `collection_item` 对 shot 的处理是同一套语义：查询端一律 `retired_at IS NULL`。
    本项目在这一族问题上连出过六处缺陷（PHASE2_DELIVERY §6.7–§6.12），
    所以这里从一开始就把过滤写进 SQL，而不是留给调用方记得。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 写
    def save_hits(
        self,
        *,
        query_text: str,
        prompt_text: str | None,
        text_kind: str,
        model: str,
        model_version: str,
        dim: int,
        normalization: str,
        vector: bytes,
        hits: list[tuple[int, int, float, bool]],
        concept_ratio: float | None = None,
    ) -> int:
        """写入/更新一个查询及其命中。**同一个词重复搜索是幂等的 UPDATE**。

        `hits` 是 `(shot_id, rank, score, is_primary_asset)` 列表。
        返回**实际写入的命中条数**（退役 Shot 会被跳过并计数在内）。
        """
        now = utc_now()
        with self.db.write() as conn:
            existing = conn.execute(
                "SELECT id FROM semantic_query WHERE query_text = ? AND model = ?"
                " AND model_version = ?",
                (query_text, model, model_version),
            ).fetchone()
            if existing:
                query_id = int(existing[0])
                conn.execute(
                    "UPDATE semantic_query SET prompt_text = ?, text_kind = ?, dim = ?,"
                    " normalization = ?, vector = ?, concept_ratio = COALESCE(?, concept_ratio),"
                    " updated_at = ? WHERE id = ?",
                    (prompt_text, text_kind, int(dim), normalization, bytes(vector),
                     concept_ratio, now, query_id),
                )
                # 结果整体替换：这个词的含义变了（换了 prompt），旧命中就作废
                conn.execute("DELETE FROM semantic_query_hit WHERE query_id = ?", (query_id,))
            else:
                cursor = conn.execute(
                    "INSERT INTO semantic_query (query_text, prompt_text, text_kind, model,"
                    " model_version, dim, normalization, vector, parsed_filters_json,"
                    " concept_ratio, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (query_text, prompt_text, text_kind, model, model_version, int(dim),
                     normalization, bytes(vector), concept_ratio, now, now),
                )
                query_id = int(cursor.lastrowid)

            written = 0
            for shot_id, rank, score, is_primary in hits:
                # 只写**活跃** Shot：退役的写了界面也看不见（且是同一个 bug 族的入口）
                alive = conn.execute(
                    "SELECT 1 FROM shot WHERE id = ? AND retired_at IS NULL", (int(shot_id),)
                ).fetchone()
                if not alive:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO semantic_query_hit (query_id, shot_id, rank,"
                    " score, is_primary_asset, computed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (query_id, int(shot_id), int(rank), float(score), 1 if is_primary else 0, now),
                )
                written += 1
        return written

    # ------------------------------------------------------------------ 读
    def list_queries(self, *, model: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """界面下拉框用：列出**已经有结果**的词，按凝聚度降序（没测过凝聚度的排最后）。

        `hit_count` 数的是**看得见的**命中（退役 Shot 不算）—— 与 §6.10 那条
        "列表页与详情页必须同一口径"的教训一致。
        """
        sql = (
            "SELECT q.id, q.query_text, q.prompt_text, q.concept_ratio, q.updated_at,"
            "       (SELECT COUNT(*) FROM semantic_query_hit h"
            "          JOIN shot s ON s.id = h.shot_id AND s.retired_at IS NULL"
            "         WHERE h.query_id = q.id) AS hit_count"
            "  FROM semantic_query q"
        )
        params: list[Any] = []
        if model:
            sql += " WHERE q.model = ?"
            params.append(model)
        sql += " ORDER BY (q.concept_ratio IS NULL), q.concept_ratio DESC, q.query_text LIMIT ?"
        params.append(int(limit))
        return [
            {
                "id": int(r["id"]),
                "query_text": r["query_text"],
                "prompt_text": r["prompt_text"],
                "concept_ratio": r["concept_ratio"],
                "updated_at": r["updated_at"],
                "hit_count": int(r["hit_count"]),
            }
            for r in self.db.query(sql, params)
        ]

    def get_query(self, query_text: str) -> dict[str, Any] | None:
        """按**词**取一行（界面的输入框走这条路：只有预置过的词才有向量）。"""
        row = self.db.query_one(
            "SELECT * FROM semantic_query WHERE query_text = ? ORDER BY updated_at DESC LIMIT 1",
            (query_text,),
        )
        return dict(row) if row else None

    def hits(
        self, query_id: int, *, limit: int = 100, library_id: int | None = None
    ) -> list[dict[str, Any]]:
        """取某个查询的命中（**只返回看得见的**），带界面渲染需要的元数据。

        返回的 `capture_at` 是关键：模型分不清日出与日落（实测两者 top-100 重合 65%），
        所以**本地拍摄时刻就是用户判断的依据**（用户 2026-10-13 明确要求）。
        """
        sql = (
            "SELECT h.rank, h.score, h.is_primary_asset,"
            "       s.id AS shot_id, s.captured_at, s.scene_id, s.camera_key,"
            "       sc.name AS scene_name,"
            "       a.id AS asset_id, a.relative_path,"
            "       m.capture_at, COALESCE(sel.status, 'UNREVIEWED') AS selection_status,"
            "       (SELECT COUNT(*) FROM asset a2 WHERE a2.shot_id = s.id) AS asset_count"
            "  FROM semantic_query_hit h"
            "  JOIN shot s ON s.id = h.shot_id AND s.retired_at IS NULL"
            "  LEFT JOIN scene sc ON sc.id = s.scene_id"
            "  JOIN asset a ON a.id = COALESCE("
            "        (SELECT a3.id FROM asset a3 WHERE a3.shot_id = s.id ORDER BY a3.id LIMIT 1), -1)"
            "  LEFT JOIN metadata m ON m.asset_id = a.id"
            "  LEFT JOIN selection sel ON sel.shot_id = s.id"
            " WHERE h.query_id = ?"
        )
        params: list[Any] = [int(query_id)]
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(int(library_id))
        sql += " ORDER BY h.rank LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.db.query(sql, params)]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM semantic_query", default=0))

    # ------------------------------------------------------------------ 人工反馈（迁移 008）
    #
    # 反馈与学习**都在服务端完成**，因为两者都只用库里已有的图像向量做点积 ——
    # 不需要 torch，所以「方案 a」（serve 不装模型运行时）不被破坏。
    # 这是把"持续学习"做进界面的前提：用户点一下就能立刻看到重排结果。

    def set_feedback(self, query_id: int, shot_id: int, action: str) -> None:
        """记一次人工表态（`keep` = 选进主题 / `drop` = 不是我要的）。

        **幂等**：同一个 (query, shot) 改主意是 UPDATE，不是堆行 ——
        否则"这个词被删过又被加回来"会在表里留下两条互相矛盾的记录，
        重算时无法判断以哪条为准。`semantic_feedback` 的 UNIQUE 约束保证了这一点。
        """
        self.set_feedback_many(query_id, [shot_id], action)

    def set_feedback_many(self, query_id: int, shot_ids: Sequence[int], action: str) -> int:
        """一次表态多张（界面的多选）。**一条事务**写完，返回实际写入的条数。

        分成 N 次 `set_feedback` 也能得到同样的表内容，但会开 N 次写事务：
        界面多选 20 张就是 20 次提交 + 20 次 fsync。表内容一样，代价不一样。
        """
        if action not in ("keep", "drop"):
            raise ValueError(f"action 必须是 keep 或 drop，实际 {action!r}")
        ids = [int(s) for s in shot_ids]
        if not ids:
            return 0
        now = utc_now()
        with self.db.write() as conn:
            conn.executemany(
                "INSERT INTO semantic_feedback (query_id, shot_id, action, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(query_id, shot_id) DO UPDATE SET action = excluded.action,"
                " updated_at = excluded.updated_at",
                [(int(query_id), shot_id, action, now, now) for shot_id in ids],
            )
        return len(ids)

    def clear_feedback(self, query_id: int, shot_ids: Sequence[int] | None = None) -> int:
        """撤销表态。不给 `shot_ids` 就清空该词的全部反馈（"我反悔了，重来"）。"""
        with self.db.write() as conn:
            if shot_ids is None:
                cursor = conn.execute(
                    "DELETE FROM semantic_feedback WHERE query_id = ?", (int(query_id),))
            else:
                ids = [int(s) for s in shot_ids]
                if not ids:
                    return 0
                marks = ",".join("?" for _ in ids)
                cursor = conn.execute(
                    f"DELETE FROM semantic_feedback WHERE query_id = ? AND shot_id IN ({marks})",
                    [int(query_id), *ids],
                )
            return int(cursor.rowcount or 0)

    def feedback_map(self, query_id: int) -> dict[int, str]:
        """`{shot_id: 'keep'|'drop'}` —— 界面给每张卡片渲染"你已表态"标记要用。"""
        return {
            int(r["shot_id"]): str(r["action"])
            for r in self.db.query(
                "SELECT shot_id, action FROM semantic_feedback WHERE query_id = ?",
                (int(query_id),),
            )
        }

    def feedback_counts(self, query_id: int) -> dict[str, int]:
        rows = self.db.query(
            "SELECT action, COUNT(*) AS n FROM semantic_feedback WHERE query_id = ? GROUP BY action",
            (int(query_id),),
        )
        counts = {"keep": 0, "drop": 0}
        for row in rows:
            counts[str(row["action"])] = int(row["n"])
        return counts

    # ------------------------------------------------------------------ 学到的重排参数

    def save_tuning(
        self,
        query_id: int,
        *,
        image_model: str,
        image_model_version: str,
        image_prep_version: str,
        dim: int,
        pos_vector: bytes | None,
        neg_vector: bytes | None,
        pos_count: int,
        neg_count: int,
        alpha: float,
        beta: float,
    ) -> None:
        """写入/更新该词的重排参数。

        与 `embedding` 的"绝不覆盖历史"纪律**有意不同**：这里覆盖是对的。
        因为 `semantic_query_hit` 保存的是**模型原始判断**（历史仍在），
        而本表保存的是**用户当前口味**——口味本来就该只有一个当前值。
        关掉学习开关随时能看到原始结果，所以"可追溯"没有被牺牲。
        """
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO semantic_tuning (query_id, image_model, image_model_version,"
                " image_prep_version, dim, pos_vector, neg_vector, pos_count, neg_count,"
                " alpha, beta, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(query_id) DO UPDATE SET"
                " image_model = excluded.image_model,"
                " image_model_version = excluded.image_model_version,"
                " image_prep_version = excluded.image_prep_version,"
                " dim = excluded.dim, pos_vector = excluded.pos_vector,"
                " neg_vector = excluded.neg_vector, pos_count = excluded.pos_count,"
                " neg_count = excluded.neg_count, alpha = excluded.alpha,"
                " beta = excluded.beta, updated_at = excluded.updated_at",
                (int(query_id), image_model, image_model_version, image_prep_version, int(dim),
                 pos_vector, neg_vector, int(pos_count), int(neg_count),
                 float(alpha), float(beta), now),
            )

    def get_tuning(self, query_id: int) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM semantic_tuning WHERE query_id = ?", (int(query_id),))
        return dict(row) if row else None

    def delete_tuning(self, query_id: int) -> None:
        self.db.execute("DELETE FROM semantic_tuning WHERE query_id = ?", (int(query_id),))

    # ------------------------------------------------------------------ 重排用的图像向量

    def shot_vectors(
        self,
        *,
        model: str,
        model_version: str,
        prep_version: str,
        shot_ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """取每个**活跃 Shot 的代表图**在该身份下的向量（重排与原型都基于它）。

        为什么取"代表图"而不是该 Shot 下所有 asset：一个 Shot 就是一个决策单元，
        界面本来就按 Shot 去重（实测 top-20 里只有 12 个独立 Shot）。
        多取会让同一个决策单元被重复计票。

        为什么**不在这里**解码成 numpy：`repos` 层不 import numpy（保持零科学计算依赖），
        解码与点积在 `library/semantic_rank.py` 里做。

        ## ★ 为什么用预聚合子查询而不是相关子查询（一次实测的性能修复）

        早先的写法是 `JOIN asset a ON a.id = (SELECT a3.id FROM asset a3
        WHERE a3.shot_id = s.id ORDER BY a3.id LIMIT 1)` —— 语义没错，
        但 SQLite **无法为"联接到一个子查询表达式"选择索引**，于是退化成逐行重新求值。
        实测：7,371 个 Shot 上这条查询要 **41 秒**，而它服务的重排本身只要 0.7 毫秒 ——
        等于"点一下等 40 秒"，功能实际上不可用。
        改成先 `GROUP BY` 拿到 `(shot_id → 最小 asset_id)` 与各自的资产数，
        再普通 JOIN（走 `idx_asset_shot`），降到毫秒级。
        """
        sql = SHOT_VECTORS_SQL
        params: list[Any] = [model, model_version, prep_version]
        if shot_ids is not None:
            ids = [int(s) for s in shot_ids]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            sql += f" AND s.id IN ({marks})"
            params.extend(ids)
        return [dict(r) for r in self.db.query(sql, params)]

    def shot_vectors_for_ids(
        self, shot_ids: Sequence[int], *, model: str, model_version: str, prep_version: str
    ) -> list[dict[str, Any]]:
        return self.shot_vectors(model=model, model_version=model_version,
                                 prep_version=prep_version, shot_ids=shot_ids)


#: `SemanticRepo.shot_vectors` 用的 SQL。**提出来是为了让测试能对它做
#: `EXPLAIN QUERY PLAN`** —— 这条查询曾经因为相关子查询慢到 41 秒（见方法说明），
#: 而"慢"在功能测试里是看不出来的，只有计划能守住它。
SHOT_VECTORS_SQL = (
    "SELECT s.id AS shot_id, a.id AS asset_id, e.vector, e.dim,"
    "       m.capture_at, s.captured_at, s.scene_id, a.relative_path,"
    "       COALESCE(sel.status, 'UNREVIEWED') AS selection_status,"
    "       COALESCE(cnt.n, 1) AS asset_count"
    "  FROM shot s"
    "  JOIN (SELECT a3.shot_id AS shot_id, MIN(a3.id) AS asset_id"
    "          FROM asset a3 WHERE a3.shot_id IS NOT NULL"
    "         GROUP BY a3.shot_id) pick ON pick.shot_id = s.id"
    "  JOIN asset a ON a.id = pick.asset_id"
    "  JOIN (SELECT a4.shot_id AS shot_id, COUNT(*) AS n FROM asset a4"
    "         WHERE a4.shot_id IS NOT NULL GROUP BY a4.shot_id) cnt"
    "    ON cnt.shot_id = s.id"
    "  JOIN embedding e ON e.asset_id = a.id AND e.model = ? AND e.model_version = ?"
    "                  AND e.prep_version = ?"
    "  LEFT JOIN metadata m ON m.asset_id = a.id"
    "  LEFT JOIN selection sel ON sel.shot_id = s.id"
    " WHERE s.retired_at IS NULL"
)


class FaceRepo:
    """`face` / `face_embedding` 的读写（Phase 4 · 迁移 008）。

    ## 分层：检测是事实，归属是判断

    本类只管**事实侧** —— "这张 asset 上这个位置有一张脸"和"这张脸的 128 维特征是什么"。
    "这张脸是谁"属于 `PeopleRepo`。分开的理由就是 PRD §9.3 那句
    「AI 不能把主观判断伪装成事实」：界面必须能说出"检测到 905 张脸"（可复算）
    与"其中 47 张我判断是同一个人"（可能错）的区别。

    ## 为什么坐标存 preview 像素

    检测时会把长边缩到 `detect_max`（900）以控制耗时，但**存库的坐标已经还原**成
    preview 原始像素。这样任何读取端（界面裁剪人脸缩略图）都不需要知道检测缩放，
    复算时也不会因为改了 `detect_max` 而让旧行失效。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 写
    def replace_asset_faces(self, asset_id: int, *, detector: str, detector_version: str,
                            detect_max: int, preview_w: int, preview_h: int,
                            faces: Sequence[dict[str, Any]]) -> int:
        """写入某张 asset 的检测结果。

        **先删后插**（只针对这一张 asset、这一个检测器身份）：
        重新检测同一张图时，旧的框可能因为阈值调整而消失；若只做 upsert，
        消失的框会永远留在库里，而它对应的脸早就"不存在"了。
        删除范围被严格限定在 `(asset_id, detector, detector_version)` 内，
        因此换检测器不会破坏旧结果（多版本并存）。
        """
        now = utc_now()
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT shot_id FROM asset WHERE id = ?", (int(asset_id),)).fetchone()
            shot_id = int(row[0]) if row and row[0] is not None else None
            conn.execute(
                "DELETE FROM face WHERE asset_id = ? AND detector = ? AND detector_version = ?",
                (int(asset_id), detector, detector_version),
            )
            written = 0
            for item in faces:
                conn.execute(
                    "INSERT INTO face (asset_id, shot_id, detector, detector_version, detect_max,"
                    " preview_w, preview_h, box_x, box_y, box_w, box_h, landmarks_json, det_score,"
                    " face_px, symmetry, eye_ratio, is_frontal, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(asset_id), shot_id, detector, detector_version, int(detect_max),
                     int(preview_w), int(preview_h),
                     float(item["box_x"]), float(item["box_y"]),
                     float(item["box_w"]), float(item["box_h"]),
                     item.get("landmarks_json"), float(item["det_score"]), float(item["face_px"]),
                     item.get("symmetry"), item.get("eye_ratio"),
                     1 if item.get("is_frontal") else 0, now),
                )
                written += 1
        return written

    def save_embeddings(self, rows: Sequence[tuple[int, bytes]], *, model: str,
                        model_version: str, prep_version: str, dim: int,
                        normalization: str = "l2") -> int:
        """批量写人脸特征。`(face_id, vector_bytes)`；同一身份重复写入是覆盖（幂等重算）。"""
        now = utc_now()
        payload = [
            (int(face_id), model, model_version, prep_version, int(dim), normalization,
             bytes(vector), now)
            for face_id, vector in rows
        ]
        if not payload:
            return 0
        with self.db.write() as conn:
            conn.executemany(
                "INSERT INTO face_embedding (face_id, model, model_version, prep_version, dim,"
                " normalization, vector, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(face_id, model, model_version, prep_version)"
                " DO UPDATE SET vector = excluded.vector, dim = excluded.dim,"
                " normalization = excluded.normalization, created_at = excluded.created_at",
                payload,
            )
        return len(payload)

    def set_shot_for_asset(self, asset_id: int) -> int:
        """把某 asset 上所有人脸的 shot_id 补齐（Shot 分组重算之后调用）。

        为什么需要这一步：`face.shot_id` 在检测时可能还是 NULL（分组尚未跑），
        而界面按 Shot 组织"这个人的照片"。所以分组之后要有一次回填，
        否则会出现"检测到了脸，但人物页里一张照片都看不到"。
        """
        return int(self.db.execute(
            "UPDATE face SET shot_id = (SELECT shot_id FROM asset WHERE id = face.asset_id)"
            " WHERE asset_id = ? AND shot_id IS NOT"
            " (SELECT shot_id FROM asset WHERE id = face.asset_id)",
            (int(asset_id),),
        ) or 0)

    def refresh_all_shots(self) -> int:
        """全库回填 `face.shot_id`（分组重算后一次调用，比逐资产快得多）。"""
        return int(self.db.execute(
            "UPDATE face SET shot_id = (SELECT a.shot_id FROM asset a WHERE a.id = face.asset_id)"
            " WHERE shot_id IS NOT"
            " (SELECT a.shot_id FROM asset a WHERE a.id = face.asset_id)"
        ) or 0)

    # ------------------------------------------------------------------ 读
    def assets_needing_detect(self, *, detector: str, detector_version: str,
                              limit: int | None = None, asset_ids: Sequence[int] | None = None,
                              scene_id: int | None = None, session_like: str | None = None,
                              library_id: int | None = None) -> list[dict[str, Any]]:
        """找出"还没有被这个检测器看过的、有 preview 的"资产。

        条件里带 `preview` join 是**必须**的：没有 preview 就没法检测，
        把它算进"待检测"会让进度永远到不了 100%，用户会以为卡住了。
        """
        sql = (
            "SELECT a.id AS asset_id, a.library_id, a.shot_id, p.path AS preview_path,"
            "       p.width AS preview_w, p.height AS preview_h"
            "  FROM asset a"
            "  JOIN preview p ON p.asset_id = a.id AND p.kind = 'preview'"
            "  LEFT JOIN shot s ON s.id = a.shot_id"
            " WHERE a.status = 'OK'"
            "   AND NOT EXISTS (SELECT 1 FROM face f WHERE f.asset_id = a.id"
            "                    AND f.detector = ? AND f.detector_version = ?)"
        )
        params: list[Any] = [detector, detector_version]
        if asset_ids is not None:
            ids = [int(i) for i in asset_ids]
            if not ids:
                return []
            sql += f" AND a.id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        if scene_id is not None:
            sql += " AND s.scene_id = ?"
            params.append(int(scene_id))
        if session_like:
            sql += " AND EXISTS (SELECT 1 FROM scene sc JOIN session se ON se.id = sc.session_id"
            sql += " WHERE sc.id = s.scene_id AND se.name LIKE ? ESCAPE '\\')"
            params.append(f"%{_escape_like(session_like)}%")
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(int(library_id))
        sql += " ORDER BY a.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.db.query(sql, params)]

    def faces_needing_embedding(self, *, model: str, model_version: str, prep_version: str,
                                limit: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.*, p.path AS preview_path"
            "  FROM face f"
            "  JOIN preview p ON p.asset_id = f.asset_id AND p.kind = 'preview'"
            " WHERE NOT EXISTS (SELECT 1 FROM face_embedding fe WHERE fe.face_id = f.id"
            "                    AND fe.model = ? AND fe.model_version = ? AND fe.prep_version = ?)"
            " ORDER BY f.id"
        )
        params: list[Any] = [model, model_version, prep_version]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.db.query(sql, params)]

    def embeddings(self, *, model: str, model_version: str, prep_version: str,
                   face_ids: Sequence[int] | None = None,
                   only_frontal: bool = False) -> list[dict[str, Any]]:
        """取人脸特征（**原始 BLOB**，解码交给调用方，保持本层不依赖 numpy）。

        ⚠️ 除了特征本身，这里还要带上**这张脸长在哪张照片上**（`asset_id` +
        `relative_path` + `capture_at`）。调用方（`people suggest` / 界面的「再找找像他的」）
        拿到的是"一批候选脸"，用户看完要做的事必然是"去看原片" ——
        上一版只给了 `asset_id`，于是建议区每一张都显示"无拍摄时间"，
        也没有任何东西能指回照片。**能追到原片是这些结果能被判断的前提。**
        """
        sql = (
            "SELECT fe.face_id, fe.vector, fe.dim, f.asset_id, f.shot_id, f.is_frontal,"
            "       f.det_score, f.face_px, f.symmetry, f.eye_ratio,"
            "       f.box_x, f.box_y, f.box_w, f.box_h,"
            "       m.capture_at, a.relative_path, s.scene_id"
            "  FROM face_embedding fe JOIN face f ON f.id = fe.face_id"
            "  LEFT JOIN asset a ON a.id = f.asset_id"
            "  LEFT JOIN metadata m ON m.asset_id = f.asset_id"
            "  LEFT JOIN shot s ON s.id = f.shot_id AND s.retired_at IS NULL"
            " WHERE fe.model = ? AND fe.model_version = ? AND fe.prep_version = ?"
        )
        params: list[Any] = [model, model_version, prep_version]
        if only_frontal:
            sql += " AND f.is_frontal = 1"
        if face_ids is not None:
            ids = [int(i) for i in face_ids]
            if not ids:
                return []
            sql += f" AND fe.face_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        sql += " ORDER BY fe.face_id"
        return [dict(r) for r in self.db.query(sql, params)]

    def face(self, face_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM face WHERE id = ?", (int(face_id),))
        return dict(row) if row else None

    # ------------------------------------------------------------------ 身份解析
    def identity(self) -> tuple[str, str, str] | None:
        """读出库里**实际存在**的人脸特征身份（没有则返回 None）。

        ## 为什么必须从库里读，而不是现场重算

        曾经这里用的是 `faces.model_version()`，而它的取值是 `cv2-<版本>-fp32`。
        问题在于**产品 venv 里没有装 cv2**（这是刻意的：模型侧只在命令行跑），
        于是 `cv2.__version__` 取不到，函数退化成 `cv2-unknown-fp32` ——
        界面按这个身份去查特征，一条都查不到，人物页永远显示"0 张脸有特征"，
        而后台数据明明是好的。

        这种"写入端与读取端的身份算法不同"的错误极难发现：命令行一切正常，
        只有界面上是空的。所以读取端一律**从库里已有的行**解析身份，
        写入端才用品当前环境算出来的身份（换 cv2 ⇒ 新身份 ⇒ 新的一行，符合铁律 #4）。
        """
        row = self.db.query_one(
            "SELECT model, model_version, prep_version, COUNT(*) AS n FROM face_embedding"
            " GROUP BY model, model_version, prep_version ORDER BY n DESC, model LIMIT 1")
        if row is None:
            return None
        return str(row["model"]), str(row["model_version"]), str(row["prep_version"])

    def identities(self) -> list[dict[str, Any]]:
        """库里全部人脸特征身份（`ai doctor` 与 troubleshooting 用）。"""
        return [dict(r) for r in self.db.query(
            "SELECT model, model_version, prep_version, COUNT(*) AS n FROM face_embedding"
            " GROUP BY model, model_version, prep_version ORDER BY n DESC")]

    def stats(self, *, model: str | None = None, model_version: str | None = None,
              prep_version: str | None = None) -> dict[str, Any]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM face", default=0))
        frontal = int(self.db.scalar("SELECT COUNT(*) FROM face WHERE is_frontal = 1", default=0))
        assets = int(self.db.scalar("SELECT COUNT(DISTINCT asset_id) FROM face", default=0))
        result: dict[str, Any] = {
            "faces": total,
            "frontal": frontal,
            "assets_with_faces": assets,
        }
        if model:
            result["embedded"] = int(self.db.scalar(
                "SELECT COUNT(*) FROM face_embedding WHERE model = ? AND model_version = ?"
                " AND prep_version = ?",
                (model, model_version, prep_version), default=0))
        return result


class PeopleRepo:
    """`person` / `person_face` / `person_reject` / `person_profile` / `person_event`。

    ## 这个类实现的是"小模型初筛 + 人来确认 + 持续学习"

    | 阶段 | 谁做 | 落在哪 |
    | --- | --- | --- |
    | 初筛 | 模型 | `person`（status='proposed'，**名字为空**）+ `person_face(source='auto')` |
    | 命名 | 用户 | `person.name` + status='named' + `person_event('rename')` |
    | 确认/否决 | 用户 | `person_face(source='user')` / `person_reject` |
    | 学习 | **重算，不训练** | `person_profile`（原型 + 学到的阈值） |
    | 增量 | 模型用**新原型**再判 | `person_face(source='auto')` 追加 |

    ## 三条不可省略的纪律

    1. **模型不给人起名**。`create_person(name=None)` 是常态；界面必须让用户命名。
    2. **"未知"与"被否决"是两回事**（缺 `person_face` 行 vs 有 `person_reject` 行）。
       学习时只有后者是负标签，前者不能当成负样本 —— 否则用户没看过的脸会被
       当成"不是这个人"，模型越学越窄。
    3. **每个决定都留痕**（`person_event`）。用户否决过的脸也要能查出来，
       与 `selection_event` 同一套"记录选择过程"的纪律。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 人
    def create_person(self, *, name: str | None = None, note: str | None = None,
                      uuid: str | None = None, status: str | None = None) -> int:
        now = utc_now()
        resolved = status or ("named" if name else "proposed")
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO person (uuid, name, status, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (uuid or uuid4().hex, name, resolved, note, now, now),
            )
            return int(cursor.lastrowid)

    def rename(self, person_id: int, name: str | None, note: str | None = None) -> None:
        now = utc_now()
        status = "named" if (name or "").strip() else "proposed"
        with self.db.write() as conn:
            conn.execute(
                "UPDATE person SET name = ?, status = ?,"
                " note = COALESCE(?, note), updated_at = ? WHERE id = ?",
                (name, status, note, now, int(person_id)),
            )

    def set_cover(self, person_id: int, face_id: int | None) -> None:
        self.db.execute(
            "UPDATE person SET cover_face_id = ?, updated_at = ? WHERE id = ?",
            (None if face_id is None else int(face_id), utc_now(), int(person_id)),
        )

    def delete_person(self, person_id: int) -> None:
        """删掉一个人（**只删归属与原型，不删脸、不删照片**）。"""
        self.db.execute("DELETE FROM person WHERE id = ?", (int(person_id),))

    def dissolve_person(self, person_id: int) -> int:
        """**解散候选集**：`status='archived'`，`person_face` 一行都不删。返回名下脸数。

        为什么不是 DELETE（那才是"删除这个人"该做的）：解散的语义是
        "这一簇的结论不算数"，不是"这些脸不存在"。留着归属有一个关键的副作用 ——
        下一次 `people discover` 取的是"**一条归属都没有**"的脸（`unassigned_face_ids`），
        所以解散过的脸不会再被聚成同一坨；而 `unclaimed_face_ids` 只看"用户确认过没有"，
        因此「再找找像他的」仍然能把这些脸提给真正的那个人。
        """
        faces = int(self.db.scalar(
            "SELECT COUNT(*) FROM person_face WHERE person_id = ?", (int(person_id),), default=0))
        self.db.execute(
            "UPDATE person SET status = 'archived', updated_at = ? WHERE id = ?",
            (utc_now(), int(person_id)))
        return faces

    def restore_person(self, person_id: int) -> None:
        """撤销解散。名字还在就回到 `named`，否则回到 `proposed`。"""
        row = self.db.query_one("SELECT name FROM person WHERE id = ?", (int(person_id),))
        status = "named" if row is not None and row["name"] else "proposed"
        self.db.execute(
            "UPDATE person SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), int(person_id)))

    def archived_persons(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """被解散的候选（界面上的"你看过、说不算数"的那一栏，可以放回来）。"""
        return [dict(r) for r in self.db.query(
            "SELECT p.id, p.name, p.status, p.updated_at,"
            "       (SELECT COUNT(*) FROM person_face pf WHERE pf.person_id = p.id) AS faces,"
            "       (SELECT pf.face_id FROM person_face pf WHERE pf.person_id = p.id"
            "         ORDER BY pf.face_id LIMIT 1) AS cover_face_id"
            "  FROM person p WHERE p.status = 'archived'"
            " ORDER BY p.updated_at DESC, p.id LIMIT ?", (int(limit),))]

    def existing_face_ids(self, face_ids: Sequence[int]) -> list[int]:
        """过滤出库里真的存在的 face id（**保持传入顺序**）。

        为什么要过滤：用户点的一批脸可能刚被"检测重算"换掉 id。让整个请求
        因为其中一张不存在而失败，会让"选了 20 张、其中 1 张刚变"变成一次白点。
        """
        ids = [int(f) for f in face_ids]
        if not ids:
            return []
        alive: set[int] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            alive.update(int(r["id"]) for r in self.db.query(
                f"SELECT id FROM face WHERE id IN ({marks})", chunk))
        return [f for f in ids if f in alive]

    def person(self, person_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM person WHERE id = ?", (int(person_id),))
        return dict(row) if row else None

    def list_persons(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """人物列表：带**确认/提议计数**与一张封面脸。

        封面优先用用户确认过的脸（`source='user'`），其次模型提议的 ——
        列表缩略图应该尽量是"用户认过的"，那才是他认人的依据。

        ⚠️ 计数与人脸墙都要 `NOT EXISTS face_verdict`：被标成"不是人"的脸不算
        这个人名下的脸。少了这一条，"你确认过 N 张"会在标完"不是人"之后
        纹丝不动 —— 而人脸墙上那张已经不见了，两处数字对不上。
        """
        not_person = ("NOT EXISTS (SELECT 1 FROM face_verdict fv"
                      " WHERE fv.face_id = pf.face_id)")
        sql = (
            "SELECT p.*,"
            "  (SELECT COUNT(*) FROM person_face pf"
            f"    WHERE pf.person_id = p.id AND pf.source = 'user' AND {not_person})"
            "   AS user_faces,"
            "  (SELECT COUNT(*) FROM person_face pf"
            f"    WHERE pf.person_id = p.id AND pf.source = 'auto' AND {not_person})"
            "   AS auto_faces,"
            "  (SELECT COUNT(*) FROM person_reject pr WHERE pr.person_id = p.id) AS rejects,"
            "  (SELECT COUNT(DISTINCT f.shot_id) FROM person_face pf JOIN face f ON f.id = pf.face_id"
            f"    WHERE pf.person_id = p.id AND f.shot_id IS NOT NULL AND NOT EXISTS"
            "     (SELECT 1 FROM face_verdict fv WHERE fv.face_id = pf.face_id)) AS shots,"
            "  (SELECT pp.threshold FROM person_profile pp WHERE pp.person_id = p.id"
            "    ORDER BY pp.updated_at DESC LIMIT 1) AS threshold,"
            "  (SELECT pp.margin FROM person_profile pp WHERE pp.person_id = p.id"
            "    ORDER BY pp.updated_at DESC LIMIT 1) AS margin,"
            "  (SELECT pp.pos_count FROM person_profile pp WHERE pp.person_id = p.id"
            "    ORDER BY pp.updated_at DESC LIMIT 1) AS profile_pos,"
            "  (SELECT pp.neg_count FROM person_profile pp WHERE pp.person_id = p.id"
            "    ORDER BY pp.updated_at DESC LIMIT 1) AS profile_neg,"
            "  COALESCE(p.cover_face_id,"
            "    (SELECT pf.face_id FROM person_face pf JOIN face f ON f.id = pf.face_id"
            "      WHERE pf.person_id = p.id"
            "        AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = pf.face_id)"
            "      ORDER BY (pf.source = 'user') DESC, pf.face_id LIMIT 1)"
            "  ) AS effective_cover_face_id"
            "  FROM person p"
        )
        if not include_archived:
            sql += " WHERE p.status <> 'archived'"
        sql += " ORDER BY (p.status = 'proposed') DESC, p.updated_at DESC, p.id"
        rows = [dict(r) for r in self.db.query(sql)]
        for row in rows:
            cover = row.pop("effective_cover_face_id", None)
            row["cover_face_id"] = cover
        return rows

    # ------------------------------------------------------------------ 归属
    def add_faces(self, person_id: int, face_ids: Sequence[int], *, source: str,
                  confidences: dict[int, float] | None = None,
                  similarities: dict[int, float] | None = None) -> int:
        """把脸归到某人名下。**同一张脸被确认给某人时，自动从别人的提议里撤掉。**

        最后这条规则很关键：一张脸只属于一个人。否则用户的确认会与模型的
        旧提议并存，界面上同一张脸出现在两个人下面 —— 用户会以为系统坏了。
        撤掉的只是 `source='auto'` 的提议；**用户自己确认过的归属永不自动撤销**。
        """
        if not face_ids:
            return 0
        now = utc_now()
        ids = [int(f) for f in face_ids]
        added = 0
        with self.db.write() as conn:
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM person_face WHERE face_id IN ({marks}) AND person_id <> ?"
                f" AND source = 'auto'",
                [*ids, int(person_id)],
            )
            # 否决记录也要撤：用户现在说"是他"，之前的"不是他"就作废了
            conn.execute(
                f"DELETE FROM person_reject WHERE face_id IN ({marks}) AND person_id = ?",
                [*ids, int(person_id)],
            )
            for face_id in ids:
                cursor = conn.execute(
                    "INSERT INTO person_face (person_id, face_id, source, confidence,"
                    " similarity, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(person_id, face_id) DO UPDATE SET source = excluded.source,"
                    " confidence = excluded.confidence, similarity = excluded.similarity,"
                    " updated_at = excluded.updated_at",
                    (int(person_id), face_id, source,
                     None if confidences is None else confidences.get(face_id),
                     None if similarities is None else similarities.get(face_id),
                     now, now),
                )
                added += 1 if cursor.rowcount else 0
        return added

    def remove_faces(self, person_id: int, face_ids: Sequence[int]) -> int:
        ids = [int(f) for f in face_ids]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        return int(self.db.execute(
            f"DELETE FROM person_face WHERE person_id = ? AND face_id IN ({marks})",
            [int(person_id), *ids]) or 0)

    def reject_faces(self, person_id: int, face_ids: Sequence[int]) -> int:
        """用户说"这不是他"。**必须同时去掉正例归属**，否则同一张脸既属于又被否决。"""
        ids = [int(f) for f in face_ids]
        if not ids:
            return 0
        now = utc_now()
        marks = ",".join("?" for _ in ids)
        with self.db.write() as conn:
            conn.execute(
                f"DELETE FROM person_face WHERE person_id = ? AND face_id IN ({marks})",
                [int(person_id), *ids])
            for face_id in ids:
                conn.execute(
                    "INSERT INTO person_reject (person_id, face_id, created_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(person_id, face_id) DO NOTHING",
                    (int(person_id), face_id, now),
                )
        return len(ids)

    def unreject_faces(self, person_id: int, face_ids: Sequence[int]) -> int:
        ids = [int(f) for f in face_ids]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        return int(self.db.execute(
            f"DELETE FROM person_reject WHERE person_id = ? AND face_id IN ({marks})",
            [int(person_id), *ids]) or 0)

    def auto_face_ids(self, person_id: int) -> list[int]:
        """这个人名下的**模型提议**（`source='auto'`）—— 一键确认要处理的就是它们。"""
        return [int(r["face_id"]) for r in self.db.query(
            "SELECT face_id FROM person_face WHERE person_id = ? AND source = 'auto'"
            " ORDER BY face_id", (int(person_id),))]

    # ---------------------------------------------------------------- 搬脸 / 不是人
    #
    # 这两个动作都**同时**动 `person_face` 与另一张表，所以各自必须是一条事务：
    # 中途失败留下"脸已经离开 A、却没到 B"的状态，用户看到的就是丢脸。

    def move_faces(self, face_ids: Sequence[int], *, target_person_id: int) -> list[int]:
        """把脸**搬**到目标人名下，返回被搬空的原人物 id（要重算判据的那些）。

        ⚠️ 与 `add_faces` 的关键区别：`add_faces` 只撤别人的 `auto` 提议，
        **不动用户确认过的归属**（那条规则保护的是"确认"这个动作）。
        但"搬"是更明确的指令：用户说"这些其实都是 XX"，那就必须从原来那个人
        （哪怕是自己以前确认过的）名下摘掉 —— 否则同一张脸挂在两个人下面。
        """
        ids = [int(f) for f in face_ids]
        if not ids:
            return []
        now = utc_now()
        target = int(target_person_id)
        with self.db.write() as conn:
            marks = ",".join("?" for _ in ids)
            sources = [int(r["person_id"]) for r in conn.execute(
                f"SELECT DISTINCT person_id FROM person_face"
                f" WHERE face_id IN ({marks}) AND person_id <> ?", [*ids, target]).fetchall()]
            conn.execute(
                f"DELETE FROM person_face WHERE face_id IN ({marks}) AND person_id <> ?",
                [*ids, target])
            conn.execute(
                f"DELETE FROM person_reject WHERE face_id IN ({marks}) AND person_id = ?",
                [*ids, target])
            for face_id in ids:
                conn.execute(
                    "INSERT INTO person_face (person_id, face_id, source, created_at, updated_at)"
                    " VALUES (?, ?, 'user', ?, ?)"
                    " ON CONFLICT(person_id, face_id) DO UPDATE SET source = 'user',"
                    " updated_at = excluded.updated_at",
                    (target, face_id, now, now))
        return sources

    def mark_not_person(self, face_ids: Sequence[int], *, note: str | None = None) -> list[int]:
        """记下"这些脸不是人"，返回受影响的人物 id（他们的判据要重算）。

        ★ **只写判定，不动 `person_face` / `person_reject` 一行** —— 这是刻意的设计。

        第一版是"顺便把归属删掉"，结果两个真缺陷立刻出现：
          1. 撤销（删掉判定行）之后，那张脸**回不到任何人名下**了。用户确认过的
             正例就这么被一次误标吃掉了 —— 而界面上"撤销"看起来是完整的回头路。
          2. 别处都在数 `person_face`，于是"标一个不是人"会让列表里的
             "你确认过 N 张"跟着跳动，撤销又跳不回去。

        正确的模型：判定是一张**滤镜**，不是一个"重新归属"的动作。
        所有读路径（人脸墙 / 学习样本 / 聚类池 / 建议池 / 各类计数）都已经
        `NOT EXISTS face_verdict` 过滤掉它，所以"从出现的地方消失"这件事
        不需要靠删行来实现。删一行判定 = 一切照旧回来（迁移 009 的注释里
        写的就是这个承诺）。返回受影响的人，是因为**他们的标签集合变了** ——
        少了一张可用的正/负例，判定线要跟着重画。
        """
        ids = [int(f) for f in face_ids]
        if not ids:
            return []
        now = utc_now()
        with self.db.write() as conn:
            marks = ",".join("?" for _ in ids)
            affected = {int(r["person_id"]) for r in conn.execute(
                f"SELECT person_id FROM person_face WHERE face_id IN ({marks})",
                ids).fetchall()}
            affected.update(int(r["person_id"]) for r in conn.execute(
                f"SELECT person_id FROM person_reject WHERE face_id IN ({marks})",
                ids).fetchall())
            for face_id in ids:
                conn.execute(
                    "INSERT INTO face_verdict (face_id, verdict, note, created_at, updated_at)"
                    " VALUES (?, 'not_person', ?, ?, ?)"
                    " ON CONFLICT(face_id) DO UPDATE SET verdict = excluded.verdict,"
                    " note = excluded.note, updated_at = excluded.updated_at",
                    (face_id, note, now, now))
        return sorted(affected)

    def clear_not_person(self, face_ids: Sequence[int]) -> tuple[int, list[int]]:
        """撤销"不是人"。返回 `(删掉的判定条数, 受影响的人物 id)`。

        为什么要报受影响的人：撤销之后这张脸**又回到他们的样本里**了
        （判定只是一张滤镜，归属一直留着），所以两边都得重算判据 ——
        少了这一步，撤销之后"已确认 N 张"对了、判定线却还停在标错时的样子。
        """
        ids = [int(f) for f in face_ids]
        if not ids:
            return 0, []
        marks = ",".join("?" for _ in ids)
        with self.db.write() as conn:
            affected = {int(r["person_id"]) for r in conn.execute(
                f"SELECT person_id FROM person_face WHERE face_id IN ({marks})",
                ids).fetchall()}
            affected.update(int(r["person_id"]) for r in conn.execute(
                f"SELECT person_id FROM person_reject WHERE face_id IN ({marks})",
                ids).fetchall())
            cursor = conn.execute(
                f"DELETE FROM face_verdict WHERE face_id IN ({marks})", ids)
            removed = int(cursor.rowcount or 0)
        return removed, sorted(affected)

    def not_person_faces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """标过"不是人"的脸（带原片路径与拍摄时刻 —— 撤销之前得能看一眼原片）。"""
        return [dict(r) for r in self.db.query(
            "SELECT fv.face_id, fv.created_at AS marked_at, f.asset_id, f.shot_id,"
            "       m.capture_at, a.relative_path"
            "  FROM face_verdict fv JOIN face f ON f.id = fv.face_id"
            "  LEFT JOIN asset a ON a.id = f.asset_id"
            "  LEFT JOIN metadata m ON m.asset_id = f.asset_id"
            " WHERE fv.verdict = 'not_person'"
            " ORDER BY fv.created_at DESC, fv.face_id LIMIT ?", (int(limit),))]

    def not_person_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM face_verdict", default=0))

    def faces(self, person_id: int, *, limit: int | None = None,
              source: str | None = None) -> list[dict[str, Any]]:
        """某人名下的脸（界面用来渲染人脸墙）。带 shot / asset / 拍摄时刻。

        带原片路径与时刻是**刻意的**：人脸墙上只有裁出来的脸，用户看完必然要
        "去看原片"（那一张的构图、场合、旁边还有谁）。没有这一列就等于找不到照片。

        排除被标成"不是人"的脸：正常情况下 `mark_not_person` 已经把归属摘掉了，
        这里再挡一次是为了兜住历史数据（先标了"不是人"、后补的归属）。
        """
        sql = (
            "SELECT f.id AS face_id, f.asset_id, f.shot_id, f.box_x, f.box_y, f.box_w, f.box_h,"
            "       f.det_score, f.face_px, f.is_frontal, pf.source, pf.confidence, pf.similarity,"
            "       m.capture_at, a.relative_path, s.scene_id,"
            "       (SELECT COUNT(*) FROM asset a2 WHERE a2.shot_id = f.shot_id) AS asset_count"
            "  FROM person_face pf JOIN face f ON f.id = pf.face_id"
            "  LEFT JOIN asset a ON a.id = f.asset_id"
            "  LEFT JOIN metadata m ON m.asset_id = f.asset_id"
            "  LEFT JOIN shot s ON s.id = f.shot_id AND s.retired_at IS NULL"
            " WHERE pf.person_id = ?"
            "   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id)"
        )
        params: list[Any] = [int(person_id)]
        if source:
            sql += " AND pf.source = ?"
            params.append(source)
        sql += " ORDER BY (pf.source = 'user') DESC, m.capture_at, f.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.db.query(sql, params)]

    def rejected_faces(self, person_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """被否决的脸。**同样带原片路径** —— 用户点「其实是他」之前，得能先看一眼原片。"""
        return [dict(r) for r in self.db.query(
            "SELECT pr.face_id, pr.created_at, f.asset_id, f.shot_id, f.box_x, f.box_y,"
            "       f.box_w, f.box_h, m.capture_at, a.relative_path, s.scene_id"
            "  FROM person_reject pr JOIN face f ON f.id = pr.face_id"
            "  LEFT JOIN metadata m ON m.asset_id = f.asset_id"
            "  LEFT JOIN asset a ON a.id = f.asset_id"
            "  LEFT JOIN shot s ON s.id = f.shot_id AND s.retired_at IS NULL"
            " WHERE pr.person_id = ?"
            "   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id)"
            " ORDER BY pr.created_at DESC LIMIT ?",
            (int(person_id), int(limit)),
        )]

    def labels(self, person_id: int) -> dict[str, list[int]]:
        """学习用的带标注样本。

        `positive` = 用户确认过的**加上**模型提议里被用户默认接受的（仍是 auto）——
        但两者在阈值学习时的权重不同，所以分开返回。
        `negative` = 用户明确否决的。`unknown` 不返回：它不是负样本。

        ★ 三类样本都排除"不是人"的脸。放进去的后果很具体：
        一张海报能被当成"这个人极不像的样本"（压低判定线）或被当成正例
        （抬高原型）—— 而它根本不该参与"这个人长什么样"的估计。

        ⚠️ 子查询里的 `face_id` **必须写全表名**。写成 `fv.face_id = face_id` 时
        SQLite 会把它解析成子查询自己的列（`fv.face_id = fv.face_id`），
        于是条件恒真、`NOT EXISTS` 恒假 —— 只要库里有一条"不是人"的判定，
        **所有人的标签集合都会变成空**。这真的发生过：界面上"已确认 0 张"、
        重算判据直接跳过。列名撞车时一律限定表名。
        """
        def exclude(outer: str) -> str:
            return (" AND NOT EXISTS (SELECT 1 FROM face_verdict fv"
                    f" WHERE fv.face_id = {outer}.face_id)")

        positives = [int(r["face_id"]) for r in self.db.query(
            "SELECT face_id FROM person_face WHERE person_id = ? AND source = 'user'"
            + exclude("person_face") + " ORDER BY face_id", (int(person_id),))]
        auto = [int(r["face_id"]) for r in self.db.query(
            "SELECT face_id FROM person_face WHERE person_id = ? AND source = 'auto'"
            + exclude("person_face") + " ORDER BY face_id", (int(person_id),))]
        negative = [int(r["face_id"]) for r in self.db.query(
            "SELECT face_id FROM person_reject WHERE person_id = ?"
            + exclude("person_reject") + " ORDER BY face_id", (int(person_id),))]
        return {"positive": positives, "auto": auto, "negative": negative}

    # ------------------------------------------------------------------ 原型（学习结果）
    def save_profile(self, person_id: int, *, model: str, model_version: str,
                     prep_version: str, dim: int, normalization: str = "l2",
                     vector: bytes, negative_vector: bytes | None = None,
                     pos_count: int = 0, neg_count: int = 0,
                     threshold: float | None = None, margin: float | None = None) -> None:
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO person_profile (person_id, model, model_version, prep_version, dim,"
                " normalization, vector, negative_vector, pos_count, neg_count, threshold, margin,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(person_id, model, model_version, prep_version) DO UPDATE SET"
                " vector = excluded.vector, negative_vector = excluded.negative_vector,"
                " pos_count = excluded.pos_count, neg_count = excluded.neg_count,"
                " threshold = excluded.threshold, margin = excluded.margin,"
                " updated_at = excluded.updated_at",
                (int(person_id), model, model_version, prep_version, int(dim), normalization,
                 bytes(vector), negative_vector, int(pos_count), int(neg_count),
                 threshold, margin, now),
            )

    def profiles(self, *, model: str, model_version: str, prep_version: str
                 ) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM person_profile WHERE model = ? AND model_version = ?"
            " AND prep_version = ? ORDER BY person_id",
            (model, model_version, prep_version))]

    def profile(self, person_id: int, *, model: str, model_version: str,
                prep_version: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM person_profile WHERE person_id = ? AND model = ? AND model_version = ?"
            " AND prep_version = ?",
            (int(person_id), model, model_version, prep_version))
        return dict(row) if row else None

    # ------------------------------------------------------------------ 未归类（增量的输入）
    def unassigned_face_ids(self, *, model: str, model_version: str, prep_version: str,
                            only_frontal: bool = False) -> list[int]:
        """有特征、但**还没有归到任何人名下**的脸。

        刻意**不排除**被某人否决过的脸：被 A 否决不影响它可能是 B。
        是否要跳过由调用方按 `person_reject` 决定。

        但**排除被标成"不是人"的脸**：那是"这压根不是一个人"的判断，
        再拿它去聚类只会把不相干的人粘在一起（用户报过"有的分组里啥都有"）。

        用途：界面上的"还有多少张没归属"这个数字 + `people discover` 的输入。
        **增量归类不要用它** —— 那需要 `unclaimed_face_ids`（见下）。
        """
        sql = (
            "SELECT fe.face_id FROM face_embedding fe JOIN face f ON f.id = fe.face_id"
            " WHERE fe.model = ? AND fe.model_version = ? AND fe.prep_version = ?"
            "   AND NOT EXISTS (SELECT 1 FROM person_face pf WHERE pf.face_id = fe.face_id)"
            "   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = fe.face_id)"
        )
        if only_frontal:
            sql += " AND f.is_frontal = 1"
        sql += " ORDER BY fe.face_id"
        return [int(r["face_id"]) for r in self.db.query(
            sql, (model, model_version, prep_version))]

    def unclaimed_face_ids(self, *, model: str, model_version: str, prep_version: str,
                           only_frontal: bool = False) -> list[int]:
        """**还没有被用户确认给任何人**的脸 —— 增量归类与候选建议的正确输入。

        与 `unassigned_face_ids` 的差别只有一条，但很关键：
        `unassigned` 要求"一条归属都没有"，而这里只要求"没有**用户确认**过的归属"。

        为什么增量要用后者：模型自己提出来的归属（`source='auto'`）**不算数**。
        如果按 `unassigned` 取，一张被模型随手放进"某人"名下的脸就再也不会
        被提议给真正的那个人了 —— 模型的初筛错误会被永久固化，
        而用户的确认恰恰是要纠正它。反过来，用户确认过的脸**必须排除**：
        同一张脸同时被两个人"确认"，归属就矛盾了。

        同样排除"不是人"的脸（见 `unassigned_face_ids`）。
        """
        sql = (
            "SELECT fe.face_id FROM face_embedding fe JOIN face f ON f.id = fe.face_id"
            " WHERE fe.model = ? AND fe.model_version = ? AND fe.prep_version = ?"
            "   AND NOT EXISTS (SELECT 1 FROM person_face pf WHERE pf.face_id = fe.face_id"
            "                    AND pf.source = 'user')"
            "   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = fe.face_id)"
        )
        if only_frontal:
            sql += " AND f.is_frontal = 1"
        sql += " ORDER BY fe.face_id"
        return [int(r["face_id"]) for r in self.db.query(
            sql, (model, model_version, prep_version))]

    # ------------------------------------------------------------------ 事件
    def log_event(self, action: str, *, person_id: int | None = None,
                  face_id: int | None = None, detail: dict[str, Any] | None = None) -> None:
        payload = None
        if detail is not None:
            payload = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        self.db.execute(
            "INSERT INTO person_event (person_id, face_id, action, detail_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (None if person_id is None else int(person_id),
             None if face_id is None else int(face_id), action, payload, utc_now()),
        )

    def events(self, person_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM person_event WHERE person_id = ? ORDER BY id DESC LIMIT ?",
            (int(person_id), int(limit)))]

    def stats(self) -> dict[str, Any]:
        """人物页顶部的计数。**同样要减去"不是人"的脸**（见 `list_persons` 的说明）。"""
        not_person = ("AND NOT EXISTS (SELECT 1 FROM face_verdict fv"
                      " WHERE fv.face_id = person_face.face_id)")
        return {
            "persons": int(self.db.scalar("SELECT COUNT(*) FROM person", default=0)),
            "named": int(self.db.scalar(
                "SELECT COUNT(*) FROM person WHERE status = 'named'", default=0)),
            "proposed": int(self.db.scalar(
                "SELECT COUNT(*) FROM person WHERE status = 'proposed'", default=0)),
            "archived": int(self.db.scalar(
                "SELECT COUNT(*) FROM person WHERE status = 'archived'", default=0)),
            "confirmed_faces": int(self.db.scalar(
                f"SELECT COUNT(*) FROM person_face WHERE source = 'user' {not_person}",
                default=0)),
            "auto_faces": int(self.db.scalar(
                f"SELECT COUNT(*) FROM person_face WHERE source = 'auto' {not_person}",
                default=0)),
            "rejected_faces": int(self.db.scalar(
                "SELECT COUNT(*) FROM person_reject pr WHERE NOT EXISTS"
                " (SELECT 1 FROM face_verdict fv WHERE fv.face_id = pr.face_id)",
                default=0)),
            "not_person_faces": self.not_person_count(),
        }


class ShotRepo:
    """`shot` / `scene` 的读写（Phase 2 · M2/M4 的落库）。

    ## 本类最重要的约束：只增不删（决策 #4）

    `selection.shot_id` 是 `ON DELETE CASCADE`。若重算导致某个 Shot 消失，
    **精选记录会被连带删掉** —— 那是不可接受的数据丢失。

    因此 `apply_regroup()` 一律**不物理删除**：
      * 资产集合与新方案一致的旧 Shot → 原地更新（`group_revision += 1`）
      * 不再对应任何新分组的旧 Shot → 标 `retired_at`，**保留行**
      * `group_source='user'` 的 Shot → **冻结**，既不复用也不退役（决策 #10）

    并有一条不变式（由 `assert_invariants()` 检查，测试里每步都调）：
    **任何重算前后 `selection` 行数只增不减**。

    ## 时间与资产

    `shot.captured_at` 用**组内最早的照片本地时间**（视频的容器 UTC 不参与，
    否则会把 Shot 时间带偏一个时区 —— 见 `library/shot_grouping.py`）。
    `asset.shot_id` 是关联链路 `asset → shot → scene → session` 的关键一跳。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读
    def get(self, shot_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM shot WHERE id = ?", (int(shot_id),))

    def active_assets(self) -> dict[int, frozenset[int]]:
        """`{shot_id: frozenset(asset_id)}`，**只含未退役**的 Shot。

        这是 `plan_regroup()` 的输入：重算时用它判断"哪些旧 Shot 可以复用"。
        """
        rows = self.db.query(
            "SELECT sh.id AS shot_id, a.id AS asset_id"
            "  FROM shot sh JOIN asset a ON a.shot_id = sh.id"
            " WHERE sh.retired_at IS NULL"
        )
        out: dict[int, set[int]] = {}
        for row in rows:
            out.setdefault(int(row["shot_id"]), set()).add(int(row["asset_id"]))
        return {k: frozenset(v) for k, v in out.items()}

    def protected_ids(self) -> frozenset[int]:
        """必须冻结的 Shot：用户手工调过的（`group_source='user'`）。

        决策 #10：用户一改即置 `user`，**永不被后续重算覆盖**。
        """
        rows = self.db.query(
            "SELECT id FROM shot WHERE group_source = 'user' AND retired_at IS NULL"
        )
        return frozenset(int(r["id"]) for r in rows)

    def protected_asset_ids(self) -> frozenset[int]:
        """**被冻结 Shot 占有的资产** —— 重算时必须在分组之前就摘出去。

        为什么需要这个方法（实测到的严重缺陷）：只把冻结 Shot 从"可复用集合"里
        排除是**不够的**。它们名下的资产仍会进入 `group_by_directory()`，
        于是新分组里带着这些资产，`apply_regroup()` 的
        `UPDATE asset SET shot_id = ?` 就把它们**搬走**了 —— 而 Shot 行还在、
        `group_source='user'` 还在，看起来"没被动过"。

        实测现场（真实库快照）：把一个 4 张资产的 Shot 手工拆分（两边都冻结），
        再改阈值重算 —— 两个冻结 Shot 的资产**全部被搬走**，变成空壳，
        而它们的 `selection` 行仍挂着 → 用户点进"锁定的组"看到的是空的。

        `apply_regroup()` 里那条"冻结的 Shot 不得出现在方案里"的检查**拦不住**
        这种情况：方案里出现的是一批**新** Shot（`shot_id=None`），
        被搬走的只是资产，检查看不到。
        """
        rows = self.db.query(
            "SELECT a.id FROM asset a JOIN shot sh ON sh.id = a.shot_id"
            " WHERE sh.group_source = 'user' AND sh.retired_at IS NULL"
            "   AND a.shot_id IS NOT NULL"
        )
        return frozenset(int(r["id"]) for r in rows)

    def count(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM shot"
        if active_only:
            sql += " WHERE retired_at IS NULL"
        return int(self.db.scalar(sql, default=0))

    def scene_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM scene", default=0))

    def asset_count(self, shot_id: int) -> int:
        """某个 Shot 名下有几个资产（人工拆分/合并后回显用）。"""
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM asset WHERE shot_id = ?", (int(shot_id),), default=0
            )
        )

    def unassigned_asset_count(self, *, library_id: int | None = None) -> int:
        """还没挂到任何 Shot 的资产数（用于判断"要不要重算"）。"""
        sql = (
            "SELECT COUNT(*) FROM asset a LEFT JOIN shot sh ON sh.id = a.shot_id"
            " WHERE sh.id IS NULL AND a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
        )
        params: list[Any] = []
        if library_id is not None:
            sql += " AND a.library_id = ?"
            params.append(int(library_id))
        return int(self.db.scalar(sql, params, default=0))

    # ------------------------------------------------------------------ 写：Shot
    def _apply_successors(self, conn: Any, successor: dict[int, int], stamp: str) -> dict[str, int]:
        """把 `{退役 Shot: 继承者 Shot}` 应用到**所有引用 shot 的地方**。

        ## 为什么要有这个函数（四次缺陷的教训）

        Shot 是"软删除"的（标 `retired_at`），而**所有界面/统计查询都带
        `retired_at IS NULL`**。于是任何指向退役 Shot 的引用都会**用户看不见**：
        行还在、断言全绿、用户却觉得东西丢了。这一个模式在四处反复出现：

        | # | 引用 | 症状 |
        | --- | --- | --- |
        | §13.35 | `selection.shot_id`（重算）| 精选行还在，界面 FINAL 从 60 变 51 |
        | §13.36 | 冻结 Shot 的**资产** | 用户锁定的组被搬空 |
        | §13.37 | `selection.shot_id`（手工合并）| 合并前标的精选看不见 |
        | §13.38 | `collection_item.shot_id` | 主题少了 18 条，列表/详情计数对不上 |

        **共同根因**：写入侧没有任何人负责把引用搬到新 Shot 上。
        所以这里把"引用搬迁"收成**一个地方**，让"还有哪些引用"这个问题只回答一次。

        引用清单（迁移 001/006 里所有 `REFERENCES shot(id)` 的列）：
          * `asset.shot_id`   —— 由调用方的 `UPDATE asset SET shot_id` 负责（改挂到新分组）
          * `selection.shot_id` —— 本函数负责搬（含 `selection_event` 留痕）
          * `collection_item.shot_id` —— 本函数负责搬（连 `sort_order` / `note`）
          * `selection_event.shot_id` —— **历史，刻意不动**（"发生过什么"不能改写）
          * `collection.cover_shot_id` —— 本函数跟着继承者走
        """
        selection_migrated = 0
        collection_items_moved = 0
        covers_moved = 0

        for old_shot_id, target in sorted(successor.items()):
            # ---- 精选
            row = conn.execute(
                "SELECT status, reason FROM selection WHERE shot_id = ?",
                (int(old_shot_id),)).fetchone()
            if row is not None:
                taken = conn.execute("SELECT 1 FROM selection WHERE shot_id = ?",
                                     (target,)).fetchone()
                if taken is None:
                    conn.execute(
                        "INSERT INTO selection (shot_id, status, reason, updated_at)"
                        " VALUES (?, ?, ?, ?)",
                        (target, str(row["status"]), row["reason"], stamp))
                    conn.execute(
                        "INSERT INTO selection_event (shot_id, from_status, to_status,"
                        " reason, created_at) VALUES (?, NULL, ?, ?, ?)",
                        (target, str(row["status"]),
                         f"migrated_from:{int(old_shot_id)}", stamp))
                    selection_migrated += 1
                # 旧行删掉：同一个决定搬了家，不是两个决定（留着会让统计虚高）
                conn.execute("DELETE FROM selection WHERE shot_id = ?",
                             (int(old_shot_id),))

            # ---- 主题成员
            # `(collection_id, shot_id)` 是主键：目标主题里已有同一张就跳过
            # （但旧行仍要删，否则会永远藏着一条看不见的重复）。
            for item in conn.execute(
                "SELECT collection_id, sort_order, note, added_at FROM collection_item"
                " WHERE shot_id = ?", (int(old_shot_id),)
            ).fetchall():
                cid = int(item["collection_id"])
                exists = conn.execute(
                    "SELECT 1 FROM collection_item WHERE collection_id = ? AND shot_id = ?",
                    (cid, target)).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO collection_item (collection_id, shot_id, sort_order,"
                        " note, added_at) VALUES (?, ?, ?, ?, ?)",
                        (cid, target, int(item["sort_order"]), item["note"],
                         item["added_at"]))
                    collection_items_moved += 1
                conn.execute(
                    "DELETE FROM collection_item WHERE collection_id = ? AND shot_id = ?",
                    (cid, int(old_shot_id)))

            # ---- 主题的**显式封面**
            # 不搬的话，`list_all()` 的 `COALESCE(cover_shot_id, 自动首条)`
            # 会优先取那个已退役的封面 —— 主题封面因此在界面上取不到图。
            cursor = conn.execute(
                "UPDATE collection SET cover_shot_id = ?, updated_at = ?"
                " WHERE cover_shot_id = ?", (target, stamp, int(old_shot_id)))
            covers_moved += cursor.rowcount or 0

        return {"selection_migrated": selection_migrated,
                "collection_items_moved": collection_items_moved,
                "collection_covers_moved": covers_moved}

    def reconcile_stranded_references(self) -> dict[str, int]:
        """兜底修复：把**还搁浅在退役 Shot 上**的引用搬到它们的资产继承者上。

        ## 为什么光有"搬迁"还不够（实测到的并发竞态）

        `apply_regroup()` 里的搬迁是在一个写事务内做完的，看起来天衣无缝。
        但它只能搬**那一刻已经存在**的引用。真实用法里界面是开着的：

        ```
        用户：标 FINAL(shot 52)          ← serve 进程写入
        重算：把 shot 52 退役 + 搬引用    ← CLI 进程写入
        ```

        如果"标 FINAL"发生在重算**算出继承者映射之后、提交之前**（或干脆在提交之后），
        那条精选就落在一个**已被退役**的 Shot 上，而没有任何人再回头看它。

        **实测**（真实库快照，一个线程按 4 ms 间隔标 300 个 Shot，同时跑
        `shots regroup --gap-seconds 2`，耗时 1.5 s）：

        * 写入异常 **0** 条（没有 `database is locked` —— WAL + busy_timeout 是对的）
        * 但表里 300 行、**可见只有 282 行** —— **18 条精选静默搁浅**，九条不变式里的 I2 变红

        这正是"没有报错但用户丢了东西"，所以必须有一个**幂等的兜底**：
        每次重算之后再跑一遍，把任何仍然搁浅的引用搬走。
        推进顺序与 `apply_regroup` 一致（资产多数归属，并列取小 id），因此可重复调用。
        """
        with self.db.read() as conn:
            stranded = [int(r["shot_id"]) for r in conn.execute(
                "SELECT s.shot_id FROM selection s JOIN shot sh ON sh.id = s.shot_id"
                " WHERE sh.retired_at IS NOT NULL"
                " UNION"
                " SELECT ci.shot_id FROM collection_item ci JOIN shot sh ON sh.id = ci.shot_id"
                " WHERE sh.retired_at IS NOT NULL"
                " UNION"
                " SELECT c.cover_shot_id FROM collection c JOIN shot sh ON sh.id = c.cover_shot_id"
                " WHERE c.cover_shot_id IS NOT NULL AND sh.retired_at IS NOT NULL")]
            if not stranded:
                return {"selection_migrated": 0, "collection_items_moved": 0,
                        "collection_covers_moved": 0, "stranded_shots": 0}
            # ① 优先用**记在退役行上**的继承者（`regroup:into:<id>` / `merged_into:<id>`）。
            #    这是唯一可靠的来源：`asset.shot_id` 已经被覆盖，"资产多数归属"
            #    在退役 Shot 名下已无资产时**推断不出来**（实测就是这样追不回的）。
            recorded: dict[int, int] = {}
            for row in conn.execute(
                "SELECT id, retired_reason FROM shot WHERE id IN (%s)"
                % ", ".join("?" for _ in stranded),
                stranded,
            ):
                reason = str(row["retired_reason"] or "")
                if ":" in reason:
                    _, _, tail = reason.partition(":")
                    tail = tail.replace("into:", "")      # merged_into:<id> / regroup:into:<id>
                    if tail.isdigit():
                        recorded[int(row["id"])] = int(tail)
            # ② 记录不可用时的**兜底推断**：看它原来的资产现在归到哪（老库兼容）。
            members: dict[int, list[int]] = {}
            for row in conn.execute(
                "SELECT shot_id, id FROM asset WHERE shot_id IN (%s)"
                % ", ".join("?" for _ in stranded),
                stranded,
            ):
                members.setdefault(int(row["shot_id"]), []).append(int(row["id"]))

        successor: dict[int, int] = dict(recorded)
        for old_shot_id, asset_ids in members.items():
            if old_shot_id in successor or not asset_ids:
                continue
            placeholders = ", ".join("?" for _ in asset_ids)
            row = self.db.query_one(
                f"SELECT shot_id FROM asset WHERE id IN ({placeholders})"
                "  AND shot_id IS NOT NULL"
                " GROUP BY shot_id ORDER BY COUNT(*) DESC, shot_id ASC LIMIT 1",
                asset_ids,
            )
            if row is not None and int(row["shot_id"]) != old_shot_id:
                successor[old_shot_id] = int(row["shot_id"])
        # 只保留指向**活跃** Shot 的继承者（记下来的那个也可能后来又被退役了）
        if successor:
            live = {int(r["id"]) for r in self.db.query(
                "SELECT id FROM shot WHERE id IN (%s) AND retired_at IS NULL"
                % ", ".join("?" for _ in successor.values()),
                sorted(set(successor.values())))}
            successor = {old: new for old, new in successor.items() if new in live}
        if not successor:
            return {"selection_migrated": 0, "collection_items_moved": 0,
                    "collection_covers_moved": 0, "stranded_shots": len(stranded)}

        from ..db import utc_now

        stamp = utc_now()
        with self.db.write() as conn:
            moved = self._apply_successors(conn, successor, stamp)
        moved["stranded_shots"] = len(stranded)
        return moved

    def apply_regroup(
        self,
        *,
        shot_rows: Sequence[dict[str, Any]],
        retired: Sequence[int] = (),
        frozen: frozenset[int] = frozenset(),
    ) -> dict[str, int]:
        """把一次 Shot 重算的结果落库（**只增不删**）。

        参数
        ----
        shot_rows
            每个 Shot 一行：至少含 `assets`（asset_id 列表）、`captured_at`、
            `primary_asset_id`、`group_confidence`、以及可选的 `shot_id`
            （有 = 复用既有 Shot，无 = 新建）。
        retired
            要标记退役的旧 Shot id（**不删除**）。
        frozen
            要冻结的 Shot id（`group_source='user'`）—— 会被断言为"既不在
            `shot_rows` 里被改写，也不在 `retired` 里"。

            ⚠️ **可以省略**：省略时会**从库里自查**（`protected_ids()`）并与传入值取并集。
            这一点很要紧 —— 早期实现只信这个参数，于是**任何忘了传 `frozen=` 的调用方
            都会让两道守卫变成空操作**（实测：冻结的 Shot 被静默改写成 `[1,2,3,4]`）。
            守卫依赖"调用方记得传参"等于没有守卫。
        """
        from ..db import utc_now

        stamp = utc_now()
        created = updated = retired_n = 0

        # ★ 冻结集合 = 调用方给的 ∪ **库里查到的**。
        # 不这么做的话，漏传 `frozen=` 会让下面两道守卫全部失效（静默做错）。
        effective_frozen = frozenset(int(s) for s in frozen) | self.protected_ids()
        conflict = sorted(set(int(s) for s in retired) & set(effective_frozen))
        if conflict:
            raise ValueError(f"冻结的 Shot 不允许同时被退役：{conflict}")

        # `selection.shot_id` 是主键（一个 Shot 一行），`selection_event` 靠它追溯。
        # 退役前先记下这些 Shot 的资产集合 —— 这个映射**马上就会被下面的
        # `UPDATE asset SET shot_id` 覆盖掉**，事后无从恢复。精选迁移要靠它。
        retiring_assets: dict[int, list[int]] = {}
        if retired:
            rows = self.db.query(
                "SELECT shot_id, id FROM asset WHERE shot_id IN (%s)"
                % ", ".join("?" for _ in retired),
                [int(s) for s in retired],
            )
            for row in rows:
                retiring_assets.setdefault(int(row["shot_id"]), []).append(int(row["id"]))

        # ★ 冻结的 Shot 不得出现在 `shot_rows` 里。
        # 为什么是"报错"而不是"跳过"：早期实现是 `continue` 跳过那一行，
        # 但那**只跳过了 Shot 本身的 UPDATE**，下面的 `UPDATE asset SET shot_id`
        # 仍然会跑 —— 结果是把用户手工锁定的 Shot 的资产**搬走**，
        # 而它的 group_source='user' 还在，看起来"没被动过"。静默做错比报错危险得多。
        # 正确分工：调用方负责把冻结的 Shot 从新方案里排除
        # （`plan_regroup(..., protected=...)` 已经这么做了）。
        offender = sorted(
            int(row["shot_id"])
            for row in shot_rows
            if row.get("shot_id") is not None and int(row["shot_id"]) in effective_frozen
        )
        if offender:
            raise ValueError(
                f"冻结的 Shot（group_source='user'）不允许出现在重算方案里：{offender}。"
                "调用方应先用 plan_regroup(protected=...) 把它们排除。"
            )

        # ★ 同样地：冻结 Shot 的**资产**也不得出现在方案里。
        #
        # 这是上一条检查拦不住的情形：方案里出现的是**新** Shot（`shot_id=None`），
        # 所以"冻结 Shot 出现在方案里"检查看不到任何问题；但那些资产原本属于
        # 某个冻结 Shot，下面那句 `UPDATE asset SET shot_id = ?` 会把它们**搬走** ——
        # 冻结的 Shot 因此变成空壳，而 `group_source='user'` 还在、`selection` 还挂着，
        # 用户点进"我锁定的那个组"看到的是空的。
        #
        # 实测（真实库快照）：手工拆分一个 4 张资产的 Shot（两边冻结）后改阈值重算，
        # 两个冻结 Shot 的资产**全部被搬空**。所以这里必须报错，
        # 强制调用方在**分组之前**就把这些资产摘出去（`protected_asset_ids()`）。
        protected_assets: set[int] = set()
        # ⚠️ 不能只信调用方传进来的 `frozen`：漏传就等于这道守卫不存在。
        # 冻结集合是**从库里查得出来的事实**，所以这里自己兜一次底（并集）。
        effective_frozen = set(int(s) for s in frozen) | set(self.protected_ids())
        if effective_frozen:
            rows_ = self.db.query(
                "SELECT a.id FROM asset a WHERE a.shot_id IN (%s)"
                % ", ".join("?" for _ in effective_frozen),
                sorted(effective_frozen),
            )
            protected_assets = {int(r["id"]) for r in rows_}
        if protected_assets:
            stolen: dict[int, list[int]] = {}
            for row in shot_rows:
                hit = [int(a) for a in (row.get("assets") or [])
                       if int(a) in protected_assets]
                if hit:
                    stolen[int(row.get("shot_id") or 0)] = hit
            if stolen:
                raise ValueError(
                    "重算方案里包含属于**冻结 Shot** 的资产 —— 会导致用户锁定的组被搬空："
                    f"{stolen}。调用方应先用 protected_asset_ids() 在分组前排除它们。"
                )

        with self.db.write() as conn:
            for row in shot_rows:
                shot_id = row.get("shot_id")
                assets = [int(a) for a in row.get("assets") or []]
                primary = row.get("primary_asset_id")
                if primary is None and assets:
                    primary = assets[0]
                if shot_id is None:
                    cursor = conn.execute(
                        "INSERT INTO shot (scene_id, captured_at, capture_date_local,"
                        " primary_asset_id, camera_key, group_source, group_confidence,"
                        " group_revision, created_at, updated_at)"
                        " VALUES (NULL, ?, ?, ?, ?, 'auto', ?, 1, ?, ?)",
                        (
                            row.get("captured_at"),
                            (row.get("captured_at") or "")[:10] or None,
                            primary,
                            row.get("camera_key"),
                            row.get("group_confidence"),
                            stamp,
                            stamp,
                        ),
                    )
                    shot_id = int(cursor.lastrowid)
                    created += 1
                else:
                    conn.execute(
                        "UPDATE shot SET captured_at = ?, capture_date_local = ?,"
                        " primary_asset_id = ?, camera_key = ?, group_confidence = ?,"
                        " group_revision = group_revision + 1, updated_at = ?"
                        " WHERE id = ?",
                        (
                            row.get("captured_at"),
                            (row.get("captured_at") or "")[:10] or None,
                            primary,
                            row.get("camera_key"),
                            row.get("group_confidence"),
                            stamp,
                            int(shot_id),
                        ),
                    )
                    updated += 1
                if assets:
                    placeholders = ", ".join("?" for _ in assets)
                    conn.execute(
                        f"UPDATE asset SET shot_id = ? WHERE id IN ({placeholders})",
                        [int(shot_id), *assets],
                    )

            # ★ 先算出"每个退役 Shot 的资产继承者" —— **必须**在标记退役之前算，
            # 因为退役行上要记 `regroup:into:<继承者>`，而且下面 `_apply_successors`
            # 也要用同一份映射。
            #
            # 继承者规则（保守、可预测）：
            #   * 只看**退休前属于该 Shot** 的资产现在归到了哪个新 Shot；
            #   * 取**重合最多**的那个（并列时取 id 最小者，保证确定性）。
            successor: dict[int, int] = {}
            for old_shot_id, asset_ids in sorted(retiring_assets.items()):
                if int(old_shot_id) not in set(int(s) for s in retired) or not asset_ids:
                    continue
                placeholders = ", ".join("?" for _ in asset_ids)
                target_row = conn.execute(
                    f"SELECT shot_id FROM asset WHERE id IN ({placeholders})"
                    "  AND shot_id IS NOT NULL"
                    " GROUP BY shot_id ORDER BY COUNT(*) DESC, shot_id ASC LIMIT 1",
                    asset_ids,
                ).fetchone()
                if target_row is None:
                    continue
                target = int(target_row["shot_id"])
                if target != int(old_shot_id):
                    successor[int(old_shot_id)] = target

            # ★ 把"继承者"**记在退役行上**（`retired_reason = 'regroup:into:<id>'`）。
            #
            # 为什么必须记：下一个事务会执行 `UPDATE asset SET shot_id = ?`，
            # 把"这些资产原来属于哪个退役 Shot"这个事实**永久覆盖掉**。
            # 之后任何需要"这个退役 Shot 的东西去哪了"的逻辑（比如并发兜底
            # `reconcile_stranded_references()`）都无法再推断出来。
            #
            # 实测现场：并发标精选时有一条落在已退役的 Shot 上，
            # 兜底逻辑想追回它 —— 但那个 Shot 名下已经**没有资产**（全被搬走了），
            # 于是推断不出继承者，追不回来。记下 `<id>` 之后就能直接跟着走。
            for shot_id in retired:
                # ⚠️ 用 `effective_frozen`（= 调用方给的 ∪ 库里查到的），
                # 不能用裸 `frozen` —— 漏传 `frozen=` 时那样会**把用户锁定的组退役掉**。
                if int(shot_id) in effective_frozen:
                    continue
                target = successor.get(int(shot_id))
                reason = f"regroup:into:{target}" if target else "regroup"
                cursor = conn.execute(
                    "UPDATE shot SET retired_at = ?, retired_reason = ?"
                    " WHERE id = ? AND retired_at IS NULL",
                    (stamp, reason, int(shot_id)),
                )
                retired_n += cursor.rowcount or 0

            # ---- 把退役 Shot 上的引用迁到它的继承者
            #
            # 决策 #4 的承诺是「精选**绝不**因重算丢失」。只标 `retired_at` 保证了
            # **行还在**（`ON DELETE CASCADE` 不会触发），但所有会话/场景/统计查询都带
            # `retired_at IS NULL` —— 留在退役行上的精选**用户看不见**，
            # 等于决策 #4 的意图落空。
            #
            # 实测（真实库 21,400 资产、标 60 个多张资产的 Shot 为 FINAL，
            # 再把 `--gap-seconds` 从 3 改成 2）：**9 条精选落到了退役行上** ——
            # 行数断言"只增不减 60 → 60"照常通过，但界面上的 FINAL 从 60 变成 51。
            # 这正是"静默做错"最危险的形态：所有检查都绿，用户却丢了东西。
            #
            # 搬迁走共享助手 `_apply_successors`（精选 + 主题成员 + 主题封面），
            # 让"引用 shot 的地方有哪些"这个问题**只回答一次**。
            moved = self._apply_successors(conn, successor, stamp)
            return {"created": created, "updated": updated, "retired": retired_n,
                    **moved}

    def set_scene(self, shot_id: int, scene_id: int | None) -> bool:
        from ..db import utc_now

        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE shot SET scene_id = ?, updated_at = ? WHERE id = ?",
                (scene_id, utc_now(), int(shot_id)),
            )
        return bool(cursor.rowcount)

    def mark_user_adjusted(self, shot_id: int) -> bool:
        """用户手工合并/拆开后调用：置 `group_source='user'`，此后**永不参与重算**。"""
        from ..db import utc_now

        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE shot SET group_source = 'user', group_revision = group_revision + 1,"
                " updated_at = ? WHERE id = ?",
                (utc_now(), int(shot_id)),
            )
        return bool(cursor.rowcount)

    # ------------------------------------------------------------------ 人工调整分组
    def split_shot(self, shot_id: int, asset_ids: Sequence[int]) -> int:
        """把 `asset_ids` 从指定 Shot 中**拆出去**，成为一个新 Shot。

        这是 PRD §9.2「所有自动归组都**允许用户调整**」的具体落点，也是断点 #3
        「相似照片能被合理聚组，但用户可以手动修正」的后半句。

        语义（刻意保守）：
          * 只能拆出**当前属于该 Shot** 的资产；不在其中的会被拒绝（不静默忽略）
          * 拆出后**两个 Shot 都置 `group_source='user'`** —— 用户碰过的分组不该被重算覆盖
          * 原 Shot 必须至少留 1 个资产（否则应当用整组合并/退役，而不是"拆成空的"）
          * `selection` 行**留在原 Shot 上**（它是用户对"那次拍摄"的判断）；
            若原 Shot 的精选需要跟着某张走，由用户在界面上显式重标

        返回新 Shot 的 id。
        """
        from ..db import utc_now

        source = self.get(shot_id)
        if source is None or source["retired_at"] is not None:
            # 已退役的 Shot 还挂着 selection 行，但它的资产已经搬走了；
            # 在上面拆分会造出一个时间线对不上的孤儿 Shot。
            raise ValueError(f"Shot {shot_id} 不存在或已退役，不能拆分")
        members_now = sorted(
            int(r["id"]) for r in self.db.query(
                "SELECT id FROM asset WHERE shot_id = ?", (int(shot_id),))
        )
        if not members_now:
            raise ValueError(f"Shot {shot_id} 没有资产")
        moving = [int(a) for a in asset_ids]
        if not moving:
            raise ValueError("必须指定至少一个要拆出的资产")
        unknown = [a for a in moving if a not in members_now]
        if unknown:
            raise ValueError(f"这些资产不属于 Shot {shot_id}：{unknown}")
        remaining = [a for a in members_now if a not in moving]
        if not remaining:
            raise ValueError("不能把 Shot 的全部资产都拆出去 —— 那等于整组移动，请用合并/重算")

        stamp = utc_now()
        with self.db.write() as conn:
            # 新 Shot：沿用原 Shot 的场景/时间等派生线索，置信度标记为人工
            cursor = conn.execute(
                "INSERT INTO shot (scene_id, captured_at, capture_date_local, primary_asset_id,"
                " camera_key, group_source, group_confidence, group_revision, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'user', NULL, 1, ?, ?)",
                (
                    source["scene_id"] if source else None,
                    source["captured_at"] if source else None,
                    source["capture_date_local"] if source else None,
                    moving[0],
                    source["camera_key"] if source else None,
                    stamp,
                    stamp,
                ),
            )
            new_id = int(cursor.lastrowid)
            placeholders = ", ".join("?" for _ in moving)
            conn.execute(
                f"UPDATE asset SET shot_id = ? WHERE id IN ({placeholders})",
                [new_id, *moving],
            )
            # 两边都置 user：用户碰过的分组永不被重算覆盖（决策 #10）
            conn.execute(
                "UPDATE shot SET group_source = 'user',"
                " group_revision = group_revision + 1, updated_at = ? WHERE id IN (?, ?)",
                (stamp, int(shot_id), new_id),
            )
        return new_id

    def merge_shots(self, shot_ids: Sequence[int], *, keep: int | None = None) -> int:
        """把多个 Shot 合并成一个（保留 `keep`，其余资产的归属搬过去并**退役**这些行）。

        被合并掉的 Shot **不物理删除**（标 `retired_at` + `merged_into:<keep>`），
        这样"合并前发生过什么"永远查得到。

        **但它上面的精选会被搬到保留者上** —— 只留行是不够的：所有界面/统计查询都带
        `retired_at IS NULL`，留在退役行上的精选**用户看不见**（与 §13.35 同一类问题）。
        保留者已有精选时**让原有的赢**，并写一条 `migrated_from:<loser>` 事件留痕。

        返回保留的那个 Shot id。
        """
        from ..db import utc_now

        targets = [int(s) for s in shot_ids]
        if len(targets) < 2:
            raise ValueError("合并至少需要两个 Shot")
        rows = {int(r["id"]): r for r in self.db.query(
            "SELECT id, scene_id FROM shot WHERE id IN (%s) AND retired_at IS NULL"
            % ", ".join("?" for _ in targets), targets)}
        missing = [t for t in targets if t not in rows]
        if missing:
            raise ValueError(f"这些 Shot 不存在或已退役：{missing}")
        scenes = {int(r["scene_id"]) for r in rows.values() if r["scene_id"] is not None}
        if len(scenes) > 1:
            # 跨 Scene 合并会让"这次拍摄属于哪个场景"变得含糊，明确拒绝而不是随便挑一个
            raise ValueError(f"不能跨 Scene 合并（涉及 Scene {sorted(scenes)}）")

        keeper = int(keep) if keep is not None else targets[0]
        if keeper not in rows:
            raise ValueError(f"keep={keep} 不在要合并的 Shot 里")
        losers = [t for t in targets if t != keeper]

        stamp = utc_now()
        with self.db.write() as conn:
            for loser in losers:
                conn.execute("UPDATE asset SET shot_id = ? WHERE shot_id = ?", (keeper, loser))
                conn.execute(
                    "UPDATE shot SET retired_at = ?, retired_reason = ?, group_source = 'user',"
                    " group_revision = group_revision + 1, updated_at = ? WHERE id = ?",
                    (stamp, f"merged_into:{keeper}", stamp, loser),
                )
            # ---- 引用搬迁（**走同一个共享助手**）
            #
            # 这里刻意**不再内联**搬迁逻辑：内联的版本只搬了精选，漏掉了
            # 主题成员与主题封面（§13.38 / §13.39 就是这么漏的）。
            # 把"引用 shot 的地方有哪些"这个问题**只回答一次**，
            # 才不会每加一个功能就漏一处。
            #
            # 合并的语义与重算相同：被合并者 → 保留者（用户刚刚明确指定了继承者，
            # 比自动推断的"资产多数归属"更可靠）。保留者已有精选时**让原有的赢**。
            moved = self._apply_successors(
                conn, {int(loser): int(keeper) for loser in losers}, stamp)
            conn.execute(
                "UPDATE shot SET group_source = 'user', group_revision = group_revision + 1,"
                " updated_at = ? WHERE id = ?",
                (stamp, keeper),
            )
        return keeper

    # ------------------------------------------------------------------ 写：Scene
    def replace_scenes(
        self,
        *,
        session_id: int,
        scenes: Sequence[dict[str, Any]],
    ) -> dict[str, int]:
        """重建一个 Session 下的 Scene 划分。

        Scene 是**纯派生结构**（没有精选直接引用它，`shot.scene_id` 是 `SET NULL`），
        因此这里可以重建；但为了可追溯，仍然优先复用**名称与时间范围一致**的既有行，
        只新建/更新差异部分。

        每个 scene 行：`name` / `captured_from` / `captured_to` / `shot_ids` / `evidence_json`。
        """
        from ..db import utc_now

        stamp = utc_now()
        created = updated = cleared = 0
        with self.db.write() as conn:
            # ⚠️ **人工调整过的 Scene 不能被自动重算冲掉。**
            #
            # 合并/拆分（`merge_scenes` / `split_scene`）会把涉及的 Scene 标成
            # `source='user'`。只要这个 Session 里存在这样的行，本函数就**整段跳过**。
            #
            # 为什么整段而不是"逐条保留"：重建的流程是"先清空该 Session 下
            # 所有 `shot.scene_id`，再按新方案指派"。逐条保留会让被保留 Scene 的
            # Shot 在清空那一步失去归属（变成孤儿），反而更糟。
            pinned = int(conn.execute(
                "SELECT COUNT(*) FROM scene WHERE session_id = ? AND source = 'user'",
                (int(session_id),),
            ).fetchone()[0])
            if pinned:
                return {"created": 0, "updated": 0, "cleared": 0,
                        "removed_stale": 0, "skipped_pinned": pinned}

            existing = {
                str(r["name"]): int(r["id"])
                for r in conn.execute(
                    "SELECT id, name FROM scene WHERE session_id = ?", (int(session_id),)
                )
            }
            # 先把该 Session 下所有 Shot 的 scene_id 清空，再重新指派 ——
            # 避免"旧的 Scene 还挂着 Shot"造成两个 Scene 共享同一个 Shot。
            cursor = conn.execute(
                "UPDATE shot SET scene_id = NULL WHERE scene_id IN"
                " (SELECT id FROM scene WHERE session_id = ?)",
                (int(session_id),),
            )
            cleared += cursor.rowcount or 0

            for row in scenes:
                name = str(row["name"])
                evidence = row.get("evidence_json")
                shot_ids = [int(s) for s in row.get("shot_ids") or []]
                scene_id = existing.pop(name, None)
                if scene_id is None:
                    cursor = conn.execute(
                        "INSERT INTO scene (session_id, name, captured_from, captured_to,"
                        " dir_hint, source, confidence, group_revision, evidence_json,"
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 'auto', ?, 1, ?, ?, ?)",
                        (
                            int(session_id),
                            name,
                            row.get("captured_from"),
                            row.get("captured_to"),
                            row.get("dir_hint"),
                            row.get("confidence"),
                            evidence,
                            stamp,
                            stamp,
                        ),
                    )
                    scene_id = int(cursor.lastrowid)
                    created += 1
                else:
                    conn.execute(
                        "UPDATE scene SET captured_from = ?, captured_to = ?,"
                        " group_revision = group_revision + 1, evidence_json = ?, updated_at = ?"
                        " WHERE id = ?",
                        (
                            row.get("captured_from"),
                            row.get("captured_to"),
                            evidence,
                            stamp,
                            int(scene_id),
                        ),
                    )
                    updated += 1
                if shot_ids:
                    placeholders = ", ".join("?" for _ in shot_ids)
                    conn.execute(
                        f"UPDATE shot SET scene_id = ? WHERE id IN ({placeholders})",
                        [int(scene_id), *shot_ids],
                    )

            # ⚠️ **删掉这次没再用到的旧行**。
            #
            # `existing` 是按 `name` 复用的；循环结束后里面剩下的就是
            # "这次重算没有产生、但库里还留着的"旧 Scene。不删它们的话，
            # 每重算一次就积一批**零 Shot 的孤儿行**：Scene 数量只增不减，
            # 而界面按 Scene 列时段，于是同一个时段会重复出现好几次。
            #
            # 实测（真实库 2026-10-16）：342 个 Scene 行里 **303 个有 Shot、
            # 39 个是空的** —— 那 39 条就是这么攒下来的。隔离数据根里重算两次，
            # Scene 从 10 涨到 19，其中 9 条是孤儿。
            #
            # 删除是安全的：`scene` 是**纯派生结构** —— 没有任何表外键引用它，
            # `shot.scene_id` 是 `ON DELETE SET NULL`（而且本函数在上面已经先把
            # 该 Session 下所有 Shot 的 scene_id 清空了）。精选、主题、人物
            # 都挂在 Shot 上，不挂 Scene。
            stale = list(existing.values())
            if stale:
                placeholders = ", ".join("?" for _ in stale)
                conn.execute(f"DELETE FROM scene WHERE id IN ({placeholders})", stale)
        return {"created": created, "updated": updated, "cleared": cleared,
                "removed_stale": len(stale)}

    # ------------------------------------------------------------------ 人工调整 Scene
    #
    # 为什么需要这三个方法：Scene 是按"时间断点 + 相似度"自动切的，实拍里必然有
    # 切错的时候（同一场被切成两段、或两场被并成一段）。用户需要能直接改，
    # 而不是只能调阈值然后整批重算。
    #
    # 标记语义：任何被人工改过的 Scene 都会置 `source='user'`；
    # `replace_scenes()` 见到它就**整段跳过**，所以手工调整不会被下一次重算冲掉。
    # 想交还给自动算法：`unpin_scene()`（之后下一次 `scenes rebuild` 就会重算这一段）。

    def _scene_row(self, conn: Any, scene_id: int) -> Any:
        row = conn.execute(
            "SELECT id, session_id, name, captured_from, captured_to, dir_hint, source"
            "  FROM scene WHERE id = ?",
            (int(scene_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"Scene #{scene_id} 不存在")
        return row

    def merge_scenes(self, primary_id: int, secondary_id: int) -> dict[str, Any]:
        """把 `secondary` 合并进 `primary`：搬 Shot、扩时间范围、删掉多余那行。

        只允许**同一 Session（同一时段）内**合并 —— 跨时段合并会让时段目录名与
        Scene 的实际时间范围对不上，而目录名是唯一能独立校验的基准。
        """
        from ..db import utc_now

        if int(primary_id) == int(secondary_id):
            raise ValueError("不能把 Scene 合并到自己")
        stamp = utc_now()
        with self.db.write() as conn:
            primary = self._scene_row(conn, primary_id)
            secondary = self._scene_row(conn, secondary_id)
            if primary["session_id"] != secondary["session_id"]:
                raise ValueError("只能合并**同一时段**内的两个 Scene")

            moved = conn.execute(
                "UPDATE shot SET scene_id = ? WHERE scene_id = ?",
                (int(primary_id), int(secondary_id)),
            ).rowcount or 0

            starts = [v for v in (primary["captured_from"], secondary["captured_from"]) if v]
            ends = [v for v in (primary["captured_to"], secondary["captured_to"]) if v]
            conn.execute(
                "UPDATE scene SET captured_from = ?, captured_to = ?, source = 'user',"
                " group_revision = group_revision + 1, updated_at = ? WHERE id = ?",
                (min(starts) if starts else None, max(ends) if ends else None, stamp,
                 int(primary_id)),
            )
            # shot.scene_id 是 ON DELETE SET NULL，所以搬完再删，不会连带丢 Shot
            conn.execute("DELETE FROM scene WHERE id = ?", (int(secondary_id),))
        return {"scene_id": int(primary_id), "merged_from": int(secondary_id),
                "moved_shots": moved}

    def split_scene(self, scene_id: int, after_shot_id: int, *,
                    name: str | None = None) -> dict[str, Any]:
        """在 `after_shot_id` 之后把 Scene 一分为二（前段保持原 id）。

        Shot 的顺序取 `(captured_at, id)` —— 与界面摆放顺序一致。
        `after_shot_id` 必须是该 Scene 内的一个**活跃** Shot。
        """
        from ..db import utc_now

        stamp = utc_now()
        with self.db.write() as conn:
            scene = self._scene_row(conn, scene_id)
            shots = conn.execute(
                "SELECT id, captured_at FROM shot"
                " WHERE scene_id = ? AND retired_at IS NULL"
                " ORDER BY COALESCE(captured_at, ''), id",
                (int(scene_id),),
            ).fetchall()
            ids = [int(r["id"]) for r in shots]
            if int(after_shot_id) not in ids:
                raise ValueError(f"Shot #{after_shot_id} 不属于 Scene #{scene_id}（或已退役）")
            cut = ids.index(int(after_shot_id))
            tail = ids[cut + 1:]
            if not tail:
                raise ValueError("这是最后一组，后面没有可拆出来的 Shot")

            new_name = name or f"{scene['name']} · 拆出"
            cursor = conn.execute(
                "INSERT INTO scene (session_id, name, captured_from, captured_to, dir_hint,"
                " source, confidence, group_revision, evidence_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'user', NULL, 1, NULL, ?, ?)",
                (int(scene["session_id"]), new_name,
                 shots[cut + 1]["captured_at"], shots[-1]["captured_at"],
                 scene["dir_hint"], stamp, stamp),
            )
            new_id = int(cursor.lastrowid)

            placeholders = ", ".join("?" for _ in tail)
            conn.execute(f"UPDATE shot SET scene_id = ? WHERE id IN ({placeholders})",
                         [new_id, *tail])

            head = ids[:cut + 1]
            head_last = shots[cut]["captured_at"]
            conn.execute(
                "UPDATE scene SET captured_to = COALESCE(?, captured_to), source = 'user',"
                " group_revision = group_revision + 1, updated_at = ? WHERE id = ?",
                (head_last, stamp, int(scene_id)),
            )
            conn.execute(
                "UPDATE scene SET captured_from = COALESCE(?, captured_from) WHERE id = ?",
                (shots[cut + 1]["captured_at"], new_id),
            )
        return {"scene_id": int(scene_id), "new_scene_id": new_id,
                "moved_shots": len(tail), "head_shots": len(head)}

    def unpin_scene(self, scene_id: int) -> dict[str, Any]:
        """解除人工标记（`source` 改回 `auto`），让下次重算可以接管这一段。"""
        from ..db import utc_now

        with self.db.write() as conn:
            self._scene_row(conn, scene_id)
            conn.execute("UPDATE scene SET source = 'auto', updated_at = ? WHERE id = ?",
                         (utc_now(), int(scene_id)))
        return {"scene_id": int(scene_id), "source": "auto"}

    # ------------------------------------------------------------------ 不变式
    def assert_invariants(self) -> dict[str, int]:
        """返回供断言的计数快照（测试在重算前后各取一次比对）。

        ★ 核心不变式：**`selection` 只增不减**。
        """
        return {
            "shots_total": int(self.db.scalar("SELECT COUNT(*) FROM shot", default=0)),
            "shots_active": self.count(),
            "shots_retired": int(
                self.db.scalar("SELECT COUNT(*) FROM shot WHERE retired_at IS NOT NULL", default=0)
            ),
            "scenes": self.scene_count(),
            "selections": int(self.db.scalar("SELECT COUNT(*) FROM selection", default=0)),
            "finals": int(
                self.db.scalar("SELECT COUNT(*) FROM selection WHERE status='FINAL'", default=0)
            ),
            "assets_with_shot": int(
                self.db.scalar("SELECT COUNT(*) FROM asset WHERE shot_id IS NOT NULL", default=0)
            ),
        }

    def live_selection_counts(self) -> dict[str, int]:
        """**用户看得见**的精选计数：只算挂在活跃 Shot 上的。

        为什么这个是必要的补充：`assert_invariants()` 的 `selections` 计的是**表里所有行**，
        包含挂在退役 Shot 上的。而所有会话/场景/统计查询都带 `retired_at IS NULL`，
        所以"行数不变"**并不等于**"用户还看得见"。

        实测：真实库标 60 个 FINAL 后把 `--gap-seconds` 从 3 改成 2，
        有 9 条落到退役 Shot 上 —— `selections` 断言 60 → 60 照常通过，
        而界面上 FINAL 从 60 变成 51。迁移逻辑补上后这里应当与总数一致。
        """
        row = self.db.query_one(
            "SELECT COUNT(*) AS n,"
            "       SUM(CASE WHEN s.status = 'FINAL' THEN 1 ELSE 0 END) AS finals"
            "  FROM selection s JOIN shot sh ON sh.id = s.shot_id"
            " WHERE sh.retired_at IS NULL"
        )
        return {
            "selections": int(row["n"] or 0) if row else 0,
            "finals": int(row["finals"] or 0) if row else 0,
        }


class SessionRepo:
    """`session` 表的读写。

    注意两点表结构事实（决定了本类的形状）：
      * `session` **没有 `library_id`** —— 它是全局的。这与既有建模一致：照片与视频
        放在两个盘上却共享同一套时段名，因为切段用的是两者合并后的时间跨度。
      * 身份是 `(city_hint, period_hint)`，由迁移 005 的部分唯一索引强制。
        因此这里按这个键做 upsert，而不是无脑 INSERT（否则重复导入会堆积同名 Session）。
    """

    #: `session.source` 的 CHECK 约束只允许这两个值（见迁移 001）
    SOURCE_AUTO = "auto"
    SOURCE_USER = "user"

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_many(self, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """按 `(city_hint, period_hint)` upsert，返回 `(新建数, 更新数)`。

        每行至少要有 `name`；建议带 `city_hint` / `period_hint` / `captured_from` /
        `captured_to` / `confidence` / `source`。
        """
        created = updated = 0
        stamp = utc_now()
        with self.db.write() as conn:
            for row in rows:
                city = row.get("city_hint")
                period = row.get("period_hint")
                if not city or not period:
                    raise ValueError("session 的 city_hint 与 period_hint 都不能为空（身份键）")
                existing = conn.execute(
                    "SELECT id, captured_from, captured_to FROM session"
                    " WHERE city_hint = ? AND period_hint = ?",
                    (city, period),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO session (name, captured_from, captured_to, source, confidence,"
                        " city_hint, period_hint, group_revision, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (
                            row.get("name") or f"{city}\\{period}",
                            row.get("captured_from"),
                            row.get("captured_to"),
                            row.get("source") or self.SOURCE_AUTO,
                            row.get("confidence"),
                            city,
                            period,
                            stamp,
                            stamp,
                        ),
                    )
                    created += 1
                    continue
                # 已存在：只在时间范围真的变化时更新（避免每次导入都无谓写库）
                if (
                    str(existing["captured_from"] or "") != str(row.get("captured_from") or "")
                    or str(existing["captured_to"] or "") != str(row.get("captured_to") or "")
                ):
                    # 时间范围变化意味着又并入了新文件 —— 重算代数 +1，便于审计
                    conn.execute(
                        "UPDATE session SET captured_from = ?, captured_to = ?,"
                        " confidence = ?, group_revision = group_revision + 1, updated_at = ?"
                        " WHERE id = ?",
                        (
                            row.get("captured_from"),
                            row.get("captured_to"),
                            row.get("confidence"),
                            stamp,
                            int(existing["id"]),
                        ),
                    )
                    updated += 1
        return created, updated

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM session", default=0))

    def list(self, *, limit: int = 500) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM session ORDER BY captured_from, city_hint, period_hint LIMIT ?",
            (int(limit),),
        )

    def get_by_dir(self, city: str, period: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM session WHERE city_hint = ? AND period_hint = ?", (city, period)
        )


def _to_asset(row: sqlite3.Row) -> AssetRecord:
    keys = row.keys()
    return AssetRecord(
        id=int(row["id"]),
        library_id=int(row["library_id"]),
        relative_path=str(row["relative_path"]),
        file_type=str(row["file_type"]),
        status=str(row["status"]),
        size=int(row["size"]) if row["size"] is not None else None,
        mtime_ns=int(row["mtime_ns"]) if row["mtime_ns"] is not None else None,
        content_hash=row["content_hash"],
        hash_algo=row["hash_algo"],
        shot_id=int(row["shot_id"]) if "shot_id" in keys and row["shot_id"] is not None else None,
        mime=row["mime"] if "mime" in keys else None,
        status_reason=row["status_reason"] if "status_reason" in keys else None,
    )


def asset_absolute_path(root: Path, relative: str) -> Path:
    """由 Library 根 + 相对路径还原绝对路径（相对路径已由 paths 层校验过）。"""
    return Path(root) / Path(relative.replace("\\", "/"))


__all__ = [
    "ASSET_STATUSES",
    "AssetRecord",
    "AssetRepo",
    "CollectionRepo",
    "EmbeddingRepo",
    "MetadataRepo",
    "PreviewRepo",
    "ScanOutcome",
    "SelectionRepo",
    "SemanticRepo",
    "SessionRepo",
    "ShotRepo",
    "asset_absolute_path",
]
