"""本地异步任务队列（PRD §7 / §9.1 / §11.1 / §11.2）。

设计要求（逐条对应 PRD）：
  * 状态必须可见：PENDING / PROCESSING / COMPLETED / FAILED / RETRY / CANCELLED / PAUSED，
    且带进度、错误、重试次数（§9.1）。
  * 必须支持暂停/恢复、失败重试、中断后继续（§11.2 导入指标）。
  * 单条失败不阻塞整批（§11.1）。
  * **不引入 Redis / Celery**（§11.3 LOCKED）：队列就存在 SQLite 里，进程重启后依然可续跑。
  * 心跳 + 孤儿回收：进程被杀（断电、任务管理器结束）后，停留在 PROCESSING 的任务
    在下次启动时会被识别为孤儿并回退为 PENDING（attempts+1），不会永远卡住。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import Database, utc_now
from ..errors import JobError, JobNotCancellable, JobNotFound

JOB_TYPES = (
    # ⚠️ 这张元组是 `job.type` 的**唯一白名单**：入队时强校验（见 `create()`）。
    # 数据库层**没有** CHECK 约束 —— `001_core.sql:213` 的列注释只列了 7 种、
    # 早就过期了（实际 14 种），而那张表是已发布的迁移、不能改。
    # 所以：新增任务类型必须改这里 + `jobs/runner.py` 的分发 dict 两处，
    # `tests/unit/test_jobs.py` 有一条**双向对账**测试会钉住两者一致。
    "scan", "hash", "metadata", "preview", "fused", "rebind", "verify", "migrate", "backup",
    "full_index",
    #: 基础物理分类导入：计划 → 复制 → 落库 → 增量索引（见 library/classify_import.py）。
    #: 做成任务而不是同步接口调用，是为了直接获得暂停/续跑/进度与孤儿回收。
    "classify_import",
    #: 从手机通过 ADB 拉到收件箱（见 library/adb.py）。
    #: 只把手机文件复制到本地暂存区、再整体发布到收件箱；**不直接入库** ——
    #: 入库仍然只有收件箱那一条路径，取消/失败都不会在库里留下半成品。
    "adb_pull",
    #: 废片清理：把已标记的照片**移入 Windows 回收站**（可还原），并删除 asset 行。
    #: 铁律修订：原片只允许经这一条路径、且用户显式确认后离开库；不提供永久删除。
    "reject_cleanup",
    #: 一键打包：把一批原片写成一个 zip（见 library/export.py）。
    #: 几百张 RAW 要几分钟 —— 同步请求会超时，而任务天然有进度、暂停与取消。
    #: `scope` = `{asset_ids, label, kind}`，业务判断在**入队之前**就做完了。
    "export",
    #: 界面直跑一条白名单命令（见 actions/catalog.py）。
    #: `scope` = `{action_id, query?, argv, python}` —— argv 由服务端从模板展开，
    #: 浏览器只能给 action_id。做成任务而不是同步请求，是因为下载 1.4 GB 模型
    #: 或给 2 万张照片算向量要几十分钟到几小时。
    "cli_action",
    #: 派生分组：`sessions backfill` → `shots regroup` → `scenes rebuild`。
    #:
    #: ⚠️ 这三步是**精选页与「按时间/地点」找照片能用的前提** —— 界面以
    #: Shot / Scene 为单位，而导入只建到 `session` 一层。在没有这个任务之前，
    #: 新库导入完点进精选只会看到"0 Shot / 0 Scene"，而且只能去命令行手敲
    #: （`plan_regroup` 在整个包里**没有一个调用者**）。
    #:
    #: 幂等：重复跑不会重复建行、也不会覆盖用户手工调整过的 Shot（决策 #10）。
    #: 因此 `full_index` 与 `classify_import` 末尾都可以安全地带上它。
    "regroup",
)

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
ACTIVE_STATUSES = frozenset({"PENDING", "PROCESSING", "RETRY", "PAUSED"})


def executor_id() -> str:
    """本进程的执行者身份：`<主机名>:<pid>`。

    用途：任务认领（`claim_next(owner=...)`）与孤儿回收。
    进程崩溃后 pid 会变，遗留归属靠心跳过期释放，因此不需要持久化注册表。
    """
    import os
    import socket

    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover - 极端环境
        host = "unknown"
    return f"{host}:{os.getpid()}"


@dataclass(frozen=True)
class JobRecord:
    id: int
    type: str
    status: str
    scope: dict[str, Any]
    progress_done: int
    progress_total: int
    progress_note: str | None
    error: str | None
    retry_count: int
    max_retries: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    #: 当前认领该任务的执行者（`<主机名>:<pid>`）；None 表示无归属
    claimed_by: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def percent(self) -> float:
        if self.progress_total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.progress_done / self.progress_total)

    def describe(self) -> str:
        base = f"#{self.id} {self.type} {self.status}"
        if self.progress_total:
            base += f" {self.progress_done}/{self.progress_total} ({self.percent:.1f}%)"
        if self.retry_count:
            base += f" 重试 {self.retry_count}"
        if self.error:
            base += f" 错误：{self.error[:120]}"
        return base


class JobQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        job_type: str,
        *,
        library_id: int | None = None,
        scope: dict[str, Any] | None = None,
        progress_total: int = 0,
        max_retries: int = 3,
        priority: int = 100,
    ) -> JobRecord:
        if job_type not in JOB_TYPES:
            raise JobError(f"未知任务类型：{job_type}（可用：{', '.join(JOB_TYPES)}）")
        # 界面直跑的命令**不自动重试**（实测踩过：`ai doctor` 判定环境不满足而退出 1，
        # 队列按默认策略又闷头重跑 3 次，用户在任务表里看到同一条命令失败 4 遍，
        # 每次都完整再跑一次环境探测 —— 既慢又让人以为点错了什么）。
        # 这类命令都是"人工触发 + 幂等 + 报错信息已经很清楚"，重跑没有收益，
        # 用户看完输出自己决定要不要再点一次更合理。
        if job_type == "cli_action":
            max_retries = 0
        payload = dict(scope or {})
        if library_id is not None:
            payload["library_id"] = library_id
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO job (type, status, scope_json, progress_total, max_retries, priority,"
                " created_at) VALUES (?, 'PENDING', ?, ?, ?, ?, ?)",
                (
                    job_type,
                    json.dumps(payload, ensure_ascii=False),
                    int(progress_total),
                    int(max_retries),
                    int(priority),
                    utc_now(),
                ),
            )
            job_id = int(cur.lastrowid or 0)
        return self.get(job_id)

    def find_active(self, job_type: str, library_id: int | None = None) -> JobRecord | None:
        rows = self.db.query(
            "SELECT * FROM job WHERE type = ? AND status IN ('PENDING','PROCESSING','RETRY','PAUSED')"
            " ORDER BY id DESC",
            (job_type,),
        )
        for row in rows:
            record = _to_job(row)
            if library_id is None or record.scope.get("library_id") == library_id:
                return record
        return None

    # ------------------------------------------------------------------ 读
    def get(self, job_id: int) -> JobRecord:
        row = self.db.query_one("SELECT * FROM job WHERE id = ?", (job_id,))
        if row is None:
            raise JobNotFound(f"任务不存在：#{job_id}", detail={"job_id": job_id})
        return _to_job(row)

    def list_recent(self, *, limit: int = 20, include_finished: bool = True) -> list[JobRecord]:
        sql = "SELECT * FROM job"
        if not include_finished:
            sql += " WHERE status IN ('PENDING','PROCESSING','RETRY','PAUSED')"
        sql += " ORDER BY id DESC LIMIT ?"
        return [_to_job(r) for r in self.db.query(sql, (limit,))]

    def list_active(self) -> list[JobRecord]:
        return [
            _to_job(r)
            for r in self.db.query(
                "SELECT * FROM job WHERE status IN ('PENDING','PROCESSING','RETRY','PAUSED')"
                " ORDER BY priority, id"
            )
        ]

    # ------------------------------------------------------------------ 状态迁移
    def claim_next(self, *, owner: str | None = None) -> JobRecord | None:
        """取出下一个可执行任务并置为 PROCESSING（原子操作）。

        `owner` 是**执行者身份**（见 `executor_id()`）。只有"无归属"或"归属自己"的
        任务才会被认领 —— 这防止多个进程（例如 `photoos serve` 的后台执行器与命令行的
        `photoos index`）同时执行同一个任务、重复读取同一批文件。
        PAUSED 的任务不会被领取。

        其他进程遗留的归属由 `recover_orphans()` 在心跳过期后释放，因此崩溃不会永久占坑。
        """
        with self.db.write() as conn:
            if owner:
                row = conn.execute(
                    "SELECT * FROM job WHERE status IN ('PENDING','RETRY')"
                    " AND (claimed_by IS NULL OR claimed_by = ?)"
                    " ORDER BY priority, id LIMIT 1",
                    (owner,),
                ).fetchone()
            else:
                # 未提供身份时保持旧行为（仅用于不涉及并发的场景，例如单元测试）
                row = conn.execute(
                    "SELECT * FROM job WHERE status IN ('PENDING','RETRY') ORDER BY priority, id LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            now = utc_now()
            conn.execute(
                "UPDATE job SET status = 'PROCESSING', claimed_by = COALESCE(?, claimed_by),"
                " started_at = COALESCE(started_at, ?), heartbeat_at = ? WHERE id = ?",
                (owner, now, now, row["id"]),
            )
            updated = conn.execute("SELECT * FROM job WHERE id = ?", (row["id"],)).fetchone()
        return _to_job(updated)

    def release_claim(self, job_id: int) -> None:
        """释放归属（任务执行完毕或失败后调用），让其他执行者可以接手。"""
        self.db.execute("UPDATE job SET claimed_by = NULL WHERE id = ?", (job_id,))

    def claimed_by(self, job_id: int) -> str | None:
        value = self.db.scalar("SELECT claimed_by FROM job WHERE id = ?", (job_id,))
        return str(value) if value else None

    def update_progress(
        self,
        job_id: int,
        *,
        done: int | None = None,
        total: int | None = None,
        note: str | None = None,
        increment: int = 0,
    ) -> None:
        sets: list[str] = ["heartbeat_at = ?"]
        params: list[Any] = [utc_now()]
        if done is not None:
            sets.append("progress_done = ?")
            params.append(int(done))
        if increment:
            sets.append("progress_done = progress_done + ?")
            params.append(int(increment))
        if total is not None:
            sets.append("progress_total = ?")
            params.append(int(total))
        if note is not None:
            sets.append("progress_note = ?")
            params.append(note[:500])
        params.append(job_id)
        self.db.execute(f"UPDATE job SET {', '.join(sets)} WHERE id = ?", params)

    def heartbeat(self, job_id: int) -> None:
        self.db.execute("UPDATE job SET heartbeat_at = ? WHERE id = ?", (utc_now(), job_id))

    def complete(self, job_id: int, *, note: str | None = None) -> bool:
        """标记完成，返回**是否真的写进去了**。

        `status NOT IN ('CANCELLED','PAUSED')` 这个守卫防的是一处窄竞态：
        `runner` 在 `runner.py:179` 读一次"是否被取消"；取消请求若恰好落在那之后、
        本方法之前，无守卫的 UPDATE 会把刚写入的 `CANCELLED` **覆盖成 `COMPLETED`**
        —— 用户点了取消，界面却显示"已完成"（见 20-reference Q-44）。
        """
        changed = self.db.execute(
            "UPDATE job SET status = 'COMPLETED', finished_at = ?, heartbeat_at = ?,"
            " progress_note = COALESCE(?, progress_note), error = NULL"
            " WHERE id = ? AND status NOT IN ('CANCELLED', 'PAUSED')",
            (utc_now(), utc_now(), note, job_id),
        )
        return bool(changed)

    def fail(self, job_id: int, error: str, *, retry: bool = True) -> JobRecord:
        """记录失败：未超重试上限则转 RETRY，否则 FAILED。"""
        record = self.get(job_id)
        attempts = record.retry_count + 1
        can_retry = retry and attempts <= record.max_retries
        status = "RETRY" if can_retry else "FAILED"
        self.db.execute(
            "UPDATE job SET status = ?, retry_count = ?, error = ?, heartbeat_at = ?,"
            " finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE finished_at END WHERE id = ?",
            (status, attempts, error[:2000], utc_now(), status, utc_now(), job_id),
        )
        return self.get(job_id)

    def cancel(self, job_id: int) -> JobRecord:
        record = self.get(job_id)
        if record.is_terminal:
            raise JobNotCancellable(
                f"任务 #{job_id} 已处于终态 {record.status}，无法取消",
                detail={"job_id": job_id, "status": record.status},
            )
        self.db.execute(
            "UPDATE job SET status = 'CANCELLED', finished_at = ?, heartbeat_at = ? WHERE id = ?",
            (utc_now(), utc_now(), job_id),
        )
        return self.get(job_id)

    def pause(self, job_id: int) -> JobRecord:
        record = self.get(job_id)
        if record.is_terminal:
            raise JobNotCancellable(f"任务 #{job_id} 已结束，无法暂停", detail={"job_id": job_id})
        self.db.execute(
            "UPDATE job SET status = 'PAUSED', heartbeat_at = ? WHERE id = ?", (utc_now(), job_id)
        )
        return self.get(job_id)

    def resume(self, job_id: int) -> JobRecord:
        """把暂停的任务放回队列。已完成/已取消的任务不会被复活。"""
        record = self.get(job_id)
        if record.status not in {"PAUSED", "FAILED"}:
            raise JobError(
                f"任务 #{job_id} 当前状态为 {record.status}，无需恢复",
                detail={"job_id": job_id, "status": record.status},
            )
        self.db.execute(
            "UPDATE job SET status = 'PENDING', error = NULL, heartbeat_at = ?,"
            # ⚠️ 必须清掉归属：`recover_orphans()` 只扫 `PROCESSING`，
            # 而 `claim_next()` 只领"无归属或归属自己"的行 —— 一条 `PENDING`
            # 却还留着旧 `claimed_by` 的任务**没有任何执行者会认领**，
            # 于是永久卡住（见 20-reference Q-32）。
            " claimed_by = NULL WHERE id = ?",
            (utc_now(), job_id),
        )
        return self.get(job_id)

    def is_cancelled(self, job_id: int) -> bool:
        status = self.db.scalar("SELECT status FROM job WHERE id = ?", (job_id,))
        return status == "CANCELLED"

    def is_paused(self, job_id: int) -> bool:
        status = self.db.scalar("SELECT status FROM job WHERE id = ?", (job_id,))
        return status == "PAUSED"

    def should_stop(self, job_id: int) -> bool:
        """worker 的协作式停止检查点：命中 CANCELLED 或 PAUSED 即应尽快收工。"""
        return self.is_cancelled(job_id) or self.is_paused(job_id)

    # ------------------------------------------------------------------ 断点续跑
    def upsert_item(
        self,
        job_id: int,
        *,
        ref_type: str,
        ref_key: str,
        ref_id: int | None = None,
        state: str = "PENDING",
        error: str | None = None,
    ) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO job_item (job_id, ref_type, ref_id, ref_key, state, attempts,"
                " last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_id, ref_type, ref_key) DO UPDATE SET"
                " state = excluded.state,"
                " attempts = job_item.attempts + CASE WHEN excluded.state = 'FAILED' THEN 1 ELSE 0 END,"
                " last_error = excluded.last_error, updated_at = excluded.updated_at",
                (job_id, ref_type, ref_id, ref_key, state, 0, error, utc_now()),
            )

    def item_states(self, job_id: int) -> dict[str, int]:
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM job_item WHERE job_id = ? GROUP BY state", (job_id,)
        )
        return {str(r["state"]): int(r["n"]) for r in rows}

    def failed_items(self, job_id: int, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM job_item WHERE job_id = ? AND state = 'FAILED' ORDER BY updated_at DESC LIMIT ?",
            (job_id, limit),
        )

    # ------------------------------------------------------------------ 孤儿回收
    def recover_orphans(self, *, stale_after_seconds: int = 300, owner: str | None = None) -> int:
        """把心跳过期的 PROCESSING 任务回退为 PENDING，并释放其归属。

        只回收**其他执行者**遗留的任务（`owner` 自己正在跑的不动）。
        进程被杀（断电、任务管理器结束）后，任务不会永远卡在 PROCESSING。
        """
        stale = self.db.query(
            "SELECT id, heartbeat_at, claimed_by FROM job WHERE status = 'PROCESSING'"
        )
        recovered = 0
        from datetime import datetime, timedelta

        threshold = datetime.now().astimezone() - timedelta(seconds=stale_after_seconds)
        for row in stale:
            if owner and row["claimed_by"] == owner:
                continue  # 自己正在执行的，跳过
            heartbeat = row["heartbeat_at"]
            expired = True
            if heartbeat:
                try:
                    expired = datetime.fromisoformat(str(heartbeat)) < threshold
                except ValueError:
                    expired = True
            if expired:
                self.db.execute(
                    "UPDATE job SET status = 'PENDING', claimed_by = NULL, heartbeat_at = ?,"
                    " error = '进程中断，已自动恢复为待执行' WHERE id = ?",
                    (utc_now(), row["id"]),
                )
                recovered += 1
        return recovered


def _to_job(row: sqlite3.Row) -> JobRecord:
    try:
        scope = json.loads(row["scope_json"]) if row["scope_json"] else {}
    except json.JSONDecodeError:
        scope = {}
    keys = set(row.keys())
    return JobRecord(
        id=int(row["id"]),
        type=str(row["type"]),
        status=str(row["status"]),
        scope=scope if isinstance(scope, dict) else {},
        progress_done=int(row["progress_done"] or 0),
        progress_total=int(row["progress_total"] or 0),
        progress_note=row["progress_note"],
        error=row["error"],
        retry_count=int(row["retry_count"] or 0),
        # ⚠️ 这里**不能**写 `int(row["max_retries"] or 3)` —— `0 or 3` 求值为 3，
        # 于是库里存的"不重试"（max_retries=0）读回来变成了默认的 3 次，
        # 失败的任务会闷头重跑三遍。必须判 `None` 而不是判假值：
        # 0 是一个**有意义的取值**，不是"没设置"。
        max_retries=int(row["max_retries"]) if row["max_retries"] is not None else 3,
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        heartbeat_at=row["heartbeat_at"],
        claimed_by=row["claimed_by"] if "claimed_by" in keys else None,
    )


__all__ = [
    "ACTIVE_STATUSES",
    "JOB_TYPES",
    "TERMINAL_STATUSES",
    "JobQueue",
    "JobRecord",
]
