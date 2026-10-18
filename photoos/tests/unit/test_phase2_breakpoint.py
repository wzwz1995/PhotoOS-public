"""Phase 2 · M8：PRD §9.2 九项断点验收的执行器测试。

两条最重要的纪律（都要在测试里守住）：
  ① **不会假装通过** —— 缺素材（手机照片）、缺物理动作（拔盘）、缺第二台电脑时，
     状态必须是「需人工执行」而**不是**「通过」。
  ② **前置未满足时说清楚** —— schema 未升级、`shot` 表为空时给出可操作的提示，
     而不是抛异常或给出一份"全绿"的假报告。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoos.breakpoint import (
    FAIL,
    MANUAL,
    PASS,
    Phase2BreakpointRunner,
    _time_adjacent_groups,
)
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from photoos.library import selection as sel
from photoos.repos import CollectionRepo, SelectionRepo, ShotRepo


def _settings(tmp_path: Path) -> Settings:
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    return settings


@pytest.fixture()
def env(tmp_path: Path):
    """一个 schema 6 + 已登记 Library + 已回填 session/shot/scene 的库（含一个 30 Shot 的 Scene）。"""
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)

    root = tmp_path / "library"
    (root / "杭州市" / "2026-02-19_0947-1749").mkdir(parents=True)

    with db.write() as conn:
        # ★ 必须真的登记 Library —— 否则 `store.get(1)` 抛 LibraryNotFound，
        #   后面每一项都会连带失败（这是写这个夹具时踩到的坑）。
        conn.execute(
            "INSERT INTO library (id,uuid,name,created_at,status,root_path_hint)"
            " VALUES (1,'brk','断点测试库','t','ONLINE', ?)", (str(root),)
        )
        conn.execute(
            "INSERT INTO session (id,name,city_hint,period_hint,captured_from,created_at,updated_at)"
            " VALUES (1,'杭州市\\2026-02-19_0947-1749','杭州市','2026-02-19_0947-1749',"
            "'2026-02-19 09:47:06','t','t')"
        )
        conn.execute(
            "INSERT INTO scene (id,session_id,name,captured_from,captured_to,created_at,updated_at)"
            " VALUES (1,1,'2026-02-19 09:47','2026-02-19 09:47:00','2026-02-19 10:30:00','t','t')"
        )
        for i in range(1, 31):     # 30 个 Shot → 落在 PRD 的 20–50 区间
            # 前 8 个排成 **2 个竞争组**（各 4 张、1 秒间隔）；第 9–30 个每 2 分钟一张 → 各自成组。
            # 因此相似组 = 2 + 22 = **24 组**（决策单元 24）。
            if i <= 8:
                minute, second = 47, (i - 1) % 4
            else:
                minute, second = 47 + (i - 8) * 2, 0
            when = f"2026-02-19 {9 + minute // 60:02d}:{minute % 60:02d}:{second:02d}"
            rel = f"杭州市\\2026-02-19_0947-1749\\DSC_{9000 + i}.JPG"
            path = root / rel.replace("\\", "/")
            path.write_bytes(b"\xff\xd8\xff" + bytes([i]) * 40)   # 极小的假 JPEG
            conn.execute(
                "INSERT INTO asset (id,library_id,relative_path,file_type,status,size,mtime_ns,first_seen_at)"
                " VALUES (?,1,?,'jpeg','OK',?,?, 't')",
                (i, rel, path.stat().st_size, path.stat().st_mtime_ns),
            )
            conn.execute(
                "INSERT INTO shot (id,scene_id,primary_asset_id,captured_at,capture_date_local,"
                "group_source,group_confidence,created_at,updated_at)"
                " VALUES (?,1,?,?,'2026-02-19','auto',?,'t','t')",
                (i, i, when, 0.4 if i <= 8 else 0.95),
            )
            conn.execute("UPDATE asset SET shot_id=? WHERE id=?", (i, i))
            conn.execute(
                "INSERT INTO preview (asset_id,kind,path,width,height,bytes,generator_version,created_at)"
                " VALUES (?,'thumb',?,320,213,1000,'p1.0','t')", (i, f"thumbnails/{i}.webp")
            )
    return settings, db, root


def _run(env) -> dict:
    settings, db, _root = env
    report = Phase2BreakpointRunner(db, settings, 1).run()
    return {case.number: case for case in report.cases}


# ============================================================ 前置条件

def test_missing_schema6_is_reported_not_crashed(tmp_path: Path) -> None:
    """schema 未升级时给出**可操作**的失败，而不是抛异常或假装全绿。"""
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)
    with db.write() as conn:                       # 把 006 建的表删掉，模拟"库还是 schema 5"
        conn.execute("INSERT INTO library (uuid,name,created_at,status)"
                     " VALUES ('x','X','t','ONLINE')")
        conn.execute("DROP TABLE collection_item")
        conn.execute("DROP TABLE collection")
        conn.execute("DROP TABLE embedding")
    report = Phase2BreakpointRunner(db, settings, 1).run()
    assert report.phase == 2
    assert len(report.cases) == 1
    case = report.cases[0]
    assert case.number == 0 and case.status == FAIL
    assert "photoos init" in case.detail


def test_all_nine_cases_present(env) -> None:
    cases = _run(env)
    assert set(cases) == {1, 2, 3, 4, 5, 6, 7, 8, 9}


def test_report_marks_phase2(env) -> None:
    settings, db, _root = env
    report = Phase2BreakpointRunner(db, settings, 1).run()
    assert report.phase == 2
    md = report.to_markdown()
    assert "Phase 2" in md
    assert "§9.2" in md
    assert "Phase 1" not in md.split("\n")[0]


# ============================================================ #1 真实场景

def test_case1_passes_with_a_20_to_50_shot_scene(env) -> None:
    case = _run(env)[1]
    assert case.status == PASS
    assert "30" in case.detail


def test_case1_fails_when_no_scene_in_band(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)
    with db.write() as conn:
        conn.execute("INSERT INTO library (uuid,name,created_at,status) VALUES ('x','X','t','ONLINE')")
    case = Phase2BreakpointRunner(db, settings, 1).run().cases[0]
    assert case.status == FAIL
    assert "shot" in case.detail or "区间" in case.detail


# ============================================================ #2 ≤10 分钟

def test_case2_reports_predicted_time_and_stays_manual(env) -> None:
    """★ 外推落在预算内时状态是**需人工**，不是通过 —— 真人计时不能由外推替代。"""
    case = _run(env)[2]
    assert case.status == MANUAL
    assert "分钟" in case.detail and "外推" in case.detail
    assert case.steps, "必须给出可照做的计时步骤"


def test_case2_decision_units_are_group_count(env) -> None:
    """决策单元数 = 相似组数（组才是决策单元）。

    30 个 Shot：前 8 个是 **2 组**（各 4 张、1 秒间隔），后 22 个每 2 分钟一张 → 各自成组，
    合计 **23 组**。
    """
    case = _run(env)[2]
    assert "相似组 23 个" in case.detail


def test_case2_fails_when_prediction_exceeds_budget(env) -> None:
    """★ 反向验证：如果外推超过 10 分钟，必须是 FAIL —— 证明这个判据真的会咬。

    注意断点 #2 的统计口径是**库内最大的 Scene**（而不是只看落在 20–50 区间的），
    因此往同一个 Scene 里继续加互不相邻的 Shot 就会把它推出预算。
    """
    settings, db, _root = env
    # 再加 200 个**彼此间隔 3 分钟**的 Shot（各自成组）→ 决策单元数远超 10 分钟预算。
    # 必须单调递增：早期版本用 `i % 60` 循环分钟数，反而把 Shot 挤成若干簇，
    # 组数没上去、判定自然还是"落在预算内"（测试「因为错误的理由」失败）。
    with db.write() as conn:
        for offset, i in enumerate(range(100, 300)):
            minute = 40 + offset * 3
            when = f"2026-02-19 {10 + minute // 60:02d}:{minute % 60:02d}:00"
            conn.execute(
                "INSERT INTO asset (id,library_id,relative_path,file_type,status,first_seen_at)"
                " VALUES (?,1,?,'jpeg','OK','t')", (i, f"杭州市\\p\\DSC_{i}.JPG"))
            conn.execute(
                "INSERT INTO shot (id,scene_id,primary_asset_id,captured_at,capture_date_local,"
                "created_at,updated_at) VALUES (?,1,?,?, '2026-02-19','t','t')",
                (i, i, when))
            conn.execute("UPDATE asset SET shot_id=? WHERE id=?", (i, i))
    case = _run(env)[2]
    assert case.status == FAIL
    assert "超过 10 分钟" in case.detail
    assert "超出 PRD 的 20–50 区间" in case.detail


# ============================================================ #3 相似聚组

def test_case3_passes_when_high_confidence_groups_exist(env) -> None:
    case = _run(env)[3]
    assert case.status == PASS
    assert "0.85" in case.detail


def test_case3_fails_without_shots(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    apply_migrations(db, backup_dir=settings.backups_dir)
    with db.write() as conn:
        conn.execute("INSERT INTO library (uuid,name,created_at,status) VALUES ('x','X','t','ONLINE')")
    assert Phase2BreakpointRunner(db, settings, 1).run().cases[2].status == FAIL


def test_case3_counts_user_frozen_shots(env) -> None:
    settings, db, _root = env
    ShotRepo(db).mark_user_adjusted(1)
    case = Phase2BreakpointRunner(db, settings, 1).run().cases[2]
    assert "用户手工调整" in case.detail


# ============================================================ #4 不碰原片

def test_case4_passes_when_files_untouched(env) -> None:
    case = _run(env)[4]
    assert case.status == PASS
    assert "变化 0" in case.detail


def test_case4_detects_a_modified_file(env) -> None:
    """★ 反向验证：改一个原片的内容与大小，必须被断点 #4 抓到。

    用夹具里**真实存在**的文件名（DSC_9001.JPG）——
    早期版本写的是 DSC_9005.JPG，那个文件只存在于另一个夹具里，
    结果是改了个不存在的路径、断点自然抓不到，测试「因为错误的理由」失败。
    """
    settings, db, root = env
    target = root / "杭州市" / "2026-02-19_0947-1749" / "DSC_9001.JPG"
    assert target.is_file(), "夹具里应当有这个文件"
    target.write_bytes(b"\xff\xd8\xff" + b"CHANGED" * 100)
    case = Phase2BreakpointRunner(db, settings, 1).run().cases[3]
    assert case.status == FAIL
    assert "变化 1" in case.detail


def test_case4_marks_manual_when_library_offline(env) -> None:
    settings, db, root = env
    with db.write() as conn:
        conn.execute("UPDATE library SET root_path_hint = 'Z:\\nonexistent', status='OFFLINE'")
    case = Phase2BreakpointRunner(db, settings, 1).run().cases[3]
    assert case.status == MANUAL
    assert case.steps


# ============================================================ #5 手机 + 相机（缺素材）

def test_case5_is_manual_when_only_one_camera_model(env) -> None:
    """★ 本库实测只有一台机身 —— 必须标「需人工」，**不能拿"结构上支持"充数**。"""
    case = _run(env)[5]
    assert case.status == MANUAL
    assert "没有手机" in case.detail or "无素材" in case.detail
    assert case.steps, "必须给出「需要什么素材」的可照做步骤"


def test_case5_passes_when_phone_photos_exist(env) -> None:
    settings, db, _root = env
    with db.write() as conn:
        conn.execute("INSERT INTO metadata (asset_id, camera_make, camera_model) VALUES (1,'Apple','iPhone 15 Pro')")
    case = _run(env)[5]           # 按键取值，不要用列表下标（cases[5] 是第 6 项）
    assert case.status == PASS
    assert "iPhone" in case.detail


def test_case5_recognises_common_phone_brands(env) -> None:
    settings, db, _root = env
    for asset_id, model in ((1, "Xiaomi 14"), (2, "HUAWEI Mate 60"), (3, "Pixel 8")):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO metadata (asset_id, camera_make, camera_model) VALUES (?,?,?)",
                (asset_id, model.split()[0], model),
            )
    assert _run(env)[5].status == PASS


# ============================================================ #6 重开后保留

def test_case6_is_manual_without_any_selection(env) -> None:
    """没有精选记录时不能判"通过" —— 那等于验证了一个空集合。"""
    case = _run(env)[6]
    assert case.status == MANUAL
    assert "精选记录" in case.detail


def test_case6_passes_after_selections_exist(env) -> None:
    settings, db, _root = env
    SelectionRepo(db).set_status(1, sel.FINAL, reason="光最好")
    cases = Phase2BreakpointRunner(db, settings, 1).run()
    case = next(c for c in cases.cases if c.number == 6)
    assert case.status == PASS
    assert "selection 1 行" in case.detail


def test_case6_reads_through_a_fresh_connection(env) -> None:
    """用新连接读同一份数据 = 模拟"重开程序"（SQLite 无内存态，等价）。"""
    settings, db, _root = env
    SelectionRepo(db).set_status(1, sel.CANDIDATE)
    settings, db, _root = env
    fresh = Database(settings.db_path)
    with fresh.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM selection").fetchone()[0] == 1


# ============================================================ #7 离线

def test_case7_passes_and_restores_status(env) -> None:
    """★ 断点 #7 的自动化部分；跑完必须把 Library 状态**恢复回 ONLINE**（不留副作用）。"""
    settings, db, _root = env
    case = _run(env)[7]           # 按键取值（cases[7] 是第 8 项）
    assert case.status == PASS
    assert case.steps, "物理拔盘必须人工"
    with db.read() as conn:
        status = conn.execute("SELECT status FROM library WHERE id=1").fetchone()[0]
    assert status == "ONLINE", "断点测试不该把库留在 OFFLINE 状态"


def test_case7_detail_mentions_thumbnails(env) -> None:
    assert "缩略图" in _run(env)[7].detail


# ============================================================ #8 反查拍摄组

def test_case8_is_manual_without_finals(env) -> None:
    case = _run(env)[8]
    assert case.status == MANUAL
    assert "FINAL" in case.detail


def test_case8_passes_with_traceable_final(env) -> None:
    settings, db, _root = env
    SelectionRepo(db).set_status(5, sel.FINAL)
    case = _run(env)[8]
    assert case.status == PASS
    assert "FINAL 1 条" in case.detail


def test_case8_fails_when_scene_link_is_broken(env) -> None:
    """★ 反向验证：把 Shot 从 Scene 摘掉后，反查链断 —— 必须 FAIL。"""
    settings, db, _root = env
    SelectionRepo(db).set_status(5, sel.FINAL)
    with db.write() as conn:
        conn.execute("UPDATE shot SET scene_id = NULL WHERE id = 5")
    case = _run(env)[8]
    assert case.status == FAIL
    assert "断链" in case.detail


# ============================================================ #9 迁移回归

def test_case9_is_manual_without_phase2_data(env) -> None:
    case = _run(env)[9]
    assert case.status == MANUAL
    assert "增量数据" in case.detail or "精选或主题" in case.detail


def test_case9_exports_and_verifies_package(env) -> None:
    """有增量数据后应真的导出迁移包并核对：含 photoos.db/manifest、**不含照片原文件**。"""
    settings, db, _root = env
    SelectionRepo(db).set_status(1, sel.FINAL)
    CollectionRepo(db).create("蓝调时刻")
    case = _run(env)[9]
    assert case.status == PASS
    assert "不含照片原文件=True" in case.detail
    assert case.steps, "实机 PC-B 必须人工"
    # 断点测试不该留下大文件
    leftovers = list(Path(settings.backups_dir).glob("phase2-breakpoint-package*"))
    assert not leftovers, f"断点测试留下了包文件：{leftovers}"


# ============================================================ 时间邻接切组（与前端同口径）

def test_time_adjacent_groups_matches_frontend_semantics() -> None:
    """断点 #2 的外推必须基于**用户实际看到的组数**，因此口径要与前端一致（2 秒阈值）。"""
    stamps = [
        "2026-02-19 09:47:00", "2026-02-19 09:47:01", "2026-02-19 09:47:02",  # 一组
        "2026-02-19 09:50:00",                                                  # 间隔 178s → 新组
        "2026-02-19 09:50:01",
    ]
    groups = _time_adjacent_groups(stamps, 2.0)
    assert [len(g) for g in groups] == [3, 2]


def test_time_adjacent_groups_handles_missing_timestamps() -> None:
    groups = _time_adjacent_groups(["2026-02-19 09:47:00", "", "2026-02-19 09:47:01"], 2.0)
    # 无时间的不能与别人"相邻" → 各自成组
    assert len(groups) == 3


def test_time_adjacent_groups_empty() -> None:
    assert _time_adjacent_groups([], 2.0) == []
