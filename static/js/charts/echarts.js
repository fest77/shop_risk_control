/* ECharts 薄封装（模块 02 §6 + BR-02-19/20/21）
 *
 * 三件事，别的不做：
 *   ① 加载**本地 vendor** 的 ECharts（`ui.loadECharts()`，BR-02-19：禁止 CDN）；
 *   ② 实例生命周期（init / setOption / dispose）——BR-02-20 要求视图卸载时 dispose，
 *      否则来回切路由会持续泄漏 canvas 与监听器（V-02-14 就是钉这条）；
 *   ③ **一个共享的 window.resize 监听**（节流 200ms，BR-02-21）统一 resize 全部活实例。
 *      每个图表各自 addEventListener 的写法看着更"模块化"，但页面有 3 张图就会挂 3 个
 *      监听器、切 10 次路由留 30 个——泄漏的正是这种"最自然的写法"。
 *
 * ## liveChartCount() 为什么存在
 * V-02-14（反复切路由 10 次，实例数不增长）需要一个**可被外部观测**的口子。
 * 只暴露给 E2E：人工排查内存泄漏时也能直接在控制台 `import(...)` 读它。
 */
import * as ui from '../ui.js';

/** 存活实例（dispose 时移除，因此它同时是"有没有泄漏"的唯一真源）。 */
const live = new Set();

let resizeBound = false;
let resizeTimer = null;

function bindResize() {
  if (resizeBound) return;
  resizeBound = true;
  window.addEventListener('resize', () => {
    // 节流：拖动窗口边缘会以每帧一次的频率触发 resize，逐次 resize 三张图会明显卡顿
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      live.forEach((c) => { try { c.resize(); } catch (e) { /* 容器已隐藏/销毁则忽略 */ } });
    }, 200);
  });
}

/** 取主题变量（ECharts 画在 canvas 上读不到 CSS 变量，只能运行期取一次计算值，BR-02-22）。 */
export function themeVar(name, fallback = '#333333') {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** 取一整套图表配色（全部来自 CSS 变量，换肤时图表跟着变）。 */
export function palette() {
  return {
    text: themeVar('--text', '#333333'),
    muted: themeVar('--muted', '#666666'),
    dim: themeVar('--dim', '#7A7A7A'),
    border: themeVar('--border', '#D9D9D9'),
    brand: themeVar('--brand', '#223248'),
    brandSoft: themeVar('--brand-soft', '#E8ECF3'),
    danger: themeVar('--danger', '#D93025'),
    warn: themeVar('--warn', '#E8710A'),
    ok: themeVar('--ok', '#1E8E3E'),
    high: themeVar('--risk-high', '#D93025'),
    medium: themeVar('--risk-mid', '#E8710A'),
    low: themeVar('--risk-low', '#1E8E3E'),
    soft: themeVar('--soft', '#FAFAFA'),
  };
}

/** 一个图表句柄：包装 echarts 实例，并在 dispose 时自动摘掉全局登记。 */
class ChartHandle {
  constructor(instance) {
    this.instance = instance;
    live.add(instance);
  }

  setOption(option, opts) {
    // notMerge=true：筛选器切换时系列数量/名称会变，合并旧 option 会留下上一轮的系列
    this.instance.setOption(option, Object.assign({ notMerge: true }, opts || {}));
    return this;
  }

  on(event, handler) {
    this.instance.on(event, handler);
    return this;
  }

  resize() {
    this.instance.resize();
    return this;
  }

  /** 幂等：重复 dispose 不抛错（切路由与区块重绘都可能走到）。 */
  dispose() {
    if (!this.instance) return;
    try { this.instance.dispose(); } catch (e) { /* 已销毁则忽略 */ }
    live.delete(this.instance);
    this.instance = null;
  }

  /** PNG dataURL（导出快照用）；实例已销毁或未渲染时返回 null。 */
  dataUrl(pixelRatio = 2) {
    if (!this.instance) return null;
    try { return this.instance.getDataURL({ type: 'png', pixelRatio, backgroundColor: '#FFFFFF' }); }
    catch (e) { return null; }
  }
}

/**
 * 在容器里建一张图。
 *
 * 容器必须**已经有非零尺寸**：ECharts 首帧按容器尺寸算布局，若容器此刻
 * `display:none`（例如还在骨架屏里），画布会退化成 0×0 且此后不会自愈。
 * 因此调用方要先让容器可见再 init。
 */
export async function create(container) {
  const echarts = await ui.loadECharts();
  bindResize();
  // 同一容器上若已有实例（快速连续刷新），先销毁，避免 ECharts 抛
  // "There is a chart instance already initialized on the dom."
  const existed = echarts.getInstanceByDom(container);
  if (existed) { live.delete(existed); try { existed.dispose(); } catch (e) { /* ignore */ } }
  return new ChartHandle(echarts.init(container));
}

/** 存活实例数（V-02-14 的观测口）。 */
export function liveChartCount() {
  return live.size;
}

/** ECharts 是否已加载（未加载时不需要（也无法）建图）。 */
export function loaded() {
  return typeof window !== 'undefined' && !!window.echarts;
}
