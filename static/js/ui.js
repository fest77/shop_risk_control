/* 公共组件（零依赖，模块 00 §2.2）
 *
 * 全部用 DOM API 构造而不是拼 innerHTML 字符串：本系统的表格里会渲染
 * 「加入原因」「处置备注」等用户输入文本，拼字符串就等于给自己开了 XSS 口子。
 * 用 textContent 赋值是默认安全的选择。
 */

/**
 * 把任意嵌套的 节点/字符串/数组 铺平成一维 DOM 节点列表。
 *
 * **这个函数是为修一个真实缺陷而加的**：最初的 `mount()` 直接把参数 appendChild，
 * 而调用方很自然地写成 `ui.mount(el, items.map(...))`（传数组）。`appendChild(数组)`
 * 会抛 TypeError，且因为 `clear()` 已经先执行，表现为"容器被清空后再也没填回来"——
 * 页面只剩静态骨架、菜单空白。`node --check` 与接口 curl 都发现不了这类问题，
 * 只有真实浏览器会暴露，因此这里统一铺平，让"传数组"成为受支持用法。
 */
function flatten(x, out = []) {
  if (x === undefined || x === null || x === false || x === true) return out;
  if (Array.isArray(x)) {
    x.forEach((item) => flatten(item, out));
    return out;
  }
  if (typeof x === 'string' || typeof x === 'number') {
    out.push(document.createTextNode(String(x)));
    return out;
  }
  out.push(x);
  return out;
}

/** 创建元素。props 里 text 走 textContent（安全），其余走属性。 */
export function h(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (v === undefined || v === null) return;
    if (k === 'text') el.textContent = String(v);
    else if (k === 'class') el.className = v;
    else if (k === 'style') el.setAttribute('style', v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  });
  flatten(children).forEach((c) => el.appendChild(c));
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** 拼装：把若干节点/字符串/数组依次塞进容器（自动铺平）。 */
export function mount(container, ...nodes) {
  clear(container);
  flatten(nodes).forEach((n) => container.appendChild(n));
  return container;
}

export function card(title, children, { soft = false } = {}) {
  const cls = 'card' + (soft ? ' card-soft' : '');
  const kids = [];
  if (title) kids.push(h('h3', { text: title }));
  kids.push(...flatten(children));
  return h('div', { class: cls }, kids);
}

export function statCard(title, value, unit = '') {
  return h('div', { class: 'stat-card' }, [
    h('div', { class: 'stat-title', text: title }),
    h('div', { class: 'stat-value' }, [
      h('span', { text: value === null || value === undefined ? '—' : String(value) }),
      unit ? h('span', { class: 'stat-unit', text: unit }) : null,
    ]),
  ]);
}

/** 工具条：左筛选器组 + 右操作按钮组（space-between）。 */
export function toolbar(leftNodes, rightNodes) {
  // h() 会铺平子节点数组，无需再手动转换
  return h('div', { class: 'toolbar' }, [
    h('div', { class: 'filters' }, leftNodes),
    h('div', { class: 'filters' }, rightNodes),
  ]);
}

const VARIANTS = ['primary', 'danger', 'warn', 'ok', 'ghost', 'plain'];

export function button(label, { variant = 'ghost', onClick, disabled = false, title = '' } = {}) {
  if (!VARIANTS.includes(variant)) throw new Error(`未知按钮样式：${variant}`);
  return h('button', {
    class: variant, text: label, title, disabled: disabled ? 'disabled' : null,
    onclick: onClick,
  });
}

/** 按钮内联转圈（异步操作期间用，替换文案）。 */
export function buttonBusy(btn, busy, busyText = '处理中') {
  if (busy) {
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    mount(btn, h('span', { class: 'spinner' }), document.createTextNode(busyText));
  } else {
    btn.disabled = false;
    mount(btn, document.createTextNode(btn.dataset.label || '提交'));
  }
}

export function inputBox({ value = '', placeholder = '', type = 'text', maxlength, onEnter } = {}) {
  const input = h('input', { type, value, placeholder, maxlength });
  if (onEnter) {
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') onEnter(input.value); });
  }
  return input;
}

export function selectBox(options, value, onChange) {
  const sel = h('select', { onchange: (e) => onChange && onChange(e.target.value) });
  options.forEach((o) => {
    sel.appendChild(h('option', { value: o.value, text: o.label,
      selected: String(o.value) === String(value) ? 'selected' : null }));
  });
  return sel;
}

export function tag(label, kind = '') {
  return h('span', { class: `tag ${kind}`.trim(), text: label });
}

/** 风险等级徽标：高=红、中=橙、低=绿（§2.2）。 */
export function riskTag(level, label) {
  const text = label || { high: '高风险', medium: '中风险', low: '低风险' }[level] || level;
  const cls = { high: 'high', medium: 'medium', low: 'low' }[level] || '';
  return h('span', { class: `tag ${cls}`.trim(), text });
}

/**
 * 表格。columns: [{name, width?, align?}]；rows 支持两种形态：
 *   - 单元格数组：`[[cell, cell], ...]`
 *   - 现成的 `<tr>` 元素：需要**展开行**（一行表头 + 一行详情、colspan 跨列）时用，
 *     例如审计页点击"变更摘要"后插入一条 diff 详情行
 * 两种混用也可以。
 */
export function table(columns, rows) {
  const thead = h('thead', {}, h('tr', {}, columns.map((c) => {
    const th = h('th', { text: c.name });
    if (c.width) th.style.width = `${c.width}px`;
    if (c.align) th.style.textAlign = c.align;
    return th;
  })));
  const tbody = h('tbody', {}, rows.map((cells) => {
    // 已经是 <tr>：直接采用（早期只支持数组，导致"传 <tr> 就报 cells.map is not a function"）
    if (cells instanceof Node) return cells;
    return h('tr', {}, cells.map((cell) => {
      const td = h('td');
      if (cell instanceof Node) td.appendChild(cell);
      else {
        const text = cell === null || cell === undefined ? '—' : String(cell);
        td.textContent = text;
        td.title = text;
      }
      return td;
    }));
  }));
  return h('table', {}, [thead, tbody]);
}

export function empty(title, hint = '') {
  return h('div', { class: 'empty' }, [
    h('div', { class: 'empty-title', text: title }),
    hint ? h('div', { class: 'hint', text: hint }) : null,
  ]);
}

/** 列表骨架屏（§2.2 loading）。 */
export function loading(rows = 5) {
  const widths = ['100%', '92%', '96%', '88%', '94%'];
  return h('div', { class: 'skeleton' }, Array.from({ length: rows }, (_, i) =>
    h('div', { class: 'skeleton-row', style: `width:${widths[i % widths.length]}` })));
}

export function banner(kind, message) {
  return h('div', { class: `banner ${kind}`, text: message });
}

export function tabs(items, activeValue, onSelect) {
  return h('div', { class: 'tabs' }, items.map((it) =>
    h('div', {
      class: 'tab' + (String(it.value) === String(activeValue) ? ' active' : ''),
      text: it.label,
      onclick: () => onSelect(it.value),
    })));
}

// ==================== toast ====================
let toastRoot = null;

function ensureToastRoot() {
  if (!toastRoot) {
    toastRoot = document.getElementById('toastRoot');
    if (!toastRoot) {
      toastRoot = h('div', { class: 'toast-root', id: 'toastRoot' });
      document.body.appendChild(toastRoot);
    }
  }
  return toastRoot;
}

/** 右上角浮层，3 秒自动消失；ok / warn / err 三态。message 支持可信 HTML 片段。 */
export function toast(message, kind = 'ok', timeout = 3000) {
  const cls = { success: 'ok', ok: 'ok', warning: 'warn', warn: 'warn', error: 'err', err: 'err' }[kind] || '';
  const el = h('div', { class: `toast ${cls}`.trim() });
  // 仅允许调用方有意传入的 <span class="toast-trace">，其余走 textContent
  if (typeof message === 'string' && message.includes('<span class="toast-trace">')) {
    el.innerHTML = message;
  } else {
    el.textContent = String(message);
  }
  ensureToastRoot().appendChild(el);
  setTimeout(() => el.remove(), timeout);
  return el;
}

// ==================== modal ====================
/**
 * 遮罩 + 居中卡片。ESC 关闭；**点击遮罩不关闭**（破坏性操作防误触，§2.2）。
 */
export function modal({ title, body, confirmText = '确认', cancelText = '取消',
                       variant = 'primary', onConfirm, onCancel }) {
  const root = document.getElementById('modalRoot');
  const close = () => {
    document.removeEventListener('keydown', onKey);
    clear(root);
  };
  const onKey = (e) => { if (e.key === 'Escape') { close(); if (onCancel) onCancel(); } };

  const confirmBtn = button(confirmText, {
    variant,
    onClick: async () => {
      if (!onConfirm) return close();
      const ok = await onConfirm();
      if (ok !== false) close();
    },
  });
  const cancelBtn = button(cancelText, { variant: 'ghost', onClick: () => { close(); if (onCancel) onCancel(); } });
  const box = h('div', { class: 'modal' }, [
    title ? h('h3', { text: title }) : null,
    // h() 内部会铺平，字符串自动变文本节点，无需在这里手动包 createTextNode
    h('div', { class: 'modal-body' }, body),
    h('div', { class: 'modal-actions' }, [cancelBtn, confirmBtn]),
  ]);
  // 遮罩上不绑定关闭事件：破坏性操作必须显式点按钮或按 ESC
  mount(root, h('div', { class: 'modal-mask' }, box));
  document.addEventListener('keydown', onKey);
  // 额外把三个句柄交出去（**向后兼容**：既有调用方只忽略返回值，行为一字未变）。
  // 模块 08 的处置弹窗需要它们：禁用确认按钮（令牌过期 / 提交中）、
  // 给取消按钮打 `data-role` 供自动化点击，以及判断弹窗是否还在文档里。
  return { close, confirmBtn, cancelBtn, el: box };
}

// ==================== 时间 ====================
const pad = (n) => String(n).padStart(2, '0');

export function fmtTime(ms, placeholder = '—') {
  if (ms === null || ms === undefined || ms === '') return placeholder;
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return placeholder;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 相对时间（大盘事件流用）。 */
export function fmtAgo(ms) {
  if (!ms) return '—';
  const diff = Date.now() - Number(ms);
  if (diff < 0) return fmtTime(ms);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return fmtTime(ms);
}

// ==================== 本地 vendor 加载 ====================
const loadedScripts = new Map();

/** 按需加载本地脚本（零 CDN，BR-00-20）。 */
export function loadScript(src) {
  if (loadedScripts.has(src)) return loadedScripts.get(src);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error(`静态资源加载失败：${src}`));
    document.head.appendChild(s);
  });
  loadedScripts.set(src, p);
  return p;
}

/** 加载本地 ECharts（D-01：不从 CDN 取）。 */
export function loadECharts() {
  return loadScript('/static/vendor/echarts.min.js').then(() => window.echarts);
}
