"""交叉校验：PhotoOS 的元数据/哈希 vs 既有 PowerShell 工具的产物。

**为什么值得保留**：既有工具用 PowerShell + 自写 .NET TIFF 解析器独立实现，
PhotoOS 用 Pillow/LibRaw 独立实现。两者对**同一批文件**给出相同结论，
是对解析正确性的强证据（比自测更有说服力）；出现差异时也是最有价值的线索。

用法：
    photoos crosscheck --library 1 --legacy-index "E:\\照片\\_归类清单\\library-index.csv" \\
                       [--legacy-hashes "E:\\照片\\_归类清单\\dup-hash-cache.csv"] [--sample 400]

**只读**：读既有 CSV + 查自己的数据库；只在验证哈希实现时读少量原文件内容，
绝不写入照片库，也不修改既有 CSV。
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .db import Database
from .hashing import hash_sha256
from .library.store import LibraryStore

#: 既有工具产物中的路径形如 `C:\Photos\<城市>\<时段>\<文件>`；这里只剥离「库根」这一层。
#: 不使用盘符作为身份（PRD §5.2），只做字符串切分。
_ROOT_MARKER_RE = re.compile(r"\\[^\\]+\\")


@dataclass
class MetadataComparison:
    legacy_rows: int = 0
    mine_rows: int = 0
    comparable: int = 0
    agree: int = 0
    differ: int = 0
    legacy_only: int = 0
    mine_only: int = 0
    differences: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def agreement_ratio(self) -> float:
        if not self.comparable:
            return 0.0
        return self.agree / self.comparable

    def lines(self) -> list[str]:
        out = [
            f"既有记录（带拍摄时间）：{self.legacy_rows}",
            f"PhotoOS 记录（带本地时间）：{self.mine_rows}",
            f"可比对：{self.comparable}",
            f"  完全一致：{self.agree}",
            f"  不一致  ：{self.differ}",
            f"  仅既有有：{self.legacy_only}（多为尚未索引的文件）",
            f"  仅 PhotoOS 有：{self.mine_only}（多为既有工具解析不到的 EXIF）",
        ]
        if self.comparable:
            out.append(f"一致率：{self.agreement_ratio * 100:.2f}%")
        for relative, legacy_value, mine_value in self.differences:
            out.append(f"  差异样例：{relative}  既有={legacy_value}  PhotoOS={mine_value}")
        return out


@dataclass
class HashComparison:
    cached_rows: int = 0
    checked: int = 0
    matched: int = 0
    missing_on_disk: int = 0
    mismatched: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"既有哈希缓存行数：{self.cached_rows}",
            f"抽样读取：{self.checked} 个（另有 {self.missing_on_disk} 个已不在原路径，通常是被隔离）",
            f"SHA256 与既有缓存一致：{self.matched}/{self.checked}",
        ]
        out.extend(f"  不一致：{name}" for name in self.mismatched[:5])
        return out


@dataclass
class CrossCheckReport:
    library_id: int
    library_name: str
    legacy_index: Path | None = None
    legacy_hashes: Path | None = None
    metadata: MetadataComparison | None = None
    hashes: HashComparison | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """是否得到了有意义的交叉验证（而不是"没有交集"这种空结论）。"""
        meta_ok = bool(self.metadata and self.metadata.comparable and self.metadata.differ == 0)
        hash_ok = bool(self.hashes and self.hashes.checked and not self.hashes.mismatched)
        return meta_ok or hash_ok

    def lines(self) -> list[str]:
        out = [f"交叉校验 · Library #{self.library_id}「{self.library_name}」"]
        if self.metadata is not None:
            out.append("")
            out.append("【拍摄时间】与既有 library-index.csv 比对")
            out.extend("  " + line for line in self.metadata.lines())
        if self.hashes is not None:
            out.append("")
            out.append("【内容哈希】与既有 dup-hash-cache.csv 比对")
            out.extend("  " + line for line in self.hashes.lines())
        if self.notes:
            out.append("")
            out.extend(f"备注：{note}" for note in self.notes)
        out.append("")
        if self.verified:
            out.append("结论：两套独立实现给出**一致**结论 → 解析与哈希得到交叉验证。")
        else:
            out.append(
                "结论：未形成有效交叉验证（交集为空或存在不一致）。"
                "交集为空通常是因为尚未索引足够多文件，属正常中间状态。"
            )
        return out


def _to_relative(absolute: str, *, root_marker: str) -> str | None:
    """把既有产物的绝对路径转成相对路径（纯字符串切分，不依赖盘符）。"""
    text = absolute.replace("/", "\\")
    marker = f"\\{root_marker}\\"
    index = text.find(marker)
    if index == -1:
        return None
    return text[index + len(marker) :]


def crosscheck(
    db: Database,
    settings: Settings,
    library_id: int,
    *,
    legacy_index: Path | None = None,
    legacy_hashes: Path | None = None,
    sample: int = 400,
    root_marker: str | None = None,
    hash_samples: int = 12,
) -> CrossCheckReport:
    """执行交叉校验。全部只读。"""
    library = LibraryStore(db).get(library_id)
    report = CrossCheckReport(
        library_id=library.id, library_name=library.name,
        legacy_index=Path(legacy_index) if legacy_index else None,
        legacy_hashes=Path(legacy_hashes) if legacy_hashes else None,
    )
    root = Path(library.root_path_hint) if library.root_path_hint else None
    marker = root_marker or (root.name if root else "")

    # ------------------------------------------------------------ 拍摄时间
    if report.legacy_index and report.legacy_index.is_file():
        legacy: dict[str, str] = {}
        with open(report.legacy_index, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                relative = _to_relative(str(row.get("Path") or ""), root_marker=marker)
                capture = str(row.get("CaptureTime") or "").strip()
                if relative and capture:
                    legacy[relative.lower()] = capture

        mine = {
            str(r["relative_path"]).lower(): (str(r["relative_path"]), str(r["capture_at"]))
            for r in db.query(
                "SELECT a.relative_path, m.capture_at FROM asset AS a"
                " JOIN metadata AS m ON m.asset_id = a.id"
                " WHERE a.library_id = ? AND m.capture_at IS NOT NULL",
                (library.id,),
            )
        }

        comparison = MetadataComparison(
            legacy_rows=len(legacy),
            mine_rows=len(mine),
            legacy_only=len(set(legacy) - set(mine)),
            mine_only=len(set(mine) - set(legacy)),
        )
        common = sorted(set(legacy) & set(mine))
        comparison.comparable = len(common)

        # 全量比对（纯字典查询，没有 IO 成本，无需抽样；抽样只用于展示差异样例）
        for key in common:
            if legacy[key] == mine[key][1]:
                comparison.agree += 1
            else:
                comparison.differ += 1
                if len(comparison.differences) < 5:
                    comparison.differences.append(
                        (mine[key][0], legacy[key], mine[key][1])
                    )
        del sample  # 保留参数以便调用方语义一致；全量比对不需要它
        report.metadata = comparison
    elif legacy_index is not None:
        report.notes.append(f"既有索引文件不存在：{legacy_index}")

    # ------------------------------------------------------------ 内容哈希
    if report.legacy_hashes and report.legacy_hashes.is_file():
        cached: list[tuple[Path, str]] = []
        with open(report.legacy_hashes, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                path = str(row.get("Path") or "")
                digest = str(row.get("Hash") or "").upper()
                if path and digest:
                    cached.append((Path(path), digest))

        comparison = HashComparison(cached_rows=len(cached))
        random.seed(20261011)  # 固定种子，结果可复现
        random.shuffle(cached)
        for path, expected in cached:
            if comparison.checked >= hash_samples:
                break
            if not path.is_file():
                comparison.missing_on_disk += 1
                continue
            comparison.checked += 1
            try:
                actual = hash_sha256(path)
            except OSError as exc:
                comparison.mismatched.append(f"{path.name}（读取失败：{exc}）")
                continue
            if actual == expected:
                comparison.matched += 1
            else:
                comparison.mismatched.append(path.name)
        report.hashes = comparison
    elif legacy_hashes is not None:
        report.notes.append(f"既有哈希缓存文件不存在：{legacy_hashes}")

    if report.metadata and report.metadata.mine_rows == 0:
        report.notes.append("PhotoOS 尚未提取到任何本地拍摄时间，请先运行 `photoos index`")
    return report


__all__ = [
    "CrossCheckReport",
    "HashComparison",
    "MetadataComparison",
    "crosscheck",
]
