# -*- coding: utf-8 -*-
"""模块 03 测试的公共助手：样例载荷、假 04/05、进程内状态复位。

## 为什么要单独一个文件（一次真实的踩坑）

最初这些助手都放在 `tests/test_event_gateway.py` 里，由另一个测试文件
`from tests.test_event_gateway import ...` 复用。单独跑任何一个文件都通过，
**两个一起跑就报** `_pytest/fixtures.py` 的 `assert not self._finalizers`。

原因是那个文件里有一个 `autouse=True` 的 async 夹具：当一个测试模块导入另一个
**带 autouse 夹具的测试模块**时，pytest 的夹具缓存会跨模块复用同一个夹具实例，
而不同模块的终结器被注册到同一个缓存项上，触发上述断言（这是 pytest 夹具作用域
的用法问题，不是被测代码的问题）。

因此把"可被多个测试文件复用"的东西放进**非测试模块**：
`tests/event_testlib.py`（文件名不以 `test_` 开头，pytest 不会收集它），
每个测试文件各自声明自己的 autouse 夹具、只在夹具体内调用本模块的复位函数。
这样既没有跨模块夹具复用，也没有把助手复制两份。
"""
from __future__ import annotations

import asyncio

from app.utils.timeutil import date_key, now_ms


# ============================================================
# 进程内状态复位
# ============================================================
def component_reset() -> None:
    """把可替换组件复位成**真实默认装配**（04 / 05 / 09 都已落地）。

    **必须逐用例复位**：`configure_components()` 改的是进程内单例，某个用例
    装了"假 05"之后不还原，后面所有用例的 fail-closed 断言都会失效——表现为
    "用例顺序不同则结果不同"的幽灵缺陷。

    **为什么这里装的是真实的 04/05/09（而不是"不可用"占位）**：三个模块都已
    落地，`app/protocols.py` 的 `Components` 默认就指向它们的真实实现
    （见 `default_feature_provider` / `default_decision_provider` /
    `default_linked_user_count_provider`）。测试夹具必须与生产默认装配一致，
    否则"默认链路"这件事从来没有被测试覆盖过——04 上线前这里装占位还能
    自圆其说，04 上线后继续装占位就等于把"04 还没做"写进了验收。

    **为什么用各 `default_*()` 工厂显式重装同一批默认值，而不是干脆不写**：
    不写的话，`install_fakes()`（装了假 04 + 假 05 的用例）留下的**假实现会
    一路泄漏**到后面的用例，直到某个用例再改它为止。实测后果：单独跑
    `test_event_failclosed.py` 时默认链路走真实 04，而先跑
    `test_event_gateway.py` 再跑它时默认链路走的是泄漏的假 04——同一批断言
    给出两个结论，正是本函数要防的那类幽灵缺陷。显式重装默认值既"跟随真实
    默认装配"，又不依赖用例执行顺序。

    **05 落地后 03 的降级语义又变了一次**：默认链路不再停在 `stage=rule`
    （`UnavailableDecisionProvider` 已不是默认值），而是拿到 05 的**真实决策**。
    要覆盖"05 真的故障"（抛异常）或"05 被摘除"（占位实现），必须在**用例内部**
    显式安装 `ExplodingDecisionProvider` / `UnavailableDecisionProvider`。
    """
    from app.protocols import (
        MockBizAdapter,
        NullFeatureStore,
        NullModelEngine,
        configure_components,
        default_decision_provider,
        default_feature_provider,
        default_linked_user_count_provider,
    )

    configure_components(
        feature_store=NullFeatureStore(),
        biz_adapter=MockBizAdapter(),
        model_engine=NullModelEngine(),
        # 与 `Components` 的字段默认值同一个工厂：04 已落地即装真实的 FeatureService
        feature_provider=default_feature_provider(),
        # 05 已落地（决策 D45）：夹具必须跟随**真实默认装配**，装 05 的真实实现。
        # 若继续在这里塞 `UnavailableDecisionProvider`，就等于用一条过时的前提
        # 把"05 到底接上没有"这件事从测试里藏起来——那正是 D45 明确禁止的做法。
        decision_provider=default_decision_provider(),
        # 09 已落地（决策 D45）：同上，装 09 的真实实现。
        linked_user_count_provider=default_linked_user_count_provider(),
    )


def reset_runtime_state() -> None:
    """复位幂等缓存与模拟器的进程内状态（理由同 `component_reset`）。"""
    from app.core.event_simulator import get_simulator
    from app.services.idempotency import reset_store

    reset_store()
    sim = get_simulator()
    sim._task = None
    sim._stop = None
    sim.emitted_total = 0
    sim._reset_state()


# ============================================================
# 假组件（可精确统计"下游被调用了几次"）
# ============================================================
class FakeFeatureProvider:
    """最小可用特征提供者：返回固定快照并计数。"""

    available = True

    def __init__(self, delay: float = 0.02) -> None:
        self.calls = 0
        self.delay = delay

    async def compute(self, event: dict) -> dict:
        self.calls += 1
        # 让出控制权：这样并发的同编号请求才会真正重叠在"首领计算中"这个窗口里。
        # 若不 await，single-flight 的逻辑错误会被"其实没并发"掩盖。
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            "snapshot_id": f"SNP{self.calls:012d}",
            "features": {"event_type": event.get("event_type")},
            "missing_features": [],
            "window_config": {"short_min": 60, "long_min": 1440},
            "compute_ms": 1,
            "degrade_suggested": False,
        }


FIXED_DECISION: dict = {
    # `list_hit` 是**对象**（Spec 05 §3.1 + 契约裁定）：任何情况下都不是 bool。
    # 这个假 05 的返回块要跟真实 05 的形状一致，否则它就成了"另一个契约"。
    "list_hit": {"hit": False, "list_type": None, "entity_type": None, "entity_value": None},
    "rule_score": 55,
    "model_score": None,
    "final_score": 55,
    "risk_level": "medium",
    "decision": "review",
    "hit_rule_count": 1,
    "hits": [{"rule_code": "R_TEST", "score": 55}],
    "rule_versions": {"R_TEST": "v1"},
    "engine_version": "rule-1.0",
    "elapsed_ms": 3,
}


class CountingDecisionProvider:
    """固定分值的假 05：返回给定决策块并计数（V-03-06 也用它）。"""

    available = True

    def __init__(self, block: dict | None = None, delay: float = 0.02) -> None:
        self.calls = 0
        self.block = dict(block or FIXED_DECISION)
        self.delay = delay

    async def evaluate(self, event: dict, features: dict) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return dict(self.block)


class ExplodingFeatureProvider:
    """V-03-07：04 抛异常（模拟特征服务不可用）。"""

    available = False

    def __init__(self) -> None:
        self.calls = 0

    async def compute(self, event: dict) -> dict:
        self.calls += 1
        raise RuntimeError("simulated feature service down")


class ExplodingDecisionProvider:
    """V-03-08：05 抛异常（模拟决策服务不可用）。"""

    available = False

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, event: dict, features: dict) -> dict:
        self.calls += 1
        raise RuntimeError("simulated rule engine down")


class SleepingDecisionProvider:
    """V-03-09：05 慢到超过 200ms 预算（触发 `EVT-5001` 超时降级）。"""

    available = True

    def __init__(self, seconds: float = 1.0) -> None:
        self.seconds = seconds
        self.calls = 0
        self.finished = False
        self.cancelled = False

    async def evaluate(self, event: dict, features: dict) -> dict:
        self.calls += 1
        try:
            await asyncio.sleep(self.seconds)
            self.finished = True     # 超时后**不该**跑到这里
        except asyncio.CancelledError:
            self.cancelled = True    # BR-03-23：超时必须取消未完成的下游协程
            raise
        return dict(FIXED_DECISION)


def install_fakes(delay: float = 0.02):
    """装上假的 04/05，返回 `(feature_provider, decision_provider)`。"""
    from app.protocols import configure_components

    feature = FakeFeatureProvider(delay=delay)
    decision = CountingDecisionProvider(delay=delay)
    configure_components(feature_provider=feature, decision_provider=decision)
    return feature, decision


# ============================================================
# 样例载荷（Spec 表 3.1 的五类事件）
# ============================================================
def login_payload(**over) -> dict:
    payload = {
        "event_type": "login",
        "user_id": "u100001",
        "device_id": "DN00001",
        "ip": "192.0.2.101",
        "phone": "13800006621",
        "scene_extra": {"login_type": "pwd", "ua": "Mozilla/5.0", "success": True},
    }
    payload.update(over)
    return payload


def coupon_payload(**over) -> dict:
    payload = {
        "event_type": "coupon_receive",
        "user_id": "w000001",
        "device_id": "DWOOL0001",
        "ip": "203.0.113.10",
        "amount": 2000,
        "scene_extra": {
            "coupon_id": "CP12345", "activity_id": "ACT001",
            "face_value": 2000, "batch_id": "B1",
        },
    }
    payload.update(over)
    return payload


def order_create_payload(**over) -> dict:
    payload = {
        "event_type": "order_create",
        "user_id": "b100001",
        "device_id": "DBRUSH0001",
        "ip": "198.51.100.20",
        "address_id": "ADDR123456",
        "amount": 9900,
        "scene_extra": {
            "order_no": "SO00000001", "sku_count": 1,
            "total_amount": 9900, "address_id": "ADDR123456",
        },
    }
    payload.update(over)
    return payload


def order_pay_payload(**over) -> dict:
    payload = {
        "event_type": "order_pay",
        "user_id": "b100001",
        "amount": 9900,
        "scene_extra": {
            "order_no": "SO00000001", "pay_channel": "alipay",
            "pay_amount": 9900, "card_tail": "6621",
        },
    }
    payload.update(over)
    return payload


def after_sale_payload(**over) -> dict:
    payload = {
        "event_type": "after_sale_apply",
        "user_id": "r100001",
        "biz_no": "AS00000001",
        "amount": 29900,
        "scene_extra": {
            "after_sale_no": "AS00000001", "order_no": "RO00000001",
            "reason_code": "not_received", "refund_amount": 29900,
            "received_goods": False,
        },
    }
    payload.update(over)
    return payload


PAYLOAD_FACTORIES: tuple[tuple[str, object], ...] = (
    ("login", login_payload),
    ("coupon_receive", coupon_payload),
    ("order_create", order_create_payload),
    ("order_pay", order_pay_payload),
    ("after_sale_apply", after_sale_payload),
)


def unique_event_id() -> str:
    """生成一个合法的 20 位 `event_id`（序列段取时间戳，保证跨用例不撞）。

    不能用 `EVT000000000000000001` 这种固定值：夹具每个用例都会清空
    `seq_counters`，因此服务端生成的编号会重复，而固定值会与它们相撞，
    导致用例之间互相干扰（表现为"单独跑通过、一起跑失败"）。
    """
    return f"EVT{date_key(now_ms())}{now_ms() % 10 ** 12:012d}"


__all__ = [
    "CountingDecisionProvider", "ExplodingDecisionProvider",
    "ExplodingFeatureProvider", "FIXED_DECISION", "FakeFeatureProvider",
    "PAYLOAD_FACTORIES", "SleepingDecisionProvider", "after_sale_payload",
    "component_reset", "coupon_payload", "install_fakes", "login_payload",
    "order_create_payload", "order_pay_payload", "reset_runtime_state",
    "unique_event_id",
]
