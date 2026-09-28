/* 「规则命中排行榜」横向柱状图配置（模块 02 §2.4 + BR-02-16）
 *
 * Top10，点击柱子 → `#/rules?code=<rule_code>`。
 *
 * ## 前端不重排（BR-02-01）
 * `items` 已由后端按 `hit_cnt` 降序给好，`rank` 也是后端给的。这里**一次 sort 都不做**：
 * 横向柱状图的第 1 名要显示在最上面，靠的是 `yAxis.inverse = true`（坐标轴方向），
 * 而不是把数组反转——反转数组虽然视觉结果一样，但从此"界面顺序"与"后端顺序"之间
 * 多了一层前端逻辑，下次有人加了并列排序规则就会对不上。
 */
import { int } from '../format.js';

/** 空态判据：没有任何命中（区间内规则未被命中是正常的，不是错误）。 */
export function isEmptyRanking(data) {
  return !((data && data.items) || []).length;
}

/** 构造 option。data = RuleRankingOut `{range, metric_field, items[], stale}`。
 *  `onPick(rule_code, rank)` 由视图注入（点击柱子下钻）。 */
export function buildRankingOption(data, c) {
  const items = (data && data.items) || [];
  const codes = items.map((it) => it.rule_code);
  return {
    animationDuration: 240,
    // 名称较长（RAFTER001 之类）时，tooltip 里带上后端给的规则名，图上只放编码避免压字
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = Array.isArray(ps) ? ps[0] : ps;
        const it = items[p.dataIndex] || {};
        return `#${it.rank || p.dataIndex + 1} ${it.rule_name || it.rule_code}<br/>` +
          `命中 ${int(it.hit_cnt)} 次` +
          (it.hit_ratio === null || it.hit_ratio === undefined ? '' : ` · 占比 ${it.hit_ratio}%`) +
          (it.rule_status ? `<br/>状态：${it.rule_status}` : '');
      },
    },
    grid: { left: 92, right: 56, top: 8, bottom: 6 },
    xAxis: {
      type: 'value',
      axisLabel: { color: c.dim, fontSize: 10, formatter: (v) => int(v) },
      splitLine: { lineStyle: { color: c.border, type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      inverse: true,               // 第 1 名在顶部（见文件头说明：靠轴方向，不重排数组）
      data: codes,
      axisLine: { lineStyle: { color: c.border } },
      axisTick: { show: false },
      axisLabel: { color: c.muted, fontSize: 11 },
    },
    series: [{
      name: '命中次数',
      type: 'bar',
      barMaxWidth: 14,
      itemStyle: { color: c.brand, borderRadius: [0, 3, 3, 0] },
      // 数值直接标在柱子右侧：横向柱的长度差在小屏上不好估，读数最快的方式是写出来
      label: { show: true, position: 'right', color: c.muted, fontSize: 10,
        formatter: (p) => int(p.value) },
      data: items.map((it) => Number(it.hit_cnt) || 0),
    }],
  };
}

/** 口径说明（`metric_field` 是后端回显的计数字段，前端不假设口径）。 */
export function rankingCaption(data) {
  const field = (data && data.metric_field) || 'hit_cnt';
  const n = ((data && data.items) || []).length;
  return `来源 metric_buckets（bucket_type=rule）· 口径字段 ${field} · 降序与 rank 均由后端下发 · 共 ${n} 条`;
}
