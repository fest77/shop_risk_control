# -*- coding: utf-8 -*-
"""案件**读侧** HTTP 入口（模块 07 §3.1 列表 / §3.2 详情）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/cases` | `case:read` | 多维筛选 + 分页（`total` 由本接口给出） |
| GET | `/cases/{case_no}` | `case:read` | **一次响应驱动三栏** + `degraded_parts` |

## 为什么读侧单独一个文件、`case_api.py` 仍是 08 的处置侧

任务书 §0 的裁定（**D67**）：**07 实现读侧**（列表 + 详情 + 查询编排），
**08 已交付的处置侧不动**。Spec 07 §6 写的"与 08 共用同一 router"是**实现细节、
不是契约**，而"模块 id 只能登记一次"是硬约束（`register_router` 对重复编号直接
报错）。两者叠加的结论只能是：**两个 router、各自登记一次**
（08 在 `case_api.py` 登记，07 在本文件登记）。

⚠️ **本文件只定义路由，不登记**：登记动作在 `app/api/__init__.py` 里以**显式
函数调用**完成（`_register_cross_module_routers`）。原因是本模块的 router 必须
排在那两个 api 模块**之后**才能登记（08 的 `case_api` 也要 import 本文件），
而 `_API_MODULES` 是"按顺序 import"的元组——在那里写 import 会形成环。
详见 `app/api/__init__.py` 的说明。

## 权限：`case:read` **仅 reviewer**（冻结矩阵，裁定 D69）

`case:read` 取自 `app/security/permissions.py`（BR-01-12 的唯一真源），
本文件**没有一处角色判断**——所有权限拒绝由权限层统一给 `AUTH-4020`（D28）。

Spec **BR-07-24** 写"admin 也可只读列表与详情用于审计排查"，而冻结矩阵里
`case:read` **只有 reviewer**。任务书 §0 的裁定（**D69**）：**本轮不实现**
"admin 只读案件"——它需要改模块 01 的矩阵与菜单映射，属跨模块重构；
admin 的审计排查能力由**模块 12 审计日志**承担。因此 admin 调本接口得到的是
`403 AUTH-4020`（而不是 Spec 07 §5.1 的 `CASE-4031`），这是一处**如实登记的
偏离**：新造一个 `CASE-4031` 会与权限层的统一拒绝逻辑打架，让"谁拒绝了这次
访问"出现两个答案。

## 错误码

`CASE-4004`（400 筛选/分页/排序非法）由 `case_query_service` 抛出；
**案件不存在（404）与读库失败（503）在接口层翻译**（见下面两个 handler 的
注释——`CaseNotFoundError` 是 08 的 `DSP-4040`，而本模块的契约码是
`CASE-4001`，不能混用）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pymongo.errors import PyMongoError

from app.api import register_router
from app.constants import PAGE_MAX, PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from app.deps import require_permission
from app.errors import CaseNotFoundError, case_error, envelope
from app.logging import get_trace_id
from app.security.permissions import P_CASE_READ
from app.services.case_query_service import CaseQueryService, get_case_query_service

router = APIRouter(tags=["案件审核"])


def get_query_service() -> CaseQueryService:
    """案件查询服务（**进程内单例**；依赖全部按需现取，不缓存库句柄）。

    不缓存 `CaseRepo` 等仓储：测试会切换数据库（`db.use_database`），
    缓存了某个库的集合句柄就会把查询打到别的库上（与 `case_api` 同因）。
    """
    return get_case_query_service()


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.get("/cases", summary="案件列表（多维筛选 + 分页）")
async def list_cases(
    request: Request,
    # 多值筛选（`risk_level` / `status` / `scene_code`）声明成 `list[str]`：
    # FastAPI 会把**重复 query 参数**收成列表（Spec §3.1 的"多值用重复参数 OR"），
    # 而 `case_query_service.normalize_multi` 同时兼容逗号分隔写法。
    risk_level: Optional[list[str]] = Query(
        None, description="medium / high；重复参数或逗号分隔（OR）"),
    scene_code: Optional[list[str]] = Query(
        None, description="E06 场景码；重复参数或逗号分隔（OR）"),
    status: Optional[list[str]] = Query(
        None, description="pending / reviewing / disposed / archived（OR）"),
    created_from: Optional[str] = Query(None, description="触发时间起（毫秒，含）"),
    created_to: Optional[str] = Query(None, description="触发时间止（毫秒，含）"),
    assignee: Optional[str] = Query(None, description="按审核人筛（「我的案件」）"),
    keyword: Optional[str] = Query(None, description="匹配 case_no 或 user_id"),
    page: str = Query("1", description=f"≥1；> {PAGE_MAX} 由服务层判为 CASE-4004"),
    page_size: str = Query(str(PAGE_SIZE_DEFAULT),
                           description=f"1~{PAGE_SIZE_MAX}；页面固定 20（BR-07-04）"),
    sort: Optional[str] = Query(
        None, description="字段:asc|desc，逗号分隔多级；默认 risk_level:desc,created_at:desc"),
    service: CaseQueryService = Depends(get_query_service),
    _user: dict = Depends(require_permission(P_CASE_READ)),
):
    """案件列表（Spec §3.1 / §3.4，BR-07-01 ~ 05）。

    ## 为什么 `page` / `page_size` 声明成 `str`

    契约要求越界给 **`400 CASE-4004`**（§5.1）。若声明成 `int = Query(..., ge=1)`，
    FastAPI 会先拦成通用 `COM-4001`(422)，**契约码永远拿不到**——这与
    `list_api` / `rule_api` 把分页边界留给服务层是同一个取舍。

    ## `total` 是顶部「共 N 条待审」的唯一来源（BR-07-05b）

    `total` 由本接口在**同一个响应**里返回（服务端 `count_documents` 逐组算准），
    前端**不得**对当前页自行计数。`as_of` 供页面右上角显示「数据截至 xx:xx:xx」。
    """
    data = await service.list_cases(
        risk_level=risk_level, scene_code=scene_code, status=status,
        created_from=created_from, created_to=created_to,
        assignee=assignee, keyword=keyword,
        page=page, page_size=page_size, sort=sort,
    )
    return envelope("OK", "查询成功", _trace(request), data)


@router.get("/cases/{case_no}", summary="案件详情（一次响应驱动三栏）")
async def get_case_detail(
    request: Request,
    case_no: str,
    service: CaseQueryService = Depends(get_query_service),
    _user: dict = Depends(require_permission(P_CASE_READ)),
):
    """一次取齐三栏数据（Spec §3.2，BR-07-06）。

    响应含 `case` / `event` / `snapshot` / `decision` / `hits` / `profile` /
    `baseline` / `graph` 八块 + `degraded_parts`：

    - **三栏必须用这同一次响应渲染**（原型注记第 1 条「不得异步错位」）；
      中栏/右栏各自再发请求就会出现"A 的画像配 B 的判定"这种审错案件的中间态；
    - `degraded_parts` 非空时**只降级对应卡片**（前端显示「xx 加载失败 + 重试」），
      其余卡片与左栏保持可用（BR-07-12：**不得整页失败、不得静默隐藏**）；
    - 判定数据取不到时 `decision=null` 且 `"decision"` 进 `degraded_parts`，
      页面必须显示「未取到系统判定数据，请勿据此放行」——
      **绝不显示 0 分或"放行"**（CASE-5004：缺数据 ≠ 放行）。

    ## 错误码的翻译（本层做，服务层不关心"对外叫哪个码"）

    - 案件不存在 → **`CASE-4001`(404)**。服务层复用的 `CaseNotFoundError`
      带的是 **08 的 `DSP-4040`**（认领/处置侧对"查无此案"的码）；列表/详情
      是 07 的端点，对外必须给 07 的契约码，否则前端按 `CASE-4001` 写的
      "自动取消选中"分支永远不触发。
    - 读库失败（Mongo 不可用）→ **`CASE-5005`(503)**，与"查无此案"严格分开：
      把依赖故障报成 404 会让审核员得出"这个案子不存在"的结论。
    """
    try:
        data = await service.get_case_detail(case_no)
    except CaseNotFoundError as e:
        raise case_error(
            "CASE-4001", "案件不存在或已归档", {"case_no": str(case_no)}
        ) from e
    except PyMongoError as e:
        raise case_error(
            "CASE-5005", "案件数据暂时不可用，请稍后重试",
            {"case_no": str(case_no), "detail": str(e)},
        ) from e
    return envelope("OK", "查询成功", _trace(request), data)


# 模块编号 "07"（`00_模块划分与边界` §6 / D18），由注册器统一加 `/api/v1` 前缀。
# ⚠️ 每个模块编号**只能登记一次**（重复登记会抛错，见 `app/api/__init__.py`）：
# 08 已在 `case_api.py` 里登记过它自己的 router，这里是**另一个**编号，不冲突。
# 本文件的 import 由 `app/api/__init__.py` 的 `_register_cross_module_routers()`
# 触发（必须排在 `case_api` 之后，见本文件模块 docstring）。
register_router("07", router)


__all__: list[Any] = ["get_query_service", "router"]
