/* 态势大盘页（模块 02 §2 / §4，对应原型 02_风控态势大盘页.pen）
 *
 * ## 只展示，不计算（BR-02-01，本模块最重要的一条边界）
 * 页面里**没有任何除法、均值、排序**：4 张卡片的数值、拦截率、占比、排行榜顺序全部由
 * 模块 11 算好下发。前端只做三件事——格式化（`format.js`）、渲染、跳转。
 * 卡片 3 用的是 `cards.pending_case_cnt`（BR-11-17 的"当前积压"gauge），而不是同名的
 * `cards.review_cnt`（那是**本区间的人审事件数**）——两个口径共用一个标签是最难发现的错。
 *
 * ## 区块级隔离（BR-02-11/12）
 * 五个区块各有自己的取数与状态：卡片 /overview、趋势 /trend、环形 /distribution、
 * 排行 /rule-ranking、事件流 SSE。任一失败只在**自己的容器**里显示「加载失败 + 重试」。
 * 因此这里**刻意不用 Promise.all**：一荣俱荣的编排会让一个 500 把整页打成错误页。
 *
 * ## 刷新节奏（BR-02-05/06/08）
 * 一个 5 秒定时器驱动四个轮询区块，手动「刷新」与它**共用同一个取数函数** `refreshAll()`；
 * 卡片与图表不走 SSE。撞到限流（COM-4290，60 次/分）时把间隔翻倍退避——4 个区块 × 12 次/分
 * 已经逼近额度上限，退避比"继续硬打、把整页刷成 429"诚实。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as fmt from '../format.js';
import { ensureEnums, labelOf } from '../store.js';
import { createStream } from '../stream.js';
import * as chart from '../charts/echarts.js';
import { buildTrendOption, trendCaption, isEmptyTrend } from '../charts/trend.js';
import { buildDoughnutOption, doughnutCaption, isEmptyDistribution } from '../charts/doughnut.js';
import { buildRankingOption, rankingCaption, isEmptyRanking } from '../charts/ranking.js';
import { findRoute } from './registry.js';

export const meta = { title: '态势大盘', crumb: '风控中台 / 态势大盘' };

const BASE = `${api.API_PREFIX}/metrics`;

const RANGES = [
  { value: '1h', label: '近 1 小时' },
  { value: '24h', label: '近 24 小时' },
  { value: '7d', label: '近 7 天' },
];
const LEVELS = [
  { value: '', label: '全部风险等级' },
  { value: 'low', label: '低风险' },
  { value: 'medium', label: '中风险' },
  { value: 'high', label: '高风险' },
];

/**
 * 场景筛选的可用取值。
 *
 * 选项来自 `/common/enums` 的 `rule_scenes`（BR-00-18：中文标签不硬编码），
 * 但必须按模块 11 `require_scene` 的白名单过滤：`rule_scenes` 里的 `common`（通用）
 * 是**规则**的场景码，指标接口不认它，选中会让四个接口一起返回 `MET-4004`。
 * 这里只过滤"哪些取值可用"，标签一律取服务端下发的 label。
 */
const METRIC_SCENES = ['login', 'coupon', 'order', 'pay', 'aftersale'];

const REFRESH_MS = 5000;          // BR-02-05
const MAX_REFRESH_MS = 60000;     // 限流退避上限
const STREAM_KEEP = 50;           // BR-02-09

/** 视图态（筛选是临时视图状态，规格未要求可分享，故不进 hash）。 */
const state = { range: '24h', scene: '', level: '', auto: true, interval: REFRESH_MS };

let refs = null;
let mounted = false;
let timer = null;
let sceneOptions = [];
/**
 * 挂载序号。
 *
 * **必须存在**：壳在登录成功时会渲染两次 `#/dashboard`（`startSession()` 自己 `await navigate()`，
 * 同时 `location.hash = DEFAULT_HASH` 又触发一次 hashchange → 又一次 `navigate()`）。
 * 两次 `render()` 并发跑到 `await ensureEnums()` 处交错，若没有序号，先来的那次会在
 * 后来的那次已经接管之后继续把 SSE 与定时器建起来——模块级 `stream`/`timer` 变量被后
 * 一次覆盖，前一条连接就**永远没人关**（实测：`liveStreamCount()` 停在 2，切走后剩 1）。
 * 有了序号，过期的那次 render 在每个 await 之后都会自行放弃。
 */
let mountSeq = 0;

/** 四个轮询区块：一个区块 = 一次取数 + 一份自己的状态。 */
const blocks = {
  cards: { name: 'cards', path: '/overview', label: '指标卡片', seq: 0, abort: null, status: 'idle', data: null, error: null },
  trend: { name: 'trend', path: '/trend', label: '拦截率趋势', seq: 0, abort: null, status: 'idle', data: null, error: null },
  doughnut: { name: 'doughnut', path: '/distribution', label: '风险等级分布', seq: 0, abort: null, status: 'idle', data: null, error: null },
  ranking: { name: 'ranking', path: '/rule-ranking', label: '规则命中排行', seq: 0, abort: null, status: 'idle', data: null, error: null },
};

/** 图表实例槽（重绘前必须 dispose，BR-02-20）。 */
const charts = { trend: null, doughnut: null, ranking: null };

/** 事件流：内存最多 50 条（BR-02-09），最新在上。 */
const streamData = { rows: [], state: 'connecting', reason: '', reconnects: 0, pinned: true, everOpen: false };
let stream = null;

// ==================== 小工具 ====================

/** trace 后 6 位；错误处理本身绝不能再抛错。 */
function traceOf(e) {
  return (e && typeof e.shortTrace === 'function') ? e.shortTrace() : '—';
}

function rangeLabel() {
  const hit = RANGES.find((r) => r.value === state.range);
  return hit ? hit.label : state.range;
}

function isMounted() {
  return mounted && !!refs && document.contains(refs.root);
}

/** 三个筛选器共用一份查询参数。 */
function baseQuery() {
  const q = { range: state.range };
  if (state.scene) q.scene = state.scene;
  if (state.level) q.level = state.level;
  return q;
}

/** 「重试」按钮：闭包捕获区块，事件对象绝不会被当成参数传下去。 */
function retryBox(block, detail) {
  return ui.h('div', { class: 'db-error', dataset: { block: block.name } }, [
    ui.h('div', { class: 'db-error-title', text: `${block.label}加载失败` }),
    ui.h('div', { class: 'hint', text: detail }),
    ui.button('重试', { variant: 'ghost', onClick: () => loadBlock(block) }),
  ]);
}

/** 区块级黄色提示（BR-02-13：`stale=true` 不隐藏，只在旁边说明）。 */
function staleNote(data) {
  if (!data || data.stale !== true) return null;
  return ui.h('div', { class: 'db-stale', dataset: { block: 'stale' }, text:
    `数据来自降级路径，仅供参考（快照时间 ${fmt.dateTime(data.stale_at, '未知')}）` });
}

const isPending = (b) => (b.status === 'idle' || b.status === 'loading') && !b.data;

// ==================== ① 指标卡片 ====================

function statCardBlock({ key, title, value, unit, caption, hot = false, drill = null }) {
  const props = {
    class: 'stat-card db-card' + (drill ? ' is-drill' : ''),
    // data-card 用**接口字段名**而不是中文标题：E2E 与人工排查都靠它定位，
    // 字段名是契约的一部分（改标题不该让断言失效，改字段才该）
    dataset: { card: key },
    title: drill ? '点击下钻查看明细' : '',
  };
  if (drill) {
    // 用 role=button 而不是 <button>：卡片是块级排版，套 button 会继承 40px 控件行高
    props.role = 'button';
    props.tabindex = '0';
    props.onclick = () => go(drill);
    props.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(drill); } };
  }
  return ui.h('div', props, [
    ui.h('div', { class: 'stat-title', text: title }),
    ui.h('div', { class: 'stat-value' + (hot ? ' is-hot' : '') }, [
      ui.h('span', { class: 'db-num', text: value }),
      unit ? ui.h('span', { class: 'stat-unit', text: unit }) : null,
    ]),
    ui.h('div', { class: 'db-caption', text: caption }),
  ]);
}

function renderCards() {
  const block = blocks.cards;
  if (isPending(block)) {
    ui.mount(refs.cards, ui.h('div', { class: 'db-cards' }, Array.from({ length: 4 }, () =>
      ui.h('div', { class: 'stat-card db-card' }, ui.loading(2)))));
    return;
  }
  if (block.status === 'error') {
    ui.mount(refs.cards, retryBox(block, `${block.error.message}（trace ${traceOf(block.error)}）`));
    return;
  }
  const d = block.data || {};
  const cards = d.cards || {};
  const gran = d.granularity || '—';
  const blockRate = cards.block_rate;
  // Spec §2.2：拦截率 > 10% 时数值变红（异常提示，不是为了好看）
  const rateHot = fmt.isNum(blockRate) && Number(blockRate) > 10;
  const caption = (field, extra = '') =>
    `来源 metric_buckets.${field} · ${rangeLabel()}（粒度 ${gran}）${extra}`;

  ui.mount(refs.cards, [
    staleNote(d),
    ui.h('div', { class: 'db-cards' }, [
      statCardBlock({
        key: 'event_cnt', title: '实时事件请求量', value: fmt.int(cards.event_cnt), unit: '次',
        caption: caption('event_cnt'),
      }),
      statCardBlock({
        key: 'block_rate', title: '拦截率', value: fmt.fixed(blockRate), unit: '%', hot: rateHot,
        caption: caption('block_rate', fmt.isNum(blockRate) ? '' : '（本区间无样本，分母为 0）'),
      }),
      statCardBlock({
        key: 'pending_case_cnt', title: '待审案件数', value: fmt.int(cards.pending_case_cnt), unit: '件',
        caption: '当前积压 gauge（不随区间变化）· 点击进入待审队列',
        drill: { hash: '#/cases', query: { status: 'pending' } },
      }),
      statCardBlock({
        key: 'estimated_saved_amount', title: '预估挽回资损',
        value: fmt.yuan(cards.estimated_saved_amount), unit: '元',
        caption: 'Σ 被 reject 的订单金额（接口给「分」，前端转元）· 点击查看拦截记录',
        drill: { hash: '#/cases', query: { decision: 'reject' } },
      }),
    ]),
    ui.h('div', { class: 'hint', text:
      '口径：全部数值与比率由模块 11 计算后下发，页面不做任何二次计算（BR-02-01）；' +
      `区间 ${rangeLabel()}，粒度 ${gran} 由后端决定（BR-02-04）。` }),
  ]);
}

// ==================== ②③④ 三张图表 ====================

/** 图表区块外壳（三块结构一致，故共用）。 */
function chartCardInner(block, { title, caption, canDraw, emptyText, emptyHint }) {
  if (isPending(block)) {
    return ui.card(title, [ui.loading(4), ui.h('div', { class: 'hint', text: '取数中…' })]);
  }
  if (block.status === 'error') {
    return ui.card(title, [retryBox(block, `${block.error.message}（trace ${traceOf(block.error)}）`)]);
  }
  return ui.card(title, [
    staleNote(block.data),
    canDraw
      ? ui.h('div', { class: 'db-chart', dataset: { chart: block.name } })
      : ui.h('div', { class: 'db-empty' }, ui.empty(emptyText, emptyHint)),
    ui.h('div', { class: 'db-caption', text: caption }),
  ]);
}

function renderTrend() {
  const block = blocks.trend;
  const empty = isEmptyTrend(block.data);
  ui.mount(refs.trend, chartCardInner(block, {
    title: '拦截率趋势',
    caption: trendCaption(block.data),
    canDraw: block.status === 'ok' && !empty,
    emptyText: '该时间范围内暂无数据',
    emptyHint: '指标桶缺失（MET-5002）不是错误：该区间内还没有产生任何事件',
  }));
  if (block.status === 'ok' && !empty) {
    drawChart('trend', refs.trend.querySelector('.db-chart'),
      (c) => buildTrendOption(block.data, c));
  } else {
    disposeChart('trend');
  }
}

function renderDoughnut() {
  const block = blocks.doughnut;
  const empty = isEmptyDistribution(block.data);
  ui.mount(refs.doughnut, chartCardInner(block, {
    title: '风险等级分布',
    caption: doughnutCaption(block.data),
    canDraw: block.status === 'ok' && !empty,
    emptyText: '该时间范围内暂无数据',
    emptyHint: '该区间内没有任何决策记录，因此没有等级占比',
  }));
  if (block.status === 'ok' && !empty) {
    drawChart('doughnut', refs.doughnut.querySelector('.db-chart'),
      (c) => buildDoughnutOption(block.data, c));
  } else {
    disposeChart('doughnut');
  }
}

function renderRanking() {
  const block = blocks.ranking;
  const empty = isEmptyRanking(block.data);
  ui.mount(refs.ranking, chartCardInner(block, {
    title: '规则命中排行榜',
    caption: rankingCaption(block.data),
    canDraw: block.status === 'ok' && !empty,
    emptyText: '该时间范围内暂无数据',
    emptyHint: '该区间内没有规则被命中（或命中的规则已被停用）',
  }));
  if (block.status === 'ok' && !empty) {
    drawChart('ranking', refs.ranking.querySelector('.db-chart'),
      (c) => buildRankingOption(block.data, c), (handle) => {
        // BR-02-16：点柱子 → `#/rules?code=<rule_code>`
        handle.on('click', (p) => {
          const items = (block.data && block.data.items) || [];
          const hit = items[p && p.dataIndex];
          if (hit) go({ hash: '#/rules', query: { code: hit.rule_code } });
        });
      });
  } else {
    disposeChart('ranking');
  }
}

function disposeChart(name) {
  const c = charts[name];
  if (c) { c.dispose(); charts[name] = null; }
}

/**
 * 建图。容器必须已挂载（否则 ECharts 首帧算出 0×0 画布且不会自愈），
 * 故调用点全部在 `ui.mount` 之后同步执行——此时容器已在文档里。
 */
function drawChart(name, container, buildOption, wire) {
  if (!container) return;
  disposeChart(name);
  const seq = blocks[name].seq;
  chart.create(container).then((handle) => {
    // 异步加载 ECharts 期间视图可能已卸载/切走/换筛选：必须**丢弃**这次绘制，
    // 否则会在已脱离文档的容器上建实例（画不出来，也永远 dispose 不掉 → V-02-14 漏）
    if (!isMounted() || blocks[name].seq !== seq || !document.contains(container)) {
      handle.dispose();
      return;
    }
    charts[name] = handle;
    handle.setOption(buildOption(chart.palette()));
    if (wire) wire(handle);
  }).catch((e) => {
    // 只降级该图（例如本地 vendor 缺失），其它区块不受影响
    if (!isMounted()) return;
    ui.mount(refs[name], ui.card(blocks[name].label,
      ui.empty('图表渲染失败', String(e && e.message ? e.message : e))));
  });
}

// ==================== ⑤ 实时事件流（SSE） ====================

/**
 * 事件流列宽。
 *
 * ⚠️ 与 Spec §2.4 的（110 / flex / 120 / 80 / 90）**有意不同**：那组宽度是按原型
 * 1440 画布定的，在 1280 视口（本页实测每个区块 470px）下"事件类型"只剩 68px，
 * 实测 `after_sale_apply` / `coupon_receive` 被省略号截成 `after_s…`——枚举值是这一列
 * 唯一的信息，截掉就等于这列不存在（D65：DOM 断言发现不了"数据对但画面没用"）。
 * 现取值 96 / flex / 88 / 52 / 74 = 310px，事件类型稳稳拿到 140px+。
 * 每格仍带 `title`，万一再被截断也能 hover 看全。
 */
const STREAM_COLUMNS = [
  { name: '时间', width: 96 },
  { name: '事件类型' },
  { name: '用户', width: 88 },
  { name: '得分', width: 52 },
  { name: '决策', width: 74 },
];

/**
 * 决策列：**原样展示英文枚举值并着色**（Spec §2.4 逐字规定 `pass` 灰 / `review` 橙 /
 * `reject` 红）。中文标签放进 `title`：列宽只有 74px，而「转人工审核」这类标签比它对应的
 * `review` 宽得多，同一列里中英混排会让列宽被中文撑开、英文行留白。
 */
function decisionCell(decision) {
  const kind = { pass: 'pass', review: 'review', reject: 'reject' }[String(decision)] || '';
  const raw = (decision === null || decision === undefined || decision === '') ? '—' : String(decision);
  const label = labelOf('decision', decision, raw);
  return ui.h('span', { class: `db-decision ${kind}`.trim(), text: raw, title: label });
}

function streamRow(row) {
  return ui.h('tr', { class: 'db-row-new' }, [
    ui.h('td', { class: 'mono', text: fmt.clock(row.ts), title: fmt.dateTime(row.ts) }),
    ui.h('td', { class: 'mono', text: row.event_type || '—', title: row.event_type || '' }),
    ui.h('td', { class: 'mono', text: row.user_id || '—', title: row.user_id || '' }),
    // 分值原样展示（0~100，后端算好）；没有分值就不显示 0
    ui.h('td', { class: 'mono', text: fmt.isNum(row.final_score) ? String(Math.round(row.final_score)) : '—' }),
    ui.h('td', {}, decisionCell(row.decision)),
  ]);
}

function streamStatusText() {
  switch (streamData.state) {
    case 'open': return streamData.rows.length ? '' : '已连接，暂无实时事件';
    case 'connecting': return '连接中…';
    case 'reconnecting': return '实时连接已断开，正在重连…';
    case 'paused': return '页面已隐藏，实时流已暂停（回到本页自动恢复）';
    default: return streamData.reason ? `实时连接已关闭：${streamData.reason}` : '实时连接已关闭';
  }
}

function streamTable() {
  return ui.h('table', {}, [
    ui.h('thead', {}, ui.h('tr', {}, STREAM_COLUMNS.map((c) => {
      const th = ui.h('th', { text: c.name });
      if (c.width) th.style.width = `${c.width}px`;
      return th;
    }))),
    ui.h('tbody', {}, streamData.rows.map(streamRow)),
  ]);
}

/** 全量重绘事件流区块（连接状态变化、筛选变化、首次渲染走这里）。 */
function paintStream() {
  const holder = refs.stream;
  const oldBody = holder.querySelector('.db-stream-body');
  const oldScroll = oldBody ? oldBody.scrollTop : 0;
  // 状态挂在容器上：E2E 与人工排查要能直接读出"这条流现在什么状态"，
  // 而不是靠"表格里有没有行"反推（没有事件与没有连接是两件事）
  holder.dataset.streamState = streamData.state;
  const status = streamStatusText() || '已连接';
  const cls = streamData.state === 'open' ? 'db-stream-state is-open'
    : (streamData.state === 'reconnecting' || streamData.state === 'paused')
      ? 'db-stream-state is-warn' : 'db-stream-state';

  ui.mount(holder, ui.card('实时事件流', [
    ui.h('div', { class: 'db-stream-head' }, [
      ui.h('div', { class: cls }, [
        ui.h('span', { class: `db-dot${streamData.state === 'open' ? ' ok' : ''}` }),
        ui.h('span', { class: 'db-stream-state-text', text: status }),
      ]),
      streamData.reconnects
        ? ui.h('span', { class: 'hint', text:
          `已重连 ${streamData.reconnects} 次（服务端不补发历史，已展示数据不清空）` })
        : null,
    ]),
    streamData.rows.length
      ? ui.h('div', { class: 'db-stream-body' }, streamTable())
      : ui.h('div', { class: 'db-stream-body' }, ui.empty('暂无实时事件',
        '启动「事件仿真」后，这里会按时间倒序实时出现事件')),
    ui.h('div', { class: 'hint', text:
      `SSE /metrics/stream（text/event-stream，服务端心跳 15s）· 最多保留最近 ${STREAM_KEEP} 条（BR-02-09）` }),
  ]));
  // 重绘后恢复滚动位置：连接状态变化（如重连成功）会整块重绘，
  // 若不恢复就会把正在翻旧事件的人拽回顶部（BR-02-10）
  const newBody = holder.querySelector('.db-stream-body');
  if (newBody && oldScroll) newBody.scrollTop = oldScroll;
  streamData.pinned = !newBody || newBody.scrollTop <= 4;
  markNewRows();
}

/** 0.3s 高亮淡出（Spec §2.4）：加类，动画结束后由这里摘掉类。 */
function markNewRows() {
  const holder = refs.stream;
  holder.querySelectorAll('tr.db-row-new').forEach((tr) => {
    setTimeout(() => tr.classList.remove('db-row-new'), 320);
  });
}

/**
 * 增量插入一条事件（最新在上，超出 50 条丢最旧）。
 *
 * 为什么不整块重绘：BR-02-10 要求"用户手动翻阅时不打断阅读"。整块重绘会把
 * `scrollTop` 归零，等于每来一条事件就把人拽回顶部——数据对，但功能是坏的。
 * 因此这里只动 tbody：顶部插一行、尾部删多余行，并按"插入高度"补偿滚动位置。
 */
function pushStreamEvent(payload) {
  if (!payload || payload.missed === true) return;
  streamData.rows.unshift(payload);
  if (streamData.rows.length > STREAM_KEEP) streamData.rows.length = STREAM_KEEP;
  if (!isMounted()) return;

  const body = refs.stream.querySelector('.db-stream-body');
  const tbody = body && body.querySelector('tbody');
  if (!body || !tbody) {   // 空态首次来事件 → 需要从"暂无实时事件"换成表格
    paintStream();
    return;
  }
  const before = body.scrollTop;
  const pinned = streamData.pinned;
  const row = streamRow(payload);
  tbody.insertBefore(row, tbody.firstChild);
  while (tbody.children.length > STREAM_KEEP) tbody.removeChild(tbody.lastChild);
  if (pinned) {
    body.scrollTop = 0;
  } else {
    // 停在旧事件上：新行把内容整体下推，不补偿就会"看着看着跳一行"
    body.scrollTop = before + row.getBoundingClientRect().height;
  }
  markNewRows();
  const statusNode = refs.stream.querySelector('.db-stream-state-text');
  if (statusNode) statusNode.textContent = streamStatusText() || '已连接';
}

function onStreamState(next, info) {
  streamData.state = next;
  streamData.reason = (info && info.reason) || '';
  if (next === 'open' && streamData.everOpen) streamData.reconnects += 1;
  if (next === 'open') streamData.everOpen = true;
  if (isMounted()) paintStream();
}

function startStream() {
  stream = createStream({
    apiPrefix: api.API_PREFIX,
    query: { scene: state.scene, level: state.level },
    onState: onStreamState,
    onEvent: pushStreamEvent,
    onGap: (info) => {
      // 缺口必须说话（服务端环形缓冲已被追平）：不假装数据连续
      ui.toast(`实时流存在缺口（${(info && info.reason) || 'buffer_exhausted'}），已展示数据保留`,
        'warn', 5000);
    },
    onReconnect: () => {
      // 重连成功 → 主动补齐卡片与图表（Spec §3.5：服务端不保证补发历史）
      refreshAll();
    },
  });
  stream.start();
}

// ==================== 取数 ====================

/** 单区块取数（每块一个 AbortController：取消互不影响，BR-02-07）。 */
async function loadBlock(block) {
  const seq = ++block.seq;
  if (block.abort) block.abort.abort();
  block.abort = new AbortController();
  const { signal } = block.abort;
  block.status = 'loading';
  block.error = null;
  if (isMounted()) redrawBlock(block.name);
  try {
    const query = baseQuery();
    if (block.name === 'doughnut') query.dim = 'level';
    if (block.name === 'ranking') query.top = 10;
    // silent：区块自己显示错误占位，不必再叠一层全局 toast
    //（每 5 秒 4 条 toast 会把页面糊住，也会把真正的错误淹掉）
    const data = await api.request(`${BASE}${block.path}`, { query, signal, silent: true });
    if (signal.aborted || seq !== block.seq) return;      // 丢弃过期响应
    block.data = data;
    block.status = 'ok';
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (signal.aborted || seq !== block.seq) return;
    block.status = 'error';
    block.error = e;
    if (e && (e.code === 'COM-4290' || e.status === 429)) {
      // 限流：整页退避，而不是每块各报一次（4 条 toast 只会让人看不清原因）
      state.interval = Math.min(state.interval * 2, MAX_REFRESH_MS);
      restartTimer();
      ui.toast(`请求过于频繁（COM-4290），自动刷新已退避到 ${Math.round(state.interval / 1000)} 秒；` +
        '可稍后点「刷新」立即重试', 'warn', 6000);
    }
  }
  if (isMounted()) redrawBlock(block.name);
}

function redrawBlock(name) {
  if (!isMounted()) return;
  if (name === 'cards') renderCards();
  else if (name === 'trend') renderTrend();
  else if (name === 'doughnut') renderDoughnut();
  else if (name === 'ranking') renderRanking();
}

/** 全部区块一起取数（手动刷新与自动刷新共用，BR-02-06）。 */
function refreshAll() {
  if (!isMounted()) return;
  Object.values(blocks).forEach((b) => { loadBlock(b); });
}

// ==================== 定时器 / 可见性 / 卸载 ====================

function restartTimer() {
  if (timer) { clearInterval(timer); timer = null; }
  if (!mounted || !state.auto) return;
  timer = setInterval(() => {
    // 兜底：登出路径不会调用 dispose（壳只清空 #view），这里靠"容器是否还在文档里"自毁，
    // 否则定时器与 SSE 会一直活着——既漏连接，又持续吃限流额度
    if (!isMounted()) { dispose(); return; }
    if (document.hidden) return;      // BR-02-05：隐藏时不取数
    refreshAll();
  }, state.interval);
}

function onVisibility() {
  if (!mounted) return;
  if (document.hidden) {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) stream.pause();       // §3.5：隐藏时主动断开以省资源
  } else {
    restartTimer();
    if (stream) stream.resume();
    refreshAll();                     // 回到页面立即补一次，别让画面停在旧数据上
  }
}

/** 切走本页立刻停表停流（登出不会调用 dispose，只能靠 hash 变化兜住）。 */
function onHashChange() {
  const route = findRoute(location.hash);
  if (!route || route.hash !== '#/dashboard') dispose();
}

/** 用户滚动事件流：滚离顶部 = 在读旧事件，回到顶部 = 恢复自动跟随（BR-02-10）。 */
function onStreamScroll(e) {
  const body = refs && refs.stream ? refs.stream.querySelector('.db-stream-body') : null;
  if (!body || e.target !== body) return;
  streamData.pinned = body.scrollTop <= 4;
}

// ==================== 下钻与导出 ====================

/**
 * 下钻跳转（BR-02-14/15/16）。
 *
 * 权限不在这里判：壳（`app.js`）对无权 hash 会统一 toast「无权访问该页面」并跳回默认页
 * （模块 01 §2.3），这正是 BR-02-18 要求的处置；前端再判一次只会多一份会漂移的权限逻辑。
 */
function go({ hash, query }) {
  const qs = new URLSearchParams();
  Object.entries(query || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
  });
  const s = qs.toString();
  location.hash = `${hash}${s ? `?${s}` : ''}`;
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

/** 把若干张 ECharts 的 PNG 竖向拼成一张（带标题条）。 */
function composePng(images) {
  return new Promise((resolve, reject) => {
    const parts = [];
    let loaded = 0;
    images.forEach((it, i) => {
      const img = new Image();
      img.onload = () => {
        parts[i] = { title: it.title, img };
        loaded += 1;
        if (loaded !== images.length) return;
        const width = Math.max(...parts.map((p) => p.img.width));
        const head = 26;
        const height = parts.reduce((sum, p) => sum + p.img.height + head, 0);
        const canvas = document.createElement('canvas');
        canvas.width = width; canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#FFFFFF'; ctx.fillRect(0, 0, width, height);
        let y = 0;
        parts.forEach((p) => {
          ctx.fillStyle = '#223248';
          ctx.font = '600 13px "Microsoft YaHei", sans-serif';
          ctx.fillText(p.title, 10, y + 17);
          ctx.drawImage(p.img, 0, y + head);
          y += p.img.height + head;
        });
        canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('canvas.toBlob 返回空'))), 'image/png');
      };
      img.onerror = () => reject(new Error('图表 PNG 解码失败'));
      img.src = it.url;
    });
  });
}

/** 导出快照：PNG（三张图拼一张）+ JSON（接口原始值），§2.1。 */
async function exportSnapshot() {
  const raw = {
    exported_at: new Date().toISOString(),
    filters: { range: state.range, scene: state.scene || null, level: state.level || null },
    // 原始响应原样落盘：导出的意义是"可复核"，格式化过的数字反而失去证据价值
    overview: blocks.cards.data,
    trend: blocks.trend.data,
    distribution: blocks.doughnut.data,
    rule_ranking: blocks.ranking.data,
  };
  download(new Blob([JSON.stringify(raw, null, 2)], { type: 'application/json' }),
    `dashboard_snapshot_${Date.now()}.json`);

  const images = [
    { title: '拦截率趋势', handle: charts.trend },
    { title: '风险等级分布', handle: charts.doughnut },
    { title: '规则命中排行榜', handle: charts.ranking },
  ].filter((it) => !!it.handle)
    .map((it) => ({ title: it.title, url: it.handle.dataUrl() }))
    .filter((it) => !!it.url);

  if (!images.length) {
    ui.toast('当前没有可导出的图表（区块为空或失败），已导出指标 JSON', 'warn', 5000);
    return;
  }
  try {
    download(await composePng(images), `dashboard_snapshot_${Date.now()}.png`);
    ui.toast(`已导出快照（PNG ${images.length} 张图 + 指标 JSON）`, 'ok');
  } catch (e) {
    ui.toast(`图表合并失败（${e && e.message}），已导出指标 JSON`, 'warn', 5000);
  }
}

// ==================== 工具栏 ====================

function buildToolbar() {
  const fRange = ui.selectBox(RANGES, state.range, (v) => { state.range = v; onFilterChange(); });
  const fScene = ui.selectBox([{ value: '', label: '全部场景' }].concat(sceneOptions),
    state.scene, (v) => { state.scene = v; onFilterChange(); });
  const fLevel = ui.selectBox(LEVELS, state.level, (v) => { state.level = v; onFilterChange(); });
  const swText = ui.h('span', { class: 'db-switch-text', text: state.auto ? '自动刷新（5 秒）' : '自动刷新已关闭' });
  const swDot = ui.h('span', { class: `db-dot${state.auto ? ' ok' : ''}` });
  const sw = ui.h('label', { class: 'db-switch', dataset: { role: 'auto-refresh' } }, [
    ui.h('input', {
      type: 'checkbox', checked: state.auto ? 'checked' : null,
      onchange: (e) => {
        state.auto = e.target.checked;
        // Spec §2.1：关闭后「文案与绿点」必须同步变化。
        // 这两个节点只在 buildToolbar() 时渲染一次，若只改 state 而不显式更新它们，
        // 就会出现"勾选框已关、文案仍写着『自动刷新（5 秒）』、绿点仍亮"的假状态。
        swText.textContent = state.auto ? '自动刷新（5 秒）' : '自动刷新已关闭';
        swDot.classList.toggle('ok', state.auto);
        restartTimer();
      },
    }),
    ui.h('span', { class: 'db-switch-track' }),
    swText,
    swDot,
  ]);
  const btnRefresh = ui.button('刷新', {
    onClick: () => {
      // 手动刷新 = 用户明确要求"现在就取"，顺手把限流退避复位
      state.interval = REFRESH_MS;
      restartTimer();
      refreshAll();
    },
  });
  const btnExport = ui.button('导出快照', { variant: 'plain', onClick: () => exportSnapshot() });
  return ui.toolbar([fRange, fScene, fLevel, sw], [btnRefresh, btnExport]);
}

/** 筛选变更（BR-02-07：先取消在途请求，再发新请求，避免旧响应覆盖新结果）。 */
function onFilterChange() {
  Object.values(blocks).forEach((b) => {
    if (b.abort) { b.abort.abort(); b.abort = null; }
    b.seq += 1;
    b.data = null;
    b.status = 'idle';
  });
  if (stream) stream.reconfigure({ scene: state.scene, level: state.level });
  refreshAll();
}

// ==================== 组装 ====================

export async function render(container) {
  const mySeq = ++mountSeq;
  releaseState();          // 先释放上一次挂载（含被并发的第二次 render 顶掉的那次）
  mounted = true;
  refs = {
    root: container,
    cards: ui.h('div', { class: 'db-holder', dataset: { block: 'cards' } }),
    trend: ui.h('div', { class: 'db-holder', dataset: { block: 'trend' } }),
    doughnut: ui.h('div', { class: 'db-holder', dataset: { block: 'doughnut' } }),
    ranking: ui.h('div', { class: 'db-holder', dataset: { block: 'ranking' } }),
    stream: ui.h('div', { class: 'db-holder', dataset: { block: 'stream' } }),
  };

  // 枚举（场景标签）与首屏一起就绪；拿不到也不阻塞渲染（选项退化为"全部场景"）
  let enums = null;
  try { enums = await ensureEnums(); } catch (e) { enums = null; }
  if (mySeq !== mountSeq) return;      // 已被更新的 render 接管（见 mountSeq 的说明）
  sceneOptions = ((enums && enums.rule_scenes) || [])
    .filter((o) => METRIC_SCENES.includes(o.value))
    .map((o) => ({ value: o.value, label: o.label }));

  ui.mount(container, [
    refs.cards,
    ui.h('div', { class: 'db-grid2' }, [refs.trend, refs.doughnut]),
    ui.h('div', { class: 'db-grid2' }, [refs.ranking, refs.stream]),
  ]);
  // 工具条插到最前（原型 dbToolbar 在页面最上方）
  container.insertBefore(ui.card(null, buildToolbar()), container.firstChild);

  // 一次性绑定（用 capture 捕获子元素的 scroll：事件流区块每次重绘都会换掉 tbody）
  refs.stream.addEventListener('scroll', onStreamScroll, true);
  window.addEventListener('hashchange', onHashChange);
  document.addEventListener('visibilitychange', onVisibility);

  renderCards();
  renderTrend();
  renderDoughnut();
  renderRanking();
  startStream();      // 先建流，区块首帧就能显示「连接中…」而不是"已关闭"
  paintStream();
  restartTimer();
  refreshAll();
}

/**
 * 视图卸载（壳在切路由时调用；登出路径不调用，故另有 hashchange + 容器存活性兜底）。
 * 必须做到：取消在途请求、停掉定时器、**关闭 SSE**、dispose 全部图表实例。
 */
export function dispose() {
  mountSeq += 1;      // 让任何还在 await 中途的 render 放弃（见 mountSeq 的说明）
  releaseState();
}

/** 真正的释放动作（`dispose()` 与 `render()` 的开头都要用，故独立成函数）。 */
function releaseState() {
  mounted = false;
  if (timer) { clearInterval(timer); timer = null; }
  Object.values(blocks).forEach((b) => {
    b.seq += 1;
    if (b.abort) { b.abort.abort(); b.abort = null; }
  });
  if (stream) { stream.stop(); stream = null; }
  Object.keys(charts).forEach((k) => disposeChart(k));
  window.removeEventListener('hashchange', onHashChange);
  document.removeEventListener('visibilitychange', onVisibility);
  streamData.rows = [];
  streamData.state = 'closed';
  streamData.reconnects = 0;
  streamData.everOpen = false;
  streamData.pinned = true;
  refs = null;
}

// E2E / 人工排查用的只读口（不参与渲染逻辑）。
// `feed` 是唯一有副作用的入口：用于在没有真实事件源时（如上游尚未接线、
// 或需要核对"多行/多列"排版，决策 D65）把一条事件按正常路径喂进事件流。
export const __debug = {
  blocks,
  state,
  feed: (row) => pushStreamEvent(row),
  streamInfo: () => ({ state: streamData.state, rows: streamData.rows.length, reconnects: streamData.reconnects }),
  chartCount: () => chart.liveChartCount(),
};
