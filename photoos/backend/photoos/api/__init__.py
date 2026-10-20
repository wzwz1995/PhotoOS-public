"""HTTP API 层（FastAPI）。

`create_app()` 是唯一入口；CLI 的 `photoos serve` 与测试都通过它装配应用。
"""

from __future__ import annotations

from .app import create_app, get_db
from .queries import (
    asset_detail,
    asset_page,
    camera_histogram,
    capture_year_histogram,
    integrity_summary,
    library_overview,
    list_jobs,
)

__all__ = [
    "asset_detail",
    "asset_page",
    "camera_histogram",
    "capture_year_histogram",
    "create_app",
    "get_db",
    "integrity_summary",
    "library_overview",
    "list_jobs",
]
