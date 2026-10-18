"""Phase 4 · 分人与搜索反馈的 HTTP 端点测试（走真实 FastAPI 应用）。

为什么必须单独测一层 HTTP：这些端点的**返回形状**是界面的唯一契约。
形状错了，`library/*.py` 里的单元测试全绿，界面照样一片空白 ——
本项目已经吃过一次"功能在、路不通"（交付文档 §12.4）。

覆盖三件事：
  A. 人物：确认/否决之后原型**当场**更新（不需要重启服务，也不需要命令行）；
  B. 搜索反馈：表态之后**下一条结果请求**就换了排序；
  C. 承诺边界：确认/否决/删除都**不许**动到照片文件，也不许降低人脸数量。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photoos.api import create_app
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations
from tests.unit.route_guard import destructive_hits

MODEL = "sface-2021dec"
MODEL_VERSION = "cv2-5.0.0-fp32"
PREP = "sf-112-align-p1.0"
DIM = 4


def _vec(*values: float) -> bytes:
    arr = np.asarray(values, dtype="f4")
    return (arr / np.linalg.norm(arr)).astype("<f4").tobytes()


@pytest.fixture()
def client(tmp_path: Path):
    data_root = tmp_path / "data"
    with use_config_path(None):
        settings = Settings(data_root=data_root)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)
        _seed(db)
        app = create_app(settings)
        with TestClient(app) as test_client:
            test_client.db = db          # type: ignore[attr-defined]
            test_client.root = data_root  # type: ignore[attr-defined]
            yield test_client


def _seed(db: Database) -> None:
    with db.write() as conn:
        conn.execute("INSERT INTO library (uuid, name, created_at, status, root_path_hint)"
                     " VALUES ('lib','测试库','t','ONLINE','Z:\\不存在的路径')")
        conn.execute("INSERT INTO session (id, name, created_at, updated_at)"
                     " VALUES (1,'城\\时段','t','t')")
        conn.execute("INSERT INTO scene (id, session_id, name, created_at, updated_at)"
                     " VALUES (1,1,'S','t','t')")
        # 3 张照片 / 3 个 Shot，各带一张预览与一个人脸
        for shot_id in (1, 2, 3):
            conn.execute("INSERT INTO asset (id, library_id, relative_path, file_type,"
                         " status, first_seen_at) VALUES (?, 1, ?, 'jpeg', 'OK', 't')",
                         (shot_id, f"城\\时段\\DSC_{shot_id:04d}.JPG"))
            conn.execute("INSERT INTO shot (id, scene_id, primary_asset_id, captured_at,"
                         " capture_date_local, created_at, updated_at)"
                         " VALUES (?, 1, ?, '2026-01-01 10:0' || ?, '2026-01-01','t','t')",
                         (shot_id, shot_id, shot_id))
            conn.execute("UPDATE asset SET shot_id = ? WHERE id = ?", (shot_id, shot_id))
            conn.execute("INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
                         " generator_version, created_at)"
                         " VALUES (?, 'preview', ?, 200, 200, 1000, 'p1.0', 't')",
                         (shot_id, f"previews/{shot_id}.webp"))
            conn.execute(
                "INSERT INTO face (id, asset_id, shot_id, detector, detector_version,"
                " detect_max, preview_w, preview_h, box_x, box_y, box_w, box_h, det_score,"
                " face_px, is_frontal, created_at)"
                " VALUES (?, ?, ?, 'yunet-2023mar', 'cv2-5.0.0', 900, 200, 200, 40, 40, 60, 60,"
                " 0.95, 60, 1, 't')", (shot_id, shot_id, shot_id))
            conn.execute(
                "INSERT INTO face_embedding (face_id, model, model_version, prep_version, dim,"
                " normalization, vector, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'l2', ?, 't')",
                (shot_id, MODEL, MODEL_VERSION, PREP, DIM,
                 _vec(*(1.0 if i == shot_id - 1 else 0.0 for i in range(DIM)))))
        # 一个已命名的人，名下已有第 1 张脸（用户确认）
        conn.execute("INSERT INTO person (id, uuid, name, status, created_at, updated_at)"
                     " VALUES (1, 'p1', '妈妈', 'named', 't', 't')")
        conn.execute("INSERT INTO person_face (person_id, face_id, source, created_at,"
                     " updated_at) VALUES (1, 1, 'user', 't', 't')")
        # 一个未命名的候选（模型提出来的）
        conn.execute("INSERT INTO person (id, uuid, name, status, created_at, updated_at)"
                     " VALUES (2, 'p2', NULL, 'proposed', 't', 't')")
        conn.execute("INSERT INTO person_face (person_id, face_id, source, created_at,"
                     " updated_at) VALUES (2, 3, 'auto', 't', 't')")
        # 语义搜索：一个词 + 三条命中
        conn.execute(
            "INSERT INTO semantic_query (id, query_text, prompt_text, text_kind, model,"
            " model_version, dim, normalization, vector, created_at, updated_at)"
            " VALUES (1, '日出', 'sunrise', 'query', 'embeddinggemma-2', 'safetensors-fp32', 4,"
            " 'l2', ?, 't', 't')", (_vec(1.0, 0.0, 0.0, 0.0),))
        for rank, shot_id in enumerate((1, 2, 3)):
            # 分数必须是**真实余弦**（文本朝第 1 个轴，三张图分别朝三个轴）。
            # 随手编一组递减的分会让"重排后 base_score 与 score 的关系"这条断言失去意义 ——
            # 那正是上一版测试没能发现问题的原因。
            conn.execute(
                "INSERT INTO semantic_query_hit (query_id, shot_id, rank, score, computed_at)"
                " VALUES (1, ?, ?, ?, 't')", (shot_id, rank, 1.0 if shot_id == 1 else 0.0))
        # 搜索重排要用的图像向量（与文本同一身份，见 semantic_rank.resolve_image_identity）
        for shot_id in (1, 2, 3):
            conn.execute(
                "INSERT INTO embedding (asset_id, model, model_version, dim, normalization,"
                " input_kind, input_version, prep_version, vector, created_at)"
                " VALUES (?, 'embeddinggemma-2', 'safetensors-fp32', 4, 'l2', 'preview',"
                " 'p1.0', 'eg2-p1.0', ?, 't')",
                (shot_id, _vec(*(1.0 if i == shot_id - 1 else 0.0 for i in range(DIM)))))


# ============================================================ A. 人物

def test_people_list_shape(client: TestClient) -> None:
    payload = client.get("/api/people").json()
    assert payload["total"] == 2
    assert payload["stats"]["named"] == 1
    assert payload["faces"]["faces"] == 3 and payload["faces"]["embedded"] == 3
    names = {item["id"]: item["name"] for item in payload["items"]}
    assert names[1] == "妈妈" and names[2] is None, "未命名的人也必须出现在列表里"
    assert payload["unassigned"] == 1, "第 2 张脸还没有归属"


def test_people_api_resolves_identity_from_the_database(client: TestClient,
                                                        monkeypatch) -> None:
    """★ 回归（真实发生过的 bug）：界面用的特征身份必须**从库里读**。

    早先这里用的是 `faces.model_version()`，而它的取值是 `cv2-<版本>-fp32`。
    产品 venv **没有装 cv2**（模型侧只在命令行跑），于是它退化成
    `cv2-unknown-fp32`，界面按这个身份去查一条特征都查不到 ——
    命令行一切正常，人物页却永远显示"0 张脸有特征"。

    这条测试把 `model_version()` 换成一个必然对不上的值：
    只要有人把读取路径改回"现场重算"，它立刻变红。
    """
    import photoos.library.faces as faces_mod

    monkeypatch.setattr(faces_mod, "model_version", lambda: "cv2-BOGUS-fp32")
    payload = client.get("/api/people").json()
    assert payload["faces"]["embedded"] == 3, "身份不能依赖 cv2 是否存在"
    assert payload["identity"]["model_version"] == MODEL_VERSION


def test_face_identity_falls_back_gracefully_on_an_empty_library(client: TestClient) -> None:
    """一条特征都没有时也要能给个诚实的 0，而不是 500。"""
    with client.db.write() as conn:   # type: ignore[attr-defined]
        conn.execute("DELETE FROM face_embedding")
    payload = client.get("/api/people").json()
    assert payload["faces"]["embedded"] == 0
    assert payload["unassigned"] == 0


def test_people_detail_reports_counts_and_profile(client: TestClient) -> None:
    payload = client.get("/api/people/1").json()
    assert payload["person"]["name"] == "妈妈"
    assert payload["counts"] == {"positive": 1, "auto": 0, "negative": 0}
    assert [f["face_id"] for f in payload["faces"]] == [1]
    assert payload["profile"] is None, "还没学习过时不该编一个原型出来"
    assert "vector" not in (payload["profile"] or {})


def test_confirming_a_face_updates_the_profile_immediately(client: TestClient) -> None:
    """★ 界面上点一下"是他"，**这一次请求**就要把原型算出来。

    如果原型要等命令行才能更新，"点一下就越准"这个循环根本转不起来。
    """
    before = client.get("/api/people/1").json()
    assert before["profile"] is None
    result = client.post("/api/people/1/faces", json={"face_ids": [2]}).json()
    assert result["changed"] == 1
    assert result["profile"] is not None
    assert result["profile"]["pos_count"] == 2
    assert result["profile"]["threshold"] is not None
    after = client.get("/api/people/1").json()
    assert after["profile"]["pos_count"] == 2
    assert sorted(f["face_id"] for f in after["faces"]) == [1, 2]


def test_rejecting_records_a_negative_and_keeps_the_face(client: TestClient) -> None:
    """否定必须成为**一等事实**（可查、可计数），而且**不能删掉那张脸**。"""
    result = client.post("/api/people/1/reject", json={"face_ids": [3]}).json()
    assert result["changed"] == 1
    assert result["profile"]["neg_count"] == 1
    detail = client.get("/api/people/1").json()
    assert [r["face_id"] for r in detail["rejected"]] == [3]
    # 脸还在（只是被这个人否决了；它可能属于别人）
    listing = client.get("/api/people").json()
    assert listing["faces"]["faces"] == 3, "否决一张脸不该让检出数量下降"


def test_reject_does_not_reduce_the_other_persons_faces(client: TestClient) -> None:
    """person 2 的第 3 张脸在被 person 1 否决后仍然留在 person 2 名下。"""
    client.post("/api/people/1/reject", json={"face_ids": [3]})
    detail = client.get("/api/people/2").json()
    assert [f["face_id"] for f in detail["faces"]] == [3]


def test_confirm_takes_the_face_away_from_the_other_person(client: TestClient) -> None:
    """用户确认之后，模型在别人名下的提议必须被撤掉（一张脸只属于一个人）。"""
    client.post("/api/people/1/faces", json={"face_ids": [3]})
    assert [f["face_id"] for f in client.get("/api/people/2").json()["faces"]] == []
    assert sorted(f["face_id"] for f in client.get("/api/people/1").json()["faces"]) == [1, 3]


def test_suggest_returns_candidates_and_marks_borderline(client: TestClient) -> None:
    client.post("/api/people/1/faces", json={"face_ids": [2]})
    payload = client.post("/api/people/1/suggest", json={"limit": 10}).json()
    assert payload["ready"] is True
    assert payload["considered"] == 1, "只有第 3 张脸还没归属"
    assert [i["face_id"] for i in payload["items"]] == [3]
    assert payload["items"][0]["accepted"] is False, "正交方向相似度低，必须标成候选"
    assert "similarity" in payload["items"][0]


def test_suggest_says_why_it_cannot_run_yet(client: TestClient) -> None:
    """没有原型时给**可照做的提示**，而不是空数组让界面无从解释。"""
    client.post("/api/people", json={"name": "新的人"})       # id = 3
    payload = client.post("/api/people/3/suggest", json={}).json()
    assert payload["ready"] is False
    assert payload["hint"], "必须说明'先确认至少一张脸'"


def test_suggest_never_proposes_a_rejected_face(client: TestClient) -> None:
    """★ 用户否决过的脸**永远不再**被提议给同一个人。"""
    client.post("/api/people/1/faces", json={"face_ids": [2]})
    client.post("/api/people/1/reject", json={"face_ids": [3]})
    payload = client.post("/api/people/1/suggest", json={"limit": 10}).json()
    assert [i["face_id"] for i in payload["items"]] == []


def test_person_can_be_named_and_may_stay_unnamed(client: TestClient) -> None:
    renamed = client.patch("/api/people/2", json={"name": "爸爸"}).json()
    assert renamed["person"]["name"] == "爸爸"
    assert renamed["person"]["status"] == "named"
    # 名字清空之后必须回到"未命名"，而不是留下一个空字符串
    cleared = client.patch("/api/people/2", json={"name": "   "}).json()
    assert cleared["person"]["name"] is None
    assert cleared["person"]["status"] == "proposed"


def test_person_to_collection_creates_then_reuses(client: TestClient) -> None:
    """「一键进专题」：第一次建同名主题，第二次**追加**到同一个主题。"""
    first = client.post("/api/people/1/collection", json={}).json()
    assert first["created"] is True and first["name"] == "妈妈"
    assert first["shots"] == 1
    client.post("/api/people/1/faces", json={"face_ids": [2]})
    second = client.post("/api/people/1/collection", json={}).json()
    assert second["created"] is False and second["collection_id"] == first["collection_id"]
    detail = client.get(f"/api/collections/{first['collection_id']}").json()
    assert detail["count"] == 2
    assert detail["source"]["kind"] == "person"
    assert detail["source"]["person_name"] == "妈妈"


def test_person_without_a_name_cannot_make_a_collection(client: TestClient) -> None:
    payload = client.post("/api/people/2/collection", json={}).json()
    assert payload["created"] is False
    assert payload["hint"], "没名字时必须说清'先起名'，而不是建一个无名主题"


def test_people_learn_all_recomputes_every_profile(client: TestClient) -> None:
    payload = client.post("/api/people/learn", json={}).json()
    assert payload["updated"] >= 2
    ids = {d["person_id"] for d in payload["details"]}
    assert {1, 2} <= ids


def test_face_thumb_is_served_and_404s_cleanly(client: TestClient) -> None:
    """没有人脸时给 404 而不是 500 —— 页面上的破图不该让整个请求栈出错。"""
    assert client.get("/api/faces/1/thumb").status_code == 404
    assert client.get("/api/faces/999/thumb").status_code == 404


def test_unknown_person_is_404(client: TestClient) -> None:
    assert client.get("/api/people/999").status_code == 404
    assert client.post("/api/people/999/faces", json={"face_ids": [1]}).status_code == 404
    assert client.post("/api/people/999/suggest", json={}).status_code == 404


def test_delete_person_requires_confirmation(client: TestClient) -> None:
    """铁律 #5：删人物必须二次确认 —— 没有 `?confirm=true` 一律 428。"""
    response = client.delete("/api/people/2")
    assert response.status_code == 428
    assert "不会删除任何照片文件" in response.json()["detail"]


def test_delete_person_removes_the_label_but_keeps_the_faces(client: TestClient) -> None:
    """★ 删的是"人"这个标签：脸、特征、照片一张都不许少。"""
    before = client.get("/api/people").json()
    assert before["faces"]["faces"] == 3
    deleted = client.delete("/api/people/2?confirm=true").json()
    assert deleted["deleted"] == 2
    after = client.get("/api/people").json()
    assert after["total"] == 1
    assert after["faces"]["faces"] == 3, "删人物把检出的人脸也删了"
    assert after["faces"]["embedded"] == 3, "删人物把特征也删了"
    assert after["stats"]["auto_faces"] == 0
    # 那张脸回到"未归类"，等着被重新指认
    assert after["unassigned"] == 2


def test_delete_person_is_idempotent_from_the_clients_view(client: TestClient) -> None:
    assert client.delete("/api/people/2?confirm=true").status_code == 200
    assert client.delete("/api/people/2?confirm=true").status_code == 404


# ============================================================ B. 搜索反馈

def test_results_carry_the_feedback_map(client: TestClient) -> None:
    payload = client.get("/api/semantic/results?query=日出").json()
    assert [i["shot_id"] for i in payload["items"]] == [1, 2, 3]
    assert payload["feedback"] == {}
    assert payload["feedback_counts"] == {"keep": 0, "drop": 0}
    assert payload["tuned"] is False


def test_dropping_a_shot_removes_it_from_the_next_result(client: TestClient) -> None:
    """★ 一次表态，下一条结果请求就必须换排序（不需要重启、不需要命令行）。

    注意这里**只有一条**表态：硬排除从第一条起就生效，
    而"学习出来的重排"要两条以上才有意义（一条的均值就是那张图本身）。
    """
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_id": 1, "action": "drop"}).json()
    assert saved["counts"] == {"keep": 0, "drop": 1}
    assert saved["tuning"]["active"] is False, "一条表态不足以重排"
    payload = client.get("/api/semantic/results?query=日出").json()
    assert 1 not in [i["shot_id"] for i in payload["items"]], "删掉的那张必须消失"
    assert [i["shot_id"] for i in payload["items"]] == [2, 3], "其余顺序保持模型原样"


def test_two_signals_activate_reranking_and_flip_the_order(client: TestClient) -> None:
    """★ 两条以上表态之后重排生效，用户选中的方向浮上来。

    这是"越用越懂你"这条承诺在 HTTP 层的可测形式。
    """
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 3, "action": "keep"})
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 2, "action": "keep"})
    payload = client.get("/api/semantic/results?query=日出").json()
    assert payload["tuned"] is True
    assert payload["tuning"]["pos_count"] == 2
    per_shot = {i["shot_id"]: i for i in payload["items"]}
    assert per_shot[2]["score"] > per_shot[2]["base_score"], "被选中的方向必须被抬高"
    assert per_shot[1]["score"] == per_shot[1]["base_score"], "没被表态的不该变"


def test_tuning_can_be_switched_off_to_see_the_model_result(client: TestClient) -> None:
    """★ "随时关掉学习看原样"必须是**真的**。"""
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 3, "action": "keep"})
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 2, "action": "keep"})
    raw = client.get("/api/semantic/results?query=日出&tuning=0").json()
    assert raw["tuned"] is False
    assert [i["shot_id"] for i in raw["items"]] == [1, 2, 3], "原始顺序必须可复现"
    marked = {i["shot_id"]: i["feedback"] for i in raw["items"]}
    assert marked[2] == "keep" and marked[3] == "keep", "关掉重排也要能看到自己标过什么"
    assert marked[1] is None, "没表过态的照片不该被标上任何东西"


def test_feedback_can_be_cleared(client: TestClient) -> None:
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 1, "action": "drop"})
    removed = client.delete("/api/semantic/feedback?query_id=1").json()
    assert removed["removed"] == 1
    payload = client.get("/api/semantic/results?query=日出").json()
    assert [i["shot_id"] for i in payload["items"]] == [1, 2, 3]


def test_feedback_on_a_missing_shot_is_a_clean_conflict(client: TestClient) -> None:
    response = client.post("/api/semantic/feedback",
                           json={"query_id": 1, "shot_id": 999, "action": "keep"})
    assert response.status_code == 409
    assert "退役" in response.json()["detail"] or "不存在" in response.json()["detail"]


def test_feedback_validates_its_input(client: TestClient) -> None:
    assert client.post("/api/semantic/feedback",
                       json={"query_id": 1, "shot_id": 1, "action": "maybe"}).status_code == 422
    assert client.post("/api/semantic/feedback",
                       json={"query_id": 1, "action": "keep"}).status_code == 422
    assert client.post("/api/semantic/feedback",
                       json={"query_id": 999, "shot_id": 1, "action": "keep"}).status_code == 404


def test_search_result_can_become_a_collection(client: TestClient) -> None:
    """关键词筛选 → 主题（用户工作流的最后一环）。"""
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 1, "action": "drop"})
    created = client.post("/api/semantic/collection",
                          json={"query_id": 1, "name": "日出合集", "mode": "clean"}).json()
    assert created["created"] is True and created["added"] == 2
    detail = client.get(f"/api/collections/{created['collection_id']}").json()
    assert sorted(i["shot_id"] for i in detail["items"]) == [2, 3]
    assert detail["source"]["kind"] == "query"
    assert detail["source"]["query_text"] == "日出"


def test_collection_mode_raw_ignores_the_edits(client: TestClient) -> None:
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_id": 1, "action": "drop"})
    created = client.post("/api/semantic/collection",
                          json={"query_id": 1, "name": "原始", "mode": "raw"}).json()
    assert created["added"] == 3, "raw 模式必须无视用户的修改"


def test_collection_without_a_name_is_rejected(client: TestClient) -> None:
    response = client.post("/api/semantic/collection", json={"query_id": 1, "name": "  "})
    assert response.status_code == 422


def test_create_collection_can_take_shot_ids_in_one_call(client: TestClient) -> None:
    created = client.post("/api/collections",
                          json={"name": "手工挑的", "shot_ids": [1, 2],
                                "source": {"kind": "manual"}}).json()
    assert created["added"] == 2
    detail = client.get(f"/api/collections/{created['id']}").json()
    assert detail["count"] == 2
    assert detail["source"] == {"kind": "manual"}


# ---------------------------------------------- B2. 一次表态多张（界面的多选）

def test_a_batch_feedback_call_marks_every_shot_at_once(client: TestClient) -> None:
    """★ 多选：一次请求标多张 —— 界面挑了 12 张，就该只发 1 个请求、只重排 1 次。

    上一版只有单张接口，界面多选只能循环发 N 个请求：
    服务端重排 N 次、界面重建 N 次、滚动位置跳 N 次。
    """
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_ids": [2, 3], "action": "keep"}).json()
    assert saved["counts"] == {"keep": 2, "drop": 0}
    assert saved["applied"] == [2, 3]
    assert saved["skipped"] == []
    assert saved["tuning"]["active"] is True, "两张表态就该激活重排"


def test_a_batch_call_recomputes_the_rerank_exactly_once(client: TestClient, monkeypatch) -> None:
    """★ 三张一次提交，只能重算一次 —— 否则"批量"只是把 N 个请求塞进一个 URL。

    这条断言是故意**能被打翻**的：把 `set_feedback_many` 换回逐张循环
    （每次循环里都重算一遍 tuning），计数立刻变成 3。
    """
    from photoos.library import semantic_rank

    calls = {"n": 0}
    original = semantic_rank.compute_tuning

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic_rank, "compute_tuning", counting)
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_ids": [1, 2, 3], "action": "keep"}).json()
    assert saved["applied"] == [1, 2, 3]
    assert calls["n"] == 1, f"一次批量表态重算了 {calls['n']} 次"


def test_batch_feedback_dedupes_but_keeps_the_order(client: TestClient) -> None:
    """重复的 shot 只写一次（表上有 UNIQUE），返回的顺序与用户点选顺序一致。"""
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_ids": [3, 2, 3], "action": "drop"}).json()
    assert saved["applied"] == [3, 2]
    assert saved["counts"] == {"keep": 0, "drop": 2}


def test_marking_the_same_shot_again_does_not_double_count(client: TestClient) -> None:
    """★ 幂等：同一张再点一次「我要这种」，计数**不能**变成 2。

    界面上多选时很容易把已经标过的卡片再框进去一次（真实浏览器验证里就撞上了：
    选中三张、其中一张刚标过，计数只 +2）。表上的 UNIQUE(query, shot) 就是为这个设的，
    否则"改主意"会在库里留下两条互相矛盾的记录。
    """
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_ids": [2], "action": "keep"})
    again = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_ids": [2], "action": "keep"}).json()
    assert again["counts"] == {"keep": 1, "drop": 0}

    # 改主意：keep → drop 是"换一条"，不是"多一条"
    flipped = client.post("/api/semantic/feedback",
                          json={"query_id": 1, "shot_ids": [2], "action": "drop"}).json()
    assert flipped["counts"] == {"keep": 0, "drop": 1}


def test_batch_feedback_drops_every_marked_shot_from_the_next_result(client: TestClient) -> None:
    client.post("/api/semantic/feedback",
                json={"query_id": 1, "shot_ids": [1, 2], "action": "drop"})
    payload = client.get("/api/semantic/results?query=日出").json()
    assert [i["shot_id"] for i in payload["items"]] == [3]


def test_batch_feedback_skips_dead_shots_and_keeps_the_living_ones(client: TestClient) -> None:
    """库里混进已退役的 Shot 时：活的照记，死的报出来，**不整批失败**。

    整批失败会让"用户选了 20 张、其中 1 张刚好被重算过"变成一次白点。
    """
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_ids": [2, 999], "action": "keep"}).json()
    assert saved["applied"] == [2]
    assert saved["skipped"] == [999]
    assert saved["counts"] == {"keep": 1, "drop": 0}


def test_a_batch_of_only_dead_shots_is_a_conflict(client: TestClient) -> None:
    """全死＝没有一件事可做，那就老实报冲突（此时"跳过"会变成静默无操作）。"""
    response = client.post("/api/semantic/feedback",
                           json={"query_id": 1, "shot_ids": [998, 999], "action": "keep"})
    assert response.status_code == 409


def test_batch_feedback_validates_its_input(client: TestClient) -> None:
    def post(shot_ids):
        return client.post("/api/semantic/feedback",
                           json={"query_id": 1, "shot_ids": shot_ids, "action": "keep"})

    assert post([]).status_code == 422, "空数组必须报错，而不是静默成功"
    assert post("1,2").status_code == 422, "字符串不是数组"
    assert post([1, "两个"]).status_code == 422, "数组里混进非整数"
    assert post([1] * 501).status_code == 422, "上限之外的批量要挡住"
    assert post([1] * 500).status_code == 200, "上限之内必须能过"


def test_the_single_shot_form_still_works(client: TestClient) -> None:
    """旧写法（单张 `shot_id`）不能被批量接口挤掉 —— 界面上的每张卡片按钮还在用它。"""
    saved = client.post("/api/semantic/feedback",
                        json={"query_id": 1, "shot_id": 2, "action": "keep"}).json()
    assert saved["applied"] == [2]
    assert saved["counts"] == {"keep": 1, "drop": 0}


# ------------------------------------------- B3. ★ 每一张结果都要能索引到原片

def test_every_search_result_carries_its_source_file(client: TestClient) -> None:
    """★ 找照片的每一条结果都必须带着**原片在哪**（asset_id + 相对路径）。

    界面把这条链路做成卡片底部的"文件夹 + 文件名 → 看原片"。
    少了它，结果就只是一张缩略图：看得出内容，找不到文件。
    """
    payload = client.get("/api/semantic/results?query=日出").json()
    assert payload["items"], "没有结果就测不出这条承诺"
    for item in payload["items"]:
        assert item["asset_id"], f"结果 shot={item['shot_id']} 没有 asset_id"
        assert item["relative_path"], f"结果 shot={item['shot_id']} 没有原片路径"


def test_ranked_results_carry_the_source_file_too(client: TestClient) -> None:
    """★ 重排后的那条路径**也要**带原片信息。

    两条路径（模型原始顺序 / 学习重排）各写一份字段，最容易在这里漂移：
    界面上就会变成"关掉重排看得见文件名、开着重排看不见"。
    """
    client.post("/api/semantic/feedback", json={"query_id": 1, "shot_ids": [2, 3],
                                                "action": "keep"})
    base = client.get("/api/semantic/results?query=日出&tuning=0").json()
    ranked = client.get("/api/semantic/results?query=日出&tuning=1").json()
    assert ranked["tuned"] is True
    required = {"rank", "score", "shot_id", "asset_id", "relative_path", "capture_at",
                "asset_count", "selection_status", "feedback"}
    for name, payload in (("原始", base), ("重排", ranked)):
        assert payload["items"], f"{name}路径没有结果"
        for item in payload["items"]:
            missing = required - set(item)
            assert not missing, f"{name}路径的结果少了字段 {sorted(missing)}"
            assert item["relative_path"], f"{name}路径的结果没有原片路径"


def test_every_face_can_be_traced_back_to_its_photo(client: TestClient) -> None:
    """★ 人物页的三处人脸列表（已确认/提议、模型建议、已否决）都要能追到原片。

    人脸墙上只有一张**裁出来的脸**。没有文件名与入口，用户看得出是谁、
    却找不到那张照片 —— 对"分人"这个用途来说等于没结果。
    """
    with client.db.write() as conn:      # type: ignore[attr-defined]
        conn.execute("INSERT INTO metadata (asset_id, capture_at, source) VALUES (2,"
                     " '2026-01-01 08:30:00', 'pillow')")
        conn.execute("INSERT INTO person_reject (person_id, face_id, created_at)"
                     " VALUES (1, 2, 't')")

    detail = client.get("/api/people/1").json()
    assert detail["faces"] and detail["rejected"]
    for group in ("faces", "rejected"):
        for face in detail[group]:
            assert face["asset_id"], f"{group} 里有一条脸没有 asset_id"
            assert face["relative_path"], f"{group} 里有一条脸没有原片路径"

    # 被否决的脸也要带**拍摄时刻**：决定"其实是他"之前得先看一眼原片是什么时候拍的
    assert detail["rejected"][0]["capture_at"] == "2026-01-01 08:30:00"

    client.post("/api/people/1/faces", json={"face_ids": [3]})
    suggest = client.post("/api/people/1/suggest", json={"limit": 10}).json()
    if suggest.get("items"):
        for item in suggest["items"]:
            assert item["asset_id"] and item["relative_path"], \
                "建议区的人脸也必须能追到原片"
            assert "capture_at" in item, "建议区少了拍摄时刻（界面会显示'无拍摄时间'）"


# ============================================ D. 候选集的四个批量动作（交付后追加）

def test_one_click_confirm_takes_all_the_proposals(client: TestClient) -> None:
    """★ 一键确认：把「模型提议」一次变成你的判断 —— 一次请求、一次重算。"""
    payload = client.post("/api/people/2/confirm-all").json()
    assert payload["confirmed"] == 1
    assert payload["profile"]["pos_count"] == 1, "确认之后必须当场重算判据"
    detail = client.get("/api/people/2").json()
    assert [f["source"] for f in detail["faces"]] == ["user"]
    # 再来一次：没有提议可确认，不能再报一次"确认了 1 张"
    again = client.post("/api/people/2/confirm-all").json()
    assert again["confirmed"] == 0 and again["profile"] is None


def test_dissolve_hides_the_candidate_but_keeps_every_face(client: TestClient) -> None:
    """★ 解散候选集：这一簇不算数，但**一张脸都不许少**。

    解散与删除的区别就在这里。删掉 person 行之后那些脸会回到"没归属"的池子里，
    下一次聚类还会把它们聚成同一坨 —— 那正是用户说的"有的分组里啥都有"。
    """
    before = client.get("/api/people").json()
    assert before["stats"]["auto_faces"] == 1
    dissolved = client.post("/api/people/2/dissolve").json()
    assert dissolved["dissolved"] is True and dissolved["faces"] == 1

    after = client.get("/api/people").json()
    assert [p["id"] for p in after["items"]] == [1], "解散的候选不该还在列表里"
    assert after["stats"]["auto_faces"] == 1, "解散把归属也删了 —— 那脸就会重新被聚起来"
    assert [p["id"] for p in after["archived"]] == [2], "解散过的候选要能被找回来"
    assert client.get("/api/people/2").json()["faces"], "脸上的信息必须还在"

    restored = client.post("/api/people/2/restore").json()
    assert restored["restored"] is True
    # 顺序由"未命名优先"决定，这里只关心它回没回到列表里
    assert sorted(p["id"] for p in client.get("/api/people").json()["items"]) == [1, 2]
    assert client.get("/api/people").json()["archived"] == []


def test_dissolve_refuses_a_person_that_already_has_a_name(client: TestClient) -> None:
    """有名字的人被解散＝从列表消失、脸却还挂在名下 —— 那比"删标签"更难理解。"""
    response = client.post("/api/people/1/dissolve")
    assert response.status_code == 409
    assert "没命名的候选" in response.json()["detail"]
    assert client.post("/api/people/999/dissolve").status_code == 404
    assert client.post("/api/people/999/restore").status_code == 404


def test_dissolved_faces_stay_out_of_clustering_but_are_still_proposable(
        client: TestClient) -> None:
    """★ 解散的**语义**：错的只是"它们是一个人"，不是这些脸本身。

      * `unassigned`（聚类的输入）不含它们 → 不会再被聚成同一坨；
      * `unclaimed`（增量/建议的输入）含它们 → 「再找找像他的」照样找得到。
    """
    from photoos.repos import PeopleRepo

    db = client.db  # type: ignore[attr-defined]
    repo = PeopleRepo(db)
    identity = dict(model=MODEL, model_version=MODEL_VERSION, prep_version=PREP)
    # 种子数据：脸 1 已确认给「妈妈」，脸 3 是模型提议给候选 2 的，脸 2 一条归属都没有
    assert repo.unassigned_face_ids(**identity) == [2]
    assert repo.unclaimed_face_ids(**identity) == [2, 3]
    client.post("/api/people/2/dissolve")
    assert repo.unassigned_face_ids(**identity) == [2], \
        "解散过的脸又回到聚类池了 —— 下一轮会发现同一个垃圾簇"
    assert repo.unclaimed_face_ids(**identity) == [2, 3], \
        "解散过的脸必须还能被提议给真正的那个人"


def test_not_person_hides_the_face_from_everyone(client: TestClient) -> None:
    """★ 「不是人」：海报/雕像/屏幕里的脸 —— 它不属于任何人，也不该再被提议。

    ⚠️ 注意这里的"摘掉"是**滤镜**，不是删归属：判定行一删，一切照旧回来
    （见 `test_undoing_not_person_restores_the_exact_confirmations`）。
    """
    marked = client.post("/api/faces/verdict",
                         json={"face_ids": [3], "verdict": "not_person"}).json()
    assert marked["changed"] == 1
    assert marked["affected"] == [2], "要说清楚它原来挂在谁名下"
    assert client.get("/api/people/2").json()["faces"] == [], "人脸墙上必须看不到它"
    listed = client.get("/api/faces/not-person").json()
    assert listed["total"] == 1
    assert listed["items"][0]["face_id"] == 3
    assert listed["items"][0]["relative_path"], "撤销之前得能看一眼原片"
    assert client.get("/api/people").json()["stats"]["not_person_faces"] == 1
    # 列表里的"模型提议 N 张"也要跟着减 —— 否则两处数字对不上
    person2 = [p for p in client.get("/api/people").json()["items"] if p["id"] == 2][0]
    assert person2["auto_faces"] == 0


def test_one_marked_face_does_not_wipe_every_label(client: TestClient) -> None:
    """★ 回归：只要库里有一条"不是人"的判定，**所有人都不能变成零样本**。

    第一版把过滤条件写成 `fv.face_id = face_id`（没有限定表名），
    SQLite 把它解析成子查询自己的列，条件恒真、`NOT EXISTS` 恒假 ——
    于是界面显示"已确认 0 张"，重算判据直接跳过。这个 bug 只在
    "库里确实有一条判定"时才出现，正好是功能的正常使用状态。
    """
    from photoos.repos import PeopleRepo

    db = client.db  # type: ignore[attr-defined]
    repo = PeopleRepo(db)
    client.post("/api/people/1/faces", json={"face_ids": [2]})       # 妈妈：脸 1、2
    assert repo.labels(1)["positive"] == [1, 2]
    client.post("/api/faces/verdict", json={"face_ids": [2], "verdict": "not_person"})
    assert repo.labels(1)["positive"] == [1], "一条判定把整个标签集合清空了"
    # 别人也必须有样本（他们跟这张脸无关）
    client.post("/api/people/2/faces", json={"face_ids": [3]})
    assert repo.labels(2)["positive"] == [3]


def test_undoing_not_person_restores_the_exact_confirmations(client: TestClient) -> None:
    """★ 回归：撤销之后，那张脸必须**回到原来那个人名下**。

    第一版在标"不是人"时顺手删了 `person_face`，撤销只删判定行 ——
    于是"标记 → 撤销"一个来回就永久吃掉一条用户确认过的正例（实测 155 → 154）。
    撤销在界面上看起来是完整的回头路，那就必须是完整的。
    """
    client.post("/api/people/1/faces", json={"face_ids": [2]})
    before = client.get("/api/people/1").json()
    assert before["counts"]["positive"] == 2

    client.post("/api/faces/verdict", json={"face_ids": [2], "verdict": "not_person"})
    during = client.get("/api/people/1").json()
    assert during["counts"]["positive"] == 1, "标了之后不该还算在他名下"
    assert sorted(f["face_id"] for f in during["faces"]) == [1]

    client.post("/api/faces/verdict", json={"face_ids": [2], "verdict": "person"})
    after = client.get("/api/people/1").json()
    assert after["counts"]["positive"] == 2, "撤销之后那条确认没了"
    assert sorted(f["face_id"] for f in after["faces"]) == [1, 2]
    assert after["profile"]["pos_count"] == 2, "判据也要按原来的样本重算回来"


def test_not_person_faces_leave_the_clustering_and_suggestion_pools(client: TestClient) -> None:
    from photoos.repos import PeopleRepo

    db = client.db  # type: ignore[attr-defined]
    repo = PeopleRepo(db)
    identity = dict(model=MODEL, model_version=MODEL_VERSION, prep_version=PREP)
    assert repo.unassigned_face_ids(**identity) == [2]
    client.post("/api/faces/verdict", json={"face_ids": [2], "verdict": "not_person"})
    assert repo.unassigned_face_ids(**identity) == [], "标了不是人的脸还在聚类池里"
    assert repo.unclaimed_face_ids(**identity) == [3], "标了不是人的脸还会被提议"


def test_undoing_not_person_puts_the_face_back(client: TestClient) -> None:
    """★ 撤销必须存在：误判一张脸太容易了。"""
    client.post("/api/faces/verdict", json={"face_ids": [3], "verdict": "not_person"})
    undone = client.post("/api/faces/verdict",
                         json={"face_ids": [3], "verdict": "person"}).json()
    assert undone["changed"] == 1
    assert client.get("/api/faces/not-person").json()["total"] == 0
    from photoos.repos import PeopleRepo
    db = client.db  # type: ignore[attr-defined]
    assert 3 in PeopleRepo(db).unclaimed_face_ids(
        model=MODEL, model_version=MODEL_VERSION, prep_version=PREP)


def test_a_not_person_face_never_enters_the_learning_set(client: TestClient) -> None:
    """★ 它不是"这个人不像的样本"，它压根不该参与"这个人长什么样"的估计。"""
    from photoos.repos import PeopleRepo

    db = client.db  # type: ignore[attr-defined]
    repo = PeopleRepo(db)
    assert repo.labels(1)["positive"] == [1]
    client.post("/api/faces/verdict", json={"face_ids": [1], "verdict": "not_person"})
    labels = repo.labels(1)
    assert labels["positive"] == [] and labels["auto"] == [] and labels["negative"] == []


def test_not_person_validates_its_input(client: TestClient) -> None:
    assert client.post("/api/faces/verdict",
                       json={"face_ids": [1], "verdict": "也许吧"}).status_code == 422
    assert client.post("/api/faces/verdict", json={"face_ids": []}).status_code == 422
    assert client.post("/api/faces/verdict",
                       json={"face_ids": [999], "verdict": "not_person"}).status_code == 404


def test_move_takes_the_faces_away_from_the_old_person(client: TestClient) -> None:
    """★ 迁移：用户说"这些人其实都是 XX" —— 必须从**原来那个人**名下摘掉。

    尤其是用户自己以前确认过的归属：`add_faces` 只撤 auto 提议，
    直接拿它当"迁移"用会让同一张脸挂在两个人下面。
    """
    moved = client.post("/api/people/move",
                        json={"face_ids": [1, 3], "target_person_id": 2}).json()
    assert moved["moved"] == 2
    assert moved["sources"] == [1], "原人物要报出来（它的判据变了）"
    assert set(moved["profiles"]) == {"1", "2"}, "两边都要重算判据"

    first = client.get("/api/people/1").json()
    second = client.get("/api/people/2").json()
    assert first["faces"] == [], "搬走之后原人物名下不该还有这些脸"
    assert sorted(f["face_id"] for f in second["faces"]) == [1, 3]
    assert [f["source"] for f in second["faces"]] == ["user", "user"], \
        "搬过去就是用户确认，不是模型提议"


def test_move_clears_a_previous_rejection(client: TestClient) -> None:
    """搬过去等于说"是他" —— 之前对这个人的"不是他"必须作废。"""
    client.post("/api/people/2/reject", json={"face_ids": [1]})
    assert [f["face_id"] for f in client.get("/api/people/2").json()["rejected"]] == [1]
    client.post("/api/people/move", json={"face_ids": [1], "target_person_id": 2})
    assert client.get("/api/people/2").json()["rejected"] == []


def test_move_validates_its_input(client: TestClient) -> None:
    assert client.post("/api/people/move",
                       json={"face_ids": [1]}).status_code == 422, "没说搬到谁名下"
    assert client.post("/api/people/move",
                       json={"face_ids": [1], "target_person_id": 999}).status_code == 404
    assert client.post("/api/people/move",
                       json={"face_ids": [], "target_person_id": 2}).status_code == 422
    assert client.post("/api/people/move",
                       json={"face_ids": [999], "target_person_id": 2}).status_code == 404


def test_batch_actions_ignore_faces_that_no_longer_exist(client: TestClient) -> None:
    """一批里混进已消失的脸：活的照做、死的报出来，**不是整批失败**。"""
    moved = client.post("/api/people/move",
                        json={"face_ids": [3, 999], "target_person_id": 1}).json()
    assert moved["moved"] == 1 and moved["skipped"] == [999]
    assert "跳过" in moved.get("note", "")


# ============================================================ C. 承诺边界

def test_no_new_destructive_routes(client: TestClient) -> None:
    """Phase 4 加了不少端点，但一个"删原文件"的入口都不许有。

    判据与 Phase 1/2 的两条守卫**共用同一份**（`route_guard.py`）：
    上一版这里是子串匹配，`confirm-all` 里的 `rm` 把它打红了。
    """
    for route in client.app.routes:  # type: ignore[attr-defined]
        hits = destructive_hits(route.path)
        assert not hits, f"发现了可疑的破坏性路由：{route.path}（{sorted(hits)}）"


def test_people_endpoints_never_touch_the_library_files(client: TestClient, monkeypatch) -> None:
    """★ 硬守卫：确认/否决/一键进专题**都不许打开照片库里的任何文件**。

    Phase 2 已经对主题端点做过同样的守卫（"只改数据库关系"）。
    这里把它扩到 Phase 4 的新写路径上 —— 承诺必须由代码守住，而不是靠注释。
    """
    opened: list[str] = []
    real_open = open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    client.post("/api/people/1/faces", json={"face_ids": [2]})
    client.post("/api/people/1/reject", json={"face_ids": [3]})
    client.patch("/api/people/1", json={"name": "妈妈2"})
    client.post("/api/people/1/collection", json={})
    client.post("/api/people/learn", json={})
    monkeypatch.undo()

    # 数据库文件本身当然会被打开；关键是**没有别的路径**（尤其是库根下的照片目录）
    for path in opened:
        assert "data\\photoos.db" in path or "data/photoos.db" in path or path.endswith(".db"), \
            f"人物端点打开了非数据库文件：{path}"
