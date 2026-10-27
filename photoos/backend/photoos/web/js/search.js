/**
 * PhotoOS · 找照片（按画面内容）
 *
 * ## 这一页要解决的两件事
 *
 * 1. **把全库缩小到几个拍摄组**：搜索只负责"少看一点"，不负责"替你选"。
 *    所以卡片上不出现评分、星级、"最佳"这类话术；相似度只是一行极小的灰字，
 *    用途是核对"为什么它在前面"。真正的判断依据是**本地拍摄时刻** ——
 *    模型分不清日出与日落，但 `07:39` 和 `17:44` 一眼就能分清。
 *
 * 2. **让用户改结果，并且系统记住**：每张卡片可以「我要这种」或「不是这种」。
 *    表态写进数据库，服务端立刻用它们重排整页 ——
 *    这是"越用越懂你"在界面上的具体形式，也是这一页存在的理由。
 *
 * ## 表态之后为什么**不能**重建整页（2026-10-14 修的）
 *
 * 上一版点一次「我要这种」就把结果容器清空成"查询中…"再整片重建：
 * 容器一空，`scrollHeight` 掉到几十像素，浏览器立刻把 `scrollTop` 夹回 0，
 * 于是在第 200 张那里点的表态会把人**弹回页首** —— 用户的原话是"很不友好"。
 * 所以现在：
 *   * 请求在飞的时候**原地保留旧卡片**（只加 `.is-busy` 压暗），不清空；
 *   * 拿到新结果后**复用同一批 DOM 节点**（按 `shot_id` 认领，只改文字与状态类），
 *     节点的 `<img>` 不会被重建，也就不会重新解码、不会闪一下；
 *   * 最后再按"表态前的滚动位置"还原一次，并补偿网格**上方**元素的高度变化。
 *
 * ## 多选为什么必须合成**一次**请求
 *
 * 选了 12 张再点一次「我要这种」，如果发 12 个请求：服务端要重排 12 次、
 * 界面要重建 12 次、滚动位置要跳 12 次 —— 而且中间那 11 次结果没人看。
 * 所以 `POST /api/semantic/feedback` 收的是 `shot_ids` 数组，
 * 一条事务写完、只重算一次。
 *
 * ## 为什么这一页不跑模型
 *
 * 界面所在的环境没有装模型运行时（刻意的取舍：装了会和场景划分抢显存）。
 * 所以能搜的词是命令行预先算好的。学习与重排**不需要模型** ——
 * 它们只是拿库里已经存好的照片向量做平均与点积，所以点一下就能立刻生效。
 */

import {
  $, API, baseName, errorHtml, escapeHtml, loadingHtml, toast, bindRetryButtons,
} from './shared.js';
// 卡片公共件：图源、状态徽标、"文件夹 + 文件名 + 看原片"这一行都从这里来
// （改造前这一页自己拼了全部四样，别的页面又各拼一遍）。
import {
  assetImageUrl, assetImgAttrs, badgeHtml, bindOpenAsset, dayPart, fileBlock, hourPart,
  updateBadge,
} from './cards.js';
// 「当前在筛什么」的唯一落点：只读投影，不引入新状态（本页两种模式共用同一条）。
import { renderQueryBar } from './query_bar.js';
// 「这个词没准备过」时直接给按钮执行 —— 命令由服务端定义，这里只提交 action id。
import {
  catalogAction, loadCatalog, openActionsDialog, runActionWithProgress,
} from './actions.js';

const state = {
  initialized: false,
  mode: 'content',        // 'content'（按画面内容）| 'when'（按时间地点）
  queries: [],
  current: null,          // 当前查询对象
  items: [],
  feedback: {},           // {shot_id: 'keep'|'drop'}
  counts: { keep: 0, drop: 0 },
  tuned: false,
  tuning: null,
  showTuning: true,       // 是否套用"按我的修改重排"
  onlyConfirmed: true,    // 找照片默认只显示已确认「我要这种」的卡片
  cities: [],
  cli: {                    // 命令行前缀（服务端给；见 refreshSearch 的说明）
    search: ['photoos search 日出 --save'],
    people: [],
  },
  queriesNote: '',
  selected: new Set(),    // 多选：这一批要一起表态的 shot_id
  anchor: null,           // Shift 连选的那一头
  sending: false,         // 是否已有一次表态在飞
  queue: [],              // 飞行途中又点的那些（**不丢点击**）
  /**
   * 「按时间 / 地点 / 场次」那一次查询**实际用了什么条件**。
   *
   * 为什么要把表单的值抄一份出来：条件条要显示"当前条件"并可逐个撤掉，
   * 而它**不能去读表单** —— 表单是输入控件，用户改了值还没点「筛选」时，
   * 显示出来的条件就已经变了，条件条会开始说谎。
   * 抄这一份之后，条件条反映的永远是**上一次真正执行的查询**。
   */
  find: null,             // { city, session, from, to, minShots } | null
  manage: null,           // /api/semantic/manage 的快照
  scene: null,            // 当前打开的场景摘要（/api/semantic/scenes/{id}）
  sceneItems: [],         // 当前这一轮 60 张
  sceneBusy: false,
  sceneBatchBusy: false,
  manageSelected: new Set(),
  manageBusy: false,
};

export function initSearch() {
  if (state.initialized) return;
  state.initialized = true;

  // 模式切换：两种找法用途不同，界面上写清楚（避免用户拿它去做它做不到的事）
  document.querySelectorAll('.find-mode').forEach((button) => {
    button.addEventListener('click', () => switchMode(button.dataset.mode));
  });

  $('#search-pick').addEventListener('change', (event) => {
    const value = event.target.value;
    if (value) searchBy(value);
  });

  $('#search-go').addEventListener('click', () => {
    const typed = String($('#search-input').value || '').trim();
    // 下拉框里的预置词也是"已经选好的词"。以前只认输入框，于是用户明明
    // 在下拉框里选了「月亮」，点搜索却弹「请先选一个」—— 提示与眼前的事实相反。
    const picked = String($('#search-pick').value || '').trim();
    if (!typed && !picked) {
      toast('先选一个预置搜索词，或在输入框里输入一个词', true);
      return;
    }
    searchBy(typed || picked);
  });

  $('#search-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      $('#search-go').click();
    }
  });

  // 多选操作条：委托一次，条子是整块重建的，逐个绑会随重建丢掉
  $('#search-pick-bar').addEventListener('click', (event) => {
    const button = event.target.closest('[data-pick-act]');
    if (button) handlePickAction(button.dataset.pickAct);
  });

  $('#find-go').addEventListener('click', () => runFind());
  ['#find-session', '#find-from', '#find-to', '#find-min-shots'].forEach((sel) => {
    $(sel).addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runFind();
      }
    });
  });
  $('#find-city').addEventListener('change', () => runFind());

  if ($('#search-manage')) {
    $('#search-manage').addEventListener('click', () => openSemanticManage());
  }

  // 预热命令目录：不进 await（不阻塞这一页的首屏），但要在用户碰到
  // "这个词没准备过"之前拿到 —— 那个空态里的「立即准备」按钮靠它决定是否可用。
  loadCatalog().catch(() => { /* 目录拿不到时按钮会显示为不可用，不再打扰用户 */ });
}

function switchMode(mode) {
  if (!mode || mode === state.mode) return;
  state.mode = mode;
  document.querySelectorAll('.find-mode').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.mode === mode);
  });
  $('#mode-content').hidden = mode !== 'content';
  $('#mode-when').hidden = mode !== 'when';
  // 条件条整块换掉：两种模式的条件形状不同（一个搜索词 vs 五个结构化字段），
  // 而且它是幂等的 —— 上一次渲染的签名与这一次不同，所以会重画。
  refreshSearchQueryBar();
  if (mode === 'when' && !state.cities.length) loadCities().then(() => runFind());
  else if (mode === 'when') runFind();
}

/** 城市选项：从 `/api/scenes` 的返回里收集（接口本来就带 city），不新增后端接口。 */
async function loadCities() {
  try {
    const payload = await API.get('/api/scenes?limit=5000');
    const cities = [...new Set((payload.items || []).map((s) => s.city).filter(Boolean))].sort();
    state.cities = cities;
    $('#find-city').innerHTML = '<option value="">（全部）</option>'
      + cities.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  } catch (error) {
    toast(`城市列表加载失败：${error.message}`, true);
  }
}

/**
 * 结构化筛选（确定的那一层）。
 *
 * 这一层**必须**做成"点一下就能进精选"：它列出的就是"某一次拍摄"，
 * 用户的下一步必然是去筛它。只给编号不给出路，就是"功能在、路不通"。
 */
async function runFind() {
  const params = new URLSearchParams();
  const city = $('#find-city').value;
  const session = String($('#find-session').value || '').trim();
  const from = $('#find-from').value;
  const to = $('#find-to').value;
  const minShots = String($('#find-min-shots').value || '').trim();
  if (city) params.set('city', city);
  if (session) params.set('session', session);
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);
  if (minShots) params.set('min_shots', minShots);

  const body = $('#find-results');
  if (![...params.keys()].length) {
    body.innerHTML = '';
    $('#find-summary').innerHTML = '<p class="hint tiny">至少给一个条件（否则等于全库）。</p>';
    state.find = null;                 // "至少给一个条件"时什么都没查，条件条也该空
    renderFindQueryBar();
    return;
  }
  // 记下**这一次真正执行的**条件（见 state.find 的说明）
  state.find = {
    city, session, from, to, minShots,
  };
  renderFindQueryBar();
  body.innerHTML = loadingHtml('正在筛选…');
  try {
    const payload = await API.get(`/api/find/scenes?${params.toString()}`);
    renderFind(payload);
  } catch (error) {
    body.innerHTML = errorHtml('这批场景', error.message, {
      retry: () => runFind(),
      hint: '条件越少越容易成功；也可以先把「城市」清掉再试。',
    });
    bindRetryButtons(body);
  }
}

function renderFind(payload) {
  const items = payload.items || [];
  $('#find-summary').innerHTML = items.length
    ? `<div class="search-head">
         <div><span class="search-word">${payload.total}</span>
           <span class="hint tiny">个场景 · 共 <strong>${payload.shots}</strong> 个拍摄组</span></div>
         <div class="hint tiny">${escapeHtml(payload.note || '')}</div>
       </div>`
    : '<p class="hint tiny">没有匹配的结果，放宽条件试试。</p>';

  if (!items.length) {
    $('#find-results').innerHTML = '';
    return;
  }
  $('#find-results').innerHTML = `
    <table class="find-table">
      <thead><tr><th>场景</th><th>时段（本地）</th><th>拍摄组</th><th>已选/最终</th>
        <th>位置</th><th></th></tr></thead>
      <tbody>${items.map((r) => `
        <tr>
          <td class="mono">#${r.scene_id}</td>
          <td class="mono">${escapeHtml(String(r.first_at || '').slice(0, 16).replace('T', ' '))}
            ~${escapeHtml(String(r.last_at || '').slice(11, 16))}</td>
          <td class="mono">${r.shots}</td>
          <td class="mono">${r.reviewed}/${r.finals}</td>
          <td class="hint tiny">${escapeHtml(r.city || '')}\\${escapeHtml(r.period || '')}</td>
          <td><button class="btn small" data-open-scene="${r.scene_id}">去精选这一场</button></td>
        </tr>`).join('')}</tbody>
    </table>`;

  // 点一下就进精选 —— 这是本模式存在的意义
  document.querySelectorAll('[data-open-scene]').forEach((button) => {
    button.addEventListener('click', async () => {
      const sceneId = Number(button.dataset.openScene);
      const app = await import('./app.js');
      await app.openWorkbench(sceneId);
    });
  });
}

// --------------------------------------------------------------------- 按画面内容

export async function refreshSearch() {
  initSearch();
  try {
    // 命令行提示（由服务端按自己所在的环境给出）与词表并行取
    await Promise.all([
      API.get('/api/cli').then((payload) => {
        const examples = (payload.examples || {}).search;
        if (examples && examples.length) state.cli.search = examples;
      }).catch(() => {}),
      API.get('/api/semantic/queries').then((payload) => {
        state.queries = payload.items || [];
        state.queriesTopHits = Number(payload.top_hits || 200);
        renderPicker(payload.note);
        state.queriesNote = payload.note || '';
      }),
    ]);
    await loadManageCoverage();
    if (!state.queries.length) {
      // ★ 不要让下拉"静静地空着"—— 那看起来就像功能坏了。
      // 常见原因是库还没升级到最新 schema，或者一个字都还没准备。
      // ⚠️ 命令由服务端给（`/api/cli`）：写死路径的话，装到别人的机器上
      // 界面会教用户执行一个不存在的解释器。
      $('#search-results').innerHTML = `
        <div class="wb-empty">
          <p><strong>还没有可以搜的词</strong></p>
          <p class="hint">搜索词是<b>一次性算好</b>的向量：准备一次，以后每次搜都是秒开，
             不需要联网、也不需要重新跑模型。</p>
          ${state.queriesNote ? `<p class="hint tiny">${escapeHtml(state.queriesNote)}</p>` : ''}
          <div class="row search-prepare-row">
            <button class="btn small gold" id="search-prepare-batch" type="button">一键准备常用词</button>
            <input id="search-prepare-words" class="prepare-words-input" type="text"
                   value="${escapeHtml(prepareWords().join('、'))}"
                   title="用、或逗号分隔；改完会记住"
                   aria-label="要准备的词">
            <button class="btn small ghost" id="search-prepare-save" type="button">存为常用词</button>
          </div>
          <p class="hint tiny">还没装模型？
            <button class="linkish" type="button"
                    onclick="window.photoosShowAi && window.photoosShowAi()">去 AI 组件准备</button>
            （中文搜索需要「中文语义搜索」组件）</p>
          <details class="search-cli">
            <summary>命令行用法（进阶）</summary>
            <pre class="search-cmd">${escapeHtml(state.cli.search[0] || 'photoos search 日落 --save')}</pre>
          </details>
        </div>`;
      $('#search-learning').innerHTML = '';
      bindPrepareBatch();
      renderPickBar();
      return;
    }
    if (!state.current) {
      // 默认选第一个（最有把握的那个），让用户一进来就看到东西
      const first = state.queries[0].query_text;
      $('#search-pick').value = first;
      await searchBy(first);
    }
  } catch (error) {
    // 接口不通时也要说清楚，而不是留一个空下拉
    $('#search-results').innerHTML = `
      <div class="wb-empty">
        <p><strong>搜索词表加载失败：${escapeHtml(error.message)}</strong></p>
        <p class="hint tiny">
          如果刚刚更新过代码，请先按 <code>Ctrl+F5</code> 强制刷新一次
          （浏览器可能缓存了旧的 JS）。
        </p>
      </div>`;
  }
}

/**
 * 画面词覆盖状态：新照片没向量、词命中停在旧日期，都必须在搜索页说清楚。
 * 失败时静默隐藏；管理入口仍可打开，由弹窗自己报错。
 */
async function loadManageCoverage() {
  const hint = $('#search-coverage');
  const button = $('#search-manage');
  try {
    const data = await API.get('/api/semantic/manage');
    state.manage = data;
    const cov = data.coverage || {};
    const missingPhotos = Number(cov.missing_photo || 0);
    const stale = (data.items || []).filter((item) => item.stale).length;
    if (hint) {
      // 主搜索栏只说“照片”。视频缺 preview 是视频库自己的事，不能写成
      // “还有 N 张照片缺预览”——那会让用户以为照片搜索结果坏了。
      if (missingPhotos > 0) {
        hint.hidden = false;
        const canEmbed = Number(cov.missing_with_preview_photo || 0);
        hint.innerHTML = `<span class="warn-text">还有 <strong>${missingPhotos}</strong> 张照片尚未参与画面搜索`
          + (canEmbed > 0 ? `（可补 ${canEmbed} 张）` : '（缺 preview）')
          + `；打开「管理关键词」查看覆盖与下一步。</span>`;
      } else if (stale > 0) {
        hint.hidden = false;
        hint.textContent = `${stale} 个词的结果早于最新入库照片，可以重算候选。`;
      } else {
        hint.hidden = true;
        hint.textContent = '';
      }
    }
    if (button) {
      button.textContent = missingPhotos > 0
        ? `管理关键词 · ${missingPhotos} 待补`
        : '管理关键词';
      button.title = missingPhotos > 0
        ? `${missingPhotos} 张照片尚未参与搜索；点开查看覆盖与下一步`
        : '查看关键词覆盖、重算候选或补算新照片向量';
    }
  } catch {
    if (hint) { hint.hidden = true; hint.textContent = ''; }
    if (button) button.textContent = '管理关键词';
  }
}

function ensureSemanticManage() {
  let node = $('#semantic-manage');
  if (node) return node;
  node = document.createElement('div');
  node.id = 'semantic-manage';
  node.className = 'semantic-manage';
  node.hidden = true;
  node.innerHTML = `
    <div class="semantic-manage-backdrop" data-sm-close></div>
    <section class="semantic-manage-panel" role="dialog" aria-modal="true"
             aria-labelledby="sm-title">
      <header class="semantic-manage-head">
        <span>
          <h2 id="sm-title">关键词工作台</h2>
          <p class="hint tiny">先看覆盖，再决定是“重算候选”（秒级）还是“补画面向量”（后台）。</p>
        </span>
        <button class="btn small" type="button" data-sm-close>关闭</button>
      </header>
      <div class="semantic-manage-body" id="sm-body"></div>
      <footer class="semantic-manage-foot">
        <div id="sm-progress" class="semantic-manage-progress"></div>
        <span class="hint tiny" id="sm-selected">未选关键词</span>
        <span class="spacer"></span>
        <button class="btn small" id="sm-refresh" type="button" disabled>重算选中候选</button>
        <button class="btn small primary" id="sm-backfill" type="button" hidden>补算新照片向量…</button>
      </footer>
    </section>`;
  document.body.appendChild(node);
  node.addEventListener('click', (event) => {
    if (event.target.closest('[data-sm-close]')) closeSemanticManage();
    if (event.target.closest('#sm-refresh')) refreshSelectedHits();
    if (event.target.closest('#sm-backfill')) backfillSemanticVectors();
    if (event.target.closest('#sm-add')) addSemanticKeyword();
    const sceneButton = event.target.closest('[data-sm-scene]');
    if (sceneButton) openSceneReview(Number(sceneButton.dataset.smScene));
    const promptButton = event.target.closest('[data-sm-prompt]');
    if (promptButton) editSemanticPrompt(Number(promptButton.dataset.smPrompt));
  });
  node.addEventListener('change', (event) => {
    const id = event.target.dataset.smId;
    if (id) toggleManageSelected(Number(id), Boolean(event.target.checked));
    if (event.target.id === 'sm-all') {
      const all = (state.manage?.items || []).map((item) => Number(item.id));
      state.manageSelected = event.target.checked ? new Set(all) : new Set();
      renderManageTable();
    }
  });
  return node;
}

async function openSemanticManage() {
  ensureSemanticManage();
  $('#semantic-manage').hidden = false;
  await reloadManage();
}

function closeSemanticManage() {
  const node = $('#semantic-manage');
  if (node) node.hidden = true;
}

// ---------------------------------------------------------------- 场景挖掘
//
// 关键词工作台负责“覆盖/命中”；场景复核页负责“按识别线挖 60 张并逐张确认”。
// 标签复用 /api/semantic/feedback（keep = 是这个场景，drop = 不是），
// 所以标签立即沉淀，下一轮挖掘和搜索重排使用同一份真源。

function ensureSceneReview() {
  let node = $('#scene-review');
  if (node) return node;
  node = document.createElement('section');
  node.id = 'scene-review';
  node.className = 'scene-review';
  node.hidden = true;
  node.innerHTML = `
    <div class="scene-review-backdrop" data-scene-close></div>
    <div class="scene-review-panel" role="dialog" aria-modal="true" aria-labelledby="scene-review-title">
      <header class="scene-review-head">
        <span>
          <h2 id="scene-review-title">场景挖掘</h2>
          <p class="hint tiny" id="scene-review-sub">人工确认会立即沉淀到场景；新照片下一轮自动参与。</p>
        </span>
        <button class="btn small" type="button" data-scene-close>关闭</button>
      </header>
      <div class="scene-review-body">
        <div class="scene-review-stats" id="scene-review-stats"></div>
        <div class="scene-suggestion" id="scene-suggestion"></div>
        <div class="scene-review-controls">
          <label class="scene-threshold-label" for="scene-threshold">
            识别线
            <input type="range" id="scene-threshold" min="0.40" max="0.99" step="0.01" value="0.70">
            <span class="mono" id="scene-threshold-value">0.70</span>
          </label>
          <button class="btn primary" id="scene-discover" type="button">发现下一批</button>
          <span class="scene-batch">
            <button class="btn small gold" id="scene-batch-keep" type="button"
                    title="只处理本批尚未标记的照片">本批剩余全部是</button>
            <button class="btn small" id="scene-batch-drop" type="button"
                    title="只处理本批尚未标记的照片">本批剩余全部不是</button>
          </span>
        </div>
        <p class="hint tiny" id="scene-review-hint"></p>
        <div class="scene-review-grid" id="scene-review-grid"></div>
      </div>
    </div>`;
  document.body.appendChild(node);
  node.addEventListener('click', (event) => {
    if (event.target.closest('[data-scene-close]')) closeSceneReview();
    if (event.target.closest('#scene-discover')) discoverSceneBatch();
    if (event.target.closest('#scene-batch-keep')) labelSceneBatch('keep');
    if (event.target.closest('#scene-batch-drop')) labelSceneBatch('drop');
    if (event.target.closest('#scene-use-recommended')) useRecommendedSceneLine();
    const open = event.target.closest('[data-scene-open]');
    if (open) {
      event.preventDefault();
      openSceneAsset(Number(open.dataset.sceneOpen));
      return;
    }
    const label = event.target.closest('[data-scene-label]');
    if (label) {
      const card = label.closest('[data-scene-shot]');
      if (card) labelSceneShot(Number(card.dataset.sceneShot), label.dataset.sceneLabel);
    }
  });
  const slider = node.querySelector('#scene-threshold');
  if (slider) {
    slider.addEventListener('input', () => {
      const value = node.querySelector('#scene-threshold-value');
      if (value) value.textContent = Number(slider.value).toFixed(2);
      const hint = node.querySelector('#scene-review-hint');
      if (hint) hint.textContent = '识别线已改；点「发现下一批」才会按新线取候选。';
    });
  }
  return node;
}

function closeSceneReview() {
  const node = $('#scene-review');
  if (node) node.hidden = true;
}

async function openSceneReview(queryId) {
  ensureSceneReview();
  state.scene = null;
  state.sceneItems = [];
  const node = $('#scene-review');
  node.hidden = false;
  const title = node.querySelector('#scene-review-title');
  if (title) title.textContent = '场景挖掘';
  const grid = node.querySelector('#scene-review-grid');
  if (grid) grid.innerHTML = loadingHtml('正在读取场景状态…');
  try {
    const data = await API.get(`/api/semantic/scenes/${encodeURIComponent(queryId)}`);
    state.scene = data;
    state.sceneItems = [];
    const slider = node.querySelector('#scene-threshold');
    if (slider) slider.value = String(data.threshold ?? 0.70);
    const value = node.querySelector('#scene-threshold-value');
    if (value) value.textContent = Number(data.threshold ?? 0.70).toFixed(2);
    if (title) title.textContent = `场景：${data.query_text || `#${queryId}`}`;
    renderSceneStatus();
    if (grid) {
      grid.innerHTML = '<p class="notice">点「发现下一批」，按当前识别线挖 60 张。</p>';
    }
  } catch (error) {
    if (grid) grid.innerHTML = `<p class="notice">场景状态读取失败：${escapeHtml(error.message)}</p>`;
    toast(`场景读取失败：${error.message}`, true);
  }
}

function renderSceneStatus() {
  const node = $('#scene-review');
  const data = state.scene;
  if (!node || !data) return;
  const stats = node.querySelector('#scene-review-stats');
  const hint = node.querySelector('#scene-review-hint');
  const suggestion = node.querySelector('#scene-suggestion');
  const targets = data.targets || {};
  if (stats) {
    stats.innerHTML = `
      <div><span>已确认</span><strong>是 ${Number(data.confirmed_keep || 0)} · 不是 ${Number(data.confirmed_drop || 0)}</strong></div>
      <div><span>识别线以上未确认</span><strong>${Number(data.above_line || 0).toLocaleString()}</strong></div>
      <div><span>未确认总量</span><strong>${Number(data.unconfirmed || 0).toLocaleString()}</strong></div>
      <div><span>轮次</span><strong>${Number(data.round || 0)}</strong></div>`;
  }
  if (suggestion) {
    const rec = data.recommended;
    const current = Number(data.threshold || 0);
    if (rec && Math.abs(Number(rec.threshold) - current) >= 0.005) {
      const low = current < Number(rec.threshold);
      const unconfirmedAtRec = Number(rec.unconfirmed_above || 0);
      suggestion.className = `scene-suggestion${low ? ' is-warn' : ''}`;
      if (unconfirmedAtRec < 20) {
        suggestion.innerHTML = `
          <span><strong>高分区间已经基本筛完。</strong>
            建议线 ${Number(rec.threshold).toFixed(3)} 以上只剩 ${unconfirmedAtRec} 张未确认；
            当前线以上还有 ${Number(data.above_line || 0).toLocaleString()} 张，但大多是分数明显更低的未确认照片。
            继续拉低识别线会带进大量无关结果。
          </span>`;
      } else {
        suggestion.innerHTML = `
          <span>${low ? '当前识别线偏低，会带进大量无关照片。' : '按已确认标签估算，建议识别线：'}
            <strong>${Number(rec.threshold).toFixed(3)}</strong>
            <span class="hint tiny">（估算精确率 ${(Number(rec.precision) * 100).toFixed(0)}% · 覆盖 ${(Number(rec.recall) * 100).toFixed(0)}%，基于 ${Number(rec.positive_labels)} 正 / ${Number(rec.negative_labels)} 负；建议线以上未确认 ${unconfirmedAtRec} 张）</span>
          </span>
          <button class="btn small gold" id="scene-use-recommended" type="button">用建议线</button>`;
      }
    } else {
      suggestion.className = 'scene-suggestion';
      suggestion.innerHTML = '';
    }
  }
  if (hint) {
    const phase = data.phase === 'warmup'
      ? `前 3 轮：${Number(targets.uncertain ?? 40)} 不确定 + ${Number(targets.certain ?? 20)} 确定`
      : `之后：${Number(targets.certain ?? 40)} 确定，确定不足用不确定补`;
    const roundNo = Number(data.round || 0);
    const roundLabel = data.items
      ? `第 ${roundNo} 轮`
      : `下一轮：第 ${roundNo + 1} 轮`;
    hint.textContent = `${roundLabel} · ${phase}`
      + ` · 当前线以上 ${Number(data.above_line || 0).toLocaleString()} 张，本轮只取 ${Number(data.items ? data.selected : (data.phase === 'warmup' ? 60 : 40))} 张`
      + (data.mode === 'text' ? ' · 冷启动（还没有正例标签）' : ' · 场景模型（正/负例均值）');
  }
}

function useRecommendedSceneLine() {
  const rec = state.scene && state.scene.recommended;
  if (!rec) return;
  const slider = $('#scene-threshold');
  const value = $('#scene-threshold-value');
  if (slider) slider.value = String(rec.threshold);
  if (value) value.textContent = Number(rec.threshold).toFixed(2);
  const hint = $('#scene-review-hint');
  if (hint) hint.textContent = `已设为建议线 ${Number(rec.threshold).toFixed(2)}；点「发现下一批」生效。`;
}

async function discoverSceneBatch() {
  if (state.sceneBusy || !state.scene) return;
  const node = $('#scene-review');
  const button = node && node.querySelector('#scene-discover');
  const slider = node && node.querySelector('#scene-threshold');
  const grid = node && node.querySelector('#scene-review-grid');
  state.sceneBusy = true;
  if (button) { button.disabled = true; button.textContent = '发现中…'; }
  if (grid) grid.innerHTML = loadingHtml('正在给全库打分并抽取候选…');
  try {
    const threshold = slider ? Number(slider.value) : 0.70;
    const data = await API.post(
      `/api/semantic/scenes/${encodeURIComponent(state.scene.query_id)}/discover`,
      { threshold },
    );
    state.scene = data;
    state.sceneItems = data.items || [];
    renderSceneStatus();
    renderSceneItems();
    const value = node && node.querySelector('#scene-threshold-value');
    if (value) value.textContent = Number(data.threshold ?? threshold).toFixed(2);
    if (slider) slider.value = String(data.threshold ?? threshold);
  } catch (error) {
    if (grid) grid.innerHTML = `<p class="notice">发现失败：${escapeHtml(error.message)}</p>`;
    toast(`发现失败：${error.message}`, true);
  } finally {
    state.sceneBusy = false;
    if (button) { button.disabled = false; button.textContent = '发现下一批'; }
  }
}

function renderSceneItems() {
  const node = $('#scene-review');
  const grid = node && node.querySelector('#scene-review-grid');
  if (!grid) return;
  const items = state.sceneItems || [];
  if (!items.length) {
    grid.innerHTML = '<p class="notice">当前识别线下没有未确认候选。新照片入库后还会继续出现。</p>';
    return;
  }
  grid.innerHTML = items.map((item) => {
    const band = item.band === 'certain' ? '确定' : (item.band === 'uncertain' ? '不确定' : '线附近');
    const bandClass = item.band === 'certain' ? 'is-certain' : (item.band === 'uncertain' ? 'is-uncertain' : 'is-near');
    return `
      <figure class="scene-card" data-scene-shot="${item.shot_id}">
        <button class="scene-open" type="button" data-scene-open="${item.asset_id}"
                title="点图看大图和原片信息" aria-label="看大图">
          <img ${assetImgAttrs({ assetId: item.asset_id })}>
          <span class="scene-open-cue">看大图</span>
        </button>
        <figcaption>
          <div class="scene-card-top">
            <span class="scene-band ${bandClass}">${band}</span>
            <span class="scene-score mono">${Number(item.score || 0).toFixed(3)}</span>
          </div>
          <div class="search-when"><span>${escapeHtml(dayPart(item.capture_at))}</span>
            <span class="search-hour">${escapeHtml(hourPart(item.capture_at))}</span></div>
          <div class="scene-card-path" title="${escapeHtml(item.relative_path || '')}">
            ${escapeHtml(baseName(item.relative_path || '') || `#${item.asset_id}`)}
          </div>
          <div class="scene-card-actions">
            <button class="btn small gold" type="button" data-scene-label="keep">是</button>
            <button class="btn small" type="button" data-scene-label="drop">不是</button>
          </div>
        </figcaption>
      </figure>`;
  }).join('');
}

async function openSceneAsset(assetId) {
  if (!assetId) return;
  const app = await import('./app.js');
  await app.openAssetDetail(Number(assetId));
}

async function labelSceneBatch(action) {
  const scene = state.scene;
  if (!scene || state.sceneBatchBusy || !['keep', 'drop'].includes(action)) return;
  const pending = (state.sceneItems || []).filter((item) => !item.label);
  if (!pending.length) {
    toast('本批没有未标记的照片');
    return;
  }
  state.sceneBatchBusy = true;
  const buttons = [...document.querySelectorAll('#scene-batch-keep, #scene-batch-drop')];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const result = await API.post('/api/semantic/feedback', {
      query_id: scene.query_id,
      shot_ids: pending.map((item) => Number(item.shot_id)),
      action,
    });
    if (result.missing) throw new Error('这个词还没有被预置过');
    pending.forEach((item) => { item.label = action; });
    pending.forEach((item) => {
      const card = document.querySelector(`[data-scene-shot="${item.shot_id}"]`);
      if (!card) return;
      card.classList.toggle('is-keep', action === 'keep');
      card.classList.toggle('is-drop', action === 'drop');
      card.querySelectorAll('[data-scene-label]').forEach((button) => {
        button.classList.toggle('is-active', button.dataset.sceneLabel === action);
      });
    });
    const counts = result.counts || {};
    scene.confirmed_keep = Number(counts.keep || 0);
    scene.confirmed_drop = Number(counts.drop || 0);
    scene.confirmed = scene.confirmed_keep + scene.confirmed_drop;
    scene.unconfirmed = Math.max(0, Number(scene.unconfirmed || 0) - pending.length);
    renderSceneStatus();
    toast(`本批已标记 ${pending.length} 张为「${action === 'keep' ? '是' : '不是'}」`);
  } catch (error) {
    toast(`批量标记失败：${error.message}`, true);
  } finally {
    state.sceneBatchBusy = false;
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function labelSceneShot(shotId, action) {
  const scene = state.scene;
  if (!scene || !['keep', 'drop'].includes(action)) return;
  try {
    const result = await API.post('/api/semantic/feedback', {
      query_id: scene.query_id,
      shot_ids: [shotId],
      action,
    });
    if (result.missing) throw new Error('这个词还没有被预置过');
    const item = state.sceneItems.find((row) => Number(row.shot_id) === Number(shotId));
    const firstLabel = item && !item.label;
    if (item) item.label = action;
    const card = document.querySelector(`[data-scene-shot="${shotId}"]`);
    if (card) {
      card.classList.toggle('is-keep', action === 'keep');
      card.classList.toggle('is-drop', action === 'drop');
      card.querySelectorAll('[data-scene-label]').forEach((button) => {
        button.classList.toggle('is-active', button.dataset.sceneLabel === action);
      });
    }
    const counts = result.counts || {};
    scene.confirmed_keep = Number(counts.keep || 0);
    scene.confirmed_drop = Number(counts.drop || 0);
    scene.confirmed = scene.confirmed_keep + scene.confirmed_drop;
    if (firstLabel) scene.unconfirmed = Math.max(0, Number(scene.unconfirmed || 0) - 1);
    renderSceneStatus();
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
  }
}

async function reloadManage() {
  const body = $('#sm-body');
  if (body) body.innerHTML = loadingHtml('正在读取关键词覆盖…');
  try {
    const data = await API.get('/api/semantic/manage');
    state.manage = data;
    const items = data.items || [];
    const previous = new Set(state.manageSelected);
    state.manageSelected = new Set(
      items.map((item) => Number(item.id)).filter((id) => previous.has(id)),
    );
    renderManage();
    loadManageCoverage();
  } catch (error) {
    if (body) body.innerHTML = `<p class="notice">关键词状态读取失败：${escapeHtml(error.message)}</p>`;
  }
}

function renderManage() {
  const data = state.manage;
  if (!data) return;
  const cov = data.coverage || {};
  const items = data.items || [];
  const missing = Number(cov.missing || 0);
  const missingPhotos = Number(cov.missing_photo || 0);
  const missingVideos = Number(cov.missing_video || 0);
  const canEmbedPhotos = Number(cov.missing_with_preview_photo || 0);
  const noPreviewPhotos = Number(cov.missing_without_preview_photo || 0);
  const stale = items.filter((item) => item.stale).length;
  const missingLabel = missingPhotos > 0
    ? `还缺 <strong>${missingPhotos.toLocaleString()}</strong> 张照片`
      + (canEmbedPhotos ? `（可补 ${canEmbedPhotos.toLocaleString()}）` : '（缺 preview）')
      + (missingVideos ? `；另有 ${missingVideos.toLocaleString()} 个视频` : '')
    : (missingVideos
      ? `还缺 <strong>${missingVideos.toLocaleString()}</strong> 个视频（不参与照片搜索）`
      : `还缺 <strong>${missing.toLocaleString()}</strong> 个资产`);
  const topHits = Number(data.top_hits || 200);
  const body = $('#sm-body');
  if (!body) return;
  body.innerHTML = `
    <div class="semantic-coverage">
      <div>
        <strong>${Number(cov.embedded || 0).toLocaleString()}</strong>
        <span>/ ${Number(cov.total_assets || 0).toLocaleString()} 个资产已有画面向量</span>
      </div>
      <div class="${missing ? 'is-warn' : ''}">
        ${missing ? missingLabel : '画面向量覆盖完整'}
      </div>
      <div>上次命中 <span class="mono">${escapeHtml(cov.last_hit_at || '—')}</span></div>
      <div>${stale} 个词可能过期</div>
    </div>
    ${missingPhotos > 0
      ? `<p class="hint tiny">还有 ${missingPhotos.toLocaleString()} 张照片没有画面向量；照片可补 ${canEmbedPhotos.toLocaleString()} 张。补向量是全局动作，所有词共用。</p>`
      : ''}
    ${noPreviewPhotos > 0
      ? `<p class="notice">其中 ${noPreviewPhotos.toLocaleString()} 张照片缺 preview：先到「维护 → 数据维护 → 索引新照片」生成预览，再来补向量。</p>`
      : ''}
    ${missingPhotos === 0 && missingVideos > 0
      ? `<p class="notice">剩下缺向量的 ${missingVideos.toLocaleString()} 个都是视频库资产，不参与照片画面词搜索；如需视频预览，请到「维护」单独处理。</p>`
      : ''}
    <div class="semantic-add">
      <input id="sm-new-word" class="semantic-add-word" type="text" maxlength="40"
             placeholder="新关键词，如 烟花" aria-label="新关键词">
      <input id="sm-new-prompt" class="semantic-add-prompt" type="text" maxlength="200"
             placeholder="实际 prompt（可留空，默认同关键词；英文通常更稳）" aria-label="实际 prompt">
      <button class="btn small gold" id="sm-add" type="button">添加并计算</button>
    </div>
    <p class="hint tiny semantic-cap-note">
      每个词的结果池最多保留 ${topHits.toLocaleString()} 组候选；表里显示
      <strong>${topHits.toLocaleString()}+ 组</strong>表示已到上限，不代表全库只有这么多组匹配。
    </p>
    <div class="semantic-table-wrap">
      <table class="semantic-table">
        <thead><tr>
          <th><input type="checkbox" id="sm-all" aria-label="全选关键词"></th>
          <th>关键词</th><th>实际 prompt</th><th>候选池</th><th>上次重算</th>
          <th>我的表态</th><th>状态</th><th>场景</th>
        </tr></thead>
        <tbody>
          ${items.map((item) => `
            <tr data-sm-row="${item.id}" class="${state.manageSelected.has(Number(item.id)) ? 'is-picked' : ''}">
              <td><input type="checkbox" data-sm-id="${item.id}"
                   ${state.manageSelected.has(Number(item.id)) ? 'checked' : ''}
                   aria-label="选择 ${escapeHtml(item.query_text)}"></td>
              <td><strong>${escapeHtml(item.query_text)}</strong></td>
              <td class="hint tiny">${escapeHtml(item.prompt_text || item.query_text)}
                <button class="linkish" type="button" data-sm-prompt="${item.id}"
                        title="改实际送进模型的 prompt；会重算这个词的向量与命中">改</button></td>
              <td class="mono">${hitCountLabel(item.hit_count, topHits)}${Number(item.hit_count || 0) >= topHits
                ? ' <span class="semantic-cap">上限</span>' : ''}</td>
              <td class="hint tiny">${escapeHtml(item.last_hit_at || '—')}</td>
              <td class="hint tiny">要 ${Number(item.feedback_keep || 0)} · 不要 ${Number(item.feedback_drop || 0)}</td>
              <td>${item.stale
                ? '<span class="semantic-chip is-stale">可能过期</span>'
                : '<span class="semantic-chip">完整</span>'}</td>
              <td><button class="btn small" type="button" data-sm-scene="${item.id}"
                    title="进入场景：按识别线挖 60 张，标完沉淀到场景">发现</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  updateManageSelection();
  const backfill = $('#sm-backfill');
  if (backfill) {
    if (missingPhotos) {
      backfill.hidden = false;
      backfill.disabled = canEmbedPhotos === 0;
      backfill.textContent = canEmbedPhotos
        ? `补算新照片向量（${canEmbedPhotos.toLocaleString()}）`
        : '照片缺 preview，暂不能补向量';
      backfill.title = canEmbedPhotos
        ? '后台任务，分钟到小时级；需要已安装中文语义搜索组件'
        : '这些照片没有 preview，先生成预览';
    } else {
      backfill.hidden = true;
      backfill.disabled = true;
    }
  }
}

function renderManageTable() {
  renderManage();
}

function toggleManageSelected(id, checked) {
  if (checked) state.manageSelected.add(id);
  else state.manageSelected.delete(id);
  updateManageSelection();
}

function updateManageSelection() {
  const count = state.manageSelected.size;
  const label = $('#sm-selected');
  if (label) label.textContent = count ? `已选 ${count} 个关键词` : '未选关键词';
  const refresh = $('#sm-refresh');
  if (refresh) refresh.disabled = count === 0 || state.manageBusy;
  const all = $('#sm-all');
  if (all) {
    const total = (state.manage?.items || []).length;
    all.checked = total > 0 && count === total;
    all.indeterminate = count > 0 && count < total;
  }
  document.querySelectorAll('[data-sm-row]').forEach((row) => {
    row.classList.toggle('is-picked', state.manageSelected.has(Number(row.dataset.smRow)));
  });
}

async function refreshSelectedHits() {
  const ids = [...state.manageSelected];
  if (!ids.length || state.manageBusy) return;
  state.manageBusy = true;
  updateManageSelection();
  const button = $('#sm-refresh');
  const old = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = '重算中…'; }
  try {
    const result = await API.post('/api/semantic/refresh', { query_ids: ids });
    toast(`已重算 ${result.updated} 个词，写入 ${result.hits} 条候选`);
    const currentId = state.current && Number(state.current.id);
    await reloadManage();
    if (currentId && ids.includes(currentId)) await searchBy(state.current.query_text, { quiet: true });
    else await searchByCurrentIfNeeded();
  } catch (error) {
    toast(`重算失败：${error.message}`, true);
  } finally {
    state.manageBusy = false;
    if (button) { button.disabled = false; button.textContent = old || '更新选中命中'; }
    updateManageSelection();
  }
}

function askSemanticText(message, defaultValue) {
  // Electron 默认不实现 window.prompt，桌面壳暴露了同步桥；浏览器里退回原生 prompt。
  if (window.photoosDesktop && typeof window.photoosDesktop.promptSync === 'function') {
    return window.photoosDesktop.promptSync(message, defaultValue);
  }
  return window.prompt(message, defaultValue);
}

async function addSemanticKeyword() {
  const wordInput = $('#sm-new-word');
  const promptInput = $('#sm-new-prompt');
  const word = String(wordInput?.value || '').trim();
  if (!word) {
    toast('先填一个关键词', true);
    wordInput?.focus();
    return;
  }
  const prompt = String(promptInput?.value || '').trim() || word;
  let action = catalogAction('search_rebuild');
  if (!action) {
    try { await loadCatalog(); } catch { /* 下面会提示 */ }
    action = catalogAction('search_rebuild');
  }
  if (!action) { toast('命令目录还没载入，稍后再试', true); return; }
  if (!action.available) {
    toast(`缺少依赖：${(action.missing_requires || []).join('、')}`, true);
    return;
  }
  try {
    const job = await runActionWithProgress(action, $('#sm-progress'), {
      query: word,
      prompt,
      label: `添加「${word}」`,
    });
    if (job && job.status === 'COMPLETED') {
      await reloadManage();
      await loadManageCoverage();
      const payload = await API.get('/api/semantic/queries');
      state.queries = payload.items || [];
      state.queriesTopHits = Number(payload.top_hits || 200);
      state.queriesNote = payload.note || '';
      renderPicker(payload.note);
      toast(`已添加「${word}」；关闭工作台后可在下拉框里搜它`);
    }
  } catch (error) {
    toast(`添加失败：${error.message}`, true);
  }
}

async function editSemanticPrompt(queryId) {
  const item = (state.manage?.items || []).find((row) => Number(row.id) === Number(queryId));
  if (!item) return;
  const next = askSemanticText(
    `「${item.query_text}」实际送进模型的 prompt（中文标签不变）：`,
    item.prompt_text || item.query_text,
  );
  if (next === null || next === undefined) return;
  const prompt = String(next).trim();
  if (!prompt || prompt === String(item.prompt_text || item.query_text)) return;
  let action = catalogAction('search_rebuild');
  if (!action) {
    try { await loadCatalog(); } catch { /* 下面会提示 */ }
    action = catalogAction('search_rebuild');
  }
  if (!action) { toast('命令目录还没载入，稍后再试', true); return; }
  if (!action.available) {
    toast(`缺少依赖：${(action.missing_requires || []).join('、')}`, true);
    return;
  }
  if (!window.confirm(
    `改「${item.query_text}」的 prompt？\n\n`
    + `新 prompt：${prompt}\n\n`
    + '会重算这个词的文本向量与命中（需要 AI 环境，通常几秒）。'
  )) return;
  try {
    const job = await runActionWithProgress(action, $('#sm-progress'), {
      query: item.query_text,
      prompt,
      label: `重建「${item.query_text}」`,
    });
    if (job && job.status === 'COMPLETED') {
      await reloadManage();
      await loadManageCoverage();
      if (state.current && Number(state.current.id) === Number(queryId)) {
        await searchBy(state.current.query_text, { quiet: true });
      }
    }
  } catch (error) {
    toast(`改词义失败：${error.message}`, true);
  }
}

async function searchByCurrentIfNeeded() {
  if (state.current) await searchBy(state.current.query_text, { quiet: true });
}

async function backfillSemanticVectors() {
  const cov = state.manage && state.manage.coverage;
  if (!cov || Number(cov.missing_with_preview_photo || 0) === 0) return;
  let action = catalogAction('embed_gemma');
  if (!action) {
    try { await loadCatalog(); } catch { /* 下面会提示 */ }
    action = catalogAction('embed_gemma');
  }
  if (!action) { toast('命令目录还没载入，稍后再试', true); return; }
  if (!action.available) {
    toast(`缺少依赖：${(action.missing_requires || []).join('、')}`, true);
    return;
  }
  const n = Number(cov.missing_with_preview_photo).toLocaleString();
  if (!window.confirm(
    `补算 ${n} 张新照片的画面向量？\n\n`
    + '这是全局后台任务，所有关键词共用；CPU 可能要数小时，CUDA 会快很多。'
    + '任务可暂停/取消，已写入的向量不会丢。完成后会自动刷新命中。'
  )) return;
  const button = $('#sm-backfill');
  const progress = $('#sm-progress');
  try {
    const job = await runActionWithProgress(action, progress, {
      button, label: `补算新照片向量（${n}）`,
    });
    if (!job) return;
    if (job.status === 'COMPLETED') {
      const ids = (state.manage.items || []).map((item) => Number(item.id));
      const result = await API.post('/api/semantic/refresh', { query_ids: ids });
      toast(`向量补算完成；已刷新 ${result.updated} 个词的命中`);
      await reloadManage();
      await loadManageCoverage();
      await searchByCurrentIfNeeded();
    } else {
      toast(`补算任务未完成：${job.status}`, true);
    }
  } catch (error) {
    toast(`补算失败：${error.message}`, true);
  }
}

function hitCountLabel(count, topHits) {
  const n = Number(count || 0);
  const cap = Number(topHits || 200);
  return n >= cap ? `${n}+ 组` : `${n} 组`;
}

function renderPicker(note) {
  const pick = $('#search-pick');
  const topHits = Number(state.queriesTopHits || 200);
  const options = state.queries
    .map((q) => `<option value="${escapeHtml(q.query_text)}">${escapeHtml(q.query_text)}`
      + `（${hitCountLabel(q.hit_count, topHits)}候选）</option>`)
    .join('');
  pick.innerHTML = `<option value="">— 选择一个预置搜索词（括号内为候选组数）—</option>`
    + options;
  const total = $('#search-total');
  total.textContent = note || `可用搜索词 ${state.queries.length} 个`;
}

/**
 * 取回一个词的结果并渲染。
 *
 * `quiet` = **原地刷新**：不清空容器、保留滚动位置。表态之后走这条路 ——
 * 用户正看着第 30 张，重排完还得看得见第 30 张附近，而不是被弹回页首。
 */
async function searchBy(text, options = {}) {
  const quiet = Boolean(options.quiet);
  const snap = options.snap || captureScroll();
  const body = $('#search-results');

  // 换一个词＝重新开始，上一次的多选就不该跟着过去
  if (!quiet && (!state.current || state.current.query_text !== text)) clearSelection();

  if (quiet) body.classList.add('is-busy');
  else body.innerHTML = loadingHtml('正在查询…');

  try {
    const url = `/api/semantic/results?query=${encodeURIComponent(text)}`
      + `&tuning=${state.showTuning ? 1 : 0}`;
    const payload = await API.get(url);
    if (payload.missing) {
      state.current = null;
      state.items = [];
      $('#search-learning').innerHTML = '';
      renderResults();          // 清掉旧卡片，否则会停在"上一个词"的结果上
      // 先把命令目录拿到再渲染：那个空态里的「立即准备」按钮要据此决定是否可用。
      // 目录已经在预热里拿到了的话这一句是瞬时的。
      try { await loadCatalog(); } catch { /* 按钮会退化为不可用 */ }
      renderNotReady(payload);
      return;
    }
    state.current = payload;
    state.items = payload.items || [];
    state.feedback = payload.feedback || {};
    state.counts = payload.feedback_counts || { keep: 0, drop: 0 };
    state.tuned = Boolean(payload.tuned);
    state.tuning = payload.tuning || null;
    renderLearning();
    renderResults();
  } catch (error) {
    // 失败要**留得住**：以前这里只写一句 `<p class="hint">`，切走再回来就什么都没了，
    // 也分不清"查询失败"与"这个词没有结果"。见 shared.js 的 errorHtml。
    if (quiet) {
      toast(`重排失败：${error.message}`, true);
    } else {
      body.innerHTML = errorHtml('这个搜索的结果', error.message, {
        retry: () => searchBy(text),
        hint: '本地服务通常几百毫秒就回来；若反复失败，去「维护」页看后台任务有没有卡住。',
      });
      bindRetryButtons(body);
    }
  } finally {
    body.classList.remove('is-busy');
    // 只有"原地刷新"才还原位置：换一个词就该从新结果的开头看起
    if (quiet) restoreScroll(snap);
  }
}

/**
 * 没准备过的词：**说清楚怎么办**，并且**直接给出那个按钮**。
 *
 * 以前这里只说"请去命令行执行一次"，于是用户得复制命令 → 切终端 → 粘贴 →
 * 回浏览器刷新。现在同一句话后面跟一个「立即准备」按钮：按下就是那条命令，
 * 走后台任务，判到哪一步都看得见。
 *
 * ⚠️ 命令本身仍然只在服务端定义（`actions/catalog.py`）—— 前端只提交 action id。
 * 保留 `payload.hint`（服务端给的命令行）是为了让"想自己动手"的用户仍能照抄，
 * 也为了在服务端拒绝执行时给出兜底说法。
 */
function renderNotReady(payload) {
  const hint = escapeHtml(payload.hint || '').replace(/\n/g, '<br>');
  const term = payload.query_text || '';
  const canRun = !!catalogAction('search_prepare');
  $('#search-results').innerHTML = `
    <div class="wb-empty">
      <p><strong>「${escapeHtml(term)}」这个词还没有准备过。</strong></p>
      <p class="hint">
        搜索要用模型算一次文字向量。点下面这个按钮就交给后台去做 ——
        第一次要下模型，会跑一阵子；跑完这里的下拉框里就会出现这个词。
      </p>
      <div class="not-ready-actions">
        <button class="btn primary" id="search-prepare"${canRun ? '' : ' disabled'}
                title="${canRun ? '在后台执行：准备这个搜索词' : '命令目录未载入'}">
          立即准备「${escapeHtml(term)}」
        </button>
        <button class="btn small" id="search-prepare-more">其它可用命令…</button>
      </div>
      <!-- 进度画在这里：按钮文字只有一行，说不清"跑到哪了、有没有卡住" -->
      <div id="search-prepare-progress" class="not-ready-progress"></div>
      <details class="not-ready-manual">
        <summary>我想自己在命令行跑</summary>
        <pre class="search-cmd">${hint}</pre>
      </details>
    </div>`;

  const button = $('#search-prepare');
  if (button && canRun) {
    button.onclick = async () => {
      const action = catalogAction('search_prepare');
      if (!action) return;
      const job = await runActionWithProgress(action, $('#search-prepare-progress'), {
        query: term,
        button,
        label: `准备「${term}」`,
      });
      // 成功了就把这个词立刻接上：用户不必自己再点一次搜索
      if (job && job.status === 'COMPLETED') {
        toast('准备完成，正在载入结果…');
        await refreshSearch();
        await searchBy(term);
      }
    };
  }
  const more = $('#search-prepare-more');
  if (more) {
    more.onclick = () => openActionsDialog({ title: '可直跑的命令', group: '找照片' });
  }
}

/**
 * 「你的判断」面板：显示表态统计、提供开关与"存成主题"。
 *
 * ⚠️ 措辞纪律：这里显示的是**用户的表态统计**，不是模型给照片打的分。
 * 所以写"我要这种 / 不是这种"，绝不写"模型置信度 / 准确率 / 评分"。
 */
function renderLearning() {
  const node = $('#search-learning');
  const counts = state.counts || { keep: 0, drop: 0 };
  const total = (counts.keep || 0) + (counts.drop || 0);
  const notes = (state.tuning && state.tuning.notes) || [];
  const visible = state.onlyConfirmed
    ? state.items.filter((item) => state.feedback[String(item.shot_id)] === 'keep')
    : state.items;

  const mode = total ? 'clean' : 'raw';
  node.innerHTML = `
    <div class="learn-bar${state.onlyConfirmed ? ' is-confirmed-only' : ''}">
      <div class="learn-info">
        <span class="learn-title">你的判断</span>
        <span class="learn-count">我要这种 <b>${counts.keep || 0}</b> · 不是这种 <b>${counts.drop || 0}</b></span>
        ${state.tuned
          ? `<span class="learn-badge">已按你的修改重排</span>`
          : (total < 2
            ? '<span class="hint tiny">标两张以上才会重排</span>'
            : '<span class="hint tiny">重排未生效</span>')}
      </div>
      <div class="learn-actions">
        <label class="check inline search-confirmed-filter">
          <input type="checkbox" id="search-only-confirmed" ${state.onlyConfirmed ? 'checked' : ''}>
          <span>只看我确认过的（<b>${visible.length}</b>）</span>
        </label>
        <span class="confirmed-only-actions">
          <label class="check inline">
            <input type="checkbox" id="search-tune-toggle" ${state.showTuning ? 'checked' : ''}>
            <span>按我的修改重排</span>
          </label>
          <button class="btn small gold" id="search-keep-all"
                  title="把本页 ${visible.length} 组一次标成「我要这种」（只发一次请求）"
                  ${visible.length ? '' : 'disabled'}>
            全部「我要这种」</button>
          <button class="btn small" id="search-drop-all"
                  title="把本页 ${visible.length} 组一次标成「不是这种」（只发一次请求）"
                  ${visible.length ? '' : 'disabled'}>
            全部「不是这种」</button>
        </span>
        <button class="btn small" id="search-save-theme">结果存成主题</button>
        <button class="btn small ghost" id="search-drop-query"
                title="删除这个搜索词；下拉框里不再出现，已算好的结果也会一起清掉">
          删除这个词</button>
        ${total ? '<button class="btn small" id="search-clear-feedback">清空修改</button>' : ''}
      </div>
    </div>
    ${notes.length ? `<p class="hint tiny">${notes.map(escapeHtml).join(' ')}</p>` : ''}`;

  const onlyConfirmed = $('#search-only-confirmed');
  if (onlyConfirmed) {
    onlyConfirmed.onchange = () => {
      state.onlyConfirmed = onlyConfirmed.checked;
      state.selected.clear();
      state.anchor = null;
      renderLearning();
      renderResults();
    };
  }
  const toggle = $('#search-tune-toggle');
  if (toggle) {
    toggle.onchange = () => {
      state.showTuning = toggle.checked;
      if (state.current) searchBy(state.current.query_text, { quiet: true });
    };
  }
  // ★ 一键确认（找照片侧）：整页都要。**一次请求**，不是 N 次表态 ——
  // 逐张点 60 下和点一下"全要了"是同一件事，前者只是更累。
  const keepAll = $('#search-keep-all');
  if (keepAll) {
    keepAll.onclick = () => {
      const ids = state.items.map((item) => Number(item.shot_id));
      if (ids.length) sendFeedback(ids, 'keep');
    };
  }
  // 一键否决：整页「不是这种」。和"全部要了"一样只发一次请求。
  const dropAll = $('#search-drop-all');
  if (dropAll) {
    dropAll.onclick = () => {
      const ids = state.items.map((item) => Number(item.shot_id));
      if (ids.length) sendFeedback(ids, 'drop');
    };
  }
  // 删除词条：连词条本身一起删（不是"清空修改"——那只是抹掉我的表态）
  const dropQuery = $('#search-drop-query');
  if (dropQuery) {
    dropQuery.onclick = async () => {
      const text = state.current ? state.current.query_text : '';
      if (!text) return;
      const ok = confirm(`删除搜索词「${text}」？\n\n它会从下拉框消失，相关的命中与我的表态一并清掉；\n需要时可以重新准备这个词。`);
      if (!ok) return;
      try {
        await API.del(`/api/semantic/feedback?query=${encodeURIComponent(text)}&drop=true`);
        toast('词条已删除');
        state.current = null;
        state.items = [];
        await refreshSearch();
      } catch (error) {
        toast(`删除失败：${error.message}`);
      }
    };
  }
  const save = $('#search-save-theme');
  if (save) save.onclick = () => saveAsTheme(mode);
  const clear = $('#search-clear-feedback');
  if (clear) clear.onclick = clearFeedback;
}

// --------------------------------------------------------------------- 表态（含多选）

/**
 * 表态队列：**永远只有一个请求在飞**。
 *
 * 队列不是为了性能（一条表态本来就只有几十毫秒），是为了**不丢点击**：
 * 直接"忙就忽略"会让用户在 0.6 秒的重排窗口里点的那一下凭空消失，
 * 而界面看上去什么也没发生 —— 那是最难查的一类问题。
 */
/** 一键准备常用词：逐个排队（服务端一次只跑一个 AI 任务）。 */
const DEFAULT_PREPARE_WORDS = ['日落', '天空', '海', '人像', '猫', '城市'];
const PREPARE_STORAGE_KEY = 'photoos.prepareWords';

function prepareWords() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREPARE_STORAGE_KEY) || 'null');
    if (Array.isArray(saved) && saved.length) return saved;
  } catch { /* 坏数据就当没有 */ }
  return DEFAULT_PREPARE_WORDS.slice();
}

function savePrepareWords(words) {
  try { localStorage.setItem(PREPARE_STORAGE_KEY, JSON.stringify(words)); } catch { /* 隐私模式 */ }
}

/** 等一个动作任务跑完（按 id 精确匹配）。 */
async function runActionAndWait(actionId, body, hint, label) {
  const queued = await API.post(`/api/actions/${actionId}/run`, body || {});
  const jobId = queued && queued.job ? queued.job.id : null;
  if (!jobId) return { ok: false, error: (queued && queued.reason) || '未能入队' };
  const started = Date.now();
  while (Date.now() - started < 60 * 60 * 1000) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    let job = null;
    try {
      const payload = await API.get('/api/jobs?limit=8');
      job = (payload.jobs || []).find((item) => Number(item.id) === Number(jobId));
    } catch { /* 轮询失败继续 */ }
    if (!job) continue;
    const state = String(job.status || '').toUpperCase();
    if (hint) hint.textContent = `${label}…（${state === 'PROCESSING' ? '进行中' : state}）`;
    if (state === 'COMPLETED') return { ok: true, job };
    if (['FAILED', 'CANCELLED'].includes(state)) {
      return { ok: false, error: job.error || '任务失败' };
    }
  }
  return { ok: false, error: '等待超时' };
}

function bindPrepareBatch() {
  const button = $('#search-prepare-batch');
  const hint = $('#search-prepare-hint');
  if (!button) return;
  const input = $('#search-prepare-words');
  const saveBtn = $('#search-prepare-save');
  const words = () => (input && input.value ? input.value : '')
    .split(/[、,，\s]+/).map((w) => w.trim()).filter(Boolean).slice(0, 12);
  if (input) input.value = prepareWords().join('、');
  if (saveBtn) {
    saveBtn.onclick = () => {
      savePrepareWords(words());
      if (hint) hint.textContent = `已记住 ${words().length} 个词`;
    };
  }
  button.onclick = async () => {
    button.disabled = true;
    // ★ 先确认组件真的装了：否则命令会跑到"模型缺失"才失败（实测：
    //   #21 任务报 embeddinggemma model.safetensors 不存在），用户只看到一串红字。
    try {
      const data = await API.get('/api/ai/components');
      const gemma = (data.components || []).find((c) => c.id === 'gemma-text');
      if (!gemma || !gemma.installed) {
        if (hint) {
          hint.innerHTML = '中文语义搜索组件还没安装。'
            + '<button class="linkish" type="button" onclick="window.photoosShowAi && window.photoosShowAi()">去 AI 组件安装</button>'
            + '（约 2.75 GB，装完再回来点这里）';
        }
        button.disabled = false;
        return;
      }
    } catch { /* 查不到就继续，让命令自己报错 */ }
    const list = words();
    savePrepareWords(list);
    let done = 0;
    for (const word of list) {
      if (hint) hint.textContent = `正在准备「${word}」…（${done + 1}/${list.length}）`;
      try {
        // 逐词等它**跑完**再进下一个：队列同时只允许一条同类任务，
        // 连着 POST 会被"已有一条在跑"顶回来（实测：一键准备只成功一个）。
        const outcome = await runActionAndWait(
          'search_prepare', { query: word }, hint, `准备「${word}」`);
        if (!outcome.ok) throw new Error(outcome.error);
        done += 1;
      } catch (error) {
        const message = String(error.message || '');
        if (message.includes('向量')) {
          // 组件装了但库里还没有向量：先把嵌入算出来，再重试这一个词
          if (hint) hint.textContent = '库里还没有向量，正在计算嵌入（照片多时要几分钟）…';
          const outcome = await runActionAndWait('embed_gemma', {}, hint, '计算嵌入');
          if (!outcome.ok) {
            if (hint) hint.textContent = `计算嵌入失败：${outcome.error}`;
            button.disabled = false;
            return;
          }
          try {
            const retry = await runActionAndWait(
              'search_prepare', { query: word }, hint, `准备「${word}」`);
            if (!retry.ok) throw new Error(retry.error);
            done += 1;
            continue;
          } catch (retryError) {
            if (hint) hint.textContent = `「${word}」仍然失败：${retryError.message}`;
            button.disabled = false;
            return;
          }
        }
        if (hint) {
          hint.textContent = `「${word}」失败：${message}`;
        }
        button.disabled = false;
        return;
      }
    }
    if (hint) hint.textContent = `已准备 ${done} 个词，正在刷新…`;
    await refreshSearch();
  };
}

async function sendFeedback(shotIds, action) {
  const current = state.current;
  const ids = [...new Set(shotIds.map(Number))].filter((n) => Number.isFinite(n));
  if (!current || !ids.length) return;
  state.queue.push({ queryId: current.id, ids, action });
  if (state.sending) return;
  state.sending = true;
  try {
    while (state.queue.length) {
      const job = state.queue.shift();
      // 排队期间用户可能换了词：那就丢掉这一条，别把 A 词的判断写到 B 词上
      if (!state.current || state.current.id !== job.queryId) continue;
      await runFeedback(job.ids, job.action);
    }
  } finally {
    state.sending = false;
  }
}

async function runFeedback(ids, action) {
  const current = state.current;
  if (!current) return;
  const body = $('#search-results');
  const snap = captureScroll();
  body.classList.add('is-busy');           // 只压暗；**绝不**清空容器（清空就跳回顶部）
  try {
    const payload = await API.post('/api/semantic/feedback', {
      query_id: current.id, shot_ids: ids, action,
    });
    state.counts = payload.counts || state.counts;
    clearSelection();
    // 立刻重查：让"我改了一下，整页跟着变"当场可见，但**留在原地**
    await searchBy(current.query_text, { quiet: true, snap });
    if (payload.skipped && payload.skipped.length) {
      toast(`${payload.skipped.length} 组已经被重算过，跳过了`, true);
    }
  } catch (error) {
    body.classList.remove('is-busy');
    toast(`没能记下这一条：${error.message}`, true);
  }
}

async function clearFeedback() {
  const current = state.current;
  if (!current) return;
  const ok = confirm(
    `清空「${current.query_text}」上的全部修改？\n\n`
    + '只会忘掉你标过的"要 / 不要"，不会删除任何照片，也不会影响主题。');
  if (!ok) return;
  try {
    await API.del(`/api/semantic/feedback?query_id=${current.id}`);
    state.showTuning = true;
    clearSelection();
    await searchBy(current.query_text, { quiet: true });
    toast('已清空，回到最初的搜索结果');
  } catch (error) {
    toast(`清空失败：${error.message}`, true);
  }
}

/** 把这个词的结果存成一个主题（默认"减掉你删掉的那些"）。 */
async function saveAsTheme(mode) {
  const current = state.current;
  if (!current) return;
  const name = prompt('主题名称', current.query_text);
  if (!name) return;
  let chosen = mode;
  if ((state.counts.keep || 0) + (state.counts.drop || 0) > 0) {
    const picked = prompt(
      '存哪些？输入 1 = 只要我选中的；2 = 全部减掉我删掉的；3 = 不管我的修改，用原始结果',
      mode === 'kept' ? '1' : (mode === 'raw' ? '3' : '2'));
    if (picked === null) return;
    chosen = { 1: 'kept', 2: 'clean', 3: 'raw' }[String(picked).trim()] || 'clean';
  }
  try {
    const payload = await API.post('/api/semantic/collection', {
      query_id: current.id, name, mode: chosen,
    });
    toast(`已${payload.created ? '新建' : '加入'}主题「${payload.name}」：`
      + `${payload.added} 张（重复的自动跳过）`);
  } catch (error) {
    toast(`存成主题失败：${error.message}`, true);
  }
}

// --------------------------------------------------------------------- 多选

/**
 * 点图 = 选中/取消；按住 Shift 点 = 把上一次点到这一张之间**连着**选上。
 *
 * Shift 是**加法**（只往选择里加，不清掉已有的）：在网格里挑照片时，
 * "先散着点几张，再补一段连续的"比"每次都从头框选"常见得多。
 */
function togglePick(shotId, { range = false } = {}) {
  const ids = state.items.map((item) => Number(item.shot_id));
  const anchor = state.anchor;
  if (range && anchor !== null && ids.includes(anchor) && ids.includes(shotId)) {
    const from = ids.indexOf(anchor);
    const to = ids.indexOf(shotId);
    const [lo, hi] = from <= to ? [from, to] : [to, from];
    for (const id of ids.slice(lo, hi + 1)) state.selected.add(id);
  } else if (state.selected.has(shotId)) {
    state.selected.delete(shotId);
  } else {
    state.selected.add(shotId);
  }
  state.anchor = shotId;
  syncSelection();
}

function clearSelection() {
  state.selected.clear();
  state.anchor = null;
  syncSelection();
}

function handlePickAction(action) {
  if (action === 'none') {
    clearSelection();
    return;
  }
  if (action === 'all') {
    state.items.forEach((item) => state.selected.add(Number(item.shot_id)));
    const last = state.items[state.items.length - 1];
    state.anchor = last ? Number(last.shot_id) : null;
    syncSelection();
    return;
  }
  if (action === 'keep' || action === 'drop') {
    const ids = [...state.selected];
    if (ids.length) sendFeedback(ids, action);
  }
}

/** 选择集与界面同步：结果里已经没有的 Shot 不留，卡片样式与操作条一起刷新。 */
function syncSelection() {
  const alive = new Set(state.items.map((item) => Number(item.shot_id)));
  for (const id of [...state.selected]) if (!alive.has(id)) state.selected.delete(id);
  if (state.anchor !== null && !alive.has(state.anchor)) state.anchor = null;
  $('#search-results').querySelectorAll('[data-shot]').forEach(applyPickStyle);
  renderPickBar();
}

function applyPickStyle(node) {
  const picked = state.selected.has(Number(node.dataset.shot));
  node.classList.toggle('is-picked', picked);
  const button = node.querySelector('.sc-pick');
  if (!button) return;
  button.setAttribute('aria-pressed', picked ? 'true' : 'false');
  button.title = picked
    ? '取消选中这一组'
    : '选中这一组（按住 Shift 点可以连着选一片）';
}

function renderPickBar() {
  const bar = $('#search-pick-bar');
  const count = state.selected.size;
  bar.hidden = count === 0;
  if (!count) {
    bar.innerHTML = '';
    return;
  }
  bar.innerHTML = `
    <span class="pick-count">已选 <b>${count}</b> 组</span>
    <span class="hint tiny pick-tip">按住 Shift 点一张＝把它到上一张之间连着选上</span>
    <div class="pick-actions">
      <button class="btn small gold" data-pick-act="keep">我要这种</button>
      <button class="btn small danger" data-pick-act="drop">不是这种</button>
      <button class="btn small" data-pick-act="all">全选这 ${state.items.length} 组</button>
      <button class="btn small" data-pick-act="none">取消选择</button>
    </div>`;
}

// --------------------------------------------------------------------- 渲染

/** 结果头 + 网格。**只认领节点，不重建**（见文件头"为什么不能重建整页"）。 */
function renderResults() {
  const q = state.current;
  const items = state.onlyConfirmed
    ? state.items.filter((item) => state.feedback[String(item.shot_id)] === 'keep')
    : state.items;
  const head = $('#search-summary');
  const grid = $('#search-results');

  if (!q) {
    head.innerHTML = '';
    grid.innerHTML = '';
    state.selected.clear();
    state.anchor = null;
    renderPickBar();
    renderContentQueryBar();          // 条件条是 state 的投影：没有查询就没有条件
    return;
  }

  head.innerHTML = `
    <div class="search-head">
      <div>
        <span class="search-word">${escapeHtml(q.query_text)}</span>
        <span class="hint tiny">
          ${q.prompt_text && q.prompt_text !== q.query_text
            ? `（实际用的查询词：${escapeHtml(q.prompt_text)}）· ` : ''}
          ${state.onlyConfirmed
            ? `展示 <strong>${items.length}</strong> 张我确认过的`
            : `取出 <strong>${items.length}</strong> 个候选拍摄组`}
        </span>
      </div>
      <div class="hint tiny">这不是「最佳照片」—— 请用拍摄时刻判断</div>
    </div>`;

  if (!items.length) {
    grid.innerHTML = state.onlyConfirmed
      ? '<p class="hint">这个词还没有你确认过的照片。取消「只看我确认过的」可以查看全部候选，或到关键词工作台的「发现」里继续筛选。</p>'
      : '<p class="hint">没有找到任何拍摄组。</p>';
    renderPickBar();
    renderContentQueryBar();
    return;
  }

  // 网格里可能还留着上一次的提示块（"查询中…" / "没有找到任何拍摄组"），先清掉
  grid.querySelectorAll(':scope > p, :scope > .wb-empty').forEach((node) => node.remove());

  // 「点文件名 = 看原片」在这里接一次就够：委托挂在网格上，
  // 卡片进出（认领节点 / 复用 / 移除）都不用再管。重复调用无副作用。
  bindOpenAsset(grid);

  const existing = new Map();
  grid.querySelectorAll('[data-shot]').forEach((node) => existing.set(node.dataset.shot, node));

  const seen = new Set();
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const key = String(item.shot_id);
    seen.add(key);
    const node = existing.get(key) || buildCard(item);
    updateCard(node, item);
    fragment.appendChild(node);      // 老节点是"搬家"：<img> 不重新加载，也不会闪
  }
  for (const [key, node] of existing) if (!seen.has(key)) node.remove();
  grid.appendChild(fragment);
  syncSelection();
  renderContentQueryBar();
}

function buildCard(item) {
  const node = document.createElement('figure');
  node.className = 'search-card';
  node.dataset.shot = String(item.shot_id);
  node.innerHTML = `
    <div class="sc-img">
      <img ${assetImgAttrs({ assetId: item.asset_id })}>
      <button class="sc-pick" type="button" aria-pressed="false">
        <span class="sc-tick" aria-hidden="true"></span>
      </button>
    </div>
    <figcaption>
      <div class="search-when"><span class="sc-when"></span>
        <span class="search-hour"></span></div>
      <div class="search-meta">
        ${badgeHtml()}
        <span class="hint tiny sc-count" hidden></span>
        <span class="sc-mark" hidden></span>
      </div>
      <div class="sc-actions">
        <button class="btn small" data-fb="keep" title="我要这种：以后多找类似的">我要这种</button>
        <button class="btn small" data-fb="drop" title="不是这种：以后少出现">不是这种</button>
      </div>
      ${fileBlock({ assetId: item.asset_id, path: item.relative_path, act: false })}
      <div class="search-rank"></div>
    </figcaption>`;
  node.querySelector('.sc-pick').addEventListener('click', (event) => {
    togglePick(Number(node.dataset.shot), { range: event.shiftKey });
  });
  // 卡片上的文件名＝这张结果的原片入口。点击由网格上的**事件委托**统一处理
  // （见 `renderResults` 里的 `bindOpenAsset`）—— 卡片是"认领节点、只改会变的字"
  // 渲染的，逐个绑定会在节点复用后重复挂。
  // ⚠️ 卡片上的按钮**只对这一张**生效（选中的那些请用底下那条操作条）。
  // 让卡片按钮"顺手"作用到整个选择集看着聪明，实际会让用户无法只改一张。
  node.querySelectorAll('[data-fb]').forEach((button) => {
    button.addEventListener('click', () => {
      sendFeedback([Number(node.dataset.shot)], button.dataset.fb);
    });
  });
  return node;
}

/**
 * 拍摄时刻拆成"日期 + 时间"两段 —— 实现已提到 `cards.js`。
 *
 * 卡片上时间那一段是**金色加粗**的（判断日出日落靠它），
 * 而 `formatWhen` 已经把时间包含进去了 —— 直接整串渲染会出现
 * 「2026-05-31 23:44 23:44」。所以拆开，各写一次。
 * 留这段指针是为了让后来者知道这两个函数**去哪了**，别在这里重新定义。
 */

/** 只改会变的那几个字与状态类 —— 重建 innerHTML 会把 `<img>` 一起换掉并重新解码。 */
function updateCard(node, item) {
  const thumb = assetImageUrl(item.asset_id);
  const img = node.querySelector('img');
  if (img && img.getAttribute('src') !== thumb) img.setAttribute('src', thumb);

  node.querySelector('.sc-when').textContent = dayPart(item.capture_at);
  node.querySelector('.search-hour').textContent = hourPart(item.capture_at);

  updateBadge(node, item.selection_status);

  const count = node.querySelector('.sc-count');
  count.hidden = !(item.asset_count > 1);
  count.textContent = `这一组 ${item.asset_count} 张`;

  // 原片入口：文件夹 + 文件名。`asset_id` 记在按钮上，点击由网格的事件委托处理。
  const path = String(item.relative_path || '');
  const dir = node.querySelector('.card-dir');
  if (dir) dir.title = path ? `完整路径：${path}` : '这张照片在库里没有路径记录';
  const open = node.querySelector('.card-open');
  if (open) {
    open.dataset.asset = String(item.asset_id || '');
    open.querySelector('.card-name').textContent = baseName(path) || `#${item.asset_id}`;
    // 结果是按"拍摄组"给的，这里指的是这一组的**代表照片** —— 文案里如实写明，
    // 免得用户以为点开的是整组。
    open.title = [
      item.asset_count > 1 ? `看这一组的代表原片（这一组共 ${item.asset_count} 张）` : '看这张原片',
      path ? `完整路径：${path}` : '',
    ].filter(Boolean).join('\n');
  }

  const mark = item.feedback || null;
  node.classList.toggle('is-keep', mark === 'keep');
  node.classList.toggle('is-drop', mark === 'drop');
  const markNode = node.querySelector('.sc-mark');
  markNode.hidden = !mark;
  markNode.textContent = mark === 'keep' ? '我要这种' : '不是这种';

  node.querySelector('[data-fb="keep"]').classList.toggle('gold', mark === 'keep');
  node.querySelector('[data-fb="drop"]').classList.toggle('danger', mark === 'drop');

  const rank = node.querySelector('.search-rank');
  rank.textContent = `#${item.rank} · 相似度 ${item.score.toFixed(3)}`;
  // 原始相似度只放在悬停说明里：它是核对用的内部数字，不是给人比较优劣的第二把尺。
  rank.title = item.base_score !== undefined && item.base_score !== null
    ? `这只用于解释排序：重排前相似度 ${Number(item.base_score).toFixed(3)}`
    : '用于解释排序，不代表照片好坏';
}

// --------------------------------------------------------------------- 条件条
//
// 这一页有两种找法，各自的条件形状不同，所以条件条也跟着换：
//   · 按画面内容 —— 一个搜索词 + "是否按我的修改重排"
//   · 按时间/地点/场次 —— 城市 / 场次 / 起止 / 最少拍摄组
// 共同点：**都只是已有 state 的只读投影**，不引入新状态。

const FIND_FIELDS = {
  city: '#find-city', session: '#find-session', from: '#find-from', to: '#find-to',
  minShots: '#find-min-shots',
};

/** 撤掉结构化查询里的一个条件并重查。 */
function dropFind(key) {
  if (!state.find) return;
  state.find[key] = '';
  const node = $(FIND_FIELDS[key]);
  if (node) node.value = '';
  runFind();
}

function renderFindQueryBar() {
  const chips = findChips();
  renderQueryBar($('#search-query'), {
    compute: () => chips,
    totalLabel: '符合',
    clearLabel: '清空条件',
    // ⚠️ 没有条件时**不给**「清空条件」按钮 —— 这样整条会收起（与照片库一致）。
    // 一条空条上孤零零挂个"清空条件"既没用又费解：没有条件可清。
    // 表单自己本来就能清（把值删掉即可），不缺这个出口。
    onClearAll: chips.length
      ? () => {
        Object.values(FIND_FIELDS).forEach((sel) => { const node = $(sel); if (node) node.value = ''; });
        state.find = null;
        $('#find-results').innerHTML = '';
        $('#find-summary').innerHTML = '';
        renderFindQueryBar();
      }
      : null,
  });
}

/** 「按时间 / 地点 / 场次」那次查询的条件清单（只读投影）。 */
function findChips() {
  const f = state.find;
  if (!f) return [];
  const chips = [];
  if (f.city) chips.push({ key: 'city', label: `地点：${f.city}`, remove: () => dropFind('city') });
  if (f.session) chips.push({ key: 'session', label: `场次：${f.session}`, remove: () => dropFind('session') });
  if (f.from) chips.push({ key: 'from', label: `从 ${f.from}`, remove: () => dropFind('from') });
  if (f.to) chips.push({ key: 'to', label: `到 ${f.to}`, remove: () => dropFind('to') });
  if (f.minShots) chips.push({ key: 'minShots', label: `至少 ${f.minShots} 个拍摄组`, remove: () => dropFind('minShots') });
  return chips;
}

function renderContentQueryBar() {
  renderQueryBar($('#search-query'), {
    compute: () => {
      const q = state.current;
      if (!q) return [];
      const chips = [{ key: 'query', label: `搜索词：${q.query_text}` }];
      // 重排不是"筛掉什么"，但它明显改变你看到的东西 —— 非默认时如实说出来。
      if (state.showTuning) chips.push({ key: 'tune', label: '已按你的修改重排', tone: 'meta' });
      if (state.onlyConfirmed) chips.push({ key: 'confirmed', label: '只看我确认过的', tone: 'meta' });
      return chips;
    },
    total: state.onlyConfirmed
      ? state.items.filter((item) => state.feedback[String(item.shot_id)] === 'keep').length
      : state.items.length,
    totalLabel: state.onlyConfirmed ? '已确认' : '找到',
    clearLabel: '清空搜索',
    onClearAll: () => {
      state.current = null;
      state.items = [];
      state.selected.clear();
      renderResults();
      renderContentQueryBar();
    },
  });
}

/** 切模式时把条件条换成对应的那一套（上一次渲染的签名是另一套的，必须清掉）。 */
function refreshSearchQueryBar() {
  const host = $('#search-query');
  if (state.mode === 'when') renderFindQueryBar();
  else renderContentQueryBar();
  return host;
}

// --------------------------------------------------------------------- 滚动位置

/**
 * 记住"表态前在看哪儿"。
 *
 * 记两样：容器自己的 `scrollTop`，以及网格相对视口的位置。
 * 只记 `scrollTop` 不够 —— 学习栏的文案长度会变（"改两张以上才会重排"→
 * "已按你的修改重排"），网格上方一高一矮，同一 `scrollTop` 就是另一批卡片了。
 */
function captureScroll() {
  const grid = $('#search-results');
  const scroller = scrollerFor(grid);
  return {
    scroller,
    top: scroller ? scroller.scrollTop : 0,
    gridTop: grid ? grid.getBoundingClientRect().top : 0,
  };
}

function restoreScroll(snap) {
  if (!snap || !snap.scroller || !snap.scroller.isConnected) return;
  snap.scroller.scrollTop = snap.top;
  const grid = $('#search-results');
  if (!grid) return;
  const delta = grid.getBoundingClientRect().top - snap.gridTop;
  if (Math.abs(delta) > 0.5) snap.scroller.scrollTop += delta;
}

/** 真正在滚的是哪个元素：不写死 `#view-search`，让布局改了也不会静默失效。 */
function scrollerFor(node) {
  let current = node;
  while (current && current !== document.body && current !== document.documentElement) {
    const overflowY = getComputedStyle(current).overflowY;
    if (overflowY === 'auto' || overflowY === 'scroll') return current;
    current = current.parentElement;
  }
  return document.scrollingElement || document.documentElement;
}

/**
 * 一键准备语义搜索：按依赖顺序装 onnxruntime（若缺）→ 下 CLIP 模型。
 * 进度显示在按钮旁边，用户不需要知道"任务/命令行"这些概念。
 */
async function prepareSemanticSearch(button) {
  const hint = document.querySelector('#search-prepare-hint');
  const say = (text) => { if (hint) hint.textContent = text; };
  button.disabled = true;
  try {
    const snapshot = await API.get('/api/ai/components');
    const byId = new Map(snapshot.components.map((item) => [item.id, item]));
    for (const id of ['onnxruntime-cpu', 'clip-model']) {
      const component = byId.get(id);
      if (!component || component.installed) continue;
      say(`正在准备 ${component.label}（${component.size_mb} MB）…`);
      await API.post(`/api/ai/components/${id}/install`, {});
      for (let i = 0; i < 900; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const now = await API.get('/api/ai/components');
        const row = now.components.find((item) => item.id === id);
        if (row && row.job && row.job.state === 'error') throw new Error(row.job.error || '安装失败');
        if (row && row.installed) break;
      }
    }
    say('模型已就绪，正在准备第一批词…');
    toast('已准备完成');
    setTimeout(() => window.location.reload(), 800);
  } catch (error) {
    say(`准备失败：${error.message}`);
    toast(`准备失败：${error.message}`, true);
    button.disabled = false;
  }
}
