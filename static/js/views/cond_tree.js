/* 条件树构建器 `condTree`（模块 06-B Spec §2.3 / BR-06-14~19）
 *
 * ## 本文件**不含任何结构校验算法**
 *
 * BR-06-17 明令：条件树的结构校验只有一处实现——模块 05 的
 * `app/engine/condition.py::validate_tree()`，06 只调用、不复制。因此这里的
 * 校验职责被劈成两半，且都是**诚实**的：
 *
 *   ① `localIssues()` 只做「空值 / 未选字段」这种零成本短路（Spec §2.3 原文：
 *      "本地检查**不是**结构校验的第二实现，只是'未填完就不必发请求'的前置短路"）。
 *      它**刻意不判**空的条件组、嵌套深度、多余键——那些一律交给服务端，
 *      否则就会出现"前端拦住了一个服务端本来能接受的结构"，用户无从绕过。
 *   ② `validate()` 把整棵树 POST 给 `/api/v1/rules/validate-tree`（服务端转调
 *      `validate_tree()`），把 `errors[{path, code, message}]` 按 `path`
 *      定位回**具体的节点行**并高亮（Spec §2.3「校验错误回显」）。
 *
 * ## 数据模型与后端 Pydantic 模型同构（BR-06-14 / AD-07）
 *
 *   非叶：`{logic:'and'|'or', children:[...]}`
 *   叶：  `{field, op, value}`，其中 `exists` **不接受 `value`**（连键都不能有）
 *
 * ## 为什么输入控件不受控 + 结构改动前必须 `syncAll()`
 *
 * 值输入框是"边打字边看"的，若每次按键都重建整棵树，光标会跳走。所以：值/字段/算子
 * 的改动**就地更新模型**，只有"增删节点"才整树重渲染；重渲染前先 `syncAll()` 把
 * DOM 里的值刷回模型，保证用户刚敲的内容不会因为点了一次「＋ 添加条件」而消失。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import { optionsOf } from '../store.js';

const FEATURE_URL = `${api.API_PREFIX}/features/meta`;
const VALIDATE_URL = `${api.API_PREFIX}/rules/validate-tree`;

// `exists` 问的是"有没有值"，再给一个比较值是自相矛盾（服务端 `condition.py` 同款裁定）
export const NO_VALUE_OPS = ['exists'];
// 多值算子：右侧是数组
export const MULTI_VALUE_OPS = ['in', 'not_in'];

const OPS_FALLBACK = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'exists', 'contains'];

/** 算子下拉选项：**取值域来自服务端枚举 `condition_op`**（BR-06-15），前端不另立一套。 */
export function opOptions() {
  const list = optionsOf('condition_op');
  if (list.length) return list;
  // 字典没加载出来时的兜底：至少让下拉可用；标签退化成取值本身
  return OPS_FALLBACK.map((v) => ({ value: v, label: v }));
}

// ==================== 特征元数据（BR-06-16）====================
// `field` 只能从 04 特征引擎公布的 18 项里选，因此**必须**是下拉而不是自由文本输入：
// 引用一个不存在的特征名不会报错，只会让规则永远不命中（最难排查的形态）。
let features = null;
let featuresInflight = null;
let featuresError = '';

export async function ensureFeatureMeta() {
  if (features) return features;
  if (!featuresInflight) {
    featuresInflight = api.get(FEATURE_URL, null, null)
      .then((data) => {
        const items = Array.isArray(data) ? data : ((data && data.items) || []);
        features = items.filter((it) => it && it.key);
        featuresError = features.length ? '' : '特征清单为空（模块 04 未返回任何特征键）';
        return features;
      })
      .catch((e) => {
        featuresError = e.message || String(e);
        features = [];
        return features;
      })
      .finally(() => { featuresInflight = null; });
  }
  return featuresInflight;
}

export function featureMetaReady() { return features; }
export function featureMetaError() { return featuresError; }

function featureOf(key) {
  return (features || []).find((f) => String(f.key) === String(key)) || null;
}

/**
 * 特征下拉（按 `group` 分组，18 项一眼能扫完）。
 * 编辑一条引用了未知特征键的历史规则时，把该键作为一个额外选项保留下来——
 * 否则打开抽屉会把用户看不明白的键**静默换掉**（那等于替用户改规则）。
 */
function fieldSelect(node, onChange) {
  const sel = ui.h('select', { onchange: () => { node.field = sel.value; if (onChange) onChange(); } });
  const groups = [];
  (features || []).forEach((f) => {
    const g = String(f.group || '其他');
    let bucket = groups.find((x) => x.name === g);
    if (!bucket) { bucket = { name: g, items: [] }; groups.push(bucket); }
    bucket.items.push(f);
  });
  sel.appendChild(ui.h('option', { value: '', text: '请选择特征…', selected: !node.field ? 'selected' : null }));
  groups.forEach((g) => {
    const og = ui.h('optgroup', { label: g.name });
    g.items.forEach((f) => og.appendChild(ui.h('option', {
      value: f.key, text: `${f.label}（${f.key}）`,
      selected: String(node.field) === String(f.key) ? 'selected' : null,
    })));
    sel.appendChild(og);
  });
  if (node.field && !featureOf(node.field)) {
    sel.appendChild(ui.h('optgroup', { label: '当前规则引用的未知特征键（模块 04 清单里没有）' }, [
      ui.h('option', { value: node.field, text: node.field, selected: 'selected' }),
    ]));
  }
  sel.classList.add('cond-field');
  return sel;
}

function opSelect(node, onChange) {
  const sel = ui.selectBox(opOptions(), node.op, (v) => { node.op = v; if (onChange) onChange(); });
  sel.classList.add('cond-op');
  return sel;
}

function valueText(node) {
  if (node.value === undefined || node.value === null) return '';
  if (Array.isArray(node.value)) return node.value.join(',');
  return String(node.value);
}

/** 值控件按 `field` 的类型切换（数值框 / 开关 / 多值输入），`exists` 则没有值。
 *
 * `token` 是"这一个控件还活着吗"的代次牌：`rebuildValue()` 换掉控件后，
 * 旧控件的监听器必须**永久失声**——浏览器在移除已聚焦输入框时会补发一次
 * `change`，那个闭包引用的是同一个 node 对象，会把刚被删掉的值写回去
 * （表现为"切到 exists 再切回来，旧值自己冒出来"，见 rebuildValue 的注释）。
 */
function valueControl(node, token) {
  const live = () => !token || token.gen() === token.my;
  if (NO_VALUE_OPS.includes(node.op)) {
    return ui.h('span', { class: 'hint cond-novalue', text: 'exists 只判断“有没有值”，不需要比较值' });
  }
  const meta = featureOf(node.field);
  const type = String((meta && meta.data_type) || 'str');
  let el;
  if (MULTI_VALUE_OPS.includes(node.op)) {
    el = ui.h('input', { type: 'text', value: valueText(node),
      placeholder: '多个值用逗号分隔，如 vip,gold' });
  } else if (type === 'bool') {
    el = ui.selectBox([
      { value: 'true', label: '是（true）' }, { value: 'false', label: '否（false）' },
    ], valueText(node) || 'true', (v) => { if (live()) node.value = v; });
  } else if (type === 'int' || type === 'float') {
    el = ui.h('input', { type: 'number', value: valueText(node),
      step: type === 'float' ? '0.01' : '1',
      placeholder: type === 'float' ? '如 0.5' : '如 5' });
  } else {
    el = ui.h('input', { type: 'text', value: valueText(node), placeholder: '比较值' });
  }
  el.classList.add('cond-value');
  // 打字就地写回模型（不重建 DOM，光标不跳）
  const writeBack = () => {
    if (!live()) return;                 // 已被新控件取代：它的 change 一律丢弃
    if (el.isConnected === false) return; // 已从文档摘除：不再回写
    node.value = el.value;
  };
  el.addEventListener('input', writeBack);
  el.addEventListener('change', writeBack);
  return el;
}

// ==================== 树模型辅助 ====================
let uidSeq = 0;
const isBranch = (n) => !!(n && n.logic);

export function newLeaf() {
  uidSeq += 1;
  return { _uid: `n${uidSeq}`, field: '', op: 'eq', value: '' };
}

export function newBranch(logic = 'and', children = null) {
  uidSeq += 1;
  return {
    _uid: `n${uidSeq}`, logic,
    children: children || [],
  };
}

/** 从服务端（或历史数据）的 JSON 树构造编辑器模型；坏数据也不抛，退化成单个空叶子。 */
export function fromJson(raw) {
  const walk = (n) => {
    if (!n || typeof n !== 'object') return newLeaf();
    if (n.logic) {
      const children = Array.isArray(n.children) ? n.children.map(walk) : [];
      const b = newBranch(n.logic === 'or' ? 'or' : 'and', children);
      return b;
    }
    uidSeq += 1;
    const leaf = { _uid: `n${uidSeq}`, field: n.field || '', op: n.op || 'eq', value: n.value };
    if (NO_VALUE_OPS.includes(leaf.op)) delete leaf.value;
    else if (leaf.value === undefined) leaf.value = '';
    return leaf;
  };
  const model = walk(raw);
  return isBranch(model) ? model : newBranch('and', [model]);
}

/** 把编辑器模型序列化成**提交给服务端**的 JSON（`_uid` 等编辑器专用键一律不出现）。 */
export function toJson(node) {
  if (isBranch(node)) {
    return { logic: node.logic, children: node.children.map(toJson) };
  }
  const out = { field: node.field, op: node.op };
  // `exists` 连 `value` 这个键都不能有（`{"value": null}` 的含义是"拿 null 去比较"）
  if (!NO_VALUE_OPS.includes(node.op)) out.value = coerce(node);
  return out;
}

function coerce(node) {
  const meta = featureOf(node.field);
  const type = String((meta && meta.data_type) || 'str');
  const one = (v) => {
    const s = String(v).trim();
    // 空串**不能**转成 0：`Number('') === 0` 会让只读 JSON 预览显示 `"value": 0`，
    // 而输入框里明明是空的——预览与实际输入不一致是这类编辑器最坏的形态。
    // 空值原样提交，由本地短路检查提示"必须填写比较值"，服务端也会拒绝。
    if (s === '') return '';
    if (type === 'bool') return s === 'true' || s === '1';
    if (type === 'int' || type === 'float') {
      const n = Number(s);
      return Number.isFinite(n) ? n : s;   // 非数字原样提交，让服务端给权威判断
    }
    return s;
  };
  const raw = node.value;
  if (MULTI_VALUE_OPS.includes(node.op)) {
    const arr = Array.isArray(raw) ? raw.map(String) : String(raw === undefined || raw === null ? '' : raw).split(',');
    return arr.map((s) => s.trim()).filter((s) => s !== '').map(one);
  }
  return one(raw === undefined || raw === null ? '' : raw);
}

/** 节点路径与后端 `_path_of()` 的渲染规则一致：`children[1].children[0].op`。 */
export function pathOf(node) {
  return node && node._path ? node._path : '';
}

/** 把 `children[1].children[0].op` 折成节点自身的路径 `children[1].children[0]`。 */
export function nodePathOf(errPath) {
  let p = String(errPath || '');
  while (p && !p.endsWith(']')) {
    const i = p.lastIndexOf('.');
    p = i >= 0 ? p.slice(0, i) : '';
  }
  return p;
}

// ==================== 编辑器 ====================
/**
 * 创建一棵可编辑的条件树。
 * @param {{onChange?:Function}} opts
 * @returns 编辑器句柄
 */
export function createCondTree({ onChange } = {}) {
  const root = newBranch('and', [newLeaf()]);
  const el = ui.h('div', { class: 'cond-tree' });
  const summary = ui.h('div', { class: 'cond-errors' });

  const notify = () => { if (onChange) onChange(); };

  /** DOM → 模型：结构变化前先把用户输入刷回去，避免"点一下就白填了"。 */
  function syncAll() {
    el.querySelectorAll('[data-uid]').forEach((row) => {
      const node = findNode(root, row.dataset.uid);
      if (!node) return;
      const f = row.querySelector('.cond-field');
      const o = row.querySelector('.cond-op');
      const v = row.querySelector('.cond-value');
      if (f) node.field = f.value;
      if (o) node.op = o.value;
      if (v) node.value = v.value;
    });
  }

  function findNode(n, uid) {
    if (!n) return null;
    if (n._uid === uid) return n;
    if (isBranch(n)) {
      for (const c of n.children) {
        const hit = findNode(c, uid);
        if (hit) return hit;
      }
    }
    return null;
  }

  function parentOf(n, target) {
    if (!isBranch(n)) return null;
    for (const c of n.children) {
      if (c === target) return n;
      const hit = parentOf(c, target);
      if (hit) return hit;
    }
    return null;
  }

  function removeNode(target) {
    const p = parentOf(root, target);
    if (!p) return;   // 根节点不可删（Spec §2.3）
    p.children = p.children.filter((c) => c !== target);
    render();
    notify();
  }

  const smallBtn = (label, onClick, cls = '') => {
    const b = ui.button(label, { onClick });
    b.classList.add('btn-sm');
    if (cls) b.classList.add(cls);
    return b;
  };

  function renderNode(node, path, depth) {
    node._path = path;
    if (isBranch(node)) {
      const head = ui.h('div', { class: 'cond-branch-head' }, [
        ui.h('span', { class: 'hint', text: '满足以下' }),
        ui.selectBox([{ value: 'and', label: '全部（AND）' }, { value: 'or', label: '任一（OR）' }],
          node.logic, (v) => { node.logic = v; notify(); }),
        smallBtn('＋ 添加条件', () => { syncAll(); node.children.push(newLeaf()); render(); notify(); }),
        smallBtn('＋ 添加条件组', () => { syncAll(); node.children.push(newBranch('or')); render(); notify(); }),
      ]);
      // 根节点不可删（Spec §2.3 原文）
      if (depth > 0) head.appendChild(smallBtn('删除该组', () => removeNode(node), 'danger'));
      return ui.h('div', {
        class: 'cond-branch', dataset: { uid: node._uid, path },
      }, [
        head,
        node.children.length
          ? ui.h('div', { class: 'cond-children' },
            node.children.map((c, i) => renderNode(c, path ? `${path}.children[${i}]` : `children[${i}]`, depth + 1)))
          : ui.h('div', { class: 'hint', text: '该条件组内还没有条件（服务端会判定它不合法）' }),
      ]);
    }

    // ---- 叶子 ----
    const valueWrap = ui.h('span', { class: 'cond-value-wrap' });
    // 代次牌：每次重建值控件都自增，旧控件的监听器因此知道自己已经"过期"
    let valueGen = 0;
    const rebuildValue = () => {
      valueGen += 1;
      const token = { my: valueGen, gen: () => valueGen };
      ui.mount(valueWrap, valueControl(node, token));
      const v = valueWrap.querySelector('.cond-value');
      // 只有**当前**控件的事件才算数（旧控件的 input/change 一律忽略）
      if (v) v.addEventListener('input', () => { if (valueWrap.querySelector('.cond-value') === v) notify(); });
    };
    const fsel = fieldSelect(node, () => { rebuildValue(); notify(); });
    const osel = opSelect(node, () => {
      if (NO_VALUE_OPS.includes(node.op)) delete node.value;
      else if (node.value === undefined) node.value = '';
      rebuildValue();
      notify();
    });
    rebuildValue();
    return ui.h('div', { class: 'cond-leaf', dataset: { uid: node._uid, path } }, [
      fsel, osel, valueWrap,
      smallBtn('删除', () => removeNode(node), 'danger'),
    ]);
  }

  function render() {
    // 每次重渲染都重算路径（增删后下标会变，路径必须跟着变，否则错误定位会错行）
    ui.mount(el, renderNode(root, '', 0));
  }

  function clearErrors() {
    el.querySelectorAll('.is-invalid').forEach((n) => n.classList.remove('is-invalid'));
    el.querySelectorAll('.cond-msg').forEach((n) => n.remove());
    ui.clear(summary);
  }

  /**
   * 本地短路检查：只看"未选字段 / 未填值"。**故意不判**空条件组、深度、多余键——
   * 那些由服务端的 `validate_tree()` 裁决（BR-06-17）。
   */
  function localIssues() {
    syncAll();
    const out = [];
    const walk = (n, path) => {
      if (isBranch(n)) {
        n.children.forEach((c, i) => walk(c, path ? `${path}.children[${i}]` : `children[${i}]`));
        return;
      }
      if (!n.field) out.push({ path, uid: n._uid, message: '请选择特征字段（只能从 18 项特征里选）' });
      if (!NO_VALUE_OPS.includes(n.op)) {
        const empty = n.value === undefined || n.value === null || String(n.value).trim() === '';
        if (empty) out.push({ path, uid: n._uid, message: `算子 ${n.op} 必须填写比较值` });
      }
    };
    walk(root, '');
    return out;
  }

  /**
   * 按 `path` 定位并高亮错误（Spec §2.3）。
   * 路径可能指向节点本身（`children[1]`）或节点内的键（`children[1].op`），
   * 因此先把键名折掉再找节点行；找不到节点的错误仍会出现在顶部汇总里（不静默丢弃）。
   */
  function highlight(errors) {
    clearErrors();
    const list = Array.isArray(errors) ? errors : [];
    list.forEach((e) => {
      const np = nodePathOf(e && e.path);
      const row = el.querySelector(`[data-path="${String(np).replace(/"/g, '\\"')}"]`);
      if (row) {
        row.classList.add('is-invalid');
        row.appendChild(ui.h('span', { class: 'cond-msg', text: `⚠ ${(e && e.message) || '不合法'}` }));
        let up = row.parentElement;
        while (up && up !== el) { up.classList.add('has-error'); up = up.parentElement; }
      }
    });
    if (list.length) {
      ui.mount(summary, [
        ui.h('div', { class: 'cond-error-title', text: `条件树有 ${list.length} 处问题` }),
        ui.h('div', { class: 'cond-error-list' }, list.map((e) => ui.h('div', {
          text: `${e && e.path ? e.path : '（根节点）'}：${(e && e.message) || '不合法'}`,
        }))),
      ]);
    }
    return list.length;
  }

  /**
   * 调服务端校验（BR-06-17：**这是唯一的校验入口**）。
   * @returns {Promise<{valid:boolean, errors:Array, normalized:object|null, unavailable?:Error}>}
   */
  async function validate(tree) {
    const cond = tree || toJson(root);
    try {
      const data = await api.request(VALIDATE_URL, { method: 'POST', body: { condition: cond }, silent: true });
      const d = data || {};
      return {
        valid: d.valid !== false,
        errors: Array.isArray(d.errors) ? d.errors : [],
        normalized: d.normalized === undefined ? null : d.normalized,
      };
    } catch (e) {
      // 服务端把"结构非法"映射成 400 CFG-4003（内含 errors[]）；这与"校验接口本身不可用"
      // 是两件事，必须分开——否则接口一挂，用户会看到一堆假的"条件树问题"。
      if (e && e.code === 'CFG-4003') {
        const errs = (e.data && Array.isArray(e.data.errors)) ? e.data.errors : [];
        return {
          valid: false,
          errors: errs.length ? errs : [{ path: '', message: e.message || '条件树结构非法' }],
          normalized: null,
        };
      }
      return { valid: true, errors: [], normalized: null, unavailable: e };
    }
  }

  render();

  return {
    el, summary,
    getRoot: () => root,
    getTree: () => { syncAll(); return toJson(root); },
    setTree: (raw) => {
      const next = fromJson(raw);
      root.logic = next.logic;
      root.children = next.children;
      render();
    },
    localIssues, highlight, clearErrors, validate, syncAll,
    refreshFeatures: () => { render(); },
  };
}
