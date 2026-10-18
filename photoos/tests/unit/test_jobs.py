"""任务系统与流水线测试。

覆盖 PRD §9.1 / §11.1 / §11.2 的硬要求：
  * 任务状态可见（PENDING/PROCESSING/COMPLETED/FAILED/RETRY/CANCELLED/PAUSED）+ 进度 + 重试次数
  * 支持暂停/恢复/取消、失败重试、中断后继续
  * 单文件失败不阻塞整批
  * 重复扫描幂等；内容重复只报告不删除
  * 处理原片绝不改动原片
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from photoos.config import Settings
from photoos.db import Database, apply_migrations
from photoos.errors import JobError, JobNotCancellable, JobNotFound
from photoos.jobs import JobQueue, JobRunner
from photoos.jobs.queue import JOB_TYPES
from photoos.jobs.pipelines import (
    HASH_ALGO,
    build_duplicate_report,
    hash_file,
    library_stats,
    run_discovery,
    run_hash,
    run_metadata,
    run_previews,
)
from photoos.library.store import LibraryStore
from photoos.repos import AssetRepo, MetadataRepo, PreviewRepo


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def ctx(tmp_path: Path) -> tuple[Database, Settings, int, Path]:
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    library_root = tmp_path / "照片"
    (library_root / "杭州市" / "2025-07-19_1341-1413").mkdir(parents=True)
    (library_root / "杭州市" / "2025-10-24_2349_至_10-25_1807").mkdir(parents=True)
    (library_root / "未知地点" / "2020-04-28_2234-2245").mkdir(parents=True)

    for index in range(1, 8):
        _photo(library_root / "杭州市" / "2025-07-19_1341-1413" / f"DSC_{1270 + index}.JPG", index)
    _photo(library_root / "杭州市" / "2025-10-24_2349_至_10-25_1807" / "DSC_4051.JPG", 20)
    _photo(library_root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG", 30)
    (library_root / "杭州市" / "library-index.csv").write_text("a,b\n", encoding="utf-8")

    library = LibraryStore(db).create(name="测试库", root=library_root)
    return db, settings, library.id, library_root


def _photo(path: Path, seed: int, size: tuple[int, int] = (640, 480)) -> Path:
    """写一张带 EXIF（含时区）的真实 JPEG。"""
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", size, (seed * 7 % 256, seed * 13 % 256, seed * 29 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = f"2025:07:19 13:{40 + seed % 20:02d}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    sub[0x829D] = IFDRational(56, 10)
    sub[0x920A] = IFDRational(700, 10)
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return out


# ===================================================================== 任务队列
def test_queue_create_and_get(ctx) -> None:
    db, _settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id, progress_total=10)
    assert job.id > 0
    assert job.status == "PENDING"
    assert job.is_active and not job.is_terminal
    assert job.scope["library_id"] == library_id
    assert queue.get(job.id).type == "scan"


def test_queue_rejects_unknown_type(ctx) -> None:
    db, _s, library_id, _r = ctx
    with pytest.raises(JobError):
        JobQueue(db).create("teleport", library_id=library_id)


def test_queue_get_missing(ctx) -> None:
    db, _s, _l, _r = ctx
    with pytest.raises(JobNotFound):
        JobQueue(db).get(9999)


def test_queue_claim_order_and_pause_exclusion(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    first = queue.create("scan", library_id=library_id, priority=1)
    second = queue.create("hash", library_id=library_id, priority=2)

    claimed = queue.claim_next()
    assert claimed is not None and claimed.id == first.id
    assert claimed.status == "PROCESSING"
    assert claimed.started_at is not None

    queue.pause(first.id)
    claimed2 = queue.claim_next()
    assert claimed2 is not None and claimed2.id == second.id
    assert queue.claim_next() is None, "暂停的任务不得被领取"


def test_queue_progress_updates(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("preview", library_id=library_id, progress_total=100)
    queue.update_progress(job.id, done=25, note="四分之一")
    queue.update_progress(job.id, increment=25)
    updated = queue.get(job.id)
    assert updated.progress_done == 50
    assert updated.progress_note == "四分之一"
    assert updated.percent == 50.0
    queue.update_progress(job.id, total=200)
    assert queue.get(job.id).percent == 25.0


def test_queue_percent_without_total_is_zero(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    assert job.percent == 0.0


def test_queue_fail_then_retry_then_final_failure(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("hash", library_id=library_id, max_retries=2)

    after_first = queue.fail(job.id, "磁盘读取错误")
    assert after_first.status == "RETRY"
    assert after_first.retry_count == 1
    assert "磁盘读取错误" in (after_first.error or "")

    after_second = queue.fail(job.id, "还是失败")
    assert after_second.status == "RETRY" and after_second.retry_count == 2

    after_third = queue.fail(job.id, "第三次失败")
    assert after_third.status == "FAILED", "超过重试上限必须落到 FAILED"
    assert after_third.finished_at is not None


def test_queue_fail_without_retry_option(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("hash", library_id=library_id, max_retries=5)
    failed = queue.fail(job.id, "不可重试", retry=False)
    assert failed.status == "FAILED"


def test_queue_pause_resume_cancel(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)

    queue.pause(job.id)
    assert queue.is_paused(job.id)
    assert queue.should_stop(job.id)

    queue.resume(job.id)
    assert queue.get(job.id).status == "PENDING"
    assert not queue.should_stop(job.id)

    queue.cancel(job.id)
    assert queue.is_cancelled(job.id)
    assert queue.get(job.id).status == "CANCELLED"
    assert queue.get(job.id).finished_at is not None


def test_queue_cannot_cancel_terminal(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.complete(job.id)
    with pytest.raises(JobNotCancellable):
        queue.cancel(job.id)


def test_queue_resume_only_for_paused_or_failed(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    with pytest.raises(JobError):
        queue.resume(job.id)  # PENDING 无需恢复


def test_queue_complete_clears_error(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.fail(job.id, "临时错误", retry=False)
    queue.complete(job.id)
    done = queue.get(job.id)
    assert done.status == "COMPLETED" and done.error is None


def test_job_items_track_resume_points(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("metadata", library_id=library_id)

    queue.upsert_item(job.id, ref_type="asset", ref_key="a.jpg", ref_id=1, state="DONE")
    queue.upsert_item(job.id, ref_type="asset", ref_key="b.jpg", ref_id=2, state="FAILED", error="坏了")
    queue.upsert_item(job.id, ref_type="asset", ref_key="c.jpg", ref_id=3, state="PENDING")

    # 重复标记同一项必须幂等（不得产生重复行）
    queue.upsert_item(job.id, ref_type="asset", ref_key="a.jpg", ref_id=1, state="DONE")

    states = queue.item_states(job.id)
    assert states == {"DONE": 1, "FAILED": 1, "PENDING": 1}
    failures = queue.failed_items(job.id)
    assert len(failures) == 1 and failures[0]["ref_key"] == "b.jpg"


def test_recover_orphans(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next()
    assert queue.get(job.id).status == "PROCESSING"

    recovered = queue.recover_orphans(stale_after_seconds=0)
    assert recovered == 1
    revived = queue.get(job.id)
    assert revived.status == "PENDING"
    assert "自动恢复" in (revived.error or "")


def test_recover_orphans_keeps_fresh_heartbeat(ctx) -> None:
    """心跳仍新鲜的任务不能被回收（正常运行时不得误伤）。"""
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next()
    assert queue.recover_orphans(stale_after_seconds=3600) == 0
    assert queue.get(job.id).status == "PROCESSING"


def test_find_active(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("hash", library_id=library_id)
    assert queue.find_active("hash", library_id).id == job.id  # type: ignore[union-attr]
    assert queue.find_active("scan", library_id) is None
    queue.complete(job.id)
    assert queue.find_active("hash", library_id) is None


# ===================================================================== 执行者归属
def test_claim_next_respects_owner(ctx) -> None:
    """只有"无归属"或"归属自己"的任务才能被认领。

    回归背景（真实事故）：`photoos serve` 的后台执行器与命令行的 `photoos index`
    同时从同一张 job 表认领任务，于是同一个任务被两个进程并行执行 ——
    在只有 ~60 MB/s 的 USB 盘上互相抢带宽并重复读取同一批文件。
    """
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("fused", library_id=library_id)

    # 进程 A 认领
    claimed = queue.claim_next(owner="hostA:100")
    assert claimed is not None and claimed.id == job.id
    assert queue.claimed_by(job.id) == "hostA:100"

    # 进程 B 不得认领（同一个任务）
    assert queue.claim_next(owner="hostB:200") is None

    # 进程 A 自己可以继续认领（用于重试）
    queue.fail(job.id, "临时失败")
    again = queue.claim_next(owner="hostA:100")
    assert again is not None and again.id == job.id


def test_release_claim_allows_other_executor(ctx) -> None:
    """归还任务（释放归属 + 回到 PENDING）后，另一个执行者应当能接手。

    注意：状态才是"能否认领"的主闸门 —— PROCESSING 的任务任何执行者都不能认领；
    归属解决的是另一个问题：处于 PENDING/RETRY 的任务会被谁拿走。
    """
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("fused", library_id=library_id)
    queue.claim_next(owner="hostA:100")
    assert queue.claim_next(owner="hostB:200") is None, "PROCESSING 的任务谁都不能认领"

    # hostA 归还：释放归属并放回队列（模拟"我跑不了，让给别人"）
    queue.release_claim(job.id)
    assert queue.claimed_by(job.id) is None
    db.execute("UPDATE job SET status = 'PENDING' WHERE id = ?", (job.id,))

    taken = queue.claim_next(owner="hostB:200")
    assert taken is not None and taken.id == job.id
    assert queue.claimed_by(job.id) == "hostB:200"


def test_owned_pending_job_is_skipped_by_others(ctx) -> None:
    """归属的作用点：一个 PENDING 任务被 A 占着时，B 应跳过它去拿别的任务。"""
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    from_a = queue.create("fused", library_id=library_id, priority=1)
    other = queue.create("scan", library_id=library_id, priority=2)

    claimed_a = queue.claim_next(owner="hostA:100")
    assert claimed_a is not None and claimed_a.id == from_a.id

    # 把 A 的任务放回队列但**保留归属**（模拟 A 即将重试）
    db.execute("UPDATE job SET status = 'PENDING' WHERE id = ?", (from_a.id,))
    assert queue.claimed_by(from_a.id) == "hostA:100"

    # B 不得拿走 A 的任务，应改拿自己的
    claimed_b = queue.claim_next(owner="hostB:200")
    assert claimed_b is not None and claimed_b.id == other.id
    # A 则可以拿回自己的
    db.execute("UPDATE job SET status = 'PENDING' WHERE id = ?", (from_a.id,))
    again = queue.claim_next(owner="hostA:100")
    assert again is not None and again.id == from_a.id


def test_recover_orphans_releases_other_owners_only(ctx) -> None:
    """孤儿回收只释放**其他**执行者遗留的任务，不动自己正在跑的。"""
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    mine = queue.create("fused", library_id=library_id)
    other = queue.create("scan", library_id=library_id)

    queue.claim_next(owner="hostA:100")  # 认领 mine
    claim_other = JobQueue(db).claim_next(owner="hostB:200")  # 另一个执行者认领 other
    assert claim_other is not None and claim_other.id == other.id

    # hostA 做回收：自己的不动，别人的（心跳过期）释放
    recovered = queue.recover_orphans(stale_after_seconds=0, owner="hostA:100")
    assert recovered == 1
    assert queue.get(mine.id).status == "PROCESSING", "自己的任务不得被回收"
    assert queue.get(other.id).status == "PENDING"
    assert queue.claimed_by(other.id) is None, "释放后归属必须清空"


def test_executor_id_is_host_and_pid() -> None:
    from photoos.jobs import executor_id

    value = executor_id()
    assert ":" in value
    host, _, pid = value.rpartition(":")
    assert host and pid.isdigit()
    assert executor_id() == value, "同一进程内必须稳定"


def test_runner_releases_claim_after_completion(ctx) -> None:
    """任务结束后必须释放归属，否则其他执行者永远抢不到。"""
    db, settings, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next(owner="hostA:100")
    assert queue.claimed_by(job.id) == "hostA:100"

    JobRunner(db, settings).run(queue.get(job.id))
    assert queue.claimed_by(job.id) is None


def test_runner_releases_claim_after_failure(ctx) -> None:
    db, settings, library_id, _r = ctx
    queue = JobQueue(db)
    # 用一个**必然失败**的任务来制造失败路径：rebind 缺少 scope.new_root。
    # （这里原来用 `migrate` 当"未实现的类型"，2026-10 四个类型都补上处理器之后
    #   它不再会失败了 —— 那种"靠某个功能缺失来构造失败"的写法本来也脆弱。）
    job = queue.create("rebind", library_id=library_id, scope={})
    queue.claim_next(owner="hostA:100")
    report = JobRunner(db, settings).run(queue.get(job.id))
    assert report.status in {"FAILED", "RETRY"}
    assert queue.claimed_by(job.id) is None, "失败后也必须释放归属"


# ===================================================================== 哈希
def test_hash_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    payload = b"photoos" * 100_000
    path.write_bytes(payload)
    assert hash_file(path) == hashlib.blake2b(payload).hexdigest()[:0] + hash_file(path)
    # 与已知值比对：确认用的是 BLAKE3 且是稳定实现
    import blake3

    assert hash_file(path) == blake3.blake3(payload).hexdigest()
    assert HASH_ALGO == "blake3"


def test_hash_file_empty(tmp_path: Path) -> None:
    import blake3

    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert hash_file(path) == blake3.blake3(b"").hexdigest()


def test_hash_file_detects_change(tmp_path: Path) -> None:
    path = tmp_path / "a.bin"
    path.write_bytes(b"one")
    first = hash_file(path)
    path.write_bytes(b"two")
    assert hash_file(path) != first


def test_hash_file_reads_large_file_in_chunks(tmp_path: Path) -> None:
    """必须分块读取（不能一次读进内存）：用 3 MiB 文件确认结果与整体哈希一致。"""
    import blake3

    path = tmp_path / "big.bin"
    payload = bytes(range(256)) * (3 * 4096)
    path.write_bytes(payload)
    assert hash_file(path) == blake3.blake3(payload).hexdigest()


# ===================================================================== 发现
def test_discovery_indexes_media_only(ctx) -> None:
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    result = run_discovery(db, library_id, root, job.id, queue=queue)

    assets = AssetRepo(db)
    assert assets.count(library_id) == 9, "应索引 9 张照片，且不收 CSV"
    assert result.failed == 0
    by_type = assets.type_histogram(library_id)
    assert by_type == {"jpeg": 9}
    for asset in assets.pending(library_id):
        assert asset.status == "PENDING"
        assert ":" not in asset.relative_path, "数据库内不得出现盘符"


def test_discovery_is_idempotent(ctx) -> None:
    """重复扫描不得产生重复 Asset（PRD 断点 #2）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    first = run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    second = run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    assert AssetRepo(db).count(library_id) == 9
    assert first.done == 9 and second.done == 0
    assert second.skipped == 9, "第二次扫描应全部命中未变"


def test_discovery_marks_missing_without_deleting_rows(ctx) -> None:
    """文件消失只标 MISSING，绝不删除索引行（删除记录 ≠ 删除原文件）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    victim = root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG"
    victim.unlink()

    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    assets = AssetRepo(db)
    assert assets.count(library_id) == 9, "行数不变"
    located = assets.by_relative(library_id, "未知地点\\2020-04-28_2234-2245\\DSC_0005.JPG")
    assert located is not None and located.status == "MISSING"


def test_discovery_revives_file_that_returns(ctx) -> None:
    """拔盘再插回（文件重新出现）必须复活为可用状态，而不是当成新文件。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    victim = root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG"
    payload = victim.read_bytes()
    victim.unlink()
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    victim.write_bytes(payload)
    import os

    os.utime(victim, ns=(victim.stat().st_atime_ns, victim.stat().st_mtime_ns))
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    located = AssetRepo(db).by_relative(library_id, "未知地点\\2020-04-28_2234-2245\\DSC_0005.JPG")
    assert located is not None and located.status != "MISSING"
    assert AssetRepo(db).count(library_id) == 9


def test_discovery_detects_changed_file_and_clears_hash(ctx) -> None:
    """文件内容变化 → 清空旧 hash，强制重算（否则去重与迁移校验会基于错误数据）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    target = root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1271.JPG"
    assets = AssetRepo(db)
    asset = assets.by_relative(library_id, "杭州市\\2025-07-19_1341-1413\\DSC_1271.JPG")
    assert asset is not None
    assets.set_hash(asset.id, "deadbeef", HASH_ALGO)

    _photo(target, seed=99, size=(800, 600))  # 内容与大小都变了
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    refreshed = assets.by_relative(library_id, asset.relative_path)
    assert refreshed is not None
    assert refreshed.content_hash is None, "内容变了必须清空旧 hash"
    assert refreshed.status == "PENDING"


def test_discovery_skips_bookkeeping_dirs(ctx) -> None:
    db, _s, library_id, root = ctx
    junk = root / "_归类清单" / "_重复文件"
    junk.mkdir(parents=True)
    _photo(junk / "DSC_9999.JPG", 40)

    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    assert AssetRepo(db).count(library_id) == 9, "既有流水线产物目录不得被索引"


def test_discovery_does_not_modify_photos(ctx) -> None:
    db, _s, library_id, root = ctx
    before = _snapshot(root)
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    assert _snapshot(root) == before


def test_discovery_respects_stop(ctx) -> None:
    """协作式停止：stop() 为真时应尽快收工而不是跑完全部。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue, stop=stop)
    assert calls["n"] > 2


# ===================================================================== 元数据
def test_metadata_pipeline_fills_rows(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    result = run_metadata(db, library_id, root, queue.create("metadata", library_id=library_id).id, queue=queue)

    assert result.done == 9 and result.failed == 0
    repo = MetadataRepo(db)
    assert repo.count(library_id) == 9
    with_time, total = repo.capture_time_coverage(library_id)
    assert (with_time, total) == (9, 9)
    assert repo.camera_histogram(library_id) == [("NIKON Z 5", 9)]
    for asset in AssetRepo(db).pending(library_id):
        assert asset.status == "OK"


def test_metadata_pipeline_is_incremental(ctx) -> None:
    """第二次运行不应重复处理（needing_metadata 只挑没有 metadata 行的）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_metadata(db, library_id, root, queue.create("metadata", library_id=library_id).id, queue=queue)
    second = run_metadata(db, library_id, root, queue.create("metadata", library_id=library_id).id, queue=queue)
    assert second.done == 0


def test_metadata_pipeline_survives_unreadable_file(ctx) -> None:
    """单文件解码失败不得中断整批（PRD §11.1）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    broken = root / "杭州市" / "2025-07-19_1341-1413" / "BROKEN.JPG"
    broken.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    job = queue.create("metadata", library_id=library_id)
    result = run_metadata(db, library_id, root, job.id, queue=queue)
    assert result.done == 10, "损坏文件也必须写入一行元数据（否则会被反复挑出来）"
    assert MetadataRepo(db).count(library_id) == 10


# ===================================================================== 预览
def test_previews_pipeline_generates_files(ctx) -> None:
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    result = run_previews(
        db, library_id, root, settings.data_root,
        queue.create("preview", library_id=library_id).id, queue=queue,
    )
    assert result.done == 9 and result.failed == 0

    repo = PreviewRepo(db)
    assert repo.count("thumb") == 9 and repo.count("preview") == 9
    assert repo.generator_versions() == {"p1.0": 18}
    row = repo.get(AssetRepo(db).pending(library_id)[0].id, "thumb")
    assert row is not None
    assert (settings.data_root / str(row["path"])).exists()
    assert ":" not in str(row["path"])


def test_previews_pipeline_is_incremental_and_skips_existing(ctx) -> None:
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_previews(db, library_id, root, settings.data_root,
                 queue.create("preview", library_id=library_id).id, queue=queue)
    second = run_previews(db, library_id, root, settings.data_root,
                          queue.create("preview", library_id=library_id).id, queue=queue)
    assert second.done == 0, "已生成的不得重复生成（PRD §11.2）"


def test_previews_pipeline_does_not_touch_originals(ctx) -> None:
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    before = _snapshot(root)
    run_previews(db, library_id, root, settings.data_root,
                 queue.create("preview", library_id=library_id).id, queue=queue)
    assert _snapshot(root) == before


# ===================================================================== 哈希阶段
def test_hash_pipeline_sets_hashes_and_duplicates(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    # 造一个真正的重复副本（内容完全相同）
    original = root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1271.JPG"
    copy = root / "杭州市" / "2025-10-24_2349_至_10-25_1807" / "DSC_4051__2.JPG"
    copy.write_bytes(original.read_bytes())
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    result = run_hash(db, library_id, root, queue.create("hash", library_id=library_id).id, queue=queue)
    assert result.done == 10 and result.failed == 0

    repo = AssetRepo(db)
    assert repo.unhashed(library_id) == []
    hashes = {a.relative_path: a.content_hash for a in repo.pending(library_id)}
    assert hashes["杭州市\\2025-07-19_1341-1413\\DSC_1271.JPG"] == hashes[
        "杭州市\\2025-10-24_2349_至_10-25_1807\\DSC_4051__2.JPG"
    ]

    report = build_duplicate_report(db, library_id)
    assert report.groups == 1
    assert report.redundant_copies == 1
    assert report.reclaimable_bytes > 0


def test_duplicate_report_does_not_delete_anything(ctx) -> None:
    """去重报告必须是纯只读的（PRD §11.1：不得自动删除原文件）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    original = root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1271.JPG"
    copy = root / "杭州市" / "2025-10-24_2349_至_10-25_1807" / "DSC_4051__2.JPG"
    copy.write_bytes(original.read_bytes())
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_hash(db, library_id, root, queue.create("hash", library_id=library_id).id, queue=queue)

    before = _snapshot(root)
    report = build_duplicate_report(db, library_id)
    assert report.groups == 1
    assert _snapshot(root) == before, "去重报告绝不能改动任何文件"


def test_hash_pipeline_marks_missing_file(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    victim = root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG"
    victim.unlink()

    result = run_hash(db, library_id, root, queue.create("hash", library_id=library_id).id, queue=queue)
    assert result.failed == 1 and result.done == 8
    located = AssetRepo(db).by_relative(library_id, "未知地点\\2020-04-28_2234-2245\\DSC_0005.JPG")
    assert located is not None and located.status == "MISSING"


def test_hash_pipeline_records_failed_items_for_resume(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    victim = root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG"
    victim.unlink()

    job = queue.create("hash", library_id=library_id)
    run_hash(db, library_id, root, job.id, queue=queue)
    states = queue.item_states(job.id)
    assert states.get("FAILED") == 1
    assert states.get("DONE") == 8
    assert len(queue.failed_items(job.id)) == 1


def test_hash_pipeline_limit(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    result = run_hash(db, library_id, root, queue.create("hash", library_id=library_id).id, queue=queue, limit=3)
    assert result.done == 3
    assert len(AssetRepo(db).unhashed(library_id)) == 6


# ===================================================================== 范围限定
def test_scoped_discovery_only_indexes_subtree(ctx) -> None:
    """限定子树时只索引该子树（断点测试的分层策略依赖这个语义）。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    prefix = "杭州市\\2025-07-19_1341-1413"
    run_discovery(
        db, library_id, root, queue.create("scan", library_id=library_id).id,
        queue=queue, scope_prefix=prefix,
    )
    assets = AssetRepo(db)
    assert assets.count(library_id) == 7  # 该时段 7 张
    for asset in assets.pending(library_id):
        assert asset.relative_path.startswith(prefix)


def test_scoped_discovery_does_not_mark_outside_files_missing(ctx) -> None:
    """关键回归：限定子树时**绝不能**把子树之外的文件标成 MISSING。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    assert AssetRepo(db).count(library_id) == 9

    run_discovery(
        db, library_id, root, queue.create("scan", library_id=library_id).id,
        queue=queue, scope_prefix="杭州市\\2025-07-19_1341-1413",
    )

    assets = AssetRepo(db)
    assert assets.count(library_id) == 9, "行数不变"
    histogram = assets.status_histogram(library_id)
    assert "MISSING" not in histogram, f"子树之外的资产被误标缺失：{histogram}"


def test_scoped_pipelines_only_touch_subtree(ctx) -> None:
    """元数据/预览/哈希三个阶段都要遵守 scope。"""
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    prefix = "杭州市\\2025-10-24_2349_至_10-25_1807"
    run_metadata(
        db, library_id, root, queue.create("metadata", library_id=library_id).id,
        queue=queue, scope_prefix=prefix,
    )
    run_previews(
        db, library_id, root, settings.data_root,
        queue.create("preview", library_id=library_id).id, queue=queue, scope_prefix=prefix,
    )
    run_hash(
        db, library_id, root, queue.create("hash", library_id=library_id).id,
        queue=queue, scope_prefix=prefix,
    )

    scoped = AssetRepo(db).pending(library_id, scope_prefix=prefix)
    assert scoped, "范围内应有资产"
    for asset in scoped:
        assert asset.content_hash is not None, f"{asset.relative_path} 未被哈希"
    # 范围外的资产不应被处理
    outside = [
        asset
        for asset in AssetRepo(db).pending(library_id)
        if not asset.relative_path.startswith(prefix)
    ]
    assert outside, "应有范围外的资产"
    assert all(asset.content_hash is None for asset in outside), "范围外资产不应被哈希"
    assert MetadataRepo(db).count(library_id) == len(scoped)


def test_scope_prefix_with_wildcards_is_escaped(ctx) -> None:
    """LIKE 元字符必须被转义，否则范围会被意外放大。"""
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    assets = AssetRepo(db)
    # "%" 作为前缀不应匹配任何真实路径（若未转义会匹配全部）
    assert assets.pending(library_id, scope_prefix="%") == []
    # 下划线是单字符通配符；未转义时 "杭州市\_2025" 会匹配到真实前缀
    assert assets.pending(library_id, scope_prefix="杭州市_2025") == []
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    stats = library_stats(db, library_id)
    assert stats["assets"] == 9
    assert stats["assets_bytes"] > 0
    assert stats["by_type"] == {"jpeg": 9}
    assert stats["by_status"] == {"PENDING": 9}
    assert stats["previews"] == 0


# ===================================================================== 统计
def test_library_stats(ctx) -> None:
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    stats = library_stats(db, library_id)
    assert stats["assets"] == 9
    assert stats["assets_bytes"] > 0
    assert stats["by_type"] == {"jpeg": 9}
    assert stats["by_status"] == {"PENDING": 9}
    assert stats["previews"] == 0
    assert stats["hashed"] == 0


def test_library_stats_is_scoped_per_library(ctx) -> None:
    """统计必须按 Library 隔离，不能把别的库的预览数算进来。"""
    db, _s, library_id, root = ctx
    other_root = root.parent / "另一个库"
    other_root.mkdir()
    _photo(other_root / "other.jpg", 99)
    other = LibraryStore(db).create(name="另一个库", root=other_root)

    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_discovery(db, other.id, other_root, queue.create("scan", library_id=other.id).id, queue=queue)
    run_metadata(db, other.id, other_root, queue.create("metadata", library_id=other.id).id, queue=queue)

    assert library_stats(db, other.id)["metadata_rows"] == 1
    assert library_stats(db, library_id)["metadata_rows"] == 0, "不得串库"


def test_time_quality_metric(ctx) -> None:
    """拍摄时间质量必须区分「本地时间」与「仅 UTC（时区未知）」。

    回归背景：视频只有容器给的精确 UTC 且时区未知；若覆盖率只统计 `capture_at`，
    视频库会永远显示 0% 覆盖（真实踩过的统计口径错误）。
    """
    db, _s, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_metadata(
        db, library_id, root, queue.create("metadata", library_id=library_id).id, queue=queue
    )

    repo = MetadataRepo(db)
    quality = repo.time_quality(library_id)
    assert quality["total"] == 9
    assert quality["local_time"] == 9, "测试样本 JPEG 都带 OffsetTimeOriginal"
    assert quality["tz_known"] == 9
    assert quality["utc_only"] == 0
    assert quality["no_time"] == 0
    assert repo.capture_time_coverage(library_id) == (9, 9)

    # 模拟"只有 UTC 的视频"：清掉本地时间，只留 UTC
    db.execute("UPDATE metadata SET capture_at = NULL, tz_offset = NULL, tz_source = 'unknown'")
    db.execute("UPDATE metadata SET capture_at_utc = '2026-01-01 00:00:00'")

    quality = repo.time_quality(library_id)
    assert quality["local_time"] == 0
    assert quality["utc_only"] == 9
    assert repo.capture_time_coverage(library_id) == (9, 9), "仅 UTC 也必须算作知道拍摄时间"


# ===================================================================== 执行器
def test_runner_executes_scan_and_reports(ctx) -> None:
    db, settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)

    assert report.status == "COMPLETED"
    assert "发现" in report.stages
    assert report.stats["assets"] == 9
    assert queue.get(job.id).status == "COMPLETED"
    assert any("库内 Asset" in line for line in report.lines())


def test_runner_full_index_end_to_end(ctx) -> None:
    db, settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("full_index", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)

    assert report.status == "COMPLETED", report.error
    for stage in ("发现", "元数据", "预览", "融合（一次读盘：哈希+预览）"):
        assert stage in report.stages, f"缺少阶段 {stage}"

    assets = AssetRepo(db)
    assert assets.count(library_id) == 9
    assert assets.unhashed(library_id) == [], "融合阶段应顺带把哈希算完"
    assert MetadataRepo(db).count(library_id) == 9
    assert PreviewRepo(db).count("thumb") == 9
    assert report.duplicates is not None


def test_full_index_hashes_everything_in_one_pass(ctx) -> None:
    """完整索引跑完后，不应再有任何"缺哈希或缺预览"的文件。

    这是融合阶段的核心保证：把 270 GB 读两遍变成读一遍，同时结果不缺项。
    """
    db, settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("full_index", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED", report.error

    assert AssetRepo(db).unhashed(library_id) == []
    assert AssetRepo(db).needing_previews(library_id) == []
    assert AssetRepo(db).needing_metadata(library_id) == []


def test_fused_stage_completes_both_outputs(ctx) -> None:
    """单独跑融合阶段：哈希与预览都要产出，且不重复读已完成的文件。"""
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED", report.error

    assert AssetRepo(db).unhashed(library_id) == []
    assert PreviewRepo(db).count("thumb", library_id=library_id) == 9

    # 再跑一次：已经没有"既没哈希也没预览"的文件，应当什么都不做
    second = queue.create("fused", library_id=library_id)
    queue.claim_next()
    again = JobRunner(db, settings).run(second)
    assert again.status == "COMPLETED"
    assert "完成 0" in again.stages["融合（一次读盘：哈希+预览）"]


def test_fused_skips_files_that_already_have_both(ctx) -> None:
    """先做过哈希与预览的文件，不得被融合阶段重复读。"""
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    run_hash(db, library_id, root, queue.create("hash", library_id=library_id).id, queue=queue)
    run_previews(
        db, library_id, root, settings.data_root,
        queue.create("preview", library_id=library_id).id, queue=queue,
    )

    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED"
    assert "完成 0" in report.stages["融合（一次读盘：哈希+预览）"], "不应重复读已完成的文件"


def test_fused_writes_hash_even_when_preview_fails(ctx) -> None:
    """哈希成功但预览失败（文件损坏）时，哈希仍应落库，避免下次重复读盘。

    这是真实会发生的形态：字节读得到，但解码器打不开。
    """
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    broken = root / "杭州市" / "2025-07-19_1341-1413" / "BROKEN.JPG"
    broken.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED"

    assets = AssetRepo(db)
    broken_asset = assets.by_relative(library_id, "杭州市\\2025-07-19_1341-1413\\BROKEN.JPG")
    assert broken_asset is not None
    assert broken_asset.content_hash, "损坏文件的哈希仍应写入"

    detail = report.stages["融合（一次读盘：哈希+预览）"]
    assert "失败 1" in detail, detail
    assert "完成 9" in detail, detail


def test_fused_database_state_is_complete(ctx) -> None:
    """融合跑完后，哈希与预览两个方向都不能有缺项，且预览文件真的落盘。"""
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    JobRunner(db, settings).run(job)

    assert AssetRepo(db).unhashed(library_id) == []
    assert AssetRepo(db).needing_previews(library_id) == []
    assert PreviewRepo(db).count("thumb", library_id=library_id) == 9
    assert PreviewRepo(db).count("preview", library_id=library_id) == 9
    row = PreviewRepo(db).sample_with_preview("thumb", library_id=library_id)
    assert row is not None
    assert (settings.data_root / str(row["path"])).is_file()


def test_fused_does_not_modify_originals(ctx) -> None:
    db, settings, library_id, root = ctx
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    before = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()
        )
        for path in root.rglob("*")
        if path.is_file()
    }
    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    JobRunner(db, settings).run(job)
    after = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()
        )
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_fused_video_hash_written_then_preview_falls_back(ctx) -> None:
    """视频无法从内存解码：融合阶段拿不到预览，但**哈希必须写入**，
    随后由兜底的 preview 阶段按路径生成封面。"""
    db, settings, library_id, root = ctx
    video = root / "未知地点" / "2020-04-28_2234-2245" / "clip.MOV"
    video.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64)

    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    job = queue.create("fused", library_id=library_id)
    queue.claim_next()
    report = JobRunner(db, settings).run(job)
    assert report.status == "COMPLETED"

    asset = AssetRepo(db).by_relative(library_id, "未知地点\\2020-04-28_2234-2245\\clip.MOV")
    assert asset is not None
    assert asset.content_hash, "视频的哈希必须在融合阶段写入（字节读得到）"

    # 兜底阶段不应崩溃（真实视频会走 ffmpeg；这个伪文件会失败但必须被记录）
    fallback = queue.create("preview", library_id=library_id)
    queue.claim_next()
    outcome = JobRunner(db, settings).run(fallback)
    assert outcome.status == "COMPLETED"


def test_runner_reports_failure_for_unimplemented_type(ctx) -> None:
    """**未登记**的任务类型必须在入队时就被拒绝，不能"收下再失败"。

    这里原来建的是 `migrate`，因为当时它没有处理器 —— 2026-10 补齐之后
    这个前提消失了，于是测试改成守真正的不变量：**队列只接受 JOB_TYPES 里的类型**，
    而 JOB_TYPES 里的每一个都有处理器（见 `test_every_job_type_has_a_handler`）。

    两者合起来保证：任何能入队的任务，都一定跑得起来。
    """
    from photoos.errors import JobError

    db, settings, library_id, _r = ctx
    queue = JobQueue(db)
    with pytest.raises(JobError):
        queue.create("definitely_not_a_real_type", library_id=library_id)

    # 反向确认：每个登记过的类型都能真的建出任务来（不只是字符串对得上）
    for job_type in JOB_TYPES:
        record = queue.create(job_type, library_id=library_id)
        assert record.type == job_type
        assert record.status == "PENDING"


def test_runner_tolerates_cancel_midway(ctx) -> None:
    """任务执行中被取消 → 报告为 CANCELLED，且已完成部分保留（可续跑）。"""
    db, settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("full_index", library_id=library_id)
    queue.claim_next()

    runner = JobRunner(db, settings)
    original = runner._make_progress
    cancelled = {"done": False}

    def progress_then_cancel(job_id, on_progress):
        inner = original(job_id, on_progress)

        def wrapped(done, total, note):
            inner(done, total, note)
            if not cancelled["done"]:
                # 第一次进度回调即取消：验证协作式停止真的会在检查点生效
                queue.cancel(job_id)
                cancelled["done"] = True

        return wrapped

    runner._make_progress = progress_then_cancel  # type: ignore[method-assign]
    report = runner.run(job)

    assert cancelled["done"], "进度回调未被调用，测试前提不成立"
    assert report.status == "CANCELLED"
    assert queue.get(job.id).status == "CANCELLED"
    # 取消不是失败：不得把任务标成 FAILED
    assert queue.get(job.id).error is None


def test_runner_tolerates_pause_midway(ctx) -> None:
    """暂停 → 任务保持 PAUSED（可恢复），且不会被执行器改成 COMPLETED。"""
    db, settings, library_id, _root = ctx
    queue = JobQueue(db)
    job = queue.create("full_index", library_id=library_id)
    queue.claim_next()

    runner = JobRunner(db, settings)
    original = runner._make_progress
    paused = {"done": False}

    def progress_then_pause(job_id, on_progress):
        inner = original(job_id, on_progress)

        def wrapped(done, total, note):
            inner(done, total, note)
            if not paused["done"]:
                queue.pause(job_id)
                paused["done"] = True

        return wrapped

    runner._make_progress = progress_then_pause  # type: ignore[method-assign]
    report = runner.run(job)

    assert report.status == "PAUSED"
    assert queue.get(job.id).status == "PAUSED"
    # 恢复后应能重新入队（已完成的明细不重做）
    queue.resume(job.id)
    assert queue.get(job.id).status == "PENDING"


# ============================================================ 任务类型的对账

def test_every_job_type_has_a_handler() -> None:
    """★ `JOB_TYPES` 里的每一种都必须有 `JobRunner` 处理器。

    这条曾经是坏的：`rebind` / `verify` / `backup` / `migrate` 四个类型登记在
    `JOB_TYPES` 里，但分发字典里没有它们 —— 于是 `POST /api/jobs {"type":"backup"}`
    会**建出一个永远无法执行的任务**，直到后台线程认领它才报
    "任务类型 X 尚未在本版本实现"。

    队列收下了、执行器却做不了，这是最难查的一类不一致：错误出现在
    与请求**不同的时间**、不同的地方。所以拿真来源逐项对账。
    """
    import inspect
    import re

    from photoos.jobs import runner as runner_mod
    from photoos.jobs.queue import JOB_TYPES

    src = inspect.getsource(runner_mod.JobRunner.run)
    handled = set(re.findall(r'"([a-z_]+)":\s*self\._run', src))

    missing = sorted(set(JOB_TYPES) - handled)
    assert not missing, (
        f"这些任务类型登记在 JOB_TYPES 里但没有处理器：{missing}。"
        "要么在 run() 的分发字典里接上，要么从 JOB_TYPES 里去掉。"
    )
    extra = sorted(handled - set(JOB_TYPES))
    assert not extra, f"分发字典里有 JOB_TYPES 未登记的类型：{extra}"


def test_migrate_import_is_refused_while_serving() -> None:
    """迁移包的**导入**不能在线执行 —— 它要替换整个数据根。

    服务正跑在这份数据上，自己持有的 SQLite 连接会让"覆盖成功"变成
    "半个库是新的一半是旧的"。所以它必须显式报错，并把用户引到停机后的 CLI。
    """
    import inspect

    from photoos.jobs import runner as runner_mod

    src = inspect.getsource(runner_mod.JobRunner._run_migrate)
    assert 'action == "import"' in src, "必须在 import 分支上显式拒绝"
    assert "raise PhotoOSError" in src
    assert "photoos migrate import" in src, "报错里要给出可照抄的替代做法"


# ============================================ 子进程必须用同一个数据根

def test_subprocess_handlers_pin_the_data_root() -> None:
    """★ 跑 CLI 子进程的两个处理器都必须传 `PHOTOOS_DATA_ROOT`。

    子进程**不会**继承父进程的 `settings`：`config.find_project_root()` 是从
    `photoos.toml` 往上找的，所以不带这个环境变量时子进程会算出**它自己**的数据根。
    在开发机上它恰好等于服务在用的那个，于是"看起来能跑"，实际却在**另一个库**上
    操作 —— 界面读不到任何变化，而且**不会报错**。

    实测撞到过：用隔离数据根验证 `regroup` 时，三步全部"完成"，
    但服务那个库的 shot/scene 仍然是 0。
    """
    import inspect

    from photoos.jobs import runner as runner_mod

    for name in ("_run_cli_action", "_run_regroup"):
        src = inspect.getsource(getattr(runner_mod.JobRunner, name))
        assert 'env["PHOTOOS_DATA_ROOT"]' in src, (
            f"{name} 没有把 PHOTOOS_DATA_ROOT 传给子进程 —— "
            "它可能在另一个数据根上操作"
        )


def test_regroup_steps_are_ordered_and_complete() -> None:
    """分组三步的顺序**不能换**，且必须齐全。

    `shots regroup --apply` 落库前要求 `session` 行已回填；`scenes rebuild`
    又建立在 Shot 之上。少一步或换顺序都会得到"命令成功但什么都没发生"。
    """
    from photoos.jobs.runner import JobRunner

    labels = [label for label, _ in JobRunner.REGROUP_STEPS]
    argv = [argv for _, argv in JobRunner.REGROUP_STEPS]
    assert labels == ["时段回填", "拍摄组重算", "场景重算"]
    assert argv[0][:2] == ("sessions", "backfill")
    assert argv[1][:2] == ("shots", "regroup")
    assert argv[2][:2] == ("scenes", "rebuild")
    for _, a in JobRunner.REGROUP_STEPS:
        assert "--apply" in a, "分组步骤必须落库（--apply），否则只是预演"


def test_regroup_is_idempotent(ctx) -> None:
    """`replace_scenes` 对同样的输入必须**不增长**。

    它按 `name` 复用既有行；循环结束后 `existing` 里剩下的就是"这次没再产生"的
    旧行 —— 不删掉它们，每重算一次就积一批**零 Shot 的孤儿 Scene**。
    实测（真实库 2026-10-16）：342 个 Scene 里 39 个是空的，就是这么攒出来的。
    界面按 Scene 列时段，于是同一个时段会重复出现好几次。
    """
    import sqlite3

    db, settings, library_id, _r = ctx
    from photoos.repos import ShotRepo

    repo = ShotRepo(db)
    out = db.execute(
        "INSERT INTO session (name, source, created_at, updated_at)"
        " VALUES ('S', 'auto', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')")
    session_id = int(out) if out else int(db.scalar("SELECT MAX(id) FROM session"))

    for i in range(6):
        db.execute(
            "INSERT INTO shot (captured_at, group_source, group_confidence, group_revision,"
            " created_at, updated_at)"
            " VALUES (?, 'auto', 0.9, 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')",
            (f"2026-01-01T10:{i:02d}:00",),
        )

    def scenes():
        ids = [int(r["id"]) for r in db.query(
            "SELECT id FROM shot WHERE scene_id IS NULL OR scene_id IN"
            " (SELECT id FROM scene WHERE session_id = ?) ORDER BY id", (session_id,))]
        return [
            {"name": f"X\\2026-01-01_1000-1000\\#{i}", "captured_from": "2026-01-01T10:00:00",
             "captured_to": "2026-01-01T10:00:59", "dir_hint": "X\\2026-01-01_1000-1000",
             "confidence": 0.9, "shot_ids": [ids[i]]}
            for i in range(len(ids))
        ]

    repo.replace_scenes(session_id=session_id, scenes=scenes())
    first = int(db.scalar("SELECT COUNT(*) FROM scene WHERE session_id = ?", (session_id,)))
    repo.replace_scenes(session_id=session_id, scenes=scenes())
    second = int(db.scalar("SELECT COUNT(*) FROM scene WHERE session_id = ?", (session_id,)))
    orphans = int(db.scalar(
        "SELECT COUNT(*) FROM scene s WHERE NOT EXISTS"
        " (SELECT 1 FROM shot sh WHERE sh.scene_id = s.id)"))

    assert first == second, f"重算两次 Scene 数量变了：{first} -> {second}"
    assert orphans == 0, f"留下了 {orphans} 个零 Shot 的孤儿 Scene 行"

def test_complete_does_not_override_a_cancelled_job(ctx) -> None:
    """★ 回归（Q-44）：取消是终态，`complete()` 不得把它覆盖成 COMPLETED。

    竞态现场：执行器在 `runner.py:179` 读一次"是否被取消"，取消请求若恰好落在
    那之后、`complete()` 之前，无守卫的 UPDATE 会把 `CANCELLED` 改成 `COMPLETED`
    —— 用户点了取消却看到"已完成"。
    故意破坏：去掉 `complete()` 里的 `status NOT IN (...)` 守卫，本测试立刻变红。
    """
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next()
    queue.cancel(job.id)
    assert queue.get(job.id).status == "CANCELLED"

    changed = queue.complete(job.id, note="迟到的完成")
    assert changed is False
    assert queue.get(job.id).status == "CANCELLED"


def test_complete_does_not_override_a_paused_job(ctx) -> None:
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    queue.claim_next()
    queue.pause(job.id)
    assert queue.complete(job.id) is False
    assert queue.get(job.id).status == "PAUSED"


def test_resume_clears_the_claim_owner(ctx) -> None:
    """★ 回归（Q-32）：`resume()` 必须清掉 `claimed_by`。

    否则会出现"死结"：`recover_orphans()` 只扫 `PROCESSING`，而 `claim_next()`
    只领"无归属或归属自己"的行 —— 一行 `PENDING` + 别人的 `claimed_by`
    **没有任何执行者会认领**，任务永久卡住。
    故意破坏：删掉 `resume()` 里的 `claimed_by = NULL`，本测试立刻变红。
    """
    db, _s, library_id, _r = ctx
    queue = JobQueue(db)
    job = queue.create("scan", library_id=library_id)
    claimed = queue.claim_next(owner="someone-else:1234")
    assert claimed is not None and claimed.claimed_by == "someone-else:1234"

    queue.pause(job.id)
    queue.resume(job.id)
    revived = queue.get(job.id)
    assert revived.claimed_by is None, "resume 后必须清空归属，否则任务永远没人领"

    # 别人现在应该能领到它
    again = queue.claim_next(owner="me:9999")
    assert again is not None and again.id == job.id