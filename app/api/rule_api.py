# -*- coding: utf-8 -*-
"""规则配置 HTTP 入口（模块 06-B）。路径为**相对 `API_PREFIX`**，前缀由
`app/api/__init__.py` 的注册器统一添加（模块 00 §6 约定）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/rules` | `rule:write` | 规则列表（分页/筛选/场景分值提示） |
| GET | `/rules/{rule_code}` | `rule:write` | 单条规则（编辑抽屉刷新 / 冲突后重取） |
| GET | `/rules/{rule_code}/impact` | `rule:write` | 删除确认的影响面（§5.2） |
| POST | `/rules` | `rule:write` | 新建（支持 `Idempotency-Key`） |
| PUT | `/rules/{rule_code}` | `rule:write` | 修改（乐观锁 `expected_version`） |
| POST | `/rules/{rule_code}/toggle` | `rule:write` | 启停用（幂等） |
| DELETE | `/rules/{rule_code}` | `rule:write` | 软删除（内置规则拒绝） |
| POST | `/rules/validate-tree` | `rule:write` | 条件树校验（**转发 05**） |
| POST | `/rules/import` | `rule:write` | 批量导入（CSV，partial/atomic） |
| GET | `/rules/import-template` | `rule:write` | 导入模板下载（CSV 附件） |

## 权限：为什么读与写都用 `rule:write`

任务裁定「读操作用矩阵里相应的读权限，不得新造权限码」。读了
`app/security/permissions.py` 的矩阵后，**规则侧只有 `rule:write` 一个权限**
（`P_RULE_WRITE`，仅 strategist），没有 `rule:read`：

- `MENU_PERMISSIONS["#/rules"] = P_RULE_WRITE`——矩阵自己就把「能进这个页面」
  定义为 strategist，因此页面里的读与写共用同一权限，语义上是自洽的；
- 名单侧不同：`P_LIST_READ` 对三个角色开放（BR-06-35 的"审核员可只读查看名单"），
  所以名单的读与写是两个权限。规则侧没有对应的只读权限。

**如实登记的一处冲突**：Spec 06 `BR-06-35` 写「reviewer 进入策略与规则页时
三个 tab 均只读渲染」，而权限矩阵把 `#/rules` 与 `rule:write` 绑定、reviewer
既看不到菜单也调不通接口。这不是本模块能自己解决的问题——按 BR-01-12
「§2.2（矩阵）为唯一真源」，要么认可"规则页对 reviewer 不可见"，要么由**模块 01**
在矩阵里新增 `rule:read` 并把 `#/rules` 的菜单权限改过去。本模块**不自造权限码**，
按现状实现。

## 越权一律由权限层给 `AUTH-4020`

Spec §5.1 的 `CFG-4031` 已作废（决策 D28：权限拒绝由模块 01 的权限层统一给出）。
因此本文件里**没有一处**角色判断，只有 `require_permission(...)`。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import PlainTextResponse

from app.api import register_router
from app.db import get_db
from app.deps import client_ip, require_permission
from app.errors import AppError, envelope
from app.logging import get_trace_id
from app.repos.rule_repo import RuleAdminRepo
from app.schemas.rule_schema import (
    RULE_IMPORT_HEADER,
    RULE_IMPORT_MAX_ROWS,
    RULE_IMPORT_MODES,
    RULE_KEYWORD_MAX,
    RuleCreate,
    RuleToggleIn,
    RuleUpdate,
    ValidateTreeIn,
)
from app.security.permissions import P_RULE_WRITE
from app.services.rule_service import RuleService, check_condition, ensure_no_immutable_fields

router = APIRouter(tags=["规则配置"])

#: `Idempotency-Key` 的长度上限：它只做内存键，不落库；但无上限的请求头
#: 会变成一个"每次请求都新建一条永不命中的缓存项"的内存放大手段。
IDEMPOTENCY_KEY_MAX = 128

#: 规则导入的文件大小上限（Spec §3.1：≤2MB）。解析发生在**内存里**（标准库 csv），
#: 不设限等于允许一个请求把进程内存打满。
IMPORT_MAX_BYTES = 2 * 1024 * 1024

#: 模板文件名：写死而不带时间戳，便于用户重复下载后覆盖同一个本地文件
TEMPLATE_FILENAME = "rule_import_template.csv"

#: 模板里的示例条件树（与 `scripts/seed.py` 的真实特征键一致，用户可直接照改）
RULE_IMPORT_EXAMPLE_TREE = {"logic": "and", "children": [
    {"field": "device_user_cnt", "op": "gte", "value": 5},
]}


def get_rule_service() -> RuleService:
    return RuleService(RuleAdminRepo(get_db()))


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


def _actor(user: dict) -> str:
    """操作人取自**令牌身份**而非请求体：客户端自报操作人等于让审计失去意义。"""
    return str(user.get("_id") or user.get("username") or "unknown")


async def _reject_immutable_fields(request: Request) -> None:
    """CFG-4007：请求体里出现不可变字段即拒绝（Spec §3.1 PUT 的 400）。

    为什么要读原始 JSON 而不是靠 Pydantic：`extra="forbid"` 会把 `_id` /
    `is_system` 判成 `COM-4001`（422），而契约要求 `400 CFG-4007`；而"忽略掉"
    更糟——调用方提交 `is_system=true` 却收到成功，会以为自己造出了内置规则。

    体解析失败时**不在这里报错**：那种情况下 FastAPI 已经/将会用
    `COM-4000`/`COM-4001` 拒绝该请求，本函数只需静默放行，避免把"报文不是
    JSON"错报成"你改了不可变字段"。
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001 - 报文本身不合法，交给模块 00 的处理器
        return
    ensure_no_immutable_fields(raw)


# ============================================================
# 校验与查询（**必须声明在 `/rules/{rule_code}` 之前**）
# ============================================================
@router.post("/rules/validate-tree", summary="条件树校验（转发 05 的 validate_tree）")
async def validate_rule_tree(
    request: Request,
    payload: ValidateTreeIn,
    _user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """条件树结构与枚举合法性校验（Spec §3.1 / BR-06-17 / BR-06-19）。

    - **不实现任何校验逻辑**：直接调用 05 的 `validate_tree()`（见
      `rule_service.validate_condition`），把结论原样包装返回；
    - **只校验结构，不求值**（BR-06-19）：不判断规则是否命中、不做特征比对；
    - **校验失败也是 200**，靠 `valid=false` 表达：这是"校验请求成功了，
      结论是这棵树不合法"，用 4xx 表达会让前端的错误处理把"树不合法"
      当成"接口挂了"，从而不敢按 `path` 回显到节点行。
    """
    result = check_condition(payload.condition)
    message = "条件树合法" if result.valid else f"条件树有 {len(result.errors)} 处问题"
    return envelope("OK", message, _trace(request), result.model_dump())


@router.get("/rules/import-template", summary="规则导入模板下载")
async def rule_import_template(
    request: Request,
    _user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """下载导入模板（CSV 附件）。

    **这是统一响应包的第二处例外**（第一处是名单模板）：返回的是文件流，
    不可能再套 `{ok, code, ...}` 信封，否则用户拿到的"CSV"会是包着信封的 JSON。

    表头由 `RULE_IMPORT_HEADER` 生成，与 `RuleService` 的解析共用同一常量——
    "模板给的表头"与"导入认的表头"因此不可能漂移（`CFG-4012` 最常见的成因）。

    ⚠️ **本路由必须声明在 `/rules/{rule_code}` 之前**：否则 `import-template`
    会被参数化路由吃掉，用户点"下载模板"会得到 `404 CFG-4001`（"规则不存在"）。
    这条顺序由 `tests/test_rule_crud.py::test_import_template_is_not_shadowed_by_rule_code`
    钉住。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(RULE_IMPORT_HEADER))
    writer.writerow([
        "", "批量导入示例规则", "login", "由导入创建（编码留空则由服务端生成）",
        json.dumps(RULE_IMPORT_EXAMPLE_TREE, ensure_ascii=False), "30", "10", "disabled",
    ])
    writer.writerow([
        f"# rule_code 留空 = 服务端按 BR-06-01 生成（R{{场景码}}{{3位序号}}）；"
        f"condition 填条件树 JSON；score ∈ 0~100；status ∈ enabled/disabled；"
        f"单次最多 {RULE_IMPORT_MAX_ROWS} 行；删除本行后再导入",
        "", "", "", "", "", "", "",
    ])
    return PlainTextResponse(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"',
            "X-Trace-Id": _trace(request),
        },
    )


@router.post("/rules/import", summary="规则批量导入")
async def import_rules(
    request: Request,
    file: UploadFile = File(..., description="CSV 文件，≤2MB，≤500 行"),
    mode: str = Form("partial", description="partial（默认）/ atomic"),
    service: RuleService = Depends(get_rule_service),
    user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """批量导入（Spec §3.1）。

    文件规模与格式校验放在接口层（HTTP 语义），行业务判定放在服务层：
    - 超过 2MB 属传输层拒绝，用 413；若交给服务层，就要先把整个文件读进内存
      才能判断大小，等于"先中招再拦截"；
    - `mode` 非法属表单参数错误，用 422（模块 01 层字段校验的语义），
      服务层还会再判一次，保证不走 HTTP 也能正确拒绝。

    **XLSX 明确不支持**（Spec 写的是"CSV / XLSX"）：开放 xlsx 需要引入
    `openpyxl` 依赖，而本模块的约定是"不引入新依赖"；模板是 CSV，
    用户用 Excel 另存为 CSV 即可。这一点与 06-A 的名单导入完全一致，
    已在交付报告中登记为偏离项而非遗漏。
    """
    raw = await file.read()
    if len(raw) > IMPORT_MAX_BYTES:
        raise AppError(
            "COM-4000",
            f"文件过大（{len(raw) // 1024}KB），上限 {IMPORT_MAX_BYTES // 1024 // 1024}MB",
            413,
        )
    if mode not in RULE_IMPORT_MODES:
        raise AppError(
            "COM-4001", f"mode 仅支持 {'/'.join(RULE_IMPORT_MODES)}，收到：{mode}", 422
        )

    result = await service.import_rules(
        raw,
        mode=mode,
        operator=_actor(user),
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope(
        "OK",
        f"导入完成：共 {result.total} 行，成功 {result.success} 行，失败 {result.failed} 行",
        _trace(request),
        result.model_dump(),
    )


@router.get("/rules", summary="规则列表")
async def list_rules(
    request: Request,
    scene_code: Optional[str] = Query(None, max_length=32),
    status: Optional[str] = Query(None, description="enabled / disabled"),
    keyword: Optional[str] = Query(None, max_length=RULE_KEYWORD_MAX),
    page: int = Query(1, description="≥1；越界由服务层判为 CFG-4008"),
    page_size: int = Query(20, description="1~100；越界由服务层判为 CFG-4008"),
    sort: str = Query("priority:asc,_id:asc", description="默认 priority:asc,_id:asc"),
    service: RuleService = Depends(get_rule_service),
    _user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    result = await service.list_rules(
        scene_code=scene_code, status=status, keyword=keyword,
        page=page, page_size=page_size, sort=sort,
    )
    return envelope("OK", "查询成功", _trace(request), result.model_dump(by_alias=True))


@router.get("/rules/{rule_code}", summary="单条规则")
async def get_rule(
    request: Request,
    rule_code: str,
    service: RuleService = Depends(get_rule_service),
    _user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """取单条规则（404 `CFG-4001`）。

    列表接口已带全量字段，本接口的价值在**乐观锁冲突之后**：前端拿到
    `409 CFG-4006` 后需要按提示"刷新后重试"，刷新一条比重新拉一页更精确。
    """
    doc = await service.get_rule(rule_code)
    return envelope("OK", "ok", _trace(request), doc.model_dump(by_alias=True))


@router.get("/rules/{rule_code}/impact", summary="规则删除的影响面（近 30 天命中次数）")
async def rule_impact(
    request: Request,
    rule_code: str,
    service: RuleService = Depends(get_rule_service),
    _user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """删除确认弹窗要展示的影响面（Spec §5.2 第 2 行）。

    后端不做二次确认（那是页面的交互职责），但**必须能提供确认所需的数字**：
    "近 30 天命中次数"无法由前端从任何现有接口算出来。
    """
    result = await service.impact(rule_code)
    return envelope("OK", "ok", _trace(request), result.model_dump())


# ============================================================
# 写
# ============================================================
@router.post("/rules", status_code=201, summary="新建规则")
async def create_rule(
    request: Request,
    payload: RuleCreate,
    idempotency_key: Optional[str] = Header(
        None, alias="Idempotency-Key", max_length=IDEMPOTENCY_KEY_MAX,
        description="同一次保存重试复用同一个键，重复键返回首次结果",
    ),
    service: RuleService = Depends(get_rule_service),
    user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """新建规则（Spec §3.1）。

    规则编码由**服务端生成**（BR-06-01，前端不填）；`version` 初始为 1、
    `status` 默认 `disabled`（BR-06-04：上线前先仿真）。
    """
    await _reject_immutable_fields(request)
    rule, replayed = await service.create_rule(
        payload, _actor(user),
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
        idempotency_key=idempotency_key,
    )
    message = "该保存已受理过，返回首次结果" if replayed else "创建成功"
    return envelope("OK", message, _trace(request), rule.model_dump(by_alias=True))


@router.put("/rules/{rule_code}", summary="修改规则")
async def update_rule(
    request: Request,
    rule_code: str,
    payload: RuleUpdate,
    service: RuleService = Depends(get_rule_service),
    user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """修改规则（Spec §3.1）。必须携带 `expected_version`（BR-06-05）。

    成功即 `version + 1` 并写一条 `rule.update` 审计（含 `before` / `after`）；
    审计写不进去则回滚本次修改并返回 `CFG-5003`（BR-06-36）。
    """
    await _reject_immutable_fields(request)
    rule = await service.update_rule(
        rule_code, payload, _actor(user),
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope("OK", "保存成功", _trace(request), rule.model_dump(by_alias=True))


@router.post("/rules/{rule_code}/toggle", summary="启用 / 停用规则")
async def toggle_rule(
    request: Request,
    rule_code: str,
    payload: RuleToggleIn,
    service: RuleService = Depends(get_rule_service),
    user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """启停用（Spec §3.1，**幂等**）。

    目标状态与当前一致时：`version` 不变、不写审计、缓存不失效，
    返回 `changed=false`（V-06-20）。响应体里的 `changed` 是前端判断
    "这次点击到底有没有生效"的唯一依据，因此必须显式回传。
    """
    rule, changed = await service.toggle_rule(
        rule_code, payload, _actor(user),
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    data = rule.model_dump(by_alias=True)
    data["changed"] = changed
    message = "状态已更新" if changed else "状态未变化（幂等，未产生新版本与审计）"
    return envelope("OK", message, _trace(request), data)


@router.delete("/rules/{rule_code}", summary="删除规则（软删）")
async def delete_rule(
    request: Request,
    rule_code: str,
    expected_version: Optional[int] = Query(
        None, description="可选：携带则严格乐观锁（BR-06-05），不匹配返回 CFG-4006",
    ),
    service: RuleService = Depends(get_rule_service),
    user: dict = Depends(require_permission(P_RULE_WRITE)),
):
    """软删除（BR-06-08 / 10）。

    - `is_system=true` 一律 `400 CFG-4005`（内置规则只能停用）；
    - 物理文档**保留**、`status` 置 `disabled` 并打删除标记：历史
      `decision_hits.rule_code` 与 `decisions.rule_versions` 必须仍能回溯；
    - 删除前的影响面由 `GET /rules/{rule_code}/impact` 提供（§5.2）。
    """
    result = await service.delete_rule(
        rule_code, _actor(user),
        expected_version=expected_version,
        actor_role=str(user.get("role", "")),
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope("OK", "已删除（软删，历史命中明细不受影响）",
                    _trace(request), result.model_dump())


# 模块 06 的路由**在 `list_api` 中登记**（`register_router("06", ...)` 只允许一次）。
# 这里用 `merge=True` 把规则路由并入同一个模块槽位：保持"一个模块一个编号 + 一条
# 前缀"的不变式，同时让每个 api 模块仍然自己声明路由（与其余 13 个模块的写法一致）。
register_router("06", router, merge=True)
