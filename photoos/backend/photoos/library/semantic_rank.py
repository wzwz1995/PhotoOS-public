"""搜索反馈的**重排**（Phase 4 · "让模型持续学习我的选择"的算法侧）。

## 这里做的不是训练，是**按你的表态重新打分**

    基础分  = cos(图像向量, 词的文本向量)          ← 模型原始判断，存在 `semantic_query_hit`
    正例分  = cos(图像向量, 你选中的那些照片的均值)
    反例分  = cos(图像向量, 你删掉的那些照片的均值)
    最终分  = 基础分 + alpha·正例分 − beta·反例分

三个设计约束，每一条都有理由：

1. **服务端必须能算** —— 只用库里**已有的图像向量**做点积（纯 numpy）。
   所以 `serve` 仍然不需要 torch/onnxruntime，「方案 a」的架构不被破坏。
   这是把"点一下就能学到"做进界面的前提。
2. **原始结果永不覆盖** —— `semantic_query_hit` 保持模型当时的判断，
   重排只发生在读取端。界面上有开关能关掉学习看到原样（PRD §9.3 断点 #6）。
3. **每一步都可复算** —— 正/负例均值与系数都落在 `semantic_tuning`，
   任何时刻重算都得到同样的排序。没有随机数、没有在线更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from ..db import Database
from ..repos import SemanticRepo

#: 重排权重。**刻意保守**：基础分（模型判断）仍然是主项，
#: 学习只做微调。理由：一两次点击不该把整页结果掀翻 ——
#: 那会让用户失去"这是搜索结果"的稳定感，也会把单张误点放大成系统性偏移。
#:
#: ⚠️ 这两个数字还有一个**可算出来的边界**，必须让调用方知道：
#: `alpha` 就是学习项能给某张照片加的上限，所以**两个候选只有在原始分差小于 α 时
#: 才可能被翻盘**。也就是说"把用户删掉的东西拿掉"这件事**主要由硬排除完成**，
#: 打分只负责在相近的候选之间做微调。这条边界由测试钉住
#: （`test_learned_tuning_cannot_override_a_large_base_gap`），
#: 免得日后有人为了"效果更明显"把它悄悄调大，反而破坏了结果的稳定性。
DEFAULT_ALPHA = 0.35
DEFAULT_BETA = 0.25

#: 少于这么多条反馈就不重排。理由：1 条反馈的"均值"就是那一张图本身，
#: 重排会退化成"找出与这张最像的"，那不是用户要的。
MIN_FEEDBACK = 2


def _decode(blob: Any, dim: int) -> np.ndarray:
    """BLOB → (dim,) float32。**长度不对就报错**，不静默截断。"""
    if blob is None:
        return np.zeros(0, dtype=np.float32)
    raw = bytes(blob)
    expected = int(dim) * 4
    if len(raw) != expected:
        raise ValueError(f"向量字节数不符：期望 {expected}（dim={dim}），实际 {len(raw)}")
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def decode_many(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    """把一批 `{dim, vector}` 行解成 (N, dim) float32。

    ## 为什么不是逐行 `_decode` 再 `vstack`（一次实测的性能修复）

    早先 `build_pool` 就是这么写的，在 7,371 行 × 768 维上实测 **42 秒**；
    而真正花时间的点积只要 **0.7 毫秒**。也就是说"学习和重排"这个功能
    99.99% 的时间花在了解码上，用户点一下要等 40 秒才能看到结果 ——
    这等于功能不可用。

    改成把所有 BLOB 拼成一块再 `np.frombuffer` 一次，降到毫秒级。
    """
    if not rows:
        return np.zeros((0, 0), dtype="f4")
    dim = int(rows[0]["dim"])
    width = dim * 4
    chunks: list[bytes] = []
    for row in rows:
        raw = bytes(row["vector"])
        if len(raw) != width:
            raise ValueError(
                f"shot {row.get('shot_id')} 的向量字节数 {len(raw)} 与 dim={dim} 不符")
        chunks.append(raw)
    return np.frombuffer(b"".join(chunks), dtype="<f4").reshape(len(chunks), dim).astype("f4")


@dataclass
class ShotPool:
    """全库 Shot 代表图向量的内存副本。

    为什么要有这个池：重排的意义不只是"把已有的结果换个顺序"，而是
    **把用户删掉的标准推广到全库** —— 用户点了几张"不是这个"之后，
    库里其他类似的东西也该往下沉，而原本没进前 60 的结果可能才该浮上来。
    只重排已有命中做不到这一点。

    代价可量级地小：21,400 × 768 float32 = 65 MB，一次点积 33 MFLOP
    （实测几十毫秒）。构建一次后缓存在进程里。
    """

    identity: tuple[str, str, str]
    shot_ids: np.ndarray
    rows: list[dict[str, Any]]
    matrix: np.ndarray
    index: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index = {int(s): i for i, s in enumerate(self.shot_ids)}

    @property
    def dim(self) -> int:
        return int(self.matrix.shape[1]) if self.matrix.size else 0

    def __len__(self) -> int:
        return int(len(self.shot_ids))


def resolve_image_identity(db: Database, query_row: dict[str, Any]) -> tuple[str, str, str]:
    """找出该查询应当用哪一套**图像**向量（而不是写死 `eg2-p1.0`）。

    文本与图像必须来自同一个模型身份，否则余弦没有意义。这里从库里查
    "这个 model/model_version 实际存在哪些 prep_version"，
    取最新写入的那个 —— 换预处理口径时，重排自动跟着走。
    """
    row = db.query_one(
        "SELECT prep_version FROM embedding WHERE model = ? AND model_version = ?"
        " GROUP BY prep_version ORDER BY MAX(created_at) DESC LIMIT 1",
        (query_row["model"], query_row["model_version"]),
    )
    if row is None:
        raise ValueError(
            f"库里没有 {query_row['model']} / {query_row['model_version']} 的图像向量，"
            "无法重排（请先运行 `photoos embed --engine gemma --apply`）"
        )
    return str(query_row["model"]), str(query_row["model_version"]), str(row["prep_version"])


def build_pool(db: Database, *, model: str, model_version: str, prep_version: str) -> ShotPool:
    """把全库 Shot 的代表图向量读进内存。"""
    repo = SemanticRepo(db)
    rows = repo.shot_vectors(model=model, model_version=model_version,
                             prep_version=prep_version)
    if not rows:
        return ShotPool(identity=(model, model_version, prep_version),
                        shot_ids=np.zeros(0, dtype=np.int64), rows=[], matrix=np.zeros((0, 0), "f4"))
    matrix = decode_many(rows)
    shot_ids = np.asarray([int(r["shot_id"]) for r in rows], dtype=np.int64)
    for row in rows:
        row.pop("vector", None)
    return ShotPool(identity=(model, model_version, prep_version),
                    shot_ids=shot_ids, rows=rows, matrix=matrix)


@dataclass
class Tuning:
    """一次重排要用的全部参数（写进 `semantic_tuning` 的就是它）。"""

    pos_vector: np.ndarray | None
    neg_vector: np.ndarray | None
    pos_count: int
    neg_count: int
    alpha: float
    beta: float
    notes: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return (self.pos_count + self.neg_count) >= MIN_FEEDBACK

    def summary(self) -> str:
        return (f"你选了 {self.pos_count} 张、删了 {self.neg_count} 张 → "
                f"重排权重 +{self.alpha:.2f} / −{self.beta:.2f}")


def compute_tuning(
    db: Database,
    *,
    query_row: dict[str, Any],
    feedback: dict[int, str],
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
) -> Tuning:
    """从用户的表态算出正/负例均值向量。

    **用均值而不是最大相似度**：均值抵抗单张误点，而且天然可复算。
    正例少于 1 张时没有正均值（不猜）；反例同理。
    """
    image_model, image_model_version, prep_version = resolve_image_identity(db, query_row)
    dim = int(query_row["dim"])
    notes: list[str] = []

    keep_ids = [int(s) for s, a in feedback.items() if a == "keep"]
    drop_ids = [int(s) for s, a in feedback.items() if a == "drop"]
    wanted = keep_ids + drop_ids
    rows = SemanticRepo(db).shot_vectors(
        model=image_model, model_version=image_model_version,
        prep_version=prep_version, shot_ids=wanted)
    vectors = {int(r["shot_id"]): _decode(r["vector"], int(r["dim"])) for r in rows}

    missing = [s for s in wanted if s not in vectors]
    if missing:
        notes.append(
            f"{len(missing)} 个表态的照片在这个模型身份下没有向量（已跳过）：{missing[:5]}")

    def mean_of(ids: Sequence[int]) -> np.ndarray | None:
        usable = [vectors[i] for i in ids if i in vectors]
        if not usable:
            return None
        stacked = np.vstack(usable)
        mean = stacked.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm <= 1e-9:
            return None
        return (mean / norm).astype(np.float32)

    pos_vector = mean_of(keep_ids)
    neg_vector = mean_of(drop_ids)
    if pos_vector is None and keep_ids:
        notes.append("选中的照片向量互相抵消（均值为零向量），未启用正向重排。")
    if not keep_ids:
        notes.append("你还没有选中任何一张：参数只能往下压，不能往上抬。")
    return Tuning(
        pos_vector=pos_vector, neg_vector=neg_vector,
        pos_count=len([i for i in keep_ids if i in vectors]),
        neg_count=len([i for i in drop_ids if i in vectors]),
        alpha=float(alpha), beta=float(beta), notes=notes,
    )


def rerank(
    pool: ShotPool,
    *,
    query_vector: np.ndarray,
    tuning: Tuning | None,
    feedback: dict[int, str],
    limit: int,
    base_scores: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """按（基础分 + 学习项）给全库排序。**被删掉的照片直接排除**。

    `base_scores` 是 `semantic_query_hit` 里模型当时的分数 —— 用来在
    返回里如实展示"模型的原始判断"，让用户能看出学习改变了什么。
    """
    if len(pool) == 0:
        return []
    base_scores = base_scores or {}

    # 文本向量与图像向量维度必须一致 —— 不一致就是身份串了，必须报错而不是广播
    query = np.asarray(query_vector, dtype=np.float32).ravel()
    if query.shape[0] != pool.dim:
        raise ValueError(
            f"文本向量维度 {query.shape[0]} 与图像向量维度 {pool.dim} 不一致："
            "两者必须来自同一个模型身份")

    score = pool.matrix @ query
    if tuning is not None and tuning.pos_vector is not None:
        score = score + float(tuning.alpha) * (pool.matrix @ tuning.pos_vector)
    if tuning is not None and tuning.neg_vector is not None:
        score = score - float(tuning.beta) * (pool.matrix @ tuning.neg_vector)

    dropped = {int(s) for s, a in feedback.items() if a == "drop"}
    kept = {int(s) for s, a in feedback.items() if a == "keep"}

    order = np.argsort(-score)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position in order:
        shot_id = int(pool.shot_ids[int(position)])
        if shot_id in dropped or shot_id in seen:
            continue
        seen.add(shot_id)
        row = dict(pool.rows[int(position)])
        row["shot_id"] = shot_id
        row["score"] = float(score[int(position)])
        row["base_score"] = base_scores.get(shot_id)
        out.append(row)
        if len(out) >= int(limit) and kept <= seen:
            break

    # 用户选中但没排进前 N 的，**必须**出现在结果里 —— 那是他自己放进去的
    for shot_id in sorted(kept - seen):
        index = pool.index.get(shot_id)
        if index is None:
            continue
        row = dict(pool.rows[index])
        row["shot_id"] = shot_id
        row["score"] = float(score[index])
        row["base_score"] = base_scores.get(shot_id)
        out.append(row)
        seen.add(shot_id)
    return out


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_BETA",
    "MIN_FEEDBACK",
    "ShotPool",
    "Tuning",
    "build_pool",
    "compute_tuning",
    "rerank",
    "resolve_image_identity",
]
