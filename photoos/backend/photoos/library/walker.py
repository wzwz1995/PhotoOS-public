"""只读文件遍历器。

设计要点：
  * **严格只读**：只做 `os.scandir` / `stat`，绝不打开文件内容、绝不修改任何东西。
  * 稳定顺序：每个目录内按名称排序，因此同一棵树的遍历顺序可复现（重扫不会乱序）。
  * 防御性：跳过系统目录与既有流水线 bookkeeping 目录；跟踪真实路径以规避
    junction / symlink 造成的目录环；权限错误记录后继续，不中断整批（PRD §11.1）。
  * 单次 `scandir` 同时拿到 size/mtime（Windows 上 DIRENT 已含这些字段），
    避免逐文件 `stat` 造成额外 IO —— 对 10 万张规模很重要。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import IGNORED_EXTENSIONS, PHOTO_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS
from . import paths


def classify_extension(ext: str) -> str | None:
    """把扩展名映射到 file_type；非媒体返回 None。"""
    ext = ext.lower()
    if ext in RAW_EXTENSIONS:
        return "raw"
    if ext in PHOTO_EXTENSIONS:
        if ext in {".jpg", ".jpeg", ".jpe"}:
            return "jpeg"
        if ext in {".heic", ".heif", ".avif"}:
            return "heic"
        if ext == ".png":
            return "png"
        if ext in {".tif", ".tiff"}:
            return "tiff"
        if ext == ".webp":
            return "webp"
        return "other"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


@dataclass(frozen=True, slots=True)
class FileEntry:
    """遍历得到的一个候选文件。`relative` 已通过 `paths.validate_relative`。"""

    path: Path
    relative: str
    size: int
    mtime_ns: int
    file_type: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()


@dataclass(slots=True)
class WalkStats:
    """遍历统计，用于 ScanResult 与断点报告。"""

    files_seen: int = 0            # 所有常规文件
    media_files: int = 0           # 被判定为照片/视频的文件
    dirs_visited: int = 0
    skipped_dirs: int = 0
    skipped_ignored_ext: int = 0
    symlink_dirs_skipped: int = 0
    loop_dirs_skipped: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class WalkError:
    path: Path
    relative: str
    message: str


def _scan_directory(directory: str) -> tuple[list[os.DirEntry[str]], OSError | None]:
    """一次 scandir 取出整个目录并按名称排序。返回 (条目, 错误)。"""
    try:
        with os.scandir(directory) as iterator:
            entries = list(iterator)
    except OSError as exc:
        return [], exc
    entries.sort(key=lambda e: e.name.casefold())
    return entries, None


def walk_media(
    root: Path,
    *,
    include_photos: bool = True,
    include_videos: bool = True,
    include_other_files: bool = False,
    follow_dir_symlinks: bool = False,
    max_depth: int | None = None,
) -> Iterator[FileEntry | WalkError]:
    """深度优先遍历 `root`，产出媒体文件条目与错误条目。

    调用方通过 `isinstance(item, WalkError)` 区分。永远是只读操作。
    """
    root = Path(root)
    if not root.is_dir():
        yield WalkError(path=root, relative="", message="目录不存在或不可访问")
        return

    stack: list[tuple[str, int]] = [(str(root), 0)]
    seen_real_dirs: set[str] = set()

    while stack:
        directory, depth = stack.pop()

        if depth == 0:
            real = _real_key(directory)
            if real:
                seen_real_dirs.add(real)
        else:
            real = _real_key(directory)
            if real is not None and real in seen_real_dirs:
                # 目录环（junction / symlink 回指）：跳过，避免无限递归
                yield WalkError(path=Path(directory), relative=_safe_relative(root, directory),
                                message="检测到目录环，已跳过")
                continue
            if real:
                seen_real_dirs.add(real)

        entries, error = _scan_directory(directory)
        if error is not None:
            yield WalkError(
                path=Path(directory),
                relative=_safe_relative(root, directory),
                message=f"{type(error).__name__}: {error}",
            )
            continue

        subdirs: list[str] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=follow_dir_symlinks):
                    if paths.should_skip_dir(entry.name):
                        continue
                    if not follow_dir_symlinks and entry.is_symlink():
                        continue
                    subdirs.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=True):
                    continue
            except OSError as exc:
                yield WalkError(
                    path=Path(entry.path),
                    relative=_safe_relative(root, entry.path),
                    message=f"{type(exc).__name__}: {exc}",
                )
                continue

            file_type = classify_extension(Path(entry.name).suffix)
            if file_type is None and not include_other_files:
                continue
            if file_type == "video" and not include_videos:
                continue
            if file_type not in (None, "video") and not include_photos:
                continue
            if file_type is None:
                if Path(entry.name).suffix.lower() in IGNORED_EXTENSIONS:
                    continue
                file_type = "other"

            try:
                stat = entry.stat(follow_symlinks=True)
                relative = paths.to_relative(root, Path(entry.path))
            except OSError as exc:
                yield WalkError(
                    path=Path(entry.path),
                    relative=_safe_relative(root, entry.path),
                    message=f"{type(exc).__name__}: {exc}",
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 单文件失败不得中断整批（PRD §11.1）
                yield WalkError(
                    path=Path(entry.path),
                    relative=_safe_relative(root, entry.path),
                    message=f"{type(exc).__name__}: {exc}",
                )
                continue

            yield FileEntry(
                path=Path(entry.path),
                relative=relative,
                size=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
                file_type=file_type,
            )

        if max_depth is not None and depth >= max_depth:
            continue
        # 逆序入栈，使弹出顺序与名称升序一致（稳定遍历顺序）
        for subdir in reversed(subdirs):
            stack.append((subdir, depth + 1))


def _real_key(path: str) -> str | None:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return None


def _safe_relative(root: Path, path: str | Path) -> str:
    try:
        return paths.to_relative(root, Path(path))
    except Exception:  # noqa: BLE001 - 仅用于错误报告
        return str(path)


def count_media(root: Path, **kwargs: object) -> tuple[int, int]:
    """轻量统计：(媒体文件数, 错误数)。只遍历不建库。"""
    media = 0
    errors = 0
    for item in walk_media(root, **kwargs):  # type: ignore[arg-type]
        if isinstance(item, WalkError):
            errors += 1
        else:
            media += 1
    return media, errors
