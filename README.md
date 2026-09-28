# shop_risk_control · 电商风险控制系统

> 项目来源：PRD「2.3 电商风险控制系统」
> 当前状态：**15 个模块（00 ~ 13）已全部交付，三道门全绿** —— `pytest` 1182 passed / 0 failed · 静态自审全过（OpenAPI 62 条路径）· 真实浏览器 E2E `165/165`。
> 自测教程见 [`TESTING.md`](TESTING.md)，项目总览与验收指南见 [`SUMMARY.md`](SUMMARY.md)。

---

## 1. 快速开始

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control

# ① 建索引 + 灌种子数据（幂等；--reset 会清空业务集合后重建）
.\.venv\Scripts\python.exe scripts\seed.py

# ② 启动服务
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8101

# ③ 浏览器打开并登录
#    http://127.0.0.1:8101/        （接口文档： http://127.0.0.1:8101/docs）

# ④ 质量门（共三道：测试 / 自审 / 真实浏览器 E2E）
#    推荐一键跑（会自动重启 8101 并带上限流预算；该脚本在**工作区**、不在项目内）：
pwsh -File "D:\Aruanjian_coding_tools\agent_workspace\dsh\risk\03_校验脚本\run_all_gates.ps1"
#    或手工分步：
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider   # 期望 1182 passed, 1 skipped
.\.venv\Scripts\python.exe scripts\self_audit.py                    # 期望 全部审计项通过
#    第 3 道门 E2E 要求**带限流预算**起服务（否则随机 429，见 TESTING.md「故障 3」）。
#    完整步骤与四个常见故障的排查顺序：见 TESTING.md。
```

### 演示账号（**仅限演示，请勿用于任何真实环境**）

| 账号 | 初始口令 | 角色 | 可访问页面 |
|---|---|---|---|
| `admin01` | `admin123` | 系统管理员 | 态势大盘 · 审计日志 · 系统设置 |
| `strategy01` | `strategy123` | 风控策略师 | 态势大盘 · 策略与规则 · 事件仿真 |
| `reviewer01` | `reviewer123` | 风控审核员 | 态势大盘 · 审核工作台 · 事件仿真 |

> 口令以 **bcrypt 加盐**存储在 `sys_users.password_hash`，库中不存在明文。
> 登录页底部也有同样提示；生产环境必须改为强制首登改密。

---

## 2. 目录结构

```
shop_risk_control/
├── .venv/                        Python 3.12.8 虚拟环境
├── .env                          真实配置（**含 JWT 密钥，严禁入库**）
├── .env.example                  配置模板（键齐全、值留空）
├── main.py                       根入口（re-export app.main:app）
├── requirements.txt              依赖清单
├── README.md / README_切片1.md   本文件 / 阶段一交付记录（历史留存）
│
├── app/                          后端应用
│   ├── main.py                   应用装配（lifespan、中间件、静态页）
│   ├── config.py                 配置加载与启动校验（fail-fast、密钥脱敏打印）
│   ├── constants.py              集合名 / 索引声明 / 分页与阈值（唯一声明处）
│   ├── enums.py                  22 组枚举唯一真源（value + 中文 label）
│   ├── errors.py                 统一响应包 + 全量错误码表 + 异常处理器
│   ├── logging.py                结构化日志 + trace_id contextvar + 脱敏过滤器
│   ├── db.py                     AsyncMongoClient 单例 + 索引初始化
│   ├── deps.py                   当前用户 / require_permission / 限流键
│   ├── protocols.py              FeatureStore · BizAdapter · ModelEngine（AD-05/08/09）
│   ├── security/                 鉴权基础件：password · jwt · permissions · login_guard
│   ├── middleware/               auth_middleware（纯 ASGI，默认拒绝）
│   ├── core/                     有状态的跨模块关注点：degraded（降级）· ratelimit（限流）
│   ├── utils/                    无状态工具：ids · timeutil · mask
│   ├── schemas/                  list_schema · auth_schema
│   ├── repos/                    list_repo · user_repo（PyMongoError → 契约错误码）
│   ├── services/                 list_service · auth_service · audit_log（哈希链写入）
│   └── api/                      路由注册器 + common_api · auth_api · list_api
│
├── static/                       前端（零构建 ES module，零 CDN）
│   ├── index.html                单页壳（未登录只渲染登录视图）
│   ├── css/app.css               主题变量 + 公共组件样式
│   ├── js/                       app.js（壳+路由）· api.js · ui.js · store.js · session.js
│   │   └── views/                registry.js（路由表）· login.js · lists.js
│   └── vendor/echarts.min.js     本地 vendor（D-01：不从 CDN 加载）
│
├── scripts/
│   ├── seed.py                   幂等种子（建索引 + 场景 + 名单 + 账号，支持 --reset）
│   └── self_audit.py             可维护性自审
│
├── tests/
│   ├── conftest.py               夹具（独立测试库 + RoleClient 自动换真令牌）
│   ├── test_list_slice.py        名单库主干（模块 06-A）
│   ├── test_deep_audit.py        边界与契约细节
│   ├── test_auth.py              模块 01 验收（V-01-01~14）+ 安全回归
│   ├── test_errors.py            错误码语义与唯一性（表驱动）
│   ├── test_enums.py             枚举组与实体文档逐组一致
│   ├── test_config.py            配置优先级 / fail-fast / 密钥不泄露
│   └── test_contract.py          统一响应包 / 分页契约 / 框架级错误
│
└── Spec_coding_步骤/             设计交付物
    ├── PRD.md
    ├── 01_数据实体/              21 个实体（E01~E21）+ 悬空点
    ├── 02_概要设计/              选型 / 架构 / 流程 / 数据结构
    ├── 03_原型图/                9 个 .pen + 评审文档
    └── 04_模块Spec/              14 份模块 Spec + 待确认汇总 + **决策记录（D1~D30）**
```

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12.8 |
| Web | FastAPI 0.141 + uvicorn 0.53 |
| 校验 | Pydantic v2 |
| 存储 | MongoDB（`AsyncMongoClient`，pymongo 4.18 自带异步客户端，**不用 motor**） |
| 鉴权 | PyJWT（HS256）+ bcrypt（**不用 passlib**，理由见 15_决策记录 D22） |
| 前端 | 零依赖、零构建 ES module + 原生 JS；ECharts 本地 vendor |
| 测试 | pytest + anyio（异步，不用 pytest-asyncio）+ httpx ASGITransport |

---

## 4. 当前实现范围

> **15 个模块（00 ~ 13）已全部交付并前后端打通。** 下面这份清单是**按开发批次追加的历史记录**（保留了各批交付当时的措辞，不再逐批回改）；**要了解当前范围、模块职责与验收方式，请直接看 [`SUMMARY.md`](SUMMARY.md)**。
> 一句话分层：`app/api`（路由与契约）→ `app/schemas`（Pydantic 模型）→ `app/services` / `app/engine`（业务与判定）→ `app/repos`（Mongo 访问）；前端在 `static/js`（零构建 ES module，`views/` 按菜单注册）。

**模块 00 · 公共基础（已验收）**
- 统一响应包 `{ok, code, message, trace_id, data}`；`X-Trace-Id` 头
- 全量错误码体系（COM/AUTH 已落地，前缀一模块一前缀）+ 兜底 `COM-5000`
- 结构化日志 + trace_id 全链路 + 脱敏过滤器（手机号/地址/密钥）
- 配置 fail-fast + 启动脱敏打印；`/health` · `/api/v1/common/enums` · `/api/v1/common/meta`
- 单页壳 + hash 路由 + 权限化菜单 + 公共组件库；本地 vendor ECharts

**模块 01 · 登录与权限鉴权（本次交付）**
- `POST /api/v1/auth/login` · `GET /api/v1/auth/me` · `POST /api/v1/auth/logout` · `POST /api/v1/auth/password`
- JWT（HS256，8 小时，`jti` 预留）+ bcrypt 口令 + 登录失败锁定（默认 `10` 次 / `60` 秒，见 §6 `LOGIN_MAX_FAILURES`）
- 三角色权限矩阵（`security/permissions.py` 唯一真源）→ 菜单与接口双重校验
- 鉴权中间件**默认拒绝**（白名单之外一律要求令牌）+ 权限依赖 `require_permission()`
- 越权写操作留痕 `auth.denied`（审计哈希链写入）
- 前端：登录页、会话管理、401 自动跳登录、403 只提示不跳页、菜单按权限渲染

**模块 06-A · 多维名单库（阶段一已交付）**
- `GET/POST /api/v1/lists` · `scene-usage` · `impact`；唯一约束、脱敏、默认有效期、fail-closed

**模块 12 · 审计日志（本次交付）**
- E16 `audit_logs` 的**哈希链**：`sha256(prev_hash + "|" + 规范化内容)`，创世哈希 `GENESIS`，唯一实现 `app/engine/audit_hash.py`
- 写入：内存队列 + **单消费者串行化**（AD-04）+ 重试 3 次指数退避 + `strict` 语义（紧操作失败 → `AUD-5001` **阻断业务**）
- `GET /api/v1/audit/logs`（四维筛选 + 分页）· `/verify`（**真实逐条重算**，返回首个不一致位置）· `/export`（CSV/Markdown，导出本身也留痕）· `/actors`
- 前端审计页：完整性校验卡片、流水表、**行展开 before/after 并排 diff**（新增绿/删除红/修改黄）

**模块 11 · 监控指标引擎（本次交付，无独占页面）**
- 指标桶**写入时增量聚合**（AD-03：`$inc` upsert，禁读改写）；四维度 `global`/`level`/`scene`/`rule`
- 时间桶 `1m`/`1h`/`1d`（日界按 `Asia/Shanghai`）+ 保留策略（`expire_at` TTL：1m 7 天 / 1h 90 天 / 1d 永久）
- 7 个接口：`overview` · `trend` · `distribution` · `rule-ranking` · `throughput` · **`stream`(SSE)** · `rollup`
- 派生量服务端算好（拦截率/均值/P95），分母 0 返回 `null` 而非 0；查询 3s 缓存 + single-flight；Mongo 不可用返回快照 + `stale=true`
- SSE：心跳 15s、`Last-Event-ID` 补偿（环形缓冲 200）、订阅队列 500 背压丢最旧、订阅上限 50、`retry: 3000`

**以下各项均已在后续批次交付**（原文写作"未实现"是当时的进度快照）：规则 CRUD 与条件树、名单移除/批量导入、事件网关、特征计算、
画像图谱、决策引擎、大盘、案件、仿真、系统设置 —— 见 [`SUMMARY.md`](SUMMARY.md)。

---

## 5. 权限矩阵（唯一真源：`app/security/permissions.py`）

| 权限 | reviewer | strategist | admin |
|---|:--:|:--:|:--:|
| `dashboard:read` | ✔ | ✔ | ✔ |
| `case:read` / `case:dispose` | ✔ | — | — |
| `rule:write` / `list:write` | — | ✔ | — |
| `list:read` | ✔ | ✔ | ✔ |
| `sim:run` | ✔ | ✔ | — |
| `audit:read` / `sys:config` / `account:manage` / `engine:config` | — | — | ✔ |

> 除共享权限（`dashboard:read` / `list:read` / `sim:run`）外，每项权限**只属于一个角色**，
> 该约束由 `test_auth.py::test_duties_are_separated` 强制守住（BR-01-13 职责分离）。
> 前端**不推导权限**：菜单由 `/auth/me` 下发的 `permissions` 决定（BR-01-12）。

---

## 6. 配置

`.env`（**含 JWT 密钥，严禁入库**；`load_dotenv(override=False)` 保证环境变量优先）：

| 变量 | 必需 | 说明 |
|---|:--:|---|
| `MONGO_URL` | ✔ | MongoDB 连接串 |
| `MONGO_DB_NAME` | ✔ | 业务库名（`risk_control`） |
| `JWT_SECRET` | ✔ | JWT 签名密钥，**≥ 32 字节**；缺失或过短则启动失败（`AUTH-5002`） |
| `JWT_EXPIRE_MINUTES` | | 令牌有效期，默认 `480`（8 小时） |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCK_SECONDS` | | 登录失败锁定，默认 `10` 次 / `60` 秒；窗口与锁定时长同值 |
| `TEST_MONGO_DB_NAME` | | 测试库名（默认 `{MONGO_DB_NAME}_test`） |
| `APP_PORT` / `APP_ENV` / `LOG_LEVEL` | | 端口 / 环境 / 日志级别 |
| `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SEC` | | 单用户限流，默认 60 次 / 60 秒（`COM-4290`） |

> 生成密钥：`python -c "import secrets;print(secrets.token_hex(48))"`

---

## 7. 已知边界与后续待办

| 事项 | 说明 |
|---|---|
| 登出不是服务端真失效 | 无状态 JWT，仅前端清 token；`jti` 字段已预留，引入黑名单表即可开启 |
| 无刷新令牌 | 8 小时后需重新登录（演示场景够用，见 01 §8） |
| 限流与登录锁定是**进程内**计数 | 多 worker 部署时额度为 `N × 配置值`，生产需换 Redis |
| 未登录访问不存在的路径返回 401 | 有意为之（不暴露路径是否存在）；已登录时才是 404 |
| 权限矩阵 G-11 | 从 PRD 2.3.2 推导，**需与老师确认**（尤其"审核员能否看规则配置"） |
| 模型引擎 | 架构预留（`NullModelEngine`，`model_score` 恒 `null`），见 G-01 |
