/* 名单库视图（模块 06，阶段一已交付；由模块 00 的单页壳接线）
 *
 * 职责边界：只做「取数 → 渲染 → 提交」，不含任何业务规则判断。唯一性、脱敏、
 * 默认有效期、权限全部由后端裁决，前端只负责把错误码翻译成人话。
 *
 * 左侧「策略与规则」菜单进入本页；模块 06-A 在此基础上补规则的 tab，
 * 模块 06-B 补规则编辑器。
 *
 * 06-A 交互补充（本次）：
 *   - 表格「操作」列：移除（仅 active 且 source != auto）/ auto 来源的灰字引导（BR-06-27）
 *   - 移除二次确认弹窗：实体值/名单类型/来源/加入时间/关联案件数 + 影响面提示（BR-06-26）
 *   - 批量导入弹窗：下载模板 → 选文件 → 导入结果与错误明细下载（BR-06-29）
 *   - 只读角色（无 list:write）三个写入口一律不渲染（BR-06-35）
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import * as session from '../session.js';
import { ensureEnums, labelOf, optionsOf } from '../store.js';
import { cfgTabs, rememberQuery } from './cfg_tabs.js';

export const meta = { title: '策略与规则', crumb: '策略与规则 / 名单库' };

// 本页是「策略与规则」页的**第二个标签**（Spec §2.1：`tabList` → `#/lists`）。
// 06-B 交付后 `#/rules` 改为默认标签「规则配置」，名单库迁到 `#/lists`（不进左侧菜单）。
const HASH = '#/lists';

// 前缀统一取 api.API_PREFIX（模块 00 约定），不在视图里另行硬编码 /api/v1
const LIST_URL = `${api.API_PREFIX}/lists`;
const IMPORT_URL = `${LIST_URL}/import`;
const TEMPLATE_URL = `${LIST_URL}/import-template`;
const IMPACT_URL = `${LIST_URL}/impact`;
const TYPE_TABS = ['black', 'white', 'gray'];

// 视图状态放模块级：在菜单间来回切换时保留筛选条件与页码（体验要求，§2.4）
const state = {
  listType: 'black',
  entityType: '',
  status: 'active',
  keyword: '',
  page: 1,
  pageSize: 20,
  total: 0,
  pages: 0,
  counts: { black: 0, white: 0, gray: 0 },
};

let inflight = null;   // 在途列表请求（§3.5：新请求先取消旧的，避免旧响应覆盖新结果）
let refs = null;       // 当前挂载的 DOM 引用
// 最近一次**成功**渲染出的内容节点（表格或空态）。
// 为什么需要它：§3.5 要求加载中显示骨架屏，而骨架屏会顶掉当前表格；
// 若此时请求失败，就必须把上次的成功结果放回去——否则用户会以为"数据没了"。
// 只记住节点、不记住数据，是为了避免"两份数据源"（节点即当时的事实）。
let lastRendered = null;

export function dispose() {
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  refs = null;
}

function isWriter() {
  // 权限来自后端 /auth/me 下发的 permissions（BR-01-12），前端不按角色推导；
  // 无权限时按钮**不渲染**（BR-01-14），而不是渲染出来再等 403
  return session.has('list:write');
}

function hasActiveFilter() {
  return !!(state.entityType || state.keyword || state.status !== 'active');
}

function fmtTime(ms) {
  return ms === null || ms === undefined ? '永久' : ui.fmtTime(ms);
}

// ---- 展示辅助 ----

// 来源中文标签：优先取服务端枚举字典（BR-00-18：前端不另立一套枚举），
// 字典里没有该分组时退回 Spec §2.4.3 固定的三档文案，避免表格里出现裸的 manual/auto/import
const SOURCE_TEXT = { manual: '人工', auto: '处置自动', import: '批量导入' };

function sourceText(src) {
  const hit = optionsOf('list_source').find((o) => String(o.value) === String(src));
  if (hit) return hit.label;
  return SOURCE_TEXT[src] || src || '—';
}

// 条目主键：契约里是 entry_id（DELETE 响应也回显它）。兼容 _id/id 只是防御性读取，
// 不改变任何提交语义；若三者都拿不到，宁可不渲染移除按钮，也不去猜一个 URL 出来。
function entryId(it) {
  if (it.entry_id !== undefined && it.entry_id !== null) return it.entry_id;
  if (it._id !== undefined && it._id !== null) return it._id;
  if (it.id !== undefined && it.id !== null) return it.id;
  return null;
}

// 加入时间：列表默认排序是 effective_at desc，故它就是"加入时间"；缺该字段的老数据退回 created_at
function joinTimeMs(it) {
  if (it.effective_at !== undefined && it.effective_at !== null) return it.effective_at;
  if (it.created_at !== undefined && it.created_at !== null) return it.created_at;
  return null;
}

/** 触发一次本地下载（模板与错误明细共用，省得两处各写一遍 a.click 与 revoke）。 */
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** 从 Content-Disposition 取后端建议的文件名；取不到就用调用方给的兜底名。 */
function attachmentName(res, fallback) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  if (!m) return fallback;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

// 失败响应是统一响应包（JSON），成功响应是文件流，所以不能无条件 res.json()
async function errorText(res) {
  const env = await res.json().catch(() => null);
  if (env && env.code) return `[${env.code}] ${env.message}`;
  return `服务返回了非标准响应（HTTP ${res.status}）`;
}

function csvCell(v) {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function errorRowsCsv(rows) {
  const head = ['行号', '实体值', '原因'].join(',');
  const body = rows.map((r) => [r.row, r.entity_value, r.reason].map(csvCell).join(','));
  return [head].concat(body).join('\r\n');
}

function fmtSize(bytes) {
  if (typeof bytes !== 'number') return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// 弹窗内联错误：用 class 控制显隐，不写内联样式（颜色也只走 CSS 变量）
function showInlineError(box, msg) {
  box.textContent = msg;
  box.classList.add('on');
}

function hideInlineError(box) {
  box.textContent = '';
  box.classList.remove('on');
}

function kv(label, value) {
  return ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: label }),
    value instanceof Node ? value : ui.h('span', { class: 'v', text: String(value) }),
  ]);
}

function step(title, children) {
  return ui.h('div', { class: 'step' }, [
    ui.h('div', { class: 'step-title', text: title }),
    ui.h('div', { class: 'step-body' }, children),
  ]);
}

// ==================== 渲染 ====================
function renderTabs() {
  ui.mount(refs.tabs, ui.tabs(
    TYPE_TABS.map((t) => ({ value: t, label: `${labelOf('list_type', t)} ${state.counts[t] || 0}` })),
    state.listType,
    (v) => { state.listType = v; state.page = 1; load(); },
  ));
}

function renderTable(items) {
  if (!items.length) {
    lastRendered = ui.empty(
      hasActiveFilter() ? '当前筛选条件下暂无名单条目' : `${labelOf('list_type', state.listType)}暂无条目`,
      hasActiveFilter() ? '可点「重置」清空筛选条件'
        : (isWriter() ? '点击右上角「＋ 新增名单」创建第一条' : '当前角色仅可查看（BR-06-35）'),
    );
    ui.mount(refs.tableWrap, lastRendered);
    return;
  }
  const columns = [
    { name: '名单类型', width: 96 }, { name: '实体类型', width: 96 },
    { name: '实体值', width: 200 }, { name: '加入原因' },
    { name: '来源', width: 88 }, { name: '失效时间', width: 150 },
    { name: '状态', width: 88 }, { name: '操作人', width: 110 },
    { name: '操作', width: 170 },
  ];
  const rows = items.map((it) => [
    ui.tag(labelOf('list_type', it.list_type), it.list_type),
    labelOf('entity_type', it.entity_type),
    ui.h('span', { class: 'mono', text: it.entity_value }),
    it.reason || '—',
    sourceText(it.source),
    fmtTime(it.expire_at),
    ui.tag(labelOf('list_status', it.status), 'plain'),
    it.operator || '—',
    opCell(it),
  ]);
  lastRendered = ui.table(columns, rows);
  ui.mount(refs.tableWrap, lastRendered);
}

/**
 * 「操作」列（BR-06-26/27/35）。
 * - 已失效/已移除 → 无可用操作
 * - source=auto → **不给移除按钮**：这类条目是案件处置链路写进来的，直接删会切断
 *   处置侧留痕（BR-06-27），只能回 08 处置模块处理，所以这里只给一句灰字引导
 * - 无 list:write → 不渲染写按钮（BR-06-35：隐藏不替代后端鉴权，后端仍逐接口校验）
 */
function opCell(it) {
  if (it.status !== 'active') return '—';
  if (it.source === 'auto') {
    return ui.h('span', { class: 'cell-note', text: '由案件处置自动写入，请到案件处置模块处理' });
  }
  if (!isWriter()) return '—';
  if (!entryId(it)) return '—';
  const btn = ui.button('移除', { variant: 'danger', onClick: () => openRemoveModal(it) });
  btn.classList.add('btn-sm');   // 表格行内按钮比工具条按钮小一号
  return btn;
}

function renderPager() {
  const pages = state.pages || (state.total ? Math.ceil(state.total / state.pageSize) : 0);
  refs.pageInfo.textContent = `共 ${state.total} 条 · 第 ${state.page} / ${Math.max(1, pages)} 页`;
  refs.btnPrev.disabled = state.page <= 1;
  refs.btnNext.disabled = pages === 0 || state.page >= pages;
}

function showError(msg) {
  ui.mount(refs.banners, ui.banner('err', msg));
}

function showOk(msg) {
  ui.mount(refs.banners, ui.banner('ok', msg));
}

function clearBanners() {
  ui.clear(refs.banners);
}

// ==================== 取数 ====================
async function load({ showSkeleton = false } = {}) {
  clearBanners();
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;

  if (showSkeleton) ui.mount(refs.tableWrap, ui.loading(6));

  try {
    const data = await api.get(LIST_URL, {
      list_type: state.listType,
      status: state.status,
      entity_type: state.entityType,
      keyword: state.keyword,
      page: state.page,
      page_size: state.pageSize,
      sort: 'effective_at:desc',
    }, signal);
    state.total = data.total;
    state.pages = data.pages || 0;
    state.counts = data.counts || state.counts;
    renderTabs();
    renderTable(data.items || []);
    renderPager();
    refs.asOf.textContent = `数据截至 ${ui.fmtTime(data.as_of, '—')}`;
  } catch (e) {
    if (e.name === 'AbortError') return;   // 已被更新的请求取代，静默丢弃
    // 失败态：把上次成功渲染的内容放回去（§3.5：报错不得让用户以为"数据没了"）
    ui.mount(refs.tableWrap, lastRendered || ui.empty('加载失败', '可点「查询」重试'));
    showError(`名单加载失败：${e.message}（trace ${typeof e.shortTrace === 'function' ? e.shortTrace() : '—'}）。已保留上次结果，可点「查询」重试。`);
  } finally {
    if (inflight && inflight.signal === signal) inflight = null;
  }
  await refreshDegraded();
}

async function refreshDegraded() {
  try {
    const data = await api.get(`${LIST_URL}/scene-usage`, null, null);
    if (data && data.degraded) {
      const el = ui.banner('warn',
        `名单服务处于降级状态（自 ${ui.fmtTime(data.degraded_since)}）：` +
        `${data.last_error || '未知原因'}。新增黑名单可能未生效，请重试。`);
      refs.banners.appendChild(el);
    }
  } catch (e) {
    // 降级状态查询失败不阻断主流程：它只是横幅，不该把可用页面变成错误页
  }
}

// ==================== 新增 ====================
function localExpireToMs() {
  const v = refs.nExpireAt.value;
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isNaN(t) ? null : t;
}

async function create() {
  clearBanners();
  const payload = {
    list_type: refs.nListType.value,
    entity_type: refs.nEntityType.value,
    entity_value: refs.nEntityValue.value.trim(),
    reason: refs.nReason.value.trim(),
    expire_at: localExpireToMs(),
    force: refs.nForce.checked,
  };
  // 前端只做"必填"这种零成本校验，其余（长度、枚举、唯一性）一律交给后端，
  // 避免同一套规则在前端再实现一遍而产生分歧
  if (!payload.entity_value) return showError('实体值不能为空');
  if (!payload.reason) return showError('加入原因不能为空');

  ui.buttonBusy(refs.btnCreate, true, '保存中');
  try {
    const created = await api.post(LIST_URL, payload);
    showOk(`新增成功：${created.entity_value}（${labelOf('list_type', created.list_type)}）`);
    refs.nEntityValue.value = '';
    refs.nReason.value = '';
    refs.nExpireAt.value = '';
    refs.nForce.checked = false;
    state.listType = created.list_type;
    state.page = 1;
    await load();
  } catch (e) {
    showError(`新增失败：${e.message}`);
  } finally {
    ui.buttonBusy(refs.btnCreate, false);
  }
}

// ==================== 移除（BR-06-26/27） ====================
/** 关联案件数：soft 字段，取不到返回 null（页面显示 —）。 */
async function lookupImpact(it) {
  try {
    const data = await api.request(IMPACT_URL, {
      query: { entity_type: it.entity_type, entity_value: it.entity_value },
      silent: true,   // 它只是弹窗里的一个提示项，查失败不该弹全局错误
    });
    const n = data ? data.active_count : null;
    return typeof n === 'number' ? n : null;
  } catch (e) {
    return null;
  }
}

function removeErrorText(e) {
  // §5.1 里这三个码的处置方式完全不同，必须分别给人话，不能只甩 message
  if (e.code === 'CFG-4032') return '该条目由处置自动写入，请到处置模块处理。';
  if (e.code === 'CFG-4006') return '状态已被他人改变，请刷新后重试。';
  if (e.code === 'CFG-5003') return '审计写入失败已回滚，可重试。';
  if (e.code === 'CFG-4011') return '名单条目不存在或已被移除。';
  return `移除失败：${e.message}`;
}

function openRemoveModal(it) {
  const id = entryId(it);
  const impact = ui.h('span', { class: 'v', text: '查询中…' });
  const errBox = ui.h('div', { class: 'form-error' });

  ui.modal({
    title: '移除名单条目',
    confirmText: '确认移除',
    variant: 'danger',   // 破坏性操作按 §5.2 用危险色
    body: [
      // §5.2 要求逐项展示：实体值 / 名单类型 / 来源 / 加入时间 / 关联案件数
      ui.h('div', { class: 'kv-list' }, [
        kv('实体值', ui.h('span', { class: 'mono', text: it.entity_value || '—' })),
        kv('名单类型', labelOf('list_type', it.list_type)),
        kv('来源', sourceText(it.source)),
        kv('加入时间', ui.fmtTime(joinTimeMs(it), '—')),
        kv('关联案件数', impact),
      ]),
      ui.h('div', { class: 'impact-note',
        text: '移除后该实体将不再被拦截或放行（本模块会主动失效缓存，实际即时生效）。' }),
      errBox,
    ],
    onConfirm: async () => {
      try {
        // silent：失败原因要显示在弹窗内的 errBox 里，再让全局 toast 报一遍就是重复噪音
        await api.request(`${LIST_URL}/${encodeURIComponent(id)}`, { method: 'DELETE', silent: true });
      } catch (e) {
        // 失败时必须返回 false：ui.modal 只有拿到"不是 false"才关闭弹窗，
        // 关掉的话用户根本看不到 4032/4006/5003 的区别
        showInlineError(errBox, removeErrorText(e));
        // 4006（状态已被他人改变）/4011（已被移除）说明页面上这份列表已经过期：
        // 后台静默重载一次，用户关掉弹窗时看到的就是最新状态
        if (e.code === 'CFG-4006' || e.code === 'CFG-4011') load();
        return false;
      }
      ui.toast(`已移除名单条目：${it.entity_value}`, 'ok');
      load();
      return true;
    },
  });

  // 关联案件数是提示项而不是前置条件：查不到就显示 —，**绝不禁用移除**
  lookupImpact(it).then((n) => { impact.textContent = n === null ? '—' : `${n} 件`; });
}

// ==================== 批量导入（BR-06-28/29/30） ====================
async function downloadTemplate(btn) {
  ui.buttonBusy(btn, true, '下载中');
  try {
    // 模板接口返回的是**文件流**，不是统一响应包，所以不能走 api.request（它会 res.json()）；
    // 但令牌仍必须放在请求头里：window.open 带不了 Authorization 头，
    // 而把 token 拼进 URL 会进浏览器历史、代理与访问日志（与 audit.js 的 doExport 同一理由）
    const res = await fetch(TEMPLATE_URL, {
      headers: { Authorization: `Bearer ${session.getToken()}` },
    });
    if (!res.ok) throw new Error(await errorText(res));
    saveBlob(await res.blob(), attachmentName(res, 'list_import_template.csv'));
    ui.toast('模板已下载，请按表头填写后再导入', 'ok');
  } catch (e) {
    ui.toast(`模板下载失败：${e.message}`, 'err', 6000);
  } finally {
    ui.buttonBusy(btn, false);
  }
}

function importErrorText(e) {
  if (e.code === 'CFG-4012') return '文件为空或表头不匹配，请使用下载的模板。';
  if (e.code === 'CFG-5002') return '名单写入失败，已进入降级状态，请重试。';
  return `导入失败：${e.message}`;
}

function importStat(label, value) {
  return ui.h('div', { class: 'import-stat' }, [
    ui.h('span', { class: 'hint', text: label }),
    ui.h('span', { class: 'import-stat-v',
      text: value === undefined || value === null ? '0' : String(value) }),
  ]);
}

function renderImportResult(wrap, data) {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const nodes = [
    ui.h('div', { class: 'import-counts' }, [
      importStat('总行数', data.total), importStat('成功', data.success), importStat('失败', data.failed),
    ]),
    ui.h('div', { class: 'hint',
      text: `导入模式：${data.mode === 'atomic' ? 'atomic（整批写入）' : 'partial（成功行入库）'}` }),
  ];
  if (rows.length) {
    nodes.push(ui.table(
      [{ name: '行号', width: 64 }, { name: '实体值', width: 170 }, { name: '原因' }],
      rows.map((r) => [
        r.row === undefined || r.row === null ? '—' : String(r.row),
        ui.h('span', { class: 'mono', text: r.entity_value || '—' }),
        r.reason || '—',
      ]),
    ));
    if (typeof data.failed === 'number' && data.failed > rows.length) {
      nodes.push(ui.h('div', { class: 'hint',
        text: `服务端每批最多回传 200 条错误明细，此处展示其中 ${rows.length} 条（其余行已计入失败数）。` }));
    }
    const btnDl = ui.button('下载错误明细 CSV', {
      variant: 'ghost',
      onClick: () => {
        // 前端用 Blob 就地生成：明细已在手上，再跑一趟接口既不必要也多一次审计面
        saveBlob(new Blob([`\uFEFF${errorRowsCsv(rows)}`], { type: 'text/csv;charset=utf-8' }),
          `list_import_errors_${Date.now()}.csv`);
        ui.toast('错误明细已下载', 'ok');
      },
    });
    btnDl.classList.add('btn-sm');
    nodes.push(ui.h('div', { class: 'file-row' }, [btnDl]));
  } else if (!data.failed) {
    nodes.push(ui.banner('ok', '所有行均导入成功，无错误明细。'));
  }
  ui.mount(wrap, nodes);
}

function openImportModal() {
  let picked = null;
  const fileInput = ui.h('input', { type: 'file', accept: '.csv' });
  const fileHint = ui.h('span', { class: 'hint', text: '尚未选择文件（仅支持 .csv）' });
  const modeSel = ui.selectBox([
    { value: 'partial', label: 'partial（成功行入库，失败行报明细）' },
    { value: 'atomic', label: 'atomic（任一行失败则整批不写）' },
  ], 'partial');
  const forceCb = ui.h('input', { type: 'checkbox' });
  const errBox = ui.h('div', { class: 'form-error' });
  const resultWrap = ui.h('div');
  const btnTpl = ui.button('下载模板', { variant: 'ghost', onClick: () => downloadTemplate(btnTpl) });
  const btnImport = ui.button('开始导入', { variant: 'primary', onClick: () => runImport() });

  fileInput.addEventListener('change', () => {
    picked = fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
    fileHint.textContent = picked
      ? `已选：${picked.name}（${fmtSize(picked.size)}）`
      : '尚未选择文件（仅支持 .csv）';
    if (picked) hideInlineError(errBox);
  });

  function setBusy(busy) {
    ui.buttonBusy(btnImport, busy, '导入中');
    // 导入期间锁住输入项：同一批文件被点两次会写两遍库、两遍审计（BR-06-36 一次操作只留一条痕）
    fileInput.disabled = busy;
    modeSel.disabled = busy;
    forceCb.disabled = busy;
    btnTpl.disabled = busy;
  }

  async function runImport() {
    if (!picked) {
      showInlineError(errBox, '请先选择要导入的 CSV 文件。');
      return;
    }
    hideInlineError(errBox);
    setBusy(true);
    const form = new FormData();
    form.append('file', picked);
    form.append('mode', modeSel.value);
    form.append('force', forceCb.checked ? 'true' : 'false');
    try {
      // postForm 内部仍走 api.request：401 清登录态、403 不跳页、统一响应包解析都照旧；
      // 这里 silent 是因为失败原因要就地显示在 errBox，不必再 toast 一次
      const data = await api.postForm(IMPORT_URL, form, { silent: true });
      renderImportResult(resultWrap, data);
      ui.toast(`导入完成：共 ${data.total} 行，成功 ${data.success} 行，失败 ${data.failed} 行`,
        data.failed ? 'warn' : 'ok', 5000);
      load();   // 新入库的条目要立刻出现在列表与各 tab 计数里
    } catch (e) {
      showInlineError(errBox, importErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  const body = ui.h('div', { class: 'import-box' }, [
    step('① 下载模板', [
      ui.h('div', { class: 'hint',
        text: '模板列：list_type, entity_type, entity_value, reason, expire_at（含示例行与枚举注释），请勿改动表头。' }),
      ui.h('div', { class: 'file-row' }, [btnTpl]),
    ]),
    step('② 选择文件', [
      ui.h('div', { class: 'file-row' }, [fileInput, fileHint]),
      ui.h('div', { class: 'file-row' }, [ui.h('span', { class: 'hint', text: '导入模式' }), modeSel]),
      ui.h('label', { class: 'check-line' }, [forceCb,
        document.createTextNode('force：已存在的同实体条目按 force 语义处理（允许跨名单类型覆盖）')]),
      errBox,
      // 提交按钮放在弹窗体内而不是用 modal 的确认键：导入完成后确认键还能再点一次，
      // 那样会把同一批文件重复提交一遍（重复写库、重复审计）
      ui.h('div', { class: 'file-row' }, [btnImport]),
    ]),
    step('③ 导入结果', [resultWrap]),
    ui.h('div', { class: 'hint', text: '单次最多 5000 行（BR-06-30）；导入的审计留痕由服务端写入。' }),
  ]);

  // 确认键只做"关闭"：真正的提交按钮在弹窗内（开始导入），这样导入完成后
  // 不会因为再点一次确认键而重复提交同一批文件
  ui.modal({ title: '批量导入名单', body, confirmText: '关闭', variant: 'ghost' });
}

// ==================== 组装 ====================
function buildForm() {
  const entityOptions = optionsOf('entity_type');
  const listTypeOptions = optionsOf('list_type');
  refs.nListType = ui.selectBox(listTypeOptions, 'black');
  refs.nEntityType = ui.selectBox(entityOptions, 'device');
  refs.nEntityValue = ui.inputBox({ placeholder: '如 D8F2A1C4 / 13900000001', maxlength: 128 });
  refs.nReason = ui.inputBox({ placeholder: '如：羊毛党设备聚集，已确认批量套券', maxlength: 200 });
  refs.nExpireAt = ui.h('input', { type: 'datetime-local' });
  refs.nForce = ui.h('input', { type: 'checkbox' });

  const field = (label, node, required = false, span = 1) => {
    const f = ui.h('div', { class: 'field', style: span > 1 ? `grid-column:span ${span}` : null }, [
      ui.h('label', {}, [document.createTextNode(label), required ? ui.h('span', { class: 'req', text: ' *' }) : null]),
      node,
    ]);
    return f;
  };

  refs.btnCreate = ui.button('保存', { variant: 'primary', onClick: create });
  return ui.card('新增名单条目', [
    ui.h('div', { class: 'form-grid' }, [
      field('名单类型', refs.nListType, true),
      field('实体类型', refs.nEntityType, true),
      field('实体值', refs.nEntityValue, true),
      field('加入原因', refs.nReason, true, 2),
      field('失效时间（留空 = 默认）', refs.nExpireAt),
    ]),
    ui.h('div', { class: 'form-actions' }, [
      refs.btnCreate,
      ui.button('取消', { variant: 'ghost', onClick: () => { refs.formWrap.style.display = 'none'; } }),
      ui.h('label', { class: 'hint' }, [refs.nForce, document.createTextNode(' force（允许同实体跨名单类型并存）')]),
    ]),
    ui.h('div', { class: 'hint' }, [
      document.createTextNode('默认有效期：黑/白名单永久；灰名单 30 天（BR-06-22）。phone 维度由服务端强制脱敏为 '),
      ui.h('code', { text: '139****0001' }),
      document.createTextNode(' 后入库（BR-06-21）。'),
    ]),
  ], { soft: true });
}

export async function render(container) {
  await ensureEnums();   // 中文标签来自服务端，不在前端硬编码

  refs = {};
  refs.tabs = ui.h('div');
  refs.banners = ui.h('div', { class: 'view' });
  refs.tableWrap = ui.h('div');
  refs.asOf = ui.h('span', { class: 'hint' });
  refs.pageInfo = ui.h('span');
  refs.btnPrev = ui.button('上一页', { onClick: () => { if (state.page > 1) { state.page -= 1; load(); } } });
  refs.btnNext = ui.button('下一页', { onClick: () => { state.page += 1; load(); } });

  const fEntity = ui.selectBox(
    optionsOf('entity_type', { includeAll: { value: '', label: '全部实体类型' } }),
    state.entityType, (v) => { state.entityType = v; },
  );
  const fStatus = ui.selectBox(
    optionsOf('list_status', { includeAll: { value: 'all', label: '全部状态' } }),
    state.status, (v) => { state.status = v; },
  );
  // list_status 的中文标签是「生效中/已过期/已移除」，直接复用服务端文案
  Array.from(fStatus.options).forEach((o) => {
    if (o.value !== 'all') o.textContent = labelOf('list_status', o.value, o.textContent);
  });
  const fKeyword = ui.inputBox({
    value: state.keyword, placeholder: '搜索实体值（手机号自动脱敏匹配）', onEnter: doSearch,
  });

  function doSearch() {
    state.entityType = fEntity.value;
    state.status = fStatus.value;
    state.keyword = fKeyword.value.trim();
    state.page = 1;
    load({ showSkeleton: true });
  }
  function doReset() {
    fEntity.value = '';
    fStatus.value = 'active';
    fKeyword.value = '';
    state.entityType = '';
    state.status = 'active';
    state.keyword = '';
    state.page = 1;
    load({ showSkeleton: true });
  }

  refs.formWrap = buildForm();
  refs.formWrap.style.display = 'none';

  const toggleForm = ui.button('＋ 新增名单', {
    variant: 'primary',
    onClick: () => {
      refs.formWrap.style.display = refs.formWrap.style.display === 'none' ? 'flex' : 'none';
    },
  });
  // 批量导入与新增并列、同风格（ghost 次级 + primary 主操作，主操作放最右）
  const openImport = ui.button('批量导入', { variant: 'ghost', onClick: () => openImportModal() });
  // BR-06-34/35：无写权限的角色**不渲染**写按钮，而不是点下去再报 403
  const actions = [refs.asOf];
  if (isWriter()) actions.push(openImport, toggleForm);
  else actions.push(ui.h('span', { class: 'hint', text: '当前角色仅可查看名单（BR-06-35）' }));

  ui.mount(container,
    cfgTabs(HASH),
    refs.tabs,
    ui.card(null, [
      ui.toolbar(
        [
          fEntity, fStatus, fKeyword,
          ui.button('查询', { onClick: doSearch }),
          ui.button('重置', { onClick: doReset }),
        ],
        actions,
      ),
      refs.formWrap,
      refs.banners,
      refs.tableWrap,
      ui.h('div', { class: 'pager' }, [refs.pageInfo, refs.btnPrev, refs.btnNext]),
    ]),
  );

  renderTabs();
  await load({ showSkeleton: true });
}
