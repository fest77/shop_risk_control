/* 「风险等级分布」环形图配置（模块 02 §2.3）
 *
 * 三项占比 low / medium / high，中心显示近区间总量。
 *
 * ## 前端不算占比（BR-02-01）
 * 扇区值用后端下发的 `cnt`，文案里的占比用后端下发的 `ratio`——**一处除法都不做**。
 * 自己算一遍就会出现"两个页面同一份数据不同占比"的经典问题，而且 Spec 明确禁止。
 *
 * ## 配色只认主题变量（BR-02-22）
 * 三档语义色（红/橙/绿）与 app.css 的 `--risk-high/--risk-mid/--risk-low` 是同一套；
 * 在这里硬编码十六进制就等于把"换肤"这件事变成两处修改。
 */
import { int, pct } from '../format.js';

/** 空态判据：总量为 0（MET-5002：区间无数据是正常的，不是错误）。 */
export function isEmptyDistribution(data) {
  if (!data) return true;
  const total = Number(data.total);
  if (Number.isFinite(total) && total > 0) return false;
  return !((data.items || []).some((it) => Number(it.cnt) > 0));
}

function colorOf(key, c) {
  return { low: c.low, medium: c.medium, high: c.high }[String(key)] || c.brand;
}

/** 构造 option。data = DistributionOut `{dim, total, items[], stale}`。 */
export function buildDoughnutOption(data, c) {
  const items = (data && data.items) || [];
  const total = Number(data && data.total) || 0;
  const slices = items.map((it) => ({
    name: it.name || it.key,
    value: Number(it.cnt) || 0,
    itemStyle: { color: colorOf(it.key, c) },
    // 占比来自后端（`ratio` 是百分数），不在这里做除法
    ratio: it.ratio,
  }));

  return {
    animationDuration: 240,
    tooltip: {
      trigger: 'item',
      formatter: (p) => `${p.name}<br/>${int(p.value)} 件 · 占比 ${pct(p.data && p.data.ratio)}`,
    },
    // 中心总量用 graphic 而不是 title：title 会额外占一行高度，把 260px 的卡片挤掉环形图
    graphic: [
      {
        type: 'text', left: 'center', top: '38%', silent: true,
        style: { text: int(total), fontSize: 26, fontWeight: 700, fill: c.text, textAlign: 'center' },
      },
      {
        type: 'text', left: 'center', top: '54%', silent: true,
        style: { text: '近区间总量（件）', fontSize: 11, fill: c.dim, textAlign: 'center' },
      },
    ],
    legend: {
      bottom: 0, left: 'center', itemWidth: 10, itemHeight: 10,
      textStyle: { color: c.muted, fontSize: 11 },
      formatter: (name) => {
        const hit = slices.find((s) => s.name === name);
        return hit ? `${name} ${int(hit.value)}（${pct(hit.ratio)}）` : name;
      },
    },
    series: [{
      type: 'pie',
      radius: ['50%', '72%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      label: { show: false },
      labelLine: { show: false },
      // 白描边把三档分开：红橙两段相邻时，没有描边会糊成一片分不清边界
      itemStyle: { borderColor: '#FFFFFF', borderWidth: 2 },
      emphasis: { scale: true, scaleSize: 4, label: { show: false } },
      data: slices,
    }],
  };
}

/** 口径说明（占比值本身来自后端 `ratio`）。 */
export function doughnutCaption(data) {
  const dim = (data && data.dim) || 'level';
  return `来源 metric_buckets（bucket_type=${dim}）· 占比 ratio 由后端下发，前端不做计算`;
}
