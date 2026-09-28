/* 统一格式化（模块 02 §6 文件规划 + BR-02-03）
 *
 * **这是全站唯一的格式化实现处**：百分比保留 1 位小数、金额「分 → 元」并千分位、
 * 整数千分位、时间串，全部只在这里写一遍。模块 02/07/12/13 的列表页必须复用本模块，
 * 不允许各自再写一份——两份实现必然在某次改动后漂移（一个四舍五入、一个截断），
 * 于是同一个数在两个页面上显示成两个值。
 *
 * 三条与 Spec 直接相关的约定：
 *   · 只做**展示形态**的转换，不做任何业务计算（BR-02-01：比率/均值/排行都由模块 11 算好）；
 *   · 金额以「分」传输，前端负责 ÷100 转元（BR-02-02）；
 *   · `null` / `undefined` / `NaN` **一律显示 `—`，绝不显示 0**：Spec §5 明确要求区分
 *     「真的没有（空态）」与「取数失败/无样本（null）」——把 null 渲染成 0 就是静默撒谎。
 */

/** 空值占位符（Spec §5：不得用 0 冒充"没有数据"）。 */
export const NA = '—';

/** 是否是可用于展示的有限数。 */
export function isNum(v) {
  return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
}

/** 千分位整数。`null` → `—`。 */
export function int(v) {
  if (!isNum(v)) return NA;
  return Math.round(Number(v)).toLocaleString('en-US');
}

/**
 * 分 → 元并千分位（BR-02-02）。
 *
 * `3580000`（分）→ `35,800`（元）。**必须 ÷100**：分的定义就是元的 1/100，
 * 少除或多除一个数量级都会让"预估挽回资损"这个对外数字错 10 倍。
 * （Spec §3.1 的示例 `3580000 → 3,580 元` 与分/元定义不自洽，按定义实现，
 *  已在交付报告中如实登记。）
 *
 * @param {number|null} fen 金额（分）
 * @param {object} [opts] { digits: 保留小数位，默认 0 }
 */
export function yuan(fen, { digits = 0 } = {}) {
  if (!isNum(fen)) return NA;
  const y = Number(fen) / 100;
  return y.toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/** 分 → 元的数值（图表用；无值返回 null，让 ECharts 走 null 而不是 0）。 */
export function yuanValue(fen) {
  return isNum(fen) ? Number(fen) / 100 : null;
}

/** 百分比，保留 1 位小数（BR-02-03）。`null` → `—`（分母为 0 时后端给 null）。 */
export function pct(v, { digits = 1, sign = false } = {}) {
  if (!isNum(v)) return NA;
  const n = Number(v);
  const s = n.toFixed(digits);
  return `${sign && n > 0 ? '+' : ''}${s}%`;
}

/** 百分比数值（图表轴用）；`null` 原样返回，让折线断开而不是落到 0。 */
export function pctValue(v) {
  return isNum(v) ? Number(v) : null;
}

/**
 * 定点小数（供"数值 + 单位"分离展示的卡片用）。
 *
 * 卡片的原型是「数值 `4.7` + 单位 `%`」两个元素，不能直接用 `pct()`（它会带上 %），
 * 但小数位数必须与 `pct()` 完全一致（BR-02-03：百分比保留 1 位小数，全站统一）。
 */
export function fixed(v, digits = 1) {
  return isNum(v) ? Number(v).toFixed(digits) : NA;
}

const pad = (n) => String(n).padStart(2, '0');

const toDate = (ts) => {
  if (!isNum(ts)) return null;
  const d = new Date(Number(ts));
  return Number.isNaN(d.getTime()) ? null : d;
};

/** `HH:mm:ss`（事件流「时间」列，Spec §2.4）。 */
export function clock(ts, placeholder = NA) {
  const d = toDate(ts);
  if (!d) return placeholder;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** `MM-DD HH:mm`（趋势图 X 轴：跨天时必须带日期，否则 24 小时的图看起来像同一天）。 */
export function monthDayHour(ts, placeholder = '') {
  const d = toDate(ts);
  if (!d) return placeholder;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`;
}

/** `HH:mm`（同一自然日内只显示时分）。 */
export function hourMinute(ts, placeholder = '') {
  const d = toDate(ts);
  if (!d) return placeholder;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `YYYY-MM-DD HH:mm:ss`（导出快照与 tooltip 用）。 */
export function dateTime(ts, placeholder = NA) {
  const d = toDate(ts);
  if (!d) return placeholder;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * 趋势图的 X 轴刻度标签。
 *
 * 粒度由**后端**回传（BR-02-04，前端不自选）；这里只按粒度决定标签密度：
 * `1m`/`1h` 只显示时分，`1d` 显示月日。24 个点全打标签会挤成一团，
 * 因此按 `step` 抽样打点（第 1 个、最后 1 个必打）。
 */
export function axisLabel(ts, granularity, { index = 0, total = 1 } = {}) {
  const step = granularity === '1d' ? 1
    : total > 16 ? Math.ceil(total / 8) : (total > 8 ? 2 : 1);
  const last = index === total - 1;
  if (index % step !== 0 && !last) return '';
  return granularity === '1d' ? monthDayHour(ts).slice(0, 5) : hourMinute(ts);
}

/** 决策 → 中文标签（走 `/common/enums`，前端不硬编码；缺失时回退原值）。 */
export const DECISION_KIND = { pass: 'low', review: 'medium', reject: 'high' };
