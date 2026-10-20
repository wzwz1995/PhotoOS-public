"""统一异常体系。

约定：所有对外错误都继承 `PhotoOSError`，并带稳定的 `code`（用于 CLI/API 判定）
与面向用户的中文 `message`。内部实现细节放 `detail`，不直接抛给用户。
"""

from __future__ import annotations

from typing import Any


class PhotoOSError(Exception):
    """所有 PhotoOS 错误的基类。"""

    code = "photoos_error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 直通
        return self.message


# --------------------------------------------------------------------- 配置 / 环境
class ConfigError(PhotoOSError):
    code = "config_error"


class NotInitializedError(PhotoOSError):
    """数据库尚未初始化（未运行 `photoos init`）。"""

    code = "not_initialized"


# --------------------------------------------------------------------- 数据库 / 迁移
class DatabaseError(PhotoOSError):
    code = "database_error"


class MigrationError(DatabaseError):
    code = "migration_error"


class SchemaVersionMismatch(MigrationError):
    """数据库 schema 版本与代码期望不一致（例如来自更新版本的迁移包）。"""

    code = "schema_version_mismatch"


# --------------------------------------------------------------------- Library / 卷
class LibraryError(PhotoOSError):
    code = "library_error"


class LibraryNotFound(LibraryError):
    code = "library_not_found"


class LibraryOffline(LibraryError):
    """Library 根路径当前不可达（PRD §9.1：离线时数据库仍可用，故这不是致命错误）。"""

    code = "library_offline"


class VolumeIdentityError(LibraryError):
    code = "volume_identity_error"


# --------------------------------------------------------------------- 媒体 / 文件
class MediaError(PhotoOSError):
    code = "media_error"


class UnsupportedFormat(MediaError):
    code = "unsupported_format"


class DecodeError(MediaError):
    code = "decode_error"


class MetadataError(MediaError):
    code = "metadata_error"


# --------------------------------------------------------------------- 任务
class JobError(PhotoOSError):
    code = "job_error"


class JobNotFound(JobError):
    code = "job_not_found"


class JobNotCancellable(JobError):
    code = "job_not_cancellable"


# --------------------------------------------------------------------- 安全
class SafetyError(PhotoOSError):
    """违反资产安全铁律（例如试图写入照片库、破坏性操作缺少确认）。"""

    code = "safety_error"


class DestructiveOperationNotConfirmed(SafetyError):
    code = "destructive_not_confirmed"


# --------------------------------------------------------------------- 迁移包
class MigrationPackageError(PhotoOSError):
    code = "migration_package_error"
