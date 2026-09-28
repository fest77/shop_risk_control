/* 系统设置页（模块 13 §2.1 ~ §2.4，对应原型 08_系统设置页.pen）
 *
 * ## 四张卡片与各自的真源
 *
 * | 卡片 | 数据真源 | 权限 |
 * |---|---|---|
 * | `rcCard` 风控运行参数 | `GET/PUT /system/config` | `sys:config` |
 * | `hcCard` 吞吐与接口健康度 | `/system/stats`（13）→ 降级 `/metrics/throughput`(11) + `/health` | 只读 |
 * | `usCard` 账号与角色管理 | `GET/POST/PUT/DELETE /system/users*` | `account:manage` |
 * | `mdCard` 决策引擎配置 | `GET/PUT /system/engine-config` | `engine:config` |
 *
 * ## 三条"宁可显示 — 也不编数"的硬约定
 *
 * 1. **健康度复用 11 的现成口径**（BR-13-25）：QPS / P95 取自 `/system/stats`；该接口未就绪时
 *    降级取 11 的 `/metrics/throughput`（`qps_1m` / `p95_elapsed_ms`），并把**真实数据来源**
 *    写在卡片上。两处都取不到就显示 `—`——把 `null` 渲染成 0 等于撒谎（`format.js` 的既有约定）。
 *    名单缓存命中率只有 13 的 `/system/stats` 提供，缺就如实显示 `—`，绝不拿别的数凑。
 * 2. **模型引擎如实标注"未接入"**（AD-09 / BR-13-21/22/23）：`mdCard` 的橙色警示条不可关闭，
 *    `engine_type` 的 `model` / `hybrid` 两个选项**置灰**，并把 `/health` 里装配的真实类名
 *    （`NullModelEngine`）原样显示——不做"可开关但无效果"的假开关。
 * 3. **账号列表只渲染契约字段**：即便响应里混进 `password_hash`，前端也一个字都不读（BR-13-20）。
 *
 * ## 危险操作一律二次确认（Spec §2.3 / BR-13-13~17）
 * 删除 / 停用 / 改角色 / 重置密码 / 批量停用 / 恢复默认，全部先弹确认；**不在前端替服务端做
 * 安全判断**（BR-13-25：隐藏按钮不替代后端鉴权）——"不能停用自己 / 最后一个管理员"由服务端
 * 拒绝，前端负责把它翻成人话并保持弹窗不关（照 `rule_editor.js` 的写法，绝不甩 JSON）。
 */
import * as api from '../api.js';
import * as session from '../session.js';
import * as ui from '../ui.js';
import { ensureEnums, optionsOf, labelOf } from '../store.js';
import { NA, isNum, int, fixed, dateTime } from '../format.js';

export const meta = { title: '系统设置', crumb: '风控中台 / 系统设置' };

const BASE = `${api.API_PREFIX}/system`;
const HC_REFRESH_MS = 10_000;   // BR-13-26：健康度 10 秒自动刷新（页面隐藏时暂停）

// ============================================================
// 运行参数定义（Spec §2.1 的表 + BR-13-01 的取值范围）
// ------------------------------------------------------------
// 本地 min/max 与服务端同值：本地校验只为"不发一个必然被拒的请求"，权威判定仍在服务端
// （SYS-4001 / SYS-4002）。**越界绝不静默截断**——输入框保留用户原值，只把该字段标红。
// ============================================================
const PARAM_FIELDS = [
  { key: 'short_window_min', label: '短窗口', kind: 'int', min: 1, max: 1440, unit: '分钟',
    def: 60, effect: '下一事件生效' },
  { key: 'long_window_min', label: '长窗口', kind: 'int', min: 1, max: 10080, unit: '分钟',
    def: 1440, effect: '下一事件生效' },
  { key: 'window_capacity', label: '内存窗口容量上限', kind: 'int', min: 1000, max: 1000000,
    unit: '条/维度', def: 10000, effect: '立即生效（下次 sweep 裁剪，可能丢弃历史窗口数据）' },
  { key: 'list_cache_ttl_sec', label: '名单缓存 TTL', kind: 'int', min: 1, max: 600, unit: '秒',
    def: 10, effect: '立即生效（同时清空现有缓存，AD-02）' },
  { key: 'decision_timeout_ms', label: '决策链路超时', kind: 'int', min: 50, max: 5000, unit: '毫秒',
    def: 200, effect: '立即生效' },
  { key: 'metric_bucket_granularity', label: '指标桶粒度', kind: 'enum', def: '1m',
    effect: '需重启（涉及定时聚合任务周期重建，BR-13-07）' },
];
const DEFAULTS = PARAM_FIELDS.reduce((o, f) => Object.assign(o, { [f.key]: f.def }), {});
const PARAM_LABEL = PARAM_FIELDS.reduce((o, f) => Object.assign(o, { [f.key]: f.label }), {});

// ============================================================
// 模块级状态
// ============================================================
let refs = null;
let mounted = false;
let hcTimer = null;
const inflight = new Set();

/** 各卡片最近一次的服务端响应（渲染只读它，不做本地推测）。 */
const data = {
  config: null,
  applied: null,
  stats: null, statsErr: null,
  throughput: null, health: null,
  users: null,
  usersFilter: { role: '', status: '', page: 1, page_size: 20 },
  engine: null, engineErr: null,
};
const inputs = {};            // 运行参数输入框
const selected = new Set();   // 批量停用勾选的账号
// 取值域与默认值：**优先用服务端下发的**（`RuntimeConfigOut.ranges` / `defaults`，BR-00-07 的
// 单一来源原则同样适用于参数域），服务端没给时才回落到本文件的常量（与 Spec §2.1/BR-13-01 同值）。
const limits = {};
let serverDefaults = null;

/** 某参数的合法区间：服务端 `ranges` 优先。 */
function limitOf(f) {
  const r = limits[f.key];
  if (Array.isArray(r) && r.length >= 2 && isNum(r[0]) && isNum(r[1])) {
    return { min: Number(r[0]), max: Number(r[1]) };
  }
  return { min: f.min, max: f.max };
}

/** 「恢复默认」要提交的值：服务端 `defaults` 优先。 */
function defaultPayload() {
  const out = Object.assign({}, DEFAULTS);
  if (serverDefaults && typeof serverDefaults === 'object') {
    PARAM_FIELDS.forEach((f) => {
      if (serverDefaults[f.key] !== undefined && serverDefaults[f.key] !== null) {
        out[f.key] = serverDefaults[f.key];
      }
    });
  }
  return out;
}

function track() {
  const c = new AbortController();
  inflight.add(c);
  return c;
}

export function dispose() {
  mounted = false;
  if (hcTimer) { clearInterval(hcTimer); hcTimer = null; }
  inflight.forEach((c) => c.abort());
  inflight.clear();
  document.removeEventListener('visibilitychange', onVisibility);
  refs = null;
}

// ============================================================
// 人话错误文案（Spec §5）：错误码 → 用户能读懂的一句话
// ============================================================
function errText(e) {
  const code = e && e.code;
  const msg = (e && e.message) || '操作失败';
  switch (code) {
    case 'SYS-4001': return msg.indexOf('参数') === 0 ? msg : `参数取值超出允许范围：${msg}`;
    case 'SYS-4002': return '短窗口必须小于长窗口，本次保存已被拒绝（一个参数都没改，原子性）。';
    case 'SYS-4003': return '模型引擎尚未实现（G-01），服务端明确拒绝，配置未变。';
    case 'SYS-4004': return msg.indexOf('已存在') >= 0 ? msg : `账号已存在：${msg}`;
    case 'SYS-4005': return '不能停用或删除当前登录账号（防止把自己锁在系统外）。';
    case 'SYS-4006': return '系统必须保留至少一个可用管理员，本次操作被拒绝。';
    case 'SYS-4007': return msg.indexOf('待处置') >= 0 ? msg
      : '该账号名下还有待处置案件，请先转交后再停用。';
    case 'SYS-4008': return '请先停用该账号再删除。';
    case 'SYS-5001': return '配置保存失败，已回滚（线上配置一个都没变，请重试）。';
    case 'SYS-5002': return '账号操作失败（未留下半成品），请重试。';
    case 'SYS-5003': return '组件探测超时：该组件显示「探测超时」，不影响其余组件。';
    case 'SYS-5004': return '配置已保存，但部分模块未生效，建议重启服务。';
    case 'NET-0001': return '网络不可达，请确认服务是否已启动。';
    default: return `${msg}${code ? `（${code}）` : ''}`;
  }
}

/** trace 后 6 位；**错误处理自己绝不能再抛错**（照 `audit.js` 的教训）。 */
function traceOf(e) {
  return (e && typeof e.shortTrace === 'function') ? e.shortTrace() : NA;
}

function showInline(box, msg) {
  if (!box) return;
  box.textContent = msg;
  box.classList.add('on');
}
function hideInline(box) {
  if (!box) return;
  box.textContent = '';
  box.classList.remove('on');
}

/** 契约字段可能被包一层（`{config:…}` / `{item:…}`）：两种都认，其余原样返回。 */
function unwrap(d) {
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    if (d.config && typeof d.config === 'object') return d.config;
    if (d.item && typeof d.item === 'object') return d.item;
  }
  return d;
}

// 写操作一律 `silent`：失败原因由本页面自己在弹窗/卡片里**翻成人话**展示
// （照 `rule_editor.js` 的做法），不叠加 api.js 的默认 toast——同一个错误弹两次。
const postSilent = (path, body) => api.request(path, { method: 'POST', body, silent: true });
const putSilent = (path, body) => api.request(path, { method: 'PUT', body, silent: true });
const delSilent = (path) => api.request(path, { method: 'DELETE', silent: true });
const userUrl = (username) => `${BASE}/users/${encodeURIComponent(username)}`;

// ============================================================
// 通用小工具
// ============================================================
function kv(k, v) {
  return ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: k }),
    v instanceof Node ? v : ui.h('span', { class: 'v', text: String(v) }),
  ]);
}

function fieldCell(labelText, node, hint = '', required = false, span = 1) {
  return ui.h('div', { class: 'field', style: span > 1 ? `grid-column:span ${span}` : null }, [
    ui.h('label', {}, [
      document.createTextNode(labelText),
      required ? ui.h('span', { class: 'req', text: ' *' }) : null,
    ]),
    node,
    hint ? ui.h('div', { class: 'hint', text: hint }) : null,
  ]);
}

/** 状态徽标：`异常/失败/已停止` 红、`正常/运行中` 绿、`超时/未使用/未接入` 橙。 */

function statusTag(text) {
  const s = String(text === null || text === undefined ? '' : text);
  if (/异常|失败|已停止|不可达/.test(s)) return ui.tag(s, 'high');
  if (/正常|运行中|已启用|ok/i.test(s)) return ui.tag(s, 'low');
  if (/超时|未使用|未接入|预留|未启用/.test(s)) return ui.tag(s, 'medium');
  return ui.tag(s || NA, 'plain');
}

// ============================================================
// 卡片一 · 风控运行参数 `rcCard`
// ============================================================
function paramField(f) {
  const wrap = ui.h('div', { class: 'field', dataset: { field: f.key } });
  const lim = limitOf(f);
  let control;
  if (f.kind === 'enum') {
    const opts = optionsOf('granularity');
    control = ui.selectBox(opts.length ? opts : [{ value: f.def, label: f.def }], f.def);
  } else {
    control = ui.h('input', {
      type: 'number', min: String(lim.min), max: String(lim.max), step: '1',
      value: String(f.def), dataset: { role: 'param-input', field: f.key },
    });
  }
  control.dataset.fieldKey = f.key;
  inputs[f.key] = control;
  wrap.appendChild(ui.h('label', {}, [
    document.createTextNode(f.label),
    f.kind === 'int' ? ui.h('span', { class: 'req', text: ' *' }) : null,
  ]));
  wrap.appendChild(control);
  wrap.appendChild(ui.h('div', {
    class: 'hint',
    text: f.kind === 'int'
      ? `范围 ${lim.min}~${lim.max} ${f.unit} · ${f.effect}` : f.effect,
  }));
  wrap.appendChild(ui.h('div', { class: 'field-err' }));
  return wrap;
}

/** 按服务端返回标红/清红某个参数（本地校验与服务端复核共用）。 */
function markField(key, msg) {
  if (!refs || !refs.rcForm) return;
  const wrap = refs.rcForm.querySelector(`[data-field="${key}"]`);
  if (!wrap) return;
  wrap.classList.add('is-invalid');
  const box = wrap.querySelector('.field-err');
  if (box) box.textContent = msg || '取值不合法';
}
function clearMarks() {
  if (!refs || !refs.rcForm) return;
  refs.rcForm.querySelectorAll('.field').forEach((w) => {
    w.classList.remove('is-invalid');
    const box = w.querySelector('.field-err');
    if (box) box.textContent = '';
  });
}

function renderConfigCard() {
  refs.rcForm = ui.h('div', { class: 'form-grid' }, PARAM_FIELDS.map(paramField));
  refs.rcMeta = ui.h('div', { class: 'hint', dataset: { role: 'rc-version' }, text: '配置版本：加载中…' });
  refs.rcErr = ui.h('div', { class: 'form-error', dataset: { role: 'rc-error' } });
  refs.rcMsg = ui.h('div', { class: 'settings-msg' });
  refs.rcSave = ui.button('保存运行参数', { variant: 'primary', onClick: () => doSaveParams() });
  refs.rcRestore = ui.button('恢复默认', { variant: 'ghost', onClick: openRestoreConfirm });
  const el = ui.card('风控运行参数', [
    refs.rcForm,
    refs.rcMeta,
    refs.rcErr,
    refs.rcMsg,
    ui.h('div', { class: 'form-actions' }, [refs.rcSave, refs.rcRestore]),
    ui.h('div', { class: 'hint', text:
      '窗口时长与容量对应悬空点 G-02 / G-12；名单缓存 TTL 决定新增黑名单的最长生效延迟（AD-02）。'
      + '保存是原子的（任一项非法则全部不生效）；服务端会明确回传「已立即生效」与「需重启」两组参数。' }),
  ]);
  el.dataset.role = 'rc-card';
  return el;
}

/** 把服务端值写进表单；读不到时**留空**（绝不把默认值伪装成服务端现值）。 */
function fillParams(cfg) {
  PARAM_FIELDS.forEach((f) => {
    const el = inputs[f.key];
    if (!el) return;
    const v = cfg ? cfg[f.key] : null;
    if (f.kind === 'enum') el.value = (v === null || v === undefined || v === '') ? f.def : String(v);
    else el.value = isNum(v) ? String(v) : '';
  });
}

function paintConfigMeta() {
  if (!refs || !refs.rcMeta) return;
  const cfg = data.config;
  if (!cfg) { refs.rcMeta.textContent = '配置版本：—（未取到）'; return; }
  const ver = cfg.config_version === undefined || cfg.config_version === null ? NA : cfg.config_version;
  const at = isNum(cfg.effective_at) ? dateTime(cfg.effective_at) : NA;
  refs.rcMeta.textContent = `配置版本：${ver} · 本次生效时间：${at}`;
}

function setParamEditable(on) {
  PARAM_FIELDS.forEach((f) => { if (inputs[f.key]) inputs[f.key].disabled = !on; });
  if (refs.rcSave) refs.rcSave.disabled = !on;
  if (refs.rcRestore) refs.rcRestore.disabled = !on;
}

async function loadConfig() {
  const c = track();
  try {
    const d = await api.request(`${BASE}/config`, { signal: c.signal, silent: true });
    data.config = unwrap(d);
    // 服务端下发的取值域/默认值优先（避免前端再抄一份数字）
    if (data.config && typeof data.config.ranges === 'object') {
      Object.assign(limits, data.config.ranges || {});
    }
    if (data.config && typeof data.config.defaults === 'object') {
      serverDefaults = data.config.defaults;
    }
    hideInline(refs.rcErr);
    clearMarks();
    fillParams(data.config);
    paintConfigMeta();
    setParamEditable(true);
  } catch (e) {
    if (e.name === 'AbortError') return;
    data.config = null;
    fillParams(null);
    paintConfigMeta();
    setParamEditable(false);
    showInline(refs.rcErr, `运行参数读取失败：${errText(e)}（trace ${traceOf(e)}）。`
      + '为避免把默认值误当成服务端现值写回，保存已禁用。');
  } finally {
    inflight.delete(c);
  }
}

/** 本地校验：越界即拒绝，**不截断、不取整**（BR-13-01）。通过则返回完整 payload。 */
function validateParams() {
  clearMarks();
  hideInline(refs.rcErr);
  const payload = {};
  let bad = 0;
  PARAM_FIELDS.forEach((f) => {
    const el = inputs[f.key];
    if (f.kind === 'enum') {
      payload[f.key] = el.value;
      if (!el.value) { markField(f.key, '请选择指标桶粒度'); bad += 1; }
      return;
    }
    const raw = String(el.value).trim();
    if (raw === '') { markField(f.key, `${f.label} 不能为空`); bad += 1; return; }
    const n = Number(raw);
    if (!Number.isInteger(n)) {
      markField(f.key, `${f.label} 必须是整数（不接受小数，也不会被静默取整）`);
      bad += 1; return;
    }
    const lim = limitOf(f);
    if (n < lim.min || n > lim.max) {
      markField(f.key, `${f.label} 超出允许范围（${lim.min}~${lim.max}${f.unit ? ` ${f.unit}` : ''}）：`
        + '已拒绝保存，输入值保持原样，未做任何截断');
      bad += 1; return;
    }
    payload[f.key] = n;
  });
  // 交叉校验：短窗必须 < 长窗（SYS-4002 的本地前置；服务端仍会复核）
  if (payload.short_window_min !== undefined && payload.long_window_min !== undefined
      && payload.short_window_min >= payload.long_window_min) {
    markField('short_window_min', '短窗口必须小于长窗口（BR-13-01）');
    markField('long_window_min', '长窗口必须大于短窗口（BR-13-01）');
    bad += 1;
  }
  if (bad) {
    showInline(refs.rcErr, `有 ${bad} 项未通过校验，**一个参数都没有提交**（BR-13-01 的原子性）。`
      + '请按字段下方的红字修改后重试。');
    return null;
  }
  return payload;
}

function renderApplied(r) {
  const res = r || {};
  const applied = Array.isArray(res.applied) ? res.applied : [];
  const restart = Array.isArray(res.requires_restart) ? res.requires_restart : [];
  const changed = (res.changed && typeof res.changed === 'object') ? res.changed : {};
  // 服务端给了结构化的 before/after（`changed`）就显示「10 → 12」：二次确认与事后追溯
  // 都靠它，"哪些值真的变了"不该让用户自己比对。
  const nameOf = (k) => {
    const label = PARAM_LABEL[k] || k;
    const c = changed[k];
    const b = c && (c.before === undefined ? c.from : c.before);
    const a = c && (c.after === undefined ? c.to : c.after);
    if (b === undefined || a === undefined) return label;
    return `${label}（${String(b)} → ${String(a)}）`;
  };
  const kids = [
    ui.h('div', { class: 'settings-line ok', dataset: { role: 'rc-applied' }, text:
      applied.length ? `已立即生效：${applied.map(nameOf).join('、')}`
        : '已立即生效：本次没有参数发生变化，以 /system/config 的回读值为准（BR-13-02）' }),
  ];
  kids.push(ui.h('div', { class: 'settings-line warn', dataset: { role: 'rc-restart' }, text:
    restart.length ? `需重启后生效：${restart.map(nameOf).join('、')}（BR-13-07：定时聚合任务周期重建）`
      : '需重启后生效：无' }));
  if (Array.isArray(res.notices)) {
    res.notices.forEach((n) => {
      const txt = n && (n.message || n.detail || n.code);
      if (txt) kids.push(ui.h('div', { class: 'settings-line warn', text: `${n.code ? `[${n.code}] ` : ''}${txt}` }));
    });
  }
  if (res.config_version !== undefined && res.config_version !== null) {
    kids.push(ui.h('div', { class: 'hint', text: `新配置版本：${res.config_version}` }));
  }
  return kids;
}

async function doSaveParams(override) {
  if (!data.config) { ui.toast('参数尚未成功读取，保存已禁用', 'warn'); return; }
  const payload = override || validateParams();
  if (!payload) return;
  ui.buttonBusy(refs.rcSave, true, '保存中');
  try {
    const r = await putSilent(`${BASE}/config`, payload);
    data.applied = r || {};
    ui.mount(refs.rcMsg, renderApplied(data.applied));
    ui.toast('运行参数已保存', 'ok');
    await loadConfig();          // 回读：页面显示服务端现值，不做本地推测
  } catch (e) {
    showInline(refs.rcErr, `保存失败：${errText(e)}（trace ${traceOf(e)}）`);
    // 服务端定位到字段时同步标红（SYS-4002 同时涉及短窗与长窗两个字段）
    invalidFromServer(e && e.data ? (e.data.field || e.data.key) : null, e);
  } finally {
    ui.buttonBusy(refs.rcSave, false);
    refs.rcSave.disabled = !data.config;
  }
}

/** 服务端定位到字段时的补标（`SYS-4002` 会同时涉及两个字段）。 */
function invalidFromServer(field, e) {
  if ((e && e.code) === 'SYS-4002') {
    markField('short_window_min', '短窗口必须小于长窗口（服务端 SYS-4002）');
    markField('long_window_min', '长窗口必须大于短窗口（服务端 SYS-4002）');
  } else if (field) {
    markField(field, `${PARAM_LABEL[field] || field}：${errText(e)}`);
  }
}

/** BR-13-09：「恢复默认」影响在线行为，必须先二次确认。 */
function openRestoreConfirm() {
  const errBox = ui.h('div', { class: 'form-error' });
  const payload = defaultPayload();
  let m = null;
  m = ui.modal({
    title: '恢复默认运行参数',
    confirmText: '确认恢复默认',
    variant: 'warn',
    body: [
      ui.h('div', { class: 'kv-list' }, PARAM_FIELDS.map((f) => kv(f.label,
        f.kind === 'enum' ? `${labelOf('granularity', payload[f.key], payload[f.key])}（${payload[f.key]}）`
          : `${payload[f.key]} ${f.unit || ''}`))),
      ui.h('div', { class: 'impact-note', text:
        '恢复默认会按 Spec §2.1 的默认值**整体保存**（默认值取自服务端 `GET /system/config` 的 '
        + '`defaults`，与后端同一份），并立即影响在线决策行为（BR-13-09）。' }),
      errBox,
    ],
    onConfirm: async () => {
      if (m) m.confirmBtn.disabled = true;
      try {
        const r = await putSilent(`${BASE}/config`, payload);
        data.applied = r || {};
        ui.mount(refs.rcMsg, renderApplied(data.applied));
        ui.toast('已恢复默认运行参数', 'ok');
        await loadConfig();
      } catch (e) {
        showInline(errBox, `恢复默认失败：${errText(e)}（trace ${traceOf(e)}）`);
        if (m) m.confirmBtn.disabled = false;
        return false;
      }
      return true;
    },
  });
}

// ============================================================
// 卡片二 · 吞吐与接口健康度 `hcCard`
// ============================================================
function hcStatCard(key, title, valueText, unit, caption, tone) {
  return ui.h('div', { class: `stat-card ${tone || ''}`.trim(), dataset: { card: key } }, [
    ui.h('div', { class: 'stat-title', text: title }),
    ui.h('div', { class: 'stat-value' }, [
      ui.h('span', { text: valueText }),
      unit ? ui.h('span', { class: 'stat-unit', text: unit }) : null,
    ]),
    caption ? ui.h('div', { class: 'hint', text: caption }) : null,
  ]);
}

function renderHealthCard() {
  refs.hcStats = ui.h('div', { class: 'stat-row', dataset: { role: 'hc-stats' } });
  refs.hcComps = ui.h('div', { dataset: { role: 'hc-components' } });
  refs.hcNote = ui.h('div', { class: 'hint', dataset: { role: 'hc-source' } });
  refs.hcTime = ui.h('span', { class: 'hint', dataset: { role: 'hc-time' }, text: '尚未刷新' });
  refs.hcErr = ui.h('div', { class: 'form-error' });
  const btn = ui.button('刷新', { variant: 'ghost', onClick: () => loadHealth() });
  const el = ui.card('吞吐与接口健康度', [
    ui.toolbar(
      [ui.h('div', { class: 'hint', text: '每 10 秒自动刷新（页面隐藏时暂停，BR-13-26）；本卡片只读，与"保存参数"互不影响。' })],
      [refs.hcTime, btn],
    ),
    refs.hcStats,
    refs.hcComps,
    refs.hcNote,
    refs.hcErr,
  ]);
  el.dataset.role = 'hc-card';
  return el;
}

/** QPS / P95 的口径优先级：13 的 `/system/stats` → 11 的 `/metrics/throughput`。 */
function throughputView() {
  const st = data.stats;
  const th = data.throughput;
  const qps = st && isNum(st.qps) ? st.qps : (th && isNum(th.qps_1m) ? th.qps_1m : null);
  const p95 = st && isNum(st.decision_p95_ms) ? st.decision_p95_ms
    : (th && isNum(th.p95_elapsed_ms) ? th.p95_elapsed_ms : null);
  const src = st && isNum(st.qps) ? '/system/stats' : (th && isNum(th.qps_1m) ? '/metrics/throughput' : null);
  return { qps, p95, src };
}

/** 名单缓存命中率（0~1）→ 百分比数；取不到返回 null。 */
function cacheHitPct() {
  const st = data.stats;
  if (!st || !isNum(st.list_cache_hit_rate)) return null;
  const v = Number(st.list_cache_hit_rate);
  return v <= 1 ? v * 100 : v;    // 契约是 0~1；若服务端给的是百分数也如实按百分数显示
}

/** 组件状态表：13 的 `components` 优先（含 `status_label` 中文与 `detail`）。 */
const COMPONENT_TONE = {
  ok: 'low', running: 'low', error: 'high', stopped: 'high',
  unused: 'medium', reserved: 'medium', timeout: 'medium',
};

function healthComponentRows() {
  const st = data.stats || {};
  if (Array.isArray(st.components) && st.components.length) {
    return st.components.map((c) => [
      c.name || NA,
      ui.tag(c.status_label || c.status || NA, COMPONENT_TONE[String(c.status)] || 'plain'),
      c.detail === undefined || c.detail === null ? NA : String(c.detail),
    ]);
  }
  // 降级：只用 /health 里**真的读到**的字段拼，缺的写 —（不编数）
  const hl = data.health || {};
  const m = hl.mongo || {};
  const comps = hl.components || {};
  const win = st.window || null;
  const mongoOk = m.connected === true ? '正常' : (m.connected === false ? '异常' : NA);
  const mongoDetail = m.connected === false && m.error
    ? String(m.error).slice(0, 90)
    : `库 ${m.db || NA}${isNum(m.latency_ms) ? ` · 延迟 ${m.latency_ms} ms` : ''}`
      + `${win ? ` · 窗口内事件 ${win.total_events} 条` : ''}`;
  return [
    ['MongoDB', statusTag(mongoOk), mongoDetail],
    ['MinIO', statusTag('未使用'), '当前不启用（Step2 §1.3）'],
    ['事件流模拟器', statusTag(NA), '模拟器状态由 /system/stats 提供；admin 无 sim:run 权限，前端不越权探测'],
    ['决策引擎', statusTag(comps.decision_provider || 'rule-engine-v1'),
      `模型引擎未接入（G-01） · 装配类 ${comps.model_engine || NA}`],
  ];
}

function paintHealth() {
  if (!refs || !refs.hcStats) return;
  const { qps, p95, src } = throughputView();
  const hit = cacheHitPct();
  const st = data.stats || {};
  const hl = data.health || {};
  // Mongo 连通性：13 的 `/system/stats` 优先（它还带集合数），否则回落 `/health` 的探活结果
  const m = (st.mongo && typeof st.mongo === 'object') ? st.mongo : hl.mongo;
  const mongoKnown = !!m;
  const mongoOk = !!m && m.connected === true;
  const collCnt = m && isNum(m.collections) ? ` · ${m.collections} 个集合` : '';

  const qpsTxt = isNum(qps) ? fixed(qps, 1) : NA;
  const p95Txt = isNum(p95) ? int(p95) : NA;
  const hitTxt = hit === null ? NA : fixed(hit, 1);
  const mongoTxt = !mongoKnown ? NA : (mongoOk ? '正常' : '异常');

  ui.mount(refs.hcStats, [
    hcStatCard('qps', '实时 QPS', qpsTxt, isNum(qps) ? '事件/秒' : '',
      src ? `近 1 分钟 · 来源 ${src}` : '取不到（/system/stats 与 /metrics/throughput 均不可用）', ''),
    hcStatCard('p95', '决策 P95 延迟', p95Txt, isNum(p95) ? 'ms' : '',
      isNum(p95) && Number(p95) > 50 ? '超过概要设计 §6 目标（≤50ms），已标红' : '目标 ≤50ms（概要设计 §6）',
      isNum(p95) && Number(p95) > 50 ? 'is-bad' : ''),
    hcStatCard('cache', '名单缓存命中率', hitTxt, hit === null ? '' : '%',
      hit === null ? '仅 /system/stats 提供，缺则显示 —（不编数）'
        : (hit < 90 ? '低于 90%，已标橙' : '目标 ≥90%'),
      hit !== null && hit < 90 ? 'is-warn' : ''),
    hcStatCard('mongo', 'Mongo 连通性', mongoTxt, '',
      mongoKnown ? `库 ${m.db || NA}${collCnt}${isNum(m.latency_ms) ? ` · 延迟 ${m.latency_ms} ms` : ''}`
        : '未取到 /health 与 /system/stats',
      mongoKnown && !mongoOk ? 'is-bad' : ''),
  ]);

  ui.mount(refs.hcComps, ui.table(
    [{ name: '组件', width: 150 }, { name: '状态', width: 150 }, { name: '说明' }],
    healthComponentRows()));

  const srcs = [];
  srcs.push(data.stats ? '/system/stats（模块 13 运维看板：QPS / P95 / 名单缓存命中率 / 窗口）'
    : '/system/stats 不可用（模块 13 未接入或该次请求失败）');
  if (!data.stats && data.throughput) srcs.push('/metrics/throughput（模块 11 现成口径，降级取 QPS 与 P95）');
  if (data.health) srcs.push('/health（存活探针：Mongo 连通性 + 真实装配组件）');
  refs.hcNote.textContent = `数据来源：${srcs.join('；')}。缺哪个指标就显示 —，不在前端另算一份（BR-13-25）。`;
  refs.hcTime.textContent = `最近刷新：${dateTime(Date.now())}`;

  const errs = [];
  if (data.statsErr) errs.push(`/system/stats：${data.statsErr}`);
  if (data.health === null) errs.push('/health：取不到');
  if (errs.length) showInline(refs.hcErr, `部分指标未取到——${errs.join('；')}（对应格子显示 —）`);
  else hideInline(refs.hcErr);
}

async function loadHealth() {
  const c = track();
  try {
    // 三个请求全部 `silent`：本卡片 10 秒自动刷新一次，失败由卡片内的说明行如实呈现，
    // 不让后台轮询在限流/断网时反复弹 toast（toast 是给"用户主动操作"用的）。
    await Promise.all([
      api.request(`${BASE}/stats`, { signal: c.signal, silent: true })
        .then((d) => { data.stats = unwrap(d); data.statsErr = null; })
        .catch((e) => { if (e.name !== 'AbortError') { data.stats = null; data.statsErr = errText(e); } }),
      api.request(`${api.API_PREFIX}/metrics/throughput`, { query: { window: '5m' }, signal: c.signal, silent: true })
        .then((d) => { data.throughput = d; })
        .catch(() => { data.throughput = null; }),
      api.request('/health', { signal: c.signal, silent: true })
        .then((d) => { data.health = d; })
        .catch(() => { data.health = null; }),
    ]);
    paintHealth();
  } catch (e) {
    if (e.name !== 'AbortError') showInline(refs.hcErr, `健康度加载失败：${errText(e)}`);
  } finally {
    inflight.delete(c);
  }
}

function restartHcTimer() {
  if (hcTimer) { clearInterval(hcTimer); hcTimer = null; }
  if (!mounted) return;
  hcTimer = setInterval(() => {
    // 兜底：登出路径不会调用 dispose（壳只清空 #view），靠容器存活性自毁，否则定时器一直活着
    if (!refs || !refs.hcCard || !refs.hcCard.isConnected) { dispose(); return; }
    if (document.hidden) return;    // BR-13-26：页面隐藏时暂停
    loadHealth();
  }, HC_REFRESH_MS);
}

function onVisibility() {
  if (!mounted) return;
  if (document.hidden) {
    if (hcTimer) { clearInterval(hcTimer); hcTimer = null; }
  } else {
    restartHcTimer();
    loadHealth();                   // 回到页面立即补一次，别让画面停在旧数据上
  }
}

// ============================================================
// 卡片三 · 账号与角色管理 `usCard`
// ============================================================
function opBtn(label, op, variant, onClick, { disabled = false, title = '' } = {}) {
  const b = ui.button(label, { variant, onClick, disabled, title });
  b.classList.add('btn-sm');
  b.dataset.op = op;
  return b;
}

function renderUsersCard() {
  refs.usRole = ui.selectBox([{ value: '', label: '全部角色' }].concat(optionsOf('role')),
    data.usersFilter.role, (v) => { data.usersFilter.role = v; data.usersFilter.page = 1; loadUsers(); });
  refs.usStatus = ui.selectBox([{ value: '', label: '全部状态' }].concat(optionsOf('sys_user_status')),
    data.usersFilter.status, (v) => { data.usersFilter.status = v; data.usersFilter.page = 1; loadUsers(); });
  refs.usTable = ui.h('div', { dataset: { role: 'us-table' } });
  refs.usErr = ui.h('div', { class: 'form-error' });
  refs.usMsg = ui.h('div', { class: 'settings-msg' });
  refs.usPage = ui.h('span', { text: '' });
  refs.usPrev = ui.button('上一页', { variant: 'ghost', onClick: () => {
    if (data.usersFilter.page > 1) { data.usersFilter.page -= 1; loadUsers(); }
  } });
  refs.usNext = ui.button('下一页', { variant: 'ghost', onClick: () => {
    data.usersFilter.page += 1; loadUsers();
  } });
  refs.usAdd = ui.button('＋ 新增账号', { variant: 'primary', onClick: openAddUser,
    title: optionsOf('role').length ? '' : '角色枚举未加载（GET /common/enums）' });
  if (!optionsOf('role').length) refs.usAdd.disabled = true;
  refs.usBatch = ui.button('批量停用', { variant: 'plain', onClick: openBatchDisable });
  const el = ui.card('账号与角色管理', [
    ui.toolbar([refs.usRole, refs.usStatus], [refs.usBatch, refs.usAdd]),
    refs.usErr,
    refs.usMsg,
    refs.usTable,
    ui.h('div', { class: 'pager' }, [refs.usPage, refs.usPrev, refs.usNext]),
    ui.h('div', { class: 'hint', text:
      '本模块是 E19 sys_users 的唯一写入方（BR-13-10）；角色只能取固定枚举三项之一（BR-13-12）；'
      + '删除为软删除（status=deleted，保留审计可追溯性，BR-13-17）。停用 / 删除 / 改角色 / 重置密码'
      + '都先弹二次确认；服务端会拒绝「停用或删除自己」「停用或删除最后一个可用管理员」这类操作'
      + '（BR-13-13/14），失败原因原样翻成人话展示。' }),
  ]);
  el.dataset.role = 'us-card';
  return el;
}

async function loadUsers() {
  const c = track();
  ui.mount(refs.usTable, ui.loading(4));
  try {
    const d = await api.request(`${BASE}/users`, {
      query: {
        role: data.usersFilter.role,
        status: data.usersFilter.status,
        page: data.usersFilter.page,
        page_size: data.usersFilter.page_size,
      },
      signal: c.signal,
      silent: true,
    });
    data.users = d || {};
    hideInline(refs.usErr);
    renderUserRows();
  } catch (e) {
    if (e.name === 'AbortError') return;
    data.users = null;
    ui.mount(refs.usTable, ui.empty('账号列表加载失败', `${errText(e)}（trace ${traceOf(e)}）`));
    refs.usPage.textContent = '';
  } finally {
    inflight.delete(c);
  }
}

function renderUserRows() {
  const res = data.users || {};
  const items = res.items || res.users || [];
  const total = res.total === undefined ? items.length : res.total;
  const size = res.page_size || data.usersFilter.page_size;
  const pages = res.pages || Math.max(1, Math.ceil(total / size));
  refs.usPage.textContent = `共 ${total} 个账号 · 第 ${data.usersFilter.page} / ${Math.max(1, pages)} 页`;
  refs.usPrev.disabled = data.usersFilter.page <= 1;
  refs.usNext.disabled = pages === 0 || data.usersFilter.page >= pages;

  if (!items.length) {
    ui.mount(refs.usTable, ui.empty('暂无账号（请先通过种子脚本初始化管理员）',
      '若这是筛选后的结果，请放宽角色/状态条件。'));
    return;
  }

  const me = (session.getUser() || {}).username || '';
  const columns = [
    { name: '', width: 34 }, { name: '账号', width: 120 }, { name: '姓名', width: 90 },
    { name: '角色', width: 110 }, { name: '状态', width: 76 }, { name: '最后登录', width: 150 },
    { name: '操作' },
  ];

  const rows = items.map((u) => {
    // `is_self` 由服务端给出（UserOut.is_self）；同时保留本地比对做兜底。
    // 注意：这里**只做展示**，停用/删除的最终判定仍在服务端（BR-13-13/14）。
    const isSelf = u.is_self === true || (!!me && u.username === me);
    const pend = Number(u.pending_case_cnt === undefined ? 0 : u.pending_case_cnt);
    const cb = ui.h('input', { type: 'checkbox', dataset: { role: 'us-check' } });
    cb.checked = selected.has(u.username);
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(u.username); else selected.delete(u.username);
    });

    const nameCell = ui.h('div', { class: 'mono' }, [
      document.createTextNode(String(u.username || NA)),
      isSelf ? ui.h('span', { class: 'tag brand', style: 'margin-left:6px', text: '当前登录' }) : null,
    ]);

    const disabled = u.status === 'disabled';
    const canDelete = disabled && pend === 0;
    const delTitle = !disabled ? '仅"已停用"的账号可删除（BR-13-17）'
      : (pend > 0 ? `该账号有 ${pend} 个待处置案件，请先转交` : '删除该账号（软删除，保留审计可追溯性）');

    return ui.h('tr', { dataset: { user: String(u.username || '') } }, [
      ui.h('td', {}, cb),
      ui.h('td', {}, nameCell),
      ui.h('td', { text: u.real_name || NA }),
      // 角色/状态标签优先读服务端下发的 `role_label` / `status_label`（BR-00-18 单一来源）
      ui.h('td', {}, ui.tag(u.role_label || labelOf('role', u.role, u.role || NA), 'brand')),
      ui.h('td', {}, ui.tag(u.status_label || labelOf('sys_user_status', u.status, u.status || NA),
        disabled ? 'gray' : 'low')),
      ui.h('td', { text: u.last_login_at ? dateTime(u.last_login_at) : '从未登录' }),
      ui.h('td', {}, ui.h('div', { class: 'row-ops' }, [
        opBtn('改角色', 'role', 'ghost', () => openRoleModal(u)),
        opBtn('重置密码', 'reset', 'ghost', () => openResetModal(u)),
        disabled
          ? opBtn('启用', 'enable', 'ok', () => doEnable(u))
          : opBtn('停用', 'disable', 'warn', () => openDisableConfirm(u)),
        opBtn('删除', 'delete', 'ghost', () => openDeleteConfirm(u),
          { disabled: !canDelete, title: delTitle }),
      ])),
    ]);
  });

  ui.mount(refs.usTable, ui.table(columns, rows));
}

/** 初始/重置密码「仅显示一次」的弹窗（BR-13-19：不落明文日志）。
 *
 * ⚠️ **必须等上一个弹窗关掉之后再打开**：`ui.modal` 的确认按钮在 `onConfirm` 返回非
 * false 时会执行 `close()`（清空 `#modalRoot`）。若在 `onConfirm` 里直接调用本函数，
 * 新弹窗会被那一次 `close()` 一起清掉——表现为"重置成功、也生成了口令，但口令框一闪不见"。
 * 因此调用点一律用 `setTimeout(..., 0)` 把它排到 close 之后。 */
function openPasswordOnce(username, pw) {
  let m = null;
  const copyBtn = ui.button('复制口令', { variant: 'ghost', onClick: async () => {
    try {
      await navigator.clipboard.writeText(pw);
      ui.toast('已复制到剪贴板', 'ok');
    } catch (e) {
      ui.toast('复制失败，请手动选中文本复制', 'warn');
    }
  } });
  m = ui.modal({
    title: `账号 ${username} 的初始口令（仅显示这一次）`,
    confirmText: '我已妥善保存',
    body: [
      ui.h('div', { class: 'mono settings-pw', dataset: { role: 'us-password' }, text: pw }),
      ui.h('div', { class: 'impact-note', text:
        '服务端只在本次响应里返回一次明文口令，不落明文日志、不写入任何接口（BR-13-19）。'
        + '关闭本弹窗后无法再查看，只能重新重置。' }),
      ui.h('div', { class: 'form-actions' }, [copyBtn]),
    ],
    onConfirm: () => true,
  });
  return m;
}

function openAddUser() {
  const roles = optionsOf('role');
  const nameI = ui.inputBox({ placeholder: '4~32 字符：字母/数字/_ . -' });
  const realI = ui.inputBox({ placeholder: '真实姓名（≤20 字，选填）' });
  const roleS = ui.selectBox(roles, 'reviewer');
  const pwdI = ui.h('input', { type: 'password', placeholder: '留空则由服务端生成（≥12 位随机）' });
  const errBox = ui.h('div', { class: 'form-error' });
  let m = null;
  m = ui.modal({
    title: '新增账号',
    confirmText: '创建账号',
    body: [
      ui.h('div', { class: 'form-grid' }, [
        fieldCell('账号', nameI, '全局唯一，创建后不可修改（BR-13-11）', true),
        fieldCell('姓名', realI, '必填，最长 32 个字（服务端同样要求非空）', true),
        fieldCell('角色', roleS, '固定枚举三项，不允许自定义（BR-13-12）', true),
        fieldCell('初始密码（选填）', pwdI,
          '6~64 位；留空由服务端生成并只显示一次（BR-13-19）', false, 2),
      ]),
      errBox,
    ],
    onConfirm: async () => {
      const username = String(nameI.value).trim();
      const realName = String(realI.value).trim();
      const pw = String(pwdI.value);
      hideInline(errBox);
      if (!/^[a-zA-Z0-9_.-]{4,32}$/.test(username)) {
        showInline(errBox, '账号需为 4~32 个字母/数字/_ . -（与模块 01 的登录账号规则一致）。');
        return false;
      }
      if (!realName) { showInline(errBox, '姓名必填（服务端要求 1~32 字）。'); return false; }
      if (realName.length > 32) { showInline(errBox, '姓名最长 32 个字。'); return false; }
      if (pw && (pw.length < 6 || pw.length > 64)) {
        showInline(errBox, '初始密码需 6~64 位（BR-13-19）。'); return false;
      }
      const body = { username, role: roleS.value, real_name: realName };
      if (pw) body.initial_password = pw;
      if (m) m.confirmBtn.disabled = true;
      try {
        const r = await postSilent(`${BASE}/users`, body);
        ui.toast(`账号已创建：${username}`, 'ok');
        selected.delete(username);
        await loadUsers();
        const generated = r && (r.generated_password || r.initial_password);
        if (generated) setTimeout(() => openPasswordOnce(username, generated), 30);
        else ui.toast(`${username} 的密码已按你填写的初始口令设置`, 'ok');
      } catch (e) {
        showInline(errBox, errText(e));
        if (m) m.confirmBtn.disabled = false;
        return false;
      }
      return true;
    },
  });
}

/** 改角色（+可选改姓名）：BR-13-14「不允许把最后一个管理员降级」由服务端拒绝。 */
function openRoleModal(u) {
  const roleS = ui.selectBox(optionsOf('role'), u.role);
  const realI = ui.inputBox({ value: u.real_name || '', placeholder: '姓名（≤20 字）' });
  const errBox = ui.h('div', { class: 'form-error' });
  let m = null;
  m = ui.modal({
    title: `修改账号：${u.username}`,
    confirmText: '确认修改',
    variant: 'warn',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('账号', ui.h('span', { class: 'mono', text: String(u.username || '') })),
        kv('当前角色', labelOf('role', u.role, u.role || NA)),
        kv('当前状态', labelOf('sys_user_status', u.status, u.status || NA)),
      ]),
      ui.h('div', { class: 'form-grid' }, [
        fieldCell('角色', roleS, '改动会立即改变该账号的权限与可见菜单（权限矩阵唯一真源）'),
        fieldCell('姓名', realI),
      ]),
      ui.h('div', { class: 'impact-note', text:
        '服务端会拒绝把**最后一个可用管理员**降级（BR-13-14）；若命中，本次修改失败、角色不变。' }),
      errBox,
    ],
    onConfirm: async () => {
      hideInline(errBox);
      const body = {};
      if (roleS.value !== u.role) body.role = roleS.value;
      if (String(realI.value).trim() !== String(u.real_name || '')) {
        body.real_name = String(realI.value).trim();
      }
      if (!Object.keys(body).length) { ui.toast('没有需要保存的改动', 'warn'); return true; }
      if (m) m.confirmBtn.disabled = true;
      try {
        await putSilent(userUrl(u.username), body);
        ui.toast(`已更新账号：${u.username}`, 'ok');
        await loadUsers();
      } catch (e) {
        showInline(errBox, errText(e));
        if (m) m.confirmBtn.disabled = false;
        return false;
      }
      return true;
    },
  });
}

/** 重置密码（BR-13-16：该账号所有令牌立即失效）。 */
function openResetModal(u) {
  const pwdI = ui.h('input', { type: 'password', placeholder: '新口令（6~64 位）', style: 'display:none' });
  const modeS = ui.selectBox([
    { value: 'auto', label: '自动生成（≥12 位随机，仅显示一次）' },
    { value: 'manual', label: '由我指定新口令（6~64 位）' },
  ], 'auto', (v) => { pwdI.style.display = v === 'manual' ? '' : 'none'; });
  const errBox = ui.h('div', { class: 'form-error' });
  let m = null;
  m = ui.modal({
    title: `重置密码：${u.username}`,
    confirmText: '确认重置',
    variant: 'warn',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('账号', ui.h('span', { class: 'mono', text: String(u.username || '') })),
        kv('姓名', u.real_name || NA),
        kv('角色', labelOf('role', u.role, u.role || NA)),
      ]),
      ui.h('div', { class: 'form-grid' }, [fieldCell('重置方式', modeS, '', true), fieldCell('新口令', pwdI)]),
      ui.h('div', { class: 'impact-note', text:
        '重置后该账号**所有令牌立即失效**，必须用新口令重新登录（BR-13-16）。' }),
      errBox,
    ],
    onConfirm: async () => {
      hideInline(errBox);
      const body = {};
      if (modeS.value === 'manual') {
        const pw = String(pwdI.value);
        if (pw.length < 6 || pw.length > 64) {
          showInline(errBox, '新口令需 6~64 位（与模块 01 的密码规则一致）。'); return false;
        }
        body.new_password = pw;
      }
      if (m) m.confirmBtn.disabled = true;
      try {
        const r = await postSilent(`${userUrl(u.username)}/reset-password`, body);
        ui.toast(`已重置 ${u.username} 的密码`, 'ok');
        const generated = r && (r.generated_password || r.initial_password);
        if (generated) setTimeout(() => openPasswordOnce(u.username, generated), 30);
        else ui.toast('该账号所有令牌已失效，需用新口令重新登录（BR-13-16）', 'warn', 5000);
      } catch (e) {
        showInline(errBox, errText(e));
        if (m) m.confirmBtn.disabled = false;
        return false;
      }
      return true;
    },
  });
}

function openDisableConfirm(u) {
  const errBox = ui.h('div', { class: 'form-error' });
  // 案件转交（BR-13-15）：名下有待处置案件时必须指定接收人（reviewer），否则服务端以
  // SYS-4007 拒绝。接收人下拉按需拉取（`GET /system/users?role=reviewer&status=active`）。
  const transferS = ui.h('select', { dataset: { role: 'us-transfer' } });
  transferS.appendChild(ui.h('option', { value: '', text: '不转交（名下无待处置案件时选它）' }));
  const transferRow = fieldCell('案件接收人（可选）', transferS,
    '仅当该账号名下有未处置案件时需要：必须指定一个**启用中的审核员**（BR-13-15）');
  api.request(`${BASE}/users`, { query: { role: 'reviewer', status: 'active', page_size: 100 }, silent: true })
    .then((d) => {
      const items = (d && (d.items || d.users)) || [];
      items.filter((x) => x.username !== u.username).forEach((x) => {
        transferS.appendChild(ui.h('option', { value: x.username,
          text: `${x.username}（${x.real_name || '—'}）` }));
      });
    })
    .catch(() => { /* 拉不到接收人列表时保持“不转交”，服务端会以 SYS-4007 如实拒绝 */ });

  let m = null;
  m = ui.modal({
    title: '停用账号',
    confirmText: '确认停用',
    variant: 'warn',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('账号', ui.h('span', { class: 'mono', text: String(u.username || '') })),
        kv('姓名', u.real_name || NA),
        kv('角色', u.role_label || labelOf('role', u.role, u.role || NA)),
        kv('状态变更', `${u.status_label || labelOf('sys_user_status', u.status, u.status)} → 停用`),
        kv('名下待处置案件', u.pending_case_cnt === undefined ? NA : String(u.pending_case_cnt)),
      ]),
      transferRow,
      ui.h('div', { class: 'impact-note', text:
        '停用后该账号无法登录；名下有未处置案件时需先转交给其他审核员（BR-13-15）。'
        + '服务端会拒绝停用**当前登录账号**（SYS-4005）或**最后一个可用管理员**（SYS-4006）——'
        + '若命中，本次操作会失败并给出原因，账号状态保持不变。' }),
      errBox,
    ],
    onConfirm: async () => {
      hideInline(errBox);
      if (m) m.confirmBtn.disabled = true;
      const body = {};
      if (transferS.value) body.transfer_to = transferS.value;
      try {
        await postSilent(`${userUrl(u.username)}/disable`, body);
        ui.toast(`已停用账号：${u.username}`, 'ok');
      } catch (e) {
        let msg = errText(e);
        const cases = e && e.data && (e.data.pending_cases || e.data.cases);
        if (e && e.code === 'SYS-4007' && Array.isArray(cases) && cases.length) {
          msg += ` 待处置案件（前 ${Math.min(cases.length, 5)} 个）：`
            + cases.slice(0, 5).map((c) => (c && (c.case_no || c.no)) || String(c)).join('、')
            + '。请在上方选择接收人后重试。';
        }
        showInline(errBox, msg);
        if (m) m.confirmBtn.disabled = false;
        await loadUsers();          // 失败也要回读：确认服务端状态确实没变
        return false;
      }
      await loadUsers();
      return true;
    },
  });
}

/** 启用不做二次确认（非破坏性动作，Spec 只要求删除/停用/改角色/改密确认）。 */
async function doEnable(u) {
  hideInline(refs.usErr);
  try {
    await postSilent(`${userUrl(u.username)}/enable`, {});
    ui.toast(`已启用账号：${u.username}`, 'ok');
  } catch (e) {
    showInline(refs.usErr, `启用失败：${errText(e)}（trace ${traceOf(e)}）`);
  }
  await loadUsers();
}

function openDeleteConfirm(u) {
  const errBox = ui.h('div', { class: 'form-error' });
  let m = null;
  m = ui.modal({
    title: '删除账号',
    confirmText: '确认删除',
    variant: 'danger',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('账号', ui.h('span', { class: 'mono', text: String(u.username || '') })),
        kv('姓名', u.real_name || NA),
        kv('角色', labelOf('role', u.role, u.role || NA)),
        kv('状态', labelOf('sys_user_status', u.status, u.status || NA)),
        kv('名下待处置案件', u.pending_case_cnt === undefined ? NA : String(u.pending_case_cnt)),
      ]),
      ui.h('div', { class: 'impact-note', text:
        '删除为**软删除**（status=deleted，BR-13-17）：该账号不再能登录，历史审计与处置记录照旧可追溯。'
        + '仅"已停用且无待处置案件"的账号可删除；服务端还会拒绝删除自己或最后一个可用管理员'
        + '（SYS-4005/4006）。' }),
      errBox,
    ],
    onConfirm: async () => {
      hideInline(errBox);
      if (m) m.confirmBtn.disabled = true;
      try {
        await delSilent(userUrl(u.username));
        ui.toast(`已删除账号：${u.username}（软删除）`, 'ok');
        selected.delete(u.username);
      } catch (e) {
        showInline(errBox, errText(e));
        if (m) m.confirmBtn.disabled = false;
        await loadUsers();
        return false;
      }
      await loadUsers();
      return true;
    },
  });
}

function openBatchDisable() {
  const names = Array.from(selected);
  if (!names.length) { ui.toast('请先勾选要停用的账号', 'warn'); return; }
  const errBox = ui.h('div', { class: 'form-error' });
  const out = ui.h('div', { class: 'settings-msg' });
  let m = null;
  m = ui.modal({
    title: '批量停用账号',
    confirmText: `确认停用 ${names.length} 个`,
    variant: 'warn',
    body: [
      ui.h('div', { class: 'mono settings-pw', text: names.join('、') }),
      ui.h('div', { class: 'impact-note', text:
        '逐个调用停用接口，结果逐条如实反馈：被服务端拒绝的（自己 / 最后一个管理员 / 有待处置案件）'
        + '保持原状态，不会静默跳过。' }),
      errBox,
      out,
    ],
    onConfirm: async () => {
      hideInline(errBox);
      if (m) m.confirmBtn.disabled = true;
      const fails = [];
      let done = 0;
      for (const n of names) {
        try {
          await postSilent(`${userUrl(n)}/disable`, {});
          done += 1;
        } catch (e) {
          fails.push(`${n}：${errText(e)}`);
        }
      }
      selected.clear();
      ui.mount(out, [
        ui.h('div', { class: 'settings-line ok', text: `已停用 ${done} / ${names.length} 个账号` }),
        fails.length ? ui.h('div', { class: 'settings-line warn', text: `失败 ${fails.length} 个：${fails.join('；')}` }) : null,
      ]);
      ui.toast(`批量停用完成：成功 ${done}，失败 ${fails.length}`, fails.length ? 'warn' : 'ok', 5000);
      await loadUsers();
      if (fails.length) { showInline(errBox, '有账号未被停用（原因见上方），列表已按服务端回读刷新。'); }
      return true;
    },
  });
}

// ============================================================
// 卡片四 · 决策引擎配置（模型引擎为预留）`mdCard`
// ============================================================
function renderEngineCard() {
  refs.mdWarn = ui.h('div', { class: 'banner warn', dataset: { role: 'md-warn' } });
  refs.mdBody = ui.h('div', { class: 'form-grid', dataset: { role: 'md-form' } });
  refs.mdStatus = ui.h('div', { class: 'kv-list', dataset: { role: 'md-status' } });
  refs.mdErr = ui.h('div', { class: 'form-error' });
  refs.mdSave = ui.button('保存引擎配置', { variant: 'primary', onClick: () => doSaveEngine() });
  const el = ui.card('决策引擎配置（模型引擎为预留）', [
    refs.mdWarn,
    refs.mdBody,
    refs.mdStatus,
    refs.mdErr,
    ui.h('div', { class: 'form-actions' }, [refs.mdSave]),
    ui.h('div', { class: 'hint', text:
      'BR-13-23 要求页面**显著标注**模型引擎未启用及原因；BR-13-24 要求 engine_type=rule 时融合权重只读。'
      + '本卡片不做"可开关但无效果"的假开关：模型引擎的两个取值直接置灰，尝试提交会被服务端以 SYS-4003 拒绝。' }),
  ]);
  el.dataset.role = 'md-card';
  return el;
}

function paintEngine() {
  if (!refs || !refs.mdWarn) return;
  const cfg = data.engine || {};
  const hl = data.health || {};
  const comps = hl.components || {};
  const modelOk = cfg.model_available === true;

  refs.mdWarn.textContent = '⚠ 模型引擎（ModelEngine）当前为空实现，恒返回 None，final_score = rule_score。'
    + '是否实现、用什么算法属悬空点 G-01，需与老师确认后填写。本页仅做架构预留展示。';

  // 引擎模式：仅 `rule` 可选，`model` / `hybrid` 置灰（BR-13-22 绝不静默忽略或假装成功）
  const engSel = ui.h('select', { dataset: { role: 'md-engine' } });
  const types = optionsOf('engine_type');
  (types.length ? types : [{ value: 'rule', label: '规则引擎' }]).forEach((o) => {
    const opt = ui.h('option', { value: o.value, text: o.label });
    if (String(o.value) !== 'rule') {
      opt.disabled = true;
      opt.dataset.unavailable = 'G-01';   // 机器可读：该取值尚未实现
    }
    engSel.appendChild(opt);
  });
  engSel.value = cfg.engine_type || 'rule';

  const fuseSel = ui.h('select', { dataset: { role: 'md-fuse' } });
  const fuses = optionsOf('fuse_mode');
  (fuses.length ? fuses : [{ value: 'rule_first', label: '规则优先' }]).forEach((o) => {
    fuseSel.appendChild(ui.h('option', { value: o.value, text: o.label }));
  });
  fuseSel.value = cfg.fuse_mode || 'rule_first';

  const ruleW = ui.h('input', { type: 'text', readonly: 'readonly',
    value: isNum(cfg.rule_weight) ? fixed(cfg.rule_weight, 1) : '1.0' });
  const modelW = ui.h('input', { type: 'text', readonly: 'readonly',
    value: isNum(cfg.model_weight) ? fixed(cfg.model_weight, 1) : '0.0' });

  ui.mount(refs.mdBody, [
    fieldCell('引擎模式', engSel,
      'model / hybrid 尚未实现（G-01），已置灰；提交会被服务端以 SYS-4003 明确拒绝（BR-13-22）', true),
    fieldCell('融合方式', fuseSel,
      cfg.fuse_mode_effective === true
        ? '当前融合方式参与打分'
        : '服务端回传 fuse_mode_effective=false：model_weight=0.0 时融合方式不改变 final_score（等于规则分），仅作架构预留保存'),
    fieldCell('融合权重 rule / model（只读）', ui.h('div', { class: 'form-actions' }, [ruleW, modelW]),
      '只读：engine_type=rule 时固定 1.0 / 0.0，不可编辑（BR-13-24）', false, 1),
  ]);

  ui.mount(refs.mdStatus, [
    kv('引擎模式', `${labelOf('engine_type', cfg.engine_type || 'rule', cfg.engine_type || 'rule')}`
      + `${cfg.engine_type === 'rule' ? '（当前唯一可用）' : ''}`
      + `${cfg.engine_version ? ` · ${cfg.engine_version}` : ''}`),
    kv('模型引擎', ui.h('span', { class: 'tag medium', dataset: { role: 'md-model-status' },
      text: '未接入（架构预留，恒返回 None）' })),
    kv('装配实现', ui.h('span', {}, [
      ui.h('span', { class: 'mono',
        text: `${cfg.model_engine || comps.model_engine || 'NullModelEngine'} · 决策 provider ${comps.decision_provider || NA}` }),
      // 拼字符串会把 DOM 节点变成 `[object HTMLSpanElement]`（这里踩过一次）——
      // 节点必须作为**子节点**挂进去，不能与字符串相加。
      document.createTextNode('（model_engine 取自 engine-config，决策 provider 取自 /health；D45：默认装配体现真实组件）'),
    ])),
    kv('model_available', String(cfg.model_available === undefined ? false : cfg.model_available)),
    kv('融合方式是否生效', String(cfg.fuse_mode_effective === undefined ? false : cfg.fuse_mode_effective)),
    kv('配置说明', cfg.note || 'model_configs 为架构预留，当前只有一条默认记录（BR-13-21）'),
  ]);

  // 模型引擎实测不可用时给出「未接入」的显式证据（不依赖某个具体类名）
  if (!modelOk) {
    refs.mdStatus.appendChild(kv('结论', ui.h('span', { class: 'tag medium', text:
      `模型引擎未接入（悬空点 ${cfg.reference || 'G-01'}）：model_score 恒为 null，`
      + '本页不提供任何"打开模型"的开关（AD-09）' })));
  }
}

async function loadEngine() {
  const c = track();
  try {
    const d = await api.request(`${BASE}/engine-config`, { signal: c.signal, silent: true });
    data.engine = unwrap(d);
    data.engineErr = null;
    hideInline(refs.mdErr);
    refs.mdSave.disabled = false;
    paintEngine();
  } catch (e) {
    if (e.name === 'AbortError') return;
    data.engine = {};
    data.engineErr = e;
    hideInline(refs.mdErr);
    showInline(refs.mdErr, `引擎配置读取失败：${errText(e)}（trace ${traceOf(e)}）。`
      + '为避免误写，保存已禁用。');
    refs.mdSave.disabled = true;
    paintEngine();
  } finally {
    inflight.delete(c);
  }
}

async function doSaveEngine() {
  if (data.engineErr) { ui.toast('引擎配置尚未成功读取，保存已禁用', 'warn'); return; }
  const eng = refs.mdBody.querySelector('[data-role="md-engine"]');
  const fuse = refs.mdBody.querySelector('[data-role="md-fuse"]');
  const body = { engine_type: eng ? eng.value : 'rule' };
  if (fuse) body.fuse_mode = fuse.value;
  hideInline(refs.mdErr);
  ui.buttonBusy(refs.mdSave, true, '保存中');
  try {
    await putSilent(`${BASE}/engine-config`, body);
    ui.toast('引擎配置已保存（模型引擎仍未启用）', 'ok');
    await loadEngine();     // 回读服务端现值为准（不做本地推测）
  } catch (e) {
    showInline(refs.mdErr, `保存失败：${errText(e)}（trace ${traceOf(e)}）`);
  } finally {
    ui.buttonBusy(refs.mdSave, false);
    refs.mdSave.disabled = !!data.engineErr;
  }
}

// ============================================================
// 组装
// ============================================================
export async function render(container) {
  dispose();
  mounted = true;
  // 下拉选项一律来自服务端枚举（BR-00-18：前端不硬编码"风控审核员"这类标签）
  await ensureEnums().catch(() => ({}));

  refs = {};
  const rcCard = renderConfigCard();
  const hcCard = renderHealthCard();
  const usCard = renderUsersCard();
  const mdCard = renderEngineCard();
  refs.hcCard = hcCard;
  refs.usCard = usCard;

  ui.mount(container, ui.h('div', { class: 'view', dataset: { role: 'settings-page' } }, [
    rcCard,
    hcCard,
    usCard,
    mdCard,
    ui.h('div', { class: 'hint', text:
      '本页每个卡片各自取数、各自失败：某张卡片的后端接口未就绪时，该卡片显示明确的原因，'
      + '其余卡片照常可用（与工作台的"分区降级"同一约定）。' }),
  ]));

  document.addEventListener('visibilitychange', onVisibility);

  // 四张卡片并行取数；任一失败都在各自卡片内呈现，绝不冒泡成"页面加载失败"
  await Promise.all([loadConfig(), loadHealth(), loadUsers(), loadEngine()]);
  restartHcTimer();
}
