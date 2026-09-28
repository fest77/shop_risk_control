# -*- coding: utf-8 -*-
"""审计接口（模块 12 §3.1 ~ §3.3）。路径相对 `API_PREFIX`。

    GET /audit/logs     审计流水查询（四维筛选 + 分页）
    GET /audit/verify   全链哈希校验（**核心接口**：真实逐条重算）
    GET /audit/export   导出 CSV / Markdown（导出本身也写审计）
    GET /audit/actors   操作人字典（供筛选下拉）

**权限**：全部要求 `audit:read`（仅 admin，BR-12-24）。越权访问由权限依赖统一
拒绝并留痕（`auth.denied`），无需在这里再写一遍。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.api import register_router
from app.deps import client_ip, require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.audit_schema import ACTION_OPTIONS, TARGET_TYPES
from app.security.permissions import P_AUDIT_READ
from app.services import audit_service

router = APIRouter(tags=["审计日志"])


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.get("/audit/logs", summary="审计流水查询")
async def query_logs(
    request: Request,
    actor: Optional[str] = Query(None, max_length=64),
    action: Optional[str] = Query(None, max_length=64, description="支持前缀匹配，如 rule."),
    target_type: Optional[str] = Query(None, max_length=32),
    target_id: Optional[str] = Query(None, max_length=128),
    from_ms: Optional[int] = Query(None, alias="from"),
    to_ms: Optional[int] = Query(None, alias="to"),
    page: int = Query(1),
    page_size: int = Query(20),
    size: Optional[int] = Query(None, description="`page_size` 的别名（模块 12 §3.1 原文用 size）"),
    _user: dict = Depends(require_permission(P_AUDIT_READ)),
):
    """四维筛选 + 分页（BR-12-20：默认 `ts desc`，不允许按 hash 排序）。

    同时接受 `page_size` 与 `size`：模块 00 §3.2 的分页契约用 `page_size`
    （全项目强制），而模块 12 §3.1 写的是 `size`——两个都认就不会有调用方踩空。
    """
    data = await audit_service.get_audit_service().query_logs(
        actor=actor, action=action, target_type=target_type, target_id=target_id,
        from_ms=from_ms, to_ms=to_ms,
        page=page, page_size=size if size is not None else page_size,
    )
    return envelope("OK", "查询成功", _trace(request), data)


@router.get("/audit/verify", summary="全链哈希校验")
async def verify_chain(
    request: Request,
    from_seq: int = Query(0, ge=0, description="从第几条开始（分段校验）"),
    limit: Optional[int] = Query(None, ge=1, le=100_000),
    _user: dict = Depends(require_permission(P_AUDIT_READ)),
):
    """逐条重算哈希并比对，返回首个不一致位置（§3.2）。

    注意响应**永远是 200**：校验接口本身成功了，只是结论可能是"发现篡改"。
    这个结论放在 `data.ok` 与 `data.broken_at` 里，并带 `notice_code`
    （`AUD-5003`/`AUD-5004`）便于日志检索与前端分流。
    """
    data = await audit_service.get_audit_service().verify_chain(from_seq=from_seq, limit=limit)
    message = "全链一致 · 无篡改" if data.get("ok") else (
        "校验未完成（可分段继续）" if data.get("truncated") else "发现篡改"
    )
    return envelope("OK", message, _trace(request), data)


@router.get("/audit/actors", summary="操作人字典")
async def actors(
    request: Request,
    _user: dict = Depends(require_permission(P_AUDIT_READ)),
):
    """筛选下拉的选项来源。同时下发动作与目标类型选项，避免前端硬编码枚举。"""
    return envelope("OK", "ok", _trace(request), {
        "actors": await audit_service.get_audit_service().actors(),
        "actions": list(ACTION_OPTIONS),
        "target_types": list(TARGET_TYPES),
    })


@router.get("/audit/export", summary="导出审计流水", response_class=PlainTextResponse)
async def export_logs(
    request: Request,
    fmt: str = Query("csv", alias="format", pattern="^(csv|markdown)$"),
    actor: Optional[str] = Query(None, max_length=64),
    action: Optional[str] = Query(None, max_length=64),
    target_type: Optional[str] = Query(None, max_length=32),
    target_id: Optional[str] = Query(None, max_length=128),
    from_ms: Optional[int] = Query(None, alias="from"),
    to_ms: Optional[int] = Query(None, alias="to"),
    user: dict = Depends(require_permission(P_AUDIT_READ)),
):
    """导出当前筛选结果（§3.3）。

    **这是统一响应包的唯一例外**：返回的是文件流，不可能再套 `{ok,...}` 信封。
    下载类接口是行业通行的例外，此处显式声明（与 `/` 返回 HTML 同理）。
    """
    filename, content = await audit_service.get_audit_service().export(
        fmt=fmt, actor_filter=actor, action=action, target_type=target_type,
        target_id=target_id, from_ms=from_ms, to_ms=to_ms,
        actor=str(user.get("_id") or user.get("username") or "system"),
        actor_role=str(user.get("role", "")),
    )
    media = "text/csv" if fmt == "csv" else "text/markdown"
    return PlainTextResponse(
        content=content,
        media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Trace-Id": _trace(request),
            "X-Exported-By": f"{user.get('_id')}@{client_ip(request)}",
        },
    )


register_router("12", router)
