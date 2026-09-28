/* 登录页（模块 01 §2.1，对应原型 01_登录页.pen）
 *
 * 对应验收：V-01-01（正常登录）、V-01-02（错误统一提示）、V-01-03（停用账号）、
 * V-01-13（已登录访问本页被重定向——由 app.js 的路由处理）与 §2.1 的页面状态机。
 *
 * 交互要点：角色**只展示不选择**（§2.1：角色由后端判定）；失败时清空密码框并
 * 把焦点放回密码框，让用户可以直接重打，不必用鼠标再点一次。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import { setToken } from '../session.js';

export const meta = { title: '登录', crumb: '风控中台' };

export async function render(container, { onSuccess } = {}) {
  const err = ui.h('div', { class: 'login-error', style: 'display:none' });
  const username = ui.inputBox({ value: '', placeholder: '请输入账号' });
  const password = ui.h('input', { type: 'password', placeholder: '请输入密码' });
  const submit = ui.button('登 录', { variant: 'primary', onClick: doSubmit });

  function showError(message) {
    err.textContent = message;
    err.style.display = 'block';
  }
  function clearError() {
    err.textContent = '';
    err.style.display = 'none';
  }

  async function doSubmit() {
    clearError();
    const u = username.value.trim();
    const p = password.value;
    // 前端只做"非空"这类零成本校验；长度/格式一律交给后端，
    // 避免同一套规则在两处实现后产生分歧
    if (!u) { showError('请输入账号'); username.focus(); return; }
    if (!p) { showError('请输入密码'); password.focus(); return; }

    ui.buttonBusy(submit, true, '登录中');
    username.readOnly = true;
    password.readOnly = true;
    try {
      const data = await api.request(`${api.API_PREFIX}/auth/login`, {
        method: 'POST',
        body: { username: u, password: p },
        // silent：错误显示在页面内联区域（§2.1 的错误提示区），不弹 toast
        silent: true,
      });
      setToken(data.access_token);
      if (typeof onSuccess === 'function') await onSuccess(data);
    } catch (e) {
      showError(e.code === 'NET-0001'
        ? e.message
        : `${e.message}（${e.code}${e.traceId ? ' · trace ' + e.shortTrace() : ''}）`);
      password.value = '';
      password.focus();
    } finally {
      ui.buttonBusy(submit, false);
      username.readOnly = false;
      password.readOnly = false;
    }
  }

  password.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSubmit(); });
  username.addEventListener('keydown', (e) => { if (e.key === 'Enter') password.focus(); });

  const field = (label, node) => ui.h('div', { class: 'field' }, [
    ui.h('label', {}, [document.createTextNode(label), ui.h('span', { class: 'req', text: ' *' })]),
    node,
  ]);

  ui.mount(container,
    ui.h('div', { class: 'login-wrap' }, [
      ui.h('div', { class: 'login-card' }, [
        ui.h('div', { class: 'login-brand', text: '风控中台' }),
        ui.h('div', { class: 'login-sub', text: '电商风险控制系统 · Risk Control Console' }),
        ui.h('div', { class: 'login-divider' }),
        err,
        field('账号', username),
        field('密码', password),
        // 角色说明区：**仅说明**，角色由后端按账号判定，不可选择
        ui.h('div', { class: 'login-roles' }, [
          ui.h('div', { class: 'hint', text: '角色由账号决定，无需选择：' }),
          ui.h('div', { class: 'login-role-tags' }, [
            ui.tag('风控审核员', 'brand'),
            ui.tag('风控策略师', 'brand'),
            ui.tag('系统管理员', 'brand'),
          ]),
        ]),
        ui.h('div', { class: 'login-actions' }, [submit]),
        ui.h('div', { class: 'login-foot hint',
          text: '凭据使用 bcrypt 加盐存储；登录态为 JWT，失效自动跳回本页。' }),
        ui.h('div', { class: 'login-foot hint',
          text: '演示账号：admin01 / strategy01 / reviewer01（密码见 README，仅限演示）。' }),
      ]),
    ]),
  );
  username.focus();
}
