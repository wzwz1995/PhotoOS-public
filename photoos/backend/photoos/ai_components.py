"""AI 组件的登记表与状态探测（P1 地基）。

设计约束（见目标）：
* **组件可选**：不装的组件不影响基础功能（导入/索引/缩略图/精选/主题），
  只有对应入口显示"需要下载"。
* **安装在包内**：绿色包把 wheel 装进 `runtime\\python\\Lib\\site-packages`；
  若该目录不可写（安装版落在 Program Files），退回 `%LOCALAPPDATA%\\PhotoOS\\pylibs`
  并在启动时挂 `sys.path`。
* **下载走镜像**：pip 源与模型源**分开配置**，默认国内可用（清华 / hf-mirror）。

这里只做"是什么、装没装、装哪儿"，**不做网络**；下载/安装由调用方（API 层）执行，
便于测试与失败回滚。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 组件登记表


@dataclass(frozen=True)
class Component:
    """一个可下载组件。

    kind:
      * ``pip``   —— 通过 pip 安装的 wheel（装进 site-packages）
      * ``model`` —— 下载的模型文件（装进 <data_root>/ai_cache）
    """

    id: str
    label: str
    kind: str
    size_mb: int
    module: str | None = None          # pip 组件：装好后应能 import 的模块名
    package: str | None = None          # pip 组件的 pip 需求串
    files: tuple[str, ...] = ()         # model 组件：相对 ai_cache 的文件
    unlocks: str = ""                   # 装完解锁什么（给用户看）
    requires: tuple[str, ...] = ()      # 依赖的其它组件 id
    note: str = ""                      # 额外说明（体积/前提）


COMPONENTS: tuple[Component, ...] = (
    Component(
        id="onnxruntime-cpu", label="ONNX Runtime（CPU）", kind="pip", size_mb=15,
        module="onnxruntime", package="onnxruntime==1.31.0",
        unlocks="视觉嵌入与人脸推理的运行时（CPU）",
    ),
    Component(
        id="clip-model", label="CLIP 视觉模型（视觉塔）", kind="model", size_mb=335,
        files=("vision_model.onnx", "preprocessor_config.json"),
        unlocks="相似照片 / 以图搜图（视觉嵌入 512 维）",
        requires=("onnxruntime-cpu",),
        note="只做图像嵌入；中文文字搜索走 Gemma，见下一条",
    ),
    Component(
        id="opencv", label="OpenCV（headless）", kind="pip", size_mb=55,
        module="cv2", package="opencv-python-headless>=4.8",
        unlocks="人脸检测与识别",
    ),
    Component(
        id="face-models", label="人脸模型（YuNet + SFace）", kind="model", size_mb=37,
        files=("face-yunet2023mar-sface2021dec",),
        unlocks="人物页（自动认人）",
        requires=("opencv",),
    ),
    Component(
        id="gemma-text", label="中文语义搜索（EmbeddingGemma）", kind="venv",
        size_mb=2750,   # 实测：venv(含运行时依赖/torchvision) ~1.3 GB + 模型 1.46 GB
        unlocks="用中文搜照片（768 维），CLIP 做不到这件事",
        note="实测约 6 分钟；装完还需导入照片并跑分组，搜索池才有内容",
    ),
    Component(
        id="onnxruntime-gpu", label="ONNX Runtime（GPU）", kind="pip", size_mb=3000,
        module="onnxruntime", package="onnxruntime-gpu==1.31.0",
        unlocks="同上，但走 NVIDIA GPU（更快）",
        requires=("clip-model",),
        note="只服务 CLIP / 人脸等 ONNX 模型；不含 EmbeddingGemma",
    ),
    Component(
        id="torch-cuda", label="EmbeddingGemma CUDA 加速（torch）", kind="venv", size_mb=2100,
        unlocks="中文画面词的图像向量走 NVIDIA GPU；GPU 实测约 234 ms/张",
        requires=("gemma-text",),
        note="装在独立 AI 环境；只加速 EmbeddingGemma，不是 ONNX Runtime GPU。"
             "安装后建议先试算 50 张，确认显存与速度。",
    ),
)

BY_ID = {component.id: component for component in COMPONENTS}


def model_root(data_root: Path) -> Path:
    """模型统一放数据根下，跟库一起走（换机不用重下）。"""
    return Path(data_root) / "ai_cache"


def site_packages_dir() -> Path:
    """当前解释器的 site-packages。

    打包版是 `runtime\\python\\Lib\\site-packages`；若不可写，
    调用方应改用 `fallback_site_packages()`。
    """
    for path in sys.path:
        if path.endswith("site-packages"):
            return Path(path)
    return Path(sys.prefix) / "Lib" / "site-packages"


def fallback_site_packages() -> Path:
    """包目录不可写时的安装位置（用户级，不需要管理员）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "PhotoOS" / "pylibs"


def writable_install_dir() -> Path:
    """选一个**可写**的安装目录：优先包内，其次用户目录。"""
    primary = site_packages_dir()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".photoos-write-test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return primary
    except OSError:
        target = fallback_site_packages()
        target.mkdir(parents=True, exist_ok=True)
        return target


# ---------------------------------------------------------------- 状态探测


def _module_available(name: str | None, extra_paths: tuple[Path, ...] = ()) -> bool:
    if not name:
        return True
    # ⚠️ 刚下载安装完的包，当前进程的 finder 还缓存着旧目录列表 ——
    # 不 invalidate 的话，用户点完"安装"会看到状态还是"未装"（重启才好）。
    importlib.invalidate_caches()
    if importlib.util.find_spec(name) is not None:
        return True
    # 组件可能装在 fallback 目录里 —— 它不一定在 sys.path 上
    for base in extra_paths:
        if (base / name).exists() or (base / f"{name}.py").exists():
            return True
    return False


def _model_ready(component_id: str, data_root: Path) -> bool:
    """模型就绪判断**交给 model_store** —— 它有真实文件名、字节数与 sha256，
    自己拼路径只会得到一个永远 404 的地址（已踩过）。"""
    from .library import model_store

    if component_id == "clip-model":
        return model_store.is_ready(data_root)
    if component_id == "clip-text-model":
        return model_store.is_text_ready(data_root)
    if component_id == "gemma-text":
        from .library import model_store

        try:
            from .ai_install import ai_venv_dir

            python = ai_venv_dir() / "Scripts" / "python.exe"
            return python.is_file() and model_store.is_gemma_ready(data_root)
        except Exception:  # noqa: BLE001
            return False
    if component_id == "torch-cuda":
        try:
            from .ai_install import ai_venv_dir

            python = ai_venv_dir() / "Scripts" / "python.exe"
            if not python.is_file():
                return False
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            probe = subprocess.run(
                [str(python), "-c",
                 "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"],
                capture_output=True, timeout=30, creationflags=flags,
            )
            return probe.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    if component_id == "face-models":
        return model_store.is_face_ready(data_root)
    return False


@dataclass
class ComponentStatus:
    id: str
    label: str
    kind: str
    size_mb: int
    installed: bool
    unlocks: str
    requires: tuple[str, ...] = ()
    note: str = ""
    blocked_by: tuple[str, ...] = field(default_factory=tuple)


def detect(data_root: Path) -> list[ComponentStatus]:
    """探测所有组件状态。纯本地，不发网络请求。"""
    extras = (fallback_site_packages(),)
    statuses: dict[str, ComponentStatus] = {}
    for component in COMPONENTS:
        if component.kind == "external":
            from .library import model_store

            installed = model_store.is_gemma_ready(data_root)
        elif component.kind == "pip":
            installed = _module_available(component.module, extras)
            # CPU/GPU 是同一个模块名（都叫 onnxruntime）—— 只能靠 provider 区分，
            # 否则装了 CPU 版会把 GPU 版也显示成"已安装"。
            if component.id == "onnxruntime-gpu":
                installed = installed and "CUDAExecutionProvider" in gpu_providers()
            elif component.id == "onnxruntime-cpu":
                installed = installed
        else:
            installed = _model_ready(component.id, data_root)
        statuses[component.id] = ComponentStatus(
            id=component.id, label=component.label, kind=component.kind,
            size_mb=component.size_mb, installed=installed,
            unlocks=component.unlocks, requires=component.requires,
            note=component.note,
        )
    # 依赖没装的组件，标出 blocked_by（UI 上灰掉并说明原因）
    for status in statuses.values():
        missing = tuple(r for r in status.requires if not statuses[r].installed)
        status.blocked_by = missing
    return [statuses[c.id] for c in COMPONENTS]


def gpu_providers() -> list[str]:
    """已安装 onnxruntime 的可用 provider（没装就返回空）。"""
    importlib.invalidate_caches()
    try:
        import onnxruntime as ort  # noqa: PLC0415  —— 可选依赖，按需导入
    except Exception:  # noqa: BLE001
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return []
