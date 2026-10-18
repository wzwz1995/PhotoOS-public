/**
 * PhotoOS · Phase 2 快速筛选工作台（PRD §9.2 的核心交互）
 *
 * 目标（PRD §9.2「首轮筛选效率标准」LOCKED）：
 *   **一个 Scene 含 20–50 张 Shot，用户能在 ≤10 分钟内完成第一轮筛选。**
 *   重点是**系统降低用户做选择的成本**，而不是 AI 替用户选择。
 *
 * 为什么这样能达成（实测依据，PHASE2_RESEARCH §11.3）：
 *   实测万宁市 302 张 → 2 s 阈值切出 176 组，但**只有 15 组 ≥3 张**。
 *   单张组可以一路 `Space` 划过，**需要"决策"的多张组只有 15 个左右**。
 *   所以核心机制不是"让用户看 50 次"，而是让用户看 10–15 次。
 *
 * 键盘（PRD：尽量减少逐张打开的成本）：
 *   ← / A  上一张        → / D  下一张
 *   ↑      跳过整组      ↓      回到本组第一张
 *   1      候选          2      精选        3      最终精选
 *   0 / Backspace  退回未审阅（PRD：允许随时退回）
 *   R      写理由        C      比较视图（本组并排）
 *   Space  下一张未审阅的（快进）    , / .   调相似度阈值
 *   Esc    退出工作台    ?      快捷键说明
 *
 * 零构建：原生 ES module。**不 import app.js**（那会二次启动应用）—— 共用 `shared.js`。
 */

import {
  API, $, toast, escapeHtml, baseName, formatWhen, selectionLabel, selectionClass,
  SELECTION_LABELS,
} from './shared.js';
// 图源只走这里：URL 模板在全应用只该有一处（见 docs/11-shell-unification.md 七）。
// ⚠️ 精选页这三处**都没有可点的文件名**（比较卡显示的是"时刻 + 状态 + 你的理由"，
// 选中理由靠卡片整块点击切换当前项），所以只统一图源，不套 `fileBlock()` ——
// 硬加一个原片入口会改掉这一页的键盘流与点击语义。
import { assetImgAttrs } from './cards.js';
// 「当前在筛什么」的唯一落点：只读投影，不引入新状态（与照片库、找照片同一条）。
import { renderQueryBar } from './query_bar.js';
import { openPickModal, closePickModal, pickModalOpen } from './pick_modal.js';
// 「还没有分好组」时直接给按钮执行（入队 `regroup` 任务，跑完三层）
import { catalogAction, openActionsDialog, runActionWithProgress } from './actions.js';

// --------------------------------------------------------------------- 状态
const state = {
  started: false,
  sessions: [],
  scenes: [],
  sceneId: null,
  detail: null,
  /** 当前 Scene 的 Shot 列表（按拍摄时间）。 */
  shots: [],
  /** 当前 Shot 下标。 */
  index: 0,
  /** 大图模式：'single' 单张大图 | 'compare' 本组并排。 */
  mode: 'single',
  /**
   * 界面近似分组用的时间邻接阈值（秒）。
   *
   * **这是"决策 #5"的用户可配置项在界面上的那一层。** 它必须来自后端
   * （`GET /api/sessions/{id}/review-config` 的 `effective`），而不是写死 ——
   * 否则用户改了配置、界面却按另一个值画分组，看起来就是"配置没生效"。
   *
   * ⚠️ 它只影响**界面上的时间近似分组**；真正落库的 Shot 边界由后端按
   * 同一个 `shot_gap_seconds` 算好（改完要跑一次重算才反映到数据上）。
   */
  gapSeconds: 3.0,
  /** 预设清单与全局默认值（从后端取，随 `gapSeconds` 一起显示代价说明）。 */
  gapPresets: [],
  gapGlobalDefault: null,
  /** 本 Session 是否覆盖了全局值（用于提示"这个时段单独设过"）。 */
  gapSource: '',
  /** ⚠️ 「本时段是否单独设过」用**这个布尔量**判断，不要解析 `gapSource` 的文案。 */
  gapOverridden: false,
  /** 后端给出的能力边界说明（时间规则抓不住"构图相似但相隔远"）。 */
  gapLimitation: '',
  /** 待提交的批量选择（用键盘逐张积累）。 */
  labels: { ...SELECTION_LABELS },
  /** 本次进入工作台后的操作次数与开始时间 —— 用于如实展示"花了多久"。 */
  startedAt: null,
  keystrokes: 0,
  /** 比较视图里当前并排的 Shot 下标（同组）。 */
  compareGroup: [],
  /** 调整分组面板的状态：`{shotId, items, picked}`，null 表示未打开。 */
  adjust: null,
  /**
   * Scene 选择器的筛选条件。
   *
   * **为什么需要它**：真实库有 303 个 Scene，一个大时段可以有 15 个以上，
   * 单列下拉里翻找等于"用眼睛搜数据库"。地点 + 日期 + 关键词三个条件
   * 就能把范围缩到一个可读的长度，而且这些字段后端**早就返回了**
   * （`/api/scenes` 里有 `city` / `captured_from` / `name`），不需要改接口。
   */
  filter: { city: '', from: '', to: '', keyword: '' },
  /** 日期弹窗是否展开（自己画的日历，不用原生 date 控件）。 */
  datePopOpen: false,
  /** 日期弹窗当前显示的月份（`YYYY-MM`）。 */
  calMonth: null,
};

const KEYS_DOC = [
  ['← / A', '上一张'],
  ['→ / D', '下一张'],
  ['↑', '跳过整组'],
  ['↓', '回到本组第一张'],
  ['1', '标为候选'],
  ['2', '标为精选'],
  ['3', '标为最终精选'],
  ['0 / Backspace', '退回未审阅'],
  ['R', '写理由'],
  ['C', '比较视图（本组并排）'],
  ['Space', '下一张未审阅'],
  [', / .', '调时间邻接阈值'],
  ['T', '把当前这张加入主题'],
  ['V', '调整分组（拆分/合并）'],
  ['Esc', '退出工作台'],
  ['?', '本说明'],
];

// --------------------------------------------------------------------- 相似组
/**
 * 把当前 Scene 的 Shot 按"时间紧邻"聚成相似组。
 *
 * ⚠️ 阈值取自 `state.gapSeconds`（后端 `shot_gap_seconds` 的有效值），
 * **不再写死** —— 写死会让"用户配置了却看不出变化"。
 *
 * ⚠️ 这里**不做视觉相似度计算**：把 512 维向量搬到浏览器再算余弦是**错的架构**
 * （后端 0.27 ms/查询，浏览器反而更慢且要传 43 MB）。所以前端只用**时间邻接**
 * 做"竞争组"的近似，真正的视觉相似度由后端计算。
 *
 * 实测已知的**能力边界**（必须如实告诉用户，见 PHASE2_RESEARCH §5.5）：
 * 时间规则**永远**抓不住"构图高度相似但相隔较远"的照片 —— 实测有一对相隔
 * 360.5 s 却余弦 0.9846。所以把阈值调大并不能"找全相似照片"，只会把更多
 * 本来独立的拍摄并成一个决策单元。
 */
function computeGroups(shots, thresholdSeconds) {
  const limit = Number.isFinite(thresholdSeconds) ? thresholdSeconds : state.gapSeconds;
  const groups = [];
  let current = [];
  shots.forEach((shot, index) => {
    const prev = current.length ? shots[current[current.length - 1]] : null;
    const stamp = shot.captured_at ? Date.parse(shot.captured_at.replace(' ', 'T')) : null;
    const prevStamp = prev && prev.captured_at ? Date.parse(prev.captured_at.replace(' ', 'T')) : null;
    const apart = stamp !== null && prevStamp !== null && (stamp - prevStamp) / 1000 > limit;
    if (current.length && (apart || stamp === null || prevStamp === null)) {
      groups.push(current);
      current = [];
    }
    current.push(index);
  });
  if (current.length) groups.push(current);
  return groups;
}

function currentGroups() {
  return computeGroups(state.shots);
}

function groupOf(index) {
  return currentGroups().find((group) => group.includes(index)) || [index];
}

// --------------------------------------------------------------------- 渲染
/** `HH:MM`。 */
function clockOf(stamp) {
  const text = String(stamp || '');
  return text.length >= 16 ? text.slice(11, 16) : '';
}

/** 月份从 `2026-01-01_0944-1954` 这种紧凑形里取日期（没有 `captured_from` 时兜底）。 */
function compactDate(period) {
  const m = String(period || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : '';
}

/**
 * 一个场景的**给人看**的时间。
 *
 * 改之前下拉里铺的是 `scene.name` 的末段 —— 那是**目录名**，
 * 长这样：`万宁市 · 2025-11-29 06:10:01`。用户明确反馈"可读性很差"，说得对：
 * 那串东西是给文件系统看的。
 *
 * 现在用 `captured_from` / `captured_to` 拼（实测 277/303 有值，275 个是当天）：
 *   同一天 → `2026-10-08 17:02–18:43`
 *   跨天   → `10-08 17:02 → 10-09 07:12`
 *   没时间 → 退回 `period` 里的日期；再没有就退回原名的末段（**不编造**）
 */
function sceneWhen(scene) {
  const from = String(scene.captured_from || '');
  const to = String(scene.captured_to || '');
  const day = from.slice(0, 10);
  if (day && to) {
    const endDay = to.slice(0, 10);
    const a = clockOf(from);
    const b = clockOf(to);
    if (endDay === day) return a && b && a !== b ? `${day} ${a}–${b}` : `${day} ${a || ''}`.trim();
    return `${day.slice(5)} ${a} → ${endDay.slice(5)} ${b}`;
  }
  if (day) return day;
  const fallback = compactDate(scene.period);
  if (fallback) return fallback;
  const parts = String(scene.name || '').split('\\').filter(Boolean);
  return parts[parts.length - 1] || String(scene.name || '');
}

/** 一个场景在下拉里的文字：时间在前、还要审多少在后。
 *
 * 完整原名仍留在 `title` 里，鼠标悬停能看到（排障时还是要看目录名）。 */
function sceneLabel(scene) {
  const unreviewed = Number.isFinite(Number(scene.needs_review))
    ? Number(scene.needs_review)
    : Math.max(0, Number(scene.shots || 0) - Number(scene.reviewed || 0));
  const tail = unreviewed === 0 ? '✅ 已审完' : `未审 ${unreviewed}`;
  return `${sceneWhen(scene)} — ${scene.shots} Shot，${tail}，最终 ${scene.finals || 0}`;
}

/** 库里出现过的地点（按时段里的 Scene 数排序）。 */
function uniqueCities() {  const counts = new Map();
  state.scenes.forEach((scene) => {
    const city = scene.city || '';
    if (!city) return;
    counts.set(city, (counts.get(city) || 0) + 1);
  });
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function sceneMatchesFilter(scene) {
  const { city, from, to, keyword } = state.filter;
  if (city && (scene.city || '') !== city) return false;
  const day = String(scene.captured_from || '').slice(0, 10);
  if (from && (!day || day < from)) return false;
  if (to && (!day || day > to)) return false;
  const needle = keyword.trim().toLowerCase();
  if (needle) {
    const haystack = `${scene.name || ''} ${scene.city || ''} ${scene.period || ''}`.toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

function visibleScenes() {
  return state.scenes.filter(sceneMatchesFilter);
}

/**
 * 还没选场景时的提示 —— 文案**跟着漏斗的当前层走**。
 *
 * 为什么不能写死一句："在上面选一个 Scene 开始筛选" 在刚进页面时是**错的**：
 * 那一层还没显示出来（要先缩小范围）。界面说什么，必须和用户此刻看得到的控件一致，
 * 否则他会去找一个不存在的东西。
 */
function reviewEmptyHtml() {
  // 空态只报"状态"，不教用户怎么用（工具条上的三层筛选自会引导）。
  // 数字必须动态：库会长大、筛选会变；语句在 N=0/1/303 时都要读得通。
  const filtered = visibleScenes().length;
  const total = state.scenes.length;
  const nothing = filtered === 0;
  const narrowed = filtered < total;
  const title = nothing ? '没有匹配的场景' : '选择一个场景';
  const count = state.sessionId
    ? `本时段 ${total}${narrowed ? ` · 筛出 ${filtered}` : ''}`
    : `场景 ${total}${narrowed ? ` · 筛出 ${filtered}` : ''}`;
  return '<div class="wb-empty">'
    + `<p class="wb-empty-title">${title}</p>`
    + `<p class="wb-empty-count">${count}</p>`
    + '</div>';
}

function renderStepVisibility() {
  const at = (name) => document.querySelector(`.wb-step[data-step="${name}"]`);
  if (!at('city')) return;                    // 还没渲染（初次进来会重建）

  const { city, from, to, keyword } = state.filter;
  const has = (v) => Boolean(v && String(v).trim());
  const filteredCount = visibleScenes().length;
  // ⚠️ **选了时段本身就是"缩小过范围"**。`state.scenes` 在选了时段之后只含该时段的场景，
  // 所以 `filteredCount < state.scenes.length` 恒为假 —— 只按这个判据会让用户在
  // 选完时段之后**永远看不到场景下拉**（症状：像"时段筛选没反应/什么都筛不出来"）。
  const sessionScoped = Boolean(state.sessionId);
  const narrowed = sessionScoped || filteredCount < state.scenes.length;

  // ① 地点与 ② 拍摄日期：**入口**，永远显示（它们是"粗"的那一层）
  at('city').hidden = false;
  at('date').hidden = false;
  // ③ 关键词：日期层之后（同一层的细分手段，不必等日期真的选了）
  at('keyword').hidden = false;
  // ④ 选场景：**缩小过范围之后才出现** —— 303 个场景直接铺出来没有意义，
  //    而这个选择器是"决定"，不是筛选。
  at('scene').hidden = !narrowed;

  // 计数与条件条一起刷新
  const count = $('#review-filter-count');
  if (count) {
    // 与空态用**同一套**写法（`场景 303 · 筛出 84` / `本时段 11 · 筛出 3`），
    // 免得两处对同一件事说两种话。
    const narrowedText = filteredCount < state.scenes.length ? ` · 筛出 ${filteredCount}` : '';
    count.textContent = sessionScoped
      ? `本时段 ${state.scenes.length}${narrowedText}`
      : `场景 ${state.scenes.length}${narrowedText}`;
  }
  renderReviewQueryBar();

  // 空态文案跟着当前层走（"在上面选一个场景"在还没显示那一层时是错的）。
  // 只在**确实处于空态**时重画，避免动到正在看的照片。
  if (!state.sceneId && !state.compareGroup.length) {
    const viewer = $('#review-viewer');
    const empty = viewer && viewer.querySelector('.wb-empty');
    if (empty) viewer.innerHTML = reviewEmptyHtml();
  }
}

/**
 * **只重画 Scene 下拉的选项**，不重建整块 picker。
 *
 * 为什么必须拆开：筛选框是 `oninput` 触发的，如果每次都重建 picker 的 innerHTML，
 * 用户每敲一个字光标都会丢 —— 这类"能筛但没法打字"的毛病比不筛更烦人。
 * 绑在 `<select>` 元素上的 `onchange` 只改 `innerHTML` 时不会丢。
 */
function renderSceneOptions() {
  const select = $('#review-scene');
  if (!select) return;
  const list = visibleScenes();
  const options = list.map((scene) => `
    <option value="${scene.id}" ${scene.id === state.sceneId ? 'selected' : ''}
      title="${escapeHtml(scene.name)}">
      ${escapeHtml(sceneLabel(scene))}
    </option>`).join('');
  // 当前选中的 Scene 被筛掉时**仍然保留一项**，否则用户会觉得"选好的场景自己没了"
  const current = state.scenes.find((scene) => scene.id === state.sceneId);
  const keepCurrent = current && !list.includes(current)
    ? `<option value="${current.id}" selected>（当前，已被筛选隐藏）${escapeHtml(sceneLabel(current))}</option>`
    : '';
  select.innerHTML = `<option value="">（未选择）</option>${keepCurrent}${options}`;
  // 计数、层的可见性、条件条都在一处刷新 —— 它们读的是同一份 state，
  // 分三处写必然漂移（这个教训在条件条那一轮已经付过学费）。
  renderStepVisibility();
  // 空态文案跟着当前层走（"在上面选一个 Scene" 在还没显示那一层时是错的）。
  // 只在**确实处于空态**时重画，避免动到正在看的照片。
  if (!state.sceneId && !state.compareGroup.length) {
    const viewer = $('#review-viewer');
    const empty = viewer && viewer.querySelector('.wb-empty');
    if (empty) viewer.innerHTML = reviewEmptyHtml();
  }
}

/**
 * 时段的显示名。
 *
 * 改之前是 `城市 · 时段目录` —— 把反斜杠换成点而已，后半段仍然是目录名
 * （`希腊市 · 2026-10-08_1702_至_10-09_1847`）。用户反馈"可读性很差"。
 * 现在把它解成时间区间：`希腊市 · 10-08 17:02 → 10-09 18:47`。
 * 完整原名仍留在 `title` 里。
 */
function sessionLabel(session) {
  const parts = String(session.name || '').split('\\').filter(Boolean);
  const city = parts[0] || '';
  const tail = parts[1] || '';
  // 目录名的形态：`2026-10-08_1702-1847` 或 `2026-10-08_1702_至_10-09_1847`
  const m = tail.match(
    /^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(?:_?至_?(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2}))?/);
  if (!m) return city ? `${city} · ${tail}` : (session.name || '');
  const start = `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  const span = m[6]
    ? `${start} → ${m[7]}-${m[8]} ${m[9]}:${m[10]}`
    : start;
  return city ? `${city} · ${span}` : span;
}

function renderPicker() {
  const node = $('#review-picker');
  if (!node) return;
  const sessions = state.sessions.map((session) => `
    <option value="${session.id}" ${session.id === state.sessionId ? 'selected' : ''}
      title="${escapeHtml(session.name)}">
      ${escapeHtml(sessionLabel(session))}（${session.shots} Shot / ${session.scenes} Scene）
    </option>`).join('');
  const cities = uniqueCities().map(([city, count]) => `
    <option value="${escapeHtml(city)}" ${city === state.filter.city ? 'selected' : ''}>
      ${escapeHtml(city)}（${count}）
    </option>`).join('');
  const presets = state.gapPresets.map((item) => {
    const isDefault = Number(item.value) === Number(state.gapGlobalDefault);
    return `<button class="btn small gap-preset${isDefault ? ' is-default' : ''}"
      data-gap="${item.value}"
      title="${escapeHtml(`${item.label}${item.note ? ` —— ${item.note}` : ''}`)}">
      ${item.value}s${isDefault ? '（默认）' : ''}</button>`;
  }).join('');
  const override = state.gapOverridden;

  // ⚠️ 这里**一次性渲染全部层**，靠 `hidden` 切换可见性（`renderStepVisibility`）。
  // 不能"按层重建 DOM 来显示下一层"：那会让正在打字的输入框丢光标 ——
  // 本文件前面已经为这个坑写过一次注释（见 `renderSceneOptions`）。
  node.innerHTML = `
    <div class="wb-steps">
      <div class="wb-step" data-step="city">
        <span class="wb-step-no">①</span>
        <div class="wb-step-body">
          <span class="wb-step-label">地点</span>
          <select id="review-city"><option value="">（全部地点）</option>${cities}</select>
        </div>
        ${state.filter.city ? '<button class="wb-step-clear" id="review-city-clear" type="button" title="撤掉地点">×</button>' : ''}
      </div>

      <div class="wb-step" data-step="date">
        <span class="wb-step-no">②</span>
        <div class="wb-step-body">
          <span class="wb-step-label">拍摄日期</span>
          <div class="wb-field-pop">
            <button class="btn wb-date-btn${dateIsSet() ? ' is-set' : ''}" id="review-date-open">
              ${escapeHtml(dateButtonLabel())}
            </button>
            <!-- ⚠️ 弹窗必须挂在工具栏内部（见 .wb-toolbar 的 overflow 说明） -->
            <div id="review-date-pop" class="wb-pop" hidden></div>
          </div>
        </div>
        ${dateIsSet() ? '<button class="wb-step-clear" id="review-date-clear" type="button" title="撤掉日期">×</button>' : ''}
      </div>

      <div class="wb-step" data-step="keyword">
        <span class="wb-step-no">③</span>
        <div class="wb-step-body">
          <span class="wb-step-label">关键词</span>
          <input type="search" id="review-keyword" placeholder="城市 / 日期 / 目录名，例如 希腊 或 2026-10"
                 value="${escapeHtml(state.filter.keyword)}">
        </div>
        ${state.filter.keyword ? '<button class="wb-step-clear" id="review-keyword-clear" type="button" title="撤掉关键词">×</button>' : ''}
      </div>

      <!-- 最后一层：从筛出来的结果里**挑一个**。它上面一条分隔线：
           上面是"缩小范围"，下面是"决定挑哪个"。 -->
      <div class="wb-step wb-step-final" data-step="scene">
        <span class="wb-step-no">④</span>
        <div class="wb-step-body">
          <span class="wb-step-label">选一个场景 · 进入逐组精选</span>
          <select id="review-scene"><option value="">（未选择）</option></select>
        </div>
        ${state.sceneId ? '<button class="wb-step-clear" id="review-scene-clear" type="button" title="撤掉场景选择">×</button>' : ''}
        <span class="wb-count" id="review-filter-count"></span>
      </div>
    </div>

    <!-- 辅助行：与上面的漏斗**同一套"标签一列、控件一列"的网格**。
         改之前这两行用的是 .wb-field（纵向：标签在上、控件在下），
         而上面几层是横向 —— 两套布局，标签一长一短时控件起点必然错开
         （实测"相邻时间阈值"92px vs 段落标签 430px，用户说"看着难受"）。
         现在标签统一占一列、控件统一从同一条竖线开始。

         ⚠️ 这段注释在**模板字符串内部**，所以**不能出现反引号** ——
         一个反引号就会提前闭合字符串，后面的内容被当成 JS 求值
         （实测：注释里写 .wb-field 导致 ReferenceError: field is not defined，
         整个精选页渲染失败，而报错信息只说"field is not defined"，极难定位）。
         这条规则由 _probe/lint_backticks.py 自动检查 —— 别只靠眼睛。 -->
    <div class="wb-row wb-row-secondary wb-rows">
      <div class="wb-field-inline">
        <span class="wb-label">相邻时间阈值</span>
        <input type="number" id="review-gap-input" min="0.1" step="0.5"
               class="wb-gap-value" value="${state.gapSeconds}"
               title="多近的两张算同一个竞争组">
        <button class="btn primary" id="review-gap-apply">应用</button>
        ${presets}
        ${override ? '<button class="btn ghost" id="review-gap-reset">恢复默认</button>' : ''}
        <span class="wb-source">${escapeHtml(state.gapSource || '')}</span>
      </div>

      <div class="wb-field-inline">
        <span class="wb-label">时段</span>
        <select id="review-session" title="整段处理一次拍摄活动时用；平时按地点+日期筛就够了">
          <option value="">（全部时段）</option>${sessions}</select>
        ${state.sessionId ? '<button class="wb-step-clear" id="review-session-clear" type="button" title="回到全部时段">×</button>' : ''}
        <button class="btn" id="review-add-collection">加入主题…</button>
        <button class="btn ghost" id="review-scene-merge"
                title="把当前这一场并入同一时段的前一场（只对「切的太碎」有用；两场都必须已自动切好）">
          并入前一场</button>
        <button class="btn ghost" id="review-scene-split"
                title="把当前这张之后的照片从这一场拆出去，形成新的一场">从当前张拆开</button>
        <button class="btn ghost" id="review-help">快捷键 <kbd>?</kbd></button>
      </div>
    </div>`;

  const sessionSelect = $('#review-session');
  if (sessionSelect) sessionSelect.onchange = (event) => selectSession(event.target.value);
  const sceneSelect = $('#review-scene');
  if (sceneSelect) sceneSelect.onchange = (event) => selectScene(Number(event.target.value));
  const sceneClear = $('#review-scene-clear');
  if (sceneClear) sceneClear.onclick = () => {
    // 回到"未选场景"：保留上面的筛选与时段，只把这个决定撤掉
    state.sceneId = null;
    state.detail = null;
    state.shots = [];
    state.index = 0;
    renderPicker();
    renderAll();
  };
  const sessionClear = $('#review-session-clear');
  if (sessionClear) sessionClear.onclick = () => selectSession('');
  const help = $('#review-help');
  if (help) help.onclick = showHelp;

  const city = $('#review-city');
  if (city) city.onchange = (event) => {
    state.filter.city = event.target.value;
    renderSceneOptions();
    renderStepVisibility();
  };
  const cityClear = $('#review-city-clear');
  if (cityClear) cityClear.onclick = () => dropReviewFilter('city');
  const dateOpen = $('#review-date-open');
  if (dateOpen) dateOpen.onclick = toggleDatePop;
  const dateClear = $('#review-date-clear');
  if (dateClear) dateClear.onclick = () => {
    state.filter.from = '';
    state.filter.to = '';
    renderPicker();
  };
  const keyword = $('#review-keyword');
  if (keyword) {
    keyword.oninput = (event) => {
      state.filter.keyword = event.target.value;
      renderSceneOptions();
      renderStepVisibility();
    };
  }
  const keywordClear = $('#review-keyword-clear');
  if (keywordClear) keywordClear.onclick = () => dropReviewFilter('keyword');

  // 条件条随 picker 一起刷新由 `renderSceneOptions` 收口（见它末尾的说明）

  const gapApply = $('#review-gap-apply');
  if (gapApply) gapApply.onclick = applyGapFromInput;
  const gapInput = $('#review-gap-input');
  if (gapInput) {
    // 回车 = 应用（不用去够鼠标）
    gapInput.onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); applyGapFromInput(); } };
  }
  node.querySelectorAll('.gap-preset').forEach((button) => {
    button.onclick = () => applyGapValue(Number(button.dataset.gap), button.title.split(' —— ')[0]);
  });
  const gapReset = $('#review-gap-reset');
  if (gapReset) gapReset.onclick = resetGapOverride;

  const addToCollection = $('#review-add-collection');
  if (addToCollection) addToCollection.onclick = openAddToCollection;

  const mergeScene = $('#review-scene-merge');
  if (mergeScene) mergeScene.onclick = mergeIntoPreviousScene;
  const splitScene = $('#review-scene-split');
  if (splitScene) splitScene.onclick = splitSceneAtCurrentShot;

  // 日期弹窗被 `innerHTML` 重建掉了：按 state 复位一次（同下面的面板处理）
  const pop = $('#review-date-pop');
  if (pop) pop.hidden = !state.datePopOpen;

  // 交给 `renderSceneOptions` 收口：它刷下拉选项，末尾统一刷层的可见性、
  // 计数与条件条。顺序反了会先按旧 state 画一遍（下方已有同样的注释）。
  renderSceneOptions();
}

// --------------------------------------------------------------- 日期弹窗（日历）
function dateIsSet() {
  return Boolean(state.filter.from || state.filter.to);
}

function dateButtonLabel() {
  const { from, to } = state.filter;
  if (!from && !to) return '全部日期';
  if (from && to) return from === to ? from : `${from} → ${to}`;
  return from ? `${from} →` : `→ ${to}`;
}

/** 库里实际有照片的月份（用来决定日历从哪个月开始、以及"今年/去年"这类快捷项）。 */
function photoMonths() {
  const months = new Set();
  state.scenes.forEach((scene) => {
    const day = String(scene.captured_from || '').slice(0, 7);
    if (day) months.add(day);
  });
  return [...months].sort();
}

function toggleDatePop() {
  state.datePopOpen = !state.datePopOpen;
  if (state.datePopOpen && !state.calMonth) {
    // 默认落在"库里最近有照片的那个月"，而不是今天 —— 你的照片可能都在过去
    const months = photoMonths();
    state.calMonth = months.length ? months[months.length - 1] : new Date().toISOString().slice(0, 7);
  }
  const pop = $('#review-date-pop');
  if (!pop) return;
  pop.hidden = !state.datePopOpen;
  renderDatePop();
}

function shiftMonth(delta) {
  const [year, month] = state.calMonth.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1 + delta, 1));
  state.calMonth = date.toISOString().slice(0, 7);
  renderDatePop();
}

/**
 * 渲染日历弹窗。
 *
 * 为什么不用 `<input type="date">`：原生控件（1）样式无法品牌化（Console P0 禁止默认样式），
 * （2）只能一天一天点，选"某个旅行区间"要点两次日历、还要在两个月之间翻页。
 * 这里给的是：**两个月并排 + 快捷区间 + 有照片的日子有标记**。
 */
function renderDatePop() {
  const pop = $('#review-date-pop');
  if (!pop || !state.datePopOpen) return;
  const months = photoMonths();
  const monthSet = new Set(months);
  const withData = new Set(state.scenes.map((s) => String(s.captured_from || '').slice(0, 10)));
  const [year, month] = state.calMonth.split('-').map(Number);
  const second = (() => {
    const date = new Date(Date.UTC(year, month, 1));
    return date.toISOString().slice(0, 7);
  })();
  const days = (ym) => {
    const [y, m] = ym.split('-').map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1));
    const startWeekday = (first.getUTCDay() + 6) % 7;   // 周一为一周之首
    const total = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const cells = [];
    for (let i = 0; i < startWeekday; i += 1) cells.push(null);
    for (let day = 1; day <= total; day += 1) cells.push(day);
    return { cells, total };
  };
  const today = new Date().toISOString().slice(0, 10);
  const cellHtml = (ym, day) => {
    if (day === null) return '<span></span>';
    const iso = `${ym}-${String(day).padStart(2, '0')}`;
    const { from, to } = state.filter;
    const classes = ['wb-cal-day'];
    if (iso === from || iso === to) classes.push('is-edge');
    else if (from && to && iso > from && iso < to) classes.push('is-in-range');
    if (iso === today) classes.push('is-today');
    if (withData.has(iso)) classes.push('is-has-data');
    return `<button class="${classes.join(' ')}" data-day="${iso}"
      title="${withData.has(iso) ? '这天有照片' : ''}">${day}</button>`;
  };
  const monthHtml = (ym) => {
    const { cells } = days(ym);
    const [y, m] = ym.split('-').map(Number);
    // 只有第一个月给翻页按钮：两个月并排时，中间并排两组箭头会让人分不清翻哪个
    const nav = ym === state.calMonth
      ? `<button class="btn wb-cal-nav" data-shift="-1" title="上个月">‹</button>
         <button class="btn wb-cal-nav" data-shift="1" title="下个月">›</button>`
      : '<span></span><span></span>';
    return `
      <div class="wb-cal">
        <div class="wb-cal-head">
          ${nav}
          <span class="wb-cal-title">${y} · ${String(m).padStart(2, '0')}</span>
        </div>
        <div class="wb-cal-weekdays">
          ${['一', '二', '三', '四', '五', '六', '日'].map((d) => `<span>${d}</span>`).join('')}
        </div>
        <div class="wb-cal-days">
          ${cells.map((day) => cellHtml(ym, day)).join('')}
        </div>
      </div>`;
  };
  const quick = [
    ['全部日期', '', ''],
    ['最近 30 天', offsetDay(-29), today],
    ['今年', `${today.slice(0, 4)}-01-01`, `${today.slice(0, 4)}-12-31`],
    ['去年', `${Number(today.slice(0, 4)) - 1}-01-01`, `${Number(today.slice(0, 4)) - 1}-12-31`],
  ];
  const dataQuick = months.slice(-4).reverse().map((ym) => {
    const [y, m] = ym.split('-').map(Number);
    const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
    return [`${y} 年 ${m} 月`, `${ym}-01`, `${ym}-${String(last).padStart(2, '0')}`];
  });
  pop.innerHTML = `
    <div class="wb-pop-head">
      <span class="wb-pop-title">按拍摄日期筛选</span>
      <button class="btn ghost small" id="date-pop-close">收起</button>
    </div>
    <div class="wb-quick">
      ${[...quick, ...dataQuick].map(([label, from, to]) => `
        <button class="btn" data-quick-from="${from}" data-quick-to="${to}">${label}</button>`).join('')}
    </div>
    <div class="wb-cal-months">${monthHtml(state.calMonth)}${monthHtml(second)}</div>
    <div class="wb-cal-foot">
      <span class="wb-count">当前：<b>${escapeHtml(dateButtonLabel())}</b>
        ${monthSet.size ? ` · 库内照片覆盖 ${monthSet.size} 个月` : ''}</span>
      <span>
        <button class="btn ghost small" id="date-pop-clear">清除日期</button>
        <button class="btn primary small" id="date-pop-apply">完成</button>
      </span>
    </div>`;
  pop.querySelectorAll('[data-shift]').forEach((button) => {
    button.onclick = () => shiftMonth(Number(button.dataset.shift));
  });
  pop.querySelectorAll('[data-day]').forEach((button) => {
    button.onclick = () => pickDay(button.dataset.day);
  });
  pop.querySelectorAll('[data-quick-from]').forEach((button) => {
    button.onclick = () => {
      state.filter.from = button.dataset.quickFrom;
      state.filter.to = button.dataset.quickTo;
      renderDatePop();
      renderSceneOptions();
      refreshDateButton();
    };
  });
  const clear = $('#date-pop-clear');
  if (clear) clear.onclick = () => {
    state.filter.from = '';
    state.filter.to = '';
    renderDatePop();
    renderSceneOptions();
    refreshDateButton();
  };
  const apply = $('#date-pop-apply');
  if (apply) apply.onclick = () => toggleDatePop();
  const close = $('#date-pop-close');
  if (close) close.onclick = () => toggleDatePop();
}

/** 点一天：没起点就设起点；有起点没终点就设终点；两端都有就重新开始一个区间。 */
function pickDay(iso) {
  const { from, to } = state.filter;
  if (!from || (from && to)) {
    state.filter.from = iso;
    state.filter.to = '';
  } else if (iso < from) {
    state.filter.from = iso;
    state.filter.to = from;
  } else {
    state.filter.to = iso;
  }
  renderDatePop();
  renderSceneOptions();
  refreshDateButton();
}

function refreshDateButton() {
  const button = $('#review-date-open');
  if (!button) return;
  button.textContent = dateButtonLabel();
  button.classList.toggle('is-set', dateIsSet());
}

function offsetDay(delta) {
  const date = new Date();
  date.setDate(date.getDate() + delta);
  return date.toISOString().slice(0, 10);
}

function clearFilter() {
  state.filter = { city: '', from: '', to: '', keyword: '' };
  state.datePopOpen = false;
  renderPicker();
}

// --------------------------------------------------------------------- 条件条
//
// 精选页的"当前条件"是**地点 / 日期 / 关键词**三个筛选（时段与阈值不算条件：
// 时段是"你在哪一段里工作"，阈值是"分组松紧"，都不是筛掉照片的东西）。
//
// 与照片库、找照片共用同一条 `.query-bar`（`js/query_bar.js`）——
// 它是 `state.filter` 的**只读投影**，不引入新状态。

function reviewFilterChips() {
  const f = state.filter;
  const chips = [];
  if (f.city) chips.push({ key: 'city', label: `地点：${f.city}`, remove: () => dropReviewFilter('city') });
  if (f.from && f.to) {
    chips.push({ key: 'date', label: `日期 ${f.from} ~ ${f.to}`,
      remove: () => { state.filter.from = ''; state.filter.to = ''; renderPicker(); } });
  } else if (f.from) {
    chips.push({ key: 'from', label: `从 ${f.from}`, remove: () => dropReviewFilter('from') });
  } else if (f.to) {
    chips.push({ key: 'to', label: `到 ${f.to}`, remove: () => dropReviewFilter('to') });
  }
  const kw = String(f.keyword || '').trim();
  if (kw) chips.push({ key: 'keyword', label: `关键词「${kw}」`, remove: () => dropReviewFilter('keyword') });
  return chips;
}

/** 撤掉一个筛选并重画（`renderPicker` 会同时刷新下拉、计数、层的可见性与输入框的值）。 */
function dropReviewFilter(key) {
  state.filter[key] = '';
  if (key === 'from' || key === 'to') state.datePopOpen = false;
  renderPicker();
}
function renderReviewQueryBar() {
  const chips = reviewFilterChips();
  const total = state.scenes.length;
  renderQueryBar($('#review-query'), {
    compute: () => chips,
    // 只在**真的在筛**的时候报数：没筛时 "显示 30 / 30 个 Scene" 是噪音。
    // 用 `totalText` 而不是 `total` —— 这里筛的是 Scene，不是"张"。
    totalText: chips.length ? `显示 ${visibleScenes().length} / ${total} 个 Scene` : '',
    clearLabel: '清除筛选',
    onClearAll: chips.length ? clearFilter : null,
  });
}

/** 从输入框读阈值并应用（点「应用」或按回车）。 */
function applyGapFromInput() {
  const input = $('#review-gap-input');
  if (!input) return;
  const raw = Number(input.value);
  if (!Number.isFinite(raw) || raw <= 0) {
    toast('阈值必须是正数（单位：秒）', true);
    return;
  }
  applyGapValue(Math.round(raw * 100) / 100, '');
}

/** 设定阈值：界面即时生效 + 写回本时段（下次重算按同一个值）。 */
function applyGapValue(value, label) {
  state.gapSeconds = value;
  renderAll();
  const preset = state.gapPresets.find((item) => Number(item.value) === value);
  renderPicker();
  toast(`相邻时间阈值 ${formatGap(value)}${label || (preset ? ` · ${preset.label}` : '')}`
    + '（界面即时生效；已写回本时段，下次重算按同一个值切分组）');
  persistGap(value);
}

/** 清除本时段的覆盖值，回到全局默认。 */
async function resetGapOverride() {
  if (!state.sessionId) return;
  try {
    const result = await API.patch(`/api/sessions/${state.sessionId}/review-config`,
      { shot_gap_seconds: null });
    state.gapSource = result.source || '';
    state.gapOverridden = false;
    await loadGapConfig(state.sessionId);
    renderPicker();
    renderAll();
    toast(`已恢复全局默认：${formatGap(state.gapSeconds)}`);
  } catch (error) {
    toast(`恢复默认失败：${error.message}`, true);
  }
}

// ----------------------------------------------------------- 加入主题（#4）
/**
 * 工作台里的「加入主题」快捷入口。
 *
 * ## 这里为什么只是**一个转发**
 *
 * 用户的原话是：
 *
 * > 加入主题的逻辑不对，主题点进去我要选我精选过的照片，
 * > 或者直接按照已有的任何物理目录或者主题来选。
 *
 * 也就是说：真正的主流程在**主题页**（「给这个主题添点片子」），而不是
 * 在工作台里找到一张，再回答放进哪个主题。所以对话框
 * （js/pick_modal.js：三个来源 = 我的精选 / 物理目录 / 已有主题）是**共用**的，
 * 工作台这里只是带着当前这一张预勾好打开它 —— 保留顺手的那条路，
 * 但不再是唯一的一条。
 */
function openAddToCollection() {
  const shot = state.shots[state.index];
  if (!shot) {
    toast('先选一个 Shot', true);
    return;
  }
  openPickModal({
    presetShotIds: [shot.id],
    presetShotsData: [{
      shotId: Number(shot.id),
      assetId: Number(shot.primary_asset_id),
      path: shot.primary_path,
      when: shot.captured_at,
      status: shot.selection_status || 'UNREVIEWED',
    }],
  });
}

// --------------------------------------------------------------------- 渲染
/** 阈值的人类可读形式（1 s 而不是 1.0 s）。 */
function formatGap(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '—';
  return `${Number.isInteger(value) ? value : value.toFixed(1)} s`;
}

/**
 * KPI 状态条：进度 + 四态计数 + 计时。
 *
 * 设计取舍（Feihong Console 场景）：
 *   · 数字用**衬线大字号**（Playfair）承担"仪表盘"的权威感，标签用 12px 中性灰；
 *   · 进度条金填充 + 皇家蓝轨道（金 = 高亮，不是"更好"）；
 *   · 状态计数**不做成评分**：只有数字和名字，没有星级、没有"推荐"（PRD §9.2 铁律）。
 */
function renderStatus() {
  const node = $('#review-status');
  if (!node) return;
  const detail = state.detail;
  if (!detail) {
    // ⚠️ 这个分支在**第一次使用时**就会碰到：刚 `photoos init` 完、
    // 分组还没跑过，所有时段都是 "0 Shot / 0 Scene"。
    // 光写"先选择一个 Scene"是不够的 —— 用户会以为**自己点错了**，
    // 而真实原因是**后端还没有派生数据**。所以要区分这两种情况，并给出可照做的下一步。
    const allEmpty = state.sessions.length > 0
      && state.sessions.every((session) => !session.shots && !session.scenes);
    const regroupAction = catalogAction('regroup');
    const canRegroup = !!(regroupAction && regroupAction.available);
    node.innerHTML = allEmpty
      ? '<div class="wb-empty"><span class="fleuron">❦</span>'
        + '<p>照片已索引，但<b>还没有分好组</b>（上面每个时段都显示 0 Shot / 0 Scene）。</p>'
        + '<p>分组要按「时段 → 拍摄组 → 场景」三层顺序算一遍，点下面这个按钮交给后台；'
        + '它只改数据库里的分组关系，<b>不移动、不复制、不删除任何照片</b>。</p>'
        + `<div class="not-ready-actions">
             <button class="btn primary" id="review-regroup"${canRegroup ? '' : ' disabled'}
                     title="${canRegroup ? '在后台依次执行：时段回填 → 拍摄组重算 → 场景重算' : '命令目录未载入'}">
               现在分组
             </button>
             <button class="btn small" id="review-regroup-more">其它可用命令…</button>
           </div>`
        + '<div id="review-regroup-progress" class="not-ready-progress"></div>'
        // 命令行收进折叠区：想自己动手或想只重算其中一层的人仍然够得着
        + '<details class="not-ready-manual"><summary>我想自己在命令行跑（或只重算其中一层）</summary>'
        + '<p class="hint tiny">三步顺序不能换：<code>shots regroup</code> 落库前要求时段已回填，'
        + '而 <code>scenes rebuild</code> 又建立在拍摄组之上。三步都幂等，可以反复跑。</p>'
        + '<pre class="cmd-block">photoos sessions backfill --apply\n'
        + 'photoos shots regroup --apply\n'
        + 'photoos scenes rebuild --apply</pre></details>'
        + '<p class="hint tiny">跑完刷新此页即可开始筛选。按 <kbd>?</kbd> 看快捷键。</p></div>'
      : '';

    // ⚠️ 未选场景时，**不要**在状态区再画一份空态：`#review-viewer` 已经有一份引导，
    // 一屏两个 ❦ + 两段"去挑一个"既重复又显得页面是空的。
    // 只有"已索引但还没分组"那种真需要动作的情况才占用这里（allEmpty 分支）。
    node.hidden = !allEmpty;
    node.style.display = allEmpty ? '' : 'none';

    // 「现在分组」：一个按钮跑完三层（入队 `regroup` 任务，一条进度、可暂停）
    const regroupButton = $('#review-regroup');
    if (regroupButton && canRegroup) {
      regroupButton.onclick = async () => {
        const job = await runActionWithProgress(regroupAction, $('#review-regroup-progress'), {
          button: regroupButton,
          label: '正在分组',
        });
        if (job && job.status === 'COMPLETED') {
          // 分组结果直接改变这一页的内容 —— 立刻重载，不要让用户自己去刷新
          await refreshReview();
          toast('分组完成');
        }
      };
    }
    const regroupMore = $('#review-regroup-more');
    if (regroupMore) {
      regroupMore.onclick = () => openActionsDialog({ title: '可直跑的命令', group: '分组' });
    }
    return;
  }
  node.hidden = false;
  node.style.display = '';
  const counts = detail.counts || {};
  const total = state.shots.length;
  const reviewed = total - (counts.UNREVIEWED || 0);
  const elapsed = state.startedAt ? (Date.now() - state.startedAt) / 1000 : 0;
  const percent = total ? Math.round((reviewed / total) * 100) : 0;
  // 标题用人话：城市 + 时间范围。原始 Scene 名是一长串目录路径，读起来费劲。
  const city = detail.city || String(detail.name || '').split('\\')[0] || '未命名场景';
  const when = detail.captured_from
    ? `${String(detail.captured_from).slice(0, 16)} → ${String(detail.captured_to || '').slice(11, 16)}`
    : String(detail.name || '');
  node.innerHTML = `
    <div class="review-summary">
      <div class="kpi-title-wrap">
        <span class="kpi-city">${escapeHtml(city)}</span>
        <span class="kpi-when">${escapeHtml(when)} · ${total} Shot（相似组 ${
          currentGroups().length} 个）</span>
      </div>
      <div class="kpi-progress">
        <div class="kpi-bar"><i style="width:${percent}%"></i></div>
        <div class="kpi-bar-label">
          <span>已审 ${reviewed}/${total}</span>
          <span>邻接 ${formatGap(state.gapSeconds)}${
            state.gapOverridden ? '（本时段）' : ''}</span>
        </div>
      </div>
      <div class="kpi-stats">
        ${Object.entries(state.labels).map(([key, label]) => `
          <span class="kpi-stat is-${key.toLowerCase()}">
            <b>${counts[key] || 0}</b><span>${escapeHtml(label)}</span>
          </span>`).join('')}
      </div>
      <div class="kpi-meta">
        本次用时 ${elapsed.toFixed(0)} 秒 · ${state.keystrokes} 次击键<br>
        <span title="界面近似分组的时间邻接阈值；可用上方数字框或 , / . 调整。${
          escapeHtml(state.gapSource || '')}">目标：一个场景 10 分钟内过完</span>
      </div>
    </div>`;
}

function renderViewer() {
  const node = $('#review-viewer');
  if (!node) return;
  // 未选场景时右侧「相似组」是一整列空说明，占着半个屏幕没有信息量 —— 收起来。
  const side = document.querySelector('.review-side');
  if (side) {
    side.hidden = !state.sceneId;
    side.style.display = state.sceneId ? '' : 'none';
  }
  const shot = state.shots[state.index];
  if (!shot) {
    // 两种"空的"要分清：还没选场景（提示去挑），和选了一场但里面真没 Shot（异常/空场景）。
    // 以前两种情况共用"这个 Scene 里没有 Shot" —— 刚进页面看到这句会以为库坏了。
    // 未选场景 → 复用 `reviewEmptyHtml()` 的三步引导（**唯一**一份空态文案，
    // 免得这里和状态区各写一句、两句还不一样）；选了场景但空 → 说明这一场确实没有。
    node.innerHTML = state.sceneId
      ? '<div class="wb-empty"><span class="fleuron">❦</span>'
        + '<p>这个 Scene 里没有可处理的 Shot。</p></div>'
      : reviewEmptyHtml();
    return;
  }

  if (state.mode === 'compare') {
    const group = state.compareGroup.length ? state.compareGroup : groupOf(state.index);
    const cards = group.map((index) => {
      const item = state.shots[index];
      const active = index === state.index ? ' is-current' : '';
      return `
        <figure class="compare-card${active}" data-index="${index}">
          <img ${assetImgAttrs({ assetId: item.primary_asset_id, size: 'preview', loading: 'eager' })}>
          <figcaption>
            <span class="cmp-name">${escapeHtml(baseName(item.primary_path || ''))}</span>
            <span class="cmp-when">${escapeHtml(formatWhen(item.captured_at))}</span>
            <span class="cmp-badge sel-badge ${selectionClass(item.selection_status)}">
              ${escapeHtml(selectionLabel(item.selection_status, state.labels))}</span>
            ${item.reason ? `<span class="cmp-reason">${escapeHtml(item.reason)}</span>` : ''}
          </figcaption>
          <div class="cmp-actions">
            <button class="btn small" data-mark="CANDIDATE">1 候选</button>
            <button class="btn small" data-mark="SELECTED">2 精选</button>
            <button class="btn gold small" data-mark="FINAL">3 最终</button>
          </div>
        </figure>`;
    }).join('');
    node.innerHTML = `
      <div class="compare-head">
        <span>相似组 · ${group.length} 张</span>
        <button class="btn small" id="compare-exit">返回单张</button>
      </div>
      <div class="compare-strip">${cards}</div>
      <p class="hint tiny" style="padding:0 16px 14px">
        并排看差异。这里是<strong>你的判断</strong>：系统只把竞争照片摆在一起，
        不比较优劣、不替你选。
      </p>`;
    const exit = $('#compare-exit');
    if (exit) exit.onclick = () => { state.mode = 'single'; renderViewer(); };
    node.querySelectorAll('.compare-card').forEach((card) => {
      card.onclick = (event) => {
        const index = Number(card.dataset.index);
        const mark = event.target.closest('[data-mark]');
        if (mark) {
          setStatus(index, mark.dataset.mark);
          return;
        }
        state.index = index;
        renderAll();
      };
    });
    return;
  }

  // 单张大图
  const group = groupOf(state.index);
  const position = group.indexOf(state.index) + 1;
  node.innerHTML = `
    <div class="viewer-head">
      <span class="viewer-name">${escapeHtml(baseName(shot.primary_path || ''))}</span>
      <span class="sel-badge ${selectionClass(shot.selection_status)}">
        ${escapeHtml(selectionLabel(shot.selection_status, state.labels))}</span>
      <span class="muted">第 ${state.index + 1}/${state.shots.length} 个 Shot
        · 本相似组 ${position}/${group.length}</span>
      <span class="spacer"></span>
      <button class="btn small" id="viewer-compare">比较本组 <kbd>C</kbd></button>
    </div>
    <div class="viewer-stage">
      <img ${assetImgAttrs({ assetId: shot.primary_asset_id, size: 'preview', loading: 'eager' })}>
    </div>
    <div class="viewer-meta">
      <span>${escapeHtml(formatWhen(shot.captured_at))}</span>
      <span>资产 ${shot.asset_count} 个（${escapeHtml(shot.primary_type || '')}）</span>
      <span>分组置信度 ${shot.group_confidence === null || shot.group_confidence === undefined
        ? '—' : Number(shot.group_confidence).toFixed(2)}</span>
      ${shot.reason ? `<span>理由：${escapeHtml(shot.reason)}</span>` : ''}
    </div>
    ${shot.group_confidence !== null && shot.group_confidence < 0.5
      ? '<div class="notice">这一组是「时间紧邻但镜头或焦段变过」——可能是两次不同的拍摄，建议人工确认。</div>'
      : ''}
    <div class="viewer-actions">
      <button class="btn mark" data-mark="CANDIDATE"><kbd>1</kbd> 候选</button>
      <button class="btn primary mark" data-mark="SELECTED"><kbd>2</kbd> 精选</button>
      <button class="btn gold mark" data-mark="FINAL"><kbd>3</kbd> 最终精选</button>
      <button class="btn ghost mark" data-mark="UNREVIEWED"><kbd>0</kbd> 退回</button>
      <button class="btn ghost mark" id="viewer-reason"><kbd>R</kbd> 写理由</button>
    </div>`;
  const compare = $('#viewer-compare');
  if (compare) compare.onclick = enterCompare;
  const reason = $('#viewer-reason');
  if (reason) reason.onclick = promptReason;
  node.querySelectorAll('[data-mark]').forEach((button) => {
    button.onclick = () => setStatus(state.index, button.dataset.mark);
  });
}

/** 右侧相似组列表：让用户看到"这个 Scene 里有哪些竞争组"。 */
function renderGroups() {
  const node = $('#review-groups');
  if (!node) return;
  // 没选场景时这一块与主空态说的是同一件事（同一屏出现两个 ❦ + 两段"去挑一个"），
  // 直接隐藏；选了场景但确实没有相邻组时才显示"没有可用分组"。
  if (!state.sceneId) {
    // ⚠️ 只设 `hidden` 不够：`.review-groups { display: flex }` 的优先级
    // 高于 UA 的 `[hidden] { display: none }`，元素照样显示（本项目踩过同类坑）。
    node.hidden = true;
    node.style.display = 'none';
    node.innerHTML = '';
    return;
  }
  node.hidden = false;
  node.style.display = '';
  const groups = currentGroups();
  node.innerHTML = groups.map((group, groupIndex) => {
    const { shown, before, after } = thumbsToShow(group, state.index);
    const thumbs = shown.map((index) => {
      const item = state.shots[index];
      const current = index === state.index ? ' is-current' : '';
      return `<button class="grp-thumb${current}" data-index="${index}" data-shot-id="${item.id}"
        title="${escapeHtml(baseName(item.primary_path || ''))}">
        <img ${assetImgAttrs({ assetId: item.primary_asset_id })}>
        <span class="grp-dot ${selectionClass(item.selection_status)}"></span>
      </button>`;
    }).join('');
    // `+N` 指示器出现在它对应的那一侧（前面被移出窗口的放左边），
    // 这样"还有更多"的位置本身就是方向信息。
    const more = (count) => (count
      ? `<span class="grp-more" title="这一组还有 ${count} 张没铺在这里；用 ← → 逐张看">+${count}</span>`
      : '');
    return `
      <div class="grp ${group.includes(state.index) ? 'is-active' : ''}">
        <div class="grp-head">
          <span class="grp-title">组 ${String(groupIndex + 1).padStart(2, '0')}${
            group.length > 1 ? ` · ${group.length} 张` : ''}</span>
          ${group.length > 1 ? '<span class="grp-tag">竞争</span>' : ''}
        </div>
        <div class="grp-thumbs">${more(before)}${thumbs}${more(after)}</div>
      </div>`;
  }).join('') || '<div class="hint">没有可用分组。</div>';
  node.querySelectorAll('.grp-thumb').forEach((button) => {
    button.onclick = () => {
      state.index = Number(button.dataset.index);
      renderAll();
    };
  });
  // 当前项必须留在视野里 —— 否则"我在哪"就丢了（见 thumbsToShow 的说明）。
  keepCurrentThumbVisible(node);
}

/** 侧栏一组最多铺几个缩略图。 */
const GROUP_THUMB_LIMIT = 8;

/**
 * 决定一组里**铺哪几张**缩略图。
 *
 * ⚠️ 这里原来是 `group.slice(0, 6)` —— 一个纯粹的截断。它有两个真实后果
 * （2026-10 修，用户报"点最后一张会跳回原位"）：
 *
 *   1. **当前项可能整张不存在。** 一个 9 张的组只铺前 6 张，走到第 7 张时
 *      侧栏里**没有任何缩略图带 `is-current`** —— 用户在侧栏里找不到自己在哪。
 *   2. **滚动位置会被重置。** 列表是整块 `innerHTML` 重建的；当"原来的高亮项"
 *      在新一轮里不存在时，浏览器丢掉了滚动锚点，侧栏就回到顶部。
 *      这正是"跳回原位"的来源。
 *
 * 所以现在是**滑动窗口**而非常量截断：窗口跟着当前项走，
 * 当前项永远在窗口内，两侧各留一点上下文（前面 1 张、后面若干）。
 * 被移出窗口的张数用 `+N` 如实标出，不静默丢弃。
 *
 * 返回的 `before`/`after` 是"窗口外、但在当前项**之前/之后**"的张数 ——
 * 分开放才能让 `+N` 指示器出现在视觉上正确的一侧。
 */
function thumbsToShow(group, currentIndex) {
  const n = group.length;
  if (n <= GROUP_THUMB_LIMIT) return { shown: group, before: 0, after: 0 };

  const at = group.indexOf(currentIndex);
  const limit = GROUP_THUMB_LIMIT;
  let start;
  if (at < 0) {
    start = 0;                                  // 当前项不在这一组：铺开头
  } else {
    // 尽量把当前项放在第 2 格：能看到"上一张 + 后面几张"
    start = Math.max(0, Math.min(at - 1, n - limit));
  }
  const shown = group.slice(start, start + limit);
  return { shown, before: start, after: n - (start + limit) };
}

/** 把 `is-current` 那个缩略图滚进侧栏视野（只在它确实不可见时动，避免无谓跳动）。 */
function keepCurrentThumbVisible(root) {
  const side = $('.review-side');
  const current = root.querySelector('.grp-thumb.is-current');
  if (!side || !current) return;
  const sr = side.getBoundingClientRect();
  const cr = current.getBoundingClientRect();
  const pad = 12;
  if (cr.top >= sr.top + pad && cr.bottom <= sr.bottom - pad) return;   // 已经看得见
  // `block: 'nearest'` 是"最小移动"：不会把列表拽回顶部
  current.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function renderAll() {
  renderStatus();
  renderViewer();
  renderGroups();
}

// --------------------------------------------------------------------- 数据
async function loadSessions() {
  const payload = await API.get('/api/sessions');
  state.sessions = payload.items;
}

async function loadScenes(sessionId) {
  // ⚠️ **必须显式给 limit**：`/api/scenes` 的默认上限是 500，超过就**静默截断** ——
  // 界面会"少掉一部分 Scene"却毫无提示（筛选计数还会煞有介事地写「显示 12 / 500」）。
  // 本库现在 303 个 Scene，但这是个会随拍摄增长的数字，所以直接取接口允许的上限（5000）。
  const query = sessionId ? `?session_id=${sessionId}&limit=5000` : '?limit=5000';
  const payload = await API.get(`/api/scenes${query}`);
  state.scenes = payload.items;
}

/**
 * 取"界面近似分组"的阈值：本 Session 覆盖值优先，否则全局默认。
 *
 * 预设清单只取一次（它不随 Session 变），但 `effective` 必须**每次选 Session 都重取** ——
 * 不同时段可以有各自的阈值。
 */
async function loadGapConfig(sessionId) {
  if (!state.gapPresets.length) {
    try {
      const config = await API.get('/api/review/config/presets');
      state.gapPresets = config.presets || [];
      state.gapGlobalDefault = config.global_default;
      state.gapLimitation = config.limitation || '';
      if (Number.isFinite(Number(config.global_default))) {
        state.gapSeconds = Number(config.global_default);
      }
    } catch (error) {
      // 取不到就退回默认值继续用，不让整页打不开
      state.gapPresets = [];
    }
  }
  if (!sessionId) return;
  try {
    const config = await API.get(`/api/sessions/${sessionId}/review-config`);
    if (Number.isFinite(Number(config.effective))) state.gapSeconds = Number(config.effective);
    state.gapSource = config.source || '';
    // ⚠️ 用**字段**判断"这个时段有没有单独设过"，不要靠 UI 文案里有没有"覆盖"两个字。
    // 先前是 `gapSource.includes('覆盖')` —— 那样一旦有人改了措辞（本次就改了），
    // "恢复默认"按钮会无声消失、KPI 上的"（本时段）"标记也不再出现。
    state.gapOverridden = config.override !== null && config.override !== undefined;
  } catch (error) {
    state.gapSource = '';
    state.gapOverridden = false;
  }
}

async function selectSession(value) {
  // ⚠️ **必须转成数字**。`<select>` 的 `value` 是**字符串**，而 `session.id` 是数字；
  // `renderPicker()` 里用的是 `session.id === state.sessionId`（严格相等），
  // 传字符串进来会让**每个选项都不匹配** —— 于是用户选完时段，下拉框又跳回「（全部）」，
  // 看起来就是"我选了但它显示的还是全部"（真实用户报的第一个问题）。
  // 同一类错误在本项目已经出现过一次（`browseScope()` 把轴名当路径），
  // 所以这里在**入口处**归一化，不指望每个调用方都记得加 `Number()`。
  state.sessionId = value ? Number(value) : null;
  // 先取阈值再画分组：顺序反了会先用上一个 Session 的阈值画一遍，出现闪变
  await loadGapConfig(state.sessionId);
  await loadScenes(state.sessionId);
  state.sceneId = null;
  state.detail = null;
  state.shots = [];
  state.datePopOpen = false;
  renderPicker();
  renderAll();
}

async function selectScene(sceneId) {
  if (!sceneId) return;
  state.sceneId = sceneId;
  const detail = await API.get(`/api/scenes/${sceneId}`);
  state.detail = detail;
  state.shots = detail.shots;
  // 用户可能只选了 Scene 而没选时段。Scene 自己知道属于哪个 Session，
  // 所以用它补上 —— 否则"调阈值后写回"会因为 sessionId 为空而**静默不保存**。
  // 必须**先**取阈值再往下算分组：顺序反了会先用旧阈值画一遍分组。
  if (detail.session_id && !state.sessionId) {
    state.sessionId = Number(detail.session_id);
    await loadGapConfig(state.sessionId);
    renderPicker();
  }
  // 从第一张**未审阅**的开始 —— 回到工作台时不该重看已经定过的
  const firstUnreviewed = state.shots.findIndex((shot) => shot.selection_status === 'UNREVIEWED');
  state.index = firstUnreviewed >= 0 ? firstUnreviewed : 0;
  state.mode = 'single';
  state.startedAt = state.startedAt || Date.now();
  renderAll();
}

/**
 * 供其它视图调用的入口：**切到工作台并打开某个 Scene**。
 *
 * 为什么要有它：Phase 3 的结构化筛选（`find`）会列出「你那次博物馆的 2 个 Scene」，
 * 用户点一下就该去筛它 —— 这比让他记住 Scene 编号、再手动在下拉里找要省事得多
 * （本项目最忌讳"功能在、路不通"，见 PHASE2_DELIVERY §12.4）。
 *
 * 调用方（`search.js`）**必须先切好视图**：本函数只负责选 Scene，
 * 不负责 `setView` —— 那会形成模块间的循环依赖。
 */
export async function openScene(sceneId) {
  await selectScene(Number(sceneId));
}

async function refreshScene() {  if (!state.sceneId) return;
  const detail = await API.get(`/api/scenes/${state.sceneId}`);
  state.detail = detail;
  state.shots = detail.shots;
}

// --------------------------------------------------------------------- 动作
async function setStatus(index, status, reason) {
  const shot = state.shots[index];
  if (!shot) return;
  state.keystrokes += 1;
  try {
    const result = await API.post(`/api/shots/${shot.id}/selection`, { status, reason });
    shot.selection_status = result.status;
    if (reason !== undefined) shot.reason = result.reason;
    if (state.detail) state.detail.counts = result.counts;
    renderAll();
    // 标完一个"决策"后自动前进到**下一个未审阅**的 —— 这是"≤10 分钟"的关键
    if (status !== 'UNREVIEWED') advance();
  } catch (error) {
    toast(`标记失败：${error.message}`, true);
  }
}

function advance() {
  const nextIndex = state.shots.findIndex(
    (shot, index) => index > state.index && shot.selection_status === 'UNREVIEWED',
  );
  if (nextIndex >= 0) {
    state.index = nextIndex;
  } else if (state.index < state.shots.length - 1) {
    state.index += 1;
  }
  renderAll();
}

function move(delta) {
  state.keystrokes += 1;
  const target = state.index + delta;
  if (target < 0 || target >= state.shots.length) return;
  state.index = target;
  renderAll();
}

function jumpToGroupEdge(edge) {
  const group = groupOf(state.index);
  state.keystrokes += 1;
  state.index = group[edge === 'start' ? 0 : group.length - 1];
  renderAll();
}

/** 跳过整组：找到当前组之后的第一张。 */
function skipGroup() {
  const groups = currentGroups();
  const currentGroupIndex = groups.findIndex((group) => group.includes(state.index));
  state.keystrokes += 1;
  if (currentGroupIndex < 0 || currentGroupIndex >= groups.length - 1) return;
  state.index = groups[currentGroupIndex + 1][0];
  renderAll();
}

/** 快进：下一张未审阅的（跨组）。 */
function nextUnreviewed() {
  state.keystrokes += 1;
  const next = state.shots.findIndex(
    (shot, index) => index > state.index && shot.selection_status === 'UNREVIEWED',
  );
  if (next >= 0) {
    state.index = next;
  } else {
    const wrap = state.shots.findIndex((shot) => shot.selection_status === 'UNREVIEWED');
    if (wrap < 0) {
      toast('这个 Scene 已经全部审过了');
      return;
    }
    state.index = wrap;
  }
  renderAll();
}

function enterCompare() {
  state.compareGroup = groupOf(state.index);
  state.mode = 'compare';
  renderViewer();
}

async function promptReason() {
  const shot = state.shots[state.index];
  if (!shot) return;
  // 预置三个 PRD 里明写的例子，减少打字成本
  const preset = ['光最好', '构图最好', '最有氛围'];
  const answer = prompt(
    `给「${baseName(shot.primary_path || '')}」写一句简短理由\n（可留空清除；例如：${preset.join(' / ')}）`,
    shot.reason || '',
  );
  if (answer === null) return;
  try {
    const result = await API.post(`/api/shots/${shot.id}/selection`, {
      status: shot.selection_status,
      reason: answer,
    });
    shot.reason = result.reason;
    renderAll();
    toast('理由已记录');
  } catch (error) {
    toast(`记录失败：${error.message}`, true);
  }
}

// ------------------------------------------------- 人工调整分组（拆分 / 合并）
/**
 * 调整模式：列出当前组的**每一张**，勾选要拆出去的那些。
 *
 * PRD §9.2 要求"所有自动归组都允许用户调整"，所以这里必须能真的改，
 * 而不只是显示一个"分组仅供参考"的提示。
 *
 * 本期刻意不做"拖拽排序"之类的重交互：勾选 + 一次确认，键盘可达，
 * 也避免用户误以为分组是必须整理的东西（分组只是**决策单元**）。
 */
function openAdjust() {
  const shot = state.shots[state.index];
  if (!shot) return;
  API.get(`/api/shots/${shot.id}/assets`)
    .then((payload) => {
      state.adjust = { shotId: shot.id, items: payload.items, picked: new Set() };
      renderAdjust();
    })
    .catch((error) => toast(`读取分组失败：${error.message}`, true));
}

/**
 * 只刷新"勾选态"相关的部分：行高亮、按钮可用性、按钮文案。
 *
 * **不重建 DOM** —— 这一点是被真实浏览器验证逼出来的：整块 `innerHTML` 重建后，
 * 任何仍持有旧节点的引用（自动化脚本的循环、快速连点）都会作用在**已脱离文档**的
 * 元素上，表现为"点了没反应"。局部更新从根上消除这类失配。
 */
function syncAdjustChrome() {
  const adjust = state.adjust;
  if (!adjust) return;
  document.querySelectorAll('#review-adjust-panel .adjust-row').forEach((row) => {
    const box = row.querySelector('input[data-asset]');
    if (box) row.classList.toggle('picked', box.checked);
  });
  const total = adjust.items.length;
  const picked = document.querySelectorAll(
    '#review-adjust-panel input[data-asset]:checked').length;
  const button = $('#adjust-split');
  if (!button) return;
  button.textContent = `拆出 ${picked} 张为新组`;
  button.disabled = !(total >= 2 && picked > 0 && picked < total);
  const hint = $('#adjust-reason');
  if (hint) {
    hint.textContent = total < 2
      ? ' —— 这个组只有 1 张，没有可拆的余地。'
      : (picked === total && total > 0
        ? ' —— 不能把整组都拆走，那等于整组移动，请改用合并。'
        : '。');
  }
}

function renderAdjust() {
  const node = $('#review-adjust-panel');
  if (!node) return;
  const adjust = state.adjust;
  if (!adjust) { node.hidden = true; return; }
  const shot = state.shots[state.index];
  const rows = adjust.items.map((item) => `
    <label class="adjust-row${adjust.picked.has(item.id) ? ' picked' : ''}">
      <input type="checkbox" data-asset="${item.id}"${adjust.picked.has(item.id) ? ' checked' : ''}>
      <img ${assetImgAttrs({ assetId: item.id, loading: 'eager' })}>
      <span class="adjust-name">${escapeHtml(item.name)}</span>
      <span class="hint tiny">${escapeHtml(formatWhen(item.captured_at))}</span>
    </label>`).join('');
  node.innerHTML = `
    <div class="panel-head">
      <h2>调整分组 · ${escapeHtml(shot ? baseName(shot.primary_path || '') : '')}</h2>
      <button class="btn small" id="adjust-cancel">取消 (Esc)</button>
    </div>
    <p class="hint tiny">
      共 ${adjust.items.length} 张。拆分后原组与新组都会被标记为<strong>人工调整</strong>，
      以后重算不再改动它们。勾选"要拆出去"的照片<span id="adjust-reason"></span>
    </p>
    <div class="adjust-grid">${rows}</div>
    <div class="panel-actions">
      <button class="btn small" id="adjust-select-odd">取奇数张</button>
      <button class="btn small" id="adjust-clear">清空</button>
      <button class="btn primary" id="adjust-split">拆出 0 张为新组</button>
      <button class="btn small" id="adjust-merge">与下一组合并</button>
    </div>`;
  node.hidden = false;

  // 事件委托到容器上：DOM 不再重建，所以不需要每次重新绑定
  const grid = node.querySelector('.adjust-grid');
  if (grid) {
    grid.onchange = (event) => {
      const box = event.target;
      if (!box || !box.dataset || box.dataset.asset === undefined) return;
      const id = Number(box.dataset.asset);
      if (box.checked) adjust.picked.add(id); else adjust.picked.delete(id);
      syncAdjustChrome();
    };
  }
  const cancel = $('#adjust-cancel');
  if (cancel) cancel.onclick = closeAdjust;
  const clear = $('#adjust-clear');
  if (clear) {
    clear.onclick = () => {
      adjust.picked.clear();
      node.querySelectorAll('input[data-asset]').forEach((box) => { box.checked = false; });
      syncAdjustChrome();
    };
  }
  const odd = $('#adjust-select-odd');
  if (odd) {
    odd.onclick = () => {
      adjust.picked.clear();
      node.querySelectorAll('input[data-asset]').forEach((box, i) => {
        const on = i % 2 === 0;
        box.checked = on;
        if (on) adjust.picked.add(Number(box.dataset.asset));
      });
      syncAdjustChrome();
    };
  }
  const split = $('#adjust-split');
  if (split) split.onclick = () => splitGroup(Array.from(adjust.picked));
  const merge = $('#adjust-merge');
  if (merge) merge.onclick = mergeWithNext;
  syncAdjustChrome();
}

function closeAdjust() {
  state.adjust = null;
  const node = $('#review-adjust-panel');
  if (node) { node.hidden = true; node.innerHTML = ''; }
}

/** 拆分：**拆出去**的是勾选的这些；剩下的留在原组。 */
async function splitGroup(assetIds) {
  const shot = state.shots[state.index];
  if (!shot || !assetIds.length) return;
  try {
    const result = await API.post(`/api/shots/${shot.id}/split`, { asset_ids: assetIds });
    closeAdjust();
    // 分组变了，计数/封面都过期了，必须重拉而不是原地改
    await refreshScene();
    renderAll();
    toast(`已拆出 ${result.moved} 张为新组（共 ${result.new_assets} 张）`);
  } catch (error) {
    toast(`拆分失败：${error.message}`, true);
  }
}

/** 合并：当前组与**时间上的下一组**并成一个。 */
async function mergeWithNext() {
  const groups = currentGroups();
  const at = groups.findIndex((group) => group.includes(state.index));
  if (at < 0 || at >= groups.length - 1) {
    toast('这是最后一组，没有可合并的下一组');
    return;
  }
  const here = state.shots[state.index];
  const next = state.shots[groups[at + 1][0]];
  if (!here || !next) return;
  // 同属当前 Scene 才可能出现在 `currentGroups()` 里，所以不必再判 Scene；
  // 后端仍会独立拒绝跨 Scene 合并（那是给直接调 API 的场景兜底）。
  try {
    const result = await API.post('/api/shots/merge', { shot_ids: [here.id, next.id] });
    closeAdjust();
    await refreshScene();
    renderAll();
    toast(`已合并为一组（共 ${result.assets} 张）`);
  } catch (error) {
    toast(`合并失败：${error.message}`, true);
  }
}

function showHelp() {
  const rows = KEYS_DOC.map(([key, what]) => `<tr><td><kbd>${key}</kbd></td><td>${what}</td></tr>`).join('');
  const node = $('#review-help-panel');
  if (!node) return;
  node.innerHTML = `
    <div class="panel-head"><h2>快捷键</h2>
      <button class="btn small" id="review-help-close">关闭</button></div>
    <table class="keys">${rows}</table>
    <p class="hint tiny">
      相似组按<strong>时间邻接</strong>切分（当前阈值 <strong>${escapeHtml(formatGap(state.gapSeconds))}</strong>，
      来源：${escapeHtml(state.gapSource || '默认值')}）：连拍与同一机位的连续取景会落进同一组，
      而<strong>组才是决策单元</strong>。按 <kbd>,</kbd> / <kbd>.</kbd> 可调，也会写回本时段。
    </p>
    <p class="hint tiny">
      ⚠️ 时间规则<strong>抓不住</strong>「构图很相似、但拍摄时间相隔较远」的照片
      —— 库里就有相隔 6 分钟、构图几乎一样的竞争照片。
      所以<strong>调大阈值不会"找全相似照片"</strong>，只是把更多独立的拍摄并成一个决策单元。
      真正需要判断时，用 <kbd>V</kbd> 手动合并 / 拆分，或按 <kbd>C</kbd> 并排比较。
    </p>`;
  node.hidden = false;
  const close = $('#review-help-close');
  if (close) close.onclick = () => { node.hidden = true; };
}

// --------------------------------------------------------------------- 键盘
function handleKey(event) {
  if (state.view !== 'review') return;
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') return;

  const key = event.key;
  const handlers = {
    ArrowLeft: () => move(-1),
    a: () => move(-1),
    A: () => move(-1),
    ArrowRight: () => move(1),
    d: () => move(1),
    D: () => move(1),
    ArrowUp: skipGroup,
    ArrowDown: () => jumpToGroupEdge('start'),
    '1': () => setStatus(state.index, 'CANDIDATE'),
    '2': () => setStatus(state.index, 'SELECTED'),
    '3': () => setStatus(state.index, 'FINAL'),
    '0': () => setStatus(state.index, 'UNREVIEWED'),
    Backspace: () => setStatus(state.index, 'UNREVIEWED'),
    r: promptReason,
    R: promptReason,
    c: enterCompare,
    C: enterCompare,
    ' ': nextUnreviewed,
    Spacebar: nextUnreviewed,
    ',': () => stepThreshold(-1),
    '.': () => stepThreshold(1),
    t: openAddToCollection,
    T: openAddToCollection,
    '?': showHelp,
    v: openAdjust,
    V: openAdjust,
  };
  // 调整面板打开时，Esc 先关面板而不是退出工作台
  if (key === 'Escape' && state.adjust) {
    event.preventDefault();
    closeAdjust();
    return;
  }
  // 加入主题对话框 / 日期弹窗同理：Esc 先收起它们
  if (key === 'Escape' && pickModalOpen()) {
    event.preventDefault();
    closePickModal();
    return;
  }
  if (key === 'Escape' && state.datePopOpen) {
    event.preventDefault();
    toggleDatePop();
    return;
  }
  const handler = handlers[key];
  if (!handler) return;
  event.preventDefault();
  handler();
}

/**
 * 用 `,` / `.` 微调"时间邻接阈值"（= 后端的 `shot_gap_seconds`，决策 #5 的界面层）。
 *
 * ⚠️ **步长修过一次**：原来写的是 `adjustThreshold(±0.01)` —— 0.01 秒的步长
 * 意味着按 100 次才动 1 秒；而且只有当后端返回了预设时才会"跳到下一个预设"，
 * 预设取不到时就退化成几乎按不动。现在：**有预设就跳预设，没有就 ±0.5 s**。
 *
 * 精确输入请用界面上那个数字框 + 「应用」（用户提出的第 3 点）。
 */
function stepThreshold(direction) {
  const presets = state.gapPresets.map((item) => Number(item.value)).sort((a, b) => a - b);
  let next;
  if (presets.length) {
    const candidates = direction > 0
      ? presets.filter((value) => value > state.gapSeconds + 1e-9)
      : presets.filter((value) => value < state.gapSeconds - 1e-9).reverse();
    next = candidates.length
      ? (direction > 0 ? Math.min(...candidates) : Math.max(...candidates))
      : Math.max(0.1, Number((state.gapSeconds + direction * 0.5).toFixed(2)));
  } else {
    next = Number((state.gapSeconds + direction * 0.5).toFixed(2));
  }
  applyGapValue(Math.max(0.1, next), '');
}

/** 把阈值写回本 Session；失败不阻塞界面（界面已经按新值画了）。 */
function persistGap(value) {
  if (!state.sessionId) {
    // 到这一步说明连 Scene 都没选，没有可归属的时段 —— 明说，不要静默丢弃
    toast('还没选时段/Scene，阈值只在本页生效，没能保存到某个时段', true);
    return;
  }
  API.patch(`/api/sessions/${state.sessionId}/review-config`, { shot_gap_seconds: value })
    .then(() => { state.gapSource = '本时段已设置'; state.gapOverridden = true; })
    .catch((error) => toast(`阈值没能写回后端：${error.message}`, true));
}

// --------------------------------------------------------------------- 对外
export function initReview() {
  if (state.started) return;
  state.started = true;
  document.addEventListener('keydown', handleKey);
}

export async function refreshReview() {
  try {
    await loadSessions();
    // 阈值要在画分组之前拿到，否则会先用兜底值画一遍再跳变。
    // 没有选中时段时它取全局默认（`loadGapConfig(null)` 本来就支持）。
    await loadGapConfig(state.sessionId);
    // ⚠️ 这里**不传 `state.sessionId`**，而是拉**全部场景**。
    // 为什么：入口第一层是"地点 + 拍摄日期"，不是时段 —— 时段只是可选的整体单元。
    // 实测 `/api/scenes` 不给 `session_id` 就返回全部 303 个，筛选在客户端做。
    // 若传了 sessionId，没选时段时就会只拿到 0 个场景，漏斗第一步就是空的。
    await loadScenes(null);
    // 排查钩子：**默认关闭**，需要时在控制台设 `localStorage.photoosDebug = '1'`
    // 再刷新。它记的是"渲染走到了哪一步 + 抛错的完整栈" ——
    // 实测一个渲染错误只报 "field is not defined"，没有栈就定位不到是哪一行。
    // 默认关闭是为了不给 `window` 留全局副作用（这个仓库明确禁止那种做法）。
    const debug = (() => {
      try { return localStorage.getItem('photoosDebug') === '1'; } catch { return false; }
    })();
    const mark = (patch) => {
      if (debug) window.__reviewBoot = { ...(window.__reviewBoot || {}), ...patch };
    };

    mark({ sessionsCount: state.sessions.length, scenesCount: state.scenes.length,
           pickerFound: Boolean(document.querySelector('#review-picker')), stage: 'before-renderPicker' });
    renderPicker();
    mark({ stage: 'after-renderPicker',
           pickerLen: (document.querySelector('#review-picker')?.innerHTML || '').length });
    renderAll();
    mark({ stage: 'after-renderAll' });
  } catch (error) {
    try {
      if (localStorage.getItem('photoosDebug') === '1') {
        window.__reviewBoot = {
          ...(window.__reviewBoot || {}), stage: 'THREW',
          error: String(error && (error.stack || error.message)),
        };
      }
    } catch { /* 隐私模式下 localStorage 不可用 —— 排查钩子不该影响主流程 */ }
    toast(`工作台加载失败：${error.message}`, true);
  }
}

/** `setView` 会写这个字段；键盘处理靠它判断"当前是不是工作台"。 */
export function setReviewViewActive(active) {
  state.view = active ? 'review' : null;
}

// ------------------------------------------------- Scene 的人工调整（合并 / 拆分）
//
// 为什么放在这一页：Scene 是这一页的「一场」，切错了人就在这里发现。
// 后端会把改过的 Scene 标成 `source='user'`，之后 `scenes rebuild` 会跳过这个时段
// —— 否则手工调整会被下一次重算冲掉。

function currentScene() {
  return state.scenes.find((scene) => Number(scene.id) === Number(state.sceneId)) || null;
}

/** 场景短名：目录名末段（与下拉框的显示口径一致）。 */
function sceneShortName(scene) {
  if (!scene) return '（未知场景）';
  const parts = String(scene.name || '').split('\\').filter(Boolean);
  return parts[parts.length - 1] || String(scene.name || '未命名');
}

/** 同一时段内、按拍摄时间排序的前一个 Scene（没有则 null）。 */
function previousScene() {
  const current = currentScene();
  if (!current) return null;
  const same = state.scenes
    .filter((scene) => scene.session_id === current.session_id)
    .sort((a, b) => String(a.captured_from || '').localeCompare(String(b.captured_from || '')));
  const at = same.findIndex((scene) => Number(scene.id) === Number(current.id));
  return at > 0 ? same[at - 1] : null;
}

async function mergeIntoPreviousScene() {
  const current = currentScene();
  const prev = previousScene();
  if (!current || !prev) {
    toast('上面没有可以合并的前一场（或当前不在某个场景里）', true);
    return;
  }
  if (!confirm(`把「${sceneShortName(current)}」并入前一场「${sceneShortName(prev)}」？\n\n`
    + '会保留前一场的 id，并把这一场的拍摄组搬过去。')) return;
  try {
    const result = await API.post('/api/scenes/merge', {
      primary_id: Number(prev.id), secondary_id: Number(current.id),
    });
    toast(`已合并，搬了 ${result.moved_shots} 个拍摄组`);
    await loadScenes(state.sessionId);
    await selectScene(Number(prev.id));
  } catch (error) {
    toast(`合并失败：${error.message}`, true);
  }
}

async function splitSceneAtCurrentShot() {
  const shot = state.shots[state.index];
  if (!state.sceneId || !shot) {
    toast('先选一个场景和一张照片', true);
    return;
  }
  if (!confirm(`把「${sceneShortName(currentScene())}」从这一张之后拆开？\n\n`
    + '这一张留在原场景，后面的照片会形成新场景。')) return;
  try {
    const result = await API.post(`/api/scenes/${state.sceneId}/split`, {
      after_shot_id: Number(shot.id),
    });
    toast(`已拆出 ${result.moved_shots} 个拍摄组`);
    await loadScenes(state.sessionId);
    await selectScene(Number(state.sceneId));
  } catch (error) {
    toast(`拆分失败：${error.message}`, true);
  }
}

export { selectScene, selectSession, setStatus, refreshScene };
