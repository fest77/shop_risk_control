/* 规则场景 tab（模块 06 Spec §2.5 / BR-06-17）
 *
 * 只读字典：`_id`（场景码）/ `name` / `event_types` / `sort`。**页面不提供增删改**
 * （场景是 E06 的数据行，由种子与模块 05 维护；D24 明确前后端都不得把场景写死在代码里）。
 *
 * 数据来源有两处，且必须如实区分：
 *   ① `GET /api/v1/rule-scenes`（**拥有者是模块 05**，Spec §3.3）——含 event_types 与 sort；
 *   ② `/api/v1/common/enums` 的 `rule_scenes` 组——只有场景码与名称（D24 的下发通道）。
 * ①不可用时**不静默**：页面明确写出"降级到 ②、缺哪两列"，避免读者以为场景没有事件类型。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import { ensureEnums, optionsOf } from '../store.js';
import { cfgTabs } from './cfg_tabs.js';

export const meta = { title: '策略与规则', crumb: '策略与规则 / 规则场景' };

const SCENE_URL = `${api.API_PREFIX}/rule-scenes`;
const HASH = '#/scenes';

export function dispose() {}

export async function render(container) {
  await ensureEnums();

  const holder = ui.h('div');
  const note = ui.h('div');
  const banner = ui.h('div');

  ui.mount(container,
    cfgTabs(HASH),
    ui.card(null, [
      ui.h('div', { class: 'hint', text: '场景为系统内置字典，仅供规则归属使用（BR-06-17：本页不提供增删改）。' }),
      banner,
      note,
      holder,
    ]),
  );

  ui.mount(holder, ui.loading(5));

  let rows = null;
  let failMsg = '';
  try {
    // silent：本页对①的缺席有明确的降级文案，再弹一次全局 toast 只是噪音
    const data = await api.request(SCENE_URL, { silent: true });
    rows = Array.isArray(data) ? data : ((data && data.items) || []);
  } catch (e) {
    failMsg = e && e.message ? e.message : String(e);
  }

  if (rows && rows.length) {
    renderTable(holder, rows.map((r) => ({
      code: String(r._id || r.scene_code || ''),
      name: r.name || '—',
      eventTypes: Array.isArray(r.event_types) ? r.event_types.join('、') : (r.event_types || '—'),
      sort: r.sort === undefined || r.sort === null ? '—' : String(r.sort),
    })));
    ui.mount(note, ui.h('div', { class: 'hint',
      text: `共 ${rows.length} 个场景（来源：GET /api/v1/rule-scenes，模块 05 拥有）。` }));
    return;
  }

  // ①不可用（或返回空）→ 退回枚举字典，并**明确写出缺哪两列**
  const fallback = optionsOf('rule_scenes').map((o) => ({
    code: String(o.value), name: o.label, eventTypes: '—', sort: '—',
  }));
  if (fallback.length) {
    ui.mount(banner, ui.banner('warn',
      `场景字典接口（GET /api/v1/rule-scenes，模块 05 提供）暂不可用${failMsg ? `：${failMsg}` : '（返回为空）'}；`
      + '下表取自 /api/v1/common/enums 的 rule_scenes 组（D24 的下发通道），**只有场景码与名称**，'
      + 'event_types 与 sort 两列暂无数据来源。'));
    renderTable(holder, fallback);
    return;
  }

  ui.mount(holder, ui.empty('暂无规则场景', failMsg
    ? `场景字典接口不可用（${failMsg}），且 /common/enums 的 rule_scenes 组为空：请先执行 scripts/seed.py 灌入 E06 场景数据。`
    : '请先执行 scripts/seed.py 灌入 E06 场景数据。'));
}

function renderTable(holder, rows) {
  ui.mount(holder, ui.table(
    [{ name: '场景码', width: 110 }, { name: '场景名称', width: 150 }, { name: '覆盖事件类型' }, { name: '排序', width: 70 }],
    rows.map((r) => [
      ui.h('span', { class: 'mono', text: r.code }),
      r.name,
      r.eventTypes,
      r.sort,
    ]),
  ));
}
