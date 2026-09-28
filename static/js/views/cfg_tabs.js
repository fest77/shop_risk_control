/* 「策略与规则」页的顶部标签条 `cfgTabs`（模块 06 Spec §2.1）
 *
 * 三个标签各自对应一个 hash 路由，**各自保留自己的筛选与页码**（写在 hash query 里）：
 *   tabRule  规则配置  #/rules   ← 默认页（模块 06-B 本次交付）
 *   tabList  名单库    #/lists   （模块 06-A 交付）
 *   tabScene 规则场景  #/scenes  （只读字典，BR-06-17）
 *
 * 为什么不用 `ui.tabs()`：`ui.tabs` 产出的 `.tabs` 类被名单位置条（黑/白/灰计数）
 * 占用，而既有的 E2E 断言 `text('.tabs')` 取的是**第一个** `.tabs` 节点并断言它含
 * 「黑名单 3」。两者共用同一个类名会让那条断言读到页面级标签条而翻掉。
 * 因此这里用独立的 `.cfg-tabs` / `.cfg-tab` 类：语义不同（页面级导航 vs 组内筛选），
 * 样式也应当不同（下划线式导航 vs 胶囊式筛选）。
 */
import * as ui from '../ui.js';

export const CFG_TABS = [
  { hash: '#/rules', label: '规则配置' },
  { hash: '#/lists', label: '名单库' },
  { hash: '#/scenes', label: '规则场景' },
];

/** 各标签最近一次的 hash query（模块级：只活在本次页面会话里，刷新即丢）。 */
const lastQuery = new Map();

/** 各视图在写入 hash query 后调用它，切回来时才能还原筛选与页码。 */
export function rememberQuery(hash, query = '') {
  if (!hash) return;
  lastQuery.set(hash, query ? String(query).replace(/^\?/, '') : '');
}

/** 当前 hash 的 query 串（不含 `?`），供视图初始化自己的状态。 */
export function currentQuery() {
  const i = String(location.hash || '').indexOf('?');
  return i >= 0 ? String(location.hash).slice(i + 1) : '';
}

/**
 * 渲染标签条。
 * @param {string} activeHash 当前高亮的 hash（如 `#/rules`）
 */
export function cfgTabs(activeHash) {
  const bar = ui.h('div', { class: 'cfg-tabs' });
  CFG_TABS.forEach((t) => {
    const remembered = lastQuery.get(t.hash) || '';
    bar.appendChild(ui.h('div', {
      class: 'cfg-tab' + (t.hash === activeHash ? ' active' : ''),
      text: t.label,
      dataset: { hash: t.hash },
      onclick: () => {
        if (t.hash === activeHash) return;   // 点当前标签不重载，保留已填筛选
        location.hash = remembered ? `${t.hash}?${remembered}` : t.hash;
      },
    }));
  });
  return bar;
}
