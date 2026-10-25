"""手机收件箱：等文件复制稳定后，再走既有的分类导入。

## 为什么是"收件箱"而不是直读手机

MTP 全量逐对象读取在这台机器上会把设备栈读挂：`DCIM\\Camera` 有 6,652 个对象，
单会话取属性到约 1,200 个就返回 `E_WPD_DEVICE_IS_HUNG`，之后通常要拔插手机才恢复。
所以第一版的做法是**让用户在资源管理器里把照片复制进一个普通文件夹**：

  * 完全不用碰 MTP，任何手机/相机都能用；
  * 6,652 个文件也不会把 PhotoOS 自己的进程读挂（只有本地磁盘 IO）；
  * 导入链路直接复用 `classify_import`（只读源、内容哈希去重、`__N` 绝不覆盖）。

## 三条纪律

1. **收件箱默认只读**：导入是复制；不移动、不改名、不删除。
   唯一例外是用户在收件箱卡片里显式勾选「导入后清理已入库文件」——只删
   台账可核对、目标仍在库里的副本；失败/目标缺失/文件已变化的都保留。
2. **复制中的文件不导入**：同一个 `(大小, mtime)` 必须**持续** `inbox_settle_seconds`
   秒（从第一次观察到它开始计时）。反复扫描**不会**重置计时 —— 否则用户
   "扫描 → 看到可导入 → 立刻点导入"时，导入内部那一扫会把时钟归零，又变回复制中。
3. **导入过的文件不再重复导入**：台账（`data/inbox/ledger.json`）按
   `(相对路径, 大小, mtime)` 记住；文件被替换（大小/mtime 变了）才会重新排队。

台账与扫描快照都写在**数据根**里（`data/inbox/`），不写进收件箱 ——
收件箱是用户的地盘，我们只读它。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import utc_now
from . import classify_import, walker

#: 台账最多保留多少条。收件箱是"中转站"，用户会长期往里放东西；
#: 台账只用于去重，超出上限时按导入时间淘汰最旧的即可。
LEDGER_LIMIT = 20_000

STATE_READY = "ready"
STATE_COPYING = "copying"
STATE_IMPORTED = "imported"
STATE_EMPTY = "empty"

_STATE_LABELS = {
    STATE_READY: "可导入",
    STATE_COPYING: "复制中",
    STATE_IMPORTED: "已导入",
    STATE_EMPTY: "空文件",
}


def _scan_path(settings: Settings) -> Path:
    return settings.inbox_state_dir / "scan.json"


def _ledger_path(settings: Settings) -> Path:
    return settings.inbox_state_dir / "ledger.json"


def _read_json(path: Path) -> dict[str, Any]:
    """读 JSON；不存在或损坏都返回空字典（**不能因为读不了就拦住导入**）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写：临时文件 + `os.replace`，半截文件永远不会成为下次扫描的输入。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class InboxFile:
    """收件箱里的一个媒体文件及其当前状态。"""

    relative: str
    name: str
    size: int
    mtime_ns: int
    state: str
    note: str = ""

    @property
    def label(self) -> str:
        return _STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative": self.relative,
            "name": self.name,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "state": self.state,
            "label": self.label,
            "note": self.note,
        }


@dataclass(frozen=True)
class InboxSnapshot:
    """一次扫描的结果。`record=True` 时同时把快照写进数据根，供下次比较。"""

    path: Path
    exists: bool
    scanned_at: str
    settle_seconds: float
    files: list[InboxFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ledger_entries: int = 0

    def _count(self, state: str) -> int:
        return sum(1 for item in self.files if item.state == state)

    @property
    def ready_bytes(self) -> int:
        return sum(item.size for item in self.files if item.state == STATE_READY)

    def ready_map(self) -> dict[str, tuple[int, int]]:
        """`relative -> (size, mtime_ns)`，只含"可以导入"的文件。"""
        return {
            item.relative: (item.size, item.mtime_ns)
            for item in self.files
            if item.state == STATE_READY
        }

    def to_dict(self, *, limit: int = 500) -> dict[str, Any]:
        counts = {
            STATE_READY: self._count(STATE_READY),
            STATE_COPYING: self._count(STATE_COPYING),
            STATE_IMPORTED: self._count(STATE_IMPORTED),
            STATE_EMPTY: self._count(STATE_EMPTY),
        }
        # 让"可导入/复制中"排在前面，用户点开就看到该处理的东西
        order = {STATE_READY: 0, STATE_COPYING: 1, STATE_EMPTY: 2, STATE_IMPORTED: 3}
        ordered = sorted(self.files, key=lambda item: (order.get(item.state, 9), item.relative))
        return {
            "path": str(self.path),
            "exists": self.exists,
            "scanned_at": self.scanned_at,
            "settle_seconds": self.settle_seconds,
            "counts": counts,
            "total": len(self.files),
            "ready": counts[STATE_READY],
            "copying": counts[STATE_COPYING],
            "imported": counts[STATE_IMPORTED],
            "empty": counts[STATE_EMPTY],
            "ready_bytes": self.ready_bytes,
            "ledger_entries": self.ledger_entries,
            "errors": self.errors[:20],
            "items": [item.to_dict() for item in ordered[:limit]],
            "truncated": max(0, len(self.files) - limit),
        }


def _judge(
    walked: walker.FileEntry,
    *,
    ledger: Mapping[str, Any],
    now: datetime,
    settle_seconds: float,
    first_seen: datetime | None,
) -> InboxFile:
    """判断一个文件此刻处于哪个状态。

    `first_seen` 是**当前这个 (大小, mtime) 第一次被观察到的时刻**；只有它持续
    `settle_seconds` 秒没变，文件才算复制完成。顺序：空文件 → 台账命中 → 稳定判定。
    台账先于"稳定"判断，否则已导入的文件会被反复算成"可导入"。
    """
    size = int(walked.size)
    mtime_ns = int(walked.mtime_ns)
    base = dict(
        relative=walked.relative,
        name=walked.path.name,
        size=size,
        mtime_ns=mtime_ns,
    )

    if size <= 0:
        return InboxFile(state=STATE_EMPTY, note="文件还是空的（可能刚创建）", **base)

    remembered = ledger.get(walked.relative)
    if (
        isinstance(remembered, dict)
        and int(remembered.get("size") or -1) == size
        and int(remembered.get("mtime_ns") or -1) == mtime_ns
    ):
        return InboxFile(
            state=STATE_IMPORTED,
            note=f"已于 {remembered.get('imported_at') or '之前'} 导入",
            **base,
        )

    if first_seen is None:
        return InboxFile(state=STATE_COPYING, note="新出现的文件，继续观察", **base)
    elapsed = (now - first_seen).total_seconds()
    if elapsed >= settle_seconds:
        return InboxFile(state=STATE_READY, note=f"大小与修改时间已稳定 {elapsed:.0f} 秒", **base)
    return InboxFile(
        state=STATE_COPYING,
        note=f"还需约 {max(1, settle_seconds - elapsed):.0f} 秒确认复制完成",
        **base,
    )


def scan_inbox(
    settings: Settings,
    *,
    now: datetime | None = None,
    record: bool = True,
) -> InboxSnapshot:
    """扫描收件箱并判定每个文件是否已经复制稳定。

    只读收件箱；`record=True` 时把本次的 `(相对路径, 大小, mtime)` 写进数据根，
    下一次扫描据此判断"有没有变"。**第一次扫描永远不会把文件判成可导入** ——
    稳定性需要两个时间点的证据。
    """
    root = Path(settings.effective_inbox_path)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    settle = float(settings.inbox_settle_seconds)
    stamp = moment.isoformat(timespec="seconds")

    previous_raw = _read_json(_scan_path(settings))
    ledger_raw = _read_json(_ledger_path(settings))
    previous = previous_raw.get("files")
    previous = previous if isinstance(previous, dict) else {}
    ledger = ledger_raw.get("entries")
    ledger = ledger if isinstance(ledger, dict) else {}
    previous_at = _parse_time(previous_raw.get("scanned_at"))

    files: list[InboxFile] = []
    current: dict[str, list[Any]] = {}
    errors: list[str] = []
    exists = root.is_dir()
    if exists:
        for walked in walker.walk_media(root, include_videos=True):
            if isinstance(walked, walker.WalkError):
                errors.append(f"{walked.relative or walked.path}: {walked.message}")
                continue
            size, mtime_ns = int(walked.size), int(walked.mtime_ns)
            seen = previous.get(walked.relative)
            unchanged = (
                isinstance(seen, (list, tuple))
                and len(seen) >= 2
                and int(seen[0]) == size
                and int(seen[1]) == mtime_ns
            )
            if unchanged:
                # 这个 (大小, mtime) 上次就见过：沿用它的"首次观察时间"。
                # 兼容旧格式（只有两项）：退回上次扫描时间，绝不把计时重置成 now。
                stored = _parse_time(seen[2]) if len(seen) >= 3 else None
                first_seen = stored or previous_at or moment
            else:
                # 新文件，或内容又变了：计时从现在开始
                first_seen = moment
            files.append(
                _judge(
                    walked,
                    ledger=ledger,
                    now=moment,
                    settle_seconds=settle,
                    first_seen=first_seen,
                )
            )
            if size > 0:
                current[walked.relative] = [
                    size,
                    mtime_ns,
                    first_seen.isoformat(timespec="seconds"),
                ]

    if record:
        _write_json(
            _scan_path(settings),
            {
                "version": 2,
                "scanned_at": stamp,
                "settle_seconds": settle,
                "files": current,
            },
        )

    return InboxSnapshot(
        path=root,
        exists=exists,
        scanned_at=stamp,
        settle_seconds=settle,
        files=files,
        errors=errors,
        ledger_entries=len(ledger),
    )


def ready_map(snapshot: InboxSnapshot) -> dict[str, tuple[int, int]]:
    """把快照里的"可导入"文件取成 `relative -> (size, mtime_ns)`。"""
    return snapshot.ready_map()


def filter_plan(
    plan: classify_import.ClassifyImportPlan,
    ready: Mapping[str, Sequence[int]],
) -> classify_import.ClassifyImportPlan:
    """把一次完整导入计划裁剪成"只包含收件箱里已稳定的文件"。

    为什么不在 `build_plan` 上加过滤参数：计划里的分类/时段/冲突改名是**整批**算的，
    在源目录层面过滤会改变时段切分。这里对**成品计划**做裁剪，语义是
    "这批里我只执行这些文件"，不重新分类。

    ⚠️ 裁剪时**再核对一次磁盘上的大小与 mtime**：从入队到真正执行之间，
    文件可能又被追加写入（用户第二次复制同名文件）。对不上的直接丢弃，下次扫描再说。
    """
    from . import classify as classify_mod

    allowed: dict[str, tuple[int, int]] = {}
    for entry in plan.entries:
        want = ready.get(entry.item.key)
        if not want or len(want) < 2:
            continue
        try:
            stat = entry.path.stat()
        except OSError:
            continue
        if int(stat.st_size) == int(want[0]) and int(stat.st_mtime_ns) == int(want[1]):
            allowed[entry.item.key] = (int(want[0]), int(want[1]))

    kept = [item for item in plan.plan.files if item.key in allowed]
    kept_entries = [entry for entry in plan.entries if entry.item.key in allowed]
    notes = ["收件箱：只导入已复制完成的文件（其余仍在复制或已导入）"] + list(plan.notes)
    filtered_plan = classify_mod.ClassifyPlan(
        files=kept,
        sessions=list(plan.plan.sessions),
        unresolved_cells=list(plan.plan.unresolved_cells),
        unknown_city_count=sum(1 for item in kept if item.city == classify_mod.UNKNOWN_CITY),
        unknown_time_count=sum(1 for item in kept if item.time_unknown),
        city_override=plan.plan.city_override,
        city_override_disagreements=list(plan.plan.city_override_disagreements),
        notes=notes,
    )
    return classify_import.ClassifyImportPlan(
        entries=kept_entries,
        plan=filtered_plan,
        skipped=list(plan.skipped),
        target_root={key: root for key, root in plan.target_root.items() if key in allowed},
        library_of={key: value for key, value in plan.library_of.items() if key in allowed},
        duplicates=[pair for pair in plan.duplicates if pair[0] in allowed],
        source_total=len(kept),
        source_bytes=sum(entry.size for entry in kept_entries),
        preview_sampled=False,
        notes=notes,
        seconds=plan.seconds,
    )


def record_import(
    settings: Settings,
    plan: classify_import.ClassifyImportPlan,
    report: classify_import.ClassifyImportReport,
) -> int:
    """把**确实已经落库**的文件记进台账，返回本次新增/更新的条数。

    只记 `report` 里报成功的两类：复制成功的（`sources_copied`）与
    目标已有同内容而跳过的（`sources_skipped`）。失败的、目标冲突的**绝不记** ——
    记了就等于把"下次重试"这条路堵死。
    """
    done: set[str] = set()
    for raw in list(report.sources_copied) + list(getattr(report, "sources_skipped", [])):
        try:
            done.add(str(Path(raw)))
        except TypeError:  # pragma: no cover - 防御：report 字段被别处塞了怪东西
            continue
    if not done:
        return 0

    ledger_file = _ledger_path(settings)
    raw = _read_json(ledger_file)
    entries = raw.get("entries")
    entries = dict(entries) if isinstance(entries, dict) else {}

    by_key = {entry.item.key: entry for entry in plan.entries}
    stamp = utc_now()
    recorded = 0
    for classified in plan.plan.files:
        entry = by_key.get(classified.key)
        if entry is None or str(entry.path) not in done:
            continue
        root = plan.target_root.get(classified.key)
        if root is None:
            continue
        target = Path(root) / Path(classified.target_relative.replace("\\", "/"))
        if not target.is_file():
            continue
        try:
            stat = entry.path.stat()
            size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
        except OSError:
            # 源文件刚被用户拿走：仍按计划时的大小/时间记，避免下次又被判成"新文件"
            size, mtime_ns = int(entry.size), -1
        entries[classified.key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "target": classified.target_relative,
            "library_id": plan.library_of.get(classified.key),
            "imported_at": stamp,
        }
        recorded += 1

    # 计划阶段因"目标已有同内容"而剔除的源文件（`plan.duplicates`）不在
    # `plan.plan.files` 里；旧实现只遍历 files，于是这 165 个源永远记不进台账，
    # 每次扫描都显示"可导入"，重复导入又继续跳过。这里按 duplicates 的既有目标
    # 补记，目标仍在校验一遍。
    for key, existing_relative in list(getattr(plan, "duplicates", []) or []):
        entry = by_key.get(key)
        if entry is None:
            continue
        root = plan.target_root.get(key)
        if root is None:
            continue
        target = Path(root) / Path(str(existing_relative).replace("\\", "/"))
        if not target.is_file():
            continue
        try:
            stat = entry.path.stat()
            size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
        except OSError:
            size, mtime_ns = int(entry.size), -1
        entries[key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "target": existing_relative,
            "library_id": plan.library_of.get(key),
            "imported_at": stamp,
        }
        recorded += 1

    if recorded:
        if len(entries) > LEDGER_LIMIT:
            newest = sorted(
                entries.items(),
                key=lambda pair: str(pair[1].get("imported_at") or ""),
            )[-LEDGER_LIMIT:]
            entries = dict(newest)
        _write_json(ledger_file, {"version": 1, "entries": entries})
    return recorded


@dataclass
class InboxCleanupResult:
    """清理已入库的收件箱副本的结果。"""

    deleted: int = 0
    bytes: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"已清理 {self.deleted} 个文件（{self.bytes / 1e6:.1f} MB）"
        if self.kept:
            text += f"；保留 {self.kept} 个（目标不存在或文件已变化）"
        if self.errors:
            text += f"；{len(self.errors)} 个删除失败"
        return text


def cleanup_imported(
    settings: Settings,
    *,
    library_roots: Mapping[int, Path],
) -> InboxCleanupResult:
    """删除收件箱里**已经安全入库**的副本。

    这是收件箱"只读"承诺的唯一例外，必须同时满足：

    * 文件在收件箱根内，且不是符号链接；
    * 台账里有它（说明之前已经导入成功）；
    * 当前大小/mtime 仍与台账一致（没有被重新复制/改写）；
    * 它所在的照片库根可解析，目标文件仍存在且大小一致。

    任何一条不满足都保留文件。只删收件箱副本，手机与照片库里的文件不动。
    台账里被删掉的条目也一并清掉，免留下"文件已不在，却仍记为已导入"的幽灵。
    """
    inbox_root = Path(settings.effective_inbox_path)
    try:
        root = inbox_root.resolve()
    except OSError:
        return InboxCleanupResult()
    ledger_file = _ledger_path(settings)
    raw = _read_json(ledger_file)
    entries = raw.get("entries")
    if not isinstance(entries, dict) or not entries:
        return InboxCleanupResult()

    roots: dict[int, Path] = {}
    for library_id, path in (library_roots or {}).items():
        if path is None:
            continue
        try:
            roots[int(library_id)] = Path(path).resolve()
        except (OSError, TypeError, ValueError):
            continue

    result = InboxCleanupResult()
    deleted_keys: list[str] = []
    removed_dirs: set[Path] = set()
    for key, entry in list(entries.items()):
        if not isinstance(entry, dict):
            continue
        try:
            source = (root / str(key)).resolve()
            if not source.is_relative_to(root) or source == root:
                result.errors.append(f"{key}: 不在收件箱内")
                continue
            if not source.is_file() or source.is_symlink():
                continue
            stat = source.stat()
            if int(entry.get("size") or -1) != int(stat.st_size):
                result.kept += 1
                continue
            if int(entry.get("mtime_ns") or -1) != int(stat.st_mtime_ns):
                result.kept += 1
                continue
            library_id = int(entry.get("library_id") or 0)
            target_root = roots.get(library_id)
            if target_root is None:
                result.kept += 1
                continue
            target = (target_root / str(entry.get("target") or "")).resolve()
            if not target.is_relative_to(target_root) or not target.is_file():
                result.kept += 1
                continue
            if target == source or target.stat().st_size != stat.st_size:
                result.kept += 1
                continue
            source.unlink()
        except OSError as exc:
            result.errors.append(f"{key}: {exc}")
            continue
        result.deleted += 1
        result.bytes += int(stat.st_size)
        deleted_keys.append(str(key))
        removed_dirs.add(source.parent)

    if deleted_keys:
        for key in deleted_keys:
            entries.pop(key, None)
        _write_json(ledger_file, {"version": 1, "entries": entries})
        # 只收走空目录；有别的文件（例如还没导入的）就保留整条路径。
        for directory in sorted(removed_dirs, key=lambda item: len(item.parts), reverse=True):
            while directory != root and directory.is_relative_to(root):
                try:
                    directory.rmdir()
                except OSError:
                    break
                directory = directory.parent
    return result


def ensure_inbox_dir(settings: Settings) -> Path:
    """确保收件箱存在（只在用户点「打开收件箱」时创建，不在启动时偷偷建）。"""
    root = Path(settings.effective_inbox_path)
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = [
    "InboxCleanupResult",
    "InboxFile",
    "InboxSnapshot",
    "cleanup_imported",
    "ensure_inbox_dir",
    "filter_plan",
    "ready_map",
    "record_import",
    "scan_inbox",
]
