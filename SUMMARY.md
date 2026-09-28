# shop_risk_control · 项目总览与验收指南

> **状态：已完成。** 15 个模块（00 ~ 13）全部交付，**最终整体验证三道门全绿**：
>
> | 门禁 | 命令 | 最终结果 |
> |---|---|---|
> | 单元/集成测试 | `pytest tests -q -p no:cacheprovider` | **1182 passed, 1 skipped, 0 failed** |
> | 静态自审 | `python scripts/self_audit.py` | **全部审计项通过**（OpenAPI **62** 条路径） |
> | 真实浏览器 E2E | `node check_frontend_e2e.js` | **165 / 165 通过**，无 JS 运行时错误 |
>
> 三道门均为**独占运行**（不与其它测试进程并发）实测所得。详见文末「如何验收」。

---

## 1. 技术栈与运行前置

| 项 | 取值 |
|---|---|
| 语言 / 运行时 | Python **3.12**（虚拟环境 `.venv/`） |
| Web 框架 | FastAPI + uvicorn（**单进程单写者**，`--workers 1` 是硬约束） |
| 数据校验 | Pydantic **v2** |
| 数据库 | MongoDB **`192.168.6.170:27017`**；业务库 `risk_control`，测试库 `risk_control_test` |
| Mongo 驱动 | **pymongo `AsyncMongoClient`**（**未使用 motor**） |
| 鉴权 | PyJWT(HS256) + **bcrypt 直调**（**刻意不用 passlib**：1.7.4 与 bcrypt 5.0 不兼容） |
| 测试 | pytest + **anyio**（未用 pytest-asyncio），httpx `ASGITransport` |
| 前端 | **零构建 ES 模块**（无 npm / 无打包器 / 无 CDN），图表用**本地** `static/vendor/echarts.min.js` |
| 外部依赖 | 无新依赖；模型引擎与业务系统均为**占位实现**（`NullModelEngine` / `MockBizAdapter`） |

**配置**：`.env`（参考 `.env.example`）——含 `JWT_SECRET`、`LOGIN_MAX_FAILURES`、`LOGIN_LOCK_SECONDS` 等；启动期 `config.validate()` **fail-fast**（缺配置拒绝启动）。

---

## 2. 代码结构

```
shop_risk_control/
├── main.py                     # 启动入口（转发 app.main:app）
├── requirements.txt / .env / .env.example / .gitignore
├── README.md / README_切片1.md # 分阶段开发记录
├── PROJECT_OVERVIEW.md         # ← 本文件
│
├── app/                        # 后端（119 个 .py）
│   ├── config.py               # 环境配置 + 启动期校验（fail-fast）
│   ├── constants.py            # ★ 集合名(COLL_*) + 索引(INDEX_SPECS) + 阈值：索引只在这里声明
│   ├── enums.py                # 全部枚举（22 组）
│   ├── errors.py               # ★ 统一响应包 envelope + 各模块错误码表 + 异常族
│   ├── logging.py              # 结构化日志 + trace_id + 脱敏过滤器
│   ├── db.py                   # Mongo 连接 / bootstrap / ensure_indexes
│   ├── deps.py / protocols.py  # 依赖注入 / ★ 跨模块冻结契约（Components 与四个 Provider）
│   ├── main.py                 # ★ lifespan（配置校验→静态自检→db→审计消费者→三个定时任务）+ 中间件装配
│   ├── middleware/             # 鉴权中间件（纯 ASGI，默认拒绝）
│   ├── security/               # password(bcrypt) · jwt · permissions(★ 权限矩阵唯一真源) · login_guard
│   ├── engine/                 # 纯逻辑引擎（可单测、多数无 IO）
│   │   ├── audit_hash.py       #   审计哈希链（唯一实现）
│   │   ├── metric_*.py         #   指标桶/聚合/分位数（模块 11）
│   │   ├── feature_*.py        #   18 项特征计算 / 滑窗 / 静态基线（模块 04）
│   │   ├── condition.py        #   ★ 条件树结构 + validate_tree()（供 06 复用）
│   │   ├── evaluator/arbiter/list_filter/rule_engine/decision.py  # 规则决策链路（模块 05）
│   │   ├── case_state.py       #   案件状态机（模块 08）
│   │   └── tracer.py           #   五步链路回溯（模块 10）
│   ├── repos/                  # 数据访问（按集合划分，唯一写入口）
│   ├── services/               # 业务编排（含 audit_service 队列+strict 语义、feature_service 等）
│   ├── core/                   # 基础设施与后台任务
│   │   ├── ratelimit.py        #   滑动窗口限流（读 RATE_LIMIT_MAX_REQUESTS，默认 60/分钟/令牌）
│   │   ├── degraded.py         #   降级标记
│   │   ├── metric_rollup / list_cleanup_task / feature_sweep_task / case_maintenance_task  # 定时任务
│   │   ├── edge_writer.py      #   异步建边（模块 09）
│   │   └── confirm_token / biz_sync_retry / case_events.py     # 处置令牌 / 联动重试 / 案件事件
│   ├── schemas/                # Pydantic 请求/响应模型（*_schema.py）
│   ├── tasks/                  # 预留目录
│   └── api/                    # ★ 路由注册器（_API_MODULES 决定装配顺序）+ 各模块 *_api.py
│       └── __init__.py         #   register_router(module_id, router)（同 id 只能登记一次）
│
├── static/                     # 前端（零构建，29 个 js）
│   ├── index.html
│   ├── css/app.css · dashboard.css
│   ├── vendor/echarts.min.js   # 本地 ECharts（禁止 CDN）
│   └── js/
│       ├── app.js(壳+hash 路由) · api.js(Bearer/401/403) · ui.js(组件库) · store.js · session.js
│       ├── format.js · stream.js(SSE)
│       ├── charts/{echarts,trend,doughnut,ranking}.js
│       └── views/              # ★ 页面级视图
│           ├── registry.js     #   权限化路由表（菜单与 hash 的唯一来源）
│           ├── login.js · dashboard.js(02) · lists.js(06-A) · audit.js+diff.js(12)
│           ├── profile.js      #   ★ 09 画像卡+图谱 与 05 判定摘要+命中明细（同页两栏）
│           ├── case_list.js / case_dispose.js(08)  ·  chain.js(10)
│           ├── rules.js / cond_tree.js / rule_editor.js / scenes.js / cfg_tabs.js(06-B)
│           └── settings.js(13)
│
├── scripts/
│   ├── seed.py                 # 演示数据（幂等；`--reset` 清空后重灌）
│   └── self_audit.py           # ★ 静态自审：路由契约/未使用 import/调试残留/硬编码密钥
│
├── tests/                      # 58 个 .py：conftest + 各模块测试 + *_testlib.py
│
├── Spec_coding_步骤/           # 需求与设计真源（21 份 md + 原型 .pen）
│   ├── PRD.md · 01_数据实体/ · 02_概要设计/ · 03_原型图/
│   └── 04_模块Spec/            #   00~15 各模块 Spec + 15_决策记录.md（D1~D70 全部裁定）
│
└── logs/                       # 运行日志（服务端日志统一落这里）
```

★ = 关键文件：**改之前先读它**。

---

## 3. 15 个模块与各自作用

> 每个模块在 `Spec_coding_步骤/04_模块Spec/` 下有独立 Spec；错误码**一模块一前缀**。

| 步 | 模块 | 错误码前缀 | 作用 |
|---|---|---|---|
| 00 | **公共基础与骨架** | `COM` | 统一响应包 `{ok,code,message,trace_id,data}`、错误码体系、日志+trace_id+脱敏、配置 fail-fast、枚举、路由注册器、单页壳+hash 路由、组件库、限流、种子脚本 |
| 01 | **登录与权限鉴权** | `AUTH` | `login/me/logout/password`、JWT(8h)、bcrypt、**权限矩阵唯一真源**、鉴权中间件**默认拒绝**、登录失败锁定 10 次/60 秒 |
| 02 | **态势大盘** | `DASH` | `#/dashboard`：4 张指标卡 + 3 张图（趋势/环形/排行）+ **SSE 实时事件流** + 筛选与下钻；**纯消费 11 的接口，不自己算指标** |
| 03 | **事件接入网关与模拟器** | `EVT` | 单条/批量接入、**幂等**、同编号不同载荷 409、`scene_extra` 防串味、事件模拟器、**fail-closed 降级**（`degrade.stage`） |
| 04 | **特征计算引擎** | `FEA` | **18 项特征**（与 E02 双向比对一致）、滑窗计数、E02 快照 + E21 静态基线、`degrade_suggested` 门控 |
| 05 | **规则决策引擎** | `RUL` | 名单过滤 → 条件树求值 → 分值累加（截断 100）→ 三档仲裁（pass/review/reject）；产出 E03 `decisions` + E04 `decision_hits`；**决策链路真正跑通的那一步** |
| 06-A | **多维名单库** | `CFG` | 名单 CRUD、批量导入（partial/atomic、真实行号、表头严格比对）、模板下载、影响面 |
| 06-B | **规则配置** | `CFG` | 规则 CRUD + 启停用 + **条件树编辑器**；**复用 05 的 `validate_tree()`**；`version` 随内容递增（决策重放前提） |
| 07 | **案件审核工作台** | `CASE` | `#/cases` 三栏（左案件列表 / 中画像与图谱 / 右判定与处置区）；案件**读侧**接口 + `case_query_service`；**一次详情拉取驱动三栏** |
| 08 | **案件处置与业务联动** | `DSP` | 处置二次确认弹窗、认领/处置/归档、处置流水、业务联动与重试；**由 review/reject 决策建案**（降级也建案） |
| 09 | **画像与关联图谱** | `GRP` | E10~E13 画像聚合 + **E14 关联边**；2 跳图查询（固定 3 次 Mongo 命令，非 N+1）、截断给真实总数；异步建边 |
| 10 | **事件仿真测试** | `SIM` | `#/sim`：用例模板 + 结构化事件表单 + **五步链路回溯（每步带数值耗时）** + 批量回放；**复用真实 `/engine/evaluate`**，且**仿真不写真实窗口/指标** |
| 11 | **监控指标引擎** | `MET` | 四维度增量聚合（E15 指标桶）、TTL 保留、7 接口（含 SSE + rollup）、3s 缓存/single-flight、`stale` 降级快照 |
| 12 | **审计日志** | `AUD` | **哈希链**（创世 `GENESIS`、`sha256(prev + 规范化 8 字段)`）、内存队列+单消费者+重试+`strict` 语义、查询/校验/导出，前端并排 diff |
| 13 | **系统设置与运行参数** | `SYS` | `#/settings` 四卡：运行参数（含"需重启生效"提示）、吞吐与健康度、**账号与角色管理**、决策引擎配置（模型引擎**如实标注未接入**） |

**跨模块契约（冻结，改签名前必读 `app/protocols.py`）**：
`FeatureProvider`（04→03）、`DecisionProvider`（05→03）、`LinkedUserCountProvider`（09→04）、`BizAdapter`、`FeatureStore`、`ModelEngine`。
`/health` 会打印当前**实际装配**的组件类名——**出现 `Unavailable*` / `Null*` 就说明该环节是占位实现**。

---

## 4. 如何验收此项目

### 4.1 一次跑完三道门（推荐）

工作区里有一个一键脚本（**不在项目内**，避免污染交付物）：

```powershell
pwsh -File "D:\Aruanjian_coding_tools\agent_workspace\dsh\risk\03_校验脚本\run_all_gates.ps1"
# 可选：-SkipE2E（只跑 pytest+自审）  -Restart（先重启 8101，自动带限流预算）
```

它会依次：重启/探活 8101 → pytest → self_audit → 浏览器 E2E，并打印汇总表。
**`/health` 的组件表会标注占位实现**（`Unavailable*`/`Null*`），便于一眼看出"哪些还没接上"。

### 4.2 手工分步

```powershell
# 0) 准备（当前目录 = 项目根）
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe scripts\seed.py            # 灌演示数据（幂等；--reset 清空重灌）

# 1) 起服务（★ 跑 E2E 前必须带限流预算，否则 165 条断言会随机 429）
$env:RATE_LIMIT_MAX_REQUESTS='600'
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8101 --log-level warning
# ★ 起来后必须回读一次 /health 确认：curl http://127.0.0.1:8101/health

# 2) 第 1 道门：测试
& .\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
#    期望：1182 passed, 1 skipped

# 3) 第 2 道门：静态自审
& .\.venv\Scripts\python.exe scripts\self_audit.py
#    期望：全部审计项通过（OpenAPI 62 条路径）

# 4) 第 3 道门：真实浏览器 E2E
Set-Location "D:\Aruanjian_coding_tools\agent_workspace\dsh\risk\05_浏览器校验"
node check_frontend_e2e.js "$PWD\e2e_shot.png"
#    期望：165/165 通过；同目录 e2e_shot*.png 为人工核对截图
```

**⚠️ 三条操作纪律（都踩过坑）**：
1. **`pytest` 是"单写者资源"**：`tests/conftest.py` 的 `prep_db` **逐用例清空测试库**，**两个 pytest 并发会互相清库**，产出跨十几个模块的**随机假失败**（同一份代码：独占跑 `0 failed`，并发跑曾出现 `122 failed`）。**跑之前先确认没有别的测试进程**。
2. **起服务后必须回读 `/health`**：只凭命令返回码可能"看着成功、其实没起来"。
   - 失败看日志区分：`No module named 'app'` = **工作目录不对**；`winerror 10048` = **端口被占用**（已有实例在跑，未必是故障）。
3. **E2E 前设 `RATE_LIMIT_MAX_REQUESTS=600`**：套件有 165 条断言，默认 60 请求/分钟（按令牌分桶）会随机 429，并连带污染「无 console.error」那条断言。

### 4.3 演示账号（种子提供）

| 账号 | 口令 | 角色 | 可见菜单 |
|---|---|---|---|
| `admin01` | `admin123` | 管理员 | 态势大盘 / 审计日志 / 系统设置 |
| `strategy01` | `strategy123` | 风控策略师 | 态势大盘 / 策略与规则 / 事件仿真 |
| `reviewer01` | `reviewer123` | 风控审核员 | 态势大盘 / 审核工作台 / 事件仿真 |

演示主路径（建议走查）：登录 `reviewer01` → `#/cases`（默认用户 `U000132`、演示事件 `EVT20260101900000000001`）→ 一屏可见 **画像卡 + 关联图谱 + 70 分判定摘要 + 2 条规则命中明细** → 点「待审案件数」下钻到案件列表 → 认领 → 处置（二次确认弹窗）→ 回到 `admin01` 看审计日志里的哈希链与并排 diff。
接口文档：`http://127.0.0.1:8101/docs`。

---

## 5. 关键设计约定（读代码前先看这几条）

- **统一响应包**：`{ok, code, message, trace_id, data}`，`ok` 由 `code == "OK"` 推出；响应头带 `X-Trace-Id`（未捕获的 500 也会带）。**只有文件下载（`/audit/export`、`/lists/import-template`）与 SSE（`/metrics/stream`）例外**。
- **分页统一**：`items/total/page/page_size/pages`；「共 N 条」的 N **必须**来自接口 `total`，前端不得自行按行计数。
- **写操作审计**：每次成功的写操作**恰好一条**审计；**审计失败则回滚该次写**。
- **并发冲突**：用条件更新（乐观锁）表达，冲突报明确错误码，前端给人话提示。
- **fail-closed**：决策链路任何依赖异常时，默认动作是 `review` 而**不是 `pass`**；**降级也必须建案**，否则这些请求会无人处理。
- **决策块契约**：由模块 05 拥有，03 只透传；`list_hit` 统一为**对象**；`model_score` 恒为 `null`（模型引擎为预留）；`rule_versions` 记**本次生效的完整规则集**（含未命中，供重放）。
- **截断不静默**：任何"取不全"都必须显式告知（图谱 `truncated` + `total_*`、指标 `partial`、审计 `truncated`…）。
- **权限矩阵唯一真源**：`app/security/permissions.py`；前端隐藏按钮**不替代后端鉴权**。

---

## 6. 已知缺口与残留（如实登记，不影响上述三道门）

| 项 | 说明 |
|---|---|
| **`RPAY001` 规则永远命不中** | `pay_fail_cnt_24h` 依赖 `order_pay.scene_extra.success`，而该键**不在模块 03 的 `scene_extra` 白名单**里 ⇒ 经真实入口**无法表达"支付失败"**，该特征恒为 0。跨 03/04/05，需裁定"改白名单"或"保留为缺口" |
| **02 有一条间歇性 E2E 取样竞态** | `pending_case_cnt` 只增不减（E2E 自身发的 `review` 会建案），而断言先读页面值、后查接口，两次读取之间可能错开。已定位到 `check_frontend_e2e.js` 的该断言块；最终一次运行为 165/165 |
| 性能类未压测 | `V-11-17`（40 万桶 P95）、`V-05-12`（1000 条 P95<50ms）、`V-09-15`（10 万实体 P95<300ms）均为**有界规模**验证，**未做大规模压测**（如实声明，未伪造达标） |
| `MET-5007` 日检复核 | Spec 提到该错误码但无接口/文件/调度落点，**声明为缺口** |
| 审计队列在内存 | 进程崩溃会丢 `strict=False` 的记录（含 `auth.denied`）；多 worker 会分叉哈希链，故**必须单进程** |
| 测试数据残留 | 业务库 `sim_cases` 有 5 条（seed 4 + 早期手工 1）、`users` 里有若干**软删**的 `e2e_tmp_13_*`（username 全局唯一、软删不释放名字）。要零残留：`python scripts/seed.py --reset` |
| 根目录残留文件 | `main.py.bak`、`uvicorn_07.log` / `uvicorn_07.log.err`（早期临时日志，**服务日志应统一落 `logs/`**）——可安全删除 |
| 无版本控制 | 该项目**不是 git 仓库**（无 `.git`），没有提交历史可追溯 |
| 外部系统为占位 | `NullModelEngine`（`model_score` 恒 `null`）、`MockBizAdapter`（业务联动为模拟；失败态只能用合成载荷验证） |

---

## 7. 需求与决策真源在哪

- **需求/设计**：`Spec_coding_步骤/`（PRD、数据实体 E01~E21、概要设计、原型 .pen、00~15 模块 Spec）。
- **全部架构裁定**：`Spec_coding_步骤/04_模块Spec/15_决策记录.md` —— **D1 ~ D70**，每条含"裁定 / 依据 / 影响面"。**遇到"两种做法都说得通"时，先查这里是否已裁定过。**

---

## 8. 一句话总结

**15 个模块的前后端已全部落地并通过三道门**（`pytest 1182 passed/0 failed` · 自审全过 62 路径 · 浏览器 E2E 165/165）。
剩下的不是"功能没做"，而是上表里那些**已知缺口、性能压测与数据清理**——它们都已如实登记，其中只有 `RPAY001` 那条需要一次产品口径裁定。


最后仍需你处理的收尾（都不影响上述测试）
1.项目根有早期临时文件 uvicorn_07.log / uvicorn_07.log.err 与 main.py.bak——可安全删除（服务日志应统一落 logs/）。
2.业务库残留：sim_cases 5 条、users 里若干软删的 e2e_tmp_13_*——跑 python scripts/seed.py --reset 即清零。
3.待你裁定 RPAY001 规则永不可达那条（order_pay.scene_extra.success 不在 03 的白名单里，详见项目根的 PROJECT_OVERVIEW.md §6）。
结论：总测试未发现功能缺陷。
