"""任务执行器：把 JobQueue 与各阶段流水线串起来。

设计：
  * `JobRunner` 在**后台线程**里串行执行任务（一次一个），每个任务内部自行决定并发度。
    这样「导入不阻塞 UI」「缩略图不等待元数据」等 PRD 要求天然满足。
  * 每个阶段开始/结束时写心跳与进度，因此 UI 轮询能得到连续反馈。
  * 进程被杀后由 `JobQueue.recover_orphans()` 恢复；单文件失败只记 `job_item`，不停整批。
  * **发现阶段与哈希阶段分离**：发现只需遍历文件系统（16k 文件约 1 秒），
    哈希需要读全部字节（252 GB ≈ 70~80 分钟）。分开后用户可以先看到库，再让哈希在后台慢慢跑。
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..actions import catalog as actions_catalog
from ..config import Settings
from ..db import Database, utc_now
from ..errors import LibraryNotFound, PhotoOSError
from ..library.store import LibraryStore
from . import pipelines
from .queue import JobQueue, JobRecord, executor_id


#: 递归守卫用的环境变量名。
#:
#: ⚠️ 为什么需要它：`_run_regroup` 是**通过子进程跑 CLI** 的，而 CLI 的
#: `photoos index --wait` 会执行 `full_index` 任务，`full_index` 又会在末尾
#: 调 `_run_regroup` —— 于是
#:
#:     full_index → 子进程 shots regroup → full_index → 子进程 … （无限递归）
#:
#: 这不是理论风险：接上自动分组之后，跑一次完整测试套件会挂死，
#: 进程树里能看到一层套一层的 `photoos shots regroup --apply`。
#:
#: 守得很简单：**父进程派生子进程时把这个变量设上**，子进程里的
#: `_run_regroup` 看到它就立刻返回。这样"自动分组"只在最外层发生一次。
AUTO_REGROUP_GUARD = "PHOTOOS_SKIP_AUTO_REGROUP"


def _interpreter_for_subprocess(settings: Settings) -> tuple[str, str]:
    """跑内建 CLI 步骤时用哪个解释器。

    与"界面直跑命令"共用同一套选择逻辑（`actions/catalog.py` 的 `interpreter`）——
    这样**只有一个地方**决定"用哪个 python"，两处不会得出不同答案。
    返回 `(python, reason)`。
    """
    from ..actions import catalog as _catalog

    return _catalog.interpreter(settings)


def _terminate(proc: "Any", *, grace: float = 5.0) -> None:
    """先礼后兵地结束一个子进程：terminate → 等 → kill。

    Windows 上没有 POSIX 信号，`terminate()` 走的是 TerminateProcess，
    子进程没有清理机会 —— 对本模块的命令来说可以接受（它们写的是
    data/ 下的文件与数据库，SQLite 自身有事务保护）。
    """
    import subprocess

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端
            pass
    except Exception:  # noqa: BLE001 - 收尾失败不该掩盖真正的错误
        pass


@dataclass
class JobReport:
    """一次任务执行的完整报告（供 CLI 输出与断点测试留档）。"""

    job_id: int
    job_type: str
    status: str
    stages: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    duplicates: dict[str, Any] | None = None
    error: str | None = None

    def lines(self) -> list[str]:
        out = [f"任务 #{self.job_id}（{self.job_type}）状态：{self.status}"]
        for name, detail in self.stages.items():
            out.append(f"  {name}: {detail}")
        if self.stats:
            assets = self.stats.get("assets")
            if assets is not None:
                out.append(f"  库内 Asset：{assets}")
            by_type = self.stats.get("by_type") or {}
            if by_type:
                out.append("  类型分布：" + "，".join(f"{k}={v}" for k, v in sorted(by_type.items())))
            by_status = self.stats.get("by_status") or {}
            if by_status:
                out.append("  状态分布：" + "，".join(f"{k}={v}" for k, v in sorted(by_status.items())))
            coverage = self.stats.get("metadata_with_capture_time")
            total_meta = self.stats.get("metadata_rows")
            if total_meta:
                out.append(f"  拍摄时间覆盖：{coverage}/{total_meta}")
            if self.stats.get("previews"):
                out.append(
                    f"  缩略图/预览：{self.stats['previews']} 个，"
                    f"{int(self.stats.get('previews_bytes') or 0) / 1e9:.2f} GB"
                )
        if self.duplicates:
            out.append(f"  重复情况：{self.duplicates.get('summary', '')}")
        if self.error:
            out.append(f"  失败原因：{self.error[:500]}")
        return out




def _child_env(base_env: dict[str, str]) -> dict[str, str]:
    r"""给子进程的 env：把后端目录塞进 PYTHONPATH。

    为什么必须：AI 解释器（`%LOCALAPPDATA%\PhotoOS\ai-venv`）里**没有安装 photoos 包**，
    而子进程 cwd 是项目根 —— 那里恰好有个同名目录 `photoos/`，Python 会把它当成
    命名空间包，于是报
    `No module named photoos.__main__; 'photoos' is a package and cannot be directly executed`。
    加 PYTHONPATH 指到 `backend/` 才能解析到真正的包。
    """
    env = dict(base_env)
    backend = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = backend + (os.pathsep + existing if existing else "")
    return env

class JobRunner:
    """同步执行器（CLI 用）。Web 层会在后台线程里调用它。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.queue = JobQueue(db)
        self.store = LibraryStore(db)

    # ------------------------------------------------------------------ 入口
    def run(self, job: JobRecord, *, on_progress=None) -> JobReport:
        """执行一个任务。任何异常都会被记录为任务失败而不外抛。

        执行器会**自行把任务置为 PROCESSING**（幂等）。这样调用方即使忘了先 claim
        （CLI 直接执行、后台线程、测试）也不会出现"任务实际在跑、状态却还是 PENDING"的
        不一致状态 —— 首次实现就踩过这个坑。
        """
        report = JobReport(job_id=job.id, job_type=job.type, status="RUNNING")
        self._mark_processing(job.id)
        try:
            handler = {
                "scan": self._run_scan,
                "hash": self._run_hash,
                "metadata": self._run_metadata,
                "preview": self._run_previews,
                "fused": self._run_fused,
                "full_index": self._run_full_index,
                "classify_import": self._run_classify_import,
                "adb_pull": self._run_adb_pull,
                "reject_cleanup": self._run_reject_cleanup,
                "export": self._run_export,
                "cli_action": self._run_cli_action,
                # 这四个曾经只登记在 JOB_TYPES 里、没有处理器 ——
                # 用 POST /api/jobs 建它们会失败并报"尚未在本版本实现"。
                # 现在补齐；`migrate` 的 import 仍然只走 CLI（要停机）。
                "rebind": self._run_rebind,
                "verify": self._run_verify,
                "backup": self._run_backup,
                "migrate": self._run_migrate,
                "regroup": self._run_regroup,
            }.get(job.type)
            if handler is None:
                raise PhotoOSError(f"任务类型 {job.type} 尚未在本版本实现（Phase 1 范围见 PRD §9.1）")
            handler(job, report, on_progress)
        except PhotoOSError as exc:
            report.error = exc.message
            self.queue.fail(job.id, exc.message)
            self.queue.release_claim(job.id)
            report.status = self.queue.get(job.id).status
            return report
        except Exception as exc:  # noqa: BLE001 - 任务失败不得拖垮进程
            report.error = f"{type(exc).__name__}: {exc}"
            self.queue.fail(job.id, f"{report.error}\n{traceback.format_exc()[-1500:]}")
            self.queue.release_claim(job.id)
            report.status = self.queue.get(job.id).status
            return report

        if self.queue.is_cancelled(job.id):
            report.status = "CANCELLED"
        elif self.queue.is_paused(job.id):
            report.status = "PAUSED"
        else:
            self.queue.complete(job.id, note=self._finish_note(report))
            report.status = "COMPLETED"
        self.queue.release_claim(job.id)
        report.stats = pipelines.library_stats(self.db, self._library_id(job))
        return report

    # ------------------------------------------------------------------ 辅助
    def _mark_processing(self, job_id: int) -> None:
        """把任务置为 PROCESSING 并写 started_at / heartbeat（幂等，不覆盖已有 started_at）。"""
        self.db.execute(
            "UPDATE job SET status = 'PROCESSING', started_at = COALESCE(started_at, ?),"
            " heartbeat_at = ?, finished_at = NULL WHERE id = ? AND status NOT IN"
            " ('COMPLETED','CANCELLED')",
            (utc_now(), utc_now(), job_id),
        )

    def _library_id(self, job: JobRecord) -> int:
        library_id = job.scope.get("library_id")
        if library_id is None:
            libraries = self.store.list_all()
            if not libraries:
                raise LibraryNotFound("尚未登记任何 Library，请先运行 `photoos library add`")
            return libraries[0].id
        return int(library_id)

    def _root(self, job: JobRecord) -> Path:
        library = self.store.get(self._library_id(job))
        if not library.root_path_hint:
            raise PhotoOSError(f"Library #{library.id} 没有可用的路径提示，请先 `photoos library add`")
        return Path(library.root_path_hint)

    def _make_progress(self, job_id: int, on_progress):
        def progress(done: int, total: int, note: str) -> None:
            self.queue.update_progress(job_id, done=done, total=total, note=note)
            if on_progress is not None:
                on_progress(done, total, note)

        return progress

    def _finish_note(self, report: JobReport) -> str:
        return "；".join(f"{k}: {v}" for k, v in report.stages.items())[:400]

    # ------------------------------------------------------------------ 各阶段
    def _run_export(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """把一批原片打包成一个 zip（人物 / 主题的"一键打包"）。

        `scope` 需要 `asset_ids`（在入队时就解析好，任务本身不做业务判断），
        可选 `label`（人物名/主题名，用于文件名与包内清单）。

        ★ 这一条**只读原片**：整个模块只有 `open(...,'rb')` 与 `zipfile.write`。
        做成任务而不是同步接口，是因为几百张 RAW 要几分钟 —— 同步请求会超时，
        而任务天然有进度、暂停与取消。
        """
        from ..library import export as export_mod

        scope = job.scope or {}
        asset_ids = [int(a) for a in (scope.get("asset_ids") or [])]
        if not asset_ids:
            raise PhotoOSError("export 任务缺少 scope.asset_ids（要打包哪些 asset）")
        label = str(scope.get("label") or "打包")

        plan = export_mod.plan_export(self.db, asset_ids=asset_ids, label=label)
        report.stages["计划"] = (
            f"{plan.count} 个文件，{plan.total_bytes / 1e9:.2f} GB"
            + (f"，不在盘上 {len(plan.missing)}" if plan.missing else "")
            + (f"，无法定位 {len(plan.no_root)}" if plan.no_root else "")
        )
        if not plan.count:
            raise PhotoOSError(
                "没有可打包的文件：这些照片的原片当前都不在盘上"
                + (f"（{len(plan.missing)} 个）" if plan.missing else ""))

        stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")[:15]
        # 输出路径由**入队方**给定（界面上要能立刻告诉用户"会写到哪"），
        # 没给才在这里算一个 —— 两个地方算同一个名字必然漂移。
        target = Path(scope.get("output_path") or
                      (self.settings.exports_dir
                       / f"{export_mod.safe_label(label)}_{stamp}.zip"))
        progress = self._make_progress(job.id, on_progress)
        stop = lambda: self.queue.should_stop(job.id)  # noqa: E731
        result = export_mod.build_archive(plan, target, progress=progress, stop=stop)

        if result.cancelled:
            report.stages["打包"] = f"已取消（半成品已删除）：{result.path.name}"
            return
        report.stages["打包"] = result.summary()
        if result.skipped:
            report.stages["跳过（不在盘上）"] = "；".join(result.skipped[:3])
        if result.duplicates:
            report.stages["包内重名已改名"] = "；".join(result.duplicates[:3])
        report.stages["输出"] = str(result.path)
        self.store.log_event(
            "export",
            library_id=self._library_id(job),
            payload={"label": label, "files": result.written,
                     "bytes": result.bytes_written, "path": str(result.path),
                     "skipped": len(result.skipped)},
        )

    def _run_cli_action(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """执行一条**白名单命令**（见 `actions/catalog.py`）。

        `scope` 由入队方（`/api/actions/{id}/run`）填好：`action_id` / `query` /
        `argv` / `python`。这里**不再做任何命令拼接** —— 只负责跑、报进度、认暂停。

        几个刻意的取舍：

        * **stderr 并进 stdout 读**。两个管道分开读时，只要其中一个写满缓冲区
          而没人读，子进程就会阻塞在写上面、父进程阻塞在读上面 —— 死锁。
          合并成一个流之后，输出顺序也正是用户在终端里看到的样子。
        * **暂停 = 终止进程**。命令是外部进程，没有协作式暂停的接口；
          与其假装暂停（其实还在烧 CPU），不如停掉并让任务留在 PAUSED，
          用户点"继续"就重跑一次。重跑这些命令是幂等的（都是"补齐/算出"语义）。
        * **失败时把最后几行输出塞进错误里**。用户看到的不该只是"退出码 1"，
          而应该是那条命令自己说的话（例如"未安装 torch / sentence-transformers"）。
        """
        import subprocess
        import time as _time

        scope = job.scope or {}
        action_id = str(scope.get("action_id") or "")
        argv = [str(a) for a in (scope.get("argv") or [])]
        python = str(scope.get("python") or "")
        command = str(scope.get("command") or "")
        if not action_id or not argv or not python:
            raise PhotoOSError("cli_action 任务缺少 scope.action_id / argv / python")

        report.stages["命令"] = command
        progress = self._make_progress(job.id, on_progress)
        progress(0, 0, f"启动：{command}")

        # 工作目录固定为项目根：命令里的相对路径（data/...）才有一致含义。
        cwd = Path(self.settings.data_root).parent
        env = _child_env(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"      # 不加这句子进程会攒够缓冲才吐，进度就"卡住"了
        # ★ 与 `_run_regroup` 同一个理由：子进程不会继承 `settings`，
        # 不传这个变量它会自己去找项目根、可能操作**另一个库**。
        # 见 `_run_regroup` 里那段说明。
        env["PHOTOOS_DATA_ROOT"] = str(self.settings.data_root)
        # 也不让子进程再自动分组（`photoos index` 会跑 full_index）。
        env[AUTO_REGROUP_GUARD] = "1"

        try:
            proc = subprocess.Popen(
                [python, *argv],
                cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            raise PhotoOSError(f"无法启动 {python}：{exc}") from exc

        tail: list[str] = []
        lines = 0
        started = _time.monotonic()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line.strip():
                    tail.append(line)
                    if len(tail) > actions_catalog.MAX_OUTPUT_LINES:
                        del tail[0]
                    lines += 1
                    progress(lines, 0, line[:400])
                # 每个输出行检查一次暂停/取消：这些命令都会持续输出，
                # 所以这个检查点的粒度对用户来说"点下去就停"。
                if self.queue.is_cancelled(job.id) or self.queue.is_paused(job.id):
                    _terminate(proc)
                    break
            # 子进程已结束；收尾等待，避免留下僵尸
            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极少见
                _terminate(proc)
                code = proc.wait(timeout=10)
        finally:
            if proc.poll() is None:  # pragma: no cover - 防御
                _terminate(proc)

        elapsed = _time.monotonic() - started
        report.stages["耗时"] = f"{elapsed:.1f}s"
        report.stages["输出行数"] = str(lines)

        if self.queue.is_cancelled(job.id) or self.queue.is_paused(job.id):
            # 让 run() 的收尾逻辑把状态写成 CANCELLED / PAUSED
            return

        if code != 0:
            detail = "\n".join(tail[-12:]) or "（命令没有输出）"
            self.store.log_event(
                "cli_action_failed",
                library_id=None,
                payload={"action_id": action_id, "exit_code": code, "command": command},
            )
            raise PhotoOSError(
                f"命令退出码 {code}：{command}\n"
                f"—— 最后几行输出：\n{detail}"
            )

        self.store.log_event(
            "cli_action",
            library_id=None,
            payload={"action_id": action_id, "command": command,
                     "seconds": round(elapsed, 1), "lines": lines},
        )

    def _run_rebind(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """把 Library 重新绑定到新位置（盘符变化 / 整盘更换）。

        `scope` 需要 `new_root`（新的照片盘根路径）。只改数据库，不动任何原片 ——
        见 library/rebind.py 的"全程只读"说明。
        """
        from ..library import rebind as rebind_mod

        scope = job.scope or {}
        raw_root = str(scope.get("new_root") or "").strip()
        if not raw_root:
            raise PhotoOSError("rebind 任务缺少 scope.new_root（新的照片库根路径）")
        new_root = Path(raw_root).expanduser()
        library_id = self._library_id(job)

        progress = self._make_progress(job.id, on_progress)
        progress(0, 0, f"扫描新位置：{new_root}")
        result = rebind_mod.rebind_library(
            self.db, library_id, new_root,
            sample=int(scope.get("sample") or 50),
            apply=True,
        )
        for line in result.lines():
            progress(0, 0, line[:400])
        report.stages["重新绑定"] = "已恢复原图访问" if result.rebound else "未执行（见任务输出）"
        self.store.log_event(
            "rebind",
            library_id=library_id,
            payload={"new_root": str(new_root), "rebound": bool(result.rebound)},
        )
        if not result.rebound:
            raise PhotoOSError(
                "重绑定未执行 —— 新位置的抽样比对没通过。请核对路径，"
                "或在 CLI 上用 `photoos library rebind --dry-run` 看完整报告。"
            )

    def _run_verify(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """检查 Library 是否可达，以及索引与磁盘的一致程度。

        这是**状态报告**，不是修数据：无论可达与否都算任务成功（离线是正常状态，
        不是失败 —— 见 PRD §9.1"拔盘期间仍然可用"）。
        """
        from ..library import rebind as rebind_mod

        library_id = self._library_id(job)
        library = self.store.get(library_id)
        progress = self._make_progress(job.id, on_progress)
        progress(0, 0, f"检查 Library #{library_id}「{library.name}」")

        reachable, total, sampled_ok = rebind_mod.library_availability(self.db, library_id)
        lines = [
            f"路径提示：{library.root_path_hint or '(未记录)'}",
            f"状态：{library.status}",
            f"根路径可达：{'是' if reachable else '否（离线）'}",
            f"索引内 Asset：{total}",
        ]
        if reachable:
            lines.append(f"抽样可访问：{sampled_ok}/50")
        else:
            lines.append("照片库当前离线 —— 索引、缩略图、EXIF 与精选记录仍然可用。")
            lines.append("插回硬盘（或换盘符）后用「离线照片库 → 执行重绑定」恢复原图访问。")
        for line in lines:
            progress(0, 0, line[:400])

        report.stages["可达"] = "是" if reachable else "否（离线）"
        report.stages["索引内 Asset"] = str(total)
        if reachable:
            report.stages["抽样可访问"] = f"{sampled_ok}/50"
        self.store.log_event(
            "verify", library_id=library_id,
            payload={"reachable": reachable, "assets": total, "sampled_ok": sampled_ok},
        )

    def _run_backup(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """在线备份数据库（`VACUUM INTO`，不阻塞读）。可选按策略清理旧备份。"""
        from ..library import migration as migration_mod

        scope = job.scope or {}
        progress = self._make_progress(job.id, on_progress)
        label = str(scope.get("label") or "manual")
        progress(0, 0, f"备份数据库（label={label}）…")

        target = migration_mod.backup_database(self.db, self.settings.backups_dir, label=label)
        size_mb = target.stat().st_size / 1e6
        progress(0, 0, f"已备份：{target.name}（{size_mb:.1f} MB）")
        report.stages["备份文件"] = str(target)
        report.stages["大小"] = f"{size_mb:.1f} MB"

        keep = scope.get("keep")
        if keep is not None:
            removed = migration_mod.prune_backups(self.settings.backups_dir, keep=int(keep))
            if removed:
                progress(0, 0, f"按保留策略清理了 {len(removed)} 份旧备份（keep={keep}）")
            report.stages["清理旧备份"] = str(len(removed))
        self.store.log_event(
            "backup", library_id=None, payload={"path": str(target), "bytes": target.stat().st_size},
        )

    def _run_migrate(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """导出 / 校验换机迁移包。

        ⚠️ **不支持 `action="import"`**。导入会**替换整个数据根**，而服务正跑在这份
        数据上：迁移器要先把当前库 `VACUUM INTO` 备份、再覆盖文件，服务自己持有的
        SQLite 连接会让"覆盖成功"变成"半个库是新的一半是旧的"。
        这是本模块唯一**必须**在服务停机时做的事，所以它只走 CLI：

            photoos migrate import <包路径> --dry-run     # 先预演
            photoos migrate import <包路径>
        """
        from ..library import migration as migration_mod

        scope = job.scope or {}
        action = str(scope.get("action") or "export")
        progress = self._make_progress(job.id, on_progress)

        if action == "import":
            raise PhotoOSError(
                "迁移包的**导入**不能在服务运行时执行（要替换整个数据根）。"
                "请停掉 photoos serve 后在命令行运行：\n"
                "  photoos migrate import <包路径> --dry-run\n"
                "  photoos migrate import <包路径>"
            )

        if action == "verify":
            raw = str(scope.get("package") or "").strip()
            if not raw:
                raise PhotoOSError("migrate verify 需要 scope.package（迁移包路径）")
            package = Path(raw).expanduser()
            progress(0, 0, f"校验迁移包：{package.name}")
            result = migration_mod.verify_package(package)
            for line in result.lines():
                progress(0, 0, line[:400])
            report.stages["校验"] = "通过" if result.ok else "未通过"
            if not result.ok:
                raise PhotoOSError(f"迁移包校验未通过：{package}")
            return

        if action != "export":
            raise PhotoOSError(f"migrate 的 action 只能是 export / verify，收到 {action!r}")

        target = scope.get("output")
        if target:
            target = Path(str(target)).expanduser()
        else:
            stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")[:15]
            target = self.settings.backups_dir / f"photoos-migration-{stamp}.zip"

        progress(0, 0, f"导出迁移包 → {target.name}")
        result = migration_mod.export_package(self.db, self.settings.data_root, target)
        for line in result.lines():
            progress(0, 0, line[:400])
        report.stages["迁移包"] = str(target)
        self.store.log_event(
            "migrate", library_id=None, payload={"action": "export", "path": str(target)},
        )

    def _run_classify_import(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """基础物理分类导入：计划 → 复制 → 落库 → 只对新增文件补预览。

        `scope` 需要 `source`（源目录）；可选 `library_id` / `video_library_id` /
        `limit` / `gap_hours` / `geo_cache`。

        **源文件始终只读**：整条路径里只有复制，没有 move/rename/delete。
        做成任务而不是同步接口，是为了直接获得暂停、续跑、进度与孤儿回收。
        """
        from ..library import classify as classify_mod
        from ..library import classify_import
        from ..library import inbox as inbox_mod
        from ..library.geo import GeoCache

        scope = job.scope or {}
        source = scope.get("source")
        if not source:
            raise PhotoOSError("classify_import 任务缺少 scope.source（源目录）")

        library_id = int(scope.get("library_id") or self._library_id(job))
        video_library_id = scope.get("video_library_id")
        cache_path = GeoCache.default_path(self.settings.data_root)
        geo = GeoCache.load(scope.get("geo_cache") or cache_path)
        progress = self._make_progress(job.id, on_progress)
        stop = lambda: self.queue.should_stop(job.id)  # noqa: E731

        plan = classify_import.build_plan(
            self.db,
            Path(source),
            photo_library_id=library_id,
            video_library_id=int(video_library_id) if video_library_id else None,
            geo=geo,
            gap_hours=float(scope.get("gap_hours") or classify_mod.DEFAULT_GAP_HOURS),
            limit=int(scope["limit"]) if scope.get("limit") else None,
            city_override=scope.get("city") or None,
            scan_workers=self.settings.effective_import_scan_workers,
            progress=progress,
        )
        # 收件箱任务：整批计划是按收件箱目录算的，但**只执行入队时已稳定的那些文件**。
        # 裁剪放在这里（而不是 build_plan 的参数里）是为了不改变时段切分：
        # 分类/时段/冲突改名必须按整批算，执行范围才是"这批里的一部分"。
        inbox_ready = scope.get("inbox_ready")
        if inbox_ready:
            ready = {str(key): tuple(value) for key, value in dict(inbox_ready).items()}
            plan = inbox_mod.filter_plan(plan, ready)
            report.stages["收件箱"] = (
                f"已复制完成 {plan.total} 个文件（其余仍在复制或已导入，本次不处理）"
            )
            if plan.total == 0:
                report.stages["收件箱"] = "没有可导入的稳定文件：等复制完成后再点「立即导入」"
                return
        report.stages["计划"] = (
            f"{plan.total} 个文件，{len(plan.plan.sessions)} 个时段，"
            f"地点未知 {plan.plan.unknown_city_count}，跳过 {len(plan.skipped)}"
            + (f"，强制城市 {plan.plan.city_override}" if plan.plan.city_override else "")
        )
        if stop():
            return

        result = classify_import.execute_plan(
            self.db, plan, geo=geo, geo_cache_path=cache_path, progress=progress
        )
        report.stages["复制"] = (
            f"成功 {result.copied}，跳过（内容相同） {result.skipped_duplicate}，"
            f"失败 {result.failed}，{result.bytes_copied / 1e9:.2f} GB"
        )
        report.stages["落库"] = (
            f"Asset {result.assets_registered}，元数据 {result.metadata_written}，"
            f"时段 新建 {result.sessions_created} / 更新 {result.sessions_updated}"
        )
        if inbox_ready:
            # 只记**真的落库**的那部分；失败的留给下一次扫描重试（台账绝不能把失败记成成功）。
            recorded = inbox_mod.record_import(self.settings, plan, result)
            report.stages["收件箱台账"] = f"已登记 {recorded} 个文件（收件箱源文件保留）"
        if result.errors:
            report.stages["错误样例"] = "; ".join(result.errors[:3])
        if stop():
            return

        # 增量索引：`needing_previews` 天然只返回"缺预览"的资产，
        # 因此这里不需要按批次限定范围 —— 库里其余部分已经完整，不会被重复处理。
        for target_library in sorted(set(plan.library_of.values())):
            library = self.store.get(target_library)
            if not library.root_path_hint:
                continue
            previews = pipelines.run_previews(
                self.db,
                target_library,
                Path(library.root_path_hint),
                self.settings.data_root,
                job.id,
                queue=self.queue,
                workers=self.settings.effective_preview_workers,
                progress=progress,
                stop=stop,
            )
            report.stages[f"预览（Library #{target_library}）"] = previews.summary()

        # 同样补上派生分组：**导入的主要目的就是能按场景筛选**。
        # 不在这里做的话，用户导入完还得记得再点一次「完整索引」，
        # 否则精选页是空的 —— 那是"功能在、路不通"。
        if not stop():
            try:
                self._run_regroup(job, report, on_progress)
            except PhotoOSError as exc:
                report.stages["分组"] = f"未完成：{exc.message[:200]}"

        self.store.log_event(
            "classify_import",
            library_id=library_id,
            payload={"source": str(source), "copied": result.copied, "failed": result.failed},
        )

    def _run_adb_pull(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """从手机 ADB 拉到暂存区 → 发布到收件箱。**只读手机**，不直接入库。

        `scope`：`serial`、`remote`、`staging`、`label`、可选 `since`（YYYY-MM-DD）。
        发布之后仍然要等收件箱的稳定判定与用户点「立即导入」—— 手机上的文件不会被删改。
        """
        from ..library import adb as adb_mod

        scope = job.scope or {}
        serial = str(scope.get("serial") or "")
        remote = str(scope.get("remote") or self.settings.adb_remote_path)
        staging_raw = str(scope.get("staging") or "")
        if not serial or not staging_raw:
            raise PhotoOSError("adb_pull 任务缺少 scope.serial / scope.staging")
        staging = Path(staging_raw)
        client = adb_mod.AdbClient.locate(self.settings)
        if client is None:
            raise PhotoOSError(
                "找不到 adb.exe。安装 Android Platform-Tools，或在 config.json 里设 adb_path；"
                "不想装也行 —— 用资源管理器把手机照片复制进收件箱是一样的。"
            )
        progress = self._make_progress(job.id, on_progress)
        stop = lambda: self.queue.should_stop(job.id)  # noqa: E731

        report.stages["设备"] = serial
        try:
            result = client.pull(
                serial,
                remote,
                staging,
                since=scope.get("since") or None,
                on_progress=progress,
                stop=stop,
            )
        except PhotoOSError:
            adb_mod.discard(staging)
            raise
        if result.cancelled or stop():
            adb_mod.discard(staging)
            report.stages["拉取"] = "已取消：暂存区已清理，手机上的文件没有被动过"
            return
        if not result.ok:
            adb_mod.discard(staging)
            raise PhotoOSError("adb pull 失败：" + ("；".join(result.errors) or "未知原因"))

        try:
            target = adb_mod.publish(
                staging, self.settings.effective_inbox_path, str(scope.get("label") or serial)
            )
        except OSError as exc:
            adb_mod.discard(staging)
            raise PhotoOSError(f"发布到收件箱失败：{exc}") from exc
        report.stages["拉取"] = (
            f"{result.files} 个文件 / {result.bytes / 1e6:.1f} MB，{result.seconds:.0f} 秒"
        )
        report.stages["收件箱"] = (
            f"{target}（等文件稳定后，到「从手机导入（收件箱）」点扫描 → 立即导入）"
        )
        self.store.log_event(
            "adb_pull",
            library_id=None,
            payload={
                "serial": serial,
                "remote": remote,
                "files": result.files,
                "bytes": result.bytes,
                "target": str(target),
            },
        )

    def _run_reject_cleanup(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """把废片移入 Windows 回收站（可还原），删除 asset 行，再重算派生分组。

        命中精选/主题的 Shot 会被 `rejects.cleanup()` 跳过并计入"跳过"。
        **不提供永久删除**：回收站是唯一出口。
        """
        from ..library import rejects as rejects_mod

        progress = self._make_progress(job.id, on_progress)
        stop = lambda: self.queue.should_stop(job.id)  # noqa: E731
        result = rejects_mod.cleanup(self.db, on_progress=progress, stop=stop)
        report.stages["废片清理"] = result.summary
        if result.skipped_items:
            report.stages["跳过样例"] = "；".join(result.skipped_items[:3])
        if result.errors:
            report.stages["错误样例"] = "；".join(result.errors[:3])
        if not result.cancelled and result.recycled:
            try:
                self._run_regroup(job, report, on_progress)
            except PhotoOSError as exc:
                report.stages["分组"] = f"未完成：{exc.message[:200]}"
        self.store.log_event(
            "reject_cleanup",
            library_id=None,
            payload={
                "recycled": result.recycled,
                "bytes": result.bytes,
                "skipped": result.skipped,
                "failed": result.failed,
            },
        )

    #: 派生分组的三步。顺序**不能换**：`shots regroup --apply` 落库前要求    #: `session` 行已回填，而 `scenes rebuild` 又建立在 Shot 之上。
    REGROUP_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("时段回填", ("sessions", "backfill", "--apply")),
        ("拍摄组重算", ("shots", "regroup", "--apply")),
        ("场景重算", ("scenes", "rebuild", "--apply")),
    )

    def _run_regroup(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """把派生分组补齐：sessions → shots → scenes。

        ## 为什么需要它

        界面上的「精选」与「按时间/地点找照片」**以 Shot / Scene 为单位**，
        而导入（`classify_import`）只建到 `session` 一层。在此之前，分组只能靠
        在命令行手敲三条命令 —— `plan_regroup` 在整个包里没有一个调用者，
        界面里也没有入口。于是新库导入完，点进精选看到的是
        "0 Shot / 0 Scene"，用户会以为导入失败。

        ## 为什么走子进程

        这三步各自有完整的 CLI 实现（含预演、校验、进度输出与安全提示），
        直接把逻辑搬进来会形成**第二份实现**，两边迟早漂移。
        从 `cli.py` 导入私有函数再拼一遍更糟 —— 那会把 CLI 的内部结构变成 API。

        命令与参数**全部由服务端写死**（见 `REGROUP_STEPS`），不接受任何外部输入，
        所以这里没有 `cli_action` 那套"只认 action id"的问题。

        ## 幂等性

        三步都幂等：`sessions backfill` 按 `(city, period)` 判重；
        `shots regroup` 会跳过用户手工调整过的 Shot（决策 #10）；
        `scenes rebuild` 按派生结果替换。所以可以随 `full_index` 反复跑。

        ## 为什么必须显式传 `PHOTOOS_DATA_ROOT`

        ⚠️ 子进程**不会**继承父进程的 `settings`。`config.find_project_root()`
        是从 `photoos.toml` 往上找项目根的，所以不带环境变量时子进程会得出
        **它自己**的数据根 —— 在开发机上恰好等于服务在用的那个，于是"看起来能跑"，
        实际上**操作的是另一个数据库**：子进程在 A 库上分组，服务从 B 库读结果，
        界面上什么都不会变（而且不会报错）。

        这个坑是实测撞出来的：用隔离数据根做验证时，分组三步全部"完成"，
        但服务的库里 shot/scene 仍然是 0。

        `PHOTOOS_DATA_ROOT` 是 `Settings` 的正式环境变量（见 config.py 的
        `env_prefix`），优先级高于配置文件 —— 所以传它就是"让子进程和父进程
        看同一份数据"的准确做法。
        """
        import os
        import subprocess

        # ★ 递归守卫：本函数是通过子进程跑 CLI 的，而 CLI 的 `index --wait` 会跑
        # `full_index`、`full_index` 又会在末尾调本函数 —— 不挡住就是无限递归。
        # 详见 `AUTO_REGROUP_GUARD` 的说明。
        if os.environ.get(AUTO_REGROUP_GUARD):
            report.stages["分组"] = "跳过（当前已是子进程，由最外层负责）"
            return

        python, _reason = _interpreter_for_subprocess(self.settings)
        progress = self._make_progress(job.id, on_progress)
        cwd = Path(self.settings.data_root).parent
        env = _child_env(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"   # 不加这句子进程攒够缓冲才吐，进度会"卡住"
        # ★ 关键：让子进程用**同一个数据根**，否则它会自己去找项目根，
        # 得出的可能是另一个库（见上面的说明）。
        env["PHOTOOS_DATA_ROOT"] = str(self.settings.data_root)
        # ★ 并告诉子进程"不要再自动分组了"—— 那正是递归的来源。
        env[AUTO_REGROUP_GUARD] = "1"

        for index, (label, argv) in enumerate(self.REGROUP_STEPS, start=1):
            if self.queue.should_stop(job.id):
                return
            command = [python, "-m", "photoos", *argv]
            progress(0, 0, f"[{index}/{len(self.REGROUP_STEPS)}] {label}…")
            try:
                proc = subprocess.Popen(
                    command, cwd=str(cwd), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as exc:
                raise PhotoOSError(f"无法启动 {python}：{exc}") from exc

            tail: list[str] = []
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    if not line.strip():
                        continue
                    tail.append(line)
                    if len(tail) > 40:
                        del tail[0]
                    progress(0, 0, line[:400])
                    if self.queue.should_stop(job.id):
                        _terminate(proc)
                        break
                try:
                    code = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - 极少见
                    _terminate(proc)
                    code = proc.wait(timeout=10)
            finally:
                if proc.poll() is None:  # pragma: no cover - 防御
                    _terminate(proc)

            if self.queue.should_stop(job.id):
                return
            if code != 0:
                detail = "\n".join(tail[-10:]) or "（命令没有输出）"
                raise PhotoOSError(
                    f"{label}失败（退出码 {code}）：{' '.join(argv)}\n"
                    f"—— 最后几行输出：\n{detail}"
                )
            report.stages[label] = "完成"

        self.store.log_event(
            "regroup", library_id=job.scope.get("library_id"),
            payload={"steps": [label for label, _ in self.REGROUP_STEPS]},
        )

    def _run_scan(self, job: JobRecord, report: JobReport, on_progress) -> None:
        library_id = self._library_id(job)
        root = self._root(job)
        result = pipelines.run_discovery(
            self.db,
            library_id,
            root,
            job.id,
            queue=self.queue,
            scope_prefix=job.scope.get("scope_prefix"),
            progress=self._make_progress(job.id, on_progress),
            stop=lambda: self.queue.should_stop(job.id),
        )
        report.stages["发现"] = result.summary()
        if result.errors:
            report.stages["发现错误样例"] = "; ".join(f"{p}: {m}" for p, m in result.errors[:3])
        self.store.log_event(
            "scan", library_id=library_id,
            payload={"done": result.done, "missing": result.failed, "files": result.done + result.skipped},
        )

    def _run_hash(self, job: JobRecord, report: JobReport, on_progress) -> None:
        library_id = self._library_id(job)
        root = self._root(job)
        limit = job.scope.get("limit")
        result = pipelines.run_hash(
            self.db,
            library_id,
            root,
            job.id,
            queue=self.queue,
            limit=int(limit) if limit else None,
            scope_prefix=job.scope.get("scope_prefix"),
            progress=self._make_progress(job.id, on_progress),
            stop=lambda: self.queue.should_stop(job.id),
        )
        report.stages["哈希"] = result.summary()
        duplicates = pipelines.build_duplicate_report(self.db, library_id)
        report.duplicates = {"summary": duplicates.summary(), "groups": duplicates.groups}
        self.store.log_event(
            "hash", library_id=library_id,
            payload={"done": result.done, "failed": result.failed, "duplicate_groups": duplicates.groups},
        )

    def _run_metadata(self, job: JobRecord, report: JobReport, on_progress) -> None:
        library_id = self._library_id(job)
        root = self._root(job)
        result = pipelines.run_metadata(
            self.db,
            library_id,
            root,
            job.id,
            queue=self.queue,
            workers=self.settings.effective_preview_workers,
            scope_prefix=job.scope.get("scope_prefix"),
            progress=self._make_progress(job.id, on_progress),
            stop=lambda: self.queue.should_stop(job.id),
        )
        report.stages["元数据"] = result.summary()
        if result.errors:
            report.stages["元数据错误样例"] = "; ".join(f"{p}: {m}" for p, m in result.errors[:3])

    def _run_previews(self, job: JobRecord, report: JobReport, on_progress) -> None:
        library_id = self._library_id(job)
        root = self._root(job)
        result = pipelines.run_previews(
            self.db,
            library_id,
            root,
            self.settings.data_root,
            job.id,
            queue=self.queue,
            workers=self.settings.effective_preview_workers,
            limit=int(job.scope["limit"]) if job.scope.get("limit") else None,
            force=bool(job.scope.get("force")),
            scope_prefix=job.scope.get("scope_prefix"),
            progress=self._make_progress(job.id, on_progress),
            stop=lambda: self.queue.should_stop(job.id),
        )
        report.stages["预览"] = result.summary()
        if result.errors:
            report.stages["预览错误样例"] = "; ".join(f"{p}: {m}" for p, m in result.errors[:3])

    def _run_fused(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """融合阶段：一次读盘同时算哈希与生成缩略图/预览。

        只处理"既没哈希也没预览"的文件，因此**不会**因为重复读盘而失去意义。
        """
        library_id = self._library_id(job)
        root = self._root(job)
        result = pipelines.run_fused_hash_previews(
            self.db,
            library_id,
            root,
            self.settings.data_root,
            job.id,
            queue=self.queue,
            workers=self.settings.effective_preview_workers,
            limit=int(job.scope["limit"]) if job.scope.get("limit") else None,
            progress=self._make_progress(job.id, on_progress),
            stop=lambda: self.queue.should_stop(job.id),
        )
        report.stages["融合（一次读盘：哈希+预览）"] = result.summary()
        if result.errors:
            report.stages["融合错误样例"] = "; ".join(f"{p}: {m}" for p, m in result.errors[:3])
        self.store.log_event(
            "fused",
            library_id=library_id,
            payload={
                "done": result.done,
                "failed": result.failed,
                "bytes": result.bytes_processed,
                "seconds": round(result.seconds, 1),
                "mb_per_s": round(result.throughput_mb_s, 1),
            },
        )

    def _run_full_index(self, job: JobRecord, report: JobReport, on_progress) -> None:
        """一次跑完发现 → 元数据 → 预览 → 哈希。

        顺序刻意让"用户尽快看到东西"优先：发现（秒级）→ 元数据/预览（可浏览）→ 哈希（最慢）。
        每一步都检查暂停/取消，因此中断点即续跑点。
        """
        library_id = self._library_id(job)
        root = self._root(job)
        stop = lambda: self.queue.should_stop(job.id)  # noqa: E731
        progress = self._make_progress(job.id, on_progress)
        scope_prefix = job.scope.get("scope_prefix")

        discovery = pipelines.run_discovery(
            self.db, library_id, root, job.id, queue=self.queue, scope_prefix=scope_prefix,
            progress=progress, stop=stop,
        )
        report.stages["发现"] = discovery.summary()
        if stop():
            return

        metadata = pipelines.run_metadata(
            self.db, library_id, root, job.id, queue=self.queue,
            workers=self.settings.effective_preview_workers, scope_prefix=scope_prefix,
            progress=progress, stop=stop,
        )
        report.stages["元数据"] = metadata.summary()
        if stop():
            return

        previews = pipelines.run_previews(
            self.db, library_id, root, self.settings.data_root, job.id, queue=self.queue,
            workers=self.settings.effective_preview_workers, scope_prefix=scope_prefix,
            progress=progress, stop=stop,
        )
        report.stages["预览"] = previews.summary()
        if stop():
            return

        # 最后一步用**融合阶段**：对"还没哈希、也没预览"的文件只读一次盘。
        # 分别跑 hash 与 preview 会把整个库读两遍；融合后少读一遍
        # （实测同一块 USB 盘上，融合的吞吐约为单独跑预览的 2 倍）。
        fused = pipelines.run_fused_hash_previews(
            self.db, library_id, root, self.settings.data_root, job.id, queue=self.queue,
            workers=self.settings.effective_preview_workers,
            progress=progress, stop=stop,
        )
        report.stages["融合（一次读盘：哈希+预览）"] = fused.summary()
        if stop():
            return

        # 兜底：视频无法从内存解码，因此仍有"缺哈希或缺预览"的文件走单独阶段
        hashed = pipelines.run_hash(
            self.db, library_id, root, job.id, queue=self.queue, scope_prefix=scope_prefix,
            progress=progress, stop=stop,
        )
        report.stages["哈希（兜底）"] = hashed.summary()
        if stop():
            return

        remaining_previews = pipelines.run_previews(
            self.db, library_id, root, self.settings.data_root, job.id, queue=self.queue,
            workers=self.settings.effective_preview_workers, scope_prefix=scope_prefix,
            progress=progress, stop=stop,
        )
        report.stages["预览（兜底）"] = remaining_previews.summary()

        duplicates = pipelines.build_duplicate_report(self.db, library_id)
        report.duplicates = {"summary": duplicates.summary(), "groups": duplicates.groups}

        # ★ 最后补上**派生分组**（sessions → shots → scenes）。
        #
        # 没有这一步，索引跑完精选页仍然是空的：界面以 Shot / Scene 为单位，
        # 而索引阶段只写 asset / metadata / preview。这是"导入完就能用"这条链
        # 上一直缺的一环 —— 用户导入完照片、点进精选，看到的是
        # "0 Shot / 0 Scene"，很容易以为导入失败了。
        #
        # 三步都幂等，所以重复跑「完整索引」不会重复建行，也不会覆盖用户手工
        # 调整过的 Shot（决策 #10）。失败只记一条 stage，**不让整个索引算失败** ——
        # 索引本身已经成功了，分组可以单独重跑。
        if not stop():
            try:
                self._run_regroup(job, report, on_progress)
            except PhotoOSError as exc:
                report.stages["分组"] = f"未完成：{exc.message[:200]}"

        self.store.log_event(
            "full_index", library_id=library_id,
            payload={
                "assets": discovery.done + discovery.skipped,
                "hashed": hashed.done + fused.done,
                "previews": previews.done + fused.done + remaining_previews.done,
                "duplicate_groups": duplicates.groups,
            },
        )


class BackgroundRunner:
    """后台执行器：在守护线程里依次执行队列中的任务。

    Web 层与 `photoos serve` 使用它；CLI 的 `--wait` 模式用 `JobRunner` 同步跑。
    """

    def __init__(self, db: Database, settings: Settings, *, idle_sleep: float = 1.0,
                 recover_interval: float = 60.0) -> None:
        self.runner = JobRunner(db, settings)
        self.queue = self.runner.queue
        self.idle_sleep = idle_sleep
        #: 运行期**周期性**孤儿回收的间隔（秒）。
        #: 为什么需要它：`recover_orphans()` 原先只在 `start()` 里跑一次，而它的
        #: 过期阈值是 300 秒 —— "崩溃后立刻重启"时上一进程的心跳距今 < 300 秒，
        #: 那一行**不会被回收**，会一直停在 `PROCESSING`（没人能领，也不报错）。
        #: 见 20-reference Q-43。
        self.recover_interval = recover_interval
        #: 执行者身份：只认领"无归属"或"归属自己"的任务。
        #: 这样 `photoos serve` 的后台执行器与命令行的 `photoos index` 不会抢同一个任务
        #: （真实事故：两者并行执行，在 60 MB/s 的 USB 盘上互相抢带宽并重复读文件）。
        self.owner = executor_id()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current_job_id: int | None = None
        self._lock = threading.Lock()
        self._last_recover = 0.0

    @property
    def current_job_id(self) -> int | None:
        with self._lock:
            return self._current_job_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.queue.recover_orphans(owner=self.owner)
        self._last_recover = time.monotonic()   # 启动时刚回收过，别立刻再来一次
        self._thread = threading.Thread(target=self._loop, name="photoos-jobs", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _maybe_recover(self) -> None:
        """运行期按 `recover_interval` 周期回收孤儿（见 `recover_interval` 的说明）。

        只在**空闲**时真正执行：正在跑任务时去改任务表没有意义，
        而且回收本身要开写事务。开销可忽略（一条 `SELECT` + 偶尔几条 `UPDATE`）。
        """
        now = time.monotonic()
        if now - self._last_recover < self.recover_interval:
            return
        self._last_recover = now
        try:
            self.queue.recover_orphans(owner=self.owner)
        except Exception:  # noqa: BLE001 - 回收失败不应杀死后台线程
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_recover()
            try:
                job = self.queue.claim_next(owner=self.owner)
            except Exception:  # noqa: BLE001 - 队列异常不应杀死后台线程
                self._stop.wait(self.idle_sleep)
                continue
            if job is None:
                self._stop.wait(self.idle_sleep)
                continue
            with self._lock:
                self._current_job_id = job.id
            try:
                self.runner.run(job)
            except Exception:  # noqa: BLE001 - runner 已自行兜底，这里是最后防线
                self.queue.fail(job.id, "后台执行器内部错误")
                self.queue.release_claim(job.id)
            finally:
                with self._lock:
                    self._current_job_id = None


__all__ = ["BackgroundRunner", "JobReport", "JobRunner"]
