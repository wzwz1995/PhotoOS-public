/**
 * 卡片原语：全应用"照一张照片"的公共部分。
 *
 * 为什么需要它：改造前有 **10 段**手写标记在各自拼同一套东西 ——
 * 缩略图 URL、文件名、拍摄时刻、精选状态徽标、"看原片"入口。
 * 每一段都自己写一遍 `baseName()` / `formatWhen()` / `selectionClass()`，
 * 于是同一件事在不同页面上手感不同（焦点环、hover、省略号规则都会漂）。
 *
 * ⚠️ 这里**只放共性**，不放页面自己的东西：
 *   - 找照片的「我要这种 / 不是这种」、多选的勾、相似度小字
 *   - 精选的 1/2/3 三档、调整分组的复选框
 *   - 主题的序号与"移出"
 * 这些一律留在各自模块里（方案见 docs/11-shell-unification.md）。
 *
 * ⚠️ 人物页的人脸卡**不**用这里的图源函数：它显示的是"一张脸"
 * （`/api/faces/{id}/thumb?size=112`，正方形裁切），不是"一张照片"。
 * 它只共用 `fileBlock()` / `bindOpenAsset()` 那一套"文件名＝原片入口"的实现。
 *
 * ⚠️ 本模块**没有顶层副作用**（不发请求、不碰 DOM、不挂事件），
 * 可以在任意时机静态 import。
 */

import {
  baseName, escapeHtml, parentPath, formatWhen, selectionClass, selectionLabel,
} from './shared.js';

// --------------------------------------------------------------------- 图源
//
// 只有这两个函数知道缩略图 URL 长什么样。
// 判据（docs/11-shell-unification.md 七）：改完之后视图模块里不该再出现这个模板。

const THUMB_URL = (assetId) => `/api/assets/${assetId}/thumb`;
const PREVIEW_URL = (assetId) => `/api/assets/${assetId}/preview`;

/**
 * 卡片要的那张图。
 * `thumb` 铺网格；`preview` 用来看得清（并排比较、要看细节的地方）。
 */
export function assetImageUrl(assetId, size = 'thumb') {
  return size === 'preview' ? PREVIEW_URL(assetId) : THUMB_URL(assetId);
}

/**
 * `<img>` 的属性串。
 *
 * 给**属性**而不是整个标签：各页面 img 外面包的容器与类名都不一样，
 * 硬塞一个完整的 `<img>` 会迫使所有调用方接受我们的类名，或者逼我们去猜它们的类名。
 *
 * `loading` 默认 `lazy`（网格里几百张图，全都立刻解码会把主线程占住）；
 * 首屏就要看的那几张（并排比较、调整分组的面板）传 `eager`。
 */
export function assetImgAttrs({
  assetId, size = 'thumb', className = '', alt = '', loading = 'lazy',
} = {}) {
  const cls = className ? ` class="${escapeHtml(className)}"` : '';
  return `src="${assetImageUrl(assetId, size)}"${cls} alt="${escapeHtml(alt)}" loading="${escapeHtml(loading)}"`;
}

// --------------------------------------------------------------------- 原片入口
//
// 卡片上的文件名＝**这张原片在哪**（照片详情：完整路径 / 大图 / 在资源管理器里显示）。
//
// 这是应用里一个既有约定，但改造前只实现在人物页（`people.js` 的 `fileButton`），
// 于是主题页与「加入主题」弹窗里的文件名是**死的文本** ——
// 用户在主题里看到一张照片，想看大图/看完整路径，没有入口。
// 提到这里之后，凡是卡片都能获得这个能力。
//
// 类名用 `.card-*`：它们定义在 `app.css`（七页都加载得到）。
// 改造前这套样式叫 `.sc-*` 且只写在 `search.css` 里 ——
// 别的页面就算产出同样的标记也拿不到样式。

/**
 * "文件夹 + 文件名"这一行。
 *
 * ⚠️ `path` 是**拼进 HTML 属性**的 —— 必须转义，路径里出现一个引号
 * 就会把属性截断、后面的内容被当成标签解析。
 */
export function fileBlock({
  assetId,
  path = '',
  label = '',
  dirText = null,           // 不传则从 path 推导
  cue = '看原片',
  extraHint = '',
  act = true,               // 是否带 `data-act="asset"`（见下）
} = {}) {
  const full = String(path || '');
  const name = label || baseName(full) || `#${assetId}`;
  // 文件夹部分用 `shared.js` 的 `parentPath`：分隔符保持 `\` 原样，
  // 这一行是给用户拿去和硬盘目录对照的，换成别的样子就对照不上了。
  const dir = dirText === null ? (parentPath(full) || '（库里没有记录路径）') : dirText;
  const hint = ['看这张原片', extraHint, full ? `完整路径：${full}` : '']
    .filter(Boolean).join('\n');
  const attrs = assetId ? ` data-asset="${escapeHtml(assetId)}"` : '';
  // `data-act="asset"` 是人物页既有的点击协议 —— 那一页在自己容器上派发 `data-act`。
  // ⚠️ 这两条路**只能走一条**：`bindOpenAsset` 会跳过带 `data-act` 的元素
  // （否则一次点击会被两个处理器各开一次弹窗）。所以：
  //   - 人物页：`act: true`（默认），由它的 `data-act` 派发接管；
  //   - 其余页面：`act: false`，由 `bindOpenAsset` 接管。
  const actAttr = act ? ' data-act="asset"' : '';
  return `<div class="card-file">
      <span class="card-dir" title="${escapeHtml(full ? `完整路径：${full}` : '这张照片在库里没有路径记录')}">${escapeHtml(dir)}</span>
      <button class="card-open" type="button"${attrs}${actAttr} title="${escapeHtml(hint)}">
        <span class="card-name">${escapeHtml(name)}</span><span class="card-cue">${escapeHtml(cue)}</span>
      </button>
    </div>`;
}

/** 从"文件名可点"的按钮上取 asset id。返回 `0` 表示取不到（别再往下走）。 */
export function assetIdOf(button) {
  return Number(button && button.dataset ? button.dataset.asset : 0) || 0;
}

/**
 * 打开某个资产详情时要**放行**的元素（点它们 = 看这张原片）。
 *
 * · `.card-open` —— 「文件夹 + 文件名」那一行（全应用统一的原片入口）；
 * · `.ci-shot`  —— 主题页成员卡的**缩略图本身**（图就是入口，最直觉的那个）。
 *
 * ⚠️ 刻意**不是**"任何带 `data-asset` 的元素"：主题页的卡片上还有
 * "上移/下移/设为封面/移出"四个按钮，如果整张卡都能触发，点那些按钮
 * 会先被这里截走（卡片的点击是 `closest('.ci-card')` 找的）。
 * 所以只放行**明确的入口元素**，白名单式，不猜测。
 */
const OPEN_SELECTORS = '.card-open[data-asset], .ci-shot[data-asset]';

/**
 * 接上「点它就是看原片」：给容器里的入口元素挂点击（事件委托）。
 *
 * 用**事件委托**而不是逐个绑定：卡片是"认领节点、只改会变的字"渲染的
 * （见 `search.js` 的 `updateCard`），逐个绑定会在节点复用后重复挂。
 * 同一个容器调多次也只生效一次 —— 靠 `dataset.openBound` 记住。
 */
export function bindOpenAsset(root) {
  if (!root || root.dataset.openBound === '1') return;
  root.dataset.openBound = '1';
  root.addEventListener('click', async (event) => {
    const button = event.target.closest(OPEN_SELECTORS);
    if (!button || !root.contains(button)) return;
    // 带 `data-act` 的元素交给**它所在页面**的派发器处理（人物页就是这么做的）——
    // 两边都接手会让一次点击开出两个弹窗。
    if (button.dataset.act) return;
    const assetId = assetIdOf(button);
    if (!assetId) return;
    event.preventDefault();
    // 卡片自己可能还有别的点击语义（如主题行的选中、拖拽），别让它跟着触发。
    event.stopPropagation();
    // `app.js` 顶层会 boot()，**不能静态 import** —— 一律函数内动态引入。
    const app = await import('./app.js');
    await app.openAssetDetail(assetId);
  });
}

// --------------------------------------------------------------------- 状态徽标

/**
 * 徽标占位。与 `updateBadge` 成对。
 *
 * `build` + `update` 两个函数是**有意拆开**的：照片库的虚拟网格复用 DOM 节点
 * （只渲染可见格），一个"返回 HTML 串"的函数会把那个 100k 张也不卡的网格毁掉。
 */
export function badgeHtml() {
  return '<span class="sel-badge" data-badge></span>';
}

/** 就地更新徽标：类名与文案在一处算，避免各页面拼出不一致的组合。
 *
 * `labels` 可选：精选页拿到服务端 `/api/selection/summary` 的 `labels` 时以它为准
 * （`shared.js` 那份只是为了避免为三个词发一次请求而存的副本，两侧不一致时听服务端的）。 */
export function updateBadge(node, status, labels) {
  const badge = node && node.querySelector('[data-badge]');
  if (!badge) return;
  badge.className = `sel-badge ${selectionClass(status)}`;
  badge.textContent = selectionLabel(status, labels || undefined);
}

// --------------------------------------------------------------------- 拍摄时刻
//
// 卡片上时间那一段在找照片页是**金色加粗**的（判断日出日落靠它），
// 而 `formatWhen` 已经把时间包含进去了 —— 直接整串渲染会出现
// 「2026-05-31 23:44 23:44」。所以拆成"日期 + 时刻"两段，各写一次。

export function dayPart(value) {
  const text = String(value || '');
  return text.length >= 16 ? text.slice(0, 10) : formatWhen(value);
}

export function hourPart(value) {
  const text = String(value || '');
  return text.length >= 16 ? text.slice(11, 16) : '';
}

/** 卡片上的完整时刻（不拆段的地方用这个）。 */
export function whenText(value) {
  return formatWhen(value);
}
