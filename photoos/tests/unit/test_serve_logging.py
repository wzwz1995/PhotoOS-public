"""`photoos serve --log-file`：**日志去哪儿**的契约。

## 为什么值得为它写测试

这个参数不是"顺手加的"：uvicorn 默认把**每一个请求**都打到 stdout，
一个人一天的浏览就能写出一兆多行。把它启动在"会被回收的后台"里
（任务包装器、终端复用器、CI），这些输出会把那一侧的缓冲撑爆，
**连带把服务一起收掉** —— 2026-10-15 真的发生过：服务本体没崩，
用户却打不开界面，日志里只有一句 `[Errno 10048]`（那是第二次启动撞端口）。

所以判据要能回答两件事：
  1. 不带值时，日志**落在数据目录的 `logs/serve.log`**（不是随手丢在工作目录）；
  2. 带值时，写的就是那个路径，而且**控制台不再逐请求刷屏**
     （否则"换了个地方写日志、控制台照样刷"就白加了）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from photoos import cli
from photoos.config import Settings, use_config_path
from photoos.db import Database, apply_migrations


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    with use_config_path(None):
        built = Settings(data_root=tmp_path / "data")
        built.ensure_dirs()
        apply_migrations(Database(built.db_path), backup_dir=built.backups_dir)
        yield built


def _run_serve(monkeypatch, settings: Settings, *argv):
    """跑 `cmd_serve`，但把 `uvicorn.run` 换成"记下参数就返回"。"""
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)
        captured["app"] = app

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(cli, "_open_db", lambda: (settings, Database(settings.db_path)))
    args = cli.build_parser().parse_args(["serve", *[str(a) for a in argv]])
    code = args.func(args)
    return code, captured


def test_bare_flag_writes_into_the_data_directory(settings: Settings, monkeypatch) -> None:
    """`--log-file`（不带值）→ 数据目录的 `logs/serve.log`。"""
    code, captured = _run_serve(monkeypatch, settings, "--log-file")
    assert code == 0
    expected = settings.logs_dir / "serve.log"
    assert expected.is_file(), f"没有在 {expected} 建出日志文件"
    # `log_config=None` 是关键：不这么写，uvicorn 会再套一层自己的配置，
    # 刚挂上的文件 handler 会被覆盖，日志照样全打到控制台。
    assert captured.get("log_config") is None, "没有告诉 uvicorn 别覆盖我们的 logging 配置"


def test_explicit_path_is_honoured(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "somewhere" / "serve.log"
    code, _ = _run_serve(monkeypatch, settings, "--log-file", target)
    assert code == 0
    assert target.is_file(), "给了路径却没写在那儿"


def test_without_the_flag_logging_stays_on_the_console(settings: Settings, monkeypatch) -> None:
    """不带参数时行为**不变**（回到 uvicorn 默认）—— 不能悄悄改掉老用法。"""
    code, captured = _run_serve(monkeypatch, settings)
    assert code == 0
    assert "log_config" not in captured, "不带 --log-file 时不该动 uvicorn 的日志配置"
    assert not (settings.logs_dir / "serve.log").exists()


def test_the_launcher_generated_by_the_installer_uses_it() -> None:
    """安装脚本生成的启动器要带这个参数 —— 否则收件人第一天就撞上刷屏。"""
    text = (Path(__file__).resolve().parents[2] / "packaging" / "install.ps1") \
        .read_text(encoding="utf-8-sig")
    assert "photoos serve --log-file" in text, "启动器没有带 --log-file"
