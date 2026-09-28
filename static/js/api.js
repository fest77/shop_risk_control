/* 统一 HTTP 客户端（BR-00-21 + 模块 01 BR-01-17/18）
 *
 * 所有页面**必须**通过本模块发请求，不得直接 fetch：
 *   - 自动携带 `Authorization: Bearer <jwt>`（模块 01 起，不再用 X-Operator/X-Role）
 *   - 自动解析统一响应包 {ok, code, message, trace_id, data}
 *   - **401 统一清 token 并跳登录页**（BR-01-17），各视图不重复实现
 *   - **403 不跳页，只 toast 后端文案**（BR-01-18）——越权不该把人踢出登录
 *   - 其他失败自动 toast（含 trace_id 后 6 位，便于对着服务端日志定位）
 */
import { toast } from './ui.js';
import { clearSession, getToken } from './session.js';

export class ApiError extends Error {
  constructor({ code, message, traceId = null, data = null, status = 0 }) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.traceId = traceId;
    this.data = data;
    this.status = status;
  }

  /** trace_id 后 6 位（§3.1 要求前端错误提示里展示它）。 */
  shortTrace() {
    return this.traceId ? String(this.traceId).slice(-6) : '—';
  }
}

let unauthorizedHandler = null;

export function configureAuth({ onUnauthorized }) {
  unauthorizedHandler = onUnauthorized;
}

function headers(extra, json = true) {
  // multipart 请求**不能**手工设置 Content-Type：boundary 由浏览器在发送时生成，
  // 手写一个没有 boundary 的 Content-Type 会让后端解析不出任何分段。
  // 因此这里给 postForm 留了 json=false 的口子（其余调用点行为不变）。
  const base = json ? { 'Content-Type': 'application/json' } : {};
  const token = getToken();
  if (token) base.Authorization = `Bearer ${token}`;
  return Object.assign(base, extra || {});
}

function buildUrl(path, query) {
  if (!query) return path;
  const qs = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return;
    qs.set(k, String(v));
  });
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

function notifyError(err) {
  const trace = err.traceId ? `<span class="toast-trace">trace: ${err.shortTrace()}</span>` : '';
  toast(`[${err.code}] ${err.message}${trace}`, 'err');
}

/**
 * 发一次请求。
 * @param {string} path 形如 /api/v1/lists（以 / 开头，含 API 前缀）
 * @param {object} opts { method, query, body, rawBody, signal, silent, headers }
 *   rawBody：原样交给 fetch 的请求体（FormData 等）。给了它就不再走 JSON 序列化，
 *   且**不会**带 Content-Type 头（见 headers()）。body 的老行为保持不变。
 *   headers：额外请求头（如规则新建的 `Idempotency-Key`，Spec 06 §3.1）。
 *   只做合并，Authorization / Content-Type 的既有语义不变。
 * @returns {Promise<any>} 成功时返回响应包里的 data
 */
export async function request(path, opts = {}) {
  const { method = 'GET', query = null, body, rawBody, signal = null, silent = false,
    headers: extraHeaders = null } = opts;
  // 记下"发请求时是否已经带着令牌"——它决定 401 的含义（见下方处理）
  const hadToken = !!getToken();
  const isForm = typeof FormData !== 'undefined' && rawBody instanceof FormData;
  let res;
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers: headers(extraHeaders, !isForm),
      signal,
      body: rawBody !== undefined ? rawBody : (body === undefined ? undefined : JSON.stringify(body)),
    });
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;   // 主动取消，不提示
    const err = new ApiError({
      code: 'NET-0001',
      message: '网络不可达，请确认服务是否已启动',
    });
    if (!silent) notifyError(err);
    throw err;
  }

  let env = null;
  try {
    env = await res.json();
  } catch (e) {
    env = null;   // 非 JSON（反代错误页、连接被重置等）
  }

  if (!env || typeof env.ok !== 'boolean') {
    const err = new ApiError({
      code: 'COM-5000',
      message: `服务返回了非标准响应（HTTP ${res.status}）`,
      traceId: res.headers.get('X-Trace-Id'),
      status: res.status,
    });
    if (!silent) notifyError(err);
    throw err;
  }

  if (env.ok) return env.data;

  const err = new ApiError({
    code: env.code,
    message: env.message,
    traceId: env.trace_id,
    data: env.data,
    status: res.status,
  });

  // 401 有两种截然不同的含义，必须分开处理：
  //   ① 请求本来就带着令牌 -> 登录态失效，清 token 并交给上层跳登录页（BR-01-17）
  //   ② 请求没带令牌（如登录接口本身）-> 只是这次操作被拒，**绝不能**当作登录失效
  // 曾把两者混为一谈：用户输错密码后，页面被重新渲染成空白登录页，
  // 连"账号或密码错误"的提示都来不及显示，看起来像"点了没反应"。
  if (res.status === 401) {
    if (hadToken) {
      clearSession();
      if (typeof unauthorizedHandler === 'function') unauthorizedHandler(err);
    }
    throw err;
  }
  // 403：只提示，不跳页（BR-01-18）
  if (!silent) notifyError(err);
  throw err;
}

export const get = (path, query, signal) => request(path, { query, signal });
export const post = (path, body) => request(path, { method: 'POST', body });
export const put = (path, body) => request(path, { method: 'PUT', body });
export const del = (path, query) => request(path, { method: 'DELETE', query });

/**
 * multipart/form-data 提交（名单批量导入）。
 * 不设 Content-Type，交给浏览器补 boundary；401/403/统一响应包的判断仍然复用 request()。
 * opts 透传给 request，视图可用 { silent: true } 自行接管错误展示（避免重复 toast）。
 */
export const postForm = (path, formData, opts = {}) =>
  request(path, Object.assign({ method: 'POST', rawBody: formData }, opts));

export const API_PREFIX = '/api/v1';
