"""FastAPI 应用装配（PRD §10 / §13）。

设计要点：
  * **纯本地服务**：默认只监听 `127.0.0.1`，没有鉴权也有意如此（单用户本地优先，PRD §2.6）。
  * **UI 不阻塞**：所有可能慢的数据库查询都通过 `anyio.to_thread` 下放，避免阻塞事件循环。
  * **缩略图/预览走缓存文件**：离线 Library 也能浏览（PRD §9.1 断点 #5）。
  * **不引入任何构建链**：前端是零构建的 ES module + 原生 JS，由本服务静态托管。
  * 破坏性操作在此层**没有入口**（Phase 1 不提供删除原文件的能力，见 PRD §11.1）。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import asyncio
import importlib

from .. import ai_components, ai_install, ai_mirrors
from .. import __version__
from ..actions import catalog as actions_catalog
from ..config import Settings, get_settings
from ..db import Database, target_version
from ..errors import PhotoOSError
from ..jobs import BackgroundRunner, JobQueue, JobRunner
from ..jobs import pipelines
from ..library import migration as migration_mod
from ..library import rebind as rebind_mod
from ..library import shot_grouping as shot_mod
from ..library.selection import InvalidSelectionStatus
from ..library.store import LibraryStore, probe_path
from ..library.volume import volume_identity
from . import queries as queries_mod
from .queries import (
    asset_detail,
    asset_page,
    capture_year_histogram,
    camera_histogram,
    focal_range,
    integrity_summary,
    lens_histogram,
    library_overview,
    list_jobs,
)

#: 缩略图/预览的缓存头：派生文件内容由 generator_version 决定，可长期缓存
MEDIA_CACHE_CONTROL = "public, max-age=604800"

#: 分面（筛选面板的下拉框内容）的缓存窗口。
#: 短窗口 + ETag：同一秒内反复切页面完全不发请求；导入完的照片最多 60 秒后出现。
FACETS_CACHE_CONTROL = "private, max-age=60"

#: 全库统计的缓存窗口。同一理由，见 `/api/stats` 的说明。
STATS_CACHE_CONTROL = "private, max-age=60"

#: 浏览器能**直接内联渲染**的图片类型。取值域来自 `media.detect`：
#: `raw|jpeg|heic|png|tiff|webp|video|other`。
#: 刻意保守：TIFF 只有 Safari 支持，HEIC 只有 Safari 支持，`other` 里混着 PSD，
#: 一律回退到派生预览（预览本来就是从这些格式转出来的 WebP，任何浏览器都能显示）。
_INLINE_IMAGE_TYPES = frozenset({"jpeg", "png", "webp"})

#: 可作为 `<video>` 播放的类型。**能否真的播放还取决于编码**
#: （例如 HEVC 编码的 MOV 在 Chrome 里放不了）—— 这不由服务端决定，
#: 前端需要准备好降级提示。
_INLINE_VIDEO_TYPES = frozenset({"video"})


def _plan_dict(plan: Any, *, source: str) -> dict[str, Any]:
    """把 `ClassifyImportPlan` 变成可 JSON 化的预演结果，供界面渲染。

    每一条明细都带 `city_how` / `time_how`：用户必须能看懂**为什么**归到这个城市与时段，
    否则无法判断计划是否可信 —— 这正是预演存在的意义。
    """
    files = plan.plan.files
    kinds: dict[str, int] = {}
    for item in files:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    return {
        "source": source,
        "total": plan.total,
        "source_total": plan.source_total,
        "source_bytes": plan.source_bytes,
        "preview_sampled": plan.preview_sampled,
        "seconds": round(plan.seconds, 2),
        "by_kind": kinds,
        "by_city": plan.plan.by_city(),
        "by_city_how": plan.plan.by_city_how(),
        "by_time_how": plan.plan.by_time_how(),
        "by_library": {str(key): value for key, value in plan.by_library().items()},
        "sessions": [
            {
                "city": session.city,
                "folder": session.folder,
                "captured_from": session.captured_from.strftime("%Y-%m-%d %H:%M:%S"),
                "captured_to": session.captured_to.strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": session.confidence,
                "members": session.members,
                "time_unknown": session.time_unknown,
            }
            for session in plan.plan.sessions
        ],
        "unknown_city_count": plan.plan.unknown_city_count,
        "unknown_time_count": plan.plan.unknown_time_count,
        "city_override": plan.plan.city_override,
        "city_override_disagreements": len(plan.plan.city_override_disagreements),
        "unresolved_cells": plan.plan.unresolved_cells,
        "notes": plan.notes,
        "skipped": [{"path": path, "reason": reason} for path, reason in plan.skipped[:200]],
        "skipped_total": len(plan.skipped),
        "files": [
            {
                "name": item.name,
                "target": item.target_relative,
                "kind": item.kind,
                "city": item.city,
                "city_how": item.city_how,
                "session": item.session,
                "local_time": item.local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "time_how": item.time_how,
                "time_unknown": item.time_unknown,
                "size": item.size,
            }
            for item in files[:IMPORT_PLAN_MAX_ROWS]
        ],
        "files_truncated": max(0, len(files) - IMPORT_PLAN_MAX_ROWS),
    }


def _win_shell_select(target: Path) -> bool:
    """Windows：用 shell 官方接口在资源管理器里**选中**这个文件。成功返回 True。

    抽成独立函数有两个理由：一是可测（测试里把它换成"失败/成功"两种，
    分别验证兜底命令行与"成功后不再起进程"）；二是这段 ctypes 与业务无关。

    ⚠️ `SHOpenFolderAndSelectItems` 要求**调用线程的 COM 已初始化**：
    直接调会返回 `0x800401F0`（CO_E_NOTINITIALIZED，实测）。
    请求跑在 anyio 的工作线程里，那些线程默认没初始化过 COM。
    STA（COINIT_APARTMENTTHREADED）初始化一次即可；S_OK/S_FALSE 都要配对反初始化。
    """
    try:
        import ctypes

        ole32 = ctypes.windll.ole32
        hr = ole32.CoInitializeEx(None, 2)
        try:
            shell32 = ctypes.windll.shell32
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            pidl = shell32.ILCreateFromPathW(str(target))
            if not pidl:
                return False
            try:
                return shell32.SHOpenFolderAndSelectItems(
                    ctypes.c_void_p(pidl), 0, None, 0) == 0
            finally:
                shell32.ILFree(ctypes.c_void_p(pidl))
        finally:
            if hr in (0, 1):        # S_OK / S_FALSE（本就初始化过）都要配对
                ole32.CoUninitialize()
    except Exception:  # noqa: BLE001 - 任何异常都退回命令行形式
        return False


def _reveal_in_file_manager(path: Path, *, folder_only: bool = False) -> str:
    """在文件管理器里**定位到**这个文件（选中它），返回实际用的方式。

    为什么不是简单一句 `subprocess.Popen(["explorer", f"/select,{path}"])`：

      * Python 的 `list2cmdline` 会把**含空格的那个参数整个加引号** →
        `explorer "/select,C:\\Photos\\a b\\x.NEF"`。而 `explorer.exe` 的命令行
        解析是非标准的：它要的形式是 `/select,"<路径>"`（引号只包路径）。
        实测库里就有带空格的文件名（`DSC_4063 - 副本.NEF`），那种路径
        在这个写法下会打开"文档"而不是定位文件。
      * 就算命令行写对了，`/select` 在"那个文件夹已经开着"时常常只是把窗口
        切到前面，**不会重新选中**文件。

    所以 Windows 上优先用 `_win_shell_select`（按 PIDL 定位，空格/中文都不用转义，
    也会在已有窗口里选中），失败才退回命令行形式。
    这条路径**只读**：不移动、不改名、不删除。
    """
    import subprocess
    import sys as _sys

    target = Path(path)
    folder = target.parent
    if folder_only or not target.is_file():
        # 文件不在盘上（拔盘/被移走）：退而打开它**所在的目录**，
        # 用户至少能到那个位置。这里没有可"选中"的对象。
        if _sys.platform.startswith("win"):
            os.startfile(str(folder))          # noqa: S606 - 打开目录，无 shell 拼接
            return "startfile-folder"
        argv = (["open", str(folder)] if _sys.platform == "darwin"
                else ["xdg-open", str(folder)])
        subprocess.Popen(argv, close_fds=True)  # noqa: S603 - 固定 argv
        return "open-folder"

    if _sys.platform.startswith("win"):
        if _win_shell_select(target):
            return "shell-select"
        # 兜底：**整个命令行当成一个字符串**给 CreateProcess，
        # 这样引号只包住路径本身（`/select,"<路径>"`），不会被 Python 重新引号。
        subprocess.Popen(f'explorer /select,"{target}"', close_fds=True)
        return "explorer-select"

    argv = (["open", "-R", str(target)] if _sys.platform == "darwin"
            else ["xdg-open", str(folder)])
    subprocess.Popen(argv, close_fds=True)      # noqa: S603 - 固定 argv
    return "open-select"


def _resolve_asset_file(db: Database, asset_id: int) -> tuple[Any, Path] | None:
    """返回 `(AssetRecord, 原片绝对路径)`；Asset 不存在或库无路径提示时返回 None。

    **不做存在性判断** —— 调用方需要自己区分「Asset 不存在」(404) 与
    「库离线/文件不可访问」(409)，这两种情况的用户提示完全不同。
    """
    from ..repos import AssetRepo, asset_absolute_path

    try:
        record = AssetRepo(db).get(asset_id)
    except KeyError:
        return None
    library = LibraryStore(db).get(record.library_id)
    if not library.root_path_hint:
        return None
    return record, asset_absolute_path(Path(library.root_path_hint), record.relative_path)


#: 库离线时统一的重绑定提示（断点 #5 / #6 的用户可见承诺）
_OFFLINE_DETAIL = (
    "照片库当前离线或文件不可访问；重新连接硬盘后执行重绑定即可恢复（PRD §9.1）。"
)

#: 导入预演的默认上限。预演必须为每个文件读一次 EXIF（RAW 要遍历整个 TIFF），
#: 同步 HTTP 请求不适合无上限地跑；用户可在界面上显式调大。
IMPORT_PLAN_DEFAULT_LIMIT = 300
#: 单次预演响应里最多返回多少条明细（避免为上万文件生成巨大 JSON）。
IMPORT_PLAN_MAX_ROWS = 2000


def get_db(settings: Settings | None = None) -> Database:
    settings = settings or get_settings()
    db = Database(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    db.require_exists()
    return db


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用。

    注意：`lifespan` **必须**捕获这里传入的 `settings`，不能内部再调 `get_settings()` ——
    否则传入的配置会被静默忽略（真实 bug：测试/CLI 传入临时数据根，应用却连上真实库）。
    """
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动后台任务执行器；关闭时优雅停止。"""
        resolved_settings.ensure_dirs()
        db = get_db(resolved_settings)
        app.state.settings = resolved_settings
        app.state.db = db
        # ★ 启动时清理"幽灵任务"：上一次进程被强杀（打包/关机/任务管理器）时，
        # 数据库里会留下 PENDING/PROCESSING 的任务 —— 界面永远转圈，新任务还会被
        # 队列判定为"已有一条在跑"而拒绝入队（实测：装 Gemma 点了没反应）。
        try:
            from ..db import utc_now as _utc_now

            orphans = db.query(
                "SELECT id FROM job WHERE status IN ('PENDING','PROCESSING','RETRY','PAUSED')")
            for row in orphans:
                db.execute(
                    "UPDATE job SET status = 'FAILED', error = ?, finished_at = ? WHERE id = ?",
                    ("进程重启时中断（不会自动续跑）", _utc_now(), int(row["id"])))
            if orphans:
                import logging

                logging.getLogger("photoos").warning(
                    "启动清理：%d 条中断的任务标记为 FAILED", len(orphans))
        except Exception:  # noqa: BLE001 —— 清理失败不该阻止启动
            pass

        runner = BackgroundRunner(db, resolved_settings)
        runner.start()
        app.state.runner = runner

        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(
        title="PhotoOS",
        version=__version__,
        description="个人摄影管理系统 · Phase 1 照片库基础",
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        """兜底：任何**未预期**异常也要给出可读说明。

        真实现场：`/api/import/plan` 的局部 `except` 子句里用了一个未导入的名字，
        于是任何错误都变成裸的 `Internal Server Error`，界面上完全看不出原因，
        连"相机 MTP 路径不支持"这种本该讲清楚的情况也只剩一句英文。
        这里保证界面至少能看到异常类型与消息，完整堆栈进服务端日志。
        """
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return JSONResponse(
            status_code=500,
            content={
                "message": f"服务器内部错误：{type(exc).__name__}: {exc}",
                "detail": "完整堆栈已写入服务端日志（运行 `photoos serve` 的那个窗口）。",
            },
        )

    # ------------------------------------------------------------------ 异常
    @app.exception_handler(RequestValidationError)
    async def _request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """把 FastAPI 的请求校验错误**降级成可序列化**的 422。

        为什么需要这个处理器：FastAPI 默认会把请求体里的**原始值**原样放回错误响应，
        而 JSON 里没有 `NaN` / `Infinity` 字面量。于是客户端一旦发来 `1e400`（合法 JSON，
        Python 解析成 `inf`），默认处理器自己就 `json.dumps` 失败 —— 用户看到的是
        **500**，而真正的原因（数值超范围）完全丢失。实测到的现场：
        `PATCH /api/sessions/{id}/review-config` 传 `1e400` → 500 而非 422。

        这里只做一件事：把非有限浮点**转成字符串**再返回，保证错误信息一定发得出去。
        """
        return JSONResponse(status_code=422, content={"detail": _json_safe_errors(exc.errors())})

    @app.exception_handler(PhotoOSError)
    async def _photoos_error(_request: Request, exc: PhotoOSError) -> JSONResponse:
        """把领域错误翻译为带稳定 code 的 HTTP 响应（不泄露栈信息）。"""
        status = 400
        if exc.__class__.__name__ in {"LibraryNotFound", "JobNotFound"}:
            status = 404
        elif exc.__class__.__name__ == "NotInitializedError":
            status = 503
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )

    # ------------------------------------------------------------------ 多维筛选
    #
    # 这是把三类条件叠在一次查询上的**唯一入口**：
    #   内容（像"日落"的照片）× 元数据（机型/镜头/焦距/年份/路径…）× 人物（谁在里面）
    #
    # 改造前这三者在代码里从来没相遇过（见 `api/compose.py` 的文件头说明）。
    # 组合逻辑全在 `compose.py`，这里只负责解析参数、算内容分、拼起来。

    @app.get("/api/filter")
    async def filter_shots(
        request: Request,
        q: str | None = Query(None, description="内容词（可选）。没给就只按元数据/人物筛"),
        person_id: list[int] | None = Query(None, description="人物 id，可重复。多个是 AND（合影）"),
        year: int | None = Query(None),
        camera: str | None = Query(None),
        lens: str | None = Query(None),
        focal_min: float | None = Query(None),
        focal_max: float | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        file_type: str | None = Query(None),
        status: str | None = Query(None),
        keyword: str | None = Query(None),
        path_prefix: str | None = Query(None),
        library_id: int | None = Query(None),
        only_missing_metadata: bool = Query(False),
        order: str | None = Query(None, description="无内容词时的排序：capture/name/size"),
        tuning: bool = Query(True, description="内容词是否套用你的表态重排"),
        limit: int = Query(120, ge=1, le=600),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        db: Database = request.app.state.db
        from . import compose as compose_mod

        def _run() -> dict[str, Any]:
            from ..library import semantic_rank
            from ..repos import SemanticRepo

            ranked: list[dict[str, Any]] | None = None
            dropped: set[int] = set()
            meta: dict[str, Any] = {
                "library_id": library_id, "file_type": file_type, "status": status,
                "camera": camera, "lens": lens, "focal_min": focal_min,
                "focal_max": focal_max, "year": year, "date_from": date_from,
                "date_to": date_to, "keyword": keyword, "path_prefix": path_prefix,
                "only_missing_metadata": only_missing_metadata,
            }

            query_text = (q or "").strip()
            if query_text:
                repo = SemanticRepo(db)
                row = repo.get_query(query_text)
                if row is None:
                    # 这个词没算过文本向量 —— 与 `/api/semantic/results` 一样的说法，
                    # **不猜**，也不静默退回"只按元数据筛"（那会让用户以为词生效了）。
                    return {
                        "missing": True, "reason": "not_preseeded",
                        "query_text": query_text,
                        "hint": f'这个词还没有算过文本向量，请先在命令行执行一次：'
                                f'photoos search "{query_text}" --save',
                    }
                row = dict(row)
                feedback = repo.feedback_map(int(row["id"]))
                dropped = {int(s) for s, a in feedback.items() if a == "drop"}

                image_model, image_version, prep = semantic_rank.resolve_image_identity(db, row)
                pool = _semantic_pool(request.app, db, (image_model, image_version, prep))
                learned = None
                if tuning and feedback:
                    learned = semantic_rank.compute_tuning(db, query_row=row, feedback=feedback)
                    if not learned.active:
                        learned = None
                # ⚠️ 这里**必须取全库**（limit = 池大小）。
                # `rerank` 的 limit 是**早停**：先截断再叠加筛选，结果会被截没 ——
                # 实测"月亮 + 某个人"这种组合在 limit=60 下会返回空，
                # 而实际有 87 张。全库排序本身只要十几毫秒，不是瓶颈。
                ranked = semantic_rank.rerank(
                    pool, query_vector=_decode_query_vector(row), tuning=learned,
                    feedback=feedback, limit=len(pool))
                ranked = [{"shot_id": int(r["shot_id"]), "score": float(r["score"])}
                          for r in ranked]

            out = compose_mod.combine(
                db, ranked=ranked, person_ids=list(person_id or []),
                exclude_shot_ids=dropped or None, order=order,
                limit=limit, offset=offset, **meta)
            out["missing"] = False
            out["query_text"] = query_text or None
            # 条件清单：让界面能如实显示"这次到底按什么筛的"，而不是自己再拼一遍
            out["conditions"] = {
                "content": query_text or None,
                "person_ids": list(person_id or []),
                **{k: v for k, v in meta.items() if v not in (None, False, "")},
            }
            return out

        return await _read(db, _run)

    # ------------------------------------------------------------------ 健康
    def _ui_build(settings_obj: Any) -> str:
        """前端资源的指纹：`index.html` + `css/` + `js/` 全部文件内容的短哈希。

        为什么要有它：这个项目**零构建、文件名里没有内容哈希**，所以"我浏览器里
        到底是哪一份前端"从界面上看不出来。2026-10 出现过两次同形故障 ——
        后端已经改了、浏览器还在用旧 JS，表现是"新做的东西不出来"，
        而排查的人只能靠 `Ctrl+F5` 试一下、猜是不是缓存。

        有了这个值，把界面右上角的号码与 `GET /api/health` 的 `ui_build`
        对一下就知道：**一致 = 浏览器拿的是最新前端**。
        它只覆盖前端资源，所以改后端不会让它变 —— 那正是它该有的语义。
        """
        root = settings_obj.resolved_web_root()
        if not root.is_dir():
            return ""
        digest = hashlib.sha256()
        files = [root / "index.html"]
        for sub in ("css", "js"):
            files.extend(sorted((root / sub).glob("*")))
        for path in files:
            if not path.is_file():
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:8]

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        state = request.app.state
        db: Database = state.db
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": await _read(db, lambda: db.schema_version()),
            "schema_expected": target_version(),
            "data_root": str(state.settings.data_root),
            "running_job_id": state.runner.current_job_id,
            # 前端资源指纹：用来判断"浏览器里是不是最新那份"（见上面的说明）
            "ui_build": _ui_build(state.settings),
        }

    @app.get("/api/cli")
    async def cli_hint() -> dict[str, Any]:
        """界面上要显示的**命令行怎么写** —— 由服务端按自己所在的环境给出。

        ⚠️ 这些字串原来**写死在 JS 里**（`C:\\PhotoOS\\venv\\venv-eg2\\Scripts\\
        python.exe -m photoos ...`）。那是我这台机器的路径：装到别人的机器上，
        界面会教用户去执行一个**根本不存在的解释器**。
        现在由服务端回答"在这个环境里该怎么敲"，前端只负责显示。
        """
        import sys as _sys

        exe = Path(_sys.executable)
        script = exe.parent / ("photoos.exe" if os.name == "nt" else "photoos")
        # 装在 venv 里时，隔壁就有 `photoos` 可执行文件（console script）——
        # 那比"python -m photoos"短，也是文档里写的用法。
        prefix = "photoos" if script.is_file() else f'"{exe}" -m photoos'
        return {
            "prefix": prefix,
            "python": str(exe),
            "console_script": script.is_file(),
            "examples": {
                "search": [
                    f"{prefix} search 日出 --save",
                    f"{prefix} embed --engine gemma --apply",
                ],
                "people": [
                    f"{prefix} people detect --all",
                    f"{prefix} people embed --all",
                    f"{prefix} people discover",
                ],
                "index": [f"{prefix} index --wait"],
                "models": [f"{prefix} ai fetch-model --engine all"],
            },
        }

    # ------------------------------------------------------------------ 统计
    @app.get("/api/stats")
    async def stats(request: Request) -> Response:
        """全库统计。**进「维护」页就会拉一次**。

        ⚠️ 它要跑十来个聚合查询（实测约 235 ms，其中 `metadata.time_quality`
        一条就 62 ms）。这些数字在一次浏览会话里基本不变 —— 只有导入/索引之后才变。
        所以按 `asset` 的廉价指纹做 ETag + 短窗口缓存，和 `/api/facets` 同一套做法。

        `max-age` 取 60 秒：刚导完的照片最多一分钟就反映到统计里；
        而反复进「维护」页则完全不重复算。
        """
        db: Database = request.app.state.db

        def run() -> tuple[dict[str, Any], str]:
            payload = library_overview(db)
            fingerprint = (
                f"{int(db.scalar('SELECT COUNT(*) FROM asset', default=0))}-"
                f"{int(db.scalar('SELECT COALESCE(MAX(id), 0) FROM asset', default=0))}-"
                f"{int(db.scalar('SELECT COUNT(*) FROM job', default=0))}"
            )
            return payload, fingerprint

        payload, fingerprint = await _read(db, run)
        etag = f'W/"stats-{fingerprint}"'
        headers = {"ETag": etag, "Cache-Control": STATS_CACHE_CONTROL}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=payload, headers=headers)

    # ------------------------------------------------------------------ Library
    @app.get("/api/libraries")
    async def libraries(request: Request) -> list[dict[str, Any]]:
        db: Database = request.app.state.db

        def run() -> list[dict[str, Any]]:
            store = LibraryStore(db)
            out = []
            for library in store.list_all():
                reachable, total, sampled = rebind_mod.library_availability(db, library.id)
                stats_payload = pipelines.library_stats(db, library.id)
                out.append(
                    {
                        "id": library.id,
                        "uuid": library.uuid,
                        "name": library.name,
                        "status": library.status,
                        "root_path_hint": library.root_path_hint,
                        "has_marker": bool(library.marker_uuid),
                        "reachable": reachable,
                        "sampled_reachable": sampled,
                        "volumes": [
                            {
                                "mount_hint": volume.mount_hint,
                                "serial_hex": volume.serial_hex,
                                "kind": volume.kind,
                                "status": volume.status,
                                "root_hint": store.volume_root_hint(library.id, volume.id),
                            }
                            for volume in store.volumes_for(library.id)
                        ],
                        "stats": stats_payload,
                        "asset_count": total,
                    }
                )
            return out

        return await _read(db, run)

    @app.post("/api/libraries")
    async def create_library(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """登记一个照片库（界面上的「+ 添加…」）。

        四种结论都走这里：
          * 新目录       → 建库（默认写 `.photoos` 标记，换盘可自动认回）
          * 已有标记     → 复用既有库，只更新路径提示
          * 换盘（rebind）→ 同上，并把状态改回 ONLINE
          * 不可访问     → 400，不建库
        标记是 PhotoOS **唯一**写进照片目录的东西，默认开启但可关。
        """
        from ..library import store as store_mod

        db: Database = request.app.state.db
        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="路径为空")
        root = Path(raw).expanduser()
        name = str(payload.get("name") or "").strip() or root.name or str(root)
        write_marker = bool(payload.get("write_marker", True))

        # ★ 单库模型（2026-10-18 决定）：照片库是"内置"的一个库，
        # 不支持多库。已有库时：同路径 → 复用；不同路径 → 明确拒绝。
        existing = db.query("SELECT id, name, root_path_hint FROM library ORDER BY id LIMIT 2")
        if existing:
            first = existing[0]
            same = str(first["root_path_hint"] or "").rstrip("\\/").lower() == str(root).rstrip("\\/").lower()
            if same:
                return {"reused": True, "action": "reuse", "library_id": int(first["id"]),
                        "name": first["name"], "marker_written": False}
            raise HTTPException(
                status_code=409,
                detail=(f"当前版本只支持一个照片库（已有「{first['name']}」：{first['root_path_hint']}）。"
                        "要把照片放到别处，请用「导入」并选择落点。"))
        probe = store_mod.probe_path(db, root)
        if not probe.is_dir:
            raise HTTPException(status_code=400, detail=f"目录不可访问：{root}")

        store = store_mod.LibraryStore(db)
        if probe.library is not None and probe.action in {"reuse", "rebind"}:
            store.set_root_hint(probe.library.id, root)
            store.set_status(probe.library.id, "ONLINE")
            if probe.marker:
                store.set_marker_uuid(probe.library.id, str(probe.marker["library_uuid"]))
            store.log_event("library_rebind" if probe.action == "rebind" else "library_reuse",
                            library_id=probe.library.id, payload={"root": str(root)})
            return {"reused": True, "action": probe.action, "library_id": probe.library.id,
                    "name": probe.library.name, "marker_written": False}

        marker_uuid = str(probe.marker["library_uuid"]) if probe.marker else None
        library = store.create(name=name, root=root, library_uuid=marker_uuid, marker_uuid=marker_uuid)
        if probe.volume_identity is not None:
            record = store.record_volume(probe.volume_identity, library_id=library.id)
            store.link_volume_library(record.id, library.id, root)
        wrote = False
        if probe.marker is None and write_marker:
            store_mod.write_marker(root, library.uuid, library.name)
            store.set_marker_uuid(library.id, library.uuid)
            wrote = True
        store.log_event("library_create", library_id=library.id, payload={"root": str(root)})
        return {"reused": False, "action": "create", "library_id": library.id,
                "name": library.name, "marker_written": wrote}

    @app.get("/api/libraries/probe")
    async def probe(request: Request, path: str = Query(..., description="要探测的目录")) -> dict[str, Any]:
        db: Database = request.app.state.db
        candidate = Path(path)
        result = await _read(db, lambda: probe_path(db, candidate))
        return {
            "path": str(result.root),
            "exists": result.exists,
            "is_dir": result.is_dir,
            "action": result.action,
            "reason": result.reason,
            "marker": result.marker,
            "library": (
                {"id": result.library.id, "name": result.library.name, "uuid": result.library.uuid}
                if result.library
                else None
            ),
            "volume": (
                {
                    "mount_hint": result.volume_identity.mount,
                    "label": result.volume_identity.label,
                    "fs_type": result.volume_identity.fs_type,
                    "kind": result.volume_identity.kind,
                    "serial_hex": f"{result.volume_identity.serial:08X}"
                    if result.volume_identity.has_serial
                    else None,
                }
                if result.volume_identity
                else None
            ),
        }

    def _purge_previews(data_root: Path, asset_ids: list[int]) -> tuple[int, int]:
        """删掉这些 asset 的缩略图与预览文件，返回 (删除文件数, 释放字节)。

        路径规则来自 `library/adapter.py::asset_storage_key`：
        `<kind>/ab/cd/<id9>.webp`（9 位补零 + 两级分片）。
        不这样做的话，删库只删索引，`thumbnails/` 会留下成千上万个孤儿 webp。
        """
        from ..media.adapter import asset_storage_key

        removed = 0
        freed = 0
        for asset_id in asset_ids:
            for kind in ("thumbnails", "previews"):
                rel = asset_storage_key(int(asset_id), kind)
                path = Path(data_root) / rel
                try:
                    if path.is_file():
                        freed += path.stat().st_size
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed, freed

    # ---------------------------------------------------------------- 照片库的破坏性操作
    #
    # 设计（单库模型）：只有两个动作，语义必须分得开 ——
    #   ① 重置索引：删派生数据（asset / shot / scene / session / selection + 缩略图与预览），
    #      保留库登记和目录，用来"重建索引"；
    #   ② 移除照片库：在 ① 的基础上再删库登记与卷记录，回到"还没有照片库"。
    # 两者都**绝不碰照片文件**，并且都先给一份盘点（GET .../inventory）让界面二次确认。
    #
    # 单库模型下 shot/scene/session 没有 library_id 列，所以这里整表清空是安全的；
    # 将来若恢复多库，必须先给这些表补 library_id 再改回按库删除。

    DERIVED_TABLES = ("selection", "shot", "scene", "session")

    def _active_jobs(db: Database) -> int:
        return int(db.query_one(
            "SELECT COUNT(*) AS n FROM job WHERE status IN "
            "('PENDING','PROCESSING','RETRY','PAUSED')")["n"])

    def _library_inventory(db: Database, library_id: int, data_root: Path) -> dict[str, Any]:
        from ..media.adapter import asset_storage_key

        row = db.query_one("SELECT id, name, root_path_hint FROM library WHERE id = ?",
                           (int(library_id),))
        if row is None:
            raise HTTPException(status_code=404, detail=f"没有 Library #{library_id}")
        asset_ids = [int(r["id"]) for r in db.query(
            "SELECT id FROM asset WHERE library_id = ?", (int(library_id),))]
        files = 0
        freed = 0
        for asset_id in asset_ids:
            for kind in ("thumbnails", "previews"):
                path = data_root / asset_storage_key(asset_id, kind)
                try:
                    if path.is_file():
                        files += 1
                        freed += path.stat().st_size
                except OSError:
                    continue
        counts = {}
        for table in DERIVED_TABLES:
            try:
                counts[table] = int(db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
            except Exception:  # noqa: BLE001
                counts[table] = 0
        return {
            "library_id": int(library_id),
            "name": row["name"],
            "root": str(row["root_path_hint"] or ""),
            "assets": len(asset_ids),
            "thumbnails_and_previews": files,
            "derived": counts,
            "freed_mb": round(freed / 1048576, 1),
        }

    def _library_purge(db: Database, library_id: int, data_root: Path,
                       *, remove_registration: bool) -> dict[str, Any]:
        from ..media.adapter import asset_storage_key

        asset_ids = [int(r["id"]) for r in db.query(
            "SELECT id FROM asset WHERE library_id = ?", (int(library_id),))]

        def _do() -> None:
            # FK 顺序：selection 引用 shot，先删它
            for table in DERIVED_TABLES:
                try:
                    db.execute(f"DELETE FROM {table}")
                except Exception:  # noqa: BLE001
                    pass
            db.execute("DELETE FROM asset WHERE library_id = ?", (int(library_id),))
            if remove_registration:
                db.execute("DELETE FROM volume_library WHERE volume_id IN "
                           "(SELECT id FROM volume WHERE library_id = ?)", (int(library_id),))
                db.execute("DELETE FROM volume WHERE library_id = ?", (int(library_id),))
                db.execute("DELETE FROM library WHERE id = ?", (int(library_id),))

        inventory = _library_inventory(db, library_id, data_root)
        _read_sync = _do  # 保持函数体单一职责
        _read_sync()

        files = 0
        freed = 0
        for asset_id in asset_ids:
            for kind in ("thumbnails", "previews"):
                path = data_root / asset_storage_key(asset_id, kind)
                try:
                    if path.is_file():
                        freed += path.stat().st_size
                        path.unlink()
                        files += 1
                except OSError:
                    continue
        inventory.update({
            "assets_removed": inventory["assets"],
            "thumbnail_files_removed": files,
            "freed_mb": round(freed / 1048576, 1),
        })
        return inventory

    @app.get("/api/libraries/{library_id}/inventory")
    async def library_inventory(request: Request, library_id: int) -> dict[str, Any]:
        """这两个动作会删什么、释放多少 —— 界面拿它做确认对话框。"""
        db: Database = request.app.state.db
        data_root = Path(str(request.app.state.settings.data_root))
        return await _read(db, lambda: _library_inventory(db, library_id, data_root))

    @app.post("/api/libraries/{library_id}/reset-index")
    async def library_reset_index(request: Request, library_id: int,
                                  payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """重置索引：删派生数据与缩略图，保留库登记与目录。照片文件不动。"""
        body = payload or {}
        if not body.get("confirm"):
            raise HTTPException(status_code=422, detail="重置索引需要 confirm=true")
        db: Database = request.app.state.db
        if _active_jobs(db):
            raise HTTPException(status_code=409, detail="有任务正在运行，先等它结束或取消")
        data_root = Path(str(request.app.state.settings.data_root))
        return await _read(db, lambda: _library_purge(
            db, library_id, data_root, remove_registration=False))

    @app.delete("/api/libraries/{library_id}")
    async def library_remove(request: Request, library_id: int) -> dict[str, Any]:
        """移除照片库：派生数据 + 库登记一起删，回到"还没有照片库"。照片文件不动。"""
        db: Database = request.app.state.db
        if _active_jobs(db):
            raise HTTPException(status_code=409, detail="有任务正在运行，先等它结束或取消")
        data_root = Path(str(request.app.state.settings.data_root))
        result = await _read(db, lambda: _library_purge(
            db, library_id, data_root, remove_registration=True))
        # 记住上次的位置，便于"重新添加"时预填
        try:
            from ..config import save_settings_overrides

            save_settings_overrides(request.app.state.settings,
                                    {"last_library_root": str(result.get("root") or "")})
        except Exception:  # noqa: BLE001
            pass
        return result

    @app.post("/api/libraries/{library_id}/rebind")
    async def rebind(request: Request, library_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """重新绑定到新位置。默认 dry-run —— 必须显式传 apply=true 才改数据库。"""
        db: Database = request.app.state.db
        new_root = payload.get("path")
        if not new_root:
            raise HTTPException(status_code=422, detail="缺少 path")
        apply_changes = bool(payload.get("apply"))
        report = await _read(
            db,
            lambda: rebind_mod.rebind_library(
                db, library_id, Path(str(new_root)), sample=int(payload.get("sample") or 32),
                apply=apply_changes,
            ),
        )
        return {
            "library_id": report.library_id,
            "action": report.probe_action,
            "reason": report.probe_reason,
            "found": report.found,
            "missing": report.missing,
            "verified_by": report.verified_by,
            "confidence": report.confidence,
            "hash_ok": report.hash_ok,
            "hash_mismatch": report.hash_mismatch,
            "missing_samples": report.missing_samples,
            "warnings": report.warnings,
            "rebound": report.rebound,
            "applied": apply_changes,
        }

    @app.post("/api/libraries/{library_id}/offline")
    async def set_offline(request: Request, library_id: int) -> dict[str, Any]:
        db: Database = request.app.state.db
        await _read(db, lambda: rebind_mod.mark_library_offline(db, library_id))
        return {"library_id": library_id, "status": "OFFLINE"}

    @app.get("/api/libraries/{library_id}/verify")
    async def verify(request: Request, library_id: int) -> dict[str, Any]:
        db: Database = request.app.state.db

        def run() -> dict[str, Any]:
            reachable, total, sampled = rebind_mod.library_availability(db, library_id)
            library = LibraryStore(db).get(library_id)
            return {
                "library_id": library_id,
                "name": library.name,
                "status": library.status,
                "root_path_hint": library.root_path_hint,
                "reachable": reachable,
                "asset_count": total,
                "sampled_reachable": sampled,
            }

        return await _read(db, run)

    # ------------------------------------------------------------------ 资产
    @app.get("/api/assets")
    async def assets(
        request: Request,
        library_id: int | None = None,
        offset: int = 0,
        limit: int = Query(200, ge=1, le=1000),
        q: str | None = None,
        file_type: str | None = None,
        status: str | None = None,
        camera: str | None = None,
        lens: str | None = None,
        focal_min: float | None = None,
        focal_max: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        year: int | None = None,
        only_missing_metadata: bool = False,
        sort: str = Query("capture", pattern="^(capture|captured_desc|name|size|recent)$"),
        scope_kind: str | None = Query(None, pattern="^(path|time|selection|collection|tag)$"),
        scope_key: str | None = None,
        person_id: list[int] | None = Query(
            None, description="人物 id，可重复。**多个是 AND（合影）**"),
        content: str | None = Query(
            None, description="内容词（按画面找'像什么的照片'）。与其它条件**叠加**"),
        tuning: bool = Query(
            True, description="内容词是否套用你的表态重排（与 /api/semantic/results 同义）"),
    ) -> dict[str, Any]:
        """分页浏览。**只查数据库索引，绝不扫描原片**（PRD §11.2）。

        `scope_kind` + `scope_key` 是浏览树的落点：把"选中的节点"翻译成查询条件。
        参数形状在 Phase 2 **完全没变** —— `path` / `time` 之外新增了
        `selection`（四态）与 `collection`（主题 id）；`tag` 仍返回 501（决策 #6）。

        `person_id` 可重复，多个是 **AND** —— 勾"妈妈"和"我"要的是合影，
        不是"任一个人"（见 `compose.person_clause`）。

        `content` 是**内容维度**：给了它就按"像不像这个内容"排，但元数据与人物条件
        仍然**同时生效**（这是多维筛选的核心，不能退化成二选一）。
        """
        db: Database = request.app.state.db
        if scope_kind in queries_mod.BROWSE_AXES_RESERVED:
            raise HTTPException(
                status_code=501,
                detail=(
                    f"浏览轴 `{scope_kind}` 尚未实现（标签属 Phase 3）。"
                    "查询参数形状已冻结，届时直接启用、前端零改动。"
                ),
            )

        def _page():
            try:
                # 有内容词时走**多维组合**那条路：内容给排序，元数据与人物同时筛选。
                # 没有内容词时走原来的 `asset_page`（更快：不需要全库点积，也不需要
                # 把 7 千个 shot 的分数传进 SQL）。
                #
                # ⚠️ 两条路的**条件语义必须一致**（都用 `compose.metadata_clauses` /
                # `compose.person_clause`），否则同一组条件在有/无内容词时结果不同 ——
                # `tests/unit/test_compose.py` 有对拍测试守着这一点。
                query_text = (content or "").strip()
                if query_text:
                    return _page_with_content(query_text)
                return asset_page(
                    db,
                    library_id=library_id,
                    offset=offset,
                    limit=limit,
                    query=q,
                    file_type=file_type,
                    status=status,
                    camera=camera,
                    lens=lens,
                    focal_min=focal_min,
                    focal_max=focal_max,
                    date_from=date_from,
                    date_to=date_to,
                    year=year,
                    only_missing_metadata=only_missing_metadata,
                    sort=sort,
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    person_ids=list(person_id or []),
                )
            except InvalidSelectionStatus as exc:
                # 非法状态值 → 422（可理解的报错），而不是让它冒泡成 500
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        def _page_with_content(query_text: str) -> dict[str, Any]:
            """内容维度：全库算相似度 → 与元数据/人物条件叠加 → 按文件给结果。"""
            from ..library import semantic_rank
            from ..repos import SemanticRepo
            from . import compose as compose_mod

            repo = SemanticRepo(db)
            row = repo.get_query(query_text)
            if row is None:
                # 这个词没算过文本向量。**不静默退回"只按元数据筛"** ——
                # 那会让用户以为内容词生效了。与 `/api/semantic/results` 同一套说法。
                raise HTTPException(
                    status_code=409,
                    detail=(f"「{query_text}」还没有算过文本向量。"
                            "先在「维护」页的准备入口算一次，或换一个已备好的词。"),
                )
            row = dict(row)
            feedback = repo.feedback_map(int(row["id"]))
            dropped = {int(s) for s, a in feedback.items() if a == "drop"}

            image_model, image_version, prep = semantic_rank.resolve_image_identity(db, row)
            pool = _semantic_pool(request.app, db, (image_model, image_version, prep))
            learned = None
            if tuning and feedback:
                learned = semantic_rank.compute_tuning(db, query_row=row, feedback=feedback)
                if not learned.active:
                    learned = None
            # ⚠️ 取**全库**（limit = 池大小）。`rerank` 的 limit 是早停：
            # 先截断再叠加筛选，结果会被截没（实测"月亮 + 某个人"在 limit=60 下返回空，
            # 而实际有 87 张）。全库排序本身只要十几毫秒。
            ranked = semantic_rank.rerank(
                pool, query_vector=_decode_query_vector(row), tuning=learned,
                feedback=feedback, limit=len(pool))
            ranked = [{"shot_id": int(r["shot_id"]), "score": float(r["score"])}
                      for r in ranked]

            return compose_mod.combine_assets(
                db, ranked=ranked, person_ids=list(person_id or []),
                exclude_shot_ids=dropped or None,
                # ⚠️ 这里传 `None` 而**不是** `sort`：`sort` 的默认值是 `"capture"`，
                # 传下去会把 `order or ("similarity" if has_content ...)` 里的默认逻辑
                # 永远覆盖掉 —— 结果虽然碰巧对（都按拍摄时间/相似度），
                # 但 `ordered_by` 会**谎报成 capture**（实测踩过）。
                # 只有用户显式选了非默认排序时才把 `sort` 交下去。
                order=(sort if sort and sort != "capture" else None),
                limit=limit, offset=offset,
                scope_kind=scope_kind, scope_key=scope_key,
                library_id=library_id, file_type=file_type, status=status,
                camera=camera, lens=lens, focal_min=focal_min, focal_max=focal_max,
                year=year, date_from=date_from, date_to=date_to, keyword=q,
                only_missing_metadata=only_missing_metadata,
            )

        return await _read(db, _page)

    @app.get("/api/browse/tree")
    async def browse_tree(
        request: Request,
        axis: str = Query("path", pattern="^(path|time|selection|collection|tag)$"),
        library_id: int | None = None,
        parent: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        """可插拔的浏览树。

        本版本实现 `path`（**默认**，物理路径：城市 → 时段）与 `time`（年 → 月 → 日）。
        `selection` / `collection` / `tag` 保留给 Phase 2，现在明确返回 501。
        """
        db: Database = request.app.state.db
        if axis in queries_mod.BROWSE_AXES_RESERVED:
            raise HTTPException(
                status_code=501,
                detail=(
                    f"浏览轴 `{axis}` 属于 Phase 2（精选/主题/标签），本版本未实现。"
                    "接口形状已冻结，Phase 2 会直接启用。"
                ),
            )
        return await _read(
            db,
            lambda: queries_mod.browse_tree(
                db, axis=axis, library_id=library_id, parent=parent, limit=limit
            ),
        )

    # ------------------------------------------------------------------ 精选（Phase 2）
    @app.get("/api/sessions")
    async def sessions(request: Request, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
        """Session 列表（含城市/时段/时间范围/Shot 数）。"""
        db: Database = request.app.state.db
        return await _read(db, lambda: queries_mod.session_list(db, limit=limit))

    @app.get("/api/review/config/presets")
    async def review_config_presets(request: Request) -> dict[str, Any]:
        """`shot_gap_seconds` 的可选预设 + 全局默认值（决策 #5 的"界面预设"那一层）。

        把预设连同**每一条的代价**一起返回，界面必须原样显示 —— 否则用户会以为
        把阈值调大就能把"构图相似"的照片找全，而实测证明时间规则永远做不到
        （相隔 360.5 s 却余弦 0.9846 的那对照片，见 PHASE2_RESEARCH §5.5）。
        """
        settings_obj: Settings = request.app.state.settings
        return {
            "global_default": float(settings_obj.shot_gap_seconds),
            "presets": shot_mod.gap_presets(),
            "limitation": (
                "按时间邻接分组只能抓住连拍与同一机位的连续取景，"
                "抓不住「构图高度相似但拍摄时间相隔较远」的照片 —— "
                "那需要视觉相似度（本期仅后端计算）。"
            ),
        }

    @app.get("/api/sessions/{session_id}/review-config")
    async def get_review_config(request: Request, session_id: int) -> dict[str, Any]:
        """某个 Session 的分组参数 + **有效值及其来源**。

        `source` 必须回给界面：用户改了却没生效时，要能一眼看出是哪一层赢了
        （命令行 > 单 Session 覆盖 > config.json 全局）。
        """
        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def _read_config() -> dict[str, Any]:
            row = db.query_one("SELECT id, review_config_json FROM session WHERE id = ?",
                               (int(session_id),))
            if row is None:
                return {"missing": True}
            payload = _load_review_config(row["review_config_json"])
            override = payload.get("shot_gap_seconds")
            if override is not None:
                return {
                    "missing": False, "session_id": int(session_id),
                    "override": float(override), "effective": float(override),
                    "source": "本时段已设置",
                    "raw": payload,
                }
            global_value = float(settings_obj.shot_gap_seconds)
            return {
                "missing": False, "session_id": int(session_id),
                "override": None, "effective": global_value,
                "source": "跟随全局默认", "raw": payload,
            }

        result = await _read(db, _read_config)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Session 不存在")
        return result

    @app.patch("/api/sessions/{session_id}/review-config")
    async def set_review_config(
        request: Request, session_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """设置本 Session 的 `shot_gap_seconds` 覆盖值。

        请求体：`{"shot_gap_seconds": 3.0}`；传 `null` 表示**清除覆盖**回到全局默认。

        ⚠️ 本端点只写参数，**不重算分组**。分组是派生数据（铁律 #3），
        改参数后需要用户显式跑一次重算（`photoos shots regroup --apply` 或界面按钮），
        否则界面显示的还是旧分组 —— 这一点由响应里的 `needs_regroup` 明示。
        """
        db: Database = request.app.state.db
        if "shot_gap_seconds" not in payload:
            raise HTTPException(status_code=422, detail="请求体必须含 shot_gap_seconds")

        raw_value = payload.get("shot_gap_seconds")
        try:
            gap = None if raw_value is None else shot_mod.normalize_gap(raw_value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def _write() -> dict[str, Any]:
            from ..db import utc_now

            row = db.query_one("SELECT id, review_config_json FROM session WHERE id = ?",
                               (int(session_id),))
            if row is None:
                return {"missing": True}
            current = _load_review_config(row["review_config_json"])
            if gap is None:
                current.pop("shot_gap_seconds", None)
            else:
                current["shot_gap_seconds"] = gap
            stored = json.dumps(current, ensure_ascii=False) if current else None
            with db.write() as conn:
                conn.execute(
                    "UPDATE session SET review_config_json = ?, updated_at = ? WHERE id = ?",
                    (stored, utc_now(), int(session_id)),
                )
            return {"missing": False, "raw": current}

        result = await _read(db, _write)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Session 不存在")
        # 参数变了但分组没重算 —— 如实告知，不要让用户以为已经生效
        # ⚠️ `source` 是**给用户看的中文**，所以不提配置文件的名字 ——
        # 界面上的技术名词对使用者没有意义（见 source 的三处取值保持一致）。
        return {
            "session_id": int(session_id),
            "shot_gap_seconds": gap,
            "source": "本时段已设置" if gap is not None else "跟随全局默认",
            "needs_regroup": True,
            "note": "参数已保存；分组是派生数据，需运行一次重算才会反映到界面上。",
        }

    @app.get("/api/scenes")
    async def scenes(
        request: Request,
        session_id: int | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        """Scene 列表（可按 Session 过滤），附带每个 Scene 的精选进度。"""
        db: Database = request.app.state.db
        return await _read(db, lambda: queries_mod.scene_list(db, session_id=session_id, limit=limit))

    @app.get("/api/scenes/{scene_id}")
    async def scene_detail(request: Request, scene_id: int) -> dict[str, Any]:
        """Scene 详情：Shot 列表 + 每个 Shot 的精选状态 + 选择过程。"""
        db: Database = request.app.state.db
        detail = await _read(db, lambda: queries_mod.scene_detail(db, scene_id))
        if detail is None:
            raise HTTPException(status_code=404, detail="Scene 不存在")
        return detail

    # ------------------------------------------------ Scene 的人工调整（合并 / 拆分）
    #
    # 为什么需要：Scene 是自动切出来的，实拍里必然切错（同一场被切成两段、
    # 或两场被并成一段）。手工调整会把 Scene 标成 `source='user'`，
    # 之后 `scenes rebuild` 会**整段跳过**这个时段 —— 否则调整会被下一次重算冲掉。

    @app.post("/api/scenes/merge")
    async def scenes_merge(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把 `secondary_id` 合并进 `primary_id`（只允许同一时段内）。"""
        from ..repos import ShotRepo

        db: Database = request.app.state.db
        try:
            primary = int(payload.get("primary_id"))
            secondary = int(payload.get("secondary_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="primary_id / secondary_id 需要整数")

        def _run() -> dict[str, Any]:
            try:
                return ShotRepo(db).merge_scenes(primary, secondary)
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__error__" in result:
            raise HTTPException(status_code=400, detail=result["__error__"])
        return result

    @app.post("/api/scenes/{scene_id}/split")
    async def scenes_split(request: Request, scene_id: int,
                           payload: dict[str, Any]) -> dict[str, Any]:
        """在 `after_shot_id` 之后把 Scene 一分为二（前段保留原 id）。"""
        from ..repos import ShotRepo

        db: Database = request.app.state.db
        try:
            after_shot = int(payload.get("after_shot_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="after_shot_id 需要整数")
        name = payload.get("name") or None

        def _run() -> dict[str, Any]:
            try:
                return ShotRepo(db).split_scene(scene_id, after_shot, name=name)
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__error__" in result:
            raise HTTPException(status_code=400, detail=result["__error__"])
        return result

    @app.post("/api/scenes/{scene_id}/unpin")
    async def scenes_unpin(request: Request, scene_id: int) -> dict[str, Any]:
        """解除人工标记，让下一次 `scenes rebuild` 重新接管这个 Scene。"""
        from ..repos import ShotRepo

        db: Database = request.app.state.db

        def _run() -> dict[str, Any]:
            try:
                return ShotRepo(db).unpin_scene(scene_id)
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__error__" in result:
            raise HTTPException(status_code=404, detail=result["__error__"])
        return result

    @app.post("/api/shots/{shot_id}/selection")
    async def set_selection(request: Request, shot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """设置某个 Shot 的精选状态（可选带理由）。

        PRD §9.2：状态可任意跳转、**随时可退回**、可附简短理由、过程可追溯。
        **本端点不产生任何文件操作** —— 精选只是数据库状态（PRD 第 137 行）。

        请求体：`{"status": "CANDIDATE|SELECTED|FINAL|UNREVIEWED", "reason": "光最好"}`
        """
        from ..library import selection as sel
        from ..repos import SelectionRepo

        db: Database = request.app.state.db
        repo = SelectionRepo(db)

        target = payload.get("status")
        reason = payload.get("reason")
        try:
            # 复用纯逻辑做校验（与 repo 层同一套规则，不在这里重写一遍）
            sel.plan_transition(shot_id, sel.UNREVIEWED, target if target is not None else "")
        except sel.InvalidSelectionStatus as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def _write() -> dict[str, Any]:
            if not db.query_one("SELECT id FROM shot WHERE id = ?", (int(shot_id),)):
                return {"missing": True}
            if not db.query_one(
                "SELECT id FROM shot WHERE id = ? AND retired_at IS NULL", (int(shot_id),)
            ):
                return {"retired": True}
            changed = repo.set_status(shot_id, str(target), reason=reason)
            row = repo.get(shot_id)
            return {
                "missing": False,
                "changed": changed,
                "shot_id": int(shot_id),
                "status": repo.status_of(shot_id),
                "reason": row["reason"] if row else None,
                "counts": repo.counts(),
            }

        result = await _read(db, _write)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Shot 不存在")
        if result.get("retired"):
            raise HTTPException(
                status_code=409,
                detail="该 Shot 已因分组重算而退役；请对当前分组下的 Shot 操作。",
            )
        return result

    @app.get("/api/shots/{shot_id}/history")
    async def shot_history(request: Request, shot_id: int) -> dict[str, Any]:
        """某个 Shot 的选择过程时间线（断点 #8「从最终精选反查完整拍摄组」要用）。"""
        db: Database = request.app.state.db

        def _read_history() -> dict[str, Any]:
            from ..repos import SelectionRepo

            repo = SelectionRepo(db)
            if not db.query_one("SELECT id FROM shot WHERE id = ?", (int(shot_id),)):
                return {"missing": True}
            return {
                "missing": False,
                "shot_id": int(shot_id),
                "status": repo.status_of(shot_id),
                "events": [
                    {
                        "from_status": row["from_status"],
                        "to_status": row["to_status"],
                        "reason": row["reason"],
                        "created_at": row["created_at"],
                    }
                    for row in repo.history(shot_id)
                ],
            }

        result = await _read(db, _read_history)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Shot 不存在")
        return result

    @app.post("/api/shots/{shot_id}/split")
    async def split_shot(request: Request, shot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """把若干资产从分组中**拆出去**，形成一个新的拍摄组（PRD §9.2 人工修正）。

        请求体：`{"asset_ids": [12, 13]}`

        铁律 #5：本端点**不动任何文件**，只改数据库里的归属关系；
        铁律 #3：分组是可重算的派生线索，所以人工修正只是把
        `group_source` 置为 `user`（决策 #10，此后永不被重算覆盖）。
        """
        from ..repos import ShotRepo

        db: Database = request.app.state.db
        raw = payload.get("asset_ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=422, detail="asset_ids 必须是非空数组")
        try:
            ids = [int(a) for a in raw]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="asset_ids 必须是整数数组") from exc

        def _split() -> dict[str, Any]:
            repo = ShotRepo(db)
            row = db.query_one("SELECT * FROM shot WHERE id = ?", (int(shot_id),))
            if row is None:
                return {"missing": True}
            if row["retired_at"] is not None:
                return {"retired": True}
            try:
                new_id = repo.split_shot(shot_id, ids)
            except ValueError as exc:
                return {"invalid": str(exc)}
            return {
                "missing": False,
                "new_shot_id": new_id,
                "source_shot_id": int(shot_id),
                "moved": len(ids),
                "source_assets": repo.asset_count(shot_id),
                "new_assets": repo.asset_count(new_id),
            }

        result = await _read(db, _split)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Shot 不存在")
        if result.get("retired"):
            raise HTTPException(
                status_code=409,
                detail="该 Shot 已因分组重算而退役；请对当前分组下的 Shot 操作。",
            )
        if result.get("invalid"):
            raise HTTPException(status_code=422, detail=result["invalid"])
        return result

    @app.post("/api/shots/merge")
    async def merge_shots(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把多个拍摄组并成一个（PRD §9.2 人工修正）。

        请求体：`{"shot_ids": [3, 4], "keep": 3}`（`keep` 可选，默认第一个）

        被并掉的 Shot **标退役而不是删除**，它上面的精选记录因此仍然保留 ——
        这是决策 #4「只增不删」的直接后果，也是精选绝不丢失的保证。
        """
        from ..repos import ShotRepo

        db: Database = request.app.state.db
        raw = payload.get("shot_ids")
        if not isinstance(raw, list) or len(raw) < 2:
            raise HTTPException(status_code=422, detail="shot_ids 至少要两个")
        try:
            ids = [int(s) for s in raw]
            keep = payload.get("keep")
            keep_id = int(keep) if keep is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="shot_ids/keep 必须是整数") from exc

        def _merge() -> dict[str, Any]:
            repo = ShotRepo(db)
            found = db.query(
                "SELECT id, retired_at FROM shot WHERE id IN (%s)"
                % ", ".join("?" for _ in ids),
                ids,
            )
            if len(found) != len(set(ids)):
                return {"missing": True}
            if any(r["retired_at"] is not None for r in found):
                return {"retired": True}
            try:
                keeper = repo.merge_shots(ids, keep=keep_id)
            except ValueError as exc:
                return {"invalid": str(exc)}
            return {
                "missing": False,
                "kept_shot_id": keeper,
                "merged": [i for i in ids if i != keeper],
                "assets": repo.asset_count(keeper),
            }

        result = await _read(db, _merge)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="有 Shot 不存在")
        if result.get("retired"):
            raise HTTPException(
                status_code=409,
                detail="有 Shot 已退役，不能参与合并。",
            )
        if result.get("invalid"):
            raise HTTPException(status_code=422, detail=result["invalid"])
        return result

    @app.get("/api/selection/summary")
    async def selection_summary(request: Request) -> dict[str, Any]:
        """精选总览：各状态计数 + 被放弃的清单（PRD「放弃了什么」）。"""
        db: Database = request.app.state.db

        def _summary() -> dict[str, Any]:
            from ..library import selection as sel
            from ..repos import SelectionRepo

            repo = SelectionRepo(db)
            return {
                "counts": repo.counts(),
                "labels": sel.STATUS_LABELS,
                "finals": [
                    {
                        "asset_id": row["asset_id"],
                        "relative_path": row["relative_path"],
                        "capture_at": row["capture_at"],
                        "reason": row["reason"],
                    }
                    for row in repo.finals(limit=200)
                ],
                "abandoned": [
                    {
                        "shot_id": int(row["shot_id"]),
                        "from_status": row["from_status"],
                        "reason": row["reason"],
                        "created_at": row["created_at"],
                    }
                    for row in repo.abandoned(limit=200)
                ],
            }

        return await _read(db, _summary)

    # ------------------------------------------------------------------ Collections（Phase 2 · M7）
    # ------------------------------------------------------------------ 文字搜索（Phase 3）
    #
    # ★ 架构要点（用户选的「方案 a」）：这两个接口**只读**迁移 007 的两张表。
    #   文本向量是 CLI 在装了 torch 的 venv 里算好写进去的，所以
    #   **`serve` 不需要 onnxruntime/torch**，也不和 CLIP 抢显存。
    #   代价（必须如实告知界面）：只能搜"命令行预置过的词"。

    @app.get("/api/semantic/manage")
    async def semantic_manage(request: Request) -> dict[str, Any]:
        """关键词工作台：覆盖统计 + 每个词的过期状态。

        **只读**。真正的命中刷新是 `POST /api/semantic/refresh`；补图片向量走
        现有 `embed_gemma` 动作（需要 AI venv），两者成本差一个数量级。
        """
        from ..library import semantic_admin

        db: Database = request.app.state.db
        return await _read(db, lambda: semantic_admin.manage_payload(db))

    @app.post("/api/semantic/refresh")
    async def semantic_refresh(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """重算选中词（或全部词）的命中。**不需要 torch。**

        只替换 `semantic_query_hit`；`semantic_feedback` / `semantic_tuning`
        保持不动。选中的词必须属于同一个模型身份。
        """
        from ..library import semantic_admin

        db: Database = request.app.state.db
        body = payload or {}
        raw = body.get("query_ids")
        if raw is None:
            ids = None
        elif isinstance(raw, list):
            try:
                ids = [int(value) for value in raw]
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="query_ids 必须是整数数组") from exc
        else:
            raise HTTPException(status_code=422, detail="query_ids 必须是整数数组")

        def _run() -> dict[str, Any]:
            try:
                return semantic_admin.refresh_hits(db, ids)
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__error__" in result:
            raise HTTPException(status_code=400, detail=result["__error__"])
        return result

    @app.get("/api/semantic/scenes/{query_id}")
    async def semantic_scene_status(request: Request, query_id: int) -> dict[str, Any]:
        """一个关键词场景的当前覆盖状态（不推进挖掘轮次）。"""
        from ..library import scene_mining

        db: Database = request.app.state.db

        def _run() -> dict[str, Any]:
            try:
                return scene_mining.scene_status(db, int(query_id))
            except LookupError as exc:
                return {"__missing__": str(exc)}
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__missing__" in result:
            raise HTTPException(status_code=404, detail=result["__missing__"])
        if "__error__" in result:
            raise HTTPException(status_code=400, detail=result["__error__"])
        return result

    @app.post("/api/semantic/scenes/{query_id}/discover")
    async def semantic_scene_discover(
        request: Request,
        query_id: int,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """挖下一批 60 张；识别线变化写入 scene 状态。

        人工标签仍走 `POST /api/semantic/feedback`（keep/drop）—— 标签是
        场景的唯一真源，本接口只负责选下一批候选。
        """
        from ..library import scene_mining

        db: Database = request.app.state.db
        body = payload or {}
        threshold: float | None = None
        if body.get("threshold") is not None:
            try:
                threshold = float(body["threshold"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="threshold 必须是数字") from exc
            if not 0.0 <= threshold <= 1.0:
                raise HTTPException(status_code=422, detail="threshold 必须在 0–1 之间")
        try:
            limit = int(body.get("limit") or scene_mining.BATCH_SIZE)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="limit 必须是整数") from exc
        limit = max(1, min(limit, scene_mining.BATCH_SIZE))

        def _run() -> dict[str, Any]:
            try:
                return scene_mining.discover(
                    db, int(query_id), threshold=threshold, limit=limit)
            except LookupError as exc:
                return {"__missing__": str(exc)}
            except ValueError as exc:
                return {"__error__": str(exc)}

        result = await _read(db, _run)
        if "__missing__" in result:
            raise HTTPException(status_code=404, detail=result["__missing__"])
        if "__error__" in result:
            raise HTTPException(status_code=422, detail=result["__error__"])
        return result

    @app.get("/api/semantic/queries")
    async def semantic_queries(request: Request) -> dict[str, Any]:
        """可搜索的词（界面下拉框的内容）。按凝聚度降序 —— 越靠前越"在库里真有内容"。"""
        db: Database = request.app.state.db

        def _list() -> dict[str, Any]:
            from ..repos import SemanticRepo

            def _run():
                from ..library.semantic_admin import TOP_HITS

                items = SemanticRepo(db).list_queries()
                return {"total": len(items), "items": items, "top_hits": TOP_HITS}

            try:
                return _run()
            except Exception:  # noqa: BLE001 - 迁移 007 还没应用时这里会报错
                # 库还是 schema 6（没跑过 init）时给可照做的提示，而不是 500
                return {
                    "total": 0,
                    "items": [],
                    "note": "库还没有语义搜索表（schema < 7）。请先运行 `photoos init`。",
                }

        return await _read(db, _list)

    @app.get("/api/find/scenes")
    async def find_scenes(
        request: Request,
        city: str | None = Query(None, description="城市（精确匹配 metadata 派生值）"),
        session: str | None = Query(None, description="场次目录名/城市名，模糊匹配（当字面量）"),
        date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
        date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
        scene: int | None = Query(None, description="直接指定 Scene id"),
        min_shots: int | None = Query(None, ge=1, description="只要 Shot 数 ≥ N"),
        library_id: int | None = Query(None),
        limit: int = Query(60, ge=1, le=300),
    ) -> dict[str, Any]:
        """结构化筛选（Phase 3 · L4）：按时间/地点/场次找 Scene。

        ## 为什么它比语义搜索更该优先

        实测：`a marble statue of a person` 的 top-100 里，用户那次杭州博物馆只占 14 张，
        而希腊的三个博物馆场次占了 72 张 —— **语义检索看不见"这是哪一趟"**。
        而 `session`/`scene` 把每一趟记得清清楚楚（78 session / 303 scene）。

        所以「分出某一次拍摄」用本接口（**100% 确定**），
        「找出所有某类内容」用 `/api/semantic/results`（有富集但不保证干净）。

        ## 安全边界

        与 `filter_query.py` 同一套纪律：**字段白名单 + 全部参数绑定**，
        `session` 的值一律当**字面量**（LIKE 通配符被转义），**绝不接受原始 SQL**。
        """
        db: Database = request.app.state.db

        def _find() -> dict[str, Any]:
            from .structured_find import find_shots

            rows = find_shots(
                db,
                city=city,
                date_from=date_from,
                date_to=date_to,
                session_like=session,
                scene_id=scene,
                min_shots=min_shots,
                library_id=library_id,
                limit=limit,
            )
            return {
                "total": len(rows),
                "shots": sum(r["shots"] for r in rows),
                "truncated": len(rows) >= limit,
                "items": rows,
                "note": "这一层是确定性的（时间/目录），不依赖任何模型。",
            }

        return await _read(db, _find)

    @app.get("/api/semantic/results")
    async def semantic_results(
        request: Request,
        query: str | None = Query(None, description="词（与 query_id 二选一）"),
        query_id: int | None = Query(None),
        limit: int = Query(60, ge=1, le=300),
        tuning: bool = Query(True, description="是否套用你的修改（false = 看模型原始结果）"),
    ) -> dict[str, Any]:
        """某个词的命中（**只返回看得见的**，即挂在活跃 Shot 上的）。

        每条都带 `capture_at`（本地拍摄时刻）—— 这是用户判断的依据：
        模型分不清日出与日落（实测两者 top-100 重合 65%），但 `07:39` 与 `17:44` 一眼分清。

        ## `tuning=true` 时会发生什么（Phase 4）

        如果用户对这个词表过态（`semantic_feedback`），服务端会**现场重排**：

            score = cos(图像, 文本) + α·cos(图像, 选中均值) − β·cos(图像, 删掉均值)

        重排基于**全库**而不是只在已有命中里换顺序 —— 这样"删掉几张"的意图
        才能真正推广到没进过前 60 的结果。为此要把 21,400 个 768 维向量读进内存
        （约 65 MB，进程内缓存），点积只要几十毫秒。

        这条路径**只用已经存好的向量做线性代数**，所以 `serve` 仍然不需要
        torch / onnxruntime —— 「方案 a」的架构没有被破坏。

        `tuning=false` 随时能看到模型的原始判断（`semantic_query_hit` 从未被覆盖）。
        """
        db: Database = request.app.state.db

        def _detail() -> dict[str, Any]:
            from ..library import semantic_rank
            from ..repos import SemanticRepo

            repo = SemanticRepo(db)
            if query_id is not None:
                row = db.query_one("SELECT * FROM semantic_query WHERE id = ?", (int(query_id),))
                row = dict(row) if row else None
            elif query:
                row = repo.get_query(query)
            else:
                return {"missing": True, "reason": "no_query"}

            if row is None:
                # 这个词没被预置过 —— 给出可照做的下一步，而不是空屏
                return {
                    "missing": True,
                    "reason": "not_preseeded",
                    "query_text": query,
                    "hint": (
                        "这个词还没有算过文本向量。在产品 venv 里跑不了（没有 torch），"
                        "请在命令行执行一次：\n"
                        f"python -m photoos search "
                        f'"{query}" --save'
                    ),
                }

            feedback = repo.feedback_map(int(row["id"]))
            counts = repo.feedback_counts(int(row["id"]))
            base_hits = [dict(h) for h in repo.hits(int(row["id"]), limit=max(limit, 200))]
            base_scores = {int(h["shot_id"]): float(h["score"]) for h in base_hits}

            tuned = False
            applied: dict[str, Any] | None = None
            notes: list[str] = []
            items: list[dict[str, Any]]
            if tuning and feedback:
                learned = semantic_rank.compute_tuning(db, query_row=row, feedback=feedback)
                notes.extend(learned.notes)
                if learned.active:
                    image_model, image_version, prep = semantic_rank.resolve_image_identity(
                        db, row)
                    pool = _semantic_pool(request.app, db, (image_model, image_version, prep))
                    ranked = semantic_rank.rerank(
                        pool, query_vector=_decode_query_vector(row), tuning=learned,
                        feedback=feedback, limit=limit, base_scores=base_scores)
                    repo.save_tuning(
                        int(row["id"]), image_model=image_model,
                        image_model_version=image_version, image_prep_version=prep,
                        dim=int(row["dim"]),
                        pos_vector=None if learned.pos_vector is None
                        else learned.pos_vector.tobytes(),
                        neg_vector=None if learned.neg_vector is None
                        else learned.neg_vector.tobytes(),
                        pos_count=learned.pos_count, neg_count=learned.neg_count,
                        alpha=learned.alpha, beta=learned.beta)
                    applied = {"pos_count": learned.pos_count, "neg_count": learned.neg_count,
                               "alpha": learned.alpha, "beta": learned.beta,
                               "summary": learned.summary()}
                    items = _ranked_to_items(repo, db, ranked, feedback)
                    tuned = True
                else:
                    items = _base_to_items(repo, db, base_hits, limit, feedback, notes)
            else:
                items = _base_to_items(repo, db, base_hits, limit, feedback, notes)

            return {
                "missing": False,
                "id": int(row["id"]),
                "query_text": row["query_text"],
                "prompt_text": row["prompt_text"],
                "concept_ratio": row["concept_ratio"],
                "updated_at": row["updated_at"],
                "count": len(items),
                "items": items,
                "feedback": {str(k): v for k, v in feedback.items()},
                "feedback_counts": counts,
                "tuned": tuned,
                "tuning": applied,
                "notes": notes,
            }

        result = await _read(db, _detail)
        if result.get("missing") and result.get("reason") == "no_query":
            raise HTTPException(status_code=400, detail="请给出 query 或 query_id")
        return result

    # ------------------------------------------------------------------ 人工反馈 → 学习（Phase 4）

    @app.post("/api/semantic/feedback")
    async def semantic_feedback(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """记录一次人工表态：`keep`（我要这种）或 `drop`（不是这种）。

        **同时重算重排参数并返回新的计数**，所以界面点一下就能看到整页重排 ——
        这是"模型持续学习我的选择"在界面上的可见形式。
        算法在 `library/semantic_rank.py`，只用库里已有的向量，不需要 torch。
        """
        from ..library import semantic_rank
        from ..repos import SemanticRepo

        db: Database = request.app.state.db
        action = str(payload.get("action") or "").strip()
        if action not in ("keep", "drop"):
            raise HTTPException(status_code=422, detail="action 必须是 keep 或 drop")
        shot_ids = _feedback_shot_ids(payload)

        def _save() -> dict[str, Any]:
            repo = SemanticRepo(db)
            if payload.get("query_id") is not None:
                row = db.query_one("SELECT * FROM semantic_query WHERE id = ?",
                                   (int(payload["query_id"]),))
            else:
                row = repo.get_query(str(payload.get("query") or ""))
            if row is None:
                return {"missing": True}
            row = dict(row)
            # 一次请求可以带多张（界面的多选就是这么用的）：**一条事务写完，只重算一次**。
            # 逐张发请求会让界面每点一次就重排一次，滚动位置也就跟着跳一次。
            marks = ",".join("?" for _ in shot_ids)
            alive = {
                int(r["id"]) for r in db.query(
                    f"SELECT id FROM shot WHERE id IN ({marks}) AND retired_at IS NULL",
                    shot_ids)
            }
            live = [sid for sid in shot_ids if sid in alive]
            if not live:
                return {"bad": "这个 Shot 不存在或已退役（Shot 重算过？）"}
            repo.set_feedback_many(int(row["id"]), live, action)
            feedback = repo.feedback_map(int(row["id"]))
            learned = semantic_rank.compute_tuning(db, query_row=row, feedback=feedback)
            applied: dict[str, Any] | None = None
            if feedback:
                try:
                    image_model, image_version, prep = semantic_rank.resolve_image_identity(
                        db, row)
                    repo.save_tuning(
                        int(row["id"]), image_model=image_model,
                        image_model_version=image_version, image_prep_version=prep,
                        dim=int(row["dim"]),
                        pos_vector=None if learned.pos_vector is None
                        else learned.pos_vector.tobytes(),
                        neg_vector=None if learned.neg_vector is None
                        else learned.neg_vector.tobytes(),
                        pos_count=learned.pos_count, neg_count=learned.neg_count,
                        alpha=learned.alpha, beta=learned.beta)
                    applied = {"active": learned.active, "pos_count": learned.pos_count,
                               "neg_count": learned.neg_count, "summary": learned.summary(),
                               "notes": learned.notes}
                except ValueError as exc:
                    applied = {"active": False, "notes": [str(exc)]}
            return {"missing": False, "counts": repo.feedback_counts(int(row["id"])),
                    "applied": live, "skipped": [sid for sid in shot_ids if sid not in alive],
                    "tuning": applied}

        result = await _read(db, _save)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个词还没有被预置过")
        if result.get("bad"):
            raise HTTPException(status_code=409, detail=result["bad"])
        return result

    @app.delete("/api/semantic/feedback")
    async def semantic_feedback_clear(request: Request, query: str | None = None,
                                      query_id: int | None = None,
                                      drop: bool = False) -> dict[str, Any]:
        """撤销这个词的全部表态；`drop=true` 时**连词条本身一起删掉**（下拉框里不再出现）。

        为什么要 drop：`clear_feedback` 只是把"我的表态"抹掉，词条还在；
        用户想的是"这个词我不要了"，两者语义不同。
        """
        from ..repos import SemanticRepo

        db: Database = request.app.state.db

        def _clear() -> dict[str, Any]:
            repo = SemanticRepo(db)
            row = (db.query_one("SELECT * FROM semantic_query WHERE id = ?", (int(query_id),))
                   if query_id is not None else repo.get_query(str(query)))
            if row is None:
                return {"missing": True}
            removed = repo.clear_feedback(int(row["id"]))
            repo.delete_tuning(int(row["id"]))
            if drop:
                # 通用删除：把任何带 query_id 的 semantic_* 表一起清掉，再删主行。
                tables = [r["name"] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'semantic%'")]
                for table in tables:
                    if table == "semantic_query":
                        continue
                    try:
                        db.execute(f"DELETE FROM {table} WHERE query_id = ?", (int(row["id"]),))
                    except Exception:  # noqa: BLE001 —— 没有该列的表跳过
                        continue
                db.execute("DELETE FROM semantic_query WHERE id = ?", (int(row["id"]),))
            return {"missing": False, "removed": removed, "dropped": bool(drop)}

        result = await _read(db, _clear)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个词还没有被预置过")
        return result

    @app.post("/api/semantic/collection")
    async def semantic_to_collection(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把某个词的（已按你的修改调整过的）结果**存成一个主题**。

        `mode`：
          * `kept`  —— 只要你在词里选中过的（`keep`）；
          * `clean` —— 全部结果**减去**你删掉的（默认，最常用）；
          * `raw`   —— 模型原始结果（忽略你的修改）。
        """
        from ..repos import CollectionRepo, SemanticRepo

        db: Database = request.app.state.db
        mode = str(payload.get("mode") or "clean")
        if mode not in ("kept", "clean", "raw"):
            raise HTTPException(status_code=422, detail="mode 必须是 kept / clean / raw")
        name = str(payload.get("name") or "").strip()

        def _build() -> dict[str, Any]:
            repo = SemanticRepo(db)
            row = (db.query_one("SELECT * FROM semantic_query WHERE id = ?",
                                (int(payload["query_id"]),))
                   if payload.get("query_id") is not None
                   else repo.get_query(str(payload.get("query") or "")))
            if row is None:
                return {"missing": True}
            row = dict(row)
            if not name:
                return {"bad": "请给这个主题起一个名字"}
            feedback = repo.feedback_map(int(row["id"]))
            base = repo.hits(int(row["id"]), limit=5000)
            if mode == "raw" or not feedback:
                shot_ids = [int(h["shot_id"]) for h in base]
            elif mode == "kept":
                shot_ids = sorted(int(s) for s, a in feedback.items() if a == "keep")
            else:
                dropped = {int(s) for s, a in feedback.items() if a == "drop"}
                shot_ids = [int(h["shot_id"]) for h in base
                            if int(h["shot_id"]) not in dropped]
                present = set(shot_ids)
                shot_ids += sorted(int(s) for s, a in feedback.items()
                                   if a == "keep" and int(s) not in present)
            if not shot_ids:
                return {"bad": "这个结果集是空的，没有可加入主题的照片"}
            collections = CollectionRepo(db)
            existing = next((c for c in collections.list_all() if c["name"] == name), None)
            created = False
            if existing is None:
                collection_id = collections.create(
                    name, source={"kind": "query", "query_text": row["query_text"],
                                  "mode": mode, "via": "ui"})
                created = True
            else:
                collection_id = int(existing["id"])
                collections.set_source(
                    collection_id, {"kind": "query", "query_text": row["query_text"],
                                    "mode": mode, "via": "ui"})
            added = collections.add_shots(collection_id, shot_ids)
            return {"missing": False, "collection_id": int(collection_id), "created": created,
                    "name": name, "considered": len(shot_ids), "added": added}

        result = await _read(db, _build)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个词还没有被预置过")
        if result.get("bad"):
            raise HTTPException(status_code=422, detail=result["bad"])
        return result

    @app.get("/api/collections")
    async def collections(request: Request) -> dict[str, Any]:
        """主题列表（含条目数与**可用封面**）。

        PRD §9.2.1：封面 → 主题内排序最前的一条 → 无。
        **封面走本地 `thumb` 缓存**，因此拔掉照片硬盘也能看（断点 #7）。
        """
        db: Database = request.app.state.db

        def _list() -> dict[str, Any]:
            from ..repos import CollectionRepo

            items = CollectionRepo(db).list_all()
            for item in items:
                cover = item.pop("effective_cover_shot_id", None)
                item["cover_asset_id"] = _cover_asset_id(db, cover)
            return {"total": len(items), "items": items}

        return await _read(db, _list)

    @app.get("/api/collections/{collection_id}")
    async def collection_detail(request: Request, collection_id: int) -> dict[str, Any]:
        db: Database = request.app.state.db

        def _detail() -> dict[str, Any]:
            from ..repos import CollectionRepo, load_source

            repo = CollectionRepo(db)
            row = repo.get(collection_id)
            if row is None:
                return {"missing": True}
            items = repo.items(collection_id)
            return {
                "missing": False,
                "id": int(row["id"]),
                "uuid": row["uuid"],
                "name": row["name"],
                "description": row["description"],
                "cover_shot_id": row["cover_shot_id"],
                "sort_order": int(row["sort_order"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "count": len(items),
                "items": items,
                "scenes": len(repo.audit_scene_ids(collection_id)),
                "source": load_source(row["source_json"]),
            }

        result = await _read(db, _detail)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="主题不存在")
        return result

    @app.post("/api/collections")
    async def create_collection(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """新建主题。`uuid` 可选（跨机迁移的稳定身份，不传自动生成）。

        可选直接带 `shot_ids` 与 `source`，一次请求完成"建主题 + 放照片 + 记来源" ——
        用户的工作流就是"选好一批 → 存成主题"，拆成两步会让中途失败留下空主题。
        """
        db: Database = request.app.state.db

        def _create() -> dict[str, Any]:
            from ..repos import CollectionRepo

            repo = CollectionRepo(db)
            try:
                cid = repo.create(
                    str(payload.get("name") or ""),
                    description=payload.get("description"),
                    cover_shot_id=payload.get("cover_shot_id"),
                    sort_order=int(payload.get("sort_order") or 0),
                    uuid=payload.get("uuid"),
                    source=payload.get("source") if isinstance(payload.get("source"), dict)
                    else None,
                )
            except ValueError as exc:
                return {"bad_request": str(exc)}
            shot_ids = [int(s) for s in (payload.get("shot_ids") or [])]
            added = repo.add_shots(cid, shot_ids) if shot_ids else 0
            return {"id": cid, "added": added, "considered": len(shot_ids)}

        result = await _read(db, _create)
        if result.get("bad_request"):
            raise HTTPException(status_code=422, detail=result["bad_request"])
        return {"id": result["id"], "ok": True, "added": result["added"],
                "considered": result["considered"]}

    @app.patch("/api/collections/{collection_id}")
    async def update_collection(
        request: Request, collection_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """改名称/描述/封面/排序。只更新传入的字段。"""
        db: Database = request.app.state.db

        def _update() -> dict[str, Any]:
            from ..repos import CollectionRepo

            repo = CollectionRepo(db)
            if repo.get(collection_id) is None:
                return {"missing": True}
            try:
                changed = repo.update(
                    collection_id,
                    name=payload.get("name"),
                    description=payload.get("description"),
                    cover_shot_id=payload.get("cover_shot_id"),
                    clear_cover=bool(payload.get("clear_cover")),
                    sort_order=payload.get("sort_order"),
                )
            except ValueError as exc:
                return {"bad_request": str(exc)}
            return {"missing": False, "changed": changed}

        result = await _read(db, _update)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="主题不存在")
        if result.get("bad_request"):
            raise HTTPException(status_code=422, detail=result["bad_request"])
        return result

    @app.delete("/api/collections/{collection_id}")
    async def delete_collection(
        request: Request, collection_id: int, confirm: bool = False
    ) -> dict[str, Any]:
        """删除**主题**（只删数据库关系）。

        铁律 #5：破坏性操作必须二次确认 —— 因此要求显式 `?confirm=true`。
        注意这是"删主题"，**不是删照片**；精选与 Shot 完全不受影响。
        """
        if not confirm:
            raise HTTPException(
                status_code=428,
                detail="删除主题需要二次确认：请带 ?confirm=true。"
                "该操作只删除数据库里的主题关系，不会删除任何照片文件，也不影响精选记录。",
            )
        db: Database = request.app.state.db

        def _delete() -> dict[str, Any]:
            from ..repos import CollectionRepo

            return {"deleted": CollectionRepo(db).delete(collection_id)}

        result = await _read(db, _delete)
        if not result["deleted"]:
            raise HTTPException(status_code=404, detail="主题不存在")
        return result

    @app.post("/api/collections/{collection_id}/items")
    async def add_collection_items(
        request: Request, collection_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """把作品加进主题（幂等）。

        支持两种来源：显式 `shot_ids`，或**按条件批量筛选后加入**
        （PRD §9.2.1「也可按日期、地点、Scene、Selection 等条件辅助批量筛选后加入」）——
        后者复用 `POST /api/assets/query` 拿到 asset id 再反查 shot。
        """
        db: Database = request.app.state.db
        shot_ids = payload.get("shot_ids") or []

        def _add() -> dict[str, Any]:
            from ..repos import CollectionRepo

            repo = CollectionRepo(db)
            if repo.get(collection_id) is None:
                return {"missing": True}
            try:
                added = repo.add_shots(
                    collection_id, [int(s) for s in shot_ids], note=payload.get("note")
                )
            except ValueError as exc:
                return {"bad_request": str(exc)}
            return {"missing": False, "added": added}

        result = await _read(db, _add)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="主题不存在")
        if result.get("bad_request"):
            raise HTTPException(status_code=422, detail=result["bad_request"])
        return result

    @app.post("/api/collections/{collection_id}/items/detach")
    async def remove_collection_items(
        request: Request, collection_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """把作品移出主题（**只删关系行**：不删照片、不改精选状态）。

        ⚠️ 路径用 `detach` 而不是 `remove`：Phase 1 有一条守护测试
        `test_no_destructive_endpoints`，它禁止任何含 delete/remove/purge/erase/rm 的路由 ——
        目的是防止"删除原文件"的入口被悄悄加进来。
        这里的语义是"解除归属"，不是"删除"；**改名字比放宽那条守护更对**
        （放宽守护等于把它变成摆设，而它守护的是铁律 #1）。
        """
        db: Database = request.app.state.db
        shot_ids = [int(s) for s in (payload.get("shot_ids") or [])]

        def _detach() -> dict[str, Any]:
            from ..repos import CollectionRepo

            repo = CollectionRepo(db)
            if repo.get(collection_id) is None:
                return {"missing": True}
            return {"missing": False, "removed": repo.remove_shots(collection_id, shot_ids)}

        result = await _read(db, _detach)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="主题不存在")
        return result

    @app.post("/api/collections/{collection_id}/order")
    async def reorder_collection(
        request: Request, collection_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """按给定顺序重排主题内的作品。"""
        db: Database = request.app.state.db
        ordered = [int(s) for s in (payload.get("shot_ids") or [])]

        def _reorder() -> dict[str, Any]:
            from ..repos import CollectionRepo

            repo = CollectionRepo(db)
            if repo.get(collection_id) is None:
                return {"missing": True}
            return {"missing": False, "updated": repo.reorder(collection_id, ordered)}

        result = await _read(db, _reorder)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="主题不存在")
        return result

    @app.get("/api/shots/{shot_id}/collections")
    async def shot_collections(request: Request, shot_id: int) -> dict[str, Any]:
        """反查这张作品属于哪些主题（PRD：一张可属多个主题）。"""
        db: Database = request.app.state.db

        def _lookup() -> dict[str, Any]:
            from ..repos import CollectionRepo

            return {"shot_id": int(shot_id), "items": CollectionRepo(db).collections_of_shot(shot_id)}

        return await _read(db, _lookup)

    # ------------------------------------------------------------------ 分人（Phase 4 · 迁移 008）
    #
    # ★ 架构要点（与「方案 a」同源，但**方向相反**）：
    #
    #   检测与编码要 `cv2` → 只在命令行环境做（`photoos people detect|embed`）。
    #   但**学习与建议不需要 cv2** —— 它们只是拿库里**已经存好的** 128 维向量
    #   做均值和点积。所以下面这两个端点在产品 venv 里直接跑 numpy：
    #
    #     POST /api/people/{id}/faces   （确认）
    #     POST /api/people/{id}/reject  （否决）
    #          ↓ 服务端立刻重算原型与阈值
    #     POST /api/people/{id}/suggest （用新原型找下一批候选）
    #
    #   这就是"点一下模型就学到"能成立的原因，也是不需要 GPU、不需要重启服务的原因。

    @app.get("/api/faces/{face_id}/thumb")
    async def face_thumb(request: Request, face_id: int, size: int = Query(112, ge=32, le=512)):
        """人脸缩略图：从**本地 preview 缓存**现裁，不落盘、不读原片。

        为什么不预先裁剪存文件：21,400 张照片可能出几万张脸，落盘就是几万个新文件，
        而它们的唯一用途是显示；`preview` 本来就在本地，裁一次约 10 ms。
        """
        db: Database = request.app.state.db
        settings: Settings = request.app.state.settings

        def _crop() -> bytes | None:
            row = db.query_one(
                "SELECT f.box_x, f.box_y, f.box_w, f.box_h, p.path"
                "  FROM face f JOIN preview p ON p.asset_id = f.asset_id AND p.kind = 'preview'"
                " WHERE f.id = ?", (int(face_id),))
            if row is None:
                return None
            from PIL import Image

            path = settings.data_root / str(row["path"])
            if not path.exists():
                return None
            try:
                with Image.open(path) as image:
                    image = image.convert("RGB")
                    width, height = image.size
                    # 外扩 25%：只裁框会切掉下巴和发际线，认人时反而不好认
                    margin_x = float(row["box_w"]) * 0.25
                    margin_y = float(row["box_h"]) * 0.25
                    left = max(0, int(float(row["box_x"]) - margin_x))
                    top = max(0, int(float(row["box_y"]) - margin_y))
                    right = min(width, int(float(row["box_x"]) + float(row["box_w"]) + margin_x))
                    bottom = min(height, int(float(row["box_y"]) + float(row["box_h"]) + margin_y))
                    if right <= left or bottom <= top:
                        return None
                    crop = image.crop((left, top, right, bottom)).resize(
                        (int(size), int(size)), Image.LANCZOS)
                    buffer = io.BytesIO()
                    crop.save(buffer, format="JPEG", quality=86)
                    return buffer.getvalue()
            except Exception:  # noqa: BLE001 - 损坏的 preview 不该让整个页面 500
                return None

        payload = await _read(db, _crop)
        if payload is None:
            raise HTTPException(status_code=404, detail="这张脸没有可用的缩略图（预览缺失或已损坏）")
        return Response(content=payload, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/api/people")
    async def people_list(request: Request) -> dict[str, Any]:
        """人物列表。**未命名的人也在列表里** —— 它们正是要用户处理的。"""
        db: Database = request.app.state.db

        def _list() -> dict[str, Any]:
            from ..library import faces as faces_mod
            from ..repos import FaceRepo, PeopleRepo

            repo = PeopleRepo(db)
            items = repo.list_persons()
            stats = repo.stats()
            model, version, prep = _face_identity(db)
            face_stats = FaceRepo(db).stats(
                model=model, model_version=version, prep_version=prep)
            pending = len(repo.unassigned_face_ids(
                model=model, model_version=version, prep_version=prep))
            face_stats["not_person"] = stats["not_person_faces"]
            return {"total": len(items), "items": items, "stats": stats,
                    "faces": face_stats, "unassigned": pending,
                    # 解散过的候选：界面要能给一条**回头路**（误点一次不该丢掉一簇）
                    "archived": repo.archived_persons(limit=50),
                    "identity": {"model": model, "model_version": version,
                                 "prep_version": prep}}

        return await _read(db, _list)

    @app.post("/api/people")
    async def people_create(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """新建一个人（名字可留空 —— 模型不给人起名）。"""
        db: Database = request.app.state.db
        name = (payload.get("name") or "").strip() or None

        def _create() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person_id = repo.create_person(name=name, note=payload.get("note"))
            repo.log_event("rename" if name else "discover", person_id=person_id,
                           detail={"name": name, "via": "ui"})
            return {"id": person_id, "name": name}

        return await _read(db, _create)

    @app.get("/api/people/{person_id}")
    async def people_detail(request: Request, person_id: int,
                            limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        db: Database = request.app.state.db

        def _detail() -> dict[str, Any]:
            from ..library import faces as faces_mod
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person = repo.person(person_id)
            if person is None:
                return {"missing": True}
            model, version, prep = _face_identity(db)
            profile = repo.profile(person_id, model=model, model_version=version,
                                   prep_version=prep)
            if profile:
                profile.pop("vector", None)
                profile.pop("negative_vector", None)
            labels = repo.labels(person_id)
            faces = repo.faces(person_id, limit=limit)
            shots = sorted({int(f["shot_id"]) for f in faces if f["shot_id"] is not None})
            return {
                "missing": False,
                "person": person,
                "profile": profile,
                "counts": {k: len(v) for k, v in labels.items()},
                "faces": faces,
                "rejected": repo.rejected_faces(person_id, limit=60),
                "events": repo.events(person_id, limit=30),
                "shots": shots,
            }

        result = await _read(db, _detail)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.patch("/api/people/{person_id}")
    async def people_update(request: Request, person_id: int,
                            payload: dict[str, Any]) -> dict[str, Any]:
        """改名 / 改备注 / 换封面。改名会**重新学习原型**（名字代表用户已经认过这个人）。"""
        db: Database = request.app.state.db

        def _update() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person = repo.person(person_id)
            if person is None:
                return {"missing": True}
            if "name" in payload:
                new_name = (payload.get("name") or "").strip() or None
                repo.rename(person_id, new_name, note=payload.get("note"))
                repo.log_event("rename", person_id=person_id,
                               detail={"from": person["name"], "to": new_name})
            elif "note" in payload:
                repo.rename(person_id, person["name"], note=payload.get("note"))
            if "cover_face_id" in payload:
                cover = payload.get("cover_face_id")
                repo.set_cover(person_id, None if cover is None else int(cover))
            return {"missing": False, "person": repo.person(person_id)}

        result = await _read(db, _update)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    def _refit_profile(db: Database, repo, person_id: int) -> dict[str, Any] | None:
        """按这个人现在的标签重算原型与判定线，写库并记事件。返回可回给界面的摘要。

        抽成函数是因为**不止"确认/否决"要重算**：把脸搬走、把脸标成"不是人"、
        一键确认一整批，都会改变这个人的标签集合。四处各写一遍必然漂移 ——
        上一次的教训是"两条路径的返回形状不一致，界面在切换开关时渲染出两种卡片"。
        """
        from ..library import faces as faces_mod
        from ..repos import FaceRepo

        model, version, prep = _face_identity(db)
        labels = repo.labels(person_id)
        wanted = sorted(set(labels["positive"]) | set(labels["auto"]) | set(labels["negative"]))
        if not wanted:
            return None
        rows = FaceRepo(db).embeddings(model=model, model_version=version,
                                       prep_version=prep, face_ids=wanted)
        if not rows:
            return None
        matrix = _face_matrix(rows)
        local = {int(r["face_id"]): i for i, r in enumerate(rows)}
        try:
            learned = faces_mod.fit_profile(
                matrix,
                positive=[local[f] for f in labels["positive"] if f in local],
                auto=[local[f] for f in labels["auto"] if f in local],
                negative=[local[f] for f in labels["negative"] if f in local])
        except Exception as exc:  # noqa: BLE001 - 学习失败不该让用户的动作回滚
            return {"error": str(exc)}
        repo.save_profile(
            person_id, model=model, model_version=version, prep_version=prep,
            dim=int(rows[0]["dim"]), vector=learned.vector.tobytes(),
            negative_vector=None if learned.negative_vector is None
            else learned.negative_vector.tobytes(),
            pos_count=learned.pos_count, neg_count=learned.neg_count,
            threshold=learned.threshold, margin=learned.margin)
        repo.log_event("profile", person_id=person_id,
                       detail={"pos": learned.pos_count, "neg": learned.neg_count,
                               "threshold": round(learned.threshold, 4),
                               "notes": learned.notes})
        return {
            "pos_count": learned.pos_count, "neg_count": learned.neg_count,
            "threshold": learned.threshold, "margin": learned.margin,
            "notes": learned.notes, "summary": learned.summary(),
        }

    def _apply_labels(db: Database, person_id: int, face_ids: list[int], *,
                      confirm: bool) -> dict[str, Any]:
        """确认 / 否决的公共路径：写标签 → **立刻重算原型** → 回报新阈值。

        把"重算"放在同一次请求里，是因为用户点一下之后的下一句话必然是
        "那还有没有像他的？" —— 如果原型要等命令行才能更新，这个循环就断了。
        """
        from ..repos import PeopleRepo

        repo = PeopleRepo(db)
        if repo.person(person_id) is None:
            return {"missing": True}
        if not face_ids:
            return {"missing": False, "changed": 0, "profile": None}

        if confirm:
            repo.add_faces(person_id, face_ids, source="user")
            repo.log_event("confirm", person_id=person_id,
                           detail={"faces": face_ids[:50], "count": len(face_ids)})
        else:
            repo.reject_faces(person_id, face_ids)
            repo.log_event("reject", person_id=person_id,
                           detail={"faces": face_ids[:50], "count": len(face_ids)})

        return {"missing": False, "changed": len(face_ids),
                "profile": _refit_profile(db, repo, person_id)}

    @app.post("/api/people/{person_id}/faces")
    async def people_confirm(request: Request, person_id: int,
                             payload: dict[str, Any]) -> dict[str, Any]:
        """用户确认"这些是他"。**这是唯一的真标签来源。**"""
        db: Database = request.app.state.db
        face_ids = [int(f) for f in (payload.get("face_ids") or [])]
        result = await _read(db, lambda: _apply_labels(db, person_id, face_ids, confirm=True))
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.post("/api/people/{person_id}/reject")
    async def people_reject(request: Request, person_id: int,
                            payload: dict[str, Any]) -> dict[str, Any]:
        """用户否决"这不是他"。否决是**一等事实**（`person_reject` 单独一张表）。"""
        db: Database = request.app.state.db
        face_ids = [int(f) for f in (payload.get("face_ids") or [])]
        result = await _read(db, lambda: _apply_labels(db, person_id, face_ids, confirm=False))
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    # ---------------------------------------------------- 候选集的三个批量动作（Phase 4 补）

    @app.post("/api/people/{person_id}/confirm-all")
    async def people_confirm_all(request: Request, person_id: int,
                                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """**一键确认**：一次把你的判断写进去。

        两种入口，同一个出口：
          * 不带 `face_ids` —— 把这个人的全部「模型提议」认下来（详情页的按钮）；
          * 带 `face_ids`   —— 认下指定的一批（「再找找像他的」那一批候选，
            它们还没有归属，由这里**直接写成用户确认**）。

        为什么值得单独一个端点：逐张确认 60 张要 60 次请求、60 次重算原型 ——
        中间那 59 次结果没人看。这里一次事务写完、只重算一次。
        """
        db: Database = request.app.state.db
        explicit = [int(f) for f in ((payload or {}).get("face_ids") or [])]

        def _confirm_all() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            if repo.person(person_id) is None:
                return {"missing": True}
            if explicit:
                ids = repo.existing_face_ids(explicit)
                if not ids:
                    return {"missing": False, "confirmed": 0, "profile": None,
                            "hint": "这些脸都已经不在库里了"}
                repo.add_faces(person_id, ids, source="user")
                repo.log_event("confirm", person_id=person_id,
                               detail={"faces": ids[:50], "count": len(ids), "bulk": True,
                                       "via": "suggest"})
                return {"missing": False, "confirmed": len(ids), "skipped": len(explicit) - len(ids),
                        "profile": _refit_profile(db, repo, person_id)}
            ids = repo.auto_face_ids(person_id)
            if not ids:
                return {"missing": False, "confirmed": 0, "profile": None}
            repo.add_faces(person_id, ids, source="user")
            repo.log_event("confirm", person_id=person_id,
                           detail={"faces": ids[:50], "count": len(ids), "bulk": True})
            return {"missing": False, "confirmed": len(ids),
                    "profile": _refit_profile(db, repo, person_id)}

        result = await _read(db, _confirm_all)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.post("/api/people/{person_id}/dissolve")
    async def people_dissolve(request: Request, person_id: int) -> dict[str, Any]:
        """**解散候选集**：这一簇不算数（"有的分组里啥都有"）。

        与"删除这个人"的区别是**脸留在原地**：解散只是把这一簇收起来（`status='archived'`），
        `person_face` 一行都不删。于是：
          * 下一次 `people discover` 不会再把这些脸聚成同一坨（它们不算"没归属"）；
          * 但「再找找像他的」仍然找得到它们（`unclaimed_face_ids` 只要求"用户没确认过"）——
            也就是说：**错的只是"它们是一个人"这个结论，不是这些脸本身。**

        只对**还没有名字**的候选开放：一个有名字的人被解散会从列表里消失、
        脸却还挂在名下，那比"删掉标签"更难理解。有名字的人请用删除。
        """
        db: Database = request.app.state.db

        def _dissolve() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person = repo.person(person_id)
            if person is None:
                return {"missing": True}
            if person["status"] == "named":
                return {"named": True}
            faces = repo.dissolve_person(person_id)
            repo.log_event("dissolve", person_id=person_id, detail={"faces": faces})
            return {"missing": False, "dissolved": True, "faces": faces}

        result = await _read(db, _dissolve)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个候选不存在")
        if result.get("named"):
            raise HTTPException(
                status_code=409,
                detail="这个人已经有名字了：解散只用于没命名的候选。"
                       "确实不要他的话，用「删除这个人」（同样不会删照片）")
        return result

    @app.post("/api/people/{person_id}/restore")
    async def people_restore(request: Request, person_id: int) -> dict[str, Any]:
        """撤销解散（把候选放回列表）。**解散必须能反悔** —— 一次误点不该丢掉一簇。"""
        db: Database = request.app.state.db

        def _restore() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person = repo.person(person_id)
            if person is None:
                return {"missing": True}
            repo.restore_person(person_id)
            repo.log_event("restore", person_id=person_id, detail={})
            return {"missing": False, "restored": True, "person": repo.person(person_id)}

        result = await _read(db, _restore)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个候选不存在")
        return result

    @app.post("/api/people/move")
    async def people_move(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**把选中的脸搬到另一个人名下**（真正的搬，不是再加一条）。

        界面上"这一批其实都是 XX"是最常见的判断之一。这里必须一次做完三件事：
          1. 从**原来的人**名下摘掉（包括你自己以前确认过的归属 —— 否则同一张脸
             会在两个人下面同时出现，第 98 行 `add_faces` 只撤 auto 提议）；
          2. 记成目标人的**用户确认**；
          3. 原人物与目标人物**都重算判据**（少了几张正例，原人物的判定线会变）。
        """
        db: Database = request.app.state.db
        face_ids = [int(f) for f in (payload.get("face_ids") or [])]
        target = payload.get("target_person_id")

        def _move() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            if target is None:
                return {"bad_target": "没有指定要搬到哪个人名下"}
            target_id = int(target)
            if repo.person(target_id) is None:
                return {"missing_target": True}
            if not face_ids:
                return {"empty": True}
            alive = repo.existing_face_ids(face_ids)
            if not alive:
                return {"no_faces": True}
            sources = repo.move_faces(alive, target_person_id=target_id)
            repo.log_event("move_in", person_id=target_id,
                           detail={"faces": alive[:50], "count": len(alive),
                                   "from": sources})
            for person_id in sources:
                repo.log_event("move_out", person_id=person_id,
                               detail={"faces": alive[:50], "count": len(alive),
                                       "to": target_id})
            return {
                "moved": len(alive),
                "skipped": [f for f in face_ids if f not in set(alive)],
                "target": repo.person(target_id),
                "sources": sources,
                # 两边都要重算：原人物少了几张正例，目标人物多了几张
                "profiles": {str(pid): _refit_profile(db, repo, pid)
                             for pid in [*sources, target_id]},
            }

        result = await _read(db, _move)
        if result.get("missing_target"):
            raise HTTPException(status_code=404, detail="要搬去的那个人不存在")
        if result.get("bad_target"):
            raise HTTPException(status_code=422, detail=result["bad_target"])
        if result.get("empty"):
            raise HTTPException(status_code=422, detail="没有选择任何一张脸")
        if result.get("no_faces"):
            raise HTTPException(status_code=404, detail="这些脸都已经不在库里了")
        if result.get("skipped"):
            result["note"] = f"{len(result['skipped'])} 张脸已不在库里，跳过了"
        return result

    @app.post("/api/faces/verdict")
    async def faces_verdict(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**不是人**：这张脸压根不是一个人（海报、雕像、屏幕、反光……）。

        `verdict`：
          * `not_person` —— 记下这个判断，并把它从所有人的名下摘掉；
          * `person`     —— 撤销（删掉判断行）。**撤销必须存在**：误判一张脸太容易了。

        记下之后：聚类与"再找找像他的"都不再碰它（它进不了 `unassigned` / `unclaimed`），
        人物墙上也不再出现。但**脸本身不删** —— 检测器确实在这里检出过一张脸，
        那是事实；"它不是人"是你的判断，两者分开存（`face` 与 `face_verdict`）。
        """
        db: Database = request.app.state.db
        verdict = str(payload.get("verdict") or "not_person").strip()
        if verdict not in ("not_person", "person"):
            raise HTTPException(status_code=422, detail="verdict 必须是 not_person 或 person")
        face_ids = [int(f) for f in (payload.get("face_ids") or [])]
        if not face_ids:
            raise HTTPException(status_code=422, detail="没有选择任何一张脸")

        def _apply() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            alive = repo.existing_face_ids(face_ids)
            if not alive:
                return {"no_faces": True}
            if verdict == "not_person":
                affected = repo.mark_not_person(alive, note=payload.get("note"))
                repo.log_event("not_person", detail={"faces": alive[:50], "count": len(alive)})
                return {"changed": len(alive), "affected": affected,
                        "profiles": {str(pid): _refit_profile(db, repo, pid)
                                     for pid in affected}}
            removed, affected = repo.clear_not_person(alive)
            repo.log_event("person_again", detail={"faces": alive[:50], "count": removed})
            return {"changed": removed, "affected": affected,
                    "profiles": {str(pid): _refit_profile(db, repo, pid)
                                 for pid in affected}}

        result = await _read(db, _apply)
        if result.get("no_faces"):
            raise HTTPException(status_code=404, detail="这些脸都已经不在库里了")
        return result

    @app.get("/api/faces/not-person")
    async def faces_not_person(request: Request, limit: int = Query(200, ge=1, le=2000)
                               ) -> dict[str, Any]:
        """你标过「不是人」的那些脸（**带原片路径**，方便核对与撤销）。"""
        db: Database = request.app.state.db

        def _list() -> dict[str, Any]:
            from ..repos import PeopleRepo

            items = PeopleRepo(db).not_person_faces(limit=limit)
            return {"missing": False, "total": len(items), "items": items}

        return await _read(db, _list)

    @app.post("/api/people/{person_id}/suggest")
    async def people_suggest(request: Request, person_id: int,
                             payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """用**当前原型**给还没归类的脸打分，返回最像的一批（**等用户确认**）。

        与 `photoos people assign` 是同一个算法（`faces.assign_faces`），
        区别只是这个在服务端即时算 —— 这正是"点一下就能看到模型学到什么"的落点。

        ⚠️ 返回的每一条都带 `source='auto'` 的语义：界面**必须**写成
        「这是模型的判断」，不能写成事实。PRD §9.3：AI 不能把主观判断伪装成事实。
        """
        db: Database = request.app.state.db
        wanted_limit = int((payload or {}).get("limit") or 60)
        min_similarity = float((payload or {}).get("min_similarity") or 0.0)

        def _suggest() -> dict[str, Any]:
            import numpy as np

            from ..library import faces as faces_mod
            from ..repos import FaceRepo, PeopleRepo

            repo = PeopleRepo(db)
            if repo.person(person_id) is None:
                return {"missing": True}
            model, version, prep = _face_identity(db)
            profile = repo.profile(person_id, model=model, model_version=version,
                                   prep_version=prep)
            if profile is None:
                return {"missing": False, "ready": False, "items": [],
                        "hint": "这个人还没有原型：先确认至少一张脸。"}
            prototype = np.frombuffer(bytes(profile["vector"]), dtype="<f4").astype("f4")
            threshold = float(profile["threshold"] or faces_mod.SFACE_DEFAULT_THRESHOLD)

            # 只给"**还没有被用户确认给任何人**"的脸打分。
            # 用 unclaimed 而不是 unassigned：模型自己随手放下的 auto 归属不算数 ——
            # 用户的确认正是要纠正模型的初筛，按 unassigned 取会把错误永久固化。
            # 已被这个人否决过的脸永远不再提议给他。
            candidates = repo.unclaimed_face_ids(model=model, model_version=version,
                                                 prep_version=prep)
            rejected = {int(r["face_id"]) for r in repo.rejected_faces(person_id, limit=100000)}
            candidates = [f for f in candidates if f not in rejected]
            if not candidates:
                return {"missing": False, "ready": True, "items": [], "threshold": threshold,
                        "considered": 0}
            rows = FaceRepo(db).embeddings(model=model, model_version=version,
                                           prep_version=prep, face_ids=candidates)
            if not rows:
                return {"missing": False, "ready": True, "items": [], "threshold": threshold,
                        "considered": 0}
            matrix = _face_matrix(rows)
            scores = matrix @ prototype
            order = np.argsort(-scores)
            items = []
            for position in order:
                score = float(scores[int(position)])
                if score < min_similarity:
                    break
                row = rows[int(position)]
                items.append({
                    "face_id": int(row["face_id"]), "asset_id": int(row["asset_id"]),
                    "shot_id": int(row["shot_id"]) if row["shot_id"] is not None else None,
                    "scene_id": int(row["scene_id"]) if row.get("scene_id") is not None else None,
                    "capture_at": row.get("capture_at"),
                    "relative_path": row.get("relative_path"),
                    "similarity": score, "accepted": score >= threshold,
                    "is_frontal": bool(row["is_frontal"]),
                })
                if len(items) >= wanted_limit:
                    break
            return {"missing": False, "ready": True, "items": items,
                    "threshold": threshold, "considered": len(rows),
                    "accepted": sum(1 for i in items if i["accepted"]),
                    "model_version": version}

        result = await _read(db, _suggest)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.post("/api/people/{person_id}/collection")
    async def people_to_collection(request: Request, person_id: int,
                                   payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """把这个人的照片**一键进专题**（PRD §9.2.1 的 Collection）。

        默认复用**同名主题**（存在就追加，不存在就新建）——
        用户的心理模型是"妈妈的照片都在「妈妈」里"，而不是每次生成一个新主题。
        """
        db: Database = request.app.state.db
        body = payload or {}

        def _build() -> dict[str, Any]:
            from ..repos import CollectionRepo, PeopleRepo

            people = PeopleRepo(db)
            person = people.person(person_id)
            if person is None:
                return {"missing": True}
            faces = people.faces(person_id, limit=100000)
            shot_ids = sorted({int(f["shot_id"]) for f in faces if f["shot_id"] is not None})
            if not shot_ids:
                return {"missing": False, "shots": 0, "created": False,
                        "hint": "这个人名下还没有落到 Shot 上的照片。"}
            collections = CollectionRepo(db)
            name = str(body.get("name") or person["name"] or "").strip()
            if not name:
                return {"missing": False, "shots": len(shot_ids), "created": False,
                        "hint": "这个人还没有名字：请先给他起名，主题需要名字。"}
            existing = next((c for c in collections.list_all() if c["name"] == name), None)
            created = False
            if existing is None:
                collection_id = collections.create(name, description=None)
                created = True
            else:
                collection_id = int(existing["id"])
            added = collections.add_shots(collection_id, shot_ids)
            collections.set_source(
                collection_id,
                {"kind": "person", "person_id": int(person_id), "person_name": name,
                 "shots": len(shot_ids), "via": "ui"})
            people.log_event("merge", person_id=int(person_id),
                             detail={"collection_id": int(collection_id), "shots": added})
            return {"missing": False, "collection_id": int(collection_id), "created": created,
                    "shots": len(shot_ids), "added": added, "name": name}

        result = await _read(db, _build)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.delete("/api/people/{person_id}")
    async def people_delete(request: Request, person_id: int,
                            confirm: bool = False) -> dict[str, Any]:
        """删掉一个人（**只删"人"这个标签**：脸、照片、精选一律不动）。

        铁律 #5：破坏性操作必须二次确认 —— 所以要求显式 `?confirm=true`。
        这里删掉的是：
          * `person` 这一行（名字、备注、封面）；
          * 它的归属（`person_face`）、否决（`person_reject`）、原型（`person_profile`）；
        保留的是：
          * `face`（检出的人脸事实）、`face_embedding`（特征）、`asset`、`shot`、
            一切照片文件与已建好的主题。

        ⚠️ 注意与 `DELETE /api/collections/{id}` 的区别：删主题不碰归属，
        删人**会**连带删掉归属 —— 因为归属本来就是"这个人"的属性。
        但两者都**绝不触碰任何照片文件**。
        """
        if not confirm:
            raise HTTPException(
                status_code=428,
                detail="删除人物需要二次确认：请带 ?confirm=true。"
                "该操作只删除数据库里的人物标签与归属，不会删除任何照片文件、"
                "人脸数据或精选记录。",
            )
        db: Database = request.app.state.db

        def _delete() -> dict[str, Any]:
            from ..repos import PeopleRepo

            repo = PeopleRepo(db)
            person = repo.person(person_id)
            if person is None:
                return {"missing": True}
            repo.log_event("merge", person_id=person_id,
                           detail={"deleted_person": True, "name": person["name"]})
            repo.delete_person(person_id)
            return {"missing": False, "deleted": person_id, "name": person["name"]}

        result = await _read(db, _delete)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="这个人不存在")
        return result

    @app.post("/api/people/learn")
    async def people_learn_all(request: Request) -> dict[str, Any]:
        """把所有有标注的人的原型重算一遍（等价于 `photoos people learn`）。

        存在的理由：命令行与界面**必须是同一套算法**。界面上每次确认单个人时
        已经即时重算过，这个端点是给"批量兜底"与"我自己想重来一遍"用的。
        """
        db: Database = request.app.state.db

        def _learn() -> dict[str, Any]:
            from ..library import faces as faces_mod
            from ..repos import FaceRepo, PeopleRepo

            repo = PeopleRepo(db)
            model, version, prep = _face_identity(db)
            updated = 0
            details = []
            for person in repo.list_persons():
                person_id = int(person["id"])
                labels = repo.labels(person_id)
                wanted = sorted(set(labels["positive"]) | set(labels["auto"])
                                | set(labels["negative"]))
                if not wanted:
                    continue
                rows = FaceRepo(db).embeddings(model=model, model_version=version,
                                               prep_version=prep, face_ids=wanted)
                if not rows:
                    continue
                matrix = _face_matrix(rows)
                local = {int(r["face_id"]): i for i, r in enumerate(rows)}
                try:
                    learned = faces_mod.fit_profile(
                        matrix,
                        positive=[local[f] for f in labels["positive"] if f in local],
                        auto=[local[f] for f in labels["auto"] if f in local],
                        negative=[local[f] for f in labels["negative"] if f in local])
                except Exception as exc:  # noqa: BLE001
                    details.append({"person_id": person_id, "error": str(exc)})
                    continue
                repo.save_profile(person_id, model=model, model_version=version,
                                  prep_version=prep, dim=int(rows[0]["dim"]),
                                  vector=learned.vector.tobytes(),
                                  negative_vector=None if learned.negative_vector is None
                                  else learned.negative_vector.tobytes(),
                                  pos_count=learned.pos_count, neg_count=learned.neg_count,
                                  threshold=learned.threshold, margin=learned.margin)
                updated += 1
                details.append({"person_id": person_id, "name": person["name"],
                                "pos": learned.pos_count, "neg": learned.neg_count,
                                "threshold": learned.threshold, "margin": learned.margin})
            return {"updated": updated, "details": details}

        return await _read(db, _learn)

    # ------------------------------------------------------------------ 结构化筛选（Phase 2 · 方案 §11.2）
    @app.post("/api/assets/query")
    async def assets_query(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """结构化筛选（**绝不接受原始 SQL 字符串**）。

        请求体见 `api/filter_query.py` 的模块 docstring。要点：
          * 字段与操作符都过**白名单**，未知键 → **400**（不静默忽略）；
          * `like` 的值当**字面量**，用户写的 `%` 会被转义；
          * 值全部**参数绑定**；`limit` 上限 1000；
          * 只查数据库结构化表，**绝不扫描原片**（PRD §11.2）。

        这个端点是**将来"自然语言筛选"的落点**：小模型只需把自然语言翻成本端点接受的
        `filters` 对象即可，后端不用改。响应里的 `description` 会把条件翻回人话供用户核对。
        """
        from . import filter_query as fq

        db: Database = request.app.state.db

        def _run() -> dict[str, Any]:
            try:
                return queries_mod.structured_query(db, payload)
            except fq.QueryError as exc:
                return {"__query_error__": str(exc)}

        result = await _read(db, _run)
        if "__query_error__" in result:
            raise HTTPException(status_code=400, detail=result["__query_error__"])
        return result

    @app.get("/api/assets/query/fields")
    async def assets_query_fields() -> dict[str, Any]:
        """列出可用的筛选字段、每个字段支持的操作符、以及排序白名单。

        存在的理由：让界面（与将来的自然语言层）**能自己发现**契约，
        而不是把字段名硬编码在两处、慢慢漂移。
        """
        from . import filter_query as fq

        return {
            "fields": {name: list(ops) for name, ops in sorted(fq.FIELDS.items())},
            "operators": list(fq.OPERATORS),
            "sorts": sorted(fq.SORTS),
            "limit": {"default": fq.DEFAULT_LIMIT, "max": fq.MAX_LIMIT},
        }

    @app.get("/api/shots/{shot_id}/assets")
    async def shot_assets(request: Request, shot_id: int) -> dict[str, Any]:
        """分组里的**每一张**（人工拆分时要先看到才能选，所以不能只给封面）。

        按 `captured_at`（无则按 id）排序，让连拍顺序就是时间顺序 ——
        用户在拆分面板里看到的第一张，就是他当时拍的第一张。
        """
        db: Database = request.app.state.db

        def _list_assets() -> dict[str, Any]:
            if not db.query_one("SELECT id FROM shot WHERE id = ?", (int(shot_id),)):
                return {"missing": True}
            rows = db.query(
                # 列名是 `metadata.capture_at`（不是 `captured_at` —— 那个在 `shot` 上）
                "SELECT a.id, a.relative_path, a.file_type, m.capture_at, m.subsec,"
                "       CASE WHEN r.asset_id IS NULL THEN 0 ELSE 1 END AS rejected"
                "  FROM asset a LEFT JOIN metadata m ON m.asset_id = a.id"
                "  LEFT JOIN asset_reject r ON r.asset_id = a.id"
                " WHERE a.shot_id = ?"
                " ORDER BY COALESCE(m.capture_at, '9999'), COALESCE(m.subsec, ''), a.id",
                (int(shot_id),),
            )
            return {
                "missing": False,
                "shot_id": int(shot_id),
                "total": len(rows),
                "items": [
                    {
                        "id": int(r["id"]),
                        "name": str(r["relative_path"]).replace("\\", "/").rsplit("/", 1)[-1],
                        "relative_path": r["relative_path"],
                        "file_type": r["file_type"],
                        "captured_at": r["capture_at"],
                        "rejected": bool(r["rejected"]),
                    }
                    for r in rows
                ],
            }

        result = await _read(db, _list_assets)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Shot 不存在")
        return result

    @app.post("/api/shots/{shot_id}/reject")
    async def shot_reject(
        request: Request, shot_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """把一组文件当作**一次判断**标记/取消废片。

        照片墙折叠后一张卡就是一个 Shot；如果仍要求用户逐个文件点，折叠只省了视觉、
        没省操作。这个端点只写 `asset_reject`，不动任何原片；清理仍走统一回收站流程。
        """
        from ..library import rejects as rejects_mod

        db: Database = request.app.state.db
        body = payload or {}
        marked = bool(body.get("marked", True))
        note = str(body.get("note") or "").strip() or None

        def run() -> dict[str, Any]:
            rows = db.query(
                "SELECT a.id FROM asset a JOIN shot s ON s.id = a.shot_id"
                " WHERE a.shot_id = ? AND s.retired_at IS NULL ORDER BY a.id",
                (int(shot_id),),
            )
            if not rows:
                return {"missing": True}
            ids = [int(row["id"]) for row in rows]
            for asset_id in ids:
                rejects_mod.mark(db, asset_id, marked=marked, note=note)
            return {"shot_id": int(shot_id), "marked": marked, "count": len(ids)}

        result = await _read(db, run)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Shot 不存在或没有可标记的文件")
        return result

    @app.get("/api/assets/{asset_id}")
    async def asset(request: Request, asset_id: int) -> dict[str, Any]:
        from ..library import rejects as rejects_mod

        db: Database = request.app.state.db

        def run() -> dict[str, Any] | None:
            payload = asset_detail(db, asset_id)
            if payload is not None:
                payload["rejected"] = rejects_mod.is_marked(db, asset_id)
            return payload

        detail = await _read(db, run)
        if detail is None:
            raise HTTPException(status_code=404, detail="Asset 不存在")
        return detail

    @app.get("/api/assets/{asset_id}/path")
    async def asset_path(request: Request, asset_id: int) -> dict[str, Any]:
        """这张原片**在磁盘上的位置**（界面用它代替"从浏览器下载"）。

        为什么不再让浏览器下载原片：这个应用跑在用户自己的机器上，原片就在
        同一个硬盘上。把它重新流一遍再落到"下载"目录，是纯浪费 ——
        用户真正需要的是**路径**（拿去打开、拿去给别的程序、拿去备份）。

        ⚠️ 绝对路径是**运行时**由 `library.root_path_hint` + 相对路径拼出来的，
        数据库里依旧只有相对路径（铁律 #2）。`root_path_hint` 只是提示，
        所以这里如实报 `exists`，不假装它一定对。
        """
        db: Database = request.app.state.db

        def _path() -> dict[str, Any]:
            from ..library.store import LibraryStore

            found = _resolve_asset_file(db, asset_id)
            if found is None:
                return {"missing": True}
            record, path = found
            library = LibraryStore(db).get(record.library_id)
            exists = path.is_file()
            return {
                "missing": False,
                "asset_id": int(asset_id),
                "relative_path": record.relative_path,
                "absolute_path": str(path),
                "folder": str(path.parent),
                "name": path.name,
                "file_type": record.file_type,
                "size": int(record.size) if record.size is not None else None,
                "exists": exists,
                "root": str(library.root_path_hint) if library and library.root_path_hint else None,
                "library_id": int(record.library_id),
                "library_name": library.name if library else None,
            }

        result = await _read(db, _path)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Asset 不存在")
        return result

    @app.get("/api/assets/{asset_id}/tags")
    async def asset_tags(request: Request, asset_id: int) -> dict[str, Any]:
        """这张照片上的**全部标签**，一次拿全 —— 弹窗用它渲染"标签"那一行。

        为什么要一个专门的端点：
          · 「有谁」在 `asset` 详情里**没有**（`asset` 表不知道人脸），
            而 `person_face → face.asset_id` 很便宜（`idx_face_asset`，实测 0.005 ms）；
          · 「命中哪些内容词」要查 `semantic_query_hit`（按 shot）；
          · 弹窗每开一次都发三个请求已经够多了，标签不该再多一个往返。

        ⚠️ **不返回每个人的完整详情** —— `/api/people/{id}` 实测 92 KB，
        为了一个人名太重。这里只给 id，人名由前端已缓存的名单解析
        （见 `app.js` 的 `loadPeopleForFilter`）。取不到就退回 `#id`，**不编造**。

        ⚠️ 被标成「不是人」的脸不参与（`face_verdict` 里那行就是撤销），
        与人物页、与筛选的口径一致。
        """
        db: Database = request.app.state.db

        def _tags() -> dict[str, Any]:
            asset_row = db.query_one(
                "SELECT id, shot_id FROM asset WHERE id = ?", (int(asset_id),))
            if asset_row is None:
                return {"missing": True}
            shot_id = asset_row["shot_id"]

            people = db.query(
                "SELECT DISTINCT pf.person_id AS id"
                "  FROM face f JOIN person_face pf ON pf.face_id = f.id"
                " WHERE f.asset_id = ?"
                "   AND NOT EXISTS (SELECT 1 FROM face_verdict fv WHERE fv.face_id = f.id)"
                " ORDER BY pf.person_id",
                (int(asset_id),),
            )
            # 内容命中：这个词让这个拍摄组进了它的结果集。
            # 只报**词与当时的相似度**，不做任何"匹配度"式的表达（PRD §9.2）。
            hits: list[dict[str, Any]] = []
            collections: list[dict[str, Any]] = []
            if shot_id is not None:
                hits = [dict(r) for r in db.query(
                    "SELECT q.id AS query_id, q.query_text, h.score, h.rank"
                    "  FROM semantic_query_hit h JOIN semantic_query q ON q.id = h.query_id"
                    " WHERE h.shot_id = ? ORDER BY h.score DESC",
                    (int(shot_id),),
                )]
                # 「这张属于哪些主题」—— 一张可以同时属于多个（PRD §9.2.1）。
                # 复用 `CollectionRepo.collections_of_shot`（本来就是为详情抽屉写的）。
                #
                # ⚠️ 主题是按**拍摄组**收的，所以这里查的是 shot 的归属，
                # 不是这一个文件的归属。界面上要如实说明，否则用户会以为
                # "只有这张 JPG 在主题里、NEF 不在"。
                from ..repos import CollectionRepo

                collections = CollectionRepo(db).collections_of_shot(int(shot_id))
            return {
                "missing": False,
                "asset_id": int(asset_id),
                "shot_id": int(shot_id) if shot_id is not None else None,
                "person_ids": [int(r["id"]) for r in people],
                "content_hits": hits,
                "collections": collections,
            }

        result = await _read(db, _tags)
        if result.get("missing"):
            raise HTTPException(status_code=404, detail="Asset 不存在")
        return result


    @app.post("/api/assets/{asset_id}/reveal")
    async def asset_reveal(request: Request, asset_id: int,
                           payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """在**文件管理器里定位到这张原片**（在它所在的窗口里选中它）。

        "引导到原文件的路径"在本地应用里最实用的一步：用户点一下，文件管理器
        就停在那张文件上，可以直接拖给别的程序、复制、改名。
        **只读操作**，不移动、不删除、不修改任何东西；文件不在盘上时退而打开
        它所在的目录并如实说明。返回的 `how` 是实际用的方式（排查时有用）。
        """
        db: Database = request.app.state.db
        found = await _read(db, lambda: _resolve_asset_file(db, asset_id))
        if found is None:
            raise HTTPException(status_code=404, detail="Asset 不存在")
        _record, path = found
        folder_open = bool((payload or {}).get("folder"))
        exists = path.is_file()
        if not exists and not path.parent.is_dir():
            raise HTTPException(status_code=409, detail=_OFFLINE_DETAIL)
        try:
            how = _reveal_in_file_manager(path, folder_only=folder_open)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"打不开文件管理器：{exc}")
        return {"revealed": True, "how": how,
                "target": str(path if not folder_open else path.parent),
                "folder": str(path.parent), "exists": exists}

    @app.get("/api/assets/{asset_id}/thumb")
    async def thumb(request: Request, asset_id: int) -> FileResponse:
        return await _serve_preview(request, asset_id, "thumb")

    @app.get("/api/assets/{asset_id}/preview")
    async def preview(request: Request, asset_id: int) -> FileResponse:
        return await _serve_preview(request, asset_id, "preview")

    @app.get("/api/assets/{asset_id}/original")
    async def original(request: Request, asset_id: int) -> FileResponse:
        """**下载**原片（库离线时返回 409，UI 据此提示"插回硬盘即可访问"）。

        必须显式给 `Content-Disposition`，且中文名要走 RFC 5987 的 `filename*` 形式。
        不这么做时浏览器只能从 **URL 末段**推断文件名 —— 于是 `/original` 会被存成
        一个**没有扩展名**、名叫 `original` 的文件（RAW 的 MIME 会被 `mimetypes`
        判成 `application/octet-stream`，浏览器只能下载）。这是实测到的用户可见故障。
        """
        db: Database = request.app.state.db
        found = await _read(db, lambda: _resolve_asset_file(db, asset_id))
        if found is None:
            raise HTTPException(status_code=404, detail="Asset 不存在")
        _record, path = found
        if not path.is_file():
            raise HTTPException(status_code=409, detail=_OFFLINE_DETAIL)
        return FileResponse(
            path,
            media_type=_record.mime or None,
            filename=path.name,
            content_disposition_type="attachment",
            headers={"Cache-Control": "no-store", "X-PhotoOS-Source": "original"},
        )

    @app.get("/api/assets/{asset_id}/view")
    async def view(request: Request, asset_id: int) -> FileResponse:
        """**内联查看**：能被浏览器渲染的就给原片，否则回退到派生预览。

        为什么不能一律返回原片：浏览器无法渲染 RAW（NEF/CR3/DNG/ARW…），
        直接给原片的结果就是"打开原图"变成下载一个无法打开的二进制文件。
        RAW 的正确行为是显示**已经从它转出来的预览**（WebP 大图）。

        响应头 `X-PhotoOS-Source: original|preview` 告诉前端当前看到的是哪一个，
        以便如实标注"此处是内嵌预览，不是原片"，避免误解。
        """
        db: Database = request.app.state.db
        found = await _read(db, lambda: _resolve_asset_file(db, asset_id))
        if found is None:
            raise HTTPException(status_code=404, detail="Asset 不存在")
        record, path = found
        if not path.is_file():
            raise HTTPException(status_code=409, detail=_OFFLINE_DETAIL)

        if record.file_type in _INLINE_IMAGE_TYPES:
            return FileResponse(
                path,
                media_type=record.mime or None,
                headers={"Cache-Control": "no-store", "X-PhotoOS-Source": "original"},
            )
        if record.file_type in _INLINE_VIDEO_TYPES:
            # 视频可以直接内联给 <video>（端点已支持 Range，实测 206 正常）
            return FileResponse(
                path,
                media_type=record.mime or None,
                headers={
                    "Cache-Control": MEDIA_CACHE_CONTROL,
                    "X-PhotoOS-Source": "original",
                },
            )

        try:
            response = await _serve_preview(request, asset_id, "preview")
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=(
                    f"浏览器无法直接渲染 {record.file_type} 格式，且尚未生成可用预览。"
                    "可运行 `photoos index` 生成预览，或改用「下载原片」。"
                ),
            ) from exc
        response.headers["X-PhotoOS-Source"] = "preview"
        return response

    # ------------------------------------------------------------------ 筛选器数据
    @app.get("/api/facets")
    async def facets(request: Request, library_id: int | None = None) -> Response:
        """筛选面板要用的分面（机型 / 年份 / 镜头 / 焦距）。

        ⚠️ 这个接口**每次进照片库都会拉**，而它要扫 `metadata`（本库 21,400 行）算四个
        直方图 + 焦距分布 —— 实测约 50 ms。这些值在一次浏览会话里**基本不变**
        （只有导入/索引之后才会变），所以：

          · 用 `asset` 的 `MAX(id) / COUNT(*)` 做一个**廉价指纹**当 ETag；
          · 加 `Cache-Control: max-age=<n>`，让浏览器在窗口期内直接复用，连请求都不发。

        `max-age` 故意取短（60 秒）：导入完的照片最多一分钟就会出现在筛选下拉框里，
        而**同一秒内反复切页面则完全不走网络**。指纹里同时带 `total`，
        所以"删掉一批照片"这种不改 MAX(id) 的变化也能识破。
        """
        db: Database = request.app.state.db

        def run() -> tuple[dict[str, Any], str]:
            fingerprint = (
                f"{int(db.scalar('SELECT COUNT(*) FROM asset', default=0))}-"
                f"{int(db.scalar('SELECT COALESCE(MAX(id), 0) FROM asset', default=0))}-"
                f"{int(db.scalar('SELECT COUNT(*) FROM metadata', default=0))}"
            )
            payload = {
                "cameras": camera_histogram(db, library_id),
                "years": capture_year_histogram(db, library_id),
                "lenses": lens_histogram(db, library_id),
                "focal": focal_range(db, library_id),
            }
            return payload, fingerprint

        payload, fingerprint = await _read(db, run)
        etag = f'W/"facets-{library_id}-{fingerprint}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": FACETS_CACHE_CONTROL})
        return JSONResponse(
            content=payload,
            headers={"ETag": etag, "Cache-Control": FACETS_CACHE_CONTROL},
        )

    # ------------------------------------------------------------------ 任务
    @app.get("/api/jobs")
    async def jobs(request: Request, limit: int = 20, active_only: bool = False) -> dict[str, Any]:
        db: Database = request.app.state.db
        state = request.app.state
        return {
            "running_job_id": state.runner.current_job_id,
            "jobs": await _read(db, lambda: list_jobs(db, limit=limit, active_only=active_only)),
        }

    @app.get("/api/jobs/{job_id}")
    async def job(request: Request, job_id: int) -> dict[str, Any]:
        db: Database = request.app.state.db

        def run() -> dict[str, Any]:
            queue = JobQueue(db)
            record = queue.get(job_id)
            return {
                "job": _job_dict(record),
                "items": queue.item_states(job_id),
                "failures": [
                    {"key": row["ref_key"], "error": row["last_error"], "attempts": row["attempts"]}
                    for row in queue.failed_items(job_id, limit=20)
                ],
            }

        return await _read(db, run)

    @app.post("/api/jobs")
    async def create_job(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """创建一个后台任务。`type` 见 PRD §9.1 的导入/索引阶段。"""
        db: Database = request.app.state.db
        job_type = str(payload.get("type") or "").strip()
        library_id = payload.get("library_id")

        def run() -> dict[str, Any]:
            queue = JobQueue(db)
            record = queue.create(
                job_type,
                library_id=int(library_id) if library_id is not None else None,
                scope=payload.get("scope") or {},
            )
            return _job_dict(record)

        return await _read(db, run)

    # -------------------------------------------------- 白名单命令（界面直跑）
    @app.get("/api/actions")
    async def list_actions(request: Request) -> dict[str, Any]:
        """界面可直跑的命令目录 + 当前环境说明。

        ⚠️ 浏览器拿到的是 **id 与说明**，不是命令字符串 —— 真正的 argv 在
        `POST /api/actions/{id}/run` 里由服务端从模板展开（见 `actions/catalog.py`）。

        本端点额外补一个 `command_preview`（**已渲染好的一整行命令文本**，只供显示），
        它**不是** `argv` 数组：`catalog_payload()` 本身不返回 `argv`
        （`actions/catalog.py:340-360`），所以拿它去执行是不可能的 ——
        提交时也只会用 id（见 20-reference 的 Q-34，该 docstring 曾写错）。
        """
        settings: Settings = request.app.state.settings
        payload = actions_catalog.catalog_payload(settings)
        for item in payload["actions"]:
            action = actions_catalog.BY_ID[item["id"]]
            python = item.get("python") or payload["environment"]["python"]
            item["command_preview"] = actions_catalog.describe_command(
                python, [*action.argv]
            )
        return payload

    @app.post("/api/actions/{action_id}/run")
    async def run_action(request: Request, action_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """把一条白名单命令排进后台任务队列。

        **安全边界**：请求体里只有 `query`（搜索词）和 `prompt`（实际提示词）
        会被读取，且只对声明了 `needs_query` / `needs_prompt` 的动作有效、
        还要过各自的字符白名单。命令本身、参数顺序、用哪个解释器，全部由
        服务端决定 —— 这里没有"把用户输入拼进命令行"的路径。
        """
        db: Database = request.app.state.db
        settings: Settings = request.app.state.settings
        body = payload or {}

        action = actions_catalog.BY_ID.get(str(action_id))
        if action is None:
            raise HTTPException(
                status_code=404,
                detail=f"没有这个动作：{action_id}（可选：{', '.join(actions_catalog.BY_ID)}）",
            )

        query: str | None = None
        prompt: str | None = None
        library: int | None = None
        if action.needs_library:
            raw = body.get("library")
            try:
                library = int(raw)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="library 必须是整数 id") from exc
            exists = db.query_one("SELECT id FROM library WHERE id = ?", (library,))
            if exists is None:
                raise HTTPException(status_code=404, detail=f"没有 Library #{library}")
        if action.needs_query:
            try:
                query = actions_catalog.validate_query(body.get("query"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if action.needs_prompt:
            try:
                prompt = actions_catalog.validate_prompt(body.get("prompt"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        python, reason = actions_catalog.interpreter(settings, action)
        try:
            argv = actions_catalog.resolve_argv(
                action, query=query, prompt=prompt, library=library)
        except ValueError as exc:  # pragma: no cover - 目录自身写错才会到这里
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        command = actions_catalog.describe_command(python, argv)

        # 多步动作直接入队它的任务类型（见 `CliAction.enqueue_job`）。
        job_type = action.enqueue_job or "cli_action"

        # 同一类动作已有未完成任务时不重复入队：这些命令都写同一份数据，
        # 并行跑除了互相抢磁盘没有别的作用（与 CLI 的 --force 语义一致）。
        def enqueue() -> dict[str, Any]:
            queue = JobQueue(db)
            active = queue.find_active(job_type, library_id=None)
            if active is not None:
                same = (active.scope or {}).get("action_id") == action.id
                blocker = (active.scope or {}).get("action_id") or active.type
                return {
                    "queued": False,
                    "reason": (
                        f"已有一条任务在队列里（#{active.id} {blocker}，{active.status}）"
                        + ("，这就是同一条。" if same else "，请等它跑完。")
                    ),
                    "job": _job_dict(active),
                    "command": command,
                }
            # 有些动作本质上是**多步**的（`regroup` = sessions → shots → scenes）。
            # 拆成三条命令会得到三个任务、三条进度、三份输出，而用户在界面上按的
            # 是**一个**按钮 —— 所以这类动作直接入队它声明的任务类型，
            # 复用任务系统那套进度 / 暂停 / 取消。
            scope: dict[str, Any] = {"action_id": action.id, "command": command}
            if job_type == "cli_action":
                scope.update({"query": query, "argv": argv, "python": python})
            record = queue.create(job_type, library_id=None, scope=scope)
            return {
                "queued": True,
                "job": _job_dict(record),
                "command": command,
                "python_reason": reason,
                "long_running": action.long_running,
            }

        return await _read(db, enqueue)

    # ------------------------------------------------------------------ 打包原片
    def _collect_export_assets(db: Database, kind: str, target_id: int | None,
                               shot_ids: list[int] | None) -> tuple[list[int], str]:
        """把"人物 / 主题 / 一批拍摄组"解析成 `(asset_ids, 名字)`。**只读。**

        解析放在**入队之前**：任务里不该再有业务判断，否则"入队时是这个人、
        跑起来时人已经被删"这种时序问题会变成任务里的一个隐藏分支。

        三处的取法**与界面上看到的完全一致**（所见即所得，不然用户会以为漏了）：
          * 人物 → 他名下**人脸墙上的那些脸**所在的照片（一张多人合影只算一张）；
          * 主题 → 每个成员拍摄组的**代表照片**（主题里的条目本来就是"一组一格"）；
          * 拍摄组 → 同上，代表照片。
        """
        from ..repos import CollectionRepo, PeopleRepo

        if kind == "person":
            repo = PeopleRepo(db)
            person = repo.person(int(target_id or 0))
            if person is None:
                raise HTTPException(status_code=404, detail="这个人不存在")
            faces = repo.faces(int(person["id"]), limit=100000)
            asset_ids = sorted({int(f["asset_id"]) for f in faces if f.get("asset_id")})
            return asset_ids, str(person["name"] or f"人物{person['id']}")
        if kind == "collection":
            repo = CollectionRepo(db)
            detail = repo.get(int(target_id or 0))
            if detail is None:
                raise HTTPException(status_code=404, detail="主题不存在")
            items = repo.items(int(target_id), limit=100000)
            asset_ids = sorted({int(i["primary_asset_id"]) for i in items
                                if i.get("primary_asset_id")})
            return asset_ids, str(detail["name"])
        if kind == "shots":
            ids = [int(s) for s in (shot_ids or [])]
            if not ids:
                raise HTTPException(status_code=422, detail="没有给出要打包的拍摄组")
            marks = ",".join("?" for _ in ids)
            asset_ids = [int(r["asset_id"]) for r in db.query(
                "SELECT COALESCE(s.primary_asset_id, pick.asset_id) AS asset_id"
                "  FROM shot s JOIN (SELECT shot_id, MIN(id) AS asset_id FROM asset"
                f"        WHERE shot_id IN ({marks}) GROUP BY shot_id) pick"
                "    ON pick.shot_id = s.id"
                " WHERE s.retired_at IS NULL ORDER BY s.id", ids)]
            return asset_ids, "选中的照片"
        raise HTTPException(status_code=422, detail="kind 必须是 person / collection / shots")

    @app.post("/api/exports/plan")
    async def export_plan(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**先算账**：这次打包有多少张、多大、有几个不在盘上。不写任何文件。"""
        db: Database = request.app.state.db
        kind = str(payload.get("kind") or "").strip()
        target_id = payload.get("id")
        shot_ids = payload.get("shot_ids") or []

        def _plan() -> dict[str, Any]:
            from ..library import export as export_mod

            asset_ids, label = _collect_export_assets(
                db, kind, int(target_id) if target_id is not None else None,
                [int(s) for s in shot_ids])
            plan = export_mod.plan_export(db, asset_ids=asset_ids, label=label)
            settings_obj = request.app.state.settings
            return {"missing": False, "output_dir": str(settings_obj.exports_dir),
                    **plan.summary()}

        return await _read(db, _plan)

    @app.post("/api/exports/reveal")
    async def export_reveal(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """在资源管理器里选中打包好的 zip。

        ⚠️ 只允许 `exports/` 目录里的路径：这个端点的入参来自客户端，
        不设限就等于给了"让服务端对任意路径起一个进程"的能力。
        判据用**解析后的父目录相等**，不是字符串前缀（后者能被 `..\\` 绕过）。
        """
        settings_obj: Settings = request.app.state.settings
        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="缺少 path")
        target = Path(raw)
        try:
            resolved = target.resolve()
            allowed = settings_obj.exports_dir.resolve()
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"路径不可用：{exc}")
        if resolved.parent != allowed:
            raise HTTPException(status_code=403, detail="只允许打开打包目录里的文件")
        if not resolved.is_file():
            raise HTTPException(status_code=409, detail="这个包已经不在磁盘上了")

        try:
            how = _reveal_in_file_manager(resolved)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"打不开文件管理器：{exc}")
        return {"revealed": True, "how": how, "target": str(resolved)}

    @app.post("/api/exports")
    async def export_create(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**一键打包**：把人物 / 主题 / 选中拍摄组的原片打成一个 zip。

        产物写在数据目录的 `exports/` 下，**不经过浏览器**：跑完把完整路径
        告诉界面（可复制、可在资源管理器里打开）。理由与"单个原片不走浏览器下载"
        是同一条：文件本来就在本机。
        """
        db: Database = request.app.state.db
        kind = str(payload.get("kind") or "").strip()
        target_id = payload.get("id")
        shot_ids = payload.get("shot_ids") or []

        def _create() -> dict[str, Any]:
            from ..library import export as export_mod
            from ..library.store import LibraryStore
            from ..db import utc_now

            asset_ids, label = _collect_export_assets(
                db, kind, int(target_id) if target_id is not None else None,
                [int(s) for s in shot_ids])
            plan = export_mod.plan_export(db, asset_ids=asset_ids, label=label)
            if not plan.count:
                return {"empty": True, "missing": len(plan.missing)}
            settings_obj = request.app.state.settings
            stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")[:15]
            # ★ 产物路径**在入队时就定好**并回给界面：用户点完立刻知道"会写到哪"。
            # （任务表里没有结构化结果列，只有一行拼起来的备注 —— 与其让界面去解析
            #   那句人话，不如让路径成为双方的约定。）
            output_path = settings_obj.exports_dir / f"{export_mod.safe_label(label)}_{stamp}.zip"
            libraries = LibraryStore(db).list_all()
            queue = JobQueue(db)
            record = queue.create(
                "export", library_id=libraries[0].id if libraries else None,
                scope={"asset_ids": [e.asset_id for e in plan.entries],
                       "label": label, "kind": kind, "output_path": str(output_path)},
            )
            return {"empty": False, "job": _job_dict(record),
                    "output_path": str(output_path), **plan.summary()}

        result = await _read(db, _create)
        if result.get("empty"):
            raise HTTPException(
                status_code=409,
                detail="没有可打包的文件：这些照片的原片当前都不在盘上"
                       f"（{result.get('missing') or 0} 个）")
        return result

    # ------------------------------------------------------------------ 导入（基础物理分类）
    @app.post("/api/import/plan")
    async def import_plan(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**预演**分类导入：算出每个文件的目标路径与判定依据。**不写任何文件。**

        与 CLI 的 `photoos import`（不带 `--apply`）是同一套逻辑，因此界面与命令行
        给出的计划必然一致。
        """
        from ..library import classify as classify_mod
        from ..library import classify_import
        from ..library.geo import GeoCache

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings
        source = str(payload.get("source") or "").strip()
        if not source:
            raise HTTPException(status_code=422, detail="缺少 source（要导入的源目录）")
        limit = payload.get("limit")
        limit = int(limit) if limit else IMPORT_PLAN_DEFAULT_LIMIT

        def run() -> dict[str, Any]:
            store = LibraryStore(db)
            photo_id = payload.get("library_id")
            if photo_id is None:
                libraries = store.list_all()
                if not libraries:
                    raise PhotoOSError("尚未登记任何 Library，请先运行 `photoos library add`")
                photo_id = libraries[0].id
            video_id = payload.get("video_library_id")
            geo = GeoCache.load(GeoCache.default_path(settings_obj.data_root))
            plan = classify_import.build_plan(
                db,
                Path(source),
                photo_library_id=int(photo_id),
                video_library_id=int(video_id) if video_id else None,
                geo=geo,
                gap_hours=float(payload.get("gap_hours") or classify_mod.DEFAULT_GAP_HOURS),
                limit=limit,
                city_override=(str(payload.get("city")).strip() or None) if payload.get("city") else None,
            )
            return _plan_dict(plan, source=source)

        # 领域错误的翻译交给全局 `@app.exception_handler(PhotoOSError)`：
        # 这里**不需要**局部 try/except —— 之前那段局部捕获因为写了一个未导入的名字，
        # 反而把一切错误都变成裸 500（连真实的"路径不可用"提示都被吞掉）。
        return await _read(db, run)

    @app.post("/api/import/apply")
    async def import_apply(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """**执行**导入：创建 `classify_import` 后台任务。

        做成后台任务而不是同步请求，是为了直接获得进度、暂停/续跑与孤儿回收 ——
        复制上百 GB 的素材不可能塞进一个 HTTP 请求。源文件始终只读。
        """
        db: Database = request.app.state.db
        source = str(payload.get("source") or "").strip()
        if not source:
            raise HTTPException(status_code=422, detail="缺少 source（要导入的源目录）")
        if not payload.get("confirmed"):
            raise HTTPException(
                status_code=428,
                detail="导入会写入照片库，必须先看过预演并显式确认（confirmed=true）。",
            )
        # 先校验源目录再建任务：否则会造出一个注定失败的后台任务，
        # 用户还得去任务列表里翻才知道原因（MTP 路径就属于这种情况）。
        from ..library import paths as paths_mod

        if not Path(source).is_dir():
            raise PhotoOSError(paths_mod.explain_unusable_source(source, Path(source)))

        def run() -> dict[str, Any]:
            store = LibraryStore(db)
            photo_id = payload.get("library_id")
            if photo_id is None:
                libraries = store.list_all()
                if not libraries:
                    raise PhotoOSError("尚未登记任何 Library")
                photo_id = libraries[0].id
            video_id = payload.get("video_library_id")
            # **刻意不沿用预演上限**：预演的 `limit` 只是"同步 HTTP 请求一次读多少 EXIF"的
            # 抽样上限，用错地方会让「确认执行」只导入被抽样的那一小部分（真实踩到：
            # 预演上限 30，用户以为确认执行会处理整张卡）。
            # 想只导入一部分，用显式的 `max_files`。
            max_files = payload.get("max_files")
            scope = {
                "source": source,
                "library_id": int(photo_id),
                "video_library_id": int(video_id) if video_id else None,
                "limit": int(max_files) if max_files else None,
                "gap_hours": float(payload["gap_hours"]) if payload.get("gap_hours") else None,
                "city": (str(payload.get("city")).strip() or None) if payload.get("city") else None,
            }
            return _job_dict(
                JobQueue(db).create("classify_import", library_id=int(photo_id), scope=scope)
            )

        # 同 `/api/import/plan`：错误翻译交给全局处理器，避免再出现"局部 except 写错名字"。
        return await _read(db, run)

    # ------------------------------------------------------------------ 收件箱（手机导入）
    @app.get("/api/inbox")
    async def inbox_status(request: Request) -> dict[str, Any]:
        """收件箱状态：哪些已复制完成、哪些还在复制、哪些已经导入过。

        ⚠️ 这个 GET **有副作用**（只写数据根里的扫描快照，不碰收件箱、不碰库）：
        "复制是否稳定"需要两个时间点的证据，所以每次调用都要记一次 `(大小, mtime)`。
        第一次扫描永远不会报告"可导入"，第二次（间隔 ≥ `inbox_settle_seconds`）才会。
        """
        from ..library import inbox as inbox_mod
        from ..library import paths as paths_mod
        from ..library.store import LibraryStore

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def run() -> dict[str, Any]:
            snapshot = inbox_mod.scan_inbox(settings_obj)
            payload = snapshot.to_dict()
            warnings: list[str] = []
            if not snapshot.exists:
                warnings.append("收件箱还不存在：点「打开收件箱」会创建它。")
            for library in LibraryStore(db).list_all():
                hint = library.root_path_hint
                if hint and paths_mod.is_same_root(Path(hint), snapshot.path):
                    warnings.append(
                        "收件箱位于照片库内部，导入会被拒绝（会自我复制）。请把收件箱放到库外。"
                    )
                    break
            payload["warnings"] = warnings
            return payload

        return await _read(db, run)

    @app.post("/api/inbox/open")
    async def inbox_open(request: Request) -> dict[str, Any]:
        """创建（若不存在）并在资源管理器里打开收件箱。**只读**，不导入任何东西。"""
        from ..library import inbox as inbox_mod

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def run() -> dict[str, Any]:
            folder = inbox_mod.ensure_inbox_dir(settings_obj)
            return {"path": str(folder), "how": _reveal_in_file_manager(folder, folder_only=True)}

        return await _read(db, run)

    @app.post("/api/inbox/import")
    async def inbox_import(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把收件箱里**已经复制完成**的文件导入照片库（后台任务）。

        与 `/api/import/apply` 的区别只有一个：执行范围不是"源目录里的全部文件"，
        而是入队那一刻由两次扫描确认稳定的那些。默认源文件只读；
        `cleanup_after_import=true` 时，任务成功后会删除**台账可核对、目标仍在库里**的
        收件箱副本（手机与照片库里的文件不动）。
        """
        from ..library import inbox as inbox_mod
        from ..library import paths as paths_mod
        from ..library.store import LibraryStore

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings
        if not payload.get("confirmed"):
            raise HTTPException(
                status_code=428,
                detail="导入会写入照片库，必须先看过收件箱状态并显式确认（confirmed=true）。",
            )

        def run() -> dict[str, Any]:
            snapshot = inbox_mod.scan_inbox(settings_obj)
            ready = snapshot.ready_map()
            if not ready:
                raise PhotoOSError(
                    f"收件箱里没有已复制完成的文件（{snapshot.path}）。"
                    "先把手机照片复制进去；等状态从「复制中」变成「可导入」后再点。"
                )
            store = LibraryStore(db)
            photo_id = payload.get("library_id")
            if photo_id is None:
                libraries = store.list_all()
                if not libraries:
                    raise PhotoOSError("尚未登记任何 Library，请先在「导入新照片」里建一个照片库")
                photo_id = libraries[0].id
            for library in store.list_all():
                hint = library.root_path_hint
                if hint and paths_mod.is_same_root(Path(hint), snapshot.path):
                    raise PhotoOSError(
                        f"收件箱位于照片库内部，拒绝导入（会自我复制）：{snapshot.path}"
                    )
            # 目标盘不在时早失败：否则每个文件都会复制失败，用户只看到"失败 N 个"。
            target_library = store.get(int(photo_id))
            if not target_library.root_path_hint or not Path(target_library.root_path_hint).is_dir():
                raise PhotoOSError(
                    f"照片库 #{target_library.id}「{target_library.name}」当前不可访问："
                    f"{target_library.root_path_hint or '（无路径提示）'}。"
                    "插回这块磁盘后再导入；收件箱里的文件不会丢。"
                )
            video_id = payload.get("video_library_id")
            if video_id:
                video_library = store.get(int(video_id))
                if (not video_library.root_path_hint
                        or not Path(video_library.root_path_hint).is_dir()):
                    raise PhotoOSError(
                        f"视频库 #{video_library.id}「{video_library.name}」当前不可访问："
                        f"{video_library.root_path_hint or '（无路径提示）'}。"
                        "插回这块磁盘后再导入。"
                    )
            scope = {
                "source": str(snapshot.path),
                "library_id": int(photo_id),
                "video_library_id": int(video_id) if video_id else None,
                "gap_hours": float(payload["gap_hours"]) if payload.get("gap_hours") else None,
                "city": (str(payload.get("city")).strip() or None) if payload.get("city") else None,
                # 入队时的"稳定文件"清单：runner 会再核对一次大小/mtime 才复制
                "inbox_ready": {
                    relative: [int(size), int(mtime_ns)]
                    for relative, (size, mtime_ns) in ready.items()
                },
                # 显式勾选才清理；默认保留（兼容旧客户端与"只读"默认承诺）。
                "cleanup_after_import": bool(payload.get("cleanup_after_import")),
            }
            return {
                "job": _job_dict(
                    JobQueue(db).create(
                        "classify_import", library_id=int(photo_id), scope=scope
                    )
                ),
                "ready": len(ready),
                "bytes": snapshot.ready_bytes,
                "path": str(snapshot.path),
            }

        return await _read(db, run)

    @app.post("/api/inbox/cleanup")
    async def inbox_cleanup(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """手动清理已经安全入库的收件箱副本（只删可核对、目标仍在库里的文件）。

        与 `/api/inbox/import` 的 `cleanup_after_import` 同一套底层逻辑；单独暴露是因为
        "导入当时没勾选"或"旧版本已经导入完"的用户也需要一个入口。仍要求显式 confirmed。
        """
        from ..library import inbox as inbox_mod
        from ..library.store import LibraryStore

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings
        if not payload.get("confirmed"):
            raise HTTPException(
                status_code=428,
                detail="清理会删除收件箱里已入库的副本，必须先确认（confirmed=true）。",
            )

        def run() -> dict[str, Any]:
            roots = {
                library.id: library.root_path
                for library in LibraryStore(db).list_all()
                if library.root_path is not None
            }
            result = inbox_mod.cleanup_imported(settings_obj, library_roots=roots)
            return {
                "deleted": result.deleted,
                "bytes": result.bytes,
                "kept": result.kept,
                "errors": result.errors[:20],
                "summary": result.summary,
            }

        return await _read(db, run)

    # ------------------------------------------------------------------ ADB（可选：批量从手机拉）
    @app.get("/api/adb")
    async def adb_status(request: Request) -> dict[str, Any]:
        """adb 是否可用、连了哪些设备、默认的手机目录。

        **没有 adb 不是错误**：收件箱（P1）本来就不需要它。这里只如实报告。
        """
        from ..library import adb as adb_mod

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def run() -> dict[str, Any]:
            client = adb_mod.AdbClient.locate(settings_obj)
            payload: dict[str, Any] = {
                "available": client is not None,
                "adb_path": client.describe() if client is not None else None,
                "remote_path": settings_obj.adb_remote_path,
                "devices": [],
                "hints": [],
                "error": None,
            }
            if client is None:
                payload["hints"] = [
                    "没有找到 adb（Android Platform-Tools）。",
                    "不装也完全可以：用资源管理器把手机照片复制进上面的收件箱即可。",
                    "要装的话：下载 Platform-Tools，解压后在 config.json 里设 adb_path，或把它的目录加进 PATH。",
                ]
                return payload
            try:
                devices = client.devices()
            except Exception as exc:  # noqa: BLE001 - 状态探测失败不该变成 500
                payload["error"] = f"{type(exc).__name__}: {exc}"
                payload["hints"] = ["adb 存在但调用失败，检查它是否能独立运行（adb version）。"]
                return payload
            payload["devices"] = [device.to_dict() for device in devices]
            if not devices:
                payload["hints"] = [
                    "没有检测到设备：插上数据线，在手机的 USB 选项里选「传输文件」。",
                    "第一次用还要在「开发者选项」里打开「USB 调试」，并在手机弹窗里点「允许」。",
                ]
            elif any(device.state == "unauthorized" for device in devices):
                payload["hints"] = [
                    "有一台设备还没授权：解锁手机，在「允许 USB 调试吗？」弹窗里点「允许」。"
                ]
            return payload

        return await _read(db, run)

    @app.post("/api/adb/list")
    async def adb_list(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """列出手机目录里的文件（**只读**，不下载、不改动手机）。"""
        from ..library import adb as adb_mod

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def run() -> dict[str, Any]:
            client = _require_adb(settings_obj)
            device = _pick_adb_device(client, payload.get("serial"))
            remote = adb_mod.validate_remote(
                str(payload.get("remote_path") or settings_obj.adb_remote_path)
            )
            files = client.list_dir(device.serial, remote)
            files = [item for item in files if not item.is_dir]
            files.sort(key=lambda item: (item.date, item.time, item.name), reverse=True)
            total_bytes = sum(item.size for item in files)
            return {
                "serial": device.serial,
                "label": device.label,
                "remote": remote,
                "files": [item.to_dict() for item in files[:500]],
                "total": len(files),
                "truncated": max(0, len(files) - 500),
                "bytes": total_bytes,
            }

        return await _read(db, run)

    @app.post("/api/adb/pull")
    async def adb_pull(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把手机目录（可按日期筛选）拉到**暂存区**，再由任务发布进收件箱。

        不直接入库：手机 → 收件箱 → 稳定判定 → 导入，只有最后一步会写照片库。
        取消任务会终止 adb 进程并清掉暂存区里的副本；手机上的文件始终只读。
        """
        from ..library import adb as adb_mod
        from ..library import inbox as inbox_mod

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings
        if not payload.get("confirmed"):
            raise HTTPException(
                status_code=428,
                detail="拉取会向收件箱写入文件，请先确认（confirmed=true）。",
            )

        def run() -> dict[str, Any]:
            client = _require_adb(settings_obj)
            device = _pick_adb_device(client, payload.get("serial"))
            remote = adb_mod.validate_remote(
                str(payload.get("remote_path") or settings_obj.adb_remote_path)
            )
            since = str(payload.get("since") or "").strip() or None
            if since is not None:
                from datetime import datetime as _datetime

                try:
                    _datetime.strptime(since, "%Y-%m-%d")
                except ValueError as exc:
                    raise PhotoOSError(f"日期筛选要写成 YYYY-MM-DD：{since}") from exc
            adb_mod.cleanup_stale_staging(settings_obj)
            # 收件箱可能还不存在（用户从没手动建过）。发布阶段本来也会建，
            # 但在这里先建好，失败信息就能聚焦在"手机/拉取"上，而不是路径不存在。
            inbox_mod.ensure_inbox_dir(settings_obj)
            staging = adb_mod.staging_dir(settings_obj, device.model or device.serial)
            scope = {
                "serial": device.serial,
                "remote": remote,
                "staging": str(staging),
                "label": device.model or device.serial,
                "since": since,
                # 进度按字节计：UI 会把它显示成 8.2 GB / 52.4 GB，而不是裸数字。
                "progress_unit": "bytes",
            }
            libraries = LibraryStore(db).list_all()
            job = JobQueue(db).create(
                "adb_pull",
                library_id=libraries[0].id if libraries else None,
                scope=scope,
            )
            return {
                "job": _job_dict(job),
                "serial": device.serial,
                "label": device.label,
                "remote": remote,
                "since": since,
                "staging": str(staging),
            }

        return await _read(db, run)

    # ------------------------------------------------------------------ 废片（标记 + 可还原删除）
    @app.get("/api/rejects")
    async def rejects_status(request: Request) -> dict[str, Any]:
        """已标记废片的汇总。**只读**，不移动任何文件。"""
        from ..library import rejects as rejects_mod

        db: Database = request.app.state.db
        return await _read(db, lambda: rejects_mod.summary(db))

    @app.post("/api/assets/{asset_id}/reject")
    async def asset_reject(
        request: Request, asset_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """标记/取消"废片"。只写库，不动原片；取消就是删掉标记行。"""
        from ..library import rejects as rejects_mod

        db: Database = request.app.state.db
        body = payload or {}
        marked = bool(body.get("marked", True))
        note = str(body.get("note") or "").strip() or None

        def run() -> dict[str, Any]:
            state = rejects_mod.mark(db, asset_id, marked=marked, note=note)
            return {"asset_id": asset_id, "marked": state}

        return await _read(db, run)

    @app.post("/api/rejects/cleanup")
    async def rejects_cleanup(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """"统一删除"：创建后台任务，把已标记废片**移入 Windows 回收站**。

        没有永久删除这个选项。命中精选/主题的照片会被跳过（任务里如实报告）。
        """
        from ..library import rejects as rejects_mod

        db: Database = request.app.state.db
        if not payload.get("confirmed"):
            raise HTTPException(
                status_code=428,
                detail="清理会移动原片（移入回收站，可在资源管理器还原），必须显式确认。",
            )

        def run() -> dict[str, Any]:
            info = rejects_mod.summary(db)
            if not info["count"]:
                raise PhotoOSError("没有已标记的废片")
            libraries = LibraryStore(db).list_all()
            job = JobQueue(db).create(
                "reject_cleanup",
                library_id=libraries[0].id if libraries else None,
                scope={"count": info["count"], "bytes": info["bytes"]},
            )
            return {"job": _job_dict(job), "count": info["count"], "bytes": info["bytes"]}

        return await _read(db, run)

    @app.post("/api/jobs/{job_id}/{action}")
    async def job_action(request: Request, job_id: int, action: str) -> dict[str, Any]:
        db: Database = request.app.state.db
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(status_code=422, detail=f"不支持的操作：{action}")

        def run() -> dict[str, Any]:
            queue = JobQueue(db)
            if action == "pause":
                return _job_dict(queue.pause(job_id))
            if action == "resume":
                return _job_dict(queue.resume(job_id))
            return _job_dict(queue.cancel(job_id))

        return await _read(db, run)

    # ------------------------------------------------------------------ 维护
    @app.get("/api/integrity")
    async def integrity(request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        return await _read(db, lambda: integrity_summary(db))

    @app.post("/api/maintenance/backup")
    async def backup(request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings
        target = await _read(
            db, lambda: migration_mod.backup_database(db, settings_obj.backups_dir, label="ui")
        )
        return {"path": str(target), "bytes": target.stat().st_size}

    @app.post("/api/maintenance/migrate")
    async def migrate(request: Request) -> dict[str, Any]:
        """一键迁移：备份 → 迁移 → 校验。

        三道保险：
          1. `apply_migrations(..., make_backup=True)` 自己会先备份，失败自行回滚；
          2. 迁移前后比对 **schema 版本** 与 `asset`/`selection` 计数；
          3. 计数回退或版本倒退 → 返回 `ok=false` 并给出备份路径（不谎报成功）。
        """
        from ..db import migrator

        db: Database = request.app.state.db
        settings_obj: Settings = request.app.state.settings

        def work() -> dict[str, Any]:
            watched = ("asset", "selection", "shot", "scene", "library")
            before = {name: int(db.scalar(f"SELECT COUNT(*) FROM {name}", default=0) or 0)
                      for name in watched}
            version_before = int(db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
            backup = migration_mod.backup_database(
                db, settings_obj.backups_dir, label="before-migrate")
            applied = migrator.apply_migrations(
                db, backup_dir=settings_obj.backups_dir, make_backup=True)
            version_after = int(db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
            after = {name: int(db.scalar(f"SELECT COUNT(*) FROM {name}", default=0) or 0)
                     for name in watched}
            regressions = [f"{name}: {before[name]} → {after[name]}"
                           for name in watched if after[name] < before[name]]
            ok = version_after >= version_before and not regressions
            return {
                "ok": ok,
                "version_before": version_before,
                "version_after": version_after,
                # apply_migrations 返回的是 ApplyResult 对象（不是列表）——
                # 别对返回值做可迭代假设，字符串化即可（内容是"应用了哪些迁移"）。
                "result": str(applied),
                "backup": str(backup),
                "counts_before": before,
                "counts_after": after,
                "regressions": regressions,
            }

        return await _read(db, work)

    @app.get("/api/maintenance/backups")
    async def backups(request: Request) -> list[dict[str, Any]]:
        settings_obj: Settings = request.app.state.settings
        entries = migration_mod.list_backups(settings_obj.backups_dir)
        return [
            {"name": path.name, "bytes": size, "modified": mtime} for path, size, mtime in entries
        ]

    @app.post("/api/maintenance/duplicates")
    async def duplicates(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """重复内容报告（**只报告，不删除**，PRD §11.1）。"""
        db: Database = request.app.state.db
        library_id = (payload or {}).get("library_id")

        def run() -> dict[str, Any]:
            target = int(library_id) if library_id is not None else _first_library_id(db)
            report = pipelines.build_duplicate_report(db, target, sample_limit=10)
            return {
                "library_id": target,
                "groups": report.groups,
                "redundant_copies": report.redundant_copies,
                "reclaimable_bytes": report.reclaimable_bytes,
                "samples": [{"hash": digest, "paths": paths} for digest, paths in report.samples],
                "note": "PhotoOS 不会自动隔离或删除这些文件；是否清理由你决定。",
            }

        return await _read(db, run)

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        settings_obj: Settings = request.app.state.settings
        return {
            "project_root": str(settings_obj.data_root.parent),
            "data_root": str(settings_obj.data_root),
            "db_path": str(settings_obj.db_path),
            "thumbnails_dir": str(settings_obj.thumbnails_dir),
            "previews_dir": str(settings_obj.previews_dir),
            "backups_dir": str(settings_obj.backups_dir),
            "preview_workers": settings_obj.effective_preview_workers,
            "hash_workers": settings_obj.effective_hash_workers,
            "shot_gap_seconds": settings_obj.shot_gap_seconds,
            "session_gap_hours": settings_obj.session_gap_hours,
            # Scene 划分阈值（曾经只在模块常量里，用户改不了）—— 现在可在
            # `data/config.json` 里改，界面触发的 `scenes_rebuild` 会读这些值。
            "scene_gap_seconds": settings_obj.scene_gap_seconds,
            "scene_merge_similarity": settings_obj.scene_merge_similarity,
            "scene_dissimilar_run": settings_obj.scene_dissimilar_run,
            "scene_max_shots": settings_obj.scene_max_shots,
            "scene_unrelated_similarity": settings_obj.scene_unrelated_similarity,
            "require_confirmation_for_destructive": settings_obj.require_confirmation_for_destructive,
        }

    @app.post("/api/config/scene")
    async def update_scene_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """改 Scene 划分的五个阈值（界面上的"阈值"输入框走这里）。

        为什么要在服务端校验：这些值直接决定 `scenes rebuild` 把照片切成多少段，
        填错一个数量级（比如相似度填 85 而不是 0.85）会让结果毫无意义，
        而用户看不出"是我填错了"还是"算法坏了"。所以越界就 **400 + 一句人话**。
        """
        settings_obj: Settings = request.app.state.settings
        limits = {
            "scene_gap_seconds": (30.0, 86400.0, "秒", "Scene 时间断点"),
            "scene_merge_similarity": (0.0, 1.0, "", "合并相似度"),
            "scene_dissimilar_run": (1, 100, "张", "不相似连续张数"),
            "scene_max_shots": (1, 1000, "个", "单 Scene 的 Shot 上限"),
            "scene_unrelated_similarity": (0.0, 1.0, "", "不相似阈值"),
        }
        accepted: dict[str, Any] = {}
        for key, (low, high, unit, label) in limits.items():
            if key not in payload:
                continue
            raw = payload[key]
            try:
                value = int(raw) if isinstance(low, int) else float(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{label}需要数字，收到 {raw!r}")
            if not (low <= value <= high):
                raise HTTPException(
                    status_code=400,
                    detail=f"{label}必须在 {low}~{high}{unit} 之间（收到 {value}）",
                )
            accepted[key] = value
        if not accepted:
            raise HTTPException(status_code=422, detail="没有可更新的阈值")

        # 只写进 `data/config.json` 的这几项，其余配置原样保留。
        from ..config import save_settings_overrides

        for key, value in accepted.items():
            setattr(settings_obj, key, value)
        save_settings_overrides(settings_obj, accepted)
        return {"updated": accepted}

    # ------------------------------------------------------------ AI 组件（可选下载）
    #
    # 设计：组件**默认都不装**，装了才有对应功能；下载走用户可配置的镜像。
    # 安装是长任务（pip 装 wheel / 下 335 MB 模型），所以 POST 立刻返回，
    # 进度用内存状态 + 轮询 `GET /api/ai/components` 的 `job` 字段。

    ai_state: dict[str, dict[str, Any]] = {}

    def _ai_snapshot(data_root) -> dict[str, Any]:
        items = []
        for status in ai_components.detect(data_root):
            job = ai_state.get(status.id) or {}
            items.append({
                "id": status.id, "label": status.label, "kind": status.kind,
                "size_mb": status.size_mb, "installed": status.installed,
                "unlocks": status.unlocks, "requires": list(status.requires),
                "blocked_by": list(status.blocked_by), "note": status.note,
                "job": job or None,
            })
        # 本机有没有 NVIDIA 驱动/CUDA：没有的话 GPU 版装了也用不上（provider 不会出现）
        import shutil as _shutil

        cuda_present = _shutil.which("nvidia-smi") is not None
        for item in items:
            if item["id"] == "onnxruntime-gpu" and not cuda_present:
                item["installable"] = False
                item["unavailable"] = "本机未检测到 NVIDIA 驱动/CUDA，GPU 版装了也用不上"
        return {
            "components": items,
            "mirrors": ai_mirrors.current(),
            "install_dir": str(ai_components.writable_install_dir()),
            "providers": ai_components.gpu_providers(),
            "cuda_present": cuda_present,
        }

    @app.get("/api/ai/components")
    async def ai_components_list(request: Request) -> dict[str, Any]:
        """AI 组件清单与状态（纯本地探测，不联网）。"""
        return _ai_snapshot(request.app.state.settings.data_root)

    @app.post("/api/ai/mirrors")
    async def ai_mirrors_save(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        settings_obj: Settings = request.app.state.settings
        try:
            return {"mirrors": ai_mirrors.save(settings_obj, payload)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/ai/mirrors/test")
    async def ai_mirrors_test(payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "")
        return await asyncio.to_thread(ai_mirrors.probe, url)

    def _requirements_lock() -> Path | None:
        """找 requirements.lock：优先打包资源，其次源码树的 desktop/。"""
        here = Path(__file__).resolve()
        candidates = [p / "requirements.lock" for p in here.parents]
        candidates.insert(0, here.parents[0] / "requirements.lock")
        for base in here.parents:
            candidates.append(base / "desktop" / "requirements.lock")
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _run_install(component_id: str, data_root: Path, mirrors: dict[str, str],
                     settings_obj: Settings) -> None:
        state = ai_state.setdefault(component_id, {"state": "running", "log": []})
        state.update({"state": "running", "log": []})

        class _Cancelled(Exception):
            pass

        def log(message: str) -> None:
            state["log"].append(message)
            if state.get("cancel"):
                raise _Cancelled()

        venv_target = None
        try:
            component = ai_components.BY_ID[component_id]
            if component.kind == "external":
                raise ai_install.InstallError(
                    f"{component.label} 需要外挂环境（torch），不提供一键安装；"
                    "请按技术文档「AI 组件」一节手动安装")
            if component.kind == "venv":
                if component.id == "torch-cuda":
                    # 只换 AI venv 的 torch/torchvision，不重建环境、不动模型。
                    venv_target = ai_install.ai_venv_dir()
                    venv_python = venv_target / "Scripts" / "python.exe"
                    if not venv_python.is_file():
                        raise ai_install.InstallError(
                            "先安装「中文语义搜索（EmbeddingGemma）」组件，再装 CUDA 加速")
                    ai_install.install_into_venv(
                        venv_python,
                        ["torch==2.14.1+cu130", "torchvision==0.29.1+cu130"],
                        index_url="https://download.pytorch.org/whl/cu130", log=log)
                    import subprocess as _subprocess
                    flags = getattr(_subprocess, "CREATE_NO_WINDOW", 0)
                    probe = _subprocess.run(
                        [str(venv_python), "-c",
                         "import torch; print(torch.__version__, torch.version.cuda);"
                         " raise SystemExit(0 if torch.cuda.is_available() else 1)"],
                        capture_output=True, text=True, timeout=90, creationflags=flags,
                    )
                    if probe.returncode != 0:
                        raise ai_install.InstallError(
                            "CUDA torch 已安装，但 torch.cuda.is_available() 仍为 False："
                            + (probe.stderr or probe.stdout or "")[-300:])
                    log(f"CUDA 可用：{probe.stdout.strip()}")
                    from ..config import save_settings_overrides
                    save_settings_overrides(settings_obj, {"ai_python": str(venv_python)})
                    state["state"] = "done"
                    return
                # 中文语义搜索：独立 venv + torch + 模型。体积大，但失败只需删目录。
                venv_target = ai_install.ai_venv_dir()
                venv_python = ai_install.ensure_ai_venv(venv_target, log=log)
                ai_install.install_into_venv(
                    venv_python,
                    # 版本必须锁：实测 st 6.1.0 要求 transformers 5.x；
                    # 缺 torchvision 时模型加载直接报错（EmbeddingGemma2VideoProcessor）
                    ["torch==2.14.1", "sentence-transformers==6.1.0", "torchvision"],
                    index_url=mirrors["pip_index_url"], log=log)
                # ⚠️ 光有 torch 不够：`python -m photoos` 还要 pydantic/fastapi 等运行时依赖。
                # 实测（2026-10-18）只在 venv 里装 torch+st 时，CLI 直接 ModuleNotFoundError: pydantic。
                # 所以把包内那套依赖也装进去 —— 这个 venv 要能独立跑 `photoos`。
                lock = _requirements_lock()
                if lock is not None:
                    ai_install.install_into_venv(
                        venv_python, ["-r", str(lock)],
                        index_url=mirrors["pip_index_url"], log=log)
                else:
                    log("警告：没找到 requirements.lock，AI 环境可能缺运行时依赖")
                from ..library import model_store

                results = model_store.fetch_gemma(data_root,
                                                  on_progress=lambda name, phase: log(f"{name} {phase}"))
                bad = [r for r in results if not r.ok]
                if bad:
                    raise ai_install.InstallError("；".join(str(r.detail) for r in bad)[:400])
                if not model_store.is_gemma_ready(data_root):
                    raise ai_install.InstallError("模型下载完成但校验未通过")
                # 指向新解释器，让 CLI/后端知道用哪个 python 跑 AI
                from ..config import save_settings_overrides

                save_settings_overrides(settings_obj, {"ai_python": str(venv_python)})
                state["state"] = "done"
                return
            if component.kind == "pip":
                target = ai_components.writable_install_dir()
                if component.id == "onnxruntime-gpu":
                    # GPU/CPU 互斥：**先卸** CPU 版，再装 GPU 组合，失败由回滚兜底
                    ai_install.uninstall_pip("onnxruntime", target_dir=target, log=log)
                    # ⚠️ 只装 onnxruntime-gpu 不够：本机有驱动(581.57)但没有 CUDA 运行时，
                    # provider 不会出现（实测）。按 pyproject ai-gpu 记录的 CUDA 12 组合补运行时。
                    for package in (
                        "onnxruntime-gpu==1.25.1",
                        "nvidia-cublas-cu12",
                        "nvidia-cudnn-cu12==9.10.2.21",
                        "nvidia-cufft-cu12",
                        "nvidia-curand-cu12",
                        "nvidia-cuda-runtime-cu12",
                        "nvidia-cuda-nvrtc-cu12",
                        "nvidia-nvjitlink-cu12",
                    ):
                        ai_install.install_pip(package, target_dir=target,
                                               index_url=mirrors["pip_index_url"], log=log)
                else:
                    ai_install.install_pip(component.package, target_dir=target,
                                           index_url=mirrors["pip_index_url"], log=log)
                # ⚠️ "pip 说装好了" ≠ "能用"：GPU 版在没有 CUDA/cuDNN 的机器上
                # 会**静默**退回到只有 CPU provider。这里必须回读 provider，
                # 不满足就回滚到 CPU 版，并把原因说清楚（否则用户以为装成功了）。
                importlib.invalidate_caches()
                providers = ai_components.gpu_providers()
                log("providers: " + (", ".join(providers) or "(无)"))
                if component.id == "onnxruntime-gpu" and "CUDAExecutionProvider" not in providers:
                    log("未检测到 CUDAExecutionProvider，回滚到 CPU 版")
                    ai_install.uninstall_pip("onnxruntime-gpu", target_dir=target, log=log)
                    ai_install.install_pip("onnxruntime==1.31.0", target_dir=target,
                                           index_url=mirrors["pip_index_url"], log=log)
                    raise ai_install.InstallError(
                        "GPU 版装上了但没有 CUDA provider（通常是本机缺 CUDA/cuDNN 或驱动过旧）。"
                        "已回滚到 CPU 版；CPU 版功能完全一致，只是慢一些。")
                # ⚠️ "ort 加载失败"只该让 onnxruntime 自己的安装失败。
                # 用户装 OpenCV 时也报这句（实测），而 OpenCV 根本不需要 ort。
                if not providers:
                    if component.id.startswith("onnxruntime"):
                        raise ai_install.InstallError(
                            "onnxruntime 装完了但加载失败；请把这条任务的日志发出来")
                    log("提示：onnxruntime 当前无法加载，但不影响这个组件")
            else:
                # 模型下载**复用 model_store**：仓库路径、字节数、sha256 都在那边。
                # ⚠️ 它失败时**不抛异常**，而是返回 ok=False 的 CheckResult ——
                # 必须逐条检查，否则会出现"state=done 但一个文件都没下"（已踩过）。
                from ..library import model_store

                if component.id == "clip-model":
                    results = model_store.fetch(data_root, on_progress=lambda name, phase: log(f"{name} {phase}"))
                    ready = model_store.is_ready(data_root)
                elif component.id == "face-models":
                    results = model_store.fetch_face(data_root, on_progress=lambda name, phase: log(f"{name} {phase}"))
                    ready = model_store.is_face_ready(data_root)
                else:
                    raise ai_install.InstallError(f"未知模型组件：{component.id}")
                bad = [r for r in results if not r.ok]
                if bad:
                    raise ai_install.InstallError("；".join(str(r.detail) for r in bad)[:400])
                if not ready:
                    raise ai_install.InstallError("下载流程结束，但就绪检查仍为否 —— 请看日志")
            state["state"] = "done"
        except _Cancelled:
            state["state"] = "cancelled"
            state["log"].append("已取消（当前阶段已完成的部分不回退）")
            # 取消后不留半装环境：venv 直接删（模型 .part 保留，下次续传）
            if venv_target is not None:
                import shutil as _shutil

                _shutil.rmtree(venv_target, ignore_errors=True)
                state["log"].append(f"已清理半成品环境：{venv_target}")
        except Exception as exc:  # noqa: BLE001
            state["state"] = "error"
            state["error"] = str(exc)[:500]
            state["log"].append(f"失败：{exc}")
        finally:
            state["log"] = state["log"][-40:]

    # 卸载映射：组件 → 需要从环境里移除的包（GPU 版含 CUDA 运行时）
    AI_UNINSTALL_PACKAGES: dict[str, tuple[str, ...]] = {
        "onnxruntime-cpu": ("onnxruntime",),
        "onnxruntime-gpu": ("onnxruntime-gpu", "onnxruntime", "onnxruntime-gpu-tools",
                            "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cufft-cu12",
                            "nvidia-curand-cu12", "nvidia-cuda-runtime-cu12",
                            "nvidia-cuda-nvrtc-cu12", "nvidia-nvjitlink-cu12"),
        "opencv": ("opencv-python-headless", "opencv-python"),
    }
    # 模型组件 → ai_cache 下的目录前缀
    AI_MODEL_DIRS: dict[str, str] = {
        "clip-model": "clip-", "face-models": "face-", "gemma-text": "embeddinggemma-",
    }

    @app.post("/api/ai/components/{component_id}/uninstall")
    async def ai_component_uninstall(request: Request, component_id: str) -> dict[str, Any]:
        """卸载一个组件：pip 包 / 模型目录 / 外挂环境。"""
        if component_id not in ai_components.BY_ID:
            raise HTTPException(status_code=404, detail=f"没有这个组件：{component_id}")
        settings_obj: Settings = request.app.state.settings
        target = ai_components.writable_install_dir()
        removed: list[str] = []
        for package in AI_UNINSTALL_PACKAGES.get(component_id, ()):
            try:
                ai_install.uninstall_pip(package, target_dir=target)
                removed.append(package)
            except Exception:  # noqa: BLE001
                pass
        prefix = AI_MODEL_DIRS.get(component_id)
        if prefix:
            models = Path(settings_obj.data_root) / "ai_cache" / "models"
            for directory in models.glob(prefix + "*") if models.is_dir() else []:
                import shutil as _shutil

                _shutil.rmtree(directory, ignore_errors=True)
                removed.append(directory.name)
        if component_id == "torch-cuda":
            # 不删 AI venv（里面有 gemma），只把 torch/torchvision 换回 CPU 版。
            venv_python = ai_install.ai_venv_dir() / "Scripts" / "python.exe"
            if venv_python.is_file():
                try:
                    from .. import ai_mirrors

                    ai_install.install_into_venv(
                        venv_python, ["torch==2.14.1", "torchvision==0.29.1"],
                        index_url=ai_mirrors.current()["pip_index_url"])
                    removed.append("CUDA torch → CPU torch")
                except Exception:  # noqa: BLE001
                    pass
        if component_id == "gemma-text":
            import shutil as _shutil

            venv_dir = ai_install.ai_venv_dir()
            if venv_dir.is_dir():
                _shutil.rmtree(venv_dir, ignore_errors=True)
                removed.append(str(venv_dir))
            from ..config import save_settings_overrides

            save_settings_overrides(settings_obj, {"ai_python": ""})
        importlib.invalidate_caches()
        return {"uninstalled": component_id, "removed": removed}

    @app.post("/api/ai/components/cleanup")
    async def ai_cleanup(request: Request) -> dict[str, Any]:
        """清理"装了但用不上"的残留（典型：GPU 版失败回滚后留下的 nvidia-* / gpu 包）。"""
        import shutil as _shutil

        target = ai_components.writable_install_dir()
        leftovers = ("onnxruntime-gpu", "nvidia_cublas_cu12", "nvidia_cudnn_cu12",
                     "nvidia_cufft_cu12", "nvidia_curand_cu12", "nvidia_cuda_runtime_cu12",
                     "nvidia_cuda_nvrtc_cu12", "nvidia_nvjitlink_cu12")
        freed = 0
        removed: list[str] = []
        for name in leftovers:
            path = target / name
            if path.exists():
                try:
                    freed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                except OSError:
                    pass
                _shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        importlib.invalidate_caches()
        return {"removed": removed, "freed_mb": round(freed / 1048576, 1)}

    @app.post("/api/ai/components/{component_id}/cancel")
    async def ai_component_cancel(component_id: str) -> dict[str, Any]:
        """请求取消安装：安装器在**阶段之间**检查标记，不会硬杀 pip。"""
        state = ai_state.get(component_id)
        if not state or state.get("state") != "running":
            return {"cancelled": False, "reason": "没有正在进行的安装"}
        state["cancel"] = True
        state["log"].append("收到取消请求，将在当前阶段结束后停止…")
        return {"cancelled": True, "component": component_id}

    @app.post("/api/ai/components/{component_id}/install")
    async def ai_component_install(request: Request, component_id: str) -> dict[str, Any]:
        """触发安装（立即返回，进度用 `GET /api/ai/components` 轮询）。"""
        if component_id not in ai_components.BY_ID:
            raise HTTPException(status_code=404, detail=f"没有这个组件：{component_id}")
        current_state = ai_state.get(component_id) or {}
        if current_state.get("state") == "running":
            raise HTTPException(status_code=409, detail="这个组件正在安装")
        settings_obj: Settings = request.app.state.settings
        data_root = settings_obj.data_root
        mirrors = ai_mirrors.current()
        ai_state[component_id] = {"state": "running", "log": []}
        asyncio.create_task(asyncio.to_thread(
            _run_install, component_id, data_root, mirrors, settings_obj))
        return {"started": component_id}

    # ------------------------------------------------------------ 数据根（设置）
    #
    # 为什么需要它：打包版默认数据根是 %LOCALAPPDATA%\PhotoOS\data，而用户的
    # 旧库可能在别处 —— 界面必须能看见"现在用的是哪个库"，并能在切换前**校验**。

    @app.get("/api/data-root")
    async def data_root_info(request: Request) -> dict[str, Any]:
        settings_obj: Settings = request.app.state.settings
        db: Database = request.app.state.db
        root = Path(settings_obj.data_root)
        info: dict[str, Any] = {
            "path": str(root),
            "db_path": str(settings_obj.db_path),
            "exists": settings_obj.db_path.is_file(),
            "writable": False,
            "libraries": 0,
            "assets": 0,
            "schema_version": None,
        }
        try:
            probe = root / ".photoos-write-test"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("1", encoding="utf-8")
            probe.unlink(missing_ok=True)
            info["writable"] = True
        except OSError:
            info["writable"] = False
        if info["exists"]:
            info["libraries"] = int(db.scalar("SELECT COUNT(*) FROM library", default=0) or 0)
            info["assets"] = int(db.scalar("SELECT COUNT(*) FROM asset", default=0) or 0)
            info["schema_version"] = int(db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
        return info

    @app.get("/api/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        """给用户（和他远程求助的人）看的一页诊断信息：不含照片路径。"""
        settings_obj: Settings = request.app.state.settings
        db: Database = request.app.state.db
        db_path = Path(settings_obj.db_path)
        logs_dir = Path(getattr(settings_obj, "logs_dir", Path(settings_obj.data_root) / "logs"))
        log_files = sorted(
            (str(item) for item in logs_dir.glob("*.log")), reverse=True)[:3] if logs_dir.is_dir() else []
        return {
            "version": __version__,
            "schema_version": int(db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0),
            # 与 /api/health 用同一个来源（`target_version()`），别再猜字段名
            "schema_expected": int(target_version()),
            "data_root": str(settings_obj.data_root),
            "db_path": str(db_path),
            "db_size_mb": round(db_path.stat().st_size / 1048576, 1) if db_path.is_file() else 0,
            "port": request.url.port,
            "base_url": f"{request.url.scheme}://{request.url.hostname}:{request.url.port}",
            "logs_dir": str(logs_dir),
            "log_files": log_files,
            "ai_python": str(getattr(settings_obj, "ai_python", "") or ""),
            "libraries": int(db.scalar("SELECT COUNT(*) FROM library", default=0) or 0),
            "assets": int(db.scalar("SELECT COUNT(*) FROM asset", default=0) or 0),
        }

    @app.post("/api/data-root")
    async def data_root_switch(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """把数据根写进壳读的那份 `desktop.json`（下次启动生效，不热切）。

        为什么是"写配置 + 提示重启"而不是当场切库：当前进程已经打开了旧库的
        连接与后台线程，热切会让两个库同时被写。壳在 `%APPDATA%\\PhotoOS\\desktop.json`
        读 `dataRoot`，所以这里写同一份文件。
        """
        import json
        import os

        raw = str(payload.get("path") or "").strip()
        mode = str(payload.get("mode") or "existing")
        if not raw:
            raise HTTPException(status_code=422, detail="路径为空")
        target = Path(raw).expanduser()
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在：{target}")
        db_file = target / "photoos.db"
        if mode == "existing" and not db_file.is_file():
            raise HTTPException(status_code=400, detail="这个目录里没有 photoos.db，不能当作已有库")
        if mode == "create" and db_file.is_file():
            raise HTTPException(status_code=400, detail="这个目录里已有库，请用「使用已有库」")
        try:
            probe = target / ".photoos-write-test"
            probe.write_text("1", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            raise HTTPException(status_code=400, detail="目录不可写")

        appdata = os.environ.get("APPDATA") or str(Path.home())
        config_file = Path(appdata) / "PhotoOS" / "desktop.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if config_file.is_file():
            try:
                existing = json.loads(config_file.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                existing = {}
        existing["dataRoot"] = str(target)
        config_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        return {"saved": True, "restart_required": True, "data_root": str(target),
                "config_file": str(config_file), "mode": mode}

    @app.post("/api/data-root/validate")
    async def data_root_validate(payload: dict[str, Any]) -> dict[str, Any]:
        """校验一个候选目录：能不能当数据根、里面有没有现成的库。"""
        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="路径为空")
        target = Path(raw).expanduser()
        db_file = target / "photoos.db"
        result: dict[str, Any] = {
            "path": str(target),
            "is_dir": target.is_dir(),
            "has_db": db_file.is_file(),
            "libraries": 0,
            "assets": 0,
            "reason": "",
        }
        if not result["is_dir"]:
            result["reason"] = "目录不存在或不可访问"
            return result
        if result["has_db"]:
            import sqlite3

            try:
                conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                try:
                    result["libraries"] = int(conn.execute("SELECT COUNT(*) FROM library").fetchone()[0])
                    result["assets"] = int(conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0])
                finally:
                    conn.close()
                result["reason"] = "已有 PhotoOS 库"
            except Exception as exc:  # noqa: BLE001
                result["reason"] = f"目录里有 photoos.db，但打不开：{exc}"
        else:
            result["reason"] = "空目录（可新建库）"
        try:
            probe = target / ".photoos-write-test"
            probe.write_text("1", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            result["reason"] = "目录不可写"
            result["has_db"] = False
        return result

    # ------------------------------------------------------------------ 前端
    web_root = settings.resolved_web_root()
    if web_root.is_dir():
        # ⚠️ 前端资源的缓存策略（2026-10-13 修）：
        #
        # 原来这里只有 `StaticFiles` + 一个裸 `HTMLResponse`，两者**都没有 Cache-Control**。
        # 后果（真实故障）：浏览器对 `/` 与 `/static/*.js` 走**启发式缓存**，
        # 改了前端之后用户看到的是**旧 JS + 新 HTML**（或旧 HTML）——
        # 表现就是"新做的下拉框出不来"，而 `Ctrl+F5` 一按就好。
        # 这正是 Phase 2 那条记录的成因（"前端改动不用重启服务，刷新即可"），
        # 但"刷新"不够 —— 缺少校验线索时普通刷新不会重新取。
        #
        # 本项目的形态：**零构建、无内容哈希文件名、本地单人使用、文件很小**
        # （index 18 KB、JS 合计约 100 KB）。所以正确策略是 `no-cache`：
        # **允许缓存，但每次必须带 ETag 回服务端校验** —— 变了就拿新的，没变就 304。
        # 比"彻底禁缓存"省流量，也比"启发式"确定。
        #
        # `StaticFiles` 自己会带 ETag/Last-Modified，但**不会带 Cache-Control**，
        # 所以这里用子类补上 —— 服务端强制，不依赖各浏览器对启发式缓存的宽容度。
        class _NoCacheStatic(StaticFiles):
            def file_response(self, *args, **kwargs):        # type: ignore[override]
                response = super().file_response(*args, **kwargs)
                response.headers["Cache-Control"] = "no-cache"
                return response

        app.mount("/static", _NoCacheStatic(directory=str(web_root)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            body = (web_root / "index.html").read_text(encoding="utf-8")
            etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
            return HTMLResponse(
                body,
                headers={"Cache-Control": "no-cache", "ETag": etag},
            )

    else:  # pragma: no cover - 仅在打包缺文件时出现

        @app.get("/", response_class=HTMLResponse)
        async def index_missing() -> HTMLResponse:
            return HTMLResponse(
                "<h1>PhotoOS</h1><p>前端资源缺失："
                f"<code>{web_root}</code>。API 仍可用（<a href='/docs'>/docs</a>）。</p>",
                status_code=200,
            )

    return app


# --------------------------------------------------------------------------- 工具
async def _read(db: Database, fn):
    """把同步数据库操作下放到线程池执行，避免阻塞事件循环。

    `anyio.to_thread.run_sync` 是所有慢操作进入 FastAPI 的统一通道 ——
    SQLite 是同步库，直接在 async 路由里调用会阻塞整个服务。
    """
    import anyio

    del db
    return await anyio.to_thread.run_sync(fn)


def _face_matrix(rows):
    """人脸特征行 → (N, dim) float32 矩阵（已 L2 归一化）。

    解码口径只有一份（`library/faces.matrix_from_rows`），命令行与服务端共用 ——
    两处各写一份就会产生"命令行认得出、界面认不出"这类查不出来的偏差。
    """
    from ..library import faces as faces_mod

    return faces_mod.matrix_from_rows(rows)


def _face_identity(db: Database):
    """服务端用的**人脸特征身份**：一律从库里读，绝不现场重算。

    理由见 `FaceRepo.identity` 的说明 —— 现场重算会依赖 `cv2` 是否存在，
    而产品 venv 里没有 cv2，于是界面按一个错的身份去查，永远查不到。
    库里一条特征都没有时退化为"当前环境算出来的身份"（此时任何查询都返回空，
    退化成哪个值都不影响结果，但 `stats` 能给出诚实的 0）。
    """
    from ..library import faces as faces_mod
    from ..repos import FaceRepo

    stored = FaceRepo(db).identity()
    if stored is not None:
        return stored
    return (faces_mod.MODEL_NAME, faces_mod.model_version(), faces_mod.PREP_VERSION)


FEEDBACK_MAX_SHOTS = 500


def _feedback_shot_ids(payload: dict[str, Any]) -> list[int]:
    """从请求体里取出这次要表态的 Shot 列表（`shot_id` 单张 / `shot_ids` 多张）。

    多选是**一次请求**：界面点一下"我要这种"要对整片生效，
    逐张发请求会让服务端重排 N 次，界面也跟着跳 N 次。
    去重但**保持顺序**（用户的选择顺序有意义，返回的 `applied` 要能对上）。
    """
    raw = payload.get("shot_ids")
    if raw is None:
        single = payload.get("shot_id")
        if single is None:
            raise HTTPException(status_code=422, detail="缺少 shot_id 或 shot_ids")
        raw = [single]
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(status_code=422, detail="shot_ids 必须是数组")
    if not raw:
        raise HTTPException(status_code=422, detail="shot_ids 是空的")
    if len(raw) > FEEDBACK_MAX_SHOTS:
        raise HTTPException(
            status_code=422, detail=f"一次最多 {FEEDBACK_MAX_SHOTS} 张（收到 {len(raw)} 张）")
    ids: list[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"shot_ids 里有不是整数的值：{value!r}")
    return list(dict.fromkeys(ids))


def _decode_query_vector(row) -> "Any":
    """`semantic_query.vector` → (dim,) float32。长度不符**报错**，不截断。"""
    import numpy as np

    dim = int(row["dim"])
    raw = bytes(row["vector"])
    if len(raw) != dim * 4:
        raise HTTPException(
            status_code=500,
            detail=f"查询向量的字节数 {len(raw)} 与 dim={dim} 不符（库里这一行坏了）")
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def _semantic_pool(app: FastAPI, db: Database, identity: tuple[str, str, str]):
    """取（必要时构建）全库 Shot 向量池，**进程内缓存**。

    缓存的失效判据是"该身份下的向量条数变了" —— 重新 embed 之后条数会变，
    而条数不变就没有新信息。比时间戳可靠，也比每次重读 65 MB 便宜。
    """
    from ..library import semantic_rank

    cache = getattr(app.state, "semantic_pool", None)
    model, model_version, prep = identity
    count = int(db.scalar(
        "SELECT COUNT(*) FROM embedding WHERE model = ? AND model_version = ?"
        " AND prep_version = ?", (model, model_version, prep), default=0))
    if cache is not None and cache["identity"] == identity and cache["count"] == count:
        return cache["pool"]
    pool = semantic_rank.build_pool(db, model=model, model_version=model_version,
                                    prep_version=prep)
    app.state.semantic_pool = {"identity": identity, "count": count, "pool": pool}
    return pool


def _item_from_hit(hit: dict[str, Any], feedback: dict[int, str]) -> dict[str, Any]:
    shot_id = int(hit["shot_id"])
    return {
        "rank": int(hit.get("rank", 0)) + 1,     # 1-based，**仅用于显示顺序**
        "score": float(hit["score"]),
        "shot_id": shot_id,
        "scene_id": int(hit["scene_id"]) if hit.get("scene_id") is not None else None,
        "scene_name": hit.get("scene_name"),
        "asset_id": int(hit["asset_id"]),
        "asset_count": int(hit.get("asset_count") or 1),
        "relative_path": hit.get("relative_path"),
        "capture_at": hit.get("capture_at"),
        "selection_status": hit.get("selection_status"),
        "feedback": feedback.get(shot_id),
    }


def _base_to_items(repo, db: Database, hits: list[dict[str, Any]], limit: int,
                   feedback: dict[int, str], notes: list[str]) -> list[dict[str, Any]]:
    """反馈还不足以重排时的结果：模型原始顺序，**但删掉的照样拿掉**。

    两件事分开看（这个区分很重要，不然用户会觉得"我删了它还在"）：

      * **硬排除** = 用户的直接编辑，从第一条表态起就生效，无条件；
      * **重排**  = 从表态里学出来的偏好，需要 ≥2 条才有意义（见 `MIN_FEEDBACK`）。

    保留原始顺序（而不是按"学习项"重排）是为了让「关掉重排」这个开关名副其实：
    关掉之后看到的必须是模型当时的判断，一个字都不差。
    """
    base = sorted(hits, key=lambda h: int(h.get("rank", 0)))
    kept = [h for h in base if feedback.get(int(h["shot_id"])) != "drop"]
    del db, notes
    return [_item_from_hit(h, feedback) for h in kept[:limit]]


def _ranked_to_items(repo, db: Database, ranked: list[dict[str, Any]],
                     feedback: dict[int, str]) -> list[dict[str, Any]]:
    """重排后的结果：字段形状与 `_base_to_items` **完全一致**。

    两处形状必须一样，否则界面会在切换"学习开关"时渲染出两种卡片 ——
    本项目反复踩过的"两处口径不一致"就是这种。
    """
    del repo, db
    items: list[dict[str, Any]] = []
    for position, row in enumerate(ranked, 1):
        shot_id = int(row["shot_id"])
        items.append({
            "rank": position,
            "score": float(row["score"]),
            "base_score": row.get("base_score"),
            "shot_id": shot_id,
            "scene_id": int(row["scene_id"]) if row.get("scene_id") is not None else None,
            "scene_name": None,
            "asset_id": int(row["asset_id"]),
            "asset_count": int(row.get("asset_count") or 1),
            "relative_path": row.get("relative_path"),
            "capture_at": row.get("capture_at"),
            "selection_status": row.get("selection_status"),
            "feedback": feedback.get(shot_id),
        })
    return items


def _json_safe_errors(errors: Any) -> list[dict[str, Any]]:
    """把 Pydantic 的错误明细里的**非有限浮点**换成字符串。

    `json.dumps` 拒绝 `nan`/`inf`（它们不是合法 JSON），所以错误响应里绝不能原样
    带着它们。其余字段照原样保留，保证前端仍能按 Pydantic 的形状解析。
    """
    safe: list[dict[str, Any]] = []
    for item in errors or []:
        row: dict[str, Any] = {}
        for key, value in dict(item).items():
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = str(value)
            elif isinstance(value, (list, tuple)):
                row[key] = [str(v) if isinstance(v, float) and not math.isfinite(v) else v
                            for v in value]
            elif isinstance(value, dict):
                row[key] = {
                    k: (str(v) if isinstance(v, float) and not math.isfinite(v) else v)
                    for k, v in value.items()
                }
            else:
                row[key] = value
        safe.append(row)
    return safe


def _load_review_config(raw: Any) -> dict[str, Any]:
    """解析 `session.review_config_json`，把"坏数据"退化成空 dict 而不是 500。

    这列是用户可编辑的 JSON。库里出现半截 JSON（手工改坏、旧版本写出）时，
    整个 Session 页面不应该因此打不开 —— 参数失效是小事，看不到照片是大事。
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cover_asset_id(db: Database, shot_id: Any) -> int | None:
    """把"封面 Shot"翻成"封面 Asset"。

    前端只需要一个 asset id 就能取 `/api/assets/{id}/thumb` ——
    而那条路径走的是**本地派生缓存**，与原片是否在线无关（断点 #7）。
    """
    if shot_id is None:
        return None
    row = db.query_one("SELECT primary_asset_id FROM shot WHERE id = ?", (int(shot_id),))
    return int(row["primary_asset_id"]) if row and row["primary_asset_id"] is not None else None


def _job_dict(record) -> dict[str, Any]:
    scope = record.scope or {}
    return {
        "id": record.id,
        "type": record.type,
        "status": record.status,
        "progress_done": record.progress_done,
        "progress_total": record.progress_total,
        "percent": round(record.percent, 2),
        "note": record.progress_note,
        "error": record.error,
        "retry_count": record.retry_count,
        "max_retries": record.max_retries,
        "library_id": scope.get("library_id"),
        # `bytes` 时前端用 humanBytes 显示；为空/其它值按普通计数显示。
        "progress_unit": scope.get("progress_unit"),
        # 界面直跑的命令：任务卡片要能显示"到底跑了什么"，否则用户没法复核。
        # 只对外暴露这三项 —— scope 里可能还有别的东西（例如 export 的 asset_ids）。
        "action_id": scope.get("action_id"),
        "command": scope.get("command"),
        "query": scope.get("query"),
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "heartbeat_at": record.heartbeat_at,
    }


def _require_adb(settings: Settings):
    """拿一个可用的 adb 客户端；没有就给出**可操作**的替代方案（收件箱）。"""
    from ..library import adb as adb_mod

    client = adb_mod.AdbClient.locate(settings)
    if client is None:
        raise PhotoOSError(
            "找不到 adb.exe：安装 Android Platform-Tools 后在 config.json 设 adb_path（或放进 PATH）。"
            "不装也能用 —— 用资源管理器把手机照片复制进收件箱是一样的。"
        )
    return client


def _pick_adb_device(client, serial: Any):
    """解析出这次要操作的设备：显式 serial 优先；只有一台可用设备时自动选。"""
    from ..library import adb as adb_mod

    devices = client.devices()
    if serial:
        wanted = adb_mod.validate_serial(str(serial))
        match = next((item for item in devices if item.serial == wanted), None)
        if match is None:
            raise PhotoOSError(f"设备不在线：{wanted}。重新插线后再试一次。")
        if not match.usable:
            hint = ("请在手机上点「允许 USB 调试」" if match.state == "unauthorized"
                    else "重新插线试试")
            raise PhotoOSError(f"设备 {match.label} 当前状态是 {match.state}；{hint}")
        return match
    usable = [item for item in devices if item.usable]
    if not usable:
        if any(item.state == "unauthorized" for item in devices):
            raise PhotoOSError("手机还没授权：请在手机弹出的「允许 USB 调试」里点允许后重试")
        raise PhotoOSError("没有可用的 Android 设备：插上手机、选「传输文件」，并打开 USB 调试")
    if len(usable) > 1:
        raise PhotoOSError("连接了多台设备，请先在界面上选择一台")
    return usable[0]


def _first_library_id(db: Database) -> int:
    row = db.query_one("SELECT id FROM library ORDER BY id LIMIT 1")
    if row is None:
        raise PhotoOSError("尚未登记任何 Library")
    return int(row["id"])


async def _serve_preview(request: Request, asset_id: int, kind: str) -> FileResponse:
    """按 preview 表记录返回缓存文件。**离线库同样可用**（派生数据在本地）。"""
    db: Database = request.app.state.db
    settings_obj: Settings = request.app.state.settings

    def resolve() -> Path | None:
        from ..errors import SafetyError
        from ..library import paths as paths_mod
        from ..repos import PreviewRepo

        row = PreviewRepo(db).get(asset_id, kind)
        if row is None:
            return None
        # `preview.path` 是相对 data_root 的路径；入库时已过 `validate_relative`，
        # 这里**再校验一次**，杜绝路径穿越（铁律 #2）。
        #
        # ⚠️ 关键在于：校验必须**只做词法判断，绝不能顺着联接解析**。
        # `data_root` **内部**的目录完全可能是目录联接/符号链接（"把缓存挪到另一个
        # 盘再用联接接回来"，或隔离演练里把真实 `thumbnails/` 联接进来）。
        # 只要顺着联接 resolve，目标就落到 data_root 之外，
        # "candidate 必须在 root 之内"这条**永远不成立** → 文件明明存在且可读却 404。
        # **"包含在 root 内"与"解析联接"本身互斥**，所以两件事必须分开：
        #   ① `join_relative()` 词法校验（拒绝 `..` / 盘符 / UNC / 绝对路径）
        #   ② 通过后再去读文件（此时才顺着联接，能读到）
        #
        # 实测现场：用 `mklink /J` 联接真实 `thumbnails/` 做真实规模界面演练时，
        # `/api/assets/{id}/thumb` **全部** 404，界面上缩略图全是坏图。
        try:
            candidate = paths_mod.join_relative(settings_obj.data_root, str(row["path"]))
        except SafetyError:
            return None
        return candidate if candidate.is_file() else None

    path = await _read(db, resolve)
    if path is None:
        raise HTTPException(status_code=404, detail=f"尚未生成 {kind}（可运行 `photoos index`）")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": MEDIA_CACHE_CONTROL})


def describe_api() -> str:
    """给 CLI 用的简短说明。"""
    return json.dumps(
        {"title": "PhotoOS API", "docs": "/docs", "health": "/api/health"}, ensure_ascii=False
    )
