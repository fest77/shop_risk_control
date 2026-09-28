/* 规则配置视图（模块 06-B · 策略与规则页的默认标签 `#/rules`）
 *
 * 职责边界：只做「取数 → 渲染 → 交回给编辑器提交」。业务不变量（编码生成、version
 * 递增、乐观锁、内置规则不可删、条件树结构校验）**全部由服务端裁决**，前端只负责
 * 把错误码翻译成人话并按 `path` 定位到节点（BR-06-17、§5.1）。
 *
 * 与 06-A 的名单库页（`#/lists`）共用 Spec §2.1 的标签条 `cfgTabs`；三个标签各自
 * 保留筛选与页码（写在 hash query 里，刷新与切标签不丢）。
 *
 * Spec 依据：§2.2 规则配置 tab / §2.2.2 9 列表格 / §2.2.3 编辑抽屉 / §4.1 业务规则
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as session from '../session.js';
import { ensureEnums, labelOf, optionsOf } from '../store.js';
import { cfgTabs, rememberQuery, currentQuery } from './cfg_tabs.js';
import { ensureFeatureMeta } from './cond_tree.js';
import { openRuleEditor, openDeleteConfirm, openToggleConfirm } from './rule_editor.js';

export const meta = { title: '策略与规则', crumb: '策略与规则 / 规则配置' };

const RULE_URL = `${api.API_PREFIX}/rules`;
const IMPORT_URL = `${RULE_URL}/import`;
const TEMPLATE_URL = `${RULE_URL}/import-template`;
const HASH = '#/rules';

// 视图状态放模块级：菜单间来回切换时保留筛选与页码（Spec §2.1）
const state = {
  scene: '', status: '', keyword: '', page: 1, pageSize: 20, total: 0, pages: 0,
};

let inflight = null;
let refs = null;
let searchTimer = null;
// 最近一次**成功**渲染出的表体内容（§3.5：失败时保留旧数据，不得白屏/清空）
let lastRendered = null;

export function dispose() {
  if (inflight) { inflight.abort(); inflight = null; }
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
  refs = null;
}

function isWriter() {
  // 权限来自 /auth/me 的 permissions（BR-01-12），前端不按角色推导；
  // 无写权限时按钮**不渲染**（BR-01-14），而不是渲染出来再等 403
  return session.has('rule:write');
}

// ==================== hash query 状态（§2.1：三个 tab 各自保留筛选与页码）====================
function readQuery() {
  const q = new URLSearchParams(currentQuery());
  if (!currentQuery()) {
    // 由菜单直接进入（无 query）→ 回到默认筛选，避免上次的筛选让人以为"规则少了"
    state.scene = ''; state.status = ''; state.keyword = ''; state.page = 1;
    return;
  }
  state.scene = q.get('scene') || '';
  state.status = q.get('status') || '';
  state.keyword = q.get('kw') || '';
  const p = parseInt(q.get('page') || '1', 10);
  state.page = Number.isFinite(p) && p > 0 ? p : 1;
}

function pushQuery() {
  const qs = new URLSearchParams();
  if (state.scene) qs.set('scene', state.scene);
  if (state.status) qs.set('status', state.status);
  if (state.keyword) qs.set('kw', state.keyword);
  if (state.page > 1) qs.set('page', String(state.page));
  const q = qs.toString();
  rememberQuery(HASH, q);
  const next = q ? `${HASH}?${q}` : HASH;
  // 用 replaceState 而不是 location.hash：筛选变化不该把历史记录塞满（回退键要能退回上一个页面）
  if (location.hash !== next) {
    try { history.replaceState(null, '', next); } catch (e) { /* 老浏览器忽略 */ }
  }
}

// ==================== 条件摘要（只读渲染成人读文本，§2.2.2）====================
const OP_SYM = {
  eq: '=', ne: '≠', gt: '>', gte: '≥', lt: '<', lte: '≤',
  in: '∈', not_in: '∉', exists: '存在', contains: '包含',
};

function valueText(v) {
  if (Array.isArray(v)) return `[${v.join(', ')}]`;
  if (v === undefined) return '—';
  return String(v);
}

/**
 * 条件树 → 人读文本（如 `device_user_cnt ≥ 5 AND user_age_days < 3`）。
 * 坏数据**不允许**中断整表渲染，因此调用方必须接住异常并降级为 `[条件树]`。
 */
function summarize(node, top = true) {
  if (!node || typeof node !== 'object') throw new Error('节点不是对象');
  if (node.logic) {
    const kids = Array.isArray(node.children) ? node.children : [];
    const parts = kids.map((k) => summarize(k, false)).filter(Boolean);
    if (!parts.length) return '';
    const joiner = String(node.logic).toLowerCase() === 'or' ? ' OR ' : ' AND ';
    const text = parts.join(joiner);
    return top ? text : `(${text})`;
  }
  if (!node.field) throw new Error('叶子缺 field');
  if (node.op === 'exists') return `${node.field} 存在`;
  const sym = OP_SYM[node.op] || node.op || '?';
  return `${node.field} ${sym} ${valueText(node.value)}`;
}

/** 条件摘要单元格：服务端给了 `condition_summary` 就用它，否则前端本地渲染。
 *
 * 摘要列在 1280px 视口下仍会被截断（9 列 + 侧栏），因此**必须挂 `title`**：
 * `ui.table` 只给字符串单元格设 title，节点单元格不会自动带，而这一列恰恰是
 * 最需要看全的一列（读不到全部条件时用户只能去开编辑抽屉）。
 */
function summaryCell(it) {
  const srv = typeof it.condition_summary === 'string' ? it.condition_summary.trim() : '';
  // 服务端渲染不出来时同样回 `[条件树]`；那种情况要给「查看 JSON」入口，而不是把
  // 这个标记当成正常摘要直接显示（§2.2.2 的降级要求）。
  if (srv && srv !== '[条件树]') return ui.h('span', { class: 'cond-summary', text: srv, title: srv });
  if (!srv) {
    let text = '';
    try { text = summarize(it.condition); } catch (e) { text = ''; }
    if (text) return ui.h('span', { class: 'cond-summary', text, title: text });
  }
  // 渲染失败时降级显示 `[条件树]` 并给出「查看 JSON」（§2.2.2 原文）
  const b = ui.button('查看 JSON', { variant: 'ghost', onClick: () => {
    ui.modal({
      title: '条件树 JSON',
      body: [ui.h('pre', { class: 'mono cond-json-body', text: JSON.stringify(it.condition, null, 2) })],
      confirmText: '关闭', variant: 'ghost',
    });
  } });
  b.classList.add('btn-sm');
  return ui.h('span', { class: 'cond-fallback' }, [
    ui.h('span', { class: 'mono', text: '[条件树]' }), b,
  ]);
}

// ==================== 表格 ====================
function renderTable(items) {
  if (!items.length) {
    const filtered = !!(state.scene || state.status || state.keyword);
    lastRendered = ui.empty(
      filtered ? '当前筛选条件下暂无规则' : '暂无规则，点击「＋ 新建规则」创建第一条规则',
      filtered ? '可点「重置」清空筛选条件'
        : (isWriter() ? '新建的规则默认停用，仿真验证通过后再启用（BR-06-04 / BR-06-09）'
          : '当前角色仅可查看（BR-06-35）'),
    );
    ui.mount(refs.tableWrap, lastRendered);
    return;
  }
  // 列宽按 1280px 视口（侧栏 200px）配平：把宽度让给「条件摘要」——它是信息量最大
  // 的一列，实测给 170px 时会被截成 `after_sale_rate_24…`，几乎不可读（D65 的教训）。
  const columns = [
    { name: '规则编码', width: 120 }, { name: '规则名称', width: 150 },
    { name: '场景', width: 76 }, { name: '条件摘要' },
    { name: '分值', width: 52, align: 'right' }, { name: '优先级', width: 58, align: 'right' },
    { name: '版本', width: 50 }, { name: '状态', width: 92 }, { name: '操作', width: 160 },
  ];
  const rows = items.map((it) => [
    codeCell(it),
    it.name || '—',
    labelOf('rule_scenes', it.scene_code, it.scene_code === 'common' ? '通用' : (it.scene_code || '—')),
    summaryCell(it),
    String(it.score === undefined || it.score === null ? '—' : it.score),
    String(it.priority === undefined || it.priority === null ? '—' : it.priority),
    `v${it.version === undefined || it.version === null ? '—' : it.version}`,
    statusCell(it),
    opCell(it),
  ]);
  lastRendered = ui.table(columns, rows);
  ui.mount(refs.tableWrap, lastRendered);
}

function codeCell(it) {
  const code = String(it._id || it.rule_code || '—');
  const nodes = [ui.h('span', { class: 'mono', text: code })];
  if (it.is_system) {
    // BR-06-08：内置规则不可删除，只能停用 —— 在编码旁挂「内置」标记（§2.2.2）
    const t = ui.tag('内置', 'plain');
    t.title = '系统内置规则只能停用、不能删除（BR-06-08）';
    nodes.push(document.createTextNode(' '), t);
  }
  return ui.h('span', { class: 'rule-code' }, nodes);
}

/** 状态列：开关置位（enabled → 置位）。点击即发起启停用（带 expected_version 乐观锁）。 */
function statusCell(it) {
  const enabled = it.status === 'enabled';
  const cb = ui.h('input', { type: 'checkbox', checked: enabled ? 'checked' : null });
  const wrap = ui.h('label', { class: 'rswitch' }, [
    cb,
    ui.h('span', { class: 'rswitch-track' }),
    ui.h('span', { class: 'rswitch-text', text: enabled ? '启用' : '停用' }),
  ]);
  if (!isWriter() || !canToggle(it)) {
    cb.disabled = true;
    wrap.classList.add('is-readonly');
    return wrap;
  }
  cb.addEventListener('change', () => {
    // 先还原开关：状态只有服务端确认后才变（乐观锁冲突时页面不能显示"已切换"）
    cb.checked = enabled;
    openToggleConfirm(it, enabled ? 'disabled' : 'enabled', () => load());
  });
  return wrap;
}

/** 已删除（软删）的规则不再允许启停用；`deleted` 标记由服务端给出时尊重它。 */
function canToggle(it) {
  return it.deleted !== true && it.status !== 'deleted';
}

function small(btn) { btn.classList.add('btn-sm'); return btn; }

function opCell(it) {
  if (!isWriter()) return ui.h('span', { class: 'cell-note', text: '当前角色仅可查看' });
  const nodes = [small(ui.button('编辑', { onClick: () => edit(it) }))];
  if (canToggle(it)) {
    const enabled = it.status === 'enabled';
    nodes.push(small(ui.button(enabled ? '停用' : '启用', {
      onClick: () => openToggleConfirm(it, enabled ? 'disabled' : 'enabled', () => load()),
    })));
  }
  // BR-06-08：内置规则**不渲染删除入口**
  if (!it.is_system) {
    nodes.push(small(ui.button('删除', { variant: 'danger', onClick: () => openDeleteConfirm(it, () => load()) })));
  }
  return ui.h('div', { class: 'row-ops' }, nodes);
}

function renderPager() {
  const pages = state.pages || (state.total ? Math.ceil(state.total / state.pageSize) : 0);
  refs.pageInfo.textContent = `共 ${state.total} 条 · 第 ${state.page} / ${Math.max(1, pages)} 页`;
  refs.btnPrev.disabled = state.page <= 1;
  refs.btnNext.disabled = pages === 0 || state.page >= pages;
}

/**
 * BR-06-12：每个场景下 `status=enabled` 的规则分值合计 > 100 时给出**非阻断**提示。
 *
 * 优先用服务端下发的 `score_hints`（它按**完整规则集**统计，分页也准确）；
 * 服务端没给（老版本/接口退化）时才退回按本页求和，并且**只在"本页就是全部数据"时**
 * 才敢显示数字——分页时按当前页求和会把总和说小，那是**错误的数字**，比不提更糟。
 */
function renderScoreHint(items, data) {
  const hints = (data && Array.isArray(data.score_hints)) ? data.score_hints : null;
  if (hints && hints.length) {
    const over = hints.filter((h) => h.over_limit === true || (Number(h.enabled_score_sum) || 0) > 100);
    if (!over.length) { ui.clear(refs.hintWrap); return; }
    ui.mount(refs.hintWrap, over.map((h) => ui.banner('warn',
      `场景「${h.scene_name || labelOf('rule_scenes', h.scene_code, h.scene_code)}」启用规则分值合计 `
      + `${h.enabled_score_sum} 分（${h.enabled_rule_count || 0} 条），累加将按 100 截断`
      + '（BR-06-12 / 悬空点 G-03；截断由模块 05 的 BR-05-16 负责）。')));
    return;
  }
  if (state.total > items.length) {
    ui.mount(refs.hintWrap, ui.h('div', { class: 'hint',
      text: '启用规则分值合计提示需要完整规则集，当前为分页视图且服务端未下发 score_hints，故暂不计算（BR-06-12）。' }));
    return;
  }
  const byScene = new Map();
  items.forEach((it) => {
    if (it.status !== 'enabled') return;
    const k = it.scene_code || 'common';
    byScene.set(k, (byScene.get(k) || 0) + (Number(it.score) || 0));
  });
  const over = [...byScene.entries()].filter(([, v]) => v > 100);
  if (!over.length) { ui.clear(refs.hintWrap); return; }
  ui.mount(refs.hintWrap, over.map(([k, v]) => ui.banner('warn',
    `场景「${labelOf('rule_scenes', k, k)}」启用规则分值合计 ${v} 分（按本页统计），累加将按 100 截断（BR-06-12 / 悬空点 G-03）。`)));
}

function showError(msg) { ui.mount(refs.banners, ui.banner('err', msg)); }
function showOk(msg) { ui.mount(refs.banners, ui.banner('ok', msg)); }
function clearBanners() { ui.clear(refs.banners); }

// ==================== 取数 ====================
async function load({ showSkeleton = false } = {}) {
  clearBanners();
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;
  if (showSkeleton) ui.mount(refs.tableWrap, ui.loading(8));

  try {
    const data = await api.get(RULE_URL, {
      scene_code: state.scene,
      status: state.status,
      keyword: state.keyword,
      page: state.page,
      page_size: state.pageSize,
      sort: 'priority:asc,_id:asc',
    }, signal);
    const items = (data && data.items) || [];
    if (!refs) return;   // 视图已被切走（dispose）→ 丢弃这次成功响应
    state.total = (data && data.total) || 0;
    state.pages = (data && data.pages) || 0;
    renderTable(items);
    renderPager();
    renderScoreHint(items, data);
    refs.asOf.textContent = `数据截至 ${ui.fmtTime(data && data.as_of, '—')}`;
  } catch (e) {
    if (e.name === 'AbortError') return;   // 已被更新的请求取代，静默丢弃
    if (!refs) return;                     // 视图已销毁，没有任何东西可更新
    // 失败态：保留上一次成功的数据（§2.2.2：不得让策略师误判"规则被删了"）
    ui.mount(refs.tableWrap, lastRendered || ui.empty('规则加载失败', '可点「查询」重试'));
    showError(`规则加载失败：${e.message}（trace ${typeof e.shortTrace === 'function' ? e.shortTrace() : '—'}）。已保留上次结果，可点「查询」重试。`);
  } finally {
    if (inflight && inflight.signal === signal) inflight = null;
  }
}

// ==================== 交互 ====================
function edit(it) {
  // onStale：抽屉里发现版本冲突（或被"载入最新版本"）时刷新列表，
  // 让行上的 version 与服务端一致——否则紧接着点「启用」会拿旧版本再撞一次 409
  openRuleEditor(it, { onSaved: () => load(), onStale: () => load(), checkDuplicate: sceneNameTaken });
}

/**
 * 同场景内重名校验（Spec §2.2.3 明确把它写成 **UI 校验**；服务端刻意不拦，
 * 见任务裁定的"场景内规则重名只做前端校验"）。
 *
 * 走服务端搜索（`keyword` 同时匹配 `_id` 与 `name`）而不是"只看当前页"：
 * 规则一多就会跨页，只看本页等于把跨页重名放过去——那种"校验通过但列表里两条同名"
 * 的状态正是这条规则要避免的。
 */
async function sceneNameTaken(name, sceneCode, excludeCode) {
  const data = await api.get(RULE_URL, {
    scene_code: sceneCode, keyword: name, page_size: 100,
  }, null);
  const items = (data && data.items) || [];
  return items.some((it) => String(it.scene_code) === String(sceneCode)
    && String(it.name || '').trim() === name
    && String(it._id) !== String(excludeCode || ''));
}

function createNew() {
  openRuleEditor(null, {
    onSaved: () => { state.page = 1; pushQuery(); load(); },
    checkDuplicate: sceneNameTaken,
  });
}

// ---- 规则批量导入（Spec §3.1 `POST /api/v1/rules/import` + `GET /rules/import-template`）----
const CSV_HINT = '尚未选择文件（仅支持 .csv，≤2MB，≤500 行）';
// 模板下载与名单导入同一手法：文件流不能走 api.request（它会 res.json()），
// 但令牌仍必须放请求头——`window.open` 带不了 Authorization，把 token 拼进 URL
// 会进浏览器历史与访问日志。
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

function attachmentName(res, fallback) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  if (!m) return fallback;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

function openImportModal() {
  let picked = null;
  // 只收 .csv：后端明确不支持 XLSX（需引入 openpyxl，与 06-A 名单导入同口径），
  // 受理 .xlsx 只会让用户在"选了文件却表头不匹配"上白花一轮
  const fileInput = ui.h('input', { type: 'file', accept: '.csv' });
  const fileHint = ui.h('span', { class: 'hint', text: CSV_HINT });
  const modeSel = ui.selectBox([
    { value: 'partial', label: 'partial（成功行入库，失败行报明细）' },
    { value: 'atomic', label: 'atomic（任一行失败则整批不写）' },
  ], 'partial');
  const errBox = ui.h('div', { class: 'form-error' });
  const resultWrap = ui.h('div');
  const btnImport = ui.button('开始导入', { variant: 'primary', onClick: () => run() });
  const btnTpl = ui.button('下载模板', { variant: 'ghost', onClick: () => template(btnTpl) });

  fileInput.addEventListener('change', () => {
    picked = fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
    fileHint.textContent = picked ? `已选：${picked.name}` : CSV_HINT;
    errBox.classList.remove('on');
  });

  async function template(btn) {
    ui.buttonBusy(btn, true, '下载中');
    try {
      const res = await fetch(TEMPLATE_URL, {
        headers: { Authorization: `Bearer ${session.getToken()}` },
      });
      if (!res.ok) {
        const env = await res.json().catch(() => null);
        throw new Error(env && env.code ? `[${env.code}] ${env.message}` : `HTTP ${res.status}`);
      }
      downloadBlob(await res.blob(), attachmentName(res, 'rule_import_template.csv'));
      ui.toast('模板已下载，请勿改动表头', 'ok');
    } catch (e) {
      ui.toast(`模板下载失败：${e.message}`, 'err', 6000);
    } finally {
      ui.buttonBusy(btn, false);
    }
  }

  async function run() {
    if (!picked) { errBox.textContent = '请先选择要导入的 CSV 文件。'; errBox.classList.add('on'); return; }
    ui.buttonBusy(btnImport, true, '导入中');
    const form = new FormData();
    form.append('file', picked);
    form.append('mode', modeSel.value);
    try {
      const data = await api.postForm(IMPORT_URL, form, { silent: true });
      // 契约：{total, success, failed, mode, rows:[{row, rule_code, reason}], rows_truncated}
      const rows = Array.isArray(data.rows) ? data.rows : [];
      ui.mount(resultWrap, [
        ui.h('div', { class: 'import-counts' }, [
          ui.h('div', { class: 'import-stat' }, [ui.h('span', { class: 'hint', text: '总行数' }),
            ui.h('span', { class: 'import-stat-v', text: String(data.total) })]),
          ui.h('div', { class: 'import-stat' }, [ui.h('span', { class: 'hint', text: '成功' }),
            ui.h('span', { class: 'import-stat-v', text: String(data.success) })]),
          ui.h('div', { class: 'import-stat' }, [ui.h('span', { class: 'hint', text: '失败' }),
            ui.h('span', { class: 'import-stat-v', text: String(data.failed) })]),
        ]),
        ui.h('div', { class: 'hint', text:
          `导入模式：${data.mode === 'atomic' ? 'atomic（整批写入）' : 'partial（成功行入库）'}；`
          + '导入的规则默认停用（BR-06-04），上线前请先仿真。' }),
        rows.length ? ui.table(
          [{ name: '行号', width: 64 }, { name: '规则编码', width: 130 }, { name: '原因' }],
          rows.map((r) => [r.row === undefined ? '—' : String(r.row),
            ui.h('span', { class: 'mono', text: r.rule_code || '（留空，由服务端生成）' }),
            r.reason || '—'])) : null,
        data.rows_truncated
          ? ui.h('div', { class: 'hint', text: '错误明细超过回传上限，此处仅展示前若干条（其余已计入失败数）。' })
          : null,
      ]);
      ui.toast(`导入完成：共 ${data.total} 行，成功 ${data.success} 行，失败 ${data.failed} 行`,
        data.failed ? 'warn' : 'ok', 5000);
      load();   // 新入库的规则要立刻出现在列表里
    } catch (e) {
      errBox.textContent = `导入失败：${e.message}`;
      errBox.classList.add('on');
    } finally {
      ui.buttonBusy(btnImport, false);
    }
  }

  ui.modal({
    title: '批量导入规则',
    confirmText: '关闭', variant: 'ghost',
    body: ui.h('div', { class: 'import-box' }, [
      ui.h('div', { class: 'step' }, [
        ui.h('div', { class: 'step-title', text: '① 下载模板' }),
        ui.h('div', { class: 'step-body' }, [
          ui.h('div', { class: 'hint', text:
            '模板列：rule_code, name, scene_code, description, condition, score, priority, status'
            + '（含示例行与枚举注释），请勿改动表头。仅支持 CSV：Excel 请另存为 CSV（后端不支持 XLSX）。' }),
          ui.h('div', { class: 'file-row' }, [btnTpl]),
        ]),
      ]),
      ui.h('div', { class: 'step' }, [
        ui.h('div', { class: 'step-title', text: '② 选择文件并导入' }),
        ui.h('div', { class: 'step-body' }, [
          ui.h('div', { class: 'file-row' }, [fileInput, fileHint]),
          ui.h('div', { class: 'file-row' }, [ui.h('span', { class: 'hint', text: '导入模式' }), modeSel]),
          ui.h('div', { class: 'hint', text:
            '`rule_code` 可留空——留空则由服务端按 R{场景码}{3位序号} 生成（BR-06-01）；'
            + '`condition` 列填条件树 JSON 文本（AD-07）。' }),
          errBox,
          ui.h('div', { class: 'file-row' }, [btnImport]),
        ]),
      ]),
      ui.h('div', { class: 'step' }, [
        ui.h('div', { class: 'step-title', text: '③ 导入结果' }),
        ui.h('div', { class: 'step-body' }, [resultWrap]),
      ]),
    ]),
  });
}

// ==================== 组装 ====================
function buildToolbar() {
  const fScene = ui.selectBox(
    optionsOf('rule_scenes', { includeAll: { value: '', label: '全部场景' } }),
    state.scene,
  );
  const fStatus = ui.selectBox([
    { value: '', label: '全部状态' },
    { value: 'enabled', label: '启用' },
    { value: 'disabled', label: '停用' },
  ], state.status);
  const fKeyword = ui.inputBox({ value: state.keyword, placeholder: '搜索规则编码 / 名称', onEnter: doSearch });

  function doSearch() {
    state.scene = fScene.value;
    state.status = fStatus.value;
    state.keyword = fKeyword.value.trim();
    state.page = 1;
    pushQuery();
    load({ showSkeleton: true });
  }
  function doReset() {
    fScene.value = ''; fStatus.value = ''; fKeyword.value = '';
    state.scene = ''; state.status = ''; state.keyword = ''; state.page = 1;
    pushQuery();
    load({ showSkeleton: true });
  }
  // §2.2.1：输入防抖 300ms（不按回车也能筛）
  fKeyword.addEventListener('input', () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, 300);
  });
  fScene.addEventListener('change', doSearch);
  fStatus.addEventListener('change', doSearch);

  const actions = [refs.asOf];
  if (isWriter()) {
    actions.push(ui.button('导入规则', { variant: 'ghost', onClick: () => openImportModal() }));
    actions.push(ui.button('＋ 新建规则', { variant: 'primary', onClick: createNew }));
  } else {
    actions.push(ui.h('span', { class: 'hint', text: '当前角色仅可查看规则（BR-06-35）' }));
  }

  return ui.toolbar([
    fScene, fStatus, fKeyword,
    ui.button('查询', { onClick: doSearch }),
    ui.button('重置', { onClick: doReset }),
  ], actions);
}

export async function render(container) {
  await ensureEnums();       // 中文标签与场景/算子取值域来自服务端（BR-00-18 / D24）
  await ensureFeatureMeta(); // field 下拉的 18 项特征白名单（BR-06-16）

  readQuery();
  refs = {};
  refs.banners = ui.h('div');
  refs.hintWrap = ui.h('div');
  refs.tableWrap = ui.h('div');
  refs.asOf = ui.h('span', { class: 'hint' });
  refs.pageInfo = ui.h('span');
  refs.btnPrev = ui.button('上一页', { onClick: () => { if (state.page > 1) { state.page -= 1; pushQuery(); load(); } } });
  refs.btnNext = ui.button('下一页', { onClick: () => { state.page += 1; pushQuery(); load(); } });

  ui.mount(container,
    cfgTabs(HASH),
    ui.card(null, [
      buildToolbar(),
      refs.banners,
      refs.hintWrap,
      refs.tableWrap,
      ui.h('div', { class: 'pager' }, [refs.pageInfo, refs.btnPrev, refs.btnNext]),
      // 原型 `ruleTblX` 下方注记（Spec §2.2.2 原文，逐字保留）
      ui.h('div', { class: 'hint', text:
        '开关启停用后立即失效规则/名单缓存，对后续事件即时生效（AD-02）。系统内置规则只能停用、不能删除。' }),
    ]),
  );

  await load({ showSkeleton: true });
}
