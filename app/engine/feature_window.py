# -*- coding: utf-8 -*-
"""滑动时间窗口的内存结构（模块 04 BR-04-01 ~ 05 / 12 ~ 15）——**纯逻辑、可注入时钟**。

## 为什么窗口在进程内存里（AD-05 / G-02）

演示量级（每分钟几十到几百事件）下，把最近 24 小时的事件放进进程内的按维度队列，
比引入 Redis 少一整套运维负担。`app/protocols.py` 的 `FeatureStore` 协议把这个
决定封装掉了：将来要换 Redis，只需换实现，模块 04/05 的代码不动。
代价必须写清楚：**多进程部署时各进程的窗口互不可见**，因此本项目当前只支持单进程。

## 为什么当前事件必须计入窗口（BR-04-02）

风控要拦的是"这一次"。若把本次事件排除在窗口外，"近 1 小时同设备下单次数"
在首发订单上恒为 0——那会把一次明显的聚集行为判成"从未发生"。
因此链路固定为 **先 `ingest` 再 `compute`**（§3.1），
`V-04-03` 断言首发 `coupon_receive` 后 `coupon_cnt_1h == 1`（而不是 0）。

## 为什么按 `ts` 有序插入而不是简单 append（BR-04-04）

时间基准是**事件时间**而不是服务器接收时间，而事件流的 `ts` 可能乱序
（模拟器回放、上游时钟漂移、网络重投）。若只在尾部追加，一条 `ts` 偏小的
事件就会落在窗口尾部，`[ts-60min, ts]` 的区间裁剪会把它当成"最新的"，从而
把真正的最新事件挤出窗口——窗口语义被静默破坏，且只在乱序时出现，极难复现。
因此这里用 `bisect` 按 `ts` 插入，并**计数告警**（`FEA-5004`），
让"发生了乱序"这件事在日志里可见，而不是被无声地纠正掉。

## 为什么容量上限必须留痕（BR-04-12 / FEA-5003）

单维度队列上限 10000 条（G-12），超限丢**最旧**。丢弃必须在快照上留下
`truncated` 标记：被丢掉的是最早的数据，其特征值只会**偏低**，而"偏低"
在风控里等于"看起来更正常"。若静默截断，人工复核会看到一个平静的窗口，
从而把一次攻击当成正常流量——这正是 §5 要求「必须提示，不能静默」的原因。

## `sweep` 为什么是**分片**的（BR-04-14）

`BR-04-14` 要求 sweep 非阻塞。全量扫描在维度键很多时（每个用户/设备/IP/地址
一个键）会变成一次长时间同步遍历，把决策链路堵在事件循环后面。因此这里
每轮只扫描**固定数量**的键（`SWEEP_KEYS_PER_ROUND`），用游标在键列表上轮转：
每轮的耗时上界可预期，所有键仍会在若干轮内被扫到。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.constants import FEATURE_WINDOW_LONG_MIN, FEATURE_WINDOW_SHORT_MIN
from app.utils.timeutil import now_ms

# ============================================================
# 契约常量
# ============================================================
#: 单个「维度 × 实体」队列的条目上限（G-12 / BR-04-12）
QUEUE_CAPACITY = 10_000

#: 每轮 sweep 最多扫描的维度键数量（BR-04-14 的非阻塞上界）
SWEEP_KEYS_PER_ROUND = 500

#: 窗口内单个条目的固定开销估算（元组 + 列表槽位 + 字典条目等），
#: 用于 `/health` 的内存估算（BR-04-15）。**是估算**：精确值需要
#: `sys.getsizeof` 递归遍历，那本身会成为一次 O(n) 的耗时操作。
ENTRY_OVERHEAD_BYTES = 96

#: 单进程窗口内存告警线（BR-04-15 举例 500MB）
MEMORY_WARN_BYTES = 500 * 1024 * 1024

#: 事件里可用的四个实体维度：维度名 -> 事件字段名
DIMENSION_FIELDS: dict[str, str] = {
    "user": "user_id",
    "device": "device_id",
    "ip": "ip",
    "address": "address_id",
}

#: 窗口配置的版本号（随快照落库，BR-04-05：历史快照可用当时配置复算）
WINDOW_CONFIG_VERSION = "w1"
WINDOW_AGG_MODE = "in_memory_sliding"


# ============================================================
# 条目与队列
# ============================================================
#: 窗口条目 = `(event_id, ts, event_type, pay_ok)`。
#:
#: 为什么要多带一个 `pay_ok`：`pay_fail_cnt_24h` 的口径是"近 24h `order_pay`
#: **且失败**的事件数"，而 `order_pay` 的成败落在 `scene_extra.success`。
#: 若窗口条目不含它，历史失败笔数就**不可知**——那时只有两条路：要么把
#: "全部支付"当成失败（虚高，把正常用户判成盗卡试探），要么返回 0
#: （伪造"从未失败"，BR-04-09 明令禁止）。两者都比多存一个布尔值糟得多。
#: 其余事件类型该位恒为 `True`（对它们而言"没有失败语义"）。
ENTRY_PAY_OK_INDEX = 3


def make_entry(event: dict) -> tuple[str, int, str, bool]:
    """把事件压成窗口条目 `(event_id, ts, event_type, pay_ok)`。

    只留这四样是刻意的：窗口只需要"谁、什么时候、做了什么、支付是否成功"。
    多留字段（金额、场景扩展）会让内存成倍膨胀，而本模块的口径里没有特征用到
    它们——需要用金额的特征应由提出者同步更新 E02 与 `/features/meta`（BR-04-06）。
    """
    event_type = str(event.get("event_type") or "")
    pay_ok = True
    if event_type == "order_pay":
        scene = event.get("scene_extra")
        success = scene.get("success") if isinstance(scene, dict) else None
        # `success` 缺失时按**成功**处理：03 的校验已保证 `order_pay.success` 存在
        # 且为布尔（`EVT-4005`），缺失只可能出现在绕过网关的内部调用上。
        # 把"结构不明"计成失败会让正常支付被判成失败，方向更危险。
        pay_ok = True if success is None else bool(success)
    return (
        str(event.get("_id") or event.get("event_id") or ""),
        int(event.get("ts") or 0),
        event_type,
        pay_ok,
    )


def count_entries(
    entries: Iterable[tuple[str, int, str, bool]],
    event_type: Optional[str],
    start_ts: int,
    end_ts: int,
    *,
    pay_ok: Optional[bool] = None,
) -> int:
    """统计 `ts ∈ [start_ts, end_ts]`（**闭区间**）且类型/支付结果匹配的条目数。

    `BR-04-03` 写的是 `[now - window, now)` 且含 `now` 这一笔（由 BR-04-02 的
    "先 ingest 后 compute"保证），合起来就是闭区间。实现上用**线性扫描**而不是
    二分定位：队列已按 `ts` 升序，扫到 `ts > end_ts` 即可提前退出，窗口内条目
    通常不多，比"两次二分 + 切片"更快也更好读。
    """
    hit = 0
    for entry in entries:
        ts = entry[1]
        if ts > end_ts:
            break
        if ts < start_ts:
            continue
        if event_type is not None and entry[2] != event_type:
            continue
        if pay_ok is not None and entry[ENTRY_PAY_OK_INDEX] is not pay_ok:
            continue
        hit += 1
    return hit


@dataclass
class WindowQueue:
    """一个「维度 × 实体」的事件队列，按 `ts` 升序。

    `truncated` 一旦置位就**不再复位**（sweep 也不复位）：容量溢出是"这个窗口
    历史上丢过数据"的事实，若随时间的推移被自动清除，快照上的黄条提示
    （FEA-5003）就会在数据仍有偏差时消失。
    """

    entries: list[tuple[str, int, str, bool]] = field(default_factory=list)
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.entries)

    def add(
        self, entry: tuple[str, int, str, bool], capacity: int = QUEUE_CAPACITY
    ) -> tuple[bool, bool]:
        """插入一条（按 `ts` 有序）。返回 `(是否乱序, 是否触发截断)`。

        相同 `(event_id, ts)` 视为**同一条事件**，直接跳过不重复插入。
        这堵的是"同一事件被并发 `ingest` 两次"：`FeatureWindow` 的
        `seen` 集合是非原子检查，跨 `await` 的两个协程可能同时通过检查。
        重复插入会让计数虚高（比丢计数更危险：会把正常用户判成高频）。

        `capacity` 由调用方传入（模块 13 的「内存窗口容量上限」运行参数，
        BR-13-06）。默认值仍是模块常量，因此"没有配置过"的行为一字不变；
        之所以做成参数而不是直接读 `self.capacity`：`WindowQueue` 是一个
        **纯数据结构**，它不该知道"配置从哪来"。
        """
        _, ts, _, _ = entry
        ordered = not self.entries or ts >= self.entries[-1][1]
        duplicate = False
        if ordered:
            if self.entries and self.entries[-1] == entry:
                duplicate = True
            else:
                self.entries.append(entry)
        else:
            # 只在乱序分支做 O(log n) 定位 + O(n) 插入：正常流量（时间递增）
            # 走 append，代价仍是 O(1)
            keys = [e[1] for e in self.entries]
            pos = bisect.bisect_right(keys, ts)
            if pos > 0 and self.entries[pos - 1] == entry:
                duplicate = True
            elif pos < len(self.entries) and self.entries[pos] == entry:
                duplicate = True
            else:
                self.entries.insert(pos, entry)

        if duplicate:
            return (not ordered), False

        truncated = False
        limit = max(1, int(capacity))
        if len(self.entries) > limit:
            # 丢最旧：被丢的是最早的数据，特征值只会偏低（见模块 docstring）
            overflow = len(self.entries) - limit
            del self.entries[:overflow]
            self.truncated = True
            truncated = True
        return (not ordered), truncated

    def count(self, event_type: Optional[str], start_ts: int, end_ts: int) -> int:
        """按闭区间统计（实现见模块级 `count_entries`，那里有区间语义的说明）。"""
        return count_entries(self.entries, event_type, start_ts, end_ts)


@dataclass
class WindowView:
    """`plan()` 产出的**只读窗口视图**，交给纯函数计算特征。

    为什么不在纯函数里直接读 `FeatureWindow`：把"取哪一段数据"与"怎么算"
    分开之后，`feature_compute` 可以在**没有任何窗口对象**的情况下被单测
    （构造一个 `WindowView` 即可），而"取哪一段"这件事只有一处实现。
    """

    #: 事件时间（毫秒）：窗口的**右端点**，也是时效类特征（年龄）的基准点
    event_ts: int
    #: 当前事件在四个维度上的实体取值（缺该维度时为 None）
    identities: dict[str, Optional[str]]
    #: `(维度, 实体ID) -> 该实体在 [event_ts - long_window, event_ts] 内的条目`
    entries: dict[tuple[str, str], tuple[tuple[str, int, str, bool], ...]]
    #: 本次计算用到的窗口配置（原样写进快照，BR-04-05）
    window_config: dict[str, Any]

    def __post_init__(self) -> None:
        # 短/长窗口时长由**配置**决定而不是读全局常量：`window_config` 随快照落库
        # 是为了"用当时的配置复算"，若计算时用的是全局常量，复算就对不上了
        self.short_window_ms = int(self.window_config["short_window_min"]) * 60_000
        self.long_window_ms = int(self.window_config["long_window_min"]) * 60_000

    def count(
        self,
        dimension: str,
        event_type: Optional[str],
        window_ms: int,
        entity_id: Optional[str] = None,
        *,
        pay_ok: Optional[bool] = None,
    ) -> int:
        """统计某维度（可指定实体）在 `[event_ts - window_ms, event_ts]` 内的条目数。"""
        if window_ms <= 0:
            return 0
        target = self.identities.get(dimension) if entity_id is None else entity_id
        if not target:
            return 0
        entries = self.entries.get((dimension, str(target)))
        if not entries:
            return 0
        return count_entries(
            entries, event_type, self.event_ts - window_ms, self.event_ts, pay_ok=pay_ok
        )

    def count_short(self, dimension: str, event_type: str) -> int:
        """短窗（60 分钟）计数。"""
        return self.count(dimension, event_type, self.short_window_ms)

    def count_long(
        self, dimension: str, event_type: Optional[str], *, pay_ok: Optional[bool] = None,
    ) -> int:
        """长窗（1440 分钟）计数。`pay_ok=False` 用于"支付失败笔数"口径。"""
        return self.count(dimension, event_type, self.long_window_ms, pay_ok=pay_ok)

    def first_ts(self, dimension: str) -> Optional[int]:
        """该维度实体在窗口内**最早**出现的 `ts`（算 `device_age_hours` 用）。"""
        target = self.identities.get(dimension)
        if not target:
            return None
        entries = self.entries.get((dimension, str(target)))
        if not entries:
            return None
        return entries[0][1]


# ============================================================
# 窗口本体
# ============================================================
class FeatureWindow:
    """四维度 × 事件类型的进程内滑动窗口（模块 04 的唯一状态载体）。

    **幂等**：同一 `event_id` 重复 `ingest` 不重复计数（§3.1）。
    去重结合两处：`seen` 里的 ID 集合（跨队列）与 `WindowQueue.add` 的
    同条目判定（队列内）。
    """

    def __init__(
        self,
        *,
        short_window_min: int = FEATURE_WINDOW_SHORT_MIN,
        long_window_min: int = FEATURE_WINDOW_LONG_MIN,
        capacity: int = QUEUE_CAPACITY,
    ) -> None:
        self.short_window_min = int(short_window_min)
        self.long_window_min = int(long_window_min)
        self.capacity = int(capacity)
        # 窗口配置版本（BR-13-08：运行参数变更后 +1，并**随快照留存**，
        # 保证"当时的参数"可追溯）。默认值就是模块常量 `w1`——即"从未保存过
        # 运行参数"的状态；模块 13 保存参数时通过 `apply_config(config_version=...)`
        # 把它推进到 `w2`/`w3`…，因此快照里的版本与 `/system/config` 的
        # `config_version` 始终是同一个字符串（两处各写一份必然漂移）。
        self.window_config_version: str = WINDOW_CONFIG_VERSION
        self._queues: dict[str, dict[str, WindowQueue]] = {
            dim: {} for dim in DIMENSION_FIELDS
        }
        # 去重集合：`event_id -> ts`。存时间而不是只存 ID，是为了让 sweep 能按
        # 时间直接淘汰（见 `_prune_seen`），否则要么永不清理、要么每轮全量扫队列。
        self._seen: dict[str, int] = {}
        self._sweep_cursor = 0
        self._last_sweep_at: Optional[int] = None
        # 计数器：供 /health 与 stats() 展示，也是 FEA-5004 告警的依据
        self.out_of_order_cnt = 0
        self.truncated_cnt = 0
        self.duplicate_cnt = 0
        self.dropped_cnt = 0

    # ---------------- 配置 ----------------
    def window_config(self) -> dict[str, Any]:
        """窗口配置（随快照落库，BR-04-05）。

        `config_version` 取**实例上的当前版本**（模块 13 的运行参数版本，
        BR-13-08）：这样"某份快照是按哪一版参数算的"从快照本身就能读到，
        而不必去翻审计或猜时间。字段集合与 E02 逐字一致（4 项，不再增减）。
        """
        return {
            "short_window_min": self.short_window_min,
            "long_window_min": self.long_window_min,
            "agg_mode": WINDOW_AGG_MODE,
            "config_version": self.window_config_version,
        }

    def memory_warned(self) -> bool:
        """内存估算是否越过告警线（BR-04-15）。"""
        return self.estimated_bytes() >= MEMORY_WARN_BYTES

    # ---------------- 写入 ----------------
    async def ingest(self, event: dict) -> bool:
        """把事件写入各维度窗口。返回 `False` 表示是重复事件（未计数）。

        只写事件**确实带有**的维度：`order_pay` 没有 `device_id`，写一个
        `device=None` 的桶会把"不知道设备"与"来自空设备"混为一谈，
        而两者在聚集度特征上的含义完全不同。
        """
        entry = make_entry(event)
        event_id, ts, _kind, _pay_ok = entry
        if not event_id or ts <= 0:
            # 编号或时间戳缺失的事件不该进窗口：缺 ts 的条目会让区间裁剪失效，
            # 缺编号的条目无法幂等。抛异常会让整条决策链路降级，代价过大，
            # 因此这里选择"不入窗口 + 由调用方从 missing 上体现数据不足"。
            return False
        if event_id in self._seen:
            self.duplicate_cnt += 1
            return False
        self._seen[event_id] = ts

        for dimension, field in DIMENSION_FIELDS.items():
            entity_id = event.get(field)
            if entity_id is None or str(entity_id).strip() == "":
                continue
            key = str(entity_id).strip()
            queue = self._queues[dimension].get(key)
            if queue is None:
                queue = WindowQueue()
                self._queues[dimension][key] = queue
            out_of_order, truncated = queue.add(entry, self.capacity)
            if out_of_order:
                self.out_of_order_cnt += 1
            if truncated:
                self.truncated_cnt += 1
                self.dropped_cnt += 1
        return True

    # ---------------- 读取 ----------------
    def plan(self, event: dict) -> WindowView:
        """取该事件在**长窗口**内的所有相关条目（BR-04-05 的复算依据）。

        只取当前事件涉及的四类实体：算 `coupon_cnt_1h` 不需要别人的设备队列。
        长窗口作为上界是保守选择——短窗口的区间裁剪在计算侧按 `window_config`
        再做一次，因此这里多取一些不影响正确性，却保证了"任何窗口配置都能算"。
        """
        ts = int(event.get("ts") or 0)
        config = self.window_config()
        long_ms = int(config["long_window_min"]) * 60_000
        start = ts - long_ms
        identities: dict[str, Optional[str]] = {}
        entries: dict[tuple[str, str], tuple[tuple[str, int, str, bool], ...]] = {}
        for dimension, field in DIMENSION_FIELDS.items():
            raw = event.get(field)
            key = str(raw).strip() if raw is not None else ""
            identities[dimension] = key or None
            if not key:
                continue
            queue = self._queues[dimension].get(key)
            if queue is None:
                continue
            # 闭区间 [start, ts]：含当前这一笔（BR-04-02/03）
            kept = tuple(e for e in queue.entries if start <= e[1] <= ts)
            if kept:
                entries[(dimension, key)] = kept
        return WindowView(
            event_ts=ts, identities=identities, entries=entries, window_config=config,
        )

    def truncated_dimensions(self, event: dict) -> list[str]:
        """该事件涉及的维度里哪些队列发生过截断（FEA-5003 的提示依据）。

        只看**本次涉及的实体**：别的实体被截断与本事件的特征值无关，
        把它算进来会让每一次快照都带黄条，提示随即失去意义。
        """
        out: list[str] = []
        for dimension, field in DIMENSION_FIELDS.items():
            raw = event.get(field)
            key = str(raw).strip() if raw is not None else ""
            if not key:
                continue
            queue = self._queues[dimension].get(key)
            if queue is not None and queue.truncated:
                out.append(dimension)
        return out

    def first_seen(self, dimension: str, entity_id: str) -> Optional[int]:
        """该维度实体在窗口内最早出现的 `ts`（不限于某个事件的窗口上界）。

        算 `device_age_hours` 用。**必须说明这是窗口内的近似**：
        E11 的 `first_seen_at` 是长期累计值（归模块 09），本模块在它可用时
        优先用它；只有在画像不可用时才回落到窗口内的最早时间，
        并在报告里登记为近似——把近似值伪装成精确值会让"新设备"的判定失真。
        """
        queue = self._queues.get(dimension, {}).get(str(entity_id))
        if not queue or not queue.entries:
            return None
        return queue.entries[0][1]

    # ---------------- 清理 ----------------
    async def sweep(self, now: Optional[int] = None) -> int:
        """丢弃超出长窗的条目并清理空键（BR-04-13）。返回丢弃条数。

        **分片执行**（BR-04-14）：每轮只扫描 `SWEEP_KEYS_PER_ROUND` 个键，
        游标在维度键上轮转。这样单轮耗时有上界，不会长时间占住事件循环。
        代价是"某个键最多可能等几轮才被扫到"——清理晚一轮的后果只是
        多占一点内存，而阻塞决策链路的后果是请求超时降级，两者不对等。
        """
        moment = now_ms() if now is None else int(now)
        cutoff = moment - self.long_window_min * 60_000
        dropped = 0

        # 所有维度键的快照（`list()` 复制一份，避免清理时改到正在遍历的 dict）
        keys: list[tuple[str, str]] = [
            (dim, key) for dim, bucket in self._queues.items() for key in bucket
        ]
        total = len(keys)
        if total:
            start = self._sweep_cursor % total
            # 每轮最多处理的键数：既受固定上界约束，也不超过键总数
            take = min(SWEEP_KEYS_PER_ROUND, total)
            for offset in range(take):
                dimension, key = keys[(start + offset) % total]
                queue = self._queues.get(dimension, {}).get(key)
                if queue is None:  # 本轮之前已被别的路径清掉
                    continue
                keep = [e for e in queue.entries if e[1] >= cutoff]
                if len(keep) != len(queue.entries):
                    dropped += len(queue.entries) - len(keep)
                    queue.entries = keep
                # 容量上限的**第二道**落地（BR-13-06「超限部分在下次 sweep 被裁剪」）：
                # 保存运行参数时 `trim_to_capacity()` 已经裁过一次，这里再裁一次是
                # 为了"容量被调小之后，就算保存那一刻的裁剪因为任何原因没跑到，
                # sweep 也一定会把它收回来"——两处都做才是"立即生效 + 下次 sweep 收敛"。
                if len(queue.entries) > self.capacity:
                    dropped += len(queue.entries) - self.capacity
                    del queue.entries[: len(queue.entries) - self.capacity]
                    queue.truncated = True
                if not queue.entries:
                    # 空键必须移除：不清的话 24 小时后 `_queues` 里会留下
                    # 几十万个空列表，内存不降反升（BR-04-13 的字面要求）
                    del self._queues[dimension][key]
            self._sweep_cursor = (start + take) % total
        else:
            self._sweep_cursor = 0

        self._prune_seen(cutoff)
        self._last_sweep_at = moment
        return dropped

    def _prune_seen(self, cutoff: int) -> None:
        """回收去重集合里"已经不可能再出现在窗口内"的编号。

        `_seen` 若只增不减，24 小时后它会成为进程内最大的单个结构。
        这里按**编号自己的时间戳**清理：`ts < cutoff` 的编号不可能再出现在
        任何窗口区间里（窗口右端点不会早于 cutoff 之前的时刻），直接丢弃即可：

        - 不需要扫描队列求"最小 ts"，因此这一步与条目总数无关，
          只与"去重集合的大小"相关，符合 BR-04-14 对 sweep 耗时的要求；
        - 保留字典而不是集合，就是为了让上面这条判断成立（集合里没有时间）。

        边界与 `sweep` 的保留规则对齐（`ts >= cutoff` 保留）：
        两边若差一个等号，就会留下一个"队列里已无该条目、但编号还在集合里"
        的孤儿条目——它不影响正确性，但会让去重集合比实际需要的略大。
        """
        if not self._seen:
            return
        alive = {eid: ts for eid, ts in self._seen.items() if ts >= cutoff}
        self._seen = alive

    # ---------------- 统计 ----------------
    def queue_lengths(self) -> dict[str, int]:
        """各维度队列长度（键数量，用于 `/health` 展示"窗口有多大"）。"""
        return {dim: len(bucket) for dim, bucket in self._queues.items()}

    def total_entries(self) -> int:
        """窗口内条目总数（重复计在多个维度上：一条事件最多进 4 个队列）。"""
        return sum(
            len(queue) for bucket in self._queues.values() for queue in bucket.values()
        )

    def max_queue_length(self) -> int:
        """**单个**「维度 × 实体」队列的最大条目数（系统设置页的 `per_dimension_max`）。

        为什么页面需要它：容量上限是**按队列**算的（BR-04-12），而
        `total_entries` 是所有队列之和——只看着后者，会有"总量还很空、
        某个队列已经顶到上限"的错觉。这两个数放在一起才能判断容量调得合不合适。
        """
        return max(
            (len(queue) for bucket in self._queues.values() for queue in bucket.values()),
            default=0,
        )

    def estimated_bytes(self) -> int:
        """内存占用估算（BR-04-15）。

        逐条统计**字符串长度**（`event_id` 与 `event_type` 是主要变量部分）
        加上固定开销。刻意不调 `sys.getsizeof` 递归遍历：那本身是一次 O(n)
        的同步操作，放在决策链路或每 60s 的 sweep 里都是自伤。
        """
        total = 0
        for bucket in self._queues.values():
            for key, queue in bucket.items():
                total += len(key) + 64                      # 键字符串 + 字典条目
                for event_id, _ts, kind, _pay_ok in queue.entries:
                    total += ENTRY_OVERHEAD_BYTES + len(event_id) + len(kind)
        return total

    # ---------------- 运行参数热更新（模块 13 / BR-13-05、BR-13-06） ----------------
    def apply_config(
        self,
        *,
        short_window_min: Optional[int] = None,
        long_window_min: Optional[int] = None,
        capacity: Optional[int] = None,
        config_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """热更新窗口配置，返回 `{changed, dropped_entries}`（**不重启**，BR-13-05）。

        ## 为什么窗口参数可以在下一个事件生效

        `FeatureWindow` 是**进程内**状态（AD-05/G-02），而"取哪一段"这件事只有
        两处读它：`plan()` 用 `window_config()` 里的长短窗时长裁剪区间、
        `sweep()` 用 `long_window_min` 算清理截止。两者都在**调用时**读实例属性，
        因此改完属性，下一个事件的快照 `window_config` 就是新值
        （V-13-05 断言的正是这一点）。已有的窗口数据**不重算**：下次 sweep
        按新窗口裁剪（BR-13-05 的末句）。

        `changed` 里的名字是**运行参数名**（`short_window_min` /
        `long_window_min` / `window_capacity` / `config_version`），不是实例属性名
        ——调用方要拿它直接拼接口的 `applied` 数组。

        ## 容量调小为什么要当场裁

        BR-13-06 的原文是"超限部分在下次 sweep 被裁剪"，而 sweep 是每 60s 一轮
        （`feature_sweep_task`）。把容量从 100000 调到 1000 之后，如果只等 sweep，
        这 60 秒里内存仍然按旧上限占着——而调小容量的动机通常**正是内存吃紧**。
        因此这里立刻裁一次并返回 `dropped_entries` 供保存接口向用户提示
        「可能丢弃历史窗口数据」（Spec §2.1 的说明栏要求提示）。
        """
        changed: list[str] = []
        if short_window_min is not None and int(short_window_min) != self.short_window_min:
            self.short_window_min = int(short_window_min)
            changed.append("short_window_min")
        if long_window_min is not None and int(long_window_min) != self.long_window_min:
            self.long_window_min = int(long_window_min)
            changed.append("long_window_min")
        dropped = 0
        if capacity is not None and int(capacity) != self.capacity:
            self.capacity = int(capacity)
            # 报告的名字用**运行参数名**（`window_capacity`）而不是属性名
            # （`capacity`）：调用方（模块 13 的 config_service）要拿它去拼
            # `applied` 数组，名字对不上就会出现"保存成功、但该参数既不在
            # applied 也不在 requires_restart"——那正是 BR-13-02 禁止的静默保存
            changed.append("window_capacity")
            dropped = self.trim_to_capacity()
        if config_version is not None and str(config_version) != self.window_config_version:
            self.window_config_version = str(config_version)
            changed.append("config_version")
        return {"changed": changed, "dropped_entries": dropped}

    def trim_to_capacity(self) -> int:
        """把所有维度队列裁剪到 `self.capacity`，返回丢弃条数（丢最旧）。

        与 `WindowQueue.add` 的溢出处置同向：丢的是**最早**的数据，特征值只会
        偏低；被裁过的队列置 `truncated=True` 留痕（FEA-5003：不能静默截断）。
        """
        dropped = 0
        limit = max(1, int(self.capacity))
        for bucket in self._queues.values():
            for queue in bucket.values():
                if len(queue.entries) > limit:
                    dropped += len(queue.entries) - limit
                    del queue.entries[: len(queue.entries) - limit]
                    queue.truncated = True
        if dropped:
            self.dropped_cnt += 1
            self.truncated_cnt += 1
        return dropped

    def stats(self) -> dict[str, Any]:
        """窗口运行状态（§3.1 的 `stats`：供 `/health` 与系统设置页展示）。"""
        return {
            "dimensions": self.queue_lengths(),
            "total_entries": self.total_entries(),
            "distinct_keys": sum(len(b) for b in self._queues.values()),
            "estimated_bytes": self.estimated_bytes(),
            "memory_warn_bytes": MEMORY_WARN_BYTES,
            "memory_warned": self.memory_warned(),
            "last_sweep_at": self._last_sweep_at,
            "capacity": self.capacity,
            "seen_ids": len(self._seen),
            "out_of_order_cnt": self.out_of_order_cnt,
            "truncated_cnt": self.truncated_cnt,
            "dropped_cnt": self.dropped_cnt,
            "duplicate_cnt": self.duplicate_cnt,
            "window_config": self.window_config(),
        }

    def reset(self) -> None:
        """清空全部窗口状态（测试与配置变更后使用）。"""
        for dim in self._queues:
            self._queues[dim] = {}
        self._seen = {}
        self._sweep_cursor = 0
        self._last_sweep_at = None
        self.out_of_order_cnt = 0
        self.truncated_cnt = 0
        self.duplicate_cnt = 0
        self.dropped_cnt = 0


def build_view(
    *,
    event_ts: int,
    identities: dict[str, Optional[str]],
    entries: Iterable[tuple[tuple[str, str], Iterable[tuple[str, int, str, bool]]]],
    window_config: dict[str, Any],
) -> WindowView:
    """便捷构造 `WindowView`（纯计算侧的单测与复算脚本用，不必造一个 FeatureWindow）。"""
    return WindowView(
        event_ts=int(event_ts),
        identities=dict(identities),
        entries={key: tuple(value) for key, value in entries},
        window_config=dict(window_config),
    )


__all__ = [
    "DIMENSION_FIELDS",
    "ENTRY_OVERHEAD_BYTES",
    "ENTRY_PAY_OK_INDEX",
    "FeatureWindow",
    "MEMORY_WARN_BYTES",
    "QUEUE_CAPACITY",
    "SWEEP_KEYS_PER_ROUND",
    "WINDOW_AGG_MODE",
    "WINDOW_CONFIG_VERSION",
    "WindowQueue",
    "WindowView",
    "build_view",
    "count_entries",
    "make_entry",
]
