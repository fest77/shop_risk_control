/* 单页壳、hash 路由与登录态编排（模块 00 §2.1 / §2.4 + 模块 01 §4.4）
 *
 * 关键行为：
 *   - 路由切换只替换主内容区，**侧边导航不重建**（V-00-09）
 *   - 未登录：只渲染登录视图，侧栏不出现
 *   - 已登录访问 `#/login` → 直接回默认页（V-01-13 / BR-01-19）
 *   - 任意接口 401 → 清 token → 跳登录页 + toast（BR-01-17，由 api.js 统一触发）
 *   - 权限不足的 hash → 跳默认页 + toast「无权访问该页面」（§2.3）
 */
import * as api from './api.js';
import * as ui from './ui.js';
import * as session from './session.js';
import { ensureEnums } from './store.js';
import {
  DEFAULT_HASH, LOGIN_HASH, allowedRoutes, canAccess, findRoute,
} from './views/registry.js';

const els = {
  menu: document.getElementById('menu'),
  view: document.getElementById('view'),
  crumb: document.getElementById('crumb'),
  pageTitle: document.getElementById('pageTitle'),
  pageActions: document.getElementById('pageActions'),
  userBox: document.getElementById('userBox'),
  logoutBtn: document.getElementById('logoutBtn'),
  pwdBtn: document.getElementById('pwdBtn'),
  healthDot: document.getElementById('healthDot'),
  healthText: document.getElementById('healthText'),
};

let currentRoute = null;
let currentView = null;
let menuPermKey = null;    // 菜单是按哪份权限渲染的（权限变化才重建）
let showingLogin = false;

// ==================== 侧边导航 ====================
function renderMenu() {
  const perms = session.permissions();
  const key = perms.join(',');
  if (menuPermKey === key) return;   // 权限未变则不重建，满足"侧栏不重建"
  menuPermKey = key;
  // `menu:false` 的路由是可访问页面但**不是**菜单项（如策略与规则页下的
  // `#/lists` / `#/scenes` 两个 tab）。Spec 00 §2.1 的菜单表是固定的 9 项，
  // 少渲染一项就少一个菜单，多渲染一项就会把既有菜单断言推翻。
  ui.mount(els.menu, allowedRoutes(perms).filter((r) => r.menu !== false).map((r) =>
    ui.h('div', {
      class: 'menu-item', text: r.label, dataset: { hash: r.hash },
      onclick: () => { location.hash = r.hash; },
    })));
  if (currentRoute) setActiveMenu(currentRoute.menuHash || currentRoute.hash);
}

function setActiveMenu(hash) {
  els.menu.querySelectorAll('.menu-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.hash === hash);
  });
}

function renderUser() {
  const u = session.getUser() || {};
  ui.mount(els.userBox, [
    ui.h('div', { class: 'user-name', text: u.real_name || u.username || '' }),
    ui.h('div', { class: 'user-role hint', text: `${u.username || ''} · ${u.role_label || ''}` }),
  ]);
}

// ==================== 登录页 ====================
async function showLogin() {
  if (showingLogin) return;   // 防止 401 与启动流程同时触发而渲染两次
  showingLogin = true;
  document.body.classList.add('guest');
  document.body.classList.remove('authed');
  currentRoute = null;
  currentView = null;
  menuPermKey = null;
  ui.clear(els.menu);
  ui.clear(els.pageActions);
  ui.clear(els.userBox);
  if (els.crumb) els.crumb.textContent = '';
  if (els.pageTitle) els.pageTitle.textContent = '';
  document.title = '风控中台 · 登录';
  try {
    const mod = await import('./views/login.js');
    currentView = mod;
    const box = ui.h('div', { class: 'view' });
    ui.mount(els.view, box);
    await mod.render(box, { onSuccess: startSession });
  } finally {
    showingLogin = false;
  }
}

/** 登录成功后：拉取 /auth/me（拿权限）→ 进入应用壳。 */
async function startSession() {
  const me = await api.get(`${api.API_PREFIX}/auth/me`);
  session.setUser(me);
  enterApp();
  if (!findRoute(location.hash) || location.hash.startsWith(LOGIN_HASH)) {
    location.hash = DEFAULT_HASH;
  }
  await navigate();
}

function enterApp() {
  document.body.classList.add('authed');
  document.body.classList.remove('guest');
  renderUser();
  renderMenu();
  ensureEnums();
  refreshHealth();
}

async function doLogout() {
  try {
    await api.post(`${api.API_PREFIX}/auth/logout`);
  } catch (e) {
    // 登出接口失败也必须清掉本地登录态，否则用户"点了登出还是登录状态"
  }
  session.clearSession();
  ui.toast('已登出', 'ok');
  location.hash = LOGIN_HASH;
  await showLogin();
}

function openChangePassword() {
  const oldP = ui.h('input', { type: 'password', placeholder: '当前密码' });
  const newP = ui.h('input', { type: 'password', placeholder: '新密码（6~64 位）' });
  const field = (label, node) => ui.h('div', { class: 'field' }, [
    ui.h('label', { text: label }), node,
  ]);
  ui.modal({
    title: '修改密码',
    body: [
      field('当前密码', oldP),
      field('新密码', newP),
      ui.h('div', { class: 'hint', text: '修改成功后当前登录状态会立即失效，需要用新密码重新登录。' }),
    ],
    confirmText: '确认修改',
    onConfirm: async () => {
      try {
        await api.post(`${api.API_PREFIX}/auth/password`, {
          old_password: oldP.value, new_password: newP.value,
        });
      } catch (e) {
        return false;   // 返回 false 让弹窗保持打开，便于直接改正
      }
      ui.toast('密码已修改，请用新密码重新登录', 'ok');
      session.clearSession();
      location.hash = LOGIN_HASH;
      await showLogin();
      return true;
    },
  });
}

// ==================== 页面外壳 ====================
function setHeader(route, viewMeta) {
  const title = (viewMeta && viewMeta.title) || route.label;
  const crumb = (viewMeta && viewMeta.crumb) || `风控中台 / ${route.label}`;
  els.crumb.textContent = crumb;
  els.pageTitle.textContent = title;
  document.title = `风控中台 · ${title}`;
  ui.clear(els.pageActions);
}

/**
 * 所属模块尚未实现时的明确状态。
 * 这不是占位桩：它把"还差哪个模块"直接显示出来，便于按模块验收时对照进度。
 */
function renderPendingModule(route) {
  ui.mount(els.view, ui.card(null, [
    ui.h('div', { class: 'pending' }, [
      ui.h('div', { class: 'pending-title', text: `${route.label} 尚未接入` }),
      ui.h('div', { class: 'hint', text:
        `本页由模块 ${route.module}（${route.desc}）提供，将在该模块的开发步骤中接入。` }),
      ui.h('div', { class: 'hint', text:
        '在此之前本菜单项可正常点击、路由与页头均正确，用于验证单页壳的路由机制（V-00-09）。' }),
    ]),
  ]));
}

// ==================== 路由 ====================
async function navigate() {
  try {
    await renderRoute();
  } catch (e) {
    // 任何未预期的渲染异常都必须变成**可见的错误面板**，绝不允许主内容区空白
    const msg = (e && e.message) ? e.message : String(e);
    ui.clear(els.view);
    els.view.textContent = `页面渲染失败：${msg}`;
    els.view.className = 'view banner err';
    // eslint-disable-next-line no-console
    console.error('路由渲染失败', e);
  }
}

async function renderRoute() {
  const hash = location.hash || DEFAULT_HASH;

  if (!session.isLoggedIn()) {
    await showLogin();
    return;
  }
  if (hash.startsWith(LOGIN_HASH)) {
    // BR-01-19：已有有效登录态时访问登录页，直接回默认页
    location.hash = DEFAULT_HASH;
    return;
  }

  const route = findRoute(hash);
  if (!route) {
    ui.toast(`页面不存在：${hash}`, 'warn');
    location.hash = DEFAULT_HASH;
    return;
  }

  if (!canAccess(route, session.permissions())) {
    if (route.hash === DEFAULT_HASH) {
      // 兜底：连默认页都不可访问（权限矩阵被改坏），明确报错而不是无限重定向
      ui.mount(els.view, ui.card(null, ui.empty('当前账号没有任何可访问页面',
        '请检查模块 01 的权限矩阵定义')));
      return;
    }
    ui.toast('无权访问该页面', 'warn');
    location.hash = DEFAULT_HASH;
    return;
  }

  if (currentView && typeof currentView.dispose === 'function') {
    currentView.dispose();   // 取消在途请求，避免旧视图回调写到新页面上
  }
  currentView = null;
  currentRoute = route;
  // 子标签（`menuHash`）高亮父菜单项：`#/lists` 仍应让左侧「策略与规则」处于选中态
  setActiveMenu(route.menuHash || route.hash);
  setHeader(route, null);
  ui.mount(els.view, ui.loading(6));

  if (!route.loader) {
    renderPendingModule(route);
    return;
  }
  try {
    const mod = await route.loader();
    currentView = mod;
    setHeader(route, mod.meta || null);
    const container = ui.h('div', { class: 'view' });
    ui.mount(els.view, container);
    await mod.render(container);
  } catch (e) {
    ui.mount(els.view, ui.card(null, ui.empty('页面加载失败', String(e && e.message ? e.message : e))));
  }
}

// ==================== 健康状态 ====================
async function refreshHealth() {
  if (!els.healthDot || !session.isLoggedIn()) return;
  try {
    const data = await api.request('/health', { silent: true });
    const status = (data && data.status) || 'down';
    els.healthDot.className = `health-dot ${status}`;
    const mongo = (data && data.mongo) || {};
    els.healthText.textContent = status === 'ok'
      ? `服务正常 · v${data.version}`
      : `服务 ${status}（Mongo ${mongo.connected ? '正常' : '不可达'}）`;
  } catch (e) {
    els.healthDot.className = 'health-dot down';
    els.healthText.textContent = '无法连接服务';
  }
}

// ==================== 启动 ====================
function bindAuth() {
  api.configureAuth({
    onUnauthorized: async () => {
      // BR-01-17：任意接口 401 → 清 token（api.js 已清）→ 跳登录页 + 统一提示
      ui.toast('登录已失效，请重新登录', 'warn');
      location.hash = LOGIN_HASH;
      await showLogin();
    },
  });
  if (els.logoutBtn) els.logoutBtn.addEventListener('click', doLogout);
  if (els.pwdBtn) els.pwdBtn.addEventListener('click', openChangePassword);
}

async function boot() {
  bindAuth();
  // 先注册监听再首屏渲染：否则首屏一旦抛异常，hashchange 永远没被注册上，
  // 表现为"点菜单完全没反应、地址栏变了但内容不动"——排查成本极高
  window.addEventListener('hashchange', navigate);

  if (!session.getToken()) {
    await showLogin();
    return;
  }
  try {
    // BR-01-16：页面加载时若存在 token，先调 /auth/me 验证其有效性
    const me = await api.get(`${api.API_PREFIX}/auth/me`, null, null);
    session.setUser(me);
    enterApp();
    setInterval(refreshHealth, 30000);
    await navigate();
  } catch (e) {
    // token 失效（401 已触发 onUnauthorized）或网络异常，都回到登录页
    await showLogin();
  }
}

boot();
