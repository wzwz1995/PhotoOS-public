/**
 * PhotoOS · Phase 1 前端（零构建 ES module）
 *
 * 设计取舍：
 *   - 没有构建链、没有框架、没有依赖 —— 直接用浏览器原生 ES module。
 *   - 网格是**虚拟列表**：只保留可视区域内的 DOM 节点并循环复用，
 *     因此 10 万张照片也不会一次创建 10 万个元素（PRD §11.2 的 Library 浏览要求）。
 *   - 缩略图懒加载 + 用 `preview` 表记录的宽高预留占位框，避免滚动时布局抖动。
 *   - 任何"删除原文件"的入口都不在这里（Phase 1 不提供该能力）。
 *
 * ⚠️ 这个模块是 Phase 1 的入口，自己带了一份 `$` / `toast` / `escapeHtml` 的
 * 本地实现（早于 `shared.js`）。**新代码不要从这里抄**：那几样在 `shared.js`
 * 里各只有一份。下面只从 shared.js 借了一个纯函数（加载态组件），
 * 因为"加载中长什么样"必须全应用一致，抄第二份必然漂移。
 */

import {
  baseName, bindRetryButtons, errorHtml, escapeHtml, loadingHtml, toast,
  SELECTION_LABELS,
} from './shared.js';
// 「当前在筛什么」的唯一落点：它是各页面 state 的**只读投影**，不引入新状态。
import { renderQueryBar } from './query_bar.js';
// 界面直跑命令：目录与执行都在 actions.js（三个入口共用一份实现）。
import { openActionsDialog, renderActionsPanel } from './actions.js';

const API = {
  async get(path) {
    const response = await fetch(path, { headers: { Accept: 'application/json' } });
    return handle(response);
  },
  async post(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return handle(response);
  },
  async patch(path, body) {
    const response = await fetch(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return handle(response);
  },
  async del(path) {
    const response = await fetch(path, { method: 'DELETE', headers: { Accept: 'application/json' } });
    return handle(response);
  },
};

async function handle(response) {
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { message: text }; }
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.detail)) || `HTTP ${response.status}`;
    const error = new Error(typeof message === 'string' ? message : JSON.stringify(message));
    error.payload = payload;
    throw error;
  }
  return payload;
}

// --------------------------------------------------------------------- 状态
/**
 * 浏览器能直接内联渲染的图片类型。必须与后端 `api/app.py` 的 `_INLINE_IMAGE_TYPES` 保持一致：
 * 其余格式（RAW / HEIC / TIFF）由 `/view` 回退到派生预览，界面据此如实标注。
 */
const INLINE_IMAGE_TYPES = new Set(['jpeg', 'png', 'webp']);

const state = {
  view: 'library',
  libraries: [],
  activeLibraryId: null,
  items: [],
  total: 0,
  offset: 0,
  pageSize: 300,
  loading: false,
  filters: {
    file_type: '', status: '', camera: '', lens: '',
    focalMin: '', focalMax: '',
    year: '', sort: 'capture',
    /**
     * **关键词**：跨字段的**纯文本**匹配（文件名 / 机型 / 镜头），不经过模型。
     * 与 `contentQ` 是**两个维度**，不能混：
     *   · `q`（这里）—— SQL 的 `LIKE`，找"名字里带这串字的"；
     *   · `contentQ` —— 模型算的画面相似度，找"看起来像这个的"。
     * 同一个框里塞两种语义会让用户不知道自己在搜什么。
     */
    q: '',
    /**
     * **内容词**（"像日落的照片"）。走 `/api/semantic/results` 的全库相似度，
     * 与元数据/人物条件是**叠加**关系 —— 这是多维筛选的核心。
     */
    contentQ: '',
    /** 人物 id 数组。**多个是 AND**（合影），不是 OR。见 `compose.person_clause`。 */
    personIds: [],
    noMeta: false,
  },
  /** 内容词候选（后端给，前端不硬编码）与已缓存的人物名单。 */
  contentWords: [],
  people: [],
  /**
   * 浏览轴状态（默认 `path` = 物理路径）。
   *
   * 模型刻意保持三层就够：`chain` 是已下钻的节点链，`nodes` 是链尾的子节点，
   * `selectedKey` 是当前作用域。效果是"列浏览器"：
   *   全部 › 上饶市 › 2026-09-06 12:15–14:04
   * Phase 2 的 `selection`/`collection`/`tag` 轴复用同一套状态与同一个
   * `scope_kind` + `scope_key` 参数，前端不需要改结构。
   */
  browse: {
    axis: 'path',
    chain: [],
    nodes: [],
    selectedKey: null,
    selectedLabel: null,
    loading: false,
  },
  jobs: [],
  runningJobId: null,
  pollTimer: null,
  /** 最近一次导入预演的请求参数（执行时原样复用，保证"看到的计划"就是"执行的计划"）。 */
  importPlan: null,
  /** 由「确认执行」创建的那个导入任务 id（只用于在导入卡片里回显进度）。 */
  importJobId: null,
};

// --------------------------------------------------------------------- 工具
//
// ⚠️ 这里原来各有一份 `$` / `toast` / `humanBytes` / `escapeHtml` / `baseName` /
// `parentPath` / `API` 的**副本**，与 `shared.js` 并存。文件头的注释一直在警告
// "新代码不要从这里抄"，但两份实现本身就是漂移的来源：
//   * `parentPath` 的**分隔符**两边不一致（这里返回 ` / `，而 `shared.js` 明确要求
//     保持 `\` —— "这一行是给用户拿去和硬盘上的目录对照的"）；它在本文件里
//     还是**死代码**（只在定义处出现过）。
//   * `toast` 少了 `shared.js` 那份的 `if (!node) return` 保护。
// 2026-10 全部改为从 `shared.js` 引入，删掉副本。
// `API` 因为带一个本文件专用的错误提示文案，暂时保留在下面（见它的注释）。

const $ = (selector) => document.querySelector(selector);

function humanBytes(value) {
  if (value === null || value === undefined) return '—';
  let size = Number(value);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${index === 0 ? size : size.toFixed(1)} ${units[index]}`;
}

// --------------------------------------------------------------------- 视图切换
/**
 * 各视图由自己的模块负责渲染（`review.js` / `collections.js` / `search.js` / `people.js`）。
 * 这里只做"切到它时通知它刷新"，避免把工作台的键盘与状态机塞进主文件。
 * 用动态 `import()` 是为了**不引入打包**也能按需加载。
 */
const VIEW_LABELS = {
  review: '精选',
  collections: '主题',
  search: '找照片',
  people: '人物',
  help: '说明',
};

async function activateLazyView(view) {
  try {
    if (view === 'review') {
      const mod = await import('./review.js');
      mod.initReview();
      mod.setReviewViewActive(true);
      await mod.refreshReview();
    } else if (view === 'collections') {
      const mod = await import('./collections.js');
      mod.initCollections();
      await mod.refreshCollections();
    } else if (view === 'search') {
      // 找照片：这一页**不跑模型**，只读命令行预先准备好的结果。
      const mod = await import('./search.js');
      mod.initSearch();
      await mod.refreshSearch();
    } else if (view === 'people') {
      const mod = await import('./people.js');
      mod.initPeople();
      await mod.refreshPeople();
    } else if (view === 'help') {
      const mod = await import('./help.js');
      mod.renderHelp($('#help-body'));
    }
  } catch (error) {
    toast(`${VIEW_LABELS[view] || view}加载失败：${error.message}`, true);
  }
}

function deactivateLazyView(view) {
  if (view !== 'review') return;
  import('./review.js')
    .then((mod) => mod.setReviewViewActive(false))
    .catch(() => { /* 键盘处理会因 state.view 不是 'review' 而自行忽略 */ });
}

/**
 * 供其它视图调用：**切到精选并直接打开某个 Scene**。
 *
 * 用在「找照片」的结构化筛选结果里（点「去筛这个 Scene」）。
 * 为什么需要它：让用户记住 Scene 编号再回工作台手选，就是"功能在、路不通"。
 */
export async function openWorkbench(sceneId) {
  setView('review');
  const mod = await import('./review.js');
  mod.initReview();
  mod.setReviewViewActive(true);
  await mod.refreshReview();
  if (sceneId) await mod.openScene(sceneId);
}

function setView(view) {
  const previous = state.view;
  state.view = view;
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach((section) => {
    section.classList.toggle('is-active', section.id === `view-${view}`);
  });
  if (previous !== view) deactivateLazyView(previous);
  // 「维护」把导入、后台任务、完整性与换盘合成一页 —— 它们都是"不常做但必须能做完"的事
  if (view === 'maintenance') {
    refreshJobs();
    refreshTools();
    refreshAbout();
  }
  if (view === 'library') {
    grid.relayout();
    // 筛选台只在照片库显示：它筛的是照片列表本身。
    // 切走时收起，免得回来时占着地方却想不起来是谁打开的。
    const panel = $('#lib-filter');
    if (panel) panel.hidden = false;
    renderFilterSummary();
  } else {
    const panel = $('#lib-filter');
    if (panel) panel.hidden = true;
  }
  if (['review', 'collections', 'search', 'people', 'help'].includes(view)) {
    activateLazyView(view);
  }
}

// --------------------------------------------------------------------- 虚拟网格
const grid = (() => {
  const scroll = $('#grid-scroll');
  const container = $('#grid');
  const GAP = 10;
  const pool = [];
  let cards = [];
  let columns = 0;
  let cardWidth = 220;
  let rowHeight = 180;
  let started = false;

  function measure() {
    const width = scroll.clientWidth - 28;
    columns = Math.max(1, Math.floor((width + GAP) / (220 + GAP)));
    cardWidth = Math.floor((width - GAP * (columns - 1)) / columns);
    const aspect = 2 / 3; // 首屏按竖构图估算；有宽高数据后会按真实比例排版
    rowHeight = Math.floor(cardWidth * aspect) + GAP;
  }

  function itemHeight(item) {
    // 竖构图占 1 格，横构图占 2 格宽 1 格高（简单瀑布流替代方案：等行高 + 跨列）
    return rowHeight;
  }

  function createCard() {
    const node = document.createElement('div');
    node.className = 'card';
    node.innerHTML = '<img loading="lazy" alt=""><div class="cap"><span class="name"></span><span class="type"></span></div>';
    return node;
  }

  function ensurePool(size) {
    while (pool.length < size) {
      const node = createCard();
      container.appendChild(node);
      pool.push(node);
    }
    while (pool.length > size && pool.length > 0) {
      const node = pool.pop();
      node.remove();
    }
  }

  function describe(item) {
    // ⚠️ 只取「月-日」：卡片一行只有约 200px，带年份和时分会被截断在日期中间
    // （`DSC_1894.JPG · 2026-10-…`），读起来像坏数据。完整时刻在详情弹窗里。
    const raw = item.capture_at || item.capture_date_local || '';
    const when = raw ? raw.slice(5, 10) : '无拍摄时间';
    return `${baseName(item.relative_path)} · ${when}`;
  }

  /**
   * 同一张照片的**多个文件** —— 这是照片库最需要说清楚的一件事。
   *
   * 库里一个拍摄组常含 JPG + NEF 两个文件（实测 2 个文件的组有 4,462 个），
   * 而 `/api/assets` 是**按文件**给的。改之前它们显示成两张一模一样的卡：
   * 网格长度凭空翻倍，画面上又分不清哪张是哪个文件 —— 用户看到的是"文件"，
   * 不是"照片"。
   *
   * ⚠️ 这里**只做视觉分组，不折叠**。把同组折叠成一个会破坏"边筛边翻看"：
   * 连拍组里挑哪一张正是要翻的东西（实测单组最多 56 个文件 / 28 个不同帧）。
   * 文件仍然逐个可见可点，只是看得出它们是一回事。
   *
   * ⚠️ 只在**相邻**项之间成组：`/api/assets` 按拍摄时间排序，同组文件本来就是
   * 相邻的（实测前 12 条是 JPG/NEF 成对出现）。不为了"找全同组"去全表扫描 ——
   * 那会把这个 10 万张也不卡的虚拟网格拖垮。
   */
  function fileGroup(index) {
    const items = state.items;
    const item = items[index];
    if (!item || item.shot_id === null || item.shot_id === undefined) return null;
    let start = index;
    let end = index;
    while (start > 0 && items[start - 1].shot_id === item.shot_id) start -= 1;
    while (end + 1 < items.length && items[end + 1].shot_id === item.shot_id) end += 1;
    const size = end - start + 1;
    if (size < 2) return null;
    return { position: index - start + 1, size };
  }

  /**
   * 网格里"最后画过的那张卡"。**仅供排查用**（`grid.lastPainted()`）——
   * 弹窗的占位图不要用它：它只是池子里最后一次 `paint()` 的那一格，
   * 与用户点的那张无关（实测点第 1 张时它是第 35 张）。
   * 占位图走 `findCachedThumb()`，从 DOM 里按 `data-asset` 找。
   */
  let lastPainted = null;

  function paint(node, item, index, left, top) {
    lastPainted = item;
    node.style.transform = `translate(${left}px, ${top}px)`;
    node.style.width = `${cardWidth}px`;
    node.style.height = `${rowHeight - GAP}px`;
    node.classList.toggle('is-missing', item.status === 'MISSING' || item.status === 'UNREADABLE');
    const image = node.querySelector('img');
    const thumbUrl = `/api/assets/${item.id}/thumb`;
    if (image.dataset.asset !== String(item.id)) {
      image.dataset.asset = String(item.id);
      if (item.has_thumb) {
        image.src = thumbUrl;
        image.style.visibility = 'visible';
      } else {
        image.removeAttribute('src');
        image.style.visibility = 'hidden';
      }
      image.alt = describe(item);
    }
    node.querySelector('.name').textContent = describe(item);
    // 角标：单个文件显示类型；一组多个文件时**额外标出它在这一组里的位置**
    // （"NEF · 1/2"）—— 用户才知道"这张照片还有另一个文件"、现在看的是哪个。
    const group = fileGroup(index);
    const type = String(item.file_type || '').toUpperCase();
    const typeNode = node.querySelector('.type');
    typeNode.textContent = group ? `${type} · ${group.position}/${group.size}` : type;
    typeNode.title = group
      ? `这张照片在库里有 ${group.size} 个文件（当前第 ${group.position} 个）`
      : '';
    node.classList.toggle('is-stacked', Boolean(group));
    node.onclick = () => openAssetDetail(item.id);
  }

  function render() {
    if (!started) return;
    const count = state.items.length;
    const viewportHeight = scroll.clientHeight;
    const totalRows = Math.ceil(count / columns);
    container.style.height = `${totalRows * rowHeight}px`;

    const scrollTop = scroll.scrollTop;
    const firstRow = Math.max(0, Math.floor(scrollTop / rowHeight) - 2);
    const lastRow = Math.min(totalRows, Math.ceil((scrollTop + viewportHeight) / rowHeight) + 2);
    const firstIndex = firstRow * columns;
    const lastIndex = Math.min(count, lastRow * columns);
    const visible = Math.max(0, lastIndex - firstIndex);
    ensurePool(visible);

    for (let i = 0; i < visible; i += 1) {
      const index = firstIndex + i;
      const item = state.items[index];
      const row = Math.floor(index / columns);
      const column = index % columns;
      paint(pool[i], item, index, column * (cardWidth + GAP), row * rowHeight);
    }
    if (visible === 0 && count === 0) {
      container.style.height = '0px';
    }
  }

  function relayout() {
    measure();
    render();
  }

  //: 接近底部时触发的回调（无限滚动用）。放这里而不是外部再挂一个 scroll 监听，
  //: 是为了只监听一次滚动 —— 两个监听器会让同一次滚动触发两遍渲染。
  let onNearBottom = null;
  let nearBottomArmed = true;

  function isNearBottom() {
    return scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - rowHeight * 2;
  }

  function handleScroll() {
    render();
    if (!onNearBottom) return;
    // 离开底部区域后重新“武装”，避免一次滚动里连续触发多次加载
    if (!isNearBottom()) { nearBottomArmed = true; return; }
    if (nearBottomArmed) {
      nearBottomArmed = false;
      onNearBottom();
    }
  }

  return {
    init() {
      if (started) return;
      started = true;
      scroll.addEventListener('scroll', handleScroll, { passive: true });
      window.addEventListener('resize', relayout);
      measure();
      render();
    },
    setOnNearBottom(fn) {
      onNearBottom = fn;
    },
    /** 加载完成后调用，让"接近底部"可以再次触发。 */
    rearm() {
      nearBottomArmed = true;
    },
    setItems(items, append) {
      state.items = append ? state.items.concat(items) : items;
      this.init();
      measure();
      render();
    },
    /** 网格里最后画过的那张（给弹窗做渐进式占位图）。 */
    lastPainted() {
      return lastPainted;
    },
    /** 只重画可见的那几十张 —— 数据变了但列表本身没变时用（如改了精选状态）。 */
    render,
    relayout,
    reset() {
      state.items = [];
      container.style.height = '0px';
      ensurePool(0);
    },
    scrollToTop() {
      scroll.scrollTop = 0;
    },
  };
})();

// --------------------------------------------------------------------- 数据加载
function browseScope() {
  const { axis, selectedKey } = state.browse;
  if (!selectedKey) return null;
  // `path` 是**层级**轴：选中的是「城市」或「城市\时段」前缀。
  // `time` / `selection` / `collection` 都是**平面**轴：选中节点的 key 本身就是
  // `scope_key`（选中的是 2026-02 / FINAL / 主题 id），不需要也不能拼成路径。
  // ⚠️ Phase 1 这里写的是 `axis === 'time' ? 'time' : 'path'` ——
  // 一旦启用 selection/collection 轴，它会把状态名或主题 id 当成**路径前缀**发出去，
  // 结果是筛选静默失效（看起来生效、实际返回全库）。axis 名与 scope_kind 同名，
  // 因此直接透传即可；`tag` 未启用，后端会返回 501。
  const kind = axis === 'selection' || axis === 'collection' || axis === 'time' ? axis : 'path';
  return { kind, key: selectedKey };
}

function buildQuery() {
  const params = new URLSearchParams();
  if (state.activeLibraryId !== null) params.set('library_id', String(state.activeLibraryId));
  params.set('limit', String(state.pageSize));
  params.set('offset', String(state.offset));
  params.set('sort', state.filters.sort);
  if (state.filters.file_type) params.set('file_type', state.filters.file_type);
  if (state.filters.status) params.set('status', state.filters.status);
  if (state.filters.camera) params.set('camera', state.filters.camera);
  if (state.filters.lens) params.set('lens', state.filters.lens);
  // 焦距范围：只在用户**真的填了**边界时才传，否则"不筛焦距"不该改变结果集。
  // ⚠️ 后端在收到范围时会排除 focal_length 为 0/NULL 的照片 ——
  // 0 是"没有这个数据"（视频、缺 EXIF），不是"0 毫米超广角"。
  if (state.filters.focalMin !== '') params.set('focal_min', String(state.filters.focalMin));
  if (state.filters.focalMax !== '') params.set('focal_max', String(state.filters.focalMax));
  if (state.filters.year) params.set('year', state.filters.year);
  // 关键词：纯文本匹配（文件名/机型/镜头）—— SQL 的 LIKE，不经过模型
  if (state.filters.q) params.set('q', state.filters.q);
  // 内容词：**后端按全库算相似度**（`/api/semantic/results`），前端只传词。
  // 与其它条件是叠加关系，不是互斥的"搜索模式"。
  if (state.filters.contentQ) params.set('content', state.filters.contentQ);
  // 人物：可重复的参数，多个 = AND（合影）。见 `compose.person_clause`。
  state.filters.personIds.forEach((pid) => params.append('person_id', String(pid)));
  if (state.filters.noMeta) params.set('only_missing_metadata', 'true');
  const scope = browseScope();
  if (scope) {
    params.set('scope_kind', scope.kind);
    params.set('scope_key', scope.key);
  }
  return params.toString();
}

/**
 * 照片库的失败态：显示错误块、藏起网格（网格 DOM 本身**不能动** ——
 * 虚拟网格实例持有对 `#grid` 的引用，覆盖它会连重试一起弄坏）。
 */
function showGridError(html, retry) {
  const box = $('#grid-error');
  const scroll = $('#grid-scroll');
  if (!box) return;
  box.innerHTML = html;
  box.hidden = false;
  if (scroll) scroll.hidden = true;
  bindRetryButtons(box, { 1: retry });
}

function clearGridError() {
  const box = $('#grid-error');
  const scroll = $('#grid-scroll');
  if (box && !box.hidden) { box.hidden = true; box.innerHTML = ''; }
  if (scroll && scroll.hidden) scroll.hidden = false;
}

/**
 * 空库引导：**添加照片库 ≠ 照片出现**。
 *
 * 用户点「添加」只是登记了目录；照片要扫描 + 索引之后才进库。
 * 以前这里只有一句"没有匹配的照片"，看起来像坏了；
 * 现在给出"下一步该做什么"和一个按钮（排队跑 index 动作）。
 */
function emptyLibraryHtml(library) {
  const name = (library && library.name) ? library.name : '这个库';
  const path = library ? library.path : '';
  return `
    <div class="empty-guide">
      <h3>${library ? `「${escapeHtml(name)}」` : escapeHtml(name)}还没有照片</h3>
      <p>添加照片库只是<strong>登记目录</strong>。要跑一次扫描 + 索引，照片才会出现在这里：
         扫描只读硬盘，索引把缩略图与拍摄信息写进数据目录 —— <strong>原文件不会被移动或修改</strong>。</p>
      ${path ? `<p class="hint tiny mono">${escapeHtml(path)}</p>` : ''}
      <p class="hint tiny">推荐走「导入」：照片会<strong>复制</strong>进库并按拍摄时间归到日期文件夹里，
         分组和精选才有稳定的场次结构；「扫描并索引」适合照片已经在库里、只想补索引的情况。</p>
      <div class="row">
        <button class="btn" id="lib-import" type="button">导入照片到此库</button>
        <button class="btn small ghost" id="lib-scan" type="button">扫描并索引（原地）</button>
        <button class="btn small ghost" id="lib-refresh" type="button">刷新</button>
      </div>
      <p class="hint tiny" id="lib-scan-hint">照片多的话要跑一会儿。</p>
      <p class="hint tiny">进度在
        <button class="linkish" id="lib-scan-jobs" type="button">后台任务</button> 里看。</p>
    </div>`;
}

/** 轮询索引任务；结束后自动入队分组。 */
async function waitForIndexThenRegroup(hint, jobId = null) {
  const started = Date.now();
  const limit = 30 * 60 * 1000;   // 30 分钟上限，超时就不再等
  while (Date.now() - started < limit) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    let job = null;
    try {
      const payload = await API.get('/api/jobs?limit=5');
      const items = payload.items || payload.jobs || [];
      // 按 id 精确匹配本次入队的任务；匹配不到就退回"最近的 index 类任务"
      job = jobId
        ? items.find((item) => Number(item.id) === Number(jobId))
        : items.find((item) => String(item.action_id || '').includes('index'));
    } catch { /* 轮询失败就继续等 */ }
    if (!job) continue;
    const state = String(job.status || '').toUpperCase();
    if (state === 'PENDING' || state === 'PROCESSING' || state === 'RETRY' || state === 'PAUSED') {
      continue;
    }
    if (state === 'COMPLETED') {
      if (hint) hint.textContent = '索引完成，正在分组（生成 Shot / Scene）…';
      try {
        await API.post('/api/actions/regroup/run', {});
        if (hint) hint.textContent = '已开始分组。完成后点「刷新」即可浏览，精选页也会有内容。';
      } catch (error) {
        if (hint) hint.textContent = `分组启动失败：${error.message}（可到维护页手动点「现在分组」）`;
      }
    } else if (hint) {
      hint.textContent = '索引未完成（可能被取消或出错），可在维护页查看任务详情。';
    }
    return;
  }
  if (hint) hint.textContent = '等待索引超时，请到维护页查看任务进度。';
}

/** 跳到「维护 → 导入新照片」。导入会复制并按日期建目录，这是推荐路径。 */
function showImportPanel() {
  setView('maintenance');
  requestAnimationFrame(() => {
    const source = document.querySelector('#import-source');
    const group = source ? source.closest('details') : null;
    if (group) group.open = true;
    (source || group)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (source) source.focus();
  });
}

function bindEmptyLibrary() {
  const scan = $('#lib-scan');
  const hint = $('#lib-scan-hint');
  if (scan) {
    scan.onclick = async () => {
      scan.disabled = true;
      if (hint) hint.textContent = '已排队，正在扫描…（进度见维护页）';
      try {
        // 多库时 CLI 必须带 --library，否则直接失败「存在多个 Library」
        const queued = await API.post('/api/actions/index_library/run',
                                      { library: Number(state.activeLibraryId) });
        if (hint) {
          hint.textContent = '已开始扫描与索引。照片多时需要几分钟；索引完成后会自动分组。';
        }
        if (jobs) jobs.hidden = false;   // 链接常驻，别被文字更新带走
        // ★ 索引 ≠ 可浏览：Shot/Scene 要靠"分组"生成，没有它们精选页是空的、
        //   找照片也没有搜索池。这里在索引结束后**自动接一次分组**，省掉用户
        //   自己回维护页点「现在分组」的那一步（实测：漏这步会以为功能坏了）。
        waitForIndexThenRegroup(hint, queued && queued.job ? queued.job.id : null);
      } catch (error) {
        if (hint) hint.textContent = `启动失败：${error.message}`;
        scan.disabled = false;
      }
    };
  }
  const importBtn = $('#lib-import');
  if (importBtn) importBtn.onclick = showImportPanel;
  const jobs = $('#lib-scan-jobs');
  if (jobs) jobs.onclick = showJobs;
  const refresh = $('#lib-refresh');
  if (refresh) refresh.onclick = () => loadAssets();
}

/**
 * 跳到「维护 → 后台任务」。
 *
 * 为什么需要：扫描/索引是**异步**的，用户点完按钮就走开了；回来想找进度，
 * 得记得"在维护页最下面"。现在侧栏常驻一个入口，空库引导里也能直接跳。
 */
/** 跳到「维护 → AI 组件」（人物/找照片/相似照片 都靠它装模型）。 */
function showAiComponents() {
  setView('maintenance');
  requestAnimationFrame(() => {
    const box = $('#ai-components');
    if (!box) return;
    const group = box.closest('details');
    if (group) group.open = true;
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

// 其它模块不 import 本文件（避免循环依赖），用 window 上的钩子调用
window.photoosShowAi = showAiComponents;

function showJobs() {
  setView('maintenance');
  requestAnimationFrame(() => {
    const table = $('#jobs-table');
    if (!table) return;
    const group = table.closest('details');
    if (group) group.open = true;
    // 滚到**正在执行的那条**顶部；没有在跑的才退回表格顶部
    const running = [...table.querySelectorAll('.job')].find((card) => {
      const state = card.querySelector('.state');
      const text = state ? state.textContent.trim().toUpperCase() : '';
      return text === 'PROCESSING' || text === 'PENDING' || text === 'RETRY';
    });
    (running || table).scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

async function loadAssets({ append = false } = {}) {
  if (state.loading) return;
  if (append && state.offset >= state.total) return;
  state.loading = true;
  try {
    if (!append) { state.offset = 0; grid.reset(); }
    const payload = await API.get(`/api/assets?${buildQuery()}`);
    clearGridError();                         // 上一次的失败已经过去了
    state.total = payload.total;
    state.offset = payload.offset + payload.items.length;
    grid.setItems(payload.items, append);
    grid.rearm();
    const library = state.libraries.find((item) => item.id === state.activeLibraryId);
    const libraryEmpty = !library || !library.stats || (library.stats.assets ?? 0) === 0;
    $('#grid-summary').textContent = payload.total !== 0
      ? `已显示 ${state.items.length} / ${payload.total} 张`
      : (libraryEmpty ? '这个库还没有照片' : '没有匹配的照片');
    if (payload.total === 0 && libraryEmpty) {
      showGridError(emptyLibraryHtml(library), () => loadAssets());
      bindEmptyLibrary();
    } else {
      clearGridError();
    }
    $('#load-more').disabled = state.offset >= payload.total;
    if (!append) grid.scrollToTop();
  } catch (error) {
    // 以前这里只有一句 toast（几秒就消失），网格**留着上一次的内容** ——
    // 用户会以为"这就是全部结果"。现在失败是一个留得住的、可重试的状态。
    toast(`加载失败：${error.message}`, true);
    state.total = 0;
    state.items = [];
    grid.reset();
    grid.setItems([], false);
    const summary = $('#grid-summary');
    if (summary) summary.textContent = '读取失败';
    const more = $('#load-more');
    if (more) more.disabled = true;
    showGridError(errorHtml('照片列表', error.message, {
      retry: () => loadAssets(),
      hint: '库还在硬盘上时，多半是后台任务正占着数据库读锁；过几秒重试通常就好。',
    }), () => loadAssets());
  } finally {
    state.loading = false;
    // 条件条跟着结果一起更新：它是 state 的**只读投影**，
    // 放在 finally 里是为了"加载失败"时也能如实显示当前条件（否则用户会以为条件没了）。
    renderLibraryQueryBar();
  }
}

function loadMore() {
  loadAssets({ append: true });
}

// ----------------------------------------------------------------- 条件条（照片库）
//
// 「当前在筛什么」在这里汇总，并且**逐个可撤**。
// 它是 `state.filters` 与 `state.browse` 的**只读投影**：没有新状态，
// 所以随时能加上、随时能拿掉，也不会与左栏的控件不同步。

// 过滤器 → 控件 id（现在都在顶栏的筛选台里）。撤一个条件时要**同时**把控件复位，
// 否则界面会出现"条件条说没筛、顶栏还选着 2024"的矛盾。
const LIBRARY_CONTROLS = {
  q: '#filter-q',
  year: '#filter-year', file_type: '#filter-type',
  status: '#filter-status', camera: '#filter-camera', lens: '#filter-lens',
  noMeta: '#filter-nometa', focalMin: '#filter-focal-min', focalMax: '#filter-focal-max',
};

/** 撤掉一个筛选条件并重查。`keep` 是复位后要保持的值（如焦距的另一端）。 */
function dropFilter(key, keep) {
  state.filters[key] = key === 'noMeta' ? false : '';
  Object.assign(state.filters, keep || {});
  const node = $(LIBRARY_CONTROLS[key]);
  if (node) {
    if (node.type === 'checkbox') node.checked = Boolean(state.filters[key]);
    else node.value = state.filters[key];
  }
  if (key === 'focalMin' || key === 'focalMax') markActivePreset();
  // ⚠️ 这里用 `loadAssets()` 而不是 `bindLibraryControls` 里的 `apply` ——
  // 后者是那个函数作用域里的局部常量（内容就是 `loadAssets()`），模块这一层看不到它。
  loadAssets();
}

/** 撤掉**内容词**：两个控件（下拉与手输框）都要复位。 */
function dropContentWord() {
  state.filters.contentQ = '';
  const pick = $('#filter-content');
  const typed = $('#filter-content-q');
  if (pick) pick.value = '';
  if (typed) typed.value = '';
  loadAssets();
}

/** 把一个人从条件里去掉。**多个人是 AND**，去掉一个就放宽一次。 */
function dropPerson(personId) {
  state.filters.personIds = state.filters.personIds.filter((x) => x !== Number(personId));
  renderPersonChips();
  renderPersonPicker();
  loadAssets();
}

/** 加一个人进条件。已经在里面的不重复加（AND 里重复同一个没有意义）。 */
function addPerson(personId) {
  const id = Number(personId);
  if (!Number.isFinite(id) || id <= 0) return;
  if (state.filters.personIds.includes(id)) return;
  state.filters.personIds = [...state.filters.personIds, id];
  renderPersonChips();
  renderPersonPicker();
  loadAssets();
}

/** 人名（来自缓存的人物名单；取不到就退回 `#id`，**不编造**）。 */
function personName(personId) {
  const found = state.people.find((p) => Number(p.id) === Number(personId));
  return (found && found.name) || `#${personId}`;
}

/** 已选人物的芯片区。 */
function renderPersonChips() {
  const host = $('#filter-people-chips');
  if (!host) return;
  const ids = state.filters.personIds;
  if (!ids.length) {
    host.innerHTML = '<span class="hint tiny">没有选人</span>';
    return;
  }
  host.innerHTML = ids.map((id) => `
    <span class="lf-chip"><span>${escapeHtml(personName(id))}</span>
      <button type="button" class="lf-chip-x" data-drop-person="${id}"
              aria-label="把${escapeHtml(personName(id))}从条件里去掉" title="去掉这个人">×</button>
    </span>`).join('');
}

// ----------------------------------------------------------------- 顶栏筛选台
//
// 展开状态**记在内存里**（不在 localStorage）：这是个本地单人工具，
// 但把界面状态写进持久存储会带来"为什么它自己打开了"这类困惑。
// 一次会话内记住就够了 —— 用户的抱怨是"每次进来都要点一下"。

let filterPanelOpen = false;

function toggleFilterPanel(force) {
  filterPanelOpen = force === undefined ? !filterPanelOpen : Boolean(force);
  const body = $('#lf-body');
  const toggle = $('#lf-toggle');
  if (body) body.hidden = !filterPanelOpen;
  if (toggle) {
    toggle.setAttribute('aria-expanded', filterPanelOpen ? 'true' : 'false');
    const caret = toggle.querySelector('.lf-caret');
    if (caret) caret.textContent = filterPanelOpen ? '▾' : '▸';
  }
  // 展开时才去拉"需要联网才知道"的东西 —— 收起态不该发任何请求。
  // ⚠️ 这里以前写的是 `.catch(() => {})` —— **把失败完全吞掉了**：
  // 人名没加载出来、内容词没加载出来，界面上什么都不说，只能靠肉眼发现。
  // 现在至少留一条控制台记录，且把状态写到 `state.` 上便于排查（探针也要看它）。
  if (filterPanelOpen) {
    loadContentWords().catch((error) => {
      state.filterLoadError = `内容词：${error.message}`;
      console.error('[筛选台] 内容词加载失败', error);
    });
    loadPeopleForFilter().catch((error) => {
      state.filterLoadError = `人物名单：${error.message}`;
      console.error('[筛选台] 人物名单加载失败', error);
    });
  }
}

/** 内容词候选：由后端给（`/api/semantic/queries`），前端**不硬编码**任何词。 */
async function loadContentWords() {
  const pick = $('#filter-content');
  if (!pick) return;
  if (!state.contentWords.length) {
    const payload = await API.get('/api/semantic/queries');
    state.contentWords = payload.items || payload.queries || [];
  }
  // 保留当前选择（重画不该把用户选的词弄丢）
  const chosen = state.filters.contentQ;
  pick.innerHTML = '<option value="">（不用内容筛）</option>'
    + state.contentWords.map((word) => {
      const text = typeof word === 'string' ? word : (word.query_text || word.text || '');
      const hits = typeof word === 'object' ? (word.hit_count ?? word.hits ?? '') : '';
      return `<option value="${escapeHtml(text)}">${escapeHtml(text)}${
        hits === '' ? '' : `（${escapeHtml(String(hits))} 组）`}</option>`;
    }).join('');
  pick.value = chosen || '';
}

/**
 * 人物名单：**一次拉全库并缓存**。
 *
 * 40 个人、实测 55 ms —— 但它是每次展开筛选面板都要用的东西，所以只拉一次。
 * 与 `actions.js` 的命令目录缓存同一套做法。
 *
 * ⚠️ 这里**不**复用 `people.js` 的 `state`：那个模块有 `initPeople()` 之类的初始化，
 * 静态 import 它会把这些副作用带进照片库的路由。名单很轻，自己拉一次更干净。
 */
async function loadPeopleForFilter() {
  if (!state.people.length) {
    const payload = await API.get('/api/people');
    state.people = (payload.items || []).map((person) => ({
      id: Number(person.id), name: person.name || '',
    }));
  }
  // ⚠️ 必须调 `renderPersonPicker()` —— 填充那个下拉的只有它。
  // 我第一版漏了这一句，结果下拉永远只有一个占位项，
  // 表现为"人物名单加载不出来"（实测踩过）。
  renderPersonPicker();
  renderPersonChips();
  renderFilterSummary();
}

/**
 * 重画「＋ 选一个人…」下拉，**把已经在条件里的排除掉**。
 *
 * 为什么必须排除：多个人是 AND，同一个人的 EXISTS 加两次结果不变 ——
 * 留着它只会让用户以为"再加一次会更精确"。排除掉，选项本身就是可选范围的答案。
 */
function renderPersonPicker() {
  const pick = $('#filter-person-add');
  if (!pick) return;
  const chosen = new Set(state.filters.personIds.map(Number));
  pick.innerHTML = '<option value="">＋ 选一个人…</option>'
    + state.people
      .filter((person) => !chosen.has(person.id))
      .map((person) => `<option value="${person.id}">${
        escapeHtml(person.name || `#${person.id}`)}</option>`).join('');
}

/** 焦距的显示文案：两端都可能只有一边。 */
function focalText(lo, hi) {  const hasLo = lo !== '' && lo !== null && lo !== undefined;
  const hasHi = hi !== '' && hi !== null && hi !== undefined;
  if (hasLo && hasHi) return `焦距 ${lo}–${hi}mm`;
  if (hasLo) return `焦距 ≥${lo}mm`;
  if (hasHi) return `焦距 ≤${hi}mm`;
  return '';
}

function libraryChips() {
  const f = state.filters;
  const chips = [];

  // 浏览轴：这是"你在看哪一块"，不是筛掉的条件 —— 所以给它一个可读的位置说明，
  // 撤销走左栏的「全部」（`clearBrowseScope`），不在这里再放一个 ×。
  const axisLabels = { path: '文件夹', time: '时间', selection: '精选状态', collection: '主题' };
  const scope = state.browse.selectedLabel;
  if (scope) {
    chips.push({ key: 'scope', label: `范围：${scope}`, tone: 'scope',
      title: `正在看「${axisLabels[state.browse.axis] || ''}」里的：${scope}` });
  }

  // 内容维度排最前：它是最"粗"的那一类，也最能改变结果集
  if (f.contentQ) {
    chips.push({ key: 'contentQ', label: `内容：${f.contentQ}`, tone: 'content',
      title: `按画面找"像 ${f.contentQ} 的照片"（相似度由模型算，与其它条件叠加）`,
      remove: () => dropContentWord() });
  }
  // 人物维度：一个人一个芯片，去掉谁就放宽一次
  f.personIds.forEach((id) => {
    chips.push({ key: `person:${id}`, label: personName(id), tone: 'person',
      title: '这张照片里有 TA（多个人是"同时在场"）',
      remove: () => dropPerson(id) });
  });

  if (f.year) chips.push({ key: 'year', label: `${f.year} 年`, remove: () => dropFilter('year') });
  if (f.q) {
    chips.push({ key: 'q', label: `关键词：${f.q}`, tone: 'meta',
      title: '文件名 / 机型 / 镜头的文本匹配（不经过模型）',
      remove: () => dropFilter('q') });
  }
  if (f.camera) chips.push({ key: 'camera', label: f.camera, remove: () => dropFilter('camera') });
  if (f.lens) chips.push({ key: 'lens', label: f.lens, remove: () => dropFilter('lens') });

  const focal = focalText(f.focalMin, f.focalMax);
  if (focal) {
    chips.push({ key: 'focal', label: focal, remove: () => {
      state.filters.focalMin = '';
      state.filters.focalMax = '';
      const a = $('#filter-focal-min'); const b = $('#filter-focal-max');
      if (a) a.value = ''; if (b) b.value = '';
      markActivePreset();
      loadAssets();
    } });
  }

  if (f.file_type) chips.push({ key: 'file_type', label: String(f.file_type).toUpperCase(), remove: () => dropFilter('file_type') });
  if (f.status) chips.push({ key: 'status', label: `状态 ${f.status}`, remove: () => dropFilter('status') });
  if (f.noMeta) chips.push({ key: 'noMeta', label: '只看缺元数据', remove: () => dropFilter('noMeta') });

  // 排序不是"筛掉什么"，但它会明显改变你看到的东西 —— 非默认时如实说出来。
  if (f.sort && f.sort !== 'capture') {
    const names = { name: '按文件名', size: '按文件大小', recent: '按最近索引' };
    chips.push({ key: 'sort', label: names[f.sort] || f.sort, tone: 'meta',
      remove: () => {
        state.filters.sort = 'capture';
        const node = $('#filter-sort'); if (node) node.value = 'capture';
        loadAssets();
      } });
  }

  return chips;
}

/** 把所有筛选条件与浏览范围**一起**清掉。
 *
 * 为什么必须一起清：只清筛选、范围还锁在某个文件夹时，用户会以为按钮坏了
 * （"我点了重置，怎么还是只有这几张"）。
 * 单一出口 —— 顶栏的「全部重置」、条件条的「回到全库」、`#filter-reset` 都走这里，
 * 免得三处各写一遍再慢慢漂移。 */
function resetAllFilters() {
  state.filters = {
    file_type: '', status: '', camera: '', lens: '', focalMin: '', focalMax: '',
    year: '', sort: 'capture', q: '', contentQ: '', personIds: [], noMeta: false,
  };
  ['#filter-type', '#filter-status', '#filter-camera', '#filter-lens', '#filter-year',
    '#filter-focal-min', '#filter-focal-max', '#filter-q', '#filter-content-q'].forEach((sel) => {
    const node = $(sel); if (node) node.value = '';
  });
  const content = $('#filter-content'); if (content) content.value = '';
  const sort = $('#filter-sort'); if (sort) sort.value = 'capture';
  const nometa = $('#filter-nometa'); if (nometa) nometa.checked = false;
  markActivePreset();
  renderPersonChips();
  renderPersonPicker();
  renderFilterSummary();

  state.browse.chain = [];
  state.browse.selectedKey = null;
  state.browse.selectedLabel = null;
  state.browse.nodes = [];
  loadBrowseNodes();
  loadAssets();
}

function renderLibraryQueryBar() {
  renderQueryBar($('#library-query'), {
    compute: libraryChips,
    total: state.total,
    totalLabel: '符合',
    onClearAll: libraryChips().some((chip) => chip.remove) || state.browse.selectedKey
      ? resetAllFilters
      : null,
  });
  renderFilterSummary();
}

/**
 * 顶栏筛选台的**收起态摘要**。
 *
 * 收起之后必须还能看出"我正在筛什么" —— 否则用户会以为筛选没了，
 * 或者忘了自己筛着东西却看不到结果为什么这么少。
 * 三个维度的数量分开报，这样一眼能看出是哪一类条件在起作用。
 */
function renderFilterSummary() {
  const node = $('#lf-summary');
  if (!node) return;
  const f = state.filters;
  const parts = [];
  if (f.contentQ) parts.push(`内容「${f.contentQ}」`);
  const metaCount = [
    f.year, f.camera, f.lens, f.file_type, f.status,
    (f.focalMin !== '' || f.focalMax !== '') ? '1' : '', f.noMeta ? '1' : '',
  ].filter(Boolean).length;
  if (metaCount) parts.push(`元数据 ${metaCount} 项`);
  // 关键词单独报 —— 它和"元数据几项"不是一回事（跨字段的文本匹配）
  if (f.q) parts.push(`关键词「${f.q}」`);
  if (f.personIds.length) {
    parts.push(`人物 ${f.personIds.map((id) => personName(id)).join(' + ')}`);
  }
  node.textContent = parts.length ? parts.join(' · ') : '没有筛选条件';
  node.classList.toggle('is-on', parts.length > 0);
  const toggle = $('#lf-toggle');
  if (toggle) toggle.classList.toggle('is-on', parts.length > 0);
}

// --------------------------------------------------------------------- 浏览树
async function loadBrowseNodes() {
  const { axis, chain } = state.browse;
  const parent = chain.length ? chain[chain.length - 1].key : '';
  state.browse.loading = true;
  renderBrowseTree();
  try {
    const params = new URLSearchParams({ axis, parent });
    if (state.activeLibraryId !== null) params.set('library_id', String(state.activeLibraryId));
    const payload = await API.get(`/api/browse/tree?${params.toString()}`);
    state.browse.nodes = payload.nodes;
  } catch (error) {
    state.browse.nodes = [];
    toast(`浏览树加载失败：${error.message}`, true);
  } finally {
    state.browse.loading = false;
    renderBrowseTree();
  }
}

/** 选中一个节点：有子节点就下钻，同时把作用域设成它。 */
function selectBrowseNode(node) {
  const browse = state.browse;
  browse.selectedKey = node.key;
  browse.selectedLabel = node.label;
  if (node.has_children) {
    browse.chain = [...browse.chain, { key: node.key, label: node.label }];
    loadBrowseNodes();
  } else {
    renderBrowseTree();
  }
  loadAssets();
}

function clearBrowseScope() {
  state.browse.chain = [];
  state.browse.selectedKey = null;
  state.browse.selectedLabel = null;
  state.browse.nodes = [];
  loadBrowseNodes();
  loadAssets();
}

/** 面包屑回退到第 index 层（-1 表示"全部"）。 */
function popBrowseTo(index) {
  const browse = state.browse;
  if (index < 0) return clearBrowseScope();
  browse.chain = browse.chain.slice(0, index + 1);
  const last = browse.chain[browse.chain.length - 1];
  browse.selectedKey = last.key;
  browse.selectedLabel = last.label;
  loadBrowseNodes();
  loadAssets();
}

function setBrowseAxis(axis) {
  if (state.browse.axis === axis) return;
  state.browse.axis = axis;
  state.browse.chain = [];
  state.browse.nodes = [];
  state.browse.selectedKey = null;
  state.browse.selectedLabel = null;
  document.querySelectorAll('#browse-axis .seg').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.axis === axis);
  });
  loadBrowseNodes();
  loadAssets();
}

function renderBrowseTree() {
  const browse = state.browse;
  const list = $('#browse-list');
  if (!list) return;

  if (browse.chain.length || browse.selectedKey) {
    const crumbs = [
      `<button class="crumb" data-crumb="-1">全部</button>`,
      ...browse.chain.map(
        (node, index) => `<span class="crumb-sep">›</span>`
          + `<button class="crumb" data-crumb="${index}">${escapeHtml(node.label)}</button>`,
      ),
    ];
    const tail = browse.selectedKey
      && !browse.chain.some((node) => node.key === browse.selectedKey)
      ? `<span class="crumb-sep">›</span><span class="crumb is-current">${escapeHtml(browse.selectedLabel || '')}</span>`
      : '';
    $('#grid-crumbs').innerHTML = crumbs.join('') + tail;
  } else {
    $('#grid-crumbs').innerHTML = `<span class="crumb is-current">全部照片</span>`;
  }

  if (browse.loading && !browse.nodes.length) {
    list.innerHTML = loadingHtml('正在读取…');
    return;
  }
  if (!browse.nodes.length) {
    list.innerHTML = `<div class="wb-empty">${
      browse.selectedKey ? '这一层没有更细的目录' : '这个库还没有可浏览的目录'
    }</div>`;
    return;
  }
  list.innerHTML = browse.nodes.map((node) => `
    <button class="browse-node ${node.key === browse.selectedKey ? 'is-active' : ''}"
            data-key="${escapeHtml(node.key)}">
      <span class="browse-label">${escapeHtml(node.label)}</span>
      <span class="browse-count">${node.count}</span>
      ${node.has_children ? '<span class="browse-more">›</span>' : ''}
    </button>`).join('');
}

async function loadLibraries() {
  const libraries = await API.get('/api/libraries');
  state.libraries = libraries;
  if (state.activeLibraryId === null && libraries.length) {
    state.activeLibraryId = libraries[0].id;
  }

  const list = $('#library-list');
  list.innerHTML = libraries.map((library) => `
    <div class="library-card ${library.id === state.activeLibraryId ? 'is-active' : ''}" data-library="${library.id}">
      <div class="name">
        <span>${escapeHtml(library.name)}</span>
        <span class="badge ${library.status === 'ONLINE' ? 'online' : 'offline'}">
          ${library.status === 'ONLINE' ? '在线' : '离线'}
        </span>
        <span class="library-actions">
          ${(library.asset_count || 0) > 0
            ? `<button class="library-op" data-library-reset="${library.id}"
                       title="删掉索引与缩略图，保留库和目录，用来重建索引">重置索引</button>` : ''}
          <button class="library-op is-danger" data-library-remove="${library.id}"
                  title="删掉索引与该库登记（照片文件不动）">移除库</button>
        </span>
      </div>
      <div class="path">${escapeHtml(library.root_path_hint || '(未记录路径)')}</div>
      <div class="meta">
        ${library.asset_count} 个文件 · ${humanBytes(library.stats.assets_bytes)} ·
        已哈希 ${library.stats.hashed ?? 0} · 预览 ${library.stats.previews}
      </div>
    </div>`).join('') || '<div class="hint">尚未登记照片库。点上方「+ 添加…」选择照片目录。</div>';

  list.querySelectorAll('[data-library-remove]').forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();          // 别触发卡片本身的"切换库"
      const id = Number(button.dataset.libraryRemove);
      const library = libraries.find((item) => item.id === id);
      const ok = confirm(`移除「${library ? library.name : id}」？\n\n只删除这条库登记与卷记录，照片文件不受影响。`);
      if (!ok) return;
      try {
        const preview = await API.get(`/api/libraries/${id}/inventory`);
        const worth = preview.assets
          ? `\n\n将删除：${preview.assets} 张照片的索引、`
            + `${preview.thumbnails_and_previews} 个缩略图/预览（约 ${preview.freed_mb} MB）`
          : '\n\n这个库还没有索引数据。';
        const sure = confirm(
          `移除照片库「${preview.name}」？${worth}\n\n`
          + '照片文件不会被删除或移动；移除后回到「还没有照片库」，可以重新导入。');
        if (!sure) return;
        await API.del(`/api/libraries/${id}`);
        toast('照片库已移除');
        if (state.activeLibraryId === id) state.activeLibraryId = null;
        await loadLibraries();
        await loadAssets();
      } catch (error) {
        toast(`移除失败：${error.message}`, true);
      }
    };
  });

  list.querySelectorAll('[data-library-reset]').forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();
      const id = Number(button.dataset.libraryReset);
      try {
        const preview = await API.get(`/api/libraries/${id}/inventory`);
        const sure = confirm(
          `重置「${preview.name}」的索引？\n\n`
          + `将删除 ${preview.assets} 张照片的索引、${preview.thumbnails_and_previews} 个缩略图/预览`
          + `（约 ${preview.freed_mb} MB）。\n`
          + '库和目录保留，照片文件不动；之后重新「扫描并索引」即可。');
        if (!sure) return;
        await API.post(`/api/libraries/${id}/reset-index`, { confirm: true });
        toast('索引已重置');
        await loadLibraries();
        await loadAssets();
      } catch (error) {
        toast(`重置失败：${error.message}`, true);
      }
    };
  });

  list.querySelectorAll('.library-card').forEach((card) => {
    card.onclick = () => {
      state.activeLibraryId = Number(card.dataset.library);
      // 换库时浏览轴的 key 全部失效（路径/年份都是库内相对的），必须重置
      state.browse.chain = [];
      state.browse.nodes = [];
      state.browse.selectedKey = null;
      state.browse.selectedLabel = null;
      loadLibraries();
      loadBrowseNodes();
      loadAssets();
      renderStats();
    };
  });

  renderStats();
  renderRebind();
  renderImportLibraries();
}

function renderStats() {
  const library = state.libraries.find((item) => item.id === state.activeLibraryId);
  const node = $('#library-stats');
  if (!library) { node.innerHTML = '<div class="hint">未选择照片库</div>'; return; }
  const s = library.stats;
  const rows = [
    ['文件总数', s.assets],
    ['总大小', humanBytes(s.assets_bytes)],
    ['已哈希', s.hashed ?? 0],
    ['元数据', s.metadata_rows],
    ['有拍摄时间', s.metadata_with_capture_time],
    ['缩略图/预览', s.previews],
    ['派生数据', humanBytes(s.previews_bytes)],
  ];
  node.innerHTML = rows.map(([key, value]) => `<dt>${key}</dt><dd>${escapeHtml(value)}</dd>`).join('');
}

// --------------------------------------------------------------------- 详情
// --------------------------------------------------------------------- 原片查看器
/**
 * 打开某张原片的**中央弹窗**：大图 + 元数据 + "这张原片在哪"。
 *
 * 挂在应用外壳上（不在任何一个视图里），所以照片库、找照片、人物三处都调得动。
 * 从别处调用用动态 `import('./app.js')`（静态 import 会把整个应用再启动一遍）。
 *
 * ## 为什么没有「下载原片」
 *
 * 原片就在本机的另一个盘上。走浏览器下载一遍再落到"下载"目录，是让磁盘
 * 读两次、还多出一份不知道放哪的副本。这里改成**把路径交给你**：
 * 完整相对路径 + 绝对路径 + 「复制路径」+「在资源管理器里显示」。
 * 真想在浏览器里看图，右上角还有「用浏览器打开原图」（那是看，不是搬）。
 */
/**
 * 弹窗首屏的占位图：用网格里那张**已经解码**的缩略图。
 *
 * ⚠️ 从 **DOM 读**，不要"记住最后画过的那张"。第一版我记了 `lastPainted`，
 * 结果它是池子里最后画的那一格（实测点第 1 张时它是第 35 张），**永远不匹配**。
 * 而用户点的那张按定义就在 DOM 里（虚拟网格只渲染可见格），
 * 实测它的 `<img>` 已经是 `complete && naturalWidth > 0` —— 拿来即用。
 *
 * 找不到就返回空串（比如从人物页点进来、网格根本没渲染过），
 * 那就老实等 `/preview`，**不假装有**。
 */
function findCachedThumb(assetId) {
  const img = document.querySelector(`#grid .card img[data-asset="${Number(assetId)}"]`);
  if (!img) return '';
  const src = img.getAttribute('src') || '';
  // 没加载成功的不算（`complete` 为假或自然宽度为 0 = 还是空的）
  if (!src.includes('/thumb') || !img.complete || !img.naturalWidth) return '';
  return src;
}

/**
 * 弹窗里的**标签行** —— 一张照片上已有的判断与归类，就在这里改。
 *
 * 合成**一行**而不是三个区块：精选状态、内容命中、人物在语义上都是"这张照片的标签"，
 * 分开占地方而且看着像三个不同的功能。实测 94% 的照片没有任何已归属人物，
 * 所以这一行**常态就是空的**（整块不出现）—— 这就是"不臃肿"的来源。
 *
 * 交互是统一的：**点标签 = 按它筛（回网格看这一类）；点 × = 解除/否决**。
 * 没有第三个动作，所以不需要额外的编辑面板。
 */
function tagsHtml(tags, detail) {
  if (!tags) return '';                       // 标签接口失败：不显示这块，不假装有
  const parts = [];

  // ① 精选状态：这是**唯一的真标签来源**（人物与内容都是模型给的），
  //    所以给它一个下拉，可以直接改。四态与精选页一致。
  if (detail.shot_id) {
    const current = String(detail.selection_status || 'UNREVIEWED');
    const options = ['UNREVIEWED', 'CANDIDATE', 'SELECTED', 'FINAL']
      .map((value) => `<option value="${value}"${value === current ? ' selected' : ''}>${
        escapeHtml(SELECTION_LABELS[value] || value)}</option>`).join('');
    parts.push(`<span class="tag tag-selection">
        <select id="asset-tag-selection" title="这张的精选状态（随时可退回）">${options}</select>
        ${detail.selection_reason
          ? `<span class="tag-note">${escapeHtml(detail.selection_reason)}</span>` : ''}
      </span>`);
  }

  // ② 内容命中：这张**进了哪些内容词的结果集**（模型算的，不是人标的）。
  //    点它 = 按这个词筛；× = "不是这种"（走同一套表态接口，会真的重排）。
  (tags.content_hits || []).forEach((hit) => {
    parts.push(`<span class="tag tag-content" data-query="${escapeHtml(hit.query_text)}"
        title="这张在「${escapeHtml(hit.query_text)}」的结果里（相似度 ${
          Number(hit.score).toFixed(3)}）。点它按这个词筛，× 表示不是这种">
        <button type="button" class="tag-open" data-filter-content="${escapeHtml(hit.query_text)}"
          >${escapeHtml(hit.query_text)}</button>
        <button type="button" class="tag-x" data-drop-content="${escapeHtml(hit.query_text)}"
          aria-label="不是这种：把这张从「${escapeHtml(hit.query_text)}」里删掉" title="不是这种">×</button>
      </span>`);
  });

  // ③ 人物：已经归属到这张照片上的人。人名由**已缓存的名单**解析，
  //    取不到就退回 `#id`（**不编造**）。
  (tags.person_ids || []).forEach((id) => {
    const name = personName(id);
    parts.push(`<span class="tag tag-person" data-person="${id}"
        title="${escapeHtml(name)}出现在这张照片里。点它按这个人筛">
        <button type="button" class="tag-open" data-filter-person="${id}">${escapeHtml(name)}</button>
        <button type="button" class="tag-x" data-goto-person="${id}"
          aria-label="去人物页处理${escapeHtml(name)}" title="去「人物」页改（解除归属在那里）">×</button>
      </span>`);
  });

  // ④ 主题：这张**已经在**哪些主题里。
  //    改之前弹窗只知道"能加进哪个主题"，不知道"已经在哪个" ——
  //    从主题页点开一张照片，看不出自己是从哪儿进来的（用户报的问题）。
  //    点它 = 去那个主题；× = 从那个主题移出（只改关系，不动文件）。
  (tags.collections || []).forEach((collection) => {
    parts.push(`<span class="tag tag-collection" data-collection="${collection.id}"
        title="这张在主题「${escapeHtml(collection.name)}」里。点它去这个主题">
        <button type="button" class="tag-open" data-goto-collection="${collection.id}"
          >${escapeHtml(collection.name)}</button>
        <button type="button" class="tag-x" data-leave-collection="${collection.id}"
          aria-label="从「${escapeHtml(collection.name)}」移出" title="从主题移出（不删照片）">×</button>
      </span>`);
  });

  if (!parts.length) {
    // ⚠️ 空的时候也**渲染这一行**，只是给一句极淡的说明。
    // 原因不是"好看"，是**防跳动**：标签是异步到的（`/tags` 与详情并行），
    // 如果无标签时整块不渲染，标签一到就会把下面的路径与元数据**整体往下推**一截。
    // 实测过：那张有主题标签的照片，标签行把下方内容推下去了。
    // 用户读元数据读到一半被推走，比多一行灰字难受得多。
    return `<div class="asset-tags is-empty" id="asset-tags">
        <span class="asset-tags-label">标签</span>
        <span class="asset-tags-list"><span class="hint tiny">还没有标签</span></span>
      </div>`;
  }
  return `<div class="asset-tags" id="asset-tags">
      <span class="asset-tags-label">标签</span>
      <span class="asset-tags-list">${parts.join('')}</span>
    </div>`;
}

/** 标签行的动作。三个：改精选状态、撤内容命中、按标签筛。 */
function bindTagActions(tags, detail) {
  const host = $('#asset-tags');
  if (!host) return;

  const sel = $('#asset-tag-selection');
  if (sel && detail.shot_id) {
    sel.onchange = async () => {
      const status = sel.value;
      try {
        await API.post(`/api/shots/${detail.shot_id}/selection`, { status });
        toast(`已标为「${SELECTION_LABELS[status] || status}」`);
        // 网格里那张卡的状态也要跟着变 —— 否则关掉弹窗看到的还是旧徽标
        syncCardSelection(detail.id, status);
      } catch (error) {
        toast(`改状态失败：${error.message}`, true);
        sel.value = String(detail.selection_status || 'UNREVIEWED');
      }
    };
  }

  host.onclick = async (event) => {
    const leave = event.target.closest('[data-leave-collection]');
    if (leave) {
      // 从主题移出：只改数据库关系，**不删照片**。
      // ⚠️ 直接打这个端点而不是调 `collections.js` 的 `detachItems()` ——
      // 那个函数用的是**它自己 `state.detail.id`**（"当前打开的主题"），
      // 从弹窗调用会依赖"用户恰好开着同一个主题"这种巧合。
      // 写完之后让主题页自己刷新一次（它按需加载，不在这里塞它的 state）。
      if (!detail.shot_id) return;
      const chip = leave.closest('.tag');
      const name = chip ? chip.querySelector('.tag-open').textContent.trim() : '这个主题';
      try {
        await API.post(`/api/collections/${leave.dataset.leaveCollection}/items/detach`, {
          shot_ids: [Number(detail.shot_id)],
        });
        chip.remove();
        toast(`已从「${name}」移出（照片本身没动）`);
        const mod = await import('./collections.js');
        if (typeof mod.refreshCollections === 'function') mod.refreshCollections();
      } catch (error) {
        toast(`移出失败：${error.message}`, true);
      }
      return;
    }
    const goto = event.target.closest('[data-goto-collection]');
    if (goto) {
      // 去那个主题。入口是 `collections.js` 导出的 `openCollection(id)` ——
      // 它内部装配好自己的 `state.activeId` / `state.detail`。
      // 不在这里塞它的 state（跨模块改别人的状态是这个仓库反复踩过的坑）。
      closeAssetDetail();
      setView('collections');
      const mod = await import('./collections.js');
      if (typeof mod.openCollection === 'function') {
        await mod.openCollection(Number(goto.dataset.gotoCollection));
      }
      return;
    }
    const drop = event.target.closest('[data-drop-content]');
    if (drop) {
      // "不是这种"= 走**同一套表态接口**（它会真的重算重排），不是只把芯片删掉。
      // 只删芯片是骗人的：下次按这个词筛，这张还会回来。
      if (!detail.shot_id) return;
      try {
        await API.post('/api/semantic/feedback', {
          query: drop.dataset.dropContent,
          shot_id: detail.shot_id,
          action: 'drop',
        });
        drop.closest('.tag').remove();
        toast(`已把这张从「${drop.dataset.dropContent}」里删掉`);
      } catch (error) {
        toast(`没能删掉：${error.message}`, true);
      }
      return;
    }
    const person = event.target.closest('[data-goto-person]');
    if (person) {
      // 人物归属是**脸级**的（`person_face`），解除要在人物页做 —— 那里能看到是哪张脸。
      // 这里不假装能改，直接给出口。
      closeAssetDetail();
      setView('people');
      toast('在「人物」页里解除归属（那里能看到是哪一张脸）');
      return;
    }
    const byContent = event.target.closest('[data-filter-content]');
    if (byContent) {
      applyTagFilter({ content: byContent.dataset.filterContent });
      return;
    }
    const byPerson = event.target.closest('[data-filter-person]');
    if (byPerson) {
      applyTagFilter({ personId: Number(byPerson.dataset.filterPerson) });
    }
  };
}

/**
 * 「点标签就按它筛」：把这一个条件设进筛选台，关掉弹窗回网格。
 *
 * **替换而不是叠加**：用户点某个标签的意图是"看这一类"，不是在现有条件上再加一条
 * （叠加会出现"当前条件下 0 张"的困惑，而他只是想看这个人的照片）。
 * 所以走 `resetAllFilters()` 再设一条 —— 单一出口，不另写一套清除逻辑。
 */
function applyTagFilter({ content = '', personId = null }) {
  closeAssetDetail();
  setView('library');
  resetAllFilters();
  if (content) {
    state.filters.contentQ = content;
    const pick = $('#filter-content');
    if (pick) pick.value = content;
  }
  if (personId) {
    state.filters.personIds = [Number(personId)];
    renderPersonChips();
    renderPersonPicker();
  }
  toggleFilterPanel(true);          // 展开了才知道自己筛了什么，也方便撤
  loadAssets();
}

/**
 * 弹窗改了精选状态之后，让网格里那张卡**当场**跟上。
 *
 * 为什么必须有：`state.items` 是拉列表时定格的快照，改状态只写了数据库。
 * 不刷这一张的话，关掉弹窗看到的还是旧徽标 —— 用户会以为没生效，然后再点一次。
 * 只刷这一张（不是重拉整页）：重拉会让滚动位置和当前视图都跳。
 */
function syncCardSelection(assetId, status) {
  const item = state.items.find((row) => Number(row.id) === Number(assetId));
  if (!item) return;
  item.selection_status = status;
  // 同一拍摄组的其它文件共享同一个 Shot 的精选状态 —— 一起刷，
  // 否则 JPG 变了、NEF 没变，看起来像两回事。
  state.items.forEach((row) => {
    if (item.shot_id !== null && row.shot_id === item.shot_id) row.selection_status = status;
  });
  // 只重画可见的那几十张（`render()` 不重设数据、不重置滚动）。
  // 不调 `setItems`：那会重建列表并可能跳动。
  grid.render();
}

export async function openAssetDetail(assetId) {
  const modal = $('#asset-modal');
  if (!modal) return;
  try {
    // ⚠️ **三级渐进**，不是"等最慢的那个"。
    //
    // 改之前是 `await Promise.all([详情, path, collections])` —— 详情实测 110 ms，
    // 用户点下去要等它才看到任何东西。现在：
    //   ① 立刻显示（0 ms）：网格里那张缩略图是**已经缓存的**，直接拿来当占位；
    //   ② ~15 ms：路径 + 标签（人名 / 内容命中 / 精选状态）；
    //   ③ ~110 ms：元数据表 + 大图替换缩略图。
    // 慢的部分只是"替换"，不是"什么都没有"。
    const cached = findCachedThumb(assetId);

    const [detail, where, collections, tags] = await Promise.all([
      API.get(`/api/assets/${assetId}`),
      API.get(`/api/assets/${assetId}/path`).catch(() => null),
      API.get('/api/collections').catch(() => ({ items: [] })),
      API.get(`/api/assets/${assetId}/tags`).catch(() => null),
    ]);
    const meta = detail.metadata || {};
    const isVideo = detail.file_type === 'video';
    const canRenderInline = INLINE_IMAGE_TYPES.has(detail.file_type);
    const rows = [
      ['类型', detail.file_type],
      ['大小', humanBytes(detail.size)],
      ['状态', detail.status],
      ['拍摄时间', meta.capture_at || '—'],
      ['时区', meta.tz_offset ? `${meta.tz_offset}（来源 ${meta.tz_source}）` : '未知（本地挂钟时间）'],
      ['机型', [meta.camera_make, meta.camera_model].filter(Boolean).join(' ') || '—'],
      ['镜头', meta.lens_model || '—'],
      ['焦段', meta.focal_length ? `${meta.focal_length}mm${meta.focal_length_35mm ? `（等效 ${meta.focal_length_35mm}mm）` : ''}` : '—'],
      ['曝光', [meta.aperture ? `f/${meta.aperture}` : null, meta.shutter_speed, meta.iso ? `ISO ${meta.iso}` : null].filter(Boolean).join('  ') || '—'],
      ['尺寸', meta.width && meta.height ? `${meta.width} × ${meta.height}` : '—'],
      ['GPS', meta.gps_lat ? `${meta.gps_lat}, ${meta.gps_lon}` : '—'],
      ['内容哈希', detail.content_hash ? `${String(detail.content_hash).slice(0, 16)}…` : '尚未计算'],
      ['元数据来源', [meta.source, meta.source_enrichment].filter(Boolean).join(' + ') || '—'],
    ];
    const notices = [];
    if (detail.status === 'MISSING' || detail.status === 'UNREADABLE') {
      notices.push('该文件当前不在硬盘上。索引、缩略图与元数据依然可用；插回硬盘后原图即可访问。');
    }
    if (!detail.has_preview) {
      notices.push('尚未生成预览。运行 `photoos index` 或点击维护页的「生成缩略图」。');
    }
    if (!canRenderInline && !isVideo) {
      notices.push(
        `浏览器无法直接渲染 ${String(detail.file_type).toUpperCase()}：`
        + '左边显示的是从原片转出来的预览。要看或处理原片，用右边给出的路径打开它。'
      );
    }
    if (meta.tz_source === 'unknown' && meta.capture_at) {
      notices.push('EXIF 未记录时区：拍摄时间按本地挂钟时间显示，且未换算 UTC（这是有意为之，避免猜错时区）。');
    }

    const absolute = (where && where.absolute_path) || '';
    const relative = (where && where.relative_path) || detail.relative_path || '';
    const stateText = !where ? '路径未知（这个库没有登记路径提示）'
      : (where.exists ? '文件在硬盘上' : '文件当前不在硬盘上 —— 插回硬盘即可访问');
    const at = `（来自「${where && where.library_name ? where.library_name : '本库'}」）`;

    $('#asset-modal-title').textContent = baseName(relative) || `#${detail.id}`;
    const open = $('#asset-modal-open');
    open.href = `/api/assets/${detail.id}/view`;

    // 图：先挂**已缓存的缩略图**当占位（同一张图浏览器里已经有了，0 延迟），
    // 大图就位后自动替换。没有缩略图就直接上大图。
    const bigSrc = isVideo
      ? ''
      : (detail.has_preview ? `/api/assets/${detail.id}/preview` : '');
    const stage = isVideo
      ? `<video controls preload="metadata" poster="/api/assets/${detail.id}/thumb"
                src="/api/assets/${detail.id}/view"></video>`
      : (detail.has_preview
        ? `<img src="${bigSrc}" alt="" data-fallback="${escapeHtml(cached || '')}"
                class="${canRenderInline ? '' : 'is-preview-fallback'}">`
        : '<p class="hint">这张还没有生成预览。</p>');

    const tagRow = tagsHtml(tags, detail);

    $('#asset-modal-body').innerHTML = `
      <div class="asset-modal-stage">${stage}</div>
      <div class="asset-modal-side">
        ${tagRow}
        <h3>原片在哪</h3>
        <div class="asset-path">
          <span class="asset-path-line" id="asset-path-absolute">${escapeHtml(absolute || relative || '（库里没有路径记录）')}</span>
          ${absolute && relative && relative !== absolute
            ? `<span class="asset-path-line is-dim">库内相对路径：${escapeHtml(relative)}</span>` : ''}
          <span class="asset-path-state ${where && where.exists ? 'is-ok' : 'is-bad'}">
            ${escapeHtml(stateText)}${where && where.library_name ? escapeHtml(at) : ''}
          </span>
          <div class="asset-path-actions">
            <button class="btn small" id="asset-copy-path"
                    data-path="${escapeHtml(absolute || relative)}">复制路径</button>
            <button class="btn small" id="asset-reveal">在资源管理器里显示</button>
          </div>
        </div>
        <h3>元数据</h3>
        <dl class="kv">${rows.map(([key, value]) => `<dt>${key}</dt><dd>${escapeHtml(value)}</dd>`).join('')}</dl>
        ${collectHtml(detail, collections.items || [])}
        ${notices.map((text) => `<div class="notice">${escapeHtml(text)}</div>`).join('')}
      </div>`;

    // 大图挂上时就地替换占位（`data-fallback` 是缩略图 URL）。
    // ⚠️ 用**预加载**而不是直接改 `src`：直接改的话图没到之前会闪成空白，
    // 而占位图本来就在显示 —— 等新图 load 完再换，画面是连续的。
    const bigImg = $('#asset-modal-body img[data-fallback]');
    if (bigImg && bigImg.dataset.fallback && bigImg.getAttribute('src') !== bigImg.dataset.fallback) {
      const placeholder = bigImg.dataset.fallback;
      const want = bigImg.getAttribute('src');
      bigImg.setAttribute('src', placeholder);
      const pre = new Image();
      pre.onload = () => {
        // 弹窗可能已经被关掉或换了另一张 —— 只在还是它的时候替换
        if (bigImg.isConnected && bigImg.getAttribute('src') === placeholder) {
          bigImg.setAttribute('src', want);
        }
      };
      pre.src = want;
    }

    bindTagActions(tags, detail);
    bindCollectActions(detail);

    const copy = $('#asset-copy-path');
    if (copy) {
      copy.onclick = async () => {
        const text = copy.dataset.path || '';
        try {
          await navigator.clipboard.writeText(text);
          toast('路径已复制');
        } catch {
          // 剪贴板被浏览器策略挡住时：把路径选中，让用户自己 Ctrl+C
          const node = $('#asset-path-absolute');
          if (node && window.getSelection) {
            const range = document.createRange();
            range.selectNodeContents(node);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
          }
          toast('已选中路径，按 Ctrl+C 复制', true);
        }
      };
    }
    const reveal = $('#asset-reveal');
    if (reveal) {
      reveal.onclick = async () => {
        try {
          const result = await API.post(`/api/assets/${detail.id}/reveal`, {});
          toast(result.exists ? '已在资源管理器里选中这张'
            : '文件不在盘上：已打开它所在的目录');
        } catch (error) {
          toast(`打不开资源管理器：${error.message}`, true);
        }
      };
    }

    modal.hidden = false;
    modal.setAttribute('aria-hidden', 'false');
  } catch (error) {
    toast(`读取详情失败：${error.message}`, true);
  }
}

/**
 * 弹窗里的「主题」一块：**就地**把这张加进某个主题，或者新建一个再加。
 *
 * 为什么放在这里：用户在弹窗里看着一张照片时的下一个念头常常是
 * "这张该收进《蓝调时刻》" —— 而主题是按**拍摄组**收的，所以加的是
 * 这张照片所属的那一组（界面上如实写明）。上一版要关掉弹窗、切到主题页、
 * 再用「添加照片」一层层找回来。
 *
 * `shot_id` 为空时**不给按钮**：这张还没归入任何拍摄组，给了也点不动，
 * 不如直接说清楚为什么（以及去哪补）。
 */
function collectHtml(detail, collections) {
  const shotId = detail.shot_id ? Number(detail.shot_id) : null;
  if (!shotId) {
    return `
      <h3>主题</h3>
      <p class="hint tiny">
        这张还没有归入任何拍摄组（主题是按拍摄组收的），所以暂时加不进主题。
        在「维护」里跑一次「扫描」或「完整索引」把分组补上之后再回来。
      </p>`;
  }
  const options = collections
    .map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`)
    .join('');
  return `
    <h3>主题</h3>
    <div class="asset-collect">
      ${collections.length
        ? `<select id="asset-collection-pick" aria-label="加入哪个主题">${options}</select>
           <button class="btn small" id="asset-add-collection">加入</button>`
        : '<span class="hint tiny">还没有主题 —— 就地在右边新建一个。</span>'}
      <button class="btn small" id="asset-new-collection">新建主题并加入</button>
    </div>
    <p class="hint tiny">加入的是这张照片所在的整组（主题按拍摄组收），只改数据库关系，不动文件。</p>`;
}

function bindCollectActions(detail) {
  const shotId = detail.shot_id ? Number(detail.shot_id) : null;
  if (!shotId) return;
  const pick = $('#asset-collection-pick');
  const add = $('#asset-add-collection');
  if (add && pick) {
    add.onclick = async () => {
      const collectionId = Number(pick.value);
      const label = pick.options[pick.selectedIndex]
        ? pick.options[pick.selectedIndex].textContent : '主题';
      try {
        const result = await API.post(`/api/collections/${collectionId}/items`,
                                      { shot_ids: [shotId] });
        toast(result.added ? `已加入「${label}」`
          : `「${label}」里已经有这一组了（重复会自动跳过）`);
      } catch (error) {
        toast(`加入主题失败：${error.message}`, true);
      }
    };
  }
  const create = $('#asset-new-collection');
  if (create) {
    create.onclick = async () => {
      const name = prompt('新主题的名字', '');
      if (!name || !String(name).trim()) return;
      try {
        const created = await API.post('/api/collections', { name: String(name).trim() });
        const result = await API.post(`/api/collections/${created.id}/items`,
                                      { shot_ids: [shotId] });
        toast(`已新建主题「${created.name}」并加入${result.added ? '' : '（它本来就在里面）'}`);
      } catch (error) {
        toast(`新建主题失败：${error.message}`, true);
      }
    };
  }
}

/** 关闭原片弹窗。Esc、点背景、点「关闭」都到这儿。 */
export function closeAssetDetail() {  const modal = $('#asset-modal');
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  const stage = modal.querySelector('.asset-modal-stage');
  if (stage) stage.innerHTML = '';      // 清掉 <video>，别让它在后台继续播
}

// --------------------------------------------------------------------- 一键打包
/**
 * **一键打包**：把一个人 / 一个主题 / 一批拍摄组的原片写成一个 zip。
 *
 * 三步，每一步都让用户看得见：
 *   1. 先问服务端"这次有多少张、多大"（`/api/exports/plan`），**把账摆在对话框里**
 *      —— 几百张 RAW 是几十 GB，不问一声就开写是不负责的；
 *   2. 入队（后台任务：有进度、可取消），进度用 toast 持续报；
 *   3. 完成后弹一个小结果框，给出**完整路径**（可复制、可在资源管理器里选中），
 *      而不是让浏览器下载一个几十 GB 的包。
 */
export async function exportPack({ kind, id, shotIds = [], label = '' }) {
  const body = { kind, id, shot_ids: shotIds };
  let plan;
  try {
    plan = await API.post('/api/exports/plan', body);
  } catch (error) {
    toast(`算不出打包规模：${error.message}`, true);
    return;
  }
  if (!plan.count) {
    toast('这些照片的原片当前都不在盘上，没法打包', true);
    return;
  }
  const size = humanBytes(plan.bytes);
  const warn = plan.missing
    ? `\n\n有 ${plan.missing} 张的原片不在盘上，会被跳过（包内清单里会写出来）。` : '';
  const ok = confirm(
    `打包「${plan.label}」的原片？\n\n`
    + `${plan.count} 个文件，约 ${size}\n`
    + `输出到：${plan.output_dir}${warn}\n\n`
    + '这是一个复制动作：原片只读，不会移动、不会改名、不会删除。');
  if (!ok) return;

  let job;
  let outputPath = '';
  try {
    const created = await API.post('/api/exports', body);
    job = created.job;
    outputPath = created.output_path || '';
  } catch (error) {
    toast(`打包没能开始：${error.message}`, true);
    return;
  }
  toast(`正在打包 ${plan.count} 个文件…（可以关掉这个提示，任务在后台跑）`);
  await followExportJob(job.id, plan, outputPath);
}

/** 盯着打包任务直到结束，然后把产物路径摆出来。 */
async function followExportJob(jobId, plan, outputPath) {
  const deadline = Date.now() + 60 * 60 * 1000;      // 一小时上限：几十 GB 够用了
  let lastNote = '';
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    let payload;
    try {
      payload = await API.get(`/api/jobs/${jobId}`);
    } catch {
      continue;                                        // 服务重启之类的瞬断：继续等
    }
    const job = payload.job || {};
    if (job.status === 'COMPLETED') {
      showPackResult({
        label: plan.label,
        output: outputPath,
        note: job.note || '',
        planned: plan.count,
      });
      return;
    }
    if (job.status === 'FAILED' || job.status === 'CANCELLED') {
      toast(job.status === 'CANCELLED' ? '打包已取消（半成品已删掉）'
        : `打包失败：${job.error || '原因见维护页的任务列表'}`, true);
      return;
    }
    if (job.note && job.note !== lastNote) {
      lastNote = job.note;
      toast(`打包中：${job.note}`);
    }
  }
  toast('打包还在跑（超过一小时了）：去「维护」看任务进度', true);
}

/** 打包完成的小结果框：路径是主角，复制/定位是两个动作。 */
function showPackResult({ label, output, note, planned }) {
  const box = $('#pack-result');
  if (!box) return;
  box.innerHTML = `
    <div class="pack-result">
      <h3>打包完成</h3>
      <p class="hint tiny">「${escapeHtml(label)}」${escapeHtml(note || `${planned} 个文件`)}</p>
      <span class="asset-path-line" id="pack-result-path">${escapeHtml(output || '（任务没报出输出路径，去维护页看）')}</span>
      <p class="hint tiny">
        包内有一份「清单.txt」，写明每一张的原路径、拍摄时刻与体积；
        打包时不在盘上的原片会列在里面（没有进包）。
        原片本身没有被移动或修改。
      </p>
      <div class="asset-path-actions">
        <button class="btn small" id="pack-copy">复制路径</button>
        <button class="btn small" id="pack-reveal">在资源管理器里显示</button>
        <button class="btn small" id="pack-close">关闭</button>
      </div>
    </div>`;
  box.hidden = false;
  const close = () => { box.hidden = true; box.innerHTML = ''; };
  $('#pack-close').onclick = close;
  $('#pack-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText(output || '');
      toast('路径已复制');
    } catch {
      toast('复制失败：请手动选中上面的路径', true);
    }
  };
  $('#pack-reveal').onclick = async () => {
    try {
      await API.post('/api/exports/reveal', { path: output });
    } catch (error) {
      toast(`打不开资源管理器：${error.message}`, true);
    }
  };
}

// --------------------------------------------------------------------- 任务
const JOB_LABELS = {
  scan: '扫描（发现文件）',
  metadata: '提取元数据',
  preview: '生成缩略图与预览',
  hash: '计算内容哈希',
  full_index: '完整索引',
  classify_import: '导入（基础物理分类）',
  export: '打包原片',
  // 界面直跑的白名单命令（见 actions/catalog.py）。具体跑了哪一条由
  // scope.action_id 决定，这里只给一个族名 —— 详情在任务卡片的命令行里。
  cli_action: '命令（界面直跑）',
  rebind: '重新绑定',
  verify: '校验',
  migrate: '迁移',
  backup: '备份',
};

async function refreshJobs() {
  try {
    const payload = await API.get('/api/jobs?limit=15');
    state.jobs = payload.jobs;
    state.runningJobId = payload.running_job_id;
    renderJobs();
    renderImportTaskFromState();
  } catch (error) {
    if (state.view === 'maintenance') toast(`任务列表加载失败：${error.message}`, true);
  }
}

const IMPORT_TASK_STATES = {
  PENDING: '排队中', PROCESSING: '进行中', RETRY: '重试中', PAUSED: '已暂停',
  COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消',
};

/** 导入卡片里那条"本次任务"状态（B 方案：把反馈放回按钮下面）。 */
function renderImportTask(job) {
  const node = $('#import-task');
  if (!node) return;
  if (!job) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  const total = Number(job.progress_total) || 0;
  const done = Number(job.progress_done) || 0;
  const percent = total ? Math.min(100, (100 * done) / total) : 0;
  const active = ['PENDING', 'PROCESSING', 'RETRY', 'PAUSED'].includes(job.status);
  const stateText = IMPORT_TASK_STATES[job.status] || job.status;
  node.hidden = false;
  node.innerHTML = `
    <div class="import-task-head">
      <b>导入任务 #${job.id}</b>
      <span class="import-task-state is-${String(job.status).toLowerCase()}">${stateText}</span>
      <span class="import-task-num">${total ? `${done} / ${total} · ${percent.toFixed(1)}%` : ''}</span>
      <span class="spacer"></span>
      ${active ? `<button class="btn small" data-import-job="${job.id}"
        data-action="${job.status === 'PAUSED' ? 'resume' : 'pause'}">${job.status === 'PAUSED' ? '继续' : '暂停'}</button>
        <button class="btn small" data-import-job="${job.id}" data-action="cancel">取消</button>` : ''}
      <button class="btn small ghost" id="import-task-more">查看全部任务 ↓</button>
    </div>
    ${total ? `<div class="import-task-bar"><i style="width:${percent}%"></i></div>` : ''}`;
  node.querySelectorAll('button[data-import-job]').forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await API.post(`/api/jobs/${button.dataset.importJob}/${button.dataset.action}`);
        await refreshJobs();
      } catch (error) {
        toast(`操作失败：${error.message}`, true);
        button.disabled = false;
      }
    };
  });
  const more = $('#import-task-more');
  if (more) {
    more.onclick = () => {
      const table = $('#jobs-table');
      if (table) table.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
  }
}

function renderImportTaskFromState() {
  if (!state.importJobId) { renderImportTask(null); return; }
  const job = (state.jobs || []).find((item) => Number(item.id) === Number(state.importJobId));
  renderImportTask(job || null);
}

function renderJobs() {
  const node = $('#jobs-table');
  if (!node) return;   // 维护页改版时丢过这个节点 —— 宁可什么都不画，也别报 null
  if (!state.jobs.length) {
    node.innerHTML = '<div class="hint">还没有任务。用上方按钮启动一个索引阶段。</div>';
    return;
  }
  node.innerHTML = state.jobs.map((job) => {
    const percent = job.progress_total ? Math.min(100, (100 * job.progress_done) / job.progress_total) : 0;
    const active = ['PENDING', 'PROCESSING', 'RETRY', 'PAUSED'].includes(job.status);
    const actions = active
      ? `<button class="btn small" data-job="${job.id}" data-action="${job.status === 'PAUSED' ? 'resume' : 'pause'}">
           ${job.status === 'PAUSED' ? '继续' : '暂停'}
         </button>
         <button class="btn small" data-job="${job.id}" data-action="cancel">取消</button>`
      : `<button class="btn small" data-job="${job.id}" data-action="resume">重新入队</button>`;
    // ⚠️ 命令类任务**天生没有 progress_total** —— 一条外部命令要跑多久、
    // 分几步，不到输出出来是不知道的（见 js/actions.js 的进度面板说明）。
    // 所以这里不能一律写「进度未知」：那对每条命令都是一句无信息量的话。
    // 分三种情况说人话：
    //   · 有 total     → 真实百分比 + 进度条；
    //   · 命令类       → 已输出 N 行（这是"它还在出东西"的可核对证据）；
    //   · 其它无 total → 如实说"未提供进度"。
    const hasTotal = Number(job.progress_total) > 0;
    const isCommand = job.type === 'cli_action';
    const doneLines = Number(job.progress_done) || 0;
    let progressText;
    if (hasTotal) {
      progressText = `${job.progress_done} / ${job.progress_total}（${percent.toFixed(1)}%）`;
    } else if (isCommand) {
      progressText = doneLines
        ? `已输出 ${doneLines} 行`
        : (job.status === 'PROCESSING' ? '正在启动…' : '无输出');
    } else {
      progressText = job.status === 'COMPLETED' ? '已完成' : '未提供进度';
    }
    return `
      <div class="job">
        <div class="job-head">
          <span class="job-title">#${job.id} ${escapeHtml(JOB_LABELS[job.type] || job.type)}</span>
          <span class="state ${job.status}">${job.status}</span>
        </div>
        <div class="job-meta">
          ${progressText}
          ${job.library_id ? ` · Library #${job.library_id}` : ''}
          ${job.retry_count ? ` · 重试 ${job.retry_count}` : ''}
        </div>
        ${job.note ? `<div class="job-meta">${escapeHtml(job.note)}</div>` : ''}
        ${job.error ? `<div class="job-meta" style="color:var(--fail)">${escapeHtml(String(job.error).slice(0, 300))}</div>` : ''}
        ${hasTotal ? `<div class="bar"><i style="width:${percent}%"></i></div>` : ''}
        <div class="job-actions">${actions}</div>
      </div>`;
  }).join('');

  node.querySelectorAll('button[data-action]').forEach((button) => {
    button.onclick = async () => {
      const jobId = button.dataset.job;
      const action = button.dataset.action;
      try {
        await API.post(`/api/jobs/${jobId}/${action}`);
        toast(`任务 #${jobId} 已${action === 'pause' ? '请求暂停' : action === 'resume' ? '重新入队' : '取消'}`);
        refreshJobs();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });
}

async function startJob(type) {
  try {
    const payload = await API.post('/api/jobs', {
      type,
      library_id: state.activeLibraryId,
    });
    toast(`已创建任务 #${payload.id}（${JOB_LABELS[type] || type}）`);
    setView('maintenance');
    refreshJobs();
  } catch (error) {
    toast(`创建任务失败：${error.message}`, true);
  }
}

// --------------------------------------------------------------------- 工具页
async function refreshTools() {
  renderRebind();
}

function renderRebind() {
  const node = $('#rebind-list');
  if (!node) return;
  if (!state.libraries.length) {
    node.innerHTML = '<div class="hint">尚未登记照片库。</div>';
    return;
  }
  node.innerHTML = state.libraries.map((library) => `
    <div class="job">
      <div class="job-head">
        <span class="job-title">#${library.id} ${escapeHtml(library.name)}</span>
        <span class="badge ${library.reachable ? 'online' : 'offline'}">${library.reachable ? '可访问' : '离线'}</span>
      </div>
      <div class="job-meta">当前路径：${escapeHtml(library.root_path_hint || '(未记录)')}</div>
      <div class="row" style="margin-top:8px">
        <input type="text" placeholder="照片库的新位置，例如 E:\\照片" data-rebind-input="${library.id}">
        <button class="btn" data-rebind-preview="${library.id}">预演</button>
        <button class="btn primary" data-rebind-apply="${library.id}">执行重绑定</button>
        <button class="btn" data-offline="${library.id}">标记离线</button>
      </div>
      <pre class="output" id="rebind-output-${library.id}" style="display:none"></pre>
    </div>`).join('');

  node.querySelectorAll('button[data-rebind-preview], button[data-rebind-apply]').forEach((button) => {
    button.onclick = async () => {
      const libraryId = button.dataset.rebindPreview || button.dataset.rebindApply;
      const apply = Boolean(button.dataset.rebindApply);
      const input = node.querySelector(`input[data-rebind-input="${libraryId}"]`);
      const output = $(`#rebind-output-${libraryId}`);
      const path = input.value.trim();
      if (!path) { toast('请先填写照片库的新位置', true); return; }
      button.disabled = true;
      try {
        const result = await API.post(`/api/libraries/${libraryId}/rebind`, { path, apply });
        output.style.display = 'block';
        output.textContent = [
          `判定：${result.action}`,
          `依据：${result.reason}`,
          `找到 ${result.found} 个 / 缺失 ${result.missing} 个`,
          `校验强度：${result.confidence}`,
          ...(result.warnings || []).map((text) => `警告：${text}`),
          apply && result.rebound ? '已重新绑定：原图访问恢复，未重新导入任何文件。' : '',
        ].filter(Boolean).join('\n');
        toast(apply && result.rebound ? '重绑定完成' : '预演完成，请确认后再执行');
        loadLibraries();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    };
  });

  node.querySelectorAll('button[data-offline]').forEach((button) => {
    button.onclick = async () => {
      try {
        await API.post(`/api/libraries/${button.dataset.offline}/offline`);
        toast('已标记离线');
        loadLibraries();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });
}

// --------------------------------------------------------------------- 顶栏与事件
async function refreshHealth() {
  try {
    const health = await API.get('/api/health');
    $('#health-dot').className = 'dot ok';
    const running = health.running_job_id ? ` · 正在执行任务 #${health.running_job_id}` : '';
    // `ui_build` = 服务端算出的**前端资源指纹**。
    // 它的用处只有一个：让"我浏览器里是不是最新那份前端"可核对 ——
    // 这个项目零构建、文件名无内容哈希，出问题时只能靠 Ctrl+F5 猜是不是缓存。
    // 右上角这个号码与 `GET /api/health` 的 `ui_build` 一致，就说明浏览器拿的是最新代码。
    const build = health.ui_build ? ` · 前端 ${health.ui_build}` : '';
    $('#health-text').textContent =
      `v${health.version} · schema ${health.schema_version}${build}${running}`;
    $('#health-text').title = health.ui_build
      ? `前端资源指纹 ${health.ui_build}（改前端会变）。若与 /api/health 不一致，`
        + '说明浏览器还在用旧资源，按 Ctrl+F5 强制刷新。'
      : '';
  } catch (error) {
    $('#health-dot').className = 'dot fail';
    $('#health-text').textContent = `服务不可用：${error.message}`;
  }
}

/**
 * 「库状态」面板：给用户看的健康摘要。
 *
 * ⚠️ 这里**有意不显示配置项与文件系统路径**（数据根、数据库文件、缩略图/备份目录）。
 * 那些是排障用的实现细节：
 *   · 对日常使用者没有意义 —— 他不会去 `C:\PhotoOS\data` 里翻东西；
 *   · 摆在界面上显得不像成品（专业工具不会把内部路径当前提展示）；
 *   · 真需要路径时，「说明」页与日志里都有，而且那时用户是在排障、
 *     有上下文能理解它。
 * 所以这里只回答使用者真正关心的三类问题：**有多少、算完了没有、有没有坏**。
 */
async function refreshAbout() {
  const node = $('#about-output');
  if (!node) return;
  try {
    const [stats, integrity] = await Promise.all([
      API.get('/api/stats'),
      API.get('/api/integrity'),
    ]);
    const t = stats.totals || {};
    const assets = Number(t.assets) || 0;
    const metadata = Number(t.metadata) || 0;
    const hashed = Number(t.hashed) || 0;
    const previews = Number(t.previews) || 0;

    const pct = (n) => (assets ? `（${Math.round((100 * n) / assets)}%）` : '');
    // 覆盖率只在"有照片"时显示百分比，否则会得到一句没有意义的 0%
    const cover = (n) => (assets ? `${n.toLocaleString()} 张${pct(n)}` : `${n.toLocaleString()} 张`);

    const healthy = integrity.quick_check === 'ok' && !Number(integrity.foreign_key_violations);
    const orphans = Number(integrity.orphan_assets || 0)
      + Number(integrity.orphan_metadata || 0)
      + Number(integrity.orphan_previews || 0);

    const libs = (stats.libraries || []).map((lib) => {
      const offline = lib.status && lib.status !== 'ONLINE';
      return `<li class="lib-status-item">
          <span class="lib-status-name">${escapeHtml(lib.name)}</span>
          <span class="lib-status-count">${Number((lib.stats || {}).assets || 0).toLocaleString()} 张</span>
          <span class="lib-status-flag ${offline ? 'is-warn' : ''}">${offline ? '离线' : '在线'}</span>
        </li>`;
    }).join('');

    node.innerHTML = `
      <div class="lib-status-grid">
        <div class="lib-status-cell">
          <span class="lib-status-label">照片与视频</span>
          <span class="lib-status-value">${assets.toLocaleString()}</span>
          <span class="lib-status-sub">${humanBytes(t.bytes)}</span>
        </div>
        <div class="lib-status-cell">
          <span class="lib-status-label">已读拍摄信息</span>
          <span class="lib-status-value">${metadata.toLocaleString()}</span>
          <span class="lib-status-sub">${assets ? `${Math.round((100 * metadata) / assets)}% 覆盖` : '—'}</span>
        </div>
        <div class="lib-status-cell">
          <span class="lib-status-label">已生成缩略图</span>
          <span class="lib-status-value">${previews.toLocaleString()}</span>
          <span class="lib-status-sub">${humanBytes(t.preview_bytes)}</span>
        </div>
        <div class="lib-status-cell">
          <span class="lib-status-label">已算内容哈希</span>
          <span class="lib-status-value">${hashed.toLocaleString()}</span>
          <span class="lib-status-sub">${cover(hashed)}</span>
        </div>
      </div>
      ${libs ? `<ul class="lib-status-list">${libs}</ul>` : ''}
      <div class="lib-status-health ${healthy ? 'is-ok' : 'is-bad'}">
        <span class="lib-status-dot"></span>
        <span>数据库检查：${healthy ? '正常' : '发现问题'}</span>
        <span class="lib-status-sub">
          索引完整性与外键${orphans ? ` · 孤立记录 ${orphans} 条` : ' · 无孤立记录'}
        </span>
      </div>
      <p class="hint tiny">
        需要看数据存放位置或导出诊断信息时，去「说明」页的「数据放在哪里」一节。
      </p>`;
  } catch (error) {
    node.innerHTML = `<p class="hint">读取失败：${escapeHtml(error.message)}</p>`;
  }
}

// ------------------------------------------------- Scene 划分阈值（维护页）
//
// 为什么放在维护页：它们是**全局**参数（对所有时段生效），一年调一两次，
// 不适合摆在精选页的日常工具条上。逐场调整在精选页（合并/拆开）。
const SCENE_FIELDS = {
  'scene-gap-seconds': 'scene_gap_seconds',
  'scene-merge-similarity': 'scene_merge_similarity',
  'scene-unrelated-similarity': 'scene_unrelated_similarity',
  'scene-dissimilar-run': 'scene_dissimilar_run',
  'scene-max-shots': 'scene_max_shots',
};

async function refreshSceneConfig() {
  const hint = $('#scene-config-hint');
  try {
    const config = await API.get('/api/config');
    for (const [elementId, key] of Object.entries(SCENE_FIELDS)) {
      const input = $(`#${elementId}`);
      if (input && config[key] !== undefined) input.value = config[key];
    }
    if (hint) hint.textContent = '已载入当前生效值。保存后写进 data/config.json，下次「现在分组」按新值切。';
  } catch (error) {
    if (hint) hint.textContent = `读配置失败：${error.message}`;
  }
}

async function saveSceneConfig() {
  const hint = $('#scene-config-hint');
  const payload = {};
  for (const [elementId, key] of Object.entries(SCENE_FIELDS)) {
    const input = $(`#${elementId}`);
    if (input && input.value !== '') payload[key] = Number(input.value);
  }
  try {
    const result = await API.post('/api/config/scene', payload);
    const keys = Object.keys(result.updated || {}).length;
    if (hint) hint.textContent = `已保存 ${keys} 项。下次「现在分组」会按新阈值切分。`;
    toast(`已保存 ${keys} 项 Scene 阈值`);
    await refreshSceneConfig();
  } catch (error) {
    if (hint) hint.textContent = `保存失败：${error.message}`;
    toast(`保存失败：${error.message}`, true);
  }
}

// ---------------------------------------------------------------- 添加照片库
//
// 登记一个目录：只读，不移动照片；唯一写入是库根的 `.photoos` 标记（可关）。
// 重复登记同一目录时后端返回 reused=true（换盘场景也走这条）。

function openLibraryAdd() {
  const modal = $('#library-add-modal');
  if (!modal) return;
  modal.hidden = false;
  const hint = $('#library-add-hint');
  if (hint) hint.textContent = '选好目录后点「添加」。';
  const path = $('#library-add-path');
  if (path) { path.value = ''; path.focus(); }
  const name = $('#library-add-name');
  if (name) name.value = '';
}

function closeLibraryAdd() {
  const modal = $('#library-add-modal');
  if (modal) modal.hidden = true;
}

async function submitLibraryAdd() {
  const pathInput = $('#library-add-path');
  const nameInput = $('#library-add-name');
  const marker = $('#library-add-marker');
  const hint = $('#library-add-hint');
  const submit = $('#library-add-submit');
  if (!pathInput) return;
  const path = pathInput.value.trim();
  if (!path) { if (hint) hint.textContent = '先填一个目录'; return; }
  if (hint) hint.textContent = '添加中…';
  if (submit) submit.disabled = true;
  try {
    const result = await API.post('/api/libraries', {
      path,
      name: nameInput ? nameInput.value.trim() : '',
      write_marker: marker ? marker.checked : true,
    });
    if (hint) {
      hint.textContent = result.reused
        ? `已登记过这个库（#${result.library_id}），已更新路径`
        : `已添加「${result.name}」${result.marker_written ? '（已写入标记）' : ''}`;
    }
    toast(result.reused ? '库已存在，已更新路径' : '照片库已添加');
    await loadLibraries();
    setTimeout(closeLibraryAdd, 700);
  } catch (error) {
    if (hint) hint.textContent = `添加失败：${error.message}`;
  } finally {
    if (submit) submit.disabled = false;
  }
}

// ---------------------------------------------------------------- 维护页目录
//
// 维护页分了很多折叠组，用户不知道"下面还有什么"。这里按 DOM 顺序生成目录，
// 点一下：展开该组 + 滚到它上面。
function buildMaintToc() {
  const nav = $('#maint-toc');
  if (!nav) return;
  const groups = [...document.querySelectorAll('#view-maintenance .maint-group')];
  if (!groups.length) return;
  const items = groups.map((group, index) => {
    const strong = group.querySelector('summary b');
    const label = strong ? strong.textContent.trim() : `第 ${index + 1} 组`;
    if (!group.id) group.id = `maint-group-${index + 1}`;
    return `<li><button type="button" data-maint-target="${group.id}">
      <span class="n">${String(index + 1).padStart(2, '0')}</span>${escapeHtml(label)}
    </button></li>`;
  }).join('');
  nav.innerHTML = '<p class="maint-toc-head">目录</p>'
    + `<ol class="maint-toc-list">${items}</ol>`;
  nav.querySelectorAll('button[data-maint-target]').forEach((button) => {
    button.onclick = () => {
      const target = document.getElementById(button.dataset.maintTarget);
      if (!target) return;
      target.open = true;
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
  });
}

// ---------------------------------------------------------------- 诊断信息
async function refreshDiagnostics() {
  const node = $('#diagnostics');
  if (!node) return;
  try {
    const info = await API.get('/api/diagnostics');
    const lines = [
      `版本 ${info.version} · schema ${info.schema_version}/${info.schema_expected}`,
      `数据根 ${info.data_root}`,
      `数据库 ${info.db_path}（${info.db_size_mb} MB）`,
      `服务 ${info.base_url}`,
      `日志 ${info.logs_dir}`,
      `库 ${info.libraries} 个 · 照片 ${info.assets} 张` + (info.ai_python ? ` · AI 解释器已配置` : ' · AI 未配置'),
    ];
    node.textContent = lines.join('\n');
    node.style.whiteSpace = 'pre-wrap';
    node.dataset.raw = lines.join('\n');
  } catch (error) {
    node.textContent = `诊断信息读取失败：${error.message}`;
  }
}

async function copyDiagnostics() {
  const node = $('#diagnostics');
  const text = node && node.dataset.raw ? node.dataset.raw : (node ? node.textContent : '');
  try {
    await navigator.clipboard.writeText(text);
    toast('诊断信息已复制');
  } catch {
    toast('复制失败，请手动选择文本', true);
  }
}

// ---------------------------------------------------------------- 数据库迁移
async function refreshMigrateInfo() {
  const version = $('#migrate-version');
  if (!version) return;
  try {
    const health = await API.get('/api/health');
    version.textContent = String(health.schema_version);
    const expected = $('#migrate-expected');
    if (expected) expected.textContent = String(health.schema_expected);
  } catch (error) {
    version.textContent = '读取失败';
  }
}

async function runMigrate() {
  const result = $('#migrate-result');
  const button = $('#migrate-run');
  if (button) button.disabled = true;
  if (result) result.textContent = '迁移中（会先备份，大库可能需要几十秒）…';
  try {
    const data = await API.post('/api/maintenance/migrate', {});
    const lines = [
      data.ok ? '迁移完成' : '迁移有问题，请查看下面的信息',
      `版本 ${data.version_before} → ${data.version_after}`,
      `备份：${data.backup}`,
    ];
    if (data.regressions && data.regressions.length) {
      lines.push('记录数回退：' + data.regressions.join('；'));
    }
    if (result) result.textContent = lines.join(' · ');
    toast(data.ok ? '迁移完成' : '迁移未通过校验', !data.ok);
    await refreshMigrateInfo();
    await refreshAbout();
  } catch (error) {
    if (result) result.textContent = `迁移失败：${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------- 数据根
//
// 打包版默认数据根在 %LOCALAPPDATA%，用户的旧库可能在别处。
// 这里的职责：**让他看见现在用的是哪个库**，并能在切换前确认"是不是空库"。

let dataRootCandidate = null;

async function refreshDataRoot() {
  const code = $('#dataroot-current');
  const meta = $('#dataroot-meta');
  if (!code) return;
  try {
    const info = await API.get('/api/data-root');
    code.textContent = info.path;
    meta.textContent = `${info.exists ? '库已就绪' : '还没有库（首次启动会新建）'}`
      + ` · 库 ${info.libraries} 个 · 照片 ${info.assets} 张`
      + ` · schema ${info.schema_version ?? '-'}`
      + ` · ${info.writable ? '可写' : '不可写'}`;
  } catch (error) {
    code.textContent = '读取失败';
    meta.textContent = error.message;
  }
}

function setDataRootButtons(state) {
  const useExisting = $('#dataroot-use-existing');
  const create = $('#dataroot-create');
  if (!useExisting || !create) return;
  useExisting.disabled = !(state && state.has_db);
  create.disabled = !(state && state.is_dir && !state.has_db);
}

async function validateDataRoot() {
  const input = $('#dataroot-input');
  const hint = $('#dataroot-hint');
  if (!input) return;
  const path = input.value.trim();
  if (!path) { if (hint) hint.textContent = '先填一个目录'; return; }
  if (hint) hint.textContent = '检查中…';
  try {
    dataRootCandidate = await API.post('/api/data-root/validate', { path });
    const info = dataRootCandidate;
    if (hint) {
      const summary = info.reason
        + (info.has_db ? ` · 这个库里有 ${info.libraries} 个库、${info.assets} 张照片` : '');
      // 以前这里只改一行灰字，用户常说"点了没反应" —— 加上状态色与图标
      hint.textContent = `✓ ${summary}`;
      hint.dataset.state = 'ok';
      hint.style.color = 'var(--gold-500)';
    }
    toast('目录检查通过');
    setDataRootButtons(info);
  } catch (error) {
    if (hint) {
      hint.textContent = `✕ 检查失败：${error.message}`;
      hint.dataset.state = 'error';
      hint.style.color = 'var(--wine-400)';
    }
    setDataRootButtons(null);
  }
}

async function switchDataRoot(mode) {
  const input = $('#dataroot-input');
  const hint = $('#dataroot-hint');
  if (!input) return;
  try {
    const result = await API.post('/api/data-root', { path: input.value.trim(), mode });
    if (hint) hint.textContent = `已保存（${result.data_root}）。请退出并重新打开 PhotoOS。`;
    toast('已保存，重启后生效');
    await refreshDataRoot();
  } catch (error) {
    if (hint) hint.textContent = `保存失败：${error.message}`;
  }
}

// ---------------------------------------------------------------- AI 组件
//
// 「装没装、多大、装完能干什么」由后端算（ai_components.detect），
// 这里只负责画和点。安装是长任务：点完 2 秒轮询一次，直到 job 结束。
let aiPollTimer = null;

async function refreshAiComponents() {
  const box = $('#ai-components');
  if (!box) return;
  try {
    const data = await API.get('/api/ai/components');
    if ($('#ai-pip-mirror') && !$('#ai-pip-mirror').dataset.dirty) {
      $('#ai-pip-mirror').value = data.mirrors.pip_index_url;
    }
    if ($('#ai-model-mirror') && !$('#ai-model-mirror').dataset.dirty) {
      $('#ai-model-mirror').value = data.mirrors.model_base_url;
    }
    box.innerHTML = data.components.map((item) => {
      // 安装中：把 queued / pending 也算进来 —— 入队到真正开跑之间有窗口期，
  // 只认 'running' 的话那段时间既没有进度条、按钮又显示"安装中…"，看着像死掉。
  const jobState = item.job ? String(item.job.state || '') : '';
  const running = ['running', 'queued', 'pending', 'starting'].includes(jobState);
      const blocked = item.blocked_by && item.blocked_by.length;
      const stateText = item.installed ? '已安装'
        : running ? '安装中…'
        : blocked ? `需要先装 ${item.blocked_by.join('、')}`
        : '未安装';
      const unavailable = item.installable === false;
      const action = item.installed
        ? `<button class="btn small ghost" data-ai-uninstall="${item.id}"
             ${running ? 'disabled' : ''}>卸载</button>`
        : `<button class="btn small${running ? ' ghost' : ''}" data-ai-install="${item.id}"
             ${running || blocked || unavailable ? 'disabled' : ''}
             title="${unavailable ? escapeHtml(item.unavailable || '') : ''}"
           >${running ? '安装中…' : '安装'}</button>`;
      // 进度条独占一行（塞进按钮格只有几十像素宽，等于看不见）。
      // 除了"安装中"，**已完成/失败也留一条**：否则用户点完 55 MB 的小组件，
      // 条子一闪就没，看起来永远"没有进度条"（实测反馈）。
      // 只在**安装进行中**显示进度条。完成/失败留着一条金色实线会被当成
      // 分隔线（实测被指"多一个进度条"），状态改用右侧文字表达。
      const bar = running
        ? `<div class="ai-bar is-running" role="progressbar" aria-label="安装进度"><i></i></div>`
        : '';
      return `
        <div class="ai-item">
        <div class="ai-row${item.installed ? ' is-installed' : ''}${blocked ? ' is-blocked' : ''}${unavailable ? ' is-unavailable' : ''}">
          <div>
            <div class="ai-name">${escapeHtml(item.label)}</div>
            <div class="ai-note">${escapeHtml(item.unlocks || '')}${item.note ? ' · ' + escapeHtml(item.note) : ''}</div>
          </div>
          <div class="ai-size">${item.size_mb} MB</div>
          <div class="ai-state">${escapeHtml(unavailable ? (item.unavailable || '本机不可用') : stateText)}</div>
          <div>${action}</div>
        </div>
        ${bar}
        </div>`;
    }).join('');
    const running = data.components.some((item) => item.job && item.job.state === 'running');
    box.querySelectorAll('button[data-ai-install]').forEach((button) => {
      button.onclick = () => startAiInstall(button.dataset.aiInstall);
    });
    box.querySelectorAll('button[data-ai-uninstall]').forEach((button) => {
      button.onclick = () => uninstallAiComponent(button.dataset.aiUninstall);
    });
    const failed = data.components.find((item) => item.job && item.job.state === 'error');
    if (failed) {
      const hint = $('#ai-mirror-hint');
      if (hint) hint.textContent = `${failed.label} 安装失败：${failed.job.error || ''}`;
    }
    if (running && !aiPollTimer) {
      aiPollTimer = setInterval(refreshAiComponents, 2000);
    } else if (!running && aiPollTimer) {
      clearInterval(aiPollTimer);
      aiPollTimer = null;
    }
  } catch (error) {
    box.innerHTML = `<div class="hint">AI 组件状态读取失败：${escapeHtml(error.message)}</div>`;
  }
}

async function startAiInstall(componentId) {
  // 体积大的组件先问一次：4.3 GB 不是"点错了也无所谓"的量级
  const rows = document.querySelectorAll('.ai-row');
  let sizeMb = 0;
  let label = componentId;
  rows.forEach((row) => {
    const button = row.querySelector('button[data-ai-install]');
    if (button && button.dataset.aiInstall === componentId) {
      const sizeNode = row.querySelector('.ai-size');
      const nameNode = row.querySelector('.ai-name');
      if (sizeNode) sizeMb = parseInt(sizeNode.textContent, 10) || 0;
      if (nameNode) label = nameNode.textContent.trim();
    }
  });
  if (sizeMb >= 1000) {
    const ok = confirm(
      `「${label}」约 ${sizeMb} MB，下载与安装可能需要十几分钟，\n` +
      '并占用同等量磁盘空间。\n\n现在开始吗？');
    if (!ok) return;
  }
  try {
    await API.post(`/api/ai/components/${componentId}/install`, {});
    await refreshAiComponents();
  } catch (error) {
    toast(`安装失败：${error.message}`, true);
  }
}

async function uninstallAiComponent(componentId) {
  const ok = confirm('确定卸载吗？\n\n这会删除对应的 pip 包 / 模型文件（Gemma 还会删掉独立环境），\n重新使用需要重新下载。');
  if (!ok) return;
  const hint = $('#ai-cleanup-hint');
  if (hint) hint.textContent = '卸载中…';
  try {
    const result = await API.post(`/api/ai/components/${componentId}/uninstall`, {});
    if (hint) hint.textContent = `已卸载：${(result.removed || []).join('、') || componentId}`;
    toast('已卸载');
    await refreshAiComponents();
  } catch (error) {
    if (hint) hint.textContent = `卸载失败：${error.message}`;
  }
}

async function cleanupAiLeftovers() {
  const hint = $('#ai-cleanup-hint');
  if (hint) hint.textContent = '清理中…';
  try {
    const result = await API.post('/api/ai/components/cleanup', {});
    const removed = (result.removed || []).length;
    if (hint) hint.textContent = removed
      ? `已清理 ${removed} 项，释放 ${result.freed_mb} MB`
      : '没有需要清理的残留';
    toast(removed ? `释放 ${result.freed_mb} MB` : '没有残留');
    await refreshAiComponents();
  } catch (error) {
    if (hint) hint.textContent = `清理失败：${error.message}`;
  }
}

async function saveAiMirrors() {
  const hint = $('#ai-mirror-hint');
  const payload = {};
  if ($('#ai-pip-mirror')) payload.pip_index_url = $('#ai-pip-mirror').value.trim();
  if ($('#ai-model-mirror')) payload.model_base_url = $('#ai-model-mirror').value.trim();
  try {
    const result = await API.post('/api/ai/mirrors', payload);
    if ($('#ai-pip-mirror')) $('#ai-pip-mirror').dataset.dirty = '';
    if ($('#ai-model-mirror')) $('#ai-model-mirror').dataset.dirty = '';
    if (hint) hint.textContent = `已保存：${result.mirrors.pip_index_url} · ${result.mirrors.model_base_url}`;
    toast('镜像已保存');
  } catch (error) {
    if (hint) hint.textContent = `保存失败：${error.message}`;
  }
}

async function testAiMirror(which) {
  const hint = $('#ai-mirror-hint');
  const input = which === 'pip' ? $('#ai-pip-mirror') : $('#ai-model-mirror');
  if (!input) return;
  if (hint) hint.textContent = '测试中…';
  try {
    const result = await API.post('/api/ai/mirrors/test', { url: input.value.trim() });
    if (hint) hint.textContent = result.ok ? `连通（${result.detail}）` : `不通：${result.detail}`;
  } catch (error) {
    if (hint) hint.textContent = `测试失败：${error.message}`;
  }
}

function bindEvents() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => setView(tab.dataset.view);
  });

  // 原片弹窗：Esc 与点背景都能关（"看大图"这件事必须能一键退出）
  const modalClose = $('#asset-modal-close');
  if (modalClose) modalClose.onclick = () => closeAssetDetail();
  const backdrop = $('#asset-modal-backdrop');
  if (backdrop) backdrop.onclick = () => closeAssetDetail();
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeAssetDetail();
  });

  const apply = () => loadAssets();
  $('#filter-type').onchange = (e) => { state.filters.file_type = e.target.value; apply(); };
  $('#filter-status').onchange = (e) => { state.filters.status = e.target.value; apply(); };
  $('#filter-camera').onchange = (e) => { state.filters.camera = e.target.value; apply(); };
  $('#filter-lens').onchange = (e) => { state.filters.lens = e.target.value; apply(); };
  $('#filter-year').onchange = (e) => { state.filters.year = e.target.value; apply(); };
  $('#filter-sort').onchange = (e) => { state.filters.sort = e.target.value; apply(); };
  $('#filter-nometa').onchange = (e) => { state.filters.noMeta = e.target.checked; apply(); };
  $('#page-size').onchange = (e) => { state.pageSize = Number(e.target.value); apply(); };
  $('#load-more').onclick = () => loadMore();

  // 焦距范围：边打字边筛会每敲一个数字就查一次库（2 万张也要几百毫秒），
  // 所以**防抖** 350ms —— 停下就生效，不用按回车，也不会打一半就发请求。
  // 关键词同理。
  const debounce = (fn, ms) => {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  };
  const applyFocal = debounce(apply, 350);
  const readFocal = () => {
    const lo = $('#filter-focal-min').value.trim();
    const hi = $('#filter-focal-max').value.trim();
    state.filters.focalMin = lo === '' ? '' : Number(lo);
    state.filters.focalMax = hi === '' ? '' : Number(hi);
    markActivePreset();
    applyFocal();
  };
  $('#filter-focal-min').oninput = readFocal;
  $('#filter-focal-max').oninput = readFocal;
  // 回车立即生效（不等人停手）
  ['#filter-focal-min', '#filter-focal-max'].forEach((sel) => {
    $(sel).onkeydown = (event) => {
      if (event.key === 'Enter') { event.preventDefault(); readFocal(); loadAssets(); }
    };
  });
  const applyKeyword = debounce(apply, 350);

  // ---- 关键词：跨字段的**纯文本**匹配（文件名 / 机型 / 镜头）----
  // 与「内容」是两回事：这个走 SQL 的 LIKE，不经过模型。
  // ⚠️ 只绑**一次** `oninput`。改造前 `#filter-q` 被绑了两次（350ms 与 260ms 各一次，
  // 后者覆盖前者）—— 那种"同一件事写两遍"正是这次要清掉的病。
  const keywordInput = $('#filter-q');
  if (keywordInput) {
    keywordInput.oninput = (e) => {
      state.filters.q = e.target.value.trim();
      applyKeyword();
    };
    keywordInput.onkeydown = (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      state.filters.q = event.target.value.trim();
      loadAssets();
    };
  }

  // ---- 内容维度：下拉选已备好的词，或直接打一个词 ----
  const contentPick = $('#filter-content');
  if (contentPick) {
    contentPick.onchange = (e) => {
      state.filters.contentQ = e.target.value;
      const typed = $('#filter-content-q');
      if (typed) typed.value = '';            // 两处只能有一个生效，避免"到底按哪个算"
      apply();
    };
  }
  const contentTyped = $('#filter-content-q');
  if (contentTyped) {
    contentTyped.oninput = (e) => {
      state.filters.contentQ = e.target.value.trim();
      applyKeyword();
    };
    contentTyped.onkeydown = (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      state.filters.contentQ = event.target.value.trim();
      loadAssets();
    };
  }

  // ---- 人物维度：加人 / 去人（多个人是 AND，见 compose.person_clause）----
  const personAdd = $('#filter-person-add');
  if (personAdd) {
    personAdd.onchange = (e) => {
      addPerson(e.target.value);
      e.target.value = '';                    // 选完复位，方便连着加下一个
    };
  }
  const chips = $('#filter-people-chips');
  if (chips) {
    chips.onclick = (event) => {
      const button = event.target.closest('[data-drop-person]');
      if (button) dropPerson(button.dataset.dropPerson);
    };
  }

  // ---- 顶栏筛选台：展开/收起 ----
  const toggle = $('#lf-toggle');
  if (toggle) toggle.onclick = () => toggleFilterPanel();
  const resetBtn = $('#filter-reset');
  if (resetBtn) resetBtn.onclick = () => resetAllFilters();

  // 浏览轴：切换 + 节点选择 + 面包屑回退（事件委托，树是动态重绘的）
  document.querySelectorAll('#browse-axis .seg').forEach((button) => {
    button.onclick = () => setBrowseAxis(button.dataset.axis);
  });
  $('#browse-list').onclick = (event) => {
    const node = event.target.closest('.browse-node');
    if (!node) return;
    const found = state.browse.nodes.find((item) => item.key === node.dataset.key);
    if (found) selectBrowseNode(found);
  };
  $('#grid-crumbs').onclick = (event) => {
    const crumb = event.target.closest('[data-crumb]');
    if (crumb) popBrowseTo(Number(crumb.dataset.crumb));
  };

  // 无限滚动：接近底部就续拉下一页（虚拟网格只监听一次滚动）
  grid.setOnNearBottom(() => {
    if (state.offset < state.total) loadMore();
  });

  document.querySelectorAll('button[data-run]').forEach((button) => {
    button.onclick = () => startJob(button.dataset.run);
  });

  // 导入面板：任一输入变化都必须让上一次的预演失效（不能让用户拿旧计划去执行）
  ['#import-source', '#import-library', '#import-video-library', '#import-limit', '#import-city'].forEach((sel) => {
    const node = $(sel);
    if (!node) return;
    node.addEventListener(sel === '#import-source' || sel === '#import-city' ? 'input' : 'change', invalidateImportPlan);
  });
  const rootPick = $('#import-root-pick');
  if (rootPick) {
    const bridge = window.photoosDesktop || window.desktopBridge || window.photoos;
    if (bridge && typeof bridge.pickDirectory === 'function') {
      rootPick.hidden = false;
      rootPick.onclick = async () => {
        const picked = await bridge.pickDirectory($('#import-root')?.value || null);
        if (picked) {
          const input = $('#import-root');
          if (input) input.value = picked;
          localStorage.setItem('photoos.libraryRoot', String(picked));
        }
      };
    }
  }
  const rootField = $('#import-root');
  if (rootField && !rootField.value) {
    rootField.value = localStorage.getItem('photoos.libraryRoot') || '';
    rootField.addEventListener('change', () => localStorage.setItem('photoos.libraryRoot', rootField.value.trim()));
  }
  $('#btn-import-plan').onclick = () => runImportPlan();
  $('#btn-import-apply').onclick = () => runImportApply();

  $('#btn-integrity').onclick = async () => {
    const output = $('#tools-output');
    output.textContent = '检查中…';
    try {
      const result = await API.get('/api/integrity');
      output.textContent = JSON.stringify(result, null, 2);
    } catch (error) { output.textContent = `失败：${error.message}`; }
  };

  // ---- 添加照片库
  const addButton = $('#library-add');
  if (addButton) addButton.onclick = openLibraryAdd;
  const addClose = $('#library-add-close');
  if (addClose) addClose.onclick = closeLibraryAdd;
  const addCancel = $('#library-add-cancel');
  if (addCancel) addCancel.onclick = closeLibraryAdd;
  const addSubmit = $('#library-add-submit');
  if (addSubmit) addSubmit.onclick = submitLibraryAdd;
  const addModal = $('#library-add-modal');
  if (addModal) addModal.onclick = (event) => { if (event.target === addModal) closeLibraryAdd(); };
  const addPick = $('#library-add-pick');
  const bridge = typeof window !== 'undefined' ? window.photoosDesktop : null;
  if (addPick && bridge && typeof bridge.pickDirectory === 'function') {
    addPick.hidden = false;
    addPick.onclick = async () => {
      const picked = await bridge.pickDirectory(null);
      if (picked) {
        const input = $('#library-add-path');
        if (input) input.value = picked;
      }
    };
  }

  // ---- 维护页目录
  buildMaintToc();

  // ---- 诊断信息
  const diagCopy = $('#diag-copy');
  if (diagCopy) diagCopy.onclick = copyDiagnostics;
  refreshDiagnostics();

  // ---- 数据库迁移
  const migrateButton = $('#migrate-run');
  if (migrateButton) migrateButton.onclick = runMigrate;
  refreshMigrateInfo();

  // ---- 数据根
  const drValidate = $('#dataroot-validate');
  if (drValidate) drValidate.onclick = validateDataRoot;
  const drUse = $('#dataroot-use-existing');
  if (drUse) drUse.onclick = () => switchDataRoot('existing');
  const drCreate = $('#dataroot-create');
  if (drCreate) drCreate.onclick = () => switchDataRoot('create');
  const drInput = $('#dataroot-input');
  if (drInput) drInput.oninput = () => setDataRootButtons(null);
  const drPick = $('#dataroot-pick');
  const desktopBridge = typeof window !== 'undefined' ? window.photoosDesktop : null;
  if (drPick && desktopBridge && typeof desktopBridge.pickDirectory === 'function') {
    drPick.hidden = false;
    drPick.onclick = async () => {
      const picked = await desktopBridge.pickDirectory(drInput ? drInput.value : null);
      if (picked && drInput) { drInput.value = picked; await validateDataRoot(); }
    };
  }
  refreshDataRoot();

  // ---- AI 组件：状态列表 + 镜像
  const importEntry = $('#library-import');
  if (importEntry) importEntry.onclick = showImportPanel;

  const sideJobs = $('#side-jobs');
  if (sideJobs) sideJobs.onclick = showJobs;

  const aiCleanup = $('#ai-cleanup');
  if (aiCleanup) aiCleanup.onclick = cleanupAiLeftovers;
  const aiSave = $('#ai-mirror-save');
  if (aiSave) aiSave.onclick = saveAiMirrors;
  const aiPipTest = $('#ai-pip-test');
  if (aiPipTest) aiPipTest.onclick = () => testAiMirror('pip');
  const aiModelTest = $('#ai-model-test');
  if (aiModelTest) aiModelTest.onclick = () => testAiMirror('model');
  ['#ai-pip-mirror', '#ai-model-mirror'].forEach((selector) => {
    const input = $(selector);
    if (input) input.oninput = () => { input.dataset.dirty = '1'; };
  });
  refreshAiComponents();

  // ---- Scene 阈值（维护页）：读当前值 / 保存
  const sceneSave = $('#btn-scene-config-save');
  if (sceneSave) sceneSave.onclick = saveSceneConfig;
  refreshSceneConfig();

  // ---- 桌面版专属：退出（连后端进程一起收走）。浏览器里没有这个入口。
  // 系统区：设置（跳维护页）/ 退出（仅桌面壳）
  const sideSettings = $('#side-settings');
  if (sideSettings) sideSettings.onclick = () => setView('maintenance');
  const desktop = typeof window !== 'undefined' ? window.photoosDesktop : null;
  const quitButton = $('#btn-quit-side');
  if (quitButton && desktop && typeof desktop.quitApp === 'function') {
    quitButton.hidden = false;
    quitButton.onclick = async () => {
      quitButton.disabled = true;
      try { await desktop.quitApp(); } finally { quitButton.disabled = false; }
    };
  }
  $('#btn-backup').onclick = async () => {
    const output = $('#tools-output');
    output.textContent = '备份中…';
    try {
      const result = await API.post('/api/maintenance/backup');
      output.textContent = `已备份到：${result.path}\n大小：${humanBytes(result.bytes)}`;
    } catch (error) { output.textContent = `失败：${error.message}`; }
  };
  $('#btn-duplicates').onclick = async () => {
    const output = $('#tools-output');
    output.textContent = '统计中…（只读数据库，不扫描原片）';
    try {
      const result = await API.post('/api/maintenance/duplicates', { library_id: state.activeLibraryId });
      const lines = [
        `重复组：${result.groups}`,
        `多余副本：${result.redundant_copies}`,
        `可回收：${humanBytes(result.reclaimable_bytes)}`,
        '',
        result.note,
      ];
      for (const sample of result.samples || []) {
        lines.push('', `组 ${sample.hash.slice(0, 16)}…（${sample.paths.length} 个内容相同的文件）`);
        for (const path of sample.paths.slice(0, 6)) lines.push(`  - ${path}`);
      }
      if (!result.groups) lines.push('', '没有发现重复内容（若刚导入尚未哈希，请先运行「计算内容哈希」）。');
      output.textContent = lines.join('\n');
    } catch (error) { output.textContent = `失败：${error.message}`; }
  };

  // ------------------------------------------------------------ 命令（界面直跑）
  // 命令目录与执行都在 shared 的 actions.js 里，这里只挂两个入口。
  // 维护页给"完整列表"，弹窗给"顺手挑一条"。
  const actionsPanel = $('#actions-panel');
  if (actionsPanel) renderActionsPanel(actionsPanel).catch(() => {});
  const actionsDialog = $('#btn-actions-dialog');
  if (actionsDialog) {
    actionsDialog.onclick = () => openActionsDialog({ title: '可直跑的命令' });
  }
  // 让 actions.js 能在命令跑完后刷新任务表（它不 import 本文件，避免循环依赖）
  window.photoosRefreshJobs = () => { refreshJobs().catch(() => {}); };
}

/** 焦距快捷区间的按钮：由后端按真实分布给出（含每个区间的张数）。 */
function renderFocalPresets(focal) {
  const node = $('#focal-presets');
  const note = $('#focal-note');
  if (!node) return;
  const presets = (focal && focal.presets) || [];
  node.innerHTML = presets.map((p) => `
    <button type="button" class="focal-preset" data-min="${p.min ?? ''}" data-max="${p.max ?? ''}"
            title="共 ${p.count} 张">
      ${escapeHtml(p.label)}<span class="focal-preset-n">${p.count}</span>
    </button>`).join('');
  node.querySelectorAll('.focal-preset').forEach((button) => {
    button.onclick = () => {
      const min = button.dataset.min;
      const max = button.dataset.max;
      $('#filter-focal-min').value = min;
      $('#filter-focal-max').value = max;
      state.filters.focalMin = min === '' ? '' : Number(min);
      state.filters.focalMax = max === '' ? '' : Number(max);
      markActivePreset();
      loadAssets();
    };
  });
  if (note && focal) {
    // 说清楚"不筛焦距"与"筛了会排除哪些" —— 否则用户会以为丢照片
    const missing = Number(focal.count_without_focal) || 0;
    note.textContent = missing
      ? `共 ${Number(focal.count_with_focal).toLocaleString()} 张有焦距信息；`
        + `另有 ${missing.toLocaleString()} 张没有（视频 / 缺拍摄信息），`
        + `按焦距筛选时不会出现。`
      : '';
  }
}

/** 高亮当前生效的快捷区间（两个输入框正好等于某个区间时）。 */
function markActivePreset() {
  const node = $('#focal-presets');
  if (!node) return;
  const lo = $('#filter-focal-min').value.trim();
  const hi = $('#filter-focal-max').value.trim();
  node.querySelectorAll('.focal-preset').forEach((button) => {
    const same = (button.dataset.min || '') === lo && (button.dataset.max || '') === hi;
    button.classList.toggle('is-active', same && (lo !== '' || hi !== ''));
  });
}

async function loadFacets() {
  try {
    const payload = await API.get(`/api/facets${state.activeLibraryId !== null ? `?library_id=${state.activeLibraryId}` : ''}`);
    const camera = $('#filter-camera');
    camera.innerHTML = '<option value="">全部</option>' + (payload.cameras || [])
      .map((row) => `<option value="${escapeHtml(row.value)}">${escapeHtml(row.value)}（${row.count}）</option>`).join('');
    const year = $('#filter-year');
    year.innerHTML = '<option value="">全部</option>' + (payload.years || [])
      .map((row) => `<option value="${escapeHtml(row.value)}">${escapeHtml(row.value)} 年（${row.count}）</option>`).join('');
    // 镜头：与机型同一种控件。实测本库只有 4 支，下拉完全放得下；
    // 后端 LIMIT 50 兜住镜头很多的情况。
    const lens = $('#filter-lens');
    if (lens) {
      lens.innerHTML = '<option value="">全部</option>' + (payload.lenses || [])
        .map((row) => `<option value="${escapeHtml(row.value)}">${escapeHtml(row.value)}（${row.count}）</option>`).join('');
      lens.value = state.filters.lens || '';
    }
    renderFocalPresets(payload.focal);
    // 重建下拉框会丢掉当前选中值 —— 复位它，否则用户会看到筛选"自己跳回去了"
    camera.value = state.filters.camera || '';
    year.value = state.filters.year || '';
  } catch (error) {
    // 筛选器属于增强功能，失败不打断主流程
    console.warn('facets 加载失败', error);
  }
}

// --------------------------------------------------------------------- 导入
/** 填充导入面板的库下拉框（照片库 / 视频库）。 */
function renderImportLibraries() {
  const photo = $('#import-library');
  const video = $('#import-video-library');
  if (!photo || !video) return;
  const options = state.libraries.map(
    (library) => `<option value="${library.id}">#${library.id} ${escapeHtml(library.name)}</option>`,
  ).join('');
  photo.innerHTML = options || '<option value="">（尚未登记照片库）</option>';
  if (state.activeLibraryId !== null) photo.value = String(state.activeLibraryId);
  video.innerHTML = '<option value="">（源中不含视频时可不选）</option>' + options;
  // 视频库默认选名字里带「视频」的那个（否则留空，让后端明确报错而不是猜）
  const guessed = state.libraries.find((library) => library.name.includes('视频'));
  if (guessed) video.value = String(guessed.id);
  invalidateImportPlan();
}

function invalidateImportPlan() {
  state.importPlan = null;
  const apply = $('#btn-import-apply');
  if (apply) { apply.disabled = true; apply.textContent = '确认执行'; }
}

function importPayload(extra = {}) {
  const video = $('#import-video-library').value;
  const limit = Number($('#import-limit').value) || 300;
  const city = ($('#import-city')?.value || '').trim();
  return {
    source: $('#import-source').value.trim(),
    library_id: Number($('#import-library').value) || null,
    video_library_id: video ? Number(video) : null,
    // `limit` 只用于预演抽样 —— **执行时不带它**，否则「确认执行」只会导入被抽样的那部分。
    limit,
    city: city || null,
    ...extra,
  };
}

/** 执行导入时的载荷：**刻意不带 `limit`**。
 *
 * `limit` 只用于预演抽样；执行必须覆盖源里的全部文件，否则点「确认执行」
 * 只会导入被抽样的那一小部分（真实踩到：预演抽样 30，用户以为会处理整张卡）。
 * 服务端也已改为只看 `max_files`，两边都不依赖 `limit` 的语义。
 */
function importApplyPayload() {
  const { limit, ...rest } = importPayload({ confirmed: true });
  return rest;
}

function renderImportPlan(plan) {
  const node = $('#import-result');
  const rows = plan.files.slice(0, 200).map((item) => `
    <tr>
      <td>${escapeHtml(item.name)}</td>
      <td class="mono">${escapeHtml(item.target)}</td>
      <td>${escapeHtml(item.city_how)}</td>
      <td>${item.time_unknown ? '<span class="warn-text">时区未知</span>' : escapeHtml(item.time_how)}</td>
    </tr>`).join('');

  const cities = Object.entries(plan.by_city)
    .map(([city, count]) => `${escapeHtml(city)} ${count}`).join('，') || '—';
  const hows = Object.entries(plan.by_city_how)
    .map(([how, count]) => `${escapeHtml(how)} ${count}`).join('，') || '—';
  const libs = Object.entries(plan.by_library)
    .map(([id, count]) => `Library #${escapeHtml(id)} ${count} 个`).join('，') || '—';

  node.innerHTML = `
    <div class="plan-summary">
      <div><span>源中媒体</span><b>${plan.source_total} 个文件 · ${humanBytes(plan.source_bytes)}</b>
        ${plan.preview_sampled
          ? `<span class="warn-text">本次预演只抽样了 ${plan.total} 个</span>`
          : '<span class="hint tiny">已全部预演</span>'}</div>
      <div><span>本次预演</span><b>${plan.total} 个文件</b>
        （${Object.entries(plan.by_kind).map(([k, v]) => `${escapeHtml(k)} ${v}`).join('，') || '无'}）</div>
      <div><span>分流</span><b>${libs}</b></div>
      ${plan.city_override ? `<div><span>强制城市</span><b>${escapeHtml(plan.city_override)}</b>
        <span class="hint tiny">压过 GPS 判定</span></div>` : ''}
      <div><span>按城市</span><b>${cities}</b></div>
      <div><span>城市依据</span><b>${hows}</b></div>
      <div><span>时段</span><b>${plan.sessions.length} 个</b></div>
      ${plan.unknown_city_count ? `<div><span>地点未知</span><b class="warn-text">${plan.unknown_city_count} 个 → 未知地点</b>
        <span class="hint tiny">若这批其实拍在同一个地方，请填「强制城市」后重新预演</span></div>` : ''}
      ${plan.unknown_time_count ? `<div><span>时区未知</span><b class="warn-text">${plan.unknown_time_count} 个</b></div>` : ''}
      ${plan.unresolved_cells.length ? `<div><span>未命中网格</span><b>${plan.unresolved_cells.length} 个</b>
        <span class="hint tiny">境外坐标通常全部未命中；用「强制城市」比逐格指认更省事</span></div>` : ''}
      ${plan.skipped_total ? `<div><span>跳过</span><b>${plan.skipped_total} 个</b>
        <span class="hint tiny">${escapeHtml(plan.skipped.slice(0, 3).map((s) => s.reason).join('；'))}</span></div>` : ''}
      <div><span>预演耗时</span><b>${plan.seconds} 秒</b></div>
    </div>
    ${plan.notes.map((note) => `<div class="notice">${escapeHtml(note)}</div>`).join('')}
    ${rows ? `<div class="plan-table-wrap"><table class="plan-table">
      <thead><tr><th>文件</th><th>目标路径</th><th>城市依据</th><th>时间依据</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${plan.files.length > 200 ? `<p class="hint tiny">仅显示前 200 条${plan.files_truncated ? `，另有 ${plan.files_truncated} 条被截断` : ''}。</p>` : ''}` : ''}
    <p class="hint"><strong>以上只是预演，尚未复制任何文件。</strong>
      确认后点「确认执行」—— 它会处理源里的<strong>全部 ${plan.source_total} 个文件</strong>（可在下方任务列表查看进度、暂停或取消）。</p>`;

  const apply = $('#btn-import-apply');
  apply.disabled = plan.total === 0;
  apply.textContent = plan.total
    ? `确认执行（全部 ${plan.source_total} 个文件）`
    : '确认执行';
}

/** 导入前确保"照片落在哪"对应的库存在；没有就用这个路径建一个（单库模型）。 */
async function ensureImportLibrary() {
  const select = $('#import-library');
  const rootInput = $('#import-root');
  if (select && select.value) return true;          // 已有库
  const root = (rootInput?.value || '').trim();
  const hint = $('#import-root')?.parentElement?.querySelector('.hint');
  if (!root) {
    if (hint) hint.textContent = '先填「照片落在哪」：这是照片库的位置，照片会复制到这里。';
    return false;
  }
  try {
    const created = await API.post('/api/libraries', { path: root });
    localStorage.setItem('photoos.libraryRoot', root);
    if (select) {
      const id = created.library_id || (created.library && created.library.id);
      select.innerHTML = `<option value="${id}">#${id} ${escapeHtml(created.name || root)}</option>`;
      select.value = String(id);
    }
    toast(created.reused ? '已使用现有照片库' : '已创建照片库');
    return true;
  } catch (error) {
    if (hint) hint.textContent = `照片库创建失败：${error.message}`;
    return false;
  }
}

async function runImportPlan() {
  if (!(await ensureImportLibrary())) return;
  const payload = importPayload();
  if (!payload.source) { toast('请先填写源目录', true); return; }
  const node = $('#import-result');
  node.innerHTML = '<p class="hint">正在读取拍摄时间与 GPS 并计算归类计划…（RAW 需要遍历整个文件，可能较慢）</p>';
  invalidateImportPlan();
  try {
    const plan = await API.post('/api/import/plan', payload);
    state.importPlan = payload;
    renderImportPlan(plan);
  } catch (error) {
    node.innerHTML = `<div class="notice">预演失败：${escapeHtml(error.message)}</div>`;
  }
}

async function runImportApply() {
  if (!(await ensureImportLibrary())) return;
  if (!state.importPlan) { toast('请先预演', true); return; }
  const apply = $('#btn-import-apply');
  apply.disabled = true;
  try {
    const job = await API.post('/api/import/apply', importApplyPayload());
    toast(`已创建导入任务 #${job.id}（处理源中全部文件），可在下方任务列表查看进度`);
    // 导入任务会在末尾**自动带上派生分组**（见 jobs/runner.py 的 `_run_regroup`），
    // 所以跑完精选页就有内容了。这里把这件事说清楚，免得用户以为还要再点一次。
    // 反馈就放在按钮下面（不放页面底部）：点了执行，视线不该离开这一块。
    state.importJobId = Number(job.id);
    const node = $('#import-result');
    if (node) {
      node.innerHTML = '<div class="notice">已开始导入：复制文件 → 建立索引 → 生成缩略图 → <strong>自动分组</strong>'
        + '（分组完成后精选页就有内容，不需要再点别的）。</div>';
    }
    renderImportTask(job);
    await refreshJobs();
    await loadBrowseNodes();
    await loadAssets();
    await loadLibraries();
  } catch (error) {
    toast(`创建导入任务失败：${error.message}`, true);
  } finally {
    apply.disabled = false;
    invalidateImportPlan();
  }
}

// --------------------------------------------------------------------- 启动
async function boot() {
  bindEvents();
  grid.init();
  // ⚠️ 必须显式设一次视图。照片库靠 HTML 上的 `is-active` 默认显示，
  // 所以 `boot()` 里原来**没有** `setView` 调用 —— 而 `setView` 里有
  // "显示筛选台 / 收起筛选台"的逻辑，不调它的话筛选台会一直是 `hidden`，
  // 而 `hidden` 元素里的 select 点了没有任何反应（实测踩过：整块筛选台失效，
  // 表现为"人物名单加载不出来""选了内容词没反应"）。
  setView('library');
  await refreshHealth();
  await loadLibraries();
  // 先建浏览树再拉列表：列表的作用域来自浏览轴，顺序反了会先拉一次全库
  await loadBrowseNodes();
  await loadAssets();
  await loadFacets();
  await refreshJobs();
  await refreshAbout();

  state.pollTimer = setInterval(() => {
    refreshHealth();
    refreshJobs();
    if (state.view === 'library') {
      // 索引在跑时，定期刷新统计与列表，让用户看到进度带来的变化
      const active = state.jobs.some((job) => ['PROCESSING', 'PENDING', 'RETRY'].includes(job.status));
      if (active) {
        loadAssets();
        loadLibraries();
      }
    }
  }, 4000);
}

boot().catch((error) => {
  console.error(error);
  toast(`初始化失败：${error.message}`, true);
});
