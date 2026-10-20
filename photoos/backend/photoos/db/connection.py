"""SQLite 连接与事务。

设计约束（PHASE1_TECH_PLAN §7）：
  * SQLite 是**单写者**数据库：Job 工作线程的写入必须经过串行化，否则会出现
    `database is locked`。这里提供 `Database.write()` 上下文，由调用方在单写线程里使用。
  * WAL 模式让「读不阻塞写」，因此 UI 浏览与后台索引可以并行。
  * 所有时间戳统一用 `utc_now()`（本地时间 ISO 秒精度，带时区偏移），
    拍摄时间另按 EXIF 语义单独存储，不混用。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import DatabaseError, NotInitializedError

#: 期望的 schema 版本（= migrations 目录中最大编号），由 migrator 校验
SCHEMA_PRAGMA_KEY = "user_version"


def utc_now() -> str:
    """返回带时区偏移的本地 ISO 时间戳（秒精度）。全库统一格式，便于字符串排序。"""
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0).isoformat()


class Database:
    """数据库句柄。持有路径，按需开连接（线程本地）。"""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 15_000, read_only: bool = False) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.read_only = read_only

    # ------------------------------------------------------------------ 连接
    def exists(self) -> bool:
        return self.path.exists()

    def require_exists(self) -> None:
        if not self.exists():
            raise NotInitializedError(
                f"数据库不存在：{self.path}。请先运行 `photoos init` 初始化。",
                detail={"db_path": str(self.path)},
            )

    def connect(self) -> sqlite3.Connection:
        """打开一个**新**连接（调用方负责关闭；建议用 `session()`）。"""
        if not self.read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.read_only:
                uri = f"file:{self.path.as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
            else:
                conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        except sqlite3.Error as exc:  # pragma: no cover - 环境相关
            raise DatabaseError(f"无法打开数据库 {self.path}：{exc}") from exc

        conn.row_factory = sqlite3.Row
        conn.text_factory = str
        self._apply_pragmas(conn)
        return conn

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            cur.execute("PRAGMA temp_store = MEMORY")
            if not self.read_only:
                cur.execute("PRAGMA journal_mode = WAL")
                cur.execute("PRAGMA synchronous = NORMAL")
                cur.execute("PRAGMA wal_autocheckpoint = 1000")
                cur.execute("PRAGMA cache_size = -65536")  # 64 MiB
                cur.execute("PRAGMA mmap_size = 268435456")  # 256 MiB
        finally:
            cur.close()

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """自动提交/回滚的连接上下文。异常时回滚，始终关闭。"""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读语义的连接上下文（不自动提交，仅关闭）。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务。要求调用方已保证串行（单写线程），因此直接用 BEGIN IMMEDIATE。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 便捷查询
    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self.write() as conn:
            cur = conn.executemany(sql, rows)
            return cur.rowcount

    # ------------------------------------------------------------------ 维护
    def integrity_check(self) -> str:
        with self.read() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return row[0] if row else "unknown"

    def foreign_key_check(self) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute("PRAGMA foreign_key_check"))

    def quick_check(self) -> str:
        with self.read() as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return row[0] if row else "unknown"

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute(f"PRAGMA {SCHEMA_PRAGMA_KEY}").fetchone()
            return int(row[0]) if row else 0

    def vacuum_into(self, target: Path) -> None:
        """在线备份：VACUUM INTO 生成一致快照，不阻塞运行中的库（PRD §12）。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        with self.read() as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
