/* before/after 差异渲染（模块 12 §2.2 / BR-12-22）
 *
 * **变更摘要由前端派生**（BR-12-22 明确如此）：后端只返回结构化 before/after，
 * 因为"摘要文案"是会变的呈现细节，放后端会让每次文案调整都要动接口与测试。
 *
 * 差异三态与原型一致：新增=绿、删除=红、修改=黄。
 */
import * as ui from '../ui.js';

/** 把嵌套对象压平成 `路径 -> 值`，便于逐字段比对。 */
export function flatten(value, prefix = '', out = new Map()) {
  if (value === null || value === undefined || typeof value !== 'object') {
    out.set(prefix || '(根)', value === undefined ? null : value);
    return out;
  }
  if (Array.isArray(value)) {
    out.set(prefix || '(根)', value);
    return out;
  }
  const keys = Object.keys(value);
  if (!keys.length) {
    out.set(prefix || '(根)', {});
    return out;
  }
  keys.forEach((k) => {
    const path = prefix ? `${prefix}.${k}` : k;
    const v = value[k];
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) flatten(v, path, out);
    else out.set(path, v);
  });
  return out;
}

function show(value) {
  if (value === undefined) return '—';
  if (value === null) return 'null';
  if (typeof value === 'object') return JSON.stringify(value, null, 0);
  return String(value);
}

/**
 * 逐字段差异列表。
 * @returns {Array<{path, before, after, kind: 'add'|'del'|'chg'|'same'}>}
 */
export function diffEntries(before, after) {
  const b = flatten(before);
  const a = flatten(after);
  const paths = [...new Set([...b.keys(), ...a.keys()])];
  return paths.map((path) => {
    const hasB = b.has(path);
    const hasA = a.has(path);
    const bv = b.get(path);
    const av = a.get(path);
    let kind = 'same';
    if (!hasB && hasA) kind = 'add';
    else if (hasB && !hasA) kind = 'del';
    else if (show(bv) !== show(av)) kind = 'chg';
    return { path, before: bv, after: av, kind, hasBefore: hasB, hasAfter: hasA };
  });
}

/** 一行式变更摘要，如 `score 30→40；version 1→2`。 */
export function summarize(before, after) {
  const changed = diffEntries(before, after).filter((e) => e.kind !== 'same');
  if (!changed.length) return '—';
  return changed
    .map((e) => {
      if (e.kind === 'add') return `${e.path} 新增 ${show(e.after)}`;
      if (e.kind === 'del') return `${e.path} 删除 ${show(e.before)}`;
      return `${e.path} ${show(e.before)}→${show(e.after)}`;
    })
    .join('；');
}

const KIND_CLASS = { add: 'diff-add', del: 'diff-del', chg: 'diff-chg', same: '' };

/** 并排对比表：字段 | 变更前 | 变更后，按差异类型着色。 */
export function renderDiff(before, after) {
  const entries = diffEntries(before, after);
  const rows = entries.map((e) => ui.h('tr', { class: KIND_CLASS[e.kind] }, [
    ui.h('td', { class: 'mono', text: e.path }),
    ui.h('td', { class: 'mono', text: e.hasBefore ? show(e.before) : '—' }),
    ui.h('td', { class: 'mono', text: e.hasAfter ? show(e.after) : '—' }),
  ]));
  return ui.h('div', { class: 'diff-wrap' }, [
    ui.h('div', { class: 'hint', text:
      '差异图例：绿=新增、红=删除、黄=修改。字段路径用 `.` 表示嵌套层级。' }),
    ui.h('table', { class: 'diff-table' }, [
      ui.h('thead', {}, ui.h('tr', {}, [
        ui.h('th', { text: '字段' }),
        ui.h('th', { text: '变更前 (before)' }),
        ui.h('th', { text: '变更后 (after)' }),
      ])),
      ui.h('tbody', {}, rows),
    ]),
    entries.length === 0
      ? ui.h('div', { class: 'hint', text: '该记录没有 before/after 快照（非变更类动作）。' })
      : null,
  ]);
}
