"""把一批原片**打包成一个 zip**（人物 / 主题的"一键打包"）。

## 为什么是"写到本地的一个 zip"，而不是浏览器流式下载

这个应用跑在用户自己的机器上，原片就在同一个硬盘上（`E:\\照片\\...`）。
让浏览器把几十 GB 的 RAW 重新流一遍再落到"下载"目录，是纯浪费：
真正的瓶颈是磁盘，而目标位置本来就由用户自己定。所以打包的产物是一个
**本地文件**，界面上给出它的完整路径（可复制、可在资源管理器里打开）——
和"单个原片不走浏览器下载、只引导到路径"是同一条纪律。

## 三条硬纪律

1. **绝不修改原片**：整个模块只 `open(path,'rb')` 与 `zipfile.write`，
   没有任何写回原路径的代码路径。
2. **不许悄悄漏**：不在盘上的文件（拔盘、被移走）会**逐个记进 `missing`**，
   报告里如实写出"跳过 N 个"，界面照实显示 —— 一个少了 2 张的包
   如果看起来"成功"，那比报错更糟。
3. **包内路径 = 数据库里的相对路径**：不同目录下的同名文件（两次外拍都有
   `DSC_0001.JPG`）必须都进得去，所以不能拍平成一个目录。
   包内还附一份 `清单.txt`，写明每张的原路径、拍摄时刻与体积。
"""

from __future__ import annotations

import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..db import Database
from .store import LibraryStore

#: zip 里那份清单的文件名（放在包的根）
MANIFEST_NAME = "清单.txt"

#: Windows 文件名里非法的字符 + 控制字符
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_label(text: str, *, fallback: str = "打包", limit: int = 60) -> str:
    """把人物名/主题名变成安全的文件名片段。

    盘符与用户名不会进数据库，但**名字会**（人物名、主题名都是用户写的），
    所以这一段必须在写文件之前做掉：`../` 之类的东西不该出现在输出路径里。
    """
    cleaned = _BAD_NAME.sub("_", str(text or "").strip())
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        cleaned = fallback
    return cleaned[:limit]


@dataclass
class ExportEntry:
    asset_id: int
    relative_path: str      # 库内相对路径（也是包内路径）
    source: Path            # 原片绝对路径
    size: int
    capture_at: str | None = None

    @property
    def arcname(self) -> str:
        return self.relative_path.replace("\\", "/")


@dataclass
class ExportPlan:
    """打包计划：**纯计算，不碰任何文件**（界面上的"共 N 张 · X GB"就是它）。"""

    label: str
    entries: list[ExportEntry] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 有记录但不在盘上的相对路径
    no_root: list[str] = field(default_factory=list)   # 库没有路径提示（无法定位原片）

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    def summary(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "count": self.count,
            "bytes": self.total_bytes,
            "missing": len(self.missing),
            "no_root": len(self.no_root),
            "missing_samples": self.missing[:5],
        }


def plan_export(db: Database, *, asset_ids: Sequence[int], label: str,
                check_exists: bool = True) -> ExportPlan:
    """把 asset 列表变成"要写哪些文件"的计划。**不写任何东西。**

    `check_exists=True` 时会做一次 `is_file()`（拔盘时就知道要跳过多少张）；
    计划阶段允许把它关掉（纯统计会快很多）。
    """
    ids = [int(a) for a in asset_ids]
    plan = ExportPlan(label=str(label or "打包"))
    if not ids:
        return plan

    from ..repos import AssetRepo, asset_absolute_path

    repo = AssetRepo(db)
    store = LibraryStore(db)
    roots: dict[int, Path] = {}
    rows: list[tuple[int, str, int, int]] = []
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        rows.extend(
            (int(r["id"]), str(r["relative_path"]), int(r["library_id"]),
             int(r["size"]) if r["size"] is not None else 0)
            for r in db.query(
                "SELECT id, relative_path, library_id, size FROM asset"
                f" WHERE id IN ({marks})", chunk)
        )
    times: dict[int, str | None] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        times.update({int(r["asset_id"]): r["capture_at"] for r in db.query(
            f"SELECT asset_id, capture_at FROM metadata WHERE asset_id IN ({marks})", chunk)})
    del repo

    for asset_id, relative, library_id, size in rows:
        if library_id not in roots:
            library = store.get(library_id)
            if not library or not library.root_path_hint:
                plan.no_root.append(relative)
                continue
            roots[library_id] = Path(library.root_path_hint)
        source = asset_absolute_path(roots[library_id], relative)
        if check_exists and not source.is_file():
            plan.missing.append(relative)
            continue
        plan.entries.append(ExportEntry(asset_id=asset_id, relative_path=relative,
                                        source=source, size=size,
                                        capture_at=times.get(asset_id)))
    return plan


@dataclass
class ExportResult:
    path: Path
    written: int
    bytes_written: int
    skipped: list[str]
    cancelled: bool
    seconds: float
    duplicates: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.written} 个文件，{self.bytes_written / 1e9:.2f} GB，"
                f"{self.seconds:.1f} 秒" + (f"，跳过 {len(self.skipped)}" if self.skipped else ""))


def _unique_names(entries: Iterable[ExportEntry]) -> tuple[dict[int, str], list[str]]:
    """包内重名时加 ` (2)` 后缀。返回 `{asset_id: arcname}` 与重名说明。

    没有这一步，两次外拍里同名的 `DSC_0001.JPG` 会在包里互相覆盖 ——
    而且 zipfile 只会安静地写两条同名记录，解压时后一条盖掉前一条。
    """
    used: dict[str, int] = {}
    names: dict[int, str] = {}
    renamed: list[str] = []
    for entry in entries:
        base = entry.arcname
        if base not in used:
            used[base] = 1
            names[entry.asset_id] = base
            continue
        used[base] += 1
        stem, dot, suffix = base.rpartition(".")
        candidate = f"{stem} ({used[base]}){dot}{suffix}" if dot else f"{base} ({used[base]})"
        renamed.append(f"{base} → {candidate}")
        names[entry.asset_id] = candidate
    return names, renamed


def build_archive(plan: ExportPlan, target: Path, *, progress: Callable[..., None] | None = None,
                  stop: Callable[[], bool] | None = None) -> ExportResult:
    """把计划写成 zip。**只读原片。**

    * 不压缩（`ZIP_STORED`）：照片/视频本来就是压缩过的，deflate 只烧 CPU；
    * 空间不足**在开始之前**就报错，不留下一个写了一半的包；
    * 取消/中止时把半成品删掉 —— 一个不完整的包比没有包更危险
      （用户会以为它就是全部）。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    needed = plan.total_bytes
    free = shutil.disk_usage(target.parent).free
    if needed and free < needed * 1.02:      # 2% 余量给文件系统与清单
        raise OSError(
            f"目标磁盘空间不足：需要约 {needed / 1e9:.1f} GB，"
            f"可用 {free / 1e9:.1f} GB（{target.parent}）")

    names, duplicates = _unique_names(plan.entries)
    started = time.time()
    written = 0
    bytes_written = 0
    skipped: list[str] = []
    total = plan.count
    last_tick = 0.0

    def tick(done: int, note: str, force: bool = False) -> None:
        nonlocal last_tick
        if progress is None:
            return
        now = time.time()
        # 每张都写一次进度就是每张一次数据库写；几百张无所谓，上万张会拖慢打包本身
        if not force and done < total and now - last_tick < 0.4:
            return
        last_tick = now
        progress(done, total, note)

    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for index, entry in enumerate(plan.entries, 1):
                if stop is not None and stop():
                    raise _Cancelled()
                try:
                    archive.write(entry.source, arcname=names[entry.asset_id])
                except OSError:
                    # 计划之后文件被移走/拔盘：**跳过并记账**，不要整批失败
                    skipped.append(entry.relative_path)
                    tick(index, f"{index}/{total} 跳过 {Path(entry.relative_path).name}")
                    continue
                written += 1
                bytes_written += entry.size
                tick(index, f"{index}/{total} {Path(entry.relative_path).name}")
            archive.writestr(MANIFEST_NAME, _manifest_text(plan, names, skipped))
            tick(total, f"{total}/{total} 完成清单", force=True)
    except _Cancelled:
        target.unlink(missing_ok=True)
        return ExportResult(path=target, written=written, bytes_written=bytes_written,
                            skipped=skipped, cancelled=True,
                            seconds=time.time() - started, duplicates=duplicates)
    except BaseException:
        target.unlink(missing_ok=True)      # 出错不留半成品
        raise

    return ExportResult(path=target, written=written, bytes_written=bytes_written,
                        skipped=skipped, cancelled=False,
                        seconds=time.time() - started, duplicates=duplicates)


class _Cancelled(Exception):
    pass


def _manifest_text(plan: ExportPlan, names: dict[int, str], skipped: Sequence[str]) -> str:
    """包内清单：一张一行，写明原路径 / 拍摄时刻 / 体积。

    为什么要有它：拿到包的人（常常不是用户本人）需要知道"这张是从哪来的"。
    另外包里的目录结构是**原库的相对路径**，清单让这一点一目了然。
    """
    lines = [f"PhotoOS 打包：{plan.label}",
             f"共 {plan.count} 个文件，{plan.total_bytes / 1e9:.2f} GB",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             ""]
    if skipped:
        lines.append(f"⚠ 以下 {len(skipped)} 个文件在打包时不在盘上，没有进包：")
        lines.extend(f"  {path}" for path in skipped)
        lines.append("")
    lines.append("包内路径\t拍摄时刻\t字节\t原库相对路径")
    for entry in plan.entries:
        lines.append(f"{names[entry.asset_id]}\t{entry.capture_at or '—'}\t"
                     f"{entry.size}\t{entry.relative_path}")
    lines.append("")
    lines.append("说明：包内路径就是照片库里的相对路径；原片本身没有被修改过。")
    return "\n".join(lines)


__all__ = ["ExportEntry", "ExportPlan", "ExportResult", "MANIFEST_NAME",
           "build_archive", "plan_export", "safe_label"]
