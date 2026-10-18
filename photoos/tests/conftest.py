"""pytest 全局装置。

## 为什么测试里要关掉「自动分组」

`full_index` 与 `classify_import` 会在末尾自动跑一次**派生分组**
（见 `jobs/runner.py` 的 `_run_regroup`）。那是为了让"导入完就能用"成立 ——
但它实现方式是**派生子进程跑 CLI**（`photoos sessions backfill` / `shots regroup` /
`scenes rebuild`），一次三个 Python 进程。

在真实使用里这是对的：用户导入一次，等几秒无所谓。
但在测试里，任何碰 `classify_import` / `full_index` 的用例都要多花 3 次解释器启动
（实测每个用例 ~6 秒），而**它们要验的根本不是分组**。

所以这里统一设上守卫变量，让 `_run_regroup` 立刻返回（它本来就支持这个开关，
守卫同时也是**递归保护** —— 见 `AUTO_REGROUP_GUARD` 的说明）。

分组本身由 `tests/unit/test_jobs.py::test_regroup_is_idempotent` 与
`test_regroup_steps_are_ordered_and_complete` 专门覆盖，不受这里影响。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _skip_auto_regroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里不要为每个索引/导入用例都派生三个 CLI 子进程。

    用 `monkeypatch.setenv` 而不是直接改 `os.environ`：它会在用例结束后自动还原，
    不会污染同进程里的其它测试。
    """
    monkeypatch.setenv("PHOTOOS_SKIP_AUTO_REGROUP", "1")
