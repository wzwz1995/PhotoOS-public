"""手机 ADB 导入：可选路径，只读手机，先落到暂存区再发布到收件箱。

## 为什么是"拉到收件箱"而不是直接入库

ADB 只负责把手机上的文件**复制到本地**；入不入库、什么时候入库，仍然由收件箱
（`library/inbox.py`）决定。这样只有一条写库路径，取消/失败都不会在库里留下半成品：

    手机 --adb pull--> 暂存区 --publish(移动)--> 收件箱 --稳定判定--> 分类导入

## 四条纪律

1. **只读手机**：本模块只会生成 `devices` / `shell ls` / `pull` 三类命令；
   没有任何 `rm`/`mv`/`push` 的构造路径（有测试钉住）。
2. **不覆盖**：每次拉取都进一个新建目录；暂存区发布时也会挑一个不存在的名字。
3. **可取消**：`pull(..., stop=...)` 每一行输出都会检查；取消会终止 adb 进程并清掉
   **暂存区里我们自己的副本**（手机上的文件不受影响）。
4. **暂存区在数据根、不在收件箱**：避免"正在拉取"的文件被收件箱扫描当成可导入文件。

ADB 是可选路径：普通用户用资源管理器把照片复制进收件箱即可（P1）；
ADB 只解决"几千个文件、想自动拉"的场景。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..errors import PhotoOSError

#: 单条 adb 命令（devices / ls）的超时；pull 用 `PULL_TIMEOUT`。
ADB_TIMEOUT = 30.0
#: 整个 pull 的兜底超时（6 小时）。超大目录也不该无限挂着。
PULL_TIMEOUT = 6 * 3600
#: 允许操作的手机路径前缀。**故意不收 `/data`**：那是应用私有数据，不是相册。
REMOTE_ROOTS = ("/sdcard/", "/storage/")
#: 序列号允许的字符（作为 argv 传给 adb，本来就没有 shell 注入面，这里只是防手滑）。
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
#: `ls -l` 的一行：`-rw-rw---- 1 u0_a1 sdcard_rw 1234 2026-10-20 12:34 IMG_1.JPG`
_LS_RE = re.compile(
    r"^(?P<mode>[-dl])\S*\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<name>.+)$"
)
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")

ProgressFn = Callable[[int, int, str], None]


def _noop_progress(done: int, total: int, note: str) -> None:  # pragma: no cover - 默认值
    return None


# --------------------------------------------------------------------------- 数据
@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str                    # device | unauthorized | offline | unknown
    model: str = ""
    product: str = ""
    transport_id: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        name = (self.model or "").replace("_", " ").strip()
        return f"{name} ({self.serial})" if name else self.serial

    def to_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model.replace("_", " "),
            "product": self.product,
            "usable": self.usable,
            "label": self.label,
        }


@dataclass(frozen=True)
class AdbFile:
    name: str
    size: int
    date: str            # YYYY-MM-DD（解析不出来时为空串）
    time: str = ""       # HH:MM[:SS]
    is_dir: bool = False

    @property
    def mtime(self) -> str:
        return f"{self.date} {self.time}".strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "size": self.size,
            "date": self.date,
            "time": self.time,
            "mtime": self.mtime,
            "is_dir": self.is_dir,
        }


@dataclass
class PullResult:
    ok: bool
    files: int = 0
    bytes: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


# --------------------------------------------------------------------------- 解析
def parse_devices(stdout: str) -> list[AdbDevice]:
    """解析 `adb devices -l`。

    形如：`R58M123ABC  device product:x1 model:MI_14 device:x1 transport_id:3`；
    未授权时状态是 `unauthorized`（手机屏幕上还没点"允许"）。
    """
    devices: list[AdbDevice] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if not _SERIAL_RE.match(serial):
            continue
        attrs = {}
        for item in parts[2:]:
            if ":" in item:
                key, value = item.split(":", 1)
                attrs[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=attrs.get("model", ""),
                product=attrs.get("product", ""),
                transport_id=attrs.get("transport_id", ""),
            )
        )
    return devices


def parse_ls(stdout: str) -> list[AdbFile]:
    """解析 `ls -l` 输出；跳过 `total N` 与解析不了的行。"""
    files: list[AdbFile] = []
    for raw in (stdout or "").splitlines():
        match = _LS_RE.match(raw.strip())
        if not match:
            continue
        files.append(
            AdbFile(
                name=match.group("name").strip(),
                size=int(match.group("size")),
                date=match.group("date"),
                time=match.group("time"),
                is_dir=match.group("mode") == "d",
            )
        )
    return files


def _shell_quote(value: str) -> str:
    """给 `adb shell` 后面的那条命令加单引号（toybox sh 认这个）。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def validate_remote(remote: str) -> str:
    """校验手机路径：必须是 `/sdcard/…` 或 `/storage/…`，且不含向上穿越。"""
    text = (remote or "").strip()
    if not text:
        raise PhotoOSError("手机路径不能为空（例如 /sdcard/DCIM/Camera）")
    if "\\" in text or "\x00" in text:
        raise PhotoOSError(f"手机路径必须用 / 分隔：{remote}")
    if ".." in text.split("/"):
        raise PhotoOSError(f"手机路径不允许向上穿越（..）：{remote}")
    if not text.startswith(REMOTE_ROOTS):
        raise PhotoOSError(
            f"只允许读取手机相册目录（/sdcard 或 /storage）；拒绝：{remote}"
        )
    return text.rstrip("/") or "/"


def validate_serial(serial: str) -> str:
    text = (serial or "").strip()
    if not text:
        raise PhotoOSError("缺少设备序列号")
    if not _SERIAL_RE.match(text):
        raise PhotoOSError(f"设备序列号不合法：{serial}")
    return text


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", (value or "").strip())
    cleaned = cleaned.strip("-._") or "手机"
    return cleaned[:40]


# --------------------------------------------------------------------------- 定位
def find_adb() -> Path | None:
    """按 PATH → 常见安装目录的顺序找 adb.exe。"""
    found = shutil.which("adb")
    if found:
        return Path(found)
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path("C:/platform-tools/adb.exe"),
            Path("D:/platform-tools/adb.exe"),
            Path("C:/Android/platform-tools/adb.exe"),
            Path("D:/Android/platform-tools/adb.exe"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


class AdbClient:
    """一条 adb 命令的薄封装。`command` 是基础 argv（生产是 adb.exe；测试可注入脚本）。"""

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("adb 命令不能为空")
        self.command = list(command)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def locate(cls, settings: Settings) -> "AdbClient | None":
        """显式配置优先；`.py` 脚本用当前解释器跑（给测试与自定义包装脚本用）。"""
        configured = getattr(settings, "adb_path", None)
        if configured is not None:
            path = Path(configured)
            if path.suffix.lower() == ".py":
                return cls([sys.executable, str(path)])
            return cls([str(path)]) if path.is_file() else None
        found = find_adb()
        return cls([str(found)]) if found is not None else None

    def describe(self) -> str:
        return str(self.command[-1])

    # ------------------------------------------------------------------ 执行
    def run(self, args: list[str], *, timeout: float = ADB_TIMEOUT) -> subprocess.CompletedProcess:
        argv = [*self.command, *args]
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except OSError as exc:
            raise PhotoOSError(f"adb 无法执行：{self.describe()}（{exc}）") from exc
        except subprocess.TimeoutExpired as exc:
            raise PhotoOSError(f"adb 命令超时：{' '.join(args[:2])}") from exc

    def devices(self) -> list[AdbDevice]:
        proc = self.run(["devices", "-l"])
        if proc.returncode != 0:
            raise PhotoOSError(f"adb devices 失败：{(proc.stderr or proc.stdout).strip()[:300]}")
        return parse_devices(proc.stdout)

    def list_dir(self, serial: str, remote: str) -> list[AdbFile]:
        """列出手机目录里的文件（只读）。"""
        serial = validate_serial(serial)
        remote = validate_remote(remote)
        proc = self.run(["-s", serial, "shell", f"ls -l {_shell_quote(remote)}"])
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            if "No such file" in message or "does not exist" in message:
                raise PhotoOSError(f"手机上没有这个目录：{remote}")
            if "unauthorized" in message:
                raise PhotoOSError("手机还没授权：请在手机上点「允许 USB 调试」后重试")
            raise PhotoOSError(f"读取手机目录失败：{message[:300]}")
        return parse_ls(proc.stdout)

    # ------------------------------------------------------------------ 拉取
    def pull(
        self,
        serial: str,
        remote: str,
        local: Path,
        *,
        since: str | None = None,
        on_progress: ProgressFn | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> PullResult:
        """把手机上的目录（或 `since` 之后的文件）拉到本地暂存目录。

        `since`：只拉 `YYYY-MM-DD` 当天及之后的文件（按手机 `ls` 的日期判断）。
        不给 `since` 就走整目录 `adb pull`（最快，一次进程）。
        """
        progress = on_progress or _noop_progress
        serial = validate_serial(serial)
        remote = validate_remote(remote)
        local = Path(local)
        local.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        if stop is not None and stop():
            return PullResult(ok=False, cancelled=True, errors=["任务已取消"])

        if since:
            files = [item for item in self.list_dir(serial, remote)
                     if not item.is_dir and item.date and item.date >= since]
            if not files:
                return PullResult(ok=False, errors=[f"手机上 {since} 之后没有文件"], seconds=0.0)
            for index, item in enumerate(files, start=1):
                if stop is not None and stop():
                    return PullResult(ok=False, cancelled=True, errors=["任务已取消"])
                target = local / item.name
                result = self.run(
                    ["-s", serial, "pull", "-a", f"{remote}/{item.name}", str(target)],
                    timeout=ADB_TIMEOUT * 20,
                )
                if result.returncode != 0:
                    return PullResult(
                        ok=False,
                        errors=[f"{item.name}: {(result.stderr or result.stdout).strip()[:200]}"],
                        seconds=time.monotonic() - started,
                    )
                progress(index, len(files), f"拉取 {index}/{len(files)}：{item.name}")
            count, size = _measure(local)
            return PullResult(ok=True, files=count, bytes=size, seconds=time.monotonic() - started)

        result = self._pull_tree(serial, remote, local, progress=progress, stop=stop)
        result.seconds = time.monotonic() - started
        if result.ok:
            result.files, result.bytes = _measure(local)
        return result

    def _pull_tree(
        self,
        serial: str,
        remote: str,
        local: Path,
        *,
        progress: ProgressFn,
        stop: Callable[[], bool] | None,
    ) -> PullResult:
        # `-a`：保留源文件的修改时间。**没有 EXIF 的保存图**（微信/抖音导出的 jpg）
        # 只能靠 mtime 归日期；不加 `-a` 会被归到"拉取的今天"，那是假的拍摄时间。
        argv = [*self.command, "-s", serial, "pull", "-a", remote, str(local)]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise PhotoOSError(f"adb 无法执行：{self.describe()}（{exc}）") from exc

        output: list[str] = []
        deadline = time.monotonic() + PULL_TIMEOUT
        last_percent = -1
        buffer = ""
        cancelled = False
        assert proc.stdout is not None
        while True:
            char = proc.stdout.read(1)
            if char == "":
                break
            if char in "\r\n":
                if buffer:
                    output.append(buffer)
                    percent = _percent(buffer)
                    if percent is not None and percent != last_percent:
                        last_percent = percent
                        progress(percent, 100, f"拉取中 {percent}%")
                    buffer = ""
                continue
            buffer += char
            if len(buffer) > 2000:  # pragma: no cover - 防御异常输出
                buffer = buffer[-1000:]
            if stop is not None and stop():
                cancelled = True
                proc.terminate()
                break
            if time.monotonic() > deadline:
                proc.terminate()
                output.append("adb pull 超时，已终止")
                break
        if buffer:
            output.append(buffer)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 只有 kill 不掉才会到
            proc.kill()
            code = proc.wait()

        tail = [line for line in output if line.strip()][-5:]
        if cancelled:
            return PullResult(ok=False, cancelled=True, errors=["任务已取消"])
        if code != 0:
            return PullResult(
                ok=False,
                errors=["adb pull 失败：" + "；".join(tail)[:400]],
            )
        return PullResult(ok=True)


def _percent(line: str) -> int | None:
    match = _PERCENT_RE.search(line or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _measure(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            try:
                stat = (Path(base) / name).stat()
            except OSError:
                continue
            count += 1
            size += stat.st_size
    return count, size


# --------------------------------------------------------------------------- 发布
def staging_dir(settings: Settings, label: str = "") -> Path:
    """为一次拉取新建暂存目录（**数据根内**，不在收件箱里）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = settings.data_root / "inbox_staging" / f"{stamp}-{_safe_label(label)}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{index}")
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def publish(staging: Path, inbox_root: Path, label: str) -> Path:
    """把拉取完成的暂存目录**整体移动**进收件箱的新目录，返回落点。

    用新目录名（时间戳 + 设备名）避免覆盖收件箱里已有的同名文件；
    移动的是我们自己的副本，动不到手机上的任何文件。
    """
    staging = Path(staging)
    if not staging.is_dir():
        raise PhotoOSError(f"暂存目录不存在：{staging}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(inbox_root) / "ADB" / f"{_safe_label(label)}_{stamp}"
    target = base
    index = 2
    while target.exists():
        target = base.with_name(f"{base.name}-{index}")
        index += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(target))
    return target


def discard(staging: Path) -> None:
    """删掉**我们自己**的暂存副本（取消/失败时用）。绝不对收件箱或手机调用。"""
    path = Path(staging)
    if path.is_dir() and "inbox_staging" in path.parts:
        shutil.rmtree(path, ignore_errors=True)


def cleanup_stale_staging(settings: Settings, *, older_than_hours: float = 24.0) -> int:
    """清掉上次崩溃/断电留下的暂存目录（只碰 `data/inbox_staging`）。"""
    root = settings.data_root / "inbox_staging"
    if not root.is_dir():
        return 0
    deadline = time.time() - older_than_hours * 3600
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < deadline:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "AdbClient",
    "AdbDevice",
    "AdbFile",
    "PullResult",
    "cleanup_stale_staging",
    "discard",
    "find_adb",
    "parse_devices",
    "parse_ls",
    "publish",
    "staging_dir",
    "validate_remote",
    "validate_serial",
]
