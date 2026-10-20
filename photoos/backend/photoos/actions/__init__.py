"""白名单命令目录与执行。

界面只能提交一个 **action id**，命令模板与参数拼接全在服务端 —— 见 `catalog.py`
顶部的安全边界说明。执行入口是 `jobs.runner.JobRunner._run_cli_action`。
"""

from .catalog import (
    BY_ID,
    CATALOG,
    CliAction,
    catalog_payload,
    describe_command,
    environment_report,
    group_actions,
    interpreter,
    resolve_argv,
    validate_query,
)

__all__ = [
    "BY_ID",
    "CATALOG",
    "CliAction",
    "catalog_payload",
    "describe_command",
    "environment_report",
    "group_actions",
    "interpreter",
    "resolve_argv",
    "validate_query",
]
