# -*- coding: utf-8 -*-
"""规则决策引擎 HTTP 入口（模块 05 §3.1）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/engine/evaluate` | `sim:run` | 单条事件决策求值（仿真 / 调试用） |

## 仿真模块（10）**必须**复用本接口（Spec §3.1）

Spec 原文：「仿真模块（10）必须调用此接口以复用真实链路，**禁止另写判定逻辑**」。
理由是仿真页的价值恰恰在于"展示真实链路会怎么判"——若 10 自己实现一遍判定，
它展示的就是一个**看起来一样但会独立漂移**的影子引擎；而仿真页正是用来验证
"这条规则配下去会发生什么"的，影子引擎一旦与真实引擎不一致，仿真结论就成了
误导（V-05-13 要验的就是这条）。本模块已经把"两条路走同一个 `decide()`"钉在
`app/engine/decision_provider.py`（03 的事件链路）与 `app/engine/decision.py`
（本接口）的唯一实现上。

## 权限假设（Spec 未规定，按最小合理假设并标注）

Spec §3.1/§3.3 都没写权限。这里取 **`sim:run`**（事件仿真测试）：

- 本接口的定位就是"仿真 / 调试"（§3.1 标题与 `dry_run` 的说明），
  其两个真实消费者是**模块 10 的仿真页**（reviewer + strategist，正是
  `sim:run` 的持有者）与排障；
- 它会**写库**（`dry_run=false` 时写 `decisions`/`decision_hits`）并且会
  触发特征计算（写入 04 的滑动窗口），因此不适合发给只读角色 admin。

**这是一处刻意的偏离**（Spec 未规定），已登记在交付报告里。若将来要求
"策略师专用"，只需把这里换成新的权限串——但新权限必须先进模块 01 的
权限矩阵（BR-01-12 的唯一真源），不能在本模块自造。

## `dry_run` 与 D11 的边界（如实说明）

`dry_run=true` 时本接口**不写** `decisions`/`decision_hits`（§3.1）。但特征
窗口的写入发生在 **04 内部**（`FeatureService.compute` 会先 `ingest`），
本接口无法关闭它——决策 D11 要求仿真"默认不写真实特征窗口"并提供
`affect_window` 开关，那个开关的宿主是**模块 10 的仿真链路**（它决定要不要
把事件投给 04 的真实窗口），不在本接口的契约里（§3.1 的请求体只有
`event`/`dry_run`/`trace`）。此处如实登记，避免"看起来已经支持 D11"的错觉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app import db as db_module
from app.api import register_router
from app.deps import require_permission
from app.engine import decision as decision_engine
from app.engine.decision import DecisionOutcome
from app.errors import AppError, envelope, rul_error
from app.logging import get_trace_id
from app.schemas.engine_schema import EngineEvaluateIn, EngineEvaluateOut
from app.security.permissions import P_SIM_RUN
from app.services.event_service import validate_event
from app.utils.ids import new_event_id
from app.utils.timeutil import now_ms

router = APIRouter(tags=["规则决策"])

#: 需要以 **HTTP 错误**形式回报的降级码（Spec §3.1 的状态码栏：503 引擎依赖不可用）。
#: 特征侧的降级（`FEA-5001`）**不在**此列：它由 04 如实报告、由 03 在事件链路上
#: 短路（D44），本接口对它的正确表达是"200 + `review` + `degraded=true`"。
HTTP_DEGRADE_CODES: tuple[str, ...] = ("RUL-5001", "RUL-5002")


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


async def _normalize_event(raw: Any) -> dict:
    """校验并归一事件体。

    分工（**不要在这里重写 03 的校验**）：

    1. `RUL-4001`（400）：`event` 不是对象、或缺 `event_type` / `user_id`
       ——Spec §5 对这条码的定义就是这两个字段；
    2. 其余字段级判定交给 03 的 `validate_event`，它的 `EVT-4xxx` **原样抛出**
       （ER-02：引用其他模块的错误码时必须保持原前缀）。两条链路对同一个
       坏报文给同一个码，调用方才能用一套分流逻辑。
    """
    if not isinstance(raw, dict) or not raw:
        raise rul_error("RUL-4001", "事件参数不合法：`event` 必须是 JSON 对象")
    missing = [
        field for field in ("event_type", "user_id")
        if not str(raw.get(field) or "").strip()
    ]
    if missing:
        raise rul_error(
            "RUL-4001",
            f"事件参数不合法：缺少必填字段 {'、'.join(missing)}",
            {"missing": missing},
        )
    try:
        event = validate_event(raw, now_ms())
    except AppError:
        raise  # EVT-4xxx 原样透出（含缺失字段清单，模块 10 靠它渲染提示）
    except Exception as e:  # noqa: BLE001 - 校验器的意外异常仍属"报文不合法"
        raise rul_error(
            "RUL-4001", f"事件参数不合法：{type(e).__name__}: {e}"
        ) from e

    # 事件编号：请求里给了就用（便于与真实事件对照），没给就现取一个。
    # E01 的编号格式是 `EVT{yyyyMMdd}{12位}`，由 `seq_counters` 原子取号，
    # 保证不会与真实事件的编号撞车（撞车的后果是详情接口查到别的事件）。
    event_id = str(raw.get("event_id") or raw.get("_id") or "").strip()
    event["_id"] = event_id or await new_event_id(db_module.get_db())
    return event


def _to_response(outcome: DecisionOutcome, *, trace: bool) -> dict[str, Any]:
    """把 `DecisionOutcome` 组装成 §3.1 的响应体。"""
    payload: dict[str, Any] = {"event_id": outcome.event_id}
    payload.update(outcome.block)
    payload["degraded"] = bool(outcome.degraded)
    # Spec §3.1：`trace=false` 时必须为 `null`（而不是空数组）——空数组会被
    # 前端读成"求值了但一条规则都没有"，与"没要求返回过程"是两回事。
    payload["trace"] = outcome.trace if trace else None
    payload["warnings"] = list(outcome.warnings)
    return EngineEvaluateOut.model_validate(payload).model_dump()


@router.post("/engine/evaluate", summary="单条事件决策求值（仿真/调试）")
async def engine_evaluate(
    request: Request,
    body: EngineEvaluateIn,
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """对一条事件跑**真实决策链路**并返回决策块（§3.1）。

    与 `POST /api/v1/events` 的区别只有三处：① 不写 `risk_events`
    （它是一次求值，不是一次接入）；② `dry_run` 默认 `true`（不写决策）；
    ③ 返回额外的 `degraded` / `trace` / `warnings`。

    ## 降级怎么表达

    - **名单 / 规则集依赖不可用**（`RUL-5001` / `RUL-5002`）：决策**已经算出**
      "本次必须转人工"，只是引擎的输入缺失。按 Spec §3.1 的状态码栏返回 `503`，
      并且**响应 `data` 里仍然带着那个 `decision=review`、`degraded=true` 的
      决策块**（"返回 degraded=true 与人工审核建议，不返回 pass"）。它已经按
      决策 D5 落库建案（`dry_run=false` 时），因此这个 503 不代表"请求丢了"。
    - **特征不完整**（`FEA-5001`）：返回 `200` + `review` + `degraded=true`。
      特征侧的问题由 04 报告、由 03 在事件链路上短路（D44），本接口没有对应的
      `RUL` 码可给——硬套 `RUL-5002` 会让"规则集读不出来"与"特征算不出来"
      共用一个码，而两者的处置完全不同（前者要查 `rules`，后者要查窗口/画像）。
    """
    event = await _normalize_event(body.event)
    outcome = await decision_engine.decide(
        event, dry_run=body.dry_run, trace=body.trace
    )
    data = _to_response(outcome, trace=body.trace)

    if outcome.degrade_code in HTTP_DEGRADE_CODES:
        raise rul_error(
            outcome.degrade_code,
            outcome.degrade_reason,
            data,   # 降级决策块随错误一起返回，调用方不会丢结论
        )
    return envelope("OK", "决策完成", _trace(request), data)


# 模块编号 "05"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀。
# ⚠️ 每个模块编号**只能登记一次**（重复登记会抛错，见 `app/api/__init__.py`）。
register_router("05", router)

__all__ = ["HTTP_DEGRADE_CODES", "router"]
