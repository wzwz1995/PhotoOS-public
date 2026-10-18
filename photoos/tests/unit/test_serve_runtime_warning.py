"""`serve` 启动期的嵌入运行时提醒（`_warn_embedding_runtime`）。

## 它补的是什么

`embed` 是**可选依赖**（`[ai-gpu]`），而产品 venv 默认不装 onnxruntime。
于是完全可能出现这种**自相矛盾**的状态：

    在装了运行时的 venv 里跑过 `photoos embed` → 库里/数据根里有模型
    之后用另一个（没装运行时的）venv 启动 `photoos serve` → 一切"看起来正常"

而依赖视觉相似度的功能会**静默退化**（设计如此：不因为没嵌入就让功能不可用）。
方案的风险登记把这条列为**高**，缓解措施写着"`serve` 启动时警告" ——
但实测 `serve` **从来没有**这个警告（启动期告警 0 行）。

本文件守住三件事：
  ① 有模型、无运行时 → **必须告警**（方案承诺的行为）
  ② 既无模型也无运行时 → **不告警**（那只是"没启用嵌入"，吓人没意义）
  ③ 有运行时 → **不告警**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoos.cli import _warn_embedding_runtime
from photoos.config import Settings
from photoos.db import Database

MODEL_DIR = "ai_cache/models/clip-vit-base-patch32-xenova-onnx-fp32"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    return settings


def _has_runtime() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def test_warns_when_model_present_but_runtime_missing(tmp_path: Path, capsys) -> None:
    """① 有模型、无运行时 → 告警（且要说清后果与怎么修）。

    ★ 这里的断言在真实库上被**纠正过一次**：原来要求文案里出现"退化/时间规则"，
    但真实情况是 —— `onnxruntime` 只被 `photoos embed` 与 `photoos ai doctor` 用到，
    相似度的**消费端**（`scenes rebuild`）读的是库里已算好的向量（只用 numpy）。
    所以"没运行时"**不会**让既有相似度退化（真实库实测：产品 venv 跑出 303 个 Scene，
    `--no-similarity` 是 371）。断言随之改为**只对真的会坏掉的那件事**告警。
    """
    if _has_runtime():
        pytest.skip("这个 venv 装了 onnxruntime，测不到'缺运行时'的分支")
    settings = _settings(tmp_path)
    (settings.data_root / MODEL_DIR).mkdir(parents=True)

    _warn_embedding_runtime(settings)

    out = capsys.readouterr()
    text = (out.out or "") + (out.err or "")
    assert "onnxruntime" in text, text
    assert "模型" in text, text
    # 必须点名**真正会失败的那条命令**，否则用户不知道后果
    assert "photoos embed" in text, text
    assert "算不了新的向量" in text or "算不了新向量" in text, text
    assert "ai doctor" in text, text
    assert "ai-gpu" in text, text
    # ★ 反向要求：**不得**再声称既有相似度会退化（那是错的，会吓到用户）
    assert "依赖视觉相似度的功能会退化" not in text, text


def test_says_existing_vectors_are_unaffected(tmp_path: Path, capsys) -> None:
    """①b 库里有向量时 → 必须明确说"已算好的向量不受影响"，并报出条数。

    这条是**用户第一眼会看到的那句话**：他的库里已经有 21,400 个向量、
    Scene 也按相似度分好了。如果只看到一句"相似度会退化"，他会以为看到的分组是假的。
    """
    if _has_runtime():
        pytest.skip("这个 venv 装了 onnxruntime，测不到'缺运行时'的分支")
    settings = _settings(tmp_path)
    (settings.data_root / MODEL_DIR).mkdir(parents=True)

    # 造一个带 embedding 表的最小库（不跑完整迁移，够这条测试用）
    #
    # ⚠️ 这里必须用**真实的模型身份**：Phase 3 起 `_warn_embedding_runtime` 报的是
    # "**参与 Scene 划分的那一版**有多少向量"（CLIP），而不是全表行数 ——
    # 因为 `embedding` 表可以并存多个模型（PRD §9.3 断点 #6）。
    # 原来这里写的是占位值 'm'/'v'/'p'，在"数全表"的旧实现下恰好能过；
    # 换成真实身份后，这条测试才真正验证了它想验证的分支。
    from photoos.library import model_store as _ms

    db = Database(settings.db_path)
    with db.write() as conn:
        conn.execute(
            "CREATE TABLE embedding (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL,"
            " library_id INTEGER NOT NULL, model TEXT NOT NULL, model_version TEXT NOT NULL,"
            " prep_version TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL,"
            " created_at TEXT NOT NULL)"
        )
        for asset_id in (1, 2, 3, 4, 5):
            conn.execute(
                "INSERT INTO embedding (asset_id, library_id, model, model_version,"
                " prep_version, dim, vector, created_at) VALUES (?, 1, ?, ?, ?, 4, X'00', '')",
                (asset_id, _ms.MODEL_NAME, _ms.MODEL_VERSION, _ms.PREP_VERSION),
            )

    _warn_embedding_runtime(settings)

    text = "".join(capsys.readouterr())
    assert "5 个向量" in text, text
    assert "不受影响" in text, text
    assert "退化为纯时间规则" not in text, text


def test_says_no_similarity_at_all_when_no_vectors(tmp_path: Path, capsys) -> None:
    """①c 库里**没有**向量时 → 这时才该说"全部退化为纯时间规则"。

    与 ①b 成对：同一句警告，库的状态不同，结论必须不同 ——
    否则就是在用一个模板套两种完全相反的情况。
    """
    if _has_runtime():
        pytest.skip("这个 venv 装了 onnxruntime，测不到'缺运行时'的分支")
    settings = _settings(tmp_path)
    (settings.data_root / MODEL_DIR).mkdir(parents=True)
    Database(settings.db_path)                       # 有库文件，但没有 embedding 表

    _warn_embedding_runtime(settings)

    text = "".join(capsys.readouterr())
    assert "还没有任何向量" in text, text
    assert "退化为纯时间规则" in text, text


def test_silent_when_no_model_at_all(tmp_path: Path, capsys) -> None:
    """② 既无模型也无运行时 → 静默（否则每次启动都吓人一跳）。"""
    settings = _settings(tmp_path)
    _warn_embedding_runtime(settings)
    out = capsys.readouterr()
    assert not (out.out or "").strip(), (out.out, out.err)
    assert not (out.err or "").strip(), (out.out, out.err)


def test_silent_when_runtime_available(tmp_path: Path, capsys) -> None:
    """③ 有运行时 → 静默（这条路走的是"运行时可用"分支）。"""
    if not _has_runtime():
        pytest.skip("这个 venv 没装 onnxruntime，测不到'有运行时'的分支")
    settings = _settings(tmp_path)
    (settings.data_root / MODEL_DIR).mkdir(parents=True)
    _warn_embedding_runtime(settings)
    out = capsys.readouterr()
    assert not (out.out or "").strip(), (out.out, out.err)
    assert not (out.err or "").strip(), (out.out, out.err)


def test_does_not_run_the_expensive_probe(tmp_path: Path) -> None:
    """★ 启动期**不得**跑完整自检（那是 `ai doctor` 的活）。

    `ai doctor` 要加载模型并真跑一次推理（实测 5.22 ms/张 + 模型加载时间）。
    把它塞进 `serve` 启动路径会明显拖慢启动，而且模型损坏时会变成"服务起不来"。
    这里用"模型目录存在但里面是垃圾文件"来证明：若真去加载模型，必然抛错；
    实际不抛，说明它只做了轻量检查。
    """
    settings = _settings(tmp_path)
    model_dir = settings.data_root / MODEL_DIR
    model_dir.mkdir(parents=True)
    (model_dir / "vision_model.onnx").write_bytes(b"not a real model")   # 垃圾内容

    _warn_embedding_runtime(settings)      # 不抛异常 = 没去加载模型
