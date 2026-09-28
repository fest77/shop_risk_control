# -*- coding: utf-8 -*-
"""事件接入网关 + 事件流模拟器的 HTTP 入口（模块 03 §3.1 ~ §3.4，决策 D7）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/events` | `sim:run` | 单条接入，**同步返回决策** |
| POST | `/events/batch` | `sim:run` | 批量接入（1~500 条，逐条串行） |
| GET  | `/events/{event_id}` | `dashboard:read` | 详情四段聚合读 |
| POST | `/mock/start` | `sim:run` | 启动模拟器 |
| POST | `/mock/stop` | `sim:run` | 停止并返回本次汇总 |
| GET  | `/mock/status` | `sim:run` | 运行状态（08 页只读展示） |

## 命名空间为什么是 `/mock/*`（Spec §8 的 N-03-5 / 决策 D7）

Spec 原文写 `/api/v1/simulator/*`，但它同时把 `/api/v1/sim/*` 留给了模块 10
（人工回放验证）。两者语义不同——一个是"造数据的事件源"，一个是"人工回放
验证"——用 `mock` 比 `simulator` 更短且不会与 `sim` 混淆。**决策 D7 已裁定**，
本实现按 D7 落地。

## 权限假设（Spec §3.4 的表把三个模拟器接口都写成 admin，接入类接口未规定）

- **接入类**（`POST /events`、`/events/batch`）与 **模拟器控制**用
  **`P_SIM_RUN`（`sim:run`，reviewer + strategist）**。理由：这两个接口的真实
  调用方是模块 10 的仿真测试页与事件流模拟器，而模块 01 §2.2 已把 `sim:run`
  定义为"事件仿真测试 / 批量回放"的能力，并把 reviewer 纳入其中（BR-01-13
  称其为"仿真测试例外"）。**没有**新增权限串——新增权限要动模块 01 的权限
  矩阵（唯一真源）与职责分离校验，属跨模块改动，超出本模块范围。
  另外：§2.2 并未把 `sim:run` 授予 admin，若这里改判 admin，就等于绕开矩阵
  另立规则（违反 BR-01-12）。**这是一处刻意的偏离，已在交付报告中登记**。
- **详情接口**用 **`P_DASHBOARD_READ`（`dashboard:read`，三角色可读）**。
  理由：详情是**只读**聚合，且模块 02 的实时事件流与模块 07 的研判都需要回看
  事件原文，把它限制成写权限持有者才能看，会让审核员无法取证。

生产环境若改为"仅模拟器可调、业务端走内部网络"，只需替换这里的权限依赖。

## 为什么 `POST /events` 手工解析请求体而不是声明 `EventIn` 参数

BR-03-01 要求"请求体必须是 JSON 对象，非对象/非法 JSON → `EVT-4001`"。
若把 `EventIn` 直接写成 FastAPI 参数，非法 JSON 会先被框架拦成 `COM-4000`，
`EVT-4001` 永远不可达。因此这里读原始 body 自己解析，再交给服务层
（第一层是 `EVT-4001`，往下才是字段级错误码）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api import register_router
from app.core.event_simulator import get_simulator
from app.deps import client_ip, require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.event_schema import (
    BatchOut,
    EventDetailOut,
    EventIngestOut,
    SimulatorStartIn,
    SimulatorStatusOut,
)
from app.security.permissions import P_DASHBOARD_READ, P_SIM_RUN
from app.services import audit_service, event_service
from app.services.event_service import evt_error

router = APIRouter(tags=["事件接入与模拟器"])


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


async def _read_json_object(request: Request) -> Any:
    """读原始请求体并解析 JSON。

    非法 JSON → `EVT-4001`（400）而不是框架的 `COM-4000`：§5 把"请求体非
    JSON 对象"明确归到本模块的码上，调用方据此区分"报文没拼对"与"字段不合格"。
    """
    raw = await request.body()
    if not raw:
        raise evt_error("EVT-4001", "请求体格式非法：请求体为空")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise evt_error(
            "EVT-4001", "请求体格式非法：不是合法 JSON", {"detail": str(e)[:200]}
        ) from e


# ============================================================
# §3.1 ~ §3.3 事件接入
# ============================================================
@router.post("/events", summary="单条事件接入（同步返回决策）")
async def ingest_event(
    request: Request,
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """接入一条事件并**同步返回决策**（§3.1）。

    响应里的决策块 12 个字段**全部来自 05**（契约冻结 / V-03-06）；04/05
    不可用或超时时按 fail-closed 返回 `review` + `degrade`，HTTP 仍是 200
    （§5 的 `EVT-5001/5002/5003`）。
    """
    payload = await _read_json_object(request)
    data = await event_service.get_event_service().ingest(payload)
    return envelope(
        "OK", "接入成功", _trace(request),
        EventIngestOut.model_validate(data).model_dump(),
    )


@router.post("/events/batch", summary="批量事件接入")
async def ingest_batch(
    request: Request,
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """批量接入（§3.2）。1~500 条，> 500 → `EVT-4010` 拒绝整批。

    批内**逐条串行**走同一决策链路（非并发）；单条校验失败只计入
    `failed_cnt` 并在 `results` 中标注，不影响其余条目（BR-03-16）。
    """
    body = await _read_json_object(request)
    data = await event_service.get_event_service().ingest_batch(body)
    return envelope(
        "OK", "批量接入完成", _trace(request),
        BatchOut.model_validate(data).model_dump(),
    )


@router.get("/events/{event_id}", summary="事件详情（四段聚合读）")
async def event_detail(
    request: Request,
    event_id: str,
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """`event` + `snapshot` + `decision` + `hits`（§3.3，对 E02/E03/E04 只读）。

    尚未异步落库时返回 `404 EVT-4404` 并提示 500ms 后重试——那是**正常**状态，
    不是错误（AD-01：决策同步返回、事件异步落库）。
    """
    data = await event_service.detail(event_id)
    return envelope(
        "OK", "查询成功", _trace(request),
        EventDetailOut.model_validate(data).model_dump(),
    )


# ============================================================
# §3.4 事件流模拟器控制（决策 D7：`/mock/*`）
# ============================================================
def _actor(user: dict) -> tuple[str, str]:
    return (
        str(user.get("_id") or user.get("username") or "unknown"),
        str(user.get("role", "")),
    )


async def _audit(request: Request, user: dict, action: str, after: dict) -> None:
    """模拟器启停留痕（BR-12-17 的 `strict=False` 类别）。

    启停是**低频管理动作**，与事件接入同属"不阻断业务"的一类：留不下痕也要让
    操作生效，否则运维会陷入"想停停不掉"。失败只告警（`AUD-5002`）。
    """
    actor, role = _actor(user)
    await audit_service.audit(
        actor=actor, actor_role=role, action=action,
        target_type="simulator", target_id="event_source",
        after=after, ip=client_ip(request),
        ua=request.headers.get("user-agent"), strict=False,
    )


@router.post("/mock/start", summary="启动事件流模拟器")
async def start_simulator(
    request: Request,
    payload: SimulatorStartIn,
    user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """启动模拟器（§3.4）。

    重复启动 → `409 EVT-4008`；参数非法 → 见 `validate_start_params`
    （`mode` 非法归 `EVT-5005`，区间越界归 `COM-4001`）。
    """
    sim = get_simulator()
    status = await sim.start(payload.model_dump())
    await _audit(request, user, "sim.start", {
        "mode": status["mode"], "rate": status["rate"], "seed": status["seed"],
        "max_events": payload.max_events, "duration_sec": payload.duration_sec,
    })
    return envelope(
        "OK", f"模拟器已启动（模式={status['mode']}）", _trace(request),
        SimulatorStatusOut.model_validate(status).model_dump(),
    )


@router.post("/mock/stop", summary="停止事件流模拟器")
async def stop_simulator(
    request: Request,
    user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """停止并返回本次汇总（§3.4）。

    未运行时也**返回 200**：`stop` 的语义是"确保它停下来"，重复调用返回
    "已停止"比报错更符合运维预期（`status.running=false` 即结论）。
    BR-03-31：汇总只写日志，**不写任何集合**。
    """
    sim = get_simulator()
    summary = await sim.stop()
    await _audit(request, user, "sim.stop", {
        "mode": summary["mode"], "emitted": summary["emitted"],
        "decision_counts": summary["decision_counts"],
    })
    return envelope(
        "OK", "模拟器已停止", _trace(request),
        SimulatorStatusOut.model_validate(summary).model_dump(),
    )


@router.get("/mock/status", summary="事件流模拟器状态")
async def simulator_status(
    request: Request,
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """运行状态（§3.4）。只读，不触发任何状态变更。"""
    status = get_simulator().status()
    return envelope(
        "OK", "ok", _trace(request),
        SimulatorStatusOut.model_validate(status).model_dump(),
    )


# 模块编号 "03"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀
register_router("03", router)

__all__ = ["router"]
