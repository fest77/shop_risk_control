# 自测教程（5 分钟跑完）

> 前提：Windows + PowerShell；MongoDB `192.168.6.170:27017` 可连通；项目在 `D:\A_Py_Java\pyFile\shop_risk_control`。
> 所有命令都在**项目根目录**执行。

---

## 一、最省事：一键跑三道门

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control

# 先灌一次演示数据（幂等；⚠️ 若报 DuplicateKeyError，见文末"故障 2"）
.\.venv\Scripts\python.exe scripts\seed.py

# 一键跑：自动重启服务 + 测试 + 自审 + 浏览器 E2E
pwsh -File "D:\Aruanjian_coding_tools\agent_workspace\dsh\risk\03_校验脚本\run_all_gates.ps1"
```

**看到这样就算全过**：

```
  服务           OK
  pytest       OK
  self_audit   OK
  E2E          OK
  三道门全绿 ✅
```

> **默认行为就是"自己重启 8101 并自动带上限流预算"**（想写成显式参数也行：`-Restart`，与默认**完全等价**）。
> 只有想**复用你在另一个窗口常驻的 uvicorn** 时才加 `-NoRestart` —— ⚠️ 且仅限"你没在脚本之外动过数据库"，原因见「故障 4」。
> 常用变体：`-SkipE2E`（只跑测试+自审，快）。
> 小提示：在**自己终端**里跑时别把它的输出接给 `| Select-Object`（脚本用 `Start-Process` 起的 uvicorn 会继承输出管道 → 前台表现为"卡住不退出"，其实门禁早就跑完并打印了汇总）。

---

## 二、想手动分步（更能看清每一步）

```powershell
cd D:\A_Py_Java\pyFile\shop_risk_control
$env:PYTHONIOENCODING='utf-8'

# 1) 灌演示数据
.\.venv\Scripts\python.exe scripts\seed.py

# 2) 起服务 —— 必须带限流预算，否则 E2E 会随机 429
$env:RATE_LIMIT_MAX_REQUESTS='600'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8101 --log-level warning
#   ↑ 这个窗口会一直占着（正常）。★ 起来后另开浏览器访问下面地址确认能开

# 3) 【另开一个 PowerShell 窗口】跑第 1 道门：测试
cd D:\A_Py_Java\pyFile\shop_risk_control
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
#   期望：1182 passed, 1 skipped

# 4) 第 2 道门：静态自审
.\.venv\Scripts\python.exe scripts\self_audit.py
#   期望：✅ 全部审计项通过（OpenAPI 共 62 条路径）

# 5) 第 3 道门：真实浏览器 E2E
cd D:\Aruanjian_coding_tools\agent_workspace\dsh\risk\05_浏览器校验
node check_frontend_e2e.js "$PWD\e2e_shot.png"
#   期望：结果：165/165 通过
```

---

## 三、手动点一遍（最能发现"显示不对"）

浏览器打开 http://127.0.0.1:8101/
用下面任一账号登录：

| 账号 | 口令 | 角色 | 能看到 |
|---|---|---|---|
| `reviewer01` | `reviewer123` | 风控审核员 | 态势大盘 / 审核工作台 / 事件仿真 |
| `strategy01` | `strategy123` | 风控策略师 | 态势大盘 / 策略与规则 / 事件仿真 |
| `admin01` | `admin123` | 管理员 | 态势大盘 / 审计日志 / 系统设置 |

**建议这样走一遍（每步只需看一眼"有没有数字/有没有图/有没有报错"）**：

1. **审核工作台（`reviewer01`）** ← 主路径
   - 左栏**案件列表**有数据、**案件号与用户能看全**（不是 `CASE2026...` ）、筛选与翻页可用；
   - 中栏画像卡四行齐全（手机号脱敏 / 设备指纹 / 来源IP / 收货地址）+ **关联实体图谱画出来了**（有 9 个圆圈和 5 类图例）；
   - 右栏 **70 分** + 「中风险/人审」+ **规则命中明细 2 条**（`+45 同设备聚集登录`、`+25 代理IP新账号登录`）。
2. **点左栏任意一行** → 中栏/右栏应**同时**刷新（不是各刷各的）。
3. **态势大盘** → 4 张卡片有数字、三张图有内容或规整空态、右上「自动刷新」可开关、事件流不报错。
4. **策略与规则（`strategy01`）** → 15 条规则可列、可新建/改分/启停用；条件树里选字段只能从 18 项特征里选。
5. **事件仿真** → 点一个用例 → 点「执行检测」→ 右侧**五步链路逐块点亮、每步带 ms 耗时**。
6. **审计日志（`admin01`）** → 有流水、点「校验」出「全链一致」、点一行看 before/after 并排 diff。
7. **系统设置（`admin01`）** → 改一个运行参数并保存 → 卡片提示「已立即生效」→ 刷新后**值还在**；账号列表不显示密码。
8. **接口文档**：http://127.0.0.1:8101/docs

**越权自测（应被拒）**：用 `reviewer01` 的浏览器手动访问 `#/settings`、`#/rules` → 应被拦回大盘并提示「无权访问该页面」。

---

## 四、四个常见故障（照这个顺序排查）

**故障 1：脚本报「/health 未就绪（60 次重试后仍失败）」**
只会是两种情况：① 你加了 `-NoRestart`，但 8101 上其实没有服务在跑；② 服务真的启动失败（脚本会把 `logs\gate_server.log.err` 尾部打印出来）。
```powershell
pwsh -File "...\run_all_gates.ps1"              # 默认就会自己把服务起起来
# 或：另开窗口按"第二节 第 2 步"常驻 uvicorn，再跑脚本并加 -NoRestart
```
排查用：`curl http://127.0.0.1:8101/health`。看日志时区分两类**完全不同**的原因：
`No module named 'app'` = **工作目录不对**（真故障）；`winerror 10048` = **端口被占用**（已有实例在跑，未必是故障）。

**故障 2：`seed.py` 报 `DuplicateKeyError: sim_cases index: uq_sim_case_name`**
库里有早期残留的同名仿真用例，而该集合 `name` 唯一。清掉重灌：
```powershell
.\.venv\Scripts\python.exe scripts\seed.py --reset
```

**故障 3：E2E 出现随机的 429 / 一批莫名其妙的失败**
- **限流**：跑 E2E 的服务必须带 `$env:RATE_LIMIT_MAX_REQUESTS='600'`（默认 60 请求/分钟，165 条断言扛不住）。
- **别并发**：`pytest` 会**逐用例清空测试库**，两个 pytest 一起跑会互相清库、产出**跨十几个模块的随机假失败**。跑之前确认没有别的测试进程。
- 判据：**失败集中在某个模块** = 真回归；**跨十几个模块均匀散开** = 测试库被并发清空。

**故障 4：E2E 里 V-05 那批断言成片变红（`GET /events/{id}` 恒 404）**
根因：事件幂等是**服务进程内的内存缓存**（`app/services/idempotency.py`，决策 D6），而 `scripts/seed.py --reset` 是**绕过服务直接清库**。
于是"库清了、进程里的幂等键还在" → 同一个 `event_id` 再 POST 会被判成 `duplicate` 直接短路、**永不落库**，详情查询就永远 404。
**纪律：凡是在脚本之外动过库（`seed.py` / `--reset`），就必须重启一次服务。** 一键脚本默认重启正是为了兜住这一点 —— 所以别习惯性加 `-NoRestart`。

---

## 五、验收标准（一句话）

| 门 | 通过标准 |
|---|---|
| `pytest` | `1182 passed, 1 skipped`（0 failed） |
| `self_audit` | 全部审计项通过，OpenAPI **62** 条路径 |
| 浏览器 E2E | `结果：165/165 通过`，且 `无 JS 运行时错误` |
| 手动走查 | 上节 8 步都不报错、数字/图表都在、越权被拒 |
