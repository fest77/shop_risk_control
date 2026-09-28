# -*- coding: utf-8 -*-
"""名单库 HTTP 入口（模块 06）。路径为**相对 `API_PREFIX`**，前缀由
`app/api/__init__.py` 的注册器统一添加（模块 00 §6 约定）。

**模块 01 接入后的权限变更**：不再读 `X-Operator`/`X-Role`，改为按权限矩阵校验
（`01 §2.2` + 15_决策记录 D26）：

| 接口 | 权限 | 允许角色 |
|---|---|---|
| `GET /lists` | `list:read` | 三角色均可（BR-06-35：审核员可只读查看） |
| `GET /lists/impact` | `list:read` | 三角色均可 |
| `GET /lists/scene-usage` | `list:read` | 三角色均可 |
| `GET /lists/import-template` | `list:read` | 三角色均可（模板是静态文案，不含数据） |
| `POST /lists` | `list:write` | **仅 strategist** |
| `DELETE /lists/{entry_id}` | `list:write` | **仅 strategist** |
| `POST /lists/import` | `list:write` | **仅 strategist** |

> 与模块 06 `BR-06-34`（"strategist 与 admin 可写"）的冲突已按 BR-01-12
> 「§2.2 为唯一真源」裁定为**仅 strategist**，该条正文已同步更正。
> 越权一律由权限层给 `AUTH-4020`，本模块**不自建角色判断**（决策 D28）。
"""
from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse

from app.api import register_router
from app.core.degraded import DEGRADED
from app.db import get_db
from app.deps import client_ip, require_permission
from app.errors import AppError, envelope
from app.logging import get_trace_id
from app.repos.list_repo import ListRepo
from app.schemas.list_schema import (
    IMPORT_HEADER,
    IMPORT_MAX_ROWS,
    IMPORT_MODES,
    ListEntryCreate,
    parse_import_list_type_default,
)
from app.security.permissions import P_LIST_READ, P_LIST_WRITE
from app.services.list_service import ListService

router = APIRouter(tags=["名单库"])

# 导入文件大小上限（模块 06 §3.2：≤5MB）。取 5MB 而不是"不限制"是因为
# 解析发生在**内存里**（标准库 csv）：不设限等于允许一个请求把进程内存打满。
IMPORT_MAX_BYTES = 5 * 1024 * 1024

# 模板文件名：写死而不带时间戳，便于用户重复下载后覆盖同一个本地文件
TEMPLATE_FILENAME = "list_import_template.csv"


def get_list_service() -> ListService:
    return ListService(ListRepo(get_db()))


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


def _actor(user: dict) -> str:
    """操作人取自**令牌身份**而非请求体：由客户端自报操作人等于让审计失去意义。"""
    return str(user.get("_id") or user.get("username") or "unknown")


def _actor_role(user: dict) -> str:
    return str(user.get("role") or "")


@router.get("/lists", summary="名单列表")
async def list_lists(
    request: Request,
    list_type: str = Query(..., description="black / white / gray（必填）"),
    entity_type: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None, max_length=128),
    status: str = Query("active", description="active / expired / removed / all"),
    expire_before: Optional[int] = Query(None, description="毫秒时间戳"),
    page: int = Query(1, description="≥1；越界由服务层判为 CFG-4008"),
    page_size: int = Query(20, description="1~100；越界由服务层判为 CFG-4008"),
    sort: str = Query("effective_at:desc"),
    service: ListService = Depends(get_list_service),
    _user: dict = Depends(require_permission(P_LIST_READ)),
):
    result = await service.list_entries(
        list_type=list_type,
        entity_type=entity_type,
        keyword=keyword,
        status=status,
        expire_before=expire_before,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return envelope("OK", "查询成功", _trace(request), result.model_dump(by_alias=True))


@router.post("/lists", status_code=201, summary="新增名单条目")
async def create_list(
    request: Request,
    payload: ListEntryCreate,
    service: ListService = Depends(get_list_service),
    user: dict = Depends(require_permission(P_LIST_WRITE)),
):
    # 操作人取自**令牌身份**而非请求体：由客户端自报操作人等于让审计失去意义。
    # BR-06-36：写操作必须留痕，角色/IP/UA 由接口层提供——服务层不依赖 Request，
    # 这是"服务可用假仓储直接单测"的前提
    entry = await service.create_entry(
        payload, operator=_actor(user),
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope("OK", "新增成功", _trace(request), entry.model_dump(by_alias=True))


@router.get("/lists/scene-usage", summary="名单降级状态")
async def list_scene_usage(
    request: Request,
    _user: dict = Depends(require_permission(P_LIST_READ)),
):
    return envelope("OK", "ok", _trace(request), DEGRADED.snapshot())


@router.get("/lists/impact", summary="某实体的在效名单条数（移除确认影响面）")
async def list_impact(
    request: Request,
    entity_type: str = Query(...),
    entity_value: str = Query(..., max_length=128),
    service: ListService = Depends(get_list_service),
    _user: dict = Depends(require_permission(P_LIST_READ)),
):
    n = await service.impact_of_entity(entity_type, entity_value)
    return envelope("OK", "ok", _trace(request), {"active_count": n})


@router.get("/lists/import-template", summary="名单导入模板下载")
async def list_import_template(
    request: Request,
    _user: dict = Depends(require_permission(P_LIST_READ)),
):
    """下载导入模板（模块 06 §3.2，BR-06-29）。

    **这是统一响应包的第二处例外**（第一处是审计导出）：返回的是文件流，
    不可能再套 `{ok, code, ...}` 信封，否则用户拿到的"CSV"会是包着信封的 JSON。

    表头由 `IMPORT_HEADER` 生成，与 `ListService` 的解析共用同一常量——
    "模板给的表头"与"导入认的表头"因此不可能漂移（CFG-4012 最常见的成因）。
    第三行是 CSV 注解行（整行放在引号里，因此是**一个**单元格），承载枚举取值
    与上限说明。它刻意**不做特殊忽略**：用户若直接拿这个文件去导入，该行会以
    "list_type 非法"被明确报出，而不是被静默丢掉——静默忽略会让用户以为
    "模板自带的行本来就会被跳过"，从而对真正填错的行也抱同样预期。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(IMPORT_HEADER))
    writer.writerow([
        "black", "phone", "13900000001", "批量套券，人工确认", "",
    ])
    writer.writerow([
        f"# list_type ∈ {'/'.join(('black', 'white', 'gray'))}；"
        f"entity_type ∈ user/phone/ip/device/address；"
        f"expire_at 为毫秒时间戳，留空 = 按名单类型取默认值（灰名单 30 天，黑白永久）；"
        f"单次最多 {IMPORT_MAX_ROWS} 行；删除本行后再导入",
        "", "", "", "",
    ])
    return PlainTextResponse(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"',
            "X-Trace-Id": _trace(request),
        },
    )


@router.post("/lists/import", summary="名单批量导入")
async def import_lists(
    request: Request,
    file: UploadFile = File(..., description="CSV 文件，≤5MB，≤5000 行"),
    mode: str = Form("partial", description="partial（默认）/ atomic"),
    force: bool = Form(False, description="true 时按 BR-06-20 覆盖异名单冲突"),
    list_type: Optional[str] = Form(None, description="行内 list_type 留空时的默认值"),
    service: ListService = Depends(get_list_service),
    user: dict = Depends(require_permission(P_LIST_WRITE)),
):
    """批量导入（BR-06-28 / 29 / 30）。

    文件规模与格式校验放在接口层（HTTP 语义），行业务判定放在服务层：
    - 超过 5MB 属传输层拒绝，用 413（模块 06 §3.2 明列该状态码）。
      若交给服务层，就要先把整个文件读进内存才能判断大小，等于先中招再拦截。
    - `mode` 非法属表单参数错误，用 422 —— 复用 01 层字段校验的语义；
      服务层还会再判一次，保证不走 HTTP 也能正确拒绝。
    """
    raw = await file.read()
    if len(raw) > IMPORT_MAX_BYTES:
        raise AppError(
            "COM-4000",
            f"文件过大（{len(raw) // 1024}KB），上限 {IMPORT_MAX_BYTES // 1024 // 1024}MB",
            413,
        )
    # 表单级参数先校验：模式写错没必要把文件解析一遍
    if mode not in IMPORT_MODES:
        raise AppError(
            "COM-4001", f"mode 仅支持 {'/'.join(IMPORT_MODES)}，收到：{mode}", 422
        )
    # 默认名单类型同样在这里校验（非空但非法时立即拒绝，不逐行重复同一原因）
    parse_import_list_type_default(list_type)

    result = await service.import_entries(
        raw,
        mode=mode,
        force=force,
        default_list_type=list_type,
        operator=_actor(user),
        actor_role=_actor_role(user),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope(
        "OK",
        f"导入完成：共 {result.total} 行，成功 {result.success} 行，失败 {result.failed} 行",
        _trace(request),
        result.model_dump(),
    )


@router.delete("/lists/{entry_id}", summary="移除名单条目（软删）")
async def remove_list(
    request: Request,
    entry_id: str,
    service: ListService = Depends(get_list_service),
    user: dict = Depends(require_permission(P_LIST_WRITE)),
):
    """移除名单条目（模块 06 §3.2，BR-06-25 / 26 / 27 / 36）。

    **服务端不做"二次确认"**：确认是页面的交互职责（BR-06-26 要求弹窗展示
    实体值/名单类型/来源/关联案件数与影响面提示），后端无法、也不该替用户确认。
    后端负责的是确认之后的**原子性**：软删 + 失效缓存 + 审计三步要么都成，
    要么回滚成原样（见 `ListService.remove_entry`）。
    """
    result = await service.remove_entry(
        entry_id,
        _actor(user),
        actor_role=_actor_role(user),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope("OK", "已移除", _trace(request), result.model_dump(by_alias=True))


register_router("06", router)
