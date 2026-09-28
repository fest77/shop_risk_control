/* 模块 08 · 案件处置与业务联动 —— **前端处置流程组件**（不是页面）
 *
 * ## 这个文件是什么 / 不是什么
 *
 * 它是 Spec 07 §1.1 定的那条边界的**下游那一半**：
 *   · **07 的右栏处置区**只收集「结论 + 动作 + 备注」，**不直接调处置执行接口**
 *     （BR-07-21）；它把表单数据交给本文件；
 *   · **二次确认弹窗由 08 渲染**（Spec 07 §1.1 明文），因为弹窗的内容必须来自
 *     服务端权威生成的 `/dispose/preview` —— 前端自己拼一份副作用清单，
 *     漏列一项就是一个安全漏洞（BR-08-26）。
 *
 * 因此本文件**不往 `#/cases` 里塞任何东西**（那页有 09/05 的大量已验证断言，
 * 且案件列表与处置区归 07）。它只导出函数，07 交付时直接调用即可：
 *
 * | 导出 | 用途 | 谁调 |
 * |---|---|---|
 * | `openDisposeFlow(caseNo, formData, opts)` | **核心**：preview → 弹窗 → 确认 → dispose → 结果反馈 | 07 的 `btnSubmit` |
 * | `openClaimConfirm(caseNo, opts)` / `claimCase(caseNo)` | 认领（BR-08-04 原子认领） | 07 的「认领」按钮 |
 * | `openArchiveConfirm(caseNo, opts)` / `archiveCase(caseNo, remark)` | 归档（仅 admin） | 07 / 13 |
 * | `retryBizSync(caseNo, actionId, opts)` | 业务联动重试（BR-08-31） | 结果反馈条上的「重试同步」 |
 * | `loadActions(caseNo)` / `renderActions(host, data)` | 处置流水（按 `acted_at` 升序） | 07 的详情页 |
 * | `disposeFeedbackNode(result)` / `renderDisposeFeedback(host, result)` | 处置结果反馈（成功 / DSP-5003 / DSP-5004） | 07 的结果位 |
 * | `incompatibleReason(conclusion, actions)` | 结论×动作相容矩阵（BR-08-14），供 07 动态禁用复选框 | 07 的右栏 |
 * | `disposeErrorText(e)` | 错误码 → 人话（Spec §5 全表） | 任何调用方 |
 *
 * ## 三条不能让步的语义（都在这里落地）
 *
 * 1. **取消 = 什么都不发生**（BR-08-28）：`/dispose/preview` 无副作用，弹窗上点
 *    「取消」只关窗，**一个执行接口都不调**。取消路径上连 preview 之外没有第二个请求。
 * 2. **失败不关弹窗、不丢输入**（照 `rule_editor.js` 的做法）：`dispose` 失败时
 *    把服务端文案显示在弹窗内并保持打开；`DSP-4006`（令牌过期/参数变了）额外
 *    禁用确认按钮并给一个「重新确认」按钮重新签发令牌，而不是把用户赶回第一步。
 * 3. **不许假装成功**（本模块硬要求）：
 *    · 只有 `POST /dispose` 返回 `200` 且 `status="disposed"` 才显示「已处置」；
 *    · `DSP-5002`（名单写入失败，紧操作 fail-closed）显示为**未生效**，绝不显示已处置；
 *    · `DSP-5003/5004`（旁路降级）显示为「已生效 + 待重试」，并且**带重试入口**；
 *    · `biz-sync/retry` 在没有待重试项时如实说「没有待重试的业务联动」，
 *      **不把它渲染成"同步成功"**（`attempt_no === 0` 与"重试后成功"是两件事）。
 *
 * ## 关于「模拟同步」（AD-08 / G-08）
 *
 * 默认适配器是 `MockBizAdapter`，它只写日志、**返回值不具权威性**。Spec §3.3 要求
 * 页面标注这件事，所以弹窗与结果反馈里凡出现业务联动结论处都带「模拟同步」字样。
 * 这是**如实标注**，不是免责声明式的装饰——它直接告诉审核员"这一栏不能当作已对接"。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';

const CASE_URL = (caseNo) => `${api.API_PREFIX}/cases/${encodeURIComponent(String(caseNo))}`;

/** 处理结论取值域（BR-08-13；标签一律取自服务端 `/common/enums`，此处只留取值）。 */
export const CONCLUSIONS = ['violation', 'normal', 'suspicious'];

/** 联动处置动作取值域（E09.`action_type`）。 */
export const ACTION_TYPES = ['pass', 'block_order', 'blacklist_user', 'ban_device', 'reject_refund'];

/** **紧动作**（BR-08-30 fail-closed 的对象）：它们的名单写入失败必须整体回滚。 */
export const TIGHT_ACTIONS = ['block_order', 'blacklist_user', 'ban_device', 'reject_refund'];

/** `remark` 上限（BR-08-16）。 */
export const REMARK_MAX = 500;

/** 弹窗底部警示行的**原型原文**（Spec §2.1：必须保留）。
 *  导出给模块 07 的处置区复用（Spec 07 §2.4.3 的"固定注记"是同一句）——
 *  两处各写一份字面量必然会漂移，而"必须保留原文"的东西一旦漂移就没人发现。 */
export const WARN_LINE = '⚠ 提交后将同步写入名单库与审计哈希链，不可撤销；执行前需二次确认。';

/** 副作用清单的兜底文案（服务端已逐项下发 `detail`；这里只在服务端没给时用）。 */
const SIDE_EFFECT_FALLBACK = {
  case_action: '写入案件处置流水 case_actions',
  list_entry: '写入名单库 list_entries',
  biz_sync: '调用业务系统适配器 BizAdapter',
  audit_log: '写入审计哈希链 audit_logs',
  case_status: '更新案件状态为 disposed',
  skip: '本次跳过某项默认映射',
};

// ============================================================
// 纯逻辑（与 `app/schemas/case_schema.py` 的矩阵同源，供 07 动态禁用）
// ============================================================

/** 去重（**保留勾选顺序**；服务端会再排序，令牌对"集合"生效，BR-08-27）。 */
export function normalizeActions(raw) {
  const list = Array.isArray(raw) ? raw : (raw ? [raw] : []);
  const out = [];
  list.forEach((v) => {
    const s = String(v === undefined || v === null ? '' : v).trim();
    if (s && !out.includes(s)) out.push(s);
  });
  return out;
}

/**
 * 结论×动作相容矩阵（BR-08-14）：相容返回 `null`，否则返回**中文原因**。
 *
 * 与 `case_schema.incompatible_reason` 逐字同规则：
 * `violation` → 至少 1 个紧动作；`normal`/`suspicious` → 只允许 `pass`。
 * 它只用于 07 的**动态禁用与原因文案**；服务端会用同一规则复核并给 `DSP-4002`。
 */
export function incompatibleReason(conclusion, actionTypes) {
  const actions = normalizeActions(actionTypes).filter((a) => ACTION_TYPES.includes(a));
  if (!actions.length) return null;   // 空数组属 DSP-4001（"至少选一项"），不是不相容
  if (conclusion === 'violation') {
    if (actions.some((a) => TIGHT_ACTIONS.includes(a))) return null;
    return '结论为『确认违规』时至少需要一项拦截类动作（pass 表示放行，不能作为违规处置的唯一动作）';
  }
  if (conclusion === 'normal' || conclusion === 'suspicious') {
    const bad = actions.filter((a) => a !== 'pass');
    if (bad.length) {
      return `结论为『${conclusion === 'normal' ? '确认为正常' : '存疑待观察'}』时不可勾选拦截类动作：${bad.join('、')}`;
    }
    return null;
  }
  return null;
}

/** 常量错误码 → 人话（Spec §5 的表；服务端文案更具体时优先用服务端的）。 */
const HUMAN_ERROR = {
  'DSP-4001': '请选择处理结论、至少一项处置动作并填写处置原因备注',
  'DSP-4003': '案件当前状态不允许该操作（仅「审核中」且由本人认领的案件可处置）',
  'DSP-4004': '该案件已完成处置',
  'DSP-4006': '确认已过期或参数已变更，请重新确认（确认令牌一次性、30 秒内有效）',
  'DSP-4040': '案件不存在或已归档',
  'DSP-5001': '处置失败：数据库不可用，本次未生效，请重试',
  'DSP-5002': '处置未生效：名单库写入失败，已回滚本次名单变更，案件保持「审核中」',
  'DSP-5005': '处置失败：流水未落库，本次未生效',
  'DSP-5003': '业务系统同步失败（模拟业务系统未对接），处置已生效并已加入重试队列',
  'AUTH-4020': '当前角色无权执行处置（case:dispose 仅审核员，BR-08-36）',
  'AUTH-403': '当前角色无权执行处置（case:dispose 仅审核员，BR-08-36）',
  'NET-0001': '网络不可达，请确认服务是否已启动',
  'COM-5000': '服务返回了非标准响应，请检查服务端日志',
};

/**
 * 错误 → 人话（绝不让用户看到裸 JSON）。
 *
 * `DSP-4001/4002/4003/4004/4005/4040` **优先用服务端 message**：它带着本次的
 * 具体上下文（哪两个动作不相容、案件被谁认领、当前状态是什么），比一句通用文案有用。
 */
export function disposeErrorText(e) {
  const code = String((e && e.code) || '');
  const msg = String((e && e.message) || '').trim();
  const data = (e && e.data) || {};
  if (code === 'DSP-4005') {
    const who = data.assignee ? `由 ${data.assignee} 认领` : '由他人认领';
    return `案件${who}，请先由本人处置或等待超时回收（BR-08-06：admin 也不得代为处置）`;
  }
  if (msg && ['DSP-4001', 'DSP-4002', 'DSP-4003', 'DSP-4004', 'DSP-4040'].includes(code)) return msg;
  if (HUMAN_ERROR[code]) return HUMAN_ERROR[code];
  return msg || '操作失败，请稍后重试';
}

function uuid() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `case-dispose-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isNode(x) { return x instanceof Node; }

function kv(key, value) {
  return ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: key }),
    isNode(value) ? value : ui.h('span', { class: 'v', text: String(value === undefined || value === null || value === '' ? '—' : value) }),
  ]);
}

function listTypeLabel(t) {
  return { black: '黑名单', white: '白名单', gray: '灰名单' }[String(t)] || String(t || '—');
}

function entityTypeLabel(t) {
  return { user: '用户', device: '设备', ip: 'IP', phone: '手机号', address: '收货地址' }[String(t)] || String(t || '—');
}

/** 一条名单写入 → 「黑名单/用户 U000128（复用既有条目）」 */
function writeText(w) {
  if (!w) return '—';
  const reused = w.reused ? '（复用既有条目，BR-08-22）' : '';
  return `${listTypeLabel(w.list_type)} · ${entityTypeLabel(w.entity_type)} ${w.entity_value || ''}${reused}`;
}

/** 业务联动结果 → 如实文案（**永不把 failed 说成成功**）。 */
export function bizSyncText(r) {
  const b = r || {};
  const target = String(b.target || '');
  const tag = target === 'mock' ? '（模拟同步，返回值不具权威性）' : '';
  const status = String(b.status || '');
  const attempt = Number(b.attempt_no || 0);
  const tail = attempt > 0 ? `第 ${attempt} 次` : '';
  if (status === 'ok') {
    return target === 'none'
      ? `没有待重试的业务联动${b.message ? `：${b.message}` : ''}`
      : `同步成功${tail ? `（${tail}）` : ''}${tag}`;
  }
  if (status === 'failed') return `同步失败${tail ? `（${tail}）` : ''}${tag}：${b.message || '原因未返回'}`;
  if (status === 'skipped') return `未调用业务系统：${b.message || '本次无对应动作'}`;
  return status ? `${status}${tag}` : '—';
}

// ============================================================
// ① 核心：处置二次确认弹窗 + 提交流程
// ============================================================

/**
 * 打开处置流程（**本模块的核心导出**）。
 *
 * @param {string} caseNo 案件编号
 * @param {object} formData `{conclusion, action_types[], remark, list_writes?, evidence_refs?, biz_targets?}`
 *   —— 由 07 的右栏处置区收集（Spec 07 §1.1）。
 * @param {object} [opts]
 *   · `idempotencyKey` 复用它做重试（缺省按本次弹窗生成一个，同一次确认重试不会产生二次副作用，BR-08-19）
 *   · `feedbackHost` 结果反馈挂载点（Node）；给了就在处置成功后渲染结果条
 *   · `onDone(result)` 处置成功回调；`onCancel()`；`onError(e)`
 * @returns {Promise<null|{close:Function, modal:Node, done:Promise}>} 打开失败返回 `null`
 */
export async function openDisposeFlow(caseNo, formData = {}, opts = {}) {
  const conclusion = String(formData.conclusion || '').trim();
  const actionTypes = normalizeActions(formData.action_types);
  const remark = formData.remark === undefined || formData.remark === null ? '' : String(formData.remark);

  // 本地短路（Spec 07 §2.1 的提交置灰条件）：省一次注定 400 的请求。
  // **服务端仍会复核**（DSP-4001/4002）——这里只是体验优化，不是校验权威。
  if (!String(caseNo || '').trim()) { ui.toast('缺少案件编号', 'err'); return null; }
  if (!conclusion) { ui.toast('请先选择处理结论（必填）', 'err'); return null; }
  if (!actionTypes.length) { ui.toast('请至少勾选一项联动处置动作（必填）', 'err'); return null; }
  if (!remark.trim()) { ui.toast('请填写处置原因备注（必填）', 'err'); return null; }
  if (remark.trim().length > REMARK_MAX) {
    ui.toast(`处置原因备注最多 ${REMARK_MAX} 字，当前 ${remark.trim().length} 字`, 'err');
    return null;
  }
  // 不相容的结论×动作**不在前端拦死**：交给服务端给 DSP-4002 的权威文案
  // （前端矩阵只用于 07 的复选框动态禁用；这里只提醒，不阻断）。

  // ---- ① preview：内容由服务端权威生成（BR-08-26），且**无任何副作用**（BR-08-28）----
  let preview = null;
  try {
    preview = await api.request(`${CASE_URL(caseNo)}/dispose/preview`, {
      method: 'POST', body: { conclusion, action_types: actionTypes }, silent: true,
    });
  } catch (e) {
    ui.toast(disposeErrorText(e), 'err', 6000);
    if (opts.onError) opts.onError(e);
    return null;
  }
  if (!preview || !preview.confirm_token) {
    ui.toast('处置预览未返回确认令牌，已中止（不执行任何副作用）', 'err', 6000);
    return null;
  }

  const idemKey = opts.idempotencyKey || uuid();
  let closed = false;
  let settled = null;
  let tokenExpired = false;
  let timer = null;

  const errBox = ui.h('div', { class: 'form-error', dataset: { role: 'dispose-error' } });
  const showErr = (msg) => { errBox.textContent = msg; errBox.classList.add('on'); };
  const clearErr = () => { errBox.textContent = ''; errBox.classList.remove('on'); };

  const bodyHolder = ui.h('div', { class: 'dispose-body' });
  const ttlLine = ui.h('div', { class: 'hint', dataset: { role: 'token-ttl' } });

  /** 渲染弹窗主体（重新确认时整块重绘，令牌与清单一起换新）。 */
  function renderBody() {
    const s = preview.summary || {};
    const writes = preview.list_writes_preview || [];
    const effects = preview.side_effects || [];
    const skipped = preview.list_writes_skipped || [];

    ui.mount(bodyHolder, [
      ui.h('div', { class: 'kv-list' }, [
        kv('案件编号', ui.h('span', { class: 'mono', text: preview.case_no || caseNo, dataset: { role: 'ds1' } })),
        kv('涉事用户', s.user_text || s.user_id || '—'),
        kv('风险评分', `${s.risk_score === undefined || s.risk_score === null ? '—' : s.risk_score} 分 · ${s.risk_level_label || s.risk_level || '—'}`),
        kv('处理结论', ui.h('span', { class: 'tag high', text: s.conclusion_label || conclusion, dataset: { role: 'ds4' } })),
        kv('联动动作', (s.action_labels || []).join(' · ') || actionTypes.join(' · ')),
        kv('处置原因备注', ui.h('span', { text: remark })),
      ]),
      // 副作用清单：**逐项列出服务端下发的每一项**，前端不做筛选、不加解释性省略
      ui.h('div', { class: 'dispose-block' }, [
        ui.h('div', { class: 'dispose-block-title', text: `将要发生的副作用（服务端 ${effects.length} 项，展示编号≠执行顺序）` }),
        ui.h('ol', { class: 'side-effect-list', dataset: { role: 'side-effects' } }, effects.map((it) => ui.h('li', {
          dataset: { role: 'side-effect', code: String(it.code || ''), seq: String(it.seq || '') },
          text: `${it.detail || SIDE_EFFECT_FALLBACK[it.code] || it.code}（${it.target || ''}）`,
        }))),
      ]),
      // 名单写入预览：勾了「拉黑用户/封禁设备」却看不到具体条目，等于没确认
      ui.h('div', { class: 'dispose-block' }, [
        ui.h('div', { class: 'dispose-block-title', text: '将写入的名单条目' }),
        writes.length
          ? ui.h('div', { class: 'kv-list', dataset: { role: 'list-writes-preview' } }, writes.map((w) => ui.h('div', {
            class: 'kv', dataset: { role: 'list-write-preview' },
          }, [
            ui.h('span', { class: 'k', text: listTypeLabel(w.list_type) }),
            ui.h('span', { class: 'v mono', text: `${entityTypeLabel(w.entity_type)} ${w.entity_value}（${w.expire_at ? `有效期至 ${ui.fmtTime(w.expire_at)}` : '永久'}）` }),
          ])))
          : ui.h('div', { class: 'hint', text: '本次无名单写入（该结论与动作按 BR-08-20 的默认映射不落名单库）' }),
      ]),
      skipped.length
        ? ui.h('div', { class: 'impact-note' }, skipped.map((t) => ui.h('div', { text: t })))
        : null,
      ui.h('div', { class: 'impact-note', text: '业务联动走 MockBizAdapter（模拟同步）：它不调用任何真实业务系统，返回值不具权威性（AD-08 / G-08）。' }),
      ui.h('div', { class: 'dispose-warn', text: WARN_LINE }),
      errBox,
    ]);
  }

  renderBody();

  const modal = ui.modal({
    title: `处置二次确认 · ${preview.case_no || caseNo}`,
    body: [bodyHolder, ttlLine],
    confirmText: '确认执行处置',
    cancelText: '取消',
    variant: 'danger',
    onCancel: () => { if (opts.onCancel) opts.onCancel(); },
    onConfirm: doDispose,
  });
  if (modal && modal.el) modal.el.dataset.role = 'dispose-modal';
  if (modal && modal.confirmBtn) modal.confirmBtn.dataset.role = 'dispose-confirm';
  if (modal && modal.cancelBtn) modal.cancelBtn.dataset.role = 'dispose-cancel';

  // ---- 令牌倒计时（BR-08-27：TTL 30s；过期就禁用确认，并给"重新确认"）----
  const expiresAt = Number(preview.expires_at) || (Date.now() + (Number(preview.expires_in) || 30) * 1000);
  function tick() {
    if (closed || !modal || !document.body.contains(modal.el)) { stopTimer(); return; }
    const left = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
    if (left > 0) {
      ttlLine.textContent = `确认令牌剩余 ${left} 秒（一次性、绑定本次参数与操作人；参数一变即失效）`;
      if (tokenExpired) { tokenExpired = false; if (modal.confirmBtn) modal.confirmBtn.disabled = false; }
    } else if (!tokenExpired) {
      tokenExpired = true;
      ttlLine.textContent = '确认令牌已过期：请点「重新确认」重新生成预览与令牌（未执行任何副作用）';
      if (modal.confirmBtn) modal.confirmBtn.disabled = true;
      const again = ui.button('重新确认', { variant: 'primary', onClick: () => rePreview(again) });
      again.dataset.role = 'dispose-repreview';
      again.classList.add('btn-sm');
      errBox.appendChild(again);
      errBox.classList.add('on');
    }
  }
  timer = setInterval(tick, 1000);
  tick();
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }

  /** 令牌过期 / 参数变了（DSP-4006）后重新签发：**重新 preview，不执行任何副作用**。 */
  async function rePreview(btn) {
    if (btn) ui.buttonBusy(btn, true, '重新确认中');
    try {
      const fresh = await api.request(`${CASE_URL(caseNo)}/dispose/preview`, {
        method: 'POST', body: { conclusion, action_types: actionTypes }, silent: true,
      });
      preview = fresh;
      clearErr();
      renderBody();
      stopTimer();
      const at = Number(preview.expires_at) || (Date.now() + (Number(preview.expires_in) || 30) * 1000);
      timer = setInterval(() => {
        if (closed || !modal || !document.body.contains(modal.el)) { stopTimer(); return; }
        const left = Math.max(0, Math.ceil((at - Date.now()) / 1000));
        ttlLine.textContent = left > 0
          ? `确认令牌剩余 ${left} 秒（一次性、绑定本次参数与操作人）`
          : '确认令牌已过期，请再次点「重新确认」';
        if (left <= 0 && modal.confirmBtn) modal.confirmBtn.disabled = true;
      }, 1000);
      if (modal.confirmBtn) modal.confirmBtn.disabled = false;
      ui.toast('已重新生成处置预览与确认令牌（仍未执行任何副作用）', 'ok');
    } catch (e) {
      showErr(disposeErrorText(e));
    } finally {
      if (btn) ui.buttonBusy(btn, false);
    }
  }

  // ---- ② 确认执行：携 confirm_token 调 /dispose（BR-08-27）----
  async function doDispose() {
    clearErr();
    if (tokenExpired) {
      showErr('确认令牌已过期，请点「重新确认」后再执行（服务端会以 DSP-4006 拒绝）');
      return false;   // 保持弹窗打开
    }
    ui.buttonBusy(modal.confirmBtn, true, '提交中');
    const payload = {
      conclusion,
      action_types: actionTypes,
      remark,
      confirm_token: preview.confirm_token,
      idempotency_key: idemKey,
    };
    if (Array.isArray(formData.list_writes) && formData.list_writes.length) payload.list_writes = formData.list_writes;
    if (Array.isArray(formData.evidence_refs) && formData.evidence_refs.length) payload.evidence_refs = formData.evidence_refs;
    if (formData.biz_targets && typeof formData.biz_targets === 'object') payload.biz_targets = formData.biz_targets;
    try {
      const result = await api.request(`${CASE_URL(caseNo)}/dispose`, {
        method: 'POST', body: payload, silent: true,
      });
      // 只有服务端确认 `status="disposed"` 才算成功——**不靠 HTTP 200 推断**
      if (!result || String(result.status) !== 'disposed') {
        showErr(`处置未确认生效（服务端返回 status=${result && result.status}），请刷新处置流水核对`);
        return false;
      }
      settled = { status: 'disposed', result };
      const labels = (result.action_labels || []).join('/') || result.action_types.join('/');
      ui.toast(`已处置：结论=${result.conclusion_label || result.conclusion} · 动作=${labels}`, 'ok', 6000);
      if (result.degraded) {
        ui.toast(result.notice || '处置已生效，但存在待重试的旁路副作用', 'warn', 8000);
      }
      if (opts.feedbackHost) renderDisposeFeedback(opts.feedbackHost, result);
      if (opts.onDone) opts.onDone(result);
      finish();
      return true;
    } catch (e) {
      // 失败**不关弹窗、不丢输入**（照 rule_editor.js）：把服务端文案留在弹窗里
      showErr(disposeErrorText(e));
      if (e && e.code === 'DSP-4006') {
        tokenExpired = true;
        if (modal.confirmBtn) modal.confirmBtn.disabled = true;
        const again = ui.button('重新确认', { variant: 'primary', onClick: () => rePreview(again) });
        again.dataset.role = 'dispose-repreview';
        again.classList.add('btn-sm');
        errBox.appendChild(again);
      }
      if (opts.onError) opts.onError(e);
      return false;   // 保持弹窗打开
    } finally {
      if (!settled) ui.buttonBusy(modal.confirmBtn, false);
    }
  }

  function finish() {
    if (closed) return;
    closed = true;
    stopTimer();
    if (modal) modal.close();
    if (cancelResolve && !settled) cancelResolve({ status: 'cancelled' });
  }

  // `done` 让调用方（含自动化）能等这一次流程的结局，而不必去轮询 DOM
  let doneResolve;
  let cancelResolve;
  const done = new Promise((resolve) => { doneResolve = resolve; cancelResolve = resolve; });
  const origOnCancel = opts.onCancel;
  // 包一层：取消（含 ESC）也要把 `done` 兑现为 cancelled
  const handle = {
    close: () => finish(),
    modal: modal ? (modal.el || null) : null,
    done,
    get settled() { return settled; },
    get preview() { return preview; },
  };
  // 取消路径：ui.modal 的取消按钮与 ESC 都会走 onCancel；这里不再补丁 DOM，
  // 而是让 `done` 在弹窗被移除时兜底兑现（见下），因此无需改写 ui.modal。
  void origOnCancel;
  const watcher = setInterval(() => {
    if (closed) { clearInterval(watcher); return; }
    if (!modal || !document.body.contains(modal.el)) {
      clearInterval(watcher);
      closed = true;
      stopTimer();
      if (!settled) cancelResolve({ status: 'cancelled' });
      else doneResolve(settled);
    }
  }, 200);
  done.then(() => { clearInterval(watcher); });
  if (settled) doneResolve(settled);
  return handle;
}

// ============================================================
// ② 处置流水（`GET /cases/{no}/actions`，供 07 详情页复用）
// ============================================================

/** 拉取处置流水（只读，`case:read`）。 */
export async function loadActions(caseNo) {
  return api.request(`${CASE_URL(caseNo)}/actions`, { silent: true });
}

/**
 * 处置流水表（Spec §3.2 冻结：按 `acted_at` **升序**）。
 *
 * 服务端 `list_by_case` 已是升序；这里**再排一次**是刻意的防御：
 * "流水按时间升序"是契约，若哪天后端换了排序，页面仍应保持契约顺序，
 * 而不是把顺序悄悄反转（审核员读处置过程必须按发生顺序读）。
 */
export function actionsTable(items) {
  const rows = (Array.isArray(items) ? items.slice() : []).sort(
    (a, b) => Number((a || {}).acted_at || 0) - Number((b || {}).acted_at || 0),
  );
  const head = ['处置时间', '动作', '结论', '处置人', '业务联动', '名单写入', '处置备注', '流水号'];
  const trs = rows.map((it) => {
    const biz = it.biz_sync_result || {};
    const failed = String(biz.status) === 'failed';
    const actedAt = Number(it.acted_at || 0);
    return ui.h('tr', {
      dataset: { role: 'action-row', actedAt: String(actedAt), actionId: String(it.action_id || '') },
    }, [
      ui.h('td', { text: ui.fmtTime(actedAt) }),
      ui.h('td', {}, ui.h('span', { class: 'tag brand', text: it.action_label || it.action_type || '—' })),
      ui.h('td', { text: it.conclusion || '—' }),
      ui.h('td', { text: `${it.operator || '—'}${it.operator_role ? `（${it.operator_role}）` : ''}` }),
      ui.h('td', {}, failed
        ? ui.h('span', { class: 'biz-failed', text: bizSyncText(biz) })
        : ui.h('span', { class: 'cell-note', text: bizSyncText(biz) })),
      ui.h('td', {}, ui.h('span', { class: 'cell-note',
        text: (it.list_writes || []).length ? (it.list_writes || []).map(writeText).join('；') : '本次无名单写入' })),
      ui.h('td', {}, ui.h('span', { class: 'cell-note', text: it.remark || '—' })),
      ui.h('td', {}, ui.h('span', { class: 'mono', text: it.action_id || '—' })),
    ]);
  });
  const table = ui.table(head.map((name) => ({ name })), trs);
  table.dataset.role = 'actions-table';
  return table;
}

/**
 * 渲染处置流水到容器。
 * @param {Node} host 容器（07 详情页给一个 div）
 * @param {object|Array} data `GET .../actions` 的响应 或 直接给 items 数组
 * @param {object} [opts] `{empty: '文案'}`
 * @returns {Node} host
 */
export function renderActions(host, data, opts = {}) {
  const items = Array.isArray(data) ? data : ((data && data.items) || []);
  if (!items.length) {
    ui.mount(host, ui.empty(opts.empty || '该案件暂无处置流水', '处置提交成功后会在此按时间升序出现（BR-08-37：一个动作一条流水）'));
    return host;
  }
  ui.mount(host, actionsTable(items));
  return host;
}

// ============================================================
// ③ 处置结果反馈（展示点 C，Spec §2.3）
// ============================================================

/**
 * 结果反馈条（成功 / DSP-5003 业务联动失败可重试 / DSP-5004 审计待重试）。
 *
 * **这里是最不能"美化"的一处**：`DSP-5002`（名单写入失败）根本走不到这里——
 * 它在 `dispose` 阶段就抛错、处置未生效，弹窗会如实显示"未生效"。
 * 能进到这里的只有"已生效但旁路待重试"，以及"全部成功"。
 *
 * @param {object} result `POST /dispose` 的响应
 * @param {object} [opts] `{caseNo, onRetried(retryResult)}`
 * @returns {Node}
 */
export function disposeFeedbackNode(result, opts = {}) {
  const r = result || {};
  const caseNo = r.case_no || opts.caseNo || '';
  const biz = r.biz_sync || {};
  const bizFailed = String(biz.status) === 'failed';
  const auditPending = !!(r.retry_hint && r.retry_hint.audit);
  const labels = (r.action_labels || r.action_types || []).join('/');

  const box = ui.h('div', { class: 'dispose-feedback', dataset: { role: 'dispose-feedback' } });

  function paint(retryInfo) {
    const info0 = (retryInfo && retryInfo.biz_sync) || null;
    const retryOk = !!(info0 && String(info0.status) === 'ok' && Number(retryInfo.attempt_no || 0) > 0);
    const nodes = [];
    if (retryOk) {
      // **重试成功后首要横幅必须是成功**：否则页面上留着一条黄色的"同步失败"，
      // 而它的语义已经被这次重试推翻了——"当前状态"与"历史记录"必须分开。
      // 历史并不掩盖：重试是**追加**记录（attempt_no 递增），流水里那条失败仍在。
      nodes.push(ui.banner('ok', `业务系统已同步成功（第 ${retryInfo.attempt_no} 次重试）· ${bizSyncText(info0)}`));
    } else if (bizFailed) {
      // BR-08-31 / DSP-5003：处置**已生效**，但业务同步失败——如实显示失败 + 给重试入口
      nodes.push(ui.banner('warn', `${r.notice || '处置已生效，业务系统同步失败（模拟业务系统未对接），已加入重试队列'}：${biz.message || '原因未返回'}`));
    } else if (auditPending) {
      nodes.push(ui.banner('warn', r.notice || '处置已生效，审计落库待重试（audit_pending）'));
    } else {
      nodes.push(ui.banner('ok', `已处置：结论=${r.conclusion_label || r.conclusion || '—'} · 动作=${labels || '—'}`));
    }
    if (auditPending && bizFailed) {
      nodes.push(ui.banner('warn', '审计落库待重试（audit_pending）'));
    }

    nodes.push(ui.h('div', { class: 'kv-list' }, [
      kv('案件编号', ui.h('span', { class: 'mono', text: caseNo })),
      kv('案件状态', ui.h('span', { class: 'tag high', text: '已处置', dataset: { role: 'case-status' } })),
      kv('处置人', `${r.operator || '—'}${r.operator_role ? `（${r.operator_role}）` : ''}`),
      kv('处置时间', ui.fmtTime(r.acted_at)),
      kv('处置流水', ui.h('span', { class: 'mono', text: (r.action_ids || (r.action_id ? [r.action_id] : [])).join('、') || '—' })),
      kv('业务联动', ui.h('span', { text: bizSyncText(info0 && retryOk ? info0 : biz) })),
      kv('名单写入', (r.list_writes || []).length ? (r.list_writes || []).map(writeText).join('；') : '本次无名单写入'),
      kv('审计', r.audit && r.audit.log_id
        ? ui.h('span', { class: 'mono', text: `${r.audit.log_id}（hash ${String(r.audit.hash || '').slice(0, 12)}…）` })
        : ui.h('span', { text: auditPending ? '待重试（audit_pending）' : '—' })),
    ]));
    nodes.push(ui.h('div', { class: 'hint', text:
      '业务联动的结论来自 MockBizAdapter（模拟同步），不具权威性，不能当作"已对接真实业务系统"（AD-08 / G-08）。' }));

    // 「重试同步」按钮：仅当"当前仍未同步成功"时出现（成功后不再留一个会让人重复点的按钮）
    if (bizFailed && !retryOk) {
      const btn = ui.button('重试同步', {
        variant: 'warn',
        onClick: () => doRetry(btn),
      });
      btn.dataset.role = 'biz-retry';
      btn.classList.add('btn-sm');
      nodes.push(ui.h('div', { class: 'dispose-retry' }, [
        btn,
        ui.h('span', { class: 'hint', text: '只重试业务同步：名单与案件状态在处置时已生效，不会重放（BR-08-31）。' }),
      ]));
    }
    if (retryInfo) {
      const info = retryInfo.biz_sync || {};
      // 重试的三种结局必须**分开**渲染，绝不合并成一句"成功"
      if (String(info.target) === 'none' || Number(retryInfo.attempt_no || 0) === 0) {
        nodes.push(ui.banner('warn', `没有待重试的业务联动${info.message ? `：${info.message}` : ''}`));
      } else if (retryOk) {
        // 已作为首要横幅呈现；这里只补一句留痕说明，说明"重试不覆盖原记录"
        nodes.push(ui.h('div', { class: 'hint', text:
          '本次重试是追加记录（attempt_no 递增）：首次处置时那次失败仍保留在处置流水的 biz_sync_result 中（Spec §3.1）。' }));
      } else {
        nodes.push(ui.banner('err', `重试后仍未同步成功（第 ${retryInfo.attempt_no} 次）：${info.message || '原因未返回'}`));
      }
    }
    ui.mount(box, nodes);
  }

  async function doRetry(btn) {
    ui.buttonBusy(btn, true, '重试中');
    try {
      const info = await retryBizSync(caseNo, r.action_id, { silent: true });
      paint(info);
      if (opts.onRetried) opts.onRetried(info);
    } catch (e) {
      ui.toast(disposeErrorText(e), 'err', 6000);
    } finally {
      if (document.body.contains(btn)) ui.buttonBusy(btn, false);
    }
  }

  paint(null);
  return box;
}

/** 把结果反馈渲染进容器（07 的结果位直接用这个）。 */
export function renderDisposeFeedback(host, result, opts = {}) {
  ui.mount(host, disposeFeedbackNode(result, opts));
  return host;
}

// ============================================================
// ④ 业务联动重试（`POST /cases/{no}/biz-sync/retry`）
// ============================================================

/**
 * 重试业务系统同步（BR-08-31）。返回服务端结果，**不吞异常**（调用方决定怎么显示）。
 * `actionId` 省略时重试该案件全部未成功的联动（Spec §3.1）。
 */
export function retryBizSync(caseNo, actionId, opts = {}) {
  const body = actionId ? { action_id: String(actionId) } : {};
  return api.request(`${CASE_URL(caseNo)}/biz-sync/retry`, {
    method: 'POST', body, silent: opts.silent !== false,
  });
}

// ============================================================
// ⑤ 认领 / 归档（Spec 归 07 渲染的按钮；这里只导出可被调用的函数）
// ============================================================

/** 认领（BR-08-04 原子认领，幂等：同人重复认领 `changed=false` 且不刷新认领时间）。 */
export function claimCase(caseNo) {
  return api.request(`${CASE_URL(caseNo)}/claim`, { method: 'POST', body: {}, silent: true });
}

/** 归档（Spec §3.1：仅 `disposed` 可归档；权限上仅 admin，BR-08-36）。 */
export function archiveCase(caseNo, remark) {
  const body = remark ? { remark: String(remark) } : {};
  return api.request(`${CASE_URL(caseNo)}/archive`, { method: 'POST', body, silent: true });
}

/** 认领二次确认弹窗（按钮归 07；这里提供可直接挂上去的流程）。 */
export function openClaimConfirm(caseNo, opts = {}) {
  const errBox = ui.h('div', { class: 'form-error', dataset: { role: 'claim-error' } });
  const m = ui.modal({
    title: `认领案件 · ${caseNo}`,
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('案件编号', ui.h('span', { class: 'mono', text: caseNo })),
        kv('认领人', '当前登录审核员（服务端取自令牌，BR-08-07）'),
      ]),
      ui.h('div', { class: 'impact-note', text:
        '认领后案件状态变为「审核中」，并生成认领截止时间（默认 30 分钟，超时未处置将被回收，BR-08-05/10）。' }),
      errBox,
    ],
    confirmText: '确认认领',
    variant: 'primary',
    onConfirm: async () => {
      try {
        const d = await claimCase(caseNo);
        if (d && d.changed === false) {
          ui.toast('你已认领该案件（幂等：未刷新认领时间）', 'warn', 5000);
        } else {
          ui.toast(`已认领 ${d && d.case_no ? d.case_no : caseNo}`
            + `（认领截止 ${ui.fmtTime(d && d.claim_deadline_at)}）`, 'ok', 5000);
        }
        if (opts.onDone) opts.onDone(d);
        return true;
      } catch (e) {
        errBox.textContent = disposeErrorText(e);
        errBox.classList.add('on');
        if (opts.onError) opts.onError(e);
        return false;
      }
    },
  });
  if (m && m.confirmBtn) m.confirmBtn.dataset.role = 'claim-confirm';
  return m;
}

/** 归档二次确认弹窗（可填备注；失败保留弹窗与错误文案）。 */
export function openArchiveConfirm(caseNo, opts = {}) {
  const errBox = ui.h('div', { class: 'form-error', dataset: { role: 'archive-error' } });
  const remarkI = ui.inputBox({ placeholder: '归档备注（选填，≤500 字）', maxlength: REMARK_MAX });
  const m = ui.modal({
    title: `归档案件 · ${caseNo}`,
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('案件编号', ui.h('span', { class: 'mono', text: caseNo })),
        kv('归档备注', remarkI),
      ]),
      ui.h('div', { class: 'impact-note', text:
        '仅「已处置」的案件可归档（DSP-4003）；归档权限属管理员（BR-08-36），且会写入审计 case.archive（BR-08-09）。' }),
      errBox,
    ],
    confirmText: '确认归档',
    variant: 'warn',
    onConfirm: async () => {
      try {
        const d = await archiveCase(caseNo, remarkI.value.trim());
        ui.toast(`案件已归档：${(d && d.case_no) || caseNo}（${ui.fmtTime(d && d.archived_at)}）`, 'ok', 5000);
        if (opts.onDone) opts.onDone(d);
        return true;
      } catch (e) {
        errBox.textContent = String((e && e.code) === 'AUTH-4020' || (e && e.code) === 'AUTH-403'
          ? '归档仅管理员可执行（BR-08-36）' : disposeErrorText(e));
        errBox.classList.add('on');
        if (opts.onError) opts.onError(e);
        return false;
      }
    },
  });
  if (m && m.confirmBtn) m.confirmBtn.dataset.role = 'archive-confirm';
  return m;
}

export default {
  openDisposeFlow,
  openClaimConfirm,
  openArchiveConfirm,
  claimCase,
  archiveCase,
  retryBizSync,
  loadActions,
  renderActions,
  actionsTable,
  disposeFeedbackNode,
  renderDisposeFeedback,
  disposeErrorText,
  incompatibleReason,
  normalizeActions,
  bizSyncText,
  CONCLUSIONS,
  ACTION_TYPES,
  TIGHT_ACTIONS,
  REMARK_MAX,
  WARN_LINE,
};
