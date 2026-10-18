"""设计系统的**可执行**守卫（静态部分）。

## 为什么要有这个文件

`search.css` 与 `phase2.css` 的注释里都写着"Phase 2 有专门断言守这条"，
但 2026-10-14 逐字 grep 过整个 tests/ 目录：**一条都不存在**。
注释里的承诺如果没人执行，它就不是承诺，只是一句自我安慰。

所以这里把设计系统里**能被静态检查**的部分钉住（渲染层面的判据在
`scratch/shot_ui.py design` 里，用 `getComputedStyle` 量）。

## 判据的选择原则

只钉"违反了就是 bug"的东西，不钉审美：
  * 品牌三色与中性令牌必须在 `body` 上定义（否则视图各拿各的颜色）；
  * **七个视图都必须有页头**（`.view-head-title`）—— 之前有四个视图没有标题，
    或者只有 13px 的区块标题冒充页面标题；
  * 表单控件必须有一条全局规则（维护页那两个输入框曾经是**浏览器的白盒子**）；
  * 空态只允许一种组件（`.wb-empty`）；
  * `.btn.gold` 必须排在 `.btn.primary` **之后**（同权重靠后生效）——
    否则"最终精选"会渲染成蓝色，而它必须是金色（金 = 你的判断）。
    这一条曾经被审计报告判成"已经坏了"，实测是好的 —— 所以断言写成
    "顺序正确"，让将来把它挪到前面的人立刻知道为什么。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "backend" / "photoos" / "web"
CSS = WEB / "css"
JS = WEB / "js"

VIEWS = ("library", "review", "search", "people", "collections", "maintenance", "help")


def _css(name: str) -> str:
    return (CSS / name).read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """去掉 `/* ... */`。注释里会引用选择器名（"这里原来有一条 `.empty`"），
    不剥掉就会被判成"还在用" —— 判据只该看代码。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _rule_bodies(text: str, selector_fragment: str) -> list[str]:
    """取出选择器里含某片段的规则体（够用：只查声明，不解析层叠）。

    ⚠️ `selector_fragment` **不要写花括号** —— 花括号是分隔符，不在选择器里。
    写过一次 `.btn {`，结果一条都匹配不到（选择器组里只有 `.btn `）。
    """
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(text)):
        if selector_fragment in match.group(1):
            out.append(match.group(2))
    return out


def _rule_body_of(text: str, selector: str) -> str | None:
    """按**精确选择器**取规则体（`.btn` 不会匹配到 `.btn.small`）。"""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(text)):
        if match.group(1).strip() == selector:
            return match.group(2)
    return None


# ============================================================ 令牌
#
# 2026-10 UI 统一之后，令牌**只在一处定义**：`css/tokens.css`（第一个加载）。
# 改前是两处 —— `app.css` 的 `:root` 一套默认蓝，`phase2.css` 再在 `body` 上
# 覆盖一遍。谁生效取决于文件顺序与选择器层级两件互不相干的事。
# 所以下面这几条断言的方向要跟着变：**钉住"只有一处"**，而不是"必须挂在 body"。
# 钉住"只有一处"比钉住具体选择器更有价值 —— 后者是实现，前者才是纪律。

def test_tokens_live_in_exactly_one_file() -> None:
    """★ 令牌只有一处定义。多一处就意味着又回到了"谁生效看运气"。"""
    owners = []
    for path in sorted(CSS.glob("*.css")):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        if re.search(r"--royal-700\s*:", text):
            owners.append(path.name)
    assert owners == ["tokens.css"], f"品牌令牌被定义在这些文件里：{owners}"


def test_tokens_file_defines_the_brand_genes() -> None:
    """三色基因必须严格是这三个值（近似色不算），加上中性/字体/焦点环令牌。"""
    tokens = _css("tokens.css")
    for token in ("--royal-700: #0a2463", "--gold-500: #f4d35e", "--wine-600: #d8315b"):
        assert token in tokens.lower(), f"品牌基因缺失或写成了近似色：{token}"
    for token in ("--ink-base", "--ink-panel", "--ink-panel-2", "--ink-divider",
                  "--ink-line", "--ink-text", "--ink-text-2", "--ink-text-3",
                  "--ink-stage", "--font-ui", "--font-display", "--font-mono",
                  "--focus", "--gutter", "--flow"):
        assert f"{token}:" in tokens, f"令牌 {token} 没有定义在 tokens.css 里"


def test_tokens_load_before_every_other_stylesheet() -> None:
    """★ tokens.css 必须**第一个**加载：后面几张表都只消费令牌。

    顺序反了不会报错，只会静默地拿到未定义变量 —— 属于最难查的一类问题，
    所以在这里钉住。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'href="/static/css/([\w.-]+)"', html)
    assert order, "index.html 里没有找到任何样式表"
    assert order[0] == "tokens.css", f"tokens.css 不是第一个加载的：{order}"


def test_the_old_variable_names_are_gone() -> None:
    """app.css 时代的旧变量名**必须彻底消失**（定义与使用都不能有）。

    这些名字（`--bg` / `--accent` / `--border` / `--radius` …）曾经以"兼容别名"
    的形式保留在 tokens.css 里，好让尚未改写的历史规则继续工作。
    2026-10 逐个数过：全部历史规则都已直接用品牌令牌，这些别名**零引用** ——
    于是整块删掉。

    留着是有害的：新人会挑一个看起来眼熟的用，等于把两套命名又引回来。
    所以这条测试按**两个方向**守：不许再定义，也不许再使用。
    """
    all_css = "\n".join(
        _strip_comments(p.read_text(encoding="utf-8")) for p in sorted(CSS.glob("*.css"))
    )
    all_js = "\n".join(p.read_text(encoding="utf-8") for p in sorted(JS.glob("*.js")))
    html = (WEB / "index.html").read_text(encoding="utf-8")
    corpus = all_css + all_js + html

    legacy = ["--bg", "--bg-elev", "--bg-elev-2", "--border", "--text",
              "--text-dim", "--accent", "--accent-dim", "--radius"]
    for name in legacy:
        # 定义：`--bg:` 这种（`--bg-elev` 不会误命中 `--bg:`，因为冒号紧跟在名字后）
        assert f"{name}:" not in all_css, f"旧变量 {name} 又被定义了"
        # 使用：`var(--bg)`
        assert f"var({name})" not in corpus, f"还有地方在用旧变量 {name}"


def test_body_font_comes_from_the_token() -> None:
    """同一个应用不该有两种正文字体（库页 Segoe、主题页 Inter 的时代结束了）。"""
    assert "font: 14px/1.5 var(--font-ui)" in _css("app.css")


# ============================================================ 七个视图

def test_every_view_has_a_page_head() -> None:
    """★ 七个标签页都要有**页面标题**，而且用同一个组件。

    没有标题的视图（照片库、维护、说明）会让用户不知道自己在哪一页；
    用 13px 区块标题冒充页面标题的（找照片、人物）则让层级看起来是错的。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for view in VIEWS:
        section = re.search(rf'<section class="view[^"]*" id="view-{view}">(.*?)\n    </section>',
                            html, re.S)
        assert section, f"找不到视图 view-{view}"
        assert "view-head-title" in section.group(1), \
            f"view-{view} 没有统一的页头（.view-head-title）"


def test_all_views_are_in_the_vertical_layout_list() -> None:
    """★ 视图是**纵向可滚动**的块，一个都不能漏。

    漏掉的那个会被 `.view.is-active{display:flex}` 排成横向 —— 维护页因此
    变成五根并排的窄柱（实测相邻面板间距 -951px）。
    """
    text = _css("phase2.css")
    for view in VIEWS:
        assert f"#view-{view}.view" in text, f"view-{view} 不在纵向布局名单里"


def test_panels_have_a_vertical_rhythm() -> None:
    """上下两块之间要有间距（维护页原来是 0），而且**走同一个令牌**。

    改前这里是硬编码的 14px，而精选页的页头/工具栏/两栏区各自又有别的数字
    （28/20/16），同一个页面三种左边距。现在由 `--flow`/`--gutter` 统一决定。
    """
    phase2 = _css("phase2.css")
    assert ".view > .panel + .panel" in phase2
    assert "margin-top: var(--flow)" in phase2
    assert "margin-left: var(--gutter)" in phase2
    assert "margin-right: var(--gutter)" in phase2


# ============================================================ 控件与按钮

def test_form_controls_have_one_global_rule() -> None:
    """表单控件必须有一条**不限定视图**的规则。

    维护页的 `#import-source` / `#import-city` 是 `type="text"`，而 app.css 只写了
    `select, input[type="search"]` —— 于是它们在深色界面里是**白色的浏览器默认盒子**。
    """
    text = _css("phase2.css")
    for selector in ('input[type="text"]', 'input[type="number"]', 'select,'):
        assert selector in text, f"全局控件规则里缺 {selector}"
    block = "\n".join(_rule_bodies(text, 'input[type="text"]'))
    assert "appearance: none" in block, "原生控件没重置外观（各浏览器会长得不一样）"
    assert "--ink-panel-2" in block and "--ink-line" in block, "控件没有用品牌底色与描边"


def test_gold_beats_primary_by_source_order() -> None:
    """★ `.btn.gold` 必须排在 `.btn.primary` 之后。

    两者权重相同（0,2,0），靠**源码顺序**决胜。把 gold 挪到 primary 前面，
    "最终精选"就会渲染成蓝色 —— 而金在本项目里固定表示"你的判断"。
    （有一条审计报告说它已经坏了；实测是好的，所以这里钉住顺序而不是颜色值。）
    """
    text = _css("phase2.css")
    primary = text.index(".btn.primary {")
    gold = text.index(".btn.gold {")
    assert gold > primary, "`.btn.gold` 被挪到了 `.btn.primary` 前面：金色会失效"


def test_button_geometry_has_a_single_source() -> None:
    """`.btn` 的几何量只应该有一份（app.css 那份是"phase2 没装"时的兜底）。

    ⚠️ 高度必须是 `var(--control-h)`，**不能**再写死一个数 ——
    写死的话就与输入框/下拉框成了两个真源（改前正是：按钮 42、输入框 38，
    并排时矮 4px，用户报"没对齐"）。见 tokens.css 里 `--control-h` 的说明。
    """
    body = _rule_body_of(_css("phase2.css"), ".btn")
    assert body is not None, "phase2.css 里没有 `.btn` 基础规则"
    assert "height: var(--control-h)" in body, \
        "`.btn` 的高度没有走 `--control-h`（会与输入框不同高）"
    assert "min-height: 42px" not in body, \
        "`.btn` 又写死了高度：与输入框/下拉框会是两个真源"
    assert ".btn.small { min-height: 34px" in _css("phase2.css").replace("\n", " ")
    # search.css 曾经把 .btn.small 又改小一号，全局生效 —— 那是"两处口径"
    assert "padding: 5px 12px" not in _strip_comments(_css("search.css")), \
        "search.css 又覆盖了 .btn.small（不同视图的按钮高度会不一致）"


def test_control_height_is_one_token() -> None:
    """输入框 / 下拉框 / 按钮**同一个高度令牌**。

    这是"控件对不齐"的根：改前输入框 `min-height: 38px`，按钮由内容算出 42px。

    ⚠️ 断言必须**限定在那条规则体内**。第一版写成"整张 phase2.css 里
    不许出现 `min-height: 38px`"，结果被两样无关的东西绊倒：
    解释这段历史的**注释**，以及 `.wb-tab { min-height: 38px }`
    （标签页，本来就不是表单控件）。判据要窄，否则它会去管不该管的事。
    """
    tokens = _css("tokens.css")
    assert "--control-h:" in tokens, "tokens.css 里没有 `--control-h`"
    # 令牌只在 tokens.css 定义一次
    for name in ("app.css", "phase2.css", "search.css", "people.css", "modal.css"):
        assert "--control-h:" not in _strip_comments(_css(name)), \
            f"{name} 又定义了一遍 `--control-h`（令牌只能定义一次）"

    # 表单控件那条全局规则：高度走令牌，且不再写死 38px
    bodies = [b for b in _rule_bodies(_css("phase2.css"), "input[type=\"text\"]")]
    assert bodies, "phase2.css 里找不到表单控件的全局规则"
    form = " ".join(bodies)
    assert "height: var(--control-h)" in form, \
        "表单控件的高度没有走 `--control-h`（会与按钮错开）"
    assert "min-height: 38px" not in form, \
        "表单控件又写死了 38px（会与按钮错开 4px）"

    # textarea 是**有意**不共用的（多行控件钉成一行高会把输入区压扁）
    ta = " ".join(_rule_bodies(_css("phase2.css"), "textarea"))
    assert "calc(var(--control-h) * 1.6)" in ta, \
        "textarea 的高度应当基于令牌按比例给，而不是被钉成一行高"


# ============================================================ 空态 / 加载态

def test_there_is_only_one_empty_state_component() -> None:
    """空态组件只能有一个：`.wb-empty`。

    曾经有五个（`.wb-empty` / `.people-empty` / `.search-empty` / `.browse-empty` /
    已死的 `.empty`），同一个状态在不同页面长得不一样。
    """
    for name in ("people.css", "search.css", "app.css"):
        text = _css(name)
        for dead in (".people-empty {", ".search-empty {", ".browse-empty {"):
            assert dead not in text, f"{name} 里还有 {dead}（空态应当统一）"
    assert "export const emptyStateHtml" not in (_css("modal.css"))
    # 共享的加载态组件必须存在，且用的是同一个壳子
    shared = (JS / "shared.js").read_text(encoding="utf-8")
    assert "export function loadingHtml" in shared
    assert "wb-empty is-loading" in shared
    assert ".wb-empty.is-loading" in _css("phase2.css")


def test_no_view_rolls_its_own_loading_text() -> None:
    """加载文案不许各写各的（曾经有 8 种写法）。

    只看**代码**，不看注释：注释里保留着"上一版写的是查询中…"这类历史说明，
    那是给人读的，不是"还在用"。
    """
    for name in ("search.js", "people.js", "pick_modal.js", "app.js"):
        text = (JS / name).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith(("*", "//", "/*")))
        for phrase in ('class="hint">查询中', 'class="hint">筛选中', '>载入中…<'):
            assert phrase not in code, f"{name} 里还有自造的加载文案：{phrase}"


# ============================================================ 死代码不再回来

def test_dead_components_stay_deleted() -> None:
    """删掉的东西不许被"顺手加回来"（它们曾经真的存在过）。"""
    app = _css("app.css")
    assert ".drawer {" not in app, "侧边抽屉已被中央弹窗取代"
    assert ".empty {" not in app, "空态统一走 .wb-empty"
    phase2 = _css("phase2.css")
    assert ".inline-panel {" not in phase2, "内嵌选择面板已被模态框取代"
    assert ".picker-row {" not in phase2
    # 死代码注释要留着，解释"为什么这里没有它"
    assert "详情抽屉（已废弃）" in app


def test_static_stylesheets_are_still_linked() -> None:
    """前端缓存契约测试会抓 `/static/css/phase2.css` 等文件；文件与链接都不能少。

    `tokens.css` 是 2026-10 UI 统一新增的令牌层，必须一起被链接与打包
    （漏了它全站会静默拿到未定义变量）。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("tokens", "app", "phase2", "modal", "search", "people", "help"):
        assert f"/static/css/{name}.css" in html, f"index.html 少了 {name}.css 的链接"
        assert (CSS / f"{name}.css").is_file(), f"css/{name}.css 不存在"


def test_every_stylesheet_is_shipped_by_packaging() -> None:
    """★ 新增样式表必须落进 wheel —— 否则装到别人机器上就是没样式的一张白页。

    项目历史上真的踩过：`package-data` 只写了 `web/*`，于是 `web/css/*.css`
    全都没进包，而作者是可编辑安装、读的是源码树，**在本机永远看不见**。
    """
    import tomllib

    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(pyproject)
    patterns = data["tool"]["setuptools"]["package-data"]["photoos"]
    assert "web/css/*.css" in patterns, f"package-data 没有覆盖 css 目录：{patterns}"
