/* 审核工作台 · 中栏 = 模块 09 的「用户画像卡」+「关联实体图谱」（模块 09 §2.1 / §2.2）
 *              · 右栏 = 模块 05 的「系统风险判定摘要」+「规则命中明细表」（模块 05 §2.1 / §2.2）
 *
 * 为什么挂在 `#/cases` 而不是新开一个页面：09 与 05 在 Spec 里都是**引擎（无独占页面）**，
 * 它们的 UI 就是 `03_风控审核工作台.pen` 中栏与右栏的展示点。新开页面必然顺带新增一个
 * 左侧菜单项，而左侧菜单是 03_原型图 9 个原型的一一映射——多一项就与原型不一致，
 * 也会让「审核员菜单 = 态势大盘/审核工作台/事件仿真」这条既有断言翻掉。
 *
 * 案件队列与处置动作归**模块 07**，尚未接入：本页交付中栏（画像 + 图谱）与右栏
 * （判定摘要 + 命中明细），并在页面底部如实说明还缺什么，不假装已有案件列表。
 *
 * 数据源（Spec §3）：
 *   GET /api/v1/profiles/{user_id}       画像全貌（09）
 *   GET /api/v1/graph/{entity_type}/{id} 2 跳关联网络（09，AD-06：max_hop 硬上限 2）
 *   GET /api/v1/events/{event_id}        事件详情里的决策块 + 命中明细（03 透传 05 的产出）
 *
 * 三条链路**各自独立加载、各自独立报错**（GRP-5001：画像失败不得拖垮图谱，反之亦然）
 * ——所以这里没有用 Promise.all 做"一荣俱荣、一损俱损"的编排。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as session from '../session.js';
import { ensureEnums, labelOf } from '../store.js';
import { queryOf } from './registry.js';
// 模块 07 的左栏（案件列表：筛选 / 分页 / 认领）——**只做加法**，本文件里 09 与 05 的
// 渲染一个字都没改（见本文件顶部注释与任务书 §0）。
import { createCaseList } from './case_list.js';
// 模块 08 的处置流程组件：二次确认弹窗、认领弹窗、处置流水、结论×动作相容矩阵。
// **本文件不自己拼确认框、不直接调 /dispose**（BR-07-21），只把表单交给它。
import {
  ACTION_TYPES, CONCLUSIONS, REMARK_MAX, WARN_LINE, disposeErrorText, incompatibleReason,
  loadActions, normalizeActions, openDisposeFlow, renderActions,
} from './case_dispose.js';

export const meta = { title: '审核工作台', crumb: '风控中台 / 审核工作台' };

/**
 * 默认演示对（用户 + 事件）。
 *
 * 为什么默认是这一对（而不是画像最饱满的 `U000128` + 它的事件）：
 *   · `U000128` 在种子黑名单里（同设备 `D8F2A1C4` 也在），于是任何挂在它身上的事件**先撞名单**，
 *     右栏永远是「黑名单直通、不展示分值、没有明细」——**最没有信息量的一屏**；
 *   · `U000132` / `EVT20260101900000000001` 是 `scripts/seed.py` 重建的演示对，右栏能同时
 *     展示真实决策的**全部要素**：70 分（中风险/人审）+ 2 条命中明细（含实际特征值），
 *     中栏的画像卡与图谱也齐全。**两个引擎的成果在一屏里同时可见**，这才是该当默认的那一屏。
 * 二者的显式参数分支（`?user=U000128` 黑名单直通、`?user=U009999` 代理 IP 反面、
 * `?user=U999998` 404 不存在）**都还在**，只是不再当默认——它们是有效分支，不是默认。
 *
 * 默认事件只在 **URL 没有 `?event=`** 时生效：`?event=` 有值就用它，`?event=none`
 * 表示"不查判定"（`清空` 按钮走这条，见 `readState`）。这样 URL 仍是唯一真源，
 * 刷新/分享都能复现同一屏，同时"默认屏"不再是空态。
 */
const DEFAULT_USER = 'U000132';
const DEFAULT_EVENT = 'EVT20260101900000000001';

/** Spec §3.2：entity_type 只有这四种，**不含 phone**（手机号是用户属性，不做独立图节点）。 */
const ENTITY_TYPES = ['user', 'device', 'ip', 'address'];
const TYPE_LABEL = { user: '用户', device: '设备', ip: '来源 IP', address: '收货地址' };

/** 账号状态 → 标签文案与配色（Spec §2.1：active 绿、frozen 橙、banned 红）。 */
const STATUS_META = {
  active: { text: '账号正常', cls: 'low' },
  frozen: { text: '账号冻结', cls: 'medium' },
  banned: { text: '账号封禁', cls: 'high' },
};

/** 聚集度阈值（Spec §2.1：同设备/同地址关联 N ≥ 5 个账号时数值标红）。 */
const CLUSTER_ALERT = 5;

const DEFAULT_MAX_HOP = 2;    // AD-06 硬上限，Spec §4.4 BR-09-15
const MAX_HOP = 2;
const DEFAULT_MAX_NODES = 200; // Spec §3.2 默认值（上限 500 由后端校验）

const clampUser = (v) => String(v || '').trim().toUpperCase();

/**
 * 从主题变量取色。
 * ECharts 画在 canvas 上，**读不到 CSS 变量**，只能在运行时取一次计算值再交给它——
 * 这样"颜色只在 CSS 里定义"的约定不被破坏，换肤时图谱跟着变。
 */
function themeVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** HTML 转义：tooltip / 抽屉里会插入来自后端的 label（含用户输入过的地址等），必须转义。 */
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ==================== 状态（真源是 hash，不额外维护一份）====================

function readState() {
  const qs = queryOf(location.hash);
  const hopRaw = Number(qs.get('max_hop'));
  const nodesRaw = Number(qs.get('max_nodes'));
  const gtypeRaw = String(qs.get('gtype') || '').trim();
  const gidRaw = String(qs.get('gid') || '').trim();
  const user = clampUser(qs.get('user')) || DEFAULT_USER;
  const gtype = ENTITY_TYPES.includes(gtypeRaw) ? gtypeRaw : 'user';
  const msOf = (key) => {
    const n = Number(qs.get(key));
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : '';
  };
  const pageRaw = parseInt(qs.get('page') || '1', 10);
  const statusRaw = String(qs.get('status') || '').trim();
  const levelRaw = String(qs.get('risk_level') || '').trim();
  const timeRaw = String(qs.get('time') || '').trim();
  return {
    user,
    // 事件编号的三态（模块 05 的右栏）：
    //   · URL 无 `?event=`  → 用**默认演示事件**（`DEFAULT_EVENT`），默认屏因此是有信息量的那一屏；
    //   · `?event=<编号>`   → 查它（URL 是唯一真源，刷新/分享可复现）；
    //   · `?event=none`     → **不查**（`清空` 按钮写这个值）。需要一个显式"空"标记来区分
    //     "没给参数"与"明确要求不查"，否则 `清空` 之后又会把默认事件查回来，按钮等于没用。
    event: resolveEvent(qs.get('event')),
    maxHop: (Number.isFinite(hopRaw) && hopRaw >= 1 && hopRaw <= MAX_HOP) ? Math.floor(hopRaw) : DEFAULT_MAX_HOP,
    maxNodes: (Number.isFinite(nodesRaw) && nodesRaw > 0) ? Math.floor(nodesRaw) : DEFAULT_MAX_NODES,
    riskOnly: qs.get('risk_only') === 'true',
    // 图中心默认为当前用户；点击非用户节点可把中心换成该实体（仍保留用户画像卡）
    gtype,
    gid: gidRaw || user,

    // ---- 模块 07：左栏案件列表的筛选状态。真源同样是 hash（BR-07-05：可分享、可前进后退）----
    // `?case=` 存在时才进入"案件驱动模式"：中栏与右栏由**一次详情响应**渲染（BR-07-06）。
    // 没有它时本页的既有行为（`?user=` / `?event=` 驱动）一字未改——09/05 的断言全钉在那些
    // 参数上，因此默认**不为任何案件自动选中**，否则那些断言会读到另一屏。
    case: String(qs.get('case') || '').trim(),
    page: Number.isFinite(pageRaw) && pageRaw >= 1 ? pageRaw : 1,
    // 筛选取值必须白名单化：非法值原样发给后端只会换来一个 `CASE-4004`，且刷新后下拉框
    // 会停在一个后端不认的选项上——"看起来选中了、其实没筛"，这比不筛更危险。
    status: ['pending', 'reviewing', 'disposed', 'archived'].includes(statusRaw) ? statusRaw : '',
    risk_level: ['medium', 'high'].includes(levelRaw) ? levelRaw : '',
    scene_code: String(qs.get('scene_code') || '').trim(),
    // `keyword`（匹配 case_no / user_id）**页面不提供输入框**（Spec §2.1：筛选项全部为下拉，
    // 避免与全文检索混淆），但 hash 里给了就照用——这样"某用户的全部案件"是一条可分享的链接，
    // 与 `status`（大盘下钻）走的是同一个机制。
    keyword: String(qs.get('keyword') || '').trim(),
    time: ['1h', '24h', '7d', 'custom'].includes(timeRaw) ? timeRaw : '',
    created_from: msOf('created_from'),
    created_to: msOf('created_to'),
  };
}

/** 事件编号三态的判定（见 `readState` 的注释）。`none` 是"明确要求不查"的哨兵值。 */
export function resolveEvent(raw) {
  const v = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!v) return DEFAULT_EVENT;       // 没给参数 → 默认演示事件
  if (v.toLowerCase() === 'none') return '';  // 明确不查 → 空态
  return v;
}

/** 以当前 query 为底改写若干参数后回到路由（图中心/用户/事件编号都体现在 URL 上，可分享、可复现）。 */
function go(patch) {
  const qs = queryOf(location.hash);
  const p = Object.assign({}, patch);
  // **手动换用户/换事件/换图中心 = 退出案件模式**：否则中栏仍由案件详情驱动，
  // 用户以为自己查的是 U000128，看到的却是案件里的那个人。同一屏里存在两个数据真源
  // 是这类联动页的经典事故（Spec 原型注记第 1 条"不得异步错位"要防的正是它）。
  if (['user', 'event', 'gtype', 'gid', 'max_hop', 'max_nodes', 'risk_only'].some((k) => k in p)
    && !('case' in p)) {
    p.case = null;
  }
  Object.entries(p).forEach(([k, v]) => {
    if (v === null || v === undefined || v === '') qs.delete(k); else qs.set(k, String(v));
  });
  const s = qs.toString();
  const next = `#/cases${s ? `?${s}` : ''}`;
  // 相同 hash 不触发 hashchange：手动重载，且只重载本次真正改动的那一块
  if (next === location.hash) { reload(p); return; }
  location.hash = next;
}

let refs = null;
let state = null;
let profileAbort = null;
let graphAbort = null;
let graphSeq = 0;          // 递增序号：丢弃"过期请求"的绘制结果，避免旧图覆盖新图
let decisionAbort = null;
let decisionSeq = 0;
let chart = null;

// ---- 模块 07：左栏与"案件驱动模式"的模块级状态 ----
let caseList = null;        // 左栏组件实例（由 case_list.js 创建）
let caseAbort = null;       // 详情请求的取消句柄（BR-07-08）
let caseSeq = 0;            // 递增序号：丢弃过期响应（连点防护）
let caseDetail = null;      // 最近一次详情响应（选中条与处置区回读它，不做本地推测）
let featureMeta = null;     // `GET /features/meta` 的 items（快照"基线值"，决策 D68）
let featureMetaInflight = null;
let feedbackFor = null;     // 结果反馈条属于哪个案件（换案件时必须清掉，否则会串档）

const onKeyDown = (e) => { if (e.key === 'Escape') closeDrawer(); };

export function dispose() {
  if (profileAbort) { profileAbort.abort(); profileAbort = null; }
  if (graphAbort) { graphAbort.abort(); graphAbort = null; }
  if (decisionAbort) { decisionAbort.abort(); decisionAbort = null; }
  if (caseAbort) { caseAbort.abort(); caseAbort = null; }
  graphSeq += 1;
  decisionSeq += 1;
  caseSeq += 1;
  if (chart) { try { chart.dispose(); } catch (e) { /* 已销毁则忽略 */ } chart = null; }
  document.removeEventListener('keydown', onKeyDown);
  refs = null;
  caseList = null;
  caseDetail = null;
  feedbackFor = null;
}

// ==================== ① 画像卡（Spec §2.1）====================

/** 风险标签底色由 `tag_meta.severity` 推导（Spec §3.1 明确要求，前端不硬编码标签名）。 */
function tagChip(tagName, tagMeta) {
  const m = (tagMeta && tagMeta[tagName]) || {};
  const sev = String(m.severity || '').toLowerCase();
  const kind = sev === 'high' ? 'sev-high'
    : (sev === 'medium' || sev === 'mid') ? 'sev-medium' : 'sev-low';
  return ui.tag(m.label || tagName, kind);
}

/** 聚集度数值：≥5 标红（V-09-04）。 */
function countNode(n) {
  const num = Number(n);
  if (!Number.isFinite(num)) return ui.h('span', { class: 'pv-na', text: '—' });
  const hot = num >= CLUSTER_ALERT;
  return ui.h('span', { class: hot ? 'hot' : 'pv-num', text: String(num) });
}

/** 明细行。`data-field` 是稳定的选择器锚点（E2E 与人工排查都对着它定位）。 */
function prow(field, key, valueNodes, extraClass = '') {
  return ui.h('div', { class: `prow ${extraClass}`.trim(), dataset: { field } }, [
    ui.h('span', { class: 'pk', text: key }),
    ui.h('span', { class: 'pv' }, valueNodes),
  ]);
}

function detailRows(data) {
  const user = data.user || {};
  const device = data.device;
  const ip = data.ip;
  const address = data.address;

  // 四行**始终渲染**：某一项缺失时显示 `—`，而不是整行消失——
  // 明细行凭空少一行会让审核员误以为"该维度不存在"，显示 `—` 才是如实表达"无数据"。
  // 文案与 03_风控审核工作台.pen 的 profileCard 逐字对齐（`138****6621`、
  // `D8F2A1C4 · 关联 12 个账号`、`117.136.**.88 · 代理 IP`、`同地址关联 8 个账号`）。
  const phoneRow = prow('phone', '手机号（脱敏）', [
    ui.h('span', { class: 'mono', text: user.phone_masked || '—' }),
  ]);

  const deviceRow = prow('device', '设备指纹', device ? [
    ui.h('span', { class: 'mono', text: device.device_id }),
    ' · 关联 ',
    countNode(device.linked_user_cnt),
    ' 个账号',
  ] : [ui.h('span', { class: 'pv-na', text: '—' })]);

  // 来源 IP 只显示 IP 与代理标记：聚集度数值在本页由"设备/地址"两行承担（V-09-04 的口径），
  // IP 侧的关联账号数在图谱 tooltip 与节点详情里给出，避免同一指标在同一张卡上出现两次。
  const ipRow = prow('ip', '来源 IP', ip ? [
    ui.h('span', { class: 'mono', text: ip.ip }),
    ip.is_proxy ? ' · 代理 IP' : '',
  ] : [ui.h('span', { class: 'pv-na', text: '—' })], ip && ip.is_proxy ? 'is-proxy' : '');

  // 收货地址按原型补上**脱敏后**的地址标识：只有"同地址关联 N 个账号"而没有地址本身，
  // 审核员无法把同一地址上的多个账号对上号；`masked_detail` 由后端脱敏存储（BR-09-05），
  // 前端不存在明文可显示。缺失时退化为只显示聚集度。
  const addrRow = prow('address', '收货地址', address ? [
    address.masked_detail ? ui.h('span', { class: 'mono', text: address.masked_detail }) : null,
    address.masked_detail ? ' · ' : null,
    '同地址关联 ',
    countNode(address.linked_user_cnt),
    ' 个账号',
  ] : [ui.h('span', { class: 'pv-na', text: '—' })]);

  return [phoneRow, deviceRow, ipRow, addrRow];
}

function profileCard(data) {
  const user = data.user || {};
  const stat = data.stat || {};
  const dec = data.latest_decision || null;
  const tagMeta = data.tag_meta || {};
  const status = STATUS_META[String(user.status || '').toLowerCase()] || null;

  const header = ui.h('div', { class: 'profile-head' }, [
    // 无真实头像：用用户编号首字母做色块（Spec §2.1）
    ui.h('div', { class: 'profile-avatar', text: String(user.user_id || '?').slice(0, 1).toUpperCase() }),
    ui.h('div', { class: 'profile-head-main' }, [
      ui.h('div', { class: 'profile-title' }, [
        ui.h('span', { class: 'profile-id', text: user.user_id || state.user }),
        dec ? ui.riskTag(dec.risk_level) : ui.h('span', { class: 'tag plain', text: '暂无决策' }),
        status ? ui.tag(status.text, status.cls) : null,
      ]),
      ui.h('div', { class: 'profile-sub', text:
        `注册 ${user.age_days === null || user.age_days === undefined ? '—' : user.age_days} 天`
        + ` · 等级 ${user.level || '—'}`
        + ` · 累计下单 ${stat.order_cnt === null || stat.order_cnt === undefined ? '—' : stat.order_cnt} 单` }),
    ]),
  ]);

  const tags = (user.risk_tags || []).length
    ? ui.h('div', { class: 'tag-row' }, (user.risk_tags || []).map((t) => tagChip(t, tagMeta)))
    : ui.h('div', { class: 'hint', text: '暂无风险标签' });

  const card = ui.card(null, [
    header,
    tags,
    ui.h('div', { class: 'profile-rows' }, detailRows(data)),
    dec ? ui.h('div', { class: 'hint', text:
      `最近决策：${dec.decision || '—'} · 风险分 ${dec.risk_score === null || dec.risk_score === undefined ? '—' : dec.risk_score}`
      + ` · ${ui.fmtTime(dec.decided_at)}` }) : null,
  ]);
  card.classList.add('profile-card');
  return card;
}

/** 该 404 是否为"画像/图谱领域自己给出的不存在"（GRP-4004），而不是"路由还没上线"。 */
const isDomainNotFound = (e) => e.status === 404 && /^GRP-/.test(String(e.code || ''));
async function loadProfile() {
  if (profileAbort) profileAbort.abort();
  profileAbort = new AbortController();
  const { signal } = profileAbort;
  ui.mount(refs.profileHolder, ui.loading(4));
  try {
    // 走 api.request 而不是 api.get：404（用户不存在）是**预期分支**，由本区块自己渲染
    // 「未找到该用户画像」，不需要再弹一条全局 toast 干扰审核员的注意力。
    const data = await api.request(`${api.API_PREFIX}/profiles/${encodeURIComponent(state.user)}`,
      { signal, silent: true });
    if (signal.aborted) return;
    ui.mount(refs.profileHolder, profileCard(data));
  } catch (e) {
    if (e.name === 'AbortError' || signal.aborted) return;
    // 只有 GRP-4004（领域 404）才是"用户不存在"；其它 404（路由尚未上线）必须如实报错，
    // 否则会把"接口没接上"伪装成"这个用户没有画像"，排查时会被彻底带偏。
    if (isDomainNotFound(e)) {
      ui.mount(refs.profileHolder, ui.card(null, ui.empty('未找到该用户画像',
        `用户编号 ${state.user} 在画像库中不存在（${e.code}）`)));
    } else {
      ui.mount(refs.profileHolder, ui.card(null, [
        ui.banner('err', `画像服务暂时不可用：${e.message}（trace ${traceOf(e)}）`),
        ui.h('div', { class: 'form-actions' }, [
          ui.button('重试', { variant: 'ghost', onClick: () => loadProfile() }),
        ]),
      ]));
    }
  }
}

/** 错误处理本身绝不能再抛错（否则真正的根因会被错误处理器的异常顶掉）。 */
function traceOf(e) {
  return (e && typeof e.shortTrace === 'function') ? e.shortTrace() : '—';
}

// ==================== ④ 右栏：系统风险判定摘要 + 规则命中明细（模块 05 §2.1 / §2.2）====================//
// 数据只来自 `GET /api/v1/events/{event_id}`：那是**真实落库**的决策（05 的产出经 03 透传）。
// 刻意不调 `POST /engine/evaluate`——那个接口 `dry_run=true` 只算不落库，用来做仿真/调试，
// 把它拿来填审核工作台会出现"看到的分数与当时实际处置的依据不是同一条记录"。

// 风险等级标签（低/中/高）直接复用组件库的 `ui.riskTag`，它内部已把
// low/medium/high 映射成「低风险/中风险/高风险」并配色；这里**不再维护第二份映射**，
// 否则主题换色时会出现"卡片上的等级色与判定摘要的等级色不一致"。

/** 系统建议 → 标签文案与配色（Spec §2.1：pass/review/reject → 放行/人审/拦截）。
 *  建议必须比等级更醒目：等级是"有多严重"，建议才是"下一步做什么"。 */
const DECISION_META = {
  pass: { text: '放行', cls: 'low' },
  review: { text: '人审', cls: 'medium' },
  reject: { text: '拦截', cls: 'high' },
};
/** 分值三档配色与风险三色**同源**：判定的红/橙/绿就是主题里的风险色（不额外定义颜色）。 */
const SCORE_CLS = { high: 'decision-score-high', medium: 'decision-score-medium', low: 'decision-score-low' };

/**
 * 名单命中态的归一化。
 *
 * Spec §3.1 把它定义为对象 `{hit, list_type, entity_type, entity_value}`，但 03 的响应模型
 * 历史上曾把它写成 `bool`。**两种形态都要认**：前端若只认一种，"直通"这个分支在另一种
 * 形态下会静默走丢——真名单直通会被渲染成普通的 0 分判定，那是最容易被误读成"没命中规则"的画面。
 * 返回 `null` 表示**名单未命中**（此时照常展示分值与明细）。
 */
function normalizeListHit(raw) {
  if (!raw) return null;
  if (raw === true) return { hit: true, list_type: '', entity_type: '', entity_value: '' };
  if (typeof raw !== 'object') return null;
  if (raw.hit !== true) return null;
  return {
    hit: true,
    list_type: String(raw.list_type || '').toLowerCase(),
    entity_type: raw.entity_type || '',
    entity_value: raw.entity_value === null || raw.entity_value === undefined ? '' : String(raw.entity_value),
  };
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** 名单直通文案：白名单直通放行 / 黑名单直通拦截。 */
function listDirectText(lh) {
  return lh.list_type === 'white' ? '白名单直通放行' : '黑名单直通拦截';
}

/** 三档配色按 `final_score` 判定（Spec §2.1：≥80 红 / 60~79 橙 / <60 绿）。 */
function scoreLevel(score) {
  if (!isNum(score)) return 'low';
  if (score >= 80) return 'high';
  if (score >= 60) return 'medium';
  return 'low';
}

/** 定长两行的标签组：标签不稳定（高/中/低宽窄不同），但块高必须稳定。 */
function tagBlock(nodes) {
  return ui.h('div', { class: 'decision-tags' }, nodes);
}

/**
 * 判定摘要卡（**纯函数**：给决策块 + 命中明细，返回一个 DOM 节点）。
 *
 * 为什么导出：E2E 要在真实浏览器里核对**每一个分支**——空态、名单直通、未命中、
 * 分值三档配色。种子数据只能覆盖其中一条分支（还取决于当天种子），若只能靠"造数据"
 * 来验，其余分支就永远没有断言。把纯渲染函数暴露出来，就可以喂真实形状的数据直接
 * 断言 DOM，既覆盖全分支，又不动 `app/**` 与种子。
 *
 * @param {object|null} decision  决策块（Spec §3.1 的 12 字段）
 * @param {object} opts {eventId, hits}
 */
export function renderDecisionCard(decision, opts = {}) {
  const eventId = String(opts.eventId || (decision && decision.event_id) || '').trim();
  const box = ui.h('div', { class: 'decision-card', dataset: { role: 'decision-card', event: eventId } });
  // 降级信封（03 的 `degrade` 字段）：与决策块分开传，因此在这里先归一，别到后面再回头找
  const degradeInfo = (opts.degrade && typeof opts.degrade === 'object') ? opts.degrade : null;

  // 空态（Spec §2.1）：**不许显示 0 分**。"0 分"是一个断言（"这条事件很干净"），
  // 而"没有记录"是另一种事实（数据异常）。用 0 分冒充后者会让审核员以为系统判过、
  // 而且判了干净——这与模块 11 "分母为 0 时返回 null 而不是 0"是同一条原则。
  if (!decision || typeof decision !== 'object') {
    return ui.mount(box, ui.h('div', { class: 'decision-empty', dataset: { role: 'no-decision' },
      text: '该事件未产生决策记录（数据异常）' }));
  }

  const lh = normalizeListHit(decision.list_hit);
  const hits = Array.isArray(opts.hits) ? opts.hits
    : (Array.isArray(decision.hits) ? decision.hits : []);
  const hitCount = isNum(decision.hit_rule_count) ? decision.hit_rule_count : hits.length;
  const score = decision.final_score;
  // 降级块（`engine_version="degraded"`，见 03 的 `degraded_decision()`）：它的 0 分是**兜底值**，
  // 含义是"规则引擎没跑"，不是"跑了但很干净"。这与空态的坑**同源**（0 分 ≠ 没有结论），
  // 所以必须显式标注，不能让它以普通判定的样子出现在右栏——那正是审核员最容易被骗的一屏。
  const degraded = !!degradeInfo || String(decision.engine_version || '').toLowerCase() === 'degraded'
    || decision.degraded === true || !!decision.degrade_reason;

  // —— 第一块：数值 / 直通 / 建议 + 等级标签 ——
  let mainBlock;
  if (lh) {
    // 名单直通态：**不展示分值**。直通时 `rule_score=0`、`hits=[]`，把 0 分摆在大字位置
    // 会让人读成"规则算出来 0 分"，而真相是"规则根本没参与"（BR-05-02/03）。
    mainBlock = ui.h('div', { class: 'decision-direct', dataset: { role: 'list-direct' } }, [
      ui.h('div', { class: 'decision-direct-text', text: listDirectText(lh) }),
      listDetailLine(lh),
    ]);
  } else {
    mainBlock = ui.h('div', { class: 'decision-main' }, [
      ui.h('div', {
        class: `decision-score ${SCORE_CLS[scoreLevel(score)]}`,
        dataset: { role: 'final-score' },
        text: isNum(score) ? String(score) : '—',
      }),
      ui.h('div', { class: 'decision-score-unit', text: degraded ? '分（降级兜底值）' : '分' }),
    ]);
  }

  const tags = [];
  if (!lh && isNum(score)) {
    // 等级与建议各自一行：右栏只有 ~350px，横排两个标签会在"高风险 + 拦截"这种
    // 最需要看清的组合上先换行，反而把最要紧的两个词挤散。
    tags.push(ui.riskTag(decision.risk_level));
    tags.push(decisionTag(decision.decision));
  } else {
    // 直通态不给"风险等级"标签：等级来自分值分档，而直通态没有分值可谈；
    // 硬贴一个"低风险"会让白名单直通看起来像是引擎算出来的结论。
    tags.push(decisionTag(decision.decision));
    tags.push(ui.h('span', { class: 'tag brand', text: '名单直通' }));
  }

  // —— 第二块：引擎说明行 ——
  // 文案与 Spec §2.1 逐字对齐：`决策引擎：{engine_version} · {名单未命中|白名单直通|黑名单直通} · 命中规则 N 条`
  const listText = !lh ? '名单未命中'
    : (lh.list_type === 'white' ? '白名单直通' : (lh.list_type === 'black' ? '黑名单直通' : '名单直通'));
  // `engine_version` 为空时**不写死 rule-engine-v1**：那会把"后端没给版本"伪装成"版本正确"。
  // 该字段缺失本身就是契约缺口，必须让它在页面上可见（显示 —）。
  const engine = decision.engine_version ? String(decision.engine_version) : '—';
  const engineLine = `决策引擎：${engine} · ${listText} · 命中规则 ${hitCount} 条`;

  const card = ui.mount(box, [
    ui.h('div', { class: 'decision-summary' }, [mainBlock, tagBlock(tags)]),
    ui.h('div', { class: 'decision-engine', dataset: { role: 'engine-line' }, text: engineLine }),
  ]);

  if (degraded) {
    // 带上 stage 与截断后的原因：审核员看到"降级"要知道是**哪一环**没起来（feature / rule / timeout），
    // 这正是排查"为什么这条事件没有真实判定"的第一手线索。原因里的 Python 异常细节属于日志内容，
    // 这里只留前 80 字，避免把异常栈堆在页面上。
    const stage = degradeInfo && degradeInfo.stage ? `stage=${degradeInfo.stage}，` : '';
    const why = degradeInfo && degradeInfo.reason
      ? `　原因：${String(degradeInfo.reason).slice(0, 80)}` : '';
    card.appendChild(ui.h('div', { class: 'decision-degraded', dataset: { role: 'degraded' },
      text: `决策降级：${stage}规则引擎未参与本次判定，该分值为兜底值，不代表“未命中规则”${why}` }));
  }

  // —— 第三块：命中明细 ——
  if (lh) {
    // 直通时隐藏明细表（Spec §2.1），但要如实说明"为什么是空的"，
    // 否则审核员会把"名单直通所以没求值"误读成"求值了但一条都没命中"。
    card.appendChild(ui.h('div', { class: 'hint', dataset: { role: 'hits-hidden' },
      text: `名单直通时不求值任何规则，故无命中明细（hits=[]）。决策耗时 ${isNum(decision.elapsed_ms) ? decision.elapsed_ms : '—'} ms` }));
  } else {
    // 明细表**自带小标题**并单独成卡：它是"证据清单"，与摘要不是同一类信息；
    // 混在一个无标题的块里，审核员在长页面上会分不清哪些是结论、哪些是依据。
    const hitsCard = ui.card(`规则命中明细（${hitCount} 条）`, hitsTable(hits));
    hitsCard.classList.add('decision-hits-card');
    card.appendChild(hitsCard);
  }
  return card;
}

/** 直通态的补充说明：命中在哪个维度的哪个值上（审核员据此复核名单是否该过期）。 */
function listDetailLine(lh) {
  if (!lh.entity_type && !lh.entity_value) return null;
  const typeText = { user_id: '用户编号', user: '用户编号', phone: '手机号', ip: 'IP', device_id: '设备指纹',
    device: '设备指纹', address_id: '收货地址', address: '收货地址' }[lh.entity_type] || lh.entity_type;
  return ui.h('div', { class: 'hint' }, [
    `命中维度：${typeText}`,
    lh.entity_value ? ui.h('span', { class: 'mono', text: ` ${lh.entity_value}` }) : null,
  ]);
}

/** 建议标签：未知取值退化成普通灰标签，而不是消失（消失会让"为什么没有结论"变成谜）。 */
function decisionTag(value) {
  const m = DECISION_META[String(value || '').toLowerCase()];
  return m ? ui.tag(m.text, m.cls) : ui.tag(value ? `未知：${value}` : '无建议', 'plain');
}

/**
 * 命中明细表（Spec §2.2）。
 *
 * - 排序按 `score` **降序**：审核员先看贡献最大的那条，而不是后端的写入顺序。
 * - 分值是**证据**，不是待办：展示成 `+40`（正分表示"累加进来"；减分规则保留 `-`）。
 * - `rule_name` 取 `decision_hits` 的**冗余快照**（BR-05-21）。**绝不**在这里回查 `rules`：
 *   实时联查会把"当时的规则名"换成"现在的规则名"，历史决策的解释当场失真——
 *   而这正是冗余快照要防的那件事；前端一联查就绕过了后端给出的保证。
 * - 列宽**不写死**（早期给编码 104px、分值 56px）：右栏只有 ~350px，固定列宽会把
 *   `reason` 挤成每行一两个字，实测"`同设备聚集登录` | `+45` | `同设备关联账号数 6 ≥ 阈值 5`"
 *   在 348px 里几乎读不出来。表格是 `table-layout: fixed`，不给宽度的列平分剩余空间，
 *   再靠 `white-space: normal` 让 reason 正常折行。
 */
function hitsTable(hits) {
  const rows = (hits || []).filter((h) => h && typeof h === 'object');
  if (!rows.length) {
    // 空态文案（Spec §2.2）：与"没有决策记录"是两回事——这里是"决策跑了，但没有规则命中"。
    return ui.h('div', { class: 'decision-hits' }, [
      ui.empty('未命中任何规则（累计 0 分）'),
    ]);
  }
  const sorted = rows.slice().sort((a, b) => (isNum(b.score) ? b.score : 0) - (isNum(a.score) ? a.score : 0));
  const table = ui.table(
    [{ name: '规则' }, { name: '分值' }, { name: '触发原因' }],
    sorted.map((h) => [
      // 编码与名称是同一件事的两个面（编码稳定、名称好读），放同一列上下排，
      // 而不是各占一列——右栏的横向空间必须留给"触发原因"这段真正的证据。
      ui.h('div', { class: 'hit-rule' }, [
        ui.h('div', { class: 'hit-rule-name', text: h.rule_name ? String(h.rule_name) : '—' }),
        h.rule_code ? ui.h('div', { class: 'hit-rule-code mono', text: String(h.rule_code) }) : null,
      ]),
      ui.h('span', { class: 'decision-hit-score', dataset: { role: 'hit-score' },
        // 分值是"贡献了多少分"：正分带 `+`（累计进来），负分（减分规则）保留 `-`
        text: isNum(h.score) ? (h.score >= 0 ? `+${h.score}` : String(h.score)) : '—' }),
      ui.h('span', { class: 'decision-reason', text: h.reason ? String(h.reason) : '—' }),
    ]),
  );
  table.dataset.role = 'hit-table';
  return ui.h('div', { class: 'decision-hits' }, table);
}

async function loadDecision() {
  if (decisionAbort) decisionAbort.abort();
  decisionAbort = new AbortController();
  const { signal } = decisionAbort;
  const seq = ++decisionSeq;

  if (!state.event) {
    const ph = ui.empty('尚未查询判定',
      `填入事件编号后查询（默认演示事件 ${DEFAULT_EVENT}）：右栏展示该事件**真实落库**的决策（模块 05 的产出）`);
    ph.dataset.role = 'decision-placeholder';
    ui.mount(refs.decisionHolder, ph);
    return;
  }

  ui.mount(refs.decisionHolder, ui.loading(3));
  try {
    // silent：事件不存在（EVT-4404）是**预期分支**（异步落库还没写完），由本区块自己渲染，
    // 不该再弹一条全局 toast 抢走审核员的注意力。
    const data = await api.request(`${api.API_PREFIX}/events/${encodeURIComponent(state.event)}`,
      { signal, silent: true });
    if (signal.aborted || seq !== decisionSeq) return;
    const decision = (data && data.decision) || null;
    // 决策块缺失 = 该事件没走到决策环节（例如 03 在 feature 阶段就短路了，决策 D44/N-03-3），
    // 这正是"数据异常"空态要如实表达的情况，而不是伪造一个 0 分结论。
    ui.mount(refs.decisionHolder, renderDecisionCard(decision, {
      eventId: state.event, hits: (data && data.hits) || [], degrade: (data && data.degrade) || null,
    }));
  } catch (e) {
    if (e.name === 'AbortError' || signal.aborted || seq !== decisionSeq) return;
    // 404 的两种含义（事件还没落库 / 路由没上线）在页面上都要如实说清楚，
    // 因此把后端文案原样带出来，而不是自己编一句"暂无数据"。
    const isMissing = e.status === 404 || e.status === 0;
    ui.mount(refs.decisionHolder, [
      ui.banner('warn', isMissing
        ? `该事件暂无可读取的详情：${e.message}（${e.code || 'HTTP ' + e.status}）`
        : `决策记录读取失败：${e.message}（trace ${traceOf(e)}）`),
      ui.h('div', { class: 'form-actions' }, [
        ui.button('重试', { variant: 'ghost', onClick: () => loadDecision() }),
      ]),
    ]);
  }
}

// ==================== ② 关联实体图谱（Spec §2.2）====================

function legendNode() {
  const item = (kind, label) => ui.h('span', { class: 'lg-item' }, [
    ui.h('span', { class: `lg-swatch lg-${kind}` }),
    ui.h('span', { text: label }),
  ]);
  // 五类必须齐全：ECharts 自带的 legend 只能表达**节点类目**，画不出"风险边/普通边"，
  // 所以图例用 DOM 实现——这样边的两类也能进图例，且样式与页面其余部分一致。
  return ui.h('div', { class: 'graph-legend' }, [
    item('center', '中心节点'), item('hop1', '一跳实体'), item('hop2', '二跳账号'),
    item('risk', '风险边'), item('normal', '普通边'),
  ]);
}

/** 节点是否中心：以后端 `is_center` 为准，缺失时退化为 hop=0。 */
const isCenter = (n) => n.is_center === true || Number(n.hop) === 0;
const catOf = (n) => (isCenter(n) ? 0 : (Number(n.hop) <= 1 ? 1 : 2));

function buildOption(data) {
  const cCenter = themeVar('--graph-center', '#223248');
  const cHop1 = themeVar('--graph-hop1', '#4A6FA5');
  const cHop2 = themeVar('--graph-hop2', '#B4BFCC');
  const cEdge = themeVar('--graph-edge', '#C8CDD4');
  const cRisk = themeVar('--graph-edge-risk', '#D93025');
  const cText = themeVar('--text', '#333333');

  const nodes = (data.nodes || []).map((n) => {
    const center = isCenter(n);
    const hop = Number(n.hop) || 0;
    return {
      id: String(n.id),
      name: String(n.id),
      // 下面这些字段既是给 ECharts 的样式依据，也是 tooltip 与抽屉的数据源。
      // 注意 `nodeLabel` 与 ECharts 的 `label`（标签样式）**不能同名**，
      // 否则后者会覆盖前者，tooltip 里就会打印出一个配置对象。
      nodeLabel: n.label || String(n.id),
      nodeType: n.type || 'user',
      hop,
      linkedUserCnt: (n.linked_user_cnt === undefined ? null : n.linked_user_cnt),
      riskLevel: n.risk_level || '',
      riskTags: n.risk_tags || [],
      isCenter: center,
      category: catOf(n),
      // 中心大号 / 一跳中号 / 二跳小号（Spec §2.2）
      symbolSize: center ? 46 : (hop <= 1 ? 30 : 20),
      itemStyle: { color: center ? cCenter : (hop <= 1 ? cHop1 : cHop2), borderColor: '#FFFFFF', borderWidth: 1.5 },
      label: { show: true, fontSize: center ? 11 : 10, color: cText,
        formatter: () => (center ? (n.label || String(n.id)) : '') },
    };
  });

  const links = (data.edges || []).map((e) => {
    const risk = e.risk_flag === true;
    return {
      source: String(e.from),
      target: String(e.to),
      relation: e.relation || '',
      weight: e.weight === null || e.weight === undefined ? null : e.weight,
      riskFlag: risk,
      lastSeenAt: e.last_seen_at || null,
      lineStyle: risk
        ? { color: cRisk, width: 2, opacity: 0.95 }
        : { color: cEdge, width: 1.2, opacity: 0.9 },
    };
  });

  return {
    // 关闭动画：力导向布局动画期间节点还在移动，'click' 命中的像素坐标不稳定；
    // 画布只有 ~190px，也不需要动画来体现层次。
    animation: false,
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: (p) => {
        if (p.dataType === 'edge') {
          const d = p.data || {};
          return `${esc(d.relation)}<br/>关联次数：${d.weight === null ? '—' : esc(d.weight)}`
            + (d.riskFlag ? '<br/><b>风险关联</b>' : '');
        }
        const d = p.data || {};
        const cnt = d.linkedUserCnt;
        return `${esc(d.nodeLabel)}<br/>类型：${esc(TYPE_LABEL[d.nodeType] || d.nodeType)}`
          + `<br/>跳数：${esc(d.hop)}`
          + (cnt === null || cnt === undefined ? '' : `<br/>关联账号：${esc(cnt)} 个`);
      },
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      symbol: 'circle',
      // layoutAnimation=false：布局一次性算完，坐标在 setOption 后即稳定（E2E 点击需要）
      force: { repulsion: 160, edgeLength: [40, 90], gravity: 0.08, layoutAnimation: false },
      categories: [{ name: '中心节点' }, { name: '一跳实体' }, { name: '二跳账号' }],
      label: { show: true, position: 'right', fontSize: 10, color: cText },
      edgeSymbol: ['none', 'none'],
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      data: nodes,
      links,
    }],
  };
}

/** 中心实体的展示文案：`用户 U000128`。
 *  后端的 `label` 可能已经是 `用户 U000128` 也可能只是 `U000128`，两种情况都要拼得对，
 *  否则副标题会出现「中心：用户 用户 U000128」这种重复。 */
function centerText(center, fallbackId) {
  const typeLabel = TYPE_LABEL[center.type] || center.type || '';
  const label = String(center.label === undefined || center.label === null ? '' : center.label).trim();
  const id = String(center.id === undefined || center.id === null ? '' : center.id).trim() || fallbackId;
  if (label && label !== id) return label.startsWith(typeLabel) ? label : `${typeLabel} ${label}`;
  return `${typeLabel} ${id}`.trim();
}

/** 依据后端返回渲染图谱；`truncated` 必须显式提示（BR-09-18，不允许静默截断）。 */
async function drawGraph(data) {
  const echarts = await ui.loadECharts();
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  const center = data.center || {};
  const centerLabel = centerText(center, state.gid);
  const hop1 = nodes.filter((n) => Number(n.hop) === 1);
  const hop2 = nodes.filter((n) => Number(n.hop) === 2);
  const riskEdges = edges.filter((e) => e.risk_flag === true).length;
  // 孤立账号的判据是"**一条边都没有**"，而不是"节点数为 0"：
  // 中心节点本身永远会返回，所以孤立账号拿到的是"1 个节点 + 0 条边"。
  // 同时必须放行截断（`total_edges > 0`）：`?max_nodes=1` 时也可能只剩中心节点，
  // 那时要显示的是截断提示而不是"孤立账号"——两者意思完全相反。
  const totalEdges = Number(data.total_edges);
  const noRelation = edges.length === 0 && !(Number.isFinite(totalEdges) && totalEdges > 0);

  // 副标题按原型给出"这个图里到底有什么"的摘要：只有节点/关系总数的话，
  // 审核员还得自己数图例；一跳实体直接列名，二跳给出账号数，一眼能看出团伙规模。
  ui.mount(refs.graphSub,
    ui.h('div', { text: `力导向图 · ${state.maxHop} 跳（AD-06） · 中心：${centerLabel}` }),
    ui.h('div', { text: `一跳：${hop1.length ? hop1.map((n) => `${n.label || n.id}`).join('、') : '无'}` }),
    ui.h('div', { text: `二跳：${hop2.length} 个账号 · 节点 ${nodes.length} · 关系 ${edges.length}`
      + (Number.isFinite(data.elapsed_ms) ? ` · 耗时 ${data.elapsed_ms} ms` : '') }),
    ui.h('div', { text: `红色边 = 已判定风险关联（${riskEdges} 条）` }));

  // 原始图数据挂到画布节点上：E2E 与人工排查都需要核对"接口到底给了哪些节点"，
  // 而从 ECharts 实例里反解数据既不稳定也不完整。
  refs.canvas.__graph = { center, nodes, edges,
    truncated: data.truncated === true,
    total_nodes: data.total_nodes, total_edges: data.total_edges };

  if (!nodes.length || noRelation) {
    // 中心是用户时按 Spec §2.2 的原文说"该用户…（孤立账号）"；中心换成设备/IP/地址时
    // 必须换称呼——把设备中心也叫"该用户"会让人以为点错了页面。
    const isUserCenter = (center.type || state.gtype) === 'user';
    showGraphEmpty(
      isUserCenter ? '该用户暂无关联实体（孤立账号）' : '该实体暂无关联实体（孤立节点）',
      isUserCenter ? '该账号尚未与任何设备 / IP / 收货地址产生关联' : '该实体尚未与任何账号产生关联');
    return;
  }

  refs.canvas.classList.remove('is-empty');
  ui.clear(refs.graphNote);
  if (chart) { try { chart.dispose(); } catch (e) { /* 已销毁则忽略 */ } }
  chart = echarts.init(refs.canvas);
  chart.setOption(buildOption(data));

  chart.on('click', (p) => {
    if (!p || p.dataType === 'edge' || !p.data) return;
    openDrawer(p.data);
  });

  if (data.truncated === true) {
    ui.mount(refs.graphNote, ui.h('div', { class: 'graph-truncate', text:
      `关系过多，已展示关联强度最高的 ${nodes.length} 个节点（共 ${data.total_nodes} 个）` }));
  } else {
    ui.mount(refs.graphNote, ui.h('div', { class: 'hint', text:
      `图完整：共 ${nodes.length} 个节点、${edges.length} 条关系（未发生截断）` }));
  }
}

function showGraphEmpty(title, hint) {
  if (chart) { try { chart.dispose(); } catch (e) { /* 已销毁则忽略 */ } chart = null; }
  refs.canvas.classList.add('is-empty');
  refs.canvas.__graph = null;
  // 副标题不能停在"加载中…"：空态/失败态也要如实说明中心是谁，否则像是在等数据
  ui.mount(refs.graphSub, ui.h('div', {
    text: `力导向图 · ${state.maxHop} 跳（AD-06） · 中心：${TYPE_LABEL[state.gtype] || state.gtype} ${state.gid}`,
  }));
  ui.mount(refs.graphNote, ui.empty(title, hint));
}

async function loadGraph() {
  if (graphAbort) graphAbort.abort();
  graphAbort = new AbortController();
  const { signal } = graphAbort;
  const seq = ++graphSeq;
  refs.canvas.classList.remove('is-empty');
  ui.mount(refs.graphSub, `加载中…`);
  ui.mount(refs.graphNote, ui.loading(1));

  const query = { max_hop: state.maxHop, max_nodes: state.maxNodes };
  if (state.riskOnly) query.risk_only = 'true';

  try {
    const data = await api.request(`${api.API_PREFIX}/graph/${state.gtype}/${encodeURIComponent(state.gid)}`,
      { query, signal, silent: true });
    if (signal.aborted || seq !== graphSeq) return;
    await drawGraph(data);
    if (seq !== graphSeq) return;
  } catch (e) {
    if (e.name === 'AbortError' || signal.aborted || seq !== graphSeq) return;
    // 空态区分（Spec §2.2）："无关联（孤立账号）"与"查不到（404）"是两件事，
    // 文案不同、含义不同——前者是结论，后者是查询失败。
    // 只有 GRP-4004 才是"实体不存在"；其它 404 是路由没上线，必须如实报错。
    if (isDomainNotFound(e)) {
      showGraphEmpty('该用户不存在，无法生成关联图谱',
        `中心实体 ${state.gtype}/${state.gid} 未找到（${e.code}）`);
    } else {
      showGraphEmpty('关联图谱加载失败', `${e.message}（trace ${traceOf(e)}）`);
    }
  }
}

// ==================== ⑤ 实体详情抽屉（点击节点后从右侧滑出）====================

function closeDrawer() {
  if (!refs || !refs.drawerRoot) return;
  const el = refs.drawerRoot.querySelector('.entity-drawer');
  if (el) el.classList.remove('open');
}

function openDrawer(d) {
  const tags = (d.riskTags || []).length
    ? ui.h('div', { class: 'tag-row' }, (d.riskTags || []).map((t) => ui.tag(t, 'sev-low')))
    : ui.h('span', { class: 'hint', text: '无' });
  const kv = (k, v) => ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: k }),
    ui.h('span', { class: 'v' }, v),
  ]);

  const actions = [];
  if (d.nodeType === 'user' && !d.isCenter) {
    // "能切到该用户画像更好"：把中心换成该账号，画像卡与图谱一起重载
    actions.push(ui.button('查看该用户画像', { variant: 'primary', onClick: () => {
      closeDrawer();
      go({ user: d.id, gtype: null, gid: null });
    } }));
  } else if (!d.isCenter) {
    actions.push(ui.button('以该实体为中心看图谱', { variant: 'primary', onClick: () => {
      closeDrawer();
      go({ gtype: d.nodeType, gid: d.id });
    } }));
  }
  actions.push(ui.button('关闭', { variant: 'ghost', onClick: closeDrawer }));

  const drawer = ui.h('div', { class: 'entity-drawer' }, [
    ui.h('div', { class: 'drawer-head' }, [
      ui.h('div', { class: 'drawer-title', text: d.nodeLabel || d.id }),
      ui.h('span', { class: 'hint', text: TYPE_LABEL[d.nodeType] || d.nodeType }),
    ]),
    ui.h('div', { class: 'kv-list' }, [
      kv('实体编号', ui.h('span', { class: 'mono', text: d.id })),
      kv('跳数', `${d.hop} 跳${d.isCenter ? '（中心）' : ''}`),
      kv('风险等级', d.riskLevel ? ui.riskTag(d.riskLevel) : '—'),
      kv('风险标签', tags),
      kv('关联账号数', d.linkedUserCnt === null || d.linkedUserCnt === undefined
        ? '—' : `${d.linkedUserCnt} 个`),
    ]),
    ui.h('div', { class: 'drawer-actions' }, actions),
    ui.h('div', { class: 'hint', text: '节点与边均为只读展示；状态变更请走案件处置流程（模块 08）。' }),
  ]);

  ui.mount(refs.drawerRoot, ui.h('div', { class: 'drawer-mask', onclick: closeDrawer }), drawer);
  // 下一帧再加 open 类：让 transition 真正发生（同一个 style 批次内改动不会产生过渡）
  requestAnimationFrame(() => { if (refs && refs.drawerRoot.contains(drawer)) drawer.classList.add('open'); });
}

// ==================== ⑥ 模块 07：案件驱动模式（中栏 + 右栏由同一次详情响应渲染）====================
//
// BR-07-06：点左栏案件行 → 发起**一次** `GET /api/v1/cases/{case_no}` → 中栏与右栏用**同一次**
// 响应渲染（原型注记第 1 条"不得异步错位"）；**不允许**中栏/右栏各自发请求，所以本段里
// 没有第二个取数函数——详情响应里的 `profile` / `graph` / `decision` / `hits` 分别交给
// 09 与 05 **已有的**渲染函数（`profileCard` / `drawGraph` / `renderDecisionCard`）。
// 另写一套渲染就会出现"同一个画像卡长得不一样"的分裂，那正是本模块最大的风险（任务书 §0）。
//
// 没有 `?case=` 时本模式完全不介入：`#/cases` 的既有行为（`?user=` / `?event=` 驱动）一字未改。

/** `degraded_parts` 的别名表：后端可能给 `profile`/`portrait`，也可能给中文，都要认。 */
const DEGRADE_ALIAS = {
  profile: ['profile', 'profiles', 'portrait', 'user_profile', '画像'],
  graph: ['graph', 'graphs', 'network', '图谱'],
  snapshot: ['snapshot', 'snapshots', 'feature', 'features', '快照'],
  event: ['event', 'events', '事件'],
  decision: ['decision', 'decisions', 'hits', '判定'],
  actions: ['actions', 'case_actions', '流水'],
};

function degradedSet(raw) {
  const out = new Set();
  (Array.isArray(raw) ? raw : []).forEach((x) => out.add(String(x === null || x === undefined ? '' : x).toLowerCase()));
  return out;
}

function isPartDegraded(set, part) {
  const alias = DEGRADE_ALIAS[part] || [part];
  return alias.some((a) => set.has(String(a).toLowerCase()));
}

/** 分区降级占位（BR-07-12）：**只把这一个区块**标成失败 + 重试，其余证据照常可见。 */
function partFailCard(label, part, hint) {
  const card = ui.card(null, [
    ui.banner('warn', `${label}加载失败：服务端在 degraded_parts 里给出了 ${part}（其余分区不受影响）`),
    ui.h('div', { class: 'hint', text: hint || '分区降级不是整页失败——请用其余证据完成研判，并点重试补齐本区块（BR-07-12 / CASE-5001）。' }),
    ui.h('div', { class: 'form-actions' }, [
      ui.button('重试', { variant: 'ghost', onClick: () => loadCaseDetail() }),
    ]),
  ]);
  card.classList.add('case-degraded');
  card.dataset.degraded = part;
  return card;
}

/** 契约缺口（既没数据、也不在 `degraded_parts` 里）：如实说明，不静默、不白屏。 */
function contractGapCard(label, part) {
  const card = ui.card(null, [
    ui.banner('err', `${label}缺失：详情响应里没有这块数据，且 degraded_parts 里也没有 ${part}`),
    ui.h('div', { class: 'hint', text: '这是接口契约缺口而不是"没有数据"——请核对 GET /api/v1/cases/{case_no} 的响应结构。' }),
    ui.h('div', { class: 'form-actions' }, [
      ui.button('重试', { variant: 'ghost', onClick: () => loadCaseDetail() }),
    ]),
  ]);
  card.classList.add('case-degraded');
  card.dataset.degraded = part;
  return card;
}

/** `yyyy-MM-dd HH:mm:ss.SSS`（Spec §2.3.2 的 `evtTime` 口径）。 */
function fmtTsMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const d = new Date(n);
  const p = (x, w = 2) => String(x).padStart(w, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** 金额统一"分 → 元"（Spec §2.3.2：`amount` 按分存储、元展示，两位小数）。 */
function yuan(v) {
  const n = Number(v);
  if (v === null || v === undefined || !Number.isFinite(n)) return '—';
  return `${(n / 100).toFixed(2)} 元`;
}

/** `scene_extra` 的关键字段（Spec §2.3.2：按 `event_type` 展示对应关键字段）。 */
function sceneExtraText(se) {
  if (!se || typeof se !== 'object') return '';
  return Object.entries(se).map(([k, v]) => {
    // 只对**看起来是金额**的键做分转元：猜错会把 20000 次读成 200.00 元
    const val = (typeof v === 'number' && /amount|price|fee|money|refund/i.test(k))
      ? `${(v / 100).toFixed(2)} 元（${v} 分）` : String(v);
    return `${k}=${val}`;
  }).join(' · ');
}

/**
 * 事件卡（Spec §2.3.2）。归属 **03/04**（事件与快照），本模块只格式化展示、不重算。
 * `detail.event` 就是详情响应里的那一块，因此与画像/判定**同源同帧**。
 */
function eventCard(ev, deg) {
  if (isPartDegraded(deg, 'event')) return partFailCard('当前触发事件', 'event');
  if (!ev) return contractGapCard('当前触发事件', 'event');
  const kv = (k, v) => ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: k }), ui.h('span', { class: 'v' }, v),
  ]);
  const cards = ui.card('当前触发事件', [
    ui.h('div', { class: 'kv-list', dataset: { role: 'event-card' } }, [
      kv('事件编号', ui.h('span', { class: 'mono', text: ev.event_id || '—' })),
      // 「中文附注 + 取值」两个面都留着：中文给人读，取值给对日志/接口用（Spec §2.3.2）
      kv('事件类型', `${labelOf('event_type', ev.event_type, ev.event_type || '—')}（${ev.event_type || '—'}）`),
      kv('业务单据号', ev.biz_no
        ? ui.h('span', { class: 'mono', text: ev.biz_no }) : '—'),
      kv('请求时间戳', ui.h('span', { class: 'mono', text: fmtTsMs(ev.ts) })),
      kv('涉事用户', ui.h('span', { class: 'mono', text: ev.user_id || '—' })),
      kv('业务金额', yuan(ev.amount)),
      kv('设备 / IP', ui.h('span', { class: 'mono', text: `${ev.device_id || '—'} / ${ev.ip || '—'}` })),
      sceneExtraText(ev.scene_extra)
        ? kv('场景附加', ui.h('span', { class: 'mono', text: sceneExtraText(ev.scene_extra) })) : null,
    ]),
  ]);
  cards.dataset.role = 'event-part';
  return cards;
}

// ==================== 特征快照卡（Spec §2.3.3；基线值来自 04 的 `/features/meta`）====================
//
// **决策 D68**：快照对比列的"基线值"直接复用模块 04 已交付的**静态参考区间**
// （`GET /api/v1/features/meta` 的 `baseline` / `baseline_desc`），**不新增 09 的聚合口径**，
// 也**不在前端自己算统计**。这样对比列与 04 的标签配色同源，不会出现两套口径。
// 「偏离」列按 Spec §2.3.3 的表做**展示格式化**（不是风控判定）。

/** 取 04 的基线表（进程内缓存一次；失败返回空表，快照卡退化为"无基线"而不是整卡失败）。 */
function loadFeatureMeta() {
  if (featureMeta) return Promise.resolve(featureMeta);
  if (!featureMetaInflight) {
    featureMetaInflight = api.request(`${api.API_PREFIX}/features/meta`, { silent: true })
      .then((data) => { featureMeta = (data && data.items) || []; return featureMeta; })
      .catch(() => [])
      .finally(() => { featureMetaInflight = null; });
  }
  return featureMetaInflight;
}

/** 从 `≤3` / `2~5` / `≥24` / `≤5%` 这类基线文案里解出区间；解不出返回 null（该列显示 `—`）。 */
function parseBaseline(raw) {
  const s = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!s || s === '__none__') return null;
  const nums = (s.match(/\d+(?:\.\d+)?/g) || []).map(Number);
  if (!nums.length) return null;
  if (s.includes('~') && nums.length >= 2) {
    return { lo: Math.min(nums[0], nums[1]), hi: Math.max(nums[0], nums[1]) };
  }
  const hasUpper = /[≤<]/.test(s);
  const hasLower = /[≥>]/.test(s);
  if (hasUpper && !hasLower) return { lo: null, hi: nums[0] };
  if (hasLower && !hasUpper) return { lo: nums[0], hi: null };
  return { lo: nums[0], hi: nums[0] };
}

/** Spec §2.3.3 的偏离渲染表（**仅为展示格式化，不是风控判定**）。 */
function deviationOf(value, meta) {
  const m = meta || {};
  if (value === null || value === undefined) return { text: '—', cls: 'na' };
  const dt = String(m.data_type || '');
  if (dt === 'bool' || typeof value === 'boolean') {
    return value === true ? { text: '▲ 命中', cls: 'hot' } : { text: '正常', cls: 'ok' };
  }
  if (dt === 'str' || m.has_baseline === false) return { text: '—', cls: 'na' };
  const n = Number(value);
  if (!Number.isFinite(n)) return { text: '—', cls: 'na' };
  const b = parseBaseline(m.baseline);
  if (!b) return { text: '—', cls: 'na' };
  if (b.hi !== null && n > b.hi) {
    return b.hi > 0
      ? { text: `▲ ${(n / b.hi).toFixed(1)} 倍`, cls: 'hot' }
      : { text: `▲ 超基线（>${b.hi}）`, cls: 'hot' };
  }
  if (b.lo !== null && n < b.lo) {
    return b.lo > 0
      ? { text: `▼ ${(n / b.lo).toFixed(1)} 倍`, cls: 'low' }
      : { text: `▼ 低于基线（<${b.lo}）`, cls: 'low' };
  }
  return { text: '正常', cls: 'ok' };
}

function fmtFeatureValue(v, unit) {
  if (typeof v === 'boolean') return v ? '是' : '否';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const s = Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
  if (!unit) return s;
  return String(unit) === '%' ? `${s}%` : `${s} ${unit}`;
}

function snapshotCard(snap, metaItems, deg, inlineBaseline) {
  if (isPartDegraded(deg, 'snapshot')) return partFailCard('特征上下文快照', 'snapshot');
  if (!snap) return contractGapCard('特征上下文快照', 'snapshot');

  const features = (snap.features && typeof snap.features === 'object') ? snap.features : {};
  const missing = new Set((snap.missing_features || []).map((x) => String(x)));
  const reasonOf = snap.missing_reasons || {};
  // 基线值有两个同源入口，都要认：
  //  ① 详情响应内联的 `baseline`（后端 `baseline_meta()`，与 `/features/meta` 同一个纯函数）；
  //  ② `GET /api/v1/features/meta`（标签 / 单位 / 类型 / 顺序**只有它**有）。
  // 两者同源（D68：复用 04 的静态参考区间），因此直接合并，缺失的键各自由另一方补上。
  const inline = (inlineBaseline && typeof inlineBaseline === 'object') ? inlineBaseline : {};
  const byKey = new Map((metaItems || []).map((m) => [String(m.key), m]));
  Object.keys(inline).forEach((k) => {
    byKey.set(String(k), Object.assign({ key: String(k) }, byKey.get(String(k)) || {}, inline[k]));
  });
  // 顺序：先按 04 的基线表（**18 项固定顺序**，Spec §2.3.3 明确"不随机排序"），
  // 再把详情里多出来的键补在后面——后端加了特征而 04 的表还没跟上时，它也必须可见。
  const order = (metaItems || []).map((m) => String(m.key));
  Object.keys(inline).forEach((k) => { if (!order.includes(k)) order.push(k); });
  Object.keys(features).forEach((k) => { if (!order.includes(k)) order.push(k); });

  const rows = order.map((key) => {
    const meta = byKey.get(key) || {};
    const value = Object.prototype.hasOwnProperty.call(features, key) ? features[key] : undefined;
    const isMissing = value === null || value === undefined || missing.has(key);
    const dev = deviationOf(isMissing ? null : value, meta);
    const nameCell = ui.h('div', { class: 'snap-name' }, [
      ui.h('div', { text: meta.label || key }),
      ui.h('div', { class: 'mono hint', text: key }),
    ]);
    const valCell = isMissing
      ? ui.h('span', { class: 'pv-na', title: reasonOf[key] || '快照未包含该特征（missing_features）',
        text: '—（缺失）' })
      : ui.h('span', { class: 'mono', text: fmtFeatureValue(value, meta.unit) });
    const baseCell = (meta.has_baseline === false || meta.baseline === undefined)
      ? ui.h('span', { class: 'pv-na', text: '—' })
      : ui.h('span', { class: 'mono', title: meta.baseline_desc || '', text: String(meta.baseline) });
    return ui.h('tr', { dataset: { feature: key, missing: isMissing ? '1' : '0' } }, [
      ui.h('td', {}, nameCell),
      ui.h('td', {}, valCell),
      ui.h('td', {}, baseCell),
      ui.h('td', {}, ui.h('span', { class: `snap-dev dev-${dev.cls}`, text: dev.text })),
    ]);
  });

  const table = ui.table(
    [{ name: '特征名' }, { name: '本次值' }, { name: '基线值' }, { name: '偏离' }], rows);
  table.classList.add('snap-tbl');
  table.dataset.role = 'snapshot-table';
  const wc = snap.window_config || {};
  const card = ui.card('特征上下文快照', [
    ui.h('div', { class: 'snap-wrap' }, table),
    ui.h('div', { class: 'hint' }, [
      `窗口：短 ${wc.short_window_min === undefined ? '—' : wc.short_window_min} 分钟 / 长 `
      + `${wc.long_window_min === undefined ? '—' : wc.long_window_min} 分钟 · 聚合 ${wc.agg_mode || '—'}`
      + ` · 计算于 ${fmtTsMs(snap.computed_at)}`
      + ` · 缺失 ${missing.size} 项（快照异步落库，可重试）`,
    ]),
    ui.h('div', { class: 'hint', text:
      '基线值取自模块 04 的静态参考区间（GET /api/v1/features/meta，决策 D68）：本页**不算任何统计**，'
      + '偏离列只是展示格式化，不是风控判定。' }),
  ]);
  card.dataset.role = 'snapshot-part';
  return card;
}

// ==================== 处置区（只收集，提交给 08；BR-07-21）====================

/**
 * 研判与处置区（Spec §2.4.3）。
 *
 * **本模块不调处置执行接口**（BR-07-21）：表单只收集「结论 + 动作 + 备注」，然后交给
 * 08 的 `openDisposeFlow`（它自己走 preview → 二次确认弹窗 → dispose）。
 * 结论×动作的相容性用 08 导出的 `incompatibleReason`（与后端 `case_schema` 同规则）做
 * **复选框动态禁用与原因提示**；`pass` 不在复选框里（Spec §2.4.3：它对应「放行」按钮）。
 */
function disposePanel(c, caseNo) {
  const status = String((c && c.status) || '');
  const hasDisposePerm = (session.permissions() || []).includes('case:dispose');
  // BR-07-19：pending 时整体禁用（先认领）；reviewing 可编辑；disposed/archived 只读回显
  const editable = status === 'reviewing' && hasDisposePerm;
  const form = { conclusion: '', actions: [], remark: '' };
  const host = ui.h('div', { class: 'dp-box', dataset: { role: 'dispose-panel', status } });
  // 三个提交按钮的句柄：备注是**打字**（每次按键都重画整个面板会丢焦点），
  // 所以按键时只刷新按钮的可用态与悬停原因，不重画面板。
  const btns = {};

  const lockedText = status === 'pending' ? '案件尚未认领：请先点左栏的「认领」再处置（BR-07-19）'
    : status === 'disposed' ? '案件已处置：处置区只读回显，不可重复提交'
      : status === 'archived' ? '案件已归档：处置区只读回显'
        : !hasDisposePerm ? '当前角色无 case:dispose 权限（矩阵里仅审核员，D69）；前端隐藏按钮不替代后端鉴权（BR-07-25）'
          : '';

  /** Spec §2.4.3 的六条提交前置校验：返回 null 表示可提交，否则返回**给人看的**原因。 */
  function blocked(kind) {
    if (!caseNo) return '请先选择案件';
    if (status === 'disposed' || status === 'archived') return '该案件已处置，不可重复提交';
    if (status === 'pending') return '请先认领案件';
    if (!hasDisposePerm) return '当前角色无处置权限（case:dispose 仅审核员）';
    if (kind === 'submit') {
      if (!form.conclusion) return '请选择处理结论';
      if (!normalizeActions(form.actions).length) return '请至少选择一项联动处置动作';
    }
    if (!form.remark.trim()) return '请填写处置原因备注';
    if (form.remark.trim().length > REMARK_MAX) {
      return `处置原因备注最多 ${REMARK_MAX} 字（当前 ${form.remark.trim().length} 字）`;
    }
    return null;
  }

  function run(kind) {
    const why = blocked(kind);
    if (why) { ui.toast(why, 'err', 5000); return; }
    const fd = kind === 'release'
      // 「放行」：conclusion=normal + [pass]（Spec §2.4.3；pass 不在复选框里）
      ? { conclusion: 'normal', action_types: ['pass'] }
      // 「违规拦截」：conclusion=violation + 勾选项（未勾时默认 block_order）
      : kind === 'reject'
        ? { conclusion: 'violation', action_types: form.actions.length ? form.actions : ['block_order'] }
        : { conclusion: form.conclusion, action_types: form.actions };
    fd.remark = form.remark.trim();
    // **交接给 08**：preview → 二次确认弹窗 → dispose 都由它负责，本模块不碰执行接口
    openDisposeFlow(caseNo, fd, {
      feedbackHost: refs.caseFeedback,
      onDone: () => {
        feedbackFor = caseNo;
        // BR-07-22：成功后**重新拉取**列表行与详情（状态已由 08 改掉），不做本地乐观更新
        if (caseList) caseList.load();
        loadCaseDetail();
      },
    });
  }

  /**
   * 只刷新三个按钮的"可提交性"（不重建面板）。
   *
   * **这是一个真实缺陷的修复**：备注是 textarea，如果每次 `input` 都整面板重画，
   * 光标会跳到开头（等于没法打字）；而如果在 `input` 里什么都不做，用户填完备注后
   * 「提交处置」会**一直停在置灰状态**——BR-07-20 要求的是"任一不满足则置灰"，
   * 满足之后必须立刻恢复可点。模块 07 的 E2E 正是踩到后者（弹窗一直不出现）。
   */
  function refreshButtons() {
    ['release', 'reject', 'submit'].forEach((kind) => {
      const b = btns[kind];
      if (!b) return;
      const why = blocked(kind);
      b.disabled = !!why;
      b.title = why || '';
    });
  }

  function paint() {
    const conflict = incompatibleReason(form.conclusion, form.actions);
    const rblock = blocked('submit');
    const isBlocked = blocked('reject');
    const relBlocked = blocked('release');
    const opt = (label, checked, disabled, onPick, title) => ui.h('label', {
      class: 'dp-opt' + (disabled ? ' is-disabled' : '') + (checked ? ' is-on' : ''), title: title || '',
    }, [
      ui.h('input', {
        type: 'checkbox', checked: checked ? 'checked' : null,
        disabled: disabled ? 'disabled' : null, onchange: onPick,
      }),
      ui.h('span', { text: label }),
    ]);
    const btn = (label, variant, kind, why, role) => {
      const b = ui.button(label, { variant, disabled: !!why, title: why || '', onClick: () => run(kind) });
      b.dataset.role = role;
      btns[kind] = b;
      return b;
    };
    ui.mount(host, [
      ui.h('div', { class: 'kv-list' }, [
        ui.h('div', { class: 'kv' }, [
          ui.h('span', { class: 'k', text: '案件状态' }),
          ui.h('span', { class: 'v' }, [case_list_statusTag(status, c.status_label),
            c.assignee ? ui.h('span', { class: 'hint', text: ` · 认领人 ${c.assignee}` }) : null]),
        ]),
      ]),
      lockedText ? ui.h('div', { class: 'impact-note', text: lockedText }) : null,
      ui.h('div', { class: 'dp-block' }, [
        ui.h('div', { class: 'dp-title', text: '处理结论（必选其一）' }),
        ui.h('div', { class: 'dp-opts' }, CONCLUSIONS.map((v) => opt(
          labelOf('conclusion', v, v), form.conclusion === v, !editable,
          () => { form.conclusion = v; paint(); },
          // Spec §2.4.3 的原型文案放在 title 上：正文用服务端枚举标签（BR-00-18 不硬编码）
          { violation: '确认违规', normal: '确认为正常', suspicious: '存疑待观察' }[v]))),
      ]),
      ui.h('div', { class: 'dp-block' }, [
        ui.h('div', { class: 'dp-title', text: '联动处置动作（可多选；放行请用下方「放行」按钮）' }),
        ui.h('div', { class: 'dp-opts' }, ACTION_TYPES.filter((a) => a !== 'pass').map((a) => {
          // 结论为「正常/存疑」时，除 pass 外的动作一律禁用（BR-08-14 的相容矩阵，同源 08）
          const dis = !editable || (form.conclusion && form.conclusion !== 'violation');
          return opt(labelOf('action_type', a, a), form.actions.includes(a), dis, () => {
            const i = form.actions.indexOf(a);
            if (i >= 0) form.actions.splice(i, 1); else form.actions.push(a);
            paint();
          }, dis && form.conclusion && form.conclusion !== 'violation'
            ? '结论为『正常/存疑』时不可勾选拦截类动作' : '');
        })),
        conflict ? ui.h('div', { class: 'dp-conflict', dataset: { role: 'dp-conflict' }, text: `⚠ ${conflict}` })
          : null,
      ]),
      ui.h('div', { class: 'dp-block' }, [
        ui.h('div', { class: 'dp-title', text: `处置原因备注（必填，1~${REMARK_MAX} 字）` }),
        ui.h('textarea', {
          class: 'dp-remark', rows: '3', maxlength: String(REMARK_MAX),
          placeholder: '请写明判定依据（如：同设备聚集 6 个账号 + 代理 IP，结合命中明细判定为团伙刷单）',
          disabled: editable ? null : 'disabled',
          oninput: (e) => { form.remark = e.target.value; refreshButtons(); },
        }),
      ]),
      // 原型原文（Spec §2.4.3 固定注记）；与 08 弹窗里那句**同源**（从 08 导出，避免两处漂移）
      ui.h('div', { class: 'dispose-warn', text: WARN_LINE }),
      ui.h('div', { class: 'form-actions' }, [
        btn('放行', 'ok', 'release', relBlocked, 'dp-release'),
        btn('违规拦截', 'danger', 'reject', isBlocked, 'dp-reject'),
        btn('提交处置', 'primary', 'submit', rblock, 'dp-submit'),
      ]),
      ui.h('div', { class: 'hint', text:
        '提交后进入模块 08 的二次确认弹窗（副作用清单由服务端权威生成）；本模块**不直接调处置执行接口**（BR-07-21）。' }),
    ]);
    // textarea 的值不受 `text` 属性控制，重新 paint 后要把用户输入放回去（不许丢输入）
    const ta = host.querySelector('.dp-remark');
    if (ta && ta.value !== form.remark) ta.value = form.remark;
  }

  paint();
  const card = ui.card('研判与处置（提交给模块 08 执行）', host);
  card.dataset.role = 'dispose-card';
  return card;
}

/** 状态标签（与左栏同一口径；左栏导出的 `statusTag` 会引入循环依赖，故此处直接复用枚举）。 */
function case_list_statusTag(status, serverLabel) {
  const key = String(status || '');
  const label = key === 'pending' ? '待审' : (serverLabel || labelOf('case_status', key, key || '—'));
  const cls = { pending: 'medium', reviewing: 'brand', disposed: 'low', archived: 'plain' }[key] || 'plain';
  return ui.tag(label, cls);
}

/** 处置流水（**复用 08 的 `loadActions` / `renderActions`**，不重写表格）。 */
function actionsCard(caseNo) {
  const host = ui.h('div', { class: 'actions-holder', dataset: { role: 'case-actions' } });
  ui.mount(host, ui.loading(2));
  loadActions(caseNo).then((data) => {
    if (!host.isConnected) return;   // 已经切走（详情重绘）→ 丢弃过期结果
    renderActions(host, data);
  }).catch((e) => {
    if (!host.isConnected) return;
    ui.mount(host, [
      ui.banner('err', `处置流水读取失败：${disposeErrorText(e)}`),
      ui.h('div', { class: 'form-actions' }, [
        ui.button('重试', { variant: 'ghost', onClick: () => {
          ui.mount(host, ui.loading(2));
          actionsCard(caseNo);
        } }),
      ]),
    ]);
  });
  const card = ui.card('处置流水（按 acted_at 升序）', host);
  card.dataset.role = 'actions-card';
  return card;
}

// ==================== 详情响应 → 09/05 的渲染函数（**只做形状适配，不重写渲染**）====================

/** 明文 11 位号码才脱敏（BR-09-05：页面上**绝不**出现明文手机号）；已是掩码则原样。 */
function maskPhone(raw) {
  const s = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!s) return '';
  return /^\d{11}$/.test(s) ? `${s.slice(0, 3)}****${s.slice(7)}` : s;
}

/**
 * 把详情响应的 `profile` 适配成 09 的 `profileCard` 认得的形状。
 *
 * 两种形态都要认（否则会"另写一套画像卡"）：
 *  · 形态 A：09 的 `/profiles/{id}` 载荷原样内联（`user/stat/latest_decision/device/ip/address/tag_meta`）；
 *  · 形态 B：Spec 07 §3.2 的聚合形态（`{user, devices[], ips[], addresses[]}`）——
 *    这时按**事件里的 device_id / ip / address_id** 挑出本案件对应的那一条（挑不中就用第一条）。
 *    "挑哪一条"不是算统计，只是选展示对象；聚集度数字仍然原样来自后端。
 */
function adaptProfile(profile, ev, c, userId) {
  const p = (profile && typeof profile === 'object') ? profile : {};
  const event = ev || {};
  const caseDoc = c || {};
  const user = Object.assign({}, p.user || {});
  if (!user.user_id) user.user_id = userId;
  if (!user.phone_masked) user.phone_masked = maskPhone(event.phone);

  const shapeA = p.user || p.stat || p.device || p.ip || p.address || p.tag_meta;
  if (shapeA) {
    return {
      user,
      stat: p.stat || {},
      // 画像自带的"最近决策"优先；没有就用**案件上的冗余快照**（BR-07-02：案件上的
      // risk_score/risk_level 就是决策当时的快照，直接展示、不重算）
      latest_decision: p.latest_decision
        || (caseDoc.risk_score === undefined || caseDoc.risk_score === null ? null : {
          risk_score: caseDoc.risk_score, risk_level: caseDoc.risk_level,
          decision: caseDoc.decision, decided_at: caseDoc.created_at,
        }),
      device: p.device || null, ip: p.ip || null, address: p.address || null,
      tag_meta: p.tag_meta || {},
    };
  }

  const pick = (arr, keys, wanted) => {
    const list = Array.isArray(arr) ? arr : [];
    if (wanted) {
      const hit = list.find((x) => x && keys.some(
        (k) => x[k] !== undefined && x[k] !== null && String(x[k]) === String(wanted)));
      if (hit) return hit;
    }
    return list[0] || null;
  };
  return {
    user,
    stat: p.stat || {},
    latest_decision: p.latest_decision
      || (caseDoc.risk_score === undefined || caseDoc.risk_score === null ? null : {
        risk_score: caseDoc.risk_score, risk_level: caseDoc.risk_level,
        decision: caseDoc.decision, decided_at: caseDoc.created_at,
      }),
    device: pick(p.devices, ['device_id', '_id'], event.device_id),
    ip: pick(p.ips, ['ip', '_id'], event.ip),
    address: pick(p.addresses, ['address_id', '_id'], event.address_id),
    tag_meta: p.tag_meta || {},
  };
}

/** 详情里的 `graph` 补上 09 的 `drawGraph` 需要的 `center` / 计数（不存在的按实际值兜底）。 */
function adaptGraph(g, userId) {
  const graph = (g && typeof g === 'object') ? g : {};
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  return {
    center: graph.center || { type: 'user', id: userId, label: userId },
    nodes, edges,
    truncated: graph.truncated === true,
    total_nodes: graph.total_nodes === undefined ? nodes.length : graph.total_nodes,
    total_edges: graph.total_edges === undefined ? edges.length : graph.total_edges,
    elapsed_ms: graph.elapsed_ms,
  };
}

/** 图谱区失败占位（含重试），与 09 的空态文案区分开：这是"没取到"，不是"没有关联"。 */
function showGraphFail(title, hint) {
  if (chart) { try { chart.dispose(); } catch (e) { /* 已销毁则忽略 */ } chart = null; }
  refs.canvas.classList.add('is-empty');
  refs.canvas.__graph = null;
  ui.mount(refs.graphSub, ui.h('div', {
    text: `力导向图 · 中心：${TYPE_LABEL[state.gtype] || state.gtype} ${state.gid}`,
  }));
  ui.mount(refs.graphNote, [
    ui.empty(title, hint),
    ui.h('div', { class: 'form-actions' }, [
      ui.button('重试', { variant: 'ghost', onClick: () => loadCaseDetail() }),
    ]),
  ]);
}

/** 把**同一次**详情响应铺到中栏与右栏（BR-07-06：这里之后没有任何取数）。 */
async function renderCaseDetail(data) {
  const c = (data && data.case) || {};
  const ev = (data && data.event) || null;
  const caseNo = String(c.case_no || state.case);
  const userId = String(c.user_id || (ev && ev.user_id) || state.user || '').trim() || state.user;
  const deg = degradedSet(data && data.degraded_parts);

  // 中栏图中心必须跟着**本案件**的用户走：否则图谱副标题会写着另一个人的编号
  state.gtype = 'user';
  state.gid = userId;

  // ---- 中栏 ① 画像卡（09 的 profileCard）----
  if (isPartDegraded(deg, 'profile')) {
    ui.mount(refs.profileHolder, partFailCard('用户画像', 'profile'));
  } else if (!data.profile) {
    ui.mount(refs.profileHolder, contractGapCard('用户画像', 'profile'));
  } else {
    ui.mount(refs.profileHolder, profileCard(adaptProfile(data.profile, ev, c, userId)));
  }

  // ---- 中栏 ② 事件卡 + ③ 快照卡（07 新增；基线值来自 04 的 /features/meta）----
  const metaItems = await loadFeatureMeta();
  ui.mount(refs.caseMidBody, eventCard(ev, deg),
    snapshotCard(data.snapshot, metaItems, deg, data.baseline));

  // ---- 中栏 ④ 关联图谱（09 的 drawGraph）----
  const g = data.graph;
  if (isPartDegraded(deg, 'graph')) {
    showGraphFail('关联图谱加载失败（服务端分区降级）',
      '图谱服务暂不可用，中栏其余证据与右栏不受影响（BR-07-12 / CASE-5002）');
  } else if (!g) {
    showGraphFail('本案件未返回关联图谱数据', '接口契约缺口（degraded_parts 里也没有 graph）');
  } else {
    if (Number(g.max_hop) >= 1) state.maxHop = Math.min(MAX_HOP, Number(g.max_hop));
    await drawGraph(adaptGraph(g, userId));
  }

  // ---- 右栏 ① 判定摘要 + 命中明细（05 的 renderDecisionCard）----
  if (isPartDegraded(deg, 'decision')) {
    ui.mount(refs.decisionHolder, [
      ui.banner('warn', '未取到系统判定数据，请勿据此放行（CASE-5004）'),
      ui.h('div', { class: 'hint', text: '判定块取用失败：**绝不**用 0 分或"放行"填充（§5.2 fail-safe 红线）。' }),
      ui.h('div', { class: 'form-actions' }, [
        ui.button('重试', { variant: 'ghost', onClick: () => loadCaseDetail() }),
      ]),
    ]);
  } else {
    // decision 为 null 时走 05 自己的空态（「该事件未产生决策记录（数据异常）」），不显示 0 分
    ui.mount(refs.decisionHolder, renderDecisionCard(data.decision || null, {
      eventId: ev && ev.event_id, hits: (data && data.hits) || [], degrade: null,
    }));
  }

  // ---- 右栏 ② 研判与处置区（08 的流程）----
  ui.mount(refs.caseRight, disposePanel(c, caseNo));
  // 处置流水**不放右栏**：08 的 `actionsTable` 是 8 列（时间/动作/结论/处置人/业务联动/
  // 名单写入/备注/流水号），塞进 348px 的右栏会被压成每行一两个字（D65 实测的坑）。
  // 它是"过程记录"而不是"当前要看的证据"，因此按整页宽度放在三栏**下方**。
  ui.mount(refs.caseBottom, actionsCard(caseNo));

  if (caseList) caseList.setSelected(caseNo, Object.assign({ case_no: caseNo }, c));
}

/** 详情彻底失败（不是分区降级）：如实报错 + 重试；案件不存在则按 BR-07-10 取消选中。 */
function caseDetailFailed(no, e) {
  const code = String((e && e.code) || '');
  const missingApi = e.status === 404 && !code.startsWith('CASE-');
  if (e.status === 404 && code.startsWith('CASE-')) {
    ui.toast(`案件不存在或已被归档：${no}（${code}）`, 'warn', 5000);
    go({ case: null });      // BR-07-10：清空 `case` 参数并回到列表空选中态
    return;
  }
  const text = missingApi
    ? `案件详情接口尚未提供（GET /api/v1/cases/${no}）：${e.message}（${code || `HTTP ${e.status}`}）`
    : `案件详情加载失败：${e.message}（trace ${traceOf(e)}）`;
  // 注意：`ui.mount` 每调用一次就**搬走**节点，所以中栏与右栏各要一份**新建**的节点
  // （不能用 `cloneNode`——那样会丢掉按钮上的 addEventListener 回调）
  const body = () => [
    ui.banner(missingApi ? 'warn' : 'err', text),
    ui.h('div', { class: 'hint', text: '左栏案件列表仍可用；中栏与右栏由这**一次**详情请求驱动，因此一起失败（BR-07-06）。' }),
    ui.h('div', { class: 'form-actions' }, [
      ui.button('重试', { variant: 'ghost', onClick: () => loadCaseDetail() }),
      ui.button('退出案件模式', { variant: 'plain', onClick: () => go({ case: null }) }),
    ]),
  ];
  ui.mount(refs.midNote, null);
  ui.mount(refs.profileHolder, ui.card(null, body()));
  ui.mount(refs.caseMidBody, null);
  ui.mount(refs.decisionHolder, ui.card(null, body()));
  ui.mount(refs.caseRight, null);
  ui.mount(refs.caseBottom, null);
  showGraphFail('未加载关联图谱', '案件详情未取到，图谱与判定一起失败（同一请求）');
}

/** 案件驱动模式的取数：**只有这一个**请求（BR-07-06），并带 seq + AbortController（BR-07-08）。 */
async function loadCaseDetail() {
  const no = String(state.case || '');
  if (!no) return;
  if (caseAbort) caseAbort.abort();
  caseAbort = new AbortController();
  const { signal } = caseAbort;
  const seq = ++caseSeq;

  if (refs.caseFeedback && feedbackFor !== no) ui.clear(refs.caseFeedback);   // 换案件：结果条不能串档
  ui.mount(refs.midNote, ui.h('div', { class: 'hint', dataset: { role: 'case-mode-note' },
    text: `本屏由案件 ${no} 驱动：中栏与右栏来自同一次 ` + '`GET /api/v1/cases/' + no + '`' + '（BR-07-06）。' }));
  // BR-07-07：先清空、后整体替换——切换案件时立刻切成加载态，**绝不**残留上一个案件的画像/图谱
  ui.mount(refs.profileHolder, ui.loading(4));
  ui.mount(refs.caseMidBody,
    ui.card('当前触发事件', ui.loading(2)), ui.card('特征上下文快照', ui.loading(3)));
  ui.mount(refs.decisionHolder, ui.loading(3));
  ui.mount(refs.caseRight, ui.card('研判与处置（提交给模块 08 执行）', ui.loading(3)));
  ui.mount(refs.caseBottom, ui.card('处置流水（按 acted_at 升序）', ui.loading(2)));
  refs.canvas.classList.remove('is-empty');
  if (chart) { try { chart.dispose(); } catch (e) { /* 已销毁则忽略 */ } chart = null; }
  ui.mount(refs.graphSub, '加载中…');
  ui.mount(refs.graphNote, ui.loading(1));

  let data;
  try {
    data = await api.request(`${api.API_PREFIX}/cases/${encodeURIComponent(no)}`, { signal, silent: true });
  } catch (e) {
    if (e.name === 'AbortError' || signal.aborted || seq !== caseSeq) return;
    caseDetailFailed(no, e);
    return;
  }
  if (signal.aborted || seq !== caseSeq) return;
  // BR-07-09：服务端串号兜底——响应里的案件号与当前选中不一致时**丢弃**，绝不让它覆盖本屏
  const got = String((data && data.case && data.case.case_no) || '');
  if (got && got !== no) {
    ui.toast(`详情响应串号（请求 ${no} / 返回 ${got}），已丢弃该响应`, 'warn', 5000);
    return;
  }
  caseDetail = data;
  await renderCaseDetail(data);
}

// ==================== 组装 ====================

function toolbar() {
  const input = ui.inputBox({
    value: state.user, placeholder: '用户编号，如 U000128',
    onEnter: (v) => submitUser(v),
  });
  input.classList.add('user-input');
  const btn = ui.button('查询', { variant: 'primary', onClick: () => submitUser(input.value) });
  btn.classList.add('btn-query');
  const hopSel = ui.selectBox(
    [{ value: '2', label: '2 跳' }, { value: '1', label: '1 跳' }],
    String(state.maxHop), (v) => go({ max_hop: v }));
  hopSel.classList.add('hop-select');
  const reset = ui.button('重置', { onClick: () => go({ user: DEFAULT_USER, max_hop: null, max_nodes: null, gtype: null, gid: null }) });
  const relayout = ui.button('重新布局', { onClick: () => { chart = null; loadGraph(); } });
  return ui.toolbar([
    ui.h('span', { class: 'hint', text: '用户编号' }), input, hopSel,
    ui.h('span', { class: 'hint', text: '（也可用 #/cases?user=U000128 直达）' }),
  ], [btn, reset, relayout]);
}

function submitUser(v) {
  const id = clampUser(v) || DEFAULT_USER;
  if (id === state.user && state.gtype === 'user') { reload(); return; }
  go({ user: id, gtype: null, gid: null });
}

/**
 * 重新读取 hash 并重载各区块（用于 hash 未变的"再次查询"）。
 *
 * `patch` 是本次改动涉及的 query 键：只重载**真的变了**的那一块。
 * 全量重载会把已经画好的图谱推倒重画（力导向布局重新抖一次），
 * 而审核员点"查询判定"时中栏根本没变——没有理由让它闪一下。
 * **null 表示"什么都不知道，全部重载"**：这是最保守的分支，宁多重不漏。
 */
function reload(patch = null) {
  state = readState();
  const has = (...keys) => keys.some((k) => patch && patch[k] !== undefined);
  // 案件模式（BR-07-06）：中栏 + 右栏**只有** loadCaseDetail 这一个取数入口
  if (state.case) {
    if (!patch || has('case', 'user', 'event', 'gtype', 'gid', 'max_hop', 'max_nodes', 'risk_only')) {
      loadCaseDetail();
    }
    if (caseList) caseList.load();
    return;
  }
  if (!patch || has('user', 'gtype', 'gid')) loadProfile();
  if (!patch || has('user', 'gtype', 'gid', 'max_hop', 'max_nodes', 'risk_only')) loadGraph();
  // 用户在"用户编号"里换了人时事件编号不变，右栏不需要重算；这里只在事件编号真的变了
  // （或是最保守的全量重载）时重查，避免每次换用户都白发一次事件详情请求（接口有限流）
  if (!patch || has('event')) loadDecision();
  if (caseList) caseList.load();
}

/** 右栏的查询条：事件编号输入 + 查询（也可用 `#/cases?event=EVT...` 直达）。 */
function eventToolbar() {
  const input = ui.inputBox({
    value: state.event, placeholder: `事件编号，如 ${DEFAULT_EVENT}`,
    onEnter: (v) => submitEvent(v),
  });
  input.classList.add('event-input');
  const btn = ui.button('查询判定', { variant: 'primary', onClick: () => submitEvent(input.value) });
  btn.classList.add('btn-decision-query');
  return ui.h('div', { class: 'decision-toolbar' }, [
    ui.h('span', { class: 'hint', text: '事件编号' }),
    ui.h('div', { class: 'dt-row' }, [input, btn]),
    ui.button('清空', { variant: 'plain', onClick: () => go({ event: 'none' }) }),
  ]);
}

function submitEvent(v) {
  const id = String(v || '').trim();
  // 空输入 = 收回右栏（写 `event=none` 这个显式哨兵，而不是删参数——删参数会落回默认演示事件）
  if (!id) { go({ event: 'none' }); return; }
  if (id === state.event) { reload({ event: id }); return; }
  go({ event: id });
}

export async function render(container) {
  // 左栏的筛选下拉要用服务端枚举（BR-00-18：不在前端硬编码"待审/审核中"这类标签）
  await ensureEnums().catch(() => ({}));
  state = readState();
  refs = {
    profileHolder: ui.h('div', { class: 'profile-holder' }),
    midNote: ui.h('div', { class: 'case-mid-note' }),
    graphSub: ui.h('div', { class: 'graph-sub hint' }),
    canvas: ui.h('div', { class: 'graph-canvas' }),
    graphNote: ui.h('div', { class: 'graph-note' }),
    decisionHolder: ui.h('div', { class: 'decision-holder' }),
    // 07 新增的四块：中栏的事件 + 快照、右栏的处置区 + 流水、以及 08 结果反馈的挂载点。
    // `case-mid` 用 `display: contents`（见 app.css），所以未进入案件模式时它**不产生任何盒子**，
    // 09/05 原有的两栏间距与布局因此一字未变。
    caseMid: ui.h('div', { class: 'case-mid' }),
    caseMidBody: ui.h('div', { class: 'case-mid-body' }),
    caseRight: ui.h('div', { class: 'case-right' }),
    caseBottom: ui.h('div', { class: 'case-bottom' }),
    caseFeedback: ui.h('div', { class: 'case-feedback' }),
    drawerRoot: ui.h('div', { class: 'drawer-root' }),
  };
  refs.caseMid.appendChild(refs.caseMidBody);
  document.addEventListener('keydown', onKeyDown);

  const graphCard = ui.card('关联实体图谱', [
    refs.graphSub,
    refs.canvas,
    refs.graphNote,
    legendNode(),
  ]);
  graphCard.classList.add('graph-card');

  // 左栏 = 模块 07 的案件列表（本文件新增的那一栏）；中栏 = 09；右栏 = 05。
  // 09/05 的容器**原样保留**（`.wb-left` / `.wb-right` 与它们的内容一字未动），
  // 只是在 `.workbench` 最前面插入了 `.wb-list`。
  const caseListEl = ui.h('div', { class: 'wb-list' });
  caseList = createCaseList({
    getState: () => state,
    getSelected: () => String((state && state.case) || ''),
    go,
    onClaimed: () => { if (caseList) caseList.load(); if (state.case) loadCaseDetail(); },
  });
  caseListEl.appendChild(caseList.el);
  if (state.case) caseList.setSelected(state.case, null);

  const midToolbar = toolbar();
  const rightToolbar = eventToolbar();
  const left = ui.h('div', { class: 'wb-left' }, [
    ui.card(null, [
      midToolbar,
      refs.midNote,
      refs.profileHolder,
    ]),
    refs.caseMid,
    graphCard,
  ]);
  const right = ui.h('div', { class: 'wb-right' }, [
    ui.card(null, [
      rightToolbar,
      refs.decisionHolder,
    ]),
    refs.caseFeedback,
    refs.caseRight,
    ui.h('div', { class: 'hint', text:
      '右栏展示该事件**真实落库**的决策（模块 05 的产出，经 03 详情接口透传）。'
      + '决策链路：名单过滤 → 条件树求值 → 分值累加 → 仲裁分级。' }),
  ]);

  // 案件模式下两个查询条没有意义（中栏/右栏由 `?case=` 那一次详情驱动），
  // 但**不删除、不改类名**——只是隐藏，退出案件模式后原样回来。
  if (state.case) {
    midToolbar.classList.add('is-hidden');
    rightToolbar.classList.add('is-hidden');
  }

  ui.mount(container, ui.h('div', { class: 'view' }, [
    ui.h('div', { class: 'workbench' }, [caseListEl, left, right]),
    refs.caseBottom,
    ui.h('div', { class: 'hint', text:
      '三栏联动：点左栏案件行 → **一次** GET /api/v1/cases/{case_no} → 中栏（09 画像/图谱 + '
      + '07 事件与快照）与右栏（05 判定摘要与命中明细 + 07 处置区）用**同一次响应**渲染（BR-07-06）。'
      + '处置只收集「结论 + 动作 + 备注」并提交给模块 08 的流程执行（BR-07-21）。' }),
    refs.drawerRoot,
  ]));

  if (state.case) {
    // 案件模式：中栏 + 右栏 = 一次详情请求（BR-07-06）；不另发画像/图谱/事件请求
    loadCaseDetail();
  } else {
    // 既有行为（一字未改）：中栏由 `?user=` 驱动、右栏由 `?event=` 驱动，各自独立加载
    loadProfile();
    loadDecision();
  }
  await caseList.load();
  if (!state.case) await loadGraph();
}
