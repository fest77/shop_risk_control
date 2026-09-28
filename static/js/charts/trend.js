/* 「拦截率趋势」折线图配置（模块 02 §2.3 + BR-02-04）
 *
 * 双 Y 轴：左轴 = 事件量，右轴 = 拦截率(%)。
 *
 * ## 为什么两个轴都是必需的
 * 事件量是几十~几万的整数，拦截率是 0~100 的小数。同轴会让拦截率被压成一条贴底的直线，
 * 看起来像"拦截率一直是 0"——这是最容易被误读成安全的那种图。分开两轴后两条曲线的形状
 * 才都读得出来。
 *
 * ## 粒度与补点都不是前端的事
 * `granularity` 由**后端**按 `range` 决定并回传（BR-02-04），空桶也由后端补零（§3.3）。
 * 前端只按 `points` 逐点画；`block_rate === null`（分母为 0）原样交给 ECharts 形成断点，
 * **绝不填 0**——填 0 等于宣称"拦截率为 0%"，而事实是"没有样本"。
 */
import { axisLabel, int, pct, hourMinute, dateTime } from '../format.js';

/** 该区间是否没有任何样本（空态判据，MET-5002：空数据不是错误）。 */
export function isEmptyTrend(data) {
  const points = (data && data.points) || [];
  if (!points.length) return true;
  const anyCount = points.some((p) => Number(p.event_cnt) > 0);
  const anyRate = points.some((p) => p.block_rate !== null && p.block_rate !== undefined);
  return !anyCount && !anyRate;
}

/** 构造 option。data = TrendOut `{range, granularity, truncated, points[], stale}`。 */
export function buildTrendOption(data, c) {
  const points = (data && data.points) || [];
  const granularity = (data && data.granularity) || '1h';
  const labels = points.map((p, i) => axisLabel(p.bucket_ts, granularity, { index: i, total: points.length }));

  return {
    animationDuration: 240,
    color: [c.brand, c.danger],
    // 图例放**底部**而不是顶部：两个 Y 轴的轴名（事件量 / 拦截率(%)）都渲染在坐标区顶端，
    // 图例再放顶部就会与轴名挤在同一行（实测：左轴名被图例压住）。底部图例 + grid.bottom 留位
    // 是唯一不牺牲任何刻度的排法。
    legend: {
      data: ['事件量', '拦截率(%)'],
      bottom: 0, left: 'center', itemWidth: 14, itemHeight: 8,
      textStyle: { color: c.muted, fontSize: 11 },
    },
    tooltip: {
      trigger: 'axis',
      // 值全部来自后端字段；null 显示 `—` 而不是 0
      formatter: (ps) => {
        if (!ps || !ps.length) return '';
        const p = points[ps[0].dataIndex] || {};
        const rows = [
          dateTime(p.bucket_ts) + (p.partial ? '（累积中）' : ''),
          `事件量：${int(p.event_cnt)}`,
          `通过 / 人审 / 拦截：${int(p.pass_cnt)} / ${int(p.review_cnt)} / ${int(p.reject_cnt)}`,
          `拦截率：${pct(p.block_rate)}`,
        ];
        return rows.join('<br/>');
      },
    },
    grid: { left: 58, right: 58, top: 26, bottom: 44 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: labels,
      axisLine: { lineStyle: { color: c.border } },
      axisLabel: { color: c.dim, fontSize: 10, interval: 0, hideOverlap: true },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value', name: '事件量', nameTextStyle: { color: c.dim, fontSize: 10 },
        splitLine: { lineStyle: { color: c.border, type: 'dashed' } },
        axisLabel: { color: c.dim, fontSize: 10, formatter: (v) => int(v) },
      },
      {
        type: 'value', name: '拦截率(%)', nameTextStyle: { color: c.dim, fontSize: 10 },
        splitLine: { show: false },
        axisLabel: { color: c.dim, fontSize: 10, formatter: (v) => `${v}%` },
      },
    ],
    series: [
      {
        name: '事件量', type: 'line', yAxisIndex: 0, smooth: false,
        symbol: 'circle', symbolSize: 4, showSymbol: points.length <= 30,
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.14 },
        data: points.map((p) => Number(p.event_cnt) || 0),
      },
      {
        name: '拦截率(%)', type: 'line', yAxisIndex: 1, smooth: false,
        symbol: 'circle', symbolSize: 4, showSymbol: points.length <= 30,
        lineStyle: { width: 2, type: 'solid' },
        // connectNulls=false：分母为 0 的桶必须显示为断点（那一段没有数据，不是 0%）
        connectNulls: false,
        data: points.map((p) => (p.block_rate === null || p.block_rate === undefined
          ? null : Number(p.block_rate))),
      },
    ],
  };
}

/** 区块下方的一句话口径说明（Spec §2.2 要求每块都写清数据来源）。 */
export function trendCaption(data) {
  const gran = (data && data.granularity) || '—';
  const points = (data && data.points) || [];
  const last = points[points.length - 1];
  const partial = last && last.partial ? `；末点 ${hourMinute(last.bucket_ts)} 为未闭合桶，仍在累积` : '';
  const trunc = data && data.truncated ? '；返回点数已被服务端截断' : '';
  return `来源 metric_buckets（bucket_type=global）· 粒度 ${gran}（由后端按时间范围决定）${partial}${trunc}`;
}
