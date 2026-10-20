"""M0 回归测试：增量索引的四个缺陷（#12 / #13 / #14 / #15）。

这四个缺陷有共同的成因特征 —— **命令报告成功，但索引其实不完整或数据是旧的**。
因此每个用例的断言对象都是"用户可观察的最终状态"（数据库行、状态、剩余量），
而不是内部调用次数：只有这样才能在将来重构时仍然守住行为。

对应的实测现场（真实库 + 隔离演示库，见 PHASE1_DELIVERY.md §6.3）：

* #12 全新库磁盘上有 4 个文件，`index --wait` 却报「索引完成：Asset 0」
* #13 就地修改过的文件：元数据/缩略图永久陈旧，状态卡在 PENDING，`--wait` 永不收敛
* #14 正常删除一个文件，scan 输出「失败 1」
* #15 融合阶段的错误提示让用户跑 `photoos preview`，而该命令不存在
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from photoos.cli import build_parser
from photoos.config import Settings, get_settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.hashing import HASH_ALGO
from photoos.jobs import JobQueue, JobRunner
from photoos.jobs.pipelines import run_discovery, run_fused_hash_previews, run_metadata
from photoos.library.store import LibraryStore
from photoos.repos import AssetRepo, MetadataRepo, PreviewRepo

REL = "杭州市\\2026-03-01_0900-1100\\DSC_0001.JPG"
SESSION_DIR = ("杭州市", "2026-03-01_0900-1100")


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def ctx(tmp_path: Path) -> tuple[Database, Settings, int, Path]:
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    library_root = tmp_path / "照片"
    (library_root / SESSION_DIR[0] / SESSION_DIR[1]).mkdir(parents=True)
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
    sub[0x9003] = f"2026:03:01 09:{10 + seed % 20:02d}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    sub[0x829D] = IFDRational(56, 10)
    sub[0x920A] = IFDRational(700, 10)
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


def _index_once(db: Database, settings: Settings, library_id: int, root: Path) -> None:
    """跑一遍完整的发现 → 元数据 → 融合，等价于 `photoos index`。"""
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)
    run_metadata(
        db, library_id, root, queue.create("metadata", library_id=library_id).id, queue=queue
    )
    run_fused_hash_previews(
        db,
        library_id,
        root,
        settings.data_root,
        queue.create("fused", library_id=library_id).id,
        queue=queue,
    )


def _preview_digest(settings: Settings, previews: PreviewRepo, asset_id: int, kind: str) -> str:
    row = previews.get(asset_id, kind)
    assert row is not None, f"{kind} 预览记录不存在"
    return hashlib.sha256((settings.data_root / str(row["path"])).read_bytes()).hexdigest()


def _remaining(db: Database, library_id: int) -> dict[str, int]:
    assets = AssetRepo(db)
    return {
        "元数据": len(assets.needing_metadata(library_id)),
        "预览": len(assets.needing_previews(library_id)),
        "哈希": len(assets.unhashed(library_id)),
    }


# ============================================================ #13 就地修改
def test_changed_file_drops_stale_metadata_and_previews(ctx) -> None:
    """#13 根因：内容变了，但旧 metadata/preview 行还留着。

    这两行不清掉，后果不是"陈旧"这么轻：
      * `needing_metadata` 要求 metadata 行**不存在** → 旧 EXIF 永不刷新
      * 融合阶段只处理「既无哈希也无预览」者 → 旧预览还在 → 既不重算哈希也不重算预览
    """
    db, settings, library_id, root = ctx
    target = _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _index_once(db, settings, library_id, root)

    assets, metas, previews = AssetRepo(db), MetadataRepo(db), PreviewRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None
    assert metas.get(asset.id) is not None
    assert previews.get(asset.id, "thumb") is not None
    assert previews.get(asset.id, "preview") is not None

    _photo(target, seed=9, size=(800, 600))  # 内容与尺寸都变了
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    assert assets.get(asset.id).content_hash is None, "内容变了必须清空旧 hash"
    assert metas.get(asset.id) is None, "旧 EXIF 必须清除，否则永远不会被重新提取"
    assert previews.get(asset.id, "thumb") is None, "旧缩略图必须清除，否则融合阶段会跳过该文件"
    assert previews.get(asset.id, "preview") is None, "旧预览必须清除"


def test_modified_file_is_reindexed_and_index_converges(ctx) -> None:
    """#13 行为：修改后重跑，派生数据必须是**新的**，且剩余工作归零（不再永不收敛）。"""
    db, settings, library_id, root = ctx
    target = _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _index_once(db, settings, library_id, root)

    assets, metas, previews = AssetRepo(db), MetadataRepo(db), PreviewRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None
    old_hash = asset.content_hash
    old_thumb = _preview_digest(settings, previews, asset.id, "thumb")
    old_meta = metas.get(asset.id)
    assert old_meta is not None and int(old_meta["width"]) == 640

    _photo(target, seed=9, size=(800, 600))
    _index_once(db, settings, library_id, root)

    refreshed = assets.get(asset.id)
    assert refreshed.content_hash is not None and refreshed.content_hash != old_hash, "必须重算哈希"
    new_meta = metas.get(asset.id)
    assert new_meta is not None, "元数据必须重新提取"
    assert int(new_meta["width"]) == 800, "元数据必须是新图的尺寸，而不是旧图的 640"
    assert _preview_digest(settings, previews, asset.id, "thumb") != old_thumb, "缩略图必须是新图"
    assert refreshed.status == "OK", "派生数据齐备后状态必须是 OK"
    assert _remaining(db, library_id) == {"元数据": 0, "预览": 0, "哈希": 0}, "必须收敛"


# ============================================================ 状态收敛不变式
def test_reconcile_status_fixes_pending_asset_with_complete_derivations(ctx) -> None:
    """`OK` 必须是可推导的不变式，而不是某条代码路径的副作用。

    真实现场：由「哈希（兜底）」补上哈希的资产没有任何阶段去置 OK，
    于是数据齐全却永远停在 PENDING（状态撒谎）。
    """
    db, settings, library_id, root = ctx
    _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _index_once(db, settings, library_id, root)

    assets = AssetRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None
    assert asset.status == "OK"

    assets.set_status(asset.id, "PENDING", "人为制造：数据齐全但状态滞后")
    assert assets.reconcile_status(library_id) == (1, 0)
    assert assets.get(asset.id).status == "OK"
    assert assets.reconcile_status(library_id) == (0, 0), "幂等：没有可收敛的资产时不动任何行"


def test_reconcile_status_demotes_ok_asset_missing_previews(ctx) -> None:
    """反方向也要对齐：`OK` 但没有缩略图同样是撒谎。

    真实现场：元数据阶段会乐观地 `mark_ok`；若随后的预览阶段因原片损坏而失败，
    资产就带着 OK 状态却没有缩略图 —— 界面空白，而状态说一切正常。
    """
    db, settings, library_id, root = ctx
    _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _index_once(db, settings, library_id, root)

    assets, previews = AssetRepo(db), PreviewRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None and asset.status == "OK"

    previews.clear_for_assets([asset.id])  # 模拟预览阶段失败 / 预览记录丢失
    assert assets.reconcile_status(library_id) == (0, 1)
    assert assets.get(asset.id).status == "PENDING"
    reason = db.scalar("SELECT status_reason FROM asset WHERE id = ?", (asset.id,))
    assert reason, "降级必须留下原因，便于排查"


def test_reconcile_status_leaves_incomplete_asset_pending(ctx) -> None:
    """缺派生数据的资产**不得**被收敛为 OK —— 否则状态就失去意义。"""
    db, settings, library_id, root = ctx
    _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    queue = JobQueue(db)
    run_discovery(db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue)

    assets = AssetRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None
    assert assets.reconcile_status(library_id) == (0, 0)
    assert assets.get(asset.id).status == "PENDING"


# ============================================================ #14 缺失 ≠ 失败
def test_discovery_counts_missing_separately_from_failures(ctx) -> None:
    """删除一个文件是正常操作，不该报成「失败」。"""
    db, settings, library_id, root = ctx
    keep = _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    gone = _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0002.JPG", 2)
    _index_once(db, settings, library_id, root)  # 两张都完整索引 → OK

    gone.unlink()
    queue = JobQueue(db)
    result = run_discovery(
        db, library_id, root, queue.create("scan", library_id=library_id).id, queue=queue
    )

    assert result.missing == 1
    assert result.failed == 0, "缺失不是失败"
    assert "缺失 1" in result.summary()
    assert keep.exists()

    assets = AssetRepo(db)
    assert assets.by_relative(library_id, REL).status == "OK", "仍在的文件不受影响"
    missing = assets.by_relative(library_id, "杭州市\\2026-03-01_0900-1100\\DSC_0002.JPG")
    assert missing is not None and missing.status == "MISSING", "行必须保留，只改状态"


# ============================================================ #15 preview 命令
def test_preview_command_is_registered() -> None:
    """融合阶段的错误提示会指向 `photoos preview`，因此该入口必须真实存在。"""
    args = build_parser().parse_args(["preview"])
    assert args.func.__name__ == "cmd_preview"
    assert getattr(args, "regenerate", False) is False


def test_preview_stage_refills_missing_previews_without_rehashing(ctx) -> None:
    """`photoos preview` 的用途：只补预览，不重算哈希（哈希是 IO 密集的）。"""
    db, settings, library_id, root = ctx
    _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _index_once(db, settings, library_id, root)

    assets, previews = AssetRepo(db), PreviewRepo(db)
    asset = assets.by_relative(library_id, REL)
    assert asset is not None
    hash_before = asset.content_hash

    # 模拟"预览记录丢失"（清理缓存 / 迁移包未含预览 / 误删）
    previews.clear_for_assets([asset.id])
    assert _remaining(db, library_id)["预览"] == 1

    runner = JobRunner(db, settings)
    queue = JobQueue(db)
    job = queue.create("preview", library_id=library_id)
    report = runner.run(job)
    assert report.status == "COMPLETED"

    assert previews.get(asset.id, "thumb") is not None
    assert previews.get(asset.id, "preview") is not None
    assert assets.get(asset.id).content_hash == hash_before, "补预览不得改动哈希"
    assert _remaining(db, library_id) == {"元数据": 0, "预览": 0, "哈希": 0}


def test_preview_stage_honours_scope_prefix(ctx) -> None:
    """`--prefix` 必须真的收敛范围：只补指定子树，不碰其他文件。"""
    db, settings, library_id, root = ctx
    other_dir = root / "杭州市" / "2026-04-01_1000-1100"
    other_dir.mkdir(parents=True)
    _photo(root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _photo(other_dir / "DSC_0002.JPG", 2)
    _index_once(db, settings, library_id, root)

    assets, previews = AssetRepo(db), PreviewRepo(db)
    inside = assets.by_relative(library_id, REL)
    outside = assets.by_relative(library_id, "杭州市\\2026-04-01_1000-1100\\DSC_0002.JPG")
    assert inside is not None and outside is not None
    previews.clear_for_assets([inside.id, outside.id])

    runner = JobRunner(db, settings)
    queue = JobQueue(db)
    job = queue.create("preview", library_id=library_id, scope={"scope_prefix": "杭州市\\2026-03-01"})
    assert runner.run(job).status == "COMPLETED"

    assert previews.get(inside.id, "thumb") is not None, "范围内的必须补齐"
    assert previews.get(outside.id, "thumb") is None, "范围外的不得被处理"


# ============================================================ #12 index --wait
def test_index_wait_runs_discovery_so_new_files_are_indexed(tmp_path, monkeypatch) -> None:
    """#12：`index --wait` 必须包含发现阶段。

    实测现场：全新库磁盘上有 4 个文件，`index --wait` 报「索引完成：Asset 0」，
    因为 `--wait` 分支只在 metadata/fused 上循环，从未遍历文件系统。
    """
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    library_root = tmp_path / "照片"
    (library_root / SESSION_DIR[0] / SESSION_DIR[1]).mkdir(parents=True)
    _photo(library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    _photo(library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0002.JPG", 2)
    library = LibraryStore(db).create(name="测试库", root=library_root)

    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(data_root))
    get_settings.cache_clear()
    try:
        with use_config_path(None):
            args = build_parser().parse_args(
                ["index", "--library", str(library.id), "--wait", "--quiet"]
            )
            assert args.func(args) == 0, "索引应成功完成"
    finally:
        get_settings.cache_clear()

    check = Database(settings.db_path)
    assets = AssetRepo(check)
    assert assets.count(library.id) == 2, "磁盘上的两个文件必须都进入索引"
    for asset in (
        assets.by_relative(library.id, REL),
        assets.by_relative(library.id, "杭州市\\2026-03-01_0900-1100\\DSC_0002.JPG"),
    ):
        assert asset is not None
        assert asset.content_hash is not None
        assert asset.status == "OK"
    assert _remaining(check, library.id) == {"元数据": 0, "预览": 0, "哈希": 0}


def test_index_wait_picks_up_file_added_after_first_index(tmp_path, monkeypatch) -> None:
    """增量场景：库已索引完成，之后新增文件，`index --wait` 必须发现它。

    这是日常最常走的路径 —— 也正因如此，#12 的静默忽略最容易被误认为"已经索引好了"。
    """
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    library_root = tmp_path / "照片"
    (library_root / SESSION_DIR[0] / SESSION_DIR[1]).mkdir(parents=True)
    _photo(library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    library = LibraryStore(db).create(name="测试库", root=library_root)

    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(data_root))
    get_settings.cache_clear()
    try:
        with use_config_path(None):
            args = build_parser().parse_args(
                ["index", "--library", str(library.id), "--wait", "--quiet"]
            )
            assert args.func(args) == 0
            assert AssetRepo(db).count(library.id) == 1

            # 新增一张（模拟导入或手动拷贝进库）
            _photo(library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0002.JPG", 2)

            args = build_parser().parse_args(
                ["index", "--library", str(library.id), "--wait", "--quiet"]
            )
            assert args.func(args) == 0

        assert AssetRepo(db).count(library.id) == 2, "新增文件必须被 --wait 发现"
        new_asset = AssetRepo(db).by_relative(
            library.id, "杭州市\\2026-03-01_0900-1100\\DSC_0002.JPG"
        )
        assert new_asset is not None
        assert new_asset.content_hash is not None, "新增文件必须完成哈希"
        assert new_asset.status == "OK"
    finally:
        get_settings.cache_clear()


# ============================================================ 收敛容忍不可解码文件
def test_index_wait_tolerates_undecodable_file_and_still_succeeds(tmp_path, monkeypatch) -> None:
    """损坏文件不得让 `index --wait` 永远失败。

    真实现场：一个 1 字节的假 JPEG 会让 `fused` 每次都失败，`needing_previews` 永远返回它，
    于是命令跑满 5 轮后以退出码 1 结束 —— 用户被卡在一个不可能成功的命令上。
    正确行为：报告"这些文件无法生成派生数据"，其余照常完成，并以成功退出。
    """
    data_root = tmp_path / "data"
    settings = Settings(data_root=data_root)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    library_root = tmp_path / "照片"
    (library_root / SESSION_DIR[0] / SESSION_DIR[1]).mkdir(parents=True)
    _photo(library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0001.JPG", 1)
    broken = library_root / SESSION_DIR[0] / SESSION_DIR[1] / "DSC_0002.JPG"
    broken.write_bytes(b"X")  # 扩展名像 JPEG，内容不是
    library = LibraryStore(db).create(name="测试库", root=library_root)

    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(data_root))
    get_settings.cache_clear()
    try:
        with use_config_path(None):
            args = build_parser().parse_args(
                ["index", "--library", str(library.id), "--wait", "--quiet"]
            )
            assert args.func(args) == 0, "存在损坏文件时也应以成功退出"
    finally:
        get_settings.cache_clear()

    check = Database(settings.db_path)
    assets = AssetRepo(check)
    assert assets.count(library.id) == 2, "损坏文件仍必须被索引（Asset 是事实单位）"
    good = assets.by_relative(library.id, REL)
    assert good is not None and good.status == "OK", "正常文件必须完成"
    bad = assets.by_relative(library.id, "杭州市\\2026-03-01_0900-1100\\DSC_0002.JPG")
    assert bad is not None
    assert bad.content_hash is not None, "哈希对损坏文件仍然算得出（1 字节也能哈希）"
    assert bad.status == "PENDING", "无法生成预览的资产不应被谎报为 OK"
