"""配置模块测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from photoos.config import (
    CONFIG_PATH,
    GENERATOR_VERSION,
    PHOTO_EXTENSIONS,
    PROJECT_MARKER,
    PROJECT_ROOT,
    RAW_EXTENSIONS,
    REPO_ROOT,
    SKIP_DIR_NAMES,
    VIDEO_EXTENSIONS,
    Settings,
    _user_root_for,
    default_user_root,
    find_project_root,
)


def test_defaults_are_under_project_root() -> None:
    settings = Settings()
    assert settings.db_path.name == "photoos.db"
    assert settings.thumbnails_dir.name == "thumbnails"
    assert settings.previews_dir.name == "previews"
    assert settings.data_root == settings.db_path.parent


def test_derived_paths_all_live_under_data_root(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path)
    for path in (
        settings.db_path,
        settings.thumbnails_dir,
        settings.previews_dir,
        settings.ai_cache_dir,
        settings.jobs_dir,
        settings.logs_dir,
        settings.backups_dir,
    ):
        assert tmp_path in path.parents or path == tmp_path


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("PHOTOOS_SHOT_GAP_SECONDS", "7")
    settings = Settings()
    assert settings.data_root == tmp_path
    assert settings.shot_gap_seconds == 7


def test_init_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"
    assert Settings(data_root=explicit).data_root == explicit


def test_ensure_dirs_creates_all(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "root")
    settings.ensure_dirs()
    assert settings.db_path.parent.is_dir()
    for path in (
        settings.thumbnails_dir,
        settings.previews_dir,
        settings.ai_cache_dir,
        settings.jobs_dir,
        settings.logs_dir,
        settings.backups_dir,
    ):
        assert path.is_dir()


def test_save_is_utf8_and_round_trippable(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "数据根")
    target = settings.save(tmp_path / "config.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["data_root"].endswith("数据根")
    assert payload["shot_gap_seconds"] == settings.shot_gap_seconds


def test_save_does_not_persist_env_overridden_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**环境变量给的数据根绝不能被持久化**（缺陷 #16）。

    真实现场：用 `PHOTOOS_DATA_ROOT` 把数据根指到隔离目录做试验，`init` 把该值写进了
    `<项目根>/data/config.json`；由于优先级是「环境变量 > config.json > 默认值」，
    **下一次不带环境变量运行**就会去打开隔离目录 —— 用户看到的是"照片库消失了"。
    本次开发中踩到两次，因此这里把两个方向都钉住。
    """
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path / "isolated"))
    target = Settings().save(tmp_path / "config.json")
    assert "data_root" not in json.loads(target.read_text(encoding="utf-8")), (
        "运行期覆盖值被落盘了，下一次不带环境变量运行会打开错误的库"
    )


def test_save_still_persists_user_chosen_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """反方向：没有环境变量时，用户设定的数据根必须照常写回（不能因为修复而丢设置）。"""
    monkeypatch.delenv("PHOTOOS_DATA_ROOT", raising=False)
    chosen = tmp_path / "我的数据根"
    target = Settings(data_root=chosen).save(tmp_path / "config.json")
    assert json.loads(target.read_text(encoding="utf-8"))["data_root"] == str(chosen)


def test_save_preserves_existing_user_data_root_under_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """环境变量覆盖时，既不能被覆盖值污染，**也不能把用户手改的值抹掉**。

    `save()` 是整文件重写：只"跳过这个键"会把用户先前的设定一起删掉。
    这里把两个方向都钉住。
    """
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"data_root": r"E:\用户自己设的数据根", "batch_size": 123}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path / "isolated"))
    Settings().save(target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["data_root"] == r"E:\用户自己设的数据根", "用户的设定被抹掉了"
    assert "isolated" not in json.dumps(payload), "运行期覆盖值被落盘了"
    assert payload["batch_size"] == Settings().batch_size, "其余字段照常写回"


def test_save_tolerates_corrupt_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """既有 config.json 损坏时，save() 必须能正常重写，而不是抛异常。"""
    target = tmp_path / "config.json"
    target.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path / "isolated"))
    Settings().save(target)
    assert "data_root" not in json.loads(target.read_text(encoding="utf-8"))


def test_worker_counts_are_bounded() -> None:
    settings = Settings(preview_workers=0, hash_workers=0)
    assert 2 <= settings.effective_preview_workers <= 8
    assert 2 <= settings.effective_hash_workers <= 8
    assert Settings(preview_workers=1).effective_preview_workers == 1


def test_extension_sets_are_disjoint_and_cover_prd_requirements() -> None:
    # PRD §9.1：RAW/JPEG/HEIC/PNG/TIFF/视频 —— 不得假设固定组合
    for ext in (".nef", ".jpg", ".heic", ".png", ".tiff", ".mov"):
        assert ext in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
    assert ".nef" in RAW_EXTENSIONS
    assert not (PHOTO_EXTENSIONS & VIDEO_EXTENSIONS)
    # 既有流水线的 bookkeeping 目录绝不能被当成照片扫描
    assert "_归类清单" in SKIP_DIR_NAMES
    assert "$recycle.bin" in SKIP_DIR_NAMES


def test_generator_version_is_a_constant() -> None:
    assert GENERATOR_VERSION == "p1.0"


def test_config_path_is_under_project_root() -> None:
    assert CONFIG_PATH.name == "config.json"
    assert CONFIG_PATH.parent.name == "data"
    assert CONFIG_PATH.parent.parent == PROJECT_ROOT


# ------------------------------------------------------------- 项目根发现机制
def test_project_marker_exists_at_project_root() -> None:
    """项目根必须有 photoos.toml 标记；数据根固定在项目根，而非代码仓库。"""
    marker = PROJECT_ROOT / PROJECT_MARKER
    assert marker.is_file(), f"缺少项目根标记文件：{marker}"


def test_repo_root_is_inside_project_root() -> None:
    assert PROJECT_ROOT in REPO_ROOT.parents


def test_find_project_root_from_repo_file() -> None:
    """从仓库内任意位置向上查找，都应定位到同一项目根。"""
    assert find_project_root(Path(__file__).resolve()) == PROJECT_ROOT
    assert find_project_root(REPO_ROOT) == PROJECT_ROOT


def test_find_project_root_walks_up_to_marker(tmp_path: Path) -> None:
    (tmp_path / PROJECT_MARKER).write_text("[project]\n", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_project_root(deep) == tmp_path


def test_find_project_root_falls_back_without_marker(tmp_path: Path) -> None:
    """没有标记时不得抛异常，退回模块内的推导路径（保证包仍可用）。"""
    fallback = find_project_root(tmp_path / "nowhere")
    assert fallback.is_absolute()


def test_default_data_root_is_under_project_root() -> None:
    settings = Settings()
    assert settings.data_root == PROJECT_ROOT / "data"
    assert settings.db_path == PROJECT_ROOT / "data" / "photoos.db"


# ============================================================ 跨平台用户目录
#
# ★ 这几个测试是**回归**：`default_user_root()` 曾经直接读 `sys.platform`
#   而 `config.py` **没有 `import sys`** —— 在 Windows 上永远走不到那行，
#   所以全量测试全绿，缺陷只在 macOS 上抛 `NameError`（正是项目文档说的
#   "本机永远看不见"那一类）。故意破坏：删掉 `config.py` 顶部的 `import sys`，
#   `test_default_user_root_is_callable_here` 立刻 NameError —— 因为在 Windows 上
#   `sys.platform` 也已经被求值了。

def test_default_user_root_is_callable_here() -> None:
    root = default_user_root()
    assert root.is_absolute()
    assert root.name in ("PhotoOS", "photoos")


def test_user_root_on_macos() -> None:
    root = _user_root_for(os_name="posix", platform="darwin")
    assert root == Path.home() / "Library" / "Application Support" / "PhotoOS"


def test_user_root_on_linux_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert _user_root_for(os_name="posix", platform="linux") == tmp_path / "xdg" / "photoos"


def test_user_root_on_linux_without_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    root = _user_root_for(os_name="posix", platform="linux")
    assert root == Path.home() / ".local" / "share" / "photoos"

# ============================================================ Scene 阈值（Q-11）
#
# ★ 背景：这五个阈值原先只存在于 `library/scene_grouping.py` 的模块常量里，
#   `Settings` 与 CLI 都没入口 —— 用户**无法调整** Scene 划分，只能接受默认值。
#   现在它们是配置项（`data/config.json` 可改，界面触发的 scenes_rebuild 会读）。

def test_scene_threshold_defaults_match_the_module_constants() -> None:
    """配置默认值与模块常量必须**逐个相等**（两处各写一个数就会漂移）。

    故意破坏：把 `Settings.scene_merge_similarity` 改成 0.9，本测试立刻变红。
    """
    from photoos.library import scene_grouping

    settings = Settings()
    assert settings.scene_gap_seconds == scene_grouping.DEFAULT_SCENE_GAP_SECONDS
    assert settings.scene_merge_similarity == scene_grouping.DEFAULT_MERGE_THRESHOLD
    assert settings.scene_dissimilar_run == scene_grouping.DEFAULT_DISSIMILAR_RUN
    assert settings.scene_max_shots == scene_grouping.DEFAULT_MAX_SHOTS
    assert settings.scene_unrelated_similarity == scene_grouping.DEFAULT_UNRELATED_SIMILARITY


def test_scene_thresholds_are_configurable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """可通过 `PHOTOOS_*` 环境变量（以及 config.json）调整。"""
    monkeypatch.setenv("PHOTOOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PHOTOOS_SCENE_MERGE_SIMILARITY", "0.9")
    monkeypatch.setenv("PHOTOOS_SCENE_DISSIMILAR_RUN", "5")
    monkeypatch.setenv("PHOTOOS_SCENE_GAP_SECONDS", "1200")
    settings = Settings()
    assert settings.scene_merge_similarity == pytest.approx(0.9)
    assert settings.scene_dissimilar_run == 5
    assert settings.scene_gap_seconds == pytest.approx(1200.0)
def test_save_settings_overrides_keeps_unknown_keys(tmp_path: Path) -> None:
    """★ 回归：改阈值**不能**把 config.json 里的其它键（尤其 ai_python）抹掉。

    故意破坏：把这个函数换成 `settings.save()`，本测试立刻变红。
    """
    from photoos.config import save_settings_overrides

    target = tmp_path / "config.json"
    target.write_text(json.dumps({"ai_python": r"D:\x\python.exe", "shot_gap_seconds": 3}),
                      encoding="utf-8")
    settings = Settings()
    save_settings_overrides(settings, {"scene_merge_similarity": 0.9}, path=target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["scene_merge_similarity"] == 0.9
    assert data["ai_python"] == r"D:\x\python.exe", "其它键必须原样保留"
    assert data["shot_gap_seconds"] == 3


def test_scene_overrides_are_persisted_and_reloadable(tmp_path: Path) -> None:
    from photoos.config import save_settings_overrides

    target = tmp_path / "config.json"
    settings = Settings()
    save_settings_overrides(settings, {"scene_max_shots": 80,
                                       "scene_unrelated_similarity": 0.42}, path=target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["scene_max_shots"] == 80
    assert data["scene_unrelated_similarity"] == 0.42