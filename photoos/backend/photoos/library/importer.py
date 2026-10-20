"""导入：从 SD 卡 / 相机挂载目录 / 普通文件夹把照片收进 Library（PRD §9.1）。

**安全纪律（P0）**：
  * 这是 PhotoOS 唯一会**写入照片库**的能力，因此规则写得比别处更严：
    - 先复制到临时文件 → 校验 size + BLAKE3 → 再原子改名落地。**绝不半途留下坏文件。**
    - **同名冲突永不覆盖**：目标已存在时比较内容哈希，相同则跳过，不同则改名 `__2`/`__3`。
    - **源文件永不删除、永不移动**：导入是"复制"，不是"剪切"。
    - 已存在的相同内容（按哈希）不会重复复制（幂等）。
  * 支持"仅校验不写入"的预演（`apply=False`），让用户先看清会做什么。

与既有 PowerShell 工具的关系：接管其 `import-from-camera.ps1` 的**暂存目录**部分
（`E:\\_相机导入\\<时间戳>\\`），MTP 设备直读尚未实现，属已知限制。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS
from ..db import Database, utc_now
from ..errors import PhotoOSError, SafetyError
from ..hashing import HASH_ALGO, hash_file
from ..media.detect import detect_by_extension_only
from ..repos import AssetRepo
from . import paths, walker
from .store import LibraryStore


@dataclass
class ImportPlanItem:
    source: Path
    relative: str
    size: int
    decision: str          # copy | skip-duplicate | rename | unsupported
    target_relative: str | None = None
    note: str = ""


@dataclass
class ImportReport:
    source_root: Path
    library_id: int
    library_root: Path
    apply: bool
    planned: int = 0
    copied: int = 0
    skipped: int = 0
    renamed: int = 0
    failed: int = 0
    bytes_copied: int = 0
    items: list[ImportPlanItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        mode = "已导入" if self.apply else "预演"
        return (
            f"{mode}：待处理 {self.planned}，复制 {self.copied}，"
            f"跳过（内容已存在）{self.skipped}，改名避免覆盖 {self.renamed}，失败 {self.failed}，"
            f"{self.bytes_copied / 1e6:.1f} MB"
        )

    def lines(self) -> list[str]:
        out = [
            f"源：{self.source_root}",
            f"目标 Library：#{self.library_id} → {self.library_root}",
            f"{self.summary}",
        ]
        for item in self.items[:20]:
            marker = {
                "copy": "复制",
                "skip-duplicate": "跳过(内容已存在)",
                "rename": "改名复制",
                "unsupported": "不支持",
            }.get(item.decision, item.decision)
            out.append(f"  [{marker}] {item.relative}" + (f" → {item.target_relative}" if item.target_relative else ""))
        if len(self.items) > 20:
            out.append(f"  … 其余 {len(self.items) - 20} 项省略")
        out.extend(f"  错误：{message}" for message in self.errors[:5])
        if not self.apply:
            out.append("这是预演，未写入任何文件。去掉 --apply 之外的确认项后再执行实际导入。")
        return out


# --------------------------------------------------------------------------- 计划
def plan_import(
    db: Database,
    library_id: int,
    source_root: Path,
    *,
    subdir: str | None = None,
    limit: int | None = None,
) -> tuple[ImportReport, list[ImportPlanItem]]:
    """生成导入计划。**只读源与库，不写任何文件**（预演与实际导入共用）。"""
    source_root = Path(source_root)
    store = LibraryStore(db)
    library = store.get(library_id)
    if not source_root.is_dir():
        raise PhotoOSError(paths.explain_unusable_source(str(source_root), source_root))
    if not library.root_path_hint:
        raise PhotoOSError(f"Library #{library.id} 没有可用的路径提示，无法确定导入目标")
    library_root = Path(library.root_path_hint)

    # 安全：源不能是库自身的一部分（否则会自我复制）
    if paths.is_same_root(library_root, source_root):
        raise SafetyError(
            f"源目录位于照片库内部，拒绝导入（会自我复制）：{source_root}",
            detail={"kind": "source_inside_library"},
        )

    report = ImportReport(
        source_root=source_root, library_id=library.id, library_root=library_root, apply=False
    )
    assets = AssetRepo(db)
    known_hashes = {
        str(row["content_hash"])
        for row in db.query(
            "SELECT content_hash FROM asset WHERE library_id = ? AND content_hash IS NOT NULL",
            (library.id,),
        )
    }

    items: list[ImportPlanItem] = []
    for entry in walker.walk_media(source_root, include_videos=True):
        if isinstance(entry, walker.WalkError):
            report.errors.append(f"{entry.relative}: {entry.message}")
            continue
        if limit is not None and len(items) >= limit:
            break

        detected = detect_by_extension_only(entry.path)
        if detected is None:
            items.append(ImportPlanItem(entry.path, entry.relative, entry.size, "unsupported"))
            continue

        relative = _target_relative(entry, subdir)
        target = library_root / Path(relative.replace("\\", "/"))

        if target.exists():
            # 同名文件已存在：**绝不覆盖**，比较内容决定跳过还是改名
            try:
                same = target.stat().st_size == entry.size and hash_file(target) == hash_file(entry.path)
            except OSError:
                same = False
            if same:
                items.append(
                    ImportPlanItem(entry.path, entry.relative, entry.size, "skip-duplicate",
                                   relative, "目标位置已有内容相同的文件")
                )
                continue
            relative = _next_free_relative(library_root, relative)
            target = library_root / Path(relative.replace("\\", "/"))
            items.append(
                ImportPlanItem(entry.path, entry.relative, entry.size, "rename", relative,
                               "同名但内容不同，改名以免覆盖")
            )
            continue

        # 库内已有相同内容（其它路径）→ 不重复复制
        try:
            digest = hash_file(entry.path)
        except OSError as exc:
            items.append(ImportPlanItem(entry.path, entry.relative, entry.size, "unsupported",
                                        None, f"无法读取：{exc}"))
            continue
        if digest in known_hashes:
            items.append(
                ImportPlanItem(entry.path, entry.relative, entry.size, "skip-duplicate", None,
                               "库内已存在相同内容的文件")
            )
            continue

        items.append(ImportPlanItem(entry.path, entry.relative, entry.size, "copy", relative))

    report.items = items
    report.planned = sum(1 for item in items if item.decision in {"copy", "rename"})
    report.skipped = sum(1 for item in items if item.decision == "skip-duplicate")
    report.renamed = sum(1 for item in items if item.decision == "rename")
    del assets
    return report, items


def _target_relative(entry: walker.FileEntry, subdir: str | None) -> str:
    base = entry.relative
    if subdir:
        prefix = subdir.replace("/", "\\").strip("\\")
        base = f"{prefix}\\{base}"
    paths.validate_relative(base)
    return base


def _next_free_relative(library_root: Path, relative: str) -> str:
    """为同名冲突找一个不覆盖任何文件的新名字（`__2`、`__3`…）。"""
    candidate = Path(relative.replace("\\", "/"))
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 1000):
        alternative = candidate.with_name(f"{stem}__{index}{suffix}")
        if not (library_root / alternative).exists():
            return str(alternative).replace("/", "\\")
    raise PhotoOSError(f"无法为 {relative} 找到可用的备用文件名（已尝试 999 次）")


# --------------------------------------------------------------------------- 执行
def execute_import(
    db: Database,
    library_id: int,
    source_root: Path,
    *,
    subdir: str | None = None,
    limit: int | None = None,
    progress=None,
) -> ImportReport:
    """执行导入。源文件只读，目标写入**先复制到临时文件、校验后再原子改名**。"""
    report, items = plan_import(db, library_id, source_root, subdir=subdir, limit=limit)
    report.apply = True
    library_root = report.library_root
    touched: list[str] = []

    for index, item in enumerate(items, start=1):
        if item.decision not in {"copy", "rename"} or item.target_relative is None:
            continue
        target = library_root / Path(item.target_relative.replace("\\", "/"))
        # 明确不写照片库之外
        if not paths.is_same_root(library_root, target):
            report.failed += 1
            report.errors.append(f"{item.relative}: 目标越出 Library 根，已拒绝")
            continue
        try:
            digest = _copy_verified(item.source, target)
        except (OSError, PhotoOSError) as exc:
            report.failed += 1
            report.errors.append(f"{item.relative}: {exc}")
            # 失败不留半成品
            _cleanup_temp(target)
            continue
        report.copied += 1
        report.bytes_copied += item.size
        touched.append((item.target_relative, digest))
        if progress is not None:
            progress(index, len(items), f"导入 {index}/{len(items)}")

    if touched:
        LibraryStore(db).log_event(
            "import_copy",
            library_id=library_id,
            payload={"source": str(source_root), "copied": len(touched), "subdir": subdir},
        )
        # 让新文件立刻出现在索引里。**同时写入内容哈希** —— 复制时已经算过，
        # 顺带落库可以让"按内容去重"在导入阶段就生效（否则相同内容会被重复复制进来），
        # 也省掉一次全文件读取。
        rows = []
        for relative, digest in touched:
            absolute = library_root / Path(relative.replace("\\", "/"))
            stat = absolute.stat()
            detected = detect_by_extension_only(absolute)
            rows.append(
                (
                    relative,
                    stat.st_size,
                    stat.st_mtime_ns,
                    detected.file_type if detected else "other",
                    detected.mime if detected else None,
                )
            )
        AssetRepo(db).upsert_discovered(library_id, rows, now=utc_now())

        with db.write() as conn:
            for relative, digest in touched:
                conn.execute(
                    "UPDATE asset SET content_hash = ?, hash_algo = ?"
                    " WHERE library_id = ? AND relative_path = ?",
                    (digest, HASH_ALGO, library_id, relative),
                )
    return report


def _copy_verified(source: Path, target: Path, *, chunk: int = 1024 * 1024) -> str:
    """复制并校验，返回内容 BLAKE3。

    步骤：复制到 `<target>.photoos-part` → 比对 size 与**内容哈希** → `os.replace` 原子改名。
    任何一步失败都不留半成品，也不触碰目标位置的既有文件（PRD §2.1 / §11.1）。
    """
    if target.exists():
        raise SafetyError(f"目标已存在，拒绝覆盖：{target}", detail={"kind": "target_exists"})
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".photoos-part")

    size = 0
    try:
        with open(source, "rb") as reader, open(temp, "wb", buffering=chunk) as writer:
            while True:
                block = reader.read(chunk)
                if not block:
                    break
                writer.write(block)
                size += len(block)
            writer.flush()
            os.fsync(writer.fileno())  # 确保落盘后再校验，避免拿到页缓存里的假象

        expected_size = source.stat().st_size
        if size != expected_size:
            raise PhotoOSError(f"复制不完整：期望 {expected_size} 字节，实际 {size} 字节")
        source_digest = hash_file(source)
        if source_digest != hash_file(temp):
            raise PhotoOSError("复制后内容哈希与源不一致，已丢弃半成品")

        os.replace(temp, target)  # 原子；此时才真正出现在库里
        return source_digest
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _cleanup_temp(target: Path) -> None:
    target.with_name(target.name + ".photoos-part").unlink(missing_ok=True)


def import_supported_extensions() -> set[str]:
    """导入时会被收进库的扩展名（照片 + 视频）。"""
    return set(PHOTO_EXTENSIONS) | set(VIDEO_EXTENSIONS)


__all__ = [
    "HASH_ALGO",
    "ImportPlanItem",
    "ImportReport",
    "execute_import",
    "import_supported_extensions",
    "plan_import",
]
