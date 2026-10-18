"""相对路径安全测试。

这是「盘符永远不会成为照片的永久身份」这条铁律的**代码级防线**：
任何非法相对路径都必须被拒绝，而不是被静默修正。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoos.errors import SafetyError
from photoos.library import paths


# --------------------------------------------------------------------- 合法路径
@pytest.mark.parametrize(
    "value",
    [
        "a.jpg",
        "杭州市\\2025-07-19_1341-1413\\DSC_1275.JPG",
        "上海市\\2025-09-10_1326-1807\\DSC_1544__2.jpg",
        "a\\b\\c\\d.nef",
        "未知地点\\2020-04-28_2234-2245\\DSC_0005.JPG",
        "深\\嵌套\\目录\\文件名带空格 与中文.jpg",
    ],
)
def test_valid_relative_paths(value: str) -> None:
    paths.validate_relative(value)
    assert paths.is_relative_safe(value)


# --------------------------------------------------------------------- 非法路径
@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("E:\\照片\\a.jpg", "drive_letter"),
        ("C:a.jpg", "drive_letter"),
        ("\\\\server\\share\\a.jpg", "unc_path"),
        ("//server/share/a.jpg", "unc_path"),
        ("\\\\?\\E:\\a.jpg", "device_path"),
        ("\\a.jpg", "leading_separator"),
        ("/a.jpg", "leading_separator"),
        ("..\\a.jpg", "parent_traversal"),
        ("a\\..\\b.jpg", "parent_traversal"),
        ("a\\\\b.jpg", "empty_segment"),
        ("", "empty_relative"),
        ("   ", "empty_relative"),
    ],
)
def test_invalid_relative_paths(value: str, kind: str) -> None:
    with pytest.raises(SafetyError) as excinfo:
        paths.validate_relative(value)
    assert excinfo.value.detail["kind"] == kind
    assert not paths.is_relative_safe(value)


def test_drive_letter_error_mentions_prd_rule() -> None:
    with pytest.raises(SafetyError) as excinfo:
        paths.validate_relative("E:\\照片\\a.jpg")
    assert "盘符" in excinfo.value.message


# --------------------------------------------------------------------- 转换
def test_to_relative_converts_separators(tmp_path: Path) -> None:
    root = tmp_path / "照片"
    target = root / "杭州市" / "DSC_0001.JPG"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert paths.to_relative(root, target) == "杭州市\\DSC_0001.JPG"


def test_to_relative_rejects_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "照片"
    root.mkdir()
    outside = tmp_path / "别处" / "a.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    with pytest.raises(SafetyError) as excinfo:
        paths.to_relative(root, outside)
    assert excinfo.value.detail["kind"] == "not_under_root"


def test_to_relative_never_emits_drive_letter(tmp_path: Path) -> None:
    """回归测试：无论 root 在哪个盘，结果都不得含盘符或前导分隔符。"""
    root = tmp_path / "lib"
    target = root / "x" / "y.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    relative = paths.to_relative(root, target)
    assert ":" not in relative
    assert not relative.startswith(("\\", "/"))


def test_join_relative_round_trip(tmp_path: Path) -> None:
    relative = "杭州市\\2025-07-19_1341-1413\\DSC_1275.JPG"
    joined = paths.join_relative(tmp_path, relative)
    assert joined == tmp_path / "杭州市" / "2025-07-19_1341-1413" / "DSC_1275.JPG"
    assert paths.to_relative(tmp_path, joined) == relative


def test_join_relative_rejects_malicious_input(tmp_path: Path) -> None:
    for bad in ("..\\..\\windows\\system32\\x.dll", "E:\\evil.exe", "\\\\host\\share\\x"):
        with pytest.raises(SafetyError):
            paths.join_relative(tmp_path, bad)


def test_relative_parts() -> None:
    assert paths.relative_parts("a\\b\\c.jpg") == ("a", "b", "c.jpg")
    with pytest.raises(SafetyError):
        paths.relative_parts("a\\..\\c.jpg")


# --------------------------------------------------------------------- 目录跳过
@pytest.mark.parametrize(
    "name",
    ["$RECYCLE.BIN", "System Volume Information", "_归类清单", ".photoos", "__pycache__", ".git"],
)
def test_should_skip_dir(name: str) -> None:
    assert paths.should_skip_dir(name)


@pytest.mark.parametrize("name", ["杭州市", "2025-07-19_1341-1413", "未知地点", "DCIM", "100NCZ"])
def test_should_not_skip_normal_dirs(name: str) -> None:
    assert not paths.should_skip_dir(name)


# --------------------------------------------------------------------- 同根判断
def test_is_same_root(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / "sub").mkdir(parents=True)
    assert paths.is_same_root(root, root / "sub" / "a.jpg")
    assert paths.is_same_root(root, root)
    assert not paths.is_same_root(root, tmp_path / "other" / "a.jpg")


def test_is_same_root_is_case_insensitive_on_windows(tmp_path: Path) -> None:
    """Windows 路径大小写不敏感，同根判断必须遵循该系统语义。"""
    root = tmp_path / "Lib"
    root.mkdir()
    assert paths.is_same_root(root, tmp_path / "lib" / "a.jpg")
