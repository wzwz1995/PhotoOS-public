"""Phase 2 · M3 落库：嵌入流水线测试。

**关键设计**：测试用一个**确定性的假 embedder**，因此本文件**不需要 onnxruntime 或模型**
就能完整验证落库逻辑 —— 产品 venv 里故意没有 ML 运行时，测试不该因此跳过。
真机（真模型 + 真 CUDA）的验证已由 `ai doctor` 覆盖（§13.5）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from photoos.db import Database, apply_migrations
from photoos.library import model_store
from photoos.library.embed_pipeline import dry_run_summary, run_embedding
from photoos.library.vectorstore import VectorStoreError, pack_vector, unpack_vector
from photoos.repos import EmbeddingRepo


class FakeEmbedder:
    """确定性 embedder：向量由图片字节的 sha256 派生。

    这样"同一张图 → 同一向量"可断言，而"不同图 → 不同向量"也成立，
    且**完全不需要 ML 运行时**。
    """

    model = model_store.MODEL_NAME
    model_version = "fake-v1"
    dim = 8
    normalization = "l2"
    prep_version = "p-test"
    device = "cpu"

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.calls: list[list[Path]] = []

    def _vector_for(self, path: Path):
        import numpy as np

        digest = hashlib.sha256(path.read_bytes()).digest()
        raw = np.frombuffer(digest[: self.dim * 4], dtype="<u4").astype("float32")
        return raw + 1.0

    def embed_paths_batched(self, paths, *, batch_size=16, on_progress=None):
        import numpy as np

        self.calls.append(list(paths))
        out = []
        for path in paths:
            if any(str(path).endswith(name) for name in self.fail_on):
                raise RuntimeError("故意失败")
            out.append(self._vector_for(path))
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype="<f4")

    def embed_images(self, images):        # pragma: no cover - 本测试不走这条路径
        raise NotImplementedError

    def embed_paths(self, paths):          # pragma: no cover
        raise NotImplementedError


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Database, Path]:
    """一个带 3 张照片（含 preview 文件）的库。"""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    db = Database(data_root / "photoos.db")
    apply_migrations(db, backup_dir=data_root / "backups")

    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库','t','ONLINE')"
        )
        for asset_id in (1, 2, 3):
            conn.execute(
                "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
                " VALUES (?, 1, ?, 'jpeg', 'OK', 't')",
                (asset_id, f"城\\时段\\DSC_{asset_id:04d}.JPG"),
            )
            rel = f"previews/16/{asset_id}.webp"
            target = data_root / rel.replace("/", "\\")
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (300, 200), (asset_id * 30 % 256, 100, 50)).save(target, format="WEBP")
            conn.execute(
                "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
                " generator_version, created_at)"
                " VALUES (?, 'preview', ?, 300, 200, 1000, 'p1.0', 't')",
                (asset_id, rel),
            )
    return db, data_root


# ============================================================ 基本落库

def test_embeds_all_pending_assets(env) -> None:
    db, data_root = env
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 3
    assert report.embedded == 3
    assert report.failed == 0
    assert EmbeddingRepo(db).count(model="clip-vit-base-patch32", model_version="fake-v1") == 3


def test_stored_vector_round_trips(env) -> None:
    """★ 序列化契约：little-endian float32，长度恰为 dim*4。"""
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())
    row = db.query_one("SELECT * FROM embedding WHERE asset_id = 1")
    blob = bytes(row["vector"])
    assert len(blob) == row["dim"] * 4
    vector = unpack_vector(blob, dim=int(row["dim"]))
    assert vector.shape == (int(row["dim"]),)


def test_records_input_identity(env) -> None:
    """铁律 #4：向量必须记录"它从哪来的" —— 输入类型与输入版本都要落库。"""
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())
    row = db.query_one("SELECT * FROM embedding WHERE asset_id = 1")
    assert row["input_kind"] == "preview"
    assert row["input_version"] == "p1.0"          # preview.generator_version
    assert row["model_sha256"] == model_store.MODEL_FILES[0][3]
    assert row["normalization"] == "l2"


def test_same_image_yields_same_vector(env) -> None:
    """确定性：同一张图算两次得到同一向量（可重算的前提）。"""
    db, data_root = env
    embedder = FakeEmbedder()
    run_embedding(db, data_root, embedder=embedder)
    repo = EmbeddingRepo(db)
    first = bytes(repo.get(1, model=embedder.model, model_version=embedder.model_version,
                           prep_version=embedder.prep_version)["vector"])
    EmbeddingRepo(db).delete_identity(
        model=embedder.model, model_version=embedder.model_version, prep_version=embedder.prep_version
    )
    run_embedding(db, data_root, embedder=FakeEmbedder())
    second = bytes(EmbeddingRepo(db).get(
        1, model=embedder.model, model_version=embedder.model_version, prep_version=embedder.prep_version
    )["vector"])
    assert first == second


# ============================================================ ★ 增量

def test_second_run_is_a_noop(env) -> None:
    """★ 已生成的不重复生成（PRD §11.2「缩略图生成…已生成的不重复生成」的同源纪律）。"""
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 0
    assert report.embedded == 0


def test_new_asset_is_picked_up_incrementally(env) -> None:
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())
    with db.write() as conn:
        conn.execute(
            "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (4, 1, '城\\时段\\DSC_0004.JPG', 'jpeg', 'OK', 't')"
        )
        rel = "previews/16/4.webp"
        target = data_root / rel.replace("/", "\\")
        Image.new("RGB", (300, 200), (7, 7, 7)).save(target, format="WEBP")
        conn.execute(
            "INSERT INTO preview (asset_id, kind, path, width, height, bytes,"
            " generator_version, created_at)"
            " VALUES (4, 'preview', ?, 300, 200, 1000, 'p1.0', 't')",
            (rel,),
        )
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 1 and report.embedded == 1


def test_asset_without_preview_is_not_pending(env) -> None:
    """没有 preview 就无法编码 —— 不该出现在待办里（否则每次都会失败一遍）。"""
    db, data_root = env
    with db.write() as conn:
        conn.execute(
            "INSERT INTO asset (id, library_id, relative_path, file_type, status, first_seen_at)"
            " VALUES (9, 1, '城\\时段\\x.JPG', 'jpeg', 'OK', 't')"
        )
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 3, "无 preview 的资产不该被计入待办"


def test_non_ok_asset_is_not_pending(env) -> None:
    """MISSING / UNREADABLE 的资产不该被反复尝试。"""
    db, data_root = env
    with db.write() as conn:
        conn.execute("UPDATE asset SET status = 'MISSING' WHERE id = 3")
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 2


# ============================================================ ★ 多版本并存（铁律 #4）

def test_new_model_version_coexists_with_old(env) -> None:
    """★ PRD §9.3：模型升级后**新增一行**，旧结果不被覆盖。"""
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())

    class V2(FakeEmbedder):
        model_version = "fake-v2"

    run_embedding(db, data_root, embedder=V2())
    repo = EmbeddingRepo(db)
    assert repo.count(model_version="fake-v1") == 3
    assert repo.count(model_version="fake-v2") == 3
    assert len(repo.identities()) == 2


def test_store_many_never_overwrites(env) -> None:
    """★ 同身份重复写入被**忽略**（不是 UPDATE）——覆盖等于污染历史结果。"""
    db, data_root = env
    embedder = FakeEmbedder()
    run_embedding(db, data_root, embedder=embedder)
    repo = EmbeddingRepo(db)
    original = bytes(repo.get(1, model=embedder.model, model_version=embedder.model_version,
                              prep_version=embedder.prep_version)["vector"])

    written = repo.store_many([
        {
            "asset_id": 1,
            "model": embedder.model,
            "model_version": embedder.model_version,
            "dim": embedder.dim,
            "normalization": "l2",
            "input_kind": "preview",
            "input_version": "p1.0",
            "prep_version": embedder.prep_version,
            "vector": pack_vector([9.0] * embedder.dim),
        }
    ])
    assert written == 0
    after = bytes(EmbeddingRepo(db).get(
        1, model=embedder.model, model_version=embedder.model_version, prep_version=embedder.prep_version
    )["vector"])
    assert after == original, "旧向量被覆盖了 —— 违反 PRD §9.3"


def test_identities_report_lists_all_versions(env) -> None:
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())

    class V2(FakeEmbedder):
        model_version = "fake-v2"

    run_embedding(db, data_root, embedder=V2())
    rows = EmbeddingRepo(db).identities()
    versions = {r["model_version"] for r in rows}
    assert versions == {"fake-v1", "fake-v2"}
    assert all(r["count"] == 3 for r in rows)


# ============================================================ ★ 单张失败不阻塞整批

def test_one_failure_does_not_block_the_batch(env) -> None:
    """★ PRD §11.1：单张 AI 失败不能导致整批任务失败。

    `FakeEmbedder` 的失败是**整批抛出**（这是最坏的情形：一次 `embed_paths_batched`
    全挂）。断言的重点是：**异常不外抛**、失败被如实计入 `report.failures`、
    调用方仍拿到一个可用的 report。
    """
    db, data_root = env
    embedder = FakeEmbedder(fail_on={"3.webp"})
    report = run_embedding(db, data_root, embedder=embedder)
    assert report.failed == 3, "整批抛出时该批全部记为失败"
    assert report.failures
    assert report.embedded == 0
    assert "故意失败" in report.failures[0][1]


def test_partial_failure_keeps_the_successful_ones(env) -> None:
    """更真实的场景：一批里只有一张坏 —— 好的仍要落库。"""
    db, data_root = env

    class PartialEmbedder(FakeEmbedder):
        """只在遇到坏图时把那一张剔掉，其余照常返回。"""

        def embed_paths_batched(self, paths, *, batch_size=16, on_progress=None):
            import numpy as np

            good = [p for p in paths if not any(str(p).endswith(n) for n in self.fail_on)]
            self.calls.append(list(good))
            return np.vstack([self._vector_for(p) for p in good]) if good else np.zeros((0, self.dim), "<f4")

    # 坏图不参与 → 先让它"文件缺失"更贴近真实：删掉文件
    (data_root / "previews" / "16" / "3.webp").unlink()
    report = run_embedding(db, data_root, embedder=PartialEmbedder())
    assert report.failed == 1
    assert report.embedded == 2
    assert EmbeddingRepo(db).count(model_version="fake-v1") == 2


def test_missing_preview_file_is_reported_not_silent(env) -> None:
    """preview 记录在但文件不在（缓存被清过）→ 记为失败，不静默跳过。"""
    db, data_root = env
    (data_root / "previews" / "16" / "3.webp").unlink()
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.failed == 1
    assert any("预览文件缺失" in msg for _, msg in report.failures)
    assert report.embedded == 2


def test_report_lines_are_readable(env) -> None:
    db, data_root = env
    lines = run_embedding(db, data_root, embedder=FakeEmbedder()).lines()
    assert any("嵌入完成" in line for line in lines)
    assert any("模型" in line for line in lines)


# ============================================================ 进度与限额

def test_progress_callback_is_invoked(env) -> None:
    db, data_root = env
    seen: list[tuple[int, int]] = []
    run_embedding(db, data_root, embedder=FakeEmbedder(), batch_size=1,
                  on_progress=lambda done, total: seen.append((done, total)))
    assert seen and seen[-1] == (3, 3)


def test_limit_caps_the_work(env) -> None:
    db, data_root = env
    report = run_embedding(db, data_root, embedder=FakeEmbedder(), limit=2)
    assert report.total == 2 and report.embedded == 2
    assert EmbeddingRepo(db).count(model_version="fake-v1") == 2


def test_run_on_empty_library_is_safe(env) -> None:
    db, data_root = env
    db.execute("DELETE FROM preview")
    report = run_embedding(db, data_root, embedder=FakeEmbedder())
    assert report.total == 0 and report.embedded == 0


# ============================================================ dry-run（只统计不写库）

def test_dry_run_reports_without_writing(env) -> None:
    db, data_root = env
    summary = dry_run_summary(db, embedder=FakeEmbedder())
    assert summary["pending"] == 3
    assert summary["identity"]["model_version"] == "fake-v1"
    assert EmbeddingRepo(db).count() == 0, "dry-run 不该写库"


def test_dry_run_works_without_embedder(env) -> None:
    """★ 没装 ML 运行时也**照样能报出"有多少张需要算"** —— 只依赖数据库。"""
    db, _data_root = env
    summary = dry_run_summary(db, embedder=None)
    assert summary["pending"] == 3
    assert summary["identity"]["model"] == model_store.MODEL_NAME
    assert summary["device"] == "unknown"


# ============================================================ 向量检索接入

def test_load_store_returns_indexed_vectors(env) -> None:
    """落库的向量能装进 `NumpyVectorStore` 直接检索（21.4k × 512 = 43.7 MB，全内存）。"""
    db, data_root = env
    embedder = FakeEmbedder()
    run_embedding(db, data_root, embedder=embedder)
    store = EmbeddingRepo(db).load_store(
        model=embedder.model, model_version=embedder.model_version, prep_version=embedder.prep_version
    )
    assert store.size == 3
    assert store.dim == 8
    assert sorted(store.keys) == [1, 2, 3]


def test_knn_by_key_excludes_itself(env) -> None:
    """★ 语义确认：`knn_by_key` 找的是"**其他**最像的照片"，结果里不该有自己。

    假 embedder 的向量由图片字节派生，彼此并不相似，所以"最像的另一张"分数不高
    （0.86）—— 这正是它不该被断言成"接近 1.0"的原因。自相似要用 `similarity(i, i)` 验证。
    """
    db, data_root = env
    embedder = FakeEmbedder()
    run_embedding(db, data_root, embedder=embedder)
    store = EmbeddingRepo(db).load_store(
        model=embedder.model, model_version=embedder.model_version, prep_version=embedder.prep_version
    )
    nearest = store.knn_by_key(1, 2)
    assert len(nearest) == 2, "应返回其余两张"
    assert store.key_at(nearest[0].index) != 1
    # 自相似度恒为 1
    own = store.keys.index(1)
    assert store.similarity(own, own) == pytest.approx(1.0, abs=1e-6)


def test_load_store_is_empty_without_vectors(env) -> None:
    """库里没有该身份的向量时，返回一个**空 store**。

    ⚠️ Phase 3 后这里必须给维度线索：`embedding` 表可以并存多个模型
    （CLIP 512 维 / EmbeddingGemma 2 768 维），所以"空结果该是几维"**不能靠硬编码猜**。
    原来本用例没传任何维度，旧实现直接硬编码 512 交差 —— 那个 512 从来没有依据。
    """
    db, _ = env
    store = EmbeddingRepo(db).load_store(
        model="clip-vit-base-patch32", model_version="nope", prep_version="nope",
        fallback_dim=512,
    )
    assert store.size == 0
    assert store.dim == 512


def test_load_store_refuses_to_guess_dimension(env) -> None:
    """★ 没有任何维度线索时**必须显式报错**，不许猜（多模型并存的安全边界）。

    这条断言是 Phase 3 加的：如果哪天有人为了方便又把维度硬编码回去，
    它会立刻变红 —— 防止"没有向量"被悄悄变成一个错误维度的 store。
    """
    db, _ = env
    with pytest.raises(VectorStoreError):
        EmbeddingRepo(db).load_store(
            model="some-unknown-model", model_version="v0", prep_version="p0"
        )


def test_delete_identity_only_removes_that_version(env) -> None:
    """回退只删指定版本，其他版本原样保留（多版本并存的意义）。"""
    db, data_root = env
    run_embedding(db, data_root, embedder=FakeEmbedder())

    class V2(FakeEmbedder):
        model_version = "fake-v2"

    run_embedding(db, data_root, embedder=V2())
    removed = EmbeddingRepo(db).delete_identity(
        model=FakeEmbedder.model, model_version="fake-v1", prep_version="p-test"
    )
    assert removed == 3
    repo = EmbeddingRepo(db)
    assert repo.count(model_version="fake-v1") == 0
    assert repo.count(model_version="fake-v2") == 3
