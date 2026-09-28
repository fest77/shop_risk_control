/* 判定链路单步回溯组件（模块 10 §2.3 / §6 的 `static/js/chain.js`）
 *
 * ## 这个文件负责的四件"容易做错"的事
 *
 * 1. **五步**必须齐全（BR-10-10）：`event_validate` / `feature_extract` /
 *    `list_filter` / `rule_evaluate` / `arbitrate`。后端少给一步时**不静默跳过**——
 *    缺的那块画成"未执行（接口未返回该步骤）"，宁可露出契约缺口也不假装链路完整。
 * 2. **每步耗时**只来自接口的 `steps[].elapsed_ms`。没有就显示 `—`，
 *    **绝不编一个数字**：链路耗时是规则上线决策的依据，编出来的时间比没有更糟。
 * 3. **逐块点亮**（BR-10-14）：结果到达后按顺序一块一块地点亮，不是一次性全渲染
 *    完成态。这是"单步呈现"这个需求的全部含义。
 * 4. **失败显式标红且后续不执行**（BR-10-15）：失败步骤标红 + 显示原因，
 *    其后的步骤一律标成"未执行"，不跳过不隐藏。
 *
 * 组件本身**不取数**：只接收 `/sim/run`、`/sim/runs/{id}` 的 `data`（两者同形）。
 */
import * as ui from '../ui.js';
import { labelOf } from '../store.js';
import { featureMetaReady } from './cond_tree.js';

/** 五个步骤的名称与标题（`steps[].name` 取值由 Spec §3.3 固定）。 */
export const STEP_SPEC = [
  { seq: 1, name: 'event_validate', title: '事件校验' },
  { seq: 2, name: 'feature_extract', title: '特征提取（18 项）' },
  { seq: 3, name: 'list_filter', title: '名单快速过滤' },
  { seq: 4, name: 'rule_evaluate', title: '规则命中链路' },
  { seq: 5, name: 'arbitrate', title: '最终决策' },
];

/** 逐块点亮的间隔（毫秒）。五步 ≈ 1 秒，既有"单步呈现"的观感，又不拖慢验收。 */
const STEP_MS = 190;

/** 步骤 2 特征小表的优先项（原型 `s2tbl` 的四项 + 两项常见计数）。 */
const KEY_FEATURES = [
  'device_user_cnt', 'coupon_cnt_1h', 'user_age_days', 'ip_is_proxy',
  'device_order_cnt_1h', 'after_sale_rate_24h', 'user_order_cnt_24h',
];

const STATUS_TEXT = { ok: '通过', failed: '失败', skipped: '未执行', pending: '未执行' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fmtMs(ms) {
  // 接口没给耗时 -> `—`（**不编时间**）。0 是合法耗时，必须与"缺数据"区分开。
  return (ms === null || ms === undefined || ms === '') ? '—' : `${ms}ms`;
}

function statusText(st) {
  return STATUS_TEXT[st] || '未执行';
}

function tagKind(st, decision) {
  if (st === 'failed') return 'high';
  if (st === 'warn') return 'medium';
  if (st === 'skipped' || st === 'pending') return '';
  if (decision) return { pass: 'low', review: 'medium', reject: 'high' }[decision] || '';
  return 'low';
}

function valueText(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** 特征中文标签来自 04 的 `GET /features/meta`（BR-04-16：前端不硬编码）。 */
function featureLabel(key) {
  const items = featureMetaReady() || [];
  const hit = items.find((f) => String(f.key) === String(key));
  return hit ? `${hit.label || key}` : key;
}

/** 名单类型的中文标签同样来自服务端枚举（BR-00-18）。 */
function labelOfList(value) {
  return labelOf('list_type', value, value || '名单');
}

/** 组装一个步骤的 DOM。`state` 决定配色，`content` 决定正文。 */
function stepNode(spec, state) {
  const el = ui.h('div', { class: 'chain-step', dataset: { step: spec.name, state } }, [
    ui.h('div', { class: 'chain-head' }, [
      ui.h('span', { class: 'chain-badge', text: String(spec.seq) }),
      ui.h('span', { class: 'chain-name', text: spec.title }),
      ui.h('span', { class: 'chain-status' }),
    ]),
    ui.h('div', { class: 'chain-concl' }),
    ui.h('div', { class: 'chain-extra' }),
  ]);
  return el;
}

function setState(el, state) {
  el.dataset.state = state;
  return el;
}

export function createChain({ onOpenRun } = {}) {
  const steps = STEP_SPEC.map((spec) => ({ spec, el: stepNode(spec, 'pending') }));
  const el = ui.h('div', { class: 'chain', dataset: { state: 'idle' } },
    steps.map((s) => s.el));
  let token = 0;          // 每次新执行/新载入自增，作废在途的逐块点亮
  let lastData = null;

  const byName = (name) => steps.find((s) => s.spec.name === name);

  function idle(activeName, text = '点击「执行检测」开始') {
    steps.forEach((s) => {
      setState(s.el, s.spec.name === activeName ? 'active' : 'pending');
      s.el.querySelector('.chain-status').className = 'chain-status';
      s.el.querySelector('.chain-status').textContent = '';
      s.el.querySelector('.chain-concl').textContent = text;
      s.el.querySelector('.chain-concl').classList.add('is-idle');
      ui.clear(s.el.querySelector('.chain-extra'));
    });
    el.dataset.state = 'idle';
  }

  /** 未执行态：链路中止后，后面的步骤必须明确写"未执行"而不是留白（BR-10-15）。 */
  function notRun(node, reason) {
    setState(node.el, 'skipped');
    node.el.querySelector('.chain-status').className = 'chain-status';
    node.el.querySelector('.chain-status').textContent = '';
    const c = node.el.querySelector('.chain-concl');
    c.classList.remove('is-idle');
    c.textContent = reason || '未执行';
    ui.clear(node.el.querySelector('.chain-extra'));
  }

  function setStatus(node, st, decision, textOverride) {
    const t = node.el.querySelector('.chain-status');
    t.className = `chain-status tag ${tagKind(st, decision)}`.trim();
    t.textContent = textOverride || statusText(st);
  }

  // ---------------------------------------------------------------- 步骤正文
  /**
   * 结论摘要由接口的 `steps[].detail` 提供（BR-10-10），而原型 `s1r`/`s5r` 把
   * 「耗时 / score / level / decision / 总耗时」也写进了同一行。因此这里先把这些
   * **结构化字段**从 detail 里摘掉，再由本组件按**规范顺序**统一拼回——
   * 否则会出现 `… · 耗时 0ms · 耗时 0ms` 这种重复（实测后端 detail 已含耗时）。
   */
  function stripStructured(text, isArbitrate) {
    let s = String(text || '');
    s = s.replace(/[\s·]*总耗时\s*[^\s·]*/g, '');
    s = s.replace(/[\s·]*耗时\s*[^\s·]*/g, '');
    s = s.replace(/[\s·]*缺失\s*\d+\s*项/g, '');
    if (isArbitrate) s = s.replace(/[\s·]*(score|level|decision)=[^\s·]*/gi, '');
    return s.replace(/(\s*·\s*)+/g, ' · ').replace(/^\s*·\s*|\s*·\s*$/g, '').trim();
  }

  function renderStep(node, data, info) {
    const concl = node.el.querySelector('.chain-concl');
    const extra = node.el.querySelector('.chain-extra');
    concl.classList.remove('is-idle');
    ui.clear(extra);

    const isArb = node.spec.name === 'arbitrate';
    const core = stripStructured(info.detail, isArb);
    const ms = fmtMs(info.elapsed_ms);
    const parts = [];
    // Spec §2.3：步骤 2 的"缺失 > 0"是**橙色**（特征不完整），不是红色（链路失败）
    const missing = (data && Array.isArray(data.missing_features)) ? data.missing_features.length : 0;
    const st = (info.status === 'ok' && node.spec.name === 'feature_extract' && missing > 0)
      ? 'warn' : info.status;
    setState(node.el, st);

    if (st === 'failed') {
      // 失败必须显式标红并给原因（BR-10-15）。接口的 `detail` 是**权威摘要**，
      // 这里原样引用，另补一句本组件的失败判定——绝不把接口摘要改写成我们自己的话。
      setStatus(node, 'failed');
    } else if (st === 'skipped') {
      setStatus(node, 'skipped');
    } else if (st === 'warn') {
      setStatus(node, 'warn', null, '部分缺失');
    } else if (isArb && data && data.decision) {
      // 步骤 5 的徽标直接是决策动作，配色按 pass/review/reject 三档（BR-10-16）
      const d = String(data.decision);
      setStatus(node, 'ok', d, d.toUpperCase());
    } else {
      setStatus(node, 'ok');
    }

    if (core) parts.push(core);
    if (st === 'failed') parts.push(core ? '结论：失败' : '结论：失败（接口未给出原因）');
    else if (st === 'skipped' && !core) parts.push('未执行');

    // 步骤 1：事件编号（原型 `s1r` 的形态）
    if (node.spec.name === 'event_validate' && data && data.event_id) {
      parts.push(`event_id=${data.event_id}`);
    }
    // 步骤 2：缺失项数量（原型 `s2r` 的形态）
    if (node.spec.name === 'feature_extract' && Array.isArray(data && data.missing_features)) {
      parts.push(`缺失 ${data.missing_features.length} 项`);
    }
    // 步骤 5：最终决策三要素（原型 `s5r` 的形态）
    if (isArb && st !== 'failed' && data) {
      parts.push(`score=${valueText(data.rule_score)}`);
      parts.push(`level=${valueText(data.risk_level)}`);
      parts.push(`decision=${String(data.decision || '').toUpperCase() || '—'}`);
    }
    // **每步耗时**：始终出现在结论行里（接口没给就显示 `—`，绝不编时间）
    parts.push(`耗时 ${ms}`);
    if (isArb && data && data.elapsed_ms !== undefined) {
      parts.push(`总耗时 ${fmtMs(data.elapsed_ms)}`);
    }
    concl.textContent = parts.join(' · ');

    if (node.spec.name === 'feature_extract') renderFeatureTable(extra, data);
    if (isArb) renderResultTags(extra, data);
    if (node.spec.name === 'rule_evaluate') renderHitsTable(extra, data, info);
    if (node.spec.name === 'list_filter') renderListHit(extra, data);
  }

  /** 步骤 2 附：4~6 项关键特征 KV 小表。 */
  function renderFeatureTable(host, data) {
    const features = (data && data.features) || null;
    if (!features || typeof features !== 'object') return;
    const missing = new Set((data.missing_features || []).map(String));
    const keys = [];
    KEY_FEATURES.forEach((k) => { if (k in features && keys.length < 6) keys.push(k); });
    Object.keys(features).forEach((k) => { if (keys.length < 4 && !keys.includes(k)) keys.push(k); });
    if (!keys.length) return;
    host.appendChild(ui.h('div', { class: 'chain-sub', text: '关键特征（本次取值，标签来自 04 的 /features/meta）' }));
    const rows = keys.map((k) => ui.h('tr', {}, [
      ui.h('td', {}, [
        ui.h('div', { class: 'chain-feat-label', text: featureLabel(k) }),
        ui.h('div', { class: 'mono chain-feat-key' }, [
          document.createTextNode(k),
          missing.has(k) ? ui.h('span', { class: 'chain-miss', text: ' 缺失' }) : null,
        ]),
      ]),
      ui.h('td', { class: 'mono' }, valueText(features[k])),
    ]));
    host.appendChild(ui.h('table', { class: 'chain-tbl' }, [
      ui.h('thead', {}, ui.h('tr', {}, [
        ui.h('th', { text: '特征名（04 的 18 项）' }), ui.h('th', { text: '本次值' }),
      ])),
      ui.h('tbody', {}, rows),
    ]));
  }

  /** 步骤 3 附：名单直通结果（`list_hit` 是对象，D56）。命中白名单=绿、黑名单=红、灰名单=橙。 */
  function renderListHit(host, data) {
    const hit = (data && data.list_hit) || null;
    if (!hit || !hit.hit) return;
    const kind = { white: 'low', black: 'high', gray: 'medium' }[String(hit.list_type || '')] || '';
    host.appendChild(ui.h('div', { class: 'chain-note' }, [
      ui.h('span', { class: 'hint', text: '名单直通：' }),
      ui.tag(labelOfList(hit.list_type), kind),
      ui.h('span', { class: 'mono', text: ` ${hit.entity_type || ''} ${hit.entity_value || ''}` }),
      ui.h('span', { class: 'hint', text: '（命中名单时步骤 4 状态为 skipped，BR-10-11）' }),
    ]));
  }

  /** 步骤 4 附：命中规则明细表（规则 / 分值 / 原因）。 */
  function renderHitsTable(host, data, info) {
    const hits = Array.isArray(info.hits) ? info.hits : ((data && data.hits) || []);
    if (!hits.length) return;
    host.appendChild(ui.h('div', { class: 'chain-sub', text: `命中 ${hits.length} 条（明细）` }));
    const rows = hits.map((h) => ui.h('tr', {}, [
      ui.h('td', {}, [
        ui.h('div', { class: 'mono', text: String(h.rule_code || '—') }),
        h.rule_name ? ui.h('div', { class: 'chain-hit-name', text: String(h.rule_name) }) : null,
      ]),
      ui.h('td', { class: 'chain-hit-score' }, `+${Number(h.score) || 0}`),
      ui.h('td', { class: 'chain-hit-reason' }, String(h.reason || '—')),
    ]));
    host.appendChild(ui.h('table', { class: 'chain-tbl' }, [
      ui.h('thead', {}, ui.h('tr', {}, [
        ui.h('th', { text: '规则' }), ui.h('th', { text: '分值' }), ui.h('th', { text: '原因' }),
      ])),
      ui.h('tbody', {}, rows),
    ]));
  }

  /** 步骤 5 附：是否符合预期 + dry_run 声明 + 记录链接。 */
  function renderResultTags(host, data) {
    if (!data) return;
    const nodes = [];
    if (data.matched_expected === true) nodes.push(ui.tag('符合预期', 'low'));
    else if (data.matched_expected === false) {
      nodes.push(ui.tag(`与预期不符（预期 ${data.expected_decision || '—'}）`, 'medium'));
    }
    if (data.dry_run === true) {
      const t = ui.tag('dry_run=true', 'plain');
      t.title = '仿真只复用真实判定链路，不落业务库、不写真实特征窗口（BR-10-05/06/09）';
      nodes.push(t);
    }
    if (data.run_id) {
      nodes.push(ui.h('span', { class: 'mono chain-runid', text: `run_id=${data.run_id}` }));
      if (onOpenRun) {
        const b = ui.button('按 run_id 回看', { variant: 'ghost', onClick: () => onOpenRun(data.run_id) });
        b.classList.add('btn-sm');
        nodes.push(b);
      }
    }
    if (nodes.length) host.appendChild(ui.h('div', { class: 'chain-tags' }, nodes));
  }

  // ---------------------------------------------------------------- 归一化
  /**
   * 把接口返回的 `steps[]` 归一到固定的五步。
   * 缺步骤时**如实标注**（"未执行（接口未返回该步骤）"），不静默跳过。
   */
  function normalize(data) {
    const raw = Array.isArray(data && data.steps) ? data.steps : [];
    const named = new Map();
    raw.forEach((s) => { if (s && s.name) named.set(String(s.name), s); });
    const failAt = raw.findIndex((s) => s && s.status === 'failed');
    return STEP_SPEC.map((spec, i) => {
      const s = named.get(spec.name);
      if (s) {
        return {
          present: true,
          status: String(s.status || 'ok'),
          detail: s.detail,
          elapsed_ms: s.elapsed_ms,
          hits: s.payload && s.payload.hits,
        };
      }
      // 接口没给这一块：只对**确实缺失**的步骤写"未执行"，
      // 绝不覆盖接口已经给出的后续步骤（步骤 2 降级为 failed 时，
      // 3/4/5 在真实链路里仍然跑了，把它们改成"未执行"就是伪造链路）。
      const aborted = failAt >= 0 && i > failAt;
      return {
        present: false,
        status: 'skipped',
        detail: aborted ? '未执行（前序步骤失败，链路已中止）' : '未执行（接口未返回该步骤）',
        elapsed_ms: null,
      };
    });
  }

  return {
    el,
    /** 空闲骨架（Spec §2.3：未执行时五块灰色 + 「点击"执行检测"开始」）。 */
    reset() {
      token += 1;
      lastData = null;
      idle(null);
    },
    /** 正在执行：高亮到第 1 块，其余仍是骨架（执行中态）。 */
    start() {
      token += 1;
      idle('event_validate', '执行中…');
      el.dataset.state = 'running';
    },
    /**
     * 展示一次执行结果：**先整块回到骨架，再按顺序逐块点亮**（BR-10-14）。
     * 返回值在全部点亮后 resolve，便于调用方接线（也可不 await）。
     */
    async show(data) {
      const my = ++token;
      lastData = data;
      const norm = normalize(data);
      idle(null);
      el.dataset.state = 'running';
      for (let i = 0; i < steps.length; i += 1) {
        if (my !== token) return;
        setState(steps[i].el, 'active');
        await sleep(STEP_MS);
        if (my !== token) return;
        renderStep(steps[i], data, norm[i]);
        if (!norm[i].present) {
          notRun(steps[i], norm[i].detail);
        }
      }
      if (my !== token) return;
      el.dataset.state = 'done';
    },
    /**
     * 整体失败（SIM-4001 之外：网络 / 503 / 504 等）。
     * `failedStep` 指定要标红的那一块（步骤 1 → 事件校验），其余标"未执行"。
     */
    async fail(message, { failedStep = 'event_validate' } = {}) {
      const my = ++token;
      idle(null);
      el.dataset.state = 'running';
      for (let i = 0; i < steps.length; i += 1) {
        if (my !== token) return;
        const node = steps[i];
        const isFail = node.spec.name === failedStep;
        setState(node.el, 'active');
        await sleep(STEP_MS);
        if (my !== token) return;
        if (isFail) {
          setState(node.el, 'failed');
          node.el.querySelector('.chain-status').className = 'chain-status tag high';
          node.el.querySelector('.chain-status').textContent = '失败';
          const c = node.el.querySelector('.chain-concl');
          c.classList.remove('is-idle');
          c.textContent = `失败：${message}`;
        } else {
          notRun(node, '未执行（链路已中止）');
        }
      }
      if (my !== token) return;
      el.dataset.state = 'error';
    },
    /** 最近一次展示的数据（供页面做后续联动）。 */
    last() { return lastData; },
  };
}
