/* 模块 07 · 左栏「案件列表」（页面 `#/cases` 上**新增**的那一栏）
 *
 * ## 为什么是一个独立文件
 *
 * `#/cases` 这个页面已经被 09（中栏画像卡 + 图谱）与 05（右栏判定摘要 + 命中明细）
 * "寄生"过（决策 D48）。本文件是模块 07 补上的**左栏**，与那两个模块**零耦合**：
 * 它只负责「筛选 → 列表 → 分页 → 认领」，一行 09/05 的渲染都不碰、一个类名都不改。
 *
 * ## 三条硬约束（都在这里落地）
 *
 * 1. **顶部「共 N 条待审」的 N 必须取列表接口的 `total`**（BR-07-05b）。
 *    本文件里没有任何一处用 `items.length` 去算这个数——页面上那个 N 只有一个来源，
 *    就是 `data.total`。越界页（`page` 超过总页数）时接口按 §3.4 返回空 `items` +
 *    正确的 `total`，此时"页面 0 行但显示共 N 条"是**正确行为**，不是 bug。
 * 2. **不自己造后端**：`GET /api/v1/cases` 由模块 07 的后端并行交付。接口还没上线时
 *    （404 / `COM-4004`）页面必须**如实说"接口尚未提供"**，而不是渲染一个空列表让
 *    人误以为"系统里没有案件"。
 * 3. **认领走 08 的流程**（`openClaimConfirm`，BR-08-04 的原子认领 + 幂等）——
 *    本模块不直接调 `/claim`（BR-07-21 同源：07 只收集与提交，不执行）。
 *
 * ## 已知取舍（写在代码里，便于复核）
 *
 * - 状态文案用 `/common/enums` 的 `case_status`（BR-00-18：不硬编码枚举），**只有
 *   `pending` 一个值被覆写成「待审」**——Spec 07 §2.2.1 与原型 `wbFilterR` 的原文是
 *   「共 N 条待审」，枚举里的标签是「待审核」。二者只差一个字，但断言与原型对齐时
 *   这个字是会被人肉核对的，所以按原型写，并在 `statusLabel` 里注明。
 * - 时间筛选项**不提供自由文本**（Spec §2.1：避免与 `keyword` 检索混淆），但保留
 *   「自定义区间」的两个 `datetime-local`（原型 `fTime` 明确有这一项）。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as session from '../session.js';
import { ensureEnums, labelOf } from '../store.js';
import { openClaimConfirm } from './case_dispose.js';

/** 每页固定 20 条（BR-07-04：页面写死，不提供切换）。 */
export const PAGE_SIZE = 20;
/** 时间区间上限跨度（Spec §2.1 `fTime` + `CASE-4004`）。 */
export const MAX_RANGE_DAYS = 90;
export const STATUSES = ['pending', 'reviewing', 'disposed', 'archived'];
/** Spec §2.1 `fLevel`：只有中/高风险可筛（`low` 不在案件取值域里）。 */
export const RISK_LEVELS = ['medium', 'high'];
export const TIME_PRESETS = [
  { value: '', label: '全部' },
  { value: '1h', label: '近 1 小时', ms: 3600 * 1000 },
  { value: '24h', label: '近 24 小时', ms: 24 * 3600 * 1000 },
  { value: '7d', label: '近 7 天', ms: 7 * 24 * 3600 * 1000 },
  { value: 'custom', label: '自定义区间' },
];

/** 状态标签：枚举为源，`pending` 按 Spec §2.2.1 / 原型原文覆写为「待审」。
 *  其它取值优先用**服务端给的 `status_label`**（BR-00-18：中文只在一处真源）。 */
export function statusLabel(v, serverLabel) {
  const key = String(v || '');
  if (key === 'pending') return '待审';
  return serverLabel || labelOf('case_status', key, key || '—');
}

/** 状态徽标配色：待审=橙（要看）、审核中=品牌色（有人在办）、已处置=绿、已归档=灰。 */
const STATUS_CLS = { pending: 'medium', reviewing: 'brand', disposed: 'low', archived: 'plain' };
export function statusTag(v, serverLabel) {
  return ui.tag(statusLabel(v, serverLabel), STATUS_CLS[String(v)] || 'plain');
}

/** 风险分配色：≥80 红 / 60~79 橙 / <60 绿（与右栏判定摘要同一套语义色）。 */
export function riskScoreCls(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return 'case-score-low';
  if (n >= 80) return 'case-score-high';
  if (n >= 60) return 'case-score-medium';
  return 'case-score-low';
}

/** 列表查询（**只走 api.js**，不自己 fetch；错误由本区块自己渲染，故 silent）。 */
export function fetchCases(query) {
  return api.request(`${api.API_PREFIX}/cases`, { query, silent: true });
}

/** 把筛选状态翻译成接口 query（`fields` 之间 AND；空值不发）。 */
export function buildQuery(state) {
  const q = { page: Math.max(1, Number(state.page) || 1), page_size: PAGE_SIZE };
  if (state.risk_level) q.risk_level = state.risk_level;
  if (state.scene_code) q.scene_code = state.scene_code;
  if (state.status) q.status = state.status;
  if (state.keyword) q.keyword = state.keyword;
  if (state.created_from) q.created_from = String(state.created_from);
  if (state.created_to) q.created_to = String(state.created_to);
  return q;
}

const isMissingApi = (e) => e && (e.status === 404 || e.code === 'COM-4004' || e.code === 'COM-5000');

/** 打开用户画像新页签（Spec §2.2.1：可点击，且**不改变**当前三栏选中）。 */
function userLink(userId) {
  const a = ui.h('a', {
    class: 'mono case-user-link',
    href: `#/cases?user=${encodeURIComponent(String(userId || ''))}`,
    target: '_blank',
    rel: 'noopener',
    title: '在新页签查看该用户画像与图谱（当前三栏选中不变）',
    text: userId || '—',
  });
  return a;
}

/**
 * 左栏组件工厂。
 *
 * @param {object} cfg
 *   · `getState()`       → 当前筛选状态（真源在 hash，由 profile.js 提供）
 *   · `go(patch)`        → 改写 hash（筛选变化 / 翻页 / 选中案件都走它）
 *   · `getSelected()`    → 当前选中的案件号
 *   · `onChanged()`      → 列表自身无法处理的状态变化（如"退出案件模式"）时的回调
 * @returns {{el: Node, load: Function, setSelected: Function, selectedRow: Function}}
 */
export function createCaseList(cfg = {}) {
  const refs = {
    count: ui.h('div', { class: 'case-count', dataset: { role: 'case-count' } }),
    asOf: ui.h('div', { class: 'case-asof hint' }),
    err: ui.h('div', { class: 'case-error' }),
    body: ui.h('div', { class: 'case-table' }),
    pageInfo: ui.h('span', { class: 'case-page-info' }),
    prev: ui.button('上一页', { onClick: () => gotoPage((cfg.getState().page || 1) - 1) }),
    next: ui.button('下一页', { onClick: () => gotoPage((cfg.getState().page || 1) + 1) }),
    sel: ui.h('div', { class: 'case-selected', dataset: { role: 'case-selected' } }),
    custom: ui.h('div', { class: 'case-custom-range' }),
  };

  // 上一次成功的结果：失败/翻页时**不得清空**（Spec §3.4 失败态：保留旧列表 + 错误条）
  let last = { items: [], total: 0, pages: 0, as_of: null };
  let abort = null;
  let seq = 0;

  // ---------------- 筛选区 ----------------
  function patch(p) {
    // BR-07-05：任一筛选变化 → page 重置为 1（翻页除外，翻页自己带 page）
    const next = Object.assign({}, p);
    if (!('page' in next)) next.page = 1;
    cfg.go(next);
  }

  function optionList(group, values, allLabel) {
    const opts = [{ value: '', label: allLabel }];
    values.forEach((v) => opts.push({ value: v, label: labelOf(group, v, v) }));
    return opts;
  }

  const fLevel = ui.selectBox(optionList('risk_level', RISK_LEVELS, '风险等级：全部'),
    cfg.getState().risk_level, (v) => patch({ risk_level: v }));
  const fStatus = ui.selectBox(optionList('case_status', STATUSES, '审核状态：全部'),
    cfg.getState().status, (v) => patch({ status: v }));
  const fTime = ui.selectBox(TIME_PRESETS.map((t) => ({ value: t.value, label: `触发时间：${t.label}` })),
    cfg.getState().time || '', (v) => applyPreset(v));

  // 事件场景：Spec §3.5 说选项来自 `GET /api/v1/rule-scenes`，但**该路由当前不存在**
  // （实测 404 `COM-4004`）。改用 `/common/enums` 的 `rule_scenes` 组——它由后端
  // `enums.py` 统一维护（BR-00-18：前后端单一来源），取值与场景码完全一致（login/
  // coupon/order/pay/aftersale/common）。这样既没有硬编码 4 个场景，也没有为一个
  // 下拉框去造一个后端接口。枚举是异步缓存的（app.js 登录后 `ensureEnums()`），
  // 因此先把下拉建出来（只有「全部」），加载完成后再回填选项。
  const fScene = ui.selectBox([{ value: '', label: '事件场景：全部' }],
    cfg.getState().scene_code, (v) => patch({ scene_code: v }));
  ensureEnums().then((all) => {
    const list = (all && all.rule_scenes) || [];
    if (!list.length) return;
    const cur = String(cfg.getState().scene_code || '');
    ui.mount(fScene, [{ value: '', label: '事件场景：全部' }].concat(list).map((o) => ui.h('option', {
      value: o.value, text: o.label,
      selected: String(o.value) === cur ? 'selected' : null,
    })));
  }).catch(() => { /* 枚举拿不到就只剩「全部」，不影响列表本身 */ });

  function applyPreset(v) {
    const preset = TIME_PRESETS.find((t) => t.value === v);
    if (!preset) return;
    if (v === 'custom') {
      // 自定义：把下拉切过去并展开两个 datetime-local，**不改 query**（等用户点「应用」）
      patch({ time: 'custom' });
      return;
    }
    if (!preset.ms) { patch({ time: null, created_from: null, created_to: null }); return; }
    const now = Date.now();
    patch({ time: v, created_from: String(now - preset.ms), created_to: String(now) });
  }

  const fromI = ui.h('input', { type: 'datetime-local', class: 'case-time-input' });
  const toI = ui.h('input', { type: 'datetime-local', class: 'case-time-input' });
  const applyRange = ui.button('应用区间', { variant: 'primary', onClick: () => {
    const f = fromI.value ? new Date(fromI.value).getTime() : null;
    const t = toI.value ? new Date(toI.value).getTime() : null;
    if (!f || !t) { ui.toast('自定义区间需要同时填写起止时间', 'err'); return; }
    if (f > t) { ui.toast('触发时间的开始不能晚于结束', 'err'); return; }
    // 上限跨度 90 天（Spec §2.1；服务端同样会以 CASE-4004 拒绝）
    if (t - f > MAX_RANGE_DAYS * 24 * 3600 * 1000) {
      ui.toast(`触发时间区间上限跨度为 ${MAX_RANGE_DAYS} 天`, 'err');
      return;
    }
    patch({ time: 'custom', created_from: String(f), created_to: String(t) });
  } });
  ui.mount(refs.custom, [
    ui.h('span', { class: 'hint', text: '自定义区间（含头含尾）' }), fromI,
    ui.h('span', { class: 'hint', text: '至' }), toI, applyRange,
  ]);

  const refreshBtn = ui.button('刷新', { onClick: () => load() });
  refreshBtn.dataset.role = 'case-refresh';
  const resetBtn = ui.button('重置', { onClick: () => {
    // 清空全部筛选并回到 page=1；**保留 `case`**（Spec §2.1：刷新保留选中案件）
    patch({ risk_level: null, scene_code: null, status: null, time: null,
      created_from: null, created_to: null, keyword: null, page: 1 });
  } });

  const filterBar = ui.h('div', { class: 'case-filters' }, [
    ui.h('div', { class: 'case-filter-row' }, [fLevel, fScene, fStatus, fTime]),
    refs.custom,
  ]);
  refs.custom.classList.toggle('on', (cfg.getState().time || '') === 'custom');
  applyStateToInputs();

  const head = ui.h('div', { class: 'case-head' }, [
    ui.h('div', { class: 'case-count-row' }, [refs.count, refs.asOf]),
    ui.h('div', { class: 'case-actions' }, [refreshBtn, resetBtn]),
  ]);

  const el = ui.card('案件列表', [
    filterBar, head, refs.err, refs.sel, refs.body,
    ui.h('div', { class: 'pager' }, [
      refs.pageInfo, refs.prev, refs.next,
      ui.h('span', { class: 'hint', text: `每页固定 ${PAGE_SIZE} 条（BR-07-04）` }),
    ]),
  ]);
  el.classList.add('case-list-card');

  function applyStateToInputs() {
    const st = cfg.getState();
    if (st.created_from) {
      const d = new Date(Number(st.created_from));
      if (!Number.isNaN(d.getTime())) fromI.value = localInput(d);
    }
    if (st.created_to) {
      const d = new Date(Number(st.created_to));
      if (!Number.isNaN(d.getTime())) toI.value = localInput(d);
    }
  }

  function gotoPage(n) {
    const st = cfg.getState();
    const pages = last.pages || (last.total ? Math.ceil(last.total / PAGE_SIZE) : 0);
    if (n < 1) return;
    if (pages && n > pages) return;
    if (n === (st.page || 1)) return;
    cfg.go({ page: n });
  }

  // ---------------- 渲染 ----------------
  function rowOf(it) {
    const selected = String(it.case_no) === String(cfg.getSelected() || '');
    const tr = ui.h('tr', {
      class: 'case-row' + (selected ? ' is-selected' : ''),
      dataset: {
        case: String(it.case_no || ''), status: String(it.status || ''),
        assignee: String(it.assignee || ''), role: 'case-row',
      },
      // 行尾的两个补充字段按 Spec §2.2.1 做 **hover 提示**，不占列宽
      title: `${it.scene_name || it.scene_code || '—'} · 触发于 ${ui.fmtTime(it.created_at)}`
        + (it.assignee ? ` · 认领人 ${it.assignee}` : ''),
      onclick: () => select(it.case_no),
    }, [
      ui.h('td', { class: 'mono case-no', text: it.case_no || '—' }),
      ui.h('td', {}, userLink(it.user_id)),
      ui.h('td', { class: `case-score ${riskScoreCls(it.risk_score)}`,
        text: it.risk_score === null || it.risk_score === undefined ? '—' : String(it.risk_score) }),
      ui.h('td', {}, [statusTag(it.status, it.status_label),
        // 已认领的案件把认领人放在状态格里（Spec §2.2.1 只允许 4 列，认领人不是列）
        it.assignee ? ui.h('div', { class: 'case-assignee hint', text: it.assignee }) : null]),
    ]);
    return tr;
  }

  function renderBody(items) {
    if (!items.length) {
      const st = cfg.getState();
      const filtered = !!(st.risk_level || st.scene_code || st.status || st.created_from || st.created_to || st.keyword);
      const total = Number(last.total) || 0;
      let node;
      if (total > 0) {
        // 越界页（Spec §3.4：`page` 超过总页数返回空 items + 正确 total，不报错）
        const back = ui.button('回到第 1 页', { variant: 'primary', onClick: () => cfg.go({ page: 1 }) });
        node = ui.empty(`本页无案件（共 ${total} 条）`,
          `第 ${st.page || 1} 页超出总页数，接口按契约返回空列表而不是报错`);
        node.appendChild(ui.h('div', { class: 'form-actions' }, [back]));
      } else if (filtered) {
        const clear = ui.button('清空筛选', { variant: 'ghost', onClick: () => {
          cfg.go({ risk_level: null, scene_code: null, status: null, time: null,
            created_from: null, created_to: null, keyword: null, page: 1 });
        } });
        node = ui.empty('当前筛选条件下无案件');
        node.appendChild(ui.h('div', { class: 'form-actions' }, [clear]));
      } else {
        node = ui.empty('暂无风控案件', '案件由 05 判定为 review / reject 后经 08 落库（pass 不建案）');
      }
      ui.mount(refs.body, node);
      return;
    }
    const table = ui.table(
      [{ name: '案件号' }, { name: '用户' }, { name: '风险分' }, { name: '状态' }],
      items.map(rowOf));
    table.classList.add('case-tbl');
    table.dataset.role = 'case-table';
    ui.mount(refs.body, table);
  }

  function renderCount() {
    const st = cfg.getState();
    const total = Number(last.total) || 0;
    // BR-07-05b：**唯一**来源是接口的 `total`；`pending` 时按原型文案说「待审」
    const noun = st.status === 'pending' ? '待审' : '案件';
    refs.count.textContent = `共 ${total} 条${noun}`;
    refs.count.dataset.total = String(total);
    const asOf = Number(last.as_of);
    refs.asOf.textContent = Number.isFinite(asOf) && asOf > 0
      ? `数据截至 ${new Date(asOf).toLocaleTimeString('zh-CN', { hour12: false })}` : '';
  }

  function renderPager() {
    const st = cfg.getState();
    const pages = last.pages || (last.total ? Math.ceil(last.total / PAGE_SIZE) : 0);
    refs.pageInfo.textContent = `${st.page || 1} / ${Math.max(1, pages)}`;
    refs.prev.disabled = (st.page || 1) <= 1;
    refs.next.disabled = pages === 0 || (st.page || 1) >= pages;
  }

  /** 选中条：案件号 / 状态 / 认领人 / 认领时长 + 认领按钮（BR-07-18）。 */
  function renderSelected() {
    const no = cfg.getSelected();
    if (!no) {
      ui.mount(refs.sel, ui.h('div', { class: 'hint' },
        '点左侧任一案件行 → 中栏与右栏由**同一次** `GET /cases/{case_no}` 一起刷新（BR-07-06）。'));
      return;
    }
    const it = last.items.find((x) => String(x.case_no) === String(no)) || selectedDetail || null;
    const status = it ? it.status : '';
    const assignee = it ? it.assignee : '';
    const me = (session.getUser() || {}).username || '';
    const nodes = [
      ui.h('span', { class: 'hint', text: '当前案件' }),
      ui.h('span', { class: 'mono case-sel-no', text: no }),
      status ? statusTag(status, it.status_label) : null,
    ];

    const canDispose = (session.permissions() || []).includes('case:dispose');
    if (status === 'pending') {
      const btn = ui.button('认领', { variant: 'primary', onClick: () => claim(no) });
      btn.dataset.role = 'case-claim';
      if (!canDispose) { btn.disabled = true; btn.title = '当前角色无 case:dispose 权限（矩阵里仅审核员，D69）'; }
      nodes.push(btn);
      nodes.push(ui.h('span', { class: 'hint', text: '认领后案件进入「审核中」，处置区解除禁用（BR-07-13/17）。' }));
    } else if (status === 'reviewing') {
      const mine = assignee && me && assignee === me;
      const btn = ui.button(mine ? '已认领（我）' : '已被他人认领', { disabled: true });
      btn.dataset.role = 'case-claim';
      btn.title = mine ? '你已认领该案件（幂等：重复认领会返回 changed=false）'
        : `案件已被 ${assignee || '他人'} 认领，请等待超时回收（BR-07-18）`;
      nodes.push(btn);
      nodes.push(ui.h('span', { class: 'hint', text: claimedText(it) }));
    } else if (status === 'disposed' || status === 'archived') {
      nodes.push(ui.h('span', { class: 'hint', text:
        status === 'disposed' ? '该案件已处置，不可重复提交（处置区只读回显）' : '该案件已归档（处置区只读回显）' }));
    }
    if (!it) nodes.push(ui.h('span', { class: 'hint', text: '（该案件不在本页列表里，状态以详情为准）' }));
    const exit = ui.button('退出案件模式', { variant: 'ghost', onClick: () => cfg.go({ case: null }) });
    exit.dataset.role = 'case-exit';
    exit.classList.add('btn-sm');
    nodes.push(exit);
    ui.mount(refs.sel, nodes);
  }

  /** BR-07-16：`reviewing` 案件展示「已由 {assignee} 认领，已认领 N 分钟」。 */
  function claimedText(it) {
    if (!it || !it.assignee) return '';
    const at = Number(it.claimed_at);
    if (!Number.isFinite(at) || at <= 0) return `已由 ${it.assignee} 认领`;
    const mins = Math.max(0, Math.floor((Date.now() - at) / 60000));
    return `已由 ${it.assignee} 认领，已认领 ${mins} 分钟（超时回收由 08 负责）`;
  }

  function claim(caseNo) {
    openClaimConfirm(caseNo, {
      onDone: () => { if (cfg.onClaimed) cfg.onClaimed(caseNo); },
      onError: (e) => {
        // BR-07-14：并发认领被拒后必须**刷新该案件**并把服务端文案原样带出来
        ui.toast((e && e.message) || '认领失败', 'err', 6000);
        if (cfg.onClaimed) cfg.onClaimed(caseNo);
      },
    });
  }

  function select(caseNo) {
    const no = String(caseNo || '');
    if (!no || no === String(cfg.getSelected() || '')) return;   // 点同一行不重发请求
    cfg.go({ case: no });
  }

  // ---------------- 取数 ----------------
  let selectedDetail = null;

  async function load() {
    if (abort) abort.abort();
    abort = new AbortController();
    const { signal } = abort;
    const my = ++seq;
    const st = cfg.getState();
    ui.clear(refs.err);

    const firstLoad = !last.items.length;
    if (firstLoad) ui.mount(refs.body, ui.loading(7));   // 首次骨架行 ×7（Spec §2.2.1）
    else refs.body.classList.add('is-busy');              // 切筛选/翻页：遮罩 + 保留旧列表

    try {
      const data = await api.request(`${api.API_PREFIX}/cases`, {
        query: buildQuery(st), signal, silent: true,
      });
      if (signal.aborted || my !== seq) return;   // 乱序响应丢弃（Spec §3.4）
      last = {
        items: Array.isArray(data && data.items) ? data.items : [],
        total: Number(data && data.total) || 0,
        pages: Number(data && data.pages) || 0,
        as_of: data && data.as_of,
      };
      if (last.items.length && !last.pages) last.pages = Math.ceil(last.total / PAGE_SIZE);
      renderBody(last.items);
      renderCount();
      renderPager();
      renderSelected();
      if (cfg.onLoaded) cfg.onLoaded(last);
    } catch (e) {
      if (e.name === 'AbortError' || signal.aborted || my !== seq) return;
      // 失败态（Spec §3.4）：**保留旧列表** + 顶部错误条 + 重试
      if (firstLoad) ui.mount(refs.body, ui.empty('案件列表暂不可用', '接口未返回数据，见下方错误条'));
      const retry = ui.button('重试', { variant: 'ghost', onClick: () => load() });
      ui.mount(refs.err, isMissingApi(e)
        ? [ui.banner('warn', `案件列表接口尚未提供（GET /api/v1/cases）：${e.message}（${e.code || 'HTTP ' + e.status}）`),
          ui.h('div', { class: 'form-actions' }, [retry])]
        : [ui.banner('err', `案件列表加载失败：${e.message}（trace ${e.shortTrace ? e.shortTrace() : '—'}）`),
          ui.h('div', { class: 'form-actions' }, [retry])]);
      renderCount();
      renderPager();
    } finally {
      if (!signal.aborted && my === seq) refs.body.classList.remove('is-busy');
    }
  }

  /** 由 profile.js 在**详情**到达后回调：条上状态以详情为准（BR-07-22 不许本地推测）。 */
  function setSelected(caseNo, detail) {
    selectedDetail = detail
      ? { case_no: detail.case_no || caseNo, status: detail.status, status_label: detail.status_label,
        assignee: detail.assignee, claimed_at: detail.claimed_at }
      : null;
    renderSelected();
  }

  function selectedRow() {
    return last.items.find((x) => String(x.case_no) === String(cfg.getSelected() || '')) || null;
  }

  return { el, load, setSelected, selectedRow, refreshCount: renderCount,
    getItems: () => last.items };
}

/** `Date` → `datetime-local` 需要的本地时间字符串（不能用 toISOString：那是 UTC）。 */
function localInput(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default { createCaseList, fetchCases, buildQuery, statusLabel, statusTag, PAGE_SIZE };
