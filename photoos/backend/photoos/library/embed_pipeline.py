"""嵌入流水线（Phase 2 · M3 落库）。

把"已有 preview → CLIP 编码 → 写 `embedding` 表"串成一步，供 CLI 与后台任务共用。

## 三个关键设计点

**① 只读 preview，绝不读原片。**
实测（`PHASE2_RESEARCH` §4）：preview→224 与 原片→224 的同图余弦 0.832–0.996，
而"不同图"只有 0.463–0.652 —— 分布不重叠，因此用 preview 编码是等价的，
而 IO 从 413 GB 降到 3.8 GB（**109 倍**）。而且 preview 在 `D:` 本地盘上，
所以**拔掉照片硬盘也能重算嵌入**。

**② 增量，已生成的不重复生成。**
`missing_assets()` 用三个条件筛：`status='OK'` + 有 preview + 该版本尚无向量。

**③ 单张失败不阻塞整批（PRD §11.1）。**
逐条记录失败原因，最后汇总；成功的照常写入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import model_store
from .embedding import DEFAULT_BATCH, Embedder
from .model_store import EmbeddingNotAvailable


@dataclass
class EmbedReport:
    """一次嵌入运行的结果（CLI / 任务面板共用）。"""

    total: int = 0
    embedded: int = 0
    failed: int = 0
    skipped: int = 0
    device: str = "unknown"
    model: str = model_store.MODEL_NAME
    model_version: str = model_store.MODEL_VERSION
    prep_version: str = model_store.PREP_VERSION
    elapsed_s: float = 0.0
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def throughput(self) -> float:
        return self.embedded / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def lines(self) -> list[str]:
        out = [
            f"嵌入完成：{self.embedded}/{self.total} 张"
            + (f"，失败 {self.failed}" if self.failed else "")
            + (f"，跳过 {self.skipped}" if self.skipped else ""),
            f"  设备 {self.device}，用时 {self.elapsed_s:.1f} s（{self.throughput:.1f} 张/秒）",
            f"  模型 {self.model} @ {self.model_version}，预处理口径 {self.prep_version}",
        ]
        for asset_id, error in self.failures[:10]:
            out.append(f"    [失败] asset {asset_id}: {error}")
        if len(self.failures) > 10:
            out.append(f"    ……另有 {len(self.failures) - 10} 条失败")
        return out


def run_embedding(
    db: Any,
    data_root: Path,
    *,
    embedder: Embedder,
    library_id: int | None = None,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH,
    workers: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> EmbedReport:
    """对"还没有该版本向量"的资产算嵌入并落库。

    `embedder` 由调用方注入 —— 这样本函数可以在**没有 ML 运行时**的环境里被测试
    （用一个返回确定性向量的假 embedder），而不需要真的跑模型。

    ## 为什么要开线程池做预处理（性能关键）

    调研实测（`PHASE2_RESEARCH` §6.1）：**推理只占端到端的 6%，94% 是图像预处理**
    （读 WebP、缩放到 224、中心裁剪、归一化）。

    | 做法 | 吞吐 | 21,400 张 |
    | --- | ---: | ---: |
    | 单线程预处理 + GPU | 19.8 img/s | **≈18 分钟** |
    | **8 线程预处理 + GPU** | **111.3 img/s** | **≈3.2 分钟** |

    第一版实现漏了线程池，在真实库演练里实测 **1222 秒（20.4 分钟）** —— 与"单线程"那一行
    几乎完全吻合，即**计划的 3.2 分钟变成了 20 分钟（差 6 倍）**。
    Pillow 解码与 numpy 运算都会释放 GIL，因此线程池有效（实测 6 线程 5.3×、8 线程 6.0×）。
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ..repos import EmbeddingRepo

    repo = EmbeddingRepo(db)
    pending = repo.missing_assets(
        model=embedder.model,
        model_version=embedder.model_version,
        prep_version=embedder.prep_version,
        library_id=library_id,
        limit=limit,
    )

    report = EmbedReport(
        total=len(pending),
        device=getattr(embedder, "device", "unknown"),
        # ⚠️ 必须从 **embedder 本身**取身份，不能用 dataclass 的默认值 ——
        # 那些默认值是 CLIP 的（历史原因），而 Phase 3 起 EG2 是第二个引擎。
        # 漏了这三行，报告会说"模型 clip-vit-base-patch32"而实际写的是 768 维的
        # embeddinggemma-2 向量 —— 正是本项目最忌讳的"提示说错了"（交付文档 §6.17）。
        model=getattr(embedder, "model", model_store.MODEL_NAME),
        model_version=getattr(embedder, "model_version", model_store.MODEL_VERSION),
        prep_version=getattr(embedder, "prep_version", model_store.PREP_VERSION),
    )
    if not pending:
        return report

    # 预处理线程数：默认 6（实测 6 线程 105.7 img/s、8 线程 118.9，再往上收益递减
    # 而峰值内存上升；全流程峰值工作集约 2.0 GB）。
    pool_size = max(1, int(workers if workers is not None else 6))
    batch = max(1, batch_size)
    own_pool = getattr(embedder, "preprocess", None) is not None
    if not own_pool:
        # 注入的 embedder 若不支持"预处理与推理分离"，退回它自己的路径（测试用假 embedder 即走这条）
        return _run_embedding_serial(
            db, data_root, embedder=embedder, pending=pending, report=report,
            batch_size=batch, on_progress=on_progress,
        )

    started = time.perf_counter()
    rows: list[dict[str, Any]] = []

    def prepare(item: dict[str, Any]):
        """读 preview 并做 CLIP 预处理（纯 CPU、可并行）。"""
        path = Path(data_root) / str(item["preview_path"]).replace("/", "\\")
        if not path.is_file():
            return item, None, f"预览文件缺失：{item['preview_path']}"
        try:
            from PIL import Image

            with Image.open(path) as handle:
                handle.load()
                array = embedder.preprocess(handle)      # type: ignore[attr-defined]
            return item, array, None
        except Exception as exc:  # noqa: BLE001 - 单张失败不拖垮整批
            return item, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        for offset in range(0, len(pending), batch):
            chunk = pending[offset : offset + batch]
            ready_items: list[dict[str, Any]] = []
            ready_arrays: list[Any] = []
            for item, array, error in pool.map(prepare, chunk):
                if error is not None:
                    report.failed += 1
                    report.failures.append((int(item["asset_id"]), str(error)))
                    continue
                ready_items.append(item)
                ready_arrays.append(array)

            if ready_arrays:
                try:
                    vectors = embedder.embed_arrays(ready_arrays)      # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    message = f"{type(exc).__name__}: {exc}"
                    for item in ready_items:
                        report.failed += 1
                        report.failures.append((int(item["asset_id"]), message))
                else:
                    rows.extend(
                        _row(embedder, item, vector)
                        for item, vector in zip(ready_items, vectors)
                    )

            if len(rows) >= 500:
                report.embedded += _store(repo, rows, report)
                rows = []
            if on_progress:
                on_progress(min(offset + len(chunk), len(pending)), len(pending))

    if rows:
        report.embedded += _store(repo, rows, report)
    report.elapsed_s = time.perf_counter() - started
    return report


def _store(repo: Any, rows: list[dict[str, Any]], report: EmbedReport) -> int:
    """写一批向量；**磁盘写满/库锁住时给出可操作的说明**，而不是裸的 sqlite 报错。

    为什么值得单独包一层：`embed` 是六步里最长的一步（本库约 6 分钟），
    而它一直在往数据库里写（21,400 × 512 × 4 B ≈ 43.7 MB）。中途磁盘写满时，
    用户看到的原本是一句 `sqlite3.OperationalError: database or disk is full` ——
    没有告诉他"**已经写进去的都在，腾出空间后原样重跑即可**"，
    也没告诉他"向量是分批提交的，不会留下半成品"。

    实测（§13.41）确认了这个性质：强杀 embed 之后库仍自洽、重跑幂等可续。
    所以这里只需要把**已经成立的性质**翻译成人话。
    """
    try:
        return int(repo.store_many(rows))
    except Exception as exc:  # noqa: BLE001 - 要按错误类型给出不同建议
        text = str(exc).lower()
        # 注意：`EmbeddingNotAvailable` 继承自 `RuntimeError`（不是 `PhotoOSError`），
        # 构造签名只有 message —— 不接 `detail=`。别照抄 PhotoOSError 的用法。
        if "disk" in text or "full" in text or "no space" in text:
            raise EmbeddingNotAvailable(
                "写向量时磁盘空间不足，嵌入已中断。"
                f"已写入的 {report.embedded:,} 条**都已提交、不会丢**；"
                "腾出空间后原样重跑 `photoos embed --apply` 即可从断点继续（不会重复写入）。"
            ) from exc
        if "locked" in text or "busy" in text:
            raise EmbeddingNotAvailable(
                "数据库被另一个进程占住，写入失败。"
                "请确认没有第二个 `photoos` 在跑（尤其是同时开着 `serve` 做重算），然后重跑 ——"
                f"已写入的 {report.embedded:,} 条会保留。"
            ) from exc
        raise


def _row(embedder: Embedder, item: dict[str, Any], vector) -> dict[str, Any]:
    return {
        "asset_id": item["asset_id"],
        "model": embedder.model,
        "model_version": embedder.model_version,
        "model_sha256": model_store.MODEL_FILES[0][3],
        "dim": embedder.dim,
        "normalization": embedder.normalization,
        "input_kind": "preview",
        "input_version": item.get("preview_version") or "",
        "prep_version": embedder.prep_version,
        "vector": _pack(vector),
    }


def _run_embedding_serial(
    db: Any,
    data_root: Path,
    *,
    embedder: Embedder,
    pending: list[dict[str, Any]],
    report: EmbedReport,
    batch_size: int,
    on_progress: Callable[[int, int], None] | None,
) -> EmbedReport:
    """给"不支持预处理/推理分离"的 embedder 用的串行回退路径。

    产品实现（`ClipOnnxEmbedder`）走并行路径；这个回退保证**测试用的假 embedder**
    与任何第三方实现都能工作（接口更宽松，不强制实现 `preprocess`）。
    """
    import time

    from ..repos import EmbeddingRepo

    repo = EmbeddingRepo(db)
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(pending), batch_size):
        chunk = pending[offset : offset + batch_size]
        paths: list[Path] = []
        usable: list[dict[str, Any]] = []
        for item in chunk:
            path = Path(data_root) / str(item["preview_path"]).replace("/", "\\")
            if not path.is_file():
                report.failed += 1
                report.failures.append((int(item["asset_id"]), f"预览文件缺失：{item['preview_path']}"))
                continue
            paths.append(path)
            usable.append(item)
        if not paths:
            continue
        try:
            vectors = embedder.embed_paths_batched(paths, batch_size=len(paths))
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            for item in usable:
                report.failed += 1
                report.failures.append((int(item["asset_id"]), message))
            continue
        rows.extend(_row(embedder, item, vector) for item, vector in zip(usable, vectors))
        if len(rows) >= 500:
            report.embedded += _store(repo, rows, report)
            rows = []
        if on_progress:
            on_progress(min(offset + len(chunk), len(pending)), len(pending))
    if rows:
        report.embedded += _store(repo, rows, report)
    report.elapsed_s = time.perf_counter() - started
    return report


def _pack(vector) -> bytes:
    """按契约打包成 little-endian float32（显式字节序，跨机迁移才安全）。"""
    from .vectorstore import pack_vector

    return pack_vector(vector)


def dry_run_summary(
    db: Any,
    *,
    embedder: Embedder | None = None,
    library_id: int | None = None,
) -> dict[str, Any]:
    """只统计不写库 —— 供 `embed --dry-run` 与界面预演使用。

    没有可用 embedder（未装运行时/模型）时**照样能报出"有多少张需要算"**，
    因为这只依赖数据库。
    """
    from ..repos import EmbeddingRepo

    repo = EmbeddingRepo(db)
    model = getattr(embedder, "model", model_store.MODEL_NAME)
    model_version = getattr(embedder, "model_version", model_store.MODEL_VERSION)
    prep = getattr(embedder, "prep_version", model_store.PREP_VERSION)

    pending = repo.missing_assets(
        model=model, model_version=model_version, prep_version=prep, library_id=library_id
    )
    return {
        "identity": {
            "model": model,
            "model_version": model_version,
            "prep_version": prep,
            "dim": getattr(embedder, "dim", model_store.MODEL_DIM),
            "normalization": getattr(embedder, "normalization", model_store.MODEL_NORMALIZATION),
        },
        "pending": len(pending),
        "existing_versions": repo.identities(),
        "device": getattr(embedder, "device", "unknown"),
    }


__all__ = ["EmbedReport", "dry_run_summary", "run_embedding"]
