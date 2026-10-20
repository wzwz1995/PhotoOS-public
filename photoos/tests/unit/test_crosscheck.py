"""交叉校验测试。

交叉校验的意义在于**独立实现互证**，因此测试要保证：
  * 一致时如实报告一致；
  * **不一致时必须报出来**（否则这个工具就是装饰品）；
  * 路径转换不依赖盘符（PRD §5.2）；
  * 全程只读。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from PIL import Image

from photoos.config import Settings, use_config_path
from photoos.crosscheck import crosscheck
from photoos.db import Database, apply_migrations
from photoos.hashing import hash_sha256
from photoos.jobs import JobQueue, JobRunner
from photoos.library.store import LibraryStore


def _photo(path: Path, seed: int) -> Path:
    from PIL.TiffImagePlugin import IFDRational

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (320, 240), (seed * 17 % 256, seed * 29 % 256, seed * 43 % 256))
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON Z 5"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = f"2025:10:2{seed} 0{seed}:3{seed}:00"
    sub[0x9011] = "+08:00"
    sub[0x8827] = 100 + seed
    image.save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.fixture()
def env(tmp_path: Path):
    """已索引的小库 + 一份"既有工具"产物（用真实值生成，模拟独立实现）。"""
    with use_config_path(None):
        settings = Settings(data_root=tmp_path / "data")
        settings.ensure_dirs()
        db = Database(settings.db_path)
        apply_migrations(db, backup_dir=settings.backups_dir)

        # 注意：库根目录名带「照片」，因为 `_to_relative` 是按 `\<库根名>\` 切分的
        # （既有产物里的绝对路径形如 `C:\Photos\<城市>\<时段>\<文件>`）。
        library_root = tmp_path / "照片"
        (library_root / "杭州市" / "2025-10-21_0900-1000").mkdir(parents=True)
        (library_root / "上海市" / "2025-10-22_0900-1000").mkdir(parents=True)
        sources = [
            _photo(library_root / "杭州市" / "2025-10-21_0900-1000" / f"DSC_{2000 + i}.JPG", i)
            for i in range(1, 4)
        ]
        sources.append(_photo(library_root / "上海市" / "2025-10-22_0900-1000" / "DSC_3001.JPG", 4))

        library = LibraryStore(db).create(name="测试库", root=library_root)
        queue = JobQueue(db)
        job = queue.create("full_index", library_id=library.id)
        queue.claim_next()
        report = JobRunner(db, settings).run(job)
        assert report.status == "COMPLETED", report.error

        yield db, settings, library.id, library_root, sources


def _write_legacy_index(path: Path, root: Path, rows: list[tuple[Path, str]]) -> Path:
    """生成既有工具的 library-index.csv（含 BOM，与真实产物一致）。

    Size/MTime 允许取不到：测试里会故意构造"不同盘符"的路径，
    而 `Path.stat()` 对不存在的盘符会抛错 —— 这与既有产物的真实情况一致
    （CSV 里记的是当年的路径，现在未必可达）。
    """
    del root
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Path", "Name", "Kind", "Size", "MTime", "CaptureTime", "City", "SessionFolder"])
        for absolute, capture in rows:
            try:
                stat = absolute.stat()
                size, mtime = stat.st_size, "2026/1/1 0:00:00"
            except OSError:
                size, mtime = 0, "2026/1/1 0:00:00"
            parts = absolute.parts
            city = parts[-3] if len(parts) >= 3 else ""
            session = parts[-2] if len(parts) >= 2 else ""
            writer.writerow([str(absolute), absolute.name, "photo", size, mtime, capture, city, session])
    return path


def test_crosscheck_reports_agreement(env, tmp_path: Path) -> None:
    db, settings, library_id, root, sources = env
    expected = {
        str(r["relative_path"]): str(r["capture_at"])
        for r in db.query(
            "SELECT a.relative_path, m.capture_at FROM asset AS a"
            " JOIN metadata AS m ON m.asset_id = a.id WHERE a.library_id = ?",
            (library_id,),
        )
    }
    legacy = _write_legacy_index(
        tmp_path / "library-index.csv",
        root,
        [(root / Path(rel.replace("\\", "/")), capture) for rel, capture in expected.items()],
    )

    report = crosscheck(db, settings, library_id, legacy_index=legacy)
    assert report.metadata is not None
    assert report.metadata.comparable == 4
    assert report.metadata.agree == 4
    assert report.metadata.differ == 0
    assert report.metadata.agreement_ratio == 1.0
    assert report.verified


def test_crosscheck_detects_disagreement(env, tmp_path: Path) -> None:
    """**关键**：拍摄时间不一致必须被报出来，而不是被四舍五入成"通过"。"""
    db, settings, library_id, root, sources = env
    legacy = _write_legacy_index(
        tmp_path / "library-index.csv",
        root,
        [(source, "2020-01-01 00:00:00") for source in sources],  # 故意全错
    )

    report = crosscheck(db, settings, library_id, legacy_index=legacy)
    assert report.metadata is not None
    assert report.metadata.differ == 4
    assert report.metadata.agree == 0
    assert report.metadata.differences, "必须给出差异样例"
    assert not report.verified

    text = "\n".join(report.lines())
    assert "不一致" in text
    assert "未形成有效交叉验证" in text


def test_crosscheck_partial_overlap(env, tmp_path: Path) -> None:
    """只覆盖一部分文件时，应如实报告"仅既有有"的数量。"""
    db, settings, library_id, root, sources = env
    rows = []
    for source in sources:
        relative = source.relative_to(root).as_posix().replace("/", "\\")
        capture = db.scalar(
            "SELECT m.capture_at FROM metadata AS m JOIN asset AS a ON a.id = m.asset_id"
            " WHERE a.relative_path = ?",
            (relative,),
        )
        rows.append((source, str(capture)))
    # 额外塞一条库外文件（模拟既有工具里有而 PhotoOS 尚未索引的）
    outside = _photo(tmp_path / "旧盘" / "照片" / "北京市" / "2020-01-01_0000-0100" / "OLD_0001.JPG", 9)
    rows.append((outside, "2020-01-01 00:00:00"))

    legacy = _write_legacy_index(tmp_path / "library-index.csv", root, rows)
    report = crosscheck(db, settings, library_id, legacy_index=legacy)

    assert report.metadata is not None
    assert report.metadata.comparable == 4
    assert report.metadata.agree == 4
    assert report.metadata.legacy_only == 1


def test_crosscheck_path_conversion_ignores_drive_letter(env, tmp_path: Path) -> None:
    """既有产物里的盘符可以不同（换盘符场景），相对路径仍应匹配上。"""
    db, settings, library_id, root, sources = env
    expected = {
        str(r["relative_path"]): str(r["capture_at"])
        for r in db.query(
            "SELECT a.relative_path, m.capture_at FROM asset AS a"
            " JOIN metadata AS m ON m.asset_id = a.id WHERE a.library_id = ?",
            (library_id,),
        )
    }
    # 伪造一个不同盘符的绝对路径：把路径前缀换成 Z:\照片\
    # 这些路径在磁盘上并不存在 —— 正是"换过盘符的旧记录"的真实形态；
    # 交叉校验只做字符串切分，不应依赖路径可达。
    rows = [(Path(f"Z:\\照片\\{rel}"), capture) for rel, capture in expected.items()]
    legacy = _write_legacy_index(tmp_path / "library-index.csv", root, rows)

    report = crosscheck(db, settings, library_id, legacy_index=legacy)
    assert report.metadata is not None
    assert report.metadata.comparable == 4, "盘符不同不应影响相对路径匹配"
    assert report.metadata.agree == 4


def test_crosscheck_hash_comparison(env, tmp_path: Path) -> None:
    db, settings, library_id, root, sources = env
    with open(tmp_path / "dup-hash-cache.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Path", "Size", "MTime", "Hash"])
        for source in sources:
            writer.writerow([str(source), source.stat().st_size, "2026/1/1 0:00:00", hash_sha256(source)])

    report = crosscheck(
        db, settings, library_id, legacy_hashes=tmp_path / "dup-hash-cache.csv", hash_samples=10
    )
    assert report.hashes is not None
    assert report.hashes.checked >= 1
    assert report.hashes.matched == report.hashes.checked
    assert not report.hashes.mismatched
    assert report.verified


def test_crosscheck_hash_detects_mismatch(env, tmp_path: Path) -> None:
    """哈希不一致必须被报出来（说明缓存过期或文件被改动）。"""
    db, settings, library_id, root, sources = env
    with open(tmp_path / "dup-hash-cache.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Path", "Size", "MTime", "Hash"])
        for source in sources:
            writer.writerow([str(source), source.stat().st_size, "2026/1/1 0:00:00", "0" * 64])

    report = crosscheck(
        db, settings, library_id, legacy_hashes=tmp_path / "dup-hash-cache.csv", hash_samples=10
    )
    assert report.hashes is not None
    assert report.hashes.mismatched, "错误的既有哈希必须被检出"
    assert not report.verified


def test_crosscheck_without_inputs_is_not_a_pass(env) -> None:
    """没给任何既有产物时，不得宣称"验证通过"。"""
    db, settings, library_id, _root, _sources = env
    report = crosscheck(db, settings, library_id)
    assert not report.verified
    assert "未形成有效交叉验证" in "\n".join(report.lines())


def test_crosscheck_missing_files_are_noted(env, tmp_path: Path) -> None:
    db, settings, library_id, _root, _sources = env
    report = crosscheck(db, settings, library_id, legacy_index=tmp_path / "不存在.csv")
    assert any("不存在" in note for note in report.notes)
    assert not report.verified


def test_crosscheck_is_read_only(env, tmp_path: Path) -> None:
    """交叉校验不得改动照片库或既有 CSV。"""
    db, settings, library_id, root, sources = env
    legacy = _write_legacy_index(
        tmp_path / "library-index.csv",
        root,
        [(source, "2025-10-21 09:31:00") for source in sources],
    )
    legacy_bytes = legacy.read_bytes()
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }

    crosscheck(db, settings, library_id, legacy_index=legacy)

    after = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert legacy.read_bytes() == legacy_bytes
