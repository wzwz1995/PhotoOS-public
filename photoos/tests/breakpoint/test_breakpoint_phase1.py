"""断点测试的 pytest 入口（`pytest -m breakpoint`）。

`pyproject.toml` 里声明了 `breakpoint` 标记，README 也写了
`pytest -m breakpoint` 的用法，因此这里必须有对应的测试 —— 否则文档在骗人。

这些测试**针对真实照片库**，默认会被跳过（需要 `PHOTOOS_BREAKPOINT_LIBRARY` 环境变量），
以免在普通 `pytest` 运行里意外跑上几十分钟。真正的完整验收用 `photoos breakpoint`。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from photoos.breakpoint import FAIL, MANUAL, PASS, BreakpointRunner
from photoos.config import get_settings
from photoos.db import Database
from photoos.library.store import LibraryStore

#: 指定要验收的 Library id；未设置时跳过（避免 CI/日常测试误跑真实库）
ENV_LIBRARY = "PHOTOOS_BREAKPOINT_LIBRARY"
ENV_PERIODS = "PHOTOOS_BREAKPOINT_PERIODS"

pytestmark = [
    pytest.mark.breakpoint,
    pytest.mark.skipif(
        not os.environ.get(ENV_LIBRARY),
        reason=f"需要真实照片库：设置环境变量 {ENV_LIBRARY}=<Library id> 后运行",
    ),
]


def _open() -> tuple[Database, int]:
    settings = get_settings()
    db = Database(settings.db_path)
    db.require_exists()
    library_id = int(os.environ[ENV_LIBRARY])
    return db, library_id


def _run(periods: int | None = None):
    settings = get_settings()
    db, library_id = _open()
    count = periods or int(os.environ.get(ENV_PERIODS, "2"))
    return BreakpointRunner(db, settings, library_id).run(periods=count)


@pytest.fixture(scope="module")
def report():
    """整模块共用一次断点执行结果（跑一遍要几十分钟，不能每个用例跑一遍）。"""
    return _run()


def test_breakpoint_overall(report) -> None:
    """总判定：不允许任何自动化项失败。"""
    failures = [
        (case.number, case.title, case.detail)
        for case in report.cases
        if case.status == FAIL
    ]
    assert not failures, f"断点测试存在失败项：{failures}"
    assert report.overall == PASS


def test_breakpoint_manual_items_are_declared(report) -> None:
    """物理操作类项目必须被标记为"需人工执行"，不能假装通过。"""
    manual = [case for case in report.cases if case.status == MANUAL]
    assert manual, "应至少有一项被明确标记为需人工执行（物理拔盘/换机）"
    for case in manual:
        assert len(case.steps) >= 4, f"#{case.number} 缺少可照做的步骤"


def test_breakpoint_no_failures(report) -> None:
    assert report.failed == 0, [case.title for case in report.cases if case.status == FAIL]


def test_breakpoint_originals_untouched(report) -> None:
    """断点 #7 必须是"通过"：原片未被修改。"""
    case7 = next(case for case in report.cases if case.number == 7)
    assert case7.status == PASS, case7.detail
    assert "变化 0 个" in case7.detail


def test_breakpoint_rebind_verified_by_hash(report) -> None:
    """断点 #10：重绑定必须由内容哈希确认（最强判据），而不是只靠路径。"""
    case10 = next(case for case in report.cases if case.number == 10)
    assert case10.status == PASS, case10.detail
    assert "校验=hash" in case10.detail


def test_breakpoint_heic_independent_of_system_codec(report) -> None:
    """断点 #11：HEIC 依赖内置 libheif。"""
    case11 = next(case for case in report.cases if case.number == 11)
    assert case11.status == PASS, case11.detail
    assert "libheif" in case11.detail


def test_breakpoint_report_is_written(tmp_path: Path, report) -> None:
    """报告必须是可读的 markdown，且包含人工步骤。"""
    target = tmp_path / "report.md"
    target.write_text(report.to_markdown(), encoding="utf-8")
    text = target.read_text(encoding="utf-8")

    assert text.startswith("# PhotoOS Phase 1 断点测试报告")
    assert "| # | 验收项 | 结果 | 说明 |" in text
    assert "## 需人工执行的步骤" in text
    # 12 项验收都要在表里出现
    for number in range(1, 13):
        assert f"| {number} |" in text or f"| {number} |" in text, f"报告缺少第 {number} 项"


def test_breakpoint_library_is_configured_for_this_machine() -> None:
    """确认被测 Library 确实存在于本机（避免环境变量指错导致"假通过"）。"""
    db, library_id = _open()
    library = LibraryStore(db).get(library_id)
    assert library.root_path_hint, "被测 Library 必须记录过路径提示"
