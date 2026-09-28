# -*- coding: utf-8 -*-
"""特征计算编排服务（模块 04 §3.1 / §3.4，实现 03 的 `FeatureProvider` 契约）。

## 链路（严格按 BR-04-02 的顺序）

    ingest（当前事件入窗口）
      → 并发取画像（E10~E13）与关联账号数（模块 09）
      → plan（取 [ts-长窗, ts] 的窗口视图）
      → compute（18 项纯计算）
      → 生成快照编号 → 组装快照
      → **异步落库**（BR-04-22/23：不阻塞、失败不影响决策）
      → 返回给 05

**为什么必须"先 ingest 再 compute"**：风控要拦的是"这一次"。若把本次排除在外，
"近 1 小时同设备下单次数"在首发订单上恒为 0，明显的聚集会被判成"从未发生"。
`V-04-03` 直接断言首发 `coupon_receive` 后 `coupon_cnt_1h == 1`。

## 落库失败为什么不回滚决策（BR-04-23）

决策链路的产物是"放行/转人工/拦截"，而快照是**复核用的证据**。若因为证据写不进库
就把已经做出的决策撤掉，等于让一次 Mongo 抖动变成一次业务中断；更糟的是，
撤到一半（05 已经写了命中明细）会出现"库里有命中、没快照"的分裂状态。
因此快照落库走**最终一致**：失败入有界重试队列 + `FEA-5002` 告警，
调用方的响应早就返回了。

## 计算异常为什么不是"整条链路失败"（FEA-5001）

单项计算失败（例如字段类型不符）时，本服务交出**可用子集**并把这些项并入
`missing_features`，同时 `error` 置位、`degrade_suggested=True`。这样：
05 不会拿到一份假的"全部为 0"的特征（那会让所有规则不命中、误判低风险放行），
而 03 看到 `degrade_suggested` 会在调 05 **之前**短路成 `review`（fail-closed）。

## `degrade_suggested` 到底什么时候为真

只有两种情况，且都**不是**"缺少画像数据"：

1. **计算出错**（`FEA-5001`）——有明确的失败原因；
2. **关键特征不可用**：`ip_is_proxy` 缺失（见 `DECISION_GATING_FEATURES`），
   或窗口被截断（`FEA-5003`：数据丢失导致特征只会偏低）。

**为什么"画像缺失"不触发降级**：冷启动用户必然缺 `user_age_days` /
`device_user_cnt` 等大量项（BR-04-10 明确说"冷启动缺失项允许较多"）。
若也让它们降级，则**每个新用户都被打成 review**，而"缺数据"与"算错"是两件事，
混在一起就等于把这个信号作废。缺失的处置归 05 的条件求值语义（BR-04-11），
本模块只如实标记（BR-04-09）。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional

from app import db as db_module
from app.core.degraded import DEGRADED
from app.engine.feature_baselines import baseline_meta
from app.engine.feature_compute import (
    FEATURE_DATA_TYPES,
    FEATURE_GROUPS,
    FEATURE_KEYS,
    FEATURE_LABELS,
    FEATURE_UNITS,
    FeatureResult,
    ProfileData,
    compute_features,
)
from app.engine.feature_window import FeatureWindow
from app.errors import FEA_CODES, FEA_NOTICE, AppError
from app.logging import get_logger
from app.repos.feature_repo import FeatureRepo, MongoProfileReader, ProfileReader
from app.utils.ids import new_snapshot_id
from app.utils.timeutil import date_key, now_ms

log = get_logger("shop_risk_control.feature")

#: 事件编号格式（与 E01 一致：`EVT{yyyyMMdd}{12位序列}`）。
#: 接口层用它区分 `FEA-4001`（格式非法）与 `FEA-4004`（查不到）——
#: 两者对调用方的含义不同：前者是报文错误（可改），后者是数据尚未落库（可重试）。
EVENT_ID_RE = re.compile(r"^EVT\d{20}$")

#: 快照编号格式（`SNP{yyyyMMdd}{12位序列}`）
SNAPSHOT_ID_RE = re.compile(r"^SNP\d{20}$")

#: 异步落库的重试次数（与 `event_service` 同因：高频路径不无限堆积后台任务）
PERSIST_RETRY = 2
PERSIST_RETRY_BASE_SEC = 0.05

#: 失败快照的重试队列上限。有界是必须的：Mongo 长时间不可用时，
#: 无界队列会把内存吃光——那时连决策链路本身都跑不动，比丢快照严重得多。
RETRY_QUEUE_MAX = 1000

#: 关键特征清单：缺失时必须建议降级（fail-closed）。
#:
#: 目前只有 `ip_is_proxy`。选它的理由：它的缺失语义**是风险项本身**——
#: "不知道这个 IP 是不是代理/机房出口"与"已知不是代理"在风控上必须分开处置
#: （代理 IP 是羊毛党/刷单的标准配置）。其余缺失项（年龄、等级、聚集度）
#: 属于"背景强弱"，缺失时由 05 的 BR-05-12 按 `false` 处理即可，不该让
#: 每一次冷启动都强制转人工——那会让审核队列被新用户淹没。
DECISION_GATING_FEATURES: tuple[str, ...] = ("ip_is_proxy",)


def snapshot_id_fallback(event_ts: int) -> str:
    """快照编号的兜底生成（序列服务不可用时使用）。

    为什么必须有兜底：快照落库是异步的，但**编号必须同步给出**——它要随决策
    一起回给调用方并写进 E03。若编号生成依赖 Mongo 而 Mongo 正忙，就会出现
    "决策里 `snapshot_id=null`"，而 E03 要求该字段必填。
    兜底值用**事件时间戳**而不是随机数：同一事件重算会得到同一个编号，
    于是 `uq_event` 唯一索引会如实报冲突，而不是悄悄多出一份快照。
    """
    return f"SNP{date_key(event_ts)}{event_ts % 10 ** 12:012d}"


def fea_error(code: str, message: Optional[str] = None, data: Any = None) -> AppError:
    """按 `FEA_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = FEA_CODES.get(code, (400, "特征服务请求失败"))
    return AppError(code, message or default, status or 400, data)


def require_event_id(event_id: Any) -> str:
    """校验事件编号格式；非法即 `FEA-4001`（§5，HTTP 422）。

    为什么在服务层判而不是用 FastAPI 的 `Path(pattern=...)`：框架拦下来的是
    通用 `COM-4001`，而契约要求 `FEA-4001`（调用方据此区分"我的编号写错了"
    与"服务端参数校验失败"）。
    """
    text = str(event_id or "").strip()
    if not EVENT_ID_RE.match(text):
        raise fea_error(
            "FEA-4001",
            f"事件编号不合法：{text or '(缺失)'}（需匹配 EVT+8位日期+12位序列）",
            {"event_id": text},
        )
    return text


def empty_snapshot(event: dict, reason: str) -> dict:
    """构造一份**明确标记为不可用**的快照（fatal 失败时的返回值）。

    它**不是**"特征都为 0 的快照"：`features` 为空、`missing_features` 是全部
    18 项、`degrade_suggested=True`。这样 03 会在调 05 之前短路成 `review`，
    而 05 永远看不到一份"看起来一切正常"的假特征。
    """
    return {
        "snapshot_id": None,
        "event_id": str(event.get("_id") or event.get("event_id") or ""),
        "user_id": str(event.get("user_id") or ""),
        "features": {},
        "missing_features": list(FEATURE_KEYS),
        "missing_reasons": {key: reason for key in FEATURE_KEYS},
        "window_config": {},
        "computed_at": now_ms(),
        "compute_ms": 0,
        "degrade_suggested": True,
        "status": "error",
        "error": reason,
    }


class FeatureService:
    """模块 04 的门面：窗口 + 画像 + 计算 + 快照（实现 03 的 `FeatureProvider`）。

    进程内单例（模块级 `get_feature_service()`），因为它持有滑动窗口状态。
    """

    def __init__(
        self,
        *,
        window: Optional[FeatureWindow] = None,
        profile_reader: Optional[ProfileReader] = None,
    ) -> None:
        self.window = window if window is not None else FeatureWindow()
        #: 画像读取器：`None` 表示"每次从当前数据库取"（测试会切换库）
        self._profile_reader = profile_reader
        #: 关联账号数提供者：`None` 表示"取全局组件里的那个"（09 未落地时不可用）
        self._linked_provider: Any = None
        self._persist_tasks: set[asyncio.Task] = set()
        self._retry_queue: list[dict] = []
        self.stats: dict[str, int] = {
            "computed": 0, "ingested": 0, "duplicate": 0, "errors": 0,
            "degraded": 0, "persisted": 0, "persist_failed": 0, "requeued": 0,
            # 模块 10 的隔离通道：`read_only` 是"按 `affect_window=False` 算过几次"，
            # `window_affected` 是"真正写过窗口几次"。两者分开计数的意义在于
            # **隔离方案本身可被观测**：仿真跑完后 `window_affected` 不涨，
            # 就是"没污染真实窗口"的直接证据（`GET /health` 与测试都读它）。
            "read_only": 0, "window_affected": 0, "skipped_persist": 0,
        }

    # ---------------- 依赖装配 ----------------
    def _repo(self) -> FeatureRepo:
        """每次取新句柄：测试会切换数据库，缓存集合句柄会写错库。"""
        return FeatureRepo(db_module.get_db())

    def _profiles(self) -> ProfileReader:
        if self._profile_reader is not None:
            return self._profile_reader
        return MongoProfileReader(db_module.get_db())

    def _linked(self) -> Any:
        """取关联账号数提供者（模块 09）。

        默认从 `app.protocols` 的全局组件取（默认实现返回 `None` = 不可用），
        便于 09 上线时只换装配、不改本模块代码。
        """
        if self._linked_provider is not None:
            return self._linked_provider
        from app.protocols import get_components

        return get_components().linked_user_count_provider

    def configure(self, *, window: Optional[FeatureWindow] = None,
                  profile_reader: Optional[ProfileReader] = None,
                  linked_provider: Any = None) -> None:
        """替换依赖（测试与 09 接入时使用）。"""
        if window is not None:
            self.window = window
        if profile_reader is not None:
            self._profile_reader = profile_reader
        if linked_provider is not None:
            self._linked_provider = linked_provider

    # ---------------- FeatureStore 协议面 ----------------
    async def record(self, entity_type: str, entity_id: str, event: dict) -> None:
        """`FeatureStore.record` 的兼容别名（AD-05）。

        `app/protocols.py` 的 `FeatureStore` 用的动词是 `record`，而本模块的
        窗口动词是 `ingest`（Spec §3.1 的 `FeatureStore` 协议写的就是 `ingest`）。
        两个名字必须都认：前者是既有契约（`isinstance(service, FeatureStore)`
        要成立），后者是 Spec 的用词。这里转发到 `ingest`，
        **不新增第二份计数逻辑**。

        `entity_type`/`entity_id` 参数被忽略：本模块的窗口是**事件驱动**的
        （一条事件同时进 user/device/ip/address 四个维度），不是"按指定实体
        单独记账"。若在这里按调用方给的实体硬塞一条事件，就会造出一个
        "谁都不知道它属于哪条事件"的条目——`event` 里已含全部维度信息，
        按它入窗口才是唯一正确的口径。
        """
        await self.ingest(event)

    async def ingest(self, event: dict) -> bool:
        """把事件写入窗口（BR-04-02 的第一步）。返回 `False` 表示重复事件。"""
        added = await self.window.ingest(event)
        if added:
            self.stats["ingested"] += 1
        else:
            self.stats["duplicate"] += 1
        return added

    async def window_stats(self, entity_type: str = "", entity_id: str = "",
                           minutes: int = 0) -> dict[str, Any]:
        """窗口聚合的只读面（`FeatureStore.window_stats` 的兼容实现）。

        本模块的窗口是**按维度队列**组织的，并不提供"任意实体的任意窗口聚合"
        这个通用接口——那会诱导调用方绕过 18 项契约自己造特征（BR-04-06 要求
        口径三处同步）。因此这里返回窗口整体统计 + 配置，并在 `note` 里
        写明正确的取数路径仍是 `compute()`。
        """
        stats = self.window.stats()
        return {
            **stats,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "minutes": int(minutes or 0),
            "note": "18 项特征请走 compute(event)；本方法只提供窗口整体统计",
        }

    async def baseline(self, feature_name: str, segment: str = "all") -> Optional[dict]:
        """取 E21 统计基线（`FeatureStore.baseline` 的兼容实现）。

        **查不到必须返回 `None`**：编码约束要求界面显示「—」，
        严禁用 0 或本次值冒充基线——那会让研判人员误判。
        """
        doc = await self._repo().list_baselines(segment)
        found = doc.get(feature_name)
        if not found:
            return None
        sample = int(found.get("sample_size") or 0)
        if sample < 100:      # E21：低于 100 视为不可用（与刷新侧同一判据）
            return None
        return found

    # ---------------- 主链路 ----------------
    async def compute(
        self,
        event: dict,
        *,
        affect_window: bool = True,
        persist: bool = True,
    ) -> dict:
        """算一条事件的特征快照（§3.1 / §3.4 的 `FeatureProvider` 契约）。

        返回值键（与 `app/protocols.py` 的 `FeatureProvider` 注释逐字对齐，并被
        `event_service._compute_and_decide` 直接读取）：
        `snapshot_id` / `features` / `missing_features` / `window_config` /
        `compute_ms` / `degrade_suggested`。

        **绝不抛异常**：任何失败都转成 `degrade_suggested=True` 的可用子集，
        由 03 按 fail-closed 转 review。抛异常虽然也会被 03 捕获降级，
        但那会丢掉"到底哪些特征算出来了"的信息，人工复核时无从下手。

        ## 两个可选开关：`affect_window` / `persist`（决策 D11 的落点）

        默认值 `True/True` 就是**真实链路的行为**，因此 `event_service` 与
        `decision._acquire_features` 这些既有调用点一个字都不用改。

        模块 10（事件仿真测试）需要的是另一条通道，`BR-10-06` 的原话是
        「仿真事件**绝不写入真实特征窗口**……仿真只**读取**当前窗口状态计算特征」：

        | 开关 | `False` 时的行为 | 理由 |
        |---|---|---|
        | `affect_window` | **不 `ingest`**：只按当前窗口状态算特征 | 否则连续仿真 20 次会把 `device_order_cnt_1h` 顶上去（V-10-05 要验的正是这条），而水位一旦抬起来就**撤不掉**——已经算出去的快照不会因为"事后回滚窗口"而变回去 |
        | `persist` | **不落 E02 快照** | 仿真不是一次接入，写 `feature_snapshots` 会让 04 的复核区出现一批"没有对应事件"的快照；且 `uq_event` 唯一索引会让同一事件重复仿真时撞键并刷 `FEA-5002` 告警 |

        ## 只读模式为什么"当前这一笔不计入"（如实声明）

        真实链路是"先 `ingest` 再 `compute`"，因此**当前这一笔是计入的**
        （`V-04-03` 直接断言首发 `coupon_receive` 后 `coupon_cnt_1h == 1`）。
        只读模式下窗口里没有这一笔，于是"含本次"的那几个窗口计数会**比真实链路
        少 1**（例如 `coupon_cnt_1h` 真实为 1、仿真为 0/缺失）。

        这是"不写真实窗口"的**必然代价**，不是可以顺手抹平的差异：
        要让它相等就必须把这一笔写进窗口，而那正是 BR-10-06 禁止的事。
        因此本模块选择**如实偏低**（对齐 BR-10-07：窗口为空时特征会偏低，
        页面须提示"当前无历史窗口数据，特征可能偏低"），并在 `sim_runs` 里
        记下 `window_premise` 说明这个前提——**不用 0 或估算值冒充真实计数**
        （BR-04-09 的同一条原则）。
        """
        started = time.perf_counter()
        try:
            return await self._compute(
                event, started, affect_window=affect_window, persist=persist
            )
        except Exception as e:  # noqa: BLE001 - 契约要求本方法永不抛（见 docstring）
            self.stats["errors"] += 1
            snapshot = empty_snapshot(
                event, f"{FEA_NOTICE['FEA-5001']}：{type(e).__name__}: {e}",
            )
            snapshot["compute_ms"] = int((time.perf_counter() - started) * 1000)
            log.exception("[FEA-5001] 特征计算失败 event_id=%s", event.get("_id"))
            return snapshot

    async def _compute(
        self, event: dict, started: float, *, affect_window: bool = True,
        persist: bool = True,
    ) -> dict:
        # 1. 先 ingest（BR-04-02）。只读模式（模块 10 仿真）**跳过这一步**——
        #    这是 BR-10-06 的唯一落点，见 `compute` 的说明。
        if affect_window:
            await self.ingest(event)
            self.stats["window_affected"] += 1
        else:
            self.stats["read_only"] += 1

        # 2. 取画像与关联账号数（并发；失败只影响对应特征）
        profile, linked = await self._gather_inputs(event)

        # 3. 取窗口视图（含当前这一笔）
        view = self.window.plan(event)

        # 4. 画像缺失时 `device_age_hours` 的回落依据（窗口内最早出现时间）
        window_first_seen: dict[str, int] = {}
        device_key = view.identities.get("device")
        if device_key:
            first = self.window.first_seen("device", device_key)
            if first is not None:
                window_first_seen["device"] = first

        # 5. 18 项纯计算（单项异常不影响其余项）
        result: FeatureResult = compute_features(
            event, view, profile,
            linked_user_counts=linked, window_first_seen=window_first_seen,
        )

        # 6. 快照编号（必须同步给出：它要随决策回给调用方并写进 E03）
        snapshot_id = await self._new_snapshot_id(int(view.event_ts))

        # 7. 组装
        truncated_dims = self.window.truncated_dimensions(event)
        missing = list(result.missing_features)
        degrade_reasons: list[str] = []
        if result.error_features:
            degrade_reasons.append(
                f"{FEA_NOTICE['FEA-5001']}（异常项：{'、'.join(result.error_features)}）"
            )
        gating_missing = [k for k in DECISION_GATING_FEATURES if k in missing]
        if gating_missing:
            degrade_reasons.append(
                f"关键特征不可用：{'、'.join(gating_missing)}——"
                "缺失语义属于风险项本身，按 fail-closed 建议转人工"
            )
        if truncated_dims:
            degrade_reasons.append(
                f"{FEA_NOTICE['FEA-5003']}（维度：{'、'.join(truncated_dims)}）"
            )

        computed_at = now_ms()
        compute_ms = int((time.perf_counter() - started) * 1000)
        snapshot: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "event_id": str(event.get("_id") or event.get("event_id") or ""),
            "user_id": str(event.get("user_id") or ""),
            "features": result.features,
            "missing_features": missing,
            # 缺失原因与窗口截断都不是 E02 的字段，但它们**必须**能被人工复核看到：
            # 只写一个特征名列表，复核者无法区分"没有这个用户"与"读库失败"。
            "missing_reasons": result.missing_reasons,
            "window_config": view.window_config,
            "computed_at": computed_at,
            "compute_ms": compute_ms,
            "degrade_suggested": bool(degrade_reasons),
            "truncated": bool(truncated_dims),
            "truncated_dimensions": truncated_dims,
            "status": "degraded" if degrade_reasons else "ok",
            # 本次快照是否把事件写进了真实窗口。真实链路恒为 True；
            # 模块 10 的仿真恒为 False（BR-10-06）。**如实落进快照**而不是
            # 只写在日志里：拿到一份特征的人有权知道"这份特征含不含当前这一笔"，
            # 否则"少 1"会被当成计算错误去排查。
            "affects_window": bool(affect_window),
        }
        if degrade_reasons:
            snapshot["degrade_reasons"] = degrade_reasons
            self.stats["degraded"] += 1

        # 8. 异步落库（BR-04-22/23）
        if persist:
            self._spawn_persist(snapshot)
        else:
            self.stats["skipped_persist"] += 1
        self.stats["computed"] += 1
        return snapshot

    async def _gather_inputs(self, event: dict) -> tuple[ProfileData, dict[str, Optional[int]]]:
        """并发取画像（E10~E13）与关联账号数（模块 09）。

        两者互相独立，串行会让 200ms 预算白白翻倍。任一侧失败都**不抛出**：
        画像失败 → 对应特征进缺失（BR-04-09）；09 失败 → 三项聚集度进缺失
        （BR-04-07 规定一律向 09 取，**不得自建去重集合**，因此这里没有
        "09 挂了就自己算"的退路——那会造出第二个去重真源）。
        """
        profiles_task = asyncio.create_task(self._load_profiles(event))
        linked_task = asyncio.create_task(self._load_linked(event))
        profile, linked = await asyncio.gather(
            profiles_task, linked_task, return_exceptions=True
        )

        if isinstance(profile, BaseException):
            self.stats["errors"] += 1
            log.warning("[FEA-5001] 画像读取异常（对应特征将标记缺失）：%s", profile)
            # 逐字段登记"读取失败"而不是只给一个笼统标记：`ProfileData.missing_reason`
            # 用**字段名**去 `errors` 里查，只有按字段登记，快照的
            # `missing_reasons` 才能区分"库里没有这条画像"（冷启动）与
            # "读库失败"（依赖故障）——两者对研判的含义完全不同。
            detail = f"{type(profile).__name__}: {profile}"
            profile = ProfileData(errors=(
                f"user_register_at={detail}",
                f"level={detail}",
                f"risk_tags={detail}",
                f"device_first_seen_at={detail}",
                f"ip_is_proxy={detail}",
                f"address_aftersale_cnt={detail}",
            ))
        if isinstance(linked, BaseException):
            self.stats["errors"] += 1
            log.warning("[FEA-5001] 关联账号数读取异常（三项聚集度将标记缺失）：%s", linked)
            linked = {}
        return profile, linked

    async def _load_profiles(self, event: dict) -> ProfileData:
        return await self._profiles().load(event)

    async def _load_linked(self, event: dict) -> dict[str, Optional[int]]:
        """向模块 09 取三项关联账号数（BR-04-07）。

        逐维度取而不是一次批量：Protocol 只定义了单个维度的
        `get_linked_user_count`（见 `app/protocols.py`），本模块无权替 09
        设计批量接口。三次调用彼此独立，因此并发发出。
        """
        from app.engine.feature_compute import LINKED_COUNT_FEATURES

        provider = self._linked()
        if provider is None:
            return {}

        async def _one(dimension: str) -> tuple[str, Optional[int]]:
            key = event.get({"device": "device_id", "ip": "ip",
                             "address": "address_id"}[dimension])
            if key is None or str(key).strip() == "":
                return dimension, None
            try:
                value = await provider.get_linked_user_count(
                    dimension, str(key).strip()
                )
            except Exception as e:  # noqa: BLE001 - 09 的故障不能让整次计算失败
                log.warning("[FEA-5001] 模块 09 关联账号数查询失败 dimension=%s：%s",
                            dimension, e)
                return dimension, None
            if value is None:
                return dimension, None
            try:
                return dimension, int(value)
            except (TypeError, ValueError):
                # 09 返回了非数值：按"不可计算"处理（进缺失），不猜一个数
                log.warning("模块 09 返回了非数值的关联账号数 dimension=%s value=%r",
                            dimension, value)
                return dimension, None

        pairs = await asyncio.gather(
            *(_one(dim) for dim in LINKED_COUNT_FEATURES.values())
        )
        return dict(pairs)

    async def _new_snapshot_id(self, event_ts: int) -> str:
        """取快照编号：优先走 `seq_counters` 的原子序列，失败则用兜底编号。

        兜底而不是抛异常（见 `snapshot_id_fallback` 的说明）：编号是决策记录的
        必填关联字段，让它为 null 会让 E03 的引用断掉。
        """
        try:
            return await new_snapshot_id(db_module.get_db(), event_ts)
        except Exception as e:  # noqa: BLE001 - 序列服务不可用不该让整次计算失败
            log.warning("[FEA-5002] 快照编号生成失败，改用兜底编号（event_ts=%s）：%s",
                        event_ts, e)
            return snapshot_id_fallback(event_ts)

    # ---------------- 异步落库（BR-04-22/23） ----------------
    def _spawn_persist(self, snapshot: dict) -> None:
        """把落库丢到后台任务，**不阻塞同步决策**（BR-04-22）。"""
        task = asyncio.create_task(
            self._persist_with_retry(snapshot), name="feature-snapshot-persist"
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)

    async def _persist_with_retry(self, snapshot: dict) -> bool:
        last: Optional[Exception] = None
        for attempt in range(1, PERSIST_RETRY + 1):
            try:
                await self._repo().insert_snapshot(snapshot)
                self.stats["persisted"] += 1
                return True
            except Exception as e:  # noqa: BLE001 - 后台任务不允许把异常抛到无人区
                last = e
                code = getattr(e, "code", "FEA-5002")
                log.warning("[%s] feature_snapshots 落库失败（第 %d/%d 次）snapshot_id=%s：%s",
                            code, attempt, PERSIST_RETRY, snapshot.get("snapshot_id"), e)
            if attempt < PERSIST_RETRY:
                await asyncio.sleep(PERSIST_RETRY_BASE_SEC * attempt)

        # 决策早已返回，这里只做"最终一致"的兜底：入有界重试队列 + 告警
        self.stats["persist_failed"] += 1
        self._enqueue_retry(snapshot)
        log.error("[FEA-5002] %s snapshot_id=%s event_id=%s（决策已正常返回，未回滚）",
                  FEA_NOTICE["FEA-5002"], snapshot.get("snapshot_id"),
                  snapshot.get("event_id"))
        if last is not None:
            detail = f"{type(last).__name__}: {last}"
        else:  # pragma: no cover - 循环至少执行一次，理论上到不了这里
            detail = "unknown"
        DEGRADED.mark(f"[FEA-5002] 特征快照落库失败：{detail}")
        return False

    def _enqueue_retry(self, snapshot: dict) -> None:
        """把失败的快照放进有界重试队列（丢弃最旧，并记数）。

        为什么"丢最旧"而不是"拒绝新的"：最新的快照对应最近的决策，
        而排障时最近的数据最有用；并且队列本身有上限，语义必须明确，
        否则它会悄悄长成一个无界内存泄漏。
        """
        if len(self._retry_queue) >= RETRY_QUEUE_MAX:
            self._retry_queue.pop(0)
        self._retry_queue.append(snapshot)
        self.stats["requeued"] += 1

    async def flush(self, timeout: float = 5.0) -> bool:
        """等待在途落库任务收尾（测试与优雅关闭用）。

        与 `event_service.flush` 同因：不等待会留下 "Task was destroyed but it is
        pending" 告警，更糟的是把待写文档带进下一个事件循环。
        """
        if not self._persist_tasks:
            return True
        pending = list(self._persist_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            log.error("仍有 %d 个快照落库任务未完成（等待 %.1fs 超时）",
                      len(pending), timeout)
            return False

    async def retry_pending(self) -> int:
        """把重试队列里的快照再写一次（sweep 任务每轮顺带执行）。

        返回成功条数。**不重试就永远躺在内存里**——进程重启即丢，
        因此这里给它们一个真实的第二次机会。
        """
        if not self._retry_queue:
            return 0
        pending = self._retry_queue
        self._retry_queue = []
        ok = 0
        for snapshot in pending:
            try:
                await self._repo().insert_snapshot(snapshot)
                ok += 1
                self.stats["persisted"] += 1
            except Exception as e:  # noqa: BLE001 - 再失败就放回队列，留待下一轮
                self._enqueue_retry(snapshot)
                log.warning("[FEA-5002] 重试队列中的快照仍无法落库 snapshot_id=%s：%s",
                            snapshot.get("snapshot_id"), e)
        return ok

    # ---------------- 查询（供 07 / 10 与运维） ----------------
    async def get_snapshot(self, event_id: Any) -> dict:
        """按事件编号取快照（§3.2）。

        兼容两种写入形态：04 自己落的 `feature_snapshots` 文档（顶层字段），
        以及 03 随事件一起写入的嵌入式 `feature_snapshot`（03 的详情接口
        在 E02 还没有数据时会回落读它）。两者字段名不同，这里统一归一，
        否则调用方要自己判两套结构——那正是"契约漂移"的开始。
        """
        eid = require_event_id(event_id)
        doc = await self._repo().find_snapshot(eid)
        if doc is None:
            raise fea_error(
                "FEA-4004",
                "未找到该事件的特征快照",
                {"event_id": eid,
                 "hint": "快照是异步落库的（BR-04-22），刚接入的事件请稍后重试"},
            )
        return _normalize_snapshot(doc)

    async def latest_baselines(self, segment: str = "all") -> dict[str, Optional[dict]]:
        """取每项特征的统计基线（E21），查不到即 `None`（不编造）。"""
        docs = await self._repo().list_baselines(segment)
        out: dict[str, Optional[dict]] = {}
        for key in FEATURE_KEYS:
            doc = docs.get(key)
            if not doc:
                out[key] = None
                continue
            sample = int(doc.get("sample_size") or 0)
            # E21：sample_size < 100 该基线标记为不可用 → 对调用方等价于"没有基线"
            if sample < 100:
                out[key] = None
                continue
            out[key] = {
                "p50": doc.get("p50"),
                "p95": doc.get("p95"),
                "sample_size": sample,
                "window_days": doc.get("window_days"),
                "computed_at": doc.get("computed_at"),
                "segment": doc.get("segment", segment),
            }
        return out


def _normalize_snapshot(doc: dict) -> dict:
    """把库里的快照文档归一成 §3.2 的响应结构（含 03 写入的嵌入形态）。"""
    if "features" in doc and "window_config" in doc:
        payload = dict(doc)
    else:
        embedded = doc.get("feature_snapshot")
        if not isinstance(embedded, dict):
            # 结构不符合任何一种已知形态：如实返回原文档而不是伪造字段，
            # 让 07/10 的页面显示"数据异常"（Spec §2.1 的空态要求）
            return {
                "snapshot_id": doc.get("_id"),
                "event_id": doc.get("event_id"),
                "user_id": doc.get("user_id"),
                "features": {},
                "window_config": {},
                "computed_at": doc.get("computed_at"),
                "compute_ms": None,
                "missing_features": list(FEATURE_KEYS),
            }
        payload = dict(embedded)
        payload.setdefault("snapshot_id", doc.get("snapshot_id") or doc.get("_id"))
        payload.setdefault("event_id", doc.get("event_id"))
        payload.setdefault("user_id", doc.get("user_id"))

    payload["snapshot_id"] = payload.get("snapshot_id") or doc.get("_id")
    payload.pop("_id", None)
    payload.setdefault("missing_features", [])
    payload.setdefault("window_config", {})
    payload.setdefault("compute_ms", 0)
    payload.setdefault("computed_at", doc.get("computed_at"))
    return payload


def build_feature_meta(
    stat_baselines: Optional[dict[str, Optional[dict]]] = None,
) -> list[dict[str, Any]]:
    """组装 `/features/meta` 的 18 项元数据（§3.3 / BR-04-16）。

    **前端不得硬编码特征名与基线**，因此中文标签、分组、单位、数据类型、
    静态参照区间**全部在这里下发**。静态参照与统计基线（E21）走不同的键：

    | 键 | 来源 | 含义 |
    |---|---|---|
    | `baseline` / `baseline_desc` | `feature_baselines.py` | 静态参照区间（如 `≤1`） |
    | `stat_baseline` | E21 集合 | P50/P95（查不到即 `None`） |

    分成两个键是刻意的：合并之后，"这个数字是人工定的还是算出来的"就再也说不清，
    而那正是复核时最需要分清的一件事（详见 `feature_baselines` 的模块说明）。
    """
    stats = stat_baselines or {}
    items: list[dict[str, Any]] = []
    for key in FEATURE_KEYS:
        stat = stats.get(key)
        items.append({
            "key": key,
            "label": FEATURE_LABELS[key],
            "group": FEATURE_GROUPS[key],
            "unit": FEATURE_UNITS[key],
            "data_type": FEATURE_DATA_TYPES[key],
            **baseline_meta(key),
            "stat_baseline": stat,
            "stat_baseline_available": stat is not None,
        })
    return items


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = FeatureService()


def get_feature_service() -> FeatureService:
    """取特征服务单例（模块 03 经 `FeatureProvider` 装配的也是它）。"""
    return _SERVICE


async def compute(
    event: dict, *, affect_window: bool = True, persist: bool = True
) -> dict:
    """模块级便捷入口（§3.4：05 通过内部调用取特征，不走 HTTP）。

    `affect_window=False` 是**模块 10 仿真专用的只读通道**（BR-10-06），
    默认值保持真实链路行为不变。详见 `FeatureService.compute`。
    """
    return await _SERVICE.compute(event, affect_window=affect_window, persist=persist)


async def flush(timeout: float = 5.0) -> bool:
    """等待在途快照落库完成（测试与关闭流程用）。"""
    return await _SERVICE.flush(timeout)


async def get_snapshot(event_id: Any) -> dict:
    """模块级便捷入口：按事件编号取快照（§3.2）。"""
    return await _SERVICE.get_snapshot(event_id)


__all__ = [
    "DECISION_GATING_FEATURES",
    "EVENT_ID_RE",
    "FeatureService",
    "PERSIST_RETRY",
    "RETRY_QUEUE_MAX",
    "SNAPSHOT_ID_RE",
    "build_feature_meta",
    "compute",
    "empty_snapshot",
    "fea_error",
    "flush",
    "get_feature_service",
    "get_snapshot",
    "require_event_id",
    "snapshot_id_fallback",
]
