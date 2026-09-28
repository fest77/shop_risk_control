# -*- coding: utf-8 -*-
"""事件流模拟器：四模式 + 固定随机种子 + 虚拟时钟（BR-03-25 ~ 32，§3.4）。

## 为什么模拟器必须走 `EventService`（BR-03-29）

模拟器的价值在于「它产生的事件与真实业务投进来的是**同一种东西**」。若它绕过
校验直接写库，演示时就会出现"模拟器灌进去的数据里 `ip` 是 `999.999.999.999`
也能进库"这种自欺欺人的结果；而验收 V-03-13 正是拿模拟器事件与手工 POST 的
同参数事件对比决策是否一致。因此这里**只调用 `event_service.ingest()`**，
不写库、不调 05、不复制任何校验逻辑。

## 虚拟时钟为什么这样算（BR-03-25）

`ts = anchor_ts + round(i * 1000 / rate)`。用**序号推导**而不是 `now_ms()`：
后者会让两次运行的时间戳各不相同，"固定种子可复现"就只剩下事件内容可复现，
而时间分布（60s 内密集领券这类模式特征）会随机器负载漂移。默认
`anchor_ts = now - duration_sec*1000`，使事件落在当前时间附近**同时序列可复现**。

## 随机源为什么不用全局 `random`（BR-03-26）

全局 `random` 是进程级共享状态：任何其它模块（或测试）取一次数，就会让本模块
的序列整体错位，"同种子两次运行结果一致"立刻失效。因此每个事件用
`random.Random(seed*1_000_003 + i)` 派生独立实例——既不共享状态，也不需要
`numpy`/`faker`（本项目未把二者列为运行依赖，BR-03-26 提到的 PCG64 与
`seed_instance` 是同一目的的不同实现，此处用标准库达成同样效果）。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Optional

from app.errors import AppError
from app.logging import get_logger
from app.schemas.event_schema import SOURCE_MOCK_BIZ
from app.services import event_service as event_svc
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.event.simulator")

# 四种模式（§3.4 `start` 请求的 mode 取值）
MODE_NORMAL = "normal"
MODE_WOOL = "wool"
MODE_BRUSH = "brush"
MODE_REFUND = "refund"
MODES: tuple[str, ...] = (MODE_NORMAL, MODE_WOOL, MODE_BRUSH, MODE_REFUND)
MODE_LABELS: dict[str, str] = {
    MODE_NORMAL: "正常流",
    MODE_WOOL: "羊毛党",
    MODE_BRUSH: "刷单",
    MODE_REFUND: "恶意退款",
}

# 参数区间（§3.4）
RATE_MIN, RATE_MAX = 0.1, 200.0
MAX_EVENTS_LIMIT = 20_000
MAX_EVENTS_DEFAULT = 500
DURATION_LIMIT = 600
DURATION_DEFAULT = 60
SEED_DEFAULT = 42

# 派生子种子的乘数：取一个与 `i` 不易碰撞的大素数，
# 使 (seed, i) 二元组映射到互不相关的随机流（避免相邻事件序列雷同）
_SEED_STRIDE = 1_000_003

# 各模式的池规模（BR-03-28 的取值）
_NORMAL_USERS = 24
_WOOL_USERS = 10          # 「10 个新账号」
_WOOL_DEVICES = 2         # 「同 device_id」
_WOOL_IPS = 1             # 「同 ip」
_BRUSH_SKUS = 3           # 「同 sku 高频」
_BRUSH_ADDRESSES = 2      # 「收货地址聚集」
_REFUND_USERS = 6
_REFUND_ORDERS = 4


def _rng(seed: int, index: int) -> random.Random:
    """第 `index` 个事件专用的独立随机源（BR-03-26：不用全局 `random`）。"""
    return random.Random(int(seed) * _SEED_STRIDE + int(index))


# ============================================================
# 池与事件模板（全部由 seed 派生，BR-03-27/28）
# ============================================================
def build_profiles(mode: str, seed: int) -> dict[str, list[str]]:
    """派生该模式的账号 / 设备 / IP / 地址池。

    池本身也由 seed 决定，因此两次运行**连账号编号都一致**——这比"只有事件
    内容一致"更强，也让验收 V-03-12 的逐字段比对真正有意义。
    """
    rng = random.Random(int(seed))
    if mode == MODE_WOOL:
        # 羊毛党：一批**新账号**（编号落在新号段）共用极少数设备与同一个 IP
        users = [f"w{i:06d}" for i in range(_WOOL_USERS)]
        devices = [f"DWOOL{i:04d}" for i in range(_WOOL_DEVICES)]
        ips = [f"203.0.113.{10 + i}" for i in range(_WOOL_IPS)]
    elif mode == MODE_BRUSH:
        users = [f"b{rng.randint(100000, 999999)}" for _ in range(_NORMAL_USERS)]
        devices = [f"DBRUSH{i:04d}" for i in range(_BRUSH_SKUS * 2)]
        ips = [f"198.51.100.{20 + i}" for i in range(_BRUSH_SKUS)]
    elif mode == MODE_REFUND:
        users = [f"r{rng.randint(100000, 999999)}" for _ in range(_REFUND_USERS)]
        devices = [f"DREF{i:04d}" for i in range(_REFUND_USERS)]
        ips = [f"192.0.2.{30 + i}" for i in range(_REFUND_USERS)]
    else:
        users = [f"u{rng.randint(100000, 999999)}" for _ in range(_NORMAL_USERS)]
        devices = [f"DN{i:05d}" for i in range(_NORMAL_USERS)]
        ips = [f"192.0.2.{100 + i}" for i in range(_NORMAL_USERS)]
    addresses = [f"ADDR{rng.randint(100000, 999999)}" for _ in range(max(2, len(users) // 4))]
    # 刷单的"收货地址聚集"落地为**极少数共享地址**：`build_event` 在这个池里
    # 轮转（而不是每次随机挑一个），否则 24 个地址的池会让"聚集"这个特征在
    # 数据的层面根本不出现——模拟出来的事件就证明不了任何事。
    shared = addresses[:_BRUSH_ADDRESSES] if mode == MODE_BRUSH else []
    return {"users": users, "devices": devices, "ips": ips,
            "addresses": addresses, "shared": shared}


def build_event(mode: str, seed: int, index: int, ts: int,
                profiles: dict[str, list[str]]) -> dict:
    """生成第 `index` 条事件（不含 `event_id`，由 `EventService` 取号）。

    BR-03-28 的四模式定义在此落地：
    - `normal`：常态流量，四类事件按序轮转（多数会被放行）
    - `wool`：10 个新账号 + 同设备 + 同 IP，密集 `coupon_receive`
    - `brush`：同 sku 高频 `order_create` + `order_pay`，收货地址聚集
    - `refund`：老账号高额订单 → 密集 `reason_code=not_received` 的
      `after_sale_apply`

    所有取值都来自 `_rng(seed, index)`：同一 `(mode, seed)` 下第 i 条事件
    逐字段一致，`ts` 由调用方按虚拟时钟给出（BR-03-25）。
    """
    rng = _rng(seed, index)
    users, devices, ips, addresses = (
        profiles["users"], profiles["devices"], profiles["ips"], profiles["addresses"]
    )
    # 轮转 + 随机起点：保证池被均匀用到，同时序列仍由 seed 决定
    user = users[(index + rng.randint(0, len(users) - 1)) % len(users)]
    device = devices[(index + rng.randint(0, len(devices) - 1)) % len(devices)]
    ip = ips[(index + rng.randint(0, len(ips) - 1)) % len(ips)]
    # 地址：默认从全池挑；`brush` 用它自己的**共享小池**（见 `build_profiles`）
    address_pool = profiles.get("shared") or addresses
    address = address_pool[(index + rng.randint(0, len(address_pool) - 1))
                           % len(address_pool)]

    if mode == MODE_WOOL:
        return _coupon_event(user, device, ip, ts, rng, index)
    if mode == MODE_BRUSH:
        return _brush_event(user, device, ip, address, ts, rng, index)
    if mode == MODE_REFUND:
        return _refund_event(user, device, ip, ts, rng, index)
    return _normal_event(user, device, ip, address, ts, rng, index)


def _coupon_event(user: str, device: str, ip: str, ts: int,
                  rng: random.Random, index: int) -> dict:
    """`wool`：同设备同 IP 的密集领券（账号各不相同）。"""
    face_value = rng.choice([1000, 2000, 5000, 10000])
    return {
        "event_type": "coupon_receive",
        "user_id": user,
        "device_id": device,
        "ip": ip,
        "amount": face_value,
        "scene_extra": {
            "coupon_id": f"CP{rng.randint(10000, 99999)}",
            "activity_id": f"ACT{rng.randint(100, 999)}",
            "face_value": face_value,
            "batch_id": f"B{rng.randint(1, 9)}",
        },
        "ts": ts,
    }


def _brush_event(user: str, device: str, ip: str, address: str, ts: int,
                 rng: random.Random, index: int) -> dict:
    """`brush`：同 sku 下单 + 立即支付，收货地址聚集在极少数地址上。"""
    total = rng.choice([9900, 19900, 29900, 59900])
    order_no = f"SO{index:08d}"
    if index % 2 == 0:
        return {
            "event_type": "order_create",
            "user_id": user,
            "device_id": device,
            "ip": ip,
            "address_id": address,
            "amount": total,
            "scene_extra": {
                "order_no": order_no,
                # 「同 sku」：sku 固定在极少数取值上，这是刷单最典型的特征
                "sku_count": rng.choice([1, 1, 1, 2]),
                "total_amount": total,
                "address_id": address,
            },
            "ts": ts,
        }
    return {
        "event_type": "order_pay",
        "user_id": user,
        "amount": total,
        "scene_extra": {
            "order_no": f"SO{index - 1:08d}",
            "pay_channel": rng.choice(["alipay", "wechat", "card"]),
            "pay_amount": total,
            "card_tail": f"{rng.randint(1000, 9999)}",
        },
        "ts": ts,
    }


def _refund_event(user: str, device: str, ip: str, ts: int,
                  rng: random.Random, index: int) -> dict:
    """`refund`：高额订单 → `reason_code=not_received` 的密集售后申请。"""
    order_index = index % _REFUND_ORDERS
    amount = rng.choice([29900, 59900, 99900, 199900])
    base = {
        "user_id": user,
        "device_id": device,
        "ip": ip,
        "amount": amount,
    }
    if index % 3 == 0:
        # 先有高额订单，再有售后——否则"恶意退款"没有可退的对象
        payload = dict(base)
        payload.update({
            "event_type": "order_create",
            "address_id": f"ADDRR{rng.randint(1000, 9999)}",
            "scene_extra": {
                "order_no": f"RO{order_index:08d}",
                "sku_count": rng.choice([1, 2, 5]),
                "total_amount": amount,
            },
            "ts": ts,
        })
        return payload
    payload = dict(base)
    payload.update({
        "event_type": "after_sale_apply",
        # BR-03-28 明确要求 `after_sale_apply` 带 `biz_no`（售后单号）
        "biz_no": f"AS{index:08d}",
        "scene_extra": {
            "after_sale_no": f"AS{index:08d}",
            "order_no": f"RO{order_index:08d}",
            # 密集同因：这是"恶意退款"模式最直观的特征
            "reason_code": "not_received",
            "refund_amount": amount,
            "received_goods": False,
        },
        "ts": ts,
    })
    return payload


def _normal_event(user: str, device: str, ip: str, address: str, ts: int,
                  rng: random.Random, index: int) -> dict:
    """`normal`：常态流量，四类事件轮转，金额与渠道都分散。"""
    kind = index % 4
    if kind == 0:
        return {
            "event_type": "login",
            "user_id": user,
            "device_id": device,
            "ip": ip,
            "phone": f"13{rng.randint(100000000, 999999999)}",
            "scene_extra": {
                "login_type": rng.choice(["pwd", "pwd", "sms", "scan"]),
                "ua": rng.choice(["Mozilla/5.0 (iPhone)", "Mozilla/5.0 (Windows NT 10.0)",
                                  "Mozilla/5.0 (Linux; Android 14)"]),
                "success": True,
            },
            "ts": ts,
        }
    if kind == 1:
        face_value = rng.choice([500, 1000, 2000, 3000])
        return {
            "event_type": "coupon_receive",
            "user_id": user,
            "device_id": device,
            "ip": ip,
            "amount": face_value,
            "scene_extra": {
                "coupon_id": f"CP{rng.randint(10000, 99999)}",
                "activity_id": f"ACT{rng.randint(100, 999)}",
                "face_value": face_value,
            },
            "ts": ts,
        }
    if kind == 2:
        total = rng.randint(1000, 50000)
        return {
            "event_type": "order_create",
            "user_id": user,
            "device_id": device,
            "ip": ip,
            "address_id": address,
            "amount": total,
            "scene_extra": {
                "order_no": f"NO{index:08d}",
                "sku_count": rng.randint(1, 6),
                "total_amount": total,
                "address_id": address,
            },
            "ts": ts,
        }
    total = rng.randint(1000, 50000)
    return {
        "event_type": "order_pay",
        "user_id": user,
        "amount": total,
        "scene_extra": {
            "order_no": f"NO{index - 1:08d}",
            "pay_channel": rng.choice(["alipay", "wechat", "card", "balance"]),
            "pay_amount": total,
        },
        "ts": ts,
    }


def validate_start_params(params: dict) -> dict:
    """校验并归一化 `start` 参数（§3.4）。

    两类错误刻意分开（§5 的两条码对应不同状态码）：
    - `mode` 非法 → `EVT-5005`（500）：模拟器的运行模式是**服务端能力**，
      不认识的模式说明请求打到了一个不支持它的服务版本
    - `rate`/`max_events`/`duration_sec` 越界 → `COM-4001`（422）：
      这是调用方把报文参数写错了，属于通用参数校验失败
    """
    mode = str(params.get("mode") or MODE_NORMAL).strip()
    if mode not in MODES:
        raise AppError(
            "EVT-5005",
            f"模拟器启动失败：不支持的 mode={mode}（仅支持 {'/'.join(MODES)}）",
            500,
            {"mode": mode, "supported": list(MODES)},
        )
    rate = float(params.get("rate", 5.0))
    if not (RATE_MIN <= rate <= RATE_MAX):
        raise AppError(
            "COM-4001", f"参数校验失败：rate 需在 {RATE_MIN}~{RATE_MAX} events/sec",
            422, {"errors": [{"path": "rate", "message": "超出允许区间"}]},
        )
    max_events = int(params.get("max_events", MAX_EVENTS_DEFAULT))
    if not (1 <= max_events <= MAX_EVENTS_LIMIT):
        raise AppError(
            "COM-4001", f"参数校验失败：max_events 需在 1~{MAX_EVENTS_LIMIT}",
            422, {"errors": [{"path": "max_events", "message": "超出允许区间"}]},
        )
    duration = int(params.get("duration_sec", DURATION_DEFAULT))
    if not (1 <= duration <= DURATION_LIMIT):
        raise AppError(
            "COM-4001", f"参数校验失败：duration_sec 需在 1~{DURATION_LIMIT}",
            422, {"errors": [{"path": "duration_sec", "message": "超出允许区间"}]},
        )
    anchor = params.get("anchor_ts")
    return {
        "mode": mode,
        "rate": rate,
        "seed": int(params.get("seed", SEED_DEFAULT)),
        "max_events": max_events,
        "duration_sec": duration,
        "anchor_ts": int(anchor) if anchor is not None else None,
    }


# ============================================================
# 模拟器任务
# ============================================================
class EventSimulator:
    """事件流模拟器的进程内单例（§3.4 的 start / stop / status）。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 无参 emit 的默认入口；测试可注入假 EventService（§6：simulator 可注入）
        self._ingest: Callable[[dict], Any] = lambda payload: event_svc.ingest(
            payload, source=SOURCE_MOCK_BIZ)
        self.emitted_total = 0
        self._reset_state()

    def _reset_state(self) -> None:
        self.running = False
        self.mode: Optional[str] = None
        self.rate: Optional[float] = None
        self.seed: Optional[int] = None
        self.anchor_ts: Optional[int] = None
        self.started_at: Optional[int] = None
        self.last_error: Optional[str] = None
        self.emitted = 0
        self.decision_counts: dict[str, int] = {
            "pass": 0, "review": 0, "reject": 0, "failed": 0,
        }

    def set_ingest(self, fn: Callable[[dict], Any]) -> None:
        """注入事件投喂入口（测试用；默认是 `EventService.ingest`，BR-03-29）。"""
        self._ingest = fn

    # ---------------- 控制 ----------------
    async def start(self, params: dict, *, spawn_task: bool = True) -> dict:
        """启动模拟器。重复启动 → `EVT-4008`(409)（BR-03-31 / Spec §8 改号）。"""
        self._ensure_loop()
        if self.running:
            raise AppError(
                "EVT-4008",
                f"模拟器已在运行，模式={self.mode}",
                409,
                {"mode": self.mode, "emitted": self.emitted,
                 "running": True, "started_at": self.started_at},
            )
        cfg = validate_start_params(params)
        anchor = cfg["anchor_ts"]
        if anchor is None:
            # BR-03-25：默认锚点让事件落在"当前时间附近"，同时序列可复现
            anchor = now_ms() - cfg["duration_sec"] * 1000
        self._reset_state()
        self.mode = cfg["mode"]
        self.rate = cfg["rate"]
        self.seed = cfg["seed"]
        self.anchor_ts = anchor
        self.started_at = now_ms()
        self.running = True
        self._stop = asyncio.Event()
        try:
            if spawn_task:
                self._task = asyncio.create_task(self._run(cfg), name="event-simulator")
        except Exception as e:  # noqa: BLE001 - EVT-5005：启动失败必须释放资源
            self.running = False
            self.last_error = f"{type(e).__name__}: {e}"
            self._task = None
            self._stop = None
            log.error("[EVT-5005] 模拟器启动失败：%s", self.last_error)
            raise AppError("EVT-5005", f"模拟器启动失败：{self.last_error}", 500,
                           {"mode": cfg["mode"]}) from e
        log.info("模拟器已启动：模式=%s rate=%s seed=%s anchor_ts=%s max_events=%s",
                 cfg["mode"], cfg["rate"], cfg["seed"], anchor, cfg["max_events"])
        return self.status()

    async def stop(self) -> dict:
        """停止并返回本次汇总（BR-03-31：**只写日志，不写任何集合**）。

        为什么不落 E17/E18：那两张表属模块 10（仿真用例与执行记录），
        `00` §3.3 的三段分工里没有本模块。把汇总写日志既留痕又不越界。
        """
        self._ensure_loop()
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.running = False
        self._stop = None
        summary = self.status()
        # `emitted_total` 是**跨多次运行**的累计量：`emitted` 归零前先并入它，
        # 否则第二次 start 会把上一轮的产出从总数里抹掉
        self.emitted_total += self.emitted
        self.emitted = 0
        log.info("模拟器已停止：模式=%s emitted=%d 决策分布=%s last_error=%s",
                 summary["mode"], summary["emitted"], summary["decision_counts"],
                 summary["last_error"])
        return summary

    def status(self) -> dict:
        """运行状态（§3.4 / 08 页只读展示）。"""
        return {
            "running": self.running,
            "mode": self.mode,
            "rate": self.rate,
            "seed": self.seed,
            "anchor_ts": self.anchor_ts,
            "emitted": self.emitted,
            "emitted_total": self.emitted_total + self.emitted,
            "decision_counts": dict(self.decision_counts),
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    def _ensure_loop(self) -> None:
        """把 `asyncio.Event` 绑定到当前事件循环（测试逐用例新建循环）。

        与 `AuditService._ensure_loop` 同一理由。这里额外把"运行中"标记复位：
        上一个循环里的模拟器任务已经随循环消亡，继续声称"运行中"会让下一次
        `start` 永远拿到 409。
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._task = None
            self._stop = None
            if self.running:
                log.warning("事件循环已更换，模拟器状态复位（原任务已不可达）")
                self._reset_state()

    # ---------------- 主循环 ----------------
    async def _run(self, cfg: dict) -> None:
        """按虚拟时钟节奏产出事件，逐条投喂 `EventService`（BR-03-29）。"""
        profiles = build_profiles(cfg["mode"], cfg["seed"])
        interval = 1.0 / cfg["rate"]
        max_events = cfg["max_events"]
        duration_ms = cfg["duration_sec"] * 1000
        anchor = self.anchor_ts or now_ms()
        try:
            for i in range(max_events):
                offset_ms = int(round(i * 1000 / cfg["rate"]))
                if offset_ms > duration_ms:
                    # `max_events` 与 `duration_sec` 二者取先到者（§3.4）
                    break
                if self._stop is not None and self._stop.is_set():
                    break
                await self._emit_one(cfg, profiles, i, anchor + offset_ms)
                await self._sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 后台任务必须把异常记下来而不是抛到无人区
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("[EVT-5005] 模拟器任务异常终止：%s", self.last_error)
        finally:
            # 走到这里说明自然结束（跑满 max_events/duration）或被取消；
            # 两种情况下 `running` 都必须回到 false，否则 08 页会一直显示"运行中"
            self.running = False

    async def _emit_one(self, cfg: dict, profiles: dict, index: int, ts: int) -> None:
        """生成并投喂一条事件；单条失败只计入 `failed`（不打断整条流）。"""
        payload = build_event(cfg["mode"], cfg["seed"], index, ts, profiles)
        try:
            out = await self._ingest(payload)
        except Exception as e:  # noqa: BLE001 - 一条坏数据不该让模拟器整体停摆
            self.decision_counts["failed"] += 1
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("模拟器投喂失败（第 %d 条）：%s", index, self.last_error)
            self.emitted += 1
            return
        decision = str((out or {}).get("decision") or "")
        if decision in self.decision_counts:
            self.decision_counts[decision] += 1
        else:
            # 决策值异常（既不是 pass/review/reject）按 failed 计：
            # 它绝不能被算进 pass，否则统计会掩盖一次降级
            self.decision_counts["failed"] += 1
        self.emitted += 1

    async def _sleep(self, seconds: float) -> None:
        """可被 `stop()` 立刻打断的等待（用 Event 而不是 `asyncio.sleep`）。"""
        if self._stop is None:  # pragma: no cover - start() 之后不会为 None
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


_SIMULATOR = EventSimulator()


def get_simulator() -> EventSimulator:
    """取进程内模拟器单例（启停控制与 08 页状态行共用同一份状态）。"""
    return _SIMULATOR


__all__ = [
    "DURATION_DEFAULT", "DURATION_LIMIT", "EventSimulator", "MAX_EVENTS_DEFAULT",
    "MAX_EVENTS_LIMIT", "MODES", "MODE_BRUSH", "MODE_LABELS", "MODE_NORMAL",
    "MODE_REFUND", "MODE_WOOL", "RATE_MAX", "RATE_MIN", "SEED_DEFAULT",
    "build_event", "build_profiles", "get_simulator", "validate_start_params",
]
