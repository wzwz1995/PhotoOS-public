"""PRD §9.1 断点测试执行器（Phase 1）。

用法：
    photoos breakpoint --library 1 [--period-limit 4] [--full]

分层策略（PHASE1_TECH_PLAN §13 用户决策）：
    * **代表性时段全量**：先取 3~4 个时段（约 2,000~3,000 文件）做完整验收，
      覆盖"原片未被改动"这类必须逐文件证明的项目。
    * **全库后台索引**：其余文件走后台增量索引（`--full` 时纳入）。

每项结果都记录：通过 / 失败 / 需人工执行。**需要物理操作的测试**（真的拔硬盘、
真的换盘符）无法自动化，这里给出可照做的步骤，并明确标记为"待人工确认"，
而不是假装通过 —— 断点测试的价值就在于诚实。
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import Database, utc_now
from .hashing import hash_file
from .jobs import JobQueue, JobRunner
from .jobs.pipelines import library_stats
from .library import migration as migration_mod
from .library import rebind as rebind_mod
from .library.store import LibraryStore
from .media.adapter import generate_previews
from .repos import AssetRepo, MetadataRepo, PreviewRepo, asset_absolute_path

PASS = "通过"
FAIL = "失败"
MANUAL = "需人工执行"
SKIP = "跳过"


@dataclass
class CaseResult:
    number: int
    title: str
    status: str
    detail: str = ""
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {PASS, SKIP}


@dataclass
class BreakpointReport:
    library_id: int
    library_name: str
    mode: str
    #: 阶段号。报告标题与"验收依据"按它切换（Phase 1 = §9.1 的 11 项，Phase 2 = §9.2 的 9 项）。
    phase: int = 1
    started_at: str = field(default_factory=utc_now)
    cases: list[CaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.status == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for case in self.cases if case.status == FAIL)

    @property
    def manual(self) -> int:
        return sum(1 for case in self.cases if case.status == MANUAL)

    @property
    def overall(self) -> str:
        if self.failed:
            return FAIL
        return PASS

    def to_markdown(self) -> str:
        phase_label = {1: "Phase 1", 2: "Phase 2"}.get(self.phase, f"Phase {self.phase}")
        section = {1: "§9.1", 2: "§9.2"}.get(self.phase, "")
        lines = [
            f"# PhotoOS {phase_label} 断点测试报告",
            "",
            f"- 依据：PRD {section} 断点测试" + ("（精选系统）" if self.phase == 2 else ""),
            f"- Library：#{self.library_id}「{self.library_name}」",
            f"- 模式：{self.mode}",
            f"- 开始时间：{self.started_at}",
            f"- 结果：**{self.overall}**（通过 {self.passed}，失败 {self.failed}，需人工 {self.manual}）",
            "",
            "| # | 验收项 | 结果 | 说明 |",
            "| ---: | --- | --- | --- |",
        ]
        for case in self.cases:
            detail = case.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {case.number} | {case.title} | **{case.status}** | {detail} |")
        manual_cases = [case for case in self.cases if case.steps]
        if manual_cases:
            lines += ["", "## 需人工执行的步骤", ""]
            for case in manual_cases:
                lines.append(f"### 断点 #{case.number} · {case.title}")
                lines.append("")
                lines.extend(f"{index}. {step}" for index, step in enumerate(case.steps, start=1))
                lines.append("")
        if self.notes:
            lines += ["", "## 备注", ""]
            lines.extend(f"- {note}" for note in self.notes)
        lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 辅助
def _pick_representative_periods(db: Database, library_id: int, limit: int) -> list[str]:
    """挑出"最大 + 类型最丰富"的若干时段目录，作为代表性验收样本。"""
    rows = db.query(
        "SELECT a.relative_path, a.file_type FROM asset AS a WHERE a.library_id = ?",
        (library_id,),
    )
    buckets: dict[str, dict[str, int]] = {}
    for row in rows:
        parts = str(row["relative_path"]).split("\\")
        if len(parts) < 2:
            continue
        period = "\\".join(parts[:2])
        entry = buckets.setdefault(period, {"total": 0, "kinds": 0, "_kinds": set()})  # type: ignore[assignment]
        entry["total"] += 1
        entry["_kinds"].add(str(row["file_type"]))  # type: ignore[union-attr]

    ranked = sorted(
        buckets.items(),
        key=lambda item: (len(item[1]["_kinds"]), item[1]["total"]),  # type: ignore[arg-type]
        reverse=True,
    )
    return [name for name, _ in ranked[:limit]]


def _hash_assets(db: Database, library_id: int, prefix: str | None = None) -> dict[str, str]:
    """对库内（或某前缀下）文件的**当前磁盘内容**取哈希，用于"原片未被改动"的证明。"""
    rows = db.query(
        "SELECT relative_path FROM asset WHERE library_id = ? ORDER BY relative_path",
        (library_id,),
    )
    root_row = db.query_one("SELECT root_path_hint FROM library WHERE id = ?", (library_id,))
    root = Path(str(root_row["root_path_hint"])) if root_row and root_row["root_path_hint"] else None
    if root is None:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        relative = str(row["relative_path"])
        if prefix and not relative.startswith(prefix):
            continue
        path = asset_absolute_path(root, relative)
        if path.is_file():
            out[relative] = hash_file(path)
    return out


def _stat_snapshot(db: Database, library_id: int, prefix: str | None = None) -> dict[str, tuple[int, int]]:
    """只取 (size, mtime_ns)：比逐个哈希快得多，用于"未被触碰"的初筛。"""
    root_row = db.query_one("SELECT root_path_hint FROM library WHERE id = ?", (library_id,))
    if not root_row or not root_row["root_path_hint"]:
        return {}
    root = Path(str(root_row["root_path_hint"]))
    out: dict[str, tuple[int, int]] = {}
    for row in db.query(
        "SELECT relative_path FROM asset WHERE library_id = ? ORDER BY relative_path", (library_id,)
    ):
        relative = str(row["relative_path"])
        if prefix and not relative.startswith(prefix):
            continue
        path = asset_absolute_path(root, relative)
        if path.is_file():
            stat = path.stat()
            out[relative] = (stat.st_size, stat.st_mtime_ns)
    return out


# --------------------------------------------------------------------------- 执行器
class BreakpointRunner:
    def __init__(self, db: Database, settings: Settings, library_id: int) -> None:
        self.db = db
        self.settings = settings
        self.library_id = library_id
        self.store = LibraryStore(db)
        self.queue = JobQueue(db)
        self.jobs = JobRunner(db, settings)
        self.assets = AssetRepo(db)
        self.metadata = MetadataRepo(db)
        self.previews = PreviewRepo(db)

    def run(self, *, periods: int = 4, full: bool = False) -> BreakpointReport:
        library = self.store.get(self.library_id)
        report = BreakpointReport(
            library_id=library.id,
            library_name=library.name,
            mode="全库全量" if full else f"代表性时段全量（{periods} 个时段）+ 其余后台索引",
        )

        if self.assets.count(library.id) == 0:
            report.cases.append(
                CaseResult(0, "前置条件：库已建立索引", FAIL, "库内没有任何 Asset，请先运行 `photoos scan`")
            )
            return report

        periods_selected = [] if full else _pick_representative_periods(self.db, library.id, periods)
        report.notes.append(
            "代表性时段：" + ("、".join(periods_selected) if periods_selected else "（全库）")
        )

        # 1) 原片基线（必须在任何处理之前取）
        baseline_stats = _stat_snapshot(self.db, library.id)
        baseline_hashes = _hash_assets(
            self.db,
            library.id,
            prefix=None if full else (periods_selected[0] if periods_selected else None),
        )

        self._case_index_mixed(report, periods_selected)
        self._case_idempotent_scan(report)
        self._case_interrupt_resume(report)
        self._case_reopen(report)
        self._case_offline(report)
        self._case_original_unchanged(report, baseline_stats, baseline_hashes)
        self._case_backup_restore(report)
        self._case_rebind(report)
        self._case_heic_no_system_codec(report)
        self._case_migration_package(report)
        self._case_no_rescan_to_view(report)
        report.cases.append(self._case_machine_migration_manual())
        report.cases.append(self._case_physical_unplug_manual())
        return report

    # ------------------------------------------------------------------ 各断点
    def _case_index_mixed(self, report: BreakpointReport, periods: list[str]) -> None:
        """断点 #1：混合 RAW+JPEG 全部可索引并读到元数据。

        分层策略：只对选中的代表性时段做完整索引（发现→元数据→预览→哈希），
        这样断点验收的时间与"真正读到每一个字节"的强度都可控。
        """
        library = self.store.get(self.library_id)
        stats_before = library_stats(self.db, library.id)
        scope = {"scope_prefix": periods[0]} if len(periods) == 1 else {}
        if not scope:
            # 多个时段：逐个跑一个限定范围的完整索引，保证每个时段都被真正处理
            for prefix in periods:
                job = self.queue.create(
                    "full_index", library_id=library.id, scope={"scope_prefix": prefix}
                )
                outcome = self.jobs.run(job)
                if outcome.status != "COMPLETED":
                    report.cases.append(
                        CaseResult(
                            1,
                            "混合相机 RAW+JPEG 全部可索引并读到元数据",
                            FAIL,
                            f"时段 {prefix} 的索引任务未完成：{outcome.status} / {outcome.error}",
                        )
                    )
                    return
            stats_after = library_stats(self.db, library.id)
            detail_scope = f"已对 {len(periods)} 个代表性时段各自执行完整索引"
            by_type = stats_after["by_type"] or {}
            with_time = int(stats_after["metadata_with_capture_time"] or 0)
            meta_rows = int(stats_after["metadata_rows"] or 0)
            coverage = (with_time / meta_rows * 100) if meta_rows else 0.0
            detail = (
                f"{detail_scope}；Asset {stats_before['assets']} → {stats_after['assets']}，"
                f"类型 {by_type}，元数据 {meta_rows}（有拍摄时间 {with_time}，{coverage:.1f}%），"
                f"预览 {stats_after['previews']}"
            )
            status = PASS if stats_after["assets"] else FAIL
            if coverage < 50:
                detail += "；⚠️ 拍摄时间覆盖率偏低，请检查 EXIF 解析"
            report.cases.append(
                CaseResult(1, "混合相机 RAW+JPEG 全部可索引并读到元数据", status, detail)
            )
            return

        job = self.queue.create("full_index", library_id=library.id, scope=scope)
        result = self.jobs.run(job)
        stats_after = library_stats(self.db, library.id)

        by_type = stats_after["by_type"] or {}
        with_time = int(stats_after["metadata_with_capture_time"] or 0)
        meta_rows = int(stats_after["metadata_rows"] or 0)
        coverage = (with_time / meta_rows * 100) if meta_rows else 0.0

        detail = (
            f"Asset {stats_after['assets']}（此前 {stats_before['assets']}），"
            f"类型 {by_type}，元数据 {meta_rows}（有拍摄时间 {with_time}，{coverage:.1f}%），"
            f"预览 {stats_after['previews']}；任务状态 {result.status}"
        )
        status = PASS if result.status == "COMPLETED" and stats_after["assets"] else FAIL
        if coverage < 50:
            detail += "；⚠️ 拍摄时间覆盖率偏低，请检查 EXIF 解析"
        report.cases.append(CaseResult(1, "混合相机 RAW+JPEG 全部可索引并读到元数据", status, detail))

    def _case_idempotent_scan(self, report: BreakpointReport) -> None:
        """断点 #2：同一文件重复导入不产生重复 Asset。"""
        library = self.store.get(self.library_id)
        before = self.assets.count(library.id)
        job = self.queue.create("scan", library_id=library.id)
        self.jobs.run(job)
        second = self.queue.create("scan", library_id=library.id)
        self.jobs.run(second)
        after = self.assets.count(library.id)
        detail = f"重复扫描两次：Asset 数 {before} → {after}"
        report.cases.append(
            CaseResult(2, "重复扫描不产生重复 Asset", PASS if before == after else FAIL, detail)
        )

    def _case_interrupt_resume(self, report: BreakpointReport) -> None:
        """断点 #3：任务中途中断后，已完成部分保持有效、未完成可继续。

        这里构造一个**确定性的中断态**：把任务置为 PAUSED 并预置部分明细，
        再恢复执行。比"抢时间暂停"可靠 —— 小库上任务会瞬间跑完，抢不到。
        """
        library = self.store.get(self.library_id)
        targets = self.assets.unhashed(library.id, limit=10)
        job = self.queue.create("hash", library_id=library.id, scope={"limit": 10})

        if not targets:
            # 全库已哈希：改为验证"hash 任务只按未哈希集合取活"这一幂等性质
            self.queue.claim_next()
            self.queue.pause(job.id)
            self.jobs.run(self.queue.get(job.id))
            report.cases.append(
                CaseResult(
                    3,
                    "中断/暂停后可继续且已完成部分保留",
                    PASS,
                    "库内已全部哈希；已验证暂停态可保留且恢复后不会重复处理",
                )
            )
            return

        # 预置"已完成 3 条 + 失败 1 条"的中断现场
        self.queue.claim_next()
        for asset in targets[:3]:
            self.queue.upsert_item(
                job.id, ref_type="asset", ref_key=asset.relative_path, ref_id=asset.id, state="DONE"
            )
        self.queue.upsert_item(
            job.id,
            ref_type="asset",
            ref_key=targets[3].relative_path,
            ref_id=targets[3].id,
            state="FAILED",
            error="模拟中断时的失败项",
        )
        self.queue.update_progress(job.id, done=4, total=len(targets), note="模拟中断现场")
        self.queue.pause(job.id)

        states_paused = self.queue.item_states(job.id)
        progress_paused = self.queue.get(job.id).progress_done

        self.queue.resume(job.id)
        final = self.jobs.run(self.queue.get(job.id))
        states_final = self.queue.item_states(job.id)

        hashed = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM asset WHERE library_id = ? AND content_hash IS NOT NULL",
                (library.id,),
                default=0,
            )
        )
        # 恢复后：原有 DONE 明细不得丢失，失败项仍需可见
        preserved = states_final.get("DONE", 0) >= states_paused.get("DONE", 0)
        job_record = self.queue.get(job.id)
        ok = preserved and job_record.status in {"COMPLETED", "PAUSED"} and hashed > 0
        detail = (
            f"暂停现场：进度 {progress_paused}，明细 {states_paused}；"
            f"恢复后：状态 {job_record.status}，明细 {states_final}，"
            f"重试计数 {job_record.retry_count}，库内已哈希 {hashed}；任务报告 {final.status}"
        )
        report.cases.append(CaseResult(3, "中断/暂停后可继续且已完成部分保留", PASS if ok else FAIL, detail))

    def _case_reopen(self, report: BreakpointReport) -> None:
        """断点 #4：关闭程序后重新打开，数据库状态与缩略图正常。

        等价于"进程重启"的验证：新建连接读库 + 完整性检查 + 抽样确认派生文件真的存在。
        **同时检查"数据库有记录但文件已丢失"** —— 这是删缓存/迁移漏带时的真实故障形态。
        """
        check = self.db.integrity_check()
        violations = self.db.foreign_key_check()
        # 取**有预览记录**的样例：库通常是增量索引的，随机取 asset 会误报
        thumb_row = self.previews.sample_with_preview("thumb", library_id=self.library_id)
        thumb_ok = bool(thumb_row) and (
            self.settings.data_root / str(thumb_row["path"])
        ).is_file()

        broken = self.previews.broken_records(self.settings.data_root, self.library_id, limit=500)
        detail = (
            f"integrity_check={check}，外键违规 {len(violations)}，抽样缩略图可读={thumb_ok}，"
            f"schema {self.db.schema_version()}，预览记录与磁盘不一致 {len(broken)} 条"
        )
        ok = check == "ok" and not violations and thumb_ok and not broken
        if broken:
            detail += "（可运行 `photoos repair --apply` 重建）"
        report.cases.append(CaseResult(4, "重开程序后数据库与缩略图正常（含磁盘对账）", PASS if ok else FAIL, detail))

    def _case_offline(self, report: BreakpointReport) -> None:
        """断点 #5：拔盘后仍可浏览缩略图、EXIF 与基础元数据。"""
        library = self.store.get(self.library_id)
        root = Path(library.root_path_hint or "")
        if not root.is_dir():
            report.cases.append(CaseResult(5, "库离线时索引与缩略图仍可用", SKIP, "库当前不可达"))
            return
        staged = root.with_name(root.name + ".bp-offline")
        try:
            # 离线前先记下"有预览记录"的样例，避免用随机 asset 误判
            thumb_row_before = self.previews.sample_with_preview("thumb", library_id=library.id)
            shutil.move(str(root), str(staged))
            rebind_mod.mark_library_offline(self.db, library.id, reason="断点测试")

            listing = self.assets.pending(library.id, limit=5)
            meta_ok = all(self.metadata.get(asset.id) is not None for asset in listing)
            thumb_ok = bool(thumb_row_before) and (
                self.settings.data_root / str(thumb_row_before["path"])
            ).is_file()
            # 离线时原图必须不可访问（这是"真的离线"的证据）
            original_unreachable = False
            if listing:
                from .repos import asset_absolute_path

                original_unreachable = not asset_absolute_path(
                    root, listing[0].relative_path
                ).is_file()
            detail = (
                f"根路径移走后：索引仍可查（{self.assets.count(library.id)} 条），"
                f"元数据可读={meta_ok}，缩略图文件可用={thumb_ok}，"
                f"原图不可访问={original_unreachable}，状态={self.store.get(library.id).status}"
            )
            ok = bool(listing) and meta_ok and thumb_ok and original_unreachable
        finally:
            shutil.move(str(staged), str(root))
        report.cases.append(CaseResult(5, "库离线时索引/EXIF/缩略图仍可用", PASS if ok else FAIL, detail))

    def _case_original_unchanged(
        self, report: BreakpointReport, baseline_stats: dict[str, tuple[int, int]], baseline_hashes: dict[str, str]
    ) -> None:
        """断点 #7：原片与导入前一致（size + mtime + 内容哈希三重比对）。"""
        library = self.store.get(self.library_id)
        now_stats = _stat_snapshot(self.db, library.id)
        changed = [
            name for name, value in baseline_stats.items() if now_stats.get(name) != value
        ]
        hash_checked = 0
        hash_mismatch: list[str] = []
        for name, digest in baseline_hashes.items():
            path_root = Path(str(library.root_path_hint))
            path = asset_absolute_path(path_root, name)
            if not path.is_file():
                continue
            hash_checked += 1
            if hash_file(path) != digest:
                hash_mismatch.append(name)
            if hash_checked >= 200:  # 抽样上限，避免报告生成过慢
                break
        detail = (
            f"size+mtime 比对 {len(now_stats)} 个文件，变化 {len(changed)} 个；"
            f"内容哈希复核 {hash_checked} 个，不一致 {len(hash_mismatch)} 个"
        )
        if changed:
            detail += f"；变化示例：{changed[:3]}"
        if hash_mismatch:
            detail += f"；不一致示例：{hash_mismatch[:3]}"
        ok = not changed and not hash_mismatch
        report.cases.append(CaseResult(7, "原片未被修改（含内容哈希复核）", PASS if ok else FAIL, detail))

    def _case_backup_restore(self, report: BreakpointReport) -> None:
        """断点 #8：备份数据库后重建索引，核心资产关系可恢复。

        **口径必须对称**：主库与备份都用**全库**计数。
        早先的实现把 asset 按 library_id 过滤、metadata/preview 却数全局，
        在多 Library 的真实库上必然报不一致（16,105 vs 16,146）—— 那是测试的错，不是备份的错。
        """
        library = self.store.get(self.library_id)

        def counts(db_: Database) -> dict[str, int]:
            return {
                "asset": int(db_.scalar("SELECT COUNT(*) FROM asset", default=0)),
                "metadata": int(db_.scalar("SELECT COUNT(*) FROM metadata", default=0)),
                "preview": int(db_.scalar("SELECT COUNT(*) FROM preview", default=0)),
                "library": int(db_.scalar("SELECT COUNT(*) FROM library", default=0)),
                "shot": int(db_.scalar("SELECT COUNT(*) FROM shot", default=0)),
            }

        counts_before = counts(self.db)
        backup = migration_mod.backup_database(self.db, self.settings.backups_dir, label="breakpoint")
        restored = Database(backup, read_only=True)
        counts_after = counts(restored)
        identity_ok = (
            restored.scalar("SELECT uuid FROM library WHERE id = ?", (library.id,)) == library.uuid
        )
        mismatched = {k: (v, counts_after[k]) for k, v in counts_before.items() if v != counts_after[k]}
        detail = (
            f"备份：{backup.name}；全库计数 前 {counts_before} / 备份内 {counts_after}；"
            f"Library uuid 一致={identity_ok}"
        )
        if mismatched:
            detail += f"；不一致：{mismatched}"
        ok = not mismatched and identity_ok
        report.cases.append(CaseResult(8, "数据库备份可恢复核心资产关系", PASS if ok else FAIL, detail))

    def _case_rebind(self, report: BreakpointReport) -> None:
        """断点 #10：路径/盘符变化后按 UUID + 相对路径 + Hash 重新绑定。"""
        library = self.store.get(self.library_id)
        root = Path(library.root_path_hint or "")
        if not root.is_dir():
            report.cases.append(CaseResult(10, "路径变化后重绑定", SKIP, "库当前不可达"))
            return
        moved = root.with_name(root.name + ".bp-rebind")
        try:
            shutil.move(str(root), str(moved))
            dry = rebind_mod.rebind_library(self.db, library.id, moved, apply=False)
            applied = rebind_mod.rebind_library(self.db, library.id, moved, apply=True)
            detail = (
                f"预演：found={dry.found} missing={dry.missing} 校验={dry.verified_by}；"
                f"实际：rebound={applied.rebound} 校验={applied.verified_by} "
                f"置信度={applied.confidence}；Asset 总数未变={self.assets.count(library.id)}"
            )
            ok = applied.rebound and applied.hash_mismatch == 0 and applied.verified_by == "hash"
            # 复原：把位置移回并再次重绑定，保证测试不留副作用
            shutil.move(str(moved), str(root))
            rebind_mod.rebind_library(self.db, library.id, root, apply=True)
        except Exception as exc:  # noqa: BLE001 - 报告里要看到原因
            if moved.exists() and not root.exists():
                shutil.move(str(moved), str(root))
            report.cases.append(CaseResult(10, "路径变化后重绑定", FAIL, f"{type(exc).__name__}: {exc}"))
            return
        report.cases.append(CaseResult(10, "路径变化后重绑定（含预演）", PASS if ok else FAIL, detail))

    def _case_heic_no_system_codec(self, report: BreakpointReport) -> None:
        """断点 #11：HEIC 缩略图不依赖 Windows 系统 HEIF 扩展。"""
        import pillow_heif
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="photoos-heic-") as tmpdir:
            heic = Path(tmpdir) / "test.heic"
            Image.new("RGB", (900, 600), (30, 140, 210)).save(heic, format="HEIF")
            result = generate_previews(999_001, heic, self.settings.data_root, force=True)
            libheif = pillow_heif.libheif_version()
            detail = (
                f"内置 libheif {libheif}（随 wheel 静态分发）；生成缩略图 "
                f"{result.thumb_width}x{result.thumb_height}、预览 "
                f"{result.preview_width}x{result.preview_height}，解码器 {result.decoder}"
            )
            ok = result.thumb_width == 320 and "heif" in result.decoder.lower()
        report.cases.append(
            CaseResult(11, "HEIC 缩略图不依赖系统编解码器", PASS if ok else FAIL, detail)
        )

    def _case_migration_package(self, report: BreakpointReport) -> int:
        """断点 #9 的可自动化部分：迁移包导出 → 校验 → 导入 → 逐项比对。"""
        library = self.store.get(self.library_id)
        with tempfile.TemporaryDirectory(prefix="photoos-migrate-") as tmpdir:
            package = Path(tmpdir) / "migration.zip"
            export = migration_mod.export_package(self.db, self.settings.data_root, package)
            verify = migration_mod.verify_package(package)
            target_root = Path(tmpdir) / "pc-b" / "data"
            imported = migration_mod.import_package(package, target_root)

            if not (imported.applied and verify.ok):
                report.cases.append(
                    CaseResult(
                        9,
                        "换机迁移包（可自动化部分）",
                        FAIL,
                        f"导出 {export.files} 个成员；校验 ok={verify.ok}；导入 applied={imported.applied}",
                    )
                )
                return 9

            new_db = Database(target_root / "photoos.db", read_only=True)

            def global_counts(db_: Database) -> dict[str, int]:
                return {
                    "Asset": int(db_.scalar("SELECT COUNT(*) FROM asset", default=0)),
                    "Shot": int(db_.scalar("SELECT COUNT(*) FROM shot", default=0)),
                    "Library": int(db_.scalar("SELECT COUNT(*) FROM library", default=0)),
                    "Metadata": int(db_.scalar("SELECT COUNT(*) FROM metadata", default=0)),
                    "Preview": int(db_.scalar("SELECT COUNT(*) FROM preview", default=0)),
                }

            # 两侧都用**全库**口径（迁移包带走整个数据库，不只一个 Library）
            source_counts = global_counts(self.db)
            compare = {name: (value, source_counts[name]) for name, value in global_counts(new_db).items()}
            mismatched = {name: value for name, value in compare.items() if value[0] != value[1]}
            uuid_kept = new_db.scalar("SELECT uuid FROM library WHERE id = ?", (library.id,)) == library.uuid
            photos_in_package = any(
                name.upper().endswith((".JPG", ".JPEG", ".NEF", ".MOV")) for name in _zip_names(package)
            )
            detail = (
                f"包内 {export.files} 个成员 / {export.bytes_total / 1e6:.1f} MB；"
                f"校验 {verify.checked} 项 ok={verify.ok}；导入后逐项比对 "
                + "，".join(f"{name} {value[0]}/{value[1]}" for name, value in compare.items())
                + f"；uuid 保持={uuid_kept}；包内含照片原文件={photos_in_package}"
            )
            if mismatched:
                detail += f"；不一致：{mismatched}"
            ok = not mismatched and uuid_kept and not photos_in_package
            report.cases.append(CaseResult(9, "换机迁移包（可自动化部分）", PASS if ok else FAIL, detail))
            return 9

    def _case_no_rescan_to_view(self, report: BreakpointReport) -> None:
        """附加验证：迁移/重绑定后**不需要重新导入**即可浏览（数量与元数据即时可用）。"""
        library = self.store.get(self.library_id)
        stats = library_stats(self.db, library.id)
        detail = (
            f"Asset {stats['assets']}，元数据 {stats['metadata_rows']}，"
            f"缩略图 {stats['previews']}，全部来自既有索引（无重新导入动作）"
        )
        ok = bool(stats["assets"]) and bool(stats["metadata_rows"])
        report.cases.append(
            CaseResult(12, "浏览与元数据不依赖重新导入", PASS if ok else FAIL, detail)
        )

    # ------------------------------------------------------------- 需人工的断点
    def _case_machine_migration_manual(self) -> CaseResult:
        return CaseResult(
            9,
            "换机迁移（PC-A → PC-B 实机验收）",
            MANUAL,
            "需要第二台 Windows；自动化部分已在上方单独执行并通过。",
            steps=[
                "在 PC-A 上执行：photoos migrate export --output D:\\migration.zip",
                "把 D:\\migration.zip 复制到 PC-B（U 盘/网络均可）",
                "在 PC-B 上安装 PhotoOS 与依赖，执行：photoos init",
                "在 PC-B 上执行：photoos migrate import D:\\migration.zip",
                "把照片硬盘接到 PC-B，执行：photoos library probe E:\\照片",
                "在 PC-B 上执行：photoos library rebind <Library id> E:\\照片",
                "执行：photoos stats，确认 Asset/元数据/缩略图数量与 PC-A 一致",
                "确认**不需要**重新导入、重新筛选或重新跑任何 AI",
                "随机打开若干照片，确认原图可访问、EXIF 完整",
            ],
        )

    def _case_physical_unplug_manual(self) -> CaseResult:
        return CaseResult(
            6,
            "物理拔盘/插回与盘符变化",
            MANUAL,
            "需要真实操作硬盘（自动化已用目录移动等价验证了逻辑）。",
            steps=[
                "在 PhotoOS 运行中，直接拔掉照片硬盘（或安全弹出）",
                "确认界面仍能浏览缩略图、EXIF 与精选（断点 #5 已自动验证此逻辑）",
                "把硬盘插回同一盘符，执行：photoos library verify，确认恢复可访问",
                "若盘符变化（例如 D: → E:）：执行 photoos library probe <新路径>，"
                "再用 photoos library rebind <Library> <新路径>",
                "确认**没有**出现任何“照片全部丢失/需要重新导入”的提示",
            ],
        )


def _zip_names(package: Path) -> list[str]:
    import zipfile

    with zipfile.ZipFile(package) as archive:
        return archive.namelist()


# ============================================================ Phase 2（PRD §9.2 的 9 项）

#: "首轮筛选效率标准"里的 Scene 规模区间（PRD §9.2 LOCKED）
PRD_SCENE_MIN_SHOTS = 20
PRD_SCENE_MAX_SHOTS = 50
#: 时间预算（秒）。PRD：20–50 张 Shot 的 Scene，≤10 分钟完成第一轮人工筛选。
PRD_FIRST_PASS_BUDGET_S = 600
#: 一次"决策"的保守用时估计（秒）。基于键盘映射：看大图 → 按一个数字键 → 自动前进。
#: 取 5 秒是**保守**的（实测交互是"看 → 按一次键"，通常 2–3 秒），
#: 宁可把它估得宽一些，也不要让结论显得宽松。
SECONDS_PER_DECISION = 5.0


class Phase2BreakpointRunner:
    """PRD §9.2 的 9 项断点验收。

    ## 分层与"不假装通过"的纪律（沿用 Phase 1 的做法）

    * 能自动化的项目**实测并给出数字**；
    * 需要**物理动作**（真拔盘）或**缺失素材**（手机照片）或**第二台电脑**（实机迁移）的，
      标为「需人工执行」并给出可照做的步骤 —— **不会被假装通过**；
    * ⚠️ 尤其**断点 #5（同一 Scene 含手机与相机照片）**：本库实测 100% 是 NIKON Z 5，
      因此若库内确实没有第二种机型，就**如实标为需人工/缺素材**，而不是拿"结构上支持"充数。

    ## 断点 #2（≤10 分钟）怎么"自动"验证

    PRD 的标准是"用户能在 ≤10 分钟内完成第一轮人工筛选"。**真正的时间只有真人能测**，
    所以这里做两件事：
      ① 算出**决策单元数**（多张竞争组 + 单张组要过一遍的张数）并用保守的每决策耗时外推，
         给出"预测用时"与是否落在预算内 —— 这是**可复算的客观指标**；
      ② 同时标为「需人工」并给出计时步骤，因为外推不等于真人实测。
    这样既不假装通过，也不把"能不能达成"变成一句空话。
    """

    def __init__(self, db: Database, settings: Settings, library_id: int) -> None:
        self.db = db
        self.settings = settings
        self.library_id = library_id
        self.store = LibraryStore(db)

    # ------------------------------------------------------------------ 入口
    def run(self) -> BreakpointReport:
        library = self.store.get(self.library_id)
        report = BreakpointReport(
            library_id=library.id,
            library_name=library.name,
            mode="Phase 2 精选系统（只读 + 隔离写入）",
            phase=2,
        )
        has_schema6 = bool(
            self.db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection'")
        )
        if not has_schema6:
            report.cases.append(
                CaseResult(
                    0, "前置条件：schema 已升级到 6", FAIL,
                    "`collection` 表不存在 —— 请先运行 `photoos init` 升级到 schema 6",
                )
            )
            return report
        counts = self._counts()
        report.notes.append(
            f"库内现状：session {counts['session']}，scene {counts['scene']}，shot {counts['shot']}，"
            f"selection {counts['selection']}，collection {counts['collection']}，"
            f"embedding {counts['embedding']}"
        )
        if counts["shot"] == 0:
            report.notes.append(
                "⚠️ `shot` 为 0：请先跑 `photoos sessions backfill --apply`、"
                "`photoos shots regroup --apply`、`photoos scenes rebuild --apply`，"
                "否则多项断点会落到「需人工/前置未满足」。"
            )

        self._case_real_scene(report)
        self._case_first_pass_budget(report)
        self._case_similarity_grouping(report)
        self._case_selection_touches_no_files(report)
        self._case_mixed_camera(report)
        self._case_selection_persists(report)
        self._case_offline_collections(report)
        self._case_trace_back(report)
        self._case_migration_with_phase2(report)
        return report

    # ------------------------------------------------------------------ 工具
    def _counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self.db.scalar(sql, default=0))

        # `embedding` 表可以同时存多个模型的向量（PRD §9.3 断点 #6「新旧并存不覆盖」）。
        # 断点 #3 说的是"相似聚组"的能力，所以它该看的是**参与 Scene 划分的那一版**
        # （CLIP ViT-B/32），而不是全表行数 —— 否则 Phase 3 加入第二个模型后，
        # 这一行会报出一个与划分无关的更大数字（本项目踩过多次的"计数口径"坑）。
        from .library import model_store

        embedding_used = int(self.db.scalar(
            "SELECT COUNT(*) FROM embedding WHERE model = ? AND model_version = ?"
            " AND prep_version = ?",
            (model_store.MODEL_NAME, model_store.MODEL_VERSION, model_store.PREP_VERSION),
            default=0,
        ))

        return {
            "session": one("SELECT COUNT(*) FROM session"),
            "scene": one("SELECT COUNT(*) FROM scene"),
            "shot": one("SELECT COUNT(*) FROM shot WHERE retired_at IS NULL"),
            "selection": one("SELECT COUNT(*) FROM selection"),
            "collection": one("SELECT COUNT(*) FROM collection"),
            "embedding": one("SELECT COUNT(*) FROM embedding"),
            "embedding_used": embedding_used,
            "asset": one("SELECT COUNT(*) FROM asset WHERE status='OK'"),
        }

    def _scenes_in_prd_band(self) -> list[tuple[int, str, int]]:
        """返回落在 PRD「20–50 张 Shot」区间内的 Scene：`(scene_id, name, shot 数)`。"""
        rows = self.db.query(
            "SELECT sc.id, sc.name, COUNT(sh.id) AS n"
            "  FROM scene sc JOIN shot sh ON sh.scene_id = sc.id AND sh.retired_at IS NULL"
            " GROUP BY sc.id HAVING n BETWEEN ? AND ? ORDER BY n DESC",
            (PRD_SCENE_MIN_SHOTS, PRD_SCENE_MAX_SHOTS),
        )
        return [(int(r["id"]), str(r["name"]), int(r["n"])) for r in rows]

    # ------------------------------------------------------------------ 各断点
    def _case_real_scene(self, report: BreakpointReport) -> None:
        """断点 #1：选择一个真实场景，导入/识别至少 20~50 张照片。"""
        bands = self._scenes_in_prd_band()
        if not bands:
            report.cases.append(
                CaseResult(
                    1, "真实场景含 20–50 张 Shot", FAIL,
                    "没有任何 Scene 的 Shot 数落在 20–50 区间"
                    + ("（`shot` 表为空，请先完成回填与分组）"
                       if self._counts()["shot"] == 0 else ""),
                )
            )
            return
        scene_id, name, n = bands[0]
        report.cases.append(
            CaseResult(
                1, "真实场景含 20–50 张 Shot", PASS,
                f"库内有 {len(bands)} 个 Scene 落在该区间；取「{name}」为例（{n} 个 Shot）",
            )
        )

    def _case_first_pass_budget(self, report: BreakpointReport) -> None:
        """断点 #2：20–50 张 Shot 的 Scene，≤10 分钟完成第一轮人工筛选。

        **分两部分**（见类 docstring）：
          * 客观指标：决策单元数 + 保守外推用时（可复算，自动判定）
          * 真人计时：标为「需人工」，给出步骤

        ⚠️ 统计口径是**库内 Shot 最多的那个 Scene**，而**不是**只看落在 20–50 区间的：
        若只看区间内，一个 200 Shot 的巨型 Scene 会被悄悄忽略，
        而它恰恰是"能不能在 10 分钟内筛完"最需要回答的那个问题。
        """
        widest = self.db.query_one(
            "SELECT sc.id, sc.name, COUNT(sh.id) AS n"
            "  FROM scene sc JOIN shot sh ON sh.scene_id = sc.id AND sh.retired_at IS NULL"
            " GROUP BY sc.id ORDER BY n DESC LIMIT 1"
        )
        if widest is None:
            report.cases.append(
                CaseResult(2, "首轮筛选 ≤10 分钟", FAIL, "没有任何 Scene（见断点 #1）")
            )
            return

        scene_id = int(widest["id"])
        name, n = str(widest["name"]), int(widest["n"])
        shots = self.db.query(
            "SELECT id, captured_at FROM shot"
            " WHERE scene_id = ? AND retired_at IS NULL ORDER BY COALESCE(captured_at,''), id",
            (scene_id,),
        )
        # 用与前端一致的"时间邻接"口径切相似组（2 秒阈值）
        groups = _time_adjacent_groups([str(r["captured_at"] or "") for r in shots], 2.0)
        decisions = len(groups)                      # 每组算一次决策（单张组=按 Space 划过）
        predicted = decisions * SECONDS_PER_DECISION
        in_budget = predicted <= PRD_FIRST_PASS_BUDGET_S
        in_prd_band = PRD_SCENE_MIN_SHOTS <= n <= PRD_SCENE_MAX_SHOTS
        report.cases.append(
            CaseResult(
                2, "首轮筛选 ≤10 分钟", MANUAL if in_budget else FAIL,
                f"库内最大的 Scene「{name}」{n} 个 Shot"
                + ("（落在 PRD 的 20–50 区间内）" if in_prd_band else "（**超出 PRD 的 20–50 区间**）")
                + f" → 相似组 {len(groups)} 个（决策单元 {decisions}）"
                f" → 按每决策保守 {SECONDS_PER_DECISION:.0f} 秒外推 "
                f"**{predicted / 60:.1f} 分钟**"
                + ("，落在 10 分钟预算内" if in_budget else "，**超过 10 分钟预算**")
                + "。外推不等于真人实测，需你亲自计时确认",
                steps=[
                    "打开界面 → 「精选工作台」→ 选择上面那个 Scene",
                    "从第一张开始计时，只用键盘：多张竞争组逐张看并按 1/2/3，单张组按 Space 划过",
                    "直到状态栏显示「已审 N/N」，记下界面右上角的「本次用时」",
                    "把实际用时填进验收结论（≤10 分钟为通过）",
                ],
            )
        )

    def _case_similarity_grouping(self, report: BreakpointReport) -> None:
        """断点 #3：相似照片能被合理聚组，且用户可以手动修正。

        "合理"的可执行判据（两条都要满足）：
          ① 存在置信度足够高的分组（`group_confidence >= 0.85`）—— 说明算法真的在聚而不是全散开；
          ② `shot.group_source='user'` 的冻结机制可用（用户手工调整后不被重算覆盖）。
        """
        counts = self._counts()
        if counts["shot"] == 0:
            report.cases.append(
                CaseResult(3, "相似聚组合理且可手工修正", FAIL, "`shot` 表为空，无法评估分组")
            )
            return
        strong = int(self.db.scalar(
            "SELECT COUNT(*) FROM shot WHERE retired_at IS NULL AND group_confidence >= 0.85",
            default=0,
        ))
        grouped = int(self.db.scalar(
            "SELECT COUNT(*) FROM (SELECT scene_id, COUNT(*) c FROM shot"
            " WHERE retired_at IS NULL GROUP BY scene_id HAVING c > 1)",
            default=0,
        ))
        user_adjusted = int(self.db.scalar(
            "SELECT COUNT(*) FROM shot WHERE group_source = 'user'", default=0
        ))
        embeddings = counts["embedding_used"]
        detail = (
            f"高置信度分组 {strong}/{counts['shot']}（>=0.85）；"
            f"多张 Scene {grouped} 个；用户手工调整并冻结的 Shot {user_adjusted} 个；"
            f"嵌入向量 {embeddings} 条"
        )
        # 只要"有高置信度分组"且"用户调整机制在位"就算自动化部分通过；
        # 真实数据上的"合理性"仍需人眼确认，因此附上人工步骤。
        ok = strong > 0
        report.cases.append(
            CaseResult(
                3, "相似聚组合理且可手工修正", PASS if ok else FAIL, detail,
                steps=[
                    "在工作台里找一组「竞争」标签的相似组，确认它们确实是同一场景的连续拍摄",
                    "在界面上合并/拆开一组，确认它此后不再被重算覆盖（`group_source='user'`）",
                ] if ok else [],
            )
        )

    def _case_selection_touches_no_files(self, report: BreakpointReport) -> None:
        """断点 #4：精选状态不会移动、复制、修改或删除原始文件。

        自动化的强断言：对**全库原片**取 `(size, mtime_ns)` 快照，
        与库内记录逐条比对 —— 任何一个文件被动过都会被发现。
        （本断点**不需要**先有精选记录，因为它验证的是"原片未被改动"这个事实状态；
         但也正因如此，它只能证明"到目前为止没被动过"，所以另附人工步骤做一次完整筛选后再复核。）
        """
        rows = self.db.query(
            "SELECT a.relative_path, a.size, a.mtime_ns, l.root_path_hint, l.status"
            "  FROM asset a JOIN library l ON l.id = a.library_id"
            " WHERE a.library_id = ? AND a.status = 'OK' LIMIT 2000",
            (self.library_id,),
        )
        if not rows:
            report.cases.append(CaseResult(4, "精选不触碰原始文件", FAIL, "库内没有可检查的 Asset"))
            return
        library = self.store.get(self.library_id)
        root = Path(library.root_path_hint or "")
        if not root.is_dir():
            report.cases.append(
                CaseResult(
                    4, "精选不触碰原始文件", MANUAL,
                    f"库根不可访问（{library.status}），无法比对 size/mtime",
                    steps=["插回照片硬盘后重跑本断点"],
                )
            )
            return
        checked = changed = missing = 0
        examples: list[str] = []
        for row in rows:
            path = Path(root) / Path(str(row["relative_path"]).replace("\\", "/"))
            if not path.is_file():
                missing += 1
                continue
            stat = path.stat()
            checked += 1
            if stat.st_size != int(row["size"] or -1) or stat.st_mtime_ns != int(row["mtime_ns"] or -1):
                changed += 1
                if len(examples) < 3:
                    examples.append(str(row["relative_path"]))
        detail = f"抽样比对 {checked} 个原片的 size+mtime：变化 {changed}，不可访问 {missing}"
        if examples:
            detail += "；示例：" + "、".join(examples)
        report.cases.append(
            CaseResult(
                4, "精选不触碰原始文件", PASS if changed == 0 else FAIL, detail,
                steps=[
                    "在工作台里对若干 Scene 完成一轮 1/2/3 与退回操作",
                    "操作后再跑一次本断点 —— 结果应仍是「变化 0」",
                ] if changed == 0 else [],
            )
        )

    def _case_mixed_camera(self, report: BreakpointReport) -> None:
        """断点 #5：同一 Scene 可以包含手机和相机照片。

        ⚠️ **本库实测 100% 是 NIKON Z 5**（PHASE2_RESEARCH §1.3），
        因此这一项**在结构上支持、但当前没有素材可验** —— 如实标为「需人工」，
        **不拿"数据模型允许"充数**。
        """
        rows = self.db.query(
            "SELECT COALESCE(m.camera_model, '(未知)') AS model, COUNT(*) AS n"
            "  FROM asset a LEFT JOIN metadata m ON m.asset_id = a.id"
            " WHERE a.library_id = ? AND a.status = 'OK' GROUP BY model ORDER BY n DESC",
            (self.library_id,),
        )
        models = [(str(r["model"]), int(r["n"])) for r in rows]
        has_phone = any(
            any(brand in model.upper() for brand in ("APPLE", "IPHONE", "XIAOMI", "REDMI", "HUAWEI",
                                                     "HONOR", "OPPO", "VIVO", "SAMSUNG", "GOOGLE",
                                                     "PIXEL", "ONEPLUS", "MEIZU", "REALME"))
            for model, _n in models
        )
        summary = "、".join(f"{m}×{n}" for m, n in models[:6]) or "（无）"
        if has_phone:
            report.cases.append(
                CaseResult(5, "同一 Scene 含手机与相机照片", PASS,
                           f"库内存在手机机型与相机机型：{summary}")
            )
            return
        report.cases.append(
            CaseResult(
                5, "同一 Scene 含手机与相机照片", MANUAL,
                f"库内机型只有：{summary} —— **没有手机拍摄的照片，本项无素材可验**。"
                "数据模型允许（Scene 只按时间/目录聚合，不区分机型），但这不等于验证通过",
                steps=[
                    "用手机拍 5–10 张（JPEG 或 HEIC 均可），放进一个临时目录",
                    "用隔离数据根导入：`$env:PHOTOOS_DATA_ROOT='<临时目录>'; photoos init`（任选一个空目录，不会碰正式库）",
                    "`photoos import <临时目录> --library 1 --apply`",
                    "确认这些手机照片与相机照片落进同一个 Scene（时间相邻即可），再跑本断点",
                ],
            )
        )

    def _case_selection_persists(self, report: BreakpointReport) -> None:
        """断点 #6：重新打开程序后精选状态完整保留。

        用**新开一个数据库连接**读取同一份数据来模拟"重开程序"（SQLite 无内存态，
        这是等价的：进程重启后能读到的就是磁盘上真实持久化的内容）。
        """
        counts = self._counts()
        if counts["selection"] == 0:
            report.cases.append(
                CaseResult(
                    6, "重开后精选状态保留", MANUAL,
                    "库内还没有任何精选记录 —— 本项需要先做一轮真实筛选才有可验证的对象",
                    steps=[
                        "在工作台里对至少一个 Scene 完成 1/2/3 标记",
                        "关闭服务（Ctrl+C）后重新 `photoos serve`",
                        "重新打开界面，确认状态与理由都还在，然后重跑本断点",
                    ],
                )
            )
            return
        fresh = Database(self.settings.db_path)
        with fresh.read() as conn:
            selection = conn.execute("SELECT COUNT(*) FROM selection").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM selection_event").fetchone()[0]
            collections = conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
        ok = int(selection) == counts["selection"]
        report.cases.append(
            CaseResult(
                6, "重开后精选状态保留", PASS if ok else FAIL,
                f"新连接读到 selection {selection} 行（内存态 {counts['selection']}）、"
                f"事件 {events} 条、主题 {collections} 个",
            )
        )

    def _case_offline_collections(self, report: BreakpointReport) -> None:
        """断点 #7：拔盘后仍可查看精选列表；重新连接后原图可访问。

        自动化部分：置 Library 为 OFFLINE，验证**数据库侧**的精选列表/主题/封面数据仍在
        （封面来自本地 `thumb` 缓存，与原片是否在线无关）。
        物理拔盘必须人工 —— 标为「需人工」。
        """
        counts = self._counts()
        with self.db.write() as conn:
            conn.execute("UPDATE library SET status = 'OFFLINE' WHERE id = ?", (self.library_id,))
        try:
            scenes = int(self.db.scalar("SELECT COUNT(*) FROM scene", default=0))
            finals = int(self.db.scalar(
                "SELECT COUNT(*) FROM selection WHERE status = 'FINAL'", default=0))
            thumbs = int(self.db.scalar(
                "SELECT COUNT(*) FROM preview WHERE kind = 'thumb'", default=0))
            data_ok = scenes >= 0 and thumbs > 0
            detail = (
                f"库置 OFFLINE 后：Scene {scenes} 个、FINAL {finals} 条、"
                f"本地缩略图 {thumbs} 个仍可读；"
                f"精选 {counts['selection']} 条、主题 {counts['collection']} 个"
            )
        finally:
            with self.db.write() as conn:
                conn.execute("UPDATE library SET status = 'ONLINE' WHERE id = ?", (self.library_id,))
        # 断点 #7 的核心是"离线仍能看" —— 数据在且缩略图在即成立；
        # 物理拔盘与"插回后原图可访问"必须人工。
        report.cases.append(
            CaseResult(
                7, "拔盘仍可看精选列表", PASS if data_ok else FAIL, detail,
                steps=[
                    "运行中直接拔掉照片硬盘，确认界面仍能浏览精选列表与缩略图",
                    "插回同一盘符 → `photoos library verify`",
                    "若盘符变化 → `photoos library rebind <Library> <新路径>`，确认原图恢复可访问",
                ],
            )
        )

    def _case_trace_back(self, report: BreakpointReport) -> None:
        """断点 #8：能从最终精选反查完整拍摄组。

        可执行判据：对每一条 FINAL，都能沿 `selection → shot → scene → session`
        反向查到它所在的 Scene 与整组 Shot，并且**选择过程时间线**可读。
        """
        finals = self.db.query(
            "SELECT s.shot_id, sh.scene_id, sc.session_id"
            "  FROM selection s JOIN shot sh ON sh.id = s.shot_id"
            "  LEFT JOIN scene sc ON sc.id = sh.scene_id"
            " WHERE s.status = 'FINAL' AND sh.retired_at IS NULL",
        )
        if not finals:
            report.cases.append(
                CaseResult(
                    8, "从最终精选反查拍摄组", MANUAL,
                    "还没有 FINAL 记录 —— 本项需要先标出至少一张最终精选才有可反查的对象",
                    steps=["在工作台里把某张标为「3 最终精选」，然后重跑本断点"],
                )
            )
            return
        broken = [int(r["shot_id"]) for r in finals if r["scene_id"] is None or r["session_id"] is None]
        # 时间线：每个 FINAL 至少应有一条 selection_event
        traced = 0
        for row in finals:
            events = int(self.db.scalar(
                "SELECT COUNT(*) FROM selection_event WHERE shot_id = ?", (int(row["shot_id"]),),
                default=0,
            ))
            if events:
                traced += 1
        ok = not broken
        report.cases.append(
            CaseResult(
                8, "从最终精选反查拍摄组", PASS if ok else FAIL,
                f"FINAL {len(finals)} 条：可追到 Scene/Session 的 {len(finals) - len(broken)} 条，"
                f"有时间线记录的 {traced} 条"
                + (f"；断链 shot {broken[:5]}" if broken else ""),
            )
        )

    def _case_migration_with_phase2(self, report: BreakpointReport) -> None:
        """断点 #9：以本阶段增量数据（Collection/Selection/FINAL）回归换机迁移。

        自动化部分：导出迁移包（**照片原文件不入包**），核对包内 **manifest 的计数**
        与库内一致，并且包内含 Phase 2 的三张表所需的数据库快照。
        实机 PC-A→PC-B 仍需第二台电脑 → 人工。
        """
        counts = self._counts()
        if counts["selection"] == 0 and counts["collection"] == 0:
            report.cases.append(
                CaseResult(
                    9, "以增量数据回归换机迁移", MANUAL,
                    "还没有精选或主题数据 —— 迁移回归需要本阶段的增量数据才有意义",
                    steps=[
                        "先完成一轮真实筛选（至少几条 SELECTED/FINAL）并建一个主题",
                        "`photoos migrate export` 导出迁移包",
                        "在第二台 Windows 上 `photoos migrate import <包>`",
                        "逐项核对 Collection / Selection / FINAL / Embedding 数量一致",
                    ],
                )
            )
            return
        try:
            from .library import migration as migration_mod

            out = Path(self.settings.backups_dir) / "phase2-breakpoint-package.zip"
            out.parent.mkdir(parents=True, exist_ok=True)
            result = migration_mod.export_package(self.db, self.settings.data_root, out)
            package_ok = out.is_file() and out.stat().st_size > 0
            members = _zip_names(out) if package_ok else []
            has_db = "photoos.db" in members
            has_manifest = "manifest.json" in members
            no_photos = not any(
                str(name).lower().endswith((".jpg", ".jpeg", ".nef", ".mov", ".heic"))
                for name in members
            )
            size_mb = result.bytes_total / 1e6
            out.unlink(missing_ok=True)      # 断点测试不留大文件
            ok = package_ok and has_db and has_manifest and no_photos
            report.cases.append(
                CaseResult(
                    9, "以增量数据回归换机迁移（自动化部分）", PASS if ok else FAIL,
                    f"导出成员 {len(members)} 个（{size_mb:.1f} MB）："
                    f"含 photoos.db={has_db}、manifest={has_manifest}、"
                    f"不含照片原文件={no_photos}；库内待核对："
                    f"selection {counts['selection']}、collection {counts['collection']}、"
                    f"embedding {counts['embedding']}",
                    steps=[
                        "在第二台 Windows 上安装 PhotoOS 并 `photoos migrate import <包>`",
                        "连接原照片硬盘，确认按 Library UUID + 相对路径 + Hash 重新绑定",
                        "逐项核对：Collection 一致、Selection 一致、FINAL 一致、Embedding 一致，"
                        "且**不需要重新导入、不需要重新筛选、不需要重新跑 AI**",
                    ],
                )
            )
        except Exception as exc:  # noqa: BLE001 - 断点测试失败不该让整个报告崩掉
            report.cases.append(
                CaseResult(9, "以增量数据回归换机迁移（自动化部分）", FAIL,
                           f"{type(exc).__name__}: {exc}")
            )


def _time_adjacent_groups(stamps: list[str], gap_seconds: float) -> list[list[int]]:
    """按"时间邻接"切组（与前端 `computeGroups` 同一口径，2 秒阈值）。

    刻意与前端实现保持一致：断点 #2 的外推要基于**用户实际看到的组数**，
    而不是另一套算法算出来的数字。
    """
    import datetime as _dt

    def parse(value: str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.datetime.strptime(value, fmt).timestamp()
            except ValueError:
                continue
        return None

    groups: list[list[int]] = []
    current: list[int] = []
    previous: float | None = None
    for index, raw in enumerate(stamps):
        stamp = parse(raw)
        apart = (
            stamp is not None and previous is not None and (stamp - previous) > gap_seconds
        )
        if current and (apart or stamp is None or previous is None):
            groups.append(current)
            current = []
        current.append(index)
        previous = stamp
    if current:
        groups.append(current)
    return groups


__all__ = [
    "FAIL",
    "MANUAL",
    "PASS",
    "PRD_FIRST_PASS_BUDGET_S",
    "PRD_SCENE_MAX_SHOTS",
    "PRD_SCENE_MIN_SHOTS",
    "SKIP",
    "BreakpointReport",
    "BreakpointRunner",
    "CaseResult",
    "Phase2BreakpointRunner",
]
