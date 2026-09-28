# -*- coding: utf-8 -*-
"""`case.disposed` 领域事件的**进程内投递**（模块 08 Spec §3.2 / §8「新增 8」）。

## 为什么本模块要自带一个最小事件总线

Spec §3.2 把 `case.disposed` 冻结成对外契约（载荷
`{case_no, user_id, device_id, conclusion, action_types, acted_at}`，消费方是
09 的画像标签/红边与 11 的资损处置统计），而 §8「新增 8」同时写明：
**"本 Spec 假定 00 公共基础提供进程内事件总线；若无，则 09 需改为轮询
`case_actions` 增量（需与 00 / 09 对齐）"**。实际情况是仓库里**没有**这个总线
（`app/core/` 下有降级、限流、SSE 总线、定时任务，没有任何领域事件机制）。

处置流程（BR-08-29 ⑦）却要求"发布 `case.disposed` 事件"。三条可能的路：

1. 直接调用 09 的接口（把"订阅"写死在 08 里）——**最省事，但方向是反的**：
   08 会知道 09 的内部结构，将来多一个消费方（11）就要再改一次 08；
2. 什么都不做、只打日志——"假装发布了事件"，消费方永远不会被触发；
3. **实现一个最小的进程内总线**（本文件），`publish` 侧只管发，
   订阅侧各自注册。选它，因为它是唯一既能让 ⑦ 真正落地、
   又不把消费方的细节焊进 08 的做法。

## 交付边界（如实声明）

这是一个**进程内、非持久**的总线：进程崩溃会丢掉尚未投递的事件。
因此它**不能**是唯一保障——`case_actions` 是持久事实（处置流水的 `acted_at`
与 `conclusion` 都在库里），消费方需要"绝不丢"时应当按 §8 新增 8 的备选方案
轮询 `case_actions` 增量。本文件的存在只是让"⑦ 发布事件"这一步有真实落点，
而不是一句注释。

## 订阅者失败绝不冒泡

`publish` 逐个调用订阅者并**吞掉异常**（记日志 + 置降级标记）。理由是位置：
⑦ 发生在"处置已经全部生效（名单已写、案件已 disposed、审计已落）"**之后**，
此时任何异常冒出去都会让调用方以为处置失败了——那是最坏的一种误报
（用户会重复处置，而处置不可撤销）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.degraded import DEGRADED
from app.logging import get_logger

log = get_logger("shop_risk_control.case.events")

#: 事件名（Spec §3.2 冻结）
EVENT_CASE_DISPOSED = "case.disposed"

Handler = Callable[[dict], Any]

_subscribers: list[tuple[str, Handler]] = []


def subscribe(name: str, handler: Handler) -> None:
    """注册订阅者（同名重复注册会被忽略，避免热重载后同一个处理跑两遍）。"""
    if any(existing == name for existing, _ in _subscribers):
        return
    _subscribers.append((name, handler))


def subscribers() -> list[str]:
    return [name for name, _ in _subscribers]


def reset_subscribers() -> None:
    """清空订阅者（测试用；生产无调用点——它会让事件无人接收）。"""
    _subscribers.clear()
    _install_default_subscribers()


async def publish(event: str, payload: dict) -> dict:
    """投递事件，返回 `{event, delivered, failed, handlers}`。

    **顺序执行**（不用 `asyncio.gather`）：订阅者是低频的旁路写，
    顺序执行让"哪一步失败"在日志里可读；并发反而会让部分失败更难归因。
    代价是发布耗时 = 各订阅者之和——因此订阅者必须自己做超时/降级控制。
    """
    delivered = 0
    failed: list[str] = []
    for name, handler in list(_subscribers):
        try:
            result = handler(payload)
            if hasattr(result, "__await__"):
                await result
            delivered += 1
        except Exception as e:  # noqa: BLE001 - 见模块 docstring：绝不冒泡
            failed.append(name)
            DEGRADED.mark(f"{event} 订阅者 {name} 失败：{e}")
            log.warning("[GRP-5003] %s 的订阅者 %s 处理失败（不影响已生效的处置）：%s",
                        event, name, e)
    if not _subscribers:
        log.info("%s 已发布但当前没有订阅者（载荷：%s）", event, payload)
    return {"event": event, "delivered": delivered, "failed": failed,
            "handlers": subscribers()}


async def publish_case_disposed(
    *, case_no: str, user_id: str, device_id: Optional[str],
    conclusion: str, action_types: list[str], acted_at: int,
    list_writes: Optional[list[dict]] = None,
) -> dict:
    """发布 `case.disposed`（BR-08-29 ⑦ 的落点）。"""
    return await publish(EVENT_CASE_DISPOSED, {
        "event": EVENT_CASE_DISPOSED,
        "case_no": str(case_no),
        "user_id": str(user_id),
        "device_id": device_id,
        "conclusion": str(conclusion),
        "action_types": [str(a) for a in (action_types or [])],
        "acted_at": int(acted_at),
        "list_writes": list(list_writes or []),
    })


# ============================================================
# 默认订阅者：09 的"红边 + 历史黑名单标签"
# ------------------------------------------------------------
# 09 目前**没有**订阅这个事件（Spec §8 新增 8 的悬空点）。在这里按 09 的
# **公开契约**（`ProfileService.tag_user` 与 `GraphRepo.mark_edge_risk`）提供
# 最小投递实现，而不是让 08 直接去写 E10 / E14——那样就绕过了 09 的
# 唯一写入方（BR-09-03 / 09-10 的"单一真源"）。
#
# 09 一旦自带订阅者，**删掉这里的 `_install_default_subscribers()` 调用即可**，
# 08 侧不需要任何改动（这正是"总线"相对"直接调用"的价值）。
# ============================================================
def _user_blacklisted(payload: dict) -> bool:
    """本次处置是否真的把**该用户**写进了黑名单（数据驱动，不是"处置了就算"）。"""
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return False
    return any(
        str(write.get("list_type")) == "black"
        and str(write.get("entity_type")) == "user"
        and str(write.get("entity_value")) == user_id
        for write in payload.get("list_writes") or []
    )


async def _tag_when_blacklisted(payload: dict) -> int:
    """写进"用户黑名单"时打 `blacklist_history` 标签（BR-09-03 / Spec §1 下游）。

    判据是**数据驱动**的（`_user_blacklisted`）：只有真的落了
    `{black, user, <本案用户>}` 才打标签。打标签必须有事实依据，
    否则"历史黑名单"会退化成装饰——而它正是下一次决策的输入。
    """
    if not _user_blacklisted(payload):
        return 0
    # 走 09 的公开契约（§3.3）：本模块**不**直接写 E10，标签的唯一真源归 09
    from app.services.profile_service import tag_user

    await tag_user(str(payload["user_id"]), "blacklist_history")
    return 1


async def _mark_edges_risk(payload: dict) -> int:
    """把该用户与"本次被拉黑/封禁的实体"之间的边标红（BR-09-10，由 08 触发）。

    `mark_edge_risk` **只能置 `true`**（红边不允许自动清除，BR-09-10），
    因此本处理是幂等的：重复处置同一用户/设备第二次不会产生新的副作用。
    """
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return 0
    targets: list[tuple[str, str]] = []
    for write in payload.get("list_writes") or []:
        entity_type = str(write.get("entity_type") or "")
        entity_value = str(write.get("entity_value") or "")
        if entity_type == "device" and entity_value:
            targets.append(("used_device", entity_value))
        elif entity_type == "ip" and entity_value:
            targets.append(("shared_ip", entity_value))
    if not targets:
        return 0

    from app import db as db_module
    from app.repos.graph_repo import GraphRepo

    graph = GraphRepo(db_module.get_db())
    marked = 0
    for relation, entity_value in targets:
        if await graph.mark_edge_risk(user_id, entity_value, relation):
            marked += 1
    return marked


def _install_default_subscribers() -> None:
    subscribe("profile.tags", _tag_when_blacklisted)
    subscribe("graph.risk_edges", _mark_edges_risk)


_install_default_subscribers()


__all__ = [
    "EVENT_CASE_DISPOSED",
    "publish",
    "publish_case_disposed",
    "reset_subscribers",
    "subscribe",
    "subscribers",
]
