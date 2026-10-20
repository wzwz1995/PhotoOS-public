/**
 * PhotoOS · Phase 2 自定义精选主题（Collections）
 *
 * PRD §9.2.1 的界面落点：
 *   - 主题不是 Scene 的别名，也不受时间、地点或 Session 限制；
 *   - 支持自定义名称、封面、描述和排序；
 *   - 同一张最终精选可以同时属于多个主题；
 *   - 加入或移除只改变数据库关系，**不移动或复制原始照片**。
 *
 * 零构建：原生 ES module，无框架、无打包、无依赖。
 */

import { API, $, toast, escapeHtml, formatWhen, selectionClass, selectionLabel } from './shared.js';
// 卡片公共件：图源 + "文件名＝看原片"。改造前主题成员卡自己拼了缩略图，
// 且文件名是**死的文本** —— 用户在这里看到照片却打不开它。
import { assetImgAttrs, bindOpenAsset, fileBlock } from './cards.js';
import { openPickModal } from './pick_modal.js';

const state = {
  items: [],
  activeId: null,
  detail: null,
};

// --------------------------------------------------------------------- 渲染
function renderList() {
  const node = $('#collections-list');
  if (!node) return;
  if (!state.items.length) {
    node.innerHTML = '<div class="hint">还没有主题。'
      + '「蓝调时刻」「西北游记」这类跨时间、跨地点的合集就建在这里。</div>';
    return;
  }
  node.innerHTML = state.items.map((item) => `
    <button class="collection-card ${item.id === state.activeId ? 'is-active' : ''}"
            data-collection="${item.id}">
      <span class="collection-cover">${
        item.cover_asset_id
          ? `<img ${assetImgAttrs({ assetId: item.cover_asset_id })}>`
          : '<span class="collection-cover-empty">无封面</span>'
      }</span>
      <span class="collection-meta">
        <span class="collection-name">${escapeHtml(item.name)}</span>
        <span class="collection-sub">${item.items} 张${item.description ? ' · ' + escapeHtml(item.description) : ''}</span>
      </span>
    </button>`).join('');
}

function renderDetail() {
  const node = $('#collections-detail');
  if (!node) return;
  const detail = state.detail;
  if (!detail) {
    node.innerHTML = '<div class="hint">选一个主题查看它的作品。</div>';
    return;
  }
  // 成员排成**缩略图网格**：改之前是一行行 46×46 的小图 + 文字，
  // 一个主题 63 张时那一列小图**没法浏览**（用户报的"可读性太差"）。
  // 现在每张给一块能看清的图，顺序、文件名、时刻、四个动作都还在。
  const rows = detail.items.map((item, index) => `
    <figure class="ci-card" data-shot="${item.shot_id}" draggable="true">
      <button type="button" class="ci-shot" ${
        item.has_thumb ? `data-asset="${item.primary_asset_id}"` : 'disabled'}
        title="${escapeHtml(item.has_thumb
          ? `看这张原片：${item.relative_path || ''}`
          : '这张还没有缩略图')}">${
        item.has_thumb
          ? `<img ${assetImgAttrs({ assetId: item.primary_asset_id, alt: item.relative_path || '' })}>`
          : '<span class="collection-cover-empty">无缩略图</span>'
      }</button>
      <figcaption class="ci-meta">
        <span class="ci-line">
          <span class="ci-order">${index + 1}</span>
          <span class="ci-sub">${escapeHtml(formatWhen(item.captured_at))}</span>
          <span class="ci-badge sel-badge ${selectionClass(item.selection_status)}">${
            escapeHtml(selectionLabel(item.selection_status))}</span>
        </span>
        ${item.note ? `<span class="ci-note">${escapeHtml(item.note)}</span>` : ''}
        ${fileBlock({ assetId: item.primary_asset_id, path: item.relative_path, act: false })}
      </figcaption>
      <div class="ci-actions">
        <button class="btn small" data-move="-1" title="上移">↑</button>
        <button class="btn small" data-move="1" title="下移">↓</button>
        <button class="btn small" data-cover title="设为封面">封面</button>
        <button class="btn small" data-detach title="移出主题（不删照片）">移出</button>
      </div>
    </figure>`).join('');

  node.innerHTML = `
    <div class="panel-head">
      <h2>${escapeHtml(detail.name)}</h2>
      <div>
        <button class="btn primary" id="collection-add-photos">添加照片</button>
        <button class="btn small" id="collection-pack"
                title="把这个主题的原片打成一个 zip（复制到数据目录的 exports/，不动原片）">打包原片</button>
        <button class="btn small" id="collection-edit">改名称/描述</button>
        <button class="btn danger small" id="collection-delete">删除主题</button>
      </div>
    </div>
    <p class="hint">
      ${escapeHtml(detail.description || '（无描述）')}
      · 共 <strong>${detail.count}</strong> 张
      · 覆盖 <strong>${detail.scenes}</strong> 个 Scene（跨 Scene / 跨时段都正常）
    </p>
    <p class="hint tiny">
      主题与物理目录<strong>完全无关</strong>：加入或移出只改数据库关系，
      <strong>不会移动、复制或删除任何照片文件</strong>。
    </p>
    <div class="collection-items">${rows || '<div class="wb-empty"><span class="fleuron">❦</span>'
      + '<p>这个主题还是空的。点右上角<b>「添加照片」</b>，'
      + '从<strong>你标过的精选</strong>、<strong>硬盘上的目录</strong>或<strong>另一个主题</strong>里挑。</p>'
      + '</div>'}</div>`;

  // 「点缩略图 = 看原片」：委托挂在成员网格上，整块重渲染也不用重挂。
  // 文件名那一行（`fileBlock`）走的是同一套委托 —— 两条路都通向同一个弹窗。
  bindOpenAsset($('.collection-items'));

  // 「添加照片」：入口在主题页 —— 用户想的是"给这个主题添点片子"，
  // 而不是"先去别处找到一张、再回来选主题"（那是上一版反了的流程）。
  const addPhotos = $('#collection-add-photos');
  if (addPhotos) {
    addPhotos.onclick = () => openPickModal({ collectionId: detail.id });
  }

  // 「打包原片」：主题里的每一组取代表照片，打成一个 zip。
  // 产物是**本地文件**（数据目录的 exports/），结果框里给出路径 ——
  // 与"单个原片不走浏览器下载"同一条纪律。
  const pack = $('#collection-pack');
  if (pack) {
    pack.onclick = async () => {
      const app = await import('./app.js');
      await app.exportPack({ kind: 'collection', id: Number(detail.id), label: detail.name });
    };
  }

  const edit = $('#collection-edit');
  if (edit) edit.onclick = editMetadata;
  const del = $('#collection-delete');
  if (del) del.onclick = removeCollection;
  // ⚠️ 一律用 `closest('.ci-card')` 取所属卡片。改网格时把容器类名从
  // `.collection-item` 换成了 `.ci-card`，这三处必须跟着改 —— 漏一处就是
  // "按钮点了没反应"（而 `data-move` 那三个按钮长得一样，不测看不出来）。
  node.querySelectorAll('[data-move]').forEach((button) => {
    button.onclick = () => moveItem(Number(button.closest('.ci-card').dataset.shot), Number(button.dataset.move));
  });
  node.querySelectorAll('[data-cover]').forEach((button) => {
    button.onclick = () => setCover(Number(button.closest('.ci-card').dataset.shot));
  });
  node.querySelectorAll('[data-detach]').forEach((button) => {
    button.onclick = () => detachItems([Number(button.closest('.ci-card').dataset.shot)]);
  });
}

// --------------------------------------------------------------------- 动作
async function loadCollections() {
  try {
    const payload = await API.get('/api/collections');
    state.items = payload.items;
    if (state.activeId && !state.items.some((item) => item.id === state.activeId)) {
      state.activeId = null;
      state.detail = null;
    }
    renderList();
    if (state.activeId) await loadDetail(state.activeId);
    else renderDetail();
  } catch (error) {
    toast(`主题列表加载失败：${error.message}`, true);
  }
}

async function loadDetail(collectionId) {
  try {
    state.activeId = collectionId;
    state.detail = await API.get(`/api/collections/${collectionId}`);
    renderList();
    renderDetail();
  } catch (error) {
    toast(`主题详情加载失败：${error.message}`, true);
  }
}

async function createCollection() {
  const name = prompt('主题名称（例如「蓝调时刻」「西北游记」）');
  if (!name) return;
  const description = prompt('描述（可留空）') || null;
  try {
    const created = await API.post('/api/collections', { name, description });
    await loadCollections();
    await loadDetail(created.id);
    toast(`已创建主题「${name}」`);
  } catch (error) {
    toast(`创建失败：${error.message}`, true);
  }
}

async function editMetadata() {
  const detail = state.detail;
  if (!detail) return;
  const name = prompt('新名称', detail.name);
  if (name === null) return;
  const description = prompt('新描述', detail.description || '');
  try {
    await API.patch(`/api/collections/${detail.id}`, { name, description });
    await loadCollections();
    toast('已更新');
  } catch (error) {
    toast(`更新失败：${error.message}`, true);
  }
}

async function removeCollection() {
  const detail = state.detail;
  if (!detail) return;
  const ok = confirm(
    `确定删除主题「${detail.name}」？\n\n`
    + '只会删除数据库里的主题关系；不会删除任何照片文件，也不影响精选记录。',
  );
  if (!ok) return;
  try {
    await API.del(`/api/collections/${detail.id}?confirm=true`);
    state.activeId = null;
    state.detail = null;
    await loadCollections();
    toast('主题已删除（照片与精选未受影响）');
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

async function detachItems(shotIds) {
  const detail = state.detail;
  if (!detail || !shotIds.length) return;
  try {
    await API.post(`/api/collections/${detail.id}/items/detach`, { shot_ids: shotIds });
    await loadCollections();
    toast('已移出主题（照片仍在库里，精选状态不变）');
  } catch (error) {
    toast(`移出失败：${error.message}`, true);
  }
}

async function setCover(shotId) {
  const detail = state.detail;
  if (!detail) return;
  try {
    await API.patch(`/api/collections/${detail.id}`, { cover_shot_id: shotId });
    await loadCollections();
    toast('已设为封面');
  } catch (error) {
    toast(`设置封面失败：${error.message}`, true);
  }
}

/** 上下移动：只在本地换序后整体提交（避免每个方向都发一次请求）。 */
async function moveItem(shotId, delta) {
  const detail = state.detail;
  if (!detail) return;
  const ids = detail.items.map((item) => item.shot_id);
  const index = ids.indexOf(shotId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= ids.length) return;
  [ids[index], ids[target]] = [ids[target], ids[index]];
  try {
    await API.post(`/api/collections/${detail.id}/order`, { shot_ids: ids });
    await loadDetail(detail.id);
  } catch (error) {
    toast(`排序失败：${error.message}`, true);
  }
}

/** 把选中的作品批量加入主题（PRD：可手动添加，也可按条件筛选后加入）。 */
async function addShotsToCollection(collectionId, shotIds) {
  if (!shotIds.length) {
    // ⚠️ 这句话原来写的是「先选中一些作品（在「精选」视图里勾选）」——
    // 但精选视图里**从来没有勾选框**，而且整个应用里没有任何地方调用这个函数。
    // 现在真正的入口是工作台里的「加入主题…」（`review.js`），所以提示照实说。
    toast('没有可加入的照片：先在工作台里选一个 Shot（或先标出最终精选）', true);
    return 0;
  }
  const added = await API.post(`/api/collections/${collectionId}/items`, { shot_ids: shotIds });
  await loadCollections();
  toast(`已加入 ${added.added} 张（重复的会自动跳过）`);
  return added.added;
}

// --------------------------------------------------------------------- 绑定
export function initCollections() {
  const create = $('#btn-collection-new');
  if (create) create.onclick = createCollection;

  const list = $('#collections-list');
  if (list) {
    list.onclick = (event) => {
      const card = event.target.closest('[data-collection]');
      if (card) loadDetail(Number(card.dataset.collection));
    };
  }
}

export async function refreshCollections() {
  await loadCollections();
}

/**
 * 载入一个主题的成员。
 *
 * ⚠️ **导出它**是为了让照片详情弹窗能"点主题标签跳过去" ——
 * 弹窗在 `app.js`，它不该直接改本模块的 `state`（跨模块改别人的状态
 * 是这个仓库反复踩过的坑）。所以由本模块自己提供入口，
 * `state.activeId` 与 `state.detail` 的装配留在内部。
 */
export async function openCollection(collectionId) {
  await loadCollections();
  await loadDetail(Number(collectionId));
}

export { loadCollections, loadDetail, addShotsToCollection, detachItems };
