"""废片标记与"统一删除"（移入 Windows 回收站）。

## 边界（比别处更严）

1. **只处理被标记的 asset**；取消标记就是删掉 `asset_reject` 一行。
2. **只移入回收站**（`SHFileOperationW` + `FOF_ALLOWUNDO`），不提供永久删除；
   用户在资源管理器里"还原"就能拿回来。
3. **命中精选或主题的 Shot 一律跳过**：废片清理不该吃掉用户的入选/主题。
4. **只动库根之内的普通文件**：解析出的绝对路径必须仍在 `library.root_path_hint` 里；
   不存在/是目录/越界都记一条错误，不调用回收站。
5. 每次清理都要 `confirmed=true`；清理是后台任务，可取消（已处理的不可回滚，
   但都在回收站里）。

## 为什么删除后连 asset 行一起删

原片已经不在库里了，保留一行 `asset` 会让浏览/统计/精选继续显示一张打不开的照片。
删除前已用 `shot_id` 检查过精选与主题；删除后由任务末尾的派生分组重算 Shot/Scene。
缩略图与预览是派生文件，留在数据根里（不占原片盘，后续维护可清）。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..db import Database, utc_now
from ..errors import PhotoOSError
from . import paths

ProgressFn = Callable[[int, int, str], None]


def _noop(done: int, total: int, note: str) -> None:  # pragma: no cover
    return None


@dataclass(frozen=True)
class RejectItem:
    asset_id: int
    library_id: int
    relative_path: str
    size: int | None
    shot_id: int | None
    marked_at: str
    selected: bool = False      # 该 Shot 在精选里
    collected: bool = False     # 该 Shot 在某个主题里

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "library_id": self.library_id,
            "relative_path": self.relative_path,
            "size": self.size,
            "marked_at": self.marked_at,
            "selected": self.selected,
            "collected": self.collected,
            "protected": self.selected or self.collected,
        }


@dataclass
class CleanupReport:
    recycled: int = 0
    bytes: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    skipped_items: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"移入回收站 {self.recycled} 个 / {self.bytes / 1e6:.1f} MB；"
            f"跳过（精选/主题）{self.skipped}；失败 {self.failed}"
        )


# --------------------------------------------------------------------------- 标记
def mark(db: Database, asset_id: int, *, marked: bool = True, note: str | None = None) -> bool:
    """标记/取消标记。返回当前是否已标记。"""
    if marked:
        now = utc_now()
        with db.write() as conn:
            exists = conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if exists is None:
                raise PhotoOSError(f"照片不存在：#{asset_id}")
            conn.execute(
                "INSERT INTO asset_reject (asset_id, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(asset_id) DO UPDATE SET note = excluded.note,"
                " updated_at = excluded.updated_at",
                (asset_id, note, now, now),
            )
        return True
    with db.write() as conn:
        conn.execute("DELETE FROM asset_reject WHERE asset_id = ?", (asset_id,))
    return False


def is_marked(db: Database, asset_id: int) -> bool:
    return db.query_one("SELECT 1 FROM asset_reject WHERE asset_id = ?", (asset_id,)) is not None


def items(db: Database) -> list[RejectItem]:
    rows = db.query(
        "SELECT r.asset_id, r.created_at, a.library_id, a.relative_path, a.size, a.shot_id"
        " FROM asset_reject r JOIN asset a ON a.id = r.asset_id"
        " ORDER BY r.created_at DESC, r.asset_id DESC"
    )
    selected_shots = {
        int(row["shot_id"]) for row in db.query("SELECT shot_id FROM selection")
    }
    collected_shots = {
        int(row["shot_id"]) for row in db.query("SELECT DISTINCT shot_id FROM collection_item")
    }
    out: list[RejectItem] = []
    for row in rows:
        shot_id = row["shot_id"]
        out.append(
            RejectItem(
                asset_id=int(row["asset_id"]),
                library_id=int(row["library_id"]),
                relative_path=str(row["relative_path"]),
                size=int(row["size"]) if row["size"] is not None else None,
                shot_id=int(shot_id) if shot_id is not None else None,
                marked_at=str(row["created_at"]),
                selected=shot_id is not None and int(shot_id) in selected_shots,
                collected=shot_id is not None and int(shot_id) in collected_shots,
            )
        )
    return out


def summary(db: Database) -> dict[str, object]:
    rows = items(db)
    total_bytes = sum(int(item.size or 0) for item in rows)
    protected = [item for item in rows if item.selected or item.collected]
    return {
        "count": len(rows),
        "bytes": total_bytes,
        "protected": len(protected),
        "items": [item.to_dict() for item in rows[:50]],
        "truncated": max(0, len(rows) - 50),
    }


# --------------------------------------------------------------------------- 回收站
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(path: Path) -> None:
    """把一个文件移入 Windows 回收站（可还原）。失败就抛 OSError，绝不退化成永久删除。"""
    target = Path(path)
    if not target.is_file():
        raise OSError(f"不是普通文件：{target}")
    if os.name != "nt":  # pragma: no cover - 本项目只支持 Windows
        raise OSError("当前系统不支持回收站（仅 Windows）")
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4
    FOF_NOERRORUI = 0x400
    source = str(target) + "\0\0"
    operation = _SHFILEOPSTRUCTW(
        None,
        FO_DELETE,
        source,
        None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
        False,
        None,
        None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))  # type: ignore[attr-defined]
    if result != 0:
        raise OSError(f"移入回收站失败（code {result}）")
    if operation.fAnyOperationsAborted:
        raise OSError("移入回收站被中止")


# --------------------------------------------------------------------------- 清理
def _library_roots(db: Database) -> dict[int, Path]:
    from .store import LibraryStore

    roots: dict[int, Path] = {}
    for library in LibraryStore(db).list_all():
        if library.root_path_hint:
            roots[int(library.id)] = Path(library.root_path_hint)
    return roots


def cleanup(
    db: Database,
    *,
    on_progress: ProgressFn = _noop,
    stop: Callable[[], bool] | None = None,
    recycle: Callable[[Path], None] | None = None,
) -> CleanupReport:
    """把已标记的废片移入回收站并删除对应 asset 行。**不做永久删除。**"""
    recycler = recycle or move_to_recycle_bin
    report = CleanupReport()
    rows = items(db)
    roots = _library_roots(db)
    total = len(rows)
    for index, item in enumerate(rows, start=1):
        if stop is not None and stop():
            report.cancelled = True
            break
        if item.selected or item.collected:
            report.skipped += 1
            report.skipped_items.append(f"{item.relative_path}（已入选/在主题里）")
            on_progress(index, total, f"跳过 {item.relative_path}")
            continue
        root = roots.get(item.library_id)
        if root is None:
            report.failed += 1
            report.errors.append(f"{item.relative_path}: 库没有路径提示")
            continue
        target = root / Path(item.relative_path.replace("\\", "/"))
        if not paths.is_same_root(root, target):
            report.failed += 1
            report.errors.append(f"{item.relative_path}: 解析出的路径越出库根，已拒绝")
            continue
        if not target.is_file():
            report.failed += 1
            report.errors.append(f"{item.relative_path}: 文件不在盘上")
            continue
        try:
            recycler(target)
        except OSError as exc:
            report.failed += 1
            report.errors.append(f"{item.relative_path}: {exc}")
            continue
        with db.write() as conn:
            conn.execute("DELETE FROM asset WHERE id = ?", (item.asset_id,))
        report.recycled += 1
        report.bytes += int(item.size or 0)
        on_progress(index, total, f"已移入回收站 {index}/{total}：{item.relative_path}")
    return report


__all__ = [
    "CleanupReport",
    "RejectItem",
    "cleanup",
    "is_marked",
    "items",
    "mark",
    "move_to_recycle_bin",
    "summary",
]
