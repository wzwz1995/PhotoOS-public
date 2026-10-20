/**
 * PhotoOS · 前端共享工具（零构建 ES module）
 *
 * 存在的理由：`app.js` 在模块顶层会**立即启动应用**（boot()、轮询、事件绑定）。
 * 因此 Phase 2 的新模块**不能 import app.js** —— 那会在导入时把应用再启动一遍。
 * 把"无副作用"的东西（API 客户端、转义、格式化）抽到这里，双方共用一份实现。
 *
 * 纪律：本文件**不得有顶层副作用**（不查 DOM、不发请求、不绑事件）。
 */

export const API = {
  async get(path) {
    return handle(await fetch(path, { headers: { Accept: 'application/json' } }));
  },
  async post(path, body) {
    return handle(await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }));
  },
  async patch(path, body) {
    return handle(await fetch(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }));
  },
  async del(path) {
    return handle(await fetch(path, { method: 'DELETE', headers: { Accept: 'application/json' } }));
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
    error.status = response.status;
    throw error;
  }
  return payload;
}

/** `document.querySelector` 的短别名。 */
export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => Array.from(document.querySelectorAll(selector));

/**
 * 加载中的**统一写法**。
 *
 * 之前每个视图各写一句：`查询中…` / `筛选中…` / `载入中…` / `正在找…（拿当前判据…）`
 * —— 同一个状态四种说法，而且有的用 `class="hint"`、有的干脆什么都不加。
 * 现在统一成一个组件（`.wb-empty.is-loading`）+ 一个动词家族（"正在…"）。
 */
export function loadingHtml(text = '正在读取…') {
  return `<div class="wb-empty is-loading"><span class="fleuron">❦</span>`
    + `<p>${escapeHtml(text)}</p></div>`;
}

/**
 * 失败的**统一写法** —— 与 `loadingHtml` 同一个壳子，只是换成酒红。
 *
 * 为什么需要它：以前"加载失败"几乎一律走 `toast()`，而 toast 六秒就自己消失。
 * 用户在别处忙一下回来，屏幕上什么都没有，只看到一块空的地方 ——
 * **分不清"失败了"和"本来就没有"**。失败必须是一个**留得住**的状态，
 * 并且带上"接下来能做什么"（`hint`，通常是一句可以照做的操作）。
 *
 * 有 `retry` 时给一个「重试」按钮：调用方必须同时调 `bindRetryButtons(root)`
 * （或直接用 `bindRetry(button, fn)`）把回调接上 —— 本函数只产出标记。
 *
 * @param {string} what   什么东西没读出来（"照片列表" / "场景" / "语义结果"…）
 * @param {string} detail 具体原因（通常传 `error.message`）
 * @param {{retry?: Function, hint?: string}} [options]
 */
export function errorHtml(what, detail, { retry = null, hint = '' } = {}) {
  const button = typeof retry === 'function'
    ? '<button class="btn small" data-retry="1">重试</button>'
    : '';
  return `<div class="wb-empty is-error"><span class="fleuron">❦</span>`
    + `<p><strong>${escapeHtml(what)}没能读出来。</strong></p>`
    + (detail ? `<p class="hint tiny">${escapeHtml(detail)}</p>` : '')
    + (hint ? `<p class="hint tiny">${escapeHtml(hint)}</p>` : '')
    + button
    + '</div>';
}

/**
 * 把 `errorHtml` 产出的「重试」按钮接到回调上。
 *
 * ⚠️ 用**事件委托**挂在外层容器上，而不是逐个 `onclick`：
 * 错误块是整块 `innerHTML` 重建的，逐个绑定会在重建后丢掉。
 *
 * ⚠️ 回调存在**宿主元素**上、每次调用都覆盖 —— 不能把它关进监听器的闭包。
 * 否则第二次渲染传入的新回调不会生效（监听器只挂一次，闭包里还是第一次那批函数）。
 * 这类"看着接上了、其实调的是旧函数"的 bug 很难从界面上看出来。
 */
export function bindRetryButtons(root, handlers = {}) {
  if (!root) return;
  root.__retryHandlers = handlers;      // 每次渲染覆盖，监听器按需读取
  if (root.dataset.retryBound === '1') return;
  root.dataset.retryBound = '1';
  root.addEventListener('click', (event) => {
    const button = event.target.closest('[data-retry]');
    if (!button || !root.contains(button)) return;
    event.preventDefault();
    const table = root.__retryHandlers || {};
    const fn = table[button.dataset.retry] || table['1'];
    if (typeof fn === 'function') fn();
  });
}

export function toast(message, isError = false) {
  const node = $('#toast');
  if (!node) return;
  node.textContent = message;
  node.classList.toggle('is-error', isError);
  node.classList.add('is-visible');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => node.classList.remove('is-visible'), isError ? 6000 : 3200);
}

export function humanBytes(value) {
  if (value === null || value === undefined) return '—';
  let size = Number(value);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${index === 0 ? size : size.toFixed(1)} ${units[index]}`;
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

export function baseName(relativePath) {
  const parts = String(relativePath || '').split('\\');
  return parts[parts.length - 1];
}

/**
 * 相对路径里的**文件夹部分**。
 *
 * 分隔符保持 `\` 原样，不换成 ` / `：这一行是给用户拿去和硬盘上的目录对照的
 * （"原片在哪"），拼成别的样子就对照不上了。空路径返回空串。
 */
export function parentPath(relativePath) {
  const parts = String(relativePath || '').split('\\');
  parts.pop();
  return parts.join('\\');
}

/** 拍摄时间的显示格式：`2026-02-19 14:02`；缺失时给出可读占位而不是空白。 */
export function formatWhen(value) {
  if (!value) return '无拍摄时间';
  return String(value).slice(0, 16);
}

/**
 * 精选状态的中文名。**必须与后端 `library/selection.py` 的 `STATUS_LABELS` 一致** ——
 * 这里再存一份是为了避免为了三个词去发一次请求；两侧不一致时以服务端 `/api/selection/summary`
 * 返回的 `labels` 为准（界面若拿到它就用它）。
 */
export const SELECTION_LABELS = {
  UNREVIEWED: '未审阅',
  CANDIDATE: '候选',
  SELECTED: '精选',
  FINAL: '最终精选',
};

export function selectionLabel(status, labels = SELECTION_LABELS) {
  return labels[status] || labels.UNREVIEWED;
}

/**
 * 精选状态的徽标 class 后缀（CSS 里用 `.sel-candidate` 等）。
 * 未知状态一律按未审阅渲染 —— 不让界面出现"没有样式的一坨文字"。
 */
export function selectionClass(status) {
  const key = String(status || '').toLowerCase();
  return ['candidate', 'selected', 'final'].includes(key) ? `sel-${key}` : 'sel-unreviewed';
}
