"""AI 组件的安装/下载执行器（P1 的"手"）。

分工：
* `ai_components` 回答"是什么、装没装、装哪儿"（纯本地、可测）
* 本模块负责**真正动手**：pip 装 wheel、HTTP 下模型 —— 会联网，因此独立成文件，
  方便在测试里替换成假实现。

三条硬规则：
1. **先写 `.part`，校验通过才改名** —— 半截文件永远不冒充成品。
2. **sha256 不符就删**，并抛错（不静默接受坏文件）。
3. **pip 安装失败不留半装状态**：GPU 版替换 CPU 版时，失败要能把旧版装回去。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# 默认镜像：国内可用。用户可在设置里改。
DEFAULT_PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_MODEL_BASE = "https://hf-mirror.com"

LogFn = Callable[[str], None]


class InstallError(RuntimeError):
    """安装/下载失败。消息是给用户看的，不要塞 stack。"""


def _noop(_: str) -> None:
    pass


# ------------------------------------------------------------------ pip 组件


def install_pip(
    requirement: str,
    *,
    target_dir: Path,
    index_url: str = DEFAULT_PIP_INDEX,
    log: LogFn = _noop,
    timeout: int = 1800,
) -> None:
    """把一个 wheel 装进 `target_dir`（不动全局环境）。

    `--only-binary=:all:`：任何要现场编译的包直接失败 —— 用户机器上没有编译器，
    让它"编译到一半再说"只会得到一句看不懂的错误。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pip", "install", requirement,
        "--target", str(target_dir),
        "--only-binary=:all:",
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--index-url", index_url,
    ]
    log("$ " + " ".join(command))
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"安装超时（{timeout}s）：{requirement}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise InstallError("安装失败：" + " / ".join(tail))
    log(f"已安装 {requirement}")


def uninstall_pip(package: str, *, target_dir: Path, log: LogFn = _noop) -> None:
    """从 `target_dir` 里移除一个包（GPU/CPU 互斥时用）。"""
    command = [
        sys.executable, "-m", "pip", "uninstall", "-y", package,
        "--target", str(target_dir), "--disable-pip-version-check",
    ]
    log("$ " + " ".join(command))
    subprocess.run(command, capture_output=True, text=True, timeout=300,
                   encoding="utf-8", errors="replace")
    # 即便 pip 说没装也不算错：目标就是"它不在"


# ------------------------------------------------------------------ 模型文件


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    log: LogFn = _noop,
) -> Path:
    """下载一个模型文件到 `dest`。

    * 已存在且校验通过 → 直接返回（幂等，可反复点）
    * 有 `.part` → 用 Range 续传（大文件断了不用重下 300 MB）
    * 校验失败 → 删掉 `.part` 与目标，抛 `InstallError`
    """
    # ⚠️ 绿色包里没有 httpx（它是开发/测试依赖）。用标准库 urllib 流式下载，
    # 否则"下载组件"这条路径在打包版上会直接 ModuleNotFoundError。
    import urllib.error
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        if expected_sha256 is None or _sha256(dest) == expected_sha256:
            log(f"已存在，跳过：{dest.name}")
            return dest
        log(f"校验不符，重新下载：{dest.name}")
        dest.unlink(missing_ok=True)

    part = dest.with_suffix(dest.suffix + ".part")
    offset = part.stat().st_size if part.is_file() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    log(f"下载 {url}  →  {dest}（从 {offset} 字节续传）" if offset else f"下载 {url}  →  {dest}")

    request = urllib.request.Request(url, headers={**headers, "User-Agent": "PhotoOS"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status = int(getattr(response, "status", 200))
            if status not in (200, 206):
                raise InstallError(f"下载失败：HTTP {status}")
            mode = "ab" if (offset and status == 206) else "wb"
            if mode == "wb":
                offset = 0
            with part.open(mode) as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        if not (exc.code == 416 and offset):      # 416 且已有 .part = 已经完整
            raise InstallError(f"下载失败：HTTP {exc.code}") from exc
    except InstallError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InstallError(f"下载失败：{exc}") from exc

    if expected_sha256 is not None:
        actual = _sha256(part)
        if actual != expected_sha256:
            part.unlink(missing_ok=True)
            raise InstallError(f"校验失败：期望 {expected_sha256[:12]}…，实际 {actual[:12]}…")
    part.replace(dest)
    log(f"完成：{dest.name}（{dest.stat().st_size / 1e6:.1f} MB）")
    return dest


def free_space_gb(path: Path) -> float:
    """目标盘的可用空间（GB）—— 下 5 GB 模型前要先看一眼。"""
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
        return usage.free / (1024 ** 3)
    except OSError:
        return 0.0


# ------------------------------------------------------------------ 外挂 AI 环境
#
# 中文语义搜索需要 torch（约 2.9 GB），不能塞进包内运行时。做法是**独立 venv**：
#   %LOCALAPPDATA%\PhotoOS\ai-venv
# 装完把 `ai_python` 指向它的 python.exe。失败就删目录重来，不污染包内 Python。

AI_VENV_DIRNAME = "ai-venv"


def ai_venv_dir(base: Path | None = None) -> Path:
    if base is not None:
        return Path(base)
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return root / "PhotoOS" / AI_VENV_DIRNAME


def ensure_ai_venv(directory: Path, *, log: LogFn = _noop, timeout: int = 300) -> Path:
    """创建（或复用）一个带 pip 的 venv，返回其中的 python.exe。"""
    try:
        import venv as venv_mod
    except ModuleNotFoundError:   # embeddable Python 没有 venv
        venv_mod = None

    directory = Path(directory)
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file():
        log(f"复用已有环境：{directory}")
        return python
    directory.parent.mkdir(parents=True, exist_ok=True)
    log(f"创建独立环境：{directory}")
    try:
        if venv_mod is None:
            raise ModuleNotFoundError("No module named 'venv'")
        venv_mod.EnvBuilder(with_pip=True, clear=False).create(str(directory))
    except Exception as exc:  # noqa: BLE001
        # ★ 打包版的解释器是 **embeddable CPython**，它没有 `venv` 模块
        # （实测报错：No module named 'venv'）。退一步用 `virtualenv`：
        # 它自带 pip/setuptools 轮子，不依赖标准库的 venv。
        message = str(exc)
        if "venv" not in message and "No module" not in message:
            raise InstallError(f"创建环境失败：{exc}") from exc
        log("当前解释器没有 venv 模块（打包版是 embeddable Python），改用 virtualenv")
        created = subprocess.run([sys.executable, "-m", "virtualenv", "--quiet", str(directory)],
                                 capture_output=True, text=True, timeout=1200)
        if created.returncode != 0:
            # 绿色包里已预装 virtualenv；走到这里说明是开发环境，补装一次
            log("virtualenv 不在环境里，尝试安装")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "virtualenv"],
                           capture_output=True, text=True, timeout=600)
            created = subprocess.run([sys.executable, "-m", "virtualenv", "--quiet", str(directory)],
                                     capture_output=True, text=True, timeout=1200)
        if created.returncode != 0 or not python.is_file():
            tail = (created.stderr or created.stdout or "").strip().splitlines()[-6:]
            raise InstallError("创建环境失败（venv 与 virtualenv 都不可用）：" + " / ".join(tail))
    if not python.is_file():
        raise InstallError(f"环境创建后找不到解释器：{python}")
    log("环境就绪")
    return python


def install_into_venv(venv_python: Path, requirements: list[str], *,
                      index_url: str = DEFAULT_PIP_INDEX, log: LogFn = _noop,
                      timeout: int = 5400) -> None:
    """把包装进**独立 venv**（不是包内 site-packages）。

    和 `install_pip` 的区别：这里用 venv 自己的 python 跑 pip，
    `--only-binary` 仍然保留（用户机器没有编译器）。
    """
    command = [str(venv_python), "-m", "pip", "install", *requirements,
               "--only-binary=:all:", "--disable-pip-version-check",
               "--no-warn-script-location", "--index-url", index_url]
    log("$ " + " ".join(command))
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"安装超时（{timeout}s）：{' '.join(requirements)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise InstallError("安装失败：" + " / ".join(tail))
    log(f"已装入独立环境：{' '.join(requirements)}")
