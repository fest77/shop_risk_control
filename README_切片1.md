# 阶段一 · 名单库垂直切片

> **切片**：多维名单库（`list_entries`，实体 E07）的 **列表查询 + 新增**
> **状态**：已实现并**在真实 MongoDB 上端到端验证通过（21/21 测试 + 真实 HTTP 冒烟）**
> **上游依据**：`Spec_coding_步骤/04_模块Spec/06_规则与名单配置管理.md` §3.2 / §4.3 / §5.1 / §6
> **归属模块**：06 规则与名单配置管理（管理时写入侧）

> ⚠️ **这是阶段一（切片 1）的历史快照 —— 不要拿它来校验收整个项目。**
> 它记录的「21/21 测试 + 真实 HTTP curl 冒烟」是**切片 1 当时的验证方法，早已被取代**。
> 现在请用 [`TESTING.md`](TESTING.md)（三道门：`pytest` 1182 passed / `self_audit` / 浏览器 E2E 165-165）
> 与 [`SUMMARY.md`](SUMMARY.md)（完整验收指南）做验收。

---

## 1. 假设与范围

### 1.1 为什么选这个切片

| 候选 | 判断 |
|---|---|
| 规则 CRUD | 条件树是嵌套 JSON，新增要跨模块转调 05 的 `validate_tree()` → 不是"最小" |
| 事件 → 决策 | 需 03/04/05 三个模块 + 内存特征窗口 → 严重超范围 |
| **名单库（选中）** | **最扁平的实体**（无嵌套、无状态机、无统计），但完整贯穿<br/>UI 表单/表格 → REST → Pydantic → 服务层不变量 → MongoDB 唯一索引 → 集成测试 |

它同时能覆盖 **3 条硬业务规则 + 3 个错误码 + 分页契约 + fail-closed**，用最小面积验证全链路。

### 1.2 已按最小合理假设处理的事项（Spec 未明确处）

| # | 事项 | 本切片的取值 | 说明 |
|---|---|---|---|
| A1 | 响应包 `{code, message, trace_id, data}` 中成功时的 `code` 取值 | `"OK"` | 模块 00 只约定了包结构，未约定成功码字面量 |
| A2 | 名单条目 `_id` 的格式 | `L` + 12 位十六进制 | E07 只说"名单条目 ID"，未规定格式 |
| A3 | `list_type` 非法时的错误码 | 复用 `CFG-4010` | 见 §8 发现 2 |
| A4 | 身份来源 | 请求头 `X-Operator` / `X-Role` | 模块 01（JWT）不在本切片，见 §1.3 |
| A5 | 测试库 | `risk_control_test`（`TEST_MONGO_DB_NAME`） | 避免污染业务库，属工程惯例 |
| A6 | `created_at` 字段 | 落库，与 `effective_at` 同值 | 见 §8 发现 1 |
| A7 | 写成功后清除降级标记 | 是 | DB 写成功即视为恢复健康 |

### 1.3 明确**不在**本切片范围（不扩展）

| 项 | 归属 |
|---|---|
| JWT 登录 / RBAC 完整实现 | 模块 01（本切片用请求头承载身份，**但权限规则真实生效且已被测试覆盖**） |
| 名单移除（DELETE）、批量导入、模板下载 | 模块 06 的后续切片 |
| 名单过期清理定时任务（BR-06-31） | 模块 06 的后续切片 |
| 审计哈希链写入（BR-06-36） | 模块 12 |
| 名单 TTL 缓存失效（BR-06-23） | 模块 05（本切片无决策链路，**故不存在需要失效的缓存**） |
| 规则 CRUD / 条件树 | 模块 06 的规则部分 + 模块 05 |
| 事件、特征、决策、案件、画像、大盘、仿真 | 03/04/05/07/08/09/02/10/11 |

> **关于权限的边界说明（重要）**：`POST /api/v1/lists` 的契约要求执行 `BR-06-34`（仅
> `strategist` / `admin` 可写，否则 `403 CFG-4031`）。若不实现身份，这条规则无法被验证。
> 因此本切片以**请求头承载身份**作为模块 01 落地前的最小身份来源——**这不是占位符**：
> 权限判定在此真实生效、有对应测试；接入模块 01 后只需把 `app/deps.py` 的两个依赖改为
> 解析 JWT，业务规则与错误码不变。

---

## 2. 文件树（本切片新增/修改）

```
shop_risk_control/
├── main.py                          [改] 根入口，re-export app.main:app
├── .env                             [已有] MONGO_URL / MONGO_DB_NAME
├── app/
│   ├── __init__.py                  [新]
│   ├── config.py                    [新] 全部配置来自环境变量，无硬编码密钥
│   ├── errors.py                    [新] 错误码 + 统一响应包 + 异常处理器
│   ├── db.py                        [新] AsyncMongoClient + 索引初始化
│   ├── deps.py                      [新] 身份与角色依赖（模块 01 落地前的边界方案）
│   ├── core/
│   │   ├── __init__.py              [新]
│   │   ├── masking.py               [新] 手机号脱敏（BR-06-21，模块 00 共享函数）
│   │   └── degraded.py              [新] 名单降级状态（BR-06-24 的可见化载体）
│   ├── schemas/
│   │   ├── __init__.py              [新]
│   │   └── list_schema.py           [新] 请求/响应模型 + 排序白名单解析
│   ├── repos/
│   │   ├── __init__.py              [新]
│   │   └── list_repo.py             [新] list_entries 读写（含部分唯一索引兜底）
│   ├── services/
│   │   ├── __init__.py              [新]
│   │   └── list_service.py          [新] 全部业务不变量（可注入假仓储单测）
│   ├── api/
│   │   ├── __init__.py              [新]
│   │   └── list_api.py              [新] 3 个路由 + 1 个影响面查询
│   └── main.py                      [新] 应用装配：lifespan / trace_id / 异常 / 静态页
├── static/
│   ├── index.html                   [新] 零依赖单页壳（清单库视图）
│   └── front/
│       └── list_lib.js              [新] 名单库视图（原生 JS，无框架无构建）
├── scripts/
│   └── seed_lists.py                [新] 幂等种子数据
└── tests/
    ├── __init__.py                  [新]
    ├── conftest.py                  [新] 测试夹具（独立测试库 + anyio）
    └── test_list_slice.py           [新] 21 个端到端/集成测试
```

**共 24 个文件。**

---

## 3. 依赖安装

**无需安装任何新依赖。** 本切片只使用 Spec 已声明的技术栈，且全部已在 `.venv` 中：

| 依赖 | 版本（已装） | 用途 |
|---|---|---|
| fastapi | 0.141.1 | Web 框架 |
| uvicorn | 0.53.0 | ASGI 服务器 |
| pydantic | 2.13.5 | 请求校验 |
| pymongo | 4.18.1 | **`AsyncMongoClient`**（无需 motor） |
| python-dotenv | 1.2.3 | 读 `.env` |
| httpx | 0.28.1 | 测试客户端（ASGITransport） |
| pytest | 9.1.1 | 测试框架 |
| anyio | 4.15.1 | **异步测试**（pytest 插件随 starlette 一起装好） |

---

## 4. 数据库：索引与种子

**不使用迁移框架**（Spec 未选型）。索引由**应用启动时自动创建**（`app/db.py: ensure_indexes`），
因此不存在"忘了跑迁移"的问题。

创建的索引：

| 名称 | 定义 | 用途 |
|---|---|---|
| `uq_active_entry` | `(list_type, entity_type, entity_value)` **唯一**，`partialFilterExpression={status:"active"}` | 唯一约束的**真正落地点**（BR-06-20）；应用层"先查再插"在并发下必然漏判 |
| `ix_status_expire` | `(status, expire_at)` | 过期清理任务（后续切片） |

种子数据（幂等，重复执行不产生重复条目）：

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control
.\.venv\Scripts\python.exe scripts\seed_lists.py
```

预期输出：新增 6 条（黑 3 / 白 2 / 灰 1），打印库名与累计条数。

---

## 5. 启动命令

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8101
```

浏览器打开 **http://127.0.0.1:8101/** → 名单库页面。

> 启动即会 `ping` MongoDB 并建索引；**连不上会直接启动失败**，不会出现"起来了但一写就炸"。

---

## 6. 验证清单（逐条可执行）

### 6.1 自动化测试（最重要，一条命令）

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control
.\.venv\Scripts\python.exe -m pytest tests -v
```

**预期：21 passed**（使用独立测试库 `risk_control_test`，不影响业务库）。
若 MongoDB 不可达，用例会 `skip` 并提示地址，不会假绿。

### 6.2 手工冒烟（逐条）

| # | 操作 | 预期 |
|---|---|---|
| 1 | `curl http://127.0.0.1:8101/health` | `status:"ok"`、`mongo.ok:true`、显示库名 `risk_control` |
| 2 | 浏览器打开 `/` | 出现「多维名单库」页面：左侧边栏 + 三个名单 tab（带计数）+ 筛选工具条 + 表格 |
| 3 | 点「白名单」tab | 表格切到白名单，`1390000001` 显示为 **`139****0001`**（脱敏） |
| 4 | 点「灰名单」tab | `117.136.12.88` 的失效时间为**当前日期 + 30 天**；黑白名单的为「永久」 |
| 5 | 筛选「实体类型=user」→ 查询 | 只剩 `U000128`（黑）；白名单 tab 下为 `U009999` |
| 6 | 点「＋ 新增名单」→ 填 black/device/`D0001`/原因 → 保存 | 顶部绿色提示「新增成功」，列表出现该条，tab 计数 +1 |
| 7 | **再保存一次同样内容** | 顶部红色提示 `[CFG-4009] 该实体已存在于本名单…`（唯一约束生效） |
| 8 | 新增 white/user/`U000128`/原因 | 红色提示含「**白名单**」与「**黑名单优先**」→ 跨名单冲突 |
| 9 | 勾选 `force` 再保存一次 | 成功（同实体跨名单并存） |
| 10 | 把右上角身份切为「风控审核员」→ 保存任意条目 | 红色提示 `[CFG-4031] 当前角色（reviewer）无策略配置权限` |
| 11 | 用 reviewer 身份点「查询」 | **正常返回列表**（只读可见，BR-06-35） |
| 12 | `curl "http://127.0.0.1:8101/api/v1/lists?list_type=black&page_size=101"` | `400` + `code:"CFG-4008"` |
| 13 | `curl "http://127.0.0.1:8101/api/v1/lists?list_type=black&page=999"` | `200` + 空 `items` + 正确 `total`（不报错） |
| 14 | `curl "http://127.0.0.1:8101/api/v1/lists?list_type=black&sort=operator:desc"` | `400` + `code:"CFG-4008"`（排序字段白名单） |
| 15 | `curl http://127.0.0.1:8101/api/v1/lists/scene-usage` | `degraded:false` |

> **fail-closed 的验证**由测试 `test_write_failure_sets_degraded_and_returns_5002` 覆盖
> （注入 Mongo 写失败 → 断言 503 `CFG-5002` 且 `degraded=true`）；手工不易复现，故不列入冒烟表。

---

## 7. 接口契约

### 7.1 统一响应包

```json
{ "code": "OK", "message": "查询成功", "trace_id": "<uuid hex>", "data": { } }
```

失败时 `code` 为错误码字符串（如 `CFG-4009`），HTTP 状态码按错误类型设置。
响应头带 `X-Trace-Id`，与包内 `trace_id` 一致，便于日志关联。

### 7.2 `GET /api/v1/lists` — 名单列表

| 参数 | 类型 | 必填 | 说明 |
|---|---|:--:|---|
| `list_type` | enum | ✔ | `black` / `white` / `gray` |
| `entity_type` | enum | | 五维之一 |
| `keyword` | string | | 按 `entity_value` 正则匹配（已脱敏后比较） |
| `status` | enum | | `active`（默认）/ `expired` / `removed` / `all` |
| `expire_before` | long | | 毫秒时间戳 |
| `page` / `page_size` | int | | 默认 1 / 20；`page>200`、`page_size>100` → `CFG-4008` |
| `sort` | string | | 默认 `effective_at:desc`；字段须在白名单内 |

`data`：`{items[], total, page, page_size, counts{black,white,gray}, as_of}`

### 7.3 `POST /api/v1/lists` — 新增

请求头：`X-Operator`（必填）、`X-Role`（必填，`strategist`/`admin`）。
请求体：

| 字段 | 类型 | 必填 | 约束 |
|---|---|:--:|---|
| `list_type` | enum | ✔ | 三值之一 |
| `entity_type` | enum | ✔ | 五维之一，否则 `CFG-4010` |
| `entity_value` | string | ✔ | ≤128；`phone` 维度**服务端强制脱敏** |
| `reason` | string | ✔ | 1~200 字 |
| `expire_at` | long\|null | | 缺省：黑/白永久、灰 30 天 |
| `force` | bool | | 默认 false；跨名单冲突时需 true |

**状态码**：`201` / `400 CFG-4008·4010·4012` / `403 CFG-4031` / `409 CFG-4009` / `503 CFG-5002`

### 7.4 辅助端点

| 端点 | 用途 |
|---|---|
| `GET /api/v1/lists/scene-usage` | 名单降级状态（`degraded` / `degraded_since` / `last_flush_at` / `last_error`） |
| `GET /api/v1/lists/impact?entity_type=&entity_value=` | 某实体在效名单条数（移除确认弹窗的影响面，BR-06-26） |
| `GET /health` | 健康检查（含 Mongo 连通性） |

---

## 8. 数据模型

集合 `list_entries`（E07），本切片写入的文档：

| 字段 | 类型 | 本切片的取值 |
|---|---|---|
| `_id` | string | `L` + 12 位十六进制 |
| `list_type` | string | black/white/gray |
| `entity_type` | string | user/phone/ip/device/address |
| `entity_value` | string | `phone` 已脱敏为 `139****0001` |
| `reason` | string | 用户填写 |
| `source` | string | 本切片固定 `manual` |
| `related_case_no` | null | 本切片不产生（处置自动写入属模块 08） |
| `effective_at` | long | 写入时刻 |
| `created_at` | long | 与 `effective_at` 同值（见发现 1） |
| `expire_at` | long\|null | 黑/白 `null`；灰 默认 +30 天 |
| `status` | string | `active` |
| `operator` | string | 来自 `X-Operator` |

---

## 9. 实现过程中发现的 Spec 问题（**需你确认**）

| # | 发现 | 位置 | 本切片处理 | 建议 |
|---|---|---|---|---|
| **1** | **E07 字段表没有 `created_at`，但模块 06 §3.2 的响应契约要求返回 `created_at`** | `01_数据实体` E07 vs `04_模块Spec/06` §3.2 | 按**响应契约**落库（与 `effective_at` 同值） | 二选一：① 在 E07 补 `created_at` 字段；② 从响应契约中删除 `created_at` |
| **2** | **§5.1 标题写「错误码清单（前缀 `RUL`）」，但表内全部是 `CFG-` 码** | 模块 06 §5.1 | 按 `CFG-` 实现 | 标题笔误，建议改为 `CFG` |
| **3** | **未定义 `list_type` 非法的错误码**（只定义了 `entity_type` 非法的 `CFG-4010`） | 模块 06 §5.1 | 复用 `CFG-4010` | 建议补一个 `CFG-4013 名单类型非法`，或明确复用 4010 |

**实现笔记（非 Spec 问题）**：pymongo 异步 API 有不对称之处——`AsyncCollection.find()` 直接返回游标，
而 `aggregate()` 是**协程**需先 `await`。首轮实现因此 6 个用例失败，已修正。

---

## 10. 已知偏差

| 偏差 | 原因 | 影响 |
|---|---|---|
| 身份来自请求头而非 JWT | 模块 01 未实现 | 无安全影响（本机演示）；接入 01 后改 `app/deps.py` 两处依赖即可 |
| 无审计写入 | 模块 12 未实现 | `BR-06-36` 未生效；**不影响本切片的读写正确性** |
| 无名单缓存失效 | 模块 05 的 TTL 缓存未实现 | 本切片无决策链路，无缓存可失效 |
| 无过期清理任务 | 属后续切片 | 过期条目仍可被 `status=expired` 筛出，但不会自动置为 expired |

---

## 11. 验证结论（最终）

| 项 | 结果 |
|---|---|
| 自动化测试 | **43 passed / 0 failed**（21 主干 + 22 深度审计，真实 MongoDB） |
| 可维护性自审 | **全部通过**（`scripts/self_audit.py`，退出码可用于 CI） |
| 真实 HTTP 冒烟 | 15 项修复项 + 回归项 **全部符合预期** |
| 唯一约束 | 部分唯一索引生效；**5 并发写同一实体 → 恰好 1 成功 4 冲突** |
| fail-closed | 注入写失败 → `503 CFG-5002` + `degraded=true`；成功后自动清除 |
| 脱敏 | 库里存 `139****0001`；**明文与脱敏两种关键词都能搜到**（共用同一函数） |
| 无副作用 | 测试用独立库 `risk_control_test`；业务库仅留 6 条种子数据 |

---

## 12. 深度自审结果（第二轮）

第一轮只验证"主干能跑"。第二轮我针对**边界、契约细节与可维护性**另写了 22 个探针，
**抓出 4 个真实缺陷**——这些在第一轮的 21 个用例里全部漏过：

| # | 缺陷 | 后果 | 修复 |
|---|---|---|---|
| **D-1** | `page=0` 返回通用校验错误码，而非契约 §3.5 要求的 `CFG-4008` | 前端按错误码分流会走错分支 | 把 `Query(ge=1)` 的边界判断移回服务层，统一抛 `CFG-4008` |
| **D-2** | **搜索时未按 BR-06-21 脱敏** —— 用户粘贴明文手机号搜不到 | 库里存 `139****0001`，用户手上是 `13900000001`，**功能直接不可用** | 关键词形如明文手机号时，同时用脱敏形态匹配（`$or`） |
| **D-3** | 纯空白 `reason="   "` 通过校验 | `min_length=1` 数的是 3 个空格，脏数据入库 | 模型层 `field_validator` 统一 strip 并拒绝空白 |
| **D-4** | Mongo 读失败返回 `500`，概要设计 §5.3 要求 `503` | 客户端会把"依赖不可用"当成"服务端 bug"，重试策略失效 | 读路径捕获 `PyMongoError` → `503 CFG-5001` |

同时修掉 3 处**可维护性缺口**：

| # | 问题 | 处理 |
|---|---|---|
| M-1 | `now_ms()` 在 `core/degraded.py` 与 `services/list_service.py` 各写一份 | 抽出 `app/core/clock.py` 统一 |
| M-2 | 未使用的 import（`ASCENDING` / `Any` / 测试里的 `READER`） | 移除，并由自审脚本持续把关 |
| M-3 | 前端缺概要设计 §3.5 要求的两项：**AbortController 丢弃乱序响应**、**空态差异化文案** | 均已补上；失败时保留上次结果不清空 |

**新增交付**：`scripts/self_audit.py` —— 5 项自动审计（未用导入 / **前端 HTML id 与 JS 引用一致性** / OpenAPI 覆盖 / 调试残留 / 硬编码密钥）。
其中"前端 id 一致性"检查已发现一次假警报（`showBanner('bannerErr')` 是间接引用），脚本已补上该模式。

**已知遗留（非缺陷，需后续切片处理）**：
`expire_at` 允许写入过去时间（当前行为已被测试钉住，若将来要拒绝需新增错误码）；
OpenAPI 未声明响应体 schema（返回的是统一包，声明需引入泛型响应模型）。

