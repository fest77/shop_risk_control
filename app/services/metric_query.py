# -*- coding: utf-8 -*-
"""五类查询的口径组装（§3.2 ~ §3.6，BR-11-07/08/09/12/13/14/17/22）。

## 这一层为什么必须独立成文件

"派生量一律在读取时由原始累计量计算"（BR-11-07）意味着**所有百分比、均值、
排行都在这里产生**，模块 02 只做格式化（边界 §3.2）。口径写错的代价是整块大盘
都在骗人，因此这里刻意做到：
1. **不依赖 FastAPI、不依赖 pymongo**，只依赖一个可注入的 repo —— 于是每条口径
   都能用假 repo 单测（`tests/test_metric_derived.py`），不必起数据库；
2. `now` 由 `now_provider` 注入 —— 趋势/吞吐的边界用例可以固定时钟断言，
   否则"末点是否 partial"这类断言会随执行时刻漂移；
3. `null` 与 `0` 严格区分（BR-11-08）：分母为 0 一律 `None`，绝不给 0，
   否则冷启动时大盘会显示"拦截率 0%"这种**看起来正常但完全错误**的结论。

## 交叉筛选（BR-11-12）的唯一依据

`pass↔low`、`review↔medium`、`reject↔high` 是 1:1 的。所以
`scene=order&level=high` 不需要第 5 个维度，直接读 `scene:order` 桶的 `reject_cnt`
即可。读取时由 `_Source.decision` 表达"要用哪一档分档计数"，其余两档恒为 0——
因为整片筛选结果按定义只包含该档事件。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Optional

from app.engine.metric_agg import (
    HIST_KEYS,
    LEVEL_KEYS,
    LEVEL_TO_DECISION,
    SAVED_AMOUNT_TYPES,
    SCENE_KEYS,
)
from app.engine.metric_bucket import (
    GRANULARITY_MS,
    MAX_TREND_POINTS,
    RANGE_SPECS,
    align,
    range_bounds,
    trend_points,
)
from app.engine.metric_percentile import percentile
from app.schemas.metric_schema import name_of_level, name_of_scene
from app.utils.timeutil import now_ms as _now_ms

# 吞吐窗口（§3.6）
WINDOW_MS: dict[str, int] = {"1m": 60_000, "5m": 300_000, "15m": 900_000}

# 各维度桶的字段集合（§3.10 的"实际写入字段"列）
COUNT_FIELDS: tuple[str, ...] = ("event_cnt", "pass_cnt", "review_cnt", "reject_cnt")
SCORE_FIELDS: tuple[str, ...] = ("score_sum", "score_cnt")
SAVED_FIELDS: tuple[str, ...] = (
    "estimated_saved_amount",
    *(f"saved_amount_by_type.{t}" for t in SAVED_AMOUNT_TYPES),
)
HIT_FIELDS: tuple[str, ...] = (
    "hit_cnt", "pass_hit_cnt", "review_hit_cnt", "reject_hit_cnt", "hit_score_sum",
)
# 卡片/趋势/分布所需的全部字段（不含延迟统计：延迟只在吞吐接口用，见 §3.6）
BASE_FIELDS: tuple[str, ...] = COUNT_FIELDS + SCORE_FIELDS + SAVED_FIELDS

_DECISION_KEYS: tuple[str, ...] = ("pass", "review", "reject")


def round_half_up(value: float, digits: int = 4) -> float:
    """四舍五入到 `digits` 位（BR-11-09）。

    用 `Decimal` 而不是 `round()`：`round()` 是**银行家舍入**（0.00005 -> 0.0000），
    与中文语境里的"四舍五入"以及页面上手工核对的结果不一致。派生量要经得起
    "拿计算器复算"的检查，因此这里显式指定 `ROUND_HALF_UP`。
    """
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def ratio(numerator: Any, denominator: Any) -> Optional[float]:
    """派生比值：分母为 0 返回 `None`（BR-11-08），否则 4 位小数（BR-11-09）。"""
    den = int(denominator or 0)
    if den <= 0:
        return None
    return round_half_up(int(numerator or 0) / den)


@dataclass(frozen=True)
class _Source:
    """「这次查询该读哪一类桶」的解析结果。"""

    bucket_type: str
    bucket_key: Optional[str]
    # 交叉筛选时要用的分档字段（pass/review/reject）；None 表示不过滤分档
    decision: Optional[str] = None


def _counts(fields: dict[str, int], decision: Optional[str]) -> dict[str, int]:
    """从桶字段里取三档计数与事件总数。

    - 无交叉筛选：直接读桶（`level` 桶天然只有一档非零，`scene`/`global` 桶三档齐全）
    - 交叉筛选：整片结果按定义只含该档事件，故 `event_cnt` = 该档计数、其余两档为 0
    """
    if decision is None:
        out = {f"{k}_cnt": int(fields.get(f"{k}_cnt") or 0) for k in _DECISION_KEYS}
        out["event_cnt"] = int(fields.get("event_cnt") or 0)
        return out
    matching = int(fields.get(f"{decision}_cnt") or 0)
    out = {f"{k}_cnt": (matching if k == decision else 0) for k in _DECISION_KEYS}
    out["event_cnt"] = matching
    return out


class MetricQuery:
    """把 repo 的原始累计量翻译成 §3.2 ~ §3.6 的响应数据。"""

    def __init__(
        self,
        repo: Any,
        *,
        write_granularity: str = "1m",
        now_provider: Optional[Callable[[], int]] = None,
    ):
        self.repo = repo
        # 写入基础粒度只影响一处：BR-11-16 的"1m 级趋势不可用"降级判定
        self.write_granularity = write_granularity
        # `None` 时在**运行期**取模块级 now_ms：默认参数会在定义时求值，
        # 那样测试就无法通过替换模块属性整体冻结时钟
        self._now = now_provider or _now_ms

    # ---------------- 公共解析 ----------------
    def _granularity(self, range_: str, granularity: Optional[str]) -> str:
        """实际使用的桶粒度（缺省由 range 推导，§3.1）。"""
        return granularity or RANGE_SPECS[range_][0]

    @staticmethod
    def _source(scene: Optional[str], level: Optional[str]) -> _Source:
        """解析读取的维度桶。

        优先级：`scene` > `level` > `global`。同时给定时读 `scene` 桶并按
        `level` 取分档计数（BR-11-12）——`scene` 桶同时带三档计数，是唯一能
        直接回答"某场景下某档有多少"的桶；`level` 桶没有场景维度，反过来不行。
        """
        if scene:
            decision = LEVEL_TO_DECISION.get(level) if level else None
            return _Source("scene", scene, decision)
        if level:
            return _Source("level", level, None)
        return _Source("global", "all", None)

    def _saved_amount(self, fields: dict[str, int], level: Optional[str]) -> tuple[int, dict[str, int]]:
        """资损口径（BR-11-14）。

        `level=low/medium` 恒为 0：这两种筛选取的是 pass/review 片，而资损只由
        `reject` 产生。若不在这里归零，`scene=order&level=low` 会把该场景**全部**
        拦截金额算进去——数字看起来"有数据"，实际与筛选条件毫无关系。
        """
        zero = {t: 0 for t in SAVED_AMOUNT_TYPES}
        if level is not None and level != "high":
            return 0, zero
        by_type = {t: int(fields.get(f"saved_amount_by_type.{t}") or 0) for t in SAVED_AMOUNT_TYPES}
        return int(fields.get("estimated_saved_amount") or 0), by_type

    # ---------------- §3.2 overview ----------------
    async def overview(
        self, *, range_: str, scene: Optional[str] = None, level: Optional[str] = None,
        granularity: Optional[str] = None,
    ) -> dict:
        """4 张卡片。`pending_case_cnt` 是当前态 gauge，**不套 range/scene/level**（BR-11-17）。"""
        now = self._now()
        gran = self._granularity(range_, granularity)
        from_ts, to_ts, _ = range_bounds(range_, now)
        src = self._source(scene, level)

        fields = await self.repo.sum_range(
            bucket_type=src.bucket_type, bucket_key=src.bucket_key, granularity=gran,
            from_ts=from_ts, to_ts=to_ts, fields=BASE_FIELDS,
        )
        counts = _counts(fields, src.decision)
        saved_total, saved_by_type = self._saved_amount(fields, level)
        # 待审案件数与时间窗无关：它回答"现在积压多少"，即使筛选变化也重新查询，
        # 保证同屏的卡片来自同一时刻（N-11-9 已登记：卡片会重新请求但数值可能不变）
        pending = await self.repo.count_pending_cases()

        return {
            "range": range_,
            "granularity": gran,
            "cards": {
                "event_cnt": counts["event_cnt"],
                "pass_cnt": counts["pass_cnt"],
                "review_cnt": counts["review_cnt"],
                "reject_cnt": counts["reject_cnt"],
                "block_rate": ratio(counts["reject_cnt"], counts["event_cnt"]),
                "avg_score": ratio(fields.get("score_sum"), fields.get("score_cnt")),
                "pending_case_cnt": int(pending),
                "estimated_saved_amount": saved_total,
                "saved_amount_by_type": saved_by_type,
            },
            "generated_at": now,
        }

    # ---------------- §3.3 trend ----------------
    async def trend(
        self, *, range_: str, scene: Optional[str] = None, level: Optional[str] = None,
        granularity: Optional[str] = None,
    ) -> dict:
        """拦截率/请求量趋势。**空桶补零**，末点 `partial=true`（§2.2）。"""
        now = self._now()
        if range_ == "1h" and self.write_granularity != "1m":
            return await self._coarse_trend(range_, scene, level, now)

        gran = self._granularity(range_, granularity)
        points = trend_points(range_, now)
        src = self._source(scene, level)
        series = await self.repo.series(
            bucket_type=src.bucket_type, bucket_key=src.bucket_key, granularity=gran,
            from_ts=points[0][0], to_ts=points[-1][0], fields=BASE_FIELDS,
        )

        out: list[dict] = []
        for bucket_ts, partial in points:
            fields = series.get(bucket_ts) or {}
            counts = _counts(fields, src.decision)
            out.append({
                "bucket_ts": bucket_ts,
                # 缺桶补 0 而不是跳过该点：折线一旦断裂，看图的人会以为"这段时间没流量"
                "event_cnt": counts["event_cnt"],
                "pass_cnt": counts["pass_cnt"],
                "review_cnt": counts["review_cnt"],
                "reject_cnt": counts["reject_cnt"],
                "block_rate": ratio(counts["reject_cnt"], counts["event_cnt"]),
                "partial": partial,
            })
        return {
            "range": range_,
            "granularity": gran,
            "truncated": len(points) > MAX_TREND_POINTS,
            "points": out,
        }

    async def _coarse_trend(
        self, range_: str, scene: Optional[str], level: Optional[str], now: int,
    ) -> dict:
        """BR-11-16 的降级：基础粒度已是 `1h` 时，`range=1h` 拿不到 `1m` 级趋势。

        与其返回 60 个全 0 的点（会被读成"近一小时没有请求"），不如**明确**返回
        1 个点 + `truncated=true`：数据是真的粗，不是真的空。
        """
        gran = "1h"
        bucket_ts = align(now, gran)
        src = self._source(scene, level)
        series = await self.repo.series(
            bucket_type=src.bucket_type, bucket_key=src.bucket_key, granularity=gran,
            from_ts=bucket_ts, to_ts=bucket_ts, fields=BASE_FIELDS,
        )
        counts = _counts(series.get(bucket_ts) or {}, src.decision)
        return {
            "range": range_,
            "granularity": gran,
            "truncated": True,
            "points": [{
                "bucket_ts": bucket_ts,
                "event_cnt": counts["event_cnt"],
                "pass_cnt": counts["pass_cnt"],
                "review_cnt": counts["review_cnt"],
                "reject_cnt": counts["reject_cnt"],
                "block_rate": ratio(counts["reject_cnt"], counts["event_cnt"]),
                "partial": True,
            }],
        }

    # ---------------- §3.4 distribution ----------------
    async def distribution(
        self, *, range_: str, scene: Optional[str] = None, level: Optional[str] = None,
        granularity: Optional[str] = None, dim: str = "level",
    ) -> dict:
        """等级/场景分布。`total` 取 `items` 之和，保证 `ratio` 与图例自洽。"""
        now = self._now()
        gran = self._granularity(range_, granularity)
        from_ts, to_ts, _ = range_bounds(range_, now)

        if dim == "level":
            counts = await self._level_counts(gran, from_ts, to_ts, scene)
            keys = list(LEVEL_KEYS)
            name_of = name_of_level
        else:
            counts = await self._scene_counts(gran, from_ts, to_ts, scene, level)
            keys = list(counts)
            name_of = name_of_scene

        if level is not None and dim == "level":
            # 已经按等级筛选过，分布只剩该档有值：其余扇区补 0 而不是删掉，
            # 环形图的图例结构必须稳定（低/中/高 三个键恒定），否则筛选一次
            # 图例就少两项；顺序也保持不变，避免与 BR-11-22 的固定序冲突。
            # 注意只对 `dim=level` 生效：`dim=scene` 时 key 是场景名，
            # 这个归零循环会把所有扇区一起清零（`_scene_counts` 里已按分档计数取过值）
            counts = {k: (v if k == level else 0) for k, v in counts.items()}

        total = sum(int(counts.get(k) or 0) for k in keys)
        items = [
            {
                "key": k,
                "name": name_of(k),
                "cnt": int(counts.get(k) or 0),
                "ratio": ratio(counts.get(k), total),
            }
            for k in keys
        ]
        return {"dim": dim, "total": total, "items": items}

    async def _level_counts(
        self, gran: str, from_ts: int, to_ts: int, scene: Optional[str],
    ) -> dict[str, int]:
        """等级分布：无 `scene` 读 `level` 桶；有 `scene` 读 `scene` 桶的分档计数。"""
        if scene:
            fields = await self.repo.sum_range(
                bucket_type="scene", bucket_key=scene, granularity=gran,
                from_ts=from_ts, to_ts=to_ts, fields=COUNT_FIELDS,
            )
            return {
                "low": int(fields.get("pass_cnt") or 0),
                "medium": int(fields.get("review_cnt") or 0),
                "high": int(fields.get("reject_cnt") or 0),
            }
        rows = await self.repo.sum_by_key(
            bucket_type="level", granularity=gran, from_ts=from_ts, to_ts=to_ts,
            fields=("event_cnt",),
        )
        by_key = {str(r["bucket_key"]): int(r.get("event_cnt") or 0) for r in rows}
        return {k: by_key.get(k, 0) for k in LEVEL_KEYS}

    async def _scene_counts(
        self, gran: str, from_ts: int, to_ts: int,
        scene: Optional[str], level: Optional[str],
    ) -> dict[str, int]:
        """场景分布：无 `level` 取 `event_cnt`；有 `level` 取该档分档计数（BR-11-12）。

        `unknown` 桶（BR-11-06）只在**确实有数据**时出现在图例里：它代表"事件类型
        无法映射场景"的脏数据，恒占一个 0 值扇区只会让运营以为系统有个"未知场景"
        业务。`scene` 筛选时只回该场景一项。
        """
        fields = COUNT_FIELDS if level else ("event_cnt",)
        rows = await self.repo.sum_by_key(
            bucket_type="scene", granularity=gran, from_ts=from_ts, to_ts=to_ts,
            fields=fields,
        )
        decision = LEVEL_TO_DECISION.get(level) if level else None
        counted: dict[str, int] = {}
        for row in rows:
            key = str(row["bucket_key"])
            counted[key] = (
                int(row.get(f"{decision}_cnt") or 0) if decision
                else int(row.get("event_cnt") or 0)
            )
        if scene:
            # 显式筛选某场景时，图例只保留它（其余场景恒 0 的柱子没有信息量）
            return {scene: counted.get(scene, 0)}
        return {key: counted[key] for key in _scene_order(counted)}

    # ---------------- §3.5 rule-ranking ----------------
    async def rule_ranking(
        self, *, range_: str, scene: Optional[str] = None, level: Optional[str] = None,
        granularity: Optional[str] = None, top: int = 10,
    ) -> dict:
        """规则命中排行。

        `metric_field`（BR-11-12）：无 `level` 或 `level=high` → `hit_cnt`；
        `level=medium` → `review_hit_cnt`；`level=low` → `pass_hit_cnt`。
        由本模块决定并**回显**，02 不得自行切口径——否则图上柱子的含义会随
        页面实现变化。
        """
        now = self._now()
        gran = self._granularity(range_, granularity)
        from_ts, to_ts, _ = range_bounds(range_, now)
        metric_field = "hit_cnt" if level in (None, "high") else f"{LEVEL_TO_DECISION[level]}_hit_cnt"

        rows = await self.repo.sum_by_key(
            bucket_type="rule", granularity=gran, from_ts=from_ts, to_ts=to_ts,
            fields=HIT_FIELDS, sort_field=metric_field, descending=True, limit=top,
        )
        # 分母用**同范围的 global event_cnt**（§3.5）：它回答"这条规则在全部事件里
        # 占多大比例"。若换成分档后的事件数，不同 level 下的比例就不可比了。
        global_fields = await self.repo.sum_range(
            bucket_type="global", bucket_key="all", granularity=gran,
            from_ts=from_ts, to_ts=to_ts, fields=("event_cnt",),
        )
        denominator = int(global_fields.get("event_cnt") or 0)

        rules = await self.repo.rules_by_codes([str(r["bucket_key"]) for r in rows])
        items: list[dict] = []
        prev_value: Optional[int] = None
        rank = 0
        for index, row in enumerate(rows):
            code = str(row["bucket_key"])
            value = int(row.get(metric_field) or 0)
            # 并列同名次（1,2,2,4）：并列的柱子必须显示同一个名次，
            # 否则"第 3 名比第 2 名还多"会直接摧毁排行的可信度
            if value != prev_value:
                rank = index + 1
                prev_value = value
            info = rules.get(code) or {}
            items.append({
                "rank": rank,
                "rule_code": code,
                "rule_name": str(info.get("name") or code),
                "rule_status": str(info.get("status") or "deleted"),
                "hit_cnt": value,
                "hit_ratio": ratio(value, denominator),
                "hit_score_sum": int(row.get("hit_score_sum") or 0),
            })
        return {"range": range_, "metric_field": metric_field, "items": items}

    # ---------------- §3.6 throughput ----------------
    async def throughput(self, *, window: str = "5m") -> dict:
        """吞吐与延迟。

        **只统计已闭合的分钟桶**：当前这一分钟仍在累积，把它算进去会让同一个
        5 分钟窗口的 qps 随"什么时候点的刷新"来回跳；`qps_1m` 取"最近一个已闭合
        的 `1m` 桶"（§3.6），与窗口统计保持同一口径。
        """
        now = self._now()
        window_ms = WINDOW_MS[window]
        minutes = window_ms // GRANULARITY_MS["1m"]
        # 右开边界的写法：window_end 恰是"当前这个未闭合分钟的起点"
        window_end = align(now, "1m")
        last_closed = window_end - GRANULARITY_MS["1m"]
        window_start = last_closed - (minutes - 1) * GRANULARITY_MS["1m"]

        window_fields = await self.repo.sum_range(
            bucket_type="global", bucket_key="all", granularity="1m",
            from_ts=window_start, to_ts=last_closed, fields=("event_cnt",),
        )
        event_cnt = int(window_fields.get("event_cnt") or 0)

        closed = await self.repo.get_bucket("global", "all", "1m", last_closed)
        closed_cnt = int(((closed or {}).get("metrics") or {}).get("event_cnt") or 0)

        hist = await self.repo.elapsed_hist(
            granularity="1m", from_ts=window_start, to_ts=last_closed,
        )
        # 直方图无样本 -> null（BR-11-13 / MET-5003）；有样本才给插值结果
        p95 = percentile({k: hist.get(k, 0) for k in HIST_KEYS})
        return {
            "window": window,
            # §3.6 明确"保留 1 位小数"（比 BR-11-09 的通用 4 位更具体，故以其为准）
            "qps_1m": round_half_up(closed_cnt / 60.0, 1),
            "qps_window": round_half_up(event_cnt / (window_ms / 1000.0), 4),
            "p95_elapsed_ms": p95,
            "elapsed_hist": {k: int(hist.get(k) or 0) for k in HIST_KEYS},
            "event_cnt": event_cnt,
            "window_start": window_start,
            "window_end": window_end,
        }


def _scene_order(known: dict[str, int]) -> list[str]:
    """场景分布的固定顺序：业务场景按 `SCENE_KEYS`，`unknown` 永远排最后。

    BR-11-22 要求"分布按 `key` 固定序"——顺序由服务端定，前端不得依赖 Mongo
    的返回顺序（`$group` 的输出顺序没有保证，图例会自己换位）。`unknown`
    （BR-11-06 的脏数据桶）只在确实有数据时出现在图例里：恒占一个 0 值扇区
    只会让人以为系统真有个"未知场景"业务。
    """
    order = [k for k in SCENE_KEYS if k in known]
    order += [k for k in known if k not in order and k != "unknown"]
    if known.get("unknown"):
        order.append("unknown")
    return order
