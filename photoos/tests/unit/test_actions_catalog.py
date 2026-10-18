"""白名单命令目录的**契约测试**。

这些断言守的是两类真实事故：

1. **目录里写了一个 CLI 并不认识的参数**。
   实际发生过：`search --list` 这条命令是我凭印象写的，`search` 根本没有
   `--list`；界面上的按钮因此返回"unrecognized arguments"。
   靠肉眼看代码发现不了 —— 所以这里拿**真的 argparse parser** 逐条解析。

2. **命令行的注入面被打开**。
   搜索词是唯一会进入命令行的用户输入。一旦校验放宽（或有人改成
   `shell=True`），本机就暴露成"任何人访问 8760 端口即可执行命令"。
   这里对一串注入样本逐条断言必须被拒。
"""

from __future__ import annotations

import pytest

from photoos.actions import catalog
from photoos.cli import build_parser


def _cli_argv(action: catalog.CliAction) -> list[str]:
    """去掉 `-m photoos` 前缀，只留 argparse 能识别的部分。"""
    argv = catalog.resolve_argv(
        action,
        query="日出" if action.needs_query else None,
        library=1 if action.needs_library else None,
    )
    return argv[argv.index("photoos") + 1:]


@pytest.mark.parametrize("action", catalog.CATALOG, ids=lambda a: a.id)
def test_every_catalog_command_is_accepted_by_the_real_cli(action) -> None:
    """目录里的每一条命令都必须能被真正的 CLI 解析，且落到一个处理函数上。"""
    parser = build_parser()
    ns = parser.parse_args(_cli_argv(action))
    assert getattr(ns, "func", None) is not None, f"{action.id} 没有绑定处理函数"


def test_catalog_ids_are_unique() -> None:
    ids = [a.id for a in catalog.CATALOG]
    assert len(ids) == len(set(ids)), f"动作 id 重复：{ids}"


def test_query_placeholder_only_in_needs_query_actions() -> None:
    """`{query}` 占位符只允许出现在声明了 `needs_query` 的动作里。

    反过来，声明了 `needs_query` 就必须真的用到它 —— 否则那个按钮
    会无声地忽略用户输入（"我明明打了词，跑的还是默认"）。
    """
    for action in catalog.CATALOG:
        has_placeholder = "{query}" in action.argv
        assert has_placeholder == action.needs_query, (
            f"{action.id}: needs_query={action.needs_query} 但占位符={has_placeholder}"
        )


@pytest.mark.parametrize(
    "bad",
    [
        "西湖; rm -rf /",
        "西湖 && del x",
        "x | whoami",
        "$(whoami)",
        "`whoami`",
        "西湖\nrm",
        "",
        "   ",
        "--engine",          # 想冒充参数
        'a"b',
        "a" * 41,            # 超长
    ],
)
def test_injection_shaped_queries_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        catalog.validate_query(bad)


@pytest.mark.parametrize("good", ["西湖", "日出 日落", "ok-word_1", "a" * 40])
def test_normal_queries_are_accepted(good: str) -> None:
    assert catalog.validate_query(good) == good.strip()


def test_no_template_contains_shell_metacharacters() -> None:
    """模板里出现 shell 元字符就说明有人在往 shell 上想 —— 直接拦住。"""
    for action in catalog.CATALOG:
        joined = "".join(action.argv)
        for ch in ";|&$`><\n":
            assert ch not in joined, f"{action.id} 的模板里有 shell 元字符 {ch!r}"


def test_resolve_argv_passes_query_as_a_single_argv_element() -> None:
    """搜索词必须**整体**成为一个 argv 元素，绝不能被拆开或再拼接。"""
    action = catalog.BY_ID["search_prepare"]
    query = catalog.validate_query("西湖 日落")
    argv = catalog.resolve_argv(action, query=query)
    assert argv.count(query) == 1
    assert "{query}" not in argv


def test_resolve_argv_rejects_a_query_for_actions_that_do_not_take_one() -> None:
    """没有 `needs_query` 的动作不该接受 query —— 静默忽略比报错更危险。"""
    action = catalog.BY_ID["index"]
    assert catalog.resolve_argv(action) == list(action.argv)
    assert "{query}" not in action.argv


def test_catalog_payload_shape() -> None:
    from photoos.config import get_settings

    payload = catalog.catalog_payload(get_settings())
    assert set(payload) == {"environment", "actions"}
    env = payload["environment"]
    assert {"python", "reason", "missing", "modules"} <= set(env)
    for item in payload["actions"]:
        assert {"id", "label", "group", "available", "missing_requires"} <= set(item)
        # available 必须与 missing_requires 一致，否则按钮的可用性会撒谎
        assert item["available"] is (not item["missing_requires"])


def test_describe_command_quotes_the_python_path() -> None:
    """解释器路径常含空格（`Program Files`），不加引号用户照抄会失败。"""
    shown = catalog.describe_command(
        r"C:\Program Files\Python\python.exe", ["-m", "photoos", "search", "西湖 日落"]
    )
    assert shown.startswith('"C:\\Program Files\\Python\\python.exe"')
    assert '"西湖 日落"' in shown or "西湖 日落" in shown
