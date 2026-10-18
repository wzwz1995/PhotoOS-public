"""换机迁移与备份（PRD §5.1 / §12 LOCKED）。

要求（逐条对应 PRD §5.1）：
  * 必须支持完整 Windows → Windows 迁移；**换电脑不需要重新导入、重新筛选或重新跑 AI**。
  * 迁移对象至少包括：photoos.db、Library 元数据、Session/Scene/Shot/Asset 关系、精选与自定义主题、
    EXIF/元数据、缩略图/预览、Embedding、AI 分析、训练记录、能力档案与系统设置。
  * **照片原文件默认不进迁移包**；新电脑重连照片盘后按 `Library UUID + 相对路径 + Hash` 重新绑定。
  * 迁移包必须带版本号、schema 版本与校验信息；未来版本升级走 migration。
  * 禁止依赖 Windows 用户名、用户目录、盘符或绝对路径作为核心资产标识。

实现要点：
  * 包是**标准 zip**，即使没有 PhotoOS 也能用资源管理器打开检查（可审计性）。
  * manifest 里对每个成员记录 sha256；导入时逐项校验，任何不符立即中止（不半途覆盖用户数据）。
  * 导入前自动备份当前数据根，并写 `restore.json` 说明如何回退 —— 迁移本身也必须可逆。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..db import Database, target_version, utc_now
from ..errors import MigrationPackageError
from ..library.store import LibraryStore

PACKAGE_FORMAT = "photoos-migration"
PACKAGE_VERSION = 1

#: 默认入包的目录（相对 data_root）。照片原文件**不在此列**，符合 PRD §5.1。
DEFAULT_INCLUDE_DIRS = ("thumbnails", "previews", "ai_cache")

#: 默认不入包（可再生或体量大）
DEFAULT_EXCLUDE = ("backups", "logs", "jobs")


@dataclass
class PackageFile:
    name: str
    size: int
    sha256: str

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


@dataclass
class ExportReport:
    path: Path
    files: int = 0
    bytes_total: int = 0
    schema_version: int = 0
    included_dirs: tuple[str, ...] = ()
    libraries: int = 0
    assets: int = 0
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"迁移包：{self.path}",
            f"  内容文件：{self.files} 个，{self.bytes_total / 1e9:.2f} GB",
            f"  schema 版本：{self.schema_version}",
            f"  收录目录：{'、'.join(self.included_dirs) or '(无)'}",
            f"  Library：{self.libraries} 个，Asset：{self.assets} 条",
            "  照片原文件默认不入包；新电脑重连照片盘后按 Library UUID + 相对路径 + Hash 重新绑定。",
        ]
        out.extend(f"  警告：{item}" for item in self.warnings)
        return out


@dataclass
class VerifyResult:
    ok: bool
    manifest: dict[str, object] | None = None
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"校验结果：{'通过' if self.ok else '失败'}（已校验 {self.checked} 项）"]
        if self.missing:
            out.append(f"  缺少成员：{len(self.missing)} 个，例如 {self.missing[:3]}")
        if self.corrupt:
            out.append(f"  校验和不符：{len(self.corrupt)} 个，例如 {self.corrupt[:3]}")
        if self.manifest:
            out.append(f"  包版本：{self.manifest.get('package_version')}")
            out.append(f"  schema 版本：{self.manifest.get('schema_version')}")
            out.append(f"  导出时间：{self.manifest.get('exported_at')}")
        out.extend(f"  警告：{item}" for item in self.warnings)
        return out


@dataclass
class ImportReport:
    data_root: Path
    applied: bool = False
    backup_path: Path | None = None
    files_restored: int = 0
    libraries: int = 0
    assets: int = 0
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"导入目标数据根：{self.data_root}",
            f"  结果：{'已应用' if self.applied else '未应用（预演或校验失败）'}",
        ]
        if self.backup_path is not None:
            out.append(f"  导入前备份：{self.backup_path}")
        out.append(f"  恢复文件：{self.files_restored} 个")
        out.append(f"  Library：{self.libraries} 个，Asset：{self.assets} 条")
        out.extend(f"  警告：{item}" for item in self.warnings)
        return out


# --------------------------------------------------------------------- 工具
def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            hasher.update(block)
    return hasher.hexdigest()


def _library_manifest(db: Database) -> list[dict[str, object]]:
    """导出 Library 身份信息。

    **只记录 uuid / 卷指纹 / 相对路径提示的"最后一次位置"**，不把盘符当作身份；
    新机器上重绑定只依赖 uuid 与相对路径。
    """
    store = LibraryStore(db)
    out: list[dict[str, object]] = []
    for library in store.list_all():
        volumes = [
            {
                "serial_hex": volume.serial_hex,
                "fs_type": volume.fs_type,
                "label": volume.label,
                "kind": volume.kind,
                "fingerprint": volume.fingerprint,
                "last_mount_hint": volume.mount_hint,
                "root_hint": store.volume_root_hint(library.id, volume.id),
            }
            for volume in store.volumes_for(library.id)
        ]
        asset_count = int(
            db.scalar("SELECT COUNT(*) FROM asset WHERE library_id = ?", (library.id,), default=0)
        )
        out.append(
            {
                "uuid": library.uuid,
                "name": library.name,
                "marker_uuid": library.marker_uuid,
                "status": library.status,
                "last_root_hint": library.root_path_hint,
                "asset_count": asset_count,
                "volumes": volumes,
                "note": "root_hint 仅为提示；新机器上请用 `photoos library probe` 重新定位并重绑定。",
            }
        )
    return out


# --------------------------------------------------------------------- 导出
def export_package(
    db: Database,
    data_root: Path,
    target: Path,
    *,
    include_dirs: tuple[str, ...] = DEFAULT_INCLUDE_DIRS,
    extra_files: tuple[str, ...] = ("config.json",),
    progress=None,
) -> ExportReport:
    """导出换机迁移包。数据库使用 `VACUUM INTO` 生成一致快照（不阻塞读）。"""
    data_root = Path(data_root)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    report = ExportReport(
        path=target,
        schema_version=db.schema_version(),
        included_dirs=include_dirs,
    )

    with tempfile.TemporaryDirectory(prefix="photoos-export-") as staging:
        staging_path = Path(staging)
        db_snapshot = staging_path / "photoos.db"
        try:
            db.vacuum_into(db_snapshot)
        except Exception as exc:  # noqa: BLE001
            raise MigrationPackageError(f"生成数据库快照失败：{exc}") from exc

        members: list[PackageFile] = []
        manifest_extra: dict[str, object] = {}

        # ---- 数据库
        members.append(
            PackageFile(
                name="photoos.db",
                size=db_snapshot.stat().st_size,
                sha256=sha256_file(db_snapshot),
            )
        )
        # 用快照库读取统计与 Library 清单，保证与包内数据库完全一致
        snapshot_db = Database(db_snapshot, read_only=True)
        libraries = _library_manifest(snapshot_db)
        manifest_extra["libraries"] = libraries
        manifest_extra["assets"] = int(snapshot_db.scalar("SELECT COUNT(*) FROM asset", default=0))
        report.libraries = len(libraries)
        report.assets = int(manifest_extra["assets"])
        if not libraries:
            report.warnings.append("数据库中没有 Library 记录，迁移包只包含 schema 与配置")

        # ---- 派生数据目录
        staged_dirs: list[Path] = []
        for name in include_dirs:
            source = data_root / name
            if not source.is_dir():
                continue
            staged_dirs.append(source)

        # ---- 附加文件
        staged_files: list[Path] = []
        for name in extra_files:
            candidate = data_root / name
            if candidate.is_file():
                staged_files.append(candidate)

        # ---- 写 Library 清单为独立文件（便于人工检查，不依赖读数据库）
        libraries_json = staging_path / "libraries.json"
        libraries_json.write_text(
            json.dumps({"libraries": libraries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # ---- 收集所有成员并计算校验和
        for directory in staged_dirs:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(data_root).as_posix()
                    members.append(
                        PackageFile(
                            name=relative, size=path.stat().st_size, sha256=sha256_file(path)
                        )
                    )
                    if progress is not None:
                        progress(len(members), 0, f"准备 {relative}")
        for path in staged_files:
            members.append(
                PackageFile(
                    name=path.name, size=path.stat().st_size, sha256=sha256_file(path)
                )
            )
        members.append(
            PackageFile(
                name="libraries.json",
                size=libraries_json.stat().st_size,
                sha256=sha256_file(libraries_json),
            )
        )

        manifest = {
            "package_format": PACKAGE_FORMAT,
            "package_version": PACKAGE_VERSION,
            "app_version": __version__,
            "schema_version": db.schema_version(),
            "exported_at": utc_now(),
            "source_platform": "windows",
            "photo_files_included": False,
            "rebind_procedure": (
                "新机器：photoos init → photoos migrate import <包> → "
                "photoos library probe <照片盘路径> → photoos library rebind <Library> <路径>"
            ),
            "included_dirs": list(include_dirs),
            "excluded_dirs": list(DEFAULT_EXCLUDE),
            "stats": {
                "libraries": report.libraries,
                "assets": report.assets,
                "files": len(members),
                "bytes": sum(item.size for item in members),
            },
            "files": [item.to_json() for item in members],
        }

        manifest_path = staging_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # ---- 打包
        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(manifest_path, "manifest.json")
            archive.write(db_snapshot, "photoos.db")
            archive.write(libraries_json, "libraries.json")
            for path in staged_files:
                archive.write(path, path.name)
            for directory in staged_dirs:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(data_root).as_posix())

    report.files = len(members)
    report.bytes_total = sum(item.size for item in members)
    if not (data_root / "thumbnails").is_dir():
        report.warnings.append("尚未生成缩略图，包内不含派生缓存（新机器需重算，但不影响原片与索引）")
    return report


# --------------------------------------------------------------------- 校验
def verify_package(package: Path, *, deep: bool = True) -> VerifyResult:
    """校验迁移包：manifest 可解析、schema 兼容、逐项 sha256 一致。"""
    package = Path(package)
    if not package.is_file():
        raise MigrationPackageError(f"迁移包不存在：{package}")

    result = VerifyResult(ok=False)
    try:
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names:
                result.warnings.append("包内缺少 manifest.json，无法确认来源与版本")
                return result
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            result.manifest = manifest

            if manifest.get("package_format") != PACKAGE_FORMAT:
                result.warnings.append(
                    f"包格式不是 {PACKAGE_FORMAT}（实际 {manifest.get('package_format')}）"
                )
                return result

            schema_version = int(manifest.get("schema_version") or 0)
            supported = target_version()
            if schema_version > supported:
                result.warnings.append(
                    f"包的 schema 版本 {schema_version} 高于本程序支持的 {supported}；"
                    "请先升级 PhotoOS 再导入（拒绝降级导入以免损坏数据）"
                )
                return result

            expected = manifest.get("files")
            if not isinstance(expected, list):
                result.warnings.append("manifest 缺少 files 清单")
                return result

            if not deep:
                result.ok = True
                return result

            for item in expected:
                name = str(item.get("name"))
                result.checked += 1
                if name not in names:
                    result.missing.append(name)
                    continue
                digest = hashlib.sha256()
                with archive.open(name) as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != item.get("sha256"):
                    result.corrupt.append(name)
    except zipfile.BadZipFile as exc:
        raise MigrationPackageError(f"迁移包不是有效的 zip：{exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MigrationPackageError(f"manifest.json 无法解析：{exc}") from exc

    result.ok = not result.missing and not result.corrupt
    return result


# --------------------------------------------------------------------- 导入
def import_package(
    package: Path,
    data_root: Path,
    *,
    apply: bool = True,
    deep_verify: bool = True,
    progress=None,
) -> ImportReport:
    """导入迁移包。

    `apply=False` 时只校验并报告（不动磁盘）。应用时会：
      1. 把现有数据根整体备份为 `<data_root>.pre-import-<时间戳>`
      2. 解压到数据根旁的临时目录
      3. 校验包内数据库可打开且 schema 版本正确
      4. 原子替换数据根
      5. 写 `restore.json` 说明回退方法
    """
    data_root = Path(data_root)
    report = ImportReport(data_root=data_root)

    verification = verify_package(package, deep=deep_verify)
    if not verification.ok:
        report.warnings.extend(verification.warnings)
        report.warnings.append("迁移包校验未通过，已中止导入（未改动任何数据）")
        return report

    manifest = verification.manifest or {}
    report.warnings.extend(verification.warnings)

    if not apply:
        report.warnings.append("这是预演（dry-run），未改动任何数据")
        report.files_restored = verification.checked
        return report

    data_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="photoos-import-", dir=str(data_root.parent)))
    try:
        with zipfile.ZipFile(package) as archive:
            # 防目录穿越：只允许写入 staging 之内
            for member in archive.infolist():
                destination = (staging / member.filename).resolve()
                if not str(destination).startswith(str(staging.resolve())):
                    raise MigrationPackageError(
                        f"迁移包含有非法路径（疑似目录穿越）：{member.filename}"
                    )
            archive.extractall(staging)
            report.files_restored = len([n for n in archive.namelist() if not n.endswith("/")])

        staged_db = staging / "photoos.db"
        if not staged_db.is_file():
            raise MigrationPackageError("迁移包内缺少 photoos.db")

        # 包内数据库必须可打开且 schema 不高于本程序
        check_db = Database(staged_db, read_only=True)
        version = check_db.schema_version()
        if version > target_version():
            raise MigrationPackageError(
                f"包内数据库 schema 版本 {version} 高于本程序支持的 {target_version()}"
            )
        if check_db.quick_check() != "ok":
            raise MigrationPackageError("包内数据库未能通过完整性检查")
        report.assets = int(check_db.scalar("SELECT COUNT(*) FROM asset", default=0))
        report.libraries = int(check_db.scalar("SELECT COUNT(*) FROM library", default=0))

        # ---- 备份现有数据根
        if data_root.exists():
            stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")
            backup = data_root.with_name(f"{data_root.name}.pre-import-{stamp}")
            shutil.move(str(data_root), str(backup))
            report.backup_path = backup

        # ---- 原子落位
        shutil.move(str(staging), str(data_root))
    except Exception:
        # 失败时尽量把原数据根放回去，避免用户处于"两边都没有"的状态
        if report.backup_path is not None and report.backup_path.exists() and not data_root.exists():
            shutil.move(str(report.backup_path), str(data_root))
            report.backup_path = None
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    (data_root / "restore.json").write_text(
        json.dumps(
            {
                "imported_at": utc_now(),
                "package": str(package),
                "package_version": manifest.get("package_version"),
                "schema_version": manifest.get("schema_version"),
                "previous_data_root_backup": str(report.backup_path) if report.backup_path else None,
                "how_to_rollback": (
                    "回退步骤：停止 PhotoOS → 删除当前数据根 → 把 "
                    f"{report.backup_path} 改回 {data_root} → 重新启动。"
                    if report.backup_path
                    else "本次为全新数据根，没有旧数据需要回退。"
                ),
                "next_steps": [
                    "连接原照片硬盘（或迁移后的硬盘）",
                    "photoos library probe <照片盘路径>",
                    "photoos library rebind <Library id|uuid> <照片盘路径>",
                    "photoos stats  # 确认 Asset 数量与旧电脑一致",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    report.applied = True
    report.warnings.append(
        "导入完成。照片原文件不在包内 —— 连接照片盘后执行 rebind 即可恢复原图访问（PRD §5.1）。"
    )
    return report


# --------------------------------------------------------------------- 备份
def backup_database(db: Database, backups_dir: Path, *, label: str = "manual") -> Path:
    """在线备份数据库（`VACUUM INTO`，不阻塞读）。"""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")
    target = backups_dir / f"photoos_{label}_{stamp}.db"
    db.vacuum_into(target)
    return target


def prune_backups(backups_dir: Path, *, keep: int = 5) -> list[Path]:
    """保留最近 `keep` 份备份，返回被删除的列表。

    **破坏性操作**：只删除 `backups/` 目录下由本系统生成的 `photoos_*.db`，
    绝不触碰迁移包或用户手动放入的其他文件。
    """
    backups_dir = Path(backups_dir)
    if not backups_dir.is_dir():
        return []
    candidates = sorted(
        (p for p in backups_dir.glob("photoos_*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for path in candidates[max(0, keep) :]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed


def list_backups(backups_dir: Path) -> list[tuple[Path, int, str]]:
    """列出备份：(路径, 字节, 修改时间)。"""
    import datetime as dt

    backups_dir = Path(backups_dir)
    if not backups_dir.is_dir():
        return []
    out = []
    for path in sorted(backups_dir.glob("*.db")):
        stat = path.stat()
        out.append(
            (path, stat.st_size, dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
        )
    return out


__all__ = [
    "DEFAULT_EXCLUDE",
    "DEFAULT_INCLUDE_DIRS",
    "PACKAGE_FORMAT",
    "PACKAGE_VERSION",
    "ExportReport",
    "ImportReport",
    "PackageFile",
    "VerifyResult",
    "backup_database",
    "export_package",
    "import_package",
    "list_backups",
    "prune_backups",
    "sha256_file",
    "verify_package",
]
