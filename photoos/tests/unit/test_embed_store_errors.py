"""`embed` 写库失败时的**可操作报错**（`_store`）。

## 为什么需要

`embed` 是六步里最长的一步（真实库约 6 分钟），期间持续往数据库写
（21,400 × 512 × 4 B ≈ 43.7 MB）。中途**磁盘写满**或**库被别的进程占住**时，
用户原本只能看到一句裸的：

    sqlite3.OperationalError: database or disk is full

这句话没有回答用户最关心的三件事：
  1. 已经算好的会不会白算？（实测**不会** —— 分批提交，见 §13.41 中断恢复）
  2. 现在该怎么办？
  3. 重跑会不会重复写？（**不会** —— 唯一键 + `INSERT OR IGNORE`）

本文件守住"把已经成立的性质翻译成人话"：
  * 磁盘满 → 说明已写入的部分安全、腾空间后原样重跑即可
  * 库被占 → 提示确认没有第二个 photoos 在跑
  * **其他异常必须原样抛出**（不能把真实 bug 伪装成"磁盘满"）
"""

from __future__ import annotations

import sqlite3

import pytest

from photoos.library.embed_pipeline import EmbedReport, _store
from photoos.library.model_store import EmbeddingNotAvailable


class _FakeRepo:
    """按注入的异常模拟 `store_many` 的失败。"""

    def __init__(self, error: Exception | None = None, written: int = 3) -> None:
        self.error = error
        self.written = written
        self.calls: list[list[dict]] = []

    def store_many(self, rows):
        self.calls.append(list(rows))
        if self.error is not None:
            raise self.error
        return min(self.written, len(rows))


def test_disk_full_gets_an_actionable_message() -> None:
    """磁盘满 → 必须说清"已写入的没丢"与"原样重跑即可"。"""
    repo = _FakeRepo(sqlite3.OperationalError("database or disk is full"))
    report = EmbedReport(embedded=1234)

    with pytest.raises(EmbeddingNotAvailable) as excinfo:
        _store(repo, [{"asset_id": 1}], report)

    message = str(excinfo.value)
    assert "磁盘空间不足" in message, message
    # 关键信息：已写入的保住了（用户最担心的"白算"）
    assert "1,234" in message, message
    assert "不会丢" in message, message
    # 关键信息：怎么办
    assert "重跑" in message and "embed --apply" in message, message
    assert "不会重复写入" in message, message


def test_database_locked_gets_its_own_message() -> None:
    """库被占住 → 提示"确认没有第二个 photoos 在跑"，而不是说成磁盘满。"""
    repo = _FakeRepo(sqlite3.OperationalError("database is locked"))
    report = EmbedReport(embedded=42)

    with pytest.raises(EmbeddingNotAvailable) as excinfo:
        _store(repo, [{"asset_id": 1}], report)

    message = str(excinfo.value)
    assert "占住" in message or "locked" in message.lower(), message
    assert "serve" in message, message
    assert "磁盘" not in message, "不能把'库被锁'说成'磁盘满'"
    assert "42" in message, message


def test_other_errors_are_not_masked() -> None:
    """★ 其他异常必须**原样抛出** —— 否则真实的 bug 会被伪装成"磁盘满"。"""
    repo = _FakeRepo(ValueError("这是个真 bug，不是磁盘问题"))
    with pytest.raises(ValueError, match="真 bug"):
        _store(repo, [{"asset_id": 1}], EmbedReport())


def test_happy_path_returns_written_count() -> None:
    """正常路径要返回真实写入条数（调用方累加到 `report.embedded`）。"""
    repo = _FakeRepo(written=3)
    assert _store(repo, [{"asset_id": 1}, {"asset_id": 2}], EmbedReport()) == 2
    assert _store(repo, [{"asset_id": 1}], EmbedReport()) == 1
    assert len(repo.calls) == 2
