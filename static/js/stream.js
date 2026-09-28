/* SSE 连接管理（模块 02 §3.5 / §4.2 / §6）
 *
 * 只管一件事：把 `/api/v1/metrics/stream` 维持住，并在断线时按**指数退避**重连。
 * 事件内容怎么渲染不属于这里（那是 views/dashboard.js 的事）。
 *
 * ## 为什么用 `EventSource` + `?token=`（而不是 fetch + ReadableStream）
 * 浏览器的 `EventSource` 不能设置请求头（W3C 如此），而全站令牌只在
 * `Authorization` 里。模块 11 为此**专为这一个端点**开了 `?token=` 通道
 * （`auth_middleware.QUERY_TOKEN_PATHS`）。代价是令牌会进访问日志——已登记为 N-11-10，
 * 只有 SSE 走这条路，其余接口仍然只认请求头。
 *
 * ## 为什么要自己管重连（而不是交给浏览器）
 * `EventSource` 自带重连，但间隔固定（服务端 `retry:` 给的是 3s），而 Spec §2.5/§5
 * 要求**指数退避 1s→2s→4s，上限 30s**：后端重启或网络抖动时，固定 3s 的重连会把
 * 连接反复砸向还没起来的服务；退避则让它自己缓过来。代价是放弃了浏览器自动携带
 * `Last-Event-ID` 的补偿能力，因此**重连后必须主动补齐**（见视图层的 `onReconnect`）。
 *
 * ## 为什么要做"心跳看门狗"
 * 中间代理（或本机防火墙）静默掐断长连接时，`EventSource` 既不报错也不触发 `error`，
 * 页面表现为"数据再也不更新但连接看着还在"——这类问题没有看门狗就极难定位。
 * 服务端每 15s 发一次 `: ping`/heartbeat 帧，因此超过 40s 一个字节都没收到即可判定为死连接。
 */
import { getToken } from './session.js';

/** 存活连接数（E2E 的泄漏观测口：切走路由后必须归零）。 */
const live = new Set();

export function liveStreamCount() {
  return live.size;
}

const FIRST_RETRY_MS = 1000;
const MAX_RETRY_MS = 30000;      // Spec §2.5：上限 30s
const WATCHDOG_MS = 40000;       // > 2 × 服务端心跳（15s）

/**
 * 建一个 SSE 管理器（不自动连接，由视图决定何时 start/stop）。
 *
 * @param {object} opts
 *   - `apiPrefix`：`/api/v1`
 *   - `query`：`{scene, level}` 服务端过滤（§3.5）
 *   - `onState(state, info)`：`connecting|open|reconnecting|closed`
 *   - `onEvent(payload)`：一条 `risk_event`
 *   - `onGap(info)`：缺口帧（服务端环形缓冲已被追平，中间有漏）
 *   - `onReconnect()`：**重连成功**（用于主动补齐，§2.5）
 */
export function createStream({
  apiPrefix = '/api/v1', query = {}, onState, onEvent, onGap, onReconnect,
} = {}) {
  let es = null;
  // 命名事件监听器要显式摘除：匿名函数 addEventListener 之后无法 remove，
  // 而"关闭旧 socket 前先摘 handler"是防止 double-connect 的关键一步
  let bound = [];
  let stopped = true;
  let paused = false;
  let retryMs = FIRST_RETRY_MS;
  let retryTimer = null;
  let watchdog = null;
  let everOpen = false;     // 用于区分"首次连接"与"断线重连成功"
  let state = 'closed';

  function setState(next, info) {
    state = next;
    if (typeof onState === 'function') onState(next, info || {});
  }

  function url() {
    const qs = new URLSearchParams();
    // 令牌走查询串（见文件头）；scene/level 由**服务端**过滤，
    // 这样"流里看到的"与"统计口径"是同一套（模块 11 的 Subscriber.matches）
    const token = getToken();
    if (token) qs.set('token', token);
    Object.entries(query).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
    });
    return `${apiPrefix}/metrics/stream?${qs.toString()}`;
  }

  function beat() {
    if (watchdog) clearTimeout(watchdog);
    // 任何一帧（事件/心跳）都重置看门狗：只有"长时间完全静默"才算死
    watchdog = setTimeout(() => {
      if (stopped || paused) return;
      closeSocket();
      schedule(`心跳超时（${WATCHDOG_MS / 1000}s 内无任何帧）`);
    }, WATCHDOG_MS);
  }

  function closeSocket() {
    if (watchdog) { clearTimeout(watchdog); watchdog = null; }
    if (es) {
      // 先摘掉 handler 再 close：否则 close() 触发的 error 会再排一次重连，形成双连接
      es.onopen = null; es.onerror = null; es.onmessage = null;
      bound.forEach(([name, fn]) => { try { es.removeEventListener(name, fn); } catch (e) { /* ignore */ } });
      try { es.close(); } catch (e) { /* 已关闭则忽略 */ }
      live.delete(es);
      es = null;
    }
    bound = [];
  }

  function schedule(reason) {
    if (stopped || paused) return;
    setState('reconnecting', { reason, retry_ms: retryMs });
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, retryMs);
    // 指数退避：1s → 2s → 4s → 8s → 16s → 30s（封顶）
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
  }

  function parse(ev) {
    try { return JSON.parse(ev.data); } catch (e) { return null; }
  }

  function connect() {
    if (stopped || paused) return;
    if (!getToken()) { setState('closed', { reason: '未登录（无令牌）' }); return; }
    if (typeof EventSource === 'undefined') {
      setState('closed', { reason: '当前浏览器不支持 EventSource' });
      return;
    }
    closeSocket();
    setState(everOpen ? 'reconnecting' : 'connecting', {});
    let sock;
    try {
      sock = new EventSource(url());
    } catch (e) {
      schedule(`EventSource 构造失败：${e && e.message}`);
      return;
    }
    es = sock;
    live.add(sock);
    bound = [];

    const add = (name, fn) => { bound.push([name, fn]); sock.addEventListener(name, fn); };

    sock.onopen = () => {
      retryMs = FIRST_RETRY_MS;          // 连上就复位退避
      const isFirstOpen = !everOpen;
      everOpen = true;
      beat();
      setState('open', {});
      // 只有**真正的重连**才通知上层补齐：首次连接时上层本来就正在首屏取数，
      // 再通知一次会让同一批接口被立刻打两遍（白吃一倍限流额度）
      if (!isFirstOpen && typeof onReconnect === 'function') onReconnect();
    };
    sock.onerror = () => {
      if (stopped || paused || es !== sock) return;
      closeSocket();
      // DASH-5001/5002：保留已展示数据（视图层负责），这里只负责"正在重连"的状态与退避
      schedule('连接中断');
    };
    add('risk_event', (ev) => {
      beat();
      const payload = parse(ev);
      if (payload && typeof onEvent === 'function') onEvent(payload);
    });
    add('heartbeat', () => { beat(); });
    add('gap', (ev) => {
      beat();
      if (typeof onGap === 'function') onGap(parse(ev) || {});
    });
    add('shutdown', () => {
      // 服务端优雅停机：不要立刻重连打上去，等退避定时器（首次 1s）
      closeSocket();
      schedule('服务端停机');
    });
    // 服务端未命名事件（兼容 `event:` 缺省的实现）
    sock.onmessage = (ev) => {
      beat();
      const payload = parse(ev);
      if (payload && typeof onEvent === 'function') onEvent(payload);
    };
  }

  return {
    /** 建立连接（幂等）。 */
    start() {
      stopped = false;
      paused = false;
      retryMs = FIRST_RETRY_MS;
      connect();
    },
    /** 永久关闭（视图 dispose / 切路由）。 */
    stop() {
      stopped = true;
      paused = false;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      closeSocket();
      setState('closed', {});
    },
    /** 暂停（页面隐藏时用，§3.5/BR-02-05：省资源、也避免限流误判）。 */
    pause() {
      paused = true;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      closeSocket();
      setState('paused', {});
    },
    /** 恢复（页面重新可见）。 */
    resume() {
      if (stopped) return;
      paused = false;
      retryMs = FIRST_RETRY_MS;
      connect();
    },
    /** 筛选变更：重置退避并立即用新 query 重连。 */
    reconfigure(next) {
      Object.assign(query, next || {});
      if (stopped || paused) return;
      retryMs = FIRST_RETRY_MS;
      connect();
    },
    get state() { return state; },
    get connected() { return !!es && state === 'open'; },
  };
}
