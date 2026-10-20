"""PhotoOS 命令行入口。

约定：
  * 所有命令在 Windows 控制台（含 GBK 代码页）下必须能正确输出中文 —— 见 `_force_utf8_io()`。
  * 退出码：0 成功，1 业务错误，2 用法错误。
  * Phase 1 提供 init / doctor / config / version；后续波次追加 scan/hash/previews/migrate/backup/serve。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import CONFIG_PATH, PROJECT_ROOT, Settings, get_settings
from .db import Database, apply_migrations, discover_migrations, target_version, verify_checksums
from .errors import PhotoOSError, SafetyError
from .jobs import pipelines
from .library import store as library_store
from .library import volume as volume_mod
from .library import walker


def _force_utf8_io() -> None:
    """让中文在 Windows 控制台正确输出（否则 GBK 代码页会抛 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 极端环境
            pass


def _print(message: str = "") -> None:
    print(message, flush=True)


def _ok(message: str) -> None:
    _print(f"  [OK]   {message}")


def _warn(message: str) -> None:
    _print(f"  [警告] {message}")


def _fail(message: str) -> None:
    _print(f"  [失败] {message}")


def _kv(key: str, value: Any) -> None:
    _print(f"  {key:<22} {value}")


# ------------------------------------------------------------------------ helpers
def _open_db() -> tuple[Settings, Database]:
    """打开已初始化的数据库；未初始化时给出可操作的提示。"""
    settings = get_settings()
    db = Database(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    db.require_exists()
    return settings, db


def _pick_library(store: library_store.LibraryStore, selector: str | None):
    """按 id / uuid 前缀 / 名称选择 Library；未指定时要求唯一。"""
    libraries = store.list_all()
    if not libraries:
        raise PhotoOSError("尚未登记任何 Library，请先运行 `photoos library add <路径> --write-marker`")
    if selector is None:
        if len(libraries) == 1:
            return libraries[0]
        raise PhotoOSError(
            "存在多个 Library，请用 --library <id|名称> 指定："
            + "，".join(f"#{lib.id} {lib.name}" for lib in libraries)
        )
    for library in libraries:
        if selector in {str(library.id), library.name} or library.uuid.startswith(selector):
            return library
    raise PhotoOSError(f"未找到 Library：{selector}")


def _run_job(args: argparse.Namespace, job_type: str, scope: dict[str, Any]) -> int:
    """创建并同步执行一个任务，打印进度与结果。"""
    from .jobs import BackgroundRunner, JobQueue, JobRunner

    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    queue = JobQueue(db)

    active = queue.find_active(job_type, library.id)
    if active is not None and not args.force:
        _warn(
            f"该 Library 已有未完成的 {job_type} 任务（#{active.id} {active.status}）。"
            "使用 --force 新建，或用 `photoos jobs resume <id>` 继续既有任务。"
        )
        return 1

    job = queue.create(job_type, library_id=library.id, scope=scope)
    _print(f"任务 #{job.id}（{job_type}）· Library「{library.name}」· 路径 {library.root_path_hint}")
    # 认领：带上执行者身份，避免与 serve 的后台执行器抢同一个任务
    from .jobs import executor_id

    job = queue.claim_next(owner=executor_id()) or job
    _print()

    started = time.perf_counter()
    last_line = [""]

    def on_progress(done: int, total: int, note: str) -> None:
        if args.quiet:
            return
        text = f"  {note}" if total <= 0 else f"  {note}  ({100.0 * done / max(1, total):.1f}%)"
        # 用回车覆盖同一行，避免刷屏（非 TTY 时退化为逐行输出）
        if sys.stdout.isatty():
            sys.stdout.write("\r" + text.ljust(max(len(last_line[0]), len(text))))
            sys.stdout.flush()
            last_line[0] = text
        else:
            _print(text)

    runner = JobRunner(db, settings)
    report = runner.run(job, on_progress=on_progress)
    if sys.stdout.isatty() and last_line[0]:
        sys.stdout.write("\n")

    _print()
    for line in report.lines():
        _print(line)
    _print(f"  耗时：{time.perf_counter() - started:.1f} 秒")

    if report.status == "COMPLETED":
        return 0
    if report.status in {"PAUSED", "CANCELLED"}:
        _warn(f"任务未完成（{report.status}）。可执行 `photoos jobs resume {job.id}` 继续。")
        return 0
    _fail(f"任务失败：{report.error}")
    return 1


# --------------------------------------------------------------------- pipelines
def cmd_scan(args: argparse.Namespace) -> int:
    """发现阶段：遍历 Library 建立/更新 asset 索引（只读文件系统）。"""
    return _run_job(args, "scan", {})


def cmd_hash(args: argparse.Namespace) -> int:
    """哈希阶段：为未哈希的 asset 计算 BLAKE3（IO 受限，可暂停续跑）。"""
    scope: dict[str, Any] = {}
    if args.limit:
        scope["limit"] = int(args.limit)
    return _run_job(args, "hash", scope)


def cmd_index(args: argparse.Namespace) -> int:
    """完整索引（实现在下方 cmd_index，带 --wait 支持按剩余量驱动）。

    这里保留一个转发，是为了让 `scan/hash/index` 三个命令在文件里相邻易读。
    真正的实现在 cmd_index_impl 中。
    """
    return cmd_index_impl(args)


# ------------------------------------------------------------------- rebind/迁移
def cmd_library_rebind(args: argparse.Namespace) -> int:
    """把 Library 重新绑定到新位置（盘符变化 / 整盘更换）。"""
    from .library import rebind as rebind_mod

    _, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    new_root = Path(args.path).expanduser()

    _print("重新绑定 Library（PRD §5.2：Library UUID → 扫描新位置 → Relative Path + Hash → 重绑定）")
    report = rebind_mod.rebind_library(
        db,
        library.id,
        new_root,
        sample=args.sample,
        apply=not args.dry_run,
    )
    _print()
    for line in report.lines():
        _print(line)
    if report.rebound:
        _print()
        _ok("原图访问已恢复；索引、缩略图与元数据未被改动，也没有重新导入任何文件。")
        return 0
    if args.dry_run:
        _warn("这是预演。确认无误后去掉 --dry-run 执行实际重绑定。")
        return 0
    _fail("未执行重绑定，请根据上面的警告检查路径。")
    return 1


def cmd_library_verify(args: argparse.Namespace) -> int:
    """检查 Library 当前是否可达，以及索引与磁盘的一致程度。"""
    from .library import rebind as rebind_mod

    _, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    reachable, total, sampled_ok = rebind_mod.library_availability(db, library.id)

    _print(f"Library #{library.id}「{library.name}」")
    _kv("路径提示", library.root_path_hint or "(未记录)")
    _kv("状态", library.status)
    _kv("根路径可达", "是" if reachable else "否（离线）")
    _kv("索引内 Asset", total)
    if reachable:
        _kv("抽样可访问", f"{sampled_ok}/50")
    else:
        _print()
        _warn("照片库当前离线。数据库、缩略图、EXIF 与精选记录仍然可用（PRD §9.1）。")
        _print("        重新连接硬盘后执行 `photoos library rebind` 恢复原图访问。")
    return 0


def cmd_library_offline(args: argparse.Namespace) -> int:
    """把 Library 显式标记为离线（例如硬盘暂时不接）。"""
    from .library import rebind as rebind_mod

    _, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    rebind_mod.mark_library_offline(db, library.id, reason=args.reason or "手动标记离线")
    _ok(f"Library #{library.id} 已标记为离线；索引与派生数据保持可用。")
    return 0


def cmd_migrate_export(args: argparse.Namespace) -> int:
    """导出换机迁移包（照片原文件默认不入包）。"""
    from .library import migration as migration_mod

    settings, db = _open_db()
    target = Path(args.output).expanduser() if args.output else (
        settings.backups_dir / f"photoos-migration-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    )

    _print("导出换机迁移包")
    report = migration_mod.export_package(db, settings.data_root, target)
    _print()
    for line in report.lines():
        _print(line)
    _print()
    _ok("迁移包已生成。请把它复制到新电脑，并在新电脑上执行：")
    _print("        photoos migrate import <包路径>")
    return 0


def cmd_migrate_verify(args: argparse.Namespace) -> int:
    """校验迁移包完整性与版本兼容性。"""
    from .library import migration as migration_mod

    _print("校验迁移包")
    result = migration_mod.verify_package(Path(args.package).expanduser())
    _print()
    for line in result.lines():
        _print(line)
    return 0 if result.ok else 1


def cmd_migrate_import(args: argparse.Namespace) -> int:
    """导入迁移包（应用前自动备份现有数据根，并写 restore.json）。"""
    from .library import migration as migration_mod

    settings, _db = _open_db()
    _print("导入换机迁移包")
    report = migration_mod.import_package(
        Path(args.package).expanduser(),
        settings.data_root,
        apply=not args.dry_run,
        deep_verify=not args.fast,
    )
    _print()
    for line in report.lines():
        _print(line)
    if report.applied:
        _print()
        _ok("下一步：连接照片硬盘后运行")
        _print("        photoos library probe <照片盘路径>")
        _print("        photoos library rebind <Library> <照片盘路径>")
        return 0
    if args.dry_run:
        _warn("这是预演，未改动任何数据。")
        return 0
    _fail("导入未应用，请检查上面的警告。")
    return 1


def cmd_backup(args: argparse.Namespace) -> int:
    """在线备份数据库（VACUUM INTO，不阻塞读）。"""
    from .library import migration as migration_mod

    settings, db = _open_db()
    if args.list:
        entries = migration_mod.list_backups(settings.backups_dir)
        if not entries:
            _print(f"暂无备份（目录：{settings.backups_dir}）")
            return 0
        _print(f"备份目录：{settings.backups_dir}")
        for path, size, mtime in entries:
            _kv(path.name, f"{size / 1e6:.1f} MB  {mtime}")
        return 0

    target = migration_mod.backup_database(db, settings.backups_dir, label=args.label)
    _ok(f"已备份：{target}（{target.stat().st_size / 1e6:.1f} MB）")

    if args.keep is not None:
        removed = migration_mod.prune_backups(settings.backups_dir, keep=args.keep)
        if removed:
            _ok(f"按保留策略清理了 {len(removed)} 份旧备份（keep={args.keep}）")
            for path in removed:
                _print(f"        - {path.name}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """从 SD 卡 / 相机暂存目录 / 普通文件夹把照片收进 Library（PRD §9.1）。

    默认走**基础物理分类**：按拍摄时间与 GPS 判定到 `<城市>\\<时段>\\` 再复制进库，
    这样新照片与既有库（18 个城市 / 71 个时段）保持同一套目录约定。

    **这是 PhotoOS 唯一写入照片库的能力**，因此默认只预演；必须显式 `--apply` 才真正复制。
    源文件永远只读：不移动、不改名、不删除（导入是复制，不是剪切）。
    """
    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    source = Path(args.source).expanduser()

    if getattr(args, "no_classify", False):
        return _import_plain(args, db, store, source)
    return _import_classified(args, settings, db, store, source)


def _import_plain(
    args: argparse.Namespace, db, store, source: Path
) -> int:
    """旧行为：照搬源目录结构（`--no-classify`）。"""
    from .library import importer

    library = _pick_library(store, args.library)

    _print("导入照片到 Library（照搬源目录结构）")
    _kv("源", source)
    _kv("目标", f"#{library.id} {library.name} → {library.root_path_hint}")
    if args.subdir:
        _kv("子目录", args.subdir)
    _print()

    if not args.apply:
        report, _items = importer.plan_import(
            db, library.id, source, subdir=args.subdir, limit=args.limit
        )
        for line in report.lines():
            _print(line)
        _print()
        if report.planned:
            _warn("以上为预演，尚未复制任何文件。确认无误后加 --apply 执行。")
        else:
            _ok("没有需要导入的新文件")
        return 0

    def on_progress(done: int, total: int, note: str) -> None:
        if not args.quiet:
            _print(f"  {note}")

    report = importer.execute_import(
        db, library.id, source, subdir=args.subdir, limit=args.limit, progress=on_progress
    )
    for line in report.lines():
        _print(line)
    _print()
    if report.failed:
        _fail(f"有 {report.failed} 个文件导入失败，详情见上。源文件未被改动。")
        return 1
    _ok("导入完成。源文件保持原样（未移动、未删除）。")
    if report.copied:
        _print("下一步：运行 `photoos index` 为新文件补齐元数据、缩略图与内容哈希。")
    return 0


def _import_classified(args: argparse.Namespace, settings, db, store, source: Path) -> int:
    """默认路径：基础物理分类 → 复制 → 增量索引。"""
    from .jobs import JobQueue, JobRunner, executor_id
    from .library import classify as classify_mod
    from .library import classify_import
    from .library.geo import GeoCache

    photo_library = _pick_library(store, args.library)
    video_library = (
        _pick_library(store, args.video_library) if args.video_library else None
    )

    cache_path = (
        Path(args.geo_cache)
        if args.geo_cache
        else GeoCache.default_path(settings.data_root)
    )
    geo = GeoCache.load(cache_path)
    gap_hours = (
        args.gap_hours if args.gap_hours is not None else classify_mod.DEFAULT_GAP_HOURS
    )

    _print("导入照片到 Library（基础物理分类）")
    _kv("源", source)
    _kv("照片库", f"#{photo_library.id} {photo_library.name} → {photo_library.root_path_hint}")
    if video_library is not None:
        _kv("视频库", f"#{video_library.id} {video_library.name} → {video_library.root_path_hint}")
    else:
        _kv("视频库", "未指定（源中若含视频会报错并要求指定）")
    _kv("GPS 缓存", f"{cache_path}（{len(geo)} 条 / {len(geo.cities())} 个城市）")
    _kv("时段间隔", f"{gap_hours:g} 小时")
    if getattr(args, "city", None):
        _kv("强制城市", f"{args.city}（压过 GPS 判定）")
    _print()

    def on_progress(done: int, total: int, note: str) -> None:
        if not args.quiet:
            _print(f"  {note}")

    try:
        plan = classify_import.build_plan(
            db,
            source,
            photo_library_id=photo_library.id,
            video_library_id=video_library.id if video_library else None,
            geo=geo,
            gap_hours=gap_hours,
            limit=args.limit,
            city_override=getattr(args, "city", None),
            progress=on_progress,
        )
    except PhotoOSError as exc:
        _fail(exc.message)
        _print()
        _print("可用的 Library：")
        for library in store.list_all():
            _kv(f"  #{library.id}", f"{library.name} → {library.root_path_hint}")
        return 1

    _print(_format_classify_plan(plan))

    if args.plan_out:
        _write_plan_csv(Path(args.plan_out), plan)

    if not plan.total:
        _print()
        _ok("没有需要导入的新文件")
        return 0

    if not args.apply:
        _print()
        _warn("以上为预演，尚未复制任何文件。确认无误后加 --apply 执行。")
        return 0

    _print()
    try:
        report = classify_import.execute_plan(
            db, plan, geo=geo, geo_cache_path=cache_path, progress=on_progress
        )
    except SafetyError as exc:
        _fail(exc.message)
        return 1

    _print()
    for line in report.lines():
        _print(line)
    _print()
    if report.failed:
        _fail(f"有 {report.failed} 个文件导入失败，详情见上。源文件未被改动。")
        return 1

    _ok("导入完成。源文件保持原样（未移动、未重命名、未删除）。")

    if report.copied and not args.no_index:
        _print()
        _print("建立增量索引（只处理刚导入的文件，不重扫全库）……")
        runner = JobRunner(db, settings)
        for library_id in sorted({plan.library_of[key] for key in plan.library_of}):
            queue = JobQueue(db)
            job = queue.create("preview", library_id=library_id)
            queue.claim_next(owner=executor_id())
            stage = runner.run(queue.get(job.id))
            _print(f"  预览阶段（Library #{library_id}）：{stage.status}")
            for line in stage.lines()[1:]:
                _print("    " + line)

    sources = classify_import.safe_to_delete_sources(report)
    if sources:
        _print()
        _print(f"源目录中已有 {len(sources)} 个文件完成复制与哈希校验（可安全删除）：")
        for path in sources[:5]:
            _print(f"    {path}")
        if len(sources) > 5:
            _print(f"    ……另有 {len(sources) - 5} 个")
        _print("  PhotoOS **不会**替你删除源文件；确认无误后请自行清理。")
    return 0


def _format_classify_plan(plan) -> str:
    """把归类计划渲染成可读文本（预演与执行共用）。"""
    from .library.classify import UNKNOWN_CITY

    out: list[str] = ["预演结果（尚未复制任何文件）"]
    kinds: dict[str, int] = {}
    for item in plan.plan.files:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    kind_text = "，".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "无"
    out.append(f"  待导入    {plan.total} 个文件（{kind_text}）")
    out.append(f"  读取耗时  {plan.seconds:.1f} 秒")
    if plan.skipped:
        out.append(f"  跳过      {len(plan.skipped)} 个（非媒体类型或读取失败）")

    by_city = plan.plan.by_city()
    if by_city:
        out.append("  按城市    " + "，".join(f"{city} {n}" for city, n in list(by_city.items())[:8]))
    by_how = plan.plan.by_city_how()
    if by_how:
        out.append("  城市依据  " + "，".join(f"{how} {n}" for how, n in by_how.items()))
    by_library = plan.by_library()
    if by_library:
        out.append(
            "  分流      " + "，".join(f"Library #{lib} {n} 个" for lib, n in sorted(by_library.items()))
        )
    out.append(f"  时段数    {len(plan.plan.sessions)} 个")
    renamed = [f for f in plan.plan.files if "__" in f.name]
    if renamed:
        out.append(f"  同名改名  {len(renamed)} 个（加 __N 后缀，绝不覆盖）")
    if plan.plan.unknown_city_count:
        out.append(f"  地点未知  {plan.plan.unknown_city_count} 个 → {UNKNOWN_CITY}")
    if plan.plan.unresolved_cells:
        out.append(
            f"  未命中网格 {len(plan.plan.unresolved_cells)} 个："
            + "，".join(plan.plan.unresolved_cells[:5])
            + "（可用 `photoos geo assign` 指认后回写缓存）"
        )
    for note in plan.notes:
        out.append(f"  说明      {note}")

    if plan.plan.files:
        out.append("")
        out.append("  计划明细（最多 15 条）")
        for item in plan.plan.files[:15]:
            city_how = item.city_how
            flag = "  [时区未知]" if item.time_unknown else ""
            out.append(f"    {item.name}  →  {item.target_relative}   [{city_how}]{flag}")
        if len(plan.plan.files) > 15:
            out.append(f"    ……另有 {len(plan.plan.files) - 15} 条（用 --plan-out 导出全部）")
    return "\n".join(out)


def _write_plan_csv(path: Path, plan) -> None:
    """导出归类计划 CSV（可审计：含判定依据，便于复核"为什么归到这个城市"）。"""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Source", "Target", "Kind", "City", "CityHow", "Session",
             "LocalTime", "TimeHow", "TimeUnknown", "Size"]
        )
        by_key = {entry.item.key: entry for entry in plan.entries}
        for item in plan.plan.files:
            entry = by_key.get(item.key)
            writer.writerow([
                str(entry.path) if entry else item.key,
                item.target_relative,
                item.kind,
                item.city,
                item.city_how,
                item.session,
                item.local_time.strftime("%Y-%m-%d %H:%M:%S"),
                item.time_how,
                "yes" if item.time_unknown else "",
                item.size or "",
            ])
    _ok(f"归类计划已写入：{path}")


def cmd_geo(args: argparse.Namespace) -> int:
    """离线地理缓存：查看 / 人工指认。

    缓存是**纯离线**的（`data_root/geo-cache.csv`，格式与旧工具 `resolve-cities.ps1`
    的产物兼容），因此导入分类永远不会联网。未命中的网格会在预演里单列出来，
    由你用本命令指认后回写 —— 下次同类地点就自动命中。
    """
    from .library.geo import GeoCache, cell_key

    settings, _db = _open_db()
    cache_path = Path(args.geo_cache) if args.geo_cache else GeoCache.default_path(settings.data_root)
    cache = GeoCache.load(cache_path)

    if args.geo_command == "list":
        _print(f"地理缓存 · {cache_path}")
        _kv("条目数", f"{len(cache)}（{len(cache.cities())} 个城市）")
        if not len(cache):
            _print()
            _warn("缓存为空。未命中的地点都会归入「未知地点」。")
            _print("  用 `photoos geo assign <纬度,经度> <城市名>` 人工指认。")
            return 0
        for city, count in cache.cities().items():
            _kv(f"  {city}", f"{count} 个网格")
        return 0

    # assign
    raw = args.coordinates
    try:
        lat_text, _, lon_text = raw.partition(",")
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        _fail(f"无法解析坐标：{raw!r}（应为 `纬度,经度`，例如 18.628868,110.213232）")
        return 1

    radius = getattr(args, "radius_km", None)
    if radius:
        written, skipped = cache.assign_radius(lat, lon, float(radius), args.city)
        _ok(f"已把 ({lat}, {lon}) 半径 {radius:g} km 内的 {written} 个网格指认为「{args.city}」")
        if skipped:
            _kv("跳过", f"{skipped} 个网格（原有机器反查结果，未被覆盖）")
    else:
        entry = cache.assign(lat, lon, args.city)
        _ok(f"已指认 {entry.cell} → {entry.city}")
    written_path = cache.save(cache_path)
    _kv("缓存", written_path)
    _print("  下次导入预演时，落在这些网格里的素材就会归到该城市。")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """修复派生数据与数据库不一致（**只动派生缓存与索引，绝不动原片**）。"""
    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)

    from .repos import PreviewRepo

    previews = PreviewRepo(db)
    _print(f"派生数据对账 · Library「{library.name}」")
    broken = previews.broken_records(settings.data_root, library.id)
    _kv("记录数", previews.count(library_id=library.id))
    _kv("磁盘文件缺失的记录", len(broken))

    if not broken:
        _ok("派生缓存与数据库一致，无需修复")
        return 0

    by_kind: dict[str, int] = {}
    for _asset_id, kind, _path in broken:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    _kv("按类型", "，".join(f"{k}={v}" for k, v in sorted(by_kind.items())))

    if not args.apply:
        _print()
        _warn("这是预演。加 --apply 才会删除这些失效记录以便重建：")
        _print("        photoos repair --apply  &&  photoos index")
        return 0

    removed = previews.clear_for_assets(sorted({asset_id for asset_id, _, _ in broken}))
    _ok(f"已删除 {removed} 条失效预览记录（未触碰任何原片）")
    _print()
    _print("下一步：运行 `photoos index` 重新生成缩略图与预览。")
    return 0


def cmd_breakpoint(args: argparse.Namespace) -> int:
    """执行 PRD 断点测试并输出报告。

    `--phase 1`（默认）= §9.1 的 11 项；`--phase 2` = §9.2 的 9 项（精选系统）。
    """
    phase = int(getattr(args, "phase", 1))
    from .breakpoint import BreakpointRunner, Phase2BreakpointRunner

    settings, db = _open_db()
    library = _pick_library(library_store.LibraryStore(db), args.library)

    if phase == 2:
        _print("Phase 2 断点测试（PRD §9.2 精选系统）")
        _kv("Library", f"#{library.id} {library.name}")
        _kv("路径", library.root_path_hint or "(未记录)")
        _print()
        _print("说明：需要物理拔盘、手机照片素材或第二台电脑的验收项无法自动化，")
        _print("      报告里会给出可照做的步骤并标记为「需人工执行」——不会被假装通过。")
        _print()
        report = Phase2BreakpointRunner(db, settings, library.id).run()
    else:
        _print("Phase 1 断点测试（PRD §9.1）")
        _kv("Library", f"#{library.id} {library.name}")
        _kv("路径", library.root_path_hint or "(未记录)")
        _kv("模式", "全库全量" if args.full else f"代表性时段全量（{args.periods} 个时段）")
        _print()
        _print("说明：需要物理操作硬盘的验收项无法自动化，报告里会给出可照做的步骤并标记为「需人工执行」。")
        _print()
        report = BreakpointRunner(db, settings, library.id).run(periods=args.periods, full=args.full)

    for case in report.cases:
        marker = {"通过": "[OK]  ", "失败": "[失败]", "需人工执行": "[人工]", "跳过": "[跳过]"}.get(
            case.status, "[?]   "
        )
        _print(f"{marker} #{case.number} {case.title}")
        if case.detail:
            _print(f"          {case.detail}")
    _print()
    _print(f"结果：{report.overall}（通过 {report.passed}，失败 {report.failed}，需人工 {report.manual}）")

    default_name = f"breakpoint-phase{phase}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    output = Path(args.output).expanduser() if args.output else (settings.logs_dir / default_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_markdown(), encoding="utf-8")
    _print(f"报告已写入：{output}")

    if report.manual:
        _print()
        _print("以下验收项需要你自己动手完成（步骤见报告）：")
        for case in report.cases:
            if case.status == "需人工执行":
                _print(f"  - 断点 #{case.number} {case.title}")

    return 0 if report.failed == 0 else 1


def cmd_crosscheck(args: argparse.Namespace) -> int:
    """与既有 PowerShell 工具的产物交叉校验（两套独立实现互证）。

    **只读**：不写照片库、不改既有 CSV。
    """
    from .crosscheck import crosscheck

    settings, db = _open_db()
    library = _pick_library(library_store.LibraryStore(db), args.library)

    legacy_index = Path(args.legacy_index).expanduser() if args.legacy_index else None
    legacy_hashes = Path(args.legacy_hashes).expanduser() if args.legacy_hashes else None
    if legacy_index is None and legacy_hashes is None:
        _warn("未指定 --legacy-index / --legacy-hashes，无法比对。示例：")
        _print('        photoos crosscheck --library 1 --legacy-index "C:\\Photos\\_归类清单\\library-index.csv"')
        return 2

    _print("交叉校验（只读）")
    report = crosscheck(
        db,
        settings,
        library.id,
        legacy_index=legacy_index,
        legacy_hashes=legacy_hashes,
        hash_samples=args.hash_samples,
    )
    _print()
    for line in report.lines():
        _print(line)
    return 0 if report.verified else 1


def cmd_serve(args: argparse.Namespace) -> int:
    """启动本地 Web UI（默认仅监听 127.0.0.1，见 PRD §2.6 本地优先）。"""
    import contextlib
    import uvicorn

    from .api import create_app

    settings, db = _open_db()
    db.integrity_check()  # 启动前快速确认库可读
    # 升级后首次启动要把库迁到当前 schema：桌面壳只会跑 `serve`，
    # 不在启动时迁移的话，旧数据根会缺新表（v0.2.0 的 asset_reject 就是这样暴露的）。
    # `apply_migrations` 自己会先备份；已是最新时是空操作。
    migrated = apply_migrations(
        db, backup_dir=settings.backups_dir, app_version=__version__
    )
    if migrated.applied:
        _ok(f"已应用迁移 {', '.join(migrated.applied)}")
        if migrated.backup_path is not None:
            _kv("迁移前备份", migrated.backup_path)
    host = args.host or settings.host
    port = int(args.port or settings.port)

    # 日志去处：`--log-file` 不带值 = 数据目录的 `logs/serve.log`。
    #
    # 为什么值得有这么一个参数：uvicorn 默认把**每一个请求**都打到 stdout，
    # 一个人一天的浏览就能写出一兆多的行。谁把它启动在"会被回收的后台"里
    # （任务包装器、终端复用器、CI），这些输出就可能把那一侧的缓冲撑爆，
    # 连带把服务一起收掉 —— 本体没崩，用户却打不开界面了。
    # 落到文件里既省事又留证据。
    log_path = None
    if getattr(args, "log_file", None) is not None:
        raw = str(args.log_file).strip()
        log_path = Path(raw) if raw else settings.logs_dir / "serve.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

    _print("PhotoOS 本地服务")
    _kv("数据根", settings.data_root)
    _kv("数据库", settings.db_path)
    _kv("前端目录", settings.resolved_web_root())
    _kv("监听地址", f"http://{host}:{port}")
    if log_path is not None:
        _kv("日志", log_path)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        _warn("服务绑定到非回环地址：本项目没有鉴权，请确认网络环境可信。")
    _warn_embedding_runtime(settings)
    _print()
    _print("按 Ctrl+C 停止。")

    app = create_app(settings)
    if log_path is None:
        uvicorn.run(app, host=host, port=port, log_level=args.log_level)
        return 0

    # 写文件：把 **root logger** 换成文件 handler。uvicorn 的 logger 是 root 的子节点，
    # 所以这一处就能同时覆盖 uvicorn / access / 应用自己的三路输出。
    # `log_config=None` 是关键：它告诉 uvicorn **不要**再套一层自己的 dictConfig，
    # 否则刚挂上的 handler 会被覆盖掉（日志照样全打到控制台）。
    import logging

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(str(args.log_level).upper())
    uvicorn.run(app, host=host, port=port, log_level=args.log_level, log_config=None)
    return 0


def _warn_embedding_runtime(settings: Any) -> None:
    """启动时检查"库里有向量 / 有模型，但运行时不可用"这种**自相矛盾**的状态。

    为什么值得在启动时提醒：`embed` 是**可选依赖**（`[ai-gpu]`），
    而产品 venv 默认不装它。用户完全可能在"装了运行时的 venv 里跑过 embed"
    之后，用另一个 venv 启动服务 —— 这时界面上一切正常，
    但**新导入的照片算不出向量**，于是它们在 Scene 划分里只能按时间规则处理。

    ⚠️ **措辞必须是准确的**（这一条改过一次，原因是原来那句是错的）：
    原文写「依赖视觉相似度的功能会退化为纯时间规则」—— 实测**不成立**。
    `onnxruntime` 只有两处用得到：`photoos embed` 与 `photoos ai doctor`。
    相似度**消费端**（`scenes rebuild`）读的是**库里已经算好的向量**，只用 numpy，
    所以"没装运行时"**不会**让既有相似度失效 —— 这一点在真实库上实测过：
    产品 venv 跑 `scenes rebuild` 得到 303 个 Scene（`--no-similarity` 是 371），
    相似度确实生效。说成"退化"会让用户以为自己看到的分组是假的。

    ⚠️ 这里**刻意不跑完整自检**：`ai doctor` 要加载模型并真跑一次推理
    （实测 5.22 ms/张 + 模型加载），把它塞进 `serve` 启动路径会明显拖慢启动，
    而且模型缺失时会变成"服务起不来"。完整自检留给 `photoos ai doctor` 显式调用。
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        has_model = (settings.data_root / "ai_cache" / "models").is_dir()
        if not has_model:
            return                      # 既没运行时也没模型 → 与"没启用嵌入"无异，不必吓人
        already, total_vectors = _embedded_vector_count(settings)
        _warn(
            "嵌入运行时（onnxruntime）未安装，但数据根里已有模型 —— "
            "本环境**算不了新的向量**"
            f"（`photoos embed` 会失败），新导入的照片不会有向量，"
            "它们在 Scene 划分里只能按时间规则处理。"
        )
        if already:
            extra = ""
            if total_vectors > already:
                extra = (f"（库里另有 {total_vectors - already:,} 个**其他模型**的向量，"
                         f"它们不参与 Scene 划分）")
            _print(f"        已经算好的 {already:,} 个向量**不受影响**："
                   "浏览、筛选、精选、以及已生成的 Scene 划分都不需要这个运行时。" + extra)
        else:
            _print("        目前库里还没有任何向量 —— 全部 Scene 划分都会退化为纯时间规则。")
        _print("        如需计算向量：安装 `[ai-gpu]` 可选依赖后重启服务；")
        _print("        完整自检请运行 `photoos ai doctor`（会真跑一次推理计时）。")


def _embedded_vector_count(settings: Any) -> tuple[int, int]:
    """返回 `(Scene 划分实际使用的向量条数, 库里全部向量条数)`。

    **为什么要分成两个数**（Phase 3 多模型并存后新增）：
    `embedding` 表可以同时存多个模型的向量（PRD §9.3 断点 #6「新旧并存不覆盖」）。
    Phase 2 起 Scene 划分用的是 **CLIP ViT-B/32** 那一版；
    Phase 3 加入 EmbeddingGemma 2（768 维）后，**全表计数会把两个模型加在一起**，
    于是启动提示会谎报"42,800 个向量不受影响" —— 而实际参与划分的只有 21,400 个。
    这正是本项目反复出现的那类错误（说错后果的提示比没有提示更糟，见交付文档 §6.17）。

    **只读、且刻意容错**：这是启动路径上的提示，不能因为"库还是老 schema"
    或"文件被占用"就让 `serve` 起不来。
    """
    from .library import model_store

    try:
        with Database(settings.db_path, read_only=True).read() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0])
            used = int(conn.execute(
                "SELECT COUNT(*) FROM embedding WHERE model = ? AND model_version = ?"
                " AND prep_version = ?",
                (model_store.MODEL_NAME, model_store.MODEL_VERSION, model_store.PREP_VERSION),
            ).fetchone()[0])
            return used, total
    except Exception:  # noqa: BLE001 - 提示失败不影响启动
        return 0, 0


def cmd_integrity(args: argparse.Namespace) -> int:
    """检查数据库完整性、外键一致性与索引统计。"""
    settings, db = _open_db()
    _print("数据库完整性检查")
    _kv("数据库", settings.db_path)
    _kv("schema 版本", f"{db.schema_version()}/{target_version()}")

    check = db.integrity_check()
    if check == "ok":
        _ok("integrity_check 通过")
    else:
        _fail(f"integrity_check 异常：{check}")

    violations = db.foreign_key_check()
    if not violations:
        _ok("foreign_key_check 通过")
    else:
        _fail(f"外键违规 {len(violations)} 处")
        for row in violations[:10]:
            _print(f"        {tuple(row)}")

    orphan_assets = db.scalar(
        "SELECT COUNT(*) FROM asset WHERE library_id NOT IN (SELECT id FROM library)", default=0
    )
    orphan_meta = db.scalar(
        "SELECT COUNT(*) FROM metadata WHERE asset_id NOT IN (SELECT id FROM asset)", default=0
    )
    orphan_preview = db.scalar(
        "SELECT COUNT(*) FROM preview WHERE asset_id NOT IN (SELECT id FROM asset)", default=0
    )
    _kv("孤立 asset", orphan_assets)
    _kv("孤立 metadata", orphan_meta)
    _kv("孤立 preview", orphan_preview)

    _print()
    _print("各 Library 统计")
    store = library_store.LibraryStore(db)
    for library in store.list_all():
        stats = pipelines.library_stats(db, library.id)
        _kv(
            f"#{library.id} {library.name}",
            f"Asset {stats['assets']}，元数据 {stats['metadata_rows']}，"
            f"预览 {stats['previews']}，{int(stats['assets_bytes']) / 1e9:.2f} GB",
        )

    ok = check == "ok" and not violations and not (orphan_assets or orphan_meta or orphan_preview)
    _print()
    if ok:
        _ok("全部检查通过")
        return 0
    _fail("存在需要处理的问题")
    return 1


def cmd_index_impl(args: argparse.Namespace) -> int:
    """完整索引：发现 → 元数据 → 预览 → 融合（一次读盘）→ 兜底。

    默认只跑创建的任务并立即返回；加 `--wait` 则原地跑完该 Library 的**全部剩余工作**，
    顺序为：**发现（一次）** → 轮询「元数据 + 融合」直到剩余量为 0（最多 5 轮）。

    两条关键纪律：

    * **发现阶段必须包含在内**：否则磁盘上的新文件永远不会进入索引，而命令仍会
      打印「索引完成」（这就是缺陷 #12 —— 全新库磁盘上有 4 个文件却报 Asset 0）。
    * "全部"是按**剩余量**判定的（每轮重新统计），而不是按任务是否存在 ——
      早先用"任务是否在跑"判定，遇到被强杀遗留的孤儿任务就会跳过整个阶段，
      最后打印"全部完成"而实际漏掉 3,829 个文件的元数据。
    """
    if not args.wait:
        return _run_job(args, "full_index", {})

    from .jobs import JobQueue, JobRunner, executor_id
    from .jobs.pipelines import library_stats
    from .repos import AssetRepo

    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    queue = JobQueue(db)
    runner = JobRunner(db, settings)
    owner = executor_id()
    repo = AssetRepo(db)

    recovered = queue.recover_orphans(stale_after_seconds=60, owner=owner)
    if recovered:
        _ok(f"回收了 {recovered} 个因进程中断而卡住的任务")

    def remaining() -> dict[str, int]:
        return {
            "元数据": len(repo.needing_metadata(library.id)),
            "预览": len(repo.needing_previews(library.id)),
            "哈希": len(repo.unhashed(library.id)),
        }

    #: 本次运行中**已被确认失败**的文件（相对路径）。用于区分"还没做"与"做不到"：
    #: 原片损坏/不可解码的文件会让 needing_* 永远返回它，重试多少轮结果都一样。
    stuck: set[str] = set()

    def run_stage(stage: str, label: str | None = None) -> bool:
        """创建并**原地**执行一个阶段任务；返回是否 COMPLETED。"""
        job = queue.create(stage, library_id=library.id)
        claimed = queue.claim_next(owner=owner)
        if claimed is None or claimed.id != job.id:
            _warn(f"任务 #{job.id} 被其他执行者占用，跳过")
            return False
        started = time.perf_counter()
        report = runner.run(queue.get(job.id))
        _print(f"  {label or stage}: {report.status}（{time.perf_counter() - started:.1f}s）")
        for line in report.lines()[1:]:
            _print("    " + line)
        for row in queue.failed_items(job.id, limit=100_000):
            stuck.add(str(row["ref_key"]))
        return report.status == "COMPLETED"

    _print(f"完整索引 · Library「{library.name}」· 路径 {library.root_path_hint}")
    overall = time.perf_counter()

    # 发现阶段必须先跑，且**只跑一次**。
    # `--wait` 的语义是"跑完该 Library 的剩余工作"，而"剩余工作"包含**还没进索引的新文件**：
    # 若只在 metadata/fused 上循环，新增文件会被彻底忽略，却照样打印「索引完成」。
    # 这曾导致全新库里磁盘上有文件而报告 Asset 0（缺陷 #12）。
    # 不放进循环的原因：发现要遍历整棵树（实测 16k 文件 43.7 秒），循环 5 轮会白扫 5 遍。
    _print()
    _print("第 0 轮，发现阶段（遍历库以识别新增/变化的文件）")
    run_stage("scan", "发现")

    for round_index in range(1, 6):
        left = remaining()
        if not any(left.values()):
            break
        _print()
        _print(f"第 {round_index} 轮，剩余：{left}")
        for stage in ("metadata", "fused"):
            if not any(remaining().values()):
                break
            if not run_stage(stage):
                _warn(f"阶段 {stage} 未完成，下一轮重试")
                break

    # 状态收敛：让 `status='OK'` 严格等价于「文件在库 + 哈希 + 元数据 + 缩略图 + 预览齐备」。
    # 否则会出现两个方向的撒谎：由「哈希（兜底）」补齐的资产永远停在 PENDING；
    # 而元数据阶段乐观置 OK、随后预览失败（原片损坏）的资产则带着 OK 却没有缩略图（缺陷 #13）。
    promoted, demoted = repo.reconcile_status(library.id)
    if promoted or demoted:
        _print(f"  （状态收敛：{promoted} 个升为 OK，{demoted} 个降回 PENDING）")

    left = remaining()
    _print()
    if any(left.values()):
        # 区分「还没做」与「做不到」：若剩余项**全部**是本次已失败过的文件，
        # 说明原片本身有问题（损坏 / 不可解码），再重试多少轮结果都一样 ——
        # 此时报出数量并以成功退出，而不是把用户卡在一个永远失败的命令上。
        pending_rel = {
            asset.relative_path
            for group in (
                repo.needing_metadata(library.id),
                repo.needing_previews(library.id),
                repo.unhashed(library.id),
            )
            for asset in group
        }
        untouched = pending_rel - stuck
        if pending_rel and not untouched:
            _warn(
                f"{len(pending_rel)} 个文件无法生成派生数据（原片可能损坏或格式无法解码），"
                "已跳过；其余全部完成。"
            )
            _print("  可用 `photoos jobs show <id>` 查看失败明细；这些文件不会自行恢复。")
            stats = library_stats(db, library.id)
            _ok(
                f"索引完成（部分派生数据缺失）：Asset {stats['assets']}，"
                f"已哈希 {stats['hashed']}，元数据 {stats['metadata_rows']}，预览文件 {stats['previews']}"
            )
            _print(f"  耗时 {time.perf_counter() - overall:.1f} 秒")
            return 0
        _fail(f"仍有剩余工作：{left}（其中 {len(untouched)} 个尚未尝试）")
        return 1
    stats = library_stats(db, library.id)
    _ok(
        f"索引完成：Asset {stats['assets']}，已哈希 {stats['hashed']}，"
        f"元数据 {stats['metadata_rows']}，预览文件 {stats['previews']}"
    )
    _print(f"  耗时 {time.perf_counter() - overall:.1f} 秒")
    return 0


def cmd_fused(args: argparse.Namespace) -> int:
    """融合阶段：**一次读盘**同时算内容哈希与生成缩略图/预览。

    相比分别跑 `hash` 与 `preview`（各读一遍原片），本命令对每个文件只读一次。
    适合在刚导入一批照片、既没有哈希也没有预览时使用。
    """
    scope: dict[str, Any] = {}
    if args.limit:
        scope["limit"] = int(args.limit)
    _print("说明：只处理「既没有哈希、也没有预览」的文件，因此不会重复读盘。")
    return _run_job(args, "fused", scope)


def cmd_preview(args: argparse.Namespace) -> int:
    """预览阶段：只生成缩略图与大图预览（**不重算内容哈希**）。

    存在的理由：融合阶段可能报「哈希成功但预览失败」（原片损坏、RAW 解码失败等），
    此时需要一条**只补预览**的路径；否则用户按提示操作会找不到命令（缺陷 #15）。
    `--prefix` 可把范围收敛到一个子树，`--force` 用于重建。
    """
    scope: dict[str, Any] = {}
    if args.limit:
        scope["limit"] = int(args.limit)
    if getattr(args, "prefix", None):
        scope["scope_prefix"] = args.prefix.replace("/", "\\").strip("\\")
    if getattr(args, "regenerate", False):
        scope["force"] = True
        _warn("--regenerate：将为该范围内**全部**在库文件重新生成缩略图与预览（需读原片，耗时）。")
    else:
        _print("说明：默认只处理「缺少缩略图或预览」的文件，已生成的不重复生成。")
    return _run_job(args, "preview", scope)


def cmd_duplicates(args: argparse.Namespace) -> int:
    """基于内容哈希报告重复文件。**只报告，不移动、不删除**（PRD §11.1）。"""
    _, db = _open_db()
    store = library_store.LibraryStore(db)
    library = _pick_library(store, args.library)
    report = pipelines.build_duplicate_report(db, library.id, sample_limit=args.samples)

    _print(f"重复文件报告 · Library「{library.name}」")
    _print(f"  {report.summary()}")
    if report.groups == 0:
        _print()
        _print("  没有发现重复内容。若刚导入尚未哈希，请先运行 `photoos hash`。")
        return 0

    hashed = db.scalar(
        "SELECT COUNT(*) FROM asset WHERE library_id = ? AND content_hash IS NOT NULL",
        (library.id,),
        default=0,
    )
    total = db.scalar("SELECT COUNT(*) FROM asset WHERE library_id = ?", (library.id,), default=0)
    _print(f"  已哈希 {hashed}/{total} 个文件（基数越大越完整）")
    _print()
    for digest, paths in report.samples:
        _print(f"  组 {digest[:16]}…（{len(paths)} 个内容相同的文件）")
        for path in paths[:6]:
            _print(f"    - {path}")
        if len(paths) > 6:
            _print(f"    … 其余 {len(paths) - 6} 个")
    _print()
    _print("  说明：PhotoOS 不会自动隔离或删除这些文件。是否需要清理由你决定；")
    _print("        既有 PowerShell 工具曾把它们移入 `_归类清单\\_重复文件（确认后可删除）`。")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """显示 Library 的索引统计（数量、类型、状态、元数据与预览覆盖）。"""
    _, db = _open_db()
    store = library_store.LibraryStore(db)
    libraries = store.list_all()
    if not libraries:
        _print("尚未登记任何 Library。")
        return 0

    targets = (
        [_pick_library(store, args.library)] if args.library or len(libraries) == 1 else libraries
    )
    for library in targets:
        stats = pipelines.library_stats(db, library.id)
        _print(f"#{library.id} {library.name}（{library.status}）")
        _kv("路径", library.root_path_hint or "(未记录)")
        _kv("Asset 总数", stats["assets"])
        _kv("总字节", f"{int(stats['assets_bytes']) / 1e9:.2f} GB")
        by_type = stats["by_type"] or {}
        if by_type:
            _kv("类型分布", "，".join(f"{k}={v}" for k, v in sorted(by_type.items())))
        by_status = stats["by_status"] or {}
        if by_status:
            _kv("状态分布", "，".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        _kv(
            "拍摄时间覆盖",
            f"{stats['metadata_with_capture_time']}/{stats['metadata_rows']}",
        )
        quality = stats.get("time_quality") or {}
        if stats["metadata_rows"]:
            _kv(
                "时间质量",
                f"本地时间 {quality.get('local_time', 0)}，"
                f"仅 UTC（时区未知）{quality.get('utc_only', 0)}，"
                f"时区已知 {quality.get('tz_known', 0)}，无时间 {quality.get('no_time', 0)}",
            )
        _kv(
            "缩略图/预览",
            f"{stats['previews']} 个文件，{int(stats['previews_bytes']) / 1e9:.2f} GB"
            f"（每个文件含缩略图+预览两份，故约等于 {stats['previews'] // 2} 个 Asset）",
        )
        versions = stats["preview_generator_versions"] or {}
        if versions:
            _kv("生成器版本", "，".join(f"{k}={v}" for k, v in versions.items()))
        _print()
    return 0


def cmd_scenes_rebuild(args: argparse.Namespace) -> int:
    """按时间（+ 可选相似度）把时段切成 Scene（Phase 2 · M4）。

    默认**只预演**。相似度信号需要嵌入（M3），未落库时自动退化为纯时间规则 ——
    与 M2 的 Shot 分组同一套降级姿态：**不因为嵌入没跑就让功能不可用**。
    """
    from .library import scene_grouping as scene_mod
    from .library import shot_grouping as grouping

    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    if not store.list_all():
        _print("尚未登记任何 Library。")
        return 0

    library_ids = None
    if args.library:
        library_ids = [_pick_library(store, args.library).id]

    gap, gap_src = _resolve_gap(settings, db, args.session, args.gap_seconds)

    def _pick(value, fallback):
        return value if value is not None else fallback

    # 五个 Scene 阈值：**命令行显式给了就用命令行的，否则读配置**
    # （`data/config.json` 里可以改；界面触发的 `scenes_rebuild` 走的就是配置值）。
    scene_gap = float(_pick(args.scene_gap_seconds, settings.scene_gap_seconds))
    params = scene_mod.SceneParams(
        gap_seconds=scene_gap,
        merge_threshold=float(_pick(args.scene_merge_similarity, settings.scene_merge_similarity)),
        dissimilar_run=int(_pick(args.scene_dissimilar_run, settings.scene_dissimilar_run)),
        max_shots=int(_pick(args.max_shots, settings.scene_max_shots)),
        unrelated_similarity=float(
            _pick(args.scene_unrelated_similarity, settings.scene_unrelated_similarity)
        ),
    )

    assets = _load_shot_assets(db, library_ids)
    periods = grouping.group_by_directory(assets, gap_seconds=gap)

    # 相似度信号：**先看库里的嵌入**。嵌入是 asset 级的（决策 #7 追问后的变更），
    # 所以这里按 asset_id → 矩阵行号建索引，再由 `build_similarity_callback`
    # 把"两组资产之间的最大余弦"包成 split_all 要的回调。
    # 没有嵌入时回调为 None → 第 ② 步整段跳过（不猜、不默认 1.0）。
    similarity_factory, sim_note = _build_scene_similarity(db, library_ids)
    if args.no_similarity:
        similarity_factory, sim_note = None, "已按 --no-similarity 关闭（纯时间规则）"
    similarity = similarity_factory(periods) if similarity_factory is not None else None
    _print("Scene 划分（默认只预演；不读取任何照片文件）")
    _kv("参与资产", len(assets))
    _kv("时段数", len(periods))
    _kv("shot_gap_seconds", f"{gap:g} s   ← {gap_src}")
    _kv("scene_gap_seconds", f"{scene_gap:g} s")
    _kv("scene_merge_similarity", f"{params.merge_threshold:g}")
    _kv("scene_dissimilar_run", params.dissimilar_run)
    _kv("scene_max_shots", params.max_shots)
    _kv("scene_unrelated_similarity", f"{params.unrelated_similarity:g}")
    _kv("相似度信号", sim_note)
    _print()

    drafts = scene_mod.split_all(periods, params=params, similarity=similarity)
    stats = scene_mod.summarize([d for v in drafts.values() for d in v])

    _kv("Scene 总数", stats["scenes"])
    _kv("Shot 总数", stats["shots"])
    _kv("每 Scene 的 Shot 数", f"最小 {stats['min_shots']}  中位附近 {stats['mean_shots']:.1f}  最大 {stats['max_shots']}")
    _kv("落在 PRD 20–50 区间", f"{stats['within_prd_band']} 个 Scene")
    _kv("超过上限的 Scene", f"{stats['over_max']} 个（必须为 0）")
    _kv("含建议人工确认的 Scene", stats["needs_review"])
    _print()

    over = [d for v in drafts.values() for d in v if d.size > params.max_shots]
    if over:
        _fail(f"有 {len(over)} 个 Scene 超过上限 —— 实现有 bug")
        return 2
    _ok("全部 Scene 都在上限内 —— 断点 #2 的「20–50 张 Shot」规模可达")

    if args.json:
        _print(json.dumps(
            {
                "params": params.to_json() | {"shot_gap_seconds": gap},
                "summary": stats,
                "by_period": {
                    "\\".join(k): [{"shots": d.size, "assets": d.asset_count,
                                    "from": d.captured_from, "to": d.captured_to,
                                    "boundaries": [b.kind for b in d.boundaries]}
                                   for d in v]
                    for k, v in drafts.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    biggest = sorted(
        ((("\\".join(k), d.size, d.asset_count, d.captured_from, d.captured_to)) for k, v in drafts.items() for d in v),
        key=lambda row: -row[1],
    )[:10]
    _print("  最大的 10 个 Scene：")
    for name, shots_n, assets_n, start, end in biggest:
        _print(f"    {name[:38]:38s} Shot {shots_n:4d}  资产 {assets_n:5d}  {str(start)[:16]} ~ {str(end)[11:16]}")
    _print()

    if not args.apply:
        _print("【预演】未写入数据库。Scene 只是**可重算的派生线索**（铁律 #3）。")
        if db.scalar("SELECT COUNT(*) FROM shot", default=0) == 0:
            _warn("shot 表还是空的：落库前请先回填 session 并重算 Shot。")
        return 0

    # ---- 落库：先确保 Shot 已写入（Scene 通过 shot.scene_id 关联）
    from .repos import ShotRepo

    repo = ShotRepo(db)
    before = repo.assert_invariants()
    if before["shots_active"] == 0:
        _fail(
            "shot 表为空，无法挂 Scene。请先执行：\n"
            "        photoos sessions backfill --apply\n"
            "        photoos shots regroup --apply"
        )
        return 2

    # 把落库后的 shot_id 映射回每个 Scene 的 Shot。
    # 用 `(primary_asset_id, 资产数)` 做键而不是 `list.index(obj)` ——
    # `ShotGroup` 是可变 dataclass（不可哈希），而 `index()` 在 8,000 个 Shot 上是 O(n²)。
    def _key(group) -> tuple[int, int]:
        return (int(group.primary_asset_id), group.size)

    flat = [g for v in periods.values() for g in v]
    _stats, mapping = _persist_shots(db, flat, library_ids=library_ids)
    shot_id_by_key = {
        _key(flat[index]): shot_id for index, shot_id in mapping.items()
    }

    created = updated = skipped_pinned_sessions = skipped_pinned_scenes = 0
    for (city, period), scene_drafts in drafts.items():
        session_row = db.query_one(
            "SELECT id FROM session WHERE city_hint = ? AND period_hint = ?", (city, period)
        )
        if session_row is None:
            continue
        rows = []
        for draft in scene_drafts:
            shot_ids = [
                shot_id_by_key[key]
                for key in (_key(s) for s in draft.shots)
                if key in shot_id_by_key
            ]
            rows.append(
                {
                    "name": f"{city}\\{period}\\{draft.captured_from or ''}",
                    "captured_from": draft.captured_from,
                    "captured_to": draft.captured_to,
                    "dir_hint": f"{city}\\{period}",
                    "confidence": None,
                    "shot_ids": shot_ids,
                    "evidence_json": json.dumps(
                        {
                            "params": params.to_json(),
                            "size": draft.size,
                            "assets": draft.asset_count,
                            "boundaries": [
                                {"kind": b.kind, "between": list(b.between), "detail": b.detail}
                                for b in draft.boundaries
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        result = repo.replace_scenes(session_id=int(session_row["id"]), scenes=rows)
        created += result["created"]
        updated += result["updated"]
        if result.get("skipped_pinned"):
            skipped_pinned_sessions += 1
            skipped_pinned_scenes += int(result["skipped_pinned"])

    after = repo.assert_invariants()
    if after["selections"] < before["selections"] or after["finals"] < before["finals"]:
        _fail("重建 Scene 导致精选记录减少 —— 这是严重缺陷，请从备份恢复。")
        return 2

    from .db import utc_now as _utc_now

    with db.write() as conn:
        conn.execute(
            "INSERT INTO app_event (at, kind, library_id, payload_json)"
            " VALUES (?, 'scenes_rebuild', NULL, ?)",
            (
                _utc_now(),
                json.dumps(
                    {"created": created, "updated": updated, "params": params.to_json()},
                    ensure_ascii=False,
                ),
            ),
        )

    _print("Scene 划分 · 已执行")
    _ok(f"新建 {created} 个 Scene，更新 {updated} 个")
    if skipped_pinned_sessions:
        _kv("因含人工调整而跳过的时段",
            f"{skipped_pinned_sessions} 个（{skipped_pinned_scenes} 个 Scene 被标为 source='user'）")
        _print("        这些时段的 Scene 是你手工合并/拆过的，重算不会覆盖；"
               "想交还给自动算法可跑 `photoos scenes unpin <scene_id>`。")
    _kv("Scene 总数", after["scenes"])
    _kv("活跃 Shot 总数", after["shots_active"])
    _kv("精选记录（前→后）", f"{before['selections']} → {after['selections']}（只增不减 ✔）")
    _print("        原始照片文件全程只被读取，未发生任何移动/改名/覆盖/删除。")
    _ = scene_mod
    return 0


# ------------------------------------------------------- scenes 人工调整（合并/拆分）
def cmd_scenes_merge(args: argparse.Namespace) -> int:
    """把第二个 Scene 合并进第一个（搬 Shot、扩时间范围、删掉多余那行）。"""
    from .repos import ShotRepo

    _settings, db = _open_db()
    try:
        result = ShotRepo(db).merge_scenes(int(args.primary), int(args.secondary))
    except ValueError as exc:
        _fail(str(exc))
        return 2
    _ok(f"已把 Scene #{result['merged_from']} 并入 #{result['scene_id']}"
        f"（搬了 {result['moved_shots']} 个 Shot）")
    _print("        该 Scene 已标记 source='user'：下一次 `scenes rebuild` 会**跳过这个时段**。")
    _print("        想交还给自动算法：`photoos scenes unpin "
           f"{result['scene_id']}`，然后再重算。")
    return 0


def cmd_scenes_split(args: argparse.Namespace) -> int:
    """在指定 Shot 之后把 Scene 一分为二（前段保留原 Scene）。"""
    from .repos import ShotRepo

    _settings, db = _open_db()
    try:
        result = ShotRepo(db).split_scene(
            int(args.scene), int(args.after_shot), name=args.name
        )
    except ValueError as exc:
        _fail(str(exc))
        return 2
    _ok(f"已从 Scene #{result['scene_id']} 拆出 Scene #{result['new_scene_id']}"
        f"（前段 {result['head_shots']} 个 Shot，后段 {result['moved_shots']} 个）")
    _print("        两个 Scene 都已标记 source='user'；重算会跳过这个时段。")
    return 0


def cmd_scenes_unpin(args: argparse.Namespace) -> int:
    """解除人工标记，让下一次 `scenes rebuild` 可以重新接管这个 Scene。"""
    from .repos import ShotRepo

    _settings, db = _open_db()
    try:
        ShotRepo(db).unpin_scene(int(args.scene))
    except ValueError as exc:
        _fail(str(exc))
        return 2
    _ok(f"Scene #{args.scene} 已解除人工标记（source='auto'）")
    _print("        下一次 `photoos scenes rebuild --apply` 就会重算它所在的时段。")
    return 0


# -------------------------------------------------------------------------- embed
def cmd_embed(args: argparse.Namespace) -> int:
    """计算视觉嵌入并落库（Phase 2 · M3）。

    输入是**已有的 preview**（本地盘 3.8 GB），**不读原片**（413 GB）——
    实测两者等价，但 IO 差 109 倍，而且拔掉照片硬盘也能重算。
    """
    from .library import embed_pipeline, model_store
    from .library.embedding import create_embedder

    settings, db = _open_db()
    engine = getattr(args, "engine", "clip")

    # 表存在性检查：给出可操作的提示，而不是让它 500
    if not db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding'"):
        _fail(
            "`embedding` 表不存在（库还是 schema 5）。请先运行 `photoos init` 升级到 schema 6。"
        )
        return 2

    if engine == "gemma":
        from .library.embedding_gemma import DEFAULT_BATCH as GEMMA_DEFAULT_BATCH
        from .library.embedding_gemma import EmbeddingGemma2Embedder

        try:
            embedder = EmbeddingGemma2Embedder.create(settings.data_root, device=args.device)
        except Exception as exc:  # noqa: BLE001
            _fail(
                f"无法创建 EmbeddingGemma 2 编码器：{exc}\n"
                "        它需要 torch + sentence-transformers（官方**没有 ONNX 产物**）。\n"
                "        取模型：photoos ai fetch-model --engine gemma"
            )
            return 2
        if args.batch is None:
            args.batch = GEMMA_DEFAULT_BATCH
        device_note = embedder.probe().summary
    else:
        try:
            embedder = create_embedder(settings.data_root, device=args.device)
        except Exception as exc:  # noqa: BLE001
            _fail(f"无法创建编码器：{exc}")
            return 2

        # `NullEmbedder` 明确抛错而不是返回零向量 —— 先探测一次，避免跑到一半才失败
        try:
            probe = embedder.probe(requested_device=args.device)  # type: ignore[attr-defined]
            device_note = probe.summary
        except Exception:  # noqa: BLE001 - NullEmbedder 没有 probe，直接走 dry_run 分支
            device_note = "不可用"
            if not args.dry_run:
                _fail(
                    "嵌入功能不可用（未安装 onnxruntime 或模型缺失）。\n"
                    "        安装：pip install -e \".[ai-gpu]\"\n"
                    "        然后：photoos ai fetch-model\n"
                    "        可先用 --dry-run 查看「有多少张需要算」（只依赖数据库）。"
                )
                return 2

    if args.dry_run:
        summary = embed_pipeline.dry_run_summary(
            db, embedder=embedder, library_id=args.library
        )
        _print("嵌入 · 预演（未写入数据库）")
        _kv("模型", f"{summary['identity']['model']} @ {summary['identity']['model_version']}")
        _kv("预处理口径", summary["identity"]["prep_version"])
        _kv("维度 / 归一化", f"{summary['identity']['dim']} / {summary['identity']['normalization']}")
        _kv("设备", f"{summary['device']}（{device_note}）")
        _print()
        _kv("待算向量", f"{summary['pending']} 张")
        if summary["existing_versions"]:
            _print("  库里已有这些版本的向量（多版本并存是特性，不是问题）：")
            for row in summary["existing_versions"]:
                _print(
                    f"    {row['model']} @ {row['model_version']} / {row['prep_version']}"
                    f"  {row['count']} 条  （{row['last_at']}）"
                )
        _print()
        _print("  只读 `preview`（本地盘），不读原片；确认后加 --apply 执行。")
        return 0

    if not args.apply:
        _print("嵌入（默认只预演）")
        _print(f"        设备：{device_note}")
        _print()
        _print("【预演】未写入数据库。加 --apply 执行；加 --dry-run 看待办数量。")
        return 0

    def progress(done: int, total: int) -> None:
        if args.quiet:
            return
        step = max(1, total // 20)
        if done % step == 0 or done == total:
            _print(f"        进度 {done}/{total}")

    report = embed_pipeline.run_embedding(
        db,
        settings.data_root,
        embedder=embedder,
        library_id=args.library,
        limit=args.limit,
        batch_size=int(args.batch or 16),
        on_progress=progress,
    )
    for line in report.lines():
        _print("  " + line if not line.startswith("  ") else line)
    _print("        向量写在数据库里；原片与预览文件都没有被改动。")
    return 0 if report.failed == 0 else 1


# -------------------------------------------------------------------------- ai
def cmd_ai_fetch_model(args: argparse.Namespace) -> int:
    """下载并校验嵌入模型。

    两个引擎（Phase 3 起并存，见 `PHASE3_PLAN.md` 附录 C）：

    | engine | 模型 | 运行时 | 用途 |
    | --- | --- | --- | --- |
    | `clip`（默认）| CLIP ViT-B/32，335 MB ONNX | onnxruntime | 视觉相似度 → **Scene 划分** |
    | `gemma` | EmbeddingGemma 2，1.49 GB safetensors | torch | **中文文字查询** |

    默认只取 `clip` —— 免得用不上中文检索的人白下 1.49 GB。`--engine all` 两个都取。
    """
    from .library import model_store

    engine = getattr(args, "engine", "clip")
    settings = get_settings()
    root = settings.data_root
    rc = 0

    if engine in ("clip", "all"):
        _print("嵌入模型获取 · CLIP（视觉，Scene 划分用）")
        _kv("目标目录", model_store.model_dir(root))
        _kv("模型", f"{model_store.MODEL_NAME} @ {model_store.MODEL_VERSION}")
        _kv("镜像", args.source or model_store.DEFAULT_BASE_URL)
        _print()

        def report(name: str, state: str) -> None:
            if state == "start":
                _print(f"  正在下载 {name} …")
            elif state == "skip":
                _kv(f"{name}", "已就位，跳过")

        results = model_store.fetch(
            root,
            base_url=args.source or model_store.DEFAULT_BASE_URL,
            force=bool(args.force),
            on_progress=report,
        )
        for item in results:
            (_ok if item.ok else _fail)(f"{item.path.name}：{item.detail}")
        if not all(item.ok for item in results):
            _fail("部分文件未能就位，见上。")
            rc = 1
        elif engine == "clip":
            _print()
            _print("模型已就位。运行 `photoos ai doctor` 检查运行时可用的设备。")
            _print("       （未安装 onnxruntime 时嵌入功能不可用，但照片库其余功能不受影响。）")

    if engine in ("gemma", "all"):
        if engine == "all":
            _print()
        _print("嵌入模型获取 · EmbeddingGemma 2（多语言文字查询用）")
        _kv("目标目录", model_store.gemma_model_dir(root))
        _kv("模型", f"{model_store.GEMMA_MODEL_NAME} @ {model_store.GEMMA_MODEL_VERSION}")
        _kv("镜像", args.source or model_store.GEMMA_DEFAULT_BASE_URL)
        _kv("权重 sha256", model_store.GEMMA_MODEL_FILE[2][:16] + "…")
        _print()
        gres = model_store.fetch_gemma(
            root, base_url=args.source or model_store.GEMMA_DEFAULT_BASE_URL,
            force=bool(args.force),
            on_progress=lambda name, state: _print(f"  正在下载 {name} …") if state == "start" else None,
        )
        for item in gres:
            (_ok if item.ok else _fail)(f"{item.path.name}：{item.detail}")
        if not all(item.ok for item in gres):
            _fail("部分文件未能就位，见上。")
            rc = 1
        else:
            # 下载完成后再做一次**深度校验**（1.49 GB 算 sha256 约 4 秒，只在这一次做）
            deep = model_store.verify_gemma(root, deep=True)
            for item in deep:
                (_ok if item.ok else _fail)(f"{item.path.name} 深度校验：{item.detail}")
            if not all(item.ok for item in deep):
                rc = 1
            else:
                _print()
                _print("已就位。这个模型**需要 torch + sentence-transformers**（没有 ONNX 产物），")
                _print("       所以只能在装了它们的 venv 里跑：")
                _print(f"         {sys.executable} -m photoos embed --engine gemma --apply")

    if engine in ("face", "all"):
        if engine == "all":
            _print()
        _print("人脸模型获取 · YuNet（检测）+ SFace（识别）")
        _kv("目标目录", model_store.face_model_dir(root))
        _kv("来源", args.source or model_store.FACE_DEFAULT_BASE_URL)
        if getattr(args, "local_dir", None):
            _kv("离线导入自", args.local_dir)
        _print()
        fres = model_store.fetch_face(
            root, base_url=args.source or model_store.FACE_DEFAULT_BASE_URL,
            force=bool(args.force), local_dir=getattr(args, "local_dir", None),
            on_progress=lambda name, state: _print(f"  正在获取 {name} …")
            if state == "start" else None,
        )
        for item in fres:
            (_ok if item.ok else _fail)(f"{item.path.name}：{item.detail}")
        if not all(item.ok for item in fres):
            _fail("部分文件未能就位，见上。")
            rc = 1
        else:
            deep = model_store.verify_face(root, deep=True)
            for item in deep:
                (_ok if item.ok else _fail)(f"{item.path.name} 深度校验：{item.detail}")
            if not all(item.ok for item in deep):
                rc = 1
            else:
                _print()
                _print("已就位。人脸检测走 OpenCV（cv2），**不需要 torch**，")
                _print("       但产品 venv 也不装它 —— 请在命令行环境跑：")
                _print("         photoos people detect --all")

    if rc == 0:
        _print()
        _print("提示：`photoos ai doctor` 可检查运行时段；`photoos people status` 看分人进度。")
    return rc

def _text_search_hits(db, store, embedder, vec, *, wanted: int) -> tuple[list, int]:
    """一次文本检索 → **按 Shot 去重的命中列表** + 原始候选数。

    抽成独立函数是因为有两个调用方：`search`（给人看）与 `search --preset`（批量落表）。
    两处必须**同一套去重与元数据口径** —— 否则界面显示的东西与命令行不一样，
    正是本项目反复踩的"两处查询用了不同口径"（PHASE2_DELIVERY §6.10）。

    返回 `[(shot_id, rank, score, is_primary_asset, row)]`，按 rank 升序。
    """
    neighbors = store.knn(vec, wanted)
    if not neighbors:
        return [], 0
    asset_ids = [store.key_at(n.index) for n in neighbors]
    score_of = {store.key_at(n.index): n.score for n in neighbors}

    marks = ",".join("?" for _ in asset_ids)
    # 别名必须区分开：`sel` = selection，`sh` = shot（早先把两者都写成 `s`，直接报错）
    rows = db.query(
        "SELECT a.id AS asset_id, a.relative_path, a.shot_id,"
        "       m.capture_at, COALESCE(sel.status, 'UNREVIEWED') AS sel,"
        "       sh.primary_asset_id"
        "  FROM asset a"
        "  LEFT JOIN metadata m ON m.asset_id = a.id"
        "  LEFT JOIN shot sh ON sh.id = a.shot_id"
        "  LEFT JOIN selection sel ON sel.shot_id = a.shot_id"
        f" WHERE a.id IN ({marks})",
        asset_ids,
    )
    info = {int(r["asset_id"]): r for r in rows}

    picked: list[tuple] = []
    seen: set[int] = set()
    for rank, aid in enumerate(asset_ids):
        row = info.get(aid)
        if row is None:
            continue
        shot = row["shot_id"]
        if shot is None:
            continue
        shot = int(shot)
        if shot in seen:
            continue
        seen.add(shot)
        is_primary = bool(row["primary_asset_id"] == aid)
        picked.append((shot, len(picked), float(score_of[aid]), is_primary, row))
    return picked, len(asset_ids)


def _preset_concepts(args: argparse.Namespace) -> int:
    """批量预置概念：把一份列表里的每个词都算成查询并落表（界面下拉框的内容）。

    为什么需要"批量"：产品 venv 没有 torch，界面算不了文本向量，
    所以**界面能搜的词必须是预先在这里算过的**（Phase 3「方案 a」的核心取舍）。
    批量入口让"一次性预置 30 个概念"变成 10 秒钟的事，而不是 30 次命令。

    输入 JSON 形如 `[{"label": "日出", "prompt": "sunrise"}, ...]`
    （`venv/p3_concepts.json` 的 `concepts` 字段即这个形状）。
    """
    import json

    from .library.embedding_gemma import EmbeddingGemma2Embedder
    from .repos import EmbeddingRepo, SemanticRepo

    path = Path(args.preset)
    if not path.is_file():
        _fail(f"概念文件不存在：{path}")
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"概念文件解析失败：{exc}")
        return 2
    items = payload.get("concepts") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        _fail("概念文件里没有 concepts 列表")
        return 2

    min_ratio = float(getattr(args, "min_ratio", 0.0) or 0.0)
    keep = [c for c in items
            if isinstance(c, dict) and c.get("label") and c.get("prompt")
            and float(c.get("ratio", 1.0)) >= min_ratio]
    if not keep:
        _fail(f"按 --min-ratio {min_ratio} 过滤后没有概念剩下（文件里共 {len(items)} 个）")
        return 2

    settings, db = _open_db()
    try:
        embedder = EmbeddingGemma2Embedder.create(settings.data_root, device=args.device)
    except Exception as exc:  # noqa: BLE001
        _fail(f"无法创建文本编码器：{exc}")
        return 2

    store = EmbeddingRepo(db).load_store(
        model=embedder.model, model_version=embedder.model_version,
        prep_version=embedder.prep_version, dim=embedder.dim)
    if store.size == 0:
        _fail("库里还没有 EG2 向量，先跑 `embed --engine gemma --apply`")
        return 2

    from .library.vectorstore import pack_vector

    sem = SemanticRepo(db)
    _print(f"批量预置概念：{len(keep)} 个（按 ratio ≥ {min_ratio} 从 {len(items)} 个里筛出）")
    _kv("向量库", f"{store.size:,} 个")
    _print()
    _print(f"  {'标签':<10}{'prompt':<34}{'比值':>6}{'写入命中':>10}")
    total_hits = 0
    for c in keep:
        vec = embedder.encode_texts([str(c["prompt"])], kind="query")[0]
        picked, _n = _text_search_hits(db, store, embedder, vec, wanted=int(args.hits))
        hits = [(shot, rank, score, prim) for shot, rank, score, prim, _row in
                picked[: int(args.hits)]]
        written = sem.save_hits(
            query_text=str(c["label"]), prompt_text=str(c["prompt"]), text_kind="query",
            model=embedder.model, model_version=embedder.model_version, dim=embedder.dim,
            normalization=embedder.normalization, vector=pack_vector(vec), hits=hits,
            concept_ratio=float(c["ratio"]) if c.get("ratio") is not None else None,
        )
        total_hits += written
        _print(f"  {str(c['label']):<10}{str(c['prompt'])[:33]:<34}"
               f"{float(c.get('ratio', 0)):>6.3f}{written:>10}")
    _print()
    _ok(f"已预置 {len(keep)} 个查询、共 {total_hits} 条命中 —— 界面下拉框现在能看到它们。")
    _print("     界面只会显示这两张表里的内容；要加新词就再跑一次这个命令。")
    return 0


def _dispatch_search(args: argparse.Namespace) -> int:
    """`search` 的分流：给概念列表就批量预置，给了词就查一次。

    分成两个入口而不是两个子命令，是因为它们是**同一件事的两端**：
    预置出来的概念就是给界面点按钮用的，而按钮点下去看到的就是 `search` 的输出。
    """
    if getattr(args, "preset", None):
        if args.query:
            _fail("`--preset` 与查询词不能同时给（预置模式不接受单个查询）")
            return 2
        return _preset_concepts(args)
    if not args.query:
        _fail("请给出要查的词，或用 `--preset <json>` 批量预置概念。\n"
              "        例：photoos search \"日出\" --prompt sunrise --save")
        return 2
    return cmd_search(args)


def cmd_search(args: argparse.Namespace) -> int:
    """文字查照片（Phase 3）：用 EmbeddingGemma 2 的文本塔在已算好的向量里找最近的。

    ## 三条产品纪律（PRD §9.2 LOCKED + 用户明确要求）

    1. **这不是"AI 替你选"**：只把候选集从全库缩小，取舍仍然全在用户。
       输出里**没有评分、没有"最佳"**，只有**本地拍摄时刻**与相似度（后者仅用于排序）。
    2. **必须带本地拍摄时刻**（用户 2026-10-13 的要求）——
       因为实测 CLIP/EG2 都**分不清日出与日落**，而 `07:37` 和 `17:20` 一眼就能分清。
       这是把"分辨"这件事从模型转移给用户，正是 PRD 的方向。
    3. **按 Shot 去重**（实测：top-20 里只有 12 个独立 Shot，RAW+JPG 成对占名额）。

    ## 为什么必须在装了 torch 的 venv 里跑

    文本塔要 PyTorch（官方无 ONNX 产物）。**图像向量早已算好躺在库里**，
    所以查询侧只需要"一次文本编码 + 一次 numpy 点积"，**不碰预览也不碰原片**。

    ## `--save` 是「方案 a」的接口

    用户选了"界面只读已算好的结果"（因为产品 venv 没有 torch）。
    于是本命令的 `--save` 把**文本向量 + 命中快照**写进 `semantic_query` /
    `semantic_query_hit`（迁移 007），界面只读那两张表 —— **`serve` 零新依赖**。
    """
    from .library.embedding_gemma import EmbeddingGemma2Embedder
    from .repos import EmbeddingRepo, SemanticRepo

    settings, db = _open_db()
    query = args.query
    top = int(args.top)
    engine = getattr(args, "engine", "gemma")

    if engine != "gemma":
        _fail("目前只有 --engine gemma 支持文字搜索（CLIP 的中文实测失效，见 PHASE3_PLAN 附录 C.4）")
        return 2

    try:
        embedder = EmbeddingGemma2Embedder.create(settings.data_root, device=args.device)
    except Exception as exc:  # noqa: BLE001
        _fail(f"无法创建文本编码器：{exc}")
        return 2

    repo = EmbeddingRepo(db)
    identity = dict(model=embedder.model, model_version=embedder.model_version,
                    prep_version=embedder.prep_version)
    store = repo.load_store(**identity, library_id=args.library, dim=embedder.dim)
    if store.size == 0:
        _fail(
            f"库里还没有 {embedder.model} 的向量 —— 先算：\n"
            f"        {sys.executable} -m photoos embed --engine gemma --apply"
        )
        return 2

    # `--prompt` 允许"界面标签是中文、实际送进模型的是英文"（实测英文 prompt 更稳）
    prompt = getattr(args, "prompt", None) or query
    vec = embedder.encode_texts([prompt], kind="query")[0]
    # 多取一些再按 Shot 去重（RAW+JPG 成对 → 需要 2 倍以上的余量）
    picked, n_candidates = _text_search_hits(
        db, store, embedder, vec, wanted=min(store.size, max(top * 4, top, 400)))
    if not picked:
        _fail("没有找到任何结果。")
        return 1

    _print(f"文字搜索：{query!r}" + (f"  （实际 prompt: {prompt!r}）" if prompt != query else ""))
    _kv("引擎", f"{embedder.model} @ {embedder.model_version}（{store.size:,} 个向量参与检索）")
    _kv("设备", embedder.device)
    _print()
    _kv("返回", f"{len(picked)} 个独立拍摄组（已按 Shot 去重；库里共 {n_candidates} 个候选）")
    _print()
    _print("   #  拍摄时刻(本地)      精选      相似度   文件")
    for n, (_shot, _rank, score, _prim, row) in enumerate(picked[:top], 1):
        cap = str(row["capture_at"] or "（无时间）")
        cap = cap[:19].replace("T", " ")
        sel_mark = {"FINAL": "★最终", "SELECTED": "已选", "CANDIDATE": "候选",
                    "REJECTED": "淘汰"}.get(str(row["sel"]), "未审")
        path = str(row["relative_path"])
        if len(path) > 62:
            path = "…" + path[-61:]
        _print(f"  {n:>3}  {cap:<19} {sel_mark:<8} {score:>7.4f}  {path}")
    _print()
    _print("  说明：这不是「最佳照片」—— 只是把全库缩小到这几个拍摄组，取舍仍然由你决定。")
    _print("        相似度**分不清日出与日落**（实测），请用左边的拍摄时刻判断。")
    _print(f"        「{query}」在库里共命中 {n_candidates} 个候选（含同一 Shot 的 RAW/JPG）。")

    if getattr(args, "save", False):
        from .library.vectorstore import pack_vector

        hits = [(shot, rank, score, prim) for shot, rank, score, prim, _row in picked]
        written = SemanticRepo(db).save_hits(
            query_text=query, prompt_text=prompt, text_kind="query",
            model=embedder.model, model_version=embedder.model_version, dim=embedder.dim,
            normalization=embedder.normalization, vector=pack_vector(vec), hits=hits,
        )
        _print()
        _ok(f"已存进库：{written} 条命中 —— 界面刷新后即可看到这个词（不需要 torch）。")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """结构化筛选（Phase 3 · L4）：按时间/地点/场次精确取照片。

    与 `search` 的分工（**实测依据**，见 `PHASE3_PLAN.md` 附录 E）：

    | 问题 | 用哪个 | 精度 |
    | --- | --- | --- |
    | 「找出所有博物馆内容」 | `search`（语义） | 富集 10×，但**混着别的城市的博物馆** |
    | 「分出杭州 10-18 那次博物馆」 | **`find`（本命令）** | **100% 确定** |

    语义检索看不见"这是哪一趟"—— 实测 `a marble statue of a person` 的 top-100 里，
    杭州那次只占 14 张，而希腊的三个博物馆场次占了 72 张。
    而 `session`/`scene` 把每一趟记得清清楚楚（78 session / 303 scene）。
    """
    from .api.structured_find import DEFAULT_LIMIT, find_shots

    if not any([args.city, args.date_from, args.date_to, args.session, args.scene,
                args.min_shots]):
        _fail(
            "至少给一个条件（否则等于全库）：\n"
            "        --city 杭州市 | --session 2025-10-18 | --date-from 2025-10-01\n"
            "        --date-to 2025-10-31 | --scene 180 | --min-shots 20"
        )
        return 2

    settings, db = _open_db()
    rows = find_shots(
        db,
        city=args.city,
        date_from=args.date_from,
        date_to=args.date_to,
        session_like=args.session,
        scene_id=args.scene,
        min_shots=args.min_shots,
        library_id=args.library,
        limit=int(args.limit or DEFAULT_LIMIT),
    )
    _print("结构化筛选")
    cond = []
    if args.city:
        cond.append(f"城市={args.city}")
    if args.session:
        cond.append(f"场次含 {args.session!r}")
    if args.date_from or args.date_to:
        cond.append(f"日期 {args.date_from or '…'} ~ {args.date_to or '…'}")
    if args.scene:
        cond.append(f"Scene #{args.scene}")
    if args.min_shots:
        cond.append(f"至少 {args.min_shots} 个 Shot")
    _kv("条件", " · ".join(cond))
    _print()
    if not rows:
        _fail("没有匹配的 Scene。可以放宽条件，或用 `photoos stats` 看库里有什么。")
        return 1

    total_shots = sum(r["shots"] for r in rows)
    _kv("命中", f"{len(rows)} 个 Scene，共 {total_shots:,} 个 Shot"
                + ("（已截断，用 --limit 调大）" if len(rows) >= int(args.limit or DEFAULT_LIMIT) else ""))
    _print()
    _print("  Scene     时段(本地)                      Shot  已选/最终   场次")
    for r in rows:
        first = str(r["first_at"] or "")[:16].replace("T", " ")
        last = str(r["last_at"] or "")[11:16]
        _print(f"  #{r['scene_id']:<7} {first}~{last:<6} {r['shots']:>6}  "
               f"{r['reviewed']:>4}/{r['finals']:<4}   {r['city']}\\{r['period']}")
    _print()
    _print("  说明：这一层是**确定性**的（时间/目录），不依赖任何模型。")
    _print("        要在工作台里筛某个 Scene，把上面的 #编号填进 Scene 选择框即可。")
    return 0


def _face_identity() -> tuple[str, str, str, int, str]:
    """人脸特征的**写入身份**：由当前环境算出来（`cv2` 版本进 model_version）。

    集中在一处取，是因为 `detect` / `embed` 必须用**完全一致**的身份，
    否则会出现"库里有两套向量、聚类只看得见其中一半"这种查不出来的偏差。

    ⚠️ **只用于写**。读取路径必须用 `_face_read_identity(db)` ——
    现场重算依赖 `cv2` 是否存在，而产品 venv 里没有 cv2，
    于是界面会按一个错的身份去查，永远查不到（这个坑真实发生过）。
    """
    from .library import faces as faces_mod

    return (faces_mod.MODEL_NAME, faces_mod.model_version(), faces_mod.PREP_VERSION,
            faces_mod.MODEL_DIM, faces_mod.MODEL_NORMALIZATION)


def _face_read_identity(db) -> tuple[str, str, str]:
    """人脸特征的**读取身份**：一律从库里已有的行解析（没有则退回写入身份）。"""
    from .library import faces as faces_mod
    from .repos import FaceRepo

    stored = FaceRepo(db).identity()
    if stored is not None:
        return stored
    return (faces_mod.MODEL_NAME, faces_mod.model_version(), faces_mod.PREP_VERSION)


def cmd_people_detect(args: argparse.Namespace) -> int:
    """检测人脸并落 `face` 表（`library/faces.py` 的 YuNet 通路）。

    只读 `preview` 缓存，**不碰原片**。落库的是"哪个位置有一张脸"这个事实，
    **不是"这是谁"** —— 归属由用户确认（见 `people name` / 界面）。
    """
    from .library import faces as faces_mod
    from .repos import FaceRepo

    settings, db = _open_db()
    try:
        models = faces_mod.FaceModels.create(settings.data_root)
    except faces_mod.FaceNotAvailable as exc:
        _fail(str(exc))
        return 2

    repo = FaceRepo(db)
    limit = None if getattr(args, "all", False) else int(args.limit or 2000)
    targets = repo.assets_needing_detect(
        detector=models.detector_name, detector_version=models.detector_version,
        limit=limit, scene_id=getattr(args, "scene", None),
        session_like=getattr(args, "session", None), library_id=getattr(args, "library", None),
    )
    _print("人脸检测（YuNet）")
    _kv("检测器", f"{models.detector_name} @ {models.detector_version}")
    _kv("待检测", f"{len(targets):,} 张照片" + ("（已截断，加 --all 处理全部）" if limit else ""))
    if not targets:
        _ok("没有需要检测的照片（全部已检测过）。")
        return 0

    import cv2
    import time as _time

    started = _time.time()
    total_faces = 0
    frontal = 0
    failed = 0
    data_root = settings.data_root
    for index, item in enumerate(targets, 1):
        path = data_root / str(item["preview_path"])
        image = cv2.imread(str(path))
        if image is None:
            failed += 1
            continue
        detected = models.detect(image, preview_w=int(item["preview_w"]),
                                 preview_h=int(item["preview_h"]))
        repo.replace_asset_faces(
            int(item["asset_id"]), detector=models.detector_name,
            detector_version=models.detector_version, detect_max=faces_mod.DETECT_MAX,
            preview_w=int(item["preview_w"]), preview_h=int(item["preview_h"]),
            faces=[face.as_row() for face in detected],
        )
        total_faces += len(detected)
        frontal += sum(1 for face in detected if face.frontal)
        if index % 100 == 0 or index == len(targets):
            elapsed = _time.time() - started
            rate = index / elapsed if elapsed else 0
            _print(f"  {index}/{len(targets)}  人脸 {total_faces}（正面 {frontal}）"
                   f"  {rate:.1f} 张/秒  剩约 {(len(targets) - index) / rate / 60:.1f} 分"
                   if rate else f"  {index}/{len(targets)}")
    _print()
    _ok(f"检测完成：{len(targets)} 张照片 → {total_faces} 张人脸（正面清晰 {frontal}）")
    if failed:
        _warn(f"{failed} 张照片的 preview 读不出来（已跳过；原片未受影响）")
    _print("  下一步：`photoos people embed` 计算人脸特征。")
    return 0


def cmd_people_embed(args: argparse.Namespace) -> int:
    """计算人脸特征（SFace 128 维，对齐裁剪后编码）。"""
    from .library import faces as faces_mod
    from .repos import FaceRepo

    settings, db = _open_db()
    try:
        models = faces_mod.FaceModels.create(settings.data_root)
    except faces_mod.FaceNotAvailable as exc:
        _fail(str(exc))
        return 2

    repo = FaceRepo(db)
    model, version, prep, dim, normalization = _face_identity()
    pending = repo.faces_needing_embedding(
        model=model, model_version=version, prep_version=prep,
        limit=None if getattr(args, "all", False) else int(args.limit or 5000))
    _print("人脸特征（SFace）")
    _kv("模型", f"{model} @ {version}")
    _kv("预处理口径", prep)
    _kv("待编码", f"{len(pending):,} 张脸")
    if not pending:
        _ok("没有需要编码的人脸。")
        return 0

    import cv2
    import time as _time

    # 按 asset 分组：同一张照片的人脸只解一次图（检测与对齐必须用同一坐标系）
    grouped: dict[int, list[dict]] = {}
    for row in pending:
        grouped.setdefault(int(row["asset_id"]), []).append(row)

    started = _time.time()
    written = 0
    skipped = 0
    for index, (asset_id, rows) in enumerate(grouped.items(), 1):
        image = cv2.imread(str(settings.data_root / str(rows[0]["preview_path"])))
        if image is None:
            skipped += len(rows)
            continue
        matrix = models.embed_rows(image, rows)
        payload = [(int(row["id"]), matrix[i].tobytes()) for i, row in enumerate(rows)]
        written += repo.save_embeddings(payload, model=model, model_version=version,
                                       prep_version=prep, dim=dim, normalization=normalization)
        if index % 100 == 0 or index == len(grouped):
            elapsed = _time.time() - started
            _print(f"  照片 {index}/{len(grouped)}  已编码 {written} 张脸  {elapsed:.0f}s")
    _print()
    _ok(f"编码完成：{written} 张人脸（{skipped} 张因图片读不出被跳过）")
    _print("  下一步：`photoos people discover` 把脸分成候选人物。")
    return 0


def cmd_people_discover(args: argparse.Namespace) -> int:
    """把**未归类**的正面清晰脸聚成候选人物（kNN + Chinese Whispers）。

    三条实测纪律（PHASE3_PLAN 附录 F）：
      1. **禁用单链聚类**：阈值 0.363 时 686/905 张脸并成一簇（传递闭包）。
      2. **只用正面清晰脸参与聚类**：侧脸的嵌入相似来自姿态而不是身份，
         混进去会让算法"按拍摄角度分组"。
      3. 没通过正面判据的脸**不丢弃**，留作候选（用户可在界面里认领）。
    """
    from .library import faces as faces_mod
    from .repos import FaceRepo, PeopleRepo

    settings, db = _open_db()
    face_repo = FaceRepo(db)
    people_repo = PeopleRepo(db)
    model, version, prep = _face_read_identity(db)

    unassigned = people_repo.unassigned_face_ids(model=model, model_version=version,
                                                 prep_version=prep, only_frontal=True)
    if len(unassigned) < int(args.min_size):
        _ok(f"未归类的正面清晰脸只有 {len(unassigned)} 张，少于 --min-size {args.min_size}，不聚类。")
        return 0

    rows = face_repo.embeddings(model=model, model_version=version, prep_version=prep,
                                face_ids=unassigned, only_frontal=True)
    matrix = _face_matrix(rows)
    _print("分人 · 聚类（kNN + Chinese Whispers）")
    _kv("参与聚类的脸", f"{len(rows):,} 张（正面清晰）")
    _kv("参数", f"k={args.k} · 最小簇 {args.min_size} · 迭代 {args.iterations}")

    clusters = faces_mod.discover_clusters(matrix, k=int(args.k), min_size=int(args.min_size),
                                           iterations=int(args.iterations))
    if not clusters:
        _warn("没有形成任何簇。可以是人脸太少，或 k/最小簇设置过大。")
        return 1

    _print()
    _print("   #  规模   同簇/全局余弦比   代表脸（asset）")
    created = 0
    for rank, cluster in enumerate(clusters[: int(args.top)], 1):
        members = [int(rows[i]["face_id"]) for i in cluster.members]
        person_id = people_repo.create_person()
        people_repo.add_faces(person_id, members, source="auto")
        block = matrix[cluster.members]
        try:
            profile = faces_mod.fit_profile(block, positive=[],
                                            auto=list(range(len(cluster.members))), negative=[])
            people_repo.save_profile(
                person_id, model=model, model_version=version, prep_version=prep,
                dim=int(rows[0]["dim"]),
                vector=profile.vector.tobytes(), negative_vector=None,
                pos_count=0, neg_count=0, threshold=profile.threshold, margin=profile.margin)
        except Exception as exc:  # noqa: BLE001
            _warn(f"簇 #{rank} 的原型估计失败（成员仍已保留）：{exc}")
        people_repo.log_event("discover", person_id=person_id,
                              detail={"size": cluster.size, "ratio": cluster.ratio,
                                      "k": int(args.k)})
        created += 1
        _print(f"  {rank:>3}  {cluster.size:>4}   {cluster.ratio:>8.2f}        "
               f"asset {int(rows[cluster.members[0]]['asset_id'])}")
    _print()
    _ok(f"创建了 {created} 个**未命名**的候选人物（共 {len(clusters)} 簇）")
    _print("  它们**没有名字** —— 模型不知道那是谁，请你来命名：")
    _print("    photoos people name <id> \"妈妈\"")
    _print("  或在界面「人物」页直接确认。")
    return 0


def _face_matrix(rows: Sequence[Any]) -> Any:
    """把 `face_embedding.vector` 解成 (N, dim) float32 并 L2 归一化。

    实现只有一份（`library/faces.matrix_from_rows`）—— 命令行与服务端必须
    用同一个解码口径，否则"命令行认得出、界面认不出"这类偏差会查不出来。
    """
    from .library import faces as faces_mod

    return faces_mod.matrix_from_rows(rows)


def _profiles_for_people(people_repo, *, model: str, version: str, prep: str,
                         named_only: bool) -> dict[int, Any]:
    """取每个人的原型向量（缺失的人**明确列出来**，不静默跳过）。"""
    import numpy as np

    persons = {int(p["id"]): p for p in people_repo.list_persons()}
    out: dict[int, Any] = {}
    for row in people_repo.profiles(model=model, model_version=version, prep_version=prep):
        person_id = int(row["person_id"])
        person = persons.get(person_id)
        if person is None:
            continue
        if named_only and not person.get("name"):
            continue
        out[person_id] = np.frombuffer(bytes(row["vector"]), dtype="<f4").astype("f4")
    return out


def cmd_people_assign(args: argparse.Namespace) -> int:
    """**增量**：用当前原型给未归类的脸找主人（模型的判断，等用户确认）。

    这是"持续学习"兑现的地方：用户每确认/否决一次，`people learn` 会更新原型，
    下一次本命令的判定就跟着变。所以标准工作流是：

        people learn  →  people assign  →  （用户确认/否决）  →  people learn  → …

    `--named-only`（默认）只让**有名字的人**参与增量：名字意味着用户已经表过态，
    而一个还没命名的模型提议没有用户信号，拿它去扩大归属只会放大模型自己的错误。
    """
    from .library import faces as faces_mod
    from .repos import FaceRepo, PeopleRepo

    settings, db = _open_db()
    face_repo = FaceRepo(db)
    people_repo = PeopleRepo(db)
    model, version, prep = _face_read_identity(db)

    profiles = _profiles_for_people(people_repo, model=model, version=version, prep=prep,
                                    named_only=not bool(args.all_persons))
    if not profiles:
        _fail("没有任何人的原型。先命名几个人（`photoos people name`），"
              "或加 `--all-persons` 让未命名的候选也参与。")
        return 1

    profiles_rows = {pid: row for pid, row in
                     ((int(r["person_id"]), r) for r in
                      people_repo.profiles(model=model, model_version=version, prep_version=prep))}
    thresholds = {pid: float(profiles_rows[pid]["threshold"] or faces_mod.SFACE_DEFAULT_THRESHOLD)
                  for pid in profiles}

    unassigned = people_repo.unclaimed_face_ids(model=model, model_version=version,
                                                prep_version=prep)
    if not unassigned:
        _ok("没有未归类的人脸。")
        return 0
    rows = face_repo.embeddings(model=model, model_version=version, prep_version=prep,
                                face_ids=unassigned)
    matrix = _face_matrix(rows)

    blocked = {pid: set() for pid in profiles}
    for pid in list(profiles):
        for row in people_repo.rejected_faces(pid, limit=100000):
            face_id = int(row["face_id"])
            if face_id in {int(r["face_id"]) for r in rows}:
                blocked[pid].add(face_id)

    # 否决集合按 face_id（不是行号）给到 assign_faces，所以这里做一次映射
    index_of_face = {int(r["face_id"]): i for i, r in enumerate(rows)}
    blocked_idx = {pid: {index_of_face[f] for f in faces if f in index_of_face}
                   for pid, faces in blocked.items()}

    result = faces_mod.assign_faces(profiles, thresholds=thresholds, matrix=matrix,
                                    candidate_indices=range(len(rows)), blocked=blocked_idx)

    _print("分人 · 增量归类（模型的判断，等你确认）")
    _kv("参与的人", f"{len(profiles)} 个（{'仅有名字的' if not args.all_persons else '全部有原型的'}）")
    _kv("未归类的脸", f"{len(rows):,} 张")
    _print()
    _print("   person  名字           阈值   接受   候选(低于阈值)  新归入")
    total_accepted = 0
    total_written = 0
    for pid in sorted(result):
        person = people_repo.person(pid) or {}
        accepted = [a for a in result[pid] if a.accepted]
        borderline = [a for a in result[pid] if not a.accepted]
        if not accepted:
            _print(f"   {pid:>6}  {str(person.get('name') or '(未命名)'):<14}"
                   f"{thresholds[pid]:>6.3f}{0:>7}{len(borderline):>14}{0:>8}")
            continue
        total_accepted += len(accepted)
        if not args.dry_run:
            face_ids = [int(rows[a.face_index]["face_id"]) for a in accepted]
            similarities = {int(rows[a.face_index]["face_id"]): a.similarity for a in accepted}
            total_written += people_repo.add_faces(pid, face_ids, source="auto",
                                                   similarities=similarities)
            people_repo.log_event("assign", person_id=pid,
                                  detail={"accepted": len(accepted),
                                          "threshold": thresholds[pid]})
        _print(f"   {pid:>6}  {str(person.get('name') or '(未命名)'):<14}"
               f"{thresholds[pid]:>6.3f}{len(accepted):>7}{len(borderline):>14}"
               f"{len(accepted) if not args.dry_run else 0:>8}")
    _print()
    if args.dry_run:
        _print(f"  （--dry-run：不会写入。若执行会新归入 {total_accepted} 张脸。）")
    else:
        _ok(f"新归入 {total_written} 张脸（source=auto，界面显示为「模型的判断」）")
    _print("  界面上逐张确认/否决之后，跑 `photoos people learn` 让原型跟着你的判断走。")
    return 0


def cmd_people_learn(args: argparse.Namespace) -> int:
    """从用户的确认/否决里**重新估计原型与阈值**（"持续学习"的落点）。

    ## 必须对用户说清楚：这不是训练

    128 维 SFace 的权重一个字节都没变。变的是 `person_profile` 里的
    **原型均值向量 + 判定阈值**，它们完全由库里的向量算出来：

        原型 = 用户确认的 × 2 + 模型提议的 × 1 的加权均值（L2 归一化）
        阈值 = 从"确认 vs 否决"两个相似度分布里学出来的切点

    之所以不做微调：千级样本训不动、微调会破坏可复算性（同一个向量在不同时刻
    得到不同分数，"当初为什么判成他"就再也算不出来），而原型学习每一步都能重算。
    """
    from .library import faces as faces_mod
    from .repos import FaceRepo, PeopleRepo

    settings, db = _open_db()
    face_repo = FaceRepo(db)
    people_repo = PeopleRepo(db)
    model, version, prep = _face_read_identity(db)

    persons = people_repo.list_persons()
    if not persons:
        _fail("还没有任何人物。先跑 `photoos people discover`，再命名。")
        return 1

    _print("分人 · 学习（重新估计原型与阈值）")
    _kv("身份", f"{model} @ {version} / {prep}")
    _print()
    _print("   person  名字           标注(正/负)  阈值    间隔     正例相似度(最低/均值)  "
           "负例相似度(最高/均值)")
    updated = 0
    for person in persons:
        person_id = int(person["id"])
        labels = people_repo.labels(person_id)
        if not labels["positive"] and not labels["auto"]:
            continue
        wanted = sorted(set(labels["positive"]) | set(labels["auto"]) | set(labels["negative"]))
        rows = face_repo.embeddings(model=model, model_version=version, prep_version=prep,
                                    face_ids=wanted)
        if not rows:
            continue
        matrix = _face_matrix(rows)
        local = {int(r["face_id"]): i for i, r in enumerate(rows)}
        try:
            profile = faces_mod.fit_profile(
                matrix,
                positive=[local[f] for f in labels["positive"] if f in local],
                auto=[local[f] for f in labels["auto"] if f in local],
                negative=[local[f] for f in labels["negative"] if f in local],
            )
        except Exception as exc:  # noqa: BLE001
            _warn(f"person {person_id} 的原型估计失败：{exc}")
            continue
        people_repo.save_profile(
            person_id, model=model, model_version=version, prep_version=prep,
            dim=int(rows[0]["dim"]),
            vector=profile.vector.tobytes(),
            negative_vector=None if profile.negative_vector is None
            else profile.negative_vector.tobytes(),
            pos_count=profile.pos_count, neg_count=profile.neg_count,
            threshold=profile.threshold, margin=profile.margin)
        people_repo.log_event("profile", person_id=person_id,
                              detail={"pos": profile.pos_count, "neg": profile.neg_count,
                                      "threshold": round(profile.threshold, 4),
                                      "notes": profile.notes})
        updated += 1
        pos = profile.pos_sims
        neg = profile.neg_sims
        pos_text = f"{min(pos):.3f}/{sum(pos) / len(pos):.3f}" if pos else "—"
        neg_text = f"{max(neg):.3f}/{sum(neg) / len(neg):.3f}" if neg else "—"
        _print(f"   {person_id:>6}  {str(person.get('name') or '(未命名)'):<14}"
               f"{profile.pos_count:>5}/{profile.neg_count:<5}"
               f"{profile.threshold:>7.3f}  "
               f"{('—' if profile.margin is None else f'{profile.margin:+.3f}'):>7}   "
               f"{pos_text:<22}{neg_text}")
        if args.verbose:
            for note in profile.notes:
                _print(f"           · {note}")
    _print()
    _ok(f"已更新 {updated} 个人的原型")
    if updated:
        _print("  下一步：`photoos people assign` 用新原型做增量归类。")
    return 0


def cmd_people_name(args: argparse.Namespace) -> int:
    """给一个候选人物起名（**名字只能来自用户**）。"""
    from .repos import PeopleRepo

    settings, db = _open_db()
    repo = PeopleRepo(db)
    person = repo.person(int(args.person_id))
    if person is None:
        _fail(f"没有 id={args.person_id} 的人物")
        return 1
    repo.rename(int(args.person_id), args.name.strip() or None)
    repo.log_event("rename", person_id=int(args.person_id),
                   detail={"from": person["name"], "to": args.name})
    _ok(f"person {args.person_id} 已命名为「{args.name}」")
    return 0


def cmd_people_merge(args: argparse.Namespace) -> int:
    """把两个候选合并成一个人（模型把同一个人拆成两簇时用）。

    `--into` 是保留的那个。归属、否决、事件全部搬过去，然后删掉源人物 ——
    **删的只是"人"这个标签，一张脸、一张照片都不会动**。
    """
    from .repos import PeopleRepo

    settings, db = _open_db()
    repo = PeopleRepo(db)
    source = int(args.person_id)
    target = int(args.into)
    if source == target:
        _fail("源与目标不能是同一个人")
        return 2
    if repo.person(source) is None or repo.person(target) is None:
        _fail("源或目标人物不存在")
        return 1

    face_ids = [int(r["face_id"]) for r in repo.faces(source, limit=100000)]
    if face_ids:
        repo.add_faces(target, face_ids, source="auto")
    repo.log_event("merge", person_id=target, detail={"merged_from": source,
                                                      "faces": len(face_ids)})
    repo.delete_person(source)
    _ok(f"已把 person {source} 的 {len(face_ids)} 张脸并入 person {target}，"
        "并删掉源人物标签（照片与脸都没有被删）")
    return 0


def cmd_people_status(args: argparse.Namespace) -> int:
    """报告分人流水线到哪一步了、以及模型的判断有多可信。

    这个命令存在的理由是**诚实**：让用户一眼看出"库里到底有没有数据"，
    而不是在界面上看到一个空页面猜是坏了还是没跑。
    """
    from .library import faces as faces_mod
    from .repos import FaceRepo, PeopleRepo

    settings, db = _open_db()
    face_repo = FaceRepo(db)
    people_repo = PeopleRepo(db)
    model, version, prep = _face_read_identity(db)

    stats = face_repo.stats(model=model, model_version=version, prep_version=prep)
    pstats = people_repo.stats()
    _print("分人流水线状态")
    _kv("检测", f"{stats['faces']:,} 张脸 / {stats['assets_with_faces']:,} 张照片"
                f"（正面清晰 {stats['frontal']:,}）")
    _kv("特征", f"{stats.get('embedded', 0):,} 张脸有 {model} 特征")
    _kv("候选人", f"{pstats['persons']} 个（已命名 {pstats['named']}）")
    _kv("归属", f"用户确认 {pstats['confirmed_faces']} · 模型提议 {pstats['auto_faces']}"
                f" · 已否决 {pstats['rejected_faces']}")

    persons = people_repo.list_persons()
    if persons:
        _print()
        _print("   person  名字             脸(确认/提议)  否决   覆盖照片  阈值    间隔")
        for person in persons[: int(args.top)]:
            margin = person.get("margin")
            threshold = person.get("threshold")
            margin_text = "—" if margin is None else f"{float(margin):+.3f}"
            threshold_text = "—" if threshold is None else f"{float(threshold):.3f}"
            _print(f"   {person['id']:>6}  {str(person.get('name') or '(未命名)'):<16}"
                   f"{person['user_faces']:>5}/{person['auto_faces']:<6}"
                   f"{person['rejects']:>6}{person['shots']:>10}"
                   f"{threshold_text:>8}{margin_text:>8}")
    _print()
    if not stats["faces"]:
        _print("  下一步：photoos people detect --all")
    elif not stats.get("embedded"):
        _print("  下一步：photoos people embed --all")
    elif not pstats["persons"]:
        _print("  下一步：photoos people discover")
    elif not pstats["named"]:
        _print("  下一步：photoos people name <id> \"名字\"（名字只能由你给）")
    else:
        _print("  维护循环：people learn → people assign → 界面确认 → people learn …")
    return 0


def cmd_ai_doctor(args: argparse.Namespace) -> int:
    """报告嵌入运行时的**真实**状态（Phase 2 · M3）。

    这个命令的核心价值在于：**CUDA 会静默降级到 CPU**。
    实测三种配置下 `get_available_providers()` 输出**完全相同**，
    `session.get_providers()` 在其中一种下**也骗人**（provider 建得起来，
    直到第一次 Conv 才抛错并自己降级）。因此这里**跑一次真推理并计时**。
    """
    from .library import embedding as embedding_mod
    from .library import model_store

    settings, _db = get_settings(), None
    root = settings.data_root

    _print("嵌入运行时自检")
    files = model_store.verify(root, deep=True)
    for item in files:
        (_ok if item.ok else _fail)(f"{item.path.name}：{item.detail}")

    try:
        import onnxruntime as ort  # noqa: F401

        _ok(f"onnxruntime 已安装（{ort.__version__}）")
    except ImportError:
        _fail("onnxruntime 未安装 —— 嵌入功能不可用（可选依赖，不影响其他功能）")
        _print()
        _print("  安装：pip install -e \".[ai-gpu]\"")
        _print("  然后：photoos ai fetch-model")
        _print()
        _print("  注：不装 GPU 版也能用 —— onnxruntime-gpu 自带 CPUExecutionProvider，")
        _print("      检测不到 CUDA 时会自动跑 CPU（全库约慢 3 倍）。")
        return 1

    if not all(item.ok for item in files):
        _fail("模型文件未就位：先运行 `photoos ai fetch-model`")
        return 1

    try:
        embedder = embedding_mod.ClipOnnxEmbedder.create(root, device=args.device)
    except embedding_mod.EmbeddingNotAvailable as exc:
        _fail(f"无法创建编码器：{exc}")
        return 1

    probe = embedder.probe(requested_device=args.device)
    _print()
    _kv("onnxruntime 版本", probe.ort_version or "（未知）")
    _kv("可用 provider", "、".join(probe.providers_available))
    _kv("会话 provider", "、".join(probe.providers_session))
    _kv("cuDNN 包版本", probe.cudnn_version or "（未安装 nvidia-cudnn-cu12）")
    _kv("实际设备", probe.device)
    _kv("实测单张耗时", f"{probe.ms_per_image:.2f} ms")
    _print()
    _kv("结论", probe.summary)
    for note in probe.notes:
        _warn(note)
    if probe.degraded:
        _print()
        _print("  排查顺序（实测唯一可用组合）：")
        _print("    1) onnxruntime-gpu 必须是 1.25.1（CUDA 12 构建）")
        _print("       1.27+ 要 CUDA 13，而 PyPI 上 CUDA 13 的 Windows 轮子只有 cuDNN 有")
        _print("    2) nvidia-cudnn-cu12 必须是 9.10.2.21（9.27.0.42 实测在第一次 Conv 就失败）")
        _print("    3) 必须显式调用 ort.preload_dlls()（本代码已做）")
        return 2
    return 0


# -------------------------------------------------------------------------- sessions
def _load_asset_times(db: Database, library_ids: Sequence[int] | None = None) -> list[Any]:
    """读出派生 Session 所需的资产时间信息（**纯数据库读取，不打开任何照片文件**）。

    只取四列：`relative_path`（提供城市/时段身份）、`file_type`、以及 metadata 的
    三个时间字段。刻意不取 size/hash 等无关列 —— 让"这个命令不读盘"在 SQL 层面可见。
    """
    from .library import session_derive as derive_mod

    sql = (
        "SELECT a.library_id, a.relative_path, a.file_type,"
        "       m.capture_at, m.capture_at_utc, m.tz_offset"
        "  FROM asset a LEFT JOIN metadata m ON m.asset_id = a.id"
        " WHERE a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
    )
    params: list[Any] = []
    if library_ids:
        sql += f" AND a.library_id IN ({', '.join('?' for _ in library_ids)})"
        params.extend(int(i) for i in library_ids)
    return [
        derive_mod.AssetTime(
            library_id=int(row["library_id"]),
            relative_path=str(row["relative_path"]),
            file_type=str(row["file_type"]),
            capture_at=row["capture_at"],
            capture_at_utc=row["capture_at_utc"],
            tz_offset=row["tz_offset"],
        )
        for row in db.query(sql, params)
    ]


def cmd_sessions_backfill(args: argparse.Namespace) -> int:
    """把库内既有的 `<城市>\\<时段>` 目录补成 `session` 行（Phase 2 · M1）。

    默认**只预演**（`--dry-run` 语义），`--apply` 才写库。`--undo` 只删由本命令
    写入的行（`source='auto'` 且 `city_hint` 非空），**不涉及任何文件**。
    """
    from .library import session_derive as derive_mod
    from .repos import SessionRepo

    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    libraries = store.list_all()
    if not libraries:
        _print("尚未登记任何 Library。")
        return 0

    if args.undo:
        repo = SessionRepo(db)
        with db.write() as conn:
            before = int(conn.execute(
                "SELECT COUNT(*) FROM session WHERE source = 'auto' AND city_hint IS NOT NULL"
            ).fetchone()[0])
            conn.execute(
                "DELETE FROM session WHERE source = 'auto' AND city_hint IS NOT NULL"
            )
        _print("Sessions 回填 · 撤销")
        _kv("删除的 session 行", before)
        _print("        （只删数据库行；没有触碰任何照片文件）")
        return 0

    library_ids = None
    if args.library:
        library_ids = [_pick_library(store, args.library).id]

    assets = _load_asset_times(db, library_ids)
    sessions = derive_mod.derive_sessions(assets)

    repo = SessionRepo(db)
    existing = {
        (str(row["city_hint"]), str(row["period_hint"]))
        for row in db.query(
            "SELECT city_hint, period_hint FROM session"
            " WHERE city_hint IS NOT NULL AND period_hint IS NOT NULL"
        )
    }
    new_rows = [s for s in sessions if (s.city, s.period) not in existing]
    upd_rows = [s for s in sessions if (s.city, s.period) in existing]

    comparable, matched, mismatches, skipped = derive_mod.verify_against_folder_names(sessions)

    _print("Sessions 回填（从既有目录派生，不读任何照片文件）")
    _kv("参与派生的资产", len(assets))
    _kv("派生出的时段", len(sessions))
    _kv("其中已存在（将按时间范围更新）", len(upd_rows))
    _kv("其中需新建", len(new_rows))
    _kv("含视频的时段", sum(1 for s in sessions if s.video_count))
    mixed = [s for s in sessions if s.mixed_offsets]
    _kv("混用时区偏移的时段", f"{len(mixed)}" + ("（已按决策 #11 降置信度并原样保留）" if mixed else ""))
    unknown = [s for s in sessions if s.videos_without_offset]
    _kv("有视频借不到偏移的时段", len(unknown))
    _print()
    _kv("目录名校验（ground truth）", f"可比 {comparable} 个，逐字相符 {matched}，不符 {len(mismatches)}，跨天跳过 {skipped}")
    for name, expected, got in mismatches[:10]:
        _print(f"        [不符] {name}：目录名 {expected}，派生 {got}")
    if len(mismatches) > 10:
        _print(f"        ……另有 {len(mismatches) - 10} 个，见 --json")
    _print()

    if args.json:
        _print(json.dumps(
            {
                "derived": len(sessions),
                "new": len(new_rows),
                "updated": len(upd_rows),
                "comparable": comparable,
                "matched": matched,
                "mismatches": [
                    {"name": n, "folder": e, "derived": g} for n, e, g in mismatches
                ],
                "sessions": [
                    {
                        **s.to_row(),
                        "photo_count": s.photo_count,
                        "video_count": s.video_count,
                        "videos_without_offset": s.videos_without_offset,
                        "distinct_offsets": s.distinct_offsets,
                    }
                    for s in sessions
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if not args.apply:
        _print("【预演】未写入数据库。确认无误后加 --apply 执行：")
        _print("        photoos sessions backfill --apply")
        if new_rows:
            _print()
            _print("  将要新建的时段（前 20 个）：")
            for s in new_rows[:20]:
                flag = ""
                if s.mixed_offsets:
                    flag = "  [混用时区]"
                elif s.videos_without_offset:
                    flag = "  [含时区未知视频]"
                _print(
                    f"    {s.name:<46s} 照片 {s.photo_count:5d} 视频 {s.video_count:3d}"
                    f"  {s.captured_from:%Y-%m-%d %H:%M} ~ {s.captured_to:%H:%M}"
                    f"  置信度 {s.confidence:.2f}{flag}"
                )
            if len(new_rows) > 20:
                _print(f"    ……另有 {len(new_rows) - 20} 个")
        return 0

    created, updated = repo.upsert_many([s.to_row() for s in sessions])
    from .db import utc_now as _utc_now

    with db.write() as conn:
        conn.execute(
            "INSERT INTO app_event (at, kind, library_id, payload_json)"
            " VALUES (?, 'sessions_backfill', NULL, ?)",
            (
                _utc_now(),
                json.dumps(
                    {
                        "derived": len(sessions),
                        "created": created,
                        "updated": updated,
                        "folder_name_matched": matched,
                        "folder_name_comparable": comparable,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    _print("Sessions 回填 · 已执行")
    _ok(f"新建 {created} 行，更新 {updated} 行")
    _kv("session 表总行数", repo.count())
    _print("        原始照片文件全程只被读取，未发生任何移动/改名/覆盖/删除。")
    return 0


def _load_shot_assets(db: Database, library_ids: Sequence[int] | None = None) -> list[Any]:
    """读出 Shot 分组所需的资产线索（**纯数据库读取，不打开任何照片文件**）。"""
    from .library import shot_grouping as grouping

    sql = (
        "SELECT a.id, a.relative_path, a.file_type,"
        "       m.capture_at, m.capture_at_utc, m.subsec, m.lens_model,"
        "       m.focal_length, m.aperture, m.shutter_speed, m.iso"
        "  FROM asset a LEFT JOIN metadata m ON m.asset_id = a.id"
        " WHERE a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
    )
    params: list[Any] = []
    if library_ids:
        sql += f" AND a.library_id IN ({', '.join('?' for _ in library_ids)})"
        params.extend(int(i) for i in library_ids)
    return [
        grouping.ShotAsset(
            asset_id=int(row["id"]),
            file_type=str(row["file_type"]),
            relative_path=str(row["relative_path"]),
            capture_at=row["capture_at"],
            subsec=row["subsec"],
            lens_model=row["lens_model"],
            focal_length=row["focal_length"],
            aperture=row["aperture"],
            shutter_speed=row["shutter_speed"],
            iso=row["iso"],
            capture_at_utc=row["capture_at_utc"],
        )
        for row in db.query(sql, params)
    ]


def _resolve_gap(settings: Any, db: Database, session_selector: str | None, override: float | None) -> tuple[float, str]:
    """确定 `shot_gap_seconds`：CLI > 单 Session 覆盖 > config。

    决策 #5：这个值**不做写死的默认**，三层可调。返回 `(值, 来源说明)`，来源要打印出来 ——
    否则用户改了配置却看到没生效时无从判断是哪一层赢了。
    """
    if override is not None:
        return float(override), "--gap-seconds（命令行）"
    if session_selector:
        selector = str(session_selector)
        with db.read() as conn:
            row = conn.execute(
                "SELECT review_config_json FROM session WHERE id = ? OR name = ?",
                (int(selector) if selector.isdigit() else -1, selector),
            ).fetchone()
        if row and row["review_config_json"]:
            try:
                payload = json.loads(row["review_config_json"])
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict) and payload.get("shot_gap_seconds") is not None:
                return float(payload["shot_gap_seconds"]), "本时段已设置（单时段覆盖）"
    # 与 API 侧同一套措辞：面向用户的中文里不出现配置文件的名字
    return float(settings.shot_gap_seconds), "跟随全局默认"


def _build_scene_similarity(db: Database, library_ids: Sequence[int] | None):
    """给 Scene 划分用的相似度信号：`(工厂函数 | None, 说明文字)`。

    **为什么必须有这个函数**：`scene_grouping` 的第 ② 步（相似度合并/细分）
    早就实现并单测通过，但 `scenes rebuild` 一直传 `similarity=None` ——
    于是"相似度切片"在真实流水线里**从来没跑过**，界面上只会显示
    「未启用（退化为纯时间规则）」。这里把它接上，让 M3 的嵌入真正参与划分。

    返回的是**工厂**而不是回调：回调需要"按时段分好的 Shot 列表"才能建立索引，
    而那个东西要等 `group_by_directory()` 跑完才有 —— 顺序约束写进签名，
    避免调用方猜（这个顺序正是当初漏接的原因）。

    契约：回调返回两个 Shot 之间**所有资产两两余弦的最大值**；任一侧没有嵌入
    就返回 None，`scene_grouping` 会据此跳过该次比较（不猜、不默认 1.0）。
    """
    from .library import model_store
    from .repos import EmbeddingRepo

    repo = EmbeddingRepo(db)
    if not repo.count():
        return None, "未启用（embedding 表为空 —— 先跑 `photoos embed --apply`，退化为纯时间规则）"

    store = repo.load_store(
        model=model_store.MODEL_NAME,
        model_version=model_store.MODEL_VERSION,
        prep_version=model_store.PREP_VERSION,
        library_id=library_ids[0] if library_ids and len(library_ids) == 1 else None,
        dim=model_store.MODEL_DIM,
    )
    if store.size == 0:
        return None, "未启用（没有与当前模型版本匹配的嵌入，退化为纯时间规则）"

    index_of = {int(key): position for position, key in enumerate(store.keys)}
    # `store.matrix()` 会**复制**整个矩阵（43.7 MB）。回调每次比较都会用到它，
    # 所以在这里取一次副本然后传引用 —— 绝不能在回调里反复调 `matrix()`。
    matrix = store.matrix()
    note = (
        f"启用中（{store.size:,} 个向量，模型 {model_store.MODEL_NAME}/"
        f"{model_store.MODEL_VERSION}/{model_store.PREP_VERSION}）"
    )

    def factory(shots_by_period):
        """`{period: [ShotGroup]}` → `(period, i, j) -> float | None`。

        `split_all` 的契约是 `(period, i, j)`：相似度**只在时段内有意义**，
        不同时段的照片不该被拿来比较。所以每个时段各建一个回调。
        """
        from .library import scene_grouping as scene_mod

        per_period = {
            key: scene_mod.build_similarity_callback(
                shots, matrix=matrix, index_of=index_of
            )
            for key, shots in shots_by_period.items()
        }

        def similarity(period, left, right):
            callback = per_period.get(period)
            if callback is None:
                return None
            return callback(left, right)

        return similarity

    return factory, note


def _persist_shots(
    db: Database, groups: list[Any], *, library_ids: Sequence[int] | None
) -> tuple[dict[str, int], dict[int, int]]:
    """把分组结果落库（只增不删）。返回 `(统计, {位置索引: shot_id})`。

    返回位置→id 的映射是为了让 `scenes rebuild --apply` 能把 Scene 挂到刚写入的 Shot 上 ——
    新建的 Shot 的 id 由数据库分配，只有落库之后才知道。
    """
    from .repos import ShotRepo

    repo = ShotRepo(db)
    existing = repo.active_assets()
    frozen = repo.protected_ids()

    # 用资产集合匹配"可复用的旧 Shot"（与 `plan_regroup` 同一策略）
    remaining: dict[frozenset[int], list[int]] = {}
    for shot_id, assets in existing.items():
        if shot_id in frozen:
            continue
        remaining.setdefault(assets, []).append(shot_id)

    rows: list[dict] = []
    for group in groups:
        key = frozenset(group.asset_ids)
        candidates = remaining.get(key)
        shot_id = candidates.pop() if candidates else None
        rows.append(
            {
                "shot_id": shot_id,
                "assets": group.asset_ids,
                "captured_at": group.captured_at,
                "primary_asset_id": group.primary_asset_id,
                "group_confidence": group.confidence,
            }
        )
    retired = sorted(sid for ids in remaining.values() for sid in ids)
    stats = repo.apply_regroup(shot_rows=rows, retired=retired, frozen=frozen)

    # 回读映射：按资产集合把位置索引对上刚写入/更新的 Shot
    active = repo.active_assets()
    by_key: dict[frozenset[int], int] = {assets: sid for sid, assets in active.items()}
    mapping: dict[int, int] = {}
    for index, group in enumerate(groups):
        found = by_key.get(frozenset(group.asset_ids))
        if found is not None:
            mapping[index] = found
    _ = library_ids
    return stats, mapping


def cmd_shots_regroup(args: argparse.Namespace) -> int:
    """按时间 + subsec 重算 Shot 分组（Phase 2 · M2）。

    默认**只预演**：打印分组统计与"建议人工确认"的组，不写数据库。
    落库（`--apply`）需要 `session` 行已回填，因此会先检查并给出可操作的提示。
    """
    from .library import shot_grouping as grouping

    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    if not store.list_all():
        _print("尚未登记任何 Library。")
        return 0

    library_ids = None
    if args.library:
        library_ids = [_pick_library(store, args.library).id]

    gap, source = _resolve_gap(settings, db, args.session, args.gap_seconds)
    assets = _load_shot_assets(db, library_ids)

    # ★ 决策 #10：用户手工调整过的 Shot（`group_source='user'`）**永不被重算覆盖**。
    #
    # 只把它们从"可复用集合"里排除是**不够的** —— 那只是不去复用它们的 shot 行，
    # 它们名下的**资产仍会被重新分组**，于是 `UPDATE asset SET shot_id` 会把资产搬走，
    # 而 Shot 行还在、`group_source='user'` 还在，看起来"没被动过"。
    #
    # 实测（真实库快照）：把一个 4 张资产的 Shot 手工拆分（因此两边都冻结），
    # 再改阈值重算 —— 两个冻结 Shot 的资产**全被搬到别处**，它们变成空壳：
    #   Shot 4 资产：期望 [5, 6, 7] 实际 []（精选行还挂着 → 用户点进去是空的）
    #
    # 因此正确做法是：**在分组之前就把这些资产摘出去**，让它们连"参与分组"都不参与。
    from .repos import ShotRepo as _ShotRepo

    locked_asset_ids = _ShotRepo(db).protected_asset_ids()
    free_assets = [a for a in assets if int(a.asset_id) not in locked_asset_ids]
    locked_count = len(assets) - len(free_assets)

    buckets = grouping.group_by_directory(free_assets, gap_seconds=gap)

    all_groups = [g for gs in buckets.values() for g in gs]
    multi = [g for g in all_groups if g.size > 1]
    review = [g for g in all_groups if g.needs_review]
    photos = [a for a in free_assets if not a.is_video]
    videos = [a for a in free_assets if a.is_video]

    _print("Shot 分组（按时间 + subsec；不读取任何照片文件）")
    _kv("参与分组的资产", f"{len(free_assets)}（照片 "
        f"{len([a for a in free_assets if not a.is_video])} + 视频 "
        f"{len([a for a in free_assets if a.is_video])}）")
    if locked_count:
        _kv("因人工锁定而跳过", f"{locked_count} 个资产"
            f"（属于 {len(_ShotRepo(db).protected_ids())} 个手工调整过的 Shot，决策 #10）")
    _kv("时段数", len(buckets))
    _kv("阈值 shot_gap_seconds", f"{gap:g} s   ← 来自 {source}")
    _print()
    _kv("派生出的 Shot 总数", len(all_groups))
    _kv("单张 Shot", len(all_groups) - len(multi))
    _kv("多张 Shot", len(multi))
    _kv("最大 Shot 资产数", max((g.size for g in all_groups), default=0))
    _kv("平均资产/Shot", f"{len(assets) / len(all_groups):.2f}" if all_groups else "—")
    _print()
    _kv("建议人工确认（置信度 < 0.5）", f"{len(review)} 个 Shot")

    if review:
        _print("        这些是「时间紧邻但镜头或焦段变过」的组 —— 很可能不是连拍：")
        for g in review[:10]:
            names = "、".join(a.name for a in g.assets[:3])
            folder = g.assets[0].relative_path.replace("/", "\\").rsplit("\\", 1)[0]
            _print(f"          {folder}\\{names}  （{g.size} 张，置信度 {g.confidence:.2f}）")
        if len(review) > 10:
            _print(f"          ……另有 {len(review) - 10} 个（见 --json）")

    dist = {}
    for g in all_groups:
        dist[g.confidence] = dist.get(g.confidence, 0) + 1
    _print()
    _kv("置信度分布", "，".join(f"{k:.2f}: {v}" for k, v in sorted(dist.items(), reverse=True)))

    if args.json:
        _print(json.dumps(
            {
                "gap_seconds": gap,
                "gap_source": source,
                "assets": len(assets),
                "periods": len(buckets),
                "shots": len(all_groups),
                "multi_asset_shots": len(multi),
                "max_assets_per_shot": max((g.size for g in all_groups), default=0),
                "needs_review": len(review),
                "confidence_distribution": {f"{k:.2f}": v for k, v in dist.items()},
                "samples": [
                    {
                        "primary_asset_id": g.primary_asset_id,
                        "assets": g.asset_ids,
                        "confidence": g.confidence,
                        "captured_at": g.captured_at,
                        "notes": g.notes,
                    }
                    for g in multi[:20]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if not args.apply:
        _print()
        _print("【预演】未写入数据库。")
        _print("        分组只是**可重算的派生线索**（铁律 #3）；重算采用「只增不删」，")
        _print("        已进入精选的 Shot 永远不会因重算而丢失（决策 #4）。")
        if db.scalar("SELECT COUNT(*) FROM session", default=0) == 0:
            _print()
            _warn("session 表还是空的：落库前请先跑 `photoos sessions backfill --apply`。")
        return 0

    # ---- 落库（只增不删）
    from .repos import ShotRepo

    repo = ShotRepo(db)
    before = repo.assert_invariants()
    stats, _mapping = _persist_shots(db, all_groups, library_ids=library_ids)

    # ★ 并发兜底：界面可能正在标精选（`serve` 是另一个进程）。
    # 若某条精选恰好写在"已被本次重算退役"的 Shot 上，上面的搬迁是搬不到的
    # （它当时还不存在）—— 实测能静默搁浅 18/300 条。这里再兜一遍，幂等。
    reconciled = repo.reconcile_stranded_references()
    if reconciled["selection_migrated"] or reconciled["collection_items_moved"] \
            or reconciled["collection_covers_moved"]:
        _kv("并发兜底：追回搁浅引用",
            f"精选 {reconciled['selection_migrated']} 条、"
            f"主题成员 {reconciled['collection_items_moved']} 条、"
            f"主题封面 {reconciled['collection_covers_moved']} 个"
            "（界面并发写入时才会出现）")

    after = repo.assert_invariants()

    # ★ 铁律 #3 + 决策 #4 的运行时断言：精选绝不能因重算而减少
    if after["selections"] < before["selections"] or after["finals"] < before["finals"]:
        _fail(
            f"重算导致精选记录减少（selection {before['selections']}→{after['selections']}，"
            f"FINAL {before['finals']}→{after['finals']}）—— 这是严重缺陷，请勿继续，"
            "并立即从备份恢复。"
        )
        return 2
    if after["assets_with_shot"] > len(assets):
        _fail("挂到 Shot 的资产数超过参与分组的资产数 —— 数据不一致，请停止操作。")
        return 2

    from .db import utc_now as _utc_now

    with db.write() as conn:
        conn.execute(
            "INSERT INTO app_event (at, kind, library_id, payload_json) VALUES (?, 'shots_regroup', NULL, ?)",
            (
                _utc_now(),
                json.dumps(
                    {"gap_seconds": gap, "quoted": stats, "shots_active": after["shots_active"]},
                    ensure_ascii=False,
                ),
            ),
        )

    _print("Shot 分组 · 已执行")
    _ok(f"新建 {stats['created']} 个 Shot，更新 {stats['updated']} 个，退役 {stats['retired']} 个")
    _kv("活跃 Shot 总数", after["shots_active"])
    _kv("退役 Shot 总数", after["shots_retired"])
    _kv("挂到 Shot 的资产", after["assets_with_shot"])
    _print()
    _kv("精选记录（重算前→后）", f"{before['selections']} → {after['selections']}（只增不减 ✔）")
    _kv("其中 FINAL（前→后）", f"{before['finals']} → {after['finals']}（只增不减 ✔）")
    # 迁移数必须报出来：`selection.shot_id` 是主键，精选若留在退役行上
    # **行数不变但用户看不见**（所有查询都带 retired_at IS NULL）。
    # 只报"只增不减"会让人误以为一切正常。
    migrated = int(stats.get("selection_migrated", 0))
    if migrated:
        _kv("精选已迁移到新分组", f"{migrated} 条（原 Shot 退役，精选随资产改挂到新 Shot）")
    live = repo.live_selection_counts()
    _kv("精选**可见**（挂活跃 Shot）", f"{live['selections']} 条，其中 FINAL {live['finals']} 条")
    _print("        原始照片文件全程只被读取，未发生任何移动/改名/覆盖/删除。")
    if stats["retired"]:
        _print()
        _warn(
            f"{stats['retired']} 个旧 Shot 被标记退役（**没有删除**）——"
            "它们若有精选记录，已保留在库里（决策 #4）。"
        )
    return 0


# -------------------------------------------------------------------------- jobs
def cmd_jobs_list(args: argparse.Namespace) -> int:
    from .jobs import JobQueue

    _, db = _open_db()
    queue = JobQueue(db)
    jobs = queue.list_recent(limit=args.limit, include_finished=not args.active)
    if not jobs:
        _print("没有任务记录。" if not args.active else "当前没有进行中的任务。")
        return 0
    for job in jobs:
        _print("  " + job.describe())
        if job.progress_note:
            _print(f"      说明：{job.progress_note}")
        if job.scope.get("library_id"):
            _print(f"      Library：#{job.scope['library_id']}")
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    from .jobs import JobQueue

    _, db = _open_db()
    queue = JobQueue(db)
    job = queue.get(args.job_id)
    _print(f"任务 #{job.id}")
    _kv("类型", job.type)
    _kv("状态", job.status)
    _kv("进度", f"{job.progress_done}/{job.progress_total} ({job.percent:.1f}%)")
    _kv("说明", job.progress_note or "-")
    _kv("重试", f"{job.retry_count}/{job.max_retries}")
    _kv("Library", job.scope.get("library_id", "-"))
    _kv("创建", job.created_at)
    _kv("开始", job.started_at or "-")
    _kv("结束", job.finished_at or "-")
    _kv("心跳", job.heartbeat_at or "-")
    if job.error:
        _kv("错误", job.error[:600])
    states = queue.item_states(job.id)
    if states:
        _kv("明细状态", "，".join(f"{k}={v}" for k, v in sorted(states.items())))
        failures = queue.failed_items(job.id, limit=10)
        if failures:
            _print("  失败明细（最多 10 条）：")
            for row in failures:
                _print(f"    - {row['ref_key']}：{str(row['last_error'])[:120]}")
    return 0


def cmd_jobs_action(args: argparse.Namespace) -> int:
    from .jobs import JobQueue

    _, db = _open_db()
    queue = JobQueue(db)
    if args.job_action == "pause":
        job = queue.pause(args.job_id)
        _ok(f"任务 #{job.id} 已请求暂停（当前状态 {job.status}）")
        _print("        正在运行的阶段会在下一个检查点停下，进度与明细都会保留。")
    elif args.job_action == "resume":
        job = queue.resume(args.job_id)
        _ok(f"任务 #{job.id} 已重新入队（状态 {job.status}）")
        _print("        执行 `photoos index` 或对应命令即可续跑；已完成的明细不会重做。")
    elif args.job_action == "cancel":
        job = queue.cancel(args.job_id)
        _ok(f"任务 #{job.id} 已取消（状态 {job.status}）")
    elif args.job_action == "recover":
        count = queue.recover_orphans(stale_after_seconds=0)
        _ok(f"已回收 {count} 个因进程中断而卡住的任务（回退为待执行）")
    return 0



# ------------------------------------------------------------------ library probe
def cmd_library_probe(args: argparse.Namespace) -> int:
    _, db = _open_db()
    root = Path(args.path).expanduser()
    result = library_store.probe_path(db, root)

    _print("Library 探测（只读）")
    _kv("路径", result.root)
    _kv("可访问", "是" if result.is_dir else "否")
    _print()

    identity = result.volume_identity
    if identity is not None:
        _print("卷身份")
        _kv("挂载点", identity.mount)
        _kv("卷标", identity.label or "(无)")
        _kv("文件系统", identity.fs_type)
        _kv("类型", identity.kind)
        _kv("序列号", volume_mod.format_serial(identity.serial))
    else:
        _print("卷身份：无法获取（路径不存在或卷未挂载）")
    _print()

    _print("库标记")
    if result.marker:
        _kv("library_uuid", result.marker.get("library_uuid"))
        _kv("标记中的名称", result.marker.get("name"))
        _kv("标记创建时间", result.marker.get("created_at"))
    else:
        _kv("标记", f"未发现（{library_store.marker_path(result.root)}）")
    _print()

    _print("判定")
    _kv("建议动作", result.action)
    _kv("依据", result.reason)
    if result.library is not None:
        _kv("命中 Library", f"#{result.library.id} {result.library.name}（uuid={result.library.uuid[:8]}…）")

    if result.action == "create" and args.count:
        _print()
        _print("媒体清点（只读遍历，可能需要几分钟）…")
        media, errors = walker.count_media(result.root)
        _kv("媒体文件数", media)
        _kv("遍历错误数", errors)
    return 0


# -------------------------------------------------------------------- library add
def cmd_library_add(args: argparse.Namespace) -> int:
    settings, db = _open_db()
    store = library_store.LibraryStore(db)
    root = Path(args.path).expanduser()
    name = args.name or root.name or str(root)

    probe = library_store.probe_path(db, root)
    _print("登记 Library")
    _kv("路径", root)
    _kv("探测结论", f"{probe.action} — {probe.reason}")
    if probe.volume_identity is not None:
        siblings = store.libraries_for_volume(probe.volume_identity.serial)
        if len(siblings) > 1 or (siblings and probe.library is None):
            _kv(
                "该卷上的其他 Library",
                "、".join(f"#{lib.id} {lib.name}" for lib, _ in siblings),
            )

    if not probe.is_dir:
        _fail("路径不可访问，无法登记。若这是已登记的 Library，当前应先保持离线状态。")
        return 1

    if probe.library is not None and probe.action in {"reuse", "rebind"}:
        library = probe.library
        store.set_root_hint(library.id, root)
        store.set_status(library.id, "ONLINE")
        # 若库根现在带有标记而库里还没记录，顺手补上（强化后续识别）
        if probe.marker and library.marker_uuid != probe.marker["library_uuid"]:
            store.set_marker_uuid(library.id, str(probe.marker["library_uuid"]))
        if probe.volume_identity is not None:
            volume_record = store.record_volume(probe.volume_identity, library_id=library.id)
            store.link_volume_library(volume_record.id, library.id, root)
        _ok(f"已更新既有 Library #{library.id}「{library.name}」的运行时路径提示")
        _kv("uuid", library.uuid)
        store.log_event(
            "library_rebind" if probe.action == "rebind" else "library_reuse",
            library_id=library.id,
            payload={"root": str(root), "action": probe.action},
        )
        return 0

    # 身份优先级：既有库标记 > 卷序列号 > 新建
    marker_uuid = str(probe.marker["library_uuid"]) if probe.marker else None
    library_uuid = marker_uuid

    library = store.create(name=name, root=root, library_uuid=library_uuid, marker_uuid=marker_uuid)
    _ok(f"已登记 Library #{library.id}「{library.name}」")
    _kv("uuid", library.uuid)

    if probe.volume_identity is not None:
        volume_record = store.record_volume(probe.volume_identity, library_id=library.id)
        store.link_volume_library(volume_record.id, library.id, root)
        _ok(f"已登记卷：{volume_record.mount_hint} 序列号 {volume_record.serial_hex}")
    else:
        _warn("未能获取卷身份：该 Library 只能依赖路径与库标记识别")

    if probe.marker is not None:
        _ok("库标记已存在，未重复写入")
    elif args.write_marker:
        target = library_store.write_marker(root, library.uuid, library.name)
        # 记下标记 uuid，使后续探测能通过「判据 1」直接命中
        store.set_marker_uuid(library.id, library.uuid)
        _ok(f"已写入库标记：{target}")
        _print("        （这是 PhotoOS 写入照片库的唯一文件，不触碰任何照片）")
    else:
        _print()
        _warn("未写入库标记（使用 --write-marker 开启）。")
        _print("        强烈建议写入：盘符变化或换机后，标记是最可靠的识别依据（PRD §5.1）。")

    store.log_event("library_create", library_id=library.id, payload={"root": str(root)})
    _print()
    _print(f"数据根：{settings.data_root}")
    return 0


# ------------------------------------------------------------------- library list
def cmd_library_list(args: argparse.Namespace) -> int:
    _, db = _open_db()
    store = library_store.LibraryStore(db)
    libraries = store.list_all()
    if not libraries:
        _print("尚未登记任何 Library。使用 `photoos library add <路径> --write-marker` 开始。")
        return 0

    for library in libraries:
        _print(f"#{library.id}  {library.name}")
        _kv("uuid", library.uuid)
        _kv("状态", library.status)
        _kv("路径提示", library.root_path_hint or "(未记录)")
        _kv("库标记", "有" if library.marker_uuid else "无")
        _kv("Asset 数", store.live_asset_count(library.id))
        for volume_record in store.volumes_for(library.id):
            hint = store.volume_root_hint(library.id, volume_record.id) or volume_record.mount_hint or "?"
            _kv(
                "卷",
                f"{hint}  序列号={volume_record.serial_hex}  "
                f"{volume_record.kind}  {volume_record.status}",
            )
        _print()
    return 0


# --------------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.data_root:
        settings = Settings(data_root=Path(args.data_root))

    _print("PhotoOS 初始化")
    _print(f"  项目根：{PROJECT_ROOT}")
    _print(f"  数据根：{settings.data_root}")

    settings.ensure_dirs()
    _ok(f"已创建数据目录（thumbnails / previews / jobs / logs / backups）")

    db = Database(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    existed = db.exists()

    result = apply_migrations(db, backup_dir=settings.backups_dir, app_version=__version__)

    if result.applied:
        for label in result.applied:
            _ok(f"已应用迁移 {label}")
    else:
        _ok(f"schema 已是最新（版本 {result.to_version}）")

    if result.backup_path is not None:
        _ok(f"迁移前备份：{result.backup_path}")

    if not existed:
        # ★ 数据根来自环境变量（`PHOTOOS_DATA_ROOT`）或 `--data-root` 时，**不写真实 config.json**。
        #
        # 为什么：`Settings.save()` 是**整文件重写**。即便它已经保护了 `data_root` 这个键
        # （Phase 1 的 #16/#20 修复），其余键仍会按当前进程的设置重写一遍 ——
        # 在"隔离数据根做实验"时，这等于悄悄改动了真实部署的配置文件。
        # 用一个不影响落盘内容的哨兵路径把这次写入重定向到隔离根内部，
        # 既保留"新库会写一份配置"的行为，又不碰真实文件。
        if os.environ.get("PHOTOOS_DATA_ROOT") or args.data_root:
            config_path = settings.save(settings.data_root / "config.json")
            _ok(f"已写入配置：{config_path}（隔离数据根，未改动真实 config.json）")
        else:
            config_path = settings.save()
            _ok(f"已写入配置：{config_path}")

    check = db.integrity_check()
    if check == "ok":
        _ok("integrity_check 通过")
    else:
        _fail(f"integrity_check 异常：{check}")
        return 1

    _print()
    _print(f"数据库：{settings.db_path}")
    _print(f"schema 版本：{db.schema_version()}")
    _print("下一步：`photoos doctor` 环境自检，或 `photoos library add <照片库路径>` 添加 Library。")
    return 0


# ------------------------------------------------------------------------- doctor
def _probe_python() -> tuple[bool, str]:
    return True, f"{sys.version.split()[0]}  ({sys.executable})"


def _probe_imports() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, str]] = [
        ("PIL", "Pillow"),
        ("pillow_heif", "pillow-heif"),
        ("rawpy", "rawpy"),
        ("numpy", "numpy"),
        ("exifread", "ExifRead"),
        ("blake3", "blake3"),
        ("imageio_ffmpeg", "imageio-ffmpeg"),
        ("fastapi", "FastAPI"),
        ("uvicorn", "Uvicorn"),
    ]
    out: list[tuple[str, bool, str]] = []
    for module_name, label in checks:
        try:
            module = __import__(module_name)
            version = getattr(module, "__version__", None) or getattr(module, "version", "") or ""
            out.append((label, True, str(version)))
        except Exception as exc:  # noqa: BLE001
            out.append((label, False, f"{type(exc).__name__}: {exc}"))
    return out


def _probe_libheif() -> tuple[bool, str]:
    """确认 HEIC 解码由随包 libheif 提供，而非依赖系统 HEIF 扩展（PRD §8 / 断点 #11）。"""
    try:
        import pillow_heif
    except Exception as exc:  # noqa: BLE001
        return False, f"import 失败：{exc}"
    try:
        version = pillow_heif.libheif_version()
    except Exception as exc:  # noqa: BLE001
        return False, f"libheif_version() 失败：{exc}"
    return True, f"内置 libheif {version}（静态随 wheel 分发，不依赖系统编解码器）"


def _probe_rawpy() -> tuple[bool, str]:
    try:
        import rawpy
    except Exception as exc:  # noqa: BLE001
        return False, f"import 失败：{exc}"
    libraw = getattr(rawpy, "libraw_version", None)
    compiled = getattr(rawpy, "libraw_version_compiled", None)
    parts = [f"rawpy {getattr(rawpy, '__version__', '?')}"]
    if libraw:
        parts.append("LibRaw " + ".".join(str(x) for x in libraw))
    if compiled and compiled != libraw:
        parts.append("(compiled " + ".".join(str(x) for x in compiled) + ")")
    return True, " ".join(parts)


def _probe_ffmpeg() -> tuple[bool, str]:
    try:
        import imageio_ffmpeg
    except Exception as exc:  # noqa: BLE001
        return False, f"import 失败：{exc}"
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        return False, f"未找到内置 ffmpeg：{exc}"
    exists = Path(exe).exists()
    version = ""
    try:
        version = imageio_ffmpeg.get_ffmpeg_version()
    except Exception:  # noqa: BLE001
        pass
    if not exists:
        return False, f"二进制不存在：{exe}"
    # 注意：imageio-ffmpeg 不含 ffprobe（实测），视频元数据走 ffmpeg 头部解析。
    return True, f"{version}  无 ffprobe（改用 ffmpeg 头部解析）"


def _probe_db(settings: Settings) -> tuple[bool, str]:
    db = Database(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    if not db.exists():
        return False, f"数据库不存在：{settings.db_path}（请先运行 `photoos init`）"
    version = db.schema_version()
    expected = target_version()
    try:
        verify_checksums(db)
    except PhotoOSError as exc:
        return False, f"迁移 checksum 校验失败：{exc}"
    check = db.quick_check()
    ok = check == "ok" and version == expected
    detail = f"schema {version}/{expected}，quick_check={check}"
    return ok, detail


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    _print("PhotoOS 环境自检 (doctor)")
    _print()

    problems = 0

    _print("[1] Python 运行时")
    _, text = _probe_python()
    _ok(text)
    db_sqlite = __import__("sqlite3").sqlite_version
    _ok(f"内置 SQLite {db_sqlite}")
    _print()

    _print("[2] 依赖库")
    for label, ok, detail in _probe_imports():
        if ok:
            _ok(f"{label:<16} {detail}")
        else:
            _fail(f"{label:<16} {detail}")
            problems += 1
    _print()

    _print("[3] 媒体解码能力（PRD §8 统一适配层）")
    for label, prober in (
        ("HEIC/HEIF", _probe_libheif),
        ("RAW/NEF", _probe_rawpy),
        ("Video", _probe_ffmpeg),
    ):
        ok, detail = prober()
        if ok:
            _ok(f"{label:<10} {detail}")
        else:
            _fail(f"{label:<10} {detail}")
            problems += 1
    _print()

    _print("[4] 数据库")
    ok, detail = _probe_db(settings)
    if ok:
        _ok(detail)
    else:
        _warn(detail)
    _print()

    _print("[5] 路径")
    _kv("配置来源", CONFIG_PATH if CONFIG_PATH.exists() else "（未生成，使用默认值）")
    _kv("数据根", settings.data_root)
    _kv("前端目录", settings.resolved_web_root())
    _print()

    if problems:
        _print(f"自检完成：发现 {problems} 个问题。")
        return 1
    _print("自检完成：环境就绪。")
    return 0


# ------------------------------------------------------------------------- config
def cmd_config(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.json:
        payload = {
            "project_root": str(PROJECT_ROOT),
            "config_path": str(CONFIG_PATH),
            "data_root": str(settings.data_root),
            "db_path": str(settings.db_path),
            "thumbnails_dir": str(settings.thumbnails_dir),
            "previews_dir": str(settings.previews_dir),
            "host": settings.host,
            "port": settings.port,
            "preview_workers": settings.effective_preview_workers,
            "hash_workers": settings.effective_hash_workers,
            "batch_size": settings.batch_size,
            "shot_gap_seconds": settings.shot_gap_seconds,
            "session_gap_hours": settings.session_gap_hours,
        }
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print("PhotoOS 配置")
    _kv("项目根", PROJECT_ROOT)
    _kv("配置文件", CONFIG_PATH if CONFIG_PATH.exists() else "（未生成）")
    _kv("数据根", settings.data_root)
    _kv("数据库", settings.db_path)
    _kv("缩略图目录", settings.thumbnails_dir)
    _kv("预览目录", settings.previews_dir)
    _kv("监听地址", f"{settings.host}:{settings.port}")
    _kv("预览线程", settings.effective_preview_workers)
    _kv("Hash 线程", settings.effective_hash_workers)
    _kv("批量事务行数", settings.batch_size)
    _kv("Shot 分组间隔", f"{settings.shot_gap_seconds}s")
    _kv("Session 分段间隔", f"{settings.session_gap_hours}h")
    # Scene 的五个阈值（可在 data/config.json 改；`scenes rebuild` 会读它们）
    _kv("Scene 时间断点", f"{settings.scene_gap_seconds:g}s")
    _kv("Scene 合并相似度", f"{settings.scene_merge_similarity:g}")
    _kv("Scene 不相似连续张数", settings.scene_dissimilar_run)
    _kv("Scene Shot 上限", settings.scene_max_shots)
    _kv("Scene 不相似阈值", f"{settings.scene_unrelated_similarity:g}")
    return 0


# ------------------------------------------------------------------------ version
def cmd_version(_: argparse.Namespace) -> int:
    _print(f"photoos {__version__}")
    _print(f"schema 目标版本 {target_version()}")
    migrations = discover_migrations()
    for migration in migrations:
        _print(f"  - {migration.label}  (sha256 {migration.checksum[:12]}…)")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photoos",
        description="PhotoOS · 个人摄影管理系统（Phase 1 照片库基础）",
    )
    parser.add_argument("--version", action="version", version=f"photoos {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p_init = sub.add_parser("init", help="初始化数据目录与数据库（可重复执行，幂等）")
    p_init.add_argument("--data-root", metavar="PATH", help="覆盖数据根目录")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="环境与数据库自检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config", help="显示当前配置")
    p_config.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_config.set_defaults(func=cmd_config)

    p_version = sub.add_parser("version", help="显示版本与迁移清单")
    p_version.set_defaults(func=cmd_version)

    # ------------------------------------------------------------------ library
    p_lib = sub.add_parser("library", help="Library 管理（添加 / 探测 / 列表）")
    lib_sub = p_lib.add_subparsers(dest="library_command", metavar="<子命令>")

    p_lib_probe = lib_sub.add_parser("probe", help="只读探测一个路径相对于现有 Library 的身份")
    p_lib_probe.add_argument("path", metavar="PATH", help="照片库根目录")
    p_lib_probe.add_argument("--count", action="store_true", help="同时清点媒体文件数（只读遍历）")
    p_lib_probe.set_defaults(func=cmd_library_probe)

    p_lib_add = lib_sub.add_parser("add", help="登记一个 Library")
    p_lib_add.add_argument("path", metavar="PATH", help="照片库根目录")
    p_lib_add.add_argument("--name", help="Library 名称（默认取目录名）")
    p_lib_add.add_argument(
        "--write-marker",
        action="store_true",
        help="在库根写入 .photoos/library.json（PhotoOS 写入照片库的唯一文件，不触碰照片）",
    )
    p_lib_add.set_defaults(func=cmd_library_add)

    p_lib_list = lib_sub.add_parser("list", help="列出已登记的 Library 与卷")
    p_lib_list.set_defaults(func=cmd_library_list)

    p_lib_rebind = lib_sub.add_parser("rebind", help="盘符变化/整盘更换后重新绑定（PRD §5.2）")
    p_lib_rebind.add_argument("path", metavar="PATH", help="照片库的新位置")
    p_lib_rebind.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_lib_rebind.add_argument("--sample", type=int, default=32, help="抽样校验数量（默认 32）")
    p_lib_rebind.add_argument("--dry-run", action="store_true", help="只预演，不改数据库")
    p_lib_rebind.set_defaults(func=cmd_library_rebind)

    p_lib_verify = lib_sub.add_parser("verify", help="检查 Library 是否可达与索引一致性")
    p_lib_verify.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_lib_verify.set_defaults(func=cmd_library_verify)

    p_lib_offline = lib_sub.add_parser("offline", help="标记 Library 离线（硬盘暂时不接）")
    p_lib_offline.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_lib_offline.add_argument("--reason", help="离线原因（记录到审计事件）")
    p_lib_offline.set_defaults(func=cmd_library_offline)

    # ------------------------------------------------------------------ 迁移
    p_migrate = sub.add_parser("migrate", help="换机迁移包：导出 / 校验 / 导入（PRD §5.1）")
    mig_sub = p_migrate.add_subparsers(dest="migrate_command", metavar="<子命令>")

    p_mig_export = mig_sub.add_parser("export", help="导出迁移包（照片原文件默认不入包）")
    p_mig_export.add_argument("--output", metavar="PATH", help="输出 zip 路径")
    p_mig_export.set_defaults(func=cmd_migrate_export)

    p_mig_verify = mig_sub.add_parser("verify", help="校验迁移包完整性与版本兼容性")
    p_mig_verify.add_argument("package", metavar="PACKAGE")
    p_mig_verify.set_defaults(func=cmd_migrate_verify)

    p_mig_import = mig_sub.add_parser("import", help="导入迁移包（自动备份现有数据根）")
    p_mig_import.add_argument("package", metavar="PACKAGE")
    p_mig_import.add_argument("--dry-run", action="store_true", help="只校验，不改动数据")
    p_mig_import.add_argument("--fast", action="store_true", help="跳过逐项 sha256 深度校验")
    p_mig_import.set_defaults(func=cmd_migrate_import)

    # ------------------------------------------------------------------ 备份/完整性
    p_backup = sub.add_parser("backup", help="在线备份数据库（VACUUM INTO）")
    p_backup.add_argument("--label", default="manual", help="备份标签（默认 manual）")
    p_backup.add_argument("--keep", type=int, help="备份后仅保留最近 N 份")
    p_backup.add_argument("--list", action="store_true", help="只列出已有备份")
    p_backup.set_defaults(func=cmd_backup)

    p_integrity = sub.add_parser("integrity", help="数据库完整性与一致性检查")
    p_integrity.set_defaults(func=cmd_integrity)

    p_serve = sub.add_parser("serve", help="启动本地 Web UI（默认 127.0.0.1）")
    p_serve.add_argument("--host", help="监听地址（默认取配置，127.0.0.1）")
    p_serve.add_argument("--port", type=int, help="监听端口（默认取配置，8760）")
    p_serve.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    p_serve.add_argument(
        "--log-file",
        nargs="?",
        const="",
        default=None,
        help="把日志写进文件（不带值 = 数据目录的 logs/serve.log）。"
             "要长期开着、或者从会被回收的后台启动时用它 —— "
             "uvicorn 默认把每个请求都打到 stdout，量很大。",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_breakpoint = sub.add_parser("breakpoint", help="执行 PRD 断点测试并生成报告（--phase 1|2）")
    p_breakpoint.add_argument(
        "--phase", type=int, choices=(1, 2), default=1,
        help="1 = PRD §9.1 照片库基础（11 项，默认）；2 = PRD §9.2 精选系统（9 项）",
    )
    p_breakpoint.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_breakpoint.add_argument("--periods", type=int, default=4, help="代表性时段数量（默认 4，仅 phase 1）")
    p_breakpoint.add_argument("--full", action="store_true", help="对整个库做全量验收（更慢，仅 phase 1）")
    p_breakpoint.add_argument("--output", metavar="PATH", help="报告输出路径（默认写入 data/logs）")
    p_breakpoint.set_defaults(func=cmd_breakpoint)

    p_repair = sub.add_parser("repair", help="修复派生数据与数据库不一致（不动原片）")
    p_repair.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_repair.add_argument("--apply", action="store_true", help="实际执行修复（默认只预演）")
    p_repair.set_defaults(func=cmd_repair)

    p_import = sub.add_parser(
        "import", help="从 SD 卡/暂存目录/文件夹导入照片（唯一写入照片库的能力，默认只预演）"
    )
    p_import.add_argument("source", metavar="SOURCE", help="源目录（SD 卡、相机暂存目录、普通文件夹）")
    p_import.add_argument("--library", metavar="ID|NAME", help="目标照片库（默认需唯一）")
    p_import.add_argument(
        "--video-library",
        metavar="ID|NAME",
        help="目标视频库（源中含视频时必填；照片与视频要一起切段，见方案 §1.3.6）",
    )
    p_import.add_argument("--subdir", help="（--no-classify 时）在 Library 内使用的子目录")
    p_import.add_argument("--limit", type=int, help="最多导入多少个文件")
    p_import.add_argument("--apply", action="store_true", help="实际执行复制（默认只预演）")
    p_import.add_argument(
        "--no-classify",
        action="store_true",
        help="不做物理分类，照搬源的目录结构（旧行为）",
    )
    p_import.add_argument(
        "--geo-cache", metavar="CSV", help="GPS→城市缓存路径（默认用数据根下的 geo-cache.csv）"
    )
    p_import.add_argument("--plan-out", metavar="CSV", help="把归类计划写成 CSV（可审计/可复核）")
    p_import.add_argument(
        "--gap-hours", type=float, default=None, help="时段切分间隔（小时），默认 12"
    )
    p_import.add_argument(
        "--city",
        metavar="城市名",
        help="整批强制指定城市（例如「雅典」）—— 用于相机整段没有 GPS、"
        "或境外拍摄不在离线缓存里的情况；给出后压过 GPS 判定",
    )
    p_import.add_argument(
        "--no-index", action="store_true", help="复制后不自动补预览（留给你稍后手动 index）"
    )
    p_import.add_argument("--quiet", action="store_true", help="不打印逐项进度")
    p_import.set_defaults(func=cmd_import)

    p_cross = sub.add_parser(
        "crosscheck", help="与既有 PowerShell 工具产物交叉校验（两套独立实现互证，只读）"
    )
    p_cross.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
    p_cross.add_argument("--legacy-index", metavar="CSV", help="既有 library-index.csv")
    p_cross.add_argument("--legacy-hashes", metavar="CSV", help="既有 dup-hash-cache.csv")
    p_cross.add_argument("--hash-samples", type=int, default=12, help="哈希抽样数量（默认 12）")
    p_cross.set_defaults(func=cmd_crosscheck)

    # ------------------------------------------------------------------ 索引流水线
    def _add_library_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--library", metavar="ID|NAME", help="指定 Library（默认需唯一）")
        parser.add_argument("--quiet", action="store_true", help="不打印逐条进度")
        parser.add_argument("--force", action="store_true", help="忽略已有未完成任务，强制新建")

    p_scan = sub.add_parser("scan", help="发现阶段：遍历 Library 建立/更新索引（只读文件系统）")
    _add_library_arg(p_scan)
    p_scan.set_defaults(func=cmd_scan)

    p_index = sub.add_parser("index", help="完整索引：发现 → 元数据 → 预览 → 融合（一次读盘）→ 兜底")
    _add_library_arg(p_index)
    p_index.add_argument(
        "--wait",
        action="store_true",
        help="原地跑完该 Library 的全部剩余工作（先发现一次，再按剩余量驱动，最多 5 轮）",
    )
    p_index.set_defaults(func=cmd_index)

    p_hash = sub.add_parser("hash", help="哈希阶段：计算 BLAKE3 内容哈希（IO 受限，可暂停续跑）")
    _add_library_arg(p_hash)
    p_hash.add_argument("--limit", type=int, help="本次最多处理多少个文件")
    p_hash.set_defaults(func=cmd_hash)

    p_fused = sub.add_parser(
        "fused", help="融合阶段：一次读盘同时算哈希与生成缩略图/预览（比分别跑省一遍读盘）"
    )
    _add_library_arg(p_fused)
    p_fused.add_argument("--limit", type=int, help="本次最多处理多少个文件")
    p_fused.set_defaults(func=cmd_fused)

    p_preview = sub.add_parser(
        "preview", help="预览阶段：只生成缩略图与大图预览（不重算内容哈希）"
    )
    _add_library_arg(p_preview)
    p_preview.add_argument("--limit", type=int, help="本次最多处理多少个文件")
    p_preview.add_argument(
        "--prefix", metavar="相对路径", help="只处理该子树（例如 `杭州市\\2026-03-01_0900-1100`）"
    )
    p_preview.add_argument(
        "--regenerate",
        action="store_true",
        help="为该范围内全部在库文件重新生成缩略图与预览（含已存在的）",
    )
    p_preview.set_defaults(func=cmd_preview)

    p_geo = sub.add_parser("geo", help="离线地理缓存：查看 / 人工指认（导入分类不联网）")
    geo_sub = p_geo.add_subparsers(dest="geo_command", metavar="<子命令>")
    p_geo_list = geo_sub.add_parser("list", help="列出缓存里的城市与网格数")
    p_geo_list.add_argument("--geo-cache", metavar="CSV", help="缓存路径（默认数据根下 geo-cache.csv）")
    p_geo_list.set_defaults(func=cmd_geo)
    p_geo_assign = geo_sub.add_parser("assign", help="把某个 GPS 网格指认给一个城市")
    p_geo_assign.add_argument("coordinates", metavar="纬度,经度")
    p_geo_assign.add_argument("city", metavar="城市名")
    p_geo_assign.add_argument(
        "--radius-km",
        type=float,
        metavar="公里",
        help="把该点半径内的所有网格都指认为该城市（一趟旅行只需一条命令）",
    )
    p_geo_assign.add_argument("--geo-cache", metavar="CSV", help="缓存路径（默认数据根下 geo-cache.csv）")
    p_geo_assign.set_defaults(func=cmd_geo)

    p_dups = sub.add_parser("duplicates", help="报告内容重复的文件（只报告，不移动不删除）")
    _add_library_arg(p_dups)
    p_dups.add_argument("--samples", type=int, default=5, help="展示多少个重复组（默认 5）")
    p_dups.set_defaults(func=cmd_duplicates)

    p_stats = sub.add_parser("stats", help="显示索引统计（数量/类型/状态/元数据与预览覆盖）")
    p_stats.add_argument("--library", metavar="ID|NAME", help="指定 Library")
    p_stats.set_defaults(func=cmd_stats)

    # ------------------------------------------------------------------ AI（Phase 2）
    p_ai = sub.add_parser("ai", help="嵌入模型与运行时（Phase 2 相似度切片）")
    ai_sub = p_ai.add_subparsers(dest="ai_command", metavar="<子命令>")

    p_af = ai_sub.add_parser(
        "fetch-model", help="下载并校验模型（clip = 视觉 / gemma = 中文查询 / face = 人脸）")
    p_af.add_argument(
        "--engine", choices=("clip", "gemma", "face", "all"), default="clip",
        help="clip（默认，335 MB ONNX，Scene 划分用）/ gemma（1.49 GB，中文查询用）/ "
             "face（YuNet + SFace，分人用）/ all",
    )
    p_af.add_argument("--source", metavar="URL", help="模型基址（默认对应引擎的镜像）")
    p_af.add_argument("--force", action="store_true", help="即使已存在也重新下载")
    p_af.add_argument(
        "--local-dir", metavar="目录",
        help="离线导入：从该目录复制（**仍然逐个校验 sha256**），用于网络不可达时",
    )
    p_af.set_defaults(func=cmd_ai_fetch_model)

    p_ad = ai_sub.add_parser("doctor", help="报告嵌入运行时的真实设备（跑一次真推理计时）")
    p_ad.add_argument(
        "--device", choices=("auto", "cuda", "cpu"), default="auto",
        help="期望设备（默认 auto：有 CUDA 就用）",
    )
    p_ad.set_defaults(func=cmd_ai_doctor)

    p_embed = sub.add_parser(
        "embed", help="计算视觉嵌入并落库（只读 preview，不读原片；默认只预演）"
    )
    p_embed.add_argument("--library", metavar="ID|NAME", help="只处理指定 Library（默认全部）")
    p_embed.add_argument("--limit", type=int, help="本次最多算多少张")
    p_embed.add_argument(
        "--engine", choices=("clip", "gemma"), default="clip",
        help="clip（默认）= 视觉相似度，供 Scene 划分；"
             "gemma = EmbeddingGemma 2，供中文文字查询（需 torch，慢约 7 倍）",
    )
    p_embed.add_argument("--batch", type=int, help="批大小（clip 默认 16；gemma 默认 8，实测最优）")
    p_embed.add_argument(
        "--device", choices=("auto", "cuda", "cpu"), default="auto", help="设备（默认 auto）"
    )
    p_embed.add_argument("--dry-run", action="store_true", help="只统计待办数量，不写库")
    p_embed.add_argument("--apply", action="store_true", help="实际写入数据库")
    p_embed.add_argument("--quiet", action="store_true", help="不打印进度")
    p_embed.set_defaults(func=cmd_embed)

    # ------------------------------------------------------------------ 文字搜索（Phase 3）
    p_search = sub.add_parser(
        "search",
        help="文字查照片（Phase 3：缩小候选集，不替你选；需 torch 环境）",
    )
    p_search.add_argument("query", nargs="?", metavar="查询",
                          help="要查的画面描述，如 「日出」「婚纱」（用 --preset 时可省略）")
    p_search.add_argument("--top", type=int, default=24, help="返回多少个拍摄组（默认 24）")
    p_search.add_argument(
        "--prompt", metavar="英文串",
        help="实际送进模型的 prompt（默认用查询本身）。实测英文 prompt 更稳，"
             "可让界面标签是中文、底层用英文",
    )
    p_search.add_argument(
        "--save", action="store_true",
        help="把文本向量与命中快照写进库（迁移 007）—— 界面据此显示，无需 torch",
    )
    p_search.add_argument(
        "--preset", metavar="JSON",
        help="批量预置概念（界面下拉框的内容）。JSON 形如 "
             "[{\"label\":\"日出\",\"prompt\":\"sunrise\",\"ratio\":1.37}, ...]",
    )
    p_search.add_argument("--min-ratio", type=float, default=0.0,
                          help="配合 --preset：只预置凝聚度比值 ≥ 此值的概念（建议 1.20）")
    p_search.add_argument("--hits", type=int, default=100,
                          help="配合 --preset：每个概念存多少条命中（默认 100）")
    p_search.add_argument(
        "--engine", choices=("gemma",), default="gemma",
        help="目前只有 gemma（EmbeddingGemma 2）—— CLIP 的中文实测失效",
    )
    p_search.add_argument("--library", metavar="ID|NAME", help="只在该 Library 内搜索（默认全部）")
    p_search.add_argument(
        "--device", choices=("auto", "cuda", "cpu"), default="auto", help="设备（默认 auto）"
    )
    p_search.set_defaults(func=_dispatch_search)

    # ------------------------------------------------------------------ 结构化筛选（Phase 3 · L4）
    p_find = sub.add_parser(
        "find",
        help="按时间/地点/场次精确取照片（确定性，不依赖模型；比语义检索准）",
    )
    p_find.add_argument("--city", metavar="城市", help="按城市（库里 19 个，见 `photoos stats`）")
    p_find.add_argument("--session", metavar="关键词", help="按场次目录名/城市名模糊匹配，如 2025-10-18")
    p_find.add_argument("--date-from", metavar="YYYY-MM-DD", help="起始日期（含）")
    p_find.add_argument("--date-to", metavar="YYYY-MM-DD", help="结束日期（含）")
    p_find.add_argument("--scene", type=int, metavar="ID", help="直接指定 Scene")
    p_find.add_argument("--min-shots", type=int, metavar="N", help="只要 Shot 数 ≥ N 的 Scene")
    p_find.add_argument("--library", type=int, metavar="ID", help="限定 Library")
    p_find.add_argument("--limit", type=int, help="最多列几个 Scene（默认 60）")
    p_find.set_defaults(func=cmd_find)

    # ------------------------------------------------------------------ 分人（Phase 4）
    p_people = sub.add_parser(
        "people",
        help="分人：检测人脸 → 特征 → 聚类出候选 → 你命名 → 持续学习你的判断",
    )
    people_sub = p_people.add_subparsers(dest="people_command", metavar="<子命令>")

    p_pd = people_sub.add_parser("detect", help="检测人脸并落库（YuNet；只读 preview，不碰原片）")
    p_pd.add_argument("--session", metavar="关键词", help="只处理某场次（目录名模糊匹配）")
    p_pd.add_argument("--scene", type=int, metavar="ID", help="只处理某个 Scene")
    p_pd.add_argument("--library", type=int, metavar="ID", help="限定 Library")
    p_pd.add_argument("--limit", type=int, default=2000, help="本次最多检测多少张（默认 2000）")
    p_pd.add_argument("--all", action="store_true", help="不限量，跑完全部待检测的照片")
    p_pd.set_defaults(func=cmd_people_detect)

    p_pe = people_sub.add_parser("embed", help="计算人脸特征（SFace 128 维）")
    p_pe.add_argument("--limit", type=int, default=5000, help="本次最多编码多少张脸（默认 5000）")
    p_pe.add_argument("--all", action="store_true", help="不限量")
    p_pe.set_defaults(func=cmd_people_embed)

    p_pc = people_sub.add_parser(
        "discover", help="把未归类的正面清晰脸聚成候选人物（结果是**未命名**的）")
    p_pc.add_argument("--k", type=int, default=8, help="kNN 邻居数（默认 8，实测最稳）")
    p_pc.add_argument("--min-size", type=int, default=3, help="最小簇规模（默认 3）")
    p_pc.add_argument("--iterations", type=int, default=40, help="Chinese Whispers 迭代上限")
    p_pc.add_argument("--top", type=int, default=30, help="最多创建多少个候选人物（默认 30）")
    p_pc.set_defaults(func=cmd_people_discover)

    p_pn = people_sub.add_parser("name", help="给候选人物起名（名字只能由你给）")
    p_pn.add_argument("person_id", type=int, metavar="ID")
    p_pn.add_argument("name", metavar="名字")
    p_pn.set_defaults(func=cmd_people_name)

    p_pm = people_sub.add_parser("merge", help="把两个候选合并成一个人（模型把同一人拆成两簇时用）")
    p_pm.add_argument("person_id", type=int, metavar="源ID")
    p_pm.add_argument("--into", type=int, required=True, metavar="目标ID", help="保留的那一个")
    p_pm.set_defaults(func=cmd_people_merge)

    p_pa = people_sub.add_parser(
        "assign", help="用当前原型给未归类的脸找主人（增量；模型的判断，等你确认）")
    p_pa.add_argument("--all-persons", action="store_true",
                      help="让未命名的候选也参与（默认只让有名字的人参与）")
    p_pa.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    p_pa.set_defaults(func=cmd_people_assign)

    p_pl = people_sub.add_parser(
        "learn", help="从你的确认/否决里重新估计原型与阈值（重算，不是训练）")
    p_pl.add_argument("--verbose", action="store_true", help="打印每条学习备注")
    p_pl.set_defaults(func=cmd_people_learn)

    p_ps = people_sub.add_parser("status", help="分人流水线状态与每个人的可信度")
    p_ps.add_argument("--top", type=int, default=30, help="最多列多少个人（默认 30）")
    p_ps.set_defaults(func=cmd_people_status)

    # ------------------------------------------------------------------ Session（Phase 2）
    p_sessions = sub.add_parser(
        "sessions", help="Session（时段）管理：把既有 <城市>\\<时段> 目录补成 session 行"
    )
    sessions_sub = p_sessions.add_subparsers(dest="sessions_command", metavar="<子命令>")

    p_sb = sessions_sub.add_parser(
        "backfill",
        help="从既有目录派生 session 行（默认只预演；不读取任何照片文件）",
    )
    p_sb.add_argument("--library", metavar="ID|NAME", help="只处理指定 Library（默认全部）")
    p_sb.add_argument("--apply", action="store_true", help="实际写入数据库（默认只预演）")
    p_sb.add_argument(
        "--undo",
        action="store_true",
        help="删除由本命令写入的 session 行（source='auto' 且 city_hint 非空）；不涉及任何文件",
    )
    p_sb.add_argument("--json", action="store_true", help="以 JSON 输出派生结果（供脚本消费）")
    p_sb.set_defaults(func=cmd_sessions_backfill)

    # ------------------------------------------------------------------ Shot（Phase 2）
    p_shots = sub.add_parser("shots", help="Shot（一次拍摄）管理：按时间 + subsec 分组")
    shots_sub = p_shots.add_subparsers(dest="shots_command", metavar="<子命令>")

    p_sr = shots_sub.add_parser(
        "regroup",
        help="重算 Shot 分组（默认只预演；不读取任何照片文件）",
    )
    p_sr.add_argument("--library", metavar="ID|NAME", help="只处理指定 Library（默认全部）")
    p_sr.add_argument("--session", metavar="ID|NAME", help="取该时段的 review_config_json 覆盖")
    p_sr.add_argument(
        "--gap-seconds", type=float, dest="gap_seconds", metavar="SEC",
        help="相邻两张超过这个间隔就切新 Shot（覆盖 config 与 session 覆盖）",
    )
    p_sr.add_argument("--apply", action="store_true", help="实际写入数据库（默认只预演）")
    p_sr.add_argument("--json", action="store_true", help="以 JSON 输出（供脚本消费）")
    p_sr.set_defaults(func=cmd_shots_regroup)

    # ------------------------------------------------------------------ Scene（Phase 2）
    p_scenes = sub.add_parser("scenes", help="Scene（拍摄场景）管理：把时段切成可一次筛完的块")
    scenes_sub = p_scenes.add_subparsers(dest="scenes_command", metavar="<子命令>")

    p_scb = scenes_sub.add_parser(
        "rebuild", help="重算 Scene 划分（默认只预演；不读取任何照片文件）"
    )
    p_scb.add_argument("--library", metavar="ID|NAME", help="只处理指定 Library（默认全部）")
    p_scb.add_argument("--session", metavar="ID|NAME", help="取该时段的 review_config_json 覆盖")
    p_scb.add_argument("--gap-seconds", type=float, dest="gap_seconds", metavar="SEC",
                       help="Shot 分组阈值（覆盖 config）")
    p_scb.add_argument("--scene-gap-seconds", type=float, dest="scene_gap_seconds", metavar="SEC",
                       help="Scene 时间断点（默认取配置 scene_gap_seconds = 900 s）")
    p_scb.add_argument("--scene-merge-similarity", type=float, dest="scene_merge_similarity",
                       metavar="COS",
                       help="跨边界余弦 ≥ 它就合并（默认取配置 0.85）")
    p_scb.add_argument("--scene-dissimilar-run", type=int, dest="scene_dissimilar_run",
                       metavar="N",
                       help="连续 N 张不相似就细分（默认取配置 8）")
    p_scb.add_argument("--scene-unrelated-similarity", type=float, dest="scene_unrelated_similarity",
                       metavar="COS",
                       help="判定「不相似」的余弦阈值（默认取配置 0.5）")
    p_scb.add_argument("--max-shots", type=int, default=None, dest="max_shots", metavar="N",
                       help="单个 Scene 的 Shot 数上限（默认取配置 scene_max_shots = 60）")
    p_scb.add_argument("--no-similarity", action="store_true", dest="no_similarity",
                       help="即使库里已有嵌入也只用时间规则（用于 A/B 对照）")
    p_scb.add_argument("--json", action="store_true", help="以 JSON 输出（供脚本消费）")
    p_scb.add_argument("--apply", action="store_true", help="实际写入数据库（默认只预演）")
    p_scb.set_defaults(func=cmd_scenes_rebuild)

    # 人工调整：合并 / 拆分 / 解除标记（重算会跳过被人工改过的时段）
    p_scm = scenes_sub.add_parser("merge", help="把第二个 Scene 合并进第一个")
    p_scm.add_argument("primary", type=int, metavar="ID", help="保留下来的 Scene id")
    p_scm.add_argument("secondary", type=int, metavar="ID", help="被并入并删除的 Scene id")
    p_scm.set_defaults(func=cmd_scenes_merge)

    p_scs = scenes_sub.add_parser("split", help="在某个 Shot 之后把 Scene 一分为二")
    p_scs.add_argument("scene", type=int, metavar="ID", help="要拆的 Scene id")
    p_scs.add_argument("--after-shot", type=int, required=True, dest="after_shot", metavar="ID",
                       help="这个 Shot 及其之前留在原 Scene，之后的分到新 Scene")
    p_scs.add_argument("--name", metavar="NAME", help="新 Scene 的名字（默认「原名 · 拆出」）")
    p_scs.set_defaults(func=cmd_scenes_split)

    p_scu = scenes_sub.add_parser("unpin", help="解除人工标记，让重算重新接管这个 Scene")
    p_scu.add_argument("scene", type=int, metavar="ID", help="Scene id")
    p_scu.set_defaults(func=cmd_scenes_unpin)

    # ------------------------------------------------------------------ 任务管理
    p_jobs = sub.add_parser("jobs", help="查看与管理后台任务")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_command", metavar="<子命令>")

    p_jobs_list = jobs_sub.add_parser("list", help="列出最近任务（默认含已结束）")
    p_jobs_list.add_argument("--limit", type=int, default=20)
    p_jobs_list.add_argument("--active", action="store_true", help="只看进行中的任务")
    p_jobs_list.set_defaults(func=cmd_jobs_list)

    p_jobs_show = jobs_sub.add_parser("show", help="查看单个任务详情与失败明细")
    p_jobs_show.add_argument("job_id", type=int, metavar="ID")
    p_jobs_show.set_defaults(func=cmd_jobs_show)

    for action, help_text in (
        ("pause", "暂停任务（在下一个检查点停下，进度保留）"),
        ("resume", "恢复已暂停或失败的任务（已完成的明细不重做）"),
        ("cancel", "取消任务"),
        ("recover", "回收因进程中断而卡住的任务（回退为待执行）"),
    ):
        parser_action = jobs_sub.add_parser(action, help=help_text)
        if action != "recover":
            parser_action.add_argument("job_id", type=int, metavar="ID")
        parser_action.set_defaults(func=cmd_jobs_action, job_action=action)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        for group in ("library", "jobs", "migrate"):
            if getattr(args, "command", None) == group:
                # 只输入 `photoos library` / `jobs` / `migrate` 时给出子命令帮助，而不是报错
                parser.parse_args([*((argv or [])[:1]), group, "--help"])
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except PhotoOSError as exc:
        _fail(exc.message)
        if getattr(exc, "detail", None):
            _print(f"         detail: {exc.detail}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        _print()
        _warn("已中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
