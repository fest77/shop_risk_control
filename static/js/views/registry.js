/* 路由与菜单注册表（模块 00 §2.1 + 模块 01 §2.2）
 *
 * 菜单顺序**不可变**，与 03_原型图 的 9 个原型一一对应。
 *
 * **可见性判断从"角色"改为"权限"**（模块 01 BR-01-12）：每条路由声明它需要的
 * 权限串，由框架拿 `/auth/me` 下发的 permissions 比对。这样权限矩阵改动只改
 * 后端一处，前端不会与后端漂移。
 *
 * `loader` 为 null 表示该页面的所属模块尚未实现，路由会渲染一个明确的
 * 「本页由模块 XX 提供」状态——这是渐进交付的**真实状态**，不是占位桩。
 */
export const ROUTES = [
  {
    hash: '#/dashboard', label: '态势大盘', module: '02',
    permission: 'dashboard:read',
    desc: '实时指标、趋势与排行榜',
    // 模块 02 已交付（4 张指标卡 + 趋势/环形/排行三张本地 ECharts + SSE 实时事件流），
    // 故本路由不再走"尚未接入"占位。**菜单项与权限串一字未改**：`dashboard:read` 是
    // 权限矩阵里既有的那一条（三角色都有），E2E 钉着"三个菜单第一项都是态势大盘"。
    loader: () => import('./dashboard.js'),
  },
  {
    hash: '#/cases', label: '审核工作台', module: '07',
    permission: 'case:read',
    desc: '待审案件队列、特征快照与研判处置',
    // 模块 09 交付了本页的中栏（用户画像卡 + 关联实体图谱），模块 05 交付了右栏
    // （系统风险判定摘要 + 规则命中明细），模块 07 交付了**左栏**（案件列表：多维筛选 +
    // 分页 + 认领）以及案件驱动模式（`?case=` 时中栏与右栏由**同一次**
    // `GET /cases/{case_no}` 渲染，BR-07-06）。
    //
    // 三者**共用同一个视图文件**（`profile.js` 是 09 建的宿主，07 只在里面做加法），
    // 因此本路由的 loader 保持不变——这正是决策 D48 的既定安排：04/05/09 在 Spec 里
    // 都是"无独占页面的引擎"，它们的 UI 就挂在本页的三栏里。**没有新增菜单项**。
    loader: () => import('./profile.js'),
  },
  {
    hash: '#/rules', label: '策略与规则', module: '06',
    permission: 'rule:write',
    desc: '规则配置与多维名单库',
    // 模块 06-B 已交付「规则配置」tab（规则列表 + 编辑抽屉 + 条件树构建器），
    // 故本路由的默认页按 Spec §2.1 换成真实视图 `rules.js`（原先挂在 `#/rules`
    // 的名单库改由 `#/lists` 承载，见下方两条 `menu:false` 子路由）。
    loader: () => import('./rules.js'),
  },
  {
    // Spec 06 §2.1 的三个 tab 各自对应一个 hash；本项与下一项**不进左侧菜单**
    // （`menu:false`）：Spec 00 §2.1 的菜单表只有「策略与规则」一项，06-A 的
    // E2E 也钉着策略师菜单恰好三项。它们共用父项的菜单高亮（`menuHash`）。
    hash: '#/lists', label: '名单库', module: '06',
    permission: 'list:write',
    menu: false, menuHash: '#/rules',
    desc: '多维名单库（策略与规则页的第二个标签）',
    loader: () => import('./lists.js'),
  },
  {
    hash: '#/scenes', label: '规则场景', module: '06',
    permission: 'rule:write',
    menu: false, menuHash: '#/rules',
    desc: '规则场景只读字典（策略与规则页的第三个标签）',
    loader: () => import('./scenes.js'),
  },
  {
    hash: '#/sim', label: '事件仿真', module: '10',
    permission: 'sim:run',
    desc: '事件流模拟与判定链路回溯',
    // 模块 10 已交付：左栏用例模板卡 + 结构化事件参数表单（`scene_extra` 按
    // `event_type` 动态显隐，与 03 同构），右栏判定链路单步回溯（五步 + 每步耗时）
    // 与批量回放结果。菜单项与权限串一字未改（`sim:run` 是权限矩阵里既有的那条）。
    loader: () => import('./sim.js'),
  },
  {
    hash: '#/audit', label: '审计日志', module: '12',
    permission: 'audit:read',
    desc: '哈希链审计流水与完整性校验',
    // 模块 12 已交付：全链校验卡片 + 四维筛选流水表 + 导出
    loader: () => import('./audit.js'),
  },
  {
    hash: '#/settings', label: '系统设置', module: '13',
    permission: 'sys:config',
    desc: '运行参数、场景与账号管理',
    // 模块 13 交付：本页是**独立页面**（不是某个宿主页的栏目），纵向四张卡片——
    // ① 运行参数（`GET/PUT /system/config`，越界即拒绝、生效方式如实回传）
    // ② 吞吐与接口健康度（复用 13 的 `/system/stats`，降级复用 11 的 `/metrics/throughput` 与 `/health`）
    // ③ 账号与角色管理（E19 `sys_users` 的唯一写入方，危险操作全部二次确认）
    // ④ 决策引擎配置（模型引擎为**架构预留**，如实标注"未接入"而不是假开关）
    // **菜单项与权限串一字未改**（`sys:config` 是权限矩阵里既有的那条，仅 admin）。
    loader: () => import('./settings.js'),
  },
];

export const DEFAULT_HASH = '#/dashboard';
/** 登录页（不属于业务菜单，未登录时渲染）。 */
export const LOGIN_HASH = '#/login';

/** 按 hash 查路由；只认路径部分（忽略 ?query）。 */
export function findRoute(hash) {
  const path = String(hash || '').split('?')[0];
  return ROUTES.find((r) => r.hash === path) || null;
}

/** 取 query 参数（如 #/rules?code=RCOUPON001）。 */
export function queryOf(hash) {
  const i = String(hash || '').indexOf('?');
  return new URLSearchParams(i >= 0 ? hash.slice(i + 1) : '');
}

/** 由权限串判断能否访问某路由。 */
export function canAccess(route, permissions) {
  return !!route && permissions.includes(route.permission);
}

export function allowedRoutes(permissions) {
  return ROUTES.filter((r) => canAccess(r, permissions));
}
