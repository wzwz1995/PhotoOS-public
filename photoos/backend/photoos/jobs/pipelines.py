"""四个处理阶段（发现 / 哈希 / 元数据 / 预览）。

并发策略（依据 W4 实测，见 PHASE1_TECH_PLAN §11.4(9)）：
  * **哈希阶段是 IO 瓶颈**：实测该外置盘冷读仅 **55~71 MB/s**（多线程读同一块盘无收益）。
    因此哈希**单线程顺序**执行，专注避免磁盘寻道，并使用 BLAKE3 的 SIMD 与多缓冲。
    252 GB 照片库 ≈ 70~80 分钟，因此它必须是**可暂停、可恢复、可后台长时间运行**的独立任务。
  * **元数据/预览阶段是 CPU 瓶颈**：rawpy 与 Pillow 解码时释放 GIL，实测 6 线程有 **9× 加速**
    （589 ms/张 → 66 ms/张）。因此这两阶段使用线程池。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import GENERATOR_VERSION
from ..db import Database
from ..errors import MediaError, PhotoOSError
from ..hashing import HASH_ALGO, READ_CHUNK, hash_file
from ..library import walker
from ..library.store import LibraryStore
from ..media.adapter import generate_previews, generate_previews_from_bytes, previews_exist
from ..media.metadata import extract_metadata
from ..repos import AssetRepo, MetadataRepo, PreviewRepo, asset_absolute_path
from .queue import JobQueue

#: 批量提交粒度：发现阶段每次事务写入的记录数
DISCOVERY_BATCH = 400

#: 进度回调签名：(已完成, 总数, 说明)
ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]


def _noop_progress(done: int, total: int, note: str) -> None:  # pragma: no cover - 默认值
    return None


def _never_stop() -> bool:  # pragma: no cover - 默认值
    return False


# --------------------------------------------------------------------------- 哈希
# 说明：哈希实现在 `photoos.hashing`（被 jobs 与 library.rebind 共用，避免循环依赖）。
# 这里重新导出以保持 `from photoos.jobs.pipelines import hash_file` 的可用性。


@dataclass
class StageResult:
    done: int = 0
    failed: int = 0
    skipped: int = 0
    #: 本次遍历未看到、因而被标记为 MISSING 的文件数。
    #: **不能并入 `failed`** —— 删掉一个文件是正常操作，报成"失败"会误导
    #: （真实踩过：正常删除一个文件，scan 输出「失败 1」）。
    missing: int = 0
    bytes_processed: int = 0
    seconds: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def throughput_mb_s(self) -> float:
        if self.seconds <= 0:
            return 0.0
        return self.bytes_processed / 1e6 / self.seconds

    def summary(self) -> str:
        parts = [f"完成 {self.done}", f"失败 {self.failed}"]
        if self.missing:
            parts.append(f"缺失 {self.missing}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.bytes_processed:
            parts.append(f"{self.bytes_processed / 1e9:.2f} GB")
        if self.seconds:
            parts.append(f"{self.seconds:.1f}s")
            if self.bytes_processed:
                parts.append(f"{self.throughput_mb_s:.0f} MB/s")
        if self.errors:
            parts.append(f"错误样例 {len(self.errors)}")
        return "，".join(parts)


# --------------------------------------------------------------------------- 发现
def run_discovery(
    db: Database,
    library_id: int,
    root: Path,
    job_id: int,
    *,
    queue: JobQueue,
    scope_prefix: str | None = None,
    progress: ProgressFn = _noop_progress,
    stop: StopFn = _never_stop,
) -> StageResult:
    """遍历 Library 并建立/更新 asset 索引。只读文件系统，不读文件内容。

    `scope_prefix` 给出时只处理该子树（断点测试的分层策略用：先对代表性时段做全量验收）。
    """
    started = time.perf_counter()
    result = StageResult()
    assets = AssetRepo(db)

    batch: list[tuple[str, int, int, str, str | None]] = []
    seen: set[str] = set()
    total = 0

    for item in walker.walk_media(root, include_videos=True):
        if stop():
            queue.update_progress(job_id, note="已暂停/取消于发现阶段")
            break
        if isinstance(item, walker.WalkError):
            result.failed += 1
            if len(result.errors) < 50:
                result.errors.append((item.relative or str(item.path), item.message))
            continue
        if scope_prefix and not item.relative.startswith(scope_prefix):
            continue

        from ..media.detect import detect_by_extension_only

        # 发现阶段**只按扩展名推断**，不打开文件：遍历 16k 文件若逐文件读文件头，
        # 在外置盘上会从 1 秒变成十几分钟（16k 次随机读）。真实类型在元数据阶段确认。
        detected = detect_by_extension_only(item.path)
        mime = detected.mime if detected else None
        file_type = detected.file_type if detected else item.file_type
        batch.append((item.relative, item.size, item.mtime_ns, file_type, mime))
        seen.add(item.relative)
        total += 1

        if len(batch) >= DISCOVERY_BATCH:
            outcome = assets.upsert_discovered(library_id, batch)
            result.done += outcome.added + outcome.updated
            result.skipped += outcome.unchanged
            batch.clear()
            progress(total, 0, f"已发现 {total} 个文件")

    if batch:
        outcome = assets.upsert_discovered(library_id, batch)
        result.done += outcome.added + outcome.updated
        result.skipped += outcome.unchanged

    # 未被本次遍历看到的文件 → MISSING（绝不删除行）。
    # 注意：**限定了子树时不做这一步** —— 否则会把子树之外的文件全部误标为缺失。
    # 计入 `missing` 而不是 `failed`：这是正常的删除/移出，不是错误。
    if not stop() and not scope_prefix:
        result.missing = assets.mark_missing(library_id, seen)

    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- 哈希
def run_hash(
    db: Database,
    library_id: int,
    root: Path,
    job_id: int,
    *,
    queue: JobQueue,
    limit: int | None = None,
    scope_prefix: str | None = None,
    progress: ProgressFn = _noop_progress,
    stop: StopFn = _never_stop,
) -> StageResult:
    """为尚未哈希的 asset 计算内容哈希。

    **单线程顺序执行**：该阶段受外置盘顺序读速度限制（实测 55~71 MB/s），
    并发读同一块盘只会加剧寻道、不会提速。可暂停/恢复，进度按文件数记录。
    """
    started = time.perf_counter()
    result = StageResult()
    assets = AssetRepo(db)

    targets = assets.unhashed(library_id, limit=limit or 100_000, scope_prefix=scope_prefix)
    total = len(targets)
    queue.update_progress(job_id, done=0, total=total, note="开始计算内容哈希")
    progress(0, total, "开始计算内容哈希")

    for index, asset in enumerate(targets, start=1):
        if stop():
            queue.update_progress(job_id, note=f"已暂停/取消于 {index - 1}/{total}")
            break
        path = asset_absolute_path(root, asset.relative_path)
        try:
            digest = hash_file(path)
        except FileNotFoundError:
            assets.set_status(asset.id, "MISSING", "文件不存在")
            result.failed += 1
            queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                              ref_id=asset.id, state="FAILED", error="文件不存在")
            continue
        except OSError as exc:
            assets.set_status(asset.id, "UNREADABLE", f"{type(exc).__name__}: {exc}")
            result.failed += 1
            if len(result.errors) < 50:
                result.errors.append((asset.relative_path, str(exc)))
            queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                              ref_id=asset.id, state="FAILED", error=str(exc))
            continue

        assets.set_hash(asset.id, digest, HASH_ALGO)
        result.done += 1
        result.bytes_processed += int(asset.size or 0)
        queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                          ref_id=asset.id, state="DONE")
        if index % 25 == 0 or index == total:
            queue.update_progress(job_id, done=index, note=f"哈希 {index}/{total}")
            progress(index, total, f"哈希 {index}/{total}")

    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- 元数据
def run_metadata(
    db: Database,
    library_id: int,
    root: Path,
    job_id: int,
    *,
    queue: JobQueue,
    workers: int = 6,
    limit: int | None = None,
    scope_prefix: str | None = None,
    progress: ProgressFn = _noop_progress,
    stop: StopFn = _never_stop,
) -> StageResult:
    """提取 EXIF/容器元数据并落库。

    **并发度按批次成分自适应**（实测结论，见 PHASE1_TECH_PLAN §11.4(11)）：
      * RAW 文件（NEF 约 30 MB）的元数据读取在该 USB 盘上受随机读限制，
        实测 **单线程 2.2 文件/秒 反而快于 6 线程 1.7 文件/秒** —— 并发只会加剧寻道。
      * JPEG 文件小且顺序性更好，实测 6 线程可达 900+ 文件/秒（缓存命中时），并发收益明显。

    因此：批次以 RAW 为主（>50%）时用 1 个 worker 顺序读；否则用配置的并发度。

    注意：即使元数据为空（无 EXIF 的手机照片）也要写入一行，
    否则 `needing_metadata` 会每次都把它们挑出来重复处理。
    """
    started = time.perf_counter()
    result = StageResult()
    assets_repo = AssetRepo(db)
    metadata_repo = MetadataRepo(db)

    targets = assets_repo.needing_metadata(library_id, limit=limit or 100_000, scope_prefix=scope_prefix)
    total = len(targets)

    raw_count = sum(1 for asset in targets if asset.file_type == "raw")
    effective_workers = 1 if (targets and raw_count * 2 > total) else max(1, workers)

    queue.update_progress(
        job_id,
        done=0,
        total=total,
        note=f"开始提取元数据（{total} 个，RAW {raw_count} 个，并发 {effective_workers}）",
    )
    progress(0, total, "开始提取元数据")

    def process(asset) -> tuple[object, object | None, str | None]:
        if stop():
            return asset, None, "stopped"
        path = asset_absolute_path(root, asset.relative_path)
        try:
            info = extract_metadata(path)
        except PhotoOSError as exc:  # 适配层承诺不抛，这里只是兜底
            return asset, None, str(exc)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不得中断整批
            return asset, None, f"{type(exc).__name__}: {exc}"
        return asset, info, None

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        for index, (asset, info, error) in enumerate(pool.map(process, targets), start=1):
            if error == "stopped":
                queue.update_progress(job_id, note=f"已暂停/取消于 {index - 1}/{total}")
                break
            if error is not None:
                result.failed += 1
                if len(result.errors) < 50:
                    result.errors.append((asset.relative_path, error))
                queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                                  ref_id=asset.id, state="FAILED", error=error)
                continue

            row = info.to_row(asset.id)
            metadata_repo.upsert(row)
            # 有元数据即视为可读；视频/无 EXIF 文件同样标记 OK（尺寸已记录）
            if asset.status == "PENDING":
                assets_repo.mark_ok(asset.id)
            result.done += 1
            queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                              ref_id=asset.id, state="DONE")
            if index % 50 == 0 or index == total:
                queue.update_progress(job_id, done=index, note=f"元数据 {index}/{total}")
                progress(index, total, f"元数据 {index}/{total}")

    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- 预览
def run_previews(
    db: Database,
    library_id: int,
    root: Path,
    data_root: Path,
    job_id: int,
    *,
    queue: JobQueue,
    workers: int = 6,
    limit: int | None = None,
    force: bool = False,
    scope_prefix: str | None = None,
    progress: ProgressFn = _noop_progress,
    stop: StopFn = _never_stop,
) -> StageResult:
    """生成缩略图与预览。CPU 密集（解码 + 缩放 + WebP 编码），使用线程池。

    只读原片；输出全部落在 `data_root` 下。已生成的不重复生成（PRD §11.2）。
    """
    started = time.perf_counter()
    result = StageResult()
    assets_repo = AssetRepo(db)
    preview_repo = PreviewRepo(db)

    targets = (
        assets_repo.pending(library_id, limit=limit or 100_000, scope_prefix=scope_prefix)
        if force
        else assets_repo.needing_previews(
            library_id, limit=limit or 100_000, scope_prefix=scope_prefix
        )
    )
    total = len(targets)

    # 预览阶段必须读完整原片（RAW 需取内嵌 JPEG），因此受同一块 USB 盘限制。
    # 实测（PHASE1_TECH_PLAN §11.4(12)）：纯单线程无法重叠 CPU 编码与 IO 读取，
    # 2 个 worker 能明显改善重叠且不至于加剧寻道，因此 RAW 为主时收到 2。
    raw_count = sum(1 for asset in targets if asset.file_type == "raw")
    effective_workers = (
        min(2, max(1, workers)) if (targets and raw_count * 2 > total) else max(1, workers)
    )

    queue.update_progress(
        job_id,
        done=0,
        total=total,
        note=f"开始生成缩略图与预览（{total} 个，RAW {raw_count} 个，并发 {effective_workers}）",
    )
    progress(0, total, "开始生成缩略图与预览")

    def process(asset):
        if stop():
            return asset, None, "stopped"
        path = asset_absolute_path(root, asset.relative_path)
        try:
            # **必须强制重建**：本阶段的目标集来自 `needing_previews`，即"数据库缺记录"的资产；
            # 而适配器判断能否复用看的是**磁盘文件是否存在**（media.adapter._write_previews）。
            # 两者口径不一致就会踩坑：原片内容变了、我们只删了数据库行而磁盘文件还在时，
            # 适配器会直接复用旧图并重新登记 —— 陈旧缩略图被静默"恢复"成新记录。
            # 实测现场：改图后重跑，缩略图字节与修改前完全一致。
            previews = generate_previews(asset.id, path, data_root, force=True)
        except MediaError as exc:
            return asset, None, str(exc)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不得中断整批
            return asset, None, f"{type(exc).__name__}: {exc}"
        return asset, previews, None

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        for index, (asset, previews, error) in enumerate(pool.map(process, targets), start=1):
            if error == "stopped":
                queue.update_progress(job_id, note=f"已暂停/取消于 {index - 1}/{total}")
                break
            if error is not None:
                result.failed += 1
                if len(result.errors) < 50:
                    result.errors.append((asset.relative_path, error))
                queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                                  ref_id=asset.id, state="FAILED", error=error)
                continue

            preview_repo.record(
                asset.id,
                thumb=(previews.thumb_path, previews.thumb_width, previews.thumb_height, previews.thumb_bytes),
                preview=(
                    previews.preview_path,
                    previews.preview_width,
                    previews.preview_height,
                    previews.preview_bytes,
                ),
                generator_version=GENERATOR_VERSION,
            )
            if asset.status in ("PENDING", "UNREADABLE"):
                assets_repo.mark_ok(asset.id)
            result.done += 1
            result.bytes_processed += previews.thumb_bytes + previews.preview_bytes
            queue.upsert_item(job_id, ref_type="asset", ref_key=asset.relative_path,
                              ref_id=asset.id, state="DONE")
            if index % 25 == 0 or index == total:
                queue.update_progress(job_id, done=index, note=f"预览 {index}/{total}")
                progress(index, total, f"预览 {index}/{total}")

    result.seconds = time.perf_counter() - started
    return result


def run_fused_hash_previews(
    db: Database,
    library_id: int,
    root: Path,
    data_root: Path,
    job_id: int,
    *,
    queue: JobQueue,
    workers: int = 2,
    limit: int | None = None,
    progress: ProgressFn = _noop_progress,
    stop: StopFn = _never_stop,
) -> StageResult:
    """**一次读盘**同时完成：内容哈希 + 缩略图 + 预览。

    动机：哈希阶段与预览阶段都要读完整原片，分开跑就是**把 270 GB 读两遍**。
    本函数用 `read_bytes()` 读一次，然后
      * 用 BLAKE3 对内存数据算哈希；
      * 用 `rawpy.imread(BytesIO(data))` / `PIL.Image.open(BytesIO(data))` 从**同一份内存**解码。

    诚实的限制：Windows 的文件缓存会让"读几次"的基准测试极不可靠
    （实测同一代码路径两次测到 1525 与 1687 MB/s，而这块 USB 盘的冷读上限只有 ~60 MB/s）。
    因此本函数的存在意义是"在真实库上跑一遍并对比实际吞吐"，而不是凭微基准下结论。
    """
    started = time.perf_counter()
    result = StageResult()
    assets_repo = AssetRepo(db)
    preview_repo = PreviewRepo(db)
    #: 哈希成功但预览失败的数量。必须单独统计：把这类算进"完成"会掩盖预览失败，
    #: 算进"失败"又会让人以为哈希也没做（真实踩过的报告口径问题）。
    partial = 0

    # 只处理"既没哈希、也没预览"的文件，保证是真正的单次读盘
    targets = [
        asset
        for asset in assets_repo.needing_previews(library_id, limit=limit or 100_000)
        if not asset.content_hash
    ]
    total = len(targets)
    queue.update_progress(
        job_id, done=0, total=total, note=f"合并阶段：一次读盘完成哈希与预览（{total} 个）"
    )
    progress(0, total, "一次读盘：哈希 + 预览")

    def process(asset) -> tuple[Any, Any, str | None, str | None]:
        if stop():
            return asset, None, None, "stopped"
        path = asset_absolute_path(root, asset.relative_path)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            return asset, None, None, f"{type(exc).__name__}: {exc}"

        import blake3

        digest = blake3.blake3(payload, max_threads=blake3.blake3.AUTO).hexdigest()

        try:
            previews = generate_previews_from_bytes(
                asset.id, payload, data_root, file_type=asset.file_type, force=True
            )
            # 校验取到的是**缩略图**记录：`needing_previews` 用
            # `t.id IS NULL OR p.id IS NULL` 推断"缺预览"，万一遇到只有 preview、
            # 没有 thumb 的畸形记录，generate_previews_from_bytes 会走"复用"分支
            # 并静默返回 preview 的路径 —— 那会写出一条错误记录且毫无收益。
            # 这里显式拦下来，宁可报错也不要静默写坏数据。
            thumb_row = preview_repo.get(asset.id, "thumb")
            if thumb_row is not None and str(thumb_row["path"]) != previews.thumb_path:
                raise MediaError(
                    f"预览记录异常：thumb 记录={thumb_row['path']} 与本次生成={previews.thumb_path}"
                    " 不一致，已跳过（可运行 `photoos repair --apply` 后重跑）"
                )
        except MediaError as exc:
            # 预览失败但哈希已算出：仍然把哈希写回去，避免下次重复读
            return asset, None, digest, str(exc)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不得中断整批
            return asset, None, digest, f"{type(exc).__name__}: {exc}"
        return asset, previews, digest, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for index, (asset, previews, digest, error) in enumerate(pool.map(process, targets), start=1):
            if error == "stopped":
                queue.update_progress(job_id, note=f"已暂停/取消于 {index - 1}/{total}")
                break

            if digest:
                assets_repo.set_hash(asset.id, digest, HASH_ALGO)

            if previews is None:
                result.failed += 1
                if error and len(result.errors) < 50:
                    result.errors.append((asset.relative_path, error))
                queue.upsert_item(
                    job_id, ref_type="asset", ref_key=asset.relative_path,
                    ref_id=asset.id, state="FAILED", error=error or "未知错误",
                )
                if digest:
                    partial += 1  # 哈希这一半成功，但不计为"完成"
                continue

            preview_repo.record(
                asset.id,
                thumb=(
                    previews.thumb_path, previews.thumb_width, previews.thumb_height,
                    previews.thumb_bytes,
                ),
                preview=(
                    previews.preview_path, previews.preview_width, previews.preview_height,
                    previews.preview_bytes,
                ),
                generator_version=GENERATOR_VERSION,
            )
            if asset.status in ("PENDING", "UNREADABLE"):
                assets_repo.mark_ok(asset.id)
            result.done += 1
            if asset.size:
                result.bytes_processed += int(asset.size)
            queue.upsert_item(
                job_id, ref_type="asset", ref_key=asset.relative_path, ref_id=asset.id, state="DONE"
            )
            if index % 25 == 0 or index == total:
                queue.update_progress(job_id, done=index, note=f"合并阶段 {index}/{total}")
                progress(index, total, f"合并阶段 {index}/{total}")

    result.seconds = time.perf_counter() - started
    if partial:
        result.errors.append(
            ("（汇总）", f"其中 {partial} 个文件哈希成功但预览失败 —— 这些文件不会重复读盘，"
                        f"可单独重跑 `photoos preview` 补齐预览")
        )
    return result


# --------------------------------------------------------------------------- 去重报告
@dataclass
class DuplicateReport:
    groups: int
    redundant_copies: int
    reclaimable_bytes: int
    samples: list[tuple[str, list[str]]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"重复组 {self.groups}，多余副本 {self.redundant_copies}，"
            f"可回收 {self.reclaimable_bytes / 1e9:.2f} GB"
        )


def build_duplicate_report(db: Database, library_id: int, *, sample_limit: int = 5) -> DuplicateReport:
    """基于内容哈希生成重复报告。

    **只报告，不动作**：PhotoOS 不会自动隔离或删除任何文件（PRD §11.1 / §14.7）。
    既有 PowerShell 工具曾把副本移入隔离区；本系统不复刻该行为，只提供清单供用户决策。
    """
    assets = AssetRepo(db)
    groups = assets.duplicate_groups(library_id)
    redundant = 0
    reclaimable = 0
    samples: list[tuple[str, list[str]]] = []
    for digest, members in groups:
        redundant += len(members) - 1
        # 保守估计：冗余副本按"除第一个之外"的体积算
        sizes = sorted((int(m.size or 0) for m in members), reverse=True)
        reclaimable += sum(sizes[1:])
        if len(samples) < sample_limit:
            samples.append((digest, [m.relative_path for m in members]))
    return DuplicateReport(
        groups=len(groups),
        redundant_copies=redundant,
        reclaimable_bytes=reclaimable,
        samples=samples,
    )


def library_stats(db: Database, library_id: int) -> dict[str, object]:
    """按 Library 汇总统计，供 CLI/UI 与断点报告使用。**所有计数都限定在该 Library 内。**"""
    assets = AssetRepo(db)
    metadata = MetadataRepo(db)
    previews = PreviewRepo(db)
    with_time, with_meta = metadata.capture_time_coverage(library_id)
    return {
        "assets": assets.count(library_id),
        "assets_bytes": assets.total_bytes(library_id),
        "by_status": assets.status_histogram(library_id),
        "by_type": assets.type_histogram(library_id),
        "hashed": int(
            db.scalar(
                "SELECT COUNT(*) FROM asset WHERE library_id = ? AND content_hash IS NOT NULL"
                " AND content_hash != ''",
                (library_id,),
                default=0,
            )
        ),
        "metadata_rows": with_meta,
        "metadata_with_capture_time": with_time,
        "time_quality": metadata.time_quality(library_id),
        "previews": previews.count(library_id=library_id),
        "previews_bytes": previews.total_bytes(library_id),
        "preview_generator_versions": previews.generator_versions(library_id),
    }


__all__ = [
    "HASH_ALGO",
    "DuplicateReport",
    "StageResult",
    "build_duplicate_report",
    "hash_file",
    "library_stats",
    "run_discovery",
    "run_hash",
    "run_metadata",
    "run_previews",
]
