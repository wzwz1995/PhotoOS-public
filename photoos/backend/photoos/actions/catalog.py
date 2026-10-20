"""界面可直接执行的**白名单命令目录**。

## 为什么需要它

在它之前，界面上有六处"这件事我算不了，请你去命令行跑一次"：准备搜索词、
算 CLIP/EG2 向量、下载模型、人脸 detect/embed/discover。每一处都得让用户
复制一条命令、切到终端、粘进去、再回浏览器刷新 —— 功能在，但路是断的。

## 安全边界（**这是本模块存在的全部理由**）

浏览器**只能提交一个 action id**，永远不能提交命令字符串。命令模板、
参数拼接、以及用哪个解释器执行，全部由服务端决定。
所以即便有人能访问 `http://127.0.0.1:8760`，他能做的也只有"触发这几条
预先定义好的命令"，而不是"在这台机器上执行任意命令"。

`query` 类参数（搜索词）是唯一进入命令行的用户输入，它走
`_validate_query()` 白名单字符校验，并且**始终作为单独一个 argv 元素传递**
（不经 shell），因此 `;` `&` `|` 之类没有注入面。

## 为什么模板里带 `-m photoos`

不用 `photoos.exe` 那个 console script：它是不是存在取决于这个 venv 是怎么装的
（可编辑安装 / wheel / 便携版各不一样），而 `-m photoos` 只要包能 import 就一定能跑。
解释器本身则通过 `sys.executable` 或配置里的 `ai_python` 指定。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: 搜索词允许的字符。中文、字母数字、空格与少量标点。
#: **故意收得很紧**：这个值是唯一会进入命令行的用户输入。
_QUERY_OK = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-·.,、，。/]{0,39}$")

#: 单个任务保留的输出行数上限（内存与 DB note 都不该被一条长命令撑爆）
MAX_OUTPUT_LINES = 120


@dataclass(frozen=True)
class CliAction:
    """一条可执行的命令。`id` 是浏览器唯一能提交的东西。"""

    id: str
    label: str
    description: str
    group: str
    #: 固定参数模板。`{query}` 会被替换成校验过的搜索词，其余位置不含用户输入。
    argv: tuple[str, ...]
    #: 需要用户提供一个搜索词（只有 search 类动作需要）
    needs_query: bool = False
    #: 需要指出对哪个 Library 执行（多库时必须，否则 CLI 会拒绝）
    needs_library: bool = False
    #: 会下载大文件或跑很久 —— 界面上要提前说清楚
    long_running: bool = False
    #: 这条命令需要哪些可选依赖（用于预检提示）
    requires: tuple[str, ...] = ()
    #: 不是"跑一条命令"，而是**入队一个任务类型**（值就是任务类型名）。
    #:
    #: 为什么需要它：有些动作本质上是**多步**的，拆成三条命令就会得到三个任务、
    #: 三条进度、三份输出，而用户在界面上按的是一个按钮。
    #: `regroup` 就是这种：它要依次跑 sessions → shots → scenes 三步。
    #: 让这类动作直接映射到任务类型，任务系统里那套进度/暂停/取消才用得上。
    #:
    #: `argv` 对这类动作**不使用**（但仍是必填字段，填与任务等价的那条命令，
    #: 以便界面向用户展示"点下去会发生什么"）。
    enqueue_job: str | None = None


#: ------------------------------------------------------------------ 命令目录
#: 顺序 = 界面上的展示顺序，按"用户最可能想干什么"排。
CATALOG: tuple[CliAction, ...] = (
    # ------ 索引（不需要可选依赖，产品 venv 直接能跑） ------
    CliAction(
        id="index",
        label="索引新照片",
        description="扫描 → 元数据 → 缩略图 → 哈希，一次跑完。",
        group="索引",
        argv=("-m", "photoos", "index", "--wait"),
        long_running=True,
    ),
    CliAction(
        id="index_library",
        label="索引这个库",
        description="只扫描指定的 Library（多库时 CLI 必须带 --library，否则会拒绝执行）。",
        group="索引",
        argv=("-m", "photoos", "index", "--library", "{library}", "--wait"),
        needs_library=True,
        long_running=True,
    ),
    # ------ 搜索词（中文检索，需要 EmbeddingGemma） ------
    CliAction(
        id="search_prepare",
        label="准备这个搜索词",
        description="用 EmbeddingGemma 算一次文字向量并存进库，之后这个词就能直接搜。",
        group="找照片",
        argv=("-m", "photoos", "search", "{query}", "--save"),
        needs_query=True,
        requires=("torch", "sentence_transformers"),
    ),
    CliAction(
        id="stats",
        label="看库的总体情况",
        description="每个 Library 有多少张、多少城市、多少时段 —— 用来判断该不该重跑索引。",
        group="索引",
        argv=("-m", "photoos", "stats"),
    ),
    # ------ 向量 ------
    CliAction(
        id="embed_gemma",
        label="算中文检索向量",
        description="EmbeddingGemma 2 视觉塔，给全库照片算 768 维向量（中文搜索的基础）。",
        group="向量",
        argv=("-m", "photoos", "embed", "--engine", "gemma", "--apply"),
        long_running=True,
        requires=("torch", "sentence_transformers"),
    ),
    CliAction(
        id="embed_clip",
        label="算 CLIP 向量",
        description="CLIP 图像向量（Scene 划分与相似度用；本机**只下视觉塔**，没有文字编码器，所以不能拿它搜图）。",
        group="向量",
        argv=("-m", "photoos", "embed", "--engine", "clip", "--apply"),
        long_running=True,
        requires=("onnxruntime",),
    ),
    # ------ 人物 ------
    CliAction(
        id="people_detect",
        label="检测人脸",
        description="YuNet 找出全库照片里的人脸位置。",
        group="人物",
        argv=("-m", "photoos", "people", "detect", "--all"),
        long_running=True,
        requires=("cv2",),
    ),
    CliAction(
        id="people_embed",
        label="算人脸特征",
        description="SFace 把人脸裁片算成特征向量，用于「这是同一个人」。",
        group="人物",
        argv=("-m", "photoos", "people", "embed", "--all"),
        long_running=True,
        requires=("cv2",),
    ),
    CliAction(
        id="people_discover",
        label="聚类出新人物",
        description="把没人认领的脸聚成候选人物，等你去命名。",
        group="人物",
        argv=("-m", "photoos", "people", "discover"),
        requires=("cv2", "numpy"),
    ),
    # ------ 模型 ------
    CliAction(
        id="fetch_clip",
        label="下载 CLIP 模型",
        description="约 335 MB，一次下载长期有效。",
        group="模型",
        argv=("-m", "photoos", "ai", "fetch-model", "--engine", "clip"),
        long_running=True,
    ),
    CliAction(
        id="fetch_gemma",
        label="下载中文检索模型",
        description="EmbeddingGemma 2，约 1.45 GB —— 中文搜索的前置条件。",
        group="模型",
        argv=("-m", "photoos", "ai", "fetch-model", "--engine", "gemma"),
        long_running=True,
    ),
    CliAction(
        id="fetch_face",
        label="下载人脸模型",
        description="YuNet + SFace，约 37 MB。",
        group="模型",
        argv=("-m", "photoos", "ai", "fetch-model", "--engine", "face"),
    ),
    # ------ 分组（精选与「按时间/地点」找照片的前提） ------
    #
    # ⚠️ 这三步**顺序不能换**：`shots regroup` 落库前要求 `session` 行已回填，
    # 而 `scenes rebuild` 又建立在 Shot 之上。三步都幂等，可以反复跑。
    #
    # 界面上最常用的是下面这一条「现在分组」：它入队一个 **`regroup` 任务**
    # （一次跑完三步、一条进度、可暂停）。另外三条单步命令保留给
    # "只调了阈值、只想重切场景"这种精细操作。
    CliAction(
        id="regroup",
        label="现在分组",
        description="把库里的照片整理成可筛选的时段 / 拍摄组 / 场景。"
                    "导入之后跑一次，精选页与「按时间地点」找照片就有内容了。",
        group="分组",
        argv=("-m", "photoos", "shots", "regroup", "--apply"),
        long_running=True,
        enqueue_job="regroup",
    ),
    CliAction(
        id="sessions_backfill",
        label="只回填时段",
        description="把库内 <城市>\\<时段> 目录补成可筛选的时段行（不读任何照片文件）。",
        group="分组",
        argv=("-m", "photoos", "sessions", "backfill", "--apply"),
    ),
    CliAction(
        id="shots_regroup",
        label="只重算拍摄组",
        description="按拍摄时间把连拍与重复构图合成为「拍摄组」——精选与主题的决策单位。",
        group="分组",
        argv=("-m", "photoos", "shots", "regroup", "--apply"),
        long_running=True,
    ),
    CliAction(
        id="scenes_rebuild",
        label="只重算场景",
        description="把拍摄组切成场景（一次拍摄）。「按时间/地点」找照片以场景为单位。",
        group="分组",
        argv=("-m", "photoos", "scenes", "rebuild", "--apply"),
        long_running=True,
    ),
    # ------ 诊断 ------
    CliAction(
        id="ai_doctor",
        label="检查 AI 环境",
        description="真跑一次推理计时，回答「这台机器现在到底能不能算向量」。",
        group="诊断",
        argv=("-m", "photoos", "ai", "doctor"),
    ),
)

#: id → 动作。执行入口只认这张表，表外的 id 一律拒绝。
BY_ID: dict[str, CliAction] = {a.id: a for a in CATALOG}


def validate_query(raw: Any) -> str:
    """校验搜索词。不合法就抛 `ValueError`（由调用方转成 400）。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("搜索词不能为空")
    if not _QUERY_OK.match(text):
        raise ValueError(
            "搜索词只能包含中文、字母、数字、空格与常用标点（最长 40 字）。"
            "命令行的其它参数不接受界面输入。"
        )
    return text


def resolve_argv(action: CliAction, *, query: str | None = None,
                 library: int | None = None) -> list[str]:
    """把动作模板展开成真正的 argv。

    用户输入只可能出现在 **query（已过白名单）** 与 **library（已在接口层校验成整数）**
    这两个位置。
    """
    out: list[str] = []
    for token in action.argv:
        if token == "{query}":
            if not action.needs_query:
                raise ValueError(f"动作 {action.id} 不该出现 query 占位符")
            if query is None:
                raise ValueError("这个动作需要一个搜索词")
            out.append(query)
        elif token == "{library}":
            if not action.needs_library:
                raise ValueError(f"动作 {action.id} 不该出现 library 占位符")
            if library is None:
                raise ValueError("这个动作需要指定 Library")
            out.append(str(int(library)))
        else:
            out.append(token)
    if action.needs_query and query is None:
        raise ValueError("这个动作需要一个搜索词")
    if action.needs_library and library is None:
        raise ValueError("这个动作需要指定 Library")
    return out


#: `(python, module) -> bool` 的进程内缓存。
#:
#: ⚠️ 为什么必须有：探测外部解释器要**起一个 python 子进程**（`_probe_external`），
#: 一次约 40–60 ms。`environment_report()` 一次问 5 个模块 —— 于是
#: `/api/actions` 每调用一次（**每次打开「命令」弹窗或刷新维护页面板**）
#: 就要起 5 个子进程，实测 **220 ms**。
#:
#: 这是纯浪费：一个 venv 里装了什么，在服务进程活着的时间里不会变。
#: 缓存之后第一次仍然要探测（这是必要的诚实），之后就是 0 开销。
#:
#: 换 `ai_python` 需要重启服务才生效 —— 那本来也是这个配置项的既定语义
#: （它写在配置文件里，由 `serve` 启动时读取）。
_MODULE_PROBE_CACHE: dict[tuple[str, str], bool] = {}


def module_available(name: str, python: str | None = None) -> bool:
    """探测可选依赖是否可用。

    指定了 `python`（外部解释器）时只能**按同目录的 site-packages 猜**，
    所以那种情况下探测结果仅供参考 —— 真正的判据永远是命令自己的输出。
    这里返回 False 只是为了"提前说清楚"，不是"拦住不让跑"。
    """
    if python is None or Path(python).resolve() == Path(sys.executable).resolve():
        # 同进程：直接查，准确，且是内存里的查找
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False
    key = (str(Path(python).resolve()), name)
    if key not in _MODULE_PROBE_CACHE:
        _MODULE_PROBE_CACHE[key] = _probe_external(python, name)
    return _MODULE_PROBE_CACHE[key]


def _probe_external(python: str, name: str) -> bool:
    """用一个短命子进程问外部解释器"你有没有这个模块"。

    失败一律当作"不知道"→ 返回 True，让命令自己去报错：
    把一个探测失败误判成"缺依赖"会让功能无谓地变灰。
    """
    import subprocess

    try:
        proc = subprocess.run(
            [python, "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec({name!r}) else 1)"],
            capture_output=True, timeout=25,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败不等于没有
        return True


def _needs_ai_env(action: "CliAction | None") -> bool:
    """只有靠 torch 的动作用 ai_python；onnxruntime / cv2 那些装在**服务环境**里。

    踩过的坑：装了 opencv + 人脸模型（进的是包内 site-packages），但 people 命令
    被派到 ai-venv 去跑，于是报"未安装 opencv（cv2）"。
    """
    if action is None:
        return False
    return any(dep in {"torch", "sentence_transformers"} for dep in action.requires)


def interpreter(settings: Any, action: "CliAction | None" = None) -> tuple[str, str]:
    """决定用哪个解释器执行命令，并说明理由。

    返回 `(python, reason)`。`reason` 会原样显示给用户 —— 因为"命令到底在哪个
    环境里跑的"是排查一切 AI 问题的第一个问题（本机就踩过：服务跑在产品 venv 里，
    而 EG2 装在旁边那个 spike venv 里，于是页面能搜却不能算）。
    """
    if not _needs_ai_env(action):
        return sys.executable, "服务自身解释器（这个动作不需要 torch）"

    configured = getattr(settings, "ai_python", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if path.is_file():
            return str(path), "取自配置 ai_python"
        # ⚠️ 配置失效时**不要**直接用服务解释器：它没有 torch，AI 命令必失败
        #（实测：搜索准备词 2 秒退出码 2）。先找"一键安装"建的独立环境。
        try:
            from ..ai_install import ai_venv_dir

            candidate = ai_venv_dir() / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if candidate.is_file():
                return str(candidate), (
                    f"配置的 ai_python 不存在（{path}），已自动改用 AI 独立环境")
        except Exception:  # noqa: BLE001
            pass
        return sys.executable, f"配置的 ai_python 不存在（{path}），已回退到服务自身的解释器"
    return sys.executable, "服务自身的解释器（未配置 ai_python）"


def environment_report(settings: Any) -> dict[str, Any]:
    """给界面用的环境说明：用哪个解释器、缺哪些可选依赖。"""
    python, reason = interpreter(settings)
    modules = ("torch", "sentence_transformers", "onnxruntime", "cv2", "numpy")
    available = {m: module_available(m, python) for m in modules}
    return {
        "python": python,
        "reason": reason,
        "is_self": Path(python).resolve() == Path(sys.executable).resolve() if Path(python).exists() else True,
        "modules": available,
        "missing": [m for m, ok in available.items() if not ok],
        "platform": os.name,
    }


def catalog_payload(settings: Any) -> dict[str, Any]:
    """`/api/actions` 的完整响应。"""
    env = environment_report(settings)
    actions = []
    for action in CATALOG:
        # 缺依赖的动作**不隐藏**，只是标记出来 —— 藏起来用户永远不知道有这个能力，
        # 而"缺什么、怎么补"正是他此刻需要知道的信息。
        missing = [m for m in action.requires if not env["modules"].get(m, True)]
        actions.append({
            "id": action.id,
            "label": action.label,
            "description": action.description,
            "group": action.group,
            "needs_query": action.needs_query,
            "long_running": action.long_running,
            "enqueue_job": action.enqueue_job,
            "requires": list(action.requires),
            "missing_requires": missing,
            "available": not missing,
        })
    return {"environment": env, "actions": actions}


def group_actions(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """按 group 归拢，保持目录里的原始顺序。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for item in payload["actions"]:
        out.setdefault(item["group"], []).append(item)
    return out


def describe_command(python: str, argv: Sequence[str]) -> str:
    """把 argv 还原成一条可以显示、可以手动照抄的命令行。"""
    def quote(token: str) -> str:
        return f'"{token}"' if any(ch in token for ch in ' \t"') else token

    return " ".join([quote(python), *(quote(a) for a in argv)])


__all__ = [
    "CATALOG",
    "BY_ID",
    "MAX_OUTPUT_LINES",
    "CliAction",
    "catalog_payload",
    "describe_command",
    "environment_report",
    "group_actions",
    "interpreter",
    "module_available",
    "resolve_argv",
    "validate_query",
]
