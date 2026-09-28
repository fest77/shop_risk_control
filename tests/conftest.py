# -*- coding: utf-8 -*-
"""测试夹具。

- 异步测试用 **anyio**（已随 starlette 安装），不引入 pytest-asyncio。
- 使用**独立测试库**（`TEST_MONGO_DB_NAME`），绝不污染业务库。
- ASGITransport 不触发 lifespan，因此连通性校验、建索引、灌账号在此显式完成。
- 限流与登录锁定都是**进程级**状态，必须逐用例复位，否则用例之间会互相污染。

## 身份怎么给（模块 01 起）

阶段一用 `X-Operator`/`X-Role` 自称身份；现在身份必须由真实登录换取的 JWT 承载。
为了不让每个用例都先写一遍登录，这里用 `RoleClient` 把测试专用标记头
`X-Test-Role: strategist` 翻译成 `Authorization: Bearer <真令牌>`：

    WRITER = {"X-Test-Role": "strategist"}
    await client.get("/api/v1/lists", headers=WRITER)

这样做而不是"直接伪造 token"，是为了让测试走**与生产完全相同的鉴权链路**
（中间件解析、回源查库、停用/改密判定都会真实执行）。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import config, db
from app.api.auth_api import LOGIN_GUARD
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_CASE_ACTIONS,
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_DEVICES,
    COLL_ENTITY_EDGES,
    COLL_FEATURE_BASELINES,
    COLL_FEATURE_SNAPSHOTS,
    COLL_IP_POOL,
    COLL_LIST_ENTRIES,
    COLL_METRIC_BUCKETS,
    COLL_MODEL_CONFIGS,
    COLL_RISK_CASES,
    COLL_RULES,
    COLL_RULE_SCENES,
    COLL_SEQ_COUNTERS,
    COLL_SIM_CASES,
    COLL_SIM_RUNS,
    COLL_SYS_USERS,
    COLL_SYSTEM_CONFIG,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.core import (
    biz_sync_retry,
    case_events,
    case_maintenance_task,
    confirm_token,
    edge_writer,
)
from app.core.degraded import DEGRADED
from app.core.ratelimit import LIMITER
from app.engine import decision as decision_engine
from app.engine import list_filter
from app.main import app
from app.security.password import hash_password
from app.services import audit_service
from app.services import case_service as case_service_mod
from app.services import config_service
from app.services import disposal_service as disposal_service_mod

# 角色 -> (账号, 口令, 姓名)。与 scripts/seed.py 的演示账号保持一致。
TEST_ROLE_USER: dict[str, tuple[str, str, str]] = {
    "strategist": ("strategy01", "strategy123", "李策略"),
    "admin": ("admin01", "admin123", "张运维"),
    "reviewer": ("reviewer01", "reviewer123", "王审核"),
}

# 既有用例沿用的三个常量，语义由"角色自称"变为"以该角色身份请求"
WRITER = {"X-Test-Role": "strategist"}
ADMIN = {"X-Test-Role": "admin"}
READER = {"X-Test-Role": "reviewer"}

# 令牌跨用例缓存：JWT 无状态，8 小时内有效；每个用例重新登录会让 100+ 用例
# 各多花一次 bcrypt 校验（约 0.25s），累积成几十秒的无谓开销。
# 安全性不受影响：`prep_db` 每个用例都会把账号重置为初始状态，
# 因此缓存下来的令牌始终对应一个真实有效的账号。
_token_cache: dict[str, str] = {}


async def _token_for(client: AsyncClient, role: str) -> str:
    if role not in _token_cache:
        username, password, _ = TEST_ROLE_USER[role]
        r = await client.post(
            f"{config.API_PREFIX}/auth/login",
            json={"username": username, "password": password},
        )
        assert r.status_code == 200, f"夹具登录失败：{r.status_code} {r.text}"
        _token_cache[role] = r.json()["data"]["access_token"]
    return _token_cache[role]


class RoleClient(AsyncClient):
    """把 `X-Test-Role` 标记翻译成真实 Bearer 令牌的测试客户端。"""

    async def send(self, request, *args, **kwargs):  # type: ignore[override]
        role = request.headers.pop("X-Test-Role", None)
        if role:
            request.headers["Authorization"] = f"Bearer {await _token_for(self, role)}"
        return await super().send(request, *args, **kwargs)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def user_hashes() -> dict[str, str]:
    """账号口令只 bcrypt 一次（bcrypt 故意很慢，逐用例算会让测试慢一个量级）。"""
    return {
        username: hash_password(password)
        for username, password, _ in TEST_ROLE_USER.values()
    }


@pytest.fixture(autouse=True)
def relaxed_rate_limit():
    """放开限流额度（限流本身由 test_contract 专门验证）。"""
    old = LIMITER.max_requests
    LIMITER.max_requests = 1_000_000
    LIMITER.reset()
    yield
    LIMITER.max_requests = old
    LIMITER.reset()


async def _seed_users(user_hashes: dict[str, str]) -> None:
    col = db.get_db()[COLL_SYS_USERS]
    for role, (username, _password, real_name) in TEST_ROLE_USER.items():
        await col.replace_one(
            {"_id": username},
            {
                "_id": username,
                "username": username,
                "password_hash": user_hashes[username],
                "real_name": real_name,
                "role": role,
                "status": "active",
                # 不设 password_changed_at：表示"未改过密码"，令牌不会因此失效。
                # 改密相关用例会自行写入该字段。
                "password_changed_at": None,
                "last_login_at": None,
            },
            upsert=True,
        )


async def _probe_mongo() -> tuple[bool, str]:
    """用**一次性客户端**探测连通性，不碰全局单例。

    为什么必须这样：若先用全局客户端探测、发现不可达再 `pytest.skip()`，夹具的
    `yield` 就永远不会执行——全局客户端会留在**当前用例的事件循环**上，下一个
    用例再用它就抛 `Cannot use AsyncMongoClient in different event loop`。
    结果是"库不可达"这个真因被一条假的循环错误盖住（实测 282 个用例里只有第 1 个
    报对了原因，其余全在报循环错误）。探测与使用分离后，所有用例都能报出真实原因。
    """
    from pymongo import AsyncMongoClient

    client = AsyncMongoClient(config.MONGO_URL, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
        return True, ""
    except Exception as e:  # noqa: BLE001 - 探测失败的原因就是要原样报出来
        return False, f"{type(e).__name__}: {e}"
    finally:
        await client.close()


@pytest.fixture(autouse=True)
async def prep_db(user_hashes):
    """每个用例：探测连通 → 切到测试库 → 建索引 → 清数据 → 灌账号 → 复位进程内状态。"""
    connected, error = await _probe_mongo()
    if not connected:
        pytest.skip(f"MongoDB 不可达（{config.MONGO_URL}）：{error}")
    db.use_database(config.TEST_MONGO_DB_NAME)
    await db.ensure_indexes()
    # 逐用例复位业务集合，保证用例之间互不影响、且不跨用例堆积。
    # 04 的产物（E02 快照 / E21 统计基线）必须一并清：事件与快照编号来自逐用例复位的
    # seq_counters，若不清就会跨用例重号、撞 E02 唯一索引并触发 FEA-5002 重试告警。
    # 09 的画像与图（E10 users / E11 devices / E12 ip_pool / E13 user_addresses /
    # E14 entity_edges）同理必须一并清，理由见决策 D47 的同一逻辑：
    # ① 事件决策之后 09 会**异步补写**这些集合，不清就会跨用例残留
    #    （实测后果：上一个用例写的 E12 行让本用例的 `insert_one` 撞 `_id` 主键）；
    # ② 残留的关联边会让"该设备关联 N 个账号"这类计数跨用例累加，断言随机失败。
    # ⚠️ 注意 `COLL_USERS`（E10 用户画像）与 `COLL_SYS_USERS`（E19 登录账号）
    #    **不是同一张表**：前者是业务画像（本模块），后者是鉴权账号，
    #    清错会把登录链路一起清掉（夹具最后会重灌 E19，但语义上仍是两回事）。
    #
    # 模块 05 的产物（E03 `decisions` / E04 `decision_hits` / E05 `rules`）同样
    # 必须一并清，理由与 D47 完全一致：
    #   ① `decisions` 的编号来自**逐用例复位**的 `seq_counters`，不清就会跨用例
    #      重号、撞 E03 主键（表现为"上一个用例的决策把本用例的覆盖掉"）；
    #   ② `decision_hits` 是 `decisions` 的子表，不清会留下孤儿明细，
    #      让"某次决策命中了几条"的断言随机失败；
    #   ③ `rules` 是决策的**输入**：残留规则会让"没有配规则时应当 pass"
    #      与"命中 N 条"这类断言在两个用例之间互相污染。
    #   ④ `rule_scenes`(E06) 已在列表里，**不重复添加**。
    #
    # 模块 08 的产物（E08 `risk_cases` / E09 `case_actions`）同样必须一并清：
    #   ① 案件的编号来自**逐用例复位**的 `seq_counters`，不清就会跨用例重号、
    #      撞 E08 主键（`CASE...` 被上一个用例的案件占住）；
    #   ② 05 的决策落库**现在会顺带建案**（D5：review/reject 必须建案），
    #      残留的待审案件会让"这个用例应该有几条案件"的断言跨用例累加；
    #   ③ `case_actions` 是 E08 的子表，残留会留下孤儿流水，让
    #      "一次处置写了几条流水"的断言随机失败（与 `decision_hits` 同因）。
    #
    # 模块 10 的产物（E17 `sim_cases` / E18 `sim_runs`）同样必须一并清：
    #   ① `sim_runs` 的编号来自**逐用例复位**的 `seq_counters`，不清就会跨用例
    #      重号、撞 E18 主键（实测：`E11000 duplicate key ... sim_runs._id_`）；
    #   ② 更要紧的是**跨模块契约**：`test_event_failclosed` 有一条既有的
    #      BR-03-31 断言「模拟器 `stop` **不写任何集合**」，它逐个数
    #      `sim_cases` / `sim_runs` 是否为 0。模块 10 的用例若把用例留在库里，
    #      那条**模块 03 的**断言就会红——而红的原因完全不在 03 那边
    #      （实测踩到：不加这两行时整套跑出 16 条失败，其中 10 条是 03 的用例）。
    # 模块 13 的产物：`system_config`（运行参数单文档）与 `model_configs`
    # （E20 决策引擎配置）。两者都**必须逐用例清**：它们没有"序号"这种天然
    # 隔离，残留会让"改参数后 config_version +1"从一个非 1 的版本开始，
    # 断言随即变成看运气（实测这类残留只会让失败出现在**别的**模块的用例上）。
    # `sys_users`(E19) 已在列表里——模块 13 的账号用例写的就是它。
    for coll in (COLL_LIST_ENTRIES, COLL_AUDIT_LOGS, COLL_SYS_USERS,
                 COLL_SEQ_COUNTERS, COLL_RULE_SCENES, COLL_METRIC_BUCKETS,
                 COLL_FEATURE_SNAPSHOTS, COLL_FEATURE_BASELINES,
                 COLL_USERS, COLL_DEVICES, COLL_IP_POOL, COLL_USER_ADDRESSES,
                 COLL_ENTITY_EDGES,
                 COLL_DECISIONS, COLL_DECISION_HITS, COLL_RULES,
                 COLL_RISK_CASES, COLL_CASE_ACTIONS,
                 COLL_SIM_CASES, COLL_SIM_RUNS,
                 COLL_SYSTEM_CONFIG, COLL_MODEL_CONFIGS):
        await db.get_db()[coll].delete_many({})
    DEGRADED.clear()
    # 05 的名单缓存是**进程内 TTL 缓存**（默认 10s，AD-02）。用例之间相隔远小于
    # 10s，不清它就会出现"上一个用例刚拉黑的人，本用例查出来还在名单里"——
    # 而且这类污染只在特定用例顺序下暴露，极难定位。`DEGRADED.clear()` 也会
    # 顺带让它失效（时间戳约定），但这里显式清一次，不依赖间接机制。
    list_filter.LIST_CACHE.clear()
    decision_engine.reset_stats()
    LOGIN_GUARD.reset_all()
    # 模块 13 的运行参数会被写进**进程级**的消费者（04 的 FeatureWindow、
    # 05 的 LIST_CACHE.ttl_sec、03 的 TIMEOUT_SEC/LATE_WINDOW_MS、05 的
    # SLOW_THRESHOLD_MS）。不复位就会出现"上一个用例把决策超时改成 5000ms，
    # 下一个用例的超时降级断言莫名其妙不触发"——而失败会出现在与模块 13
    # **无关**的用例上（与 `_reset_feature_injections` 踩过的是同一类坑）。
    config_service.reset_runtime_state()
    # 08 的三处**进程内**状态同样必须逐用例复位，否则会跨用例串味：
    # ① 二次确认令牌的"已用集合"（不复位会让下个用例的令牌莫名被判已用过）；
    # ② 处置幂等缓存（同键同载荷会返回上一个用例的响应体）；
    # ③ 旁路重试队列与事件订阅者（队列残留会让"待重试 N 条"的断言飘）。
    confirm_token.reset_store()
    disposal_service_mod.reset_idempotency()
    biz_sync_retry.reset_queue()
    case_events.reset_subscribers()
    await _seed_users(user_hashes)
    yield
    # 关闭顺序：先让异步旁路收尾，再停审计消费者，最后断数据库。
    # 05 的决策落库是异步旁路（AD-01）：不等到它收尾就 `db.close()`，
    # 后台任务会在已关闭的客户端上抛错，并把"决策没写进库"的假象带到下一个用例。
    # 它必须排在 `stop_consumer()` **之前**：建案钩子（D5）会在这一步里产生
    # `case.create` 审计，审计消费者已停时那些记录会触发自动重启消费者
    # （在 `db.close()` 之后写库 → 日志里一片无关的报错）。
    await decision_engine.flush()
    # 09 的建边 worker 同理：事件决策之后它在后台写 E10~E14，
    # 不显式停掉会带着旧事件循环的库句柄跑进下一个用例（并在 db.close() 之后报错）
    await edge_writer.stop()
    # 08 的案件维护任务（超时回收/自动归档/补建案）：测试不跑 lifespan，
    # 但有用例会显式 `start_maintenance()`；不停掉就会带着旧循环的句柄继续跑
    await case_maintenance_task.stop_maintenance()
    # 审计单消费者是进程内任务：不显式停掉会留下"pending task"告警，
    # 且会把队列里的记录带到下一个用例的事件循环里（跨测试污染）
    await audit_service.stop_consumer()
    case_service_mod.reset_service()
    disposal_service_mod.reset_service()
    # 04 的**注入型依赖**在收尾复位（`_profile_reader` / `_linked_provider`）。
    #
    # ## 为什么必须有这一处
    #
    # 它们是 `FeatureService` 的**实例属性**：注入它们**不经过**
    # `configure_components(...)`，因此 `component_reset()` 看不到它们，
    # `prep_db` 原本也不管它们。一个用例装上"假画像"之后，那个假画像会
    # **一路泄漏给后面的所有用例**，而失败却出现在**别的模块**的用例上：
    #
    # ① `test_sim_isolation` 装 `ip_is_proxy=False` 的假画像 ⇒
    #    `test_event_failclosed` 的「默认组件下永不 pass」拿到 `pass`
    #    （那条红线本意是验"04 不可用时 fail-closed"，却差点被误诊成模块 03 回归）；
    # ② `test_sim_consistency` 装"代理 IP + 6 个关联账号"的假画像 ⇒
    #    白名单直通用例被判成 `review`。
    #
    # ## 为什么放在**收尾**（一次真实的教训）
    #
    # 放在 `prep_db` **开头**时，我一度看到整套 `77 failed`——但那些失败
    # **不是**这条复位造成的，而是"两个 pytest 进程并发跑同一个测试库"的假象
    # （`prep_db` 逐用例清库，两个进程互相清库会让失败跨十几个模块均匀散开，
    # 看起来就像共享夹具被改坏了）。教训：**同一个测试库只能有一个 pytest 进程**。
    #
    # 放在收尾同样能覆盖全部路径：pytest 保证已启动的 teardown 一定被执行，
    # 因此**下一个测试文件**的第一个用例必然看到干净的依赖。
    _reset_feature_injections()
    await db.close()


def _reset_feature_injections() -> None:
    """把 04 的注入型依赖恢复成"从当前数据库现取"（见 `prep_db` 的收尾说明）。

    直接改属性而不是调用 `configure()`：后者的签名是
    `configure(profile_reader=None)` 且语义为"**不改动**"，
    因此无法用它**清除**已注入的值——这是本函数必须存在的原因。
    """
    from app.services import feature_service as feature_service_mod

    service = feature_service_mod.get_feature_service()
    service._profile_reader = None      # noqa: SLF001 - 唯一清除点，见 prep_db
    service._linked_provider = None     # noqa: SLF001


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with RoleClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def tolerant_client():
    """不重抛应用异常的客户端（用于验证兜底 500 的响应体）。"""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with RoleClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def login(client):
    """按任意凭据登录，返回 `(status_code, body)`（供认证类用例直接使用）。"""

    async def _login(username: str, password: str):
        r = await client.post(
            f"{config.API_PREFIX}/auth/login",
            json={"username": username, "password": password},
        )
        return r.status_code, r.json()

    return _login


@pytest.fixture
async def bearer(client):
    """取某角色的真实 Bearer 头（供需要手工拼令牌的用例使用）。"""

    async def _bearer(role: str) -> dict:
        return {"Authorization": f"Bearer {await _token_for(client, role)}"}

    return _bearer
