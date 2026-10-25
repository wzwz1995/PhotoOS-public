/**
 * PhotoOS · 界面直跑命令（共享模块）
 *
 * ## 这里只做三件事
 *
 * 1. 拉取**服务端的命令目录**（`/api/actions`）—— 命令字符串、参数、用哪个
 *    解释器，全部由服务端决定；前端只认识 `action id`。
 * 2. 把一个 action 排进后台任务队列（`POST /api/actions/{id}/run`）。
 * 3. 轮询任务直到终态，把进度与输出显示出来（复用维护页那张任务表）。
 *
 * ## 为什么要有这个模块
 *
 * 在它之前，界面上有六处"这件事我算不了，请你去命令行跑一次"：准备搜索词、
 * 算 CLIP/EG2 向量、下载模型、人脸 detect/embed/discover。每处都要用户
 * 复制命令 → 切终端 → 粘贴 → 回浏览器刷新。分散实现必然长得不一样，
 * 所以这里收成一份，三个入口（找照片 / 人物 / 维护）共用。
 *
 * 纪律：本文件**不得有顶层副作用**（与 shared.js 同规矩）。
 */

import { API, $, escapeHtml, toast } from './shared.js';

/** 模块级缓存：目录在一次会话里不会变，没必要每次都拉。 */
let catalogCache = null;

export async function loadCatalog(force = false) {
  if (catalogCache && !force) return catalogCache;
  catalogCache = await API.get('/api/actions');
  return catalogCache;
}

export function catalogAction(id) {
  if (!catalogCache) return null;
  return catalogCache.actions.find((a) => a.id === id) || null;
}

export function environmentLine() {
  if (!catalogCache) return '';
  const env = catalogCache.environment;
  return env.reason ? `${env.reason}` : '';
}

/**
 * 排一条命令进队列。
 *
 * `query` 只有 `needs_query` 的动作会被服务端读取；其余动作传了也会被忽略
 * —— 前端不做这个判断，判断权在服务端（单一真相）。
 */
export async function runAction(id, { query, prompt } = {}) {
  const body = {};
  if (query !== undefined) body.query = query;
  if (prompt !== undefined) body.prompt = prompt;
  return API.post(`/api/actions/${encodeURIComponent(id)}/run`, body);
}

/**
 * 轮询一个任务直到终态。
 *
 * 为什么是轮询而不是流式：任务本身就在服务端跑（关掉页面也不中断），
 * 而这张任务表本来就靠轮询显示所有后台任务 —— 再开一条流式通道只会多一套
 * 状态要同步。这里把间隔做成自适应的：前几秒快一点（让"点了有反应"成立），
 * 之后放慢（长任务可能跑几十分钟，没必要每 800ms 问一次）。
 */
export async function waitForJob(jobId, { onProgress, intervalMs = 800, maxMs = 0 } = {}) {
  const terminal = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
  const started = Date.now();
  let interval = intervalMs;
  for (;;) {
    let payload;
    try {
      payload = await API.get(`/api/jobs/${jobId}`);
    } catch (error) {
      return { status: 'UNKNOWN', error: error.message };
    }
    const job = payload.job || {};
    if (onProgress) onProgress(job);
    if (terminal.has(job.status)) return job;
    if (maxMs && Date.now() - started > maxMs) return job;
    await new Promise((resolve) => setTimeout(resolve, interval));
    interval = Math.min(Math.round(interval * 1.3), 4000);
  }
}

/** 任务卡片上的状态中文名。与后端状态机一致（见 jobs/queue.py）。 */
export const STATUS_LABEL = {
  PENDING: '排队中',
  PROCESSING: '执行中',
  RETRY: '将重试',
  PAUSED: '已暂停',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
};

/** 一条动作在列表里的一行（按钮 + 说明 + 会执行什么）。 */
function actionRow(action, { compact = false } = {}) {
  const disabled = !action.available;
  const badge = action.long_running
    ? '<span class="act-badge is-long">耗时</span>'
    : '';
  const warn = disabled
    ? `<span class="act-badge is-missing">缺 ${escapeHtml(action.missing_requires.join('、'))}</span>`
    : '';
  return `
    <div class="act-row${compact ? ' is-compact' : ''}">
      <div class="act-info">
        <span class="act-label">${escapeHtml(action.label)}${badge}${warn}</span>
        <span class="act-desc">${escapeHtml(action.description)}</span>
        <code class="act-cmd">${escapeHtml(action.command_preview || '')}</code>
      </div>
      <button class="btn small act-run" data-action="${escapeHtml(action.id)}"
              ${disabled ? 'disabled title="缺少依赖，先看维护页的环境说明"' : ''}>
        ${disabled ? '缺依赖' : '执行'}
      </button>
    </div>`;
}

/**
 * 「命令」选择器：分组列出所有可直跑的动作。
 *
 * 做成弹窗而不是常驻面板的原因：维护页已经很长，而这里是"偶尔来一次"的
 * 操作入口；常驻会挤掉真正的日常内容（导入、任务、备份）。
 */
export async function openActionsDialog({ title = '直接执行命令', group, onDone } = {}) {
  let payload;
  try {
    payload = await loadCatalog();
  } catch (error) {
    toast(`命令目录读取失败：${error.message}`, true);
    return;
  }

  const env = payload.environment;
  const missing = env.missing || [];
  const groups = new Map();
  for (const action of payload.actions) {
    if (group && action.group !== group) continue;
    if (!groups.has(action.group)) groups.set(action.group, []);
    groups.get(action.group).push(action);
  }

  const host = document.createElement('div');
  host.className = 'asset-modal';
  host.innerHTML = `
    <div class="asset-modal-backdrop" data-close></div>
    <div class="asset-modal-panel act-panel" role="document">
      <div class="asset-modal-head">
        <span class="asset-modal-title">${escapeHtml(title)}</span>
        <span class="asset-modal-actions">
          <button class="btn small" data-close>关闭</button>
        </span>
      </div>
      <div class="act-body">
        <p class="hint">
          命令由<strong>服务端固定</strong>，只能选、不能改。执行走后台任务，关掉这个窗口也不会中断。
        </p>
        <div class="act-env">
          <span class="act-env-label">执行环境</span>
          <code>${escapeHtml(env.python)}</code>
          <span class="act-env-reason">${escapeHtml(env.reason)}</span>
          ${missing.length
            ? `<span class="act-env-missing">缺可选依赖：${escapeHtml(missing.join('、'))}
                 —— 相关命令会失败，失败信息里会有具体装法。</span>`
            : '<span class="act-env-ok">可选依赖齐全。</span>'}
        </div>
        ${[...groups.entries()].map(([name, items]) => `
          <div class="act-group">
            <h3 class="act-group-title">${escapeHtml(name)}</h3>
            ${items.map((a) => actionRow(a)).join('')}
          </div>`).join('')}
      </div>
    </div>`;
  document.body.appendChild(host);

  const close = () => host.remove();
  host.querySelectorAll('[data-close]').forEach((node) => { node.onclick = close; });
  host.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });

  host.querySelectorAll('.act-run').forEach((button) => {
    button.onclick = async () => {
      const action = catalogAction(button.dataset.action);
      if (!action) return;
      let query;
      if (action.needs_query) {
        // 只用 prompt 拿一个词：它是唯一进入命令行的用户输入，服务端还会再校验一次。
        const answer = window.prompt(`「${action.label}」需要一个搜索词：`, '');
        if (answer === null) return;
        query = answer.trim();
        if (!query) { toast('搜索词不能为空', true); return; }
      }
      await submitAction(action, { query, button });
      if (onDone) onDone();
    };
  });

  return { close, refresh: () => openActionsDialog({ title, group, onDone }) };
}

/**
 * 提交一条动作并跟踪到终态。
 *
 * 这是所有入口（弹窗 / 「立即准备」按钮 / 维护页面板）共用的那一条路径 ——
 * 三处各写一遍必然出现"同一件事三种反馈"。
 */
export async function submitAction(action, { query, prompt, button, silent = false } = {}) {
  const restore = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = '提交中…'; }
  try {
    const result = await runAction(action.id, { query, prompt });
    if (!result.queued) {
      toast(result.reason || '已有一条命令在队列里', true);
      return result;
    }
    const jobId = result.job.id;
    toast(`已排入后台任务 #${jobId}${result.long_running ? '（这条会跑一阵子）' : ''}`);
    if (button) button.textContent = `任务 #${jobId} 执行中…`;

    const job = await waitForJob(jobId, {
      onProgress: (current) => {
        if (button && current.note) {
          button.textContent = `#${jobId} ${(current.progress_done || 0)} 行…`;
        }
      },
    });

    if (job.status === 'COMPLETED') {
      toast(`任务 #${jobId} 完成`);
    } else if (job.status === 'FAILED') {
      // 失败原因里带命令自己的最后几行输出 —— 直接给用户看，不要吞掉。
      const detail = String(job.error || '').split('\n').slice(-6).join('\n');
      if (!silent) window.alert(`任务 #${jobId} 失败：\n\n${detail}`);
      toast(`任务 #${jobId} 失败`, true);
    } else if (job.status === 'CANCELLED') {
      toast(`任务 #${jobId} 已取消`);
    }
    return job;
  } catch (error) {
    toast(`提交失败：${error.message}`, true);
    return { status: 'ERROR', error: error.message };
  } finally {
    if (button) {
      button.disabled = false;
      // 保持宽度稳定：长文案会让按钮在列表里跳动
      button.textContent = restore;
    }
  }
}

/**
 * 维护页那块常驻面板：按组列出全部动作。
 * 弹窗是"顺手来一下"，这里是"我要找某条命令"的完整入口。
 */
export async function renderActionsPanel(node) {
  if (!node) return;
  let payload;
  try {
    payload = await loadCatalog(true);
  } catch (error) {
    const summary = $('#actions-summary');
    if (summary) summary.textContent = '命令目录读取失败';
    node.innerHTML = `<p class="hint">命令目录读取失败：${escapeHtml(error.message)}</p>`;
    return;
  }
  const env = payload.environment;
  const missing = env.missing || [];
  const groups = new Map();
  for (const action of payload.actions) {
    if (!groups.has(action.group)) groups.set(action.group, []);
    groups.get(action.group).push(action);
  }

  // 收起状态也要能回答「里面有多少条、有没有缺依赖」，否则用户还得先展开。
  const summary = $('#actions-summary');
  if (summary) {
    const unavailable = payload.actions.filter((a) => !a.available).length;
    summary.textContent = [
      `${payload.actions.length} 条`,
      unavailable ? `${unavailable} 条缺依赖` : '依赖齐全',
    ].join(' · ');
  }

  node.innerHTML = `
    <div class="act-env">
      <span class="act-env-label">执行环境</span>
      <code>${escapeHtml(env.python)}</code>
      <span class="act-env-reason">${escapeHtml(env.reason)}</span>
      ${missing.length
        ? `<span class="act-env-missing">缺可选依赖：${escapeHtml(missing.join('、'))}
             —— 相关命令会失败，失败信息里会写明怎么装。</span>`
        : '<span class="act-env-ok">可选依赖齐全。</span>'}
    </div>
    ${[...groups.entries()].map(([name, items]) => `
      <div class="act-group">
        <h3 class="act-group-title">${escapeHtml(name)}</h3>
        ${items.map((a) => actionRow(a, { compact: true })).join('')}
      </div>`).join('')}`;

  node.querySelectorAll('.act-run').forEach((button) => {
    button.onclick = async () => {
      const action = catalogAction(button.dataset.action);
      if (!action) return;
      let query;
      if (action.needs_query) {
        const answer = window.prompt(`「${action.label}」需要一个搜索词：`, '');
        if (answer === null) return;
        query = answer.trim();
        if (!query) { toast('搜索词不能为空', true); return; }
      }
      await submitAction(action, { query, button });
      // 命令可能改变了库的内容（新搜索词 / 新向量），刷新任务表让用户看到结果
      if (typeof window.photoosRefreshJobs === 'function') window.photoosRefreshJobs();
    };
  });
}

/**
 * 进度面板：命令在跑的时候**让用户看得见**。
 *
 * ## 为什么必须有它
 *
 * 一开始只有按钮文案在变（`任务 #22 47 行…`）+ 一条 toast，任务表里也只有
 * 「进度未知」的空条 —— 因为服务端**从不给命令类任务设置 `progress_total`**
 * （命令要跑多久、有几步，不到输出出来是不知道的）。于是用户点了「立即准备
 * 搜索词」之后，界面上几乎什么都没有。
 *
 * 现在给两种进度：
 *   · **确定进度**：从命令输出里认出 `123/456` 或 `(40.0%)` 这类标记，就画真实百分比；
 *   · **不确定进度**：认不出来时，画一条流动的条 + 已输出行数。
 *     这不是"假装有进度"，而是如实表达"在跑、还在出东西" —— 比一条 0% 的
 *     静止空条诚实得多。
 *
 * 面板同时显示**命令原文**与**最近几行输出**：这两样是用户判断"它是不是卡住了"
 * 的唯一依据，也是失败时能自己看懂原因的地方。
 */
export function createProgressPanel(container, { command, label } = {}) {
  if (!container) return { update() {}, finish() {}, remove() {} };
  const started = Date.now();

  container.innerHTML = `
    <div class="run-progress" role="status" aria-live="polite">
      <div class="run-progress-head">
        <span class="run-progress-title">${escapeHtml(label || '正在执行')}</span>
        <span class="run-progress-meta" data-run-meta>已开始…</span>
      </div>
      <div class="run-progress-bar" data-run-bar><i></i></div>
      ${command ? `<code class="run-progress-cmd">${escapeHtml(command)}</code>` : ''}
      <pre class="run-progress-tail" data-run-tail></pre>
    </div>`;

  const bar = container.querySelector('[data-run-bar]');
  const meta = container.querySelector('[data-run-meta]');
  const tail = container.querySelector('[data-run-tail]');
  const lines = [];
  /** 最近一次认出的确定进度。**一旦有了就保留** —— 见下面 update() 的说明。 */
  let best = null;

  /** 从一行输出里认出进度标记。认不出返回 null（**不猜**）。 */
  function parseProgress(text) {
    if (!text) return null;
    // `1234/5678`、`1234 / 5678`、`1234/5678 [00:12<01:03]`
    let m = text.match(/(\d[\d,]{0,9})\s*\/\s*(\d[\d,]{0,9})/);
    if (m) {
      const done = Number(m[1].replace(/,/g, ''));
      const total = Number(m[2].replace(/,/g, ''));
      if (Number.isFinite(done) && Number.isFinite(total) && total > 0 && done <= total) {
        return { done, total };
      }
    }
    // `(40.0%)` / `40%`
    m = text.match(/\(?\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\)?/);
    if (m) {
      const pct = Number(m[1]);
      if (Number.isFinite(pct) && pct >= 0 && pct <= 100) {
        return { done: pct, total: 100 };
      }
    }
    return null;
  }

  function elapsed() {
    const secs = Math.round((Date.now() - started) / 1000);
    if (secs < 60) return `${secs} 秒`;
    const m = Math.floor(secs / 60);
    return `${m} 分 ${secs % 60} 秒`;
  }

  return {
    update(job) {
      const count = Number(job.progress_done) || 0;
      if (job.note) {
        const line = String(job.note);
        if (lines[lines.length - 1] !== line) {
          lines.push(line);
          // 只留最近 6 行：这是"它还活着"的证据，不是完整日志
          while (lines.length > 6) lines.shift();
          if (tail) tail.textContent = lines.join('\n');
        }
      }
      // ⚠️ **一旦认出过确定进度就保留它**，不要因为下一行没有比例数就退回不确定态。
      // 命令的输出是混合的（模型加载条 `1376/1376` 之后就是结果行），
      // 来回切换会让进度条在 100% 与流动之间反复跳 —— 看起来像出了故障。
      // 只在认到"更大的总量"时更新（多阶段命令会换一个分母）。
      const hint = parseProgress(job.note);
      if (hint && (!best || hint.total > best.total
                   || (hint.total === best.total && hint.done > best.done))) {
        best = hint;
      }
      if (best && bar) {
        const pct = Math.max(0, Math.min(100, (100 * best.done) / best.total));
        bar.classList.add('is-determinate');
        bar.querySelector('i').style.width = `${pct.toFixed(1)}%`;
        if (meta) {
          meta.textContent = `${best.done.toLocaleString()} / ${best.total.toLocaleString()}`
            + `（${pct.toFixed(1)}%）· ${elapsed()}`;
        }
      } else {
        if (bar) bar.classList.remove('is-determinate');
        if (meta) meta.textContent = `已输出 ${count} 行 · ${elapsed()}`;
      }
    },
    finish(job) {
      const ok = job.status === 'COMPLETED';
      const text = ok ? '已完成'
        : (job.status === 'CANCELLED' ? '已取消' : '失败');
      if (meta) meta.textContent = `${text} · 用时 ${elapsed()}`;
      if (bar) {
        bar.classList.add('is-determinate');
        bar.classList.toggle('is-failed', !ok);
        bar.querySelector('i').style.width = ok ? '100%' : '100%';
      }
      if (tail && job.error) {
        tail.textContent = String(job.error).split('\n').slice(-8).join('\n');
      }
    },
    remove() { container.innerHTML = ''; },
  };
}

/**
 * 在给定容器里跑一条动作，并把进度画出来。
 * 三个入口（找照片空态 / 人物空态 / 维护页面板）共用这一条路径。
 */
export async function runActionWithProgress(action, container, { query, prompt, button, label } = {}) {
  const panel = createProgressPanel(container, {
    command: action.command_preview,
    label: label || action.label,
  });
  const restore = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = '提交中…'; }

  try {
    const result = await runAction(action.id, { query, prompt });
    if (!result.queued) {
      toast(result.reason || '已有一条命令在队列里', true);
      panel.finish({ status: 'CANCELLED' });
      return result;
    }
    const jobId = result.job.id;
    if (button) button.textContent = `任务 #${jobId} 执行中…`;
    if (result.long_running) {
      toast(`已排入后台任务 #${jobId} —— 这条会跑一阵子，可以切到别的页面`);
    }
    const job = await waitForJob(jobId, { onProgress: (cur) => panel.update(cur) });
    panel.finish(job);

    if (job.status === 'COMPLETED') {
      toast(`${label || action.label}：完成`);
    } else if (job.status === 'FAILED') {
      toast(`${label || action.label}：失败`, true);
    }
    return job;
  } catch (error) {
    toast(`执行失败：${error.message}`, true);
    panel.finish({ status: 'FAILED', error: error.message });
    return { status: 'ERROR', error: error.message };
  } finally {
    if (button) { button.disabled = false; button.textContent = restore; }
  }
}

/** 「立即准备这个搜索词」这类行内按钮共用的一份实现。 */
export async function runPrepareQuery(term, button) {
  const action = catalogAction('search_prepare');
  if (!action) {
    toast('命令目录还没载入，请稍后重试', true);
    return null;
  }
  return submitAction(action, { query: term, button });
}

/**
 * 生成一个"行内执行按钮"的 HTML 片段（含"其它命令"次要入口）。
 *
 * 空态里那种"这件事要跑一条命令才能继续"的地方，视觉形状应该完全一致 ——
 * 三个页面各写一遍必然长歪，所以收在这里。按钮的点击绑定由各页在渲染后
 * `bindInlineRun()` 完成（`innerHTML` 之后才知道节点在哪）。
 */
export function inlineRunHtml(actionId, label, { secondary = true } = {}) {
  const action = catalogAction(actionId);
  const ready = !!action && action.available;
  const title = !action
    ? '命令目录还没载入'
    : (ready ? action.description : `缺少依赖：${action.missing_requires.join('、')}`);
  return `
    <div class="not-ready-actions">
      <button class="btn primary" data-run-action="${escapeHtml(actionId)}"
              ${ready ? '' : 'disabled'} title="${escapeHtml(title)}">
        ${escapeHtml(label)}
      </button>
      ${secondary
        ? '<button class="btn small" data-run-more="1">其它可用命令…</button>'
        : ''}
    </div>`;
}

/**
 * 给刚渲染出来的 `inlineRunHtml()` 片段绑事件。
 * 返回一个是否绑到了按钮的布尔值，方便调用方决定要不要再等目录。
 */
export function bindInlineRun(root) {
  let bound = 0;
  root.querySelectorAll('[data-run-action]').forEach((button) => {
    button.onclick = async () => {
      const action = catalogAction(button.dataset.runAction);
      if (!action) return;
      await submitAction(action, { button });
      if (typeof window.photoosRefreshJobs === 'function') window.photoosRefreshJobs();
    };
    bound += 1;
  });
  root.querySelectorAll('[data-run-more]').forEach((button) => {
    button.onclick = () => openActionsDialog({ title: '可直跑的命令' });
    bound += 1;
  });
  return bound;
}

/**
 * 目录晚到时的收尾：把已经渲染出来的行内按钮**原地**改成正确状态。
 *
 * 只改 `disabled` 与 `title`，不重建 DOM —— 这里正是用户刚进页面看到的那一屏，
 * 重建会让滚动位置跳走（而且看起来像页面闪了一下）。
 */
export async function enableActionButtonsWhenReady(nodes) {
  try {
    await loadCatalog();
  } catch {
    return;
  }
  for (const node of nodes) {
    node.querySelectorAll('[data-run-action]').forEach((button) => {
      const action = catalogAction(button.dataset.runAction);
      if (!action) return;
      button.disabled = !action.available;
      button.title = action.available
        ? action.description
        : `缺少依赖：${action.missing_requires.join('、')}`;
    });
  }
}
