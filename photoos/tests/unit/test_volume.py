"""卷身份与文件身份测试。

这些是不变量测试：无论跑在哪个盘、盘符是什么，结论都必须一致 —— 因为
「盘符不能成为照片身份」是 PRD §5.2 的硬要求。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from photoos.library import volume
from photoos.library.volume import (
    FileIdentity,
    VolumeIdentity,
    drive_kind,
    file_identity,
    format_serial,
    human_bytes,
    volume_identity,
    volume_identity_for_root,
    volume_root_for,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="卷身份依赖 Win32 API")


# --------------------------------------------------------------------- 纯函数
def test_format_serial() -> None:
    assert format_serial(0x24422F45) == "24422F45"
    assert format_serial(0) == "无"
    assert format_serial(None) == "无"


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (1024**4 * 3, "3.0 TB"),
        (1024**5, "1024.0 TB"),
    ],
)
def test_human_bytes(size: int, expected: str) -> None:
    assert human_bytes(size) == expected


def test_volume_identity_has_serial_flag() -> None:
    assert VolumeIdentity("E:\\", 0x24422F45, "Elements SE", "NTFS", "fixed", "E:").has_serial
    assert not VolumeIdentity("E:\\", 0, "x", "NTFS", "fixed", "E:").has_serial


def test_describe_includes_serial_hex() -> None:
    identity = VolumeIdentity("E:\\", 0x24422F45, "Elements SE", "NTFS", "fixed", "E:")
    text = identity.describe()
    assert "24422F45" in text
    assert "Elements SE" in text


def test_file_identity_key_is_stable_string() -> None:
    identity = FileIdentity(volume_serial=0x24422F45, file_index="0000000000001A2B", size=1, mtime_ns=2)
    assert identity.as_key() == "24422F45:0000000000001A2B"


# --------------------------------------------------------------------- Win32
@windows_only
def test_volume_identity_of_temp_dir(tmp_path: Path) -> None:
    identity = volume_identity(tmp_path)
    assert identity is not None
    assert identity.root.endswith("\\")
    assert identity.fs_type, "应能读到文件系统类型"
    assert identity.kind in {"fixed", "removable", "network", "ram", "cdrom", "unknown"}


@windows_only
def test_volume_root_for_is_drive_root(tmp_path: Path) -> None:
    root = volume_root_for(tmp_path)
    assert root is not None
    assert len(root) == 3 and root.endswith(":\\"), f"应是盘根，实际 {root!r}"


@windows_only
def test_drive_kind_known_value() -> None:
    root = volume_root_for(Path.cwd())
    assert root is not None
    assert drive_kind(root) in set(volume.DRIVE_TYPES.values())


@windows_only
def test_volume_identity_for_nonexistent_root_returns_none() -> None:
    assert volume_identity_for_root("Q:\\") is None
    assert volume_identity(Path("Q:\\nope")) is None


@windows_only
def test_file_identity_is_stable_across_rename(tmp_path: Path) -> None:
    """同一卷内改名后 (volume_serial, file_index) 不变 —— 用于识别"文件被移动"。"""
    original = tmp_path / "a.bin"
    original.write_bytes(b"photoos-identity-test")
    first = file_identity(original)
    assert first is not None

    renamed = tmp_path / "b.bin"
    original.rename(renamed)
    second = file_identity(renamed)
    assert second is not None

    assert second.volume_serial == first.volume_serial
    assert second.file_index == first.file_index
    assert second.size == first.size == len(b"photoos-identity-test")


@windows_only
def test_file_identity_matches_stat(tmp_path: Path) -> None:
    target = tmp_path / "c.bin"
    payload = b"x" * 4096
    target.write_bytes(payload)
    identity = file_identity(target)
    stat = target.stat()
    assert identity is not None
    assert identity.size == stat.st_size
    assert identity.mtime_ns == stat.st_mtime_ns


@windows_only
def test_file_identity_returns_none_for_missing(tmp_path: Path) -> None:
    assert file_identity(tmp_path / "missing.bin") is None


@windows_only
def test_file_identity_does_not_modify_file(tmp_path: Path) -> None:
    """只读守卫：读取身份后文件的 size/mtime/内容必须完全不变。"""
    target = tmp_path / "d.bin"
    target.write_bytes(b"immutable-check")
    before = target.stat()
    digest_before = target.read_bytes()
    assert file_identity(target) is not None
    after = target.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert target.read_bytes() == digest_before
    assert not target.with_suffix(".bin.tmp").exists()
