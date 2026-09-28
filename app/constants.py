# -*- coding: utf-8 -*-
"""集合名、索引声明、分页与超时阈值的**唯一声明处**（BR-00-07）。

**为什么必须集中**：BR-00-07 要求业务代码不得散落硬编码集合名。Mongo 对
未知集合是"写入即创建"，所以拼错集合名**不会报错**，只会静默把数据写进
另一个集合——这类缺陷在演示当天极难定位。因此集合名只在这里出现一次。

**索引声明为什么也在这里**：索引是"数据契约"的一部分（唯一约束决定了
BR-06-20 的重复判定），把它和集合名放在一起才能保证两者同步演进。
`scripts/seed.py` 读取 `INDEX_SPECS` 统一建索引，业务代码不得自建。
"""
from __future__ import annotations

# ============================================================
# 集合名（对齐 01_数据实体 E01~E21，命名与实体文档逐字一致）
# ============================================================
COLL_RISK_EVENTS = "risk_events"            # E01 风控事件
COLL_FEATURE_SNAPSHOTS = "feature_snapshots"  # E02 特征快照
COLL_DECISIONS = "decisions"                # E03 决策记录
COLL_DECISION_HITS = "decision_hits"        # E04 决策命中明细
COLL_RULES = "rules"                        # E05 风控规则
COLL_RULE_SCENES = "rule_scenes"            # E06 规则场景
COLL_LIST_ENTRIES = "list_entries"          # E07 名单条目
COLL_RISK_CASES = "risk_cases"              # E08 风控案件
COLL_CASE_ACTIONS = "case_actions"          # E09 案件处置流水
COLL_USERS = "users"                        # E10 用户画像
COLL_DEVICES = "devices"                    # E11 设备画像
COLL_IP_POOL = "ip_pool"                    # E12 IP 画像
COLL_USER_ADDRESSES = "user_addresses"      # E13 收货地址画像
COLL_ENTITY_EDGES = "entity_edges"          # E14 实体关联边
COLL_METRIC_BUCKETS = "metric_buckets"      # E15 监控指标桶
COLL_AUDIT_LOGS = "audit_logs"              # E16 审计日志
COLL_SIM_CASES = "sim_cases"                # E17 仿真用例模板
COLL_SIM_RUNS = "sim_runs"                  # E18 仿真执行记录
COLL_SYS_USERS = "sys_users"                # E19 系统用户
COLL_MODEL_CONFIGS = "model_configs"        # E20 模型配置（预留，G-01）
COLL_FEATURE_BASELINES = "feature_baselines"  # E21 特征基线（决策 D9）

# 模块 13 的运行参数集合（单文档）。**不是业务实体**，因此不占用 E 编号：
# 它是"可由 UI 修改的运行参数"的落点（任务书 §2③）。之所以必须另起一个集合
# 而不是写回 `app/config.py`：那套配置是**启动期 fail-fast** 的（D25/D20），
# 把 UI 值灌回去就等于"保存一个参数 → 下次启动失败"。
COLL_SYSTEM_CONFIG = "system_config"

# 模块 00 自有的基础设施集合（非业务实体，故不占用 E 编号）。
# 存在理由：`EVT/DEC/CASE` 等业务编号是「每日 12 位序列」，必须跨进程重启
# 依然唯一，因此序列号需要持久化落库——用原子 `$inc` 保证并发下不重号。
COLL_SEQ_COUNTERS = "seq_counters"

COLLECTION_NAMES: tuple[str, ...] = (
    COLL_RISK_EVENTS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_RULES,
    COLL_RULE_SCENES,
    COLL_LIST_ENTRIES,
    COLL_RISK_CASES,
    COLL_CASE_ACTIONS,
    COLL_USERS,
    COLL_DEVICES,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_ENTITY_EDGES,
    COLL_METRIC_BUCKETS,
    COLL_AUDIT_LOGS,
    COLL_SIM_CASES,
    COLL_SIM_RUNS,
    COLL_SYS_USERS,
    COLL_MODEL_CONFIGS,
    COLL_FEATURE_BASELINES,
    COLL_SEQ_COUNTERS,
    COLL_SYSTEM_CONFIG,
)

# ============================================================
# 索引声明
# ------------------------------------------------------------
# 结构：{集合名: [ {name, keys, unique?, partialFilterExpression?}, ... ]}
# `keys` 用 1 / -1 直接表达升/降序（1 == ASCENDING），这样本文件**不依赖
# pymongo**，可以在任何上下文（含纯单测）安全导入。
#
# 各模块在实现时把自己的索引**追加到这里**，不在自己的仓储里建索引：
# BR-00-07 要求索引只有一处真源，否则 `--reset` 重建后会出现"代码建的索引
# 与种子脚本建的索引不一致"。
# ============================================================
INDEX_SPECS: dict[str, list[dict]] = {
    COLL_LIST_ENTRIES: [
        # 唯一约束的落地点（E07 + BR-06-20）：
        # list_type + entity_type + entity_value + status=active 组合唯一。
        # 用**部分唯一索引**表达，而不是应用层"先查再插"——后者在并发下必然漏判。
        {
            "name": "uq_active_entry",
            "keys": [("list_type", 1), ("entity_type", 1), ("entity_value", 1)],
            "unique": True,
            "partialFilterExpression": {"status": "active"},
        },
        # 过期清理任务与「即将过期」筛选使用
        {
            "name": "ix_status_expire",
            "keys": [("status", 1), ("expire_at", 1)],
        },
    ],
    COLL_ENTITY_EDGES: [
        # 团伙发现需要多跳遍历，故对两个方向都建索引（E14 §索引）
        {"name": "ix_from", "keys": [("from_type", 1), ("from_id", 1)]},
        {"name": "ix_to", "keys": [("to_type", 1), ("to_id", 1)]},
        {
            "name": "uq_edge",
            "keys": [("from_id", 1), ("to_id", 1), ("relation", 1)],
            "unique": True,
        },
    ],
    # 模块 09（画像与关联图谱）：E10 `users` / E11 `devices` / E12 `ip_pool` /
    # E13 `user_addresses` 的画像读取与增量维护**全部走 `_id`**（`_id` 在 MongoDB
    # 中天然唯一且自带索引），因此这三张表**刻意不声明额外索引**——另建同义索引
    # 只增加写入开销与"两处定义可能不一致"的风险（与 `COLL_SEQ_COUNTERS` 同理）。
    COLL_USER_ADDRESSES: [
        # 唯一的例外：§3.1 的画像卡要按 `user_id` 反查该用户的收货地址（E13 的主键
        # 是 `address_id`，从用户这一侧查不到 `_id`）。没有这条索引，每次打开画像卡
        # 都会对 `user_addresses` 做一次全集合扫描。
        {"name": "ix_user", "keys": [("user_id", 1)]},
    ],
    COLL_SEQ_COUNTERS: [
        # 不声明任何索引：`_id` 在 MongoDB 中天然唯一且自带索引，
        # 序列号的并发安全正是靠这一次原子 `$inc` 落在这条 `_id` 上实现的。
    ],
    COLL_AUDIT_LOGS: [
        # 索引依赖取自 Step1 E16 + 模块 12 §6：
        # ① 列表按时间倒序翻页；② 按操作人筛选；③ 按目标对象反查处置留痕
        {"name": "ix_ts_desc", "keys": [("ts", -1)]},
        {"name": "ix_actor_ts", "keys": [("actor", 1), ("ts", -1)]},
        {"name": "ix_target", "keys": [("target_type", 1), ("target_id", 1)]},
        # 刻意**不建 TTL 索引**：BR-12-12 要求审计永久保留（与 risk_events 的 90 天 TTL 相反）
    ],
    COLL_METRIC_BUCKETS: [
        # ① 按维度取一段时间的桶（趋势/分布/排行都走它）
        {"name": "ix_dim_ts", "keys": [
            ("bucket_type", 1), ("bucket_key", 1), ("granularity", 1), ("bucket_ts", 1),
        ]},
        # ② 只按粒度+时间取（global 趋势、吞吐统计）
        {"name": "ix_gran_ts", "keys": [("granularity", 1), ("bucket_ts", 1)]},
        # ③ 保留策略（BR-11-21）：TTL 建在 `expire_at` 上而不是 `bucket_ts`。
        #    原因：MongoDB 的一个集合只有**一份** expireAfterSeconds，无法按粒度区分；
        #    若按 bucket_ts 设 7 天，`1h` 桶（更旧）会被一起清掉，与"1h 留 90 天"矛盾。
        #    因此每条文档写入时自带 `expire_at`（1m→+7d、1h→+90d、1d→不写该字段=永久）。
        {"name": "ttl_expire", "keys": [("expire_at", 1)], "expireAfterSeconds": 0},
    ],
    COLL_RISK_EVENTS: [
        # ① 查询用索引（模块 03 §6 / 模块 02 的实时事件流与 07 的研判都要按
        #    事件类型或用户回看一段时间的事件）
        {"name": "ix_type_ts", "keys": [("event_type", 1), ("ts", -1)]},
        {"name": "ix_user_ts", "keys": [("user_id", 1), ("ts", -1)]},
        # ② 保留策略：`ts` 的 90 天 TTL（模块 03 §6）。
        #    **不直接建在 `ts` 上**，理由与 `COLL_METRIC_BUCKETS` 的 `ttl_expire`
        #    完全相同：MongoDB 一个集合只有**一份** `expireAfterSeconds`，
        #    建在 `ts` 上就把它焊死成"固定 90 天"，将来想按事件类型区分保留期
        #    （例如风控事件留 90 天、行为日志留 7 天）必须先删索引重建，
        #    而重建期间过期数据会一直堆积。改用**每条文档自带 `expire_at`
        #    （= received_at + 90d）** 的方式，`expireAfterSeconds=0` 表示
        #    "以文档自带的时刻为准"，保留策略的取值就落在业务代码一处
        #    （`repos/event_repo.expire_at_for`），改策略不需要动索引。
        {"name": "ttl_expire", "keys": [("expire_at", 1)], "expireAfterSeconds": 0},
        # 刻意**不建 `event_id` 索引**：E01 的主键就是 `_id = event_id`，
        # 而 `_id` 在 MongoDB 中天然唯一且自带索引（BR-03-13 的幂等兜底正是
        # 靠它）。另建一条同义索引只增加写入开销与"两处定义可能不一致"的风险。
    ],
    # 模块 04（特征计算引擎）：E02 快照 + E21 统计基线
    COLL_FEATURE_SNAPSHOTS: [
        # ① 唯一 `event_id`（Step1 E02 的索引要求）：一个事件只有一份快照。
        #    用**唯一约束**而不是普通索引是刻意的——同一事件重算时应当插入失败
        #    而不是静默产生第二份（否则"这份快照是最初算的还是后来重算的"
        #    再也说不清，而快照是复核时的事实地基）。
        {"name": "uq_event", "keys": [("event_id", 1)], "unique": True},
        # ② `user_id + computed_at desc`（Step1 E02 的复合索引）：按用户回看
        #    历史快照，也是 E21 统计基线按用户分组的查询路径
        {"name": "ix_user_computed", "keys": [("user_id", 1), ("computed_at", -1)]},
        # ③ 单键 `computed_at`：E21 的每日刷新要按时间窗批量扫描快照
        #    （`computed_at >= now - window_days`），没有它就会退化成全集合扫描
        {"name": "ix_computed_at", "keys": [("computed_at", -1)]},
    ],
    COLL_FEATURE_BASELINES: [
        # 不声明任何索引：E21 的 `_id` 就是 `{feature_name}:{segment}`
        # （既是主键也是查询键），`_id` 在 MongoDB 中天然唯一且自带索引，
        # 另建索引只增加写入开销（与 COLL_SEQ_COUNTERS 同理）。
    ],
    # 模块 13（系统设置）：两个单文档/单行集合，**刻意不声明索引**
    COLL_SYSTEM_CONFIG: [
        # 运行参数只有一行（`_id="runtime"`），按 `_id` 读即走主键索引。
        # 另建索引只会让"改一个参数"多一次索引写。
    ],
    COLL_MODEL_CONFIGS: [
        # E20 预留（G-01）：当前只有一条默认记录（`_id="default"`），按 `_id` 读。
    ],
    # 模块 08（案件处置与业务联动）：E08 `risk_cases` + E09 `case_actions`
    COLL_RISK_CASES: [
        # ① **唯一 `event_id`**（Step1 E08 的索引要求，BR-08-02 幂等的真正落点）。
        #    用唯一约束而不是应用层"先查再建"是刻意的：建案由决策链路**异步**触发
        #    （AD-01），并发/重试下"先查后插"必然漏判，结果是同一个事件出现两个案件
        #    ——审核员会看到两条一模一样的待审案件，而它们指向同一次决策。
        #    有唯一索引后，第二路插入被数据库拦下，服务层据此**返回既有案件**。
        #
        #    ⚠️ **必须是部分索引**（`$type: "string"`）：Mongo 的唯一索引把
        #    `null` 当成一个**值**，因此一条不带 `event_id` 的文档（测试夹具、
        #    将来的外部数据源、人工补数）会让第二条约同样缺字段的文档撞键——
        #    而"两条都没有 event_id"根本不构成重复。真实案件一定带字符串
        #    `event_id`（由决策链路写入），因此部分索引的覆盖面没有缺口。
        #    这条不是理论风险：模块 11 的 `pending_case_cnt` 用例就会灌入
        #    两条**无 event_id** 的案件，全量唯一索引下它必然失败。
        {"name": "uq_event", "keys": [("event_id", 1)], "unique": True,
         "partialFilterExpression": {"event_id": {"$type": "string"}}},
        # ② 工作台列表主查询：`status + risk_level + created_at desc`（E08 §索引）。
        #    07 的列表默认按 `status=pending` 倒序取最新案件，没有它就会全集合扫描。
        {"name": "ix_status_level_created",
         "keys": [("status", 1), ("risk_level", 1), ("created_at", -1)]},
        # ③ 按用户回看其案件（07 的三栏联动 / 09 的画像"关联案件"）。
        {"name": "ix_user_created", "keys": [("user_id", 1), ("created_at", -1)]},
        # ④ 超时回收扫描（BR-08-10）：只扫 `status=reviewing` 且已过
        #    `claim_deadline_at` 的案件。每 60s 跑一次，**不能退化成全表扫描**。
        {"name": "ix_status_deadline", "keys": [("status", 1), ("claim_deadline_at", 1)]},
        # ⑤ 自动归档扫描（BR-08-38）：`disposed` 且 `disposed_at <= 阈值`。
        {"name": "ix_status_disposed_at", "keys": [("status", 1), ("disposed_at", 1)]},
    ],
    COLL_CASE_ACTIONS: [
        # 处置流水按案件查询、按处置时间升序（Spec §3.2 冻结给 07 的契约：
        # `case_action_repo.list_by_case()` 返回**升序**列表）。
        # E09 `_id` 是随机流水号，不承载时间语义，因此排序必须靠 `acted_at`。
        {"name": "ix_case_acted", "keys": [("case_no", 1), ("acted_at", 1)]},
        # 业务同步重试队列的恢复扫描（BR-08-31 ②「重启后按标记恢复」）：
        # 只捞 `biz_sync_result.status="failed"` 的流水。
        {"name": "ix_biz_sync_status", "keys": [("biz_sync_result.status", 1)]},
    ],
    # 模块 10（事件仿真测试）：E17 `sim_cases` + E18 `sim_runs`
    COLL_SIM_CASES: [
        # `SIM-4003`（同名用例）的唯一性**必须由索引兜底**：服务层的"先查再插"
        # 在并发下必然漏判（BR-06-20 的同一条教训），而用例是低频写、读到重复
        # 用例却是常态（左栏列表），所以这里的成本可以接受。
        #
        # ⚠️ **必须是部分索引**（`status=active`）：BR-10-20 规定删除是**软删**
        # （`status=archived`），文档留在库里供历史 `sim_runs` 追溯。若做全量
        # 唯一索引，"删掉旧用例再建一个同名的新用例"就会撞键——那是完全正常的
        # 操作，却被数据库拒掉，而用户看到的只是"已存在同名用例"这句莫名其妙的话。
        {
            "name": "uq_sim_case_name",
            "keys": [("name", 1)],
            "unique": True,
            "partialFilterExpression": {"status": "active"},
        },
        # 左栏模板列表的默认读法：只取 active，按创建时间倒序（新存的排前面）。
        {"name": "ix_sim_case_status_created", "keys": [("status", 1), ("created_at", -1)]},
    ],
    COLL_SIM_RUNS: [
        # ① 历史执行记录按时间倒序回看（`GET /sim/runs/{id}` 走 `_id`，本索引
        #    服务于列表/排障与"这个用例跑过几次"）。
        {"name": "ix_sim_run_at", "keys": [("run_at", -1)]},
        # ② 按用例回看它的历次执行（批量回放的复核路径）。
        {"name": "ix_sim_run_case_at", "keys": [("case_id", 1), ("run_at", -1)]},
        # 刻意**不建 TTL 索引**：`sim_runs` 是"规则上线依据"的证据（Spec §1 的下游），
        # 与审计同理——证据不自动消失，清理是运维决定而不是默认行为。
    ],
}

# ============================================================
# 分页契约（模块 00 §3.2：全项目所有列表接口强制）
# ============================================================
PAGE_DEFAULT = 1
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
# 模块 06 §"分页"裁定：page 超过总页数不算错（返回空 items + 正确 total），
# 但 page > 200 视为参数越界 CFG-4008——防止深翻页拖垮 Mongo。
PAGE_MAX = 200

# 模块 06 的业务默认值（名单有效期，BR-06-22：灰名单 30 天，黑白永久）
GRAY_DEFAULT_EXPIRE_DAYS = 30

# ============================================================
# 模块 08 的业务默认值（案件处置）
# ------------------------------------------------------------
# 说明：`case_claim_timeout_min` / 自动归档天数在 Spec 里归**模块 13 系统设置**
# 下发（BR-08-12 / BR-08-38），而 13 尚未落地。此处按 06/01 的既有做法
# （`GRAY_DEFAULT_EXPIRE_DAYS` / `LOGIN_MAX_FAILURES`）给出**带环境变量覆盖的
# 进程默认值**：13 落地后只需把下发值写进同一处，不必改调用方。
# ============================================================
#: 认领后未处置的超时回收阈值（悬空点 G-06，Spec 取值 30 分钟）。
#: **0 表示关闭超时回收**（BR-08-12），此时不写 `claim_deadline_at`。
#: 它同时决定 `claim_deadline_at = claimed_at + N×60_000`（BR-08-05）。
CASE_CLAIM_TIMEOUT_MIN_DEFAULT = 30
#: `disposed` 后自动归档的天数（BR-08-38；PRD 未定义，Spec 取 7 天）。
#: 同时也是「可手动归档」之外的兜底——只归档，不删除。
CASE_ARCHIVE_AFTER_DAYS = 7
#: 处置进行中的内部锁的**过期时间**：一次处置在几十毫秒内结束，超过该时长
#: 说明持锁的进程已经死了（崩溃/被 kill）。届时允许接管，
#: 否则案件会永远停在"另一个处置进行中"而无人能处置（见 `case_repo.lock_for_disposal`）。
CASE_DISPOSE_LOCK_STALE_MS = 60_000

# ============================================================
# 超时与窗口阈值（各模块共用，避免同一个阈值在两处写不同值）
# ============================================================
# 决策同步链路总预算（BR-03-23，取值来自系统设置页「决策链路超时＝200」）
DECISION_TIMEOUT_MS = 200
# 事件滑动窗口（悬空点 G-02）
FEATURE_WINDOW_SHORT_MIN = 60
FEATURE_WINDOW_LONG_MIN = 1440
# 请求耗时告警线：超过则打 WARNING，便于提前发现慢查询
SLOW_REQUEST_MS = 1000
# 简易内存限流（悬空点"限流策略"）：单进程按用户计数
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_REQUESTS = 600

# ============================================================
# 模块 13（系统设置与运行参数）
# ------------------------------------------------------------
# 运行参数的**默认值与取值域只在这里声明一次**（BR-00-07 的同一条原则）：
# 服务层用它做校验与「恢复默认」，接口层用它生成文档，前端从
# `GET /system/config` 读当前值——三处不允许各写一份数字。
#
# ## 为什么这些值**不**回灌 `app/config.py`
#
# `app/config.py` 是**启动期 fail-fast** 的配置（`validate()` 缺 MONGO_URL/JWT_SECRET
# 即拒绝启动，D25/D20）。运行参数是"管理员在页面上随时可改"的另一类东西，
# 它的落点是 `COLL_SYSTEM_CONFIG`（运行时读库），因此保存一个越界之外的合法值
# 永远不会让下一次启动失败（任务书 §2③）。
#
# ## 两个镜像常量为什么要在这里写死一个字面量
#
# `window_capacity` 与 `list_cache_ttl_sec` 的真源分别在模块 04
# （`engine.feature_window.QUEUE_CAPACITY`）与模块 05
# （`engine.list_filter.DEFAULT_CACHE_TTL_SEC`）。从 constants **反向 import**
# 引擎层会形成循环（引擎层本来就 import 本文件），因此这里写镜像值，并由
# `tests/test_system_api.py::test_runtime_defaults_mirror_module_owners`
# 断言"两处必须相等"——比悄悄漂移成两个数字要好。
# ============================================================
#: 运行参数单文档的 `_id`（单文档是刻意的：一次 `update_one` 即原子，
#: 不会出现"TTL 改了、超时没改"的半个配置，Spec §5 的原子性要求）
RUNTIME_CONFIG_ID = "runtime"

#: 参数名 -> 默认值（对齐 Spec 13 §2.1 的"默认"列）
RUNTIME_CONFIG_DEFAULTS: dict[str, int | str] = {
    "short_window_min": FEATURE_WINDOW_SHORT_MIN,      # 60（本文件，04 也读它）
    "long_window_min": FEATURE_WINDOW_LONG_MIN,        # 1440
    "window_capacity": 10_000,                         # = feature_window.QUEUE_CAPACITY
    "list_cache_ttl_sec": 10,                          # = list_filter.DEFAULT_CACHE_TTL_SEC
    "decision_timeout_ms": DECISION_TIMEOUT_MS,        # 200
    "metric_bucket_granularity": "1m",                 # 1m / 1h / 1d（1d 见 config_service 的说明）
}

#: 参数名 -> `(下限, 上限)`（BR-13-01 的取值约束）。窗口对大小关系另有一条
#: `SYS-4002`（短窗 < 长窗），它无法用单字段区间表达，故在服务层单独判定。
RUNTIME_CONFIG_RANGES: dict[str, tuple[int, int]] = {
    "short_window_min": (1, 1440),
    "long_window_min": (1, 10080),
    "window_capacity": (1000, 1_000_000),
    "list_cache_ttl_sec": (1, 600),
    "decision_timeout_ms": (50, 5000),
}

#: 指标桶粒度取值（Spec 13 §3.1 的枚举）。**1d 只在存储与展示上可接受**：
#: 模块 11 的 `set_write_granularity()` 明确拒绝 1d 作为写入基础粒度
#: （BR-11-03：一天一个桶会让 24h 趋势只剩一个点），因此
#: `config_service` 会把"选了 1d"如实回报成 `SYS-5004` 提示，而不是假装生效。
METRIC_BUCKET_GRANULARITIES: tuple[str, ...] = ("1m", "1h", "1d")

#: 账号管理（模块 13 §4.2）的口令与账号约束
USER_PASSWORD_MIN_LEN = 6
USER_PASSWORD_MAX_LEN = 64
#: 后端生成的初始口令长度（BR-13-19：≥12 位且足够随机）
GENERATED_PASSWORD_LEN = 16
#: 账号软删除后的状态取值（BR-13-17）。它**不在** E19 的枚举字典里
#: （E19.status 只有 active/disabled），是 Spec §8「新增」行的裁定，
#: 因此这里显式声明并配中文标签，避免各处手写字符串。
USER_STATUS_DELETED = "deleted"
#: E19.status 的中文标签（含软删除态；active/disabled 与 `enums.SysUserStatus` 一致）
USER_STATUS_LABELS: dict[str, str] = {
    "active": "启用",
    "disabled": "已停用",
    USER_STATUS_DELETED: "已删除",
}
#: 组件探测的单个组件超时（BR-13-28：任一组件探测超时不得拖垮整卡）
HEALTH_PROBE_TIMEOUT_SEC = 1.0

# ============================================================
# 模块 10（事件仿真测试）的业务阈值
# ------------------------------------------------------------
# 集中在这里而不是散在 `sim_service` 里：`SIM-4005` 的提示文案里写着"最多 200 条"，
# 若阈值与文案各写一处，改了阈值就会得到一个自相矛盾的提示。
# ============================================================
#: 批量回放的默认条数（BR-10-19；Spec §3.4 的请求示例就是 `repeat: 20`）
SIM_BATCH_DEFAULT_REPEAT = 20
#: 批量回放的条数上限（BR-10-19：防止误操作打爆服务）。超限**拒绝**而不是截断，
#: 理由见 `SimBatchLimitError`。
SIM_BATCH_MAX_REPEAT = 200
#: 单次仿真的墙钟预算（Spec §5 的 `SIM-5003`：仿真超时 >5s）。
#: 它**比决策链路自己的 200ms 预算（`DECISION_TIMEOUT_MS`）宽得多**，因为仿真
#: 是人工触发的诊断动作、还带一次额外的特征只读计算，但也不能无限等下去——
#: 没有它，一次 Mongo 卡死会让仿真页永远转圈。
SIM_TIMEOUT_MS = 5_000
#: 用例名长度（Spec §3.2：2~64 字符）
SIM_CASE_NAME_MIN, SIM_CASE_NAME_MAX = 2, 64
#: 用例描述长度（Spec §3.2：≤200 字）
SIM_CASE_DESC_MAX = 200
#: 批量回放的扰动幅度（BR-10-18 的"同 seed 可复现"）——见 `sim_service.ripple_event`。
#: 取 20%：足以让金额维度的规则产生分叉，又不至于把事件变成另一个业务场景。
SIM_BATCH_AMOUNT_RIPPLE = 0.2
