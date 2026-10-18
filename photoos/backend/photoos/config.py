"""应用配置。

数据根（`data_root`）**整体可搬迁**：photoos.db、thumbnails、previews、embeddings、
ai_cache、jobs、logs、backups 都在其下（PRD §5）。照片库本身永远在外部存储。

配置来源优先级（高 → 低）：构造参数 > 环境变量 `PHOTOOS_*` > `data/config.json` > 默认值。

**注意**：本模块不得把任何绝对路径写进数据库；配置里的路径只用于运行时定位。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "PHOTOOS_"

#: 项目根标记文件。放在项目根（`C:\PhotoOS\photoos.toml`），使根目录可被稳健地发现，
#: 而不依赖「本文件必须位于仓库内第几层」这种脆弱假设（可编辑安装 / 打包后仍然有效）。
PROJECT_MARKER = "photoos.toml"


def default_user_root() -> Path:
    """**装到别人机器上**时的默认根目录（找不到 `photoos.toml` 的时候用它）。

    为什么不是"退回到模块往上三级"：那是 `site-packages`（或 venv 里的 `Lib`）。
    数据放那儿有三个问题 —— 卸载/升级 venv 会连库一起没了、用户根本找不到它、
    系统级安装还未必可写。

    所以退回**每个用户自己的目录**（各平台惯例位置）：
      * Windows：`%LOCALAPPDATA%\\PhotoOS`
      * macOS：`~/Library/Application Support/PhotoOS`
      * 其它：`$XDG_DATA_HOME/photoos` 或 `~/.local/share/photoos`

    源码仓库里（有 `photoos.toml`）行为完全不变：仍是"数据跟着项目根"。

    ⚠️ 平台判断**在这里求值**再交给 `_user_root_for()`（而不是在后者内部读全局），
    这样跨平台分支可以脱离 `os.name`/`sys.platform` 全局补丁被单测覆盖
    （补 `os.name` 会污染 `pathlib` 的家目录推导）。同时它也保证了
    `sys.platform` 在**任何平台**上都会被求值 —— 缺 `import sys` 时这里立刻 `NameError`，
    而不是等到某天有人在 macOS 上跑才炸。
    """
    return _user_root_for(os_name=os.name, platform=sys.platform)


def _user_root_for(*, os_name: str, platform: str) -> Path:
    """`default_user_root()` 的纯函数内核（平台以参数传入，便于测试）。"""
    if os_name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "PhotoOS"
        return Path.home() / "PhotoOS"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PhotoOS"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "photoos"


def find_project_root(start: Path | None = None) -> Path:
    """向上查找包含 `photoos.toml` 的目录；找不到则退回**用户目录**（见上）。

    这样代码仓库可以位于项目根下的任意子目录，数据根仍固定在项目根。
    """
    if start is None:
        start = Path(__file__).resolve()
    for candidate in (start, *start.parents):
        if (candidate / PROJECT_MARKER).is_file():
            return candidate
    return default_user_root()


#: 项目根（默认 `C:\PhotoOS`）
PROJECT_ROOT = find_project_root()
#: 代码仓库根（`<项目根>/photoos`）
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "data" / "config.json"

#: 当前生效的配置文件路径（可被测试临时改写）。
#: 用 contextvar 而不是模块级赋值：上下文变量在并发任务之间是隔离的，
#: 避免"某个测试改了全局变量，另一个线程读到别的配置"这类难查的问题。
#:
#: 需要区分三种状态，因此用一个专用哨兵而不是 `None`：
#:   * `_UNSET`     → 尚未设置，使用默认 CONFIG_PATH
#:   * `None`       → **显式禁用**配置文件（测试隔离用）
#:   * `Path(...)`  → 使用指定文件
_UNSET: Any = object()
_ACTIVE_CONFIG: ContextVar[Any] = ContextVar("photoos_active_config", default=_UNSET)


def active_config_path() -> Path | None:
    """返回当前生效的配置文件路径；显式禁用时返回 `None`。"""
    override = _ACTIVE_CONFIG.get()
    if override is _UNSET:
        return CONFIG_PATH
    return None if override is None else Path(override)


@contextmanager
def use_config_path(path: Path | None) -> Iterator[Path | None]:
    """临时把配置来源指向另一个文件；传 `None` 表示**完全禁用**配置文件。

    测试必须用它来与开发机的真实 `data/config.json` 隔离 ——
    否则测试会静默地作用在真实数据库上（这是本项目踩过的真实陷阱，
    由 `test_config_isolation` 回归守护）。
    """
    token = _ACTIVE_CONFIG.set(path)
    try:
        yield active_config_path()
    finally:
        _ACTIVE_CONFIG.reset(token)

# ---- 媒体/索引常量（Phase 1 固定值；写入 preview.generator_version）----
THUMBNAIL_MAX_EDGE = 320
PREVIEW_MAX_EDGE = 1600
PREVIEW_FORMAT = "WEBP"
PREVIEW_QUALITY = 82
GENERATOR_VERSION = "p1.0"

#: 扫描时永不进入的目录名（大小写不敏感）。显式排除既有 PowerShell 流水线的
#: bookkeeping 目录（`_归类清单`）与系统目录，避免把日志/CSV 当照片。
SKIP_DIR_NAMES = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        ".photoos",
        "_photoos",
        "_归类清单",
        "found.000",
        ".git",
        "__pycache__",
        ".thumbnails",
    }
)

#: 识别为 Asset 的扩展名。不假设固定组合：手机可能只有 JPEG/HEIC（PRD §2.2）。
PHOTO_EXTENSIONS = frozenset(
    {
        ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif",
        ".heic", ".heif", ".avif",
        ".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2",
        ".raf", ".orf", ".rw2", ".pef", ".dng", ".srw", ".raw", ".rwl", ".3fr", ".iiq",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp",
    }
)

#: RAW 家族（含 Nikon NEF）—— 决定 primary_asset 优先级与快速路径
RAW_EXTENSIONS = frozenset(
    {
        ".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2",
        ".raf", ".orf", ".rw2", ".pef", ".dng", ".srw", ".raw", ".rwl", ".3fr", ".iiq",
    }
)

#: 明确不索引的杂项扩展名（既有流水线的 CSV/日志、配置文件等）。
#: 即使这些文件位于照片库中也不会被当作 Asset 收进索引。
IGNORED_EXTENSIONS = frozenset(
    {
        ".csv", ".log", ".txt", ".md", ".ps1", ".cmd", ".bat", ".json", ".xml",
        ".dat", ".db", ".db-wal", ".db-shm", ".ini", ".lnk", ".url", ".tmp",
    }
)


class Settings(BaseSettings):
    """运行时配置。字段名即 `PHOTOOS_<FIELD>` 环境变量名，也即 config.json 的键名。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        # 配置文件是 UTF-8（可能带 BOM）；不指定编码时 Windows 会按 GBK 解码而报错。
        json_file_encoding="utf-8-sig",
    )

    # ---- 位置 ----
    data_root: Path = Field(default=PROJECT_ROOT / "data")
    web_root: Path | None = Field(default=None, description="零构建前端目录；None = 包内默认")

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8760

    # ---- 并发（0 = 自动）----
    preview_workers: int = 0
    #: ⚠️ **仅用于界面/命令行展示，不改变任何行为**：哈希阶段固定是**单线程顺序循环**
    #: （外置盘顺序读受 IO 限制，并发只会加剧寻道，见 `jobs/pipelines.py` 的说明）。
    #: 保留这个值是为了让 `/api/config` 与 `photoos config` 能如实报出"本机算出来的并发度"。
    hash_workers: int = 0
    #: 导入预演读 EXIF 的并发度；0 = 自动（见 `effective_import_scan_workers`）
    import_scan_workers: int = 0
    batch_size: int = 500
    sqlite_busy_timeout_ms: int = 15_000

    # ---- 分组阈值（§6.4，全部可重算）----
    shot_gap_seconds: int = 3
    session_gap_hours: int = 12

    # ---- Scene 划分阈值（此前只在模块常量里，用户改不了 —— 见 20-reference Q-11）----
    #
    # ⚠️ 这些默认值必须与 `library/scene_grouping.py` 的 `DEFAULT_*` 一一相等
    # （`tests/unit/test_config.py` 有一条断言钉住，避免两处各写一个数而漂移）。
    # 为什么不在 config 里 import：`scene_grouping` 依赖 numpy/底层常量，
    # 而 config 是最底层模块，反向依赖会形成环。
    #
    #: Scene 的时间断点：相邻 Shot 间隔超过它就切段（默认 900 s = 15 分钟）
    scene_gap_seconds: float = 900.0
    #: 跨边界的余弦 ≥ 它 → 两段合并（默认 0.85）
    scene_merge_similarity: float = 0.85
    #: 段内连续多少张与前一张相似度 < `scene_unrelated_similarity` 就细分（默认 8）
    scene_dissimilar_run: int = 8
    #: 单个 Scene 的 Shot 数上限（默认 60）
    scene_max_shots: int = 60
    #: 判定"不相似"的余弦阈值（默认 0.5）
    scene_unrelated_similarity: float = 0.5

    # ---- 安全（PRD §11.1：破坏性操作必须二次确认；Phase 1 无绕过入口）----
    require_confirmation_for_destructive: bool = True

    # ---- 界面直跑命令用哪个解释器（可选）----
    #: 界面上那些"准备搜索词 / 算向量 / 下载模型"的动作，默认用**服务自身的**
    #: 解释器（`sys.executable`）执行。但产品 venv 按设计**不装** AI 可选依赖
    #: （见 pyproject 的 `ai-cpu` extra），所以真机上常常需要指向另一个环境。
    #:
    #: 实际踩过：服务跑在 `C:\PhotoOS\.venv`，而 EmbeddingGemma 装在旁边的
    #: `venv` 里 —— 页面因此"能搜却不能算"，且看不出为什么。
    #: 指到这里之后，界面上的按钮就能真的跑通。
    #:
    #: 留空 = 用服务自身的解释器。命令行等价写法：`PHOTOOS_AI_PYTHON=<路径>`。
    ai_python: Path | None = Field(
        default=None,
        description="执行界面 AI 动作所用的 python.exe；None = 服务自身的解释器",
    )

    @field_validator("data_root", "web_root", "ai_python", mode="before")
    @classmethod
    def _expand(cls, v: Any) -> Any:
        if v is None or isinstance(v, Path):
            return v
        return Path(os.path.expandvars(os.path.expanduser(str(v))))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """来源优先级：构造参数 > 环境变量 > 配置文件 > 默认值。

        配置文件路径是**动态**解析的（`active_config_path()`），因此测试可以完全禁用它。
        """
        path = active_config_path()
        if path is None or not path.is_file():
            return (init_settings, env_settings)
        return (
            init_settings,
            env_settings,
            JsonConfigSettingsSource(settings_cls, json_file=path),
        )

    # ------------------------------------------------------------------ 派生路径
    @property
    def db_path(self) -> Path:
        return self.data_root / "photoos.db"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_root / "thumbnails"

    @property
    def previews_dir(self) -> Path:
        return self.data_root / "previews"

    @property
    def ai_cache_dir(self) -> Path:
        return self.data_root / "ai_cache"

    @property
    def jobs_dir(self) -> Path:
        """⚠️ **目前只被创建、没有任何写入点**（任务进度与日志都在数据库里）。

        保留它是为了兼容已有的数据目录布局（`ensure_dirs()` 会建它），
        但**不要**以为这里有 per-job 日志文件 —— 想查任务详情请看
        `/api/jobs/{id}` 或 `photoos jobs show`。若将来真要做磁盘日志，落点就是它。
        """
        return self.data_root / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_root / "backups"

    @property
    def exports_dir(self) -> Path:
        """打包产物（人物 / 主题的"一键打包"）写在这里。

        与 `backups/` 分开：备份是**数据库**的副本，打包是**原片**的副本，
        两者体积、生命周期、清理方式都不一样，混在一个目录里迟早误删。
        """
        return self.data_root / "exports"

    @property
    def effective_preview_workers(self) -> int:
        if self.preview_workers > 0:
            return self.preview_workers
        return max(2, min(6, (os.cpu_count() or 4) - 2))

    @property
    def effective_hash_workers(self) -> int:
        if self.hash_workers > 0:
            return self.hash_workers
        return max(2, min(8, (os.cpu_count() or 4) - 2))

    @property
    def effective_import_scan_workers(self) -> int:
        """导入预演读 EXIF 的并发度。

        实测（存储卡，60~90 个文件/轮）：1 worker 5.0 文件/秒、2 → 9.5、4 → 15.8、
        8 → 25.5、12 → 30.8。扩展性很好，因为这一步是**每次读一个小文件的头部**
        （NEF 需要遍历 TIFF），不像预览阶段那样要读完整片。
        取 8 作为默认：相对单线程约 5×，与预览/哈希的上限保持一致，也留有余量。
        源若是慢速机械盘，可用 `PHOTOOS_IMPORT_SCAN_WORKERS` 调小。
        """
        if self.import_scan_workers > 0:
            return self.import_scan_workers
        return max(2, min(8, (os.cpu_count() or 4)))

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.thumbnails_dir,
            self.previews_dir,
            self.ai_cache_dir,
            self.jobs_dir,
            self.logs_dir,
            self.backups_dir,
            self.exports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def resolved_web_root(self) -> Path:
        """零构建前端目录：优先配置，其次包内 `photoos/web`。"""
        if self.web_root is not None:
            return Path(self.web_root)
        return Path(__file__).resolve().parent / "web"

    # ------------------------------------------------------------------ 持久化
    def save(self, path: Path | None = None) -> Path:
        """把可持久化的字段写入 config.json（不含派生路径与服务端口等运行期项）。

        **`data_root` 只在"不是环境变量给的"时候才写入。**
        真实现场（本次开发中踩到两次）：用 `PHOTOOS_DATA_ROOT` 把数据根指到隔离目录做试验，
        `init` 会把这个**运行时覆盖值**持久化进 `<项目根>/data/config.json`；
        而优先级是「环境变量 > config.json > 默认值」，所以**下一次不带环境变量运行**
        就会去打开那个隔离目录 —— 用户看到的是"照片库消失了"（实际是打开了另一个库）。
        判据精确到值的来源：环境变量在，就说明这是运行期覆盖，不落盘。
        """
        target = Path(path) if path is not None else CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "_comment": "PhotoOS 配置；可被环境变量 PHOTOOS_* 覆盖。路径仅用于运行时定位，不写入数据库。",
            "shot_gap_seconds": self.shot_gap_seconds,
            "session_gap_hours": self.session_gap_hours,
            # Scene 五个阈值（界面可改；见 `POST /api/config/scene`）
            "scene_gap_seconds": self.scene_gap_seconds,
            "scene_merge_similarity": self.scene_merge_similarity,
            "scene_dissimilar_run": self.scene_dissimilar_run,
            "scene_max_shots": self.scene_max_shots,
            "scene_unrelated_similarity": self.scene_unrelated_similarity,
            "preview_workers": self.preview_workers,
            "hash_workers": self.hash_workers,
            "import_scan_workers": self.import_scan_workers,
            "batch_size": self.batch_size,
        }
        # 环境变量未设置时，data_root 只可能来自"用户手改的 config.json"或默认值，可以安全写回。
        # 设置时它是运行期覆盖 —— 绝不落盘，否则会污染下一次运行。
        if not os.environ.get(f"{ENV_PREFIX}DATA_ROOT"):
            payload["data_root"] = str(self.data_root)
        else:
            # 但要**保留**用户先前手改的值：`save()` 是整文件重写，
            # 直接跳过这个键会把它抹掉（等于把用户的设定删了）。
            previous = _read_existing_config(target)
            if previous.get("data_root"):
                payload["data_root"] = previous["data_root"]
        if self.web_root is not None:
            payload["web_root"] = str(self.web_root)
        # 键顺序：把 data_root 放在 _comment 之后、其余设置之前，便于人读
        ordered = {"_comment": payload.pop("_comment")}
        if "data_root" in payload:
            ordered["data_root"] = payload.pop("data_root")
        ordered.update(payload)
        target.write_text(
            json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target


def _read_existing_config(path: Path) -> dict[str, Any]:
    """读回既有 config.json；文件不存在或损坏时返回空字典（**绝不因为读不了就报错**）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings_overrides(settings: Settings, overrides: dict[str, Any],
                            path: Path | None = None) -> Path:
    """把若干配置项**合并**进 config.json（只改这几项，其它键原样保留）。

    ⚠️ 不能用 `Settings.save()` 代替：那个是"按字段整文件重写"，会**丢掉**
    不在它字段清单里的用户键（例如 `ai_python`）。改一个阈值就把 AI 解释器配置
    抹掉，是那种"看起来只是改了个数"的事故。
    """
    target = Path(path) if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = _read_existing_config(target)
    if not data:
        data = {"_comment": "PhotoOS 配置；可被环境变量 PHOTOOS_* 覆盖。"}
    data.update(overrides)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清除配置缓存。"""
    get_settings.cache_clear()


def ai_python_for_hint(settings: "Settings" | None = None, *, fallback: str = "python") -> str:
    """给用户看的「该用哪个解释器跑 AI 命令」。

    ⚠️ 打包发给别人之后，本机写死的 `D:\\PhotoOS\\scratch\\venv-eg2\\...` 根本不存在 ——
    一律从配置推导：`ai_python` 有值就用它；否则给一个中性的 `python`（不假装知道路径）。
    """
    value = getattr(settings, "ai_python", None) if settings is not None else None
    return str(value) if value else fallback
