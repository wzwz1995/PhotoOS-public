"""Library 子系统：卷身份、相对路径、只读遍历、Library/Volume 持久化、重绑定、迁移。"""

from __future__ import annotations

from .migration import (
    ExportReport,
    ImportReport,
    VerifyResult,
    backup_database,
    export_package,
    import_package,
    list_backups,
    prune_backups,
    verify_package,
)
from .rebind import (
    RebindReport,
    library_availability,
    mark_library_offline,
    rebind_library,
)
from .store import (
    LibraryRecord,
    LibraryStore,
    ProbeResult,
    VolumeRecord,
    compute_fingerprint,
    match_library_by_root,
    probe_path,
    read_marker,
    write_marker,
)
from .volume import FileIdentity, VolumeIdentity, file_identity, volume_identity
from .walker import FileEntry, WalkError, WalkStats, classify_extension, count_media, walk_media

__all__ = [
    "ExportReport",
    "FileEntry",
    "FileIdentity",
    "ImportReport",
    "LibraryRecord",
    "LibraryStore",
    "ProbeResult",
    "RebindReport",
    "VerifyResult",
    "VolumeIdentity",
    "VolumeRecord",
    "WalkError",
    "WalkStats",
    "backup_database",
    "classify_extension",
    "compute_fingerprint",
    "count_media",
    "export_package",
    "file_identity",
    "import_package",
    "library_availability",
    "list_backups",
    "mark_library_offline",
    "match_library_by_root",
    "probe_path",
    "prune_backups",
    "read_marker",
    "rebind_library",
    "verify_package",
    "volume_identity",
    "walk_media",
    "write_marker",
]
