"""一键打包（`library/export.py` + `/api/exports*`）与"引导到原片路径"的契约测试。

这个功能的承诺有三条，测试就围着它们转：

  A. **原片只读**：打包前后原片逐字节相同（sha256 比对，不是"看起来没动"）；
  B. **不许悄悄漏**：不在盘上的文件必须逐个出现在报告与包内清单里 ——
     一个少了 2 张、却显示"成功"的包，比直接报错更糟；
  C. **产物是一个本地文件**：写到 `data/exports/`，界面上给出路径，
     **不经过浏览器**（与"单个原片只给路径、不下载"同一条纪律）。

另外还钉住两个容易写错的地方：包内重名（两次外拍都有 `DSC_0001.JPG`）不能互相覆盖、
空间不足必须在**动手之前**就拒绝（不留半成品）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.jobs.runner import JobRunner
from photoos.jobs.queue import JobQueue
from photoos.library import export as export_mod
from photoos.library.store import LibraryStore, write_marker

PAYLOAD = b"PhotoOS-original-bytes-" + bytes(range(64)) * 8


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: bytes = PAYLOAD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload + path.name.encode("utf-8"))   # 每个文件内容不同
    return path


@pytest.fixture()
def env(tmp_path: Path):
    """一个只有 4 张照片的最小库，但**包含一个重名**（两个目录里都有 DSC_0001.JPG）。"""
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        root = tmp_path / "library"
        files = {
            "杭州市/2025-10-25_1000-1100/DSC_0001.JPG":
                _write(root / "杭州市/2025-10-25_1000-1100/DSC_0001.JPG"),
            "杭州市/2025-10-25_1000-1100/DSC_0002.JPG":
                _write(root / "杭州市/2025-10-25_1000-1100/DSC_0002.JPG"),
            "上海市/2025-10-26_1000-1100/DSC_0001.JPG":
                _write(root / "上海市/2025-10-26_1000-1100/DSC_0001.JPG"),
            "上海市/2025-10-26_1000-1100/DSC_0009.JPG":
                _write(root / "上海市/2025-10-26_1000-1100/DSC_0009.JPG"),
        }
        write_marker(root, "uuid-export", "打包测试库")
        library = LibraryStore(db).create(name="打包测试库", root=root,
                                          library_uuid="uuid-export",
                                          marker_uuid="uuid-export")
        with db.write() as conn:
            for index, relative in enumerate(files, 1):
                conn.execute(
                    "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                    " size, first_seen_at) VALUES (?, ?, ?, 'jpeg', 'OK', ?, 't')",
                    (index, library.id, str(relative).replace("/", "\\"),
                     (root / relative).stat().st_size))
        yield {"settings": settings, "db": db, "library": library, "root": root,
               "files": files}


# ============================================================ A. 计划（不写任何文件）

def test_plan_counts_files_and_bytes_without_writing_anything(env) -> None:
    before = sorted(p.name for p in env["settings"].exports_dir.iterdir())
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    assert plan.count == 4
    assert plan.total_bytes == sum(f.stat().st_size for f in env["files"].values())
    assert plan.missing == [] and plan.no_root == []
    after = sorted(p.name for p in env["settings"].exports_dir.iterdir())
    assert before == after, "计划阶段写了文件"


def test_plan_reports_files_that_are_not_on_disk(env) -> None:
    """★ 不许悄悄漏：拔盘/被移走的文件必须出现在 `missing` 里。"""
    (env["root"] / "上海市/2025-10-26_1000-1100/DSC_0009.JPG").unlink()
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    assert plan.count == 3
    assert plan.missing == ["上海市\\2025-10-26_1000-1100\\DSC_0009.JPG"]
    assert plan.summary()["missing"] == 1


# ============================================================ B. 打包

def test_archive_contains_the_originals_under_their_relative_paths(env) -> None:
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    target = env["settings"].exports_dir / "树树.zip"
    result = export_mod.build_archive(plan, target)
    assert result.written == 4 and not result.cancelled
    assert target.is_file()

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "杭州市/2025-10-25_1000-1100/DSC_0002.JPG" in names
        assert export_mod.MANIFEST_NAME in names
        # 内容逐字节相同 —— 这是"原片只读"最直接的证据
        for relative, source in env["files"].items():
            assert archive.read(relative) == source.read_bytes()
        manifest = archive.read(export_mod.MANIFEST_NAME).decode("utf-8")
        assert "树树" in manifest
        assert "杭州市\\2025-10-25_1000-1100\\DSC_0001.JPG" in manifest
        assert "原片本身没有被修改过" in manifest


def test_duplicate_names_do_not_overwrite_each_other(env) -> None:
    """★ 两次外拍都有 `DSC_0001.JPG`。包内路径是**相对路径**，所以两个都在；
    真撞上同一个相对路径时必须改名，而不是安静地覆盖。"""
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    # 人为制造一次真撞名：把第 3 张的相对路径改成和第 1 张一样
    plan.entries[2].relative_path = plan.entries[0].relative_path
    target = env["settings"].exports_dir / "重名.zip"
    result = export_mod.build_archive(plan, target)
    assert result.written == 4
    assert result.duplicates, "撞名了却没有报告"
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert len([n for n in names if n.endswith(".JPG")]) == 4, "有文件被覆盖了"
        assert any(n.endswith("(2).JPG") for n in names)


def test_originals_are_byte_identical_after_packing(env) -> None:
    """★ 硬守卫：打包**只读**原片。"""
    before = {relative: _sha(path) for relative, path in env["files"].items()}
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    export_mod.build_archive(plan, env["settings"].exports_dir / "树树.zip")
    after = {relative: _sha(path) for relative, path in env["files"].items()}
    assert before == after


def test_a_file_that_disappears_mid_pack_is_reported_not_dropped(env) -> None:
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    (env["root"] / "上海市/2025-10-26_1000-1100/DSC_0001.JPG").unlink()   # 计划之后消失
    target = env["settings"].exports_dir / "树树.zip"
    result = export_mod.build_archive(plan, target)
    assert result.written == 3
    assert result.skipped == ["上海市\\2025-10-26_1000-1100\\DSC_0001.JPG"]
    with zipfile.ZipFile(target) as archive:
        assert "以下 1 个文件在打包时不在盘上" in archive.read(
            export_mod.MANIFEST_NAME).decode("utf-8")


def test_insufficient_space_refuses_before_writing(env, monkeypatch) -> None:
    """★ 空间不足要在动手之前拒绝：一个写了一半的包最容易被当成"就是这些"。"""
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    monkeypatch.setattr(export_mod.shutil, "disk_usage",
                        lambda _p: shutil._ntuple_diskusage(1 << 40, 0, 1024))
    target = env["settings"].exports_dir / "没空间.zip"
    with pytest.raises(OSError) as excinfo:
        export_mod.build_archive(plan, target)
    assert "空间不足" in str(excinfo.value)
    assert not target.exists(), "拒绝了却留下了半成品"


def test_cancel_deletes_the_partial_archive(env) -> None:
    plan = export_mod.plan_export(env["db"], asset_ids=[1, 2, 3, 4], label="树树")
    target = env["settings"].exports_dir / "取消.zip"
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1          # 放过第一个文件，之后叫停

    result = export_mod.build_archive(plan, target, stop=stop)
    assert result.cancelled is True
    assert not target.exists(), "取消了却留下半成品"


def test_safe_label_cannot_escape_the_output_directory() -> None:
    assert export_mod.safe_label("../../evil") == "evil"
    assert export_mod.safe_label("树树/2025") == "树树_2025"
    assert export_mod.safe_label("   ") == "打包"
    assert "/" not in export_mod.safe_label("a\\b/c:d*e?f")


# ============================================================ C. 走 HTTP：计划 / 入队 / 路径

@pytest.fixture()
def client(env):
    with use_config_path(None):
        app = create_app(env["settings"])
        with TestClient(app) as test_client:
            test_client.db = env["db"]          # type: ignore[attr-defined]
            test_client.env = env               # type: ignore[attr-defined]
            # 一个人物 + 一个主题，好让 kind=person / kind=collection 都测得到
            with env["db"].write() as conn:
                conn.execute("INSERT INTO shot (id, captured_at, capture_date_local,"
                             " created_at, updated_at) VALUES (1, 't', 't', 't', 't')")
                conn.execute("INSERT INTO shot (id, captured_at, capture_date_local,"
                             " created_at, updated_at) VALUES (2, 't', 't', 't', 't')")
                conn.execute("UPDATE asset SET shot_id = 1 WHERE id IN (1, 2, 3)")
                conn.execute("UPDATE asset SET shot_id = 2 WHERE id = 4")
                conn.execute("UPDATE shot SET primary_asset_id = 1 WHERE id = 1")
                conn.execute("UPDATE shot SET primary_asset_id = 4 WHERE id = 2")
                conn.execute("INSERT INTO person (id, uuid, name, status, created_at,"
                             " updated_at) VALUES (1, 'p1', '树树', 'named', 't', 't')")
                conn.execute("INSERT INTO face (id, asset_id, shot_id, detector,"
                             " detector_version, detect_max, preview_w, preview_h, box_x,"
                             " box_y, box_w, box_h, det_score, face_px, created_at)"
                             " VALUES (1, 1, 1, 'yunet', 'v1', 900, 100, 100, 1, 2, 30, 30,"
                             " 0.9, 30, 't')")
                conn.execute("INSERT INTO person_face (person_id, face_id, source,"
                             " created_at, updated_at) VALUES (1, 1, 'user', 't', 't')")
                conn.execute("INSERT INTO collection (id, uuid, name, created_at, updated_at)"
                             " VALUES (1, 'c1', '蓝调时刻', 't', 't')")
                conn.execute("INSERT INTO collection_item (collection_id, shot_id,"
                             " sort_order, added_at) VALUES (1, 1, 0, 't')")
                conn.execute("INSERT INTO collection_item (collection_id, shot_id,"
                             " sort_order, added_at) VALUES (1, 2, 1, 't')")
            yield test_client


def test_plan_endpoint_reports_count_and_output_dir(client: TestClient) -> None:
    payload = client.post("/api/exports/plan",
                          json={"kind": "person", "id": 1}).json()
    assert payload["count"] == 1 and payload["label"] == "树树"
    assert payload["output_dir"].endswith("exports")
    assert payload["missing"] == 0

    collection = client.post("/api/exports/plan",
                             json={"kind": "collection", "id": 1}).json()
    # 主题里两组，每组取代表照片 → 2 张
    assert collection["count"] == 2 and collection["label"] == "蓝调时刻"

    shots = client.post("/api/exports/plan",
                        json={"kind": "shots", "shot_ids": [1, 2]}).json()
    assert shots["count"] == 2

    assert client.post("/api/exports/plan",
                       json={"kind": "nope"}).status_code == 422
    assert client.post("/api/exports/plan",
                       json={"kind": "person", "id": 999}).status_code == 404


def test_export_endpoint_enqueues_a_job_that_really_packs(client: TestClient) -> None:
    """★ 走完整的 HTTP → 任务 → 产物这条路，验证 zip 真的能读出来。"""
    created = client.post("/api/exports", json={"kind": "person", "id": 1}).json()
    assert created["count"] == 1 and created["job"]["type"] == "export"

    env = client.env        # type: ignore[attr-defined]
    queue = JobQueue(env["db"])
    job = queue.get(int(created["job"]["id"]))
    report = JobRunner(env["db"], env["settings"]).run(job)
    assert report.status == "COMPLETED", report.error
    out = Path(report.stages["输出"])
    assert out.is_file() and out.parent == env["settings"].exports_dir
    with zipfile.ZipFile(out) as archive:
        assert any(n.endswith("DSC_0001.JPG") for n in archive.namelist())


def test_export_of_an_offline_library_is_a_clean_conflict(client: TestClient) -> None:
    """原片都不在盘上时：说清楚原因（409），**不要**生成一个空包。"""
    env = client.env        # type: ignore[attr-defined]
    for path in env["files"].values():
        path.unlink()
    response = client.post("/api/exports", json={"kind": "collection", "id": 1})
    assert response.status_code == 409
    assert "不在盘上" in response.json()["detail"]
    assert not list(env["settings"].exports_dir.glob("*.zip"))


def test_asset_path_endpoint_gives_the_real_location(client: TestClient) -> None:
    """★ "引导到原文件的路径"：界面拿到的是绝对路径 + 存在与否。"""
    payload = client.get("/api/assets/1/path").json()
    env = client.env        # type: ignore[attr-defined]
    expected = env["root"] / "杭州市/2025-10-25_1000-1100/DSC_0001.JPG"
    assert Path(payload["absolute_path"]) == expected
    assert payload["relative_path"] == "杭州市\\2025-10-25_1000-1100\\DSC_0001.JPG"
    assert payload["folder"] == str(expected.parent)
    assert payload["exists"] is True
    assert payload["library_name"] == "打包测试库"

    expected.unlink()
    offline = client.get("/api/assets/1/path").json()
    assert offline["exists"] is False, "文件不在盘上时必须如实说"
    assert client.get("/api/assets/999/path").status_code == 404


def test_reveal_asks_the_file_manager_to_select_the_file(client: TestClient, monkeypatch) -> None:
    """★ 只读操作：让文件管理器**定位到**这个文件（选中它），不动它。

    Windows 上优先走 shell 接口 `SHOpenFolderAndSelectItems`；那条路径失败时
    退回命令行形式。这里两条都测：先让 shell 接口失败，看兜底的命令行对不对；
    再让它成功，看还会不会多此一举地起进程。
    """
    import subprocess

    from photoos.api import app as api_app

    calls: list[str] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append(argv if isinstance(argv, str) else " ".join(argv))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(api_app, "_win_shell_select", lambda _path: False)   # 强制走兜底
    env = client.env        # type: ignore[attr-defined]
    expected = env["root"] / "杭州市/2025-10-25_1000-1100/DSC_0001.JPG"

    payload = client.post("/api/assets/1/reveal").json()
    assert payload["revealed"] is True and payload["exists"] is True
    assert payload["how"] == "explorer-select", payload
    assert len(calls) == 1
    # ★ 关键：**整个命令行是一个字符串**，引号只包住路径本身。
    # 写成 list 时 Python 会把 `/select,<含空格的路径>` 整个加引号，
    # 而 explorer.exe 的解析是非标准的 —— 那种形式会打开"文档"而不是定位文件。
    assert calls[0].startswith("explorer /select,"), calls[0]
    assert f'"{expected}"' in calls[0], calls[0]

    # 文件没了 → 退而打开它所在的目录（用户还是得到得了那个位置）
    expected.unlink()
    fallback = client.post("/api/assets/1/reveal").json()
    assert fallback["how"] == "startfile-folder"
    assert client.post("/api/assets/999/reveal").status_code == 404


def test_reveal_prefers_the_shell_api_when_it_works(client: TestClient, monkeypatch) -> None:
    """★ shell 接口成功时**不许**再起 explorer 进程（两条路不能都跑）。"""
    import subprocess

    from photoos.api import app as api_app

    calls: list[str] = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: calls.append(str(argv)))
    monkeypatch.setattr(api_app, "_win_shell_select", lambda _path: True)

    payload = client.post("/api/assets/1/reveal").json()
    assert payload["how"] == "shell-select", payload
    assert calls == [], f"shell 接口成功了却还起了进程：{calls}"


def test_packaging_never_touches_the_originals_through_http(client: TestClient,
                                                            monkeypatch) -> None:
    """★ 端点级的硬守卫：plan / export 全程**只读**原片目录。

    与人物端点的写法相同（Phase 2/4 都有这一条）：把 `open` 换成间谍，
    断言没有任何一次是写模式打开照片库里的文件。
    """
    writes: list[str] = []
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            writes.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    client.post("/api/exports/plan", json={"kind": "collection", "id": 1})
    created = client.post("/api/exports", json={"kind": "collection", "id": 1}).json()
    env = client.env        # type: ignore[attr-defined]
    JobRunner(env["db"], env["settings"]).run(
        JobQueue(env["db"]).get(int(created["job"]["id"])))
    monkeypatch.undo()

    library_root = str(env["root"])
    offenders = [path for path in writes if library_root in path]
    assert not offenders, f"打包过程写了照片库里的文件：{offenders[:3]}"


def test_export_job_without_assets_is_a_clean_failure(env) -> None:
    """任务本身也要挡住坏输入（不能靠调用方永远记得传对）。"""
    queue = JobQueue(env["db"])
    job = queue.create("export", library_id=env["library"].id, scope={})
    report = JobRunner(env["db"], env["settings"]).run(job)
    # 队列对失败会重试（`max_retries`），所以状态可能是 RETRY —— 关键是**没成功**、
    # 而且原因说得明白，而不是安静地打出一个空包
    assert report.status != "COMPLETED"
    assert "asset_ids" in (report.error or "")


def test_job_scope_is_json_serializable_for_the_queue(client: TestClient) -> None:
    """入队时写进 `job.scope` 的东西必须是合法 JSON（否则任务永远跑不起来）。"""
    created = client.post("/api/exports", json={"kind": "person", "id": 1}).json()
    env = client.env        # type: ignore[attr-defined]
    row = env["db"].query_one("SELECT scope_json FROM job WHERE id = ?",
                              (int(created["job"]["id"]),))
    scope = json.loads(row["scope_json"])
    assert scope["asset_ids"] == [1] and scope["kind"] == "person"
