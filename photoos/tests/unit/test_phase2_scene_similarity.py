"""Phase 2 · 把嵌入的相似度真正接进 Scene 划分（决策 #7 的"相似度切片"）。

## 这个文件补的是一个**功能真空**

`scene_grouping` 的第 ② 步（相似度合并/细分）早就实现、也有单测，
但 `scenes rebuild` 一直硬编码 `similarity=None` —— 于是真实流水线里
**相似度切片从来没跑过**，界面上只会显示「未启用（退化为纯时间规则）」。
原有单测发现不了这件事，因为它们都是直接调 `split_scenes(similarity=...)` 的，
**没有一条走过 CLI**。

所以本文件两层都测：
  ① **纯逻辑**：跨组相似度必须取"所有资产两两的**最大值**"（不是只看边界那一对）
  ② **端到端**：`photoos scenes rebuild` 在有嵌入时报"启用中"，**并且真的改变划分**
     （靠 `--no-similarity` 做 A/B 对照，否则"接上了"可能只是多打印了一行字）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from photoos.cli import main
from photoos.config import Settings, reset_settings_cache, use_config_path
from photoos.db import Database, apply_migrations
from photoos.library import model_store
from photoos.library.scene_grouping import (
    build_similarity_callback,
    max_cross_similarity,
)
from photoos.library.shot_grouping import ShotAsset, ShotGroup
from photoos.library.vectorstore import NumpyVectorStore

STAMP = "2026-01-01T00:00:00+08:00"


# ============================================================ ① 纯逻辑

def _shot(asset_id: int) -> ShotGroup:
    return ShotGroup(assets=[
        ShotAsset(asset_id=asset_id, file_type="jpeg",
                  relative_path=f"城市\\2026-01-01_1000-1100\\DSC_{asset_id:04d}.JPG")
    ])


def test_max_cross_similarity_takes_the_true_maximum() -> None:
    """核心语义：取**所有资产两两**的最大余弦，而不是只看边界那一对。

    构造"相似的那张不在边界上"的情形（实测那 52 对跨组高相似照片正是这个形态）：
      left  = [10]（向量 e0）
      right = [12, 11]，其中 12 ⊥ e0、11 ≈ e0
    边界对 (10,12) 余弦 = 0，但真实最大值由 (10,11) 给出 ≈ 0.995。
    """
    matrix = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.99, 0.1, 0.0]], dtype="<f4")
    store = NumpyVectorStore.from_matrix(matrix, keys=[10, 12, 11])
    index_of = {10: 0, 12: 1, 11: 2}

    value = max_cross_similarity(
        [10], [12, 11], matrix=store.matrix(), index_of=index_of)
    assert value is not None and value > 0.99, value

    # 反向对照：只看边界那一对是 0 —— 证明这条测试不是白测的
    assert abs(float(matrix[0] @ matrix[1])) < 1e-6


def test_max_cross_similarity_none_without_embeddings() -> None:
    """任一侧没有嵌入 → None（调用方据此跳过，而不是当成 0 或 1）。"""
    store = NumpyVectorStore.from_matrix(np.array([[1.0, 0.0]], dtype="<f4"), keys=[10])
    matrix = store.matrix()
    assert max_cross_similarity([99], [10], matrix=matrix, index_of={10: 0}) is None
    assert max_cross_similarity([10], [99], matrix=matrix, index_of={10: 0}) is None


def test_build_callback_returns_none_without_store() -> None:
    """没有向量库时必须返回 None —— 让第 ② 步**整段跳过**，不猜默认值。"""
    assert build_similarity_callback([_shot(1)], matrix=None, index_of={}) is None
    assert build_similarity_callback([_shot(1)], matrix=None, index_of={1: 0}) is None


def test_build_callback_guards_out_of_range_indices() -> None:
    store = NumpyVectorStore.from_matrix(np.array([[1.0, 0.0]], dtype="<f4"), keys=[1])
    callback = build_similarity_callback(
        [_shot(1)], matrix=store.matrix(), index_of={1: 0})
    assert callback(0, 0) is not None
    assert callback(5, 0) is None    # 越界不能抛异常，否则整条流水线挂掉
    assert callback(0, -1) is None


# ============================================================ ② 端到端（走 CLI）

def _vector_512(cluster: int, index: int, *, similar: bool) -> np.ndarray:
    """造 512 维单位向量（**必须**是 512 —— 那是 `model_store.MODEL_DIM` 的契约，
    库里存别的维度会让 `load_store()` 按 `dim=512` 解读而报"字节数不符"）。
    """
    vec = np.zeros(model_store.MODEL_DIM, dtype="<f4")
    if cluster == 0:
        vec[0] = 1.0
    elif similar:
        # 与簇 0 余弦 ≈ 0.995：同一构图、略有差异
        vec[0], vec[1] = 0.995, 0.1
    else:
        # 与簇 0 正交：完全不同的题材
        vec[1] = 1.0
    vec[2] = 0.02 * index          # 簇内细微差异，保证不是同一个向量
    return (vec / np.linalg.norm(vec)).astype("<f4")


def _seed(db: Database, *, with_embeddings: bool, similar: bool) -> None:
    """造两簇各 4 张，被 20 分钟的时间间隔切开，视觉上**相同或相反**。

    * 簇内相邻 70 秒 → 在 `--gap-seconds 60` 下各自成 Shot（两簇共 8 个 Shot）
    * 两簇之间相隔 20 分钟 → 默认 900 秒的 scene_gap **会**切成两段
    * `similar=True`：两簇余弦 ≈ 0.995 → 第 ② 步应当**合并**回一个 Scene
    * `similar=False`：两簇正交 → **不该**合并
    """
    with db.write() as conn:
        conn.execute(
            "INSERT INTO library (uuid, name, created_at, status)"
            " VALUES ('lib','测试库',?,'ONLINE')", (STAMP,))
        conn.execute(
            "INSERT INTO session (id, name, captured_from, created_at, updated_at)"
            " VALUES (1,'时段','2026-01-01 10:00:00',?,?)", (STAMP, STAMP))
        asset_id = 1
        for cluster in range(2):
            for i in range(4):
                seconds = (0 if cluster == 0 else 20 * 60) + i * 70
                minute, second = divmod(seconds, 60)
                conn.execute(
                    "INSERT INTO asset (id, library_id, relative_path, file_type, status,"
                    " first_seen_at) VALUES (?,1,?,'jpeg','OK',?)",
                    (asset_id,
                     f"城市\\2026-01-01_1000-1100\\DSC_{asset_id:04d}.JPG", STAMP))
                conn.execute(
                    "INSERT INTO metadata (asset_id, capture_at, subsec) VALUES (?,?,?)",
                    (asset_id, f"2026-01-01 10:{minute:02d}:{second:02d}", "00"))
                if with_embeddings:
                    vec = _vector_512(cluster, i, similar=similar)
                    conn.execute(
                        "INSERT INTO embedding (asset_id, model, model_version, dim,"
                        " normalization, input_kind, input_version, prep_version, vector,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (asset_id, model_store.MODEL_NAME, model_store.MODEL_VERSION,
                         model_store.MODEL_DIM, model_store.MODEL_NORMALIZATION,
                         "preview", "p1.0", model_store.PREP_VERSION,
                         vec.tobytes(), STAMP))
                asset_id += 1


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """隔离数据根（走 `PHOTOOS_DATA_ROOT`）+ 一个已建好并填了数据的库。

    ⚠️ 两个关键点，缺一个这个文件就会**静默地测错东西**：

    1. **必须**设 `PHOTOOS_DATA_ROOT`。否则 CLI 会去打开真实库 ——
       那正是 Phase 1 踩过的坑（用户看到"照片库消失了"），这里不能再踩。
    2. **必须**清 `get_settings()` 的 `lru_cache`。它是 `@lru_cache(maxsize=1)`，
       一旦被本次会话里任何先跑的测试调用过，就会一直返回**那一次的** Settings ——
       于是 `--data-root`/环境变量全部失效，CLI 会去读真实库。
       本文件第一版就栽在这里：seed 明明写进了 8 个 embedding，
       CLI 却报"embedding 表为空"，而 `参与资产` 又是对的（因为真实库也有资产）。
    """
    data_root = tmp_path / "data"

    def build(*, with_embeddings: bool, similar: bool = True) -> Path:
        monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(data_root))
        reset_settings_cache()
        with use_config_path(None):
            settings = Settings(data_root=data_root)
            settings.ensure_dirs()
            db = Database(settings.db_path)
            apply_migrations(db, backup_dir=settings.backups_dir)
            _seed(db, with_embeddings=with_embeddings, similar=similar)
        reset_settings_cache()
        return data_root

    yield build
    reset_settings_cache()


def _scene_count(text: str) -> int:
    for line in text.splitlines():
        if "Scene 总数" in line:
            return int(line.rsplit(None, 1)[-1])
    raise AssertionError(f"输出里没有 Scene 总数：\n{text}")


def _run(capsys, *argv: str) -> tuple[int, str]:
    """跑一条 CLI 命令并返回 `(退出码, stdout+stderr)`。

    ⚠️ 必须**同时**收 stderr：CLI 的错误/警告走 stderr，只看 stdout 会让
    断言在失败时只显示半截信息（本文件第一版就踩了这个坑）。
    """
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, (captured.out or "") + (captured.err or "")


def test_similarity_disabled_without_embeddings(env, capsys) -> None:
    """没有嵌入时如实说"未启用"，且仍然能跑出结果（降级可用）。"""
    env(with_embeddings=False)
    code, out = _run(capsys, "scenes", "rebuild", "--gap-seconds", "60")
    assert code == 0, out
    assert "未启用" in out, out
    assert "embedding 表为空" in out, out


def test_similarity_enabled_with_embeddings(env, capsys) -> None:
    """有嵌入时必须报"启用中" —— 这一条就是那个功能真空的回归断言。"""
    data_root = env(with_embeddings=True)
    from photoos.repos import EmbeddingRepo

    probe = Database(data_root / "photoos.db")
    print(f"  [probe] db.path={probe.path} raw="
          f"{probe.scalar('SELECT COUNT(*) FROM embedding', default=0)} "
          f"repo={EmbeddingRepo(probe).count()} "
          f"idents={EmbeddingRepo(probe).identities()}")
    code, out = _run(capsys, "scenes", "rebuild", "--gap-seconds", "60")
    assert code == 0, out
    assert "启用中" in out, out
    assert "8 个向量" in out, out
    assert "未启用" not in out, out


def test_similarity_actually_changes_the_split(env, capsys) -> None:
    """关键：开/关相似度必须给出**不同的划分**，否则"接上了"只是多打印一行字。"""
    env(with_embeddings=True)
    _code, with_sim = _run(capsys, "scenes", "rebuild", "--gap-seconds", "60")
    _code2, without_sim = _run(
        capsys, "scenes", "rebuild", "--gap-seconds", "60", "--no-similarity")

    assert _scene_count(without_sim) == 2, without_sim   # 20 分钟间隔切成两段
    assert _scene_count(with_sim) == 1, with_sim         # 视觉相同 → 合并回一个
    assert "已按 --no-similarity 关闭" in without_sim


def test_dissimilar_clusters_are_not_merged(env, capsys) -> None:
    """反向：视觉相反的簇**不该**被合并 —— 否则"永远合并"也能骗过上面那条。"""
    env(with_embeddings=True, similar=False)
    _code, with_sim = _run(capsys, "scenes", "rebuild", "--gap-seconds", "60")
    _code2, without_sim = _run(
        capsys, "scenes", "rebuild", "--gap-seconds", "60", "--no-similarity")

    assert _scene_count(with_sim) == 2, with_sim
    assert _scene_count(without_sim) == 2, without_sim


def test_apply_still_requires_shots_on_disk(env, capsys) -> None:
    """`--apply` 在**没有 Shot 落库**时必须明确报错，而不是写出一堆挂不上 Shot 的 Scene。

    这是接了相似度之后最容易带坏的一条路径：划分算得对，但落库前提没满足。
    """
    data_root = env(with_embeddings=True)
    code, out = _run(capsys, "scenes", "rebuild", "--gap-seconds", "60", "--apply")
    assert code != 0, out
    assert "shot 表为空" in out, out
    db = Database(data_root / "photoos.db")
    assert int(db.scalar("SELECT COUNT(*) FROM scene", default=0)) == 0
