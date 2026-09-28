/* 登录态（模块 01 BR-01-16 / BR-01-20）
 *
 * 只做三件事：读写 token、缓存 /auth/me 的结果、判断自己有没有某权限。
 *
 * **权限不从角色推导**（BR-01-12）：`has(perm)` 查的是后端 `/auth/me` 下发的
 * permissions 数组。前端一旦自行按角色推导，权限矩阵改动就会出现"后端已收紧、
 * 前端仍显示按钮"的漂移。
 */
const TOKEN_KEY = 'rc_token';

let token = '';
let user = null;

try {
  token = localStorage.getItem(TOKEN_KEY) || '';
} catch (e) {
  // 隐私模式等场景下 localStorage 不可用：退化为"每次都要重新登录"，
  // 而不是让整个页面脚本崩掉
  token = '';
}

export function getToken() {
  return token;
}

export function setToken(value) {
  token = value || '';
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* 存不进去只影响下次免登录，不影响本次 */ }
}

export function setUser(value) {
  user = value || null;
}

export function getUser() {
  return user;
}

/** 清本地登录态（BR-01-20：只清 token 与用户信息，不动其他本地数据）。 */
export function clearSession() {
  setToken('');
  user = null;
}

export function isLoggedIn() {
  return !!token && !!user;
}

export function permissions() {
  return (user && user.permissions) || [];
}

export function has(permission) {
  return permissions().includes(permission);
}
