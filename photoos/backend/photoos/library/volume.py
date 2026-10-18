"""Windows 卷与文件身份（ctypes，不依赖第三方库）。

为什么需要它（PRD §5.1 / §5.2 LOCKED）：
  * 照片的永久身份**不能**是盘符、绝对路径、用户名或机器名。
  * 因此每块盘需要一个稳定标识：卷序列号（`GetVolumeInformationW`）是主要来源；
    卷序列号会因格式化/整盘更换而改变，所以再叠加**卷指纹**（相对路径+size+mtime 抽样摘要）
    作为第二判据。

同时还读取 Win32 文件索引（`GetFileInformationByHandle`）——同一卷内改名/移动后
`(volume_serial, file_index)` 不变，可用于"文件被移动"的高置信度识别。
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

IS_WINDOWS: Final = os.name == "nt"

#: 远程/不可用卷序列号（GetVolumeInformationW 失败时的返回值），不作为身份
INVALID_SERIAL: Final = 0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None


# --------------------------------------------------------------------------- 类型
class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


if IS_WINDOWS:  # pragma: no branch
    _kernel32.GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    _kernel32.GetVolumeInformationW.restype = ctypes.c_int

    _kernel32.GetVolumePathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    _kernel32.GetVolumePathNameW.restype = ctypes.c_int

    _kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    _kernel32.GetDriveTypeW.restype = ctypes.c_uint32

    _kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    _kernel32.CreateFileW.restype = ctypes.c_void_p

    _kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _kernel32.GetFileInformationByHandle.restype = ctypes.c_int

    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int

# 卷类型（Win32 GetDriveType 返回值）
DRIVE_TYPES: Final[dict[int, str]] = {
    0: "unknown",
    1: "invalid",
    2: "removable",
    3: "fixed",
    4: "network",
    5: "cdrom",
    6: "ram",
}

_GENERIC_READ: Final = 0x80000000
_FILE_SHARE_ALL: Final = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING: Final = 3
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value


# --------------------------------------------------------------------------- 数据
@dataclass(frozen=True)
class VolumeIdentity:
    """一个挂载点的卷身份。`serial` 是主判据，其余用于消歧与展示。"""

    root: str          # 例如 "E:\\"
    serial: int        # 卷序列号（0 = 无法获取）
    label: str         # 卷标
    fs_type: str       # NTFS / exFAT / ...
    kind: str          # fixed / removable / network / ...
    mount: str         # 最近一次盘符（仅提示，不作身份）

    @property
    def has_serial(self) -> bool:
        return self.serial != INVALID_SERIAL

    def describe(self) -> str:
        serial = f"{self.serial:08X}" if self.has_serial else "无序列号"
        return f"{self.mount} 卷标={self.label or '(无)'} 类型={self.kind} FS={self.fs_type} 序列号={serial}"


@dataclass(frozen=True)
class FileIdentity:
    """同一卷内稳定的文件身份：改名/移动后 `(volume_serial, file_index)` 不变。"""

    volume_serial: int
    file_index: str    # 64 位十六进制
    size: int
    mtime_ns: int

    def as_key(self) -> str:
        return f"{self.volume_serial:08X}:{self.file_index}"


# --------------------------------------------------------------------------- 卷
def volume_root_for(path: Path | str) -> str | None:
    """返回路径所属卷的根（如 `E:\\`）。UNC/网络路径可能返回 None。"""
    if not IS_WINDOWS:
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    if not _kernel32.GetVolumePathNameW(str(Path(path).absolute()), buffer, len(buffer)):
        return None
    return buffer.value


def drive_kind(mount: str) -> str:
    if not IS_WINDOWS:
        return "unknown"
    return DRIVE_TYPES.get(int(_kernel32.GetDriveTypeW(mount)), "unknown")


def volume_identity(path: Path | str) -> VolumeIdentity | None:
    """读取路径所在卷的身份。路径不存在或无法访问时返回 None（调用方按离线处理）。"""
    if not IS_WINDOWS:
        return None
    root = volume_root_for(path)
    if not root:
        return None
    return volume_identity_for_root(root)


def volume_identity_for_root(root: str) -> VolumeIdentity | None:
    if not IS_WINDOWS:
        return None
    serial = ctypes.c_uint32(0)
    max_component = ctypes.c_uint32(0)
    flags = ctypes.c_uint32(0)
    label = ctypes.create_unicode_buffer(261)
    fs_type = ctypes.create_unicode_buffer(261)

    ok = _kernel32.GetVolumeInformationW(
        root,
        label,
        len(label),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_type,
        len(fs_type),
    )
    if not ok:
        # 卷当前不可访问（例如拔盘）：返回 None，让上层把 Library 标记为 OFFLINE
        return None

    return VolumeIdentity(
        root=root,
        serial=int(serial.value),
        label=label.value,
        fs_type=fs_type.value,
        kind=drive_kind(root),
        mount=root.rstrip("\\/"),
    )


# --------------------------------------------------------------------------- 文件
def file_identity(path: Path | str) -> FileIdentity | None:
    """读取文件身份。不修改文件（只读打开，且允许共享读写删除）。"""
    if not IS_WINDOWS:
        return None
    handle = _kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    try:
        info = _ByHandleFileInformation()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None
        index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        mtime = (int(info.ftLastWriteTime.dwHighDateTime) << 32) | int(
            info.ftLastWriteTime.dwLowDateTime
        )
        # FILETIME 以 100ns 为单位，自 1601-01-01 起算；转换为 Unix epoch 纳秒
        mtime_ns = (mtime - 116444736000000000) * 100
        return FileIdentity(
            volume_serial=int(info.dwVolumeSerialNumber),
            file_index=f"{index:016X}",
            size=size,
            mtime_ns=mtime_ns,
        )
    finally:
        _kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- 展示
def format_serial(serial: int | None) -> str:
    if not serial:
        return "无"
    return f"{int(serial):08X}"


def human_bytes(size: int | float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
