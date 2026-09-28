# -*- coding: utf-8 -*-
"""E02 `feature_snapshots` / E21 `feature_baselines` 的读写，以及画像读取（模块 04 §6）。

## 为什么画像读取也放在这里

Spec §6 规划的是 `repos/profile_repo.py`。本项目把"只被本模块读取、且只读不写"
的画像读取合并进同一个仓储文件，理由是它只有 20 行、且与特征计算一一对应，
拆成两个文件会让"这个特征的值从哪个字段来"的追查多跳一层。
**只读**是硬边界：E10~E13 的写入归模块 09，本模块不得越界（BR-04-07）。

## 为什么快照落库失败不抛到调用方

`BR-04-23`：快照落库失败**不回滚决策**。因此仓储把 `PyMongoError` 统一转成带
明确错误码的 `AppError`（`FEA-5002`），由**服务层在后台任务里**捕获并转成
"重试队列 + 告警"；同步决策路径早已返回，不受影响。
这与 `event_repo` 的处理一致——区别是事件落库的失败码是 `EVT-5004`。

## 为什么基线查询"查不到就返回 None"

E21 的编码约束（也被 `app/protocols.py` 的 `FeatureStore.baseline` 引用）：
查不到基线时界面展示「—」，**严禁用 0 或本次值冒充基线**。因此这里的返回类型
是 `Optional[dict]`，`None` 是"没有可用基线"的唯一表达。

## 为什么这里自己算 P50/P95 而不用 numpy

`numpy` 虽在 `requirements.txt` 里（模块 11 的验收脚本用它做真值比对），
但把"换台机器跑测试"的成败押在一个仅用于比对的可选依赖上是不划算的。
`percentile` 用标准库实现，并且与 `metric_percentile.percentile` 采用**同一种
插值口径**（线性插值），两处的分位数语义因此一致。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, Sequence

from pymongo.errors import PyMongoError

from app.constants import (
    COLL_DEVICES,
    COLL_FEATURE_BASELINES,
    COLL_FEATURE_SNAPSHOTS,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.engine.feature_compute import ProfileData
from app.errors import AppError, FEA_NOTICE
from app.logging import get_logger

log = get_logger("shop_risk_control.feature.repo")

#: 快照落库失败（`FEA-5002`）是**后台告警**，HTTP 状态取 200。
#:
#: 为什么 200 而不是 5xx：这条错误永远不会出现在 HTTP 响应里（落库是异步旁路），
#: 它只被后台任务捕获后转成"重试队列 + 告警"。若给它一个 5xx，就与 BR-00-12 的
#: 「5xxx = 服务端/依赖错误（会产生 5xx 响应）」语义打架，`test_errors.py` 的
#: 段位校验也会（正确地）报错——因此它不进 `FEA_CODES`，只在 `FEA_NOTICE` 里。
FEA_PERSIST_STATUS = 200

#: 统计基线的默认窗口（E21 `window_days`，默认 7）
DEFAULT_BASELINE_WINDOW_DAYS = 7
#: 样本量下限（E21：`sample_size < 100` 该基线标记为不可用）
MIN_BASELINE_SAMPLE = 100
#: 单次刷新最多拉取的快照条数上限。
#:
#: 有界是必须的：把 7×24 小时的全部快照拉进内存算分位，在演示库上可能几十万条。
#: 这里取最近 `max_items` 条（按 `computed_at` 倒序），并在返回里如实汇报
#: `truncated=True`——**不假装算的是全量**。
MAX_BASELINE_SAMPLES = 50_000


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位数（与模块 11 的 `metric_percentile` 同口径）。

    - 空序列 → `None`（不是 0：没有样本与"分位为 0"是两件事）
    - 单元素 → 该元素
    - 否则按 `pos = (n-1) * p` 在相邻两点间线性插值
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    if p <= 0:
        return ordered[0]
    if p >= 1:
        return ordered[-1]
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def _unavailable(detail: str, *, op: str) -> AppError:
    """把驱动异常转成 `FEA-5002`（快照落库失败，后台重试）。

    `data` 里只放错误类型与摘要：错误响应会回给前端，绝不能带连接信息。
    """
    return AppError(
        "FEA-5002", FEA_NOTICE["FEA-5002"], FEA_PERSIST_STATUS,
        {"detail": detail, "op": op},
    )


# ============================================================
# 画像读取（只读 E10~E13，写权限归模块 09）
# ============================================================
class ProfileReader(Protocol):
    """画像读取抽象。

    抽出 Protocol 是为了让"画像不可用"这条分支可被测试注入
    （`tests/feature_testlib.py` 的 `EmptyProfileReader` / `ExplodingProfileReader`），
    而不必真的把 Mongo 停掉——停库会让整条链路的其它断言一起失去意义。
    """

    async def load(self, event: dict) -> ProfileData:
        """读该事件涉及的四类画像；读不到就留 `None`，**不得编造**。"""


class MongoProfileReader:
    """从 E10~E13 读画像（只读）。"""

    def __init__(self, db: Any):
        self.db = db

    async def load(self, event: dict) -> ProfileData:
        """并发读四类画像（彼此独立，串行读会让 200ms 预算更紧）。

        任何一类读取失败都**不抛出**：那会把一次网抖动升级成整条决策链路降级。
        失败字段记进 `ProfileData.errors`，对应特征照常进 `missing`
        （BR-04-09：宁可声明"不知道"，也不给假值）。
        """
        import asyncio

        user_id = event.get("user_id")
        device_id = event.get("device_id")
        ip = event.get("ip")
        address_id = event.get("address_id")

        async def _find(coll: str, key: Any) -> tuple[Optional[dict], Optional[str]]:
            if key is None or str(key).strip() == "":
                return None, None
            try:
                doc = await self.db[coll].find_one({"_id": str(key).strip()})
                return doc, None
            except PyMongoError as e:  # 数据库不可用：记下来，不要影响其它画像
                return None, f"{type(e).__name__}: {e}"

        results = await asyncio.gather(
            _find(COLL_USERS, user_id),
            _find(COLL_DEVICES, device_id),
            _find(COLL_IP_POOL, ip),
            _find(COLL_USER_ADDRESSES, address_id),
        )
        (user, user_err), (device, device_err), (ip_doc, ip_err), (addr, addr_err) = results

        errors: list[str] = []
        for name, err in (
            ("user", user_err), ("device", device_err),
            ("ip", ip_err), ("address", addr_err),
        ):
            if err:
                errors.append(f"{name}:{err}")

        return ProfileData(
            user_register_at=_pick_ts(user, "register_at"),
            user_level=_pick_str(user, "level"),
            user_risk_tag_cnt=_pick_len(user, "risk_tags"),
            device_first_seen_at=_pick_ts(device, "first_seen_at", "first_seen"),
            ip_is_proxy=_pick_proxy(ip_doc),
            address_aftersale_cnt=_pick_int(addr, "aftersale_cnt"),
            errors=tuple(errors),
        )


def _pick_ts(doc: Optional[dict], *keys: str) -> Optional[int]:
    """取时间戳字段；兼容"秒"与"毫秒"两种写入习惯。

    秒/毫秒的判别用**数量级**：`< 10^11` 视为秒。这个界是安全的——
    10^11 毫秒 ≈ 1973 年，10^11 秒 ≈ 公元 5138 年，两边都不可能与真实数据混淆。
    """
    if not doc:
        return None
    for key in keys:
        value = doc.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        return number * 1000 if number < 10 ** 11 else number
    return None


def _pick_str(doc: Optional[dict], key: str) -> Optional[str]:
    if not doc:
        return None
    value = doc.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_int(doc: Optional[dict], key: str) -> Optional[int]:
    if not doc:
        return None
    value = doc.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_len(doc: Optional[dict], key: str) -> Optional[int]:
    """取数组字段的长度（`user_risk_tag_cnt = len(users.risk_tags)`）。

    记录**存在但该字段缺失**时返回 `None` 而不是 0：`risk_tags` 缺失可能是
    "没爬过"也可能是"写漏了"，把两者都当 0 会得出"该用户没有任何风险标签"
    这一未经证实的结论。
    """
    if not doc:
        return None
    value = doc.get(key)
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return None


def _pick_proxy(doc: Optional[dict]) -> Optional[bool]:
    """`ip_is_proxy`：E12 的 `is_proxy` 或 `is_idc` 任一为真即为真。

    Spec §4.2 的原文是「由 IP 库的 `is_proxy`/`is_idc` 标记得出」——两者都
    指向"非住宅出口"，合取（or）语义才能覆盖"机房 IP 没标代理"的情况。
    两个字段都缺失 → `None`（进缺失），**不默认 False**：默认 False 等于
    对 05 宣称"这个 IP 是干净的住宅 IP"，那是本次计算里最危险的一次猜测。
    """
    if not doc:
        return None
    seen = False
    for key in ("is_proxy", "is_idc"):
        value = doc.get(key)
        if isinstance(value, bool):
            seen = True
            if value:
                return True
    return False if seen else None


# ============================================================
# E02 快照 / E21 基线 仓储
# ============================================================
class FeatureRepo:
    """`feature_snapshots` 与 `feature_baselines` 的唯一读写出口。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_FEATURE_SNAPSHOTS]
        self.baseline_col = db[COLL_FEATURE_BASELINES]

    # ---------------- E02 写入 ----------------
    async def insert_snapshot(self, doc: dict) -> str:
        """插入一条快照。失败抛 `FEA-5002`（服务层在后台任务里捕获）。

        **`_id` 必须落进去**：E02 的主键就是快照编号 `SNP{yyyyMMdd}{12位}`。
        服务层组装时用的是业务字段名 `snapshot_id`（03 的契约要求这个键名），
        因此在这一层做一次映射——两个名字指向同一个事实，只在这里转换一次，
        比让上层各处记住"什么时候用哪个名字"可靠得多。
        （编号的序列号是**每日**递增的，不同日期的编号天然不同，因此不存在
        "两个快照撞主键"的问题。）

        用 `insert_one` 而不是 `replace_one(upsert=True)`：同一事件重算时应当
        **报冲突**而不是静默覆盖——静默覆盖会让"这份快照是第一次算的还是后来
        重算的"再也说不清，而快照正是复核时的事实地基。
        """
        payload = dict(doc)
        payload.setdefault("_id", payload.get("snapshot_id"))
        if not payload.get("_id"):
            # 编号缺失不该被静默补成 ObjectId：那会让 E03 的 `snapshot_id`
            # 引用断掉。抛 FEA-5002 由后台任务记录，而不是写一份无编号的快照。
            raise _unavailable("快照编号缺失（snapshot_id 为空）", op="insert_snapshot")
        try:
            await self.col.insert_one(payload)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="insert_snapshot") from e
        return str(payload["_id"])

    async def find_snapshot(self, event_id: str) -> Optional[dict]:
        """按 `event_id` 取快照（走唯一索引）；查不到返回 `None`。

        查不到与"查询失败"都返回 `None`：HTTP 层对两者的处置相同
        （`FEA-4004` 404 + 提示），而把后者变成 500 只会让前端把"写入中"
        显示成"系统故障"。失败原因进日志。
        """
        try:
            return await self.col.find_one({"event_id": event_id})
        except PyMongoError as e:
            log.warning("[FEA-5002] feature_snapshots 查询失败 event_id=%s：%s",
                        event_id, e)
            return None

    async def find_by_snapshot_id(self, snapshot_id: str) -> Optional[dict]:
        """按快照编号取（`_id`），供日后"按序号核对"的排障路径使用。"""
        try:
            return await self.col.find_one({"_id": snapshot_id})
        except PyMongoError as e:
            log.warning("[FEA-5002] feature_snapshots 查询失败 snapshot_id=%s：%s",
                        snapshot_id, e)
            return None

    async def count_scanned(self, since_ms: int, limit: int) -> tuple[list[dict], bool]:
        """取 `computed_at >= since_ms` 的最近 `limit` 条快照（统计基线用）。

        返回 `(docs, truncated)`：`truncated=True` 表示窗口内的快照多于 `limit`，
        本次分位只基于**最近 limit 条**。如实汇报比"悄悄算个近似值"重要。
        """
        try:
            cursor = (
                self.col.find({"computed_at": {"$gte": int(since_ms)}})
                .sort("computed_at", -1)
                .limit(limit + 1)
            )
            rows = await cursor.to_list(length=limit + 1)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="count_scanned") from e
        truncated = len(rows) > limit
        return rows[:limit], truncated

    # ---------------- E21 写入 / 读取 ----------------
    @staticmethod
    def baseline_id(feature_name: str, segment: str) -> str:
        """E21 的主键：`{feature_name}:{segment}`（E21 的 `_id` 定义）。"""
        return f"{feature_name}:{segment}"

    async def upsert_baselines(self, docs: Sequence[dict]) -> int:
        """覆盖式写入统计基线（每日刷新，幂等）。

        用 `$set` 覆盖而不是 `$inc`：基线是"从当前样本重新算一遍"的结果，
        重跑必须得到同一份数值（与模块 11 的 rollup 同因）。
        """
        if not docs:
            return 0
        written = 0
        for doc in docs:
            try:
                await self.baseline_col.update_one(
                    {"_id": doc["_id"]}, {"$set": dict(doc)}, upsert=True,
                )
                written += 1
            except PyMongoError as e:
                raise _unavailable(f"{type(e).__name__}: {e}", op="upsert_baselines") from e
        return written

    async def delete_baselines(self, ids: Iterable[str]) -> int:
        """删除不再有效的基线（样本量跌破下限、或特征已不在窗口内）。

        **为什么是删除而不是写一个"不可用"标记**：E21 的编码约束是"查不到即
        显示 —"。留一条 `available=false` 的记录会让读取方必须判断两个字段，
        而只要有一个调用方忘了判，界面上就会出现一条**过期的 P95**。
        删掉之后"没有基线"只有一种表达，判断只有一处。
        """
        wanted = [i for i in ids if i]
        if not wanted:
            return 0
        try:
            result = await self.baseline_col.delete_many({"_id": {"$in": wanted}})
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="delete_baselines") from e
        return int(getattr(result, "deleted_count", 0))

    async def list_baselines(self, segment: str = "all") -> dict[str, dict]:
        """取某分段的全部基线，返回 `{feature_name: doc}`（查不到即空 dict）。"""
        try:
            cursor = self.baseline_col.find({"segment": segment})
            rows = await cursor.to_list(length=200)
        except PyMongoError as e:
            log.warning("[FEA-5002] feature_baselines 查询失败 segment=%s：%s", segment, e)
            return {}
        return {str(r.get("feature_name")): r for r in rows if r.get("feature_name")}

    async def all_baselines(self) -> dict[str, dict[str, dict]]:
        """取全部基线，按 `segment` 分组：`{segment: {feature_name: doc}}`。"""
        try:
            cursor = self.baseline_col.find({})
            rows = await cursor.to_list(length=1000)
        except PyMongoError as e:
            log.warning("[FEA-5002] feature_baselines 全量查询失败：%s", e)
            return {}
        out: dict[str, dict[str, dict]] = {}
        for row in rows:
            segment = str(row.get("segment") or "all")
            name = str(row.get("feature_name") or "")
            if name:
                out.setdefault(segment, {})[name] = row
        return out


__all__ = [
    "DEFAULT_BASELINE_WINDOW_DAYS",
    "FeatureRepo",
    "MAX_BASELINE_SAMPLES",
    "MIN_BASELINE_SAMPLE",
    "MongoProfileReader",
    "ProfileReader",
    "percentile",
]
