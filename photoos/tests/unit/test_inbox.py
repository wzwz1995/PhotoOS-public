"""手机收件箱（P1）的单元与 API 测试。

覆盖的承诺：
  * **复制中的文件不会被导入**：两次数值一致 + 间隔达到 settle 秒才算稳定。
  * **已导入的不重复导入**：台账按 (相对路径, 大小, mtime) 记住。
  * **收件箱里的源文件只读**：导入后仍在原处、字节不变。
  * **入队后文件又变了**：执行时再核对一次大小/mtime，对不上就丢弃（下次扫描再说）。
  * 收件箱位于照片库内部时拒绝导入（会自我复制）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library import inbox as inbox_mod
from photoos.library.store import LibraryStore, write_marker


def _photo(path: Path, seed: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (320, 240), (seed * 17 % 256, seed * 31 % 256, seed * 53 % 256))
    image.save(path, format="JPEG")
    return path


def _force_stable(settings: Settings, *, seconds: float = 120.0) -> None:
    """把"这个 (大小, mtime) 首次被观察到的时刻"改成很久以前。

    这比 `time.sleep(settle)` 快得多，测的仍然是真实判定逻辑（读快照 → 比数值 → 算持续时长）。
    """
    inbox_mod.scan_inbox(settings)
    state = settings.inbox_state_dir / "scan.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    payload["scanned_at"] = old
    for values in payload.get("files", {}).values():
        if len(values) >= 3:
            values[2] = old
        else:  # pragma: no cover - 兼容旧格式
            values.append(old)
    state.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path):
    with use_config_path(None):
        settings = Settings(
            data_root=tmp_path / "data",
            inbox_path=tmp_path / "inbox",
            inbox_settle_seconds=1.0,
        )
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        root.mkdir()
        write_marker(root, "uuid-inbox", "收件箱测试库")
        library = LibraryStore(db).create(
            name="收件箱测试库", root=root, library_uuid="uuid-inbox", marker_uuid="uuid-inbox"
        )

        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.settings = settings  # type: ignore[attr-defined]
            test_client.db = db  # type: ignore[attr-defined]
            test_client.library = library  # type: ignore[attr-defined]
            test_client.library_root = root  # type: ignore[attr-defined]
            yield test_client


# ===================================================================== 配置
def test_default_inbox_path_is_under_pictures() -> None:
    with use_config_path(None):
        settings = Settings(inbox_path=None)
    path = settings.effective_inbox_path
    assert path.name == "PhotoOS 收件箱"
    assert path.parent.name == "Pictures", "默认放在用户能找到的图片文件夹下"


def test_explicit_inbox_path_wins(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(inbox_path=tmp_path / "我的收件箱")
    assert settings.effective_inbox_path == tmp_path / "我的收件箱"


# ===================================================================== 稳定性判定
def test_first_scan_never_reports_ready(tmp_path: Path) -> None:
    """第一次扫描没有"上一次"可比 —— 必须报复制中，不能凭 mtime 猜。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=8.0)
        _photo(settings.effective_inbox_path / "a.JPG", 1)
        t0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
        snapshot = inbox_mod.scan_inbox(settings, now=t0)
        assert snapshot.exists
        assert [item.state for item in snapshot.files] == [inbox_mod.STATE_COPYING]
        assert snapshot.ready_map() == {}


def test_file_becomes_ready_after_two_scans_and_settle(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=8.0)
        _photo(settings.effective_inbox_path / "a.JPG", 2)
        t0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
        inbox_mod.scan_inbox(settings, now=t0)
        # 间隔不够：仍然复制中（防止"刚扫完就说好了"）
        mid = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=3))
        assert mid.ready_map() == {}
        # 间隔够了且数值没变：可导入
        late = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=20))
        assert late.ready_map() == {"a.JPG": (late.files[0].size, late.files[0].mtime_ns)}
        assert late.to_dict()["ready"] == 1


def test_repeated_scan_does_not_reset_stability_clock(tmp_path: Path) -> None:
    """**回归**：反复扫描不能把"已稳定"的计时归零。

    真实现场：第二次扫描看到"可导入"，用户立刻点「立即导入」，而导入端点在内部
    又扫了一次 —— 如果稳定性按"距上次扫描的间隔"算，这一扫就把时钟重置，
    同一个文件瞬间变回"复制中"，导入被拒（实测踩到）。
    """
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=8.0)
        _photo(settings.effective_inbox_path / "a.JPG", 11)
        t0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
        inbox_mod.scan_inbox(settings, now=t0)
        inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=5))
        # 自首次观察起已 9 秒：即使上一次扫描只隔了 4 秒，也必须可导入
        ready = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=9))
        assert ready.ready_map(), ready.to_dict()["items"]
        # 紧接着再扫一次（模拟导入端点内部那一扫）：仍然可导入
        again = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=9))
        assert again.ready_map(), "重复扫描不得重置计时"


def test_file_still_growing_stays_copying(tmp_path: Path) -> None:
    """大小变了 = 还在复制：即使间隔超过 settle 也不能放行。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=1.0)
        target = settings.effective_inbox_path / "big.JPG"
        _photo(target, 3)
        t0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
        inbox_mod.scan_inbox(settings, now=t0)
        with open(target, "ab") as handle:
            handle.write(b"\x00" * 4096)
        later = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=30))
        assert later.ready_map() == {}
        assert later.files[0].state == inbox_mod.STATE_COPYING


def test_empty_file_is_never_ready(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=1.0)
        inbox = settings.effective_inbox_path
        inbox.mkdir(parents=True)
        (inbox / "empty.JPG").write_bytes(b"")
        t0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
        inbox_mod.scan_inbox(settings, now=t0)
        later = inbox_mod.scan_inbox(settings, now=t0 + timedelta(seconds=30))
        assert later.files[0].state == inbox_mod.STATE_EMPTY
        assert later.ready_map() == {}


def test_non_media_files_are_ignored(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
                            inbox_settle_seconds=1.0)
        inbox = settings.effective_inbox_path
        inbox.mkdir(parents=True)
        (inbox / "说明.txt").write_text("不是照片", encoding="utf-8")
        snapshot = inbox_mod.scan_inbox(settings)
        assert snapshot.files == []


# ===================================================================== API：状态
def test_inbox_status_endpoint_reports_states(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    _photo(settings.effective_inbox_path / "a.JPG", 4)

    first = client.get("/api/inbox").json()
    assert first["exists"] is True
    assert first["copying"] == 1
    assert first["ready"] == 0
    assert first["items"][0]["state"] == "copying"

    _force_stable(settings)
    second = client.get("/api/inbox").json()
    assert second["ready"] == 1
    assert second["copying"] == 0
    assert second["items"][0]["state"] == "ready"


def test_inbox_status_warns_when_inbox_inside_library(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    library_root: Path = client.library_root  # type: ignore[attr-defined]
    settings.inbox_path = library_root / "收件箱"
    _photo(settings.inbox_path / "a.JPG", 5)
    payload = client.get("/api/inbox").json()
    assert any("照片库内部" in warning for warning in payload["warnings"])


# ===================================================================== API：导入
def test_inbox_import_requires_confirmation(client: TestClient) -> None:
    response = client.post("/api/inbox/import", json={})
    assert response.status_code == 428
    assert "确认" in response.json()["detail"]


def test_inbox_import_without_ready_files_is_400(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    _photo(settings.effective_inbox_path / "a.JPG", 6)
    response = client.post("/api/inbox/import", json={"confirmed": True})
    assert response.status_code == 400
    assert "没有已复制完成" in response.json()["message"]


def test_inbox_import_creates_job_with_ready_scope(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    _photo(settings.effective_inbox_path / "a.JPG", 7)
    _force_stable(settings)

    response = client.post("/api/inbox/import", json={"confirmed": True})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ready"] == 1
    assert payload["job"]["type"] == "classify_import"

    row = client.db.query_one(  # type: ignore[attr-defined]
        "SELECT scope_json FROM job WHERE id = ?", (payload["job"]["id"],)
    )
    scope = json.loads(row["scope_json"])
    assert scope["source"] == str(settings.effective_inbox_path)
    assert list(scope["inbox_ready"]) == ["a.JPG"]


# ===================================================================== 端到端
def test_inbox_import_copies_but_never_touches_the_source(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    source = _photo(settings.effective_inbox_path / "a.JPG", 8)
    original = source.read_bytes()
    _force_stable(settings)

    job = client.post("/api/inbox/import", json={"confirmed": True}).json()["job"]
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error

    # 源文件：还在原处、字节完全一致（只读承诺）
    assert source.is_file()
    assert source.read_bytes() == original

    # 目标文件：已经在库里，而且不是 __2 之类的冲突副本
    copied = list(client.library_root.rglob("*.JPG"))  # type: ignore[attr-defined]
    assert len(copied) == 1
    assert copied[0].read_bytes() == original
    assert "__2" not in copied[0].name

    # 台账：下一次扫描应显示"已导入"，且再次导入会被拒绝（不重复导入）
    status = client.get("/api/inbox").json()
    assert status["imported"] == 1
    assert status["ready"] == 0
    again = client.post("/api/inbox/import", json={"confirmed": True})
    assert again.status_code == 400
    assert again.json()["message"]


def test_inbox_import_can_clean_sources_after_success(client: TestClient) -> None:
    """显式勾选清理后：只删已入库的收件箱副本，目标文件仍在库里。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    source = _photo(settings.effective_inbox_path / "cleanup.JPG", 18)
    _force_stable(settings)

    job = client.post(
        "/api/inbox/import",
        json={"confirmed": True, "cleanup_after_import": True},
    ).json()["job"]
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error

    assert not source.exists(), "勾选清理后，已成功入库的收件箱副本应当删除"
    copied = list(client.library_root.rglob("*.JPG"))  # type: ignore[attr-defined]
    assert len(copied) == 1
    assert "收件箱清理" in report.stages
    assert "已清理 1 个" in report.stages["收件箱清理"]

    status = client.get("/api/inbox").json()
    assert status["total"] == 0


def test_inbox_cleanup_endpoint_requires_confirmation_and_removes(client: TestClient) -> None:
    """手动清理入口：未确认拒绝；确认后只删已入库副本，目标仍在。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    source = _photo(settings.effective_inbox_path / "manual.JPG", 21)
    _force_stable(settings)
    job = client.post("/api/inbox/import", json={"confirmed": True}).json()["job"]
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    assert source.is_file()

    denied = client.post("/api/inbox/cleanup", json={})
    assert denied.status_code == 428
    result = client.post("/api/inbox/cleanup", json={"confirmed": True}).json()
    assert result["deleted"] == 1
    assert result["errors"] == []
    assert not source.exists()
    assert len(list(client.library_root.rglob("*.JPG"))) == 1  # type: ignore[attr-defined]


def test_duplicate_source_is_recorded_and_cleaned(client: TestClient) -> None:
    """同内容换名字再导入：也要记进台账，否则永远显示"可导入"。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    first = _photo(settings.effective_inbox_path / "first.JPG", 22)
    _force_stable(settings)
    job = client.post("/api/inbox/import", json={"confirmed": True}).json()["job"]
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error

    duplicate = settings.effective_inbox_path / "copy-of-first.JPG"
    shutil.copyfile(first, duplicate)
    _force_stable(settings)
    job = client.post(
        "/api/inbox/import",
        json={"confirmed": True, "cleanup_after_import": True},
    ).json()["job"]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    assert "收件箱台账" in report.stages, report.stages
    assert "已登记 1 个文件" in report.stages["收件箱台账"]
    assert not first.exists() and not duplicate.exists()
    assert len(list(client.library_root.rglob("*.JPG"))) == 1  # type: ignore[attr-defined]
    assert client.get("/api/inbox").json()["total"] == 0


def test_inbox_cleanup_keeps_file_when_target_is_missing(client: TestClient) -> None:
    """目标文件不在了就绝不删收件箱副本 —— 清理不能变成丢照片。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    source = _photo(settings.effective_inbox_path / "keep.JPG", 19)
    _force_stable(settings)

    job = client.post("/api/inbox/import", json={"confirmed": True}).json()["job"]
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    copied = list(client.library_root.rglob("*.JPG"))  # type: ignore[attr-defined]
    assert len(copied) == 1
    copied[0].unlink()

    result = inbox_mod.cleanup_imported(
        settings,
        library_roots={client.library.id: client.library_root},  # type: ignore[attr-defined]
    )
    assert source.is_file()
    assert result.deleted == 0
    assert result.kept == 1


def test_inbox_cleanup_never_touches_unimported_files(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    stray = _photo(settings.effective_inbox_path / "not-imported.JPG", 20)
    result = inbox_mod.cleanup_imported(
        settings,
        library_roots={client.library.id: client.library_root},  # type: ignore[attr-defined]
    )
    assert result.deleted == 0
    assert stray.is_file()


def test_job_drops_file_that_changed_after_enqueue(client: TestClient) -> None:
    """入队后文件又被写入：执行时核对大小/mtime 不通过 → 本次不导入。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    source = _photo(settings.effective_inbox_path / "a.JPG", 9)
    _force_stable(settings)
    job = client.post("/api/inbox/import", json={"confirmed": True}).json()["job"]

    with open(source, "ab") as handle:
        handle.write(b"\x00" * 2048)

    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == job["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    assert "没有可导入的稳定文件" in json.dumps(report.stages, ensure_ascii=False)
    assert list(client.library_root.rglob("*.JPG")) == []  # type: ignore[attr-defined]

    # 文件稳定后（重新扫描 + 把扫描时间改成很久以前）还能被正常导入 ——
    # 一次丢弃不该把它永久排除
    _force_stable(settings)
    status = client.get("/api/inbox").json()
    assert status["ready"] == 1


def test_inbox_import_refuses_when_library_root_is_missing(client: TestClient) -> None:
    """照片库所在的盘不在时，导入要**早失败**并说清原因（而不是每个文件都失败）。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    _photo(settings.effective_inbox_path / "a.JPG", 12)
    _force_stable(settings)
    shutil.rmtree(client.library_root)  # type: ignore[attr-defined]  # 模拟"盘被拔掉"
    response = client.post("/api/inbox/import", json={"confirmed": True})
    assert response.status_code == 400
    assert "不可访问" in response.json()["message"]
    assert "收件箱里的文件不会丢" in response.json()["message"]


def test_inbox_inside_library_refuses_import(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    library_root: Path = client.library_root  # type: ignore[attr-defined]
    settings.inbox_path = library_root / "收件箱"
    _photo(settings.inbox_path / "a.JPG", 10)
    _force_stable(settings)
    response = client.post("/api/inbox/import", json={"confirmed": True})
    assert response.status_code == 400
    assert "内部" in response.json()["message"]
