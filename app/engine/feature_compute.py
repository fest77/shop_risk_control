# -*- coding: utf-8 -*-
"""18 项特征的口径实现（模块 04 BR-04-06）——**纯函数优先，无 IO、无全局状态**。

## 设计要点

1. **窗口数据**由 `WindowView` 传入（`feature_window.plan` 的产物），
   本模块不持有任何窗口对象，因此可以脱离 `FeatureWindow` 单测（Spec §6 的
   "纯函数、重点单测对象"）。
2. **画像数据**由 `ProfileData` 传入（由 `feature_repo` 从 E10~E13 读出）。
   画像不可用时对应项一律进 `missing`，**绝不填 0**（BR-04-09）。
3. **单个特征失败不拖垮整次计算**：每项各自 `try/except`，失败项并入
   `missing` 并记录原因（`FEA-5001` 的"返回可用子集"）。兜底值**永不写进
   `features`**——写进去就等于对 05 宣称"这个值是算出来的"。

## 为什么缺失必须与 0 分开（BR-04-08 / §5 的 fail-closed）

`0` 是一个**结论**（"这件事没有发生过"），`缺失`是**事实**（"我们不知道"）。
把"不知道"写成 0，05 看到的是"该用户从不下单、从未售后、不是代理 IP"，
于是所有条件都不命中 → 判低风险 → 放行。这与风控系统的目的正好相反。
因此本模块只做两件事：能算的给出真实值，不能算的**列进 `missing_features`**；
至于缺失在条件求值里怎么处理，由 05 的 BR-05-12 定义（BR-04-11：本模块只标记）。

## 严格说明 `device_age_hours` 的近似

E11 的 `first_seen_at` 是长期累计值，**归模块 09 维护**。04 的窗口只有 24 小时，
因此当画像不可用时只能回落到"窗口内最早出现时间"——那会把一个 3 天前注册的设备
算成"24 小时内新出现"。这个近似**只在画像缺失时生效**，并且仍会写进 `missing`
之外的真实值；报告里已登记为待 09 落地后消除的偏差。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.engine.feature_window import WindowView

# ============================================================
# 18 项特征的键名（**唯一真源之一**：与 E02 §features 表逐项一致，BR-04-06）
# ------------------------------------------------------------
# 顺序即五组展示顺序：行为频次 7 + 设备环境 3 + 网络 IP 3 + 地址聚集 2 + 账号画像 3。
# `V-04-02` 会把这份列表与 E02 的键名集合逐项比对。
# ============================================================
FEATURE_KEYS: tuple[str, ...] = (
    # 分组一 · 行为频次
    "login_cnt_1h",
    "coupon_cnt_1h",
    "order_cnt_1h",
    "order_cnt_24h",
    "pay_fail_cnt_24h",
    "aftersale_cnt_24h",
    "aftersale_rate_24h",
    # 分组二 · 设备环境
    "device_user_cnt",
    "device_order_cnt_1h",
    "device_age_hours",
    # 分组三 · 网络 IP
    "ip_user_cnt",
    "ip_order_cnt_1h",
    "ip_is_proxy",
    # 分组四 · 地址聚集
    "address_user_cnt",
    "address_aftersale_cnt",
    # 分组五 · 账号画像
    "user_age_days",
    "user_level",
    "user_risk_tag_cnt",
)

#: 需要向模块 09 取"关联账号数"的三项（BR-04-07）。
#: **本模块不得自建去重集合**——去重真源只能有一个（对齐 09 的 BR-09-13/14）。
LINKED_COUNT_FEATURES: dict[str, str] = {
    "device_user_cnt": "device",
    "ip_user_cnt": "ip",
    "address_user_cnt": "address",
}

#: 特征值的数据类型（与 E02 的「类型」列一致；`/features/meta` 与 05 的条件求值都依赖它）
FEATURE_DATA_TYPES: dict[str, str] = {
    "login_cnt_1h": "int",
    "coupon_cnt_1h": "int",
    "order_cnt_1h": "int",
    "order_cnt_24h": "int",
    "pay_fail_cnt_24h": "int",
    "aftersale_cnt_24h": "int",
    "aftersale_rate_24h": "float",
    "device_user_cnt": "int",
    "device_order_cnt_1h": "int",
    "device_age_hours": "float",
    "ip_user_cnt": "int",
    "ip_order_cnt_1h": "int",
    "ip_is_proxy": "bool",
    "address_user_cnt": "int",
    "address_aftersale_cnt": "int",
    "user_age_days": "int",
    "user_level": "str",
    "user_risk_tag_cnt": "int",
}

#: 单位（前端渲染列头用；计数类无单位）
FEATURE_UNITS: dict[str, str] = {
    "login_cnt_1h": "次",
    "coupon_cnt_1h": "次",
    "order_cnt_1h": "单",
    "order_cnt_24h": "单",
    "pay_fail_cnt_24h": "次",
    "aftersale_cnt_24h": "次",
    "aftersale_rate_24h": "%",
    "device_user_cnt": "个",
    "device_order_cnt_1h": "单",
    "device_age_hours": "小时",
    "ip_user_cnt": "个",
    "ip_order_cnt_1h": "单",
    "ip_is_proxy": "",
    "address_user_cnt": "个",
    "address_aftersale_cnt": "次",
    "user_age_days": "天",
    "user_level": "",
    "user_risk_tag_cnt": "个",
}

#: 中文标签。**由后端统一下发**（`/features/meta`），前端不得硬编码（BR-04-16 同理）
FEATURE_LABELS: dict[str, str] = {
    "login_cnt_1h": "近 1 小时登录次数",
    "coupon_cnt_1h": "近 1 小时领券次数",
    "order_cnt_1h": "近 1 小时下单次数",
    "order_cnt_24h": "近 24 小时下单次数",
    "pay_fail_cnt_24h": "近 24 小时支付失败次数",
    "aftersale_cnt_24h": "近 24 小时售后次数",
    "aftersale_rate_24h": "近 24 小时退款率",
    "device_user_cnt": "同设备关联账号数",
    "device_order_cnt_1h": "同设备近 1 小时下单",
    "device_age_hours": "设备首现至今（小时）",
    "ip_user_cnt": "同 IP 关联账号数",
    "ip_order_cnt_1h": "同 IP 近 1 小时下单",
    "ip_is_proxy": "是否代理/机房 IP",
    "address_user_cnt": "同地址关联账号数",
    "address_aftersale_cnt": "同地址历史售后次数",
    "user_age_days": "账号注册至今天数",
    "user_level": "会员等级",
    "user_risk_tag_cnt": "风险标签数量",
}

#: 分组名（与 Spec §4.2 的五个分组逐字一致）
FEATURE_GROUPS: dict[str, str] = {
    **{k: "行为频次" for k in FEATURE_KEYS[:7]},
    **{k: "设备环境" for k in FEATURE_KEYS[7:10]},
    **{k: "网络 IP" for k in FEATURE_KEYS[10:13]},
    **{k: "地址聚集" for k in FEATURE_KEYS[13:15]},
    **{k: "账号画像" for k in FEATURE_KEYS[15:]},
}

#: 退款率保留位数（Spec §4.2：保留 4 位小数）
AFTERSALE_RATE_DIGITS = 4
#: 设备时长的保留位数（小时，2 位足以表达"新设备"）
DEVICE_AGE_DIGITS = 2


# ============================================================
# 画像输入（由 feature_repo 读出，纯数据）
# ============================================================
@dataclass(frozen=True)
class ProfileData:
    """一次计算用到的画像快照。

    任何一项为 `None` 表示"查不到"，对应特征进 `missing`（**不是 0**）。
    `errors` 记录读取失败的字段名，用于区分"库里没有"与"读库失败"——
    两者对研判的含义不同（前者是冷启动，后者是依赖故障）。
    """

    user_register_at: Optional[int] = None
    user_level: Optional[str] = None
    user_risk_tag_cnt: Optional[int] = None
    device_first_seen_at: Optional[int] = None
    ip_is_proxy: Optional[bool] = None
    address_aftersale_cnt: Optional[int] = None
    errors: tuple[str, ...] = ()

    @staticmethod
    def empty() -> "ProfileData":
        """全空画像（读库不可用时的等价物）。"""
        return ProfileData()

    def missing_reason(self, field: str) -> str:
        """给出"这一项为什么没有"的可读原因（写进快照供人工复核）。

        `errors` 里的每一项形如 `"<字段名>=<详情>"`，因此用 `startswith` 匹配：
        只有按字段登记，才能把"库里没有这条画像"（冷启动）与"读库失败"
        （依赖故障）分开——两者在复核时的处置完全不同。
        """
        for item in self.errors:
            if item.startswith(f"{field}="):
                return f"画像读取失败（{item}）"
        return f"画像无记录（{field}）"


# ============================================================
# 返回值
# ============================================================
@dataclass(frozen=True)
class FeatureResult:
    """一次特征计算的结果（`feature_service` 据此组装快照）。"""

    features: dict[str, Any]
    missing_features: list[str]
    missing_reasons: dict[str, str]
    #: 计算过程中抛异常的项（FEA-5001 的"异常项"）。它是 `missing_features`
    #: 的子集，单独留一份是为了让服务层判断"这次是真的算错了"而不是"数据不足"。
    error_features: tuple[str, ...] = ()


# ============================================================
# 计算实现
# ============================================================
def compute_features(
    event: dict,
    view: WindowView,
    profile: Optional[ProfileData] = None,
    *,
    linked_user_counts: Optional[dict[str, Optional[int]]] = None,
    window_first_seen: Optional[dict[str, int]] = None,
) -> FeatureResult:
    """算出 18 项特征。

    | 参数 | 含义 |
    |---|---|
    | `event` | 当前事件（已 `ingest` 进窗口，BR-04-02） |
    | `view` | `[ts - long_window, ts]` 的窗口视图 |
    | `profile` | E10~E13 的画像读取结果（可为空） |
    | `linked_user_counts` | 模块 09 给的关联账号数：`{dimension: count 或 None}` |
    | `window_first_seen` | 画像缺失时 `device_age_hours` 的回落依据：`{dimension: 最早 ts}` |

    `linked_user_counts` 里的 `None` 表示"09 无法计算"，此时对应特征进 `missing`
    （BR-04-07 + BR-04-09）——**不得用 0 冒充"该设备只关联 0 个账号"**。
    """
    data = profile if profile is not None else ProfileData.empty()
    counts = dict(linked_user_counts or {})
    first_seen = dict(window_first_seen or {})

    values: dict[str, Any] = {}
    missing: list[str] = []
    reasons: dict[str, str] = {}
    errored: list[str] = []

    def fail(name: str, reason: str, *, error: bool = False) -> None:
        """统一登记缺失项（幂等：同一项只记一次）。"""
        if name not in missing:
            missing.append(name)
        reasons.setdefault(name, reason)
        if error and name not in errored:
            errored.append(name)

    def ok(name: str, value: Any) -> None:
        values[name] = value

    def guard(name: str):
        """把单项计算包进 try/except：一项出错不能让整次计算没有结果。

        这是 FEA-5001 要求的"返回可用子集"的落点——若让异常冒泡，
        服务层只能交出一份空特征，而空特征会让 05 把"什么都不知道"
        当成"什么坏事都没发生"。
        """

        class _Guard:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, exc_type, exc, _tb) -> bool:
                if exc_type is None:
                    return False
                fail(name, f"计算异常：{exc_type.__name__}: {exc}", error=True)
                return True  # 吞掉异常，继续算下一项

        return _Guard()

    ts = int(view.event_ts)

    # ---------- 分组一 · 行为频次 ----------
    # 全部取 `user` 维度 + 长/短窗计数。当前事件已在窗口内（BR-04-02），
    # 因此首发事件即得 1（V-04-03）。
    with guard("login_cnt_1h"):
        ok("login_cnt_1h", view.count_short("user", "login"))

    with guard("coupon_cnt_1h"):
        ok("coupon_cnt_1h", view.count_short("user", "coupon_receive"))

    with guard("order_cnt_1h"):
        ok("order_cnt_1h", view.count_short("user", "order_create"))

    with guard("order_cnt_24h"):
        ok("order_cnt_24h", view.count_long("user", "order_create"))

    with guard("pay_fail_cnt_24h"):
        ok("pay_fail_cnt_24h", _pay_fail_count(view))

    with guard("aftersale_cnt_24h"):
        ok("aftersale_cnt_24h", view.count_long("user", "after_sale_apply"))

    with guard("aftersale_rate_24h"):
        orders = values.get("order_cnt_24h")
        aftersales = values.get("aftersale_cnt_24h")
        if orders is None or aftersales is None:
            # 依赖的两项没算出来，退款率自然也"无法计算"
            fail("aftersale_rate_24h", "依赖 order_cnt_24h / aftersale_cnt_24h，二者未算出")
        elif int(orders) <= 0:
            # BR-04-08：分母为 0 **必须标记缺失**，不得返回 0。
            # 0 的含义是"退款率是 0%"，而事实是"这个用户根本没下过单，无法计算"。
            fail("aftersale_rate_24h", "近 24h 无下单（分母为 0），无法计算退款率")
        else:
            ok("aftersale_rate_24h",
               round(int(aftersales) / max(int(orders), 1), AFTERSALE_RATE_DIGITS))

    # ---------- 分组二 · 设备环境 ----------
    with guard("device_user_cnt"):
        _linked(ok, fail, "device_user_cnt", "device", counts)

    with guard("device_order_cnt_1h"):
        if not view.identities.get("device"):
            # 该事件**没带设备维度**（如 order_pay）：这一项不是"算不出来"，
            # 而是"本次事件与设备无关"，返回 0 是正确的口径（设备上没有下单记录）。
            # 与"有设备但窗口里查不到"的结果一致，故不标记缺失。
            ok("device_order_cnt_1h", 0)
        else:
            ok("device_order_cnt_1h", view.count_short("device", "order_create"))

    with guard("device_age_hours"):
        _device_age(ok, fail, ts, data, first_seen)

    # ---------- 分组三 · 网络 IP ----------
    with guard("ip_user_cnt"):
        _linked(ok, fail, "ip_user_cnt", "ip", counts)

    with guard("ip_order_cnt_1h"):
        if not view.identities.get("ip"):
            ok("ip_order_cnt_1h", 0)
        else:
            ok("ip_order_cnt_1h", view.count_short("ip", "order_create"))

    with guard("ip_is_proxy"):
        if data.ip_is_proxy is None:
            fail("ip_is_proxy", data.missing_reason("ip_is_proxy"))
        else:
            ok("ip_is_proxy", bool(data.ip_is_proxy))

    # ---------- 分组四 · 地址聚集 ----------
    with guard("address_user_cnt"):
        _linked(ok, fail, "address_user_cnt", "address", counts)

    with guard("address_aftersale_cnt"):
        if data.address_aftersale_cnt is None:
            fail("address_aftersale_cnt", data.missing_reason("address_aftersale_cnt"))
        else:
            ok("address_aftersale_cnt", int(data.address_aftersale_cnt))

    # ---------- 分组五 · 账号画像 ----------
    with guard("user_age_days"):
        if data.user_register_at is None:
            fail("user_age_days", data.missing_reason("user_register_at"))
        else:
            days = (ts - int(data.user_register_at)) // 86_400_000
            # 负值只可能来自"注册时间晚于事件时间"（时钟问题）。夹到 0 而不是
            # 记成负数：负年龄在条件求值里会同时小于所有下限，制造出更危险的误判。
            ok("user_age_days", max(0, int(days)))

    with guard("user_level"):
        if not data.user_level:
            fail("user_level", data.missing_reason("level"))
        else:
            ok("user_level", str(data.user_level))

    with guard("user_risk_tag_cnt"):
        if data.user_risk_tag_cnt is None:
            fail("user_risk_tag_cnt", data.missing_reason("risk_tags"))
        else:
            ok("user_risk_tag_cnt", int(data.user_risk_tag_cnt))

    # 兜底扫描：任何既没进 values 也没被显式标记的键（将来新增特征却忘了实现）
    # 在这里被暴露为缺失，而不是从快照里静默消失——`V-04-02` 要求 18 项键名恒在。
    # 注意：这里**不调用 `fail` 的 error 分支**，因为"没实现"≠"算错了"。
    for key in FEATURE_KEYS:
        if key not in values:
            fail(key, reasons.get(key, "本次未产出该特征"))

    return FeatureResult(
        features=values,
        missing_features=sorted(missing, key=FEATURE_KEYS.index),
        missing_reasons={k: v for k, v in reasons.items() if k in missing},
        error_features=tuple(sorted(errored, key=FEATURE_KEYS.index)),
    )


def _linked(ok, fail, name: str, dimension: str, counts: dict[str, Optional[int]]) -> None:
    """向 09 取关联账号数（BR-04-07）。

    `counts` 里根本没有该维度、或值为 `None`，都表示"09 无法给出这个数"。
    两种写法都出现是因为：默认的 `UnavailableLinkedUserCountProvider` 返回 `None`，
    而测试注入的假 provider 可能只填自己关心的维度。
    """
    value = counts.get(dimension)
    if value is None:
        fail(name, f"模块 09 未提供 {dimension} 关联账号数（不可计算，不以 0 冒充）")
    else:
        ok(name, int(value))


def _pay_fail_count(view: WindowView) -> int:
    """`pay_fail_cnt_24h`：近 24h `order_pay` 且 `success=false` 的事件数。

    成败标志由窗口条目自己携带（`make_entry` 的第 4 位 `pay_ok`，
    取自 `scene_extra.success`）。**为什么必须带它**：若窗口只记事件类型，
    "失败的支付笔数"就不可知——那时只有两条错路：把全部支付当成失败（虚高，
    会把正常用户判成盗卡试探），或者恒返回 0（伪造"从未失败"，BR-04-09 禁止）。
    多存一个布尔值的代价远小于这两种错法。
    """
    return view.count_long("user", "order_pay", pay_ok=False)


def _device_age(
    ok, fail, ts: int, data: ProfileData, first_seen: dict[str, int],
) -> None:
    """`device_age_hours`：`(now - 设备首次出现时间) / 3600`。

    优先 E11 的 `first_seen_at`（长期口径，归 09）；查不到时回落到**窗口内**
    最早出现时间（近似，见模块 docstring）。两者都没有 → 标记缺失（BR-04-09）。
    """
    start = data.device_first_seen_at
    if start is None:
        start = first_seen.get("device")
    if start is None:
        fail("device_age_hours", data.missing_reason("device_first_seen_at"))
        return
    hours = (ts - int(start)) / 3_600_000
    ok("device_age_hours", round(max(0.0, hours), DEVICE_AGE_DIGITS))


__all__ = [
    "AFTERSALE_RATE_DIGITS",
    "DEVICE_AGE_DIGITS",
    "FEATURE_DATA_TYPES",
    "FEATURE_GROUPS",
    "FEATURE_KEYS",
    "FEATURE_LABELS",
    "FEATURE_UNITS",
    "LINKED_COUNT_FEATURES",
    "FeatureResult",
    "ProfileData",
    "compute_features",
]
