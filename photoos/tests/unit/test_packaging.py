"""★ 「发给别人能不能装上」的守卫（静态部分）。

## 为什么需要这个文件

这台机器上 PhotoOS 是**可编辑安装**（`__editable__.photoos-0.1.0.pth` 指向源码树），
所以下面三个缺陷在本机**全都看不见**，只有真的打一个包、装到干净环境里才会暴露：

  1. `pyproject.toml` 里写的是 `web/*` —— 那只匹配 `web` 目录**下一层**，
     于是 `web/js/*.js`、`web/css/*.css`、`web/fonts/*` 全都不进包。
     【实测】修之前 wheel 里只有 `index.html`：装到别人机器上打开就是白页。
  2. 界面里写死了我自己的解释器路径
     （`C:\\PhotoOS\\venv\\venv-eg2\\Scripts\\python.exe -m photoos ...`）。
     别人点了那个提示，只会得到"系统找不到指定的路径"。
  3. 安装包装好后数据根会落到 `site-packages` 旁边 —— 卸载 venv 连库一起没。

所以这里的判据都是"**这次真的踩过**"的那几条，不是想象的规范。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend" / "photoos"
WEB = BACKEND / "web"
PACKAGING = REPO / "packaging"   # 安装脚本与说明的**唯一来源**（跟着代码一起版本化）


def _package_data_patterns() -> list[str]:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"\[tool\.setuptools\.package-data\](.*?)(\n\[|\Z)", text, re.S)
    assert match, "pyproject.toml 里找不到 [tool.setuptools.package-data]"
    block = match.group(1)
    start = block.index("[")
    return re.findall(r'"([^"]+)"', block[start:])


def test_package_data_covers_every_file_under_web() -> None:
    """★ 前端每一个文件都要被 package-data 的某条模式匹配到。

    这条断言就是为上面那个缺陷写的：`web/*` 看着像"整个 web 目录"，
    实际只覆盖一层。漏掉的那部分在作者机器上永远发现不了。
    """
    patterns = _package_data_patterns()
    missing = []
    for path in sorted(WEB.rglob("*")):
        if not path.is_file():
            continue
        relative = "web/" + path.relative_to(WEB).as_posix()
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            missing.append(relative)
    assert not missing, f"这些前端文件不会进 wheel：{missing}（当前模式：{patterns}）"


def test_package_data_covers_the_migrations() -> None:
    patterns = _package_data_patterns()
    migrations = sorted((BACKEND / "db" / "migrations").glob("*.sql"))
    assert migrations, "找不到迁移文件"
    for path in migrations:
        relative = f"db/migrations/{path.name}"
        assert any(fnmatch.fnmatch(relative, p) for p in patterns), \
            f"迁移 {path.name} 不会进 wheel"


@pytest.mark.parametrize("name", ["app.js", "search.js", "people.js", "shared.js",
                                  "collections.js", "review.js", "help.js",
                                  "pick_modal.js"])
def test_frontend_never_hardcodes_the_authors_machine(name: str) -> None:
    """★ 前端里不许出现作者的机器路径。

    `C:\\PhotoOS\\venv\\venv-eg2\\...` 这种字符串在别人机器上**一定**是错的：
    界面上显示的"请到命令行执行"会指向一个不存在的解释器。
    正确做法是问服务端（`GET /api/cli`）。
    """
    text = (WEB / "js" / name).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(("*", "//", "/*")))
    for bad in ("venv", "venv-eg2", "C:\\PhotoOS", "C:/PhotoOS"):
        assert bad not in code, f"{name} 里写死了作者机器的路径：{bad}"


def test_frontend_has_no_hardcoded_drive_paths_at_all() -> None:
    """再宽一点：前端不该把盘符路径当**数据**用。

    允许的唯一情形是输入框的 `placeholder`（"例如 C:\\Photos" 这种示例文案，
    用户要照着填）；真正会被执行/发送的路径一律由服务端给。
    """
    offenders: list[str] = []
    for path in sorted((WEB / "js").glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("*", "//", "/*")):
                continue
            if "placeholder=" in line:
                continue
            for match in re.finditer(r"[A-Za-z]:\\\\[^\"'`\s]+", line):
                offenders.append(f"{path.name}:{number}: {match.group(0)[:40]}")
    assert not offenders, f"前端里出现了盘符路径：{offenders}"


def test_install_script_ships_with_the_package() -> None:
    """交付目录里必须有安装脚本与说明，而且脚本要能被 Windows PowerShell 读懂。

    ⚠️ **必须是 UTF-8 with BOM**：Windows PowerShell 5.1 读没有 BOM 的脚本时
    按系统 ANSI 解码，脚本里那些中文会变成乱码（甚至把语法读坏）。
    """
    script = PACKAGING / "install.ps1"
    note = PACKAGING / "安装说明.md"
    assert script.is_file(), f"缺少安装脚本：{script}"
    assert note.is_file(), f"缺少安装说明：{note}"
    raw = script.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1 必须带 UTF-8 BOM（否则 5.1 下中文乱码）"
    text = raw.decode("utf-8-sig")
    assert "photoos init" in text and "pip install" in text
    # 脚本里不许出现**作者机器的**绝对路径（它会带着 wheel 一起发出去）。
    # 文档示例里出现别的盘符（`-DataRoot C:\PhotoOSData`）是允许的 —— 那是用户自己填的。
    assert "venv" not in text, "install.ps1 里出现了作者机器上的目录"
    assert "C:\\PhotoOS\\" not in text, "install.ps1 里写死了作者的项目路径"


def test_install_notes_answer_the_questions_a_recipient_actually_asks() -> None:
    """说明里必须回答：要不要 Python、装完做什么、照片会不会被动、怎么卸载。"""
    text = (PACKAGING / "安装说明.md").read_text(encoding="utf-8")
    for keyword in ("Python 3.12", "添加库", "不会", "卸载", "模型"):
        assert keyword in text, f"安装说明里缺了「{keyword}」这一段"
    assert "1,455 MB" in text or "1455 MB" in text, "模型体积要写清楚（下载前得知道多大）"


def test_packaging_script_lives_outside_the_delivery_folder() -> None:
    """★ 打包脚本不能放在它自己会清空的目录里。

    第一版 `打包.ps1` 就放在了 `_dist_package\\`（交付目录）里，而它的第一步是
    `Remove-Item $OutDir -Recurse -Force` —— 等于每次打包先把自己删掉。
    现在脚本在 `packaging\\`，并且**显式拒绝**把输出目录设在自己所在的目录下。
    """
    script = PACKAGING / "打包.ps1"
    assert script.is_file(), f"缺少打包脚本：{script}"
    text = script.read_text(encoding="utf-8-sig")
    assert "Remove-Item" in text
    assert "输出目录不能放在 packaging" in text, "缺少「不许清空自己所在目录」的守卫"
    assert "_dist_package" in text, "默认输出目录应当写明"