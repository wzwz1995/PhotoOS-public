"""内容哈希（Phase 1 的统一实现）。

为什么单独成一个模块：`jobs.pipelines`（流水线）与 `library.rebind`（重绑定）都需要哈希，
但 `jobs` 依赖 `library`（遍历器、Library 存储）。若把哈希留在 `jobs.pipelines` 里，
`library.rebind` 反向导入就会形成循环依赖。放在这里两者都能依赖，依赖方向保持单向。

约定（PHASE1_TECH_PLAN §11.4(5)）：
  * 主算法 **BLAKE3**（用户决策：重新初始化，不承接既有 SHA256 缓存）。
  * `hash_algo` 随行存储，因此未来切换算法不会污染历史数据。
  * **只读**：本模块绝不写入或修改任何文件。
"""

from __future__ import annotations

from pathlib import Path

import blake3

#: 主算法标识，写入 `asset.hash_algo`
HASH_ALGO = "blake3"

#: 读取块大小：1 MiB 兼顾顺序读效率与内存占用
READ_CHUNK = 1024 * 1024

#: SHA256 只用于与既有工具产物做一次性交叉校验（迁移/审计），不作为主算法
ALT_HASH_ALGO = "sha256"


def hash_file(path: Path, *, chunk_size: int = READ_CHUNK) -> str:
    """计算文件内容 BLAKE3（十六进制小写）。分块读取，不会把整个文件读进内存。

    使用 `max_threads=AUTO` 让 BLAKE3 走 SIMD/内部并行：在磁盘受限场景下
    CPU 侧远不是瓶颈（实测该外置盘冷读仅 55~71 MB/s）。
    """
    hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
    with open(path, "rb", buffering=chunk_size) as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def hash_sha256(path: Path, *, chunk_size: int = READ_CHUNK) -> str:
    """SHA256（大写，与既有 PowerShell 工具的缓存格式一致）。仅用于交叉校验。"""
    import hashlib

    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest().upper()


def hash_bytes(payload: bytes, *, algo: str = HASH_ALGO) -> str:
    """内存数据的哈希（测试与小文件用）。"""
    if algo == ALT_HASH_ALGO:
        import hashlib

        return hashlib.sha256(payload).hexdigest().upper()
    return blake3.blake3(payload).hexdigest()


__all__ = ["ALT_HASH_ALGO", "HASH_ALGO", "READ_CHUNK", "hash_bytes", "hash_file", "hash_sha256"]
