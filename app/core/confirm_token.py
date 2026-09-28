# -*- coding: utf-8 -*-
"""二次确认令牌：签发与校验（模块 08 §4.5，BR-08-26 / 27 / 28）。

## 它防的是什么

处置**不可撤销**（BR-08-18）。前端弹窗只是体验，**真正的闸门必须在服务端**：
绕过页面直接 `POST /cases/{no}/dispose` 的人（手工 curl、脚本、被劫持的前端）
必须拿不到"执行"这一下。因此 `/dispose/preview` 签发一个**签名令牌**，
`/dispose` 校验它——没有令牌就没有副作用。

## 四个约束，逐条对应 BR-08-27

| 约束 | 实现 |
|---|---|
| **一次性** | 进程内"已用令牌"集合（`jti`），校验通过即登记；重复使用 → `DSP-4006` |
| **TTL 30s** | 令牌载荷里带 `exp`（毫秒），校验时与时钟比较 |
| **签名绑定** `case_no + conclusion + action_types + operator` | 载荷里放这四个值，签名覆盖整个载荷；**校验时把当前请求的同名参数一起比对**——参数一变，即便令牌没被改也拒绝 |
| **参数变更即失效** | 同上一行：`action_types` 用"去重+排序"后的规范形式（`normalize_action_types`），因此只换勾选顺序不算变更 |

## 为什么用 HMAC 而不是"把参数哈希后存内存"

存内存（把 `case_no+结论+动作` 当键、值是一个随机数）也能工作，但它把
"一次性"与"参数绑定"耦合在同一张表上，多进程部署时直接失效。HMAC 令牌
**无状态**，可以在多进程/多副本下同时成立（一次性那一半仍需共享存储，
已如实登记为已知局限）。密钥从 `JWT_SECRET` 派生（**不新引入一个必需的
环境变量**：本项目已有的密钥类配置只有 JWT_SECRET，多一个就要多一份
部署文档与一次"忘了配"的启动失败），派生用 `HMAC(secret, "confirm_token")`
——与 JWT 签名共用原文但域分隔，令牌不能当 JWT 用、JWT 也不能当令牌用。

## 已知局限（如实登记，不掩盖）

- "一次性"的登记表在**进程内**：多 worker 部署时同一个令牌可能在两个 worker
  上各用一次。要彻底解决需把 `jti` 落库（Redis/Mongo）——当前单进程部署
  （AD-04 的单写者也是这个前提）下不是问题，且**30 秒的 TTL 已经把窗口
  压得极小**。
- 令牌的有效期只有 30 秒（BR-08-27）：审核员在弹窗上停留超过 30 秒再点确认
  会拿到 `DSP-4006`，前端自动回退到弹窗重签——这是**刻意**的，处置是不可逆
  操作，"想清楚再动手"比"点得快"重要。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Callable

from app import config
from app.errors import ConfirmTokenInvalidError
from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.case.confirm_token")

#: 派生密钥的域分隔串（换掉它等于一次性作废所有在途令牌，属预期内的运维动作）
_KDF_CONTEXT = b"shop_risk_control:case:confirm_token:v1"

#: 进程内"已用过"的令牌 id（一次性）。有界：只保留最近 `_USED_MAX` 条，
#: 且随 TTL 自然过期——无界集合会被"每次 preview 都签发但从不使用"的请求撑爆。
_USED_MAX = 4096
_used_jti: "dict[str, int]" = {}


def _key() -> bytes:
    """由 `JWT_SECRET` 派生的令牌签名密钥。

    **每次调用都重新派生**（成本是两次哈希，可忽略）：`config.JWT_SECRET` 在
    测试里会被 monkeypatch 覆盖，若在模块导入时算好并缓存，用例就再也换不掉它
    ——而"令牌必须真的被签名保护"这件事只能靠"换个密钥后旧令牌全部失效"来证明。
    """
    secret = (config.JWT_SECRET or "").encode("utf-8")
    return hmac.new(secret, _KDF_CONTEXT, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def payload_of(case_no: str, conclusion: str, action_types: list[str], operator: str) -> dict:
    """令牌要绑定的载荷（**只有这四项**，BR-08-27）。

    `remark` / `list_writes` / `evidence_refs` **刻意不绑定**：它们在弹窗上
    还可能被微调（例如补一句备注），把它们纳入签名会让"改了一个错别字就得
    重新确认"；而真正决定后果的是结论与动作，以及"是谁在操作"。

    `action_types` 在**这里**做一次归一化（去重 + 排序）：签发侧与校验侧
    共用同一个函数，所以"用户只是换了勾选顺序"不会被判成参数变更
    （BR-08-27 的"参数变更即失效"指的是**内容**变了，不是复选框的顺序）。
    """
    from app.schemas.case_schema import normalize_action_types

    return {
        "c": str(case_no),
        "k": str(conclusion),
        "a": normalize_action_types(action_types),
        "o": str(operator),
    }


def issue(
    case_no: str,
    conclusion: str,
    action_types: list[str],
    operator: str,
    *,
    ttl_sec: int = 30,
    clock: Callable[[], int] = now_ms,
) -> dict:
    """签发令牌，返回 `{token, expires_at, expires_in}`。

    `ttl_sec` / `clock` 可注入：TTL 的边界行为必须能在测试里被确定性地推进
    （否则"30 秒后失效"只能靠等 30 秒或改系统时间来验，等于没有测试）。
    """
    moment = int(clock())
    body: dict[str, Any] = {
        **payload_of(case_no, conclusion, action_types, operator),
        "exp": moment + int(ttl_sec) * 1000,
        # `jti`：一次性判定的键。用 `secrets` 而不是 `random`——并发下
        # `random` 的种子碰撞概率更高，而两个请求拿到同一个 jti 意味着
        # "其中一个的令牌会被误判为已用过"（用户看到莫名其妙的 DSP-4006）
        "jti": secrets.token_hex(8),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    signature = hmac.new(_key(), raw.encode("utf-8"), hashlib.sha256).digest()
    return {
        "token": f"{_b64e(raw.encode('utf-8'))}.{_b64e(signature)}",
        "expires_at": body["exp"],
        "expires_in": int(ttl_sec),
    }


def _decode(token: str) -> dict:
    """拆包 + 验签，任何异常都归一到 `DSP-4006`（**绝不 500**）。

    令牌是**外部输入**：伪造者可以送任何字符串进来。让 `base64` / `json` 的
    解析异常冒出去，会把一次"有人在试探"变成一次 500 告警，掩盖真实的安全事件。
    """
    try:
        raw_part, sig_part = str(token).split(".", 1)
        raw = _b64d(raw_part)
        signature = _b64d(sig_part)
    except Exception as e:  # noqa: BLE001 - 非法令牌属预期输入
        raise ConfirmTokenInvalidError("malformed", f"{type(e).__name__}") from e
    expected = hmac.new(_key(), raw, hashlib.sha256).digest()
    # `compare_digest`：普通 `==` 的短路比较会泄露"前几位对不对"，
    # 对签名校验这是标准的时序侧信道
    if not hmac.compare_digest(signature, expected):
        raise ConfirmTokenInvalidError("bad_signature")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - 签名对了但内容不是 JSON（理论上不可能）
        raise ConfirmTokenInvalidError("malformed", f"{type(e).__name__}") from e
    if not isinstance(body, dict):
        raise ConfirmTokenInvalidError("malformed", "载荷不是对象")
    return body


def _sweep(moment: int) -> None:
    """清理过期条目（有界集合的维护，见 `_USED_MAX`）。"""
    expired = [jti for jti, exp in _used_jti.items() if exp <= moment]
    for jti in expired:
        _used_jti.pop(jti, None)
    while len(_used_jti) > _USED_MAX:
        _used_jti.pop(next(iter(_used_jti)), None)


def consume(
    token: str,
    case_no: str,
    conclusion: str,
    action_types: list[str],
    operator: str,
    *,
    clock: Callable[[], int] = now_ms,
) -> dict:
    """校验并**消费**令牌（一次性）。返回载荷；任何不合法都抛 `DSP-4006`。

    校验顺序刻意是：**签名 → 过期 → 参数绑定 → 一次性**。
    - 签名第一：内容不可信时，后面所有比较都没有意义；
    - 一次性最后：只有"这次确认确实要执行"时才把它标记为已用。
      若把它放在前面，一次因参数不匹配而失败的调用会**吃掉**这个令牌，
      用户改正参数后重试会拿到"确认已过期"，而他明明什么都没做错。
    """
    body = _decode(token)
    moment = int(clock())
    try:
        expires_at = int(body.get("exp"))
    except (TypeError, ValueError) as e:
        raise ConfirmTokenInvalidError("malformed", "缺少有效期") from e
    if expires_at <= moment:
        raise ConfirmTokenInvalidError("expired")

    want = payload_of(case_no, conclusion, action_types, operator)
    for field, label in (("c", "case_no"), ("k", "conclusion"), ("a", "action_types"),
                         ("o", "operator")):
        if body.get(field) != want[field]:
            raise ConfirmTokenInvalidError(
                "params_changed",
                f"{label} 与签发时不一致（签发 {body.get(field)!r}，"
                f"本次 {want[field]!r}）",
            )

    jti = str(body.get("jti") or "")
    _sweep(moment)
    if jti and jti in _used_jti:
        raise ConfirmTokenInvalidError("reused", "该确认已执行过，请重新确认")
    if jti:
        _used_jti[jti] = expires_at
    return body


def reset_store() -> None:
    """清空"已用令牌"集合（测试夹具逐用例复位；生产**没有**调用点）。

    生产上清空它等于让在途令牌可以被再用一次——虽然窗口只有 30 秒，
    但那 30 秒里"重复处置"正是 `DSP-4006` 要挡的事。
    """
    _used_jti.clear()


def used_count() -> int:
    """当前已用令牌数（供测试与排障断言，不对外暴露）。"""
    return len(_used_jti)


__all__ = [
    "consume",
    "issue",
    "payload_of",
    "reset_store",
    "used_count",
]
