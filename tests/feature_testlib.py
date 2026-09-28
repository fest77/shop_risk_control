# -*- coding: utf-8 -*-
"""模块 04 测试的公共助手：假 09 提供者、假画像读取器、假仓储、窗口构造。

## 为什么单独一个文件（沿用模块 03 的踩坑结论）

文件名不以 `test_` 开头，pytest 不会收集它，因此可以被多个测试文件安全导入。
`tests/event_testlib.py` 的模块 docstring 记录过原因：两个**带 autouse 夹具**的
测试模块互相导入时，pytest 的夹具缓存会跨模块复用实例并触发
`assert not self._finalizers`。把可复用的助手放进非测试模块即可绕开。

## 为什么所有假实现都"能精确计数"

模块 04 的关键命题是**"不得用 0 冒充"**与**"聚集度一律向 09 取"**。
这两条都只能通过"调了几次、拿到的是什么"来验证，因此这里的假 09 提供者
既记录入参，也能按需返回 `None`（模拟 09 尚未落地）。
"""
from __future__ import annotations

from typing import Any, Optional

from app.engine.feature_compute import ProfileData
from app.engine.feature_window import FeatureWindow, WindowView, build_view
from app.errors import AppError

# ============================================================
# 时钟与窗口
# ============================================================
#: 固定锚点（毫秒），让窗口边界的断言完全确定、不依赖 `now_ms()`
ANCHOR_TS = 1_800_000_000_000

#: 测试用的窗口配置（与生产默认一致，但显式写出以免默认值变动影响断言）
WINDOW_CONFIG: dict[str, Any] = {
    "short_window_min": 60,
    "long_window_min": 1440,
    "agg_mode": "in_memory_sliding",
    "config_version": "w1",
}


def make_event(
    event_id: str,
    ts: int,
    event_type: str = "login",
    *,
    user_id: Optional[str] = None,
    device_id: Optional[str] = None,
    ip: Optional[str] = None,
    address_id: Optional[str] = None,
    success: Optional[bool] = None,
) -> dict:
    """构造一条**内部事件 dict**（形状与 `event_service.validate_event` 的产物一致）。

    刻意不走 HTTP 校验：纯计算侧的用例不该被簿记（编号、脱敏、幂等）拖慢。
    走真实校验的用例在 `test_feature_pipeline.py` 里。
    """
    event: dict[str, Any] = {
        "_id": event_id,
        "event_type": event_type,
        "user_id": user_id or "u_anchor",
        "ts": ts,
        "scene_extra": {},
    }
    if device_id:
        event["device_id"] = device_id
    if ip:
        event["ip"] = ip
    if address_id:
        event["address_id"] = address_id
    if success is not None:
        event["scene_extra"]["success"] = success
    return event


def make_window(**over: Any) -> FeatureWindow:
    """构造一个窗口（默认短 60 / 长 1440 分钟）。"""
    params: dict[str, Any] = {
        "short_window_min": WINDOW_CONFIG["short_window_min"],
        "long_window_min": WINDOW_CONFIG["long_window_min"],
    }
    params.update(over)
    return FeatureWindow(**params)


def view_with(
    *,
    event_ts: int,
    identities: dict[str, Optional[str]],
    entries: dict[tuple[str, str], list[tuple[str, int, str, bool]]],
) -> WindowView:
    """直接构造窗口视图（纯计算侧用例用，不必先 ingest）。"""
    return build_view(
        event_ts=event_ts,
        identities=identities,
        entries=entries,
        window_config=WINDOW_CONFIG,
    )


def entry(event_id: str, ts: int, event_type: str, pay_ok: bool = True):
    """一条窗口条目 `(event_id, ts, event_type, pay_ok)`。"""
    return (event_id, ts, event_type, pay_ok)


# ============================================================
# 假模块 09（关联账号数）
# ============================================================
class FakeLinkedUserCountProvider:
    """假 09：返回预设值并记录调用。

    `counts` 里的值可以是 `None`（模拟"09 也算不出来"），用于验证
    04 是否**如实标记缺失**而不是回落到 0。
    """

    available = True

    def __init__(self, counts: Optional[dict[str, Optional[int]]] = None) -> None:
        self.counts = dict(counts or {})
        self.calls: list[tuple[str, str]] = []
        #: 逐维度调用次数，便于断言"没有自建去重集合"（每次都问 09）
        self.repeat = 0

    async def get_linked_user_count(
        self, entity_type: str, entity_id: str
    ) -> Optional[int]:
        self.calls.append((entity_type, entity_id))
        self.repeat += 1
        return self.counts.get(entity_type)


class ExplodingLinkedUserCountProvider:
    """假 09：查询抛异常（依赖故障）。04 必须把它降级成"三项缺失"。"""

    available = False

    def __init__(self) -> None:
        self.calls = 0

    async def get_linked_user_count(
        self, entity_type: str, entity_id: str
    ) -> Optional[int]:
        self.calls += 1
        raise RuntimeError("simulated graph service down")


# ============================================================
# 假画像读取器（E10~E13）
# ============================================================
class FakeProfileReader:
    """返回固定画像并计数。"""

    def __init__(self, profile: Optional[ProfileData] = None) -> None:
        self.profile = profile or ProfileData()
        self.calls = 0

    async def load(self, event: dict) -> ProfileData:
        self.calls += 1
        return self.profile


class EmptyProfileReader:
    """冷启动：四类画像都不存在（全 `None`）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def load(self, event: dict) -> ProfileData:
        self.calls += 1
        return ProfileData.empty()


class ExplodingProfileReader:
    """画像读取抛异常，验证"依赖故障不拖垮整次计算、只让对应项缺失"。"""

    def __init__(self) -> None:
        self.calls = 0

    async def load(self, event: dict) -> ProfileData:
        self.calls += 1
        raise RuntimeError("simulated profile db down")


FULL_PROFILE = ProfileData(
    user_register_at=ANCHOR_TS - 10 * 86_400_000,   # 10 天前注册
    user_level="gold",
    user_risk_tag_cnt=2,
    device_first_seen_at=ANCHOR_TS - 48 * 3_600_000,  # 48 小时前首现
    ip_is_proxy=False,
    address_aftersale_cnt=1,
)


# ============================================================
# 假仓储（落库失败路径）
# ============================================================
class FailingSnapshotRepo:
    """落库永远失败的仓储：验证 BR-04-23（不阻塞、入重试队列、不回滚决策）。"""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.attempts = 0
        self.error = error or AppError(
            "FEA-5002", "快照落库失败（后台重试）", 200, {"detail": "simulated"}
        )

    async def insert_snapshot(self, doc: dict) -> str:
        self.attempts += 1
        raise self.error


class CountingSnapshotRepo:
    """记录写入而不真的落库（验证落库内容，不受 Mongo 影响）。"""

    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def insert_snapshot(self, doc: dict) -> str:
        self.docs.append(dict(doc))
        return str(doc.get("_id"))


__all__ = [
    "ANCHOR_TS",
    "CountingSnapshotRepo",
    "EmptyProfileReader",
    "ExplodingLinkedUserCountProvider",
    "ExplodingProfileReader",
    "FULL_PROFILE",
    "FailingSnapshotRepo",
    "FakeLinkedUserCountProvider",
    "FakeProfileReader",
    "WINDOW_CONFIG",
    "entry",
    "make_event",
    "make_window",
    "view_with",
]
