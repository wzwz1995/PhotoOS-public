"""Library / Volume 持久化与身份识别（PRD §5.1 / §5.2 LOCKED）。

身份判定优先级（**永不使用盘符或用户名**）：

  1. `<root>\\.photoos\\library.json` 中的 `library_uuid`  —— 照片库自带标记，最强判据
  2. Windows 卷序列号（`GetVolumeInformationW`）
  3. 卷指纹（已知相对路径 + size + mtime 抽样匹配率）      —— 应对整盘更换/重新格式化

盘符（`mount_hint`）只作为**展示提示**写入数据库，任何判定逻辑都不得依赖它。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..db import Database, utc_now
from ..errors import LibraryError, LibraryNotFound, SafetyError
from . import paths, volume

MARKER_DIRNAME = ".photoos"
MARKER_FILENAME = "library.json"
MARKER_SCHEMA = 1


# --------------------------------------------------------------------------- 模型
@dataclass(frozen=True)
class LibraryRecord:
    id: int
    uuid: str
    name: str
    root_path_hint: str | None
    marker_uuid: str | None
    status: str
    created_at: str
    last_seen_at: str | None
    notes: str | None

    @property
    def root_path(self) -> Path | None:
        return Path(self.root_path_hint) if self.root_path_hint else None

    @property
    def is_online(self) -> bool:
        return self.status in {"ONLINE", "SCANNING"}


@dataclass(frozen=True)
class VolumeRecord:
    id: int
    library_id: int | None
    kind: str
    serial: int | None
    fingerprint: str | None
    fs_type: str | None
    label: str | None
    mount_hint: str | None
    status: str
    first_seen_at: str
    last_seen_at: str | None

    @property
    def serial_hex(self) -> str:
        return volume.format_serial(self.serial)


# --------------------------------------------------------------------------- 标记
def marker_path(root: Path) -> Path:
    return Path(root) / MARKER_DIRNAME / MARKER_FILENAME


def read_marker(root: Path) -> dict[str, Any] | None:
    """读取照片库自带的库标记。不存在或损坏返回 None（不抛错，交由后续判据）。"""
    path = marker_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("library_uuid"):
        return None
    return payload


def write_marker(root: Path, library_uuid: str, name: str, *, dry_run: bool = False) -> Path:
    """写入库标记。

    **这是 PhotoOS 唯一允许写入照片库的例外**，且只写一个属于 PhotoOS 自己的
    隐藏小文件（不触碰任何照片）。为遵守「Phase 1 对照片库只读」的纪律，
    默认 `dry_run=True` 由调用方显式决定是否落盘。
    """
    path = marker_path(root)
    payload = {
        "marker_schema": MARKER_SCHEMA,
        "library_uuid": library_uuid,
        "name": name,
        "created_at": utc_now(),
        "created_by": "PhotoOS",
        "note": "此文件用于在盘符变化或换机后识别本照片库；请勿删除或修改。",
    }
    if dry_run:
        return path
    if not Path(root).is_dir():
        raise LibraryError(f"照片库路径不存在，无法写入标记：{root}")
    if path.exists():
        # 标记已存在（可能来自另一台电脑或另一次登记）。绝不静默改写既有身份，
        # 否则「库标记是跨机器最强判据」这条会被破坏。
        raise LibraryError(
            f"该库根已存在 PhotoOS 标记，拒绝覆盖：{path}。"
            "如需接管既有库，请使用 `photoos library add`（会沿用标记中的 uuid）。",
            detail={"kind": "marker_exists", "path": str(path)},
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)  # 原子替换，避免留下半截 JSON
    except OSError as exc:
        tmp.unlink(missing_ok=True)  # 失败不留残骸
        raise LibraryError(f"写入库标记失败：{exc}", detail={"path": str(path)}) from exc
    return path


# --------------------------------------------------------------------------- 指纹
def compute_fingerprint(samples: list[tuple[str, int, int]], *, limit: int = 256) -> str:
    """由抽样 (相对路径, size, mtime_ns) 计算卷指纹。

    只使用**相对路径**与文件元数据，因此与盘符、机器、用户名完全无关。
    排序后取前 `limit` 条，保证同一棵树在不同扫描顺序下得到相同指纹。
    """
    normalized = sorted((rel.replace("/", "\\").casefold(), size, mtime) for rel, size, mtime in samples)
    hasher = hashlib.sha256()
    for rel, size, mtime in normalized[:limit]:
        hasher.update(f"{rel}|{size}|{mtime}\n".encode("utf-8"))
    return hasher.hexdigest()


# --------------------------------------------------------------------------- Store
class LibraryStore:
    """Library / Volume 的读写。所有写入都假定调用方处于单写线程语义下。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 读
    def get(self, library_id: int) -> LibraryRecord:
        row = self.db.query_one("SELECT * FROM library WHERE id = ?", (library_id,))
        if row is None:
            raise LibraryNotFound(f"Library 不存在：id={library_id}", detail={"library_id": library_id})
        return _to_library(row)

    def find_by_uuid(self, library_uuid: str) -> LibraryRecord | None:
        row = self.db.query_one("SELECT * FROM library WHERE uuid = ?", (library_uuid,))
        return _to_library(row) if row else None

    def find_by_marker(self, marker_uuid: str) -> LibraryRecord | None:
        row = self.db.query_one(
            "SELECT * FROM library WHERE marker_uuid = ? OR uuid = ?", (marker_uuid, marker_uuid)
        )
        return _to_library(row) if row else None

    def list_all(self) -> list[LibraryRecord]:
        return [_to_library(r) for r in self.db.query("SELECT * FROM library ORDER BY id")]

    def find_by_volume_serial(self, serial: int) -> tuple[LibraryRecord, VolumeRecord] | None:
        """兼容接口：返回该卷上最近登记的一个 Library。

        **注意**：同一卷上可能有多个 Library（例如同一块盘上的 `照片` 与 `视频`），
        因此这里的"命中"**不足以判定身份**。判定必须走 `match_library_by_root()`。
        """
        if not serial:
            return None
        row = self.db.query_one(
            "SELECT * FROM volume WHERE serial = ?"
            " ORDER BY CASE WHEN status = 'ONLINE' THEN 0 ELSE 1 END, id DESC LIMIT 1",
            (serial,),
        )
        if row is None or row["library_id"] is None:
            return None
        volume_record = _to_volume(row)
        return self.get(int(row["library_id"])), volume_record

    def libraries_for_volume(self, serial: int) -> list[tuple[LibraryRecord, str | None]]:
        """该卷上登记过的**全部** Library 及其根路径提示。"""
        if not serial:
            return []
        rows = self.db.query(
            "SELECT l.*, vl.root_hint AS vl_root_hint FROM volume_library AS vl"
            " JOIN library AS l ON l.id = vl.library_id"
            " JOIN volume AS v ON v.id = vl.volume_id"
            " WHERE v.serial = ? ORDER BY l.id",
            (serial,),
        )
        return [(_to_library(r), r["vl_root_hint"]) for r in rows]

    def link_volume_library(self, volume_id: int, library_id: int, root_hint: Path | str) -> None:
        """登记「该卷承载该库」的关系（多对多，幂等）。"""
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO volume_library (volume_id, library_id, root_hint, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(volume_id, library_id) DO UPDATE SET"
                " root_hint = excluded.root_hint, last_seen_at = excluded.last_seen_at",
                (volume_id, library_id, str(root_hint), now, now),
            )

    def root_hints_for_volume(self, serial: int) -> list[str]:
        if not serial:
            return []
        rows = self.db.query(
            "SELECT DISTINCT vl.root_hint AS h FROM volume_library AS vl"
            " JOIN volume AS v ON v.id = vl.volume_id"
            " WHERE v.serial = ? AND vl.root_hint IS NOT NULL",
            (serial,),
        )
        return [str(r["h"]) for r in rows]

    def known_dirs(self, library_id: int, limit: int = 64) -> set[str]:
        """该 Library 已知的顶层相对目录名（取自 asset 表），用于结构重合度比对。"""
        rows = self.db.query(
            "SELECT relative_path FROM asset WHERE library_id = ? LIMIT ?",
            (library_id, limit * 8),
        )
        tops: set[str] = set()
        for row in rows:
            parts = str(row["relative_path"]).split("\\")
            if len(parts) > 1:
                tops.add(parts[0].casefold())
            if len(tops) >= limit:
                break
        return tops

    def volumes_for(self, library_id: int) -> list[VolumeRecord]:
        """该 Library 所在的全部卷。

        走 `volume_library` 连接表而不是 `volume.library_id` —— 后者是"最近一次登记"的
        单值字段，同一块盘上存在多个 Library 时会被后来的库覆盖（真实现场：
        `C:\\Photos` 与 `E:\\视频` 同盘）。
        """
        rows = self.db.query(
            "SELECT v.* FROM volume_library AS vl"
            " JOIN volume AS v ON v.id = vl.volume_id"
            " WHERE vl.library_id = ? ORDER BY v.id",
            (library_id,),
        )
        if rows:
            return [_to_volume(r) for r in rows]
        # 兼容尚未建立连接关系的历史数据
        legacy = self.db.query("SELECT * FROM volume WHERE library_id = ? ORDER BY id", (library_id,))
        return [_to_volume(r) for r in legacy]

    def volume_root_hint(self, library_id: int, volume_id: int) -> str | None:
        row = self.db.query_one(
            "SELECT root_hint FROM volume_library WHERE library_id = ? AND volume_id = ?",
            (library_id, volume_id),
        )
        return str(row["root_hint"]) if row and row["root_hint"] else None

    def live_asset_count(self, library_id: int) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM asset WHERE library_id = ? AND status NOT IN ('MISSING','QUARANTINED')",
                (library_id,),
                default=0,
            )
        )

    # ------------------------------------------------------------------ 写
    def create(
        self,
        *,
        name: str,
        root: Path,
        library_uuid: str | None = None,
        marker_uuid: str | None = None,
        notes: str | None = None,
    ) -> LibraryRecord:
        library_uuid = library_uuid or str(uuid_module.uuid4())
        now = utc_now()
        if not Path(root).is_dir():
            raise LibraryError(
                f"照片库路径不存在或不可访问：{root}",
                detail={"kind": "root_missing", "root": str(root)},
            )
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO library (uuid, name, root_path_hint, marker_uuid, status, created_at,"
                " last_seen_at, notes) VALUES (?, ?, ?, ?, 'ONLINE', ?, ?, ?)",
                (library_uuid, name, str(root), marker_uuid, now, now, notes),
            )
            library_id = int(cur.lastrowid or 0)
        return self.get(library_id)

    def touch_seen(self, library_id: int) -> None:
        self.db.execute(
            "UPDATE library SET last_seen_at = ? WHERE id = ?", (utc_now(), library_id)
        )

    def set_status(self, library_id: int, status: str) -> None:
        self.db.execute("UPDATE library SET status = ? WHERE id = ?", (status, library_id))

    def set_root_hint(self, library_id: int, root: Path) -> None:
        """更新运行时路径提示。**这不是身份变更**，仅记录最近一次看到的位置。"""
        self.db.execute(
            "UPDATE library SET root_path_hint = ?, last_seen_at = ? WHERE id = ?",
            (str(root), utc_now(), library_id),
        )

    def set_marker_uuid(self, library_id: int, marker_uuid: str) -> None:
        """记录库根标记中的 uuid。

        注意 `uuid` 与 `marker_uuid` 通常相同，但允许不同：若某个库根已带有别的 uuid，
        我们沿用标记里的 uuid 作为身份，避免同一块盘在两台机器上被登记成两个库。
        """
        self.db.execute("UPDATE library SET marker_uuid = ? WHERE id = ?", (marker_uuid, library_id))

    # ------------------------------------------------------------------ 卷
    def record_volume(
        self,
        identity: volume.VolumeIdentity,
        *,
        library_id: int | None = None,
        fingerprint: str | None = None,
    ) -> VolumeRecord:
        """登记（或更新）一个卷。同一 serial 视为同一卷。"""
        now = utc_now()
        existing = None
        if identity.has_serial:
            existing = self.db.query_one("SELECT * FROM volume WHERE serial = ?", (identity.serial,))
        if existing is None:
            with self.db.write() as conn:
                cur = conn.execute(
                    "INSERT INTO volume (library_id, kind, serial, fingerprint, fs_type, label,"
                    " mount_hint, status, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'ONLINE', ?, ?)",
                    (
                        library_id,
                        identity.kind,
                        identity.serial if identity.has_serial else None,
                        fingerprint,
                        identity.fs_type,
                        identity.label,
                        identity.mount,
                        now,
                        now,
                    ),
                )
                volume_id = int(cur.lastrowid or 0)
            return self.get_volume(volume_id)

        with self.db.write() as conn:
            conn.execute(
                "UPDATE volume SET library_id = COALESCE(?, library_id), kind = ?, fingerprint ="
                " COALESCE(?, fingerprint), fs_type = ?, label = ?, mount_hint = ?, status ="
                " 'ONLINE', last_seen_at = ? WHERE id = ?",
                (
                    library_id,
                    identity.kind,
                    fingerprint,
                    identity.fs_type,
                    identity.label,
                    identity.mount,
                    now,
                    existing["id"],
                ),
            )
        return self.get_volume(int(existing["id"]))

    def get_volume(self, volume_id: int) -> VolumeRecord:
        row = self.db.query_one("SELECT * FROM volume WHERE id = ?", (volume_id,))
        if row is None:
            raise LibraryError(f"卷记录不存在：id={volume_id}")
        return _to_volume(row)

    def mark_volumes_offline(self, library_id: int) -> int:
        return self.db.execute(
            "UPDATE volume SET status = 'OFFLINE' WHERE library_id = ? AND status = 'ONLINE'",
            (library_id,),
        )

    # ------------------------------------------------------------------ 事件
    def log_event(self, kind: str, *, library_id: int | None = None, payload: Any = None) -> None:
        self.db.execute(
            "INSERT INTO app_event (at, kind, library_id, payload_json) VALUES (?, ?, ?, ?)",
            (
                utc_now(),
                kind,
                library_id,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            ),
        )


# --------------------------------------------------------------------------- 探测
@dataclass(frozen=True)
class ProbeResult:
    """对一个路径的只读探测结论。`action` 指明应如何登记它。"""

    root: Path
    exists: bool
    is_dir: bool
    action: str                    # create | adopt | reuse | rebind | offline
    reason: str
    marker: dict[str, Any] | None
    volume_identity: volume.VolumeIdentity | None
    library: LibraryRecord | None

    @property
    def needs_marker_write(self) -> bool:
        """是否建议在该库根写入 `.photoos/library.json`（新建库时才需要）。"""
        return self.action == "create"


def probe_path(db: Database, root: Path) -> ProbeResult:
    """判定一个路径相对于现有 Library 的身份。纯只读。

    判据强度（高 → 低）：
      1. **库标记** `<root>\\.photoos\\library.json` —— 唯一无歧义判据
      2. **结构重合度**（同卷候选库的顶层目录交集）—— 处理标记丢失的情况
      3. 卷序列号 —— **只用于缩小候选范围，不足以判定身份**
         （同一块盘上有多个 Library 是正常情况，例如 `C:\\Photos` 与 `E:\\视频`）
    """
    store = LibraryStore(db)
    root = Path(root)
    marker = read_marker(root) if root.is_dir() else None
    identity = volume.volume_identity(root) if root.is_dir() else None

    if not root.exists():
        return ProbeResult(
            root=root, exists=False, is_dir=False, action="offline",
            reason="路径不存在或当前不可访问（若这是已登记的 Library，可将其标记为离线后继续使用索引）",
            marker=None, volume_identity=None, library=None,
        )
    if not root.is_dir():
        return ProbeResult(
            root=root, exists=True, is_dir=False, action="offline",
            reason="路径存在但不是目录", marker=None, volume_identity=identity, library=None,
        )

    # ---- 判据 1：库自带标记
    if marker is not None:
        marker_uuid = str(marker["library_uuid"])
        existing = store.find_by_marker(marker_uuid)
        if existing is not None:
            unchanged = _same_hint(existing, root)
            return ProbeResult(
                root, True, True,
                "reuse" if unchanged else "rebind",
                "通过库标记命中已登记的 Library（路径未变）"
                if unchanged
                else "通过库标记命中已登记的 Library，但路径已变化（盘符变化/换盘），需重绑定",
                marker, identity, existing,
            )
        # 带标记但本机库里没有 → 采纳（换机场景），沿用标记中的 uuid
        return ProbeResult(
            root, True, True, "adopt",
            f"发现已有的 PhotoOS 库标记（uuid={marker_uuid}），将按该 uuid 登记",
            marker, identity, None,
        )

    # ---- 判据 2/3：无标记，按结构与卷比对
    match, reason = match_library_by_root(store, root)
    if match is not None:
        unchanged = _same_hint(match, root)
        return ProbeResult(
            root, True, True,
            "reuse" if unchanged else "rebind",
            f"{reason}；{'路径未变' if unchanged else '路径已变化，需重绑定'}",
            marker, identity, match,
        )

    # ---- 无标记且无法确认 → 作为新库（并说明为何没有匹配上）
    return ProbeResult(
        root, True, True, "create",
        f"{reason}；将作为新 Library 登记",
        marker, identity, None,
    )


def _same_hint(library: LibraryRecord, root: Path) -> bool:
    if not library.root_path_hint:
        return False
    try:
        return os.path.normcase(str(Path(library.root_path_hint).resolve())) == os.path.normcase(
        str(Path(root).resolve())
    )
    except OSError:
        return False


def top_level_dirs(root: Path, *, limit: int = 512) -> set[str]:
    """读取目录的顶层子目录名（小写）。

    用于结构重合度比对：`C:\\Photos` 的顶层是城市名，`E:\\视频` 也是城市名，因此
    **结构重合度判据无法区分它们** —— 这正是必须依赖库标记的原因。保留该判据是为了
    处理「库标记丢失但结构仍在」的情况，并且只有在重合度足够高时才接受。
    """
    names: set[str] = set()
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                if len(names) >= limit:
                    break
                try:
                    if entry.is_dir():
                        names.add(entry.name.casefold())
                except OSError:
                    continue
    except OSError:
        return set()
    return names


def match_library_by_root(
    store: LibraryStore, root: Path
) -> tuple[LibraryRecord | None, str]:
    """在已知 Library 中寻找与 `root` 相符的一个（用于"无标记"情形）。

    判据（按强度）：
      1. 路径提示完全相同 → 直接命中（属于同一路径，不是身份判断）
      2. 该卷上候选库的结构重合度（顶层目录 Jaccard）≥ 0.6 且交集 ≥ 2 → 命中
      3. 候选不唯一 → 拒绝猜测（返回 None），由调用方要求显式处理

    **绝不**因为"卷序列号相同"就判定为同一个库：同一块盘上可以有多个 Library。
    """
    identity = volume.volume_identity(root)
    if identity is None or not identity.has_serial:
        return None, "该路径无可用卷序列号，无法做卷级比对"

    candidates = store.libraries_for_volume(identity.serial)
    if not candidates:
        return None, "该卷上没有登记过任何 Library"

    # 判据 1：同一路径
    for library, _hint in candidates:
        if _same_hint(library, root):
            return library, f"路径与已登记 Library「{library.name}」的路径提示一致"

    # 判据 2：结构重合度（同时要求目录名集合不退化）
    found_dirs = top_level_dirs(root)
    scored: list[tuple[float, int, LibraryRecord]] = []
    for library, _hint in candidates:
        known = store.known_dirs(library.id)
        if not known or not found_dirs:
            continue
        inter = known & found_dirs
        union = known | found_dirs
        overlap = len(inter) / len(union) if union else 0.0
        scored.append((overlap, len(inter), library))

    if not scored:
        names = "、".join(library.name for library, _ in candidates)
        return None, f"该卷上登记有 {len(candidates)} 个 Library（{names}），但目录结构不足以判定"

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_overlap, best_inter, best_library = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if best_overlap >= 0.6 and best_inter >= 2 and best_overlap > runner_up:
        return best_library, (
            f"目录结构与该卷上的 Library「{best_library.name}」高度重合"
            f"（重合度 {best_overlap:.0%}，共同顶层目录 {best_inter} 个）"
        )

    names = "、".join(library.name for library, _ in candidates)
    return None, (
        f"该卷上登记有 {len(candidates)} 个 Library（{names}），"
        f"但目录结构重合度不足以区分（最高 {best_overlap:.0%}）"
    )


# --------------------------------------------------------------------------- 断言
def assert_within_root(root: Path, candidate: Path) -> None:
    """任何准备写入的路径必须位于给定根之下（防止误写照片库或系统目录）。"""
    if not paths.is_same_root(root, candidate):
        raise SafetyError(
            f"拒绝操作：{candidate} 不在允许的根 {root} 之下",
            detail={"kind": "outside_root", "root": str(root), "candidate": str(candidate)},
        )


def default_data_root_guard(candidate: Path) -> None:
    """确保不会把数据写进照片库以外的意外位置（Phase 1 的照片库一律只读）。"""
    assert_within_root(PROJECT_ROOT, candidate)


# --------------------------------------------------------------------------- 映射
def _to_library(row: Any) -> LibraryRecord:
    return LibraryRecord(
        id=int(row["id"]),
        uuid=str(row["uuid"]),
        name=str(row["name"]),
        root_path_hint=row["root_path_hint"],
        marker_uuid=row["marker_uuid"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        last_seen_at=row["last_seen_at"],
        notes=row["notes"],
    )


def _to_volume(row: Any) -> VolumeRecord:
    return VolumeRecord(
        id=int(row["id"]),
        library_id=int(row["library_id"]) if row["library_id"] is not None else None,
        kind=str(row["kind"]),
        serial=int(row["serial"]) if row["serial"] is not None else None,
        fingerprint=row["fingerprint"],
        fs_type=row["fs_type"],
        label=row["label"],
        mount_hint=row["mount_hint"],
        status=str(row["status"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=row["last_seen_at"],
    )
