# -*- coding: utf-8 -*-
"""事件仿真测试 HTTP 入口（模块 10 §3）。路径**相对 `API_PREFIX`**，前缀由
`app/api/__init__.py` 的注册器统一添加（模块 00 §6 约定）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET  | `/sim/cases` | `sim:run` | 用例列表（仅未归档） |
| POST | `/sim/cases` | `sim:run` | 保存用例（**恰好一条**审计，D41） |
| POST | `/sim/run` | `sim:run` | **单条仿真执行（核心）** |
| POST | `/sim/batch` | `sim:run` | 批量回放（默认 20，上限 200） |
| GET  | `/sim/runs/{run_id}` | `sim:run` | 历史执行详情（BR-10-13） |

## 权限：`sim:run`（不新造权限码）

矩阵 `app/security/permissions.py` 里 `P_SIM_RUN` 的持有者是
**reviewer + strategist**，`MENU_PERMISSIONS["#/sim"]` 也是它——这正是
E2E 已断言的"审核员菜单含「事件仿真」"。BR-10-21 的正文还额外写了
"admin 无此权限"，与矩阵一致（`PERMISSION_MATRIX` 里没有 admin）。
因此本模块**不新增任何权限串**（BR-01-12：新增权限要动模块 01 的唯一真源）。

**五个端点全部用同一个权限**（读写不分）：矩阵里 `sim:run` 是**唯一**一个
仿真侧权限，没有 `sim:read`；而 Spec §3 的接口清单也没有给读端点单列权限。
这与 06-B 的规则侧（读与写共用 `rule:write`）是同一个处境、同一个处置。

## 超时（`SIM-5003`）为什么包在接口层而不是服务层

Spec §5 定的是"仿真超时（>5s）→ 504"。包在接口层有三个好处：
① 服务层拿到的是"干净的一次执行"，不必在每个 `await` 后面插超时判断；
② `asyncio.wait_for` 的取消会传播到正在跑的协程，因此"超时即停"是真的停，
不是"返回了但后台还在写"；
③ `run_id` 在超时路径上仍然可用——`_write_run` 是在 `simulate()` 内部
**同步**完成的（不是后台任务），所以**能在超时前落库的执行都有记录**；
超时被取消时那一条会缺失，这一点在 `SimTimeoutError.data` 里如实标注。

## 批量回放的限流预算（任务书 §4 前端提到"限流 60 请求/分钟"）

`POST /sim/batch` 一次请求会产生 `repeat` 条 `sim_runs`，但**只算 1 次
HTTP 请求**（限流按请求计），因此 200 条上限是真正的保护手段（BR-10-19）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api import register_router
from app.constants import SIM_BATCH_DEFAULT_REPEAT, SIM_BATCH_MAX_REPEAT, SIM_TIMEOUT_MS
from app.deps import client_ip, require_permission
from app.errors import SimTimeoutError, envelope
from app.logging import get_trace_id
from app.schemas.sim_schema import (
    SimBatchIn,
    SimBatchOut,
    SimCaseCreatedOut,
    SimCaseIn,
    SimCaseListOut,
    SimRunDataOut,
    SimRunIn,
)
from app.security.permissions import P_SIM_RUN
from app.services.sim_service import SimService, validate_case_fields

router = APIRouter(tags=["事件仿真测试"])


def get_sim_service() -> SimService:
    """服务装配点（与 `get_rule_service` 同形，便于测试覆写依赖）。"""
    return SimService()


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


def _actor(user: dict) -> str:
    """操作人取自**令牌身份**而非请求体：客户端自报操作人等于让审计失去意义。"""
    return str(user.get("_id") or user.get("username") or "unknown")


def _role(user: dict) -> str:
    return str(user.get("role", ""))


async def _run_with_budget(coro: Any, *, step: str = ""):
    """`SIM-5003`：超过 `SIM_TIMEOUT_MS` 就取消并抛 504。

    `coro` 是一个**已经创建**的协程对象：超时后 `wait_for` 会取消它，
    取消会一路传播到 04/05 内部的 `await`（与 03 的 BR-03-23 同一机制）。
    `step` 只用于错误 message，便于排障时知道卡在哪一步。
    """
    try:
        return await asyncio.wait_for(coro, timeout=SIM_TIMEOUT_MS / 1000.0)
    except asyncio.TimeoutError as e:
        raise SimTimeoutError(SIM_TIMEOUT_MS, failed_step=step or None) from e


# ============================================================
# §3.1 / §3.2 用例
# ============================================================
@router.get("/sim/cases", summary="仿真用例列表")
async def list_sim_cases(
    request: Request,
    service: SimService = Depends(get_sim_service),
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """列出未归档用例（Spec §3.1）。

    已归档的用例**不出现在列表里**，但它们仍能被 `case_id` 解析
    （`simulate` 用的是 `find_by_id`）——BR-10-20 的软删是为了让历史
    `sim_runs` 可追溯，不是为了继续占据左栏。
    """
    data = await service.list_cases()
    return envelope(
        "OK", "查询成功", _trace(request),
        SimCaseListOut.model_validate(data).model_dump(),
    )


@router.post("/sim/cases", status_code=201, summary="保存仿真用例")
async def create_sim_case(
    request: Request,
    payload: SimCaseIn,
    service: SimService = Depends(get_sim_service),
    user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """保存用例（Spec §3.2）。同名（未归档）→ `409 SIM-4003`。

    **审计恰好一条**（D41 范式，同 `RuleService.create_rule`）：
    服务层先落库、再写 `sim.case.create`、审计失败就**物理撤销**这次写入并抛
    `503 AUD-5001`（审计模块自己的码，见 `SimService.save_case` 的说明）。
    """
    validate_case_fields(payload.category, payload.expected_decision)
    data, _case_id = await service.save_case(
        payload, _actor(user),
        actor_role=_role(user),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope(
        "OK", "用例已保存", _trace(request),
        SimCaseCreatedOut.model_validate(data).model_dump(),
    )


# ============================================================
# §3.3 单条仿真执行（核心）
# ============================================================
@router.post("/sim/run", summary="单条仿真执行（复用真实决策链路）")
async def run_sim(
    request: Request,
    payload: SimRunIn,
    service: SimService = Depends(get_sim_service),
    user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """跑一次仿真并返回五步链路（Spec §3.3）。

    ## 状态码

    | 码 | HTTP | 触发 |
    |---|---|---|
    | `SIM-4001` | 400 | 事件体非法（`data` 带 03 的原码与缺失字段） |
    | `SIM-4002` | 422 | `scene_extra` 非合法 JSON |
    | `SIM-5001` | 503 | 引擎降级 / 依赖不可用（**不返回任何结论**） |
    | `SIM-5003` | 504 | 超过 5s |

    ## 为什么 `dry_run` 不在请求体里

    Spec §3.3 的响应写明 `dry_run` **恒 true**，BR-10-09 也要求
    "服务端强制，不信任前端传参"。因此请求体里没有这个开关——
    有开关就意味着有一半的调用会把它设成 `false`（"我就是想写一条真决策看看"），
    而那会让仿真污染 07 的案件列表与 11 的统计。
    `affect_window` 是**另一回事**（D11 明确要给这个开关），且默认关闭。
    """
    outcome = await _run_with_budget(
        service.simulate(
            payload.event,
            case_id=payload.case_id,
            affect_window=bool(payload.affect_window),
            operator=_actor(user),
            actor_role=_role(user),
            ip=client_ip(request),
            ua=request.headers.get("user-agent"),
        ),
        step="sim.run",
    )
    data = outcome.to_data()
    message = "仿真完成" + (
        "（含未命中预期的结果，请复核规则阈值）"
        if outcome.matched_expected is False else ""
    )
    if not outcome.record_saved:
        # `SIM-5002`（HTTP 200）：结果照常展示，只提示可能无法回看。
        # 用 `data.notice_code` 表达而不是另造一个 HTTP 错误——Spec §5 的
        # HTTP 列写的就是 200（与 08 的 `DSP-5003/5004` 同一形态）。
        data["notice_code"] = "SIM-5002"
        data["notice_message"] = "本次结果已返回，但链路记录保存失败（可能无法回看）"
        message = f"{message}；但链路记录保存失败，本次可能无法回看"
    return envelope(
        "OK", message, _trace(request),
        SimRunDataOut.model_validate(data).model_dump(),
    )


# ============================================================
# §3.4 批量回放
# ============================================================
@router.post("/sim/batch", summary="批量回放（默认 20，上限 200）")
async def run_sim_batch(
    request: Request,
    payload: SimBatchIn,
    service: SimService = Depends(get_sim_service),
    user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """批量回放一个用例（Spec §3.4）。

    `repeat > 200` → `422 SIM-4005`（BR-10-19）。**不静默截断**：
    见 `SimBatchLimitError` 的说明。

    `affect_window` **不在请求体里**：决策 D11 的原话是"批量回放强制为 false"。
    把开关留着再"忽略它"是一种更差的表达——调用方会以为自己开的开关生效了。
    """
    result = await _run_with_budget(
        service.replay(
            case_id=payload.case_id,
            event=payload.event,
            repeat=payload.repeat,
            seed=payload.seed,
            operator=_actor(user),
            actor_role=_role(user),
            ip=client_ip(request),
            ua=request.headers.get("user-agent"),
        ),
        step="sim.batch",
    )
    return envelope(
        "OK",
        f"批量回放完成：共 {result.total} 条，符合预期 {result.matched} 条，"
        f"不符 {result.mismatched} 条，误伤 {result.false_positive} 条",
        _trace(request),
        SimBatchOut.model_validate(result.to_data()).model_dump(),
    )


# ============================================================
# §3.5 历史执行详情
# ============================================================
@router.get("/sim/runs/{run_id}", summary="查看历史执行详情")
async def get_sim_run(
    request: Request,
    run_id: str,
    service: SimService = Depends(get_sim_service),
    _user: dict = Depends(require_permission(P_SIM_RUN)),
):
    """按 `run_id` 还原链路（Spec §3.5 / BR-10-13）。查不到 → `404 SIM-4004`。"""
    data = await service.get_run(run_id)
    return envelope(
        "OK", "查询成功", _trace(request),
        SimRunDataOut.model_validate(data).model_dump(),
    )


# 模块编号 "10"（`00_模块划分与边界` §6：`10 → SIM`），由注册器统一加 `/api/v1` 前缀
register_router("10", router)

__all__ = [
    "SIM_BATCH_DEFAULT_REPEAT",
    "SIM_BATCH_MAX_REPEAT",
    "get_sim_service",
    "router",
]
