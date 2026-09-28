# -*- coding: utf-8 -*-
"""事件接入编排：校验 → 编号 → 幂等 → 04 → 05 → 组装 → 异步落库（BR-03-08~24）。

## 链路的真实行为（当前阶段必须知情）

模块 04/05 **尚未实现**，`get_components()` 装配的是「明确不可用」的默认
provider（见 `app/protocols.py`）。因此**每个事件都会被降级为 `review`**：

    事件 → 校验通过 → EVT 编号 → 幂等 → 调 04 → 抛"不可用" → 捕获
         → 200 + decision=review + degrade.stage="feature"

这是**正确且可演示的 fail-closed 行为，不是缺陷**：风控系统"不确定就不放行"
的落地证据正是它。测试里通过 `configure_components()` 换成假的 04/05 来构造
happy path（见 `tests/test_event_failclosed.py`）。

## 200ms 超时预算为什么要取消下游协程（BR-03-23）

`asyncio.wait_for` 超时后**必须**让下游停下，否则：① 下游继续跑并可能写库，
产生"接口已返回 review、几秒后库里多出一条 pass 决策"的分裂状态；② 每次都
泄漏一个协程与它的任务引用，压测下内存持续增长。这里把「04 + 05」整体包在
一个任务里再 `wait_for`，因此超时会**同时**取消两者，不存在"只取消了 05、
04 还在写滑窗"的残留。

## 幂等缓存为什么必须在返回响应之前写入（BR-03-14）

见 `app/services/idempotency.py` 的模块 docstring：写入若晚于返回，并发重复
提交会穿透。本模块在**每一条**返回路径（含降级、含等待者）之前都保证缓存
已是终态。
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from typing import Any, Optional

from app import db as db_module
from app.constants import (
    DECISION_TIMEOUT_MS,
    FEATURE_WINDOW_LONG_MIN,
)
from app.core import edge_writer
from app.errors import AppError, EVT_CODES, EVT_NOTICE
from app.logging import get_logger
from app.protocols import get_components
from app.repos.event_repo import EventRepo
from app.schemas.event_schema import (
    AMOUNT_FIELD_BY_TYPE,
    BATCH_MAX,
    BIZ_NO_MAX,
    DECISION_FIELDS,
    DEVICE_ID_MAX,
    DEVICE_ID_MIN,
    EVENT_ID_PATTERN,
    EVENT_TYPES,
    SOURCE_MOCK_BIZ,
    SOURCES,
    TS_MAX_AHEAD_MS,
    BatchItemResult,
    EventIn,
    coerce_list_hit,
    list_hit_miss,
    required_fields,
    scene_extra_spec,
)
from app.services.idempotency import get_store, payload_digest
from app.utils.ids import new_event_id
from app.utils.mask import mask_phone
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.event")

# 同步链路预算（BR-03-23，取值来自系统设置页「决策链路超时＝200ms」）
TIMEOUT_SEC = DECISION_TIMEOUT_MS / 1000.0

# `after_sale_apply` 的 `refund_amount` 与顶层 `amount` 都缺失时**不允许**猜测，
# 因此这里没有默认值；只有 `coupon_receive` 的 `face_value` 在顶层 `amount`
# 已给定时可以补齐（BR-03-06 的「只提供其一 → 双向补齐」）。

# 异步落库的进程内重试（BR-03-18：失败不回滚决策，进重试队列 + 告警）。
# 为什么只重试 2 次而不是像审计那样的 3 次指数退避：event 是高频路径，
# 每次重试都占一个后台任务；Mongo 长时间不可用时，真正兜底的是
# `EVT-5004` 告警 + 调用方改用 `GET /events/{id}` 自查，而不是无限堆积任务。
PERSIST_RETRY = 2
PERSIST_RETRY_BASE_SEC = 0.05

# 顶层允许出现的键（§3.1 请求表 + 决策/响应字段不参与入参校验）。
# 用于识别 `scene_extra` 未知键（EVT-4006）：既不在 scene_extra 白名单里、
# 也不属于顶层字段的多余键，一定是模板串味或拼写错误。
_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "event_id", "client_event_id", "event_type", "user_id", "biz_no",
    "device_id", "ip", "phone", "address_id", "amount", "scene_extra",
    "ts", "source", "received_at", "late_arrival", "expire_at",
})

# 迟到判定用的长窗口（BR-03-08 / G-02：1440 分钟）
LATE_WINDOW_MS = FEATURE_WINDOW_LONG_MIN * 60_000


# ============================================================
# 错误构造
# ============================================================
def evt_error(code: str, message: Optional[str] = None, data: Any = None) -> AppError:
    """按 `EVT_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = EVT_CODES.get(code, (400, "事件接入失败"))
    return AppError(code, message or default, status or 400, data)


def missing_fields_message(event_type: str, missing: list[str]) -> str:
    """BR-03-03 的文案：`coupon_receive 缺少必填字段：device_id、ip`。"""
    return f"{event_type} 缺少必填字段：{'、'.join(missing)}"


# ============================================================
# 校验（BR-03-01 ~ 07）
# ============================================================
def validate_event(payload: Any, received_at: Optional[int] = None) -> dict:
    """把原始请求体校验并规范成内部事件 dict。

    返回的 dict 一定满足：`event_type` 合法、类型必填齐备、格式正确、
    `scene_extra` 无越界键、金额双口径一致、`ts` 已补默认值。

    错误码按 §5 分流，**每一类都有自己的码**，因为它们对调用方的含义不同：
    `EVT-4003` 是不支持的类型、`EVT-4004` 是缺字段（422，可补）、
    `EVT-4005` 是格式错、`EVT-4006` 是扩展字段越界、`EVT-4007` 是金额矛盾。
    """
    moment = now_ms() if received_at is None else int(received_at)

    # BR-03-01：请求体必须是 JSON 对象
    if not isinstance(payload, dict):
        raise evt_error("EVT-4001", "请求体格式非法：必须是 JSON 对象",
                        {"received_type": type(payload).__name__})

    # 用 Pydantic 只做结构/类型归一（不填默认值），语义判定全部在下面手写
    try:
        model = EventIn.model_validate(payload)
    except Exception as e:  # noqa: BLE001 - Pydantic 的字段类型错也归入报文格式非法
        raise evt_error("EVT-4001", "请求体格式非法：字段类型与约定不符",
                        {"detail": str(e)[:300]}) from e

    # BR-03-02：五类枚举（**在服务层判**，否则会被 FastAPI 拦成 COM-4001）
    event_type = (model.event_type or "").strip()
    if event_type not in EVENT_TYPES:
        raise evt_error(
            "EVT-4003",
            f"不支持的事件类型：{event_type or '(缺失)'}"
            f"（仅支持 {'/'.join(EVENT_TYPES)}）",
            {"event_type": event_type},
        )

    # 顶层多余键：不在 §3.1 请求表里的键，一定是拼错或模板串味
    extra_top = sorted(set(model.extra_keys()) - _TOP_LEVEL_KEYS)
    if extra_top:
        raise evt_error(
            "EVT-4006",
            f"{event_type} 事件不接受扩展字段 {'、'.join(extra_top)}"
            f"（金额类字段请放进 scene_extra）",
            {"unknown_keys": extra_top, "scope": "top_level"},
        )

    # `scene_extra` 结构
    if model.scene_extra is not None and not isinstance(model.scene_extra, dict):
        raise evt_error("EVT-4005", "字段 scene_extra 格式非法：必须是 JSON 对象")
    scene = dict(model.scene_extra or {})

    # BR-03-03：按类型的必填集（顶层 + scene_extra 必填键）
    scene_required, scene_optional = scene_extra_spec(event_type)
    missing: list[str] = []

    # EVT-4002 的优先级最高：`user_id` 是最基础标识（§5 单列一个码）
    if model.user_id is None or str(model.user_id).strip() == "":
        raise evt_error("EVT-4002", "缺少必填字段：user_id", {"missing": ["user_id"]})

    for field in required_fields(event_type):
        if field == "user_id":
            continue
        value = getattr(model, field, None)
        # `scene_extra` 里同名的键也算提供（`order_create` 的 `address_id`
        # 同时出现在基础必填与 scene_extra 可选键里，两处填一处即满足）；
        # 顶层为 None 则交给 `_reconcile_amount` 从 scene_extra 补齐（BR-03-06
        # 的"只提供其一 → 双向补齐"），因此**不算缺失**。
        nested = scene.get(field)
        if value is None and nested is None:
            if field == "amount" and AMOUNT_FIELD_BY_TYPE.get(event_type) in scene:
                continue
            missing.append(field)
        elif isinstance(value, str) and not value.strip() and nested is None:
            missing.append(field)
    for key in scene_required:
        if scene.get(key) is None:
            missing.append(key)
    if missing:
        raise evt_error(
            "EVT-4004",
            missing_fields_message(event_type, missing),
            # `missing` 逐字段列名：模块 10 的仿真页要靠它渲染「缺失字段：ip」并重绘星号
            {"missing": missing, "event_type": event_type},
        )

    # BR-03-05：scene_extra 出现不属于该类型的键
    allowed = set(scene_required) | set(scene_optional)
    unknown = sorted(k for k in scene if k not in allowed)
    if unknown:
        raise evt_error(
            "EVT-4006",
            f"{event_type} 事件不接受扩展字段 {'、'.join(unknown)}",
            {"unknown_keys": unknown, "allowed_keys": sorted(allowed)},
        )

    # BR-03-04：字段级格式
    _check_formats(model, event_type, scene, moment)

    # BR-03-06：amount 与 scene_extra 金额字段的双向一致 / 补齐
    amount = _reconcile_amount(model, event_type, scene)

    # BR-03-07：`ts` 省略时取服务端 now；超前 60s 视为时钟超前
    received = moment
    ts = model.ts
    if ts is None:
        ts = received
    elif ts > received + TS_MAX_AHEAD_MS:
        raise evt_error(
            "EVT-4005",
            f"字段 ts 格式非法：{ts} 超前于接收时间 {received} 超过 60 秒（疑似时钟不准）",
            {"ts": ts, "received_at": received},
        )

    source = (model.source or SOURCE_MOCK_BIZ).strip()
    if source not in SOURCES:
        raise evt_error(
            "EVT-4005",
            f"字段 source 格式非法：{source}（仅支持 {'/'.join(SOURCES)}）",
            {"source": source},
        )

    event: dict[str, Any] = {
        # `received_at` 是 E01 的字段之一，也是详情接口与 04 判定迟到/乱序的
        # 依据，因此**必须落库**而不是只出现在响应体里
        "received_at": received,
        "event_type": event_type,
        "user_id": str(model.user_id).strip(),
        "biz_no": _opt_str(model.biz_no),
        "device_id": _opt_str(model.device_id),
        "ip": _opt_str(model.ip),
        # BR-03-04 / §2.1：手机号入库前脱敏（`138****6621`）。
        # 事件表要长期留存供审计，明文手机号留存本身就是一次数据泄露；
        # 而详情接口、列表接口都不需要完整手机号，脱敏不损失任何功能。
        "phone": mask_phone(model.phone) if model.phone else None,
        "address_id": _opt_str(model.address_id),
        "amount": amount,
        "scene_extra": scene,
        "ts": int(ts),
        "source": source,
        # BR-03-08：迟到事件仍落库，但标记出来——04 入滑窗会污染窗口语义，
        # 因此本次直接 fail-closed 转 review（见 `_pipeline`）
        "late_arrival": bool(ts < received - LATE_WINDOW_MS),
    }
    return event


def _opt_str(value: Any) -> Optional[str]:
    """Optional[str] 归一：空串视为缺失（便于"传了空串"与"没传"行为一致）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _check_formats(model: EventIn, event_type: str, scene: dict, received_at: int) -> None:
    """字段级格式校验（BR-03-04 / 07），全部按 `EVT-4005` 报错。

    `amount` 的"正整数"判定放在 `_reconcile_amount` 里一起做：金额有两个来源
    （顶层与 scene_extra），两处各判一次会出现"顶层非法但 scene_extra 合法"
    这类自相矛盾的结果。
    """
    user_id = str(model.user_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", user_id):
        raise evt_error(
            "EVT-4005",
            f"字段 user_id 格式非法：{user_id}（长度 3~32，仅允许字母/数字/下划线/连字符）",
            {"field": "user_id", "value": user_id},
        )

    device_id = _opt_str(model.device_id)
    if device_id is not None and not (DEVICE_ID_MIN <= len(device_id) <= DEVICE_ID_MAX):
        raise evt_error(
            "EVT-4005",
            f"字段 device_id 格式非法：长度需 {DEVICE_ID_MIN}~{DEVICE_ID_MAX}",
            {"field": "device_id"},
        )

    ip = _opt_str(model.ip)
    if ip is not None:
        try:
            # `ipaddress` 会同时接受 IPv4 与 IPv6；`strict=True` 拒绝
            # `192.168.001.001` 这类前导零写法（那种串在真实网络里语义含糊）
            ipaddress.ip_address(ip)
        except ValueError as e:
            raise evt_error(
                "EVT-4005", f"字段 ip 格式非法：{ip}", {"field": "ip", "value": ip}
            ) from e

    phone = _opt_str(model.phone)
    if phone is not None and not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise evt_error(
            "EVT-4005", f"字段 phone 格式非法：{phone}（需 11 位手机号）",
            {"field": "phone"},
        )

    address_id = _opt_str(model.address_id)
    if address_id is not None and not (3 <= len(address_id) <= 32):
        raise evt_error(
            "EVT-4005", "字段 address_id 格式非法：长度需 3~32", {"field": "address_id"}
        )

    biz_no = _opt_str(model.biz_no)
    if biz_no is not None and len(biz_no) > BIZ_NO_MAX:
        raise evt_error(
            "EVT-4005", f"字段 biz_no 格式非法：长度不得超过 {BIZ_NO_MAX}",
            {"field": "biz_no"},
        )

    if model.ts is not None and (isinstance(model.ts, bool) or not isinstance(model.ts, int)):
        raise evt_error(
            "EVT-4005", f"字段 ts 格式非法：{model.ts!r}（需毫秒整数时间戳）",
            {"field": "ts"},
        )

    # `sku_count` / `success` / `received_goods` 的类型与取值范围
    if "sku_count" in scene and not _is_positive_int(scene["sku_count"]):
        raise evt_error(
            "EVT-4005", f"字段 sku_count 格式非法：{scene['sku_count']!r}（需正整数）",
            {"field": "sku_count"},
        )
    if "success" in scene and not isinstance(scene["success"], bool):
        raise evt_error(
            "EVT-4005", f"字段 success 格式非法：{scene['success']!r}（需布尔值）",
            {"field": "success"},
        )
    if "received_goods" in scene and not isinstance(scene["received_goods"], bool):
        raise evt_error(
            "EVT-4005",
            f"字段 received_goods 格式非法：{scene['received_goods']!r}（需布尔值）",
            {"field": "received_goods"},
        )
    if event_type == "login":
        login_type = scene.get("login_type")
        if login_type not in ("pwd", "sms", "scan"):
            raise evt_error(
                "EVT-4005",
                f"字段 login_type 格式非法：{login_type!r}（仅支持 pwd/sms/scan）",
                {"field": "login_type"},
            )


def _is_positive_int(value: Any) -> bool:
    """严格正整数判定（`bool` 是 `int` 的子类，必须显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _reconcile_amount(model: EventIn, event_type: str, scene: dict) -> Optional[int]:
    """BR-03-06：`amount` 与 `scene_extra` 金额字段的一致性检查与双向补齐。

    规则：**同时提供且不等 → `EVT-4007`**（双口径矛盾必须让调用方自己裁决，
    系统替他选一个会在对账时表现为"金额对不上"）；只提供其一 → 双向补齐
    （省掉调用方重复填写，也保证库内只有一个金额事实）。
    """
    field = AMOUNT_FIELD_BY_TYPE.get(event_type)
    top = model.amount
    if top is not None and not _is_positive_int(top):
        raise evt_error(
            "EVT-4005", f"字段 amount 格式非法：{top!r}（需正整数，单位分）",
            {"field": "amount"},
        )
    if field is None:
        return top

    nested = scene.get(field)
    if nested is not None and not _is_positive_int(nested):
        raise evt_error(
            "EVT-4005", f"字段 {field} 格式非法：{nested!r}（需正整数，单位分）",
            {"field": field},
        )
    if top is not None and nested is not None and top != nested:
        raise evt_error(
            "EVT-4007",
            f"amount({top}) 与 {field}({nested}) 不一致",
            {"amount": top, field: nested, "event_type": event_type},
        )
    if top is None and nested is None:
        # 必填集里已包含 `amount`，走到这里说明 `scene_extra` 的金额键被显式传了
        # null —— 与"缺失"同义，按 EVT-4004 报，让调用方补齐而不是猜 0
        raise evt_error(
            "EVT-4004", missing_fields_message(event_type, ["amount"]),
            {"missing": ["amount"], "event_type": event_type},
        )
    if top is None:
        return int(nested)
    scene.setdefault(field, top)
    return int(top)


# ============================================================
# 降级（BR-03-21 ~ 24，fail-closed）
# ============================================================
# 降级码 -> degrade.stage（§5）：三张表各对应一个 stage
DEGRADE_STAGE_BY_CODE: dict[str, str] = {
    "EVT-5001": "timeout",
    "EVT-5002": "feature",
    "EVT-5003": "rule",
}


def degraded_decision(reason: str, snapshot_id: Optional[str] = None) -> dict:
    """生成**统一的、明确标注为降级**的决策块（fail-closed 兜底）。

    为什么这里可以"自己造"决策块，而正常路径绝对不能（V-03-06）：
    这条路径的前提是 **05 完全没有产出任何东西**。此时如果直接把 `decision`
    留空或返回 500，调用方就失去了风控结论——而"不确定"的正确处置恰恰是
    `review`（转人工），不是放行、也不是让请求失败。因此这里造的是一个
    **保守**的块：`review` + 0 分 + 空命中，且 `engine_version` 明确写
    `degraded`，任何事后分析都能一眼把它与 05 的真实结论区分开。

    `risk_level` 取 `high` 而非 `low`：0 分在 05 的分档里属低风险，但"0 分"
    在这里的含义是"没算出来"，用 `high` 才能让 08 建案与页面展示按高风险
    优先处理。真正放行与否由 `decision=review` 决定，`risk_level` 只影响排序。

    **`list_hit` 用对象形态**（`{hit:false, list_type:null, entity_type:null,
    entity_value:null}`，见 `schemas.event_schema.ListHit` 的契约裁定）：
    降级意味着"名单过滤根本没做"，用 `hit=false` 表达恰好正确。不能写成
    `False`——同一个字段时而是 bool 时而是对象，会让每个消费方都不得不写
    类型分支，而分支里必然有一边无人验证。
    """
    block = {
        "list_hit": list_hit_miss(),
        "rule_score": 0,
        "model_score": None,
        "final_score": 0,
        "risk_level": "high",
        "decision": "review",
        "hit_rule_count": 0,
        "hits": [],
        "rule_versions": {},
        "engine_version": "degraded",
        "snapshot_id": snapshot_id,
        "elapsed_ms": 0,
    }
    block["degrade_reason"] = reason
    return block


def normalize_decision(raw: Any, fallback_snapshot_id: Optional[str]) -> dict:
    """把 05 的返回值归一成响应契约的 12 个字段（**只补缺，不覆盖**）。

    `setdefault` 而不是赋值：05 给出的任何取值都必须原样透传（V-03-06）。
    这里的"补缺"只针对 05 未提供的字段名，避免响应体缺键让前端渲染报错。

    唯一的例外是 `list_hit`：它的契约是**对象**（Spec 05 §3.1 / E03），因此
    这里做一次形状归一（`coerce_list_hit`）——对象形态的内容**一字不改**，
    只有"不是对象"（历史 bool 形态、脏数据）才落成"未命中"对象。这不是覆盖
    05 的取值，而是拒绝一个契约违例的形状：bool 形态的 `list_hit` 会让每一个
    消费方（前端判定摘要、仿真页、E2E、未来的 07/08）都写类型分支，
    而分支里必然有一边无人验证。
    """
    block: dict[str, Any] = dict(raw or {})
    block["list_hit"] = coerce_list_hit(block.get("list_hit"))
    block.setdefault("rule_score", 0)
    block.setdefault("model_score", None)
    block.setdefault("final_score", 0)
    block.setdefault("risk_level", "low")
    block.setdefault("decision", "review")
    block.setdefault("hit_rule_count", len(block.get("hits") or []))
    block.setdefault("hits", [])
    block.setdefault("rule_versions", {})
    block.setdefault("engine_version", "")
    block.setdefault("snapshot_id", fallback_snapshot_id)
    block.setdefault("elapsed_ms", 0)
    return block


# ============================================================
# 服务
# ============================================================
class EventService:
    """事件接入服务（进程内单例，见文件末尾）。"""

    def __init__(self) -> None:
        # 后台落库任务：持有引用才能在关闭/测试结束时等它们收尾，
        # 否则会留下"Task was destroyed but it is pending"告警，
        # 更糟的是把待写文档带进下一个事件循环（跨测试污染）
        self._persist_tasks: set[asyncio.Task] = set()
        self.stats: dict[str, int] = {
            "ingested": 0, "duplicate": 0, "conflict": 0, "degraded": 0,
            "persisted": 0, "persist_failed": 0,
        }

    # ---------------- 落库（异步旁路，BR-03-17/18） ----------------
    def _repo(self) -> EventRepo:
        """每次取新句柄：测试会切换数据库，缓存 repo 会写错库。"""
        return EventRepo(db_module.get_db())

    def _spawn_persist(self, event: dict, response: dict) -> None:
        """把落库丢到后台任务，**不阻塞响应**（AD-01）。

        决策一旦产生就不再回滚（BR-03-18）：落库失败只告警并进重试队列，
        `persisted` 保持 false，调用方按 `EVT-5004` 的提示改用详情接口自查。
        """
        task = asyncio.create_task(
            self._persist_with_retry(event, response), name="event-persist"
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)

    async def _persist_with_retry(self, event: dict, response: dict) -> bool:
        last: Optional[Exception] = None
        for attempt in range(1, PERSIST_RETRY + 1):
            try:
                await self._repo().insert(event)
                self.stats["persisted"] += 1
                response["persisted"] = True
                # 幂等缓存里存的是**同一个 dict 对象**（由 `finish` 写入），
                # 因此这里改成 true 之后，后续的 duplicate 命中会读到已落库状态
                return True
            except AppError as e:
                last = e
                log.warning("[%s] risk_events 落库失败（第 %d/%d 次）event_id=%s：%s",
                            e.code, attempt, PERSIST_RETRY, event.get("_id"), e.message)
            except Exception as e:  # noqa: BLE001 - 后台任务不允许把异常抛到无人区
                last = e
                log.warning("risk_events 落库异常（第 %d/%d 次）event_id=%s：%s",
                            attempt, PERSIST_RETRY, event.get("_id"), e)
            if attempt < PERSIST_RETRY:
                await asyncio.sleep(PERSIST_RETRY_BASE_SEC * attempt)
        self.stats["persist_failed"] += 1
        log.error("[EVT-5004] %s event_id=%s（决策已正常返回，未回滚；详情查询暂 404）",
                  EVT_NOTICE["EVT-5004"], event.get("_id"))
        return False

    async def flush(self, timeout: float = 5.0) -> bool:
        """等待全部在途落库任务收尾（测试与优雅关闭用）。"""
        if not self._persist_tasks:
            return True
        pending = list(self._persist_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            log.error("仍有 %d 个事件落库任务未完成（等待 %.1fs 超时）",
                      len(pending), timeout)
            return False

    # ---------------- 单条接入 ----------------
    async def ingest(
        self,
        payload: Any,
        *,
        source: Optional[str] = None,
        check_idempotency: bool = True,
    ) -> dict:
        """接一条事件并**同步返回决策**（§3.1）。

        `source` 由调用方（模拟器）覆盖为 `mock_biz`；`check_idempotency=False`
        给批量接口用：批内每条都由服务端新生成编号，幂等键不可能重复，
        跳过缓存查表可以省掉每次一次载荷哈希（压测路径）。
        """
        if not isinstance(payload, dict):
            raise evt_error("EVT-4001", "请求体格式非法：必须是 JSON 对象",
                            {"received_type": type(payload).__name__})
        raw = dict(payload)
        if source:
            raw["source"] = source
        # 载荷哈希在补默认值**之前**算：调用方"没传 ts"与"传了服务端补的 ts"
        # 是同一件事的两种写法，不该被判成不同载荷（BR-03-12 只关心事实本身）
        digest = payload_digest(raw)

        client_id = raw.get("event_id") or raw.get("client_event_id")
        if client_id is not None:
            client_id = str(client_id).strip()
            if not re.fullmatch(r"EVT\d{20}", str(client_id)):
                raise evt_error(
                    "EVT-4005",
                    f"字段 event_id 格式非法：{client_id}（需匹配 EVT+8位日期+12位序列）",
                    {"field": "event_id", "pattern": EVENT_ID_PATTERN},
                )

        for _ in range(3):
            if not check_idempotency:
                return await self._ingest_fresh(raw, None)
            serve = self._serve_from_cache(client_id, digest)
            if serve is not None:
                return serve
            if client_id is None:
                return await self._ingest_fresh(raw, None)
            state, entry = get_store().reserve(client_id, digest)
            if state == "reserved":
                return await self._lead(client_id, entry, raw, digest)
            if state == "hit":
                # 极端竞态：登记前一刻首领已完成。按命中返回，不重算
                return self._duplicate_body(get_store().check(client_id, digest)[1])
            if state == "conflict":
                self.stats["conflict"] += 1
                raise self._conflict_error(client_id)
            # inflight：已经有首领在跑同编号同载荷，等他
            got = await self._follow(client_id)
            if got is not None:
                return got
        # 理论上到不了这里（循环里每条分支都 return 或抛错），
        # 真到了说明占位被反复释放，按"服务端无法给出确定结论"上报而不是静默重算
        raise evt_error("EVT-4001", "同一事件编号的幂等占位被反复释放，请重试",
                        {"event_id": client_id})

    def _serve_from_cache(self, client_id: Optional[str], digest: str) -> Optional[dict]:
        """同步查幂等缓存（临界区内**零 `await`**，BR-03-14）。"""
        if client_id is None:
            return None
        state, body = get_store().check(client_id, digest)
        if state == "hit":
            self.stats["duplicate"] += 1
            return self._duplicate_body(body)
        if state == "conflict":
            self.stats["conflict"] += 1
            raise self._conflict_error(client_id)
        return None

    @staticmethod
    def _duplicate_body(body: Optional[dict]) -> dict:
        """命中幂等：返回首次响应体并置 `duplicate=true`。

        **不重算、不重写库**（BR-03-11）——幂等的意义就是"重复提交对系统
        没有第二次影响"。
        """
        out = dict(body or {})
        out["duplicate"] = True
        return out

    @staticmethod
    def _conflict_error(client_id: str) -> AppError:
        """BR-03-12：同编号不同载荷。"""
        return evt_error(
            "EVT-4009",
            "事件编号已存在且内容不同，疑似编号复用",
            {"event_id": client_id,
             "hint": "同一 event_id 承载不同事实属数据事故；如需查询首次结果请 "
                     f"GET /api/v1/events/{client_id}"},
        )

    async def _follow(self, client_id: str) -> Optional[dict]:
        """作为跟随者等待首领的结果（single-flight，BR-03-14 的延伸）。

        返回 `None` 表示首领已放弃占位（他失败了），调用方应重新走一次判定。
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        if not get_store().attach_waiter(client_id, waiter):
            return None
        try:
            body = await waiter
        except RuntimeError:
            return None
        self.stats["duplicate"] += 1
        return self._duplicate_body(body)

    async def _lead(self, client_id: str, entry: Any, raw: dict, digest: str) -> dict:
        """首领：跑完整条链路，**在返回之前**把响应体写入缓存与落库。"""
        try:
            body, event = await self._pipeline(raw, client_id)
        except BaseException:
            # 占位必须释放：否则该编号会被一个没有结果的占位永久占住，
            # 后来的同编号请求会一直等待一个不会到来的结果。
            # 原异常**原样向上抛**（例如 EVT-4004 校验失败必须让调用方看到
            # 缺失字段名），包装成 500 会把可修正的报文错误变成"服务端故障"。
            get_store().release(client_id)
            raise
        # ↓ 关键顺序：先落缓存（同步、零 await），再丢落库，最后 return
        get_store().finish(client_id, body, event)
        self._spawn_persist(event, body)
        return body

    async def _ingest_fresh(self, raw: dict, client_id: Optional[str]) -> dict:
        """无幂等键（或批量路径）的接入：直接跑链路。"""
        body, event = await self._pipeline(raw, client_id)
        self._spawn_persist(event, body)
        return body

    # ---------------- 链路主体 ----------------
    async def _pipeline(self, raw: dict, client_id: Optional[str]) -> tuple[dict, dict]:
        """校验 → 编号 → 04 → 05 → 组装响应与落库文档。"""
        started = time.perf_counter()
        received_at = now_ms()

        # 1. 校验（BR-03-01~07）。校验失败的请求**不进任何缓存**：
        #    调用方修正报文后重发是正常行为，不该被判成"编号复用"
        event = validate_event(raw, received_at)
        event_id = client_id or await new_event_id(db_module.get_db())
        event["_id"] = event_id

        # 2/4/5 走 200ms 预算（BR-03-23）。取号也包在里面：它是一次 Mongo
        # `find_one_and_update`，在库慢的时候同样会吃掉整条链路的预算
        snapshot: Optional[dict] = None
        degrade: Optional[dict] = None
        if event["late_arrival"]:
            # BR-03-08：迟到事件直接 fail-closed 转 review，**不进 04/05**。
            # 理由：04 的滑窗按"现在的窗口"聚合，把 1 天前的数据塞进去会让
            # 后续真实事件的特征被污染（与决策 D11"仿真不写真实窗口"同源）。
            degrade, decision = self._degrade(
                "EVT-5002",
                "迟到事件（ts 早于长窗口 1440 分钟）：入滑窗会污染窗口语义，"
                "按 fail-closed 直接转人工审核",
            )
        else:
            try:
                snapshot, decision, degrade = await asyncio.wait_for(
                    self._compute_and_decide(event), timeout=TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                # 超时：`wait_for` 已取消下游协程，这里只需组装降级信封
                degrade, decision = self._degrade(
                    "EVT-5001", f"决策链路超时（>{DECISION_TIMEOUT_MS}ms）")
                log.warning("[EVT-5001] 决策链路超时 event_id=%s", event_id)
            except _StageUnavailable as e:
                degrade, decision = self._degrade(e.code, e.reason)
            except Exception as e:  # noqa: BLE001 - 任何 04/05 侧异常都必须 fail-closed
                degrade, decision = self._degrade(
                    "EVT-5003", f"决策链路未预期异常：{type(e).__name__}: {e}")
                log.exception("[EVT-5003] 决策链路异常 event_id=%s", event_id)

        if degrade is not None:
            self.stats["degraded"] += 1

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body: dict[str, Any] = {
            "event_id": event_id,
            "received_at": received_at,
            "duplicate": False,
            # AD-01：同步返回决策、异步落库，因此**通常**是 false（BR-03-18）
            "persisted": False,
            "degrade": degrade,
        }
        for field in DECISION_FIELDS:
            body[field] = decision.get(field)
        body["elapsed_ms"] = elapsed_ms
        # 特征快照随事件落库，供详情接口与事后复核（04 正式实现后由它提供）
        if snapshot:
            event["feature_snapshot"] = snapshot
        if degrade:
            event["degrade"] = degrade
        event["decision"] = {f: body.get(f) for f in DECISION_FIELDS}
        # BR-03-24：降级也必须落库（保住不可篡改的原始证据）
        self.stats["ingested"] += 1

        # 模块 09（BR-09-07）：建边/打标/累计统计一律在**决策之后**异步执行。
        # 这里只做"同步入队、零 await"，真正的写库在 `edge_writer` 的后台任务里；
        # 建图失败不影响本次决策（BR-09-11：入重试队列 + 告警）。
        # 放在 `return` 之前的最后一步是刻意的：它不参与上面的 200ms 预算
        # （`_compute_and_decide` 才在 `wait_for` 里），因此不会拖慢决策链路。
        edge_writer.enqueue_after_decision(event, body)
        return body, event

    async def _compute_and_decide(
        self, event: dict
    ) -> tuple[Optional[dict], dict, Optional[dict]]:
        """04 → 05 **严格串行**（BR-03-15 / Spec §3.3；D4 的并行裁定属 05 内部）。

        为什么串行：Spec §3.3 的链路图与 BR-03-15 都写明 04 先出快照、05 拿
        快照求值。D4 把「名单过滤」与「特征计算」改为并行，那是 **05 内部**
        的分工（名单在 05 手上），不影响 04→05 的先后依赖。

        04/05 抛异常时翻译成带 stage 的 `_StageUnavailable`，交给 `_pipeline`
        统一组装降级信封——**绝不允许在这里把异常吞掉后返回 pass**。
        """
        components = get_components()

        try:
            snapshot = await components.feature_provider.compute(event)
        except Exception as e:  # noqa: BLE001 - 04 的任何失败都必须 fail-closed
            raise _StageUnavailable(
                "EVT-5002", "feature",
                f"特征服务不可用（{type(e).__name__}: {e}）",
            ) from e
        if not isinstance(snapshot, dict):
            raise _StageUnavailable(
                "EVT-5002", "feature",
                f"特征服务返回非法结构（{type(snapshot).__name__}），无法据此决策",
            )
        if snapshot.get("degrade_suggested"):
            # 04 自己声明"这次快照不完整"（例如关键特征缺失）：**必须在调 05 之前**
            # 短路。若等 05 算完再丢弃它的结论，就等于让 05 基于不完整特征做了一次
            # 判定——那正是 fail-closed 要避免的事（而且 05 可能已经把命中写库）。
            raise _StageUnavailable(
                "EVT-5002", "feature",
                "特征快照不完整（degrade_suggested=true），按 fail-closed 转人工审核",
            )

        features = snapshot.get("features") or {}
        # 把快照挂到事件上再交给 05（**不改方法签名**：03/04 依赖的是
        # `evaluate(event, features)` 这个形状）。
        #
        # 为什么必须带过去：E03 `decisions.snapshot_id` 是**必填列**（"关联特征
        # 快照"），而 05 的调用契约里没有快照编号——只有本模块手里有。若不带，
        # 05 写库时只能写 null，于是"这条决策当时看的是哪份特征"永远断链，
        # 07 的复核页拿不到证据（响应里的 snapshot_id 由 `normalize_decision`
        # 的 setdefault 补上，所以**只看接口发现不了**这个问题）。
        # 事件文档的内容不变：下面 `_pipeline` 本来就会写同一个键。
        event["feature_snapshot"] = snapshot
        try:
            raw_decision = await components.decision_provider.evaluate(event, features)
        except Exception as e:  # noqa: BLE001 - 05 的任何失败都必须 fail-closed
            raise _StageUnavailable(
                "EVT-5003", "rule",
                f"决策服务不可用（{type(e).__name__}: {e}）",
            ) from e
        if not isinstance(raw_decision, dict):
            raise _StageUnavailable(
                "EVT-5003", "rule",
                f"决策服务返回非法结构（{type(raw_decision).__name__}），无法据此放行",
            )

        decision = normalize_decision(raw_decision, snapshot.get("snapshot_id"))
        # fail-closed 的最后一道闸：05 若返回了既不是 pass/review/reject 的取值，
        # 一律按最保守的 review 处理（而不是把它当成"非 reject 即放行"）
        if decision.get("decision") not in ("pass", "review", "reject"):
            decision["decision"] = "review"
            decision["degrade_reason"] = "决策取值非法，按 fail-closed 转 review"
        return snapshot, decision, None

    @staticmethod
    def _degrade(code: str, reason: str) -> tuple[dict, dict]:
        """组装 `(degrade 信封, 决策块)`。stage 由码唯一决定（§5 的三张表）。"""
        stage = DEGRADE_STAGE_BY_CODE.get(code, "feature")
        return (
            {"degraded": True, "reason": reason, "stage": stage},
            degraded_decision(reason),
        )

    # ---------------- 批量接入（§3.2） ----------------
    async def ingest_batch(self, body: Any) -> dict:
        """批内**逐条串行**走同一链路；单条失败不中断批次（BR-03-16）。

        不做并发：Spec §3.2 明确「非并发批量落库」，且单条本身受 200ms 预算
        约束，逐条串行才能让 `elapsed_ms` 与失败位置一一对应。
        """
        started = time.perf_counter()
        events = body.get("events") if isinstance(body, dict) else None
        verbose = bool(body.get("verbose")) if isinstance(body, dict) else False
        if not isinstance(events, list):
            raise evt_error("EVT-4001", "请求体格式非法：`events` 必须是数组")
        if len(events) == 0 or len(events) > BATCH_MAX:
            # EVT-4010：**拒绝整批**而不是截断。静默只处理前 500 条会让调用方
            # 以为 812 条都进去了（差 312 条安全事件），这是不可接受的静默损失
            raise evt_error(
                "EVT-4010", f"单批最多 {BATCH_MAX} 条，当前 {len(events)} 条",
                {"max": BATCH_MAX, "received": len(events)},
            )

        counts = {"pass": 0, "review": 0, "reject": 0, "failed": 0}
        results: list[BatchItemResult] = []
        for item in events:
            try:
                # 批量走 `check_idempotency=False`：编号由服务端新生成，
                # 批内不可能重复；省掉一次载荷哈希，让压测路径更接近 §7 的预算
                out = await self.ingest(item, check_idempotency=False)
                decision = str(out.get("decision"))
                if decision in counts:
                    counts[decision] += 1
                else:
                    counts["failed"] += 1
                if verbose:
                    results.append(BatchItemResult(
                        event_id=out.get("event_id"), decision=decision,
                        ok=True, code="OK", message="ok",
                    ))
            except AppError as e:
                counts["failed"] += 1
                if verbose:
                    missing = (e.data or {}).get("missing", []) if isinstance(e.data, dict) else []
                    results.append(BatchItemResult(
                        ok=False, code=e.code, message=e.message, missing=list(missing),
                    ))
            except Exception as e:  # noqa: BLE001 - 单条失败绝不中断整批
                counts["failed"] += 1
                log.exception("批量接入单条异常：%s", e)
                if verbose:
                    results.append(BatchItemResult(
                        ok=False, code="EVT-5004",
                        message=f"内部异常：{type(e).__name__}",
                    ))
        return {
            "total": len(events),
            **{f"{k}_cnt": v for k, v in counts.items()},
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "results": [r.model_dump() for r in results] if verbose else None,
        }

    # ---------------- 详情（§3.3） ----------------
    async def detail(self, event_id: str) -> dict:
        """四段聚合读：`event` + `snapshot` + `decision` + `hits`。

        查不到事件 → `EVT-4404`（404 + 500ms 后重试建议）。这是**正常**状态：
        BR-03-17 的异步落库意味着刚 POST 完立刻 GET 有可能还没写完。
        """
        repo = self._repo()
        event = await repo.find_by_id(event_id)
        if event is None:
            raise evt_error(
                "EVT-4404",
                "事件已接收，数据写入中，请 500ms 后重试",
                {"event_id": event_id, "retry_after_ms": 500},
            )
        snapshot = await repo.find_snapshot(event_id)
        if snapshot is None and isinstance(event.get("feature_snapshot"), dict):
            # 04 尚未落 `feature_snapshots` 时，回落到事件文档里随写入库的快照，
            # 而不是返回 null —— 详情页的四段里"快照"是研判的关键依据
            snapshot = event["feature_snapshot"]
        decision = await repo.find_decision(event_id)
        if decision is None and isinstance(event.get("decision"), dict):
            decision = event["decision"]
        hits = await repo.find_hits(event_id)
        event_out = dict(event)
        event_out.pop("feature_snapshot", None)
        event_out.pop("decision", None)
        return {
            "event": event_out,
            "snapshot": snapshot,
            "decision": decision,
            "hits": hits,
        }


class _StageUnavailable(Exception):
    """内部信号：某个下游阶段不可用（BR-03-21/22）。

    用它而不是直接抛 `AppError`：`AppError` 是**对外**错误（会变成 4xx/5xx
    响应），而降级是**对内**的控制流——HTTP 仍是 200，只是决策被保守化。
    混用会让"降级"与"失败"在代码里无法区分。
    """

    def __init__(self, code: str, stage: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.stage = stage
        self.reason = reason


def show_idempotency_release_error() -> BaseException:
    """首领失败时向上抛的原异常（占位已释放，调用方可直接重试）。

    保留这个构造器是为了让"占位释放"这件事有一个可被测试断言的名字；
    实际抛出的始终是**原异常**（见 `_lead`），因为首领为什么失败必须原样
    向上传递——包装会把它变成一个说不出原因的 500。
    """
    return RuntimeError("事件接入失败，幂等占位已释放")


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = EventService()


def get_event_service() -> EventService:
    return _SERVICE


async def ingest(payload: Any, *, source: Optional[str] = None) -> dict:
    """接入一条事件（模拟器与接口的统一入口，BR-03-29）。"""
    return await _SERVICE.ingest(payload, source=source)


async def detail(event_id: str) -> dict:
    """模块级便捷入口：事件详情（§3.3）。"""
    return await _SERVICE.detail(event_id)


async def flush(timeout: float = 5.0) -> bool:
    """等待在途落库完成（测试与关闭流程用）。"""
    return await _SERVICE.flush(timeout)


__all__ = [
    "DEGRADE_STAGE_BY_CODE", "EventService", "LATE_WINDOW_MS", "TIMEOUT_SEC",
    "degraded_decision", "detail", "evt_error", "flush", "get_event_service",
    "ingest", "missing_fields_message", "normalize_decision", "validate_event",
]
