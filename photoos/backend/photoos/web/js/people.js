/**
 * PhotoOS · 分人（人物标签）
 *
 * ## 这一页为什么这样设计（三个取舍，都源于接口的形状）
 *
 * 1. **确认/否决之后，原型是"立刻"重算的**：`POST /api/people/{id}/faces|reject`
 *    在**同一次请求里**就把原型与阈值学好了（服务端只做均值与点积，不需要 GPU、
 *    不需要重启）。所以每张卡片上都有「是他 / 不是他」的单击按钮 —— 不必先勾选、
 *    不必等批量。用户点一下 → 阈值变了 → 「再找找像他的」立刻用新阈值找他。
 *    把确认做成"批量提交"会把这个循环拉长成两步，反而更慢。
 *
 * 2. **模型的话与用户的话必须分区**：`source='auto'` 是**模型的判断**，
 *    `source='user'` 才是**用户确认过的事实**。两者混在一个网格里，
 *    用户会把模型的猜测当成自己认过的 —— 所以分成两块，
 *    模型那块标题直接写「以下是模型的判断，不是你确认的」。
 *
 * 3. **内部判据照实显示，但不做评价**：阈值/余量是机器用来划线的数，
 *    它们**不是给用户打分的**。界面只写"你确认了 N 张、否决了 M 张，判定线 X"，
 *    没有星级、没有百分比准确率、没有"第几名"—— 那是另一回事，
 *    也和这一页的目的（把人认准）无关。
 *
 * ## 库里的活是命令行干的
 *
 * 检测（`people detect`）与算特征（`people embed`）要 `cv2`，只在命令行环境里跑；
 * 这一页只读库里**已经存好的**特征。所以一张脸都没有时，页面必须把**完整命令**
 * 写给用户，而不是留一片空白让人猜是不是坏了。
 */

import { $, $$, API, escapeHtml, formatWhen, loadingHtml, toast } from './shared.js';
// 「文件夹 + 文件名 + 看原片」这一行现在是全应用公共件（改造前只实现在本页）。
// 人脸卡本身仍用 `/api/faces/…` 的**脸**缩略图 —— 它显示的是"一张脸"而不是
// "一张照片"，强合并会得到一个方形比例与 16:9 缩略图互斥的组件。
import { fileBlock } from './cards.js';
import { loadCollections } from './collections.js';
import {
  bindInlineRun, enableActionButtonsWhenReady, inlineRunHtml,
} from './actions.js';

// 界面里要显示的命令行**由服务端给出**（`/api/cli`）：写死路径的话，
// 装到别人的机器上，界面会教用户去执行一个**根本不存在的解释器**。
// 取不到就退回文档里的朴素写法 —— 提示文案不值当把整页弄坏。
let CMD_DETECT = 'photoos people detect --all';
let CMD_EMBED = 'photoos people embed --all';

/** 取一次命令行前缀（内容很少，每个会话一次就够）。 */
export async function loadCliHints() {
  try {
    const payload = await API.get('/api/cli');
    const examples = (payload.examples || {}).people || [];
    if (examples[0]) CMD_DETECT = examples[0];
    if (examples[1]) CMD_EMBED = examples[1];
  } catch {
    /* 保持默认值 */
  }
}

// 一次确认/否决不超过这个数：拆开发是为了**一张脸对不上不至于整批失败**
const CHUNK = 200;

const state = {
  initialized: false,
  q: {},               // 视图里的 DOM 节点，解析一次就够
  people: [],
  stats: {},
  faces: {},
  unassigned: null,
  activeId: null,
  detail: null,
  identity: null,      // `{uuid, name}`：换人时用它判断"是不是同一个人"
  suggest: null,       // 上一次「再找找像他的」的结果；换人或重新确认后作废
  picked: new Set(),   // 勾选中的 face_id（批量操作的目标）
  notPerson: [],       // 我标过「不是人」的脸（界面上要能撤销）
  archived: [],        // 我解散过的候选（界面上要能放回来）
  busy: false,         // 提交中，挡住重复点击
};

// ------------------------------------------------------------------ 渲染小件
/** 一个人显示什么名字：模型提议的人**没有名字**，必须说清楚而不是显示空白。 */
function personTitle(person) {
  return (person && person.name) || '未命名';
}

function coverUrl(person) {
  return person && person.cover_face_id ? `/api/faces/${person.cover_face_id}/thumb?size=112` : '';
}

/**
 * 学到的判据。
 *
 * ★ 唯一的硬规矩：**不说分数**。这些数字是机器用来划线的内部判据，
 * 不是对人的评价 —— 所以只写"你确认了几张、否决了几张、线画在哪"，
 * 并且在下面那行小字里**明说**它不是打分。星级/百分比/名次一律不出现。
 */
function profileHtml(detail) {
  const profile = detail.profile;
  const counts = detail.counts || {};
  if (!profile) {
    return `
      <div class="person-profile">
        <p class="hint tiny">
          <strong>还没有判据。</strong>先在下面任意一张脸上点「是他」——
          点完这一下，这一页就会用它算出判据，之后「再找找像他的」才有依据。
        </p>
      </div>`;
  }
  const threshold = Number(profile.threshold);
  const margin = Number(profile.margin);
  const positive = Number.isFinite(Number(counts.positive)) ? counts.positive : profile.pos_count;
  const negative = Number.isFinite(Number(counts.negative)) ? counts.negative : profile.neg_count;
  // ⚠️ 这句**自己写**，不用服务端的 `profile.summary`：那是给命令行看的一行摘要
  // （形如"原型 4 正 / 1 负 · 阈值 0.363 · 间隔 +0.05"），"原型/正负/间隔"是我们的行话，
  // 摆在这一页只会让人猜。服务端给的 `notes` 是中文说明，放在下面当补充更合适。
  const learned = `你确认了 ${positive} 张、否决了 ${negative} 张`
    + ` —— 模型据此把判定线画在 ${Number.isFinite(threshold) ? threshold.toFixed(3) : '—'}。`;
  const notes = Array.isArray(profile.notes) ? profile.notes : [];
  return `
    <div class="person-profile">
      <p class="person-learned">${escapeHtml(learned)}</p>
      <div class="person-criteria">
        <span class="person-criterion"><b>已确认</b><i>${profile.pos_count}</i><em>张（你说的）</em></span>
        <span class="person-criterion"><b>已否决</b><i>${profile.neg_count}</i><em>张（你说的）</em></span>
        <span class="person-criterion"><b>判定线</b>
          <i>${Number.isFinite(threshold) ? threshold.toFixed(3) : '—'}</i><em>像到什么程度算他</em></span>
        <span class="person-criterion"><b>判定余量</b>
          <i>${Number.isFinite(margin) ? margin.toFixed(3) : '—'}</i>
          <em>你的判断与模型不一致时留的余地</em></span>
      </div>
      <p class="hint tiny">
        这几行是<strong>机器用来划线的内部判据</strong>，不是给你的照片或这个人打的分；
        「判定线」越高，模型越只敢提最像的那几张。它随着你的每一次确认/否决当场变化。
      </p>
      ${notes.length ? '<p class="person-notes-label">模型对自己这次判法的说明（照实列出）：</p>'
        + `<ul class="person-notes">${notes.slice(0, 6)
          .map((note) => `<li>${escapeHtml(note)}</li>`).join('')}</ul>` : ''}
    </div>`;
}

/** 来源标签：模型提议的每一张都必须带，防止被当成"我认过的"。 */
function sourceTag(source) {
  return source === 'user'
    ? '<span class="person-tag is-user">你确认的</span>'
    : '<span class="person-tag is-auto">模型提议</span>';
}

/**
 * 模型确信度：**只有模型提议的脸**才有，而且只用于"我该不该信这一张"。
 *
 * 两个口径必须写清（也写在 title 里）：
 *   * 它是这张脸与**这个人当前原型**的余弦（原型随你的每次确认/否决变化），
 *     不是"当初提议那一刻"的历史快照；
 *   * 它是**给你自己核对用的**，不是评分、不是排序、不是"这张更好"。
 *
 * 数据缺失时**不显示**（而不是显示 0.00）—— 老数据回填之前这里是空白的，
 * 显示 0 会让人以为"模型完全没把握"。
 */
function confidenceTag(face) {
  if (face.source === 'user') return '';
  const raw = face.similarity;
  if (raw === null || raw === undefined || Number.isNaN(Number(raw))) return '';
  const value = Number(raw);
  return `<span class="pf-sim" title="这张脸与这个人当前原型的相似度（余弦）。它只是核对用的一个数，不是评分、也不是在替你选">`
    + `模型确信度 ${value.toFixed(2)}</span>`;
}

/**
 * 原片入口：这张脸长在哪张照片上。
 *
 * 人脸墙上只有一张**裁出来的脸**，没有它就等于"看得出是谁、找不到照片" ——
 * 用户看完必然要做的事是去翻原片（看构图、看场合、看旁边是谁）。
 *
 * 实现已**提升为全应用公共件**（`js/cards.js` 的 `fileBlock()`）：
 * 改造前这段只存在于人物页，于是主题页与「加入主题」弹窗里的文件名是死的文本。
 * 这里保留一个薄封装，只为把"脸"的字段翻译成公共件的参数。
 * 点击仍走本页 `data-act="asset"` 的派发（见 `onActionClick`），
 * 公共件的 `bindOpenAsset` 会**跳过**带 `data-act` 的元素，不会重复打开。
 */
function fileButton(face) {
  if (!face.asset_id) return '';
  // ⚠️ `path` 是**拼进 HTML 属性**的，公共件内部已做转义 ——
  // 路径里出现一个引号就会把属性截断、后面的内容被当成标签解析。
  return fileBlock({ assetId: face.asset_id, path: face.relative_path });
}

function faceCard(face, scope) {
  const checked = state.picked.has(Number(face.face_id));
  return `
    <figure class="person-face is-${escapeHtml(scope)}">
      <span class="pf-pick">
        <input type="checkbox" data-pick="${face.face_id}" ${checked ? 'checked' : ''}
               aria-label="选中这张脸">
      </span>
      <img loading="lazy" alt="" src="/api/faces/${face.face_id}/thumb?size=112">
      <figcaption>
        <span class="pf-when">${escapeHtml(formatWhen(face.capture_at))}</span>
        ${fileButton(face)}
        ${scope === 'suggest'
          ? '<span class="pf-sim" title="这只是模型内部用来排序的数，不是给你打的分">'
            + `像 ${Number(face.similarity || 0).toFixed(3)}</span>`
            + (face.accepted
              ? '<span class="person-tag is-accepted">达到判定线</span>'
              : '<span class="person-tag is-auto">差一点</span>')
          : `${sourceTag(face.source)}${confidenceTag(face)}`}
        <span class="pf-actions">
          <button class="btn small" data-act="confirm" data-face="${face.face_id}">是他</button>
          <button class="btn small" data-act="reject" data-face="${face.face_id}">不是他</button>
          ${scope === 'face'
            ? `<button class="btn small" data-act="cover" data-face="${face.face_id}"
                       title="列表里这个人用这张脸当封面">当封面</button>`
            : ''}
          <button class="btn small quiet" data-act="not-person" data-face="${face.face_id}"
                  title="这张脸压根不是一个人（海报、雕像、屏幕里的脸、反光的团块…）">
            不是人</button>
        </span>
      </figcaption>
    </figure>`;
}

/** 人脸墙格子数放不下时给一句话，不要让用户以为"就这些"。 */
function moreNote(total, shown) {
  return total > shown
    ? `<p class="hint tiny">这里只画了前 ${shown} 张，共 ${total} 张。</p>` : '';
}

function facesHtml(detail) {
  const faces = detail.faces || [];
  const user = faces.filter((f) => f.source === 'user');
  const auto = faces.filter((f) => f.source !== 'user');
  if (!faces.length) {
    return `
      <div class="wb-empty"><span class="fleuron">❦</span>
        <p><b>这个人名下现在一张脸也没有。</b><br>
           常见原因：刚新建的人还没确认过脸，或者他在库里的照片还没有算过人脸特征。</p>
        <p class="hint tiny">
          下面点「再找找像他的」可以让模型先提一批候选（没有人脸特征时需要先在命令行
          执行 <code>photoos people embed --all</code>）。
        </p>
      </div>`;
  }
  return `
    <section class="person-section is-user">
      <h3 class="person-section-head">已确认<span class="person-count">${user.length} 张</span>
        <span class="hint tiny">这些是<strong>你说过他</strong>的照片，是学习判据的唯一依据</span>
      </h3>
      ${bulkRow(detail, 'user')}
      <div class="person-face-wall">
        ${user.map((f) => faceCard(f, 'face')).join('')
          || '<p class="hint tiny">还没有你确认过的脸 —— 模型提议的那一批在下面。</p>'}
      </div>
    </section>

    <section class="person-section is-auto">
      <h3 class="person-section-head">模型提议<span class="person-count">${auto.length} 张</span>
        <span class="hint tiny">以下是模型的判断，不是你确认的</span>
      </h3>
      <p class="hint tiny">
        它按这个人现有的判据挑出来的。逐张点「是他」才会变成你的判断；
        点「不是他」的下次不会再提给他。混进来的非人脸点「不是人」。
      </p>
      ${bulkRow(detail, 'auto')}
      <div class="person-face-wall">
        ${auto.map((f) => faceCard(f, 'auto')).join('')
          || '<p class="hint tiny">模型现在没有新提议 —— 点下面的「再找找像他的」让它找一批。</p>'}
      </div>
    </section>`;
}

/**
 * 批量操作条（两个区共用）。
 *
 * 四个动作解决的是四种不同的判断，**不能合并**：
 *   * 确认选中的 —— "这些都是他"（写正例，参与学习）；
 *   * 否决选中的 —— "这些都不是他"（写负例，下次不再提给他）；
 *   * 选中的不是人 —— "这压根不是人"（海报/雕像/屏幕；从所有人名下摘掉）；
 *   * 迁移选中的 —— "这些其实都是另一个人"（搬到已命名的人名下，两边都重学）。
 * 前两个是**相对这个人**的，后两个是**关于这些脸本身**的 ——
 * 这一点在界面上必须让用户看得出来（文案与提示里都写清楚了作用范围）。
 */
function bulkRow(detail, scope) {
  const person = detail.person || {};
  const autoCount = Number((detail.counts || {}).auto || 0);
  const others = state.people.filter((item) => item.id !== person.id && item.name);
  return `
    <p class="person-bulk">
      <button class="btn small" data-act="bulk-confirm">确认选中的</button>
      <button class="btn small" data-act="bulk-reject">否决选中的</button>
      <button class="btn small quiet" data-act="bulk-not-person"
              title="选中的这些脸压根不是人。它们会从所有人的名下摘掉，并且不再参与聚类与建议">不是人</button>
      ${others.length ? `
        <span class="person-move">
          <select data-move-target aria-label="把选中的脸移到哪个人名下">
            <option value="">移到已命名的人…</option>
            ${others.map((item) => `<option value="${item.id}">`
              + `${escapeHtml(item.name)}（${item.user_faces || 0} 张）</option>`).join('')}
          </select>
          <button class="btn small" data-act="bulk-move">迁移</button>
        </span>` : ''}
      ${scope === 'auto' && autoCount ? `
        <button class="btn small gold" data-act="confirm-all"
                title="把模型提议的这 ${autoCount} 张一次确认为你的判断">一键确认全部 ${autoCount} 张</button>` : ''}
      <span class="hint tiny">先勾选缩略图左上角的方框，再点这里批量处理</span>
    </p>`;
}

/**
 * 模型的新建议区。
 *
 * `ready=false` 表示这个人**还没有原型**（一张都没确认过），此时服务端只给一句
 * 中文说明 —— 必须把这句话显示出来，不能让区域空着让人以为按钮坏了。
 */
function suggestHtml() {
  const data = state.suggest;
  if (!data) {
    return `
      <p class="hint tiny">
        点「再找找像他的」，模型会拿当前的判据去<strong>还没有归属的脸</strong>里找一批最像的。
        找出来的每一条都要你点一下才算数。
      </p>`;
  }
  if (!data.ready) {
    return `<p class="hint"><strong>还找不了：</strong>${escapeHtml(data.hint || '这个人还没有判据。')}</p>`;
  }
  const items = data.items || [];
  const accepted = items.filter((item) => item.accepted).length;
  const head = `
    <p class="hint tiny person-suggest-note">
      在<strong>${data.considered || 0}</strong> 张还没有归属的脸里找的，
      其中 <strong>${accepted}</strong> 张达到判定线
      （判定线 ${Number(data.threshold || 0).toFixed(3)}）。
      下面是它认为最像的一批 —— <strong>这些仍然是它的判断</strong>，你点过才算你的。
    </p>`;
  if (!items.length) {
    return head + `
      <div class="wb-empty"><span class="fleuron">❦</span>
        <p><b>这次没有找到像他的新脸。</b><br>
           ${data.considered ? '未归类的人脸里没有比上面这些更像的了。' : '现在没有"还没有归属"的人脸可以找。'}</p>
        <p class="hint tiny">
          再确认几张（或者去命令行跑 <code>photoos people discover --all</code> 找出新的人物候选）
          之后再回来点一次。
        </p>
      </div>`;
  }
  // ★ 建议区也要有批量动作：60 张一张张点是这一块最烦的地方。
  // 两个"一键确认"给的是**两种不同的取舍**，不是一个按钮的两个说法：
  //   达到判定线的 = 模型有把握的；全部 = 连"差一点"的也认。
  const borderline = items.length - accepted;
  return head + `
    <p class="person-bulk">
      <button class="btn small" data-act="bulk-confirm">确认选中的</button>
      <button class="btn small" data-act="bulk-reject">否决选中的</button>
      <button class="btn small quiet" data-act="bulk-not-person"
              title="选中的这些脸压根不是人">不是人</button>
      ${accepted ? `<button class="btn small gold" data-act="suggest-confirm"
              data-scope="accepted"
              title="把达到判定线的这些一次认成你的判断">一键确认达到判定线的 ${accepted} 张</button>` : ''}
      ${borderline ? `<button class="btn small" data-act="suggest-confirm" data-scope="all"
              title="连「差一点」的那些也一起认下来（${borderline} 张没到判定线）">
              全部 ${items.length} 张都要</button>` : ''}
      <button class="btn small quiet" data-act="suggest-reject-rest"
              title="把列表里剩下的这些一次标成「不是他」——挑完之后收尾用">
        剩下的 ${items.length} 张全部不是他</button>
      <span class="hint tiny">先勾选缩略图左上角的方框，再点这里批量处理</span>
    </p>
    <div class="person-face-wall">
      ${items.map((item) => faceCard(item, 'suggest')).join('')}
    </div>`;
}

/** 被否决的脸：默认折叠。它们仍然有用（"这张不是他"本身也是学习依据）。 */
function rejectedHtml(detail) {
  const rejected = detail.rejected || [];
  if (!rejected.length) return '';
  return `
    <details class="person-rejected">
      <summary>你否决过的（${rejected.length} 张）</summary>
      <p class="hint tiny">
        这些是你说过「不是他」的。它们仍然在学：模型下次不会再把他提给这个人。
        点「其实是他」可以撤销这次否决 —— 撤销之后模型会**立刻**按新的判断重算。
      </p>
      <div class="person-face-wall">
        ${rejected.map((face) => `
          <figure class="person-face is-rejected">
            <img loading="lazy" alt="" src="/api/faces/${face.face_id}/thumb?size=112">
            <figcaption>
              <span class="pf-when">${escapeHtml(formatWhen(face.capture_at))}</span>
              ${fileButton(face)}
              <span class="person-tag is-rejected">已否决</span>
              <span class="pf-actions">
                <button class="btn small" data-act="unreject" data-face="${face.face_id}">其实是他</button>
                <button class="btn small quiet" data-act="not-person" data-face="${face.face_id}"
                        title="这张脸压根不是一个人">不是人</button>
              </span>
            </figcaption>
          </figure>`).join('')}
      </div>
      ${moreNote(Number(detail.counts && detail.counts.negative), rejected.length)}
    </details>`;
}

function detailHtml(detail) {
  const person = detail.person || {};
  const counts = detail.counts || {};
  const cover = coverUrl(person);
  return `
    <div class="person-head">
      <span class="person-head-cover">${cover
        ? `<img alt="" src="${cover}">`
        : '<span class="collection-cover-empty">无封面</span>'}</span>
      <span class="person-head-line">
        <span class="person-head-name">${escapeHtml(personTitle(person))}</span>
        <span class="hint tiny">
          #${person.id} ·
          已确认 <strong>${counts.positive || 0}</strong> 张 ·
          模型提议 <strong>${counts.auto || 0}</strong> 张 ·
          否决 <strong>${counts.negative || 0}</strong> 张 ·
          覆盖 <strong>${(detail.shots || []).length}</strong> 张照片
        </span>
        ${person.name ? '' : '<span class="hint tiny">模型只圈出了"可能是同一个人"，<strong>名字要你来起</strong>。</span>'}
      </span>
      <span class="person-head-actions">
        <button class="btn small" data-act="rename">改名</button>
        <button class="btn primary" data-act="collection">加入主题</button>
        <button class="btn small" data-act="pack"
                title="把这个人的原片打成一个 zip（复制到数据目录的 exports/，不动原片）">打包原片</button>
        ${person.name ? '' : `<button class="btn small" data-act="dissolve"
          title="这一簇不算数（有的候选里啥都有）。脸一张都不会删，
                 只是不再把它们当成一个人；之后可以用「不是人」把混进来的非人脸永久排除">解散候选</button>`}
        <button class="btn danger small" data-act="delete">删除这个人（只删标签，不删照片）</button>
      </span>
    </div>

    <section class="person-block" data-person-profile>
      <h3 class="person-section-head">模型学到了什么</h3>
      ${profileHtml(detail)}
    </section>

    <section class="person-block">
      <h3 class="person-section-head">再找找像他的</h3>
      <p class="person-bulk">
        <button class="btn" data-act="suggest">再找找像他的</button>
        <span class="hint tiny">
          每次确认或否决之后，判据<strong>当场就更新了</strong> —— 所以可以一路
          「点几张 → 再找一次 → 再点几张」。
        </span>
      </p>
      <!-- ⚠️ data-person-suggest 必须画在**这段会被重画掉的 HTML 里**。
           上一版把它当成"初始化时挂上去的锚点"（见 fillBlocks），
           而整块右栏是 innerHTML 重画的 —— 锚点第一次重画就没了，
           于是「再找找像他的」请求成功、toast 也报了"找到 60 张候选"，
           **列表却画在一个已经脱离文档的节点上，屏幕上什么都不出现**。
           （注意：这段注释在模板字符串里，**不能**出现反引号。） -->
      <div class="person-suggest" data-person-suggest>${suggestHtml()}</div>
    </section>

    <section class="person-block" data-person-faces>${facesHtml(detail)}</section>

    <div data-person-rejected>${rejectedHtml(detail)}</div>

    <p class="hint tiny">
      「重新加载」会丢掉当前没提交的勾选与建议列表，重新问一次服务端。
      <button class="btn small" data-act="reload">重新加载</button>
    </p>`;
}

// ------------------------------------------------------------------ 渲染
function renderList() {
  const node = state.q.list;
  if (!node) return;
  const stats = state.stats || {};
  const faces = state.faces || {};
  const unassigned = state.unassigned;

  // 一条都还没有时，列表里也要写清"下一步做什么"，而不是"没有数据"四个字
  const emptyHint = !state.people.length
    ? `<p class="hint tiny">还没有人物候选。人脸检测需要「OpenCV + 人脸模型」这两个组件。
             <button class="linkish" type="button"
                     onclick="window.photoosShowAi && window.photoosShowAi()">去 AI 组件准备</button></p>`
    : '';

  // 顶部条：只陈述事实，不做排序/推荐
  const chips = [
    ['人物', stats.persons],
    ['已命名', stats.named],
    ['待命名', stats.proposed],
    ['确认过的脸', stats.confirmed_faces],
    ['模型提议的脸', stats.auto_faces],
    ['否决过的脸', stats.rejected_faces],
  ];
  const coverage = Number(faces.faces)
    ? `已算出特征 <strong>${faces.embedded || 0}</strong> / 检出的 ${faces.faces} 张脸`
      + `（其中通过"正面清晰"判据的 ${faces.frontal || 0} 张）`
    : '还没有检出任何人脸';
  // 顶部条：只陈述事实，不做排序/推荐（容器本身由视图提供，这里只填内容）
  node.innerHTML = `
    <div class="person-stat-chips">
      ${chips.map(([label, value]) => `<span class="person-stat-chip">${label}
        <b>${Number(value) || 0}</b></span>`).join('')}
    </div>
    <p class="hint tiny">
      ${coverage}${unassigned
        ? ` · 还有 <strong>${unassigned}</strong> 张脸没有归到任何人名下` : ''}
    </p>
    ${emptyHint}
    <div class="people-cards">
      ${state.people.map((item) => {
        const cover = coverUrl(item);
        const unnamed = !item.name;
        return `
        <button class="person-card ${item.id === state.activeId ? 'is-active' : ''}"
                data-person="${item.id}">
          <span class="person-cover">${cover
            ? `<img loading="lazy" alt="" src="${cover}">`
            : '<span class="collection-cover-empty">无封面</span>'}</span>
          <span class="person-card-meta">
            <span class="person-card-name">${escapeHtml(personTitle(item))}</span>
            <span class="person-card-sub">
              你确认过 <strong>${item.user_faces || 0}</strong> 张 ·
              模型提议 <strong>${item.auto_faces || 0}</strong> 张
            </span>
            <span class="person-card-sub">
              覆盖 <strong>${item.shots || 0}</strong> 张照片
              ${item.rejects ? ` · 否决 ${item.rejects}` : ''}
            </span>
          </span>
          <span class="person-card-tags">
            ${unnamed ? '<span class="person-tag is-unnamed">未命名</span>' : ''}
          </span>
        </button>`;
      }).join('')}
    </div>
    ${state.notPerson.length ? notPersonHtml() : ''}
    ${(state.archived || []).length ? archivedHtml() : ''}`;
}

/**
 * 「不是人」那一栏 —— 它必须有一个**看得见的回头路**。
 *
 * 判定一张脸"不是人"只需要一次点击，误判也只需要一次点击；
 * 而撤销如果只能靠 SQL，那这个功能就不敢用。这里把标过的都列出来（带原片路径，
 * 撤销之前能先看一眼那张照片），点一下就能放回去。
 */
function notPersonHtml() {
  const items = state.notPerson;
  return `
    <details class="person-rejected person-side-block">
      <summary>你标过「不是人」的（${items.length} 张）</summary>
      <p class="hint tiny">
        这些脸不是人（海报、雕像、屏幕里的脸、反光的团块…）。
        它们不再参与聚类，也不会被提给任何人 —— 但照片与检出的脸都还在。
        点「这是人脸」可以撤销这个判断。
      </p>
      <div class="person-face-wall">
        ${items.map((face) => `
          <figure class="person-face is-rejected">
            <img loading="lazy" alt="" src="/api/faces/${face.face_id}/thumb?size=112">
            <figcaption>
              <span class="pf-when">${escapeHtml(formatWhen(face.capture_at))}</span>
              ${fileButton(face)}
              <span class="person-tag is-rejected">不是人</span>
              <span class="pf-actions">
                <button class="btn small" data-act="person-again" data-face="${face.face_id}">
                  这是人脸</button>
              </span>
            </figcaption>
          </figure>`).join('')}
      </div>
    </details>`;
}

/** 解散过的候选：同样给一条回头路（误点一次不该丢掉一簇）。 */
function archivedHtml() {
  const items = state.archived || [];
  return `
    <details class="person-rejected person-side-block">
      <summary>你解散过的候选（${items.length} 个）</summary>
      <p class="hint tiny">
        这些候选的结论被你打散了 —— 脸一张没删，只是不再把它们当成一个人，
        也不会再被聚成同一坨。点「放回列表」可以撤销。
      </p>
      <ul class="person-archived-list">
        ${items.map((item) => `
          <li>
            <span class="person-archived-cover">${item.cover_face_id
              ? `<img loading="lazy" alt=""
                      src="/api/faces/${item.cover_face_id}/thumb?size=56">`
              : '<span class="collection-cover-empty">无封面</span>'}</span>
            <span class="hint tiny">#${item.id} · ${item.faces || 0} 张脸</span>
            <button class="btn small" data-act="restore-person"
                    data-person-id="${item.id}">放回列表</button>
          </li>`).join('')}
      </ul>
    </details>`;
}

function renderStats() {
  const node = state.q.stats;
  if (!node) return;
  const faces = state.faces || {};
  const stats = state.stats || {};
  if (!Number(faces.faces)) {
    node.textContent = '库里还没有人脸数据';
  } else {
    node.textContent = `${stats.persons || 0} 个人物候选 · 检出 ${faces.faces} 张脸 · `
      + `${state.unassigned || 0} 张还没有归属`;
  }
}

/** 库里一张脸都没有（或还没算特征）时：给按钮直跑，命令行作为备选收起来。
 *
 * 以前这里只贴一条命令让用户自己复制去终端；现在同一句话后面跟一个按钮，
 * 走后台任务、有进度、可暂停。命令原文保留在可折叠区里（想自己动手的人仍能照抄，
 * 也方便复核"点这个按钮到底会跑什么"）。
 */
function emptyBoxHtml() {
  const faces = state.faces || {};
  if (!Number(faces.faces)) {
    return `
      <div class="wb-empty">
        <p><strong>库里还没有检出任何人脸。</strong></p>
        <p class="hint">
          人脸检测要完整跑一遍全库照片。点下面这个按钮交给后台 ——
          它只读照片、在数据库里记下人脸位置，<strong>不会改动或删除任何照片文件</strong>。
        </p>
        ${inlineRunHtml('people_detect', '开始检测人脸')}
        <details class="not-ready-manual">
          <summary>我想自己在命令行跑</summary>
          <pre class="people-cmd">${escapeHtml(CMD_DETECT)}</pre>
        </details>
        <p class="hint tiny">跑完回到这里按 <code>Ctrl+F5</code> 刷新一次。</p>
      </div>`;
  }
  if (!Number(faces.embedded)) {
    return `
      <div class="wb-empty">
        <p><strong>检出 ${faces.faces} 张脸，但还没有算过特征。</strong></p>
        <p class="hint">
          没有特征就没法比"像不像"，也就分不出谁是谁。
          这一步同样不动任何照片文件。
        </p>
        ${inlineRunHtml('people_embed', '开始算人脸特征')}
        <details class="not-ready-manual">
          <summary>我想自己在命令行跑</summary>
          <pre class="people-cmd">${escapeHtml(CMD_EMBED)}</pre>
        </details>
        <p class="hint tiny">
          要算的是 ${faces.faces} 张脸（可能几千张，跑几分钟到十几分钟）。
        </p>
      </div>`;
  }
  if (state.people.length && !Number((state.stats || {}).named)) {
    return `
      <div class="wb-empty">
        <p><strong>有 ${state.people.length} 个候选，但一个人都还没命名。</strong></p>
        <p class="hint">
          左边每一张卡片就是模型圈出的一个"可能是同一个人"。
          点开任意一个、看一眼里面的人脸：
          <strong>是他</strong>就点「是他」，<strong>不是</strong>就点「不是他」，
          确认几张之后点「改名」给他起个名字。
        </p>
        <p class="hint tiny">命名之后，点「加入主题」就能把这些照片收进以他名字命名的主题里。</p>
      </div>`;
  }
  return '';
}

function renderDetail() {
  const node = state.q.detail;
  if (!node) return;
  if (!state.detail) {
    node.innerHTML = '<div class="hint">从左边选一个人，看他名下的脸。</div>';
    return;
  }
  node.innerHTML = detailHtml(state.detail);
  // ⚠️ 节点全是新画的：必须把引用**指回新节点**。
  // 否则后面那些"只换这一块"的局部刷新会写进早已脱离文档的旧节点 ——
  // 请求都成功、界面纹丝不动，是最难查的一类故障（`suggestNode()` 就是踩过这个坑）。
  fillBlocks();
  syncSelection();
}

// ------------------------------------------------------------------ 数据
async function loadList() {
  try {
    const payload = await API.get('/api/people');
    // 已命名的人排前面，同组按张数多的优先：未命名候选是"待处理"，不该压住已经认过的人
    const rank = (item) => (item.name ? 0 : 1);
    state.people = (payload.items || []).slice().sort((a, b) => (
      rank(a) - rank(b)
      || (b.user_faces || 0) - (a.user_faces || 0)
      || (b.auto_faces || 0) - (a.auto_faces || 0)
      || (a.id || 0) - (b.id || 0)));
    state.stats = payload.stats || {};
    state.faces = payload.faces || {};
    state.unassigned = payload.unassigned;
    state.archived = payload.archived || [];
    try {
      // 「不是人」那一栏要能撤销，所以每次刷新列表都取一次（最多 200 条，很小）
      const marked = await API.get('/api/faces/not-person?limit=200');
      state.notPerson = marked.items || [];
    } catch {
      state.notPerson = [];    // 拿不到就不显示那一栏，不要因此把整页弄坏
    }
    if (state.activeId && !state.people.some((item) => item.id === state.activeId)) {
      state.activeId = null;
      state.detail = null;
      state.identity = null;
    }
    renderList();
    renderStats();
  } catch (error) {
    toast(`人物列表加载失败：${error.message}`, true);
  }
}

function clearTransient() {
  // 换了人（或重新加载）之后，旧的勾选与旧的建议都失去意义
  state.picked.clear();
  state.suggest = null;
}

async function loadDetail(personId) {
  try {
    const payload = await API.get(`/api/people/${personId}?limit=200`);
    state.activeId = personId;
    state.detail = payload;
    state.identity = {
      uuid: payload.person && payload.person.uuid,
      name: payload.person && payload.person.name,
    };
    clearTransient();
    renderList();
    renderDetail();
  } catch (error) {
    if (error.status === 404) {
      // 列表点了别人已经删掉/合并掉的人 —— 静默重取一次，不留死页面
      toast('这个人已经不在了，列表已重新加载', true);
      state.activeId = null;
      state.detail = null;
      state.identity = null;
      await loadList();
      renderDetail();
      return;
    }
    toast(`这个人加载失败：${error.message}`, true);
  }
}

// ------------------------------------------------------------------ 动作
async function createPerson() {
  const name = prompt('名字（可留空，之后可以再起名）');
  if (name === null) return;
  try {
    const created = await API.post('/api/people', { name: name.trim() });
    await loadList();
    await loadDetail(created.id);
    toast(created.name ? `已新建「${created.name}」` : '已新建一个人（还没名字，随时可以改）');
  } catch (error) {
    toast(`新建失败：${error.message}`, true);
  }
}

/**
 * 「重新学习全部」：把所有**有标注的人**的原型与阈值重算一遍。
 *
 * 它等价于 `photoos people learn`（同一个 `fit_profile`，`POST /api/people/learn`）。
 * 为什么还需要这个按钮：每次点「是他 / 不是他」服务端确实当场重算了那个人，
 * 但**阈值用的是留一法分布**（把每张正例排除后重算原型再打分），一个人攒得越多，
 * 早先那个阈值就越偏保守。这个按钮是一次性的兜底重算。
 *
 * 为什么先禁用：重算要读这个人的全部特征向量，人多时会明显花时间；
 * 连点两下就是两次全库重算 —— 用户不该为这个买单（与「是他」按钮同一处理）。
 */
async function learnAll(button) {
  if (button) button.disabled = true;
  try {
    const result = await API.post('/api/people/learn');
    const updated = Number(result.updated) || 0;
    const failed = (result.details || []).filter((item) => item.error);
    if (updated === 0 && !failed.length) {
      toast('还没有可重算的人：先确认或否决几张脸');
    } else if (failed.length) {
      toast(`已重算 ${updated} 个人，${failed.length} 个失败：${failed[0].error}`, true);
    } else {
      toast(`已重算 ${updated} 个人的判据`);
    }
    await loadList();
    if (state.activeId) await loadDetail(state.activeId);
    else renderList();
  } catch (error) {
    toast(`重算失败：${error.message}`, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function renamePerson() {
  const person = state.detail && state.detail.person;
  if (!person) return;
  const name = prompt('新名字（留空 = 取消命名）', person.name || '');
  if (name === null) return;
  try {
    const payload = await API.patch(`/api/people/${person.id}`, { name: name.trim() });
    const updated = payload.person || {};
    toast(updated.name ? `已改名为「${updated.name}」` : '已取消这个名字（他回到未命名）');
    await loadList();
    // 改名只影响名字与状态，不必重取整个详情
    if (state.detail) state.detail.person = { ...state.detail.person, ...updated };
    renderList();
    renderDetail();
  } catch (error) {
    toast(`改名失败：${error.message}`, true);
  }
}

/**
 * 「是他」：一次请求，服务端当场重算原型。
 * 提交前先把按钮停掉 —— 用户连点两下就是两张脸被确认两次，他不该为此买单。
 */
async function confirmFaces(faceIds, { scope = 'face', thenRefresh = true } = {}) {
  const ids = [...new Set((faceIds || []).map(Number).filter(Number.isFinite))];
  const person = state.detail && state.detail.person;
  if (!person) {
    toast('先选一个人', true);
    return;
  }
  if (!ids.length) {
    toast('先勾选几张脸（或直接点某一张上的「是他」）', true);
    return;
  }
  if (state.busy) return;
  state.busy = true;
  try {
    let changed = 0;
    let profile = null;
    let failed = 0;
    for (let start = 0; start < ids.length; start += CHUNK) {
      const chunk = ids.slice(start, start + CHUNK);
      try {
        const payload = await API.post(`/api/people/${person.id}/faces`, { face_ids: chunk });
        changed += Number(payload.changed) || 0;
        if (payload.profile) profile = payload.profile;
      } catch (error) {
        failed += chunk.length;
        toast(`这一批确认失败：${error.message}`, true);
      }
    }
    if (!changed && failed) return;
    ids.forEach((id) => state.picked.delete(id));
    if (profile && state.detail) state.detail.profile = profile;
    if (scope === 'suggest') {
      // 已经在候选列表里的，立刻拿掉 —— 别让用户以为没生效
      dropFromSuggest(ids);
    }
    toast(`已确认 ${changed} 张。判据已按你的判断重算 —— 点「再找找像他的」看看有没有新的`);
    if (thenRefresh) await afterDecision();
  } finally {
    state.busy = false;
  }
}

/** 「不是他」：同样当场重算。被否决的脸下次不会再被提给这个人。 */
async function rejectFaces(faceIds, { scope = 'face', thenRefresh = true } = {}) {
  const ids = [...new Set((faceIds || []).map(Number).filter(Number.isFinite))];
  const person = state.detail && state.detail.person;
  if (!person) {
    toast('先选一个人', true);
    return;
  }
  if (!ids.length) {
    toast('先勾选几张脸（或直接点某一张上的「不是他」）', true);
    return;
  }
  if (state.busy) return;
  state.busy = true;
  try {
    let changed = 0;
    let profile = null;
    let failed = 0;
    for (let start = 0; start < ids.length; start += CHUNK) {
      const chunk = ids.slice(start, start + CHUNK);
      try {
        const payload = await API.post(`/api/people/${person.id}/reject`, { face_ids: chunk });
        changed += Number(payload.changed) || 0;
        if (payload.profile) profile = payload.profile;
      } catch (error) {
        failed += chunk.length;
        toast(`这一批否决失败：${error.message}`, true);
      }
    }
    if (!changed && failed) return;
    ids.forEach((id) => state.picked.delete(id));
    if (profile && state.detail) state.detail.profile = profile;
    if (scope === 'suggest') {
      // 已经在候选列表里的，立刻拿掉 —— 别让用户以为没生效
      dropFromSuggest(ids);
    }

    toast(`已否决 ${changed} 张。模型下次不会再把这些脸提给他`);
    if (thenRefresh) await afterDecision();
  } finally {
    state.busy = false;
  }
}

/**
 * 一次决定之后的收尾。
 *
 * 只在**第一次**决定时整块重画：从这一刻起，"身份的证明"就只是 `uuid` 了 ——
 * 按名字重画的话，用户在未命名的人上确认第一张脸之后，页面会突然跳回"请选一个人"
 * （未命名 → 命名是服务端另一条路径的事，界面不该把决定绑在名字上）。
 */
async function afterDecision() {
  if (!state.activeId) return;
  try {
    const payload = await API.get(`/api/people/${state.activeId}?limit=200`);
    const identity = payload.person || {};
    const firstTime = !state.identity || identity.uuid !== state.identity.uuid;
    state.detail = payload;
    state.identity = { uuid: identity.uuid, name: identity.name };
    // 第一次决定时列表顺序会重排（命名过的排前面），必须重取；
    // 顺手清掉勾选 —— 刚确认过的那几张不该还勾着
    if (firstTime) state.picked.clear();
    await loadList();
    // 一次决定就把用户刚找出来的建议列表冲掉，等于让他白点一次 —— 所以先留一份
    const previousSuggest = state.suggest;
    renderDetail();
    // ⚠️ 锚点要在**重画之后**再取：`renderDetail()` 换掉的是整栏 HTML，
    // 重画前取到的是马上要脱落的旧节点，往那里写等于什么都没做。
    const blocks = {
      profile: state.q.profile,
      faces: state.q.faces,
      rejected: state.q.rejected,
      suggest: suggestNode(),
    };
    if (blocks.profile && payload.profile) blocks.profile.innerHTML = profileHtml(payload);
    if (blocks.faces) blocks.faces.innerHTML = facesHtml(payload);
    if (blocks.rejected) blocks.rejected.innerHTML = rejectedHtml(payload);
    if (blocks.suggest && previousSuggest) {
      const note = blocks.suggest.querySelector('.person-suggest-note');
      if (note) note.textContent = '上面这些是上一次找的结果（新的判据已经在用了，可以再找一次）';
    }
  } catch (error) {
    toast(`重新读取这个人失败：${error.message}`, true);
  }
}

async function setCover(faceId) {
  const person = state.detail && state.detail.person;
  if (!person) return;
  try {
    await API.patch(`/api/people/${person.id}`, { cover_face_id: Number(faceId) });
    await loadList();
    if (state.detail) state.detail.person = { ...state.detail.person, cover_face_id: Number(faceId) };
    renderList();
    renderDetail();
    toast('已把这张脸设为这个人的封面');
  } catch (error) {
    toast(`设置封面失败：${error.message}`, true);
  }
}

/** 「再找找像他的」：用**当前判据**去未归类的人脸里找下一批候选。 */
async function findSimilar() {
  const person = state.detail && state.detail.person;
  if (!person) {
    toast('先选一个人', true);
    return;
  }
  const node = suggestNode();
  if (node) node.innerHTML = loadingHtml('正在找…（拿当前判据去比对还没有归属的脸）');
  try {
    const payload = await API.post(`/api/people/${person.id}/suggest`, { limit: 60 });
    state.suggest = payload;
    const target = suggestNode();
    if (target) target.innerHTML = suggestHtml();
    if (!payload.ready) {
      // 服务端给的中文说明必须显示出来，不能让区块空着
      toast(payload.hint || '这个人还没有判据：先确认至少一张脸', true);
    } else {
      toast(`在 ${payload.considered || 0} 张未归类的人脸里找到 ${(payload.items || []).length} 张候选`);
    }
  } catch (error) {
    const target = suggestNode();
    if (target) target.innerHTML = suggestHtml();
    toast(`查找失败：${error.message}`, true);
  }
}

async function toCollection() {
  const person = state.detail && state.detail.person;
  if (!person) {
    toast('先选一个人', true);
    return;
  }
  if (!person.name) {
    toast('这个人还没有名字：先点「改名」给他起个名字，主题要用它命名', true);
    return;
  }
  try {
    const payload = await API.post(`/api/people/${person.id}/collection`, {});
    if (payload.hint) {
      toast(payload.hint, true);
      return;
    }
    await loadCollections();   // 让「主题」那页立刻能看到它，不用用户手动刷新
    toast(payload.created
      ? `已新建主题「${payload.name}」并放进 ${payload.added} 张（重复的自动跳过）`
      : `已放进主题「${payload.name}」${payload.added} 张（重复的自动跳过）`);
  } catch (error) {
    toast(`加入主题失败：${error.message}`, true);
  }
}

/**
 * 删除这个人。
 *
 * 文案必须先说清"删的是什么"：用户最怕的是点一下照片没了。这里删的只是
 * 数据库里"这个人"这个标签 —— 照片、人脸框、你在别处做的精选都不动。
 * 服务端目前**还没有**这个接口，所以失败时照实说，并给出命令行里等价的合并/清理办法，
 * 不假装删掉了。
 */
async function deletePerson() {
  const person = state.detail && state.detail.person;
  if (!person) return;
  const ok = confirm(
    `删除「${personTitle(person)}」？\n\n`
    + '删除的只是数据库里"这个人"这个标签：他的确认记录、否决记录与学到的判据会一起消失。\n'
    + '照片文件、人脸框、以及你在别处做过的精选，都**不会被删除、也不会被移动**。\n\n'
    + `（他名下现在有 ${(state.detail.counts && state.detail.counts.positive) || 0} 张你确认过的脸、`
    + `${(state.detail.counts && state.detail.counts.auto) || 0} 张模型提议的脸。）`,
  );
  if (!ok) return;
  try {
    // `?confirm=true` 是服务端要求的二次确认（没有它一律 428）——
    // 上面的 confirm() 是给用户看的，这一个参数是给接口看的，两者都要有。
    await API.del(`/api/people/${person.id}?confirm=true`);
    toast('已删除这个人物标签（照片、人脸与精选都没有被动过）');
    state.activeId = null;
    state.detail = null;
    state.identity = null;
    clearTransient();
    await loadList();
    renderDetail();
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

// ------------------------------------------------------------------ 初始化
function pickView() {
  return $('#view-people') || $('.people-layout') || null;
}

function fillQueries(root) {
  const q = {
    stats: root.querySelector('[data-people-stats]') || $('#people-stats'),
    list: root.querySelector('[data-people-list]') || $('#people-list'),
    detail: root.querySelector('[data-people-detail]') || $('#people-detail'),
    empty: root.querySelector('[data-people-empty]') || $('#people-empty'),
    // 「新建人物」的 id 以视图给出的为准，`btn-person-new` 只是别处可能用的另一个写法
    newButton: root.querySelector('#people-new, #btn-person-new') || $('#people-new'),
    // 「重新学习全部」：后端端点一直存在（`POST /api/people/learn`），
    // 但 HTML 里这个按钮此前**没有绑任何事件**（点了没反应），现在按 id 取到它。
    learnAllButton: root.querySelector('#people-learn-all') || $('#people-learn-all'),
  };
  // 三个区域是这一页的骨架：缺一个就没法工作，也**不能只坏一半** ——
  // 标记为不可用，refreshPeople 会在界面上说明情况（toast 一闪而过，说不清）。
  q.usable = Boolean(q.list && q.detail);
  state.q = q;
  fillBlocks();
}

/**
 * 右栏内部的三块锚点是**可选的**：父视图给了就用，没给就在这里建。
 *
 * 为什么要有它们：服务端每次确认/否决都会重算判据，重画时只换这三块的内层 HTML，
 * 就不会碰到"模型的新建议"那一块 —— 否则刷新一次就把用户刚找出来的候选冲掉了。
 * ⚠️ 这三个锚点必须放在**右栏容器内部**（重画的是右栏容器的 innerHTML）。
 */
/**
 * 把三个区块的引用指到**当前右栏里那三个节点**上。
 *
 * 这几个锚点由 `detailHtml` 画出来（`data-person-profile/faces/rejected`），
 * 所以每次整栏重画之后都要重新取一遍 —— 见 `renderDetail` 的说明。
 * 这里不再"没有就建一个"：建出来的节点和画出来的内容会重复一份。
 */
function fillBlocks() {
  const detail = state.q.detail;
  if (!detail) return;
  state.q.profile = detail.querySelector('[data-person-profile]');
  state.q.faces = detail.querySelector('[data-person-faces]');
  state.q.rejected = detail.querySelector('[data-person-rejected]');
}

/** 「模型的新建议」的容器：每次现查，因为它可能被整栏重画换掉过节点。 */
function suggestNode() {
  const detail = state.q.detail;
  return (detail && detail.querySelector('[data-person-suggest]'))
    || (state.q.empty && state.q.empty.querySelector('[data-person-suggest]'))
    || null;
}

/**
 * 勾选状态 → 两个批量按钮的可用性。
 *
 * 每次重画右栏之后都要再调一次（节点是新建的，`disabled` 不会跟着状态走），
 * 否则会出现"一张都没勾，按钮看起来却能点"。
 */
function syncSelection() {
  const count = state.picked.size;
  const box = state.q.detail || document;
  box.querySelectorAll('[data-act="bulk-confirm"], [data-act="bulk-reject"]')
    .forEach((button) => { button.disabled = count === 0; });
}

function togglePick(faceId, checked) {
  if (checked) state.picked.add(Number(faceId));
  else state.picked.delete(Number(faceId));
  syncSelection();
}

/**
 * 这张卡片在**哪个区块**里（已确认 / 模型提议 / 候选建议 / 已否决）。
 *
 * 区块决定两件事：这条动作要不要从候选列表里摘掉它、撤销走哪条路。
 * **必须从 DOM 推**，不能按按钮类型写死 —— 同一个「是他」按钮在四个区块里
 * 语义相同、后果不同（见 `case 'confirm'` 的注释）。
 */
function sectionOf(button) {
  if (!button) return 'face';
  if (button.closest('.person-suggest')) return 'suggest';
  if (button.closest('.person-rejected')) return 'rejected';
  return 'face';
}

/** 某个区里勾选中的脸；一个都没勾就退回到全局勾选（几个区的方框是分开的）。 */
function pickedFaces(button) {  const scope = button
    ? button.closest('.person-section, .person-suggest, .person-rejected') : null;
  const inScope = scope
    ? [...scope.querySelectorAll('[data-pick]:checked')].map((box) => Number(box.dataset.pick))
    : [];
  if (inScope.length) return inScope;
  const detail = $('#people-detail');
  return [...(detail || document).querySelectorAll('[data-pick]:checked')]
    .map((box) => Number(box.dataset.pick));
}

/** ★ 建议区的一键确认：`scope='accepted'` 只认达到判定线的，`'all'` 连差一点的也认。 */
/**
 * ★ 这一批候选：认下挑中的，**把剩下的留着**让你一次否决。
 *
 * ⚠️ 上一版这里是 `state.suggest = null`（整块候选列表作废）—— 用户的反馈很具体：
 * "我选择了该选择的，剩下的没办法一键否认"。确认完之后剩下的那些不该消失，
 * 它们正是接下来要处理的对象。
 */
async function confirmSuggested(scope) {
  const data = state.suggest;
  const person = state.detail && state.detail.person;
  if (!person || !data || !(data.items || []).length) {
    toast('先点「再找找像他的」找一批候选', true);
    return;
  }
  const items = data.items || [];
  const targets = scope === 'all' ? items : items.filter((item) => item.accepted);
  if (!targets.length) {
    toast('这一批里没有达到判定线的：可以勾选后点「确认选中的」逐张认', true);
    return;
  }
  const borderline = items.length - targets.length;
  const ok = confirm(
    `把 ${targets.length} 张候选确认为「${personTitle(person)}」？\n\n`
    + '确认之后它们就算你的判断，会拿去重算这个人的判据。\n'
    + (borderline
      ? `剩下 ${borderline} 张会留在列表里 —— 可以逐张看，也可以一次「剩下的全部不是他」。`
      : '这一批全部达到判定线。'));
  if (!ok) return;
  try {
    const payload = await API.post(`/api/people/${person.id}/confirm-all`,
                                   { face_ids: targets.map((item) => Number(item.face_id)) });
    toast(`已确认 ${payload.confirmed} 张，判据已按你的判断重算`);
    dropFromSuggest(payload.confirmed_faces || targets.map((item) => item.face_id));
    await afterDecision();
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
  }
}

/**
 * ★「剩下的全部不是他」：把列表里**还没处理**的候选一次全否决。
 *
 * 这是"我挑完了"的收尾动作。否决是**按人**记的（`person_reject`）：
 * 这些脸以后不会再被提给他，但仍然可能是别人的照片 —— 所以界面文案写"不是他"，
 * 不写"不是人"（后者是另一件事，在每张卡片上单独点）。
 */
async function rejectRestOfSuggestions() {
  const data = state.suggest;
  const person = state.detail && state.detail.person;
  const items = (data && data.items) || [];
  if (!person || !items.length) {
    toast('现在没有待处理的候选', true);
    return;
  }
  const ok = confirm(
    `把列表里剩下的 ${items.length} 张全部标成「不是他」？\n\n`
    + '以后不会再把这些脸提给这个人（按"人"记，不是把脸删掉）。\n'
    + '误判了可以去下面「你否决过的」里点「其实是他」撤销。');
  if (!ok) return;
  await rejectFaces(items.map((item) => Number(item.face_id)), { scope: 'suggest' });
}

/** 从候选列表里摘掉几张（就地更新，不重新问服务端 —— 逐张点的时候要立刻看到它消失）。 */
function dropFromSuggest(faceIds) {
  if (!state.suggest) return;
  const ids = new Set((faceIds || []).map(Number));
  state.suggest = {
    ...state.suggest,
    items: (state.suggest.items || []).filter((item) => !ids.has(Number(item.face_id))),
    accepted: (state.suggest.items || [])
      .filter((item) => !ids.has(Number(item.face_id)) && item.accepted).length,
  };
}

/** ★ 一键打包：把这个人的原片打成一个 zip（路径写在结果框里，不走浏览器下载）。 */
async function packPerson() {
  const person = state.detail && state.detail.person;
  if (!person) return;
  const app = await import('./app.js');
  await app.exportPack({ kind: 'person', id: Number(person.id),
                         label: personTitle(person) });
}

/** ★ 不是人：这张脸压根不是一个人。**带撤销提示** —— 否则用户不敢点。 */
async function markNotPerson(faceIds, { thenRefresh = true } = {}) {
  const ids = [...new Set((faceIds || []).map(Number).filter(Number.isFinite))];
  if (!ids.length) {
    toast('先勾选几张脸（或直接点某一张上的「不是人」）', true);
    return;
  }
  try {
    const payload = await API.post('/api/faces/verdict',
                                   { face_ids: ids, verdict: 'not_person' });
    ids.forEach((id) => state.picked.delete(id));
    dropFromSuggest(ids);      // "不是人"的脸同样不该留在候选列表里
    toast(`已把 ${payload.changed} 张标成「不是人」。它们不再参与分人；`
          + '撤销在左边「你标过「不是人」的」里');
    if (thenRefresh) await afterDecision();
  } catch (error) {
    toast(`标记失败：${error.message}`, true);
  }
}

/** 撤销「不是人」。 */
async function markPersonAgain(faceId) {
  try {
    await API.post('/api/faces/verdict', { face_ids: [faceId], verdict: 'person' });
    toast('已撤销：这张脸重新参与分人');
    await loadList();
  } catch (error) {
    toast(`撤销失败：${error.message}`, true);
  }
}

/**
 * ★ 迁移：把这些脸搬到另一个**已命名**的人名下。
 *
 * 迁完之后**自动切到那个人**：用户刚说"这些其实都是 XX"，
 * 接下来要看的一定是 XX 现在长什么样，而不是原先那个人的空墙。
 */
async function moveFaces(faceIds, targetPersonId) {
  const ids = [...new Set((faceIds || []).map(Number).filter(Number.isFinite))];
  const target = state.people.find((item) => item.id === Number(targetPersonId));
  if (!target) {
    toast('先在右边的下拉框里选一个已命名的人', true);
    return;
  }
  if (!ids.length) {
    toast('先勾选要迁移的脸', true);
    return;
  }
  const ok = confirm(
    `把勾选的 ${ids.length} 张脸迁到「${target.name}」名下？\n\n`
    + '这些脸会从原来那个人名下移走（原来那个人会立刻按剩下的脸重算判据）。\n'
    + '只改数据库里的归属，不动任何照片。');
  if (!ok) return;
  try {
    const payload = await API.post('/api/people/move',
                                   { face_ids: ids, target_person_id: target.id });
    ids.forEach((id) => state.picked.delete(id));
    state.suggest = null;
    await loadList();
    await loadDetail(target.id);
    toast(`已迁到「${target.name}」名下：${payload.moved} 张`
          + (payload.skipped && payload.skipped.length
            ? `（${payload.skipped.length} 张已不在库里，跳过）` : ''));
  } catch (error) {
    toast(`迁移失败：${error.message}`, true);
  }
}

/** ★ 一键确认：把模型提议的整批认下来。**必须二次确认**（它会一次性改掉判据）。 */
async function confirmAllProposals() {
  const person = state.detail && state.detail.person;
  const count = Number((state.detail && state.detail.counts || {}).auto || 0);
  if (!person || !count) {
    toast('这个人现在没有待确认的提议', true);
    return;
  }
  const ok = confirm(
    `把模型的 ${count} 张提议全部确认为「${personTitle(person)}」？\n\n`
    + '确认之后它们就算你的判断，会拿去重算这个人的判据。\n'
    + `传错认单项也可以回头改：逐张点「不是他」即可。点错了整批，用「不是人」把非人脸排掉。`);
  if (!ok) return;
  try {
    const payload = await API.post(`/api/people/${person.id}/confirm-all`, {});
    toast(`已确认 ${payload.confirmed} 张，判据已按你的判断重算`);
    await afterDecision();
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
  }
}

/** ★ 解散候选集：这一簇不算数。脸一张不删，只是不再被当成一个人。 */
async function dissolvePerson() {
  const person = state.detail && state.detail.person;
  if (!person) return;
  const faces = Number((state.detail && state.detail.counts || {}).positive || 0)
    + Number((state.detail && state.detail.counts || {}).auto || 0);
  const ok = confirm(
    `解散这个候选（${faces} 张脸）？\n\n`
    + '这一簇的结论会被丢掉，候选从左边列表里移走。\n'
    + '脸一张都不会删，也不会从照片里消失；它们只是不再被当成"同一个人"。\n'
    + '之后可以用「不是人」把混进来的海报/雕像永久排除，也可以在左边「你解散过的候选」里放回来。');
  if (!ok) return;
  try {
    await API.post(`/api/people/${person.id}/dissolve`, {});
    toast('已解散这个候选。要反悔在左边「你解散过的候选」里点「放回列表」');
    state.activeId = null;
    state.detail = null;
    state.identity = null;
    state.suggest = null;
    clearTransient();
    await loadList();
    if (state.people.length) await loadDetail(state.people[0].id);
    else renderDetail();
  } catch (error) {
    toast(`解散失败：${error.message}`, true);
  }
}

/** 撤销解散。 */
async function restorePerson(personId) {
  try {
    await API.post(`/api/people/${personId}/restore`, {});
    toast('已放回列表');
    await loadList();
  } catch (error) {
    toast(`放回失败：${error.message}`, true);
  }
}

async function onActionClick(event) {
  // 左栏的卡片是"选人"（没有 data-act）：必须先处理它，否则点了没反应
  const card = event.target.closest('[data-person]');
  if (card) {
    await loadDetail(Number(card.dataset.person));
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  const faceId = Number(button.dataset.face);
  event.preventDefault();

  if (act === 'bulk-confirm' || act === 'bulk-reject') {
    const targets = pickedFaces(button);
    const scope = sectionOf(button);          // ⚠️ 同上：候选区里的批量也要摘掉
    if (act === 'bulk-confirm') await confirmFaces(targets, { scope });
    else await rejectFaces(targets, { scope });
    return;
  }
  if (act === 'bulk-not-person') {
    const targets = pickedFaces(button);
    if (targets.length > 1
        && !confirm(`把勾选的 ${targets.length} 张脸都标成「不是人」？\n\n`
                    + '它们会从所有人的名下摘掉，并且不再参与分人。\n'
                    + '撤销在左边「你标过「不是人」的」里。')) return;
    await markNotPerson(targets);
    return;
  }
  if (act === 'bulk-move') {
    const select = button.closest('.person-bulk').querySelector('[data-move-target]');
    await moveFaces(pickedFaces(button), select ? select.value : '');
    return;
  }

  switch (act) {
    // ★ 「是他 / 不是他」的作用范围**从卡片所在的区块推**，不能写死。
    //
    //   上一版这里写死 `scope: 'face'`，而"从候选列表里摘掉这一张"的逻辑挂在
    //   `scope === 'suggest'` 上 —— 于是候选区里点「是他」「不是他」时，
    //   服务端确实记下了，界面上那张卡片却**赖着不走**。
    //   用户的原话："被选了的不会退出，被否认的也不会退出。" 根因就是这一行。
    case 'confirm': await confirmFaces([faceId], { scope: sectionOf(button) }); break;
    case 'reject': await rejectFaces([faceId], { scope: sectionOf(button) }); break;
    case 'suggest-confirm': await confirmSuggested(button.dataset.scope || 'accepted'); break;
    case 'suggest-reject-rest': await rejectRestOfSuggestions(); break;
    case 'pack': await packPerson(); break;
    case 'not-person': await markNotPerson([faceId]); break;
    case 'person-again': await markPersonAgain(faceId); break;
    case 'confirm-all': await confirmAllProposals(); break;
    case 'dissolve': await dissolvePerson(); break;
    case 'restore-person': await restorePerson(Number(button.dataset.personId)); break;
    case 'asset': {
      // 「看原片」：打开这张脸所在照片的详情抽屉（外壳上的，不属于任何视图）
      const assetId = Number(button.dataset.asset);
      if (!assetId) break;
      const app = await import('./app.js');
      await app.openAssetDetail(assetId);
      break;
    }
    case 'unreject':
      // 撤销否决 = 重新确认这张脸（服务端会把 person_reject 里的记录撤掉）
      await confirmFaces([faceId], { scope: 'rejected' });
      break;
    case 'cover': await setCover(faceId); break;
    case 'rename': await renamePerson(); break;
    case 'collection': await toCollection(); break;
    case 'delete': await deletePerson(); break;
    case 'suggest': await findSimilar(); break;
    case 'new-person': await createPerson(); break;
    case 'reload':
      clearTransient();
      await loadList();
      if (state.activeId) await loadDetail(state.activeId);
      else renderDetail();
      break;
    default: break;
  }
}

export function initPeople() {
  if (state.initialized) return;
  const root = pickView();
  if (!root) return;   // 视图还没挂上：不标记已初始化，等 refreshPeople 时再来
  state.initialized = true;

  fillQueries(root);
  // 事件全部挂在**视图根**上（委托）：区块是重画的，节点会被换掉，
  // 挂在节点上的监听器会跟着一起消失 —— 那正是"点第二次没反应"的成因。
  root.addEventListener('click', onActionClick);
  root.addEventListener('change', (event) => {
    const box = event.target.closest('[data-pick]');
    if (box) togglePick(Number(box.dataset.pick), box.checked);
  });
  if (state.q.newButton) {
    state.q.newButton.addEventListener('click', async () => { await createPerson(); });
  }
  if (state.q.learnAllButton) {
    state.q.learnAllButton.addEventListener('click', async () => {
      await learnAll(state.q.learnAllButton);
    });
  }
  syncSelection();
}

export async function refreshPeople() {
  initPeople();
  if (!state.initialized || !state.q.usable) {
    toast('人物视图还没准备好：页面里缺少放人物列表与详情的容器', true);
    return;
  }
  await loadCliHints();      // 空态里那两条命令要按当前环境显示（见文件头）
  await loadList();
  if (state.activeId) {
    await loadDetail(state.activeId);
    return;
  }
  if (state.people.length) {
    // 进来就有东西看：直接打开第一个（未命名的优先）候选
    await loadDetail(state.people[0].id);
    return;
  }
  // 一个人物都没有：把"库里现在有什么 + 下一步做什么"写在右栏，
  // 而不是留一片空白让人以为功能坏了
  const empty = state.q.empty;
  const box = emptyBoxHtml();
  if (empty) {
    empty.innerHTML = box;
    empty.hidden = !box;
  }
  if (state.q.detail) state.q.detail.innerHTML = box || '<div class="hint">从左边选一个人。</div>';

  // 空态里有「开始检测人脸 / 开始算特征」按钮。命令目录是异步来的：
  // 若此刻还没到，按钮会是 disabled —— 等目录到了只**原地启用**它们，
  // 不重建 DOM（重建会让滚动位置跳走，而这里正是用户刚进来看到的那一屏）。
  if (box && box.includes('data-run-action')) {
    const nodes = [empty, state.q.detail].filter(Boolean);
    nodes.forEach((node) => bindInlineRun(node));
    enableActionButtonsWhenReady(nodes);
  }
}
