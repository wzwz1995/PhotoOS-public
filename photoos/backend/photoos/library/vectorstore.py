"""向量检索抽象（PRD §10：「VectorStore：通过接口抽象；不要让业务层直接依赖某一个向量数据库」）。

## 为什么本期只有 numpy 一个实现

调研阶段在真实数据（16,146 × 512）上实测：

| 操作 | 实测 |
| --- | ---: |
| 单查询 top-k（暴力扫全库） | **0.24–0.27 ms** |
| 全对全 16,146² 矩阵乘法 | 0.88 s（结果矩阵 1.04 GB） |

**这比一次 SQLite 分页查询还便宜。** 因此本期**不引入** faiss / 向量数据库 / 近似索引 ——
那是 PRD §11.3「反过度工程」明确禁止的方向。

## 到 10 万级怎么办（只做判断，不实现）

外推：单查询 ≈ 1.7 ms（仍然够快，**不需要 ANN**）；全对全 ≈ 34 s / 40 GB（**矩阵放不下**）。
正确做法是**只在时段内做全对全**（本库最大时段 866 张 → 866² × 512 矩阵 < 10 ms），
而不是把全库两两算一遍。只有出现"交互式全库相似搜索"且规模超百万时，才值得引入 HNSW ——
**换实现即可，本文件的接口不变**。

## 序列化契约（跨机迁移的前提）

向量以 **little-endian float32** 存 `BLOB`，长度必须恰为 `dim * 4`。
显式指定字节序：将来换机或换平台时，字节序不能成为隐患。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

#: 本项目的向量存储格式版本（与 `embedding.prep_version` 是两件事：
#: 这个管"字节怎么摆"，那个管"像素怎么预处理"）
VECTOR_DTYPE = np.dtype("<f4")


class VectorStoreError(RuntimeError):
    """向量维度/长度不符合契约。"""


def pack_vector(vector: "np.ndarray | Sequence[float]") -> bytes:
    """把向量打包成 `BLOB`（little-endian float32）。"""
    array = np.asarray(vector, dtype=VECTOR_DTYPE)
    if array.ndim != 1:
        raise VectorStoreError(f"向量必须是一维，实际 {array.ndim} 维")
    return array.tobytes()


def unpack_vector(blob: bytes, *, dim: int) -> "np.ndarray":
    """从 `BLOB` 还原向量，并**严格校验长度**。"""
    expected = int(dim) * VECTOR_DTYPE.itemsize
    if len(blob) != expected:
        raise VectorStoreError(
            f"向量字节数不符：期望 {expected}（dim={dim} × {VECTOR_DTYPE.itemsize}），实际 {len(blob)}"
        )
    return np.frombuffer(blob, dtype=VECTOR_DTYPE).copy()


def pack_matrix(matrix: "np.ndarray") -> list[bytes]:
    """把一个 `(N, dim)` 矩阵逐行打包。"""
    array = np.asarray(matrix, dtype=VECTOR_DTYPE)
    if array.ndim != 2:
        raise VectorStoreError(f"矩阵必须是二维，实际 {array.ndim} 维")
    return [np.ascontiguousarray(row).tobytes() for row in array]


def l2_normalize(matrix: "np.ndarray") -> "np.ndarray":
    """按行做 L2 归一化。

    归一化的意义：余弦相似度退化为点积，于是"找最像的"就是一次矩阵乘法。
    零向量（理论上不该出现，但可能来自损坏的模型输出）保持为零而不是产生 NaN ——
    静默产生 NaN 会让整个相似度矩阵变成垃圾，比抛错更难查。
    """
    array = np.asarray(matrix, dtype="<f4")
    if array.ndim == 1:
        array = array[None, :]
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    safe = np.where(norms > 0, norms, 1.0)
    return (array / safe).astype("<f4", copy=False)


@dataclass(frozen=True)
class Neighbor:
    """一条近邻结果。"""

    index: int
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """向量检索接口（PRD §10 要求的抽象）。"""

    @property
    def dim(self) -> int: ...

    @property
    def size(self) -> int: ...

    def knn(
        self,
        query: "np.ndarray",
        k: int,
        *,
        allow: "np.ndarray | None" = None,
    ) -> list[Neighbor]:
        """返回最相似的 k 个（按 score 降序）。`allow` 是布尔掩码，用于限定候选集。"""
        ...

    def pairwise_above(self, threshold: float) -> list[tuple[int, int, float]]:
        """返回所有相似度 ≥ threshold 的 `(i, j, score)`（i < j）。"""
        ...


class NumpyVectorStore:
    """numpy 暴力实现（本期唯一实现）。

    全部向量常驻内存：本库 21,400 × 512 × 4 B = **43.7 MB**，完全放得下。
    """

    def __init__(self, *, dim: int, keys: Sequence[int] | None = None) -> None:
        self._dim = int(dim)
        self._keys: list[int] = list(keys) if keys is not None else []
        self._matrix: "np.ndarray" = np.zeros((0, self._dim), dtype="<f4")

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_rows(cls, rows: Sequence[tuple[int, bytes]], *, dim: int) -> "NumpyVectorStore":
        """从 `(key, blob)` 列表构造。key 通常是 `asset_id`。"""
        store = cls(dim=dim, keys=[int(k) for k, _ in rows])
        if rows:
            store._matrix = l2_normalize(
                np.vstack([unpack_vector(blob, dim=dim) for _, blob in rows])
            )
        return store

    @classmethod
    def from_matrix(
        cls, matrix: "np.ndarray", *, keys: Sequence[int] | None = None
    ) -> "NumpyVectorStore":
        array = np.asarray(matrix, dtype="<f4")
        if array.ndim != 2:
            raise VectorStoreError("矩阵必须是二维")
        store = cls(dim=array.shape[1], keys=keys)
        store._matrix = l2_normalize(array)
        return store

    # ------------------------------------------------------------------ 属性
    @property
    def dim(self) -> int:
        return self._dim

    @property
    def size(self) -> int:
        return int(self._matrix.shape[0])

    @property
    def keys(self) -> list[int]:
        return list(self._keys)

    def key_at(self, index: int) -> int:
        """第 `index` 行对应的 key；没有显式 key 时返回 index 本身。"""
        if self._keys:
            return self._keys[int(index)]
        return int(index)

    def matrix(self) -> "np.ndarray":
        """只读副本（调用方不应修改 —— 归一化后的矩阵是整个检索的前提）。"""
        return self._matrix.copy()

    # ------------------------------------------------------------------ 检索
    def knn(
        self,
        query: "np.ndarray",
        k: int,
        *,
        allow: "np.ndarray | None" = None,
    ) -> list[Neighbor]:
        if self.size == 0:
            return []
        vector = l2_normalize(np.asarray(query, dtype="<f4").reshape(1, -1))
        if vector.shape[1] != self._dim:
            raise VectorStoreError(
                f"查询向量维度不符：期望 {self._dim}，实际 {vector.shape[1]}"
            )
        scores = (self._matrix @ vector[0]).astype("<f4", copy=False)

        if allow is not None:
            mask = np.asarray(allow, dtype=bool)
            if mask.shape[0] != self.size:
                raise VectorStoreError(
                    f"allow 掩码长度不符：期望 {self.size}，实际 {mask.shape[0]}"
                )
            # 被排除的位置置 -inf，这样 argpartition 自然把它们排到最后，
            # 不需要先做一次布尔索引（避免为每个查询分配新数组）
            scores = np.where(mask, scores, np.float32("-inf"))

        k = max(1, min(int(k), self.size))
        # argpartition 是 O(n)，比全排序快；只对前 k 个排序
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [
            Neighbor(index=int(i), score=float(scores[int(i)]))
            for i in top
            if np.isfinite(scores[int(i)])
        ]

    def knn_by_key(
        self, key: int, k: int, *, allow: "np.ndarray | None" = None
    ) -> list[Neighbor]:
        """以某个已有向量为查询（"找出和这张最像的其他照片"）。"""
        if not self._keys:
            raise VectorStoreError("未提供 keys，无法按 key 检索")
        try:
            index = self._keys.index(int(key))
        except ValueError as exc:
            raise VectorStoreError(f"key {key} 不在向量库中") from exc
        # 取 k+1 再剔掉自己
        result = self.knn(self._matrix[index], k + 1, allow=allow)
        return [n for n in result if n.index != index][:k]

    def similarity(self, i: int, j: int) -> float:
        """两个位置的余弦相似度（矩阵已归一化，所以是点积）。"""
        return float(self._matrix[int(i)] @ self._matrix[int(j)])

    def pairwise_above(self, threshold: float) -> list[tuple[int, int, float]]:
        """所有相似度 ≥ threshold 的两两组合。

        实现要点：**只算上三角**。本库最大时段 866 张 → 866² ≈ 75 万次点积，
        float32 下不到 10 ms；全库 21,400 若一次算完需要 1.04 GB 的结果矩阵，
        因此**调用方必须先在时段内切片**（见 `PHASE2_TECH_PLAN` §3.10）。
        """
        if self.size < 2:
            return []
        gram = self._matrix @ self._matrix.T
        rows, cols = np.triu_indices(self.size, k=1)
        picked = gram[rows, cols] >= float(threshold)
        return [
            (int(r), int(c), float(gram[r, c]))
            for r, c in zip(rows[picked], cols[picked])
        ]

    def pairwise_above_keys(self, threshold: float) -> list[tuple[int, int, float]]:
        """同 `pairwise_above`，但把位置换成 key（通常是 `asset_id`）。"""
        return [(self.key_at(i), self.key_at(j), s) for i, j, s in self.pairwise_above(threshold)]

    # ------------------------------------------------------------------ 组合
    def subset(self, indices: Sequence[int]) -> "NumpyVectorStore":
        """取子集（用于"只在某个时段内做全对全"）。"""
        idx = [int(i) for i in indices]
        store = NumpyVectorStore(
            dim=self._dim,
            keys=[self._keys[i] for i in idx] if self._keys else idx,
        )
        if idx:
            store._matrix = self._matrix[idx]
        return store


__all__ = [
    "Neighbor",
    "NumpyVectorStore",
    "VECTOR_DTYPE",
    "VectorStore",
    "VectorStoreError",
    "l2_normalize",
    "pack_matrix",
    "pack_vector",
    "unpack_vector",
]
