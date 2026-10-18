/**
 * 条件条：把"当前在筛什么"渲染成一行**可读摘要 + 可逐个撤掉的芯片**。
 *
 * ## 解决的是什么
 *
 * 四个任务视角各有自己的一套筛选控件，但**没有任何地方**告诉你"当前是什么条件"。
 * 在照片库筛到"杭州市 2024 年"，切到找照片要重新说一遍；切回来又要再说一遍。
 * 筛着筛着想知道"整个库还有多少"，也没有出口。
 *
 * ## 三条设计纪律
 *
 * 1. **它是只读投影，不引入新状态。** 芯片全部从各页面**已有**的 state 算出来
 *    （`compute(host)` 返回一个数组）。所以它可以随时加上、随时拿掉，
 *    不会变成第五套需要同步的状态。
 * 2. **芯片是"移除"而不是"编辑"。** 点 × 就少一个条件，立即重查；
 *    要改条件仍回到各页面原有的控件（那里有下拉框、日期选择器、滑块）。
 *    条件条只做汇总与撤销，不重复实现一遍输入控件。
 * 3. **芯片上没有"推荐"字眼。** 这里显示的是**你设的条件**，
 *    不是系统对照片的评价（PRD §9.2）。
 *
 * ## 用法
 *
 * ```js
 * renderQueryBar($('#library-query'), {
 *   compute: () => [{ key: 'year', label: '2024 年', remove() { … } }],
 *   total: 1234,
 *   onClearAll() { … },
 * });
 * ```
 *
 * 同一个宿主重复调用是**幂等**的：签名没变就不动 DOM（避免每次重查都重排，
 * 也避免把正在被点的芯片从脚下抽走）。
 */

import { $, escapeHtml } from './shared.js';

/** 每个宿主记住上一次渲染的签名，用于幂等跳过。 */
const lastSignature = new WeakMap();

/**
 * @param {Element|null} host   条件条的容器（放 `<div class="query-bar">` 的地方）
 * @param {object} options
 * @param {() => Array<{key:string,label:string,title?:string,remove?:Function,tone?:string}>} options.compute
 *        返回当前条件的**只读投影**；`remove` 缺省表示这一条不可单独撤销（如"浏览轴"）。
 * @param {number|string} [options.total]   命中数量。不传则不显示。
 * @param {string} [options.totalLabel]     数量前面的词，默认「共」。
 * @param {string} [options.totalText]      整段数量的**纯文本**（带自己的单位）。
 *        照片按"张"数，而精选里筛的是 Scene —— 硬拼一个「张」就不对了，
 *        所以给需要自己措辞的地方留这个出口。传了它就忽略 `total`/`totalLabel`。
 * @param {Function} [options.onClearAll]   点「回到全库」做什么。不传则不给这个出口。
 * @param {string} [options.clearLabel]     「回到全库」的文案。
 */
export function renderQueryBar(host, options = {}) {
  if (!host) return;
  const {
    compute, total = null, totalLabel = '共', totalText = '',
    onClearAll = null, clearLabel = '回到全库',
  } = options;

  let chips = [];
  try {
    chips = (compute && compute()) || [];
  } catch (error) {
    // 投影失败不该把整个页面弄崩 —— 条件条是辅助信息，不是主流程。
    chips = [];
  }
  chips = chips.filter((chip) => chip && chip.label);

  const signature = JSON.stringify({
    chips: chips.map((c) => [c.key, c.label, Boolean(c.remove), c.tone || '']),
    total, totalLabel, totalText, clearLabel, hasClear: Boolean(onClearAll),
  });

  // 一条条件都没有、也没有"回到全库"可去 —— 整条收起来，不占版面
  const hasAnything = chips.length > 0 || onClearAll;
  if (!hasAnything) {
    if (lastSignature.get(host) !== 'EMPTY') {
      host.hidden = true;
      host.innerHTML = '';
      lastSignature.set(host, 'EMPTY');
    }
    return;
  }
  host.hidden = false;
  if (lastSignature.get(host) === signature) return;
  lastSignature.set(host, signature);

  const chipHtml = chips.map((chip) => {
    const removable = typeof chip.remove === 'function';
    const tone = chip.tone ? ` is-${escapeHtml(chip.tone)}` : '';
    const hint = chip.title || (removable ? `撤掉这个条件：${chip.label}` : chip.label);
    return `<span class="qb-chip${tone}" data-chip="${escapeHtml(chip.key)}">
        <span class="qb-chip-label" title="${escapeHtml(hint)}">${escapeHtml(chip.label)}</span>
        ${removable
          ? `<button class="qb-chip-x" type="button" data-chip-remove="${escapeHtml(chip.key)}"
                     aria-label="撤掉这个条件：${escapeHtml(chip.label)}"
                     title="撤掉这个条件">×</button>`
          : ''}
      </span>`;
  }).join('');

  const totalHtml = totalText
    ? `<span class="qb-total">${escapeHtml(totalText)}</span>`
    : (total === null || total === undefined
      ? ''
      : `<span class="qb-total">${escapeHtml(totalLabel)} <strong>${escapeHtml(String(total))}</strong> 张</span>`);

  const clearHtml = onClearAll
    ? `<button class="qb-clear" type="button" data-chip-clear>${escapeHtml(clearLabel)}</button>`
    : '';

  host.innerHTML = `
    <div class="qb-lead">当前条件</div>
    <div class="qb-chips">${chipHtml}</div>
    <div class="qb-tail">${totalHtml}${clearHtml}</div>`;

  // 委托一次挂上：芯片是整条重建的（上面已做签名幂等），
  // 逐个绑定既啰嗦、又会在重建后留下悬空引用。
  if (host.dataset.qbBound !== '1') {
    host.dataset.qbBound = '1';
    host.addEventListener('click', (event) => {
      const clear = event.target.closest('[data-chip-clear]');
      if (clear) {
        event.preventDefault();
        const fn = host.__qbClearAll;
        if (typeof fn === 'function') fn();
        return;
      }
      const x = event.target.closest('[data-chip-remove]');
      if (!x) return;
      event.preventDefault();
      event.stopPropagation();          // 别让"撤条件"顺带触发芯片本身的点击语义
      const fn = host.__qbRemovers && host.__qbRemovers.get(x.dataset.chipRemove);
      if (typeof fn === 'function') fn();
    });
  }
  // 回调存到宿主上：重建 innerHTML 只换内容，宿主元素本身与它的监听器都还在。
  host.__qbRemovers = new Map(
    chips.filter((c) => typeof c.remove === 'function').map((c) => [String(c.key), c.remove]),
  );
  host.__qbClearAll = onClearAll;
}

/** 视图切走时把条件条收起来（避免"人不在照片库，却看到照片库的条件"）。 */
export function hideQueryBar(host) {
  if (!host) return;
  host.hidden = true;
  host.innerHTML = '';
  lastSignature.delete(host);
}
