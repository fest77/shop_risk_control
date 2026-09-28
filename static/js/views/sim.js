/* 事件仿真测试页（模块 10 · 独立页面 `#/sim`）
 *
 * Spec 依据：`04_模块Spec/10_事件仿真测试.md` §2（页面结构）/ §3（接口字段）/ §4（业务规则）
 *
 * ## 这一页的价值与"极易出错点"
 *
 * - **一致性**（BR-10-01/02）：页面**只**调用 `POST /api/v1/sim/run`、`/sim/batch`、
 *   `/sim/runs/{id}`，判定与计分由 05 的 `decide()` 完成。本文件里没有、也**不允许**
 *   有任何条件求值或分值累加——一旦在这里重算一遍，"仿真通过"就不再等于"线上通过"。
 * - **数据隔离**（BR-10-05/06）：隔离由服务端负责（`dry_run=true` + 不写特征窗口），
 *   页面如实展示 `dry_run` 与"特征基于当前真实窗口"这一前提（BR-10-07），
 *   绝不把"仿真"渲染成"已生效"。
 *
 * ## 表单为什么要"同构"
 *
 * 任务书 §1：**事件参数表单必须与真实入口同构**。因此：
 *   - 基础字段与类型必填集来自 03 的 `REQUIRED_BY_TYPE`（决定哪个字段标 `*`）；
 *   - 类型专属字段**全部落在 `scene_extra` 里**，按 `event_type` 动态显隐
 *     （照 03 的防串味规则：`scene_extra` 只放本类型的键，串味的键由服务端
 *     `EVT-4006` 拒绝）；
 *   - 这里只做 Spec §2.2 明列的两项**本地**校验（`event_type`/`user_id` 必填、
 *     `scene_extra` 必须是合法 JSON），**其余一律交给服务端**——前端放宽一分，
 *     "仿真通过、真实入口 422"就多一分。
 *
 * ## 与服务端表的镜像关系（为什么可以在这里写一份表）
 *
 * `REQUIRED_BY_TYPE` / `SCENE_FIELDS` 是 `app/schemas/event_schema.py` 里
 * `REQUIRED_BY_TYPE` / `SCENE_EXTRA_BY_TYPE` 的**只读镜像**，用途仅限
 * 「渲染哪些输入框 + 标哪个星号 + 给什么提示」。它**不参与**任何拦截：
 * 提交时只做上面那两项本地校验，长度/取值/白名单/金额一致性全部由 03 判定
 * （`EVT-4004` / `EVT-4006` / `EVT-4007`）。镜像一旦与后端漂移，最坏结果是
 * "少显示一个输入框"，而**不是**"放过一条非法事件"。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as session from '../session.js';
import { ensureEnums, optionsOf, labelOf } from '../store.js';
import { ensureFeatureMeta } from './cond_tree.js';
import { createChain } from './chain.js';

export const meta = { title: '事件仿真测试', crumb: '事件仿真' };

const SIM = `${api.API_PREFIX}/sim`;
const CASES_URL = `${SIM}/cases`;
const RUN_URL = `${SIM}/run`;
const BATCH_URL = `${SIM}/batch`;
const RUNS_URL = (id) => `${SIM}/runs/${encodeURIComponent(id)}`;

// ============================================================
// 03 的镜像表（见文件头"与服务端表的镜像关系"）
// ============================================================
/** 基础字段（Spec §2.2 的 7 个标量字段 + 03 里 `after_sale_apply` 必填的 `biz_no`）。 */
const BASE_FIELDS = [
  { key: 'user_id', label: 'user_id', placeholder: '如 U000128' },
  { key: 'device_id', label: 'device_id', placeholder: '如 D8F2A1C4' },
  { key: 'ip', label: 'ip', placeholder: 'IPv4 / IPv6' },
  { key: 'phone', label: 'phone', placeholder: '若填则参与名单匹配' },
  { key: 'address_id', label: 'address_id', placeholder: '如 ADDR-7712' },
  { key: 'amount', label: 'amount(分)', type: 'number', placeholder: '单位：分' },
  { key: 'biz_no', label: 'biz_no', placeholder: '售后申请必填（03 的必需字段）' },
];

/** `event_type -> 基础必填字段`（镜像 `event_schema.REQUIRED_BY_TYPE`）。 */
const REQUIRED_BY_TYPE = {
  login: ['user_id', 'device_id', 'ip'],
  coupon_receive: ['user_id', 'device_id', 'ip', 'amount'],
  order_create: ['user_id', 'device_id', 'ip', 'address_id', 'amount'],
  order_pay: ['user_id', 'amount'],
  after_sale_apply: ['user_id', 'biz_no', 'amount'],
};

/** `event_type -> scene_extra 的字段清单`（镜像 `event_schema.SCENE_EXTRA_BY_TYPE`）。 */
const SCENE_FIELDS = {
  login: [
    { key: 'login_type', type: 'enum', options: ['pwd', 'sms', 'scan'], required: true },
    { key: 'ua', type: 'text' },
    { key: 'success', type: 'bool' },
  ],
  coupon_receive: [
    { key: 'coupon_id', type: 'text', required: true },
    { key: 'activity_id', type: 'text', required: true },
    { key: 'face_value', type: 'int' },
    { key: 'batch_id', type: 'text' },
  ],
  order_create: [
    { key: 'order_no', type: 'text', required: true },
    { key: 'sku_count', type: 'int', required: true },
    { key: 'total_amount', type: 'int' },
    { key: 'address_id', type: 'text' },
  ],
  order_pay: [
    { key: 'order_no', type: 'text', required: true },
    { key: 'pay_channel', type: 'text', required: true },
    { key: 'pay_amount', type: 'int' },
    { key: 'card_tail', type: 'text' },
  ],
  after_sale_apply: [
    { key: 'after_sale_no', type: 'text', required: true },
    { key: 'order_no', type: 'text', required: true },
    { key: 'reason_code', type: 'enum', required: true, options: ['not_received', 'damaged', 'wrong_item', 'quality', 'other'] },
    { key: 'refund_amount', type: 'int' },
    { key: 'received_goods', type: 'bool' },
  ],
};

/** 用例分类（Spec §3.2 的取值域；标签是界面文案）。 */
const CATEGORIES = [
  { value: 'coupon_abuse', label: '羊毛党（coupon_abuse）' },
  { value: 'aftersale_abuse', label: '恶意退款（aftersale_abuse）' },
  { value: 'normal', label: '正常（normal）' },
  { value: 'boundary', label: '边界（boundary）' },
  { value: 'other', label: '其它（other）' },
];

/** 表单初值（原型 `05_事件仿真测试页.pen` 的 `sf*` 占位内容，逐字一致）。 */
const DEFAULTS = {
  event_type: 'coupon_receive',
  user_id: 'U000128',
  device_id: 'D8F2A1C4',
  ip: '117.136.12.88',
  phone: '13800006621',
  address_id: 'ADDR-7712',
  amount: '20000',
  biz_no: '',
};
const DEFAULT_SCENE = {
  coupon_receive: { coupon_id: 'CPN-8821', face_value: 20000, activity_id: 'ACT-2026-0918' },
};

// ============================================================
// 视图状态（模块级：菜单间来回切换时保留参数，与 06/07 的做法一致）
// ============================================================
const state = {
  cases: [],
  selectedId: '',
  eventType: DEFAULTS.event_type,
  /** `event_type -> {scene_extra 键: 原始 JSON 值}`：切类型再切回来不丢参数。 */
  sceneStore: clone(DEFAULT_SCENE),
};

let refs = null;
let chain = null;

/** 深拷贝一份场景初值（避免多处共享同一对象后被就地改动）。 */
function clone(o) { return JSON.parse(JSON.stringify(o)); }

export function dispose() {
  refs = null;
  chain = null;
}

function isRunner() {
  // 权限来自 `/auth/me` 的 permissions（BR-01-12），前端不按角色推导
  return session.has('sim:run');
}

// ============================================================
// 小工具
// ============================================================
function field(labelText, node, { required = false, hint = '', span = 1 } = {}) {
  return ui.h('div', {
    class: 'field', style: span > 1 ? `grid-column:span ${span}` : null,
  }, [
    ui.h('label', {}, [
      document.createTextNode(labelText),
      required ? ui.h('span', { class: 'req', text: ' *' }) : null,
    ]),
    node,
    hint ? ui.h('div', { class: 'hint', text: hint }) : null,
  ]);
}

/** 缺数据一律显示 `—`：`null` 与 `0` 是两件事，不能混。 */
function show(v, suffix = '') {
  if (v === null || v === undefined || v === '') return '—';
  return `${v}${suffix}`;
}

function decisionTag(decision, fallback = '—') {
  const kind = { pass: 'low', review: 'medium', reject: 'high' }[decision] || '';
  return ui.tag(labelOf('decision', decision, decision || fallback), kind);
}

// ============================================================
// scene_extra：结构化字段 ←→ JSON 文本
// store 里存**原始 JSON 值**（数字仍是数字、布尔仍是布尔），
// 只有渲染输入框时才转成字符串——否则 `{"face_value": 20000}` 会被静默改成字符串。
// ============================================================
function sceneSpec(eventType) { return SCENE_FIELDS[eventType] || []; }

function sceneBucket(eventType) {
  if (!state.sceneStore[eventType]) state.sceneStore[eventType] = {};
  return state.sceneStore[eventType];
}

function toInputValue(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** 输入框字符串 → JSON 取值（int 转数字、bool 转布尔，其余按文本）。 */
function fromInputValue(spec, raw) {
  const s = String(raw).trim();
  if (s === '') return undefined;
  if (spec.type === 'int') {
    const n = Number(s);
    return Number.isFinite(n) ? n : s;   // 非法数字原样交给服务端判（不在这里拦截）
  }
  if (spec.type === 'bool') {
    if (s === 'true') return true;
    if (s === 'false') return false;
    return s;
  }
  return s;
}

/** 当前类型的 store → JSON 文本域。 */
function syncSceneJson() {
  const bucket = sceneBucket(state.eventType);
  const out = {};
  Object.keys(bucket).forEach((k) => {
    if (bucket[k] !== undefined) out[k] = bucket[k];
  });
  refs.sceneJson.value = Object.keys(out).length ? JSON.stringify(out, null, 2) : '';
}

function setSceneError(text) {
  if (!refs || !refs.sceneErr) return;
  refs.sceneErr.textContent = text || '';
  refs.sceneErr.classList.toggle('on', !!text);
  refs.sceneJson.classList.toggle('is-invalid', !!text);
}

/** 文本域内容校验：合法对象→对象；空→{}；非法→null（并标红 SIM-4002）。 */
function parseSceneJson() {
  const raw = refs.sceneJson.value.trim();
  if (raw === '') { setSceneError(''); return {}; }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    setSceneError('扩展参数不是合法 JSON（SIM-4002）。服务端同样会拒绝，请先修正。');
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    setSceneError('扩展参数必须是 JSON 对象（如 {"coupon_id":"CPN-1"}），对应 SIM-4002。');
    return null;
  }
  setSceneError('');
  return parsed;
}

/** 只做格式校验、不改错误提示（供 oninput 实时反馈用）。 */
function sceneJsonIssue() {
  const raw = refs.sceneJson.value.trim();
  if (!raw) return '';
  try {
    const p = JSON.parse(raw);
    return (p && typeof p === 'object' && !Array.isArray(p)) ? '' : '扩展参数必须是 JSON 对象。';
  } catch (e) {
    return '扩展参数不是合法 JSON（SIM-4002）。';
  }
}

/** 渲染当前 event_type 的结构化 scene_extra 字段（"动态显隐"= 整块重建）。 */
function renderSceneFields({ fromJson = null } = {}) {
  const spec = sceneSpec(state.eventType);
  const bucket = sceneBucket(state.eventType);
  if (fromJson) Object.assign(bucket, fromJson);
  const nodes = [];
  if (!spec.length) {
    nodes.push(ui.h('div', { class: 'hint', text:
      `事件类型「${state.eventType || '—'}」没有登记扩展字段；可在下方 JSON 里直接填写（会原样提交给 03 校验）。` }));
  }
  spec.forEach((f) => {
    const val = toInputValue(bucket[f.key]);
    let ctrl;
    const onPick = (v) => {
      if (v === '') delete bucket[f.key]; else bucket[f.key] = fromInputValue(f, v);
      syncSceneJson();
      setSceneError('');
    };
    if (f.type === 'enum') {
      ctrl = ui.selectBox([{ value: '', label: '（未填）' },
        ...f.options.map((o) => ({ value: o, label: o }))], val, onPick);
    } else if (f.type === 'bool') {
      ctrl = ui.selectBox([{ value: '', label: '（未填）' },
        { value: 'true', label: 'true' }, { value: 'false', label: 'false' }], val, onPick);
    } else {
      ctrl = ui.h('input', {
        type: f.type === 'int' ? 'number' : 'text', value: val,
        placeholder: f.type === 'int' ? '整数' : '',
        oninput: (e) => {
          const v = fromInputValue(f, e.target.value);
          if (v === undefined) delete bucket[f.key]; else bucket[f.key] = v;
          syncSceneJson();
          setSceneError('');
        },
      });
    }
    ctrl.dataset.key = f.key;
    ctrl.classList.add('sim-scene-input');
    nodes.push(field(f.key, ctrl, {
      required: !!f.required,
      hint: f.type === 'enum' ? `取值：${f.options.join(' / ')}` : '',
    }));
  });
  ui.mount(refs.sceneFields, nodes);
  syncSceneJson();
}

// ============================================================
// 表单读写
// ============================================================
function setForm(values) {
  Object.entries(values || {}).forEach(([k, v]) => {
    if (refs.inputs[k]) refs.inputs[k].value = (v === null || v === undefined) ? '' : String(v);
  });
}

/** 读表单 + 按 event_type 刷新必填星号（与 03 的 `REQUIRED_BY_TYPE` 同源）。 */
function collectForm() {
  const out = {};
  Object.keys(refs.inputs).forEach((k) => { out[k] = refs.inputs[k].value.trim(); });
  // `event_type` 不在 `refs.inputs` 里：它是下拉、且决定 scene_extra 的字段集与必填星号，
  // 所以单独取它（并且以**下拉当前值**为准，避免自动化/程序化改值后 state 落后）。
  const sel = refs.typeSelect ? refs.typeSelect.value : '';
  out.event_type = sel || state.eventType;
  if (sel) state.eventType = sel;
  const req = REQUIRED_BY_TYPE[state.eventType] || ['user_id'];
  BASE_FIELDS.forEach((f) => {
    const label = refs.labels[f.key];
    if (!label) return;
    const star = label.querySelector('.req');
    if (req.includes(f.key) && !star) label.appendChild(ui.h('span', { class: 'req', text: ' *' }));
    if (!req.includes(f.key) && star) star.remove();
  });
  return out;
}

/** 组装请求要的 `event`（只放有值的字段，避免把空串当成"填了空值"）。 */
function buildEvent() {
  const values = collectForm();
  const event = { event_type: state.eventType };
  BASE_FIELDS.forEach((f) => {
    const v = values[f.key];
    if (v === undefined || v === '') return;
    if (f.type === 'number') {
      const n = Number(v);
      event[f.key] = Number.isFinite(n) ? n : v;
    } else {
      event[f.key] = v;
    }
  });
  const extra = parseSceneJson();
  if (extra === null) return { error: '扩展参数（scene_extra）不是合法 JSON，已按 SIM-4002 标红。' };
  if (Object.keys(extra).length) event.scene_extra = extra;
  return { event };
}

// ============================================================
// 用例列表
// ============================================================
function caseItem(c) {
  const id = String(c.case_id || c._id || '');
  const el = ui.h('div', {
    class: 'sim-case' + (id === state.selectedId ? ' is-selected' : ''),
    dataset: { caseId: id },
    onclick: () => selectCase(id),
  }, [
    ui.h('div', { class: 'sim-case-name', text: c.name || '（未命名用例）' }),
    ui.h('div', { class: 'sim-case-meta' }, [
      decisionTag(c.expected_decision),
      ui.h('span', { class: 'hint mono', text: String(c.category || '') }),
    ]),
  ]);
  el.title = c.description || '';
  return el;
}

function renderCases() {
  if (!refs) return;
  if (!state.cases.length) {
    ui.mount(refs.casesWrap, ui.empty('暂无用例，可手动填写参数后保存',
      '「把当前参数存为新用例」会把整份事件体存进 E17 `sim_cases`（BR-10-17）。'));
    return;
  }
  ui.mount(refs.casesWrap, state.cases.map(caseItem));
}

async function loadCases({ silent = false } = {}) {
  try {
    const data = await api.get(CASES_URL, null, null);
    if (!refs) return;
    const items = Array.isArray(data) ? data : ((data && data.items) || []);
    state.cases = items.filter((c) => c && (c.case_id || c._id));
    renderCases();
    refs.caseCount.textContent = `共 ${state.cases.length} 条用例`;
    // 首个用例默认选中并载入整份模板（原型里第一条即为选中态；也满足 BR-10-17）
    if (!state.selectedId && state.cases.length) selectCase(String(state.cases[0].case_id || state.cases[0]._id), { quiet: true });
  } catch (e) {
    if (!refs) return;
    refs.caseCount.textContent = '';
    ui.mount(refs.casesWrap, [
      ui.banner('err', `用例加载失败：${e.message}`),
      ui.h('div', { class: 'hint', text: '可手动填写参数后直接「执行检测」；用例只用于一键载入与批量回放。' }),
    ]);
    if (!silent) ui.toast(`用例加载失败：${e.message}`, 'err', 5000);
  }
}

function selectCase(id, { quiet = false } = {}) {
  const c = state.cases.find((x) => String(x.case_id || x._id) === String(id));
  state.selectedId = c ? String(id) : '';
  renderCases();
  if (!c) return;
  loadTemplate(c);
  runMeta(`已载入用例「${c.name || ''}」：表单按 POST /events 的同一套字段整份填充（BR-10-17）。`);
  if (!quiet) ui.clear(refs.errBanners);
}

/** 用例模板 → 整份表单（BR-10-17：**完整事件体**，载入即填充，不允许只填部分）。 */
function loadTemplate(c) {
  const tpl = (c && c.event_template) || {};
  const type = String(tpl.event_type || state.eventType);
  if (SCENE_FIELDS[type]) state.eventType = type;
  if (refs.typeSelect) refs.typeSelect.value = state.eventType;
  // 用例带整份模板：先清空本类型的 store 再灌入模板值，否则会残留上一个用例的参数
  state.sceneStore[state.eventType] = {};
  const extra = (tpl.scene_extra && typeof tpl.scene_extra === 'object') ? tpl.scene_extra : {};
  Object.assign(sceneBucket(state.eventType), extra);
  renderSceneFields();
  setForm({
    user_id: tpl.user_id, device_id: tpl.device_id, ip: tpl.ip, phone: tpl.phone,
    address_id: tpl.address_id, biz_no: tpl.biz_no,
    amount: (tpl.amount === undefined || tpl.amount === null) ? '' : tpl.amount,
  });
  setSceneError('');
  collectForm();   // 刷新必填星号
}

// ============================================================
// 右栏：执行结果
// ============================================================
function runMeta(text, kind = '') {
  if (!refs || !refs.runMeta) return;
  refs.runMeta.textContent = text || '';
  refs.runMeta.className = `hint${kind ? ` ${kind}` : ''}`;
}

function renderRunHead(data) {
  const nodes = [];
  if (data && data.run_id) nodes.push(ui.h('span', { class: 'mono', text: `run_id=${data.run_id}` }));
  nodes.push(ui.h('span', { class: 'hint', text: `总耗时 ${show(data && data.elapsed_ms, 'ms')}` }));
  if (data && data.expected_decision) {
    nodes.push(ui.h('span', { class: 'sim-expect' }, [
      ui.h('span', { class: 'hint', text: '预期' }), decisionTag(data.expected_decision),
      ui.h('span', { class: 'hint', text: data.matched_expected === true ? '实际与预期一致'
        : (data.matched_expected === false ? '实际与预期不一致' : '') }),
    ]));
  }
  if (data && data.dry_run === true) {
    nodes.push(ui.tag('dry_run=true（不落业务库 / 不写真实窗口）', 'plain'));
  }
  ui.mount(refs.runHead, nodes);
}

/** BR-10-04：说明"这次结论基于哪一版规则"。 */
function renderRuleVersions(data) {
  const rv = (data && data.rule_versions) || null;
  if (!rv || typeof rv !== 'object' || !Object.keys(rv).length) {
    ui.mount(refs.ruleVers, ui.h('span', { class: 'hint', text:
      '本次响应未回传 rule_versions（BR-10-04 要求仿真说明结论基于哪一版规则）——如实标注，不推测。' }));
    return;
  }
  const items = Object.entries(rv).map(([code, v]) => ui.h('span', { class: 'mono sim-rv', text: `${code}=v${v}` }));
  ui.mount(refs.ruleVers, [
    ui.h('span', { class: 'hint', text: `规则集版本（${items.length} 条，当前生效版本）：` }),
    ...items,
  ]);
}

/** BR-10-07：特征基于"当前真实窗口"这一前提必须如实提示。 */
function renderWindowNote(data) {
  const missing = (data && Array.isArray(data.missing_features)) ? data.missing_features : [];
  const empty = !!(data && (data.window_empty === true || data.no_window_data === true));
  if (empty || missing.length) {
    ui.mount(refs.windowNote, ui.banner('warn',
      '特征基于当前真实窗口计算，仿真不写入窗口（BR-10-06）。'
      + (missing.length ? `本次缺失 ${missing.length} 项特征，特征可能偏低。` : '当前无历史窗口数据，特征可能偏低。')));
  } else {
    ui.clear(refs.windowNote);
  }
}

/** 错误码 → 人话（Spec §5）。返回用于链路块展示的文案。 */
function renderSimError(e) {
  const code = e && e.code;
  const msg = (e && e.message) ? e.message : String(e);
  const map = {
    'SIM-4001': `事件参数不合法：${msg}`,
    'SIM-4002': `扩展参数不是合法 JSON：${msg}`,
    'SIM-4003': `已存在同名用例：${msg}`,
    'SIM-4004': '未找到该次仿真记录（SIM-4004）。',
    'SIM-4005': '单次最多回放 200 条（SIM-4005）。',
    'SIM-5001': '决策引擎暂时不可用，无法仿真（SIM-5001）。不会降级为"用简化逻辑算一下"——降级即说谎。',
    'SIM-5002': '本次结果已返回，但记录保存失败（SIM-5002），可能无法回看。',
    'SIM-5003': '执行超时，请简化参数后重试（SIM-5003）。',
  };
  if (code === 'SIM-4002') setSceneError('扩展参数不是合法 JSON（SIM-4002）。');
  const text = map[code] || `${code ? `[${code}] ` : ''}${msg}`;
  ui.mount(refs.errBanners, ui.banner('err', text));
  return text;
}

// ============================================================
// 执行 / 批量 / 回看
// ============================================================
async function doRun() {
  ui.clear(refs.errBanners);
  ui.clear(refs.runHead);
  ui.clear(refs.ruleVers);
  ui.clear(refs.windowNote);
  setSceneError('');
  const values = collectForm();
  if (!values.event_type) { runMeta('请先选择 event_type（必填）。', 'is-err'); return; }
  if (!values.user_id) { runMeta('请先填写 user_id（必填）。', 'is-err'); return; }
  const built = buildEvent();
  if (built.error) { runMeta(built.error, 'is-err'); return; }

  ui.buttonBusy(refs.btnRun, true, '执行中');
  chain.start();
  runMeta('执行中：正在按 事件校验 → 特征计算 → 规则求值 → 仲裁 → 落库 逐步回放…');
  try {
    const data = await api.request(RUN_URL, {
      method: 'POST', silent: true,
      body: { event: built.event, case_id: state.selectedId || undefined, trace: true },
    });
    if (!refs) return;
    renderRunHead(data);
    renderRuleVersions(data);
    renderWindowNote(data);
    runMeta('链路回溯完成；每步耗时取自接口 steps[].elapsed_ms，缺数据一律显示「—」，不在前端编时间。');
    await chain.show(data);
    if (data && (data.record_saved === false || data.trace_saved === false)) {
      ui.mount(refs.errBanners, ui.banner('warn',
        '本次结果已返回，但记录保存失败（SIM-5002），刷新后可能无法回看这次链路。'));
    }
  } catch (e) {
    if (!refs) return;
    const text = renderSimError(e);
    const code = String((e && e.code) || '');
    if (code.startsWith('SIM-4')) {
      // 事件体 / 扩展参数非法 → 步骤 1 标红，其后"未执行"（BR-10-15）
      await chain.fail(text, { failedStep: 'event_validate' });
    } else {
      // SIM-5001 / SIM-5003 / 网络：**不给出任何决策结论**（Spec §5 明确不许降级）
      await chain.fail(text, { failedStep: 'arbitrate' });
      ui.clear(refs.runHead);
      runMeta('本次没有任何决策结论可用；SIM-5001 时绝不降级为"用简化逻辑算一下"。', 'is-err');
    }
  } finally {
    ui.buttonBusy(refs.btnRun, false);
  }
}

function renderBatch(data) {
  const fp = Number(data && data.false_positive) || 0;
  const stats = [
    ['执行条数 total', show(data && data.total)],
    ['与预期一致 matched', show(data && data.matched)],
    ['与预期不符 mismatched', show(data && data.mismatched)],
    ['误伤 false_positive', show(data && data.false_positive)],
    ['总耗时', show(data && data.elapsed_ms, 'ms')],
  ];
  const nodes = [
    ui.h('div', { class: 'sim-batch-stats' }, stats.map(([k, v]) => ui.h('div', {
      class: 'sim-bs' + (k.startsWith('误伤') && fp > 0 ? ' is-warn' : ''),
      dataset: { stat: k.split(' ')[1] || k },
    }, [
      ui.h('div', { class: 'hint', text: k }),
      ui.h('div', { class: 'sim-bs-v', text: v }),
    ]))),
  ];
  if (fp > 0) {
    nodes.push(ui.banner('warn', `存在误伤（${fp} 条）：预期 pass 却被判 review/reject，建议复核规则阈值。`));
  } else {
    nodes.push(ui.h('div', { class: 'hint', text: '本批无误伤；误伤口径为"预期 pass 但被判 review/reject"（Spec §2.4）。' }));
  }
  const samples = Array.isArray(data && data.mismatch_samples) ? data.mismatch_samples : [];
  if (samples.length) {
    nodes.push(ui.h('div', { class: 'chain-sub', text: `前 ${samples.length} 条不符样例（点「查看链路」按 run_id 回看）` }));
    nodes.push(ui.table(
      [{ name: 'run_id' }, { name: '实际', width: 88 }, { name: '预期', width: 88 }, { name: '操作', width: 104 }],
      samples.map((s) => [
        ui.h('span', { class: 'mono', text: String(s.run_id || '—') }),
        decisionTag(s.decision),
        decisionTag(s.expected_decision),
        (() => {
          const b = ui.button('查看链路', { variant: 'ghost', onClick: () => openRun(String(s.run_id || '')) });
          b.classList.add('btn-sm');
          return b;
        })(),
      ])));
  } else {
    nodes.push(ui.h('div', { class: 'hint', text: '无不符样例。' }));
  }
  nodes.push(ui.h('div', { class: 'hint', text:
    '同一用例 + 同一 seed 必须产生完全一致的结果（BR-10-18）。误伤统计依赖用例的"预期决策"标注，'
    + '衡量的是"与用例预期的偏差"，不构成对规则正确性的最终判决。' }));
  ui.mount(refs.batchWrap, ui.card(null, nodes, { soft: true }));
}

async function doBatch() {
  ui.clear(refs.errBanners);
  if (!state.selectedId) {
    ui.toast('请先在左栏选择一个用例：批量回放以用例的预期决策为基准（Spec §3.4 的 case_id）', 'warn', 6000);
    runMeta('批量回放需要先选择用例（接口 `POST /sim/batch` 只接受 `case_id`）。', 'is-err');
    return;
  }
  const repeat = Math.max(1, Math.min(200, Number(refs.repeatInput.value) || 20));
  const seed = Number(refs.seedInput.value);
  refs.repeatInput.value = String(repeat);   // 回显夹取后的值，避免"看起来发了 500"
  ui.buttonBusy(refs.btnBatch, true, '回放中');
  runMeta('批量回放中…（上限 200 条，BR-10-19）');
  try {
    const data = await api.request(BATCH_URL, {
      method: 'POST', silent: true,
      body: { case_id: state.selectedId, repeat, seed: Number.isFinite(seed) ? seed : 42 },
    });
    if (!refs) return;
    renderBatch(data);
    runMeta(`批量回放完成：${show(data && data.total)} 条，与预期不符 ${show(data && data.mismatched)} 条。`);
  } catch (e) {
    if (!refs) return;
    const text = renderSimError(e);
    ui.mount(refs.batchWrap, ui.banner('err', `批量回放失败：${text}`));
    runMeta(`批量回放失败：${text}`, 'is-err');
  } finally {
    ui.buttonBusy(refs.btnBatch, false);
  }
}

/** V-10-17 / BR-10-13：按 run_id 回看历史执行记录（链路与保存时一致）。 */
async function openRun(runId) {
  if (!runId) { ui.toast('该样例没有 run_id，无法回看', 'warn'); return; }
  ui.clear(refs.errBanners);
  ui.clear(refs.runHead);
  ui.clear(refs.ruleVers);
  ui.clear(refs.windowNote);
  runMeta(`正在回看执行记录 ${runId}…`);
  try {
    const data = await api.request(RUNS_URL(runId), { silent: true });
    if (!refs) return;
    renderRunHead(data);
    renderRuleVersions(data);
    renderWindowNote(data);
    runMeta(`已回看历史执行记录（GET /sim/runs/{run_id}，BR-10-13）：${runId}`);
    await chain.show(data);
  } catch (e) {
    if (!refs) return;
    const text = (e && e.code === 'SIM-4004') ? '未找到该次仿真记录（SIM-4004）。' : `回看失败：${e.message}`;
    ui.mount(refs.errBanners, ui.banner('err', text));
    runMeta(text, 'is-err');
  }
}

// ============================================================
// 保存用例（BR-10-17 / V-10-08）
// ============================================================
function openSaveCase() {
  ui.clear(refs.errBanners);
  const values = collectForm();
  if (!values.event_type || !values.user_id) {
    runMeta('保存用例前请先补全 event_type 与 user_id（用例模板必须是完整事件体，BR-10-17）。', 'is-err');
    return;
  }
  const built = buildEvent();
  if (built.error) { runMeta(built.error, 'is-err'); return; }

  const nameI = ui.inputBox({ value: '', placeholder: '2~64 字，重名会被服务端拒绝（SIM-4003）', maxlength: 64 });
  const catS = ui.selectBox(CATEGORIES, 'other');
  const expS = ui.selectBox(optionsOf('decision').length ? optionsOf('decision')
    : [{ value: 'review', label: 'review' }], 'reject');
  const descI = ui.h('textarea', { rows: '2', maxlength: '200', placeholder: '≤200 字（选填）' });
  const errBox = ui.h('div', { class: 'form-error' });

  ui.modal({
    title: '把当前参数存为新用例',
    confirmText: '保存用例',
    variant: 'primary',
    body: [
      ui.h('div', { class: 'form-grid sim-save-grid' }, [
        field('用例名', nameI, { required: true }),
        field('分类', catS, { required: true }),
        field('预期决策', expS, { required: true, hint: '批量回放按它判定"命中 / 未命中 / 误伤"' }),
        field('说明', descI, { span: 2 }),
      ]),
      ui.h('div', { class: 'chain-sub', text: '将保存的完整事件体（BR-10-17）' }),
      ui.h('pre', { class: 'mono cond-json-body', text: JSON.stringify(built.event, null, 2) }),
      errBox,
    ],
    onConfirm: async () => {
      const name = nameI.value.trim();
      if (name.length < 2 || name.length > 64) {
        errBox.textContent = '用例名需 2~64 字。';
        errBox.classList.add('on');
        return false;
      }
      try {
        const saved = await api.request(CASES_URL, {
          method: 'POST', silent: true,
          body: {
            name, category: catS.value, expected_decision: expS.value,
            description: descI.value.trim(), event_template: built.event,
          },
        });
        ui.toast(`用例已保存：${(saved && (saved.case_id || saved._id)) || name}`, 'ok', 5000);
        await loadCases({ silent: true });
        const id = saved && (saved.case_id || saved._id);
        if (id) selectCase(String(id));
        return true;
      } catch (e) {
        const map = {
          'SIM-4003': '已存在同名用例（SIM-4003），请改名或覆盖。',
          'SIM-4001': `事件体被服务端拒绝：${e.message}（用例模板必须是合法事件体）。`,
        };
        errBox.textContent = map[e && e.code] || `保存失败：${e && e.message ? e.message : e}`;
        errBox.classList.add('on');
        return false;   // 失败不关弹窗、不清输入
      }
    },
  });
}

// ============================================================
// 组装
// ============================================================
function buildCaseCard() {
  refs.casesWrap = ui.h('div', { class: 'sim-cases' });
  refs.caseCount = ui.h('span', { class: 'hint' });
  refs.btnSaveCase = ui.button('把当前参数存为新用例', { variant: 'plain', onClick: openSaveCase });
  refs.btnSaveCase.classList.add('is-full');
  return ui.card('测试用例模板', [
    ui.h('div', { class: 'sim-case-head' }, [
      refs.caseCount,
      ui.h('span', { class: 'hint', text: '点击用例即载入整份事件体' }),
    ]),
    refs.casesWrap,
    refs.btnSaveCase,
    ui.h('div', { class: 'hint', text:
      '用例存于 E17 sim_cases；删除为软删除（BR-10-20），历史 sim_runs 仍可追溯。' }),
  ]);
}

function buildFormCard() {
  refs.typeSelect = ui.selectBox(
    optionsOf('event_type').length ? optionsOf('event_type') : [{ value: '', label: '（事件类型字典为空）' }],
    state.eventType,
    (v) => {
      // 切类型：先把当前 JSON 收进旧类型的 store，再渲染新类型的字段（动态显隐）
      const parsed = parseSceneJson();
      if (parsed) {
        const bucket = sceneBucket(state.eventType);
        Object.keys(parsed).forEach((k) => { bucket[k] = parsed[k]; });
      }
      state.eventType = v;
      setSceneError('');
      renderSceneFields();
      collectForm();
    },
  );
  refs.typeSelect.id = 'simTypeSelect';
  refs.typeSelect.dataset.field = 'event_type';
  refs.inputs = {};
  refs.labels = {};
  const grid = BASE_FIELDS.map((f) => {
    const node = ui.h('input', {
      type: f.type === 'number' ? 'number' : 'text', value: '', placeholder: f.placeholder || '',
      id: `sim-${f.key}`,
    });
    node.dataset.field = f.key;
    refs.inputs[f.key] = node;
    const fld = field(f.label, node, { required: f.key === 'user_id' });
    refs.labels[f.key] = fld.querySelector('label');
    return fld;
  });

  refs.sceneFields = ui.h('div', { class: 'form-grid sim-scene-grid' });
  refs.sceneErr = ui.h('div', { class: 'form-error' });
  refs.sceneJson = ui.h('textarea', {
    rows: '4', class: 'mono sim-json', id: 'simSceneExtra',
    onchange: () => {
      // 手改 JSON 后回填结构化输入（只在 change 时发生，不与正在输入的人抢光标）
      const parsed = parseSceneJson();
      if (parsed) renderSceneFields({ fromJson: parsed });
    },
    oninput: () => setSceneError(sceneJsonIssue()),
  });
  refs.sceneJson.dataset.field = 'scene_extra';

  refs.btnRun = ui.button('▶ 执行检测', { variant: 'primary', onClick: doRun });
  refs.btnReset = ui.button('重置', { variant: 'ghost', onClick: doReset });
  refs.btnBatch = ui.button('批量回放此用例 × 20', { variant: 'plain', onClick: doBatch });
  refs.seedInput = ui.h('input', { type: 'number', value: '42', id: 'simSeed' });
  refs.repeatInput = ui.h('input', { type: 'number', value: '20', min: '1', max: '200', id: 'simRepeat' });
  refs.seedInput.dataset.field = 'seed';
  refs.repeatInput.dataset.field = 'repeat';

  return ui.card('事件参数（结构化输入）', [
    ui.h('div', { class: 'form-grid sim-form-grid' }, [
      field('event_type', refs.typeSelect, {
        required: true,
        hint: '取值域来自服务端枚举（BR-00-18）；类型专属字段按类型显隐并落在 scene_extra 里',
      }),
      ...grid,
    ]),
    ui.h('div', { class: 'sim-scene-block' }, [
      ui.h('div', { class: 'chain-sub', text: 'scene_extra（按事件类型变化的扩展 KV）' }),
      ui.h('div', { class: 'hint', text:
        '类型专属字段只放在这里；串味的键会被 03 以 EVT-4006 拒绝——仿真与真实入口同一套校验，不因仿真放宽。' }),
      refs.sceneFields,
      refs.sceneJson,
      refs.sceneErr,
    ]),
    ui.h('div', { class: 'form-actions' }, [
      refs.btnRun, refs.btnReset, refs.btnBatch,
      ui.h('label', { class: 'sim-inline' }, [document.createTextNode('seed'), refs.seedInput]),
      ui.h('label', { class: 'sim-inline' }, [document.createTextNode('repeat（≤200）'), refs.repeatInput]),
    ]),
  ]);
}

function buildChainCard() {
  chain = createChain({ onOpenRun: openRun });
  refs.runHead = ui.h('div', { class: 'sim-runhead' });
  refs.ruleVers = ui.h('div', { class: 'sim-rulevers' });
  refs.windowNote = ui.h('div');
  refs.errBanners = ui.h('div', { class: 'sim-errs' });
  refs.runMeta = ui.h('div', { class: 'hint' });
  refs.batchWrap = ui.h('div', { class: 'sim-batch' });
  return ui.card('判定链路单步回溯', [
    ui.h('div', { class: 'hint', text:
      '五步依次为 事件校验 → 特征提取（04）→ 名单快速过滤 → 规则命中链路（05）→ 最终决策。'
      + '本页只调用 /api/v1/sim/run（内部即 05 的 decide()），不在前端重算任何分值与判定（BR-10-01）。' }),
    refs.errBanners,
    refs.runHead,
    refs.ruleVers,
    refs.windowNote,
    chain.el,
    refs.runMeta,
    refs.batchWrap,
  ]);
}

function doReset() {
  state.selectedId = '';
  state.eventType = DEFAULTS.event_type;
  state.sceneStore = clone(DEFAULT_SCENE);
  if (refs.typeSelect) refs.typeSelect.value = state.eventType;
  renderCases();
  renderSceneFields();
  setForm(DEFAULTS);
  setSceneError('');
  ui.clear(refs.errBanners);
  ui.clear(refs.runHead);
  ui.clear(refs.ruleVers);
  ui.clear(refs.windowNote);
  ui.clear(refs.batchWrap);
  chain.reset();
  collectForm();
  runMeta('已重置为默认参数（不载入用例）。');
}

export async function render(container) {
  await ensureEnums();        // event_type / decision 的中文标签与取值域（BR-00-18）
  await ensureFeatureMeta();  // 步骤 2 特征小表的标签来自 04 的 /features/meta（BR-04-16）

  refs = {};
  state.eventType = DEFAULTS.event_type;
  state.sceneStore = clone(DEFAULT_SCENE);

  ui.mount(container, ui.h('div', { class: 'sim-row' }, [
    ui.h('div', { class: 'sim-left' }, [buildCaseCard(), buildFormCard()]),
    ui.h('div', { class: 'sim-right' }, [buildChainCard()]),
  ]));

  if (refs.typeSelect) refs.typeSelect.value = state.eventType;
  renderSceneFields();
  setForm(DEFAULTS);
  collectForm();
  chain.reset();

  if (!isRunner()) {
    // BR-10-21：只有 reviewer / strategist 可用。前端隐藏不替代后端鉴权（越权时接口仍会 403）。
    refs.btnRun.disabled = true;
    refs.btnBatch.disabled = true;
    refs.btnSaveCase.disabled = true;
    runMeta('当前账号没有 sim:run 权限，无法执行仿真（BR-10-21）。', 'is-err');
  }

  await loadCases();
}
