/* 审计日志页（模块 12 §2.1 / §2.2，对应原型 07_审计日志页.pen）
 *
 * 两块内容：
 *   ① 哈希链完整性卡片：执行全链校验（真实逐条重算，不是查长度）
 *   ② 审计流水表：四维筛选 + 分页 + 行展开看 before/after 并排 diff + 导出
 *
 * 呈现规则：变更摘要由前端派生（BR-12-22）；哈希缩略展示、hover 看完整值（BR-12-23）。
 */
import * as api from '../api.js';
import * as session from '../session.js';
import * as ui from '../ui.js';
import { labelOf } from '../store.js';
import { renderDiff, summarize } from './diff.js';

export const meta = { title: '审计日志', crumb: '风控中台 / 审计日志' };

const BASE = `${api.API_PREFIX}/audit`;

const TIME_RANGES = [
  { value: '1h', label: '最近 1 小时', ms: 3600_000 },
  { value: '24h', label: '最近 24 小时', ms: 86_400_000 },
  { value: '7d', label: '最近 7 天', ms: 7 * 86_400_000 },
  // 上限 30 天：后端 AUD-4003 会拒绝更大跨度（防全表扫描）
  { value: '30d', label: '最近 30 天', ms: 30 * 86_400_000 },
  { value: 'custom', label: '自定义', ms: null },
];

const state = {
  actor: '', action: '', targetType: '', range: '24h',
  from: null, to: null, page: 1, pageSize: 20,
  expanded: new Set(),
};

let refs = null;
let inflight = null;
// 校验卡片的容器引用（模块内直接持有）。
// **不用 `getElementById`**：那是 HTML 里静态存在的 id 才该用的方式，
// 对动态创建的节点用 id 查找，一旦拼错就静默失效（自审脚本会拦下这类引用）。
let verifyHolder = null;
let verifyState = { status: 'idle', data: null };

function dispose() {
  if (inflight) { inflight.abort(); inflight = null; }
  refs = null;
  verifyHolder = null;
}

/**
 * 取错误里的 trace 后 6 位。
 *
 * **错误处理本身绝不能再抛错**：曾写成 `e.shortTrace()`，而抛出的是普通 TypeError
 * （没有该方法），于是真正的错误被"e.shortTrace is not a function"顶掉，
 * 排查时看到的是错误处理器的异常而不是根因。
 */
function traceOf(e) {
  return (e && typeof e.shortTrace === 'function') ? e.shortTrace() : '—';
}

function rangeToMs() {
  if (state.range === 'custom') return { from: state.from, to: state.to };
  const hit = TIME_RANGES.find((r) => r.value === state.range);
  return hit && hit.ms ? { from: Date.now() - hit.ms, to: null } : { from: null, to: null };
}

// ==================== ① 校验卡片 ====================
function renderVerifyCard() {
  const { status, data } = verifyState;
  const resultNode = (() => {
    if (status === 'idle') return ui.h('span', { class: 'tag plain', text: '未校验' });
    if (status === 'loading') return ui.h('span', { class: 'tag plain', text: '校验中…' });
    if (status === 'error') return ui.h('span', { class: 'tag high', text: '校验失败（接口异常）' });
    if (data && data.truncated) {
      return ui.h('span', { class: 'tag medium',
        text: `链较长，已校验前 ${data.chain_length} 条（可点继续）` });
    }
    if (data && data.ok) {
      return ui.h('span', { class: 'tag low', text: `全链一致 · 无篡改（${data.elapsed_ms} ms）` });
    }
    const broken = (data && data.broken_at) || {};
    return ui.h('span', { class: 'tag high',
      text: `发现篡改：第 ${broken.seq} 条（${broken.log_id}）` });
  })();

  const detail = (status === 'done' && data && !data.ok && data.broken_at)
    ? ui.h('div', { class: 'verify-broken' }, [
        ui.h('div', { class: 'hint', text: `原因：${data.broken_at.reason}` }),
        ui.h('div', { class: 'mono', text: `期望哈希 ${data.broken_at.expected_hash}` }),
        ui.h('div', { class: 'mono', text: `实际哈希 ${data.broken_at.actual_hash}` }),
      ])
    : null;

  const runBtn = ui.button(status === 'loading' ? '校验中' : '▶ 执行全链校验', {
    variant: 'primary',
    disabled: status === 'loading',
    // 必须包一层箭头函数：直接写 `onClick: runVerify` 会把 **PointerEvent**
    // 当作 fromSeq 传进去，请求变成 `from_seq=[object PointerEvent]` → 422
    onClick: () => runVerify(0),
  });

  return ui.card('审计哈希链完整性', [
    ui.h('div', { class: 'toolbar' }, [
      ui.h('div', { class: 'filters' }, [runBtn]),
      ui.h('div', { class: 'filters' }, [resultNode]),
    ]),
    ui.h('div', { class: 'stat-row' }, [
      ui.statCard('链长度', data ? data.chain_length : '—', '条'),
      ui.h('div', { class: 'stat-card' }, [
        ui.h('div', { class: 'stat-title', text: '创世哈希' }),
        ui.h('div', { class: 'stat-value mono',
          style: 'font-size:15px', text: data ? data.genesis_hash : 'GENESIS' }),
      ]),
      ui.h('div', { class: 'stat-card' }, [
        ui.h('div', { class: 'stat-title', text: '最新区块哈希' }),
        ui.h('div', { class: 'stat-value mono', style: 'font-size:15px',
          text: data && data.head_hash ? `0x${data.head_hash.slice(0, 4)}…${data.head_hash.slice(-4)}` : '—' }),
      ]),
    ]),
    detail,
    ui.h('div', { class: 'hint', text:
      '校验算法：sha256(prev_hash + "|" + 规范化内容)，从创世块逐条重算并比对，返回首个不一致位置。' }),
  ]);
}

async function runVerify(fromSeq = 0) {
  // 防御：事件对象/字符串都不该被当成序号发出去（见按钮处 onClick 的说明）
  const seq = Number.isInteger(fromSeq) && fromSeq > 0 ? fromSeq : 0;
  verifyState = { status: 'loading', data: verifyState.data };
  redraw();
  try {
    const data = await api.get(`${BASE}/verify`, seq ? { from_seq: seq } : null);
    verifyState = { status: 'done', data };
  } catch (e) {
    verifyState = { status: 'error', data: null };
  }
  redraw();
  const d = verifyState.data;
  if (d && d.ok === true && !d.truncated) ui.toast(`全链一致 · ${d.chain_length} 条（${d.elapsed_ms} ms）`, 'ok');
  if (d && d.ok === false && !d.truncated) ui.toast(`发现篡改：第 ${d.broken_at.seq} 条`, 'err', 6000);
}

// ==================== ② 流水表 ====================
function renderTable(items) {
  if (!items.length) {
    ui.mount(refs.tableWrap, ui.empty('当前筛选条件下无审计记录', '可放宽时间范围或清空筛选'));
    return;
  }
  const columns = [
    { name: '时间', width: 86 }, { name: '操作人', width: 96 },
    { name: '角色', width: 92 }, { name: '动作', width: 130 },
    { name: '目标对象', width: 140 }, { name: '变更摘要' },
    { name: '本条哈希', width: 120 }, { name: '前序哈希', width: 120 },
  ];

  const rows = items.flatMap((it) => {
    const time = new Date(Number(it.ts));
    const pad = (n) => String(n).padStart(2, '0');
    const hhmmss = `${pad(time.getHours())}:${pad(time.getMinutes())}:${pad(time.getSeconds())}`;
    const summary = summarize(it.before, it.after);
    const expanded = state.expanded.has(it.log_id);

    const mainRow = ui.h('tr', {}, [
      ui.h('td', { class: 'mono', title: ui.fmtTime(it.ts), text: hhmmss }),
      ui.h('td', { text: it.actor || '—' }),
      ui.h('td', { text: labelOf('role', it.actor_role, it.actor_role || '—') }),
      ui.h('td', { class: 'mono', text: it.action }),
      ui.h('td', { class: 'mono', text: it.target_id || '—' }),
      ui.h('td', {}, [
        ui.h('span', { class: 'summary', title: '点击展开 before/after 对比',
          text: (expanded ? '▾ ' : '▸ ') + summary,
          onclick: () => {
            if (state.expanded.has(it.log_id)) state.expanded.delete(it.log_id);
            else state.expanded.add(it.log_id);
            renderTable(items);
          } }),
      ]),
      ui.h('td', { class: 'mono', title: it.hash || '', text: it.hash_short }),
      ui.h('td', { class: 'mono', title: it.prev_hash || '', text: it.prev_hash_short }),
    ]);

    if (!expanded) return [mainRow];
    const detailRow = ui.h('tr', { class: 'diff-row' }, [
      ui.h('td', { colspan: '8' }, renderDiff(it.before, it.after)),
    ]);
    return [mainRow, detailRow];
  });

  ui.mount(refs.tableWrap, ui.table(columns, rows));
}

function renderPager() {
  const total = state.total || 0;
  const pages = state.pages || 0;
  refs.pageInfo.textContent = `共 ${total} 条 · 第 ${state.page} / ${Math.max(1, pages)} 页`;
  refs.btnPrev.disabled = state.page <= 1;
  refs.btnNext.disabled = pages === 0 || state.page >= pages;
}

async function load() {
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;
  const { from, to } = rangeToMs();
  ui.mount(refs.tableWrap, ui.loading(6));
  try {
    const data = await api.get(`${BASE}/logs`, {
      actor: state.actor, action: state.action, target_type: state.targetType,
      from, to, page: state.page, page_size: state.pageSize,
    }, signal);
    state.total = data.total;
    state.pages = data.pages;
    renderTable(data.items || []);
    renderPager();
  } catch (e) {
    if (e.name === 'AbortError') return;
    ui.mount(refs.tableWrap, ui.empty('审计流水加载失败', `${e.message}（trace ${traceOf(e)}）`));
  } finally {
    if (inflight && inflight.signal === signal) inflight = null;
  }
}

async function doExport(fmt) {
  const { from, to } = rangeToMs();
  const qs = new URLSearchParams({ format: fmt });
  Object.entries({ actor: state.actor, action: state.action,
    target_type: state.targetType, from, to }).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
  });
  try {
    // 下载必须自己发请求：`window.open` 无法携带 Authorization 头，
    // 而令牌只放在请求头里（不放 URL —— URL 会进浏览器历史与日志）
    const res = await fetch(`${BASE}/export?${qs.toString()}`, {
      headers: { Authorization: `Bearer ${session.getToken()}` },
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body ? `[${body.code}] ${body.message}` : `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `audit_${Date.now()}.${fmt === 'csv' ? 'csv' : 'md'}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    ui.toast(`已导出 ${fmt.toUpperCase()}`, 'ok');
  } catch (e) {
    ui.toast(`导出失败：${e.message}`, 'err', 6000);
  }
}

// ==================== 组装 ====================
function buildFilters(dict) {
  const actorOpts = [{ value: '', label: '全部操作人' }]
    .concat((dict.actors || []).map((a) => ({ value: a, label: a })));
  const actionOpts = [{ value: '', label: '全部动作' }]
    .concat((dict.actions || []).map((a) => ({ value: a, label: a })));
  const targetOpts = [{ value: '', label: '全部目标类型' }]
    .concat((dict.target_types || []).map((t) => ({ value: t, label: t })));

  const fActor = ui.selectBox(actorOpts, state.actor, (v) => { state.actor = v; state.page = 1; load(); });
  const fAction = ui.selectBox(actionOpts, state.action, (v) => { state.action = v; state.page = 1; load(); });
  const fTarget = ui.selectBox(targetOpts, state.targetType, (v) => { state.targetType = v; state.page = 1; load(); });
  const fRange = ui.selectBox(TIME_RANGES.map((r) => ({ value: r.value, label: r.label })),
    state.range, (v) => { state.range = v; state.page = 1; load(); });
  return [fActor, fAction, fTarget, fRange];
}

export async function render(container) {
  refs = {};
  verifyHolder = ui.h('div');
  refs.tableWrap = ui.h('div');
  refs.pageInfo = ui.h('span');
  refs.btnPrev = ui.button('上一页', { onClick: () => { if (state.page > 1) { state.page -= 1; load(); } } });
  refs.btnNext = ui.button('下一页', { onClick: () => { state.page += 1; load(); } });

  let dict = { actors: [], actions: [], target_types: [] };
  try {
    dict = await api.get(`${BASE}/actors`, null, null);
  } catch (e) { /* 字典拉不到时筛选器退化为空选项，不影响看流水 */ }

  ui.mount(container, ui.h('div', { class: 'view' }, [
    verifyHolder,
    ui.card(null, [
      ui.toolbar(
        buildFilters(dict),
        [
          ui.button('导出 CSV', { onClick: () => doExport('csv') }),
          ui.button('导出 Markdown', { onClick: () => doExport('markdown') }),
          ui.button('刷新', { onClick: () => { load(); redraw(); } }),
        ],
      ),
      refs.tableWrap,
      ui.h('div', { class: 'pager' }, [refs.pageInfo, refs.btnPrev, refs.btnNext]),
      ui.h('div', { class: 'hint', text:
        '审计日志为只读，任何角色都不可修改或删除；规则/名单变更必须记录 before/after 快照。' }),
    ]),
  ]));

  redraw();
  await load();
}

/** 只重绘校验卡片（重绘整表会丢失展开状态与滚动位置）。 */
function redraw() {
  if (!verifyHolder) return;
  ui.mount(verifyHolder, renderVerifyCard());
}
