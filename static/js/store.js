/* 服务端枚举缓存（BR-00-18：前后端共用单一来源）
 *
 * 前端**不得**再硬编码「黑名单 / 白名单」这类中文标签，一律通过
 * `labelOf(group, value)` 从 `/api/v1/common/enums` 取。这样改一个枚举
 * 只需要改后端 `enums.py`，不会出现"后端加了新取值、前端下拉框没有"的错位。
 */
import { get, API_PREFIX } from './api.js';

let cache = null;
let inflight = null;

/** 取全部枚举（进程内缓存；失败时返回空对象而不是抛错，保证页面还能渲染）。 */
export async function ensureEnums() {
  if (cache) return cache;
  if (!inflight) {
    inflight = get(`${API_PREFIX}/common/enums`)
      .then((data) => { cache = data || {}; return cache; })
      .catch(() => ({}))
      .finally(() => { inflight = null; });
  }
  return inflight;
}

export function enumsReady() {
  return cache;
}

/** 取某取值的中文标签；未加载或未定义时回退为原值。 */
export function labelOf(group, value, fallback = null) {
  const list = (cache && cache[group]) || [];
  const hit = list.find((o) => String(o.value) === String(value));
  if (hit) return hit.label;
  return fallback === null ? String(value) : fallback;
}

/** 取某组的选项（可直接喂给 ui.selectBox）。 */
export function optionsOf(group, { includeAll = null } = {}) {
  const list = ((cache && cache[group]) || []).map((o) => ({ ...o }));
  return includeAll ? [includeAll, ...list] : list;
}

/** 仅供测试/调试：清空缓存。 */
export function resetEnumCache() {
  cache = null;
  inflight = null;
}
