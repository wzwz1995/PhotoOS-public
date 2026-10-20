"""本地异步任务系统（PRD §7 / §11.1 / §11.2 / §11.3）。

队列存在 SQLite 里，不引入 Redis/Celery；支持暂停、恢复、取消、失败重试、断点续跑与孤儿回收。
"""

from __future__ import annotations

from .pipelines import (
    HASH_ALGO,
    DuplicateReport,
    StageResult,
    build_duplicate_report,
    hash_file,
    library_stats,
    run_discovery,
    run_fused_hash_previews,
    run_hash,
    run_metadata,
    run_previews,
)
from .queue import JOB_TYPES, JobQueue, JobRecord, executor_id
from .runner import BackgroundRunner, JobReport, JobRunner

__all__ = [
    "HASH_ALGO",
    "JOB_TYPES",
    "BackgroundRunner",
    "DuplicateReport",
    "JobQueue",
    "JobRecord",
    "JobReport",
    "JobRunner",
    "StageResult",
    "build_duplicate_report",
    "executor_id",
    "hash_file",
    "library_stats",
    "run_discovery",
    "run_fused_hash_previews",
    "run_hash",
    "run_metadata",
    "run_previews",
]
