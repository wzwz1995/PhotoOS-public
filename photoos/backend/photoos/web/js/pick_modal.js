/**
 * PhotoOS · 「添加照片到主题」对话框
 *
 * ## 为什么要有这个模块（用户原话）
 *
 * > 加入主题的逻辑不对，主题点进去我要选我精选过的照片，
 * > 或者直接按照已有的任何物理目录或者主题来选。
 *
 * 原来的实现方向反了：它要求用户**先在工作台里找到某一张**，再回答"放进哪个主题"。
 * 而真实的动作是反过来的 —— 用户想的是"给这个主题添点片子"，
 * 然后从**他已经组织好的东西**里挑：我标过的精选、我拍的某个目录、我另一个主题。
 * 所以这里做的是「选来源 → 多选 → 加入」，入口放在主题页。
 *
 * 三个来源（全部走**已有的**接口，没有新增后端）：
 *
 * | 来源 | 数据 | 接口 |
 * | --- | --- | --- |
 * | 我的精选 | 按精选状态筛：最终精选 / 精选 / 候选 | `POST /api/assets/query`（`selection_status`）|
 * | 物理目录 | 城市 → 时段 → Scene（就是你硬盘上的目录层级）| `/api/scenes` + `/api/scenes/{id}` |
 * | 已有主题 | 另一个主题里的成员，复制进来 | `/api/collections/{id}` |
 *
 * ## 设计
 *
 * 按 Feihong Design System 的 Console 场景：一个主操作（皇家蓝「加入主题」）、
 * 三个来源用 Tab 切换、选中项用金色描边+序号（金色=高亮，不是"更好"）、
 * 危险操作零个。缩略图用等宽小字标注时间与状态。
 */

import { API, $, toast, escapeHtml, baseName, formatWhen, loadingHtml, selectionLabel, selectionClass,
  SELECTION_LABELS } from './shared.js';
// 图源只走这里：URL 模板在全应用只该有一处（见 docs/11-shell-unification.md 七）。
// ⚠️ 这两个格子（`.wb-pick` / `.wb-chip`）**不画文件名**，所以不用 `fileBlock()` ——
// 它们的语义是"挑一张"，不是"看这张的原片"。
import { assetImgAttrs } from './cards.js';
import { addShotsToCollection } from './collections.js';

const SOURCES = [
  { key: 'selection', label: '我的精选' },
  { key: 'folder', label: '物理目录' },
  { key: 'collection', label: '已有主题' },
];

const state = {
  open: false,
  source: 'selection',
  /** 目标主题（从主题页打开时固定；从工作台打开时可选） */
  targetId: null,
  collections: [],
  /** 已选中的 shot：`shotId -> {shotId, assetId, path, when, status}`（Map 保持加入顺序） */
  picked: new Map(),
  /** 来源筛选状态 */
  status: 'FINAL',
  city: '',
  keyword: '',
  sceneId: null,
  sourceCollectionId: null,
  /** 当前来源可选的 Shot 列表（已按 shot 去重） */
  pool: [],
  /** 目标主题里已有的 shot id（用来标"已在主题里"） */
  existing: new Set(),
  loading: false,
  error: '',
};

const STATUS_CHOICES = [
  ['FINAL', '最终精选'],
  ['SELECTED', '精选'],
  ['CANDIDATE', '候选'],
  ['REVIEWED', '已审（候选+精选+最终）'],
];

// --------------------------------------------------------------------- 工具
function node() {
  return $('#pick-modal');
}

/** 资产列表 → 每个 Shot 取一张代表图（缩略图优先，其次预览）。 */
function shotsFromAssets(assets) {
  const byShot = new Map();
  assets.forEach((asset) => {
    const shotId = asset.shot_id;
    if (!shotId) return;                       // 还没分组的资产：这里不给加入主题
    if (byShot.has(shotId)) return;
    byShot.set(shotId, {
      shotId: Number(shotId),
      assetId: Number(asset.id),
      path: asset.relative_path,
      when: asset.capture_at || asset.capture_date_local,
      status: asset.selection_status || 'UNREVIEWED',
      hasThumb: Boolean(asset.has_thumb),
    });
  });
  return [...byShot.values()];
}

function cardHtml(shot, index) {
  const picked = state.picked.get(shot.shotId);
  const order = picked ? [...state.picked.keys()].indexOf(shot.shotId) + 1 : '';
  const already = state.existing.has(shot.shotId);
  return `
    <button class="wb-pick${picked ? ' is-picked' : ''}" data-shot="${shot.shotId}"
      data-index="${index}" data-order="${order}"
      title="${escapeHtml(baseName(shot.path || ''))}${already ? ' · 已在主题里' : ''}">
      <img ${assetImgAttrs({ assetId: shot.assetId })}>
      <span class="wb-pick-cap">
        <span>${escapeHtml(String(shot.when || '').slice(5, 16))}</span>
        <span>${already ? '已在' : escapeHtml(selectionLabel(shot.status, SELECTION_LABELS))}</span>
      </span>
    </button>`;
}

function gridHtml() {
  if (state.loading) return loadingHtml('正在读取…');
  if (state.error) return `<div class="wb-empty"><p>${escapeHtml(state.error)}</p></div>`;
  if (!state.pool.length) {
    return '<div class="wb-empty"><span class="fleuron">❦</span>'
      + '<p>这个来源下没有可加入的照片。换个来源，或先在工作台里标出精选。</p></div>';
  }
  return `<div class="wb-pick-grid">${state.pool.map(cardHtml).join('')}</div>`;
}

// --------------------------------------------------------------------- 数据
async function loadPool() {
  state.loading = true;
  state.error = '';
  renderBody();
  try {
    if (state.source === 'selection') {
      const statuses = state.status === 'REVIEWED'
        ? ['CANDIDATE', 'SELECTED', 'FINAL'] : [state.status];
      const filters = { selection_status: { in: statuses } };
      if (state.city) filters.city = { eq: state.city };
      const payload = await API.post('/api/assets/query', {
        filters, sort: 'capture_desc', limit: 500,
      });
      state.pool = shotsFromAssets(payload.items || []);
    } else if (state.source === 'folder') {
      if (!state.sceneId) {
        state.pool = [];
      } else {
        const detail = await API.get(`/api/scenes/${state.sceneId}`);
        state.pool = (detail.shots || []).map((shot) => ({
          shotId: Number(shot.id),
          assetId: Number(shot.primary_asset_id),
          path: shot.primary_path,
          when: shot.captured_at,
          status: shot.selection_status || 'UNREVIEWED',
          hasThumb: Boolean(shot.has_thumb),
        }));
      }
    } else {
      if (!state.sourceCollectionId) {
        state.pool = [];
      } else {
        const detail = await API.get(`/api/collections/${state.sourceCollectionId}`);
        state.pool = (detail.items || []).map((item) => ({
          shotId: Number(item.shot_id),
          assetId: Number(item.primary_asset_id),
          path: item.relative_path,
          when: item.captured_at,
          status: item.selection_status || 'UNREVIEWED',
          hasThumb: Boolean(item.has_thumb),
        }));
      }
    }
  } catch (error) {
    state.error = `读取失败：${error.message}`;
    state.pool = [];
  } finally {
    state.loading = false;
    renderBody();
  }
}

async function loadCollections() {
  try {
    const payload = await API.get('/api/collections');
    state.collections = payload.items || [];
  } catch (error) {
    state.collections = [];
  }
}

async function loadExisting() {
  state.existing = new Set();
  if (!state.targetId) return;
  try {
    const detail = await API.get(`/api/collections/${state.targetId}`);
    state.existing = new Set((detail.items || []).map((item) => Number(item.shot_id)));
  } catch (error) {
    /* 目标主题还没内容 / 读不到都不影响加入 */
  }
}

// --------------------------------------------------------------------- 渲染
function filterRowHtml() {
  if (state.source === 'selection') {
    return `
      <div class="wb-source-row">
        <label class="wb-field"><span class="wb-label">我标过的</span>
          <select id="pick-status">
            ${STATUS_CHOICES.map(([value, label]) => `
              <option value="${value}" ${value === state.status ? 'selected' : ''}>${label}</option>`).join('')}
          </select>
        </label>
        <label class="wb-field"><span class="wb-label">地点</span>
          <select id="pick-city"><option value="">（全部地点）</option>
            ${state.cities.map(([city, count]) => `
              <option value="${escapeHtml(city)}" ${city === state.city ? 'selected' : ''}>
                ${escapeHtml(city)}（${count}）</option>`).join('')}
          </select>
        </label>
        <span class="wb-count">按 <b>拍摄时间</b> 倒序 · 每个拍摄组取一张代表</span>
      </div>`;
  }
  if (state.source === 'folder') {
    return `
      <div class="wb-source-row">
        <label class="wb-field"><span class="wb-label">地点</span>
          <select id="pick-folder-city"><option value="">（全部地点）</option>
            ${state.cities.map(([city, count]) => `
              <option value="${escapeHtml(city)}" ${city === state.city ? 'selected' : ''}>
                ${escapeHtml(city)}（${count}）</option>`).join('')}
          </select>
        </label>
        <label class="wb-field"><span class="wb-label">关键词</span>
          <input type="search" id="pick-folder-keyword" placeholder="日期或目录名，例如 2026-10"
                 value="${escapeHtml(state.keyword)}">
        </label>
        <label class="wb-field wb-grow"><span class="wb-label">时段 / 场景（对应硬盘上的目录）</span>
          <select id="pick-scene"><option value="">（先选一个场景）</option>
            ${sceneOptionsHtml()}
          </select>
        </label>
      </div>`;
  }
  const other = state.collections.filter((item) => Number(item.id) !== Number(state.targetId));
  return `
    <div class="wb-source-row">
      <label class="wb-field wb-grow"><span class="wb-label">从哪个主题挑选</span>
        <select id="pick-source-collection">
          <option value="">（选择一个主题）</option>
          ${other.map((item) => `
            <option value="${item.id}" ${Number(item.id) === Number(state.sourceCollectionId) ? 'selected' : ''}>
              ${escapeHtml(item.name)}（${item.items} 张）</option>`).join('')}
        </select>
      </label>
      <span class="wb-count">把别的主题里的照片加到这里；原主题不受影响</span>
    </div>`;
}

function sceneOptionsHtml() {
  const needle = state.keyword.trim().toLowerCase();
  const list = state.scenes.filter((scene) => {
    if (state.city && (scene.city || '') !== state.city) return false;
    if (needle && !String(scene.name || '').toLowerCase().includes(needle)) return false;
    return true;
  });
  if (!list.length) return '<option value="">（没有匹配的场景）</option>';
  return list.map((scene) => `
    <option value="${scene.id}" ${Number(scene.id) === Number(state.sceneId) ? 'selected' : ''}>
      ${escapeHtml(scene.name)} — ${scene.shots} 组
    </option>`).join('');
}

function renderHead() {
  const head = $('#pick-head');
  if (!head) return;
  const target = state.collections.find((item) => Number(item.id) === Number(state.targetId));
  head.innerHTML = `
    <div>
      <h2>添加照片</h2>
      <span class="wb-modal-sub">${
        target ? `加入主题「${escapeHtml(target.name)}」` : '先选一个目标主题'}</span>
    </div>
    <span class="spacer" style="flex:1"></span>
    ${target ? '' : `<label class="wb-field"><span class="wb-label">目标主题</span>
      <select id="pick-target">
        <option value="">（选择主题）</option>
        ${state.collections.map((item) => `
          <option value="${item.id}" ${Number(item.id) === Number(state.targetId) ? 'selected' : ''}>
            ${escapeHtml(item.name)}（${item.items} 张）</option>`).join('')}
      </select></label>`}
    <button class="wb-modal-x" id="pick-close" type="button" aria-label="关闭" title="关闭（Esc）">
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M1.5 1.5 L10.5 10.5 M10.5 1.5 L1.5 10.5" stroke="currentColor"
              stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    </button>`;
  const targetSelect = $('#pick-target');
  if (targetSelect) {
    targetSelect.onchange = async (event) => {
      state.targetId = event.target.value ? Number(event.target.value) : null;
      state.sourceCollectionId = state.sourceCollectionId === state.targetId
        ? null : state.sourceCollectionId;
      await loadExisting();
      render();
    };
  }
  const close = $('#pick-close');
  if (close) close.onclick = closeModal;
}

function renderTabs() {
  const tabs = $('#pick-tabs');
  if (!tabs) return;
  tabs.innerHTML = SOURCES.map((source) => `
    <button class="wb-tab${source.key === state.source ? ' is-active' : ''}" data-source="${source.key}">
      ${source.label}</button>`).join('');
  tabs.querySelectorAll('[data-source]').forEach((button) => {
    button.onclick = async () => {
      state.source = button.dataset.source;
      state.picked.clear();
      if (state.source === 'folder' && !state.scenes.length) await loadScenes();
      render();
      await loadPool();
    };
  });
}

/**
 * 「已选」条：**永远显示已经勾了什么**，不管它来自哪个来源。
 *
 * 为什么必须有：从工作台点「加入主题…」进来时，当前这张是**预勾选**的，
 * 但它可能根本不在当前来源的列表里（比如来源是"最终精选"，而当前这张只是候选）。
 * 那样用户会看到"页脚写着已选 1 张，网格里却一个勾都没有"，只能凭猜测。
 */
function pickedStripHtml() {
  if (!state.picked.size) return '';
  const chips = [...state.picked.values()].map((shot, index) => `
    <span class="wb-chip" data-chip="${shot.shotId}"
          title="${escapeHtml(baseName(shot.path || ''))}">
      <img ${assetImgAttrs({ assetId: shot.assetId })}>
      <b>${index + 1}</b>
      <button class="wb-chip-x" data-remove="${shot.shotId}" title="移出选择">×</button>
    </span>`).join('');
  return `
    <div class="wb-picked-strip">
      <span class="wb-label">已选 ${state.picked.size} 张</span>
      <div class="wb-picked-chips">${chips}</div>
    </div>`;
}

function renderBody() {
  const body = $('#pick-body');
  if (!body) return;
  body.innerHTML = `${filterRowHtml()}${pickedStripHtml()}${gridHtml()}`;
  const status = $('#pick-status');
  if (status) {
    status.onchange = async (event) => { state.status = event.target.value; await loadPool(); };
  }
  const city = $('#pick-city') || $('#pick-folder-city');
  if (city) {
    city.onchange = async (event) => {
      state.city = event.target.value;
      state.sceneId = null;
      if (state.source === 'folder') renderBody();
      await loadPool();
    };
  }
  const keyword = $('#pick-folder-keyword');
  if (keyword) {
    // 只重画下拉的选项，不重建整块（否则每敲一个字光标就丢）
    keyword.oninput = (event) => {
      state.keyword = event.target.value;
      const select = $('#pick-scene');
      if (select) {
        const keep = state.sceneId;
        select.innerHTML = sceneOptionsHtml();
        select.value = keep ? String(keep) : '';
      }
    };
  }
  const scene = $('#pick-scene');
  if (scene) {
    scene.onchange = async (event) => {
      state.sceneId = event.target.value ? Number(event.target.value) : null;
      await loadPool();
    };
  }
  const source = $('#pick-source-collection');
  if (source) {
    source.onchange = async (event) => {
      state.sourceCollectionId = event.target.value ? Number(event.target.value) : null;
      await loadPool();
    };
  }
  const selectAll = $('#pick-select-all');
  if (selectAll) {
    selectAll.onclick = () => {
      state.pool.forEach((shot) => state.picked.set(shot.shotId, shot));
      renderBody();
      renderFoot();
    };
  }
  const clearAll = $('#pick-clear-all');
  if (clearAll) {
    clearAll.onclick = () => { state.picked.clear(); renderBody(); renderFoot(); };
  }
  body.querySelectorAll('[data-remove]').forEach((button) => {
    button.onclick = (event) => {
      event.stopPropagation();
      state.picked.delete(Number(button.dataset.remove));
      renderBody();
    };
  });
  body.querySelectorAll('[data-shot]').forEach((button) => {
    button.onclick = () => {
      const shotId = Number(button.dataset.shot);
      if (state.picked.has(shotId)) state.picked.delete(shotId);
      else {
        const shot = state.pool.find((item) => item.shotId === shotId);
        if (shot) state.picked.set(shotId, shot);
      }
      renderBody();
      renderFoot();
    };
  });
  // ⚠️ 每次重画内容都要顺手刷新页脚：`全选本页（N）` 里的 N 来自 `state.pool`，
  // 换来源/换 Scene 后如果不刷新，它会留着上一次的数字（实测：网格里 7 张、
  // 页脚却写「全选本页（0）」）。
  renderFoot();
}

function renderFoot() {
  const foot = $('#pick-foot');
  if (!foot) return;
  const count = state.picked.size;
  const target = state.collections.find((item) => Number(item.id) === Number(state.targetId));
  foot.innerHTML = `
    <span class="wb-picked-note">已选 ${count} 张</span>
    <button class="btn ghost small" id="pick-select-all">全选本页（${state.pool.length}）</button>
    <button class="btn ghost small" id="pick-clear-all">清空选择</button>
    <span class="spacer"></span>
    <span class="wb-count">${target ? `目标：<b>${escapeHtml(target.name)}</b>` : '还没选目标主题'}</span>
    <button class="btn primary" id="pick-add" ${count && state.targetId ? '' : 'disabled'}>
      加入主题</button>`;
  const selectAll = $('#pick-select-all');
  if (selectAll) {
    selectAll.onclick = () => {
      state.pool.forEach((shot) => state.picked.set(shot.shotId, shot));
      renderBody();
      renderFoot();
    };
  }
  const clearAll = $('#pick-clear-all');
  if (clearAll) {
    clearAll.onclick = () => { state.picked.clear(); renderBody(); renderFoot(); };
  }
  const add = $('#pick-add');
  if (add) add.onclick = submit;
}

function render() {
  if (!state.open) return;
  const root = node();
  if (!root) return;
  root.hidden = false;
  renderHead();
  renderTabs();
  renderBody();
  renderFoot();
}

// --------------------------------------------------------------------- 动作
async function submit() {
  if (!state.picked.size || !state.targetId) return;
  const ids = [...state.picked.keys()];
  try {
    const added = await addShotsToCollection(state.targetId, ids);
    state.picked.clear();
    await loadCollections();
    await loadExisting();
    render();
    toast(`已加入 ${added} 张${added < ids.length ? `（${ids.length - added} 张本来就在主题里）` : ''}`);
  } catch (error) {
    toast(`加入失败：${error.message}`, true);
  }
}

function closeModal() {
  state.open = false;
  const root = node();
  if (root) {
    root.hidden = true;
    root.innerHTML = '';
  }
}

/** 物理目录来源需要 Scene 清单（只在第一次切到该 Tab 时拉）。 */
async function loadScenes() {
  try {
    const payload = await API.get('/api/scenes?limit=5000');
    state.scenes = payload.items || [];
    const counts = new Map();
    state.scenes.forEach((scene) => {
      const city = scene.city || '';
      counts.set(city, (counts.get(city) || 0) + 1);
    });
    state.cities = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  } catch (error) {
    state.scenes = [];
    state.cities = [];
  }
}

/**
 * 打开对话框。
 *
 * @param {{ collectionId?: number, presetShotIds?: number[], presetShotsData?: object[] }} options
 *   `collectionId` —— 从主题页打开时固定目标；从工作台打开时不传，让用户在对话框里选。
 *   `presetShotIds` —— 工作台里"把当前这张加进去"的快捷路径：预先勾好。
 */
export async function openPickModal(options = {}) {
  state.open = true;
  state.targetId = options.collectionId ? Number(options.collectionId) : null;
  state.source = options.presetShotIds && options.presetShotIds.length ? 'folder' : 'selection';
  state.source = 'selection';
  state.picked.clear();
  state.sceneId = null;
  state.sourceCollectionId = null;
  state.keyword = '';
  state.city = '';
  state.scenes = state.scenes || [];
  state.cities = state.cities || [];

  const root = node();
  if (!root) return;
  root.hidden = false;
  root.innerHTML = `
    <div class="wb-modal-card">
      <div class="wb-modal-head" id="pick-head"></div>
      <div class="wb-modal-tabs" id="pick-tabs"></div>
      <div class="wb-modal-body" id="pick-body"></div>
      <div class="wb-modal-foot" id="pick-foot"></div>
    </div>`;
  root.onclick = (event) => { if (event.target === root) closeModal(); };

  await loadCollections();
  if (!state.targetId && state.collections.length === 1) {
    state.targetId = Number(state.collections[0].id);   // 只有一个主题时不用再选
  }
  await loadExisting();
  if (options.presetShotIds && options.presetShotIds.length) {
    // 工作台快捷入口：先把当前这张勾上，用户再决定要不要多挑几张
    (options.presetShotsData || []).forEach((shot) => state.picked.set(shot.shotId, shot));
  }
  render();
  await loadPool();
}

export function closePickModal() {
  closeModal();
}

/** 面板是否开着（Esc 用）。 */
export function pickModalOpen() {
  return state.open;
}
