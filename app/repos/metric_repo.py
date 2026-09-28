# -*- coding: utf-8 -*-
"""E15 `metric_buckets` 仓储：唯一的桶读写出口（模块 11 §3.10 / AD-03）。

## 为什么写入只能 `$inc` upsert（BR-11-05）

指标桶是**增量聚合**的载体：一次决策同时写 `global`/`level`/`scene`/`rule` 四类桶，
高并发下同一秒会有大量决策落在同一个 `_id` 上。若用"读出来 → 加一 → 写回去"，
两个并发请求会读到同一个旧值，后写的把先写的覆盖掉——表现为大盘数字**莫名偏少**，
而且丢的量与并发度相关，几乎无法复现。`UpdateOne(..., {"$inc": ...}, upsert=True)`
把"自增"交给 MongoDB 单文档原子操作，丢更新在原理上就不存在。

`$setOnInsert` 只负责**首次插入**时补齐静态字段：桶的维度、粒度、起点与创建时间
在一次 upsert 中只会写一次，重复 upsert 不该反复刷新 `created_at`。
`expire_at` 也放在 `$setOnInsert` 且**仅当不是 `None` 时写入**——`1d` 桶不带该字段
即"永不过期"（BR-11-21），这是"每类粒度保留期不同"的唯一实现方式（集合级只有
一份 TTL 配置，见 `constants.INDEX_SPECS[COLL_METRIC_BUCKETS]` 的说明）。

## 为什么读一律走聚合管道

AD-03 禁止查询时扫明细，但没有禁止"扫很多桶"。桶的数量是 `维度 × 粒度 × 时间格`
（30 天 × 四维度约几千到几十万），把它拉到 Python 里循环求和既慢又吃内存。
`$match + $group` 让 Mongo 在服务端完成求和，只回传**一行**（或每个维度一行）。

## 错误转换为什么放在这一层

与 `user_repo.py` 同源：转换放在离数据库最近的一层，"驱动异常穿透成 500" 就
不可能发生——若放在服务层，将来新增调用点忘了 try 就会漏。本模块不参与放行判定，
因此这里的失败**只影响指标展示**，由服务层决定降级（返回最近一次成功快照）。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from pymongo import ASCENDING, DESCENDING, UpdateOne
from pymongo.errors import PyMongoError

from app.constants import COLL_METRIC_BUCKETS, COLL_RISK_CASES, COLL_RULES
from app.engine.metric_agg import HIST_KEYS
from app.errors import MET_CODES, AppError
from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.metric.repo")

# 逻辑字段名 -> 库中路径的前缀。桶内所有计数都收在 `metrics` 子文档下（§3.10），
# 集中一处拼前缀，避免调用方各自手写 "metrics.xxx" 拼错后静默得到 0。
METRICS_PREFIX = "metrics."


def _sum_expr(field: str) -> dict:
    """聚合表达式：对某字段求和，缺失按 0 计。

    显式 `$ifNull` 而不是依赖 `$sum` 忽略缺失值：桶的 `metrics` 子文档在并发
    upsert 初期可能只有部分键（例如 `elapsed_hist` 尚未写入），把"缺失"与"0"
    写成同一个表达式，读代码的人不必去查 `$sum` 对缺失字段的隐式规则。
    """
    return {"$sum": {"$ifNull": [f"${METRICS_PREFIX}{field}", 0]}}


def _alias(field: str) -> str:
    """聚合输出字段的别名：把 `.` 换成 `__`。

    **这是必须的**：`$group` 的输出字段名不允许包含 `.`（Mongo 会直接报
    "The field name 'metrics.event_cnt' cannot contain '.'"），但输入路径
    `$metrics.event_cnt` 是合法的。因此求和时用别名做输出键，读回来时再映射回
    业务字段名——调用方完全看不到这一层。
    """
    return field.replace(".", "__")


def _as_int(value: Any) -> int:
    """聚合结果统一成 int（Mongo 可能回传 int32/int64/float）。"""
    if value is None:
        return 0
    return int(value)


def _unavailable(detail: str, *, op: str) -> AppError:
    """把驱动异常转成 `MET-5001`（§5）。

    只带错误类型与摘要，不带连接串（脱敏由日志过滤器兜底，但错误 `data`
    会回给前端，绝不能含凭据）。
    """
    return AppError("MET-5001", MET_CODES["MET-5001"][1], 503,
                    {"detail": detail, "op": op})


class MetricRepo:
    """`metric_buckets` 的读写实现 + 两个只读联查（`rules` / `risk_cases`）。"""

    def __init__(self, db: Any):
        # 保留 db 句柄：卡片要联查 `risk_cases` 的待审计数、排行要联查 `rules.name`，
        # 这两者不属于本模块的实体，只读不写（BR-11-18）。
        self.db = db
        self.col = db[COLL_METRIC_BUCKETS]

    # ============================================================
    # 写入
    # ============================================================
    async def upsert_many(self, payloads: Sequence[dict]) -> int:
        """批量增量写入（BR-11-04/05），返回被写入的桶数量。

        **同批去重**：若同一批里出现相同 `_id`（例如 `hits` 里重复了同一个
        `rule_code`），无序 `bulk_write` 对同一文档的两次更新结果未定义。因此在
        应用侧先把 `inc` 合并成一次更新——批内合并是纯函数运算，不引入读改写。
        """
        if not payloads:
            return 0

        merged: dict[str, dict] = {}
        for payload in payloads:
            doc = merged.get(payload["_id"])
            if doc is None:
                # 静态字段只取"首次插入"需要的部分：expire_at 为 None 时**不写入**，
                # 因为 Mongo 的 TTL 索引遇到字段缺失即视为"永不过期"，
                # 而显式写 null 会让 TTL 在部分版本上按纪元时间处理、当场删桶。
                static: dict[str, Any] = {
                    "bucket_type": payload["bucket_type"],
                    "bucket_key": payload["bucket_key"],
                    "granularity": payload["granularity"],
                    "bucket_ts": payload["bucket_ts"],
                    "created_at": payload["set_on_insert"]["created_at"],
                }
                if payload.get("expire_at") is not None:
                    static["expire_at"] = payload["expire_at"]
                merged[payload["_id"]] = {"inc": dict(payload["inc"]), "static": static}
                continue
            for key, value in payload["inc"].items():
                doc["inc"][key] = doc["inc"].get(key, 0) + value

        ops = [
            UpdateOne({"_id": bucket_id},
                      {"$inc": body["inc"], "$setOnInsert": body["static"]},
                      upsert=True)
            for bucket_id, body in merged.items()
        ]
        try:
            result = await self.col.bulk_write(ops, ordered=False)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="upsert_many") from e
        return int(getattr(result, "upserted_count", 0)) + int(result.modified_count)

    async def overwrite_many(self, docs: Sequence[dict]) -> int:
        """**覆盖式**批量写入（rollup 专用，BR-11-15）。

        与 `upsert_many` 的本质区别：这里用 `$set` 整体替换 `metrics` 子文档，
        因此同一时间窗重复执行的结果**完全一致**（幂等）。若也写成 `$inc`，
        重跑一次就会把数值翻倍——这正是 rollup 必须与实时写入分开一条通道的原因。

        `$set` 写整个 `metrics` 对象还有一个附带好处：源数据里已消失的分档字段
        （例如早期误写的字段）会被一并清掉，不会留下"越算越多"的幽灵计数。
        """
        if not docs:
            return 0

        ts = now_ms()
        ops = []
        for doc in docs:
            static: dict[str, Any] = {
                "bucket_type": doc["bucket_type"],
                "bucket_key": doc["bucket_key"],
                "granularity": doc["granularity"],
                "bucket_ts": doc["bucket_ts"],
                "updated_at": ts,
            }
            if doc.get("expire_at") is not None:
                static["expire_at"] = doc["expire_at"]
            ops.append(
                UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {**static, "metrics": doc["metrics"]},
                     "$setOnInsert": {"created_at": doc.get("created_at", ts)}},
                    upsert=True,
                )
            )
        try:
            result = await self.col.bulk_write(ops, ordered=False)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="overwrite_many") from e
        return int(getattr(result, "upserted_count", 0)) + int(result.modified_count)

    # ============================================================
    # 读取
    # ============================================================
    @staticmethod
    def bucket_filter(
        bucket_type: str,
        granularity: str,
        from_ts: int,
        to_ts: int,
        bucket_key: Optional[str] = None,
    ) -> dict:
        """桶查询条件（顺序与 `ix_dim_ts` 索引前缀一致，避免全集合扫描）。

        `bucket_ts` 用**闭区间**：`from_ts`/`to_ts` 都由 `metric_bucket.range_bounds`
        或 `trend_points` 给出，本身就是桶起点，包含两端正是"图上枚举的桶"
        与"库里过滤的桶"严格同一批的保证。
        """
        flt: dict[str, Any] = {
            "bucket_type": bucket_type,
            "granularity": granularity,
            "bucket_ts": {"$gte": int(from_ts), "$lte": int(to_ts)},
        }
        if bucket_key is not None:
            flt["bucket_key"] = bucket_key
        return flt

    async def sum_range(
        self,
        *,
        bucket_type: str,
        granularity: str,
        from_ts: int,
        to_ts: int,
        fields: Sequence[str],
        bucket_key: Optional[str] = None,
    ) -> dict[str, int]:
        """区间内单个维度的求和（卡片、排行分母、吞吐都走它）。

        `fields` 是**相对 `metrics.`** 的字段名，允许带点（如 `elapsed_hist.le5`），
        这样聚合表达式不必区分"一级计数"与"嵌套直方图"。
        """
        field_list = list(fields)
        pipeline: list[dict] = [
            {"$match": self.bucket_filter(bucket_type, granularity, from_ts, to_ts, bucket_key)},
            {"$group": {"_id": None, **{_alias(f): _sum_expr(f) for f in field_list}}},
        ]
        try:
            # 注意：pymongo 的异步 `aggregate()` **本身是协程**（返回 AsyncCommandCursor），
            # 必须 `await` 两次才能拿到文档——少一次 await 只会在真正取数据时报
            # "'coroutine' object has no attribute 'to_list'"，把聚合写成"看起来对"的代码
            cursor = await self.col.aggregate(pipeline)
            rows = await cursor.to_list(length=1)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="sum_range") from e
        row = rows[0] if rows else {}
        return {f: _as_int(row.get(_alias(f))) for f in field_list}

    async def series(
        self,
        *,
        bucket_type: str,
        granularity: str,
        from_ts: int,
        to_ts: int,
        fields: Sequence[str],
        bucket_key: Optional[str] = None,
    ) -> dict[int, dict[str, int]]:
        """按 `bucket_ts` 分组的序列（趋势图专用），返回 `{bucket_ts: {字段: 值}}`。

        只回传"每个时间桶一行"，与明细量无关（V-11-02 的耗时只与桶数相关）。
        缺桶由调用方补零（§2.2：折线不能断裂是服务端职责）。
        """
        field_list = list(fields)
        pipeline: list[dict] = [
            {"$match": self.bucket_filter(bucket_type, granularity, from_ts, to_ts, bucket_key)},
            {"$group": {"_id": "$bucket_ts", **{_alias(f): _sum_expr(f) for f in field_list}}},
            {"$sort": {"_id": ASCENDING}},
        ]
        try:
            cursor = await self.col.aggregate(pipeline)
            rows = await cursor.to_list(length=None)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="series") from e
        out: dict[int, dict[str, int]] = {}
        for row in rows:
            out[_as_int(row.get("_id"))] = {f: _as_int(row.get(_alias(f))) for f in field_list}
        return out

    async def sum_by_key(
        self,
        *,
        bucket_type: str,
        granularity: str,
        from_ts: int,
        to_ts: int,
        fields: Sequence[str],
        sort_field: Optional[str] = None,
        descending: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """按 `bucket_key` 分组求和（等级/场景分布、规则排行）。

        排序与截断**下沉到数据库**（BR-11-22：排序即计算）。`bucket_key` 升序是
        默认的第二排序键——并列时的稳定顺序必须由服务端给出，否则两次查询可能
        返回不同顺序，页面上的"名次"会自己跳动。
        """
        field_list = list(fields)
        pipeline: list[dict] = [
            {"$match": self.bucket_filter(bucket_type, granularity, from_ts, to_ts)},
            {"$group": {"_id": "$bucket_key", **{_alias(f): _sum_expr(f) for f in field_list}}},
        ]
        sort_spec: list[tuple[str, int]] = []
        if sort_field:
            sort_spec.append((_alias(sort_field), DESCENDING if descending else ASCENDING))
        sort_spec.append(("_id", ASCENDING))
        pipeline.append({"$sort": dict(sort_spec)})
        if limit is not None:
            pipeline.append({"$limit": int(limit)})
        try:
            cursor = await self.col.aggregate(pipeline)
            rows = await cursor.to_list(length=limit)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="sum_by_key") from e
        return [
            {"bucket_key": str(row.get("_id")),
             **{f: _as_int(row.get(_alias(f))) for f in field_list}}
            for row in rows
        ]

    async def sum_by_dimension(
        self,
        *,
        granularity: str,
        from_ts: int,
        to_ts: int,
        fields: Sequence[str],
        bucket_types: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """按 `(bucket_type, bucket_key)` 分组求和（rollup 的源查询，BR-11-15）。

        一次调用给出"该时间窗内所有维度"的合计，因此一个目标桶只需**一次**聚合，
        而不是每个维度各查一次；`_n` 是参与本次聚合的源桶个数，用于回传
        `scanned_1m_buckets`（省掉一次 `count_documents`）。
        """
        field_list = list(fields)
        flt: dict[str, Any] = {
            "granularity": granularity,
            "bucket_ts": {"$gte": int(from_ts), "$lte": int(to_ts)},
        }
        if bucket_types is not None:
            flt["bucket_type"] = {"$in": list(bucket_types)}
        pipeline: list[dict] = [
            {"$match": flt},
            {"$group": {
                "_id": {"t": "$bucket_type", "k": "$bucket_key"},
                "_n": {"$sum": 1},
                **{_alias(f): _sum_expr(f) for f in field_list},
            }},
        ]
        try:
            cursor = await self.col.aggregate(pipeline)
            rows = await cursor.to_list(length=None)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="sum_by_dimension") from e
        out: list[dict] = []
        for row in rows:
            key = row.get("_id") or {}
            out.append({
                "bucket_type": str(key.get("t")),
                "bucket_key": str(key.get("k")),
                "scanned": _as_int(row.get("_n")),
                **{f: _as_int(row.get(_alias(f))) for f in field_list},
            })
        return out

    async def get_bucket(
        self, bucket_type: str, bucket_key: str, granularity: str, bucket_ts: int
    ) -> Optional[dict]:
        """读单个桶（吞吐取"最近一个已闭合 `1m` 桶"用；也是排障时的最小抓手）。"""
        try:
            return await self.col.find_one({
                "bucket_type": bucket_type,
                "bucket_key": bucket_key,
                "granularity": granularity,
                "bucket_ts": int(bucket_ts),
            })
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="get_bucket") from e

    async def elapsed_hist(
        self,
        *,
        granularity: str,
        from_ts: int,
        to_ts: int,
        bucket_type: str = "global",
        bucket_key: Optional[str] = "all",
    ) -> dict[str, int]:
        """延迟直方图求和（BR-11-13）；返回 9 个桶位的计数，缺失位为 0。

        单独包一层是为了让调用方只认"直方图"这个业务概念，不必知道它在桶内
        存成 `metrics.elapsed_hist.le5` 这种路径。
        """
        raw = await self.sum_range(
            bucket_type=bucket_type, granularity=granularity,
            from_ts=from_ts, to_ts=to_ts, bucket_key=bucket_key,
            fields=[f"elapsed_hist.{k}" for k in HIST_KEYS],
        )
        return {k: raw[f"elapsed_hist.{k}"] for k in HIST_KEYS}

    async def count(self) -> int:
        """桶总数（运维排查与测试断言用）。"""
        try:
            return await self.col.count_documents({})
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="count") from e

    # ============================================================
    # 只读联查（**非本模块实体**，只读不写，BR-11-17/18）
    # ============================================================
    async def count_pending_cases(self) -> int:
        """待审案件数（卡片用，BR-11-17：当前态 gauge，不套时间范围）。

        刻意不做 `range` 过滤：它回答的是"现在有多少单子等着人处理"，
        套上时间窗会变成"所选时间段内创建的待审案件"，与运营的直觉不符。
        """
        try:
            return int(await self.db[COLL_RISK_CASES].count_documents({"status": "pending"}))
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="count_pending_cases") from e

    async def rules_by_codes(self, codes: Sequence[str]) -> dict[str, dict]:
        """按 `rule_code` 批量取规则的名称与状态（排行联查，§2.4）。

        `_id` 与 `rule_code` **两种键都认**：E05 的规则编号既是主键也是业务编号，
        但模块 05 尚未落地，无法从实现确认它究竟写在哪个字段上。多认一个键
        不会改变正确结果，却能让排行在两种存法下都显示出规则名（而不是回落成编码）。
        """
        wanted = [c for c in {str(c) for c in codes} if c]
        if not wanted:
            return {}
        try:
            cursor = self.db[COLL_RULES].find(
                {"$or": [{"_id": {"$in": wanted}}, {"rule_code": {"$in": wanted}}]},
                {"name": 1, "status": 1, "rule_code": 1},
            )
            rows = await cursor.to_list(length=len(wanted))
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="rules_by_codes") from e
        out: dict[str, dict] = {}
        for row in rows:
            code = str(row.get("rule_code") or row.get("_id") or "")
            if code:
                out[code] = {"name": row.get("name"), "status": row.get("status")}
        return out
