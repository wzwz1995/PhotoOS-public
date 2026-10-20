"""相对路径处理 —— 数据库身份的关键边界。

规则（PRD §5.2 LOCKED）：
  * 数据库中**只**保存相对 Library 根的路径，使用反斜杠分隔（与 Windows 习惯一致）。
  * 相对路径**必须**拒绝：盘符、UNC 前缀、前导斜杠、`..` 上跳、绝对路径。
  * 任何 `Path.relative_to` 的结果都必须经 `validate_relative()` 才能入库，
    保证「盘符永远不会成为照片的永久身份」这一条在代码层面无法被绕过。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import SafetyError
from ..config import SKIP_DIR_NAMES

#: 盘符形式：`C:` / `E:`；UNC：`\\server\share`；设备命名空间：`\\?\`
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNC_RE = re.compile(r"^(\\\\|//)")
_DEVICE_RE = re.compile(r"^\\\\[.?]\\")


def to_relative(root: Path, path: Path) -> str:
    """把绝对路径转换为相对 `root` 的路径字符串（反斜杠分隔）。"""
    root = Path(root)
    path = Path(path)
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise SafetyError(
            f"路径不在 Library 根之下：{path}（根：{root}）",
            detail={"kind": "not_under_root", "path": str(path), "root": str(root)},
        ) from exc
    text = str(rel).replace("/", "\\")
    validate_relative(text)
    return text


def join_relative(root: Path, relative: str) -> Path:
    """把相对路径还原为绝对路径。相对路径必须已通过 `validate_relative`。"""
    validate_relative(relative)
    return Path(root) / Path(relative.replace("\\", "/"))


def validate_relative(relative: str) -> None:
    """校验相对路径的合法性；非法立即抛错（绝不静默修正）。"""
    if not relative or not relative.strip():
        raise SafetyError("相对路径为空", detail={"kind": "empty_relative"})
    text = relative.strip()
    if _DEVICE_RE.match(text):
        raise SafetyError(
            f"相对路径不得使用设备命名空间：{text}", detail={"kind": "device_path", "value": text}
        )
    if _UNC_RE.match(text):
        raise SafetyError(
            f"相对路径不得是 UNC 路径：{text}", detail={"kind": "unc_path", "value": text}
        )
    if _DRIVE_RE.match(text):
        raise SafetyError(
            f"相对路径不得包含盘符：{text}（PRD §5.2：盘符不能成为照片身份）",
            detail={"kind": "drive_letter", "value": text},
        )
    if text.startswith(("\\", "/")):
        raise SafetyError(
            f"相对路径不得以分隔符开头（这是绝对路径）：{text}",
            detail={"kind": "leading_separator", "value": text},
        )
    if re.search(r"[\\/]{2,}", text):
        raise SafetyError(
            f"相对路径不得包含连续分隔符或空路径段：{text}",
            detail={"kind": "empty_segment", "value": text},
        )
    parts = re.split(r"[\\/]", text)
    if any(part == ".." for part in parts):
        raise SafetyError(
            f"相对路径不得包含 '..'：{text}", detail={"kind": "parent_traversal", "value": text}
        )
    if any(part == "" for part in parts):
        raise SafetyError(
            f"相对路径包含空路径段：{text}", detail={"kind": "empty_segment", "value": text}
        )


def is_relative_safe(relative: str) -> bool:
    """`validate_relative` 的布尔版本（用于过滤而非中断）。"""
    try:
        validate_relative(relative)
    except SafetyError:
        return False
    return True


def relative_parts(relative: str) -> tuple[str, ...]:
    """拆分为路径段（已校验）。"""
    validate_relative(relative)
    return tuple(re.split(r"[\\/]+", relative))


def should_skip_dir(name: str) -> bool:
    """目录是否应被扫描器跳过（系统目录、既有流水线 bookkeeping、我们的数据目录）。"""
    return name.strip().lower() in SKIP_DIR_NAMES


def is_same_root(root: Path, path: Path) -> bool:
    """判断路径是否位于 root 之下（大小写不敏感，遵循 Windows 语义）。"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def like_prefix_pattern(prefix: str) -> str:
    """把相对路径前缀转成 `LIKE ... ESCAPE '!'` 可用的模式串。

    **转义字符必须用 `!` 而不是反斜杠**：Windows 相对路径里全是反斜杠，
    若用反斜杠做 ESCAPE 字符，`LIKE ? ESCAPE '\\'` 会因为路径里的 `\\` 被当成
    "被转义的反斜杠"而匹配失败（本项目真实踩过的坑）。`!` 不会出现在路径里，因此安全。
    """
    escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"{escaped}%"


def split_relative(relative: str) -> list[str]:
    """把相对路径按 `\\` 或 `/` 切成段（两种分隔符都容忍）。"""
    return [part for part in str(relative).replace("/", "\\").split("\\") if part]


# --------------------------------------------------------------------- 源路径诊断
def is_absolute_windows(text: str) -> bool:
    """是否像 Windows 绝对路径（盘符如 `E:\\` 或 UNC 如 `\\\\server\\share`）。"""
    import re

    stripped = str(text).strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", stripped)) or stripped.startswith("\\\\")


#: MTP 便携设备在资源管理器里的路径特征。这类路径**没有盘符**，只有资源管理器能访问。
_MTP_MARKERS = ("此电脑", "本机", "This PC", "可移动存储", "便携设备", "DCIM", "MTP")


def looks_like_mtp(text: str) -> bool:
    """判断是否像相机/手机通过 USB 连接的 MTP 路径（例如 `此电脑\\Z 5\\可移动存储 10001\\DCIM`）。"""
    stripped = str(text).strip()
    if not stripped or is_absolute_windows(stripped):
        return False
    return any(marker in stripped for marker in _MTP_MARKERS)


def explain_unusable_source(source: str, resolved: Path) -> str:
    """给"源目录不可用"生成**可操作**的说明，而不是干巴巴一句"不存在"。

    实测现场：用户在界面上填了相机 USB 直连的路径
    （`此电脑\\Z 5\\可移动存储 10001\\DCIM\\101NCZ_5`），只得到「Internal Server Error」。
    这类路径没有盘符，普通文件 API 读不到（既有已知限制），
    但用户需要知道的是**接下来该怎么做**，而不是"不存在"。
    """
    text = str(source).strip()
    if looks_like_mtp(text):
        return (
            "这看起来是相机/手机通过 USB 连接的 MTP 便携设备路径，PhotoOS 不能直接读取：\n"
            f"    {text}\n"
            "MTP 设备没有盘符，只有资源管理器能访问（这是已知限制，本阶段未实现直读）。请任选一种：\n"
            "  1) 把存储卡取出、用读卡器插上 —— 卡会直接变成盘符（例如 E:），再用盘符路径导入（最快，推荐）；\n"
            "  2) 在资源管理器里把相机里的 DCIM 文件夹复制到本地磁盘\n"
            "     （例如 E:\\_相机导入\\20261011\\），再把那个本地目录作为源。\n"
            "无论哪种方式，相机与存储卡上的文件都不会被改动、更不会被删除。"
        )
    if not is_absolute_windows(text):
        return (
            "源目录必须是绝对路径（例如 E:\\_相机导入\\20261011）：\n"
            f"    你填的：{text}\n"
            f"    按当前工作目录解析后：{resolved}（不存在）"
        )
    return f"源目录不存在或不可访问：{text}"
