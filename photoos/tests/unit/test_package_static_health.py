"""静态健全性检查：越界相对导入。

## 为什么需要这个测试

`cli.py` 里曾经有三处 `from ..repos import ShotRepo` —— 而 `cli.py` 本身就在包顶层
（`photoos/cli.py`），`..` 已经越过顶层包，运行到那一行就 `ImportError:
attempted relative import beyond top-level package`。

**它是怎么被发现的**：在真实库快照上做隔离演练时，`shots regroup --apply` 一跑就炸。
单元测试**完全覆盖不到** —— 因为那是个函数体内的延迟导入，只有真的执行到那一行才会触发，
而当时的测试都是直接调 `ShotRepo`，没有走 CLI 的 `--apply` 分支。

这类错误是**纯静态的**：模块在包里的深度决定了最多能上跳几层。
因此最合适的防线是一条静态检查，而不是"等它跑起来再说"。
"""

from __future__ import annotations

import ast
import pathlib

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "backend" / "photoos"


def _module_depth(path: pathlib.Path) -> int:
    """模块在包里的深度：`photoos/cli.py` → 0；`photoos/api/app.py` → 1。"""
    return len(path.relative_to(PACKAGE_ROOT).parts) - 1


def _relative_imports(tree: ast.AST):
    """产出 `(行号, 上跳层数, 模块名)`。`from .x` 上跳 0 层，`from ..x` 上跳 1 层。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            yield node.lineno, node.level - 1, node.module or ""


def test_no_relative_import_goes_beyond_the_package() -> None:
    """★ 任何模块的相对导入都不得越过顶层包。

    这条检查能拦住一整类"只在运行时才炸"的错误 ——
    而且它不需要真的执行到那一行。
    """
    offenders: list[str] = []
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert files, f"没找到任何模块：{PACKAGE_ROOT}"
    for path in files:
        depth = _module_depth(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, ups, module in _relative_imports(tree):
            if ups > depth:
                offenders.append(
                    f"{path.relative_to(PACKAGE_ROOT)}:{lineno} "
                    f"上跳 {ups} 层（模块深度只有 {depth}）：{'.' * (ups + 1)}{module}"
                )
    assert not offenders, "发现越界的相对导入：\n  " + "\n  ".join(offenders)


def test_cli_uses_single_dot_for_sibling_packages() -> None:
    """回归：`cli.py` 引用同级子包必须用 `.repos` 而不是 `..repos`（真实踩过的坑）。"""
    source = (PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8")
    assert "from ..repos" not in source
    assert "from .repos" in source


def test_every_module_in_the_package_imports_cleanly() -> None:
    """所有模块都能被导入 —— 顺带抓语法错误与顶层导入问题。

    （函数体内的延迟导入由上面那条静态检查守；这条守的是模块级导入。）
    """
    import importlib

    failures: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT.parent)
        name = ".".join(relative.with_suffix("").parts)
        if name.endswith(".__main__"):
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "以下模块无法导入：\n  " + "\n  ".join(failures)
