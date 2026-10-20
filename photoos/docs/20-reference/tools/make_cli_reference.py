#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate docs/20-reference/41-CLI命令参考.md from the captured CLI --help text.

Input : _probe/docwork/cli-help.txt   (produced by running `photoos <cmd> --help`
                                        for all 29 top-level commands + subcommands)
Output: photoos/docs/20-reference/41-CLI命令参考.md

Why generated: the argument list of 29 commands is exactly the kind of thing that
rots silently in handwritten docs. This way the reference is a *transcription* of
the tool's own --help, and re-running it is the update procedure.

Usage:
    python docs/20-reference/tools/make_cli_reference.py
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1]
REPO = DOCS.parents[2]
SRC = REPO / "_probe" / "docwork" / "cli-help.txt"
#: `photoos --help` 的原文（含顶层表的一句话说明），由采集脚本一并落盘
TOPLEVEL_HELP = REPO / "_probe" / "docwork" / "cli-toplevel-help.txt"
OUT = DOCS / "41-CLI命令参考.md"

#: 按用途分组（手工维护；命令名必须与 --help 的顶层命令一致）
GROUPS: list[tuple[str, str, list[str]]] = [
    ("安装与自检", "第一次上手机器时按顺序跑", ["init", "doctor", "config", "version"]),
    ("库与卷", "登记照片库、换盘符后重绑", ["library"]),
    ("索引（读原片）", "建立/更新索引；这些是唯一会大量读原片的命令", [
        "scan", "hash", "fused", "preview", "index", "repair", "duplicates", "stats", "crosscheck",
    ]),
    ("导入（写照片库）", "把 SD 卡/暂存目录的照片复制进库；**默认只预演**", ["import", "geo"]),
    ("分组", "时段 / 拍摄组 / 场景", ["sessions", "shots", "scenes"]),
    ("AI", "向量与人脸（需要 AI 解释器，见 50 号文档）", ["ai", "embed", "search", "find", "people"]),
    ("服务与任务", "Web 界面与后台任务", ["serve", "jobs"]),
    ("数据安全", "备份、迁移、完整性", ["backup", "migrate", "integrity"]),
    ("断点测试", "PRD 验收断点，需要真实照片库", ["breakpoint"]),
]


def parse_toplevel_descriptions(help_text: str) -> dict[str, str]:
    """从 `photoos --help` 的顶层表里取「命令 → 一句话」。

    顶层表形如（缩进 4 空格）：
        init        初始化数据目录与数据库（可重复执行，幂等）
    """
    out: dict[str, str] = {}
    for line in help_text.splitlines():
        if not line.startswith("    ") or not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith(("<", "-")):
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def parse(path: Path) -> dict[str, dict]:
    """把采集到的 --help 文本切成 {命令: {'usage':..., 'sections':{标题:[行]}}}"""
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^### photoos (\S+)$", text, flags=re.M)
    # blocks[0] 是前导空段，之后交替：命令名, 内容
    cmds: dict[str, dict] = {}
    for i in range(1, len(blocks), 2):
        name, body = blocks[i], blocks[i + 1]
        usage = ""
        sections: dict[str, list[str]] = {}
        current = None
        for line in body.splitlines():
            if line.startswith("usage: "):
                usage = line[len("usage: "):].strip()
            elif line.strip() in ("positional arguments:", "options:"):
                current = line.strip()
                sections[current] = []
            elif line.startswith("--- subcommands ---"):
                break
            elif current and line.strip():
                sections[current].append(line.rstrip())
        cmds[name] = {"usage": usage, "sections": sections}
    return cmds


def parse_subs(path: Path) -> dict[str, list[tuple[str, str]]]:
    """取每个命令的子命令清单 [(子名, 说明)]"""
    text = path.read_text(encoding="utf-8")
    subs: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(
        r"^### photoos (\S+)\n(.*?)(?=^### photoos |\Z)", text, flags=re.M | re.S
    ):
        name, body = m.group(1), m.group(2)
        found: list[tuple[str, str]] = []
        in_pos = False
        for line in body.splitlines():
            if line.strip() == "positional arguments:":
                in_pos = True
                continue
            if line.strip() == "options:":
                in_pos = False
                continue
            if line.startswith("--- subcommands ---"):
                break
            if in_pos and re.match(r"^\s{4}\S", line):
                parts = line.strip().split(None, 1)
                if parts[0] != "<子命令>":
                    found.append((parts[0], parts[1] if len(parts) > 1 else ""))
        if found:
            subs[name] = found
    return subs


def code_block(usage: str) -> str:
    return f"```\n{usage}\n```" if usage else ""


def main() -> int:
    cmds = parse(SRC)
    subs = parse_subs(SRC)
    lines: list[str] = []
    add = lines.append

    add("# 41 · CLI 命令参考")
    add("")
    add("> **本篇由脚本生成，不要手改** —— 它是 `photoos <命令> --help` 的转写。")
    add("> 重新生成（命令有变动时）：")
    add("> `python docs/20-reference/tools/make_cli_reference.py`")
    add(">")
    add(f"> 【实测】顶层命令 **{len(cmds)} 个**（`photoos --help` 实测），")
    add("> 参数列表来自每个命令的 `--help` 原文。")
    add("")
    add("---")
    add("")
    add("## 0. 三条纪律（先读）")
    add("")
    add("1. **默认只预演**：`import` 这类会写数据的命令**必须显式加开关**才落盘")
    add("   （`import` 要 `--apply`）。这是项目刻意的安全设计，不是啰嗦。")
    add("2. **导入是唯一写入照片库的能力**：其余命令碰原片只读")
    add("   （见 `70-安全边界与硬规则.md`）。")
    add("3. **AI 命令可能跑在另一个解释器上**：`embed`/`search`/`people` 需要")
    add("   `torch`/`onnxruntime`/`cv2`，而产品 venv 故意不装（见 `50-AI子系统.md`）。")
    add("   命令行直跑时，请用配置里 `ai_python` 指向的解释器。")
    add("")
    add("---")
    add("")
    add("## 1. 命令总表")
    add("")
    add("| 分组 | 命令 | 一句话 |")
    add("| --- | --- | --- |")
    # 一句话取自 `photoos --help` 顶层表；缺失的（子命令型命令）补成"见下节"
    briefs = parse_toplevel_descriptions(TOPLEVEL_HELP.read_text(encoding="utf-8")) \
        if TOPLEVEL_HELP.exists() else {}
    for group, _desc, names in GROUPS:
        for n in names:
            if n not in cmds:
                continue
            brief = briefs.get(n, "（见下节）")
            add(f"| {group} | [`{n}`](#{n}) | {brief} |")
    # 未分组的命令兜底
    grouped = {n for _g, _d, names in GROUPS for n in names}
    missing = [n for n in cmds if n not in grouped]
    for n in missing:
        add(f"| （未分组） | [`{n}`](#{n}) | {briefs.get(n, '')} |")
    add("")
    add("> 【实测】完整列表：" + "、".join(f"`{n}`" for n in cmds) + "。")
    add("")
    add("---")
    add("")
    add("## 2. 逐命令参数")
    add("")
    for group, desc, names in GROUPS:
        present = [n for n in names if n in cmds]
        if not present:
            continue
        add(f"### {group}")
        add("")
        add(f"*{desc}*")
        add("")
        for n in present:
            add(f'<a id="{n}"></a>')
            add("")
            add(f"#### `photoos {n}`")
            add("")
            add(code_block(cmds[n]["usage"]))
            add("")
            body = cmds[n]["sections"]
            opts = body.get("options:", [])
            opts = [o for o in opts if "--help" not in o]
            if opts:
                for o in opts:
                    add(f"* {o.strip()}")
                add("")
            if n in subs:
                add("子命令：")
                add("")
                for sname, sdesc in subs[n]:
                    add(f"* `{n} {sname}` — {sdesc}")
                add("")
        add("---")
        add("")
    if missing:
        add("### 未分组（脚本未归类，请补进 `GROUPS`）")
        add("")
        for n in missing:
            add(f'<a id="{n}"></a>')
            add("")
            add(f"#### `photoos {n}`")
            add("")
            add(code_block(cmds[n]["usage"]))
            add("")
        add("---")
        add("")
    add("返回 [`00-文档规范与索引.md`](00-文档规范与索引.md) · "
        "相关：[`31-任务引擎与流水线.md`](31-任务引擎与流水线.md)（界面能触发的子集）")
    add("")

    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    print(f"commands: {len(cmds)}, with subcommands: {len(subs)}, lines: {len(lines)}")
    if missing:
        print(f"WARNING ungrouped commands: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
