#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PhotoOS 技术参考文档 · 只读复算脚本

用途
----
一次跑出 `docs/20-reference/00-文档规范与索引.md` §5「快照基线」里的全部数字，
供人复核、也供机器在文档与代码漂移时对账。

硬约束（本脚本按项目铁律写成，改动时不要破坏）
--------------------------------------------
1. **只读**：数据库一律用 `file:...?mode=ro` 打开；绝不 INSERT/UPDATE/DELETE/DDL。
2. **不碰原片**：只统计 `data/` 下的派生数据，从不遍历照片盘（E: 常离线）。
3. **不写任何文件**：结果只打到 stdout。
4. **网络只打本机**：仅 GET `127.0.0.1:8760` 的两个只读端点（/api/health、/openapi.json），
   服务不在跑就跳过并标注，不算失败。

退出码
------
0 = 全部读到；1 = 有项目读不到（例如服务未启动、DB 不存在），细节见输出。

对应关系
--------
输出每一行的 `key=` 与 00 号文档 §5 的表格行一一对应：

    pkg_version / py_requires   -> pyproject.toml:6-7,10
    schema_user_version         -> 00 §5「schema 版本」
    live_objects                -> 00 §5「活库对象」（表/索引/触发器/视图）
    db_bytes                    -> 00 §5「活库大小」
    assets_photo / assets_video -> 00 §5「活库资产规模」
    derived_thumbnails / derived_previews / models_ai_cache -> 00 §5「派生数据」「模型缓存」
    backend_lines / tests_lines -> 00 §5「后端 Python」「测试」
    frontend_js_lines / frontend_css_lines -> 00 §5「前端 JS」「前端 CSS」
    cli_commands                -> 00 §5「CLI 顶层命令」
    git_head / git_tracked      -> 00 §5「git HEAD」「跟踪文件总数」
    api_paths                   -> 00 §5「服务」/ 40 号文档接口表的条数
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 路径定位

REPO_ROOT = Path(__file__).resolve().parents[3]        # D:\Photoos
PKG_ROOT = REPO_ROOT / "photoos"                        # 代码仓库
DATA_ROOT = REPO_ROOT / "data"                          # 数据根（可整体搬迁）
BACKEND = PKG_ROOT / "backend"
WEB = BACKEND / "photoos" / "web"
DB_PATH = DATA_ROOT / "photoos.db"
BASE_URL = "http://127.0.0.1:8760"

problems: list[str] = []


def say(key: str, value: Any, note: str = "") -> None:
    tail = f"   # {note}" if note else ""
    print(f"{key} = {value}{tail}")


def warn(msg: str) -> None:
    problems.append(msg)
    print(f"  [skip] {msg}")


# ---------------------------------------------------------------- 只读 DB


def ro_connection() -> sqlite3.Connection:
    """唯一的数据库入口：只读 URI，物理上写不进去。"""
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def check_database() -> None:
    if not DB_PATH.exists():
        warn(f"数据库不存在：{DB_PATH}")
        return
    say("db_path", DB_PATH)
    say("db_bytes", DB_PATH.stat().st_size, "214.15 MB 量级")
    con = ro_connection()
    try:
        say("schema_user_version", con.execute("PRAGMA user_version").fetchone()[0], "期望 9")
        rows = con.execute(
            "SELECT type, COUNT(*) FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' GROUP BY type ORDER BY type"
        ).fetchall()
        counts = {t: n for t, n in rows}
        say("live_objects", json.dumps(counts, ensure_ascii=False),
            "期望 31 表 / 53 索引 / 1 触发器 / 0 视图")
        say("integrity_check", con.execute("PRAGMA integrity_check").fetchone()[0], "期望 ok")
        say("foreign_key_violations", len(con.execute("PRAGMA foreign_key_check").fetchall()), "期望 0")
    finally:
        con.close()


def count(table: str) -> int | None:
    con = ro_connection()
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def check_counts() -> None:
    if not DB_PATH.exists():
        return
    con = ro_connection()
    try:
        # 注意：asset 表自己带 library_id（asset_instance 在本库里是空的，
        # 【实测】COUNT(*) = 0，所以按 asset.library_id 统计，不要走 instance 表）。
        libs = con.execute("SELECT id, name, status, root_path_hint FROM library ORDER BY id").fetchall()
        for lib_id, name, status, hint in libs:
            n = con.execute("SELECT COUNT(*) FROM asset WHERE library_id = ?", (lib_id,)).fetchone()[0]
            say(f"assets_library{lib_id}", f"{n} ({name}, {status}, {hint})")
        for table in ("asset", "shot", "scene", "session", "embedding", "face", "person",
                      "selection", "collection", "collection_item"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            say(f"rows_{table}", n)
    finally:
        con.close()


# ---------------------------------------------------------------- 目录/代码规模


def dir_stats(path: Path) -> tuple[int, int]:
    files = 0
    total = 0
    if not path.exists():
        return (0, 0)
    for root, _dirs, names in os.walk(path):
        for n in names:
            fp = Path(root) / n
            try:
                total += fp.stat().st_size
                files += 1
            except OSError:
                pass
    return (files, total)


def check_derived() -> None:
    for key, sub in (("derived_thumbnails", "thumbnails"),
                     ("derived_previews", "previews"),
                     ("models_ai_cache", "ai_cache")):
        files, total = dir_stats(DATA_ROOT / sub)
        say(key, f"files={files} bytes={total} ({total / 1024 / 1024:.1f} MB)")


def line_count(path: Path) -> int:
    """按真实换行符计数（本仓库换行不统一，不能用平台默认按行读）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    n = data.count(b"\n")
    return n if data.endswith(b"\n") else n + 1


def tracked_files(*patterns: str) -> list[Path]:
    """git ls-files -z：NUL 分隔，避免本机 GBK 控制台把 UTF-8 路径读崩。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", *patterns],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        warn(f"git ls-files 失败：{exc}")
        return []
    names = proc.stdout.decode("utf-8", errors="replace").split("\0")
    return [REPO_ROOT / p for p in names if p.strip()]


def check_code_scale() -> None:
    all_files = tracked_files()
    # 用相对仓库根的 POSIX 路径做前缀匹配（Path.as_posix() 带绝对前缀，直接匹配会全都落空）
    rel = [(p.relative_to(REPO_ROOT).as_posix(), p) for p in all_files]
    for key, prefix, ext in (
        ("backend_lines", "photoos/backend/", ".py"),
        ("tests_lines", "photoos/tests/", ".py"),
        ("frontend_js_lines", "photoos/backend/photoos/web/js/", ".js"),
        ("frontend_css_lines", "photoos/backend/photoos/web/css/", ".css"),
    ):
        files = [p for name, p in rel
                 if name.startswith(prefix) and name.endswith(ext)
                 and "__pycache__" not in name]
        total = sum(line_count(p) for p in files)
        say(key, f"files={len(files)} lines={total}")
    say("git_tracked", len(all_files), "期望 339")


def check_git() -> None:
    try:
        head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
        say("git_head", f"{head} (branch {branch})")
        say("git_worktree_clean", "no" if dirty else "yes",
            "本目录只读复算应保持干净；文档本身会显示为 modified")
    except (OSError, subprocess.CalledProcessError) as exc:
        warn(f"git 查询失败：{exc}")


def check_pyproject() -> None:
    pyproject = PKG_ROOT / "pyproject.toml"
    if not pyproject.exists():
        warn(f"缺少 {pyproject}")
        return
    version = requires = "?"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            version = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("requires-python"):
            requires = line.split("=", 1)[1].strip().strip('"')
    say("pkg_version", version)
    say("py_requires", f"{requires} (running {sys.version.split()[0]})")


def check_cli() -> None:
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    try:
        out = subprocess.run([str(py), "-m", "photoos", "--help"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", cwd=str(PKG_ROOT), check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        warn(f"photoos --help 失败：{exc}")
        return
    # 顶层命令表：缩进 4 空格；首行是 positional arguments 的表头「<命令>」，按 < 排除。
    # ⚠️ 必须显式 encoding="utf-8"：默认走系统 ANSI 代码页（本机 GBK）会把中文说明读坏，
    #    导致个别命令行不满足 startswith("    ") 而被漏数（实测漏过 1 个，29 vs 实际 30）。
    names = [ln.split()[0] for ln in out.splitlines()
             if ln.startswith("    ") and ln.strip() and not ln.strip().startswith(("<", "-"))]
    say("cli_commands", len(names), ", ".join(names))


def check_api() -> None:
    """只 GET，不改任何状态。"""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        say("api_health", json.dumps(health, ensure_ascii=False))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        warn(f"{BASE_URL} 上的服务没在跑（{exc}）—— 这不是文档错误，跳过接口核对")
        return
    try:
        with urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=10) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
        paths = spec.get("paths", {})
        methods = sum(len([m for m in ops if m in
                           ("get", "post", "put", "patch", "delete", "head", "options")])
                      for ops in paths.values())
        say("api_paths", f"paths={len(paths)} operations={methods}")
        read_only = sum(1 for ops in paths.values() for m in ops if m == "get")
        say("api_read_only_get", read_only)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        warn(f"/openapi.json 读不到：{exc}")


def main() -> int:
    print("=" * 72)
    print("PhotoOS 技术参考文档 · 只读复算（docs/20-reference/verify_docs.py）")
    print("=" * 72)
    print(f"repo_root  = {REPO_ROOT}")
    print(f"data_root  = {DATA_ROOT}")
    print("-" * 72)
    check_pyproject()
    check_git()
    check_database()
    check_counts()
    check_derived()
    check_code_scale()
    check_cli()
    check_api()
    print("-" * 72)
    if problems:
        print(f"有 {len(problems)} 项没读到（多为环境原因，不等于文档写错）：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全部读到。请把上面的值与 00-文档规范与索引.md §5 逐格对照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
