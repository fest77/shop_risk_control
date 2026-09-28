# -*- coding: utf-8 -*-
"""指标编排：写入（§3.9）+ 查询（§3.2~§3.6 的缓存/合并/降级）+ 参数校验。

## 一、写入为什么"永不抛异常"（BR-11-11 / MET-5002）

指标是**决策链路的旁路**：它的失败既不产生 `pass` 也不产生 `review`。
若 `record_decision` 把异常抛给模块 05，"统计写失败"就会升级成"决策失败"——
一个纯展示问题把风控主流程拖垮，是本末倒置。因此这里：
- `build_payloads` 之后的一切失败都只记 WARN + 计数（`stats["write_fail"]`）；
- 写入前先推 SSE（纯内存、不会失败）：顺序反过来会让"库一抖，大盘实时流也停"，
  两类互不相干的故障被绑成一件；
- 重试策略**不做内存重试队列**：`errors.MET_NOTICE` 已把处置定为"由 rollup /
  手动补算修复"，多一个进程内队列只会在崩溃时静默丢失（Spec §4.2 的"重试队列"
  与错误码表的措辞不一致，此处以错误码表 + 任务约定为准，已在交付说明登记）。

## 二、幂等（BR-11-10）：权威保障 + 兜底

Spec 把 at-most-once 的唯一保障交给调用方（`decisions.event_id` 唯一索引冲突即
跳过调用）。本实现在此之上加了一层**进程内 `event_id` 去重**，理由是 V-11-11 的
验收形态就是"同一 `event_id` 重放 5 次只 +1"，而"调用方一定会跳过"这句话无法
由本模块验证。它是**有界的兜底**（2 万条 FIFO），重启即失效，跨进程不生效——
因此不改变"权威保障在唯一索引"这一事实。

## 三、查询的缓存 / 合并 / 降级（BR-11-19 / BR-11-20）

- **TTL 3s**：小于页面 5s 的自动刷新间隔（N-11-12），避免"每次刷新都打一次库"。
- **single-flight**：相同查询键的并发请求合并成一次库查询（多个客户端同时刷页
  只查一次）。等待者用 `asyncio.shield` 包住共享任务：某个客户端断开时不能把
  "大家共用的这一次查询"一起取消。
- **降级**：Mongo 不可用时返回**最近一次成功快照** + `stale=true` + `stale_at`；
  从未成功过才抛 `MET-5001`。**绝不用 0 值冒充真实数据**——0 拦截率、0 P95
  在页面上都"看起来正常"，那才是真正危险的错误（§5 的 fail-closed 数据侧义务）。
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Callable, Optional

from pymongo.errors import PyMongoError

from app.core.metric_stream_bus import get_bus
from app.engine.metric_agg import build_payloads
from app.engine.metric_bucket import (
    ALLOWED_GRANULARITY,
    DEFAULT_RANGE,
    RANGE_SPECS,
)
from app.errors import MET_CODES, AppError
from app.logging import get_logger
from app.repos.metric_repo import MetricRepo
from app.schemas.metric_schema import (
    DIMS,
    LEVELS,
    ROLLUP_DIMENSIONS,
    ROLLUP_GRANULARITIES,
    SCENES,
    WINDOWS,
)
from app.services.metric_query import MetricQuery
from app.utils.timeutil import iso, now_ms

log = get_logger("shop_risk_control.metric.service")

# BR-11-19：进程内缓存 TTL（小于页面 5s 自动刷新间隔）
CACHE_TTL_MS = 3_000
# BR-11-03：写入只允许基础粒度，**不允许 1d**（一天一个桶会让 24h 趋势只剩 1 个点）
WRITE_GRANULARITIES: tuple[str, ...] = ("1m", "1h")
DEFAULT_WRITE_GRANULARITY = "1m"
# BR-11-10 的兜底去重窗口
REPLAY_GUARD_SIZE = 20_000


def met_error(code: str, message: Optional[str] = None, data: Any = None) -> AppError:
    """按 `MET_CODES` 表构造 `AppError`（状态码与错误码只有一处真源）。

    刻意放在服务层而不是每个文件各写一遍：状态码与错误码由两处维护，迟早出现
    "表里写 400、代码里传 503"，而这类分歧在联调时表现为前端按参数错误处理、
    后端其实返回依赖错误。
    """
    status, default = MET_CODES.get(code, (500, "指标服务暂不可用"))
    return AppError(code, message or default, status or 500, data)


# ============================================================
# 运行参数：「指标桶粒度」（§2.7，由模块 13 写入）
# ============================================================
_WRITE_GRANULARITY = DEFAULT_WRITE_GRANULARITY


def write_granularity() -> str:
    """当前生效的**写入**基础粒度。"""
    return _WRITE_GRANULARITY


def set_write_granularity(granularity: str) -> str:
    """设置写入基础粒度（模块 13 保存「指标桶粒度」运行参数时调用），返回生效值。

    非法值（含 `1d`）**不抛异常**，只告警并保持原值：这是运行参数而非请求参数，
    它的校验失败不该让保存配置的页面拿到 500；同时"写入 1d 桶"本身违反 BR-11-03，
    必须被挡住（宁可降级为 1m，也不能让 24h 趋势图只剩一个点）。
    """
    global _WRITE_GRANULARITY
    value = str(granularity or "").strip()
    if value not in WRITE_GRANULARITIES:
        log.warning("[MET-4002] 指标桶粒度 %r 不能用于写入（仅 %s，1d 会毁掉分钟级趋势），保持 %s",
                    granularity, "/".join(WRITE_GRANULARITIES), _WRITE_GRANULARITY)
        return _WRITE_GRANULARITY
    _WRITE_GRANULARITY = value
    log.info("指标写入基础粒度已设为 %s", value)
    return value


# ============================================================
# 参数校验（§3.1 ~ §3.6 / §5 的 MET-4xxx）
# ============================================================
def require_range(value: Optional[str]) -> str:
    """`range` 校验；非法 -> `MET-4001`。"""
    candidate = str(value or DEFAULT_RANGE).strip()
    if candidate not in RANGE_SPECS:
        raise met_error("MET-4001", f"时间范围参数不合法，可选 {'/'.join(RANGE_SPECS)}",
                        {"allowed": list(RANGE_SPECS), "received": value})
    return candidate


def require_granularity(range_: str, value: Optional[str]) -> str:
    """`granularity` 与 `range` 的匹配校验（BR-11-11）；不匹配 -> `MET-4002`。"""
    allowed = ALLOWED_GRANULARITY[range_]
    if value is None or str(value).strip() == "":
        return RANGE_SPECS[range_][0]
    candidate = str(value).strip()
    if candidate not in allowed:
        raise met_error(
            "MET-4002",
            f"时间粒度与范围不匹配：range={range_} 仅支持 {'/'.join(sorted(allowed))}",
            {"allowed": sorted(allowed), "received": candidate},
        )
    return candidate


def require_scene(value: Optional[str]) -> Optional[str]:
    """`scene` 校验；非法 -> `MET-4004`（Spec 未给该参数单列错误码，见交付说明）。"""
    if value is None or str(value).strip() == "":
        return None
    candidate = str(value).strip()
    if candidate not in SCENES:
        # 非法枚举若被静默忽略，页面会显示"全部场景"的数据却顶着"场景=xx"的筛选条，
        # 这种"看起来生效了"的错误比报错更难发现
        raise met_error("MET-4004", f"场景参数不合法，可选 {'/'.join(SCENES)}",
                        {"allowed": list(SCENES), "received": value})
    return candidate


def require_level(value: Optional[str]) -> Optional[str]:
    """`level` 校验；非法 -> `MET-4004`。"""
    if value is None or str(value).strip() == "":
        return None
    candidate = str(value).strip()
    if candidate not in LEVELS:
        raise met_error("MET-4004", f"风险等级参数不合法，可选 {'/'.join(LEVELS)}",
                        {"allowed": list(LEVELS), "received": value})
    return candidate


def require_dim(value: Optional[str]) -> str:
    """`dim` 校验；非法 -> `MET-4004`。"""
    candidate = str(value or "level").strip()
    if candidate not in DIMS:
        raise met_error("MET-4004", f"分布维度不合法，可选 {'/'.join(DIMS)}",
                        {"allowed": list(DIMS), "received": value})
    return candidate


def require_top(value: Any) -> int:
    """`top` 校验（1~50，默认 10）；越界 -> `MET-4003`。

    Spec §5 的提示列写"已按 10 返回"，但 HTTP 状态列写 400——**以状态列为准**：
    静默夹取参数会让调用方以为自己的 `top=100` 生效了（`errors.MET_CODES` 已把
    这条矛盾固化在码表注释里）。
    """
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        raise met_error("MET-4003", f"Top 取值需在 1~50，收到 {value!r}") from None
    if not 1 <= candidate <= 50:
        raise met_error("MET-4003", f"Top 取值需在 1~50，收到 {candidate}")
    return candidate


def require_window(value: Optional[str]) -> str:
    """吞吐窗口校验；非法 -> `MET-4001`（它本质是时间范围参数，见交付说明）。"""
    candidate = str(value or "5m").strip()
    if candidate not in WINDOWS:
        raise met_error("MET-4001", f"时间窗口参数不合法，可选 {'/'.join(WINDOWS)}",
                        {"allowed": list(WINDOWS), "received": value})
    return candidate


def require_rollup_window(from_ts: Any, to_ts: Any) -> tuple[int, int]:
    """rollup 时间区间校验；`from_ts > to_ts` -> `MET-4005`。"""
    try:
        start, end = int(from_ts), int(to_ts)
    except (TypeError, ValueError):
        raise met_error("MET-4005", "rollup 时间区间非法：from_ts / to_ts 必须是毫秒时间戳") from None
    if start > end:
        raise met_error("MET-4005",
                        f"rollup 时间区间非法：from_ts({start}) 不能大于 to_ts({end})")
    return start, end


def require_rollup_granularity(value: Any) -> str:
    """rollup 目标粒度校验（仅 `1h`/`1d`）；非法 -> `MET-4002`。"""
    candidate = str(value or "").strip()
    if candidate not in ROLLUP_GRANULARITIES:
        raise met_error("MET-4002",
                        f"rollup 目标粒度不合法，可选 {'/'.join(ROLLUP_GRANULARITIES)}",
                        {"allowed": list(ROLLUP_GRANULARITIES), "received": value})
    return candidate


def require_dimension(value: Any) -> str:
    """rollup 的 `dimension` 校验；非法 -> `MET-4004`（与 `dim` 同义：维度取值非法）。"""
    candidate = str(value or "all").strip()
    if candidate not in ROLLUP_DIMENSIONS:
        raise met_error("MET-4004", f"补算维度不合法，可选 {'/'.join(ROLLUP_DIMENSIONS)}",
                        {"allowed": list(ROLLUP_DIMENSIONS), "received": value})
    return candidate


# ============================================================
# 幂等兜底：进程内重放去重（BR-11-10 的补充手段）
# ============================================================
class _ReplayGuard:
    """有界 FIFO 的 `event_id` 去重集合。

    只记住"最近 N 条"，因此内存有上界；被挤出去的 `event_id` 若再次到来会被
    当成新事件——这是**故意**的：无界集合会随运行时长稳定增长，而真正的重放
    都发生在秒级窗口内（重试、消息重复投递）。
    """

    def __init__(self, maxsize: int = REPLAY_GUARD_SIZE):
        self._maxsize = maxsize
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def claim(self, event_id: Any) -> bool:
        """首次见到返回 `True`（可以写入）；重复返回 `False`（跳过写入）。"""
        if not event_id:
            # 没有 event_id 就无法判定重复。此处放行而不是丢弃：丢掉一条真实事件
            # 会让统计永久偏少，而多记一次的影响面小得多
            return True
        key = str(event_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > self._maxsize:
            self._seen.discard(self._order.popleft())
        return True

    def clear(self) -> None:
        self._seen.clear()
        self._order.clear()


# ============================================================
# 服务
# ============================================================
class MetricService:
    """写入 / 查询编排。查询侧带进程内缓存、single-flight 与降级快照。"""

    def __init__(
        self,
        repo_factory: Optional[Callable[[], MetricRepo]] = None,
        *,
        cache_ttl_ms: int = CACHE_TTL_MS,
    ):
        # 仓储**每次操作重新取**（不缓存）：测试会切换数据库（`db.use_database`），
        # 缓存集合句柄会把指标写进业务库——这类污染极难发现（同 audit_service）
        self._repo_factory = repo_factory
        self.cache_ttl_ms = cache_ttl_ms
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cache: dict[str, tuple[int, dict]] = {}
        self._snapshots: dict[str, tuple[int, dict]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._replay = _ReplayGuard()
        self.stats: dict[str, int] = {
            "write_ok": 0, "write_fail": 0, "build_fail": 0, "replay_skipped": 0,
            "query_ok": 0, "cache_hit": 0, "coalesced": 0, "stale": 0,
        }

    def _ensure_loop(self) -> None:
        """把进程内状态绑定到**当前**事件循环；换循环则整体复位。

        与 `audit_service` 同因：`asyncio.Queue` / `Task` 绑定首次使用它们的循环，
        换循环再用会抛 "bound to a different event loop"。生产是单循环，这个分支
        只在测试里触发（逐用例新循环）——复位比报错正确：旧循环里的缓存、快照与
        重放集合都已失效，带着它们跨循环只会产生莫名其妙的失败。
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._cache.clear()
            self._snapshots.clear()
            self._inflight.clear()
            self._replay.clear()

    def _repo(self) -> MetricRepo:
        if self._repo_factory is not None:
            return self._repo_factory()
        from app import db as db_module

        return MetricRepo(db_module.get_db())

    # ---------------- 写入（§3.9 的内部契约） ----------------
    async def record_decision(
        self,
        event: dict,
        decision: dict,
        hits: list[dict],
        *,
        granularity: Optional[str] = None,
    ) -> None:
        """把一次决策翻译成指标桶增量并广播实时事件（**永不抛异常**）。

        调用约定见 §3.9：必须在 `decisions` 落库成功之后调用；重复事件由调用方
        跳过（本模块再用 `_ReplayGuard` 兜一层）。
        """
        self._ensure_loop()
        event = event or {}
        decision = decision or {}
        hit_list = list(hits or [])
        event_id = event.get("event_id")

        if not self._replay.claim(event_id):
            self.stats["replay_skipped"] += 1
            log.warning("重复事件跳过指标写入（BR-11-10 at-most-once）：event_id=%s", event_id)
            return

        payloads: list[dict] = []
        try:
            payloads = build_payloads(
                event=event, decision=decision, hits=hit_list,
                granularity=self._write_granularity_for(granularity), now_ms=now_ms(),
            )
        except Exception as e:  # noqa: BLE001 - 载荷构造失败同样不能影响决策
            # `build_payloads` 假定上游给的是 E01/E03 的合法字段；`ts` 传成字符串这类
            # 脏数据会在这里抛 ValueError。它的代价只应是"这条统计没记上"，
            # 因此与写库失败同等处置：记 WARN + 计数，然后照常推实时流。
            self.stats["build_fail"] += 1
            log.warning("[MET-5002] 指标载荷构造失败（不影响决策与实时流）event_id=%s：%s",
                        event_id, e)

        await self._publish(event, decision, hit_list)
        if not payloads:
            return

        try:
            await self._repo().upsert_many(payloads)
            self.stats["write_ok"] += 1
        except Exception as e:  # noqa: BLE001 - 指标写入失败绝不阻断决策（MET-5002）
            self.stats["write_fail"] += 1
            log.warning("[MET-5002] 指标桶写入失败（不影响决策，由 rollup/手动补算修复）"
                        " event_id=%s buckets=%d：%s", event_id, len(payloads), e)

    def _write_granularity_for(self, granularity: Optional[str]) -> str:
        """确定本次写入的基础粒度（BR-11-03）。"""
        if granularity is None or str(granularity).strip() == "":
            return write_granularity()
        value = str(granularity).strip()
        if value not in WRITE_GRANULARITIES:
            log.warning("[MET-4002] 写入粒度 %r 不可用（不允许 1d），回落 %s",
                        value, write_granularity())
            return write_granularity()
        return value

    async def _publish(self, event: dict, decision: dict, hits: list[dict]) -> None:
        """推送实时事件帧的数据部分（§2.5）。

        **只推 `user_id`**，不推手机号 / IP / 地址：脱敏责任在 00/09，实时流是
        唯一会"越过页面渲染直接进浏览器控制台"的出口，多推一个字段就是多一处泄露面。
        """
        payload = {
            "event_id": event.get("event_id"),
            "ts": event.get("ts") or now_ms(),
            "event_type": event.get("event_type"),
            "user_id": event.get("user_id"),
            "final_score": decision.get("final_score"),
            "risk_level": decision.get("risk_level"),
            "decision": decision.get("decision"),
            "list_hit": decision.get("list_hit"),
            "hit_rule_count": len(hits),
            "biz_no": event.get("biz_no"),
            "amount": event.get("amount"),
            "case_no": decision.get("case_no"),
        }
        try:
            await get_bus().publish(payload)
        except Exception as e:  # noqa: BLE001 - 广播失败与指标写入失败同理，不影响决策
            log.warning("SSE 广播失败（不影响决策与指标写入）：%s", e)

    # ---------------- 查询（§3.2 ~ §3.6） ----------------
    async def overview(
        self, *, range_: Optional[str] = None, scene: Optional[str] = None,
        level: Optional[str] = None, granularity: Optional[str] = None,
    ) -> dict:
        """4 张卡片。"""
        rng = require_range(range_)
        gran = require_granularity(rng, granularity)
        sc, lv = require_scene(scene), require_level(level)
        key = _cache_key("overview", rng, gran, sc, lv)
        return await self._cached(
            key, lambda: self._query().overview(range_=rng, scene=sc, level=lv, granularity=gran)
        )

    async def trend(
        self, *, range_: Optional[str] = None, scene: Optional[str] = None,
        level: Optional[str] = None, granularity: Optional[str] = None,
    ) -> dict:
        """趋势（含补零与末点 partial）。"""
        rng = require_range(range_)
        gran = require_granularity(rng, granularity)
        sc, lv = require_scene(scene), require_level(level)
        key = _cache_key("trend", rng, gran, sc, lv)
        return await self._cached(
            key, lambda: self._query().trend(range_=rng, scene=sc, level=lv, granularity=gran)
        )

    async def distribution(
        self, *, range_: Optional[str] = None, scene: Optional[str] = None,
        level: Optional[str] = None, granularity: Optional[str] = None,
        dim: Optional[str] = None,
    ) -> dict:
        """等级 / 场景分布。"""
        rng = require_range(range_)
        gran = require_granularity(rng, granularity)
        sc, lv = require_scene(scene), require_level(level)
        dimension = require_dim(dim)
        key = _cache_key("distribution", rng, gran, sc, lv, dimension)
        return await self._cached(
            key,
            lambda: self._query().distribution(
                range_=rng, scene=sc, level=lv, granularity=gran, dim=dimension
            ),
        )

    async def rule_ranking(
        self, *, range_: Optional[str] = None, scene: Optional[str] = None,
        level: Optional[str] = None, granularity: Optional[str] = None,
        top: Any = 10,
    ) -> dict:
        """规则命中排行。

        `scene` 参数**对排行不生效**：`rule` 维度桶不带场景字段（§3.10 的桶结构里
        `rule` 桶只有命中计数），因此无法按场景切分。这里显式接受并忽略它——
        02 可能对四个查询接口统一带筛选参数，若因此报错反而会让大盘整块不可用；
        代价是"场景筛选下排行仍是全局排行"，已登记在交付说明。
        """
        rng = require_range(range_)
        gran = require_granularity(rng, granularity)
        require_scene(scene)
        lv = require_level(level)
        limit = require_top(top)
        key = _cache_key("rule_ranking", rng, gran, None, lv, limit)
        return await self._cached(
            key,
            lambda: self._query().rule_ranking(
                range_=rng, level=lv, granularity=gran, top=limit
            ),
        )

    async def throughput(self, *, window: Optional[str] = None) -> dict:
        """吞吐与延迟（不套 range，§3.6）。"""
        win = require_window(window)
        key = _cache_key("throughput", win)
        return await self._cached(key, lambda: self._query().throughput(window=win))

    async def rollup(
        self, *, granularity: Any, from_ts: Any, to_ts: Any, dimension: Any = "all",
    ) -> dict:
        """手动补算（§3.8）。

        函数内 import：`metric_rollup` 需要复用本文件的校验器与 `met_error`，
        模块级互相 import 会成环。委托给服务而不是让接口直连 rollup，是为了让
        API 层只依赖"服务 + 总线"两个入口。
        """
        from app.core import metric_rollup

        return await metric_rollup.rollup(
            granularity=granularity, from_ts=from_ts, to_ts=to_ts, dimension=dimension,
        )

    # ---------------- 缓存 / 合并 / 降级 ----------------
    def _query(self) -> MetricQuery:
        return MetricQuery(self._repo(), write_granularity=write_granularity())

    def clear_cache(self) -> None:
        """清空查询缓存（手动补算后调用：运维刚算完就该看到新数）。"""
        self._cache.clear()

    async def _cached(self, key: str, loader: Callable[[], Any]) -> dict:
        """TTL 缓存 + single-flight + 降级快照的统一入口。"""
        self._ensure_loop()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now_ms() and self.cache_ttl_ms > 0:
            self.stats["cache_hit"] += 1
            return dict(hit[1])
        try:
            data = await self._single_flight(key, loader)
        except (AppError, PyMongoError) as e:
            return self._degrade(key, e)

        self.stats["query_ok"] += 1
        fresh = self._stamp(data, stale=False, stale_at=None)
        self._cache[key] = (now_ms() + self.cache_ttl_ms, fresh)
        self._snapshots[key] = (now_ms(), fresh)
        return dict(fresh)

    async def _single_flight(self, key: str, loader: Callable[[], Any]) -> dict:
        """相同查询键合并成一次查询。"""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(loader())
            self._inflight[key] = task
            # 回调里必须"取走"异常：若某个任务失败且没有等待者，Python 会打
            # "Task exception was never retrieved"，把一次正常的降级变成一条吓人的日志
            task.add_done_callback(lambda t, k=key: self._release(k, t))
        else:
            self.stats["coalesced"] += 1
        # shield：某个客户端断开/超时，不能把"大家共用的这一次查询"取消掉
        return await asyncio.shield(task)

    def _release(self, key: str, task: asyncio.Task) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    def _degrade(self, key: str, error: Exception) -> dict:
        """Mongo 不可用时的处置（BR-11-20）。"""
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            log.warning("[MET-5001] 指标查询失败且无可用快照 key=%s：%s", key, error)
            raise met_error("MET-5001", data={"cache_key": key, "detail": str(error)}) from error
        at, data = snapshot
        self.stats["stale"] += 1
        log.warning("[MET-5001] 指标查询失败，返回 %s 的成功快照（stale=true）key=%s：%s",
                    iso(at), key, error)
        return self._stamp(data, stale=True, stale_at=at)

    @staticmethod
    def _stamp(data: dict, *, stale: bool, stale_at: Optional[int]) -> dict:
        """给查询结果盖上降级标记。

        `generated_at` 恒为**本次响应**生成的时刻（降级时晚于 `stale_at`）：
        两个字段若相等，前端就无法区分"数据是旧的"与"请求是旧的"。
        """
        out = dict(data)
        out["stale"] = stale
        out["stale_at"] = stale_at
        if "generated_at" in out:
            out["generated_at"] = now_ms()
        return out


def _cache_key(kind: str, *parts: Any) -> str:
    """查询键 = 接口名 + 全部筛选参数。

    **刻意不含 `now`**：含时间戳的键在 3s 内几乎永不重复，single-flight 会失效
    （多个客户端在同一秒请求的其实是"同一张图"）。数据新鲜度由 3s TTL 界定，
    N-11-12 已登记这个取舍。
    """
    return "|".join([kind, *(("" if p is None else str(p)) for p in parts)])


# ============================================================
# 进程内单例 + 模块级入口（§3.9 的 `record_decision(...)`）
# ============================================================
_SERVICE = MetricService()


def get_metric_service() -> MetricService:
    """取进程内服务单例（缓存/快照是跨请求共享的，必须同一实例）。"""
    return _SERVICE


async def record_decision(
    event: dict, decision: dict, hits: list[dict], *, granularity: Optional[str] = None,
) -> None:
    """供模块 05 / 08 的异步落库任务调用（§3.9）。"""
    await _SERVICE.record_decision(event, decision, hits, granularity=granularity)


__all__ = [
    "CACHE_TTL_MS", "DEFAULT_WRITE_GRANULARITY", "MetricService", "WRITE_GRANULARITIES",
    "get_metric_service", "met_error", "record_decision", "require_dim", "require_dimension",
    "require_granularity", "require_level", "require_range", "require_rollup_granularity",
    "require_rollup_window", "require_scene", "require_top", "require_window",
    "set_write_granularity", "write_granularity",
]
