"""断点测试执行器自身的测试。

断点测试是 Phase 1 的交付闸门，因此它自己必须先可信：
  * 每项结果必须是 通过/失败/需人工执行 之一，不得静默"假装通过"。
  * 失败必须如实反映（人为破坏一个不变量，报告就要变红）。
  * 报告是 markdown 且包含全部验收项与人工步骤。
  * 执行过程不得改动原片。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from photoos.breakpoint import FAIL, MANUAL, PASS, BreakpointRunner
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library.store import LibraryStore, write_marker


def _photo(path: Path, seed: int) -> Path:
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", (480, 320), (seed * 19 % 256, seed * 41 % 256, seed * 61 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = f"2025:10:2{seed} 09:{seed:02d}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    sub[0x829D] = IFDRational(40, 10)
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def bp_env(tmp_path: Path):
    """一个已建索引的小库 + 断点执行器。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        (root / "杭州市" / "2025-10-21_0900-1000").mkdir(parents=True)
        (root / "上海市" / "2025-10-22_0900-1000").mkdir(parents=True)
        for index in range(1, 5):
            _photo(root / "杭州市" / "2025-10-21_0900-1000" / f"DSC_{3000 + index}.JPG", index)
        for index in range(1, 3):
            _photo(root / "上海市" / "2025-10-22_0900-1000" / f"DSC_{4000 + index}.JPG", index)

        write_marker(root, "uuid-bp", "断点测试库")
        library = LibraryStore(db).create(
            name="断点测试库", root=root, library_uuid="uuid-bp", marker_uuid="uuid-bp"
        )
        queue = JobQueue(db)
        job = queue.create("full_index", library_id=library.id)
        queue.claim_next()
        report = JobRunner(db, settings).run(job)
        assert report.status == "COMPLETED", report.error

        yield db, settings, library.id, root


def test_breakpoint_runner_passes_on_healthy_library(bp_env) -> None:
    db, settings, library_id, _root = bp_env
    report = BreakpointRunner(db, settings, library_id).run(periods=2)

    assert report.failed == 0, [(c.number, c.title, c.status, c.detail) for c in report.cases]
    assert report.overall == PASS
    assert report.passed > 0
    # 物理操作类必须被标记为"需人工执行"，不能假称通过
    assert report.manual >= 2
    manual_titles = [c.title for c in report.cases if c.status == MANUAL]
    assert any("换机迁移" in title for title in manual_titles)
    assert any("拔盘" in title for title in manual_titles)
    # 每个需人工的项都要给出可照做的步骤
    for case in report.cases:
        if case.status == MANUAL:
            assert len(case.steps) >= 4, f"#{case.number} 缺少可执行步骤"


def test_breakpoint_covers_prd_required_items(bp_env) -> None:
    """PRD §9.1 的 11 项都要在报告里有对应条目（允许合并为 12 号附加项）。"""
    db, settings, library_id, _root = bp_env
    report = BreakpointRunner(db, settings, library_id).run(periods=2)
    numbers = {case.number for case in report.cases}
    for required in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert required in numbers, f"缺少断点 #{required}"


def test_breakpoint_detects_modified_original(bp_env) -> None:
    """人为改动原片后，断点 #7 必须变红 —— 这是执行器可信度的关键。"""
    db, settings, library_id, root = bp_env
    runner = BreakpointRunner(db, settings, library_id)
    # 先建立基线（run 内部会取基线，这里模拟"基线之后被外部改动"）
    baseline = None

    original_hash = None
    victim = root / "杭州市" / "2025-10-21_0900-1000" / "DSC_3001.JPG"
    original_hash = victim.read_bytes()

    # 用 monkeypatch 在基线取完之后、比对之前篡改文件
    import photoos.breakpoint as bp

    real_snapshot = bp._stat_snapshot
    calls = {"n": 0}

    def snapshot_then_tamper(db_, library_id_, prefix=None):
        result = real_snapshot(db_, library_id_, prefix)
        calls["n"] += 1
        if calls["n"] == 1:
            # 改内容且改 mtime，模拟"系统改写了原片"
            victim.write_bytes(original_hash + b"tampered")
        return result

    bp._stat_snapshot = snapshot_then_tamper
    try:
        report = runner.run(periods=2)
    finally:
        bp._stat_snapshot = real_snapshot
        victim.write_bytes(original_hash)

    assert baseline is None
    case7 = next(case for case in report.cases if case.number == 7)
    assert case7.status == FAIL, f"改动原片必须被检出，实际：{case7.status} / {case7.detail}"
    assert report.overall == FAIL


def test_breakpoint_detects_original_change_against_injected_baseline(bp_env) -> None:
    """注入一个"错误的基线"后，断点 #7 必须（且只能）报失败。

    说明：不能靠"先破坏索引再跑断点"来测检出能力 —— 断点流程本身会重新索引，
    会把人为破坏修好（这是预期行为）。因此这里改为把**基线**注入成与磁盘不符，
    模拟"文件在索引之后被外部改动"。
    """
    db, settings, library_id, root = bp_env
    import photoos.breakpoint as bp

    real_snapshot = bp._stat_snapshot
    calls = {"n": 0}

    def drifted_snapshot(db_, library_id_, prefix=None):
        result = real_snapshot(db_, library_id_, prefix)
        calls["n"] += 1
        if calls["n"] == 1 and result:
            # 只篡改基线中的第一个条目 → 与磁盘现状不符
            first = next(iter(result))
            result[first] = (result[first][0] + 1, result[first][1])
        return result

    bp._stat_snapshot = drifted_snapshot
    try:
        report = BreakpointRunner(db, settings, library_id).run(periods=2)
    finally:
        bp._stat_snapshot = real_snapshot

    case7 = next(case for case in report.cases if case.number == 7)
    assert case7.status == FAIL, f"基线不符必须被检出，实际：{case7.status} / {case7.detail}"
    assert report.overall == FAIL
    assert "变化 1 个" in case7.detail


def test_breakpoint_detects_missing_previews(bp_env) -> None:
    """删掉预览文件（但数据库记录还在）后，断点 #4 必须变红。

    这是真实故障形态：清理缓存、迁移包漏带派生数据、误删目录 ——
    数据库记录不会自动消失，如果不主动对账，界面会一直是空白缩略图且永不重建。
    """
    db, settings, library_id, _root = bp_env
    for directory in (settings.data_root / "thumbnails", settings.data_root / "previews"):
        if directory.is_dir():
            shutil.rmtree(directory)

    report = BreakpointRunner(db, settings, library_id).run(periods=1)
    case4 = next(case for case in report.cases if case.number == 4)
    assert case4.status == FAIL, case4.detail
    assert "不一致" in case4.detail
    assert report.overall == FAIL


def test_broken_preview_records_power_repair(bp_env) -> None:
    """对账 + 修复闭环：删缓存后能发现，修复后能重建。"""
    from photoos.repos import PreviewRepo

    db, settings, library_id, _root = bp_env
    previews = PreviewRepo(db)
    assert previews.broken_records(settings.data_root, library_id) == []

    shutil.rmtree(settings.data_root / "thumbnails")
    shutil.rmtree(settings.data_root / "previews")

    broken = previews.broken_records(settings.data_root, library_id)
    assert broken, "应能发现数据库有记录但磁盘文件缺失"
    assert {kind for _id, kind, _path in broken} == {"thumb", "preview"}

    removed = previews.clear_for_assets(sorted({asset_id for asset_id, _, _ in broken}))
    assert removed == len(broken)
    assert previews.broken_records(settings.data_root, library_id) == []

    # 记录清掉后，预览阶段应重新生成
    from photoos.jobs import JobQueue, JobRunner

    queue = JobQueue(db)
    job = queue.create("preview", library_id=library_id)
    result = JobRunner(db, settings).run(job)
    assert result.status == "COMPLETED"
    assert previews.count(library_id=library_id) == len(broken)
    assert previews.broken_records(settings.data_root, library_id) == []


def test_breakpoint_report_markdown(bp_env) -> None:
    db, settings, library_id, _root = bp_env
    report = BreakpointRunner(db, settings, library_id).run(periods=2)
    text = report.to_markdown()

    assert text.startswith("# PhotoOS Phase 1 断点测试报告")
    assert "| # | 验收项 | 结果 | 说明 |" in text
    assert "## 需人工执行的步骤" in text
    assert "换机迁移" in text
    assert str(report.library_id) in text
    assert "**通过**" in text or "**失败**" in text


def test_breakpoint_does_not_modify_originals(bp_env) -> None:
    """整个断点执行过程不得改动任何原片。"""
    db, settings, library_id, root = bp_env
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    BreakpointRunner(db, settings, library_id).run(periods=2)
    after = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_breakpoint_restores_library_location(bp_env) -> None:
    """重绑定与离线测试必须复原现场，不能给用户留下被搬走的库。"""
    db, settings, library_id, root = bp_env
    BreakpointRunner(db, settings, library_id).run(periods=2)

    assert root.is_dir(), "断点测试结束后照片库必须回到原位"
    assert not root.with_name(root.name + ".bp-offline").exists()
    assert not root.with_name(root.name + ".bp-rebind").exists()
    library = LibraryStore(db).get(library_id)
    assert library.root_path_hint == str(root)
    assert library.status == "ONLINE"


def test_breakpoint_empty_library_reports_failure(tmp_path: Path) -> None:
    """空库必须直接失败并给出可操作提示，而不是给出一份全绿报告。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        root = tmp_path / "empty"
        root.mkdir()
        library = LibraryStore(db).create(name="空库", root=root)

        report = BreakpointRunner(db, settings, library.id).run(periods=1)
        assert report.overall == FAIL
        assert any("没有任何 Asset" in case.detail for case in report.cases)


def test_pick_representative_periods_prefers_variety(bp_env) -> None:
    import photoos.breakpoint as bp

    db, _settings, library_id, _root = bp_env
    periods = bp._pick_representative_periods(db, library_id, 2)
    assert len(periods) == 2
    assert all("\\" in name for name in periods)
    assert periods[0].startswith(("杭州市", "上海市"))


def test_breakpoint_migration_package_case_is_readonly(bp_env) -> None:
    """断点 #9 的自动化部分只应读取数据根并生成临时包。

    注意：`photoos.db` 本身会被断点流程写入（任务记录、审计事件），那是预期行为；
    这里校验的是**派生缓存**（缩略图/预览）在迁移包流程中没有被改写或删除。
    """
    db, settings, library_id, _root = bp_env
    derived_before = {
        path.relative_to(settings.data_root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in settings.data_root.rglob("*")
        if path.is_file() and path.suffix == ".webp"
    }
    assert derived_before, "前置条件：应有已生成的缩略图/预览"

    report = BreakpointRunner(db, settings, library_id).run(periods=1)

    derived_after = {
        path.relative_to(settings.data_root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in settings.data_root.rglob("*")
        if path.is_file() and path.suffix == ".webp"
    }
    for name, snapshot in derived_before.items():
        assert name in derived_after, f"{name} 在断点流程中被删除了"
        assert derived_after[name] == snapshot, f"{name} 在断点流程中被改写"

    case9 = next(case for case in report.cases if case.number == 9 and case.status != MANUAL)
    assert case9.status == PASS, case9.detail
    assert "uuid 保持=True" in case9.detail
    assert "包内含照片原文件=False" in case9.detail
