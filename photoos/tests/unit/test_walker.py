"""只读文件遍历测试。

重点保证：
  * 只收媒体文件，绝不误收既有流水线的 CSV/日志/脚本；
  * 遍历顺序稳定（重扫可复现）；
  * 单文件/单目录失败不中断整批（PRD §11.1）；
  * 绝不修改被遍历的文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoos.library import walker
from photoos.library.walker import WalkError, classify_extension, count_media, walk_media


def _make_tree(root: Path) -> None:
    (root / "杭州市" / "2025-07-19_1341-1413").mkdir(parents=True)
    (root / "杭州市" / "2025-10-24_2349_至_10-25_1807").mkdir(parents=True)
    (root / "未知地点" / "2020-04-28_2234-2245").mkdir(parents=True)

    (root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.JPG").write_bytes(b"jpeg-1")
    (root / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.NEF").write_bytes(b"nef-1")
    (root / "杭州市" / "2025-10-24_2349_至_10-25_1807" / "DSC_4051.JPG").write_bytes(b"jpeg-2")
    (root / "未知地点" / "2020-04-28_2234-2245" / "DSC_0005.JPG").write_bytes(b"jpeg-3")
    (root / "未知地点" / "2020-04-28_2234-2245" / "clip.MOV").write_bytes(b"mov-1")

    # 非媒体文件：必须被忽略
    (root / "杭州市" / "library-index.csv").write_bytes(b"a,b\n")
    (root / "杭州市" / "run.log").write_bytes(b"log\n")
    (root / "tool.ps1").write_bytes(b"# script\n")
    (root / "readme.md").write_bytes(b"# doc\n")

    # 既有流水线 bookkeeping 目录：必须整目录跳过
    skip = root / "_归类清单" / "_重复文件"
    skip.mkdir(parents=True)
    (skip / "DSC_0005.JPG").write_bytes(b"quarantined-copy")
    (root / "_归类清单" / "dup-hash-cache.csv").write_bytes(b"hash\n")

    # 系统目录：必须跳过
    recycle = root / "$RECYCLE.BIN"
    recycle.mkdir()
    (recycle / "DSC_9999.JPG").write_bytes(b"deleted")


def test_walk_collects_only_media(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    entries = [e for e in walk_media(tmp_path) if not isinstance(e, WalkError)]
    relatives = sorted(e.relative for e in entries)

    assert relatives == sorted(
        [
            "杭州市\\2025-07-19_1341-1413\\DSC_1275.JPG",
            "杭州市\\2025-07-19_1341-1413\\DSC_1275.NEF",
            "杭州市\\2025-10-24_2349_至_10-25_1807\\DSC_4051.JPG",
            "未知地点\\2020-04-28_2234-2245\\DSC_0005.JPG",
            "未知地点\\2020-04-28_2234-2245\\clip.MOV",
        ]
    )


def test_walk_skips_bookkeeping_and_system_dirs(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    relatives = [e.relative for e in walk_media(tmp_path) if not isinstance(e, WalkError)]
    assert not any("_归类清单" in r for r in relatives), "不得进入既有流水线产物目录"
    assert not any("$RECYCLE.BIN" in r for r in relatives)
    assert not any(r.endswith((".csv", ".log", ".ps1", ".md")) for r in relatives)


def test_walk_order_is_stable(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    first = [e.relative for e in walk_media(tmp_path) if not isinstance(e, WalkError)]
    second = [e.relative for e in walk_media(tmp_path) if not isinstance(e, WalkError)]
    assert first == second
    assert len(first) == len(set(first)), "不得重复产出同一文件"


def test_walk_relative_paths_are_safe(tmp_path: Path) -> None:
    """遍历产出的相对路径不得含盘符/前导分隔符/上跳 —— 否则会污染数据库身份。"""
    _make_tree(tmp_path)
    for entry in walk_media(tmp_path):
        if isinstance(entry, WalkError):
            continue
        assert ":" not in entry.relative
        assert not entry.relative.startswith(("\\", "/"))
        assert ".." not in entry.relative.split("\\")


def test_walk_classifies_file_types(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    by_name = {
        Path(e.relative).name: e.file_type for e in walk_media(tmp_path) if not isinstance(e, WalkError)
    }
    assert by_name["DSC_1275.NEF"] == "raw"
    assert by_name["DSC_1275.JPG"] == "jpeg"
    assert by_name["clip.MOV"] == "video"


def test_walk_records_size_and_mtime(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    target = tmp_path / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.JPG"
    entry = next(
        e
        for e in walk_media(tmp_path)
        if not isinstance(e, WalkError) and e.path == target
    )
    stat = target.stat()
    assert entry.size == stat.st_size
    assert entry.mtime_ns == stat.st_mtime_ns


def test_walk_does_not_modify_files(tmp_path: Path) -> None:
    """只读守卫：遍历前后，所有文件的 size + mtime + 内容必须完全一致。"""
    _make_tree(tmp_path)
    before = {
        p.relative_to(tmp_path).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    list(walk_media(tmp_path))
    after = {
        p.relative_to(tmp_path).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_walk_include_flags(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    photos = [e for e in walk_media(tmp_path, include_videos=False) if not isinstance(e, WalkError)]
    assert photos and all(e.file_type != "video" for e in photos)

    videos = [e for e in walk_media(tmp_path, include_photos=False) if not isinstance(e, WalkError)]
    assert videos and all(e.file_type == "video" for e in videos)


def test_walk_include_other_files(tmp_path: Path) -> None:
    """`include_other_files` 收录"未知但非忽略"的类型；
    `IGNORED_EXTENSIONS` 里的杂项（脚本/日志/CSV）永远不收录 —— 这是索引卫生要求。
    """
    _make_tree(tmp_path)
    (tmp_path / "杭州市" / "weird.xyz").write_bytes(b"unknown-type")

    others = [
        e
        for e in walk_media(tmp_path, include_other_files=True)
        if not isinstance(e, WalkError) and e.file_type == "other"
    ]
    assert {e.name for e in others} == {"weird.xyz"}


def test_walk_never_indexes_ignored_extensions(tmp_path: Path) -> None:
    """即使显式要求收录其他文件，脚本/日志/CSV/配置也绝不能被收进索引。"""
    _make_tree(tmp_path)
    relatives = [
        e.relative for e in walk_media(tmp_path, include_other_files=True) if not isinstance(e, WalkError)
    ]
    assert not any(
        r.lower().endswith((".csv", ".log", ".ps1", ".md", ".cmd", ".json", ".db", ".dat"))
        for r in relatives
    )


def test_walk_max_depth(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    shallow = [e for e in walk_media(tmp_path, max_depth=0) if not isinstance(e, WalkError)]
    assert shallow == [], "max_depth=0 时不应进入任何子目录"


def test_walk_missing_root_yields_error(tmp_path: Path) -> None:
    items = list(walk_media(tmp_path / "nope"))
    assert len(items) == 1
    assert isinstance(items[0], WalkError)


def test_walk_reports_error_for_unreadable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_tree(tmp_path)

    real_scandir = walker.os.scandir

    def flaky_scandir(path: object) -> object:
        if str(path).endswith("未知地点"):
            raise PermissionError(13, "拒绝访问（模拟）")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(walker.os, "scandir", flaky_scandir)
    items = list(walk_media(tmp_path))
    errors = [i for i in items if isinstance(i, WalkError)]
    entries = [i for i in items if not isinstance(i, WalkError)]

    assert any("拒绝访问" in e.message for e in errors), "单目录失败必须被记录"
    assert entries, "其他目录必须照常产出（不得因一个目录失败而中断整批）"
    assert all("未知地点" not in e.relative for e in entries)


def test_walk_handles_directory_loop(tmp_path: Path) -> None:
    """junction / symlink 造成的目录环必须被识别并跳过，不能无限递归。"""
    _make_tree(tmp_path)
    loop = tmp_path / "杭州市" / "loop"
    try:
        loop.symlink_to(tmp_path / "杭州市", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建目录符号链接")

    items = list(walk_media(tmp_path, follow_dir_symlinks=True))
    assert any(isinstance(i, WalkError) and "目录环" in i.message for i in items)
    assert len(items) < 100, "目录环未被截断（可能无限递归）"


def test_count_media(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    media, errors = count_media(tmp_path)
    assert media == 5
    assert errors == 0


def test_count_media_missing_root(tmp_path: Path) -> None:
    media, errors = count_media(tmp_path / "nope")
    assert media == 0
    assert errors == 1


# --------------------------------------------------------------------- 扩展名分类
@pytest.mark.parametrize(
    ("ext", "expected"),
    [
        (".NEF", "raw"),
        (".nef", "raw"),
        (".CR3", "raw"),
        (".DNG", "raw"),
        (".JPG", "jpeg"),
        (".jpeg", "jpeg"),
        (".HEIC", "heic"),
        (".heif", "heic"),
        (".PNG", "png"),
        (".TIF", "tiff"),
        (".WEBP", "webp"),
        (".MOV", "video"),
        (".MP4", "video"),
        (".mts", "video"),
        (".txt", None),
        (".csv", None),
        (".exe", None),
        ("", None),
    ],
)
def test_classify_extension(ext: str, expected: str | None) -> None:
    assert classify_extension(ext) == expected


def test_extension_sets_are_case_insensitive_via_classify() -> None:
    """PRD §2.2：不得假设固定格式组合；大小写不得导致误判。"""
    for ext in (".JPG", ".Jpg", ".jpg"):
        assert classify_extension(ext) == "jpeg"
