"""盘符变化与整盘更换后的 Asset 重绑定（PRD §5.2 LOCKED）。

流程（严格按 PRD 顺序）：

```
Library UUID → 扫描新位置 → Relative Path + Hash 校验 → 重新绑定 Asset
```

核心纪律：
  * **盘符永远不是资产身份**：`D:\\PHOTO_LIBRARY` → `E:\\PHOTO_LIBRARY` 只是路径提示变化，
    绝不能因此判定"照片全部消失"或"需要重新导入"。
  * 校验优先用**内容哈希**（若已计算），否则退化为 size + mtime 快速校验，
    再退化为"仅相对路径存在"。三条判据的强弱会在报告里标明。
  * 全程只读：重绑定只改数据库，不移动、不改名、不触碰任何原文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..db import Database, utc_now
from ..errors import LibraryError
from ..hashing import hash_file
from ..repos import AssetRepo, asset_absolute_path
from .store import LibraryRecord, LibraryStore, ProbeResult, probe_path
from .volume import volume_identity

#: 校验强度标签
VERIFY_HASH = "hash"          # 内容哈希一致 —— 最强
VERIFY_META = "size+mtime"    # 大小与修改时间一致 —— 快
VERIFY_PATH = "path-only"     # 仅路径存在 —— 最弱（未哈希时）

#: 抽样校验数量：全量哈希在大库上很慢，抽样足以判定"这是同一棵树"
DEFAULT_SAMPLE = 32


@dataclass
class RebindReport:
    library_id: int
    library_uuid: str
    old_root: str | None
    new_root: str
    probe_action: str
    probe_reason: str
    total_assets: int = 0
    found: int = 0
    missing: int = 0
    verified_by: str = VERIFY_PATH
    hash_ok: int = 0
    hash_mismatch: int = 0
    sample_size: int = 0
    missing_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rebound: bool = False

    @property
    def confidence(self) -> str:
        if self.verified_by == VERIFY_HASH:
            return "高（内容哈希一致）"
        if self.verified_by == VERIFY_META:
            return "中（大小与时间一致）"
        return "低（仅路径存在，尚未哈希）"

    def lines(self) -> list[str]:
        out = [
            f"Library #{self.library_id}「{self.library_uuid[:8]}…」",
            f"  原路径提示：{self.old_root or '(未记录)'}",
            f"  新路径提示：{self.new_root}",
            f"  判定：{self.probe_action} — {self.probe_reason}",
            f"  索引中 Asset：{self.total_assets}",
            f"  在新位置找到：{self.found}",
            f"  未找到：{self.missing}",
            f"  校验强度：{self.confidence}",
        ]
        if self.sample_size:
            out.append(
                f"  抽样校验：{self.sample_size} 个文件，"
                f"哈希一致 {self.hash_ok}，不一致 {self.hash_mismatch}"
            )
        if self.missing_samples:
            out.append("  未找到样例（最多 5 条）：")
            out.extend(f"    - {item}" for item in self.missing_samples[:5])
        out.extend(f"  警告：{item}" for item in self.warnings)
        out.append(f"  结果：{'已重新绑定' if self.rebound else '未改动数据库'}")
        return out


def rebind_library(
    db: Database,
    library_id: int,
    new_root: Path,
    *,
    sample: int = DEFAULT_SAMPLE,
    verify_hashes: bool = True,
    apply: bool = True,
) -> RebindReport:
    """把 Library 重新绑定到新位置。

    `apply=False` 时只做只读预演（dry-run），不改数据库 —— 用于先确认再执行。
    """
    store = LibraryStore(db)
    library = store.get(library_id)
    new_root = Path(new_root)

    probe: ProbeResult = probe_path(db, new_root)
    report = RebindReport(
        library_id=library.id,
        library_uuid=library.uuid,
        old_root=library.root_path_hint,
        new_root=str(new_root),
        probe_action=probe.action,
        probe_reason=probe.reason,
    )

    if not probe.is_dir:
        report.warnings.append("新位置当前不可访问；若这是临时离线，请保持 Library 离线状态而不是重绑定")
        return report

    if probe.action == "offline":
        report.warnings.append("探测结论为离线，已中止")
        return report

    assets = AssetRepo(db)
    rows = list(
        db.query(
            "SELECT id, relative_path, size, mtime_ns, content_hash, status"
            " FROM asset WHERE library_id = ? ORDER BY id",
            (library.id,),
        )
    )
    report.total_assets = len(rows)

    # ---- 第一步：逐条检查新位置是否存在（纯 stat，不读内容）
    present: list[tuple[object, Path]] = []
    for row in rows:
        path = asset_absolute_path(new_root, str(row["relative_path"]))
        if path.is_file():
            present.append((row, path))
        else:
            report.missing += 1
            if len(report.missing_samples) < 20:
                report.missing_samples.append(str(row["relative_path"]))
    report.found = len(present)

    if report.total_assets and report.found == 0:
        report.warnings.append(
            "在新位置没有找到任何已索引文件 —— 这更可能是选错了目录，而不是照片全部丢失。"
            "请确认路径后重试（数据库未做任何改动）。"
        )
        return report

    # ---- 第二步：抽样校验内容/元数据（决定"这是不是同一棵树"）
    hashed_rows = [item for item in present if item[0]["content_hash"]]
    if verify_hashes and hashed_rows:
        report.verified_by = VERIFY_HASH
        step = max(1, len(hashed_rows) // max(1, sample))
        chosen = hashed_rows[::step][:sample]
        report.sample_size = len(chosen)
        for row, path in chosen:
            try:
                digest = hash_file(path)
            except OSError as exc:
                report.warnings.append(f"抽样校验读取失败：{row['relative_path']}（{exc}）")
                continue
            if digest == row["content_hash"]:
                report.hash_ok += 1
            else:
                report.hash_mismatch += 1
        if report.hash_mismatch:
            report.warnings.append(
                f"抽样中有 {report.hash_mismatch} 个文件内容与索引不一致 —— "
                "该位置可能不是同一个照片库，或文件已被外部修改。重绑定已中止。"
            )
            return report
    else:
        # 未哈希：退化为 size + mtime 抽样比对
        report.verified_by = VERIFY_META
        step = max(1, len(present) // max(1, sample))
        chosen = present[::step][:sample]
        report.sample_size = len(chosen)
        mismatch = 0
        for row, path in chosen:
            stat = path.stat()
            if int(row["size"] or -1) == stat.st_size and int(row["mtime_ns"] or -1) == stat.st_mtime_ns:
                report.hash_ok += 1
            else:
                mismatch += 1
        if chosen and mismatch == len(chosen):
            report.verified_by = VERIFY_PATH
            report.warnings.append(
                "抽样的大小/时间全部不一致，且尚未计算内容哈希 —— 无法确认是同一个库。"
                "建议先对新位置执行 `photoos hash` 后再重绑定。"
            )
            return report
        if mismatch:
            report.warnings.append(f"抽样中 {mismatch}/{len(chosen)} 个文件大小或时间不一致，请留意")

    if not apply:
        report.warnings.append("这是预演（dry-run），数据库未做改动")
        return report

    # ---- 第三步：落库（只改数据库）
    stamp = utc_now()
    found_relatives = {str(row["relative_path"]) for row, _ in present}
    missing_relatives = [str(row["relative_path"]) for row in rows if str(row["relative_path"]) not in found_relatives]

    with db.write() as conn:
        conn.execute(
            "UPDATE library SET root_path_hint = ?, status = 'ONLINE', last_seen_at = ? WHERE id = ?",
            (str(new_root), stamp, library.id),
        )
        # 在新位置找到的 → 复活为可用；未找到的 → MISSING（不删除行）
        conn.executemany(
            "UPDATE asset SET status = CASE WHEN content_hash IS NULL THEN 'PENDING' ELSE 'OK' END,"
            " status_reason = NULL, last_verified_at = ? WHERE id = ?",
            [(stamp, int(row["id"])) for row, _ in present],
        )
        if missing_relatives:
            conn.executemany(
                "UPDATE asset SET status = 'MISSING', status_reason = ?, last_verified_at = ?"
                " WHERE library_id = ? AND relative_path = ?",
                [("重绑定后在新位置未找到", stamp, library.id, rel) for rel in missing_relatives],
            )

    # 卷关系：同一块盘可能承载多个 Library，因此走连接表
    identity = volume_identity(new_root)
    if identity is not None:
        record = store.record_volume(identity, library_id=library.id)
        store.link_volume_library(record.id, library.id, new_root)

    store.log_event(
        "rebind",
        library_id=library.id,
        payload={
            "old_root": report.old_root,
            "new_root": str(new_root),
            "found": report.found,
            "missing": report.missing,
            "verified_by": report.verified_by,
            "hash_ok": report.hash_ok,
            "hash_mismatch": report.hash_mismatch,
        },
    )
    report.rebound = True
    return report


def mark_library_offline(db: Database, library_id: int, reason: str = "手动标记离线") -> None:
    """把 Library 标记为离线（PRD §9.1：离线时数据库、缩略图、AI 信息仍然可用）。"""
    store = LibraryStore(db)
    store.set_status(library_id, "OFFLINE")
    store.mark_volumes_offline(library_id)
    store.log_event("library_offline", library_id=library_id, payload={"reason": reason})


def library_availability(db: Database, library_id: int) -> tuple[bool, int, int]:
    """(根路径是否可达, 索引内 asset 数, 根下可 stat 到的 asset 数)。

    只做 stat，不读内容，因此可以在打开 UI 时调用。
    """
    store = LibraryStore(db)
    library = store.get(library_id)
    total = int(
        db.scalar("SELECT COUNT(*) FROM asset WHERE library_id = ?", (library_id,), default=0)
    )
    if not library.root_path_hint:
        return False, total, 0
    root = Path(library.root_path_hint)
    if not root.is_dir():
        return False, total, 0

    # 抽样 stat（避免打开界面时遍历十几万文件）
    rows = db.query(
        "SELECT relative_path FROM asset WHERE library_id = ? ORDER BY id LIMIT 50", (library_id,)
    )
    reachable = sum(
        1 for row in rows if asset_absolute_path(root, str(row["relative_path"])).is_file()
    )
    return True, total, reachable


__all__ = [
    "DEFAULT_SAMPLE",
    "VERIFY_HASH",
    "VERIFY_META",
    "VERIFY_PATH",
    "RebindReport",
    "library_availability",
    "mark_library_offline",
    "rebind_library",
]
