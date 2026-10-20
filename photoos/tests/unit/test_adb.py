"""ADB 导入（P2）的单元与 API 测试 —— 全部用**假 adb**，不需要真机。

覆盖的承诺：
  * 解析：`adb devices -l` / `ls -l`（含 `total` 行、含空格的名称）；
  * 只读：生成的命令只有 devices / shell ls / pull，绝没有 rm/mv/push；
  * 不越界：手机路径只允许 /sdcard 与 /storage；
  * 拉取：整目录、按日期筛选、失败可读、取消会清暂存；
  * 发布：进收件箱的新目录、永不覆盖；暂存目录只删自己的；
  * API：状态/列表/拉取三个端点，确认门槛与错误翻译。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs import JobQueue, JobRunner
from photoos.library import adb as adb_mod
from photoos.library.store import LibraryStore, write_marker

FAKE_ADB = '''
import os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
fail = os.environ.get("FAKE_ADB_FAIL") == "1"
source = Path(os.environ.get("FAKE_ADB_SOURCE", "."))

if fail:
    print("adb: device offline", file=sys.stderr)
    sys.exit(1)

if args[:2] == ["devices", "-l"]:
    print("List of devices attached")
    print("FAKE123\\tdevice product:test model:Fake_Phone device:test transport_id:7")
    print("UNAUTH1\\tunauthorized usb:1-2 transport_id:8")
    sys.exit(0)

if "shell" in args:
    print("total 4096")
    print("-rw-rw---- 1 u0_a1 sdcard_rw 1024 2026-10-20 12:00 IMG_0001.JPG")
    print("-rw-rw---- 1 u0_a1 sdcard_rw 2048 2026-10-15 09:30 IMG_0002.JPG")
    print("-rw-rw---- 1 u0_a1 sdcard_rw 4096 2026-10-20 12:05 VID_0003.MP4")
    sys.exit(0)

if "pull" in args:
    remote = args[-2]
    dest = Path(args[-1])
    if str(remote).rstrip("/").endswith("Camera"):
        for path in sorted(source.rglob("*")):
            if path.is_file():
                target = dest / "Camera" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                print("[ 50%] " + path.name)
        print("[100%] done")
    else:
        name = Path(remote).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, dest)
        print("[100%] " + name)
    sys.exit(0)

print("unknown command", file=sys.stderr)
sys.exit(2)
'''

LS_SAMPLE = "\n".join(
    [
        "total 4096",
        "-rw-rw---- 1 u0_a1 sdcard_rw 1024 2026-10-20 12:00 IMG_0001.JPG",
        "-rw-rw---- 1 u0_a1 sdcard_rw 2048 2026-10-15 09:30 IMG 0002.JPG",
        "drwxrwx--- 2 u0_a1 sdcard_rw 4096 2026-10-20 12:00 Raw",
    ]
)


@pytest.fixture()
def phone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """返回 `(fake_adb 脚本, 手机文件目录)`；环境变量指给假 adb。"""
    source = tmp_path / "phone"
    source.mkdir()
    (source / "IMG_0001.JPG").write_bytes(b"A" * 1024)
    (source / "IMG_0002.JPG").write_bytes(b"B" * 2048)
    (source / "VID_0003.MP4").write_bytes(b"C" * 4096)
    script = tmp_path / "fake_adb.py"
    script.write_text(FAKE_ADB, encoding="utf-8")
    monkeypatch.setenv("FAKE_ADB_SOURCE", str(source))
    return script, source


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    with use_config_path(None):
        value = Settings(data_root=tmp_path / "data", inbox_path=tmp_path / "inbox")
        value.ensure_dirs()
        yield value


@pytest.fixture()
def client(tmp_path: Path, phone: tuple[Path, Path]):
    script, _source = phone
    with use_config_path(None):
        settings = Settings(
            data_root=tmp_path / "data",
            inbox_path=tmp_path / "inbox",
            adb_path=script,
        )
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        root = tmp_path / "library"
        root.mkdir()
        write_marker(root, "uuid-adb", "ADB 测试库")
        library = LibraryStore(db).create(
            name="ADB 测试库", root=root, library_uuid="uuid-adb", marker_uuid="uuid-adb"
        )
        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.settings = settings  # type: ignore[attr-defined]
            test_client.db = db  # type: ignore[attr-defined]
            test_client.library = library  # type: ignore[attr-defined]
            yield test_client


def _client(script: Path) -> adb_mod.AdbClient:
    return adb_mod.AdbClient([sys.executable, str(script)])


# ===================================================================== 解析
def test_parse_devices() -> None:
    devices = adb_mod.parse_devices(
        "List of devices attached\n"
        "R58M123ABC\tdevice product:x1 model:MI_14 device:x1 transport_id:3\n"
        "012345\tunauthorized usb:1-2 transport_id:4\n"
        "* daemon not running; starting now\n"
    )
    assert [d.serial for d in devices] == ["R58M123ABC", "012345"]
    assert devices[0].usable and devices[0].model == "MI_14"
    assert devices[0].label == "MI 14 (R58M123ABC)"
    assert not devices[1].usable and devices[1].state == "unauthorized"


def test_parse_ls_skips_total_and_keeps_spaces() -> None:
    files = adb_mod.parse_ls(LS_SAMPLE)
    assert [f.name for f in files] == ["IMG_0001.JPG", "IMG 0002.JPG", "Raw"]
    assert files[0].size == 1024 and files[0].date == "2026-10-20"
    assert files[2].is_dir is True


def test_remote_path_must_be_public_storage() -> None:
    assert adb_mod.validate_remote("/sdcard/DCIM/Camera") == "/sdcard/DCIM/Camera"
    for bad in ("/data/data/com.example", "", "/sdcard/../data", "C:\\DCIM", "/sdcard/a\\b"):
        with pytest.raises(Exception):
            adb_mod.validate_remote(bad)


def test_locate_uses_python_script_when_configured(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(adb_path=tmp_path / "wrap.py")
    client = adb_mod.AdbClient.locate(settings)
    assert client is not None
    assert client.command == [sys.executable, str(tmp_path / "wrap.py")]


# ===================================================================== 客户端
def test_devices_and_list_with_fake_adb(phone: tuple[Path, Path]) -> None:
    script, _ = phone
    client = _client(script)
    devices = client.devices()
    assert len(devices) == 2
    files = client.list_dir("FAKE123", "/sdcard/DCIM/Camera")
    assert [f.name for f in files] == ["IMG_0001.JPG", "IMG_0002.JPG", "VID_0003.MP4"]


def test_pull_tree_copies_everything_and_reports_progress(
    phone: tuple[Path, Path], tmp_path: Path
) -> None:
    script, _ = phone
    client = _client(script)
    staging = tmp_path / "staging"
    events: list[tuple[int, int, str]] = []
    result = client.pull(
        "FAKE123", "/sdcard/DCIM/Camera", staging,
        on_progress=lambda done, total, note: events.append((done, total, note)),
    )
    assert result.ok and result.files == 3
    assert result.bytes == 1024 + 2048 + 4096
    assert (staging / "Camera" / "IMG_0001.JPG").read_bytes() == b"A" * 1024
    assert events, "至少要有一次进度回调"


def test_pull_since_only_copies_recent_files(phone: tuple[Path, Path], tmp_path: Path) -> None:
    script, _ = phone
    client = _client(script)
    staging = tmp_path / "staging"
    result = client.pull(
        "FAKE123", "/sdcard/DCIM/Camera", staging, since="2026-10-16"
    )
    assert result.ok and result.files == 2
    assert (staging / "IMG_0001.JPG").is_file()
    assert not (staging / "IMG_0002.JPG").exists()


def test_pull_failure_is_reported(
    phone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = phone
    monkeypatch.setenv("FAKE_ADB_FAIL", "1")
    result = _client(script).pull("FAKE123", "/sdcard/DCIM/Camera", tmp_path / "staging")
    assert result.ok is False
    assert result.errors and "device offline" in result.errors[0]


def test_commands_never_mutate_the_phone(
    phone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = phone
    client = _client(script)
    captured: list[list[str]] = []

    def fake_run(args, *, timeout=adb_mod.ADB_TIMEOUT):
        captured.append(list(args))
        return adb_mod.subprocess.CompletedProcess(args, 0, LS_SAMPLE, "")

    monkeypatch.setattr(client, "run", fake_run)
    client.list_dir("FAKE123", "/sdcard/DCIM/Camera")
    client.pull("FAKE123", "/sdcard/DCIM/Camera", tmp_path / "s1", since="2026-10-16")

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured.append(list(argv))
            self.stdout = io.StringIO("")
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

    monkeypatch.setattr(adb_mod.subprocess, "Popen", FakePopen)
    client.pull("FAKE123", "/sdcard/DCIM/Camera", tmp_path / "s2")

    flat = [" ".join(argv) for argv in captured]
    assert flat, "没有捕获到任何命令"
    for argv in captured:
        for token in ("rm", "mv", "push", "unlink", "del", "delete"):
            assert token not in argv, f"生成了会改动手机的命令：{argv}"
    for line in flat:
        assert " rm " not in line and " mv " not in line and " push " not in line


def test_pull_always_preserves_source_mtime(
    phone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**回归**：pull 必须带 `-a`。

    真机验收发现：不带 `-a` 时 adb 不保留 mtime，没有 EXIF 的保存图（抖音/微信导出）
    会被归到"拉取的今天"。`-a` 让本地 mtime 等于手机上的修改时间。
    """
    script, _ = phone
    client = _client(script)
    captured: list[list[str]] = []

    def fake_run(args, *, timeout=adb_mod.ADB_TIMEOUT):
        captured.append(list(args))
        return adb_mod.subprocess.CompletedProcess(args, 0, LS_SAMPLE, "")

    monkeypatch.setattr(client, "run", fake_run)
    client.pull("FAKE123", "/sdcard/DCIM/Camera", tmp_path / "s1", since="2026-10-16")

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured.append(list(argv))
            self.stdout = io.StringIO("")
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

    monkeypatch.setattr(adb_mod.subprocess, "Popen", FakePopen)
    client.pull("FAKE123", "/sdcard/DCIM/Camera", tmp_path / "s2")

    pull_commands = [argv for argv in captured if "pull" in argv]
    assert pull_commands, f"没有捕获到 pull 命令：{captured}"
    for argv in pull_commands:
        assert "-a" in argv, f"pull 少了 -a（时间戳会丢）：{argv}"


# ===================================================================== 暂存/发布
def test_publish_moves_into_new_directory_and_never_overwrites(
    settings: Settings, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    first = adb_mod.staging_dir(settings, "Fake Phone")
    (first / "a.JPG").write_bytes(b"a")
    target1 = adb_mod.publish(first, inbox, "Fake Phone")
    assert (target1 / "a.JPG").is_file()
    assert not first.exists(), "发布后暂存目录应该已经移走"

    second = adb_mod.staging_dir(settings, "Fake Phone")
    (second / "a.JPG").write_bytes(b"b")
    target2 = adb_mod.publish(second, inbox, "Fake Phone")
    assert target2 != target1, "第二次必须落到新目录，不能覆盖"
    assert (target1 / "a.JPG").read_bytes() == b"a"


def test_discard_only_touches_staging(settings: Settings, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.JPG").write_bytes(b"x")
    adb_mod.discard(outside)
    assert (outside / "keep.JPG").is_file(), "收件箱/用户目录绝不能被 discard 碰到"

    staging = adb_mod.staging_dir(settings, "x")
    (staging / "part.JPG").write_bytes(b"y")
    adb_mod.discard(staging)
    assert not staging.exists()


def test_cleanup_stale_staging_only_removes_old(settings: Settings) -> None:
    fresh = adb_mod.staging_dir(settings, "fresh")
    stale = adb_mod.staging_dir(settings, "stale")
    old = 1_600_000_000
    import os

    os.utime(stale, (old, old))
    assert adb_mod.cleanup_stale_staging(settings, older_than_hours=24) == 1
    assert fresh.exists() and not stale.exists()


# ===================================================================== API
def test_adb_status_reports_devices(client: TestClient) -> None:
    payload = client.get("/api/adb").json()
    assert payload["available"] is True
    assert payload["remote_path"] == "/sdcard/DCIM/Camera"
    assert len(payload["devices"]) == 2
    assert sum(1 for item in payload["devices"] if item["usable"]) == 1


def test_adb_status_without_adb(tmp_path: Path) -> None:
    with use_config_path(None):
        settings = Settings(
            data_root=tmp_path / "data", inbox_path=tmp_path / "inbox",
            adb_path=tmp_path / "not-there.exe",
        )
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        with TestClient(create_app(settings)) as test_client:
            payload = test_client.get("/api/adb").json()
    assert payload["available"] is False
    assert any("收件箱" in hint for hint in payload["hints"]), "要告诉用户不装也能用"


def test_adb_list_returns_files(client: TestClient) -> None:
    payload = client.post("/api/adb/list", json={}).json()
    assert payload["total"] == 3
    assert payload["bytes"] == 1024 + 2048 + 4096
    assert payload["files"][0]["name"] == "VID_0003.MP4", "按日期/时间倒序，最新的在前"


def test_adb_list_rejects_private_path(client: TestClient) -> None:
    response = client.post("/api/adb/list", json={"remote_path": "/data/data/com.example"})
    assert response.status_code == 400
    assert "/sdcard" in response.json()["message"]


def test_adb_pull_requires_confirmation(client: TestClient) -> None:
    response = client.post("/api/adb/pull", json={})
    assert response.status_code == 428


def test_adb_pull_creates_inbox_when_missing(client: TestClient) -> None:
    """收件箱目录不存在也能拉：入队时先建好（用户不必先手动"打开收件箱"）。"""
    settings: Settings = client.settings  # type: ignore[attr-defined]
    assert not settings.effective_inbox_path.exists()
    response = client.post("/api/adb/pull", json={"confirmed": True})
    assert response.status_code == 200, response.text
    assert settings.effective_inbox_path.is_dir()


def test_adb_pull_rejects_bad_date(client: TestClient) -> None:
    response = client.post(
        "/api/adb/pull", json={"confirmed": True, "since": "20-10-2026"}
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["message"]


def test_adb_pull_job_publishes_into_inbox(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    payload = client.post("/api/adb/pull", json={"confirmed": True}).json()
    staging = Path(payload["staging"])
    assert staging.is_dir()

    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    assert record is not None and record.id == payload["job"]["id"]
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error

    assert not staging.exists(), "发布后暂存区应当已移走"
    pulled = list((settings.effective_inbox_path / "ADB").rglob("*.JPG"))
    assert len(pulled) == 2
    assert any(path.name == "VID_0003.MP4"
               for path in (settings.effective_inbox_path / "ADB").rglob("*"))
    assert "收件箱" in json.dumps(report.stages, ensure_ascii=False)

    # 收件箱第一次扫描：新文件应当是"复制中"，不会被立刻导入
    status = client.get("/api/inbox").json()
    assert status["imported"] == 0
    assert status["copying"] == 3


def test_adb_pull_since_filters(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    payload = client.post(
        "/api/adb/pull", json={"confirmed": True, "since": "2026-10-16"}
    ).json()
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    record = queue.claim_next()
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "COMPLETED", report.error
    pulled = list((settings.effective_inbox_path / "ADB").rglob("*"))
    names = {path.name for path in pulled if path.is_file()}
    assert names == {"IMG_0001.JPG", "VID_0003.MP4"}


def test_adb_pull_cancel_cleans_staging(client: TestClient) -> None:
    settings: Settings = client.settings  # type: ignore[attr-defined]
    payload = client.post("/api/adb/pull", json={"confirmed": True}).json()
    staging = Path(payload["staging"])
    queue = JobQueue(client.db)  # type: ignore[attr-defined]
    queue.cancel(payload["job"]["id"])
    record = queue.get(payload["job"]["id"])
    report = JobRunner(client.db, settings).run(record)  # type: ignore[attr-defined]
    assert report.status == "CANCELLED"
    assert not staging.exists(), "取消要清掉自己的暂存副本"
    assert not (settings.effective_inbox_path / "ADB").exists(), "取消不应往收件箱发布任何东西"
