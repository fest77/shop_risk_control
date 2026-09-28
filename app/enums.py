# -*- coding: utf-8 -*-
"""全项目枚举的**唯一真源**（BR-00-16 / BR-00-18）。

取值来源：`01_数据实体/数据实体设计.md` §4「枚举字典」——共 22 组，与
BR-00-18 的"必须与 Step1 枚举字典完全一致"逐组对应。

**关键约束（BR-00-17）**：E05/E07/E08/E10/E19/E20 六个实体都有名为 `status`
的字段，但**取值域完全不同**。因此这里按实体各自定义枚举类
（`RuleStatus` / `ListStatus` / `CaseStatus` / `UserStatus` / `SysUserStatus`
/ `ModelStatus`），**刻意不提供通用 `StatusEnum`**——否则必然出现
"规则 status=enabled 被写成案件 pending"这类脏数据。

**前端如何拿到中文标签**：`GET /api/v1/common/enums` 直接下发本文件的
`{value, label}`，前端不得再各自硬编码中文（BR-00-18 / §3.3 单一来源原则）。
"""
from __future__ import annotations

from enum import StrEnum
from typing import TypeVar


class LabeledStrEnum(StrEnum):
    """带中文标签的字符串枚举。

    成员写法为 `(取值, 中文标签)` 二元组，例如：

        class Decision(LabeledStrEnum):
            PASS = ("pass", "放行")

    这样 `Decision.PASS == "pass"`（可直接与库中字符串比较、可直接序列化），
    同时 `Decision.PASS.label == "放行"` 可下发给前端。
    """

    label: str

    def __new__(cls, value: str, label: str) -> "LabeledStrEnum":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label
        return obj


# ============================================================
# 1. 事件与决策域
# ============================================================
class EventType(LabeledStrEnum):
    """E01.event_type —— 电商链路的五类风控事件（PRD 2.3.1）。"""

    LOGIN = ("login", "登录")
    COUPON_RECEIVE = ("coupon_receive", "领券")
    ORDER_CREATE = ("order_create", "下单")
    ORDER_PAY = ("order_pay", "支付")
    AFTER_SALE_APPLY = ("after_sale_apply", "售后申请")


class Decision(LabeledStrEnum):
    """E03.decision / E08.decision —— 决策结论。fail-closed 时只能取 REVIEW。"""

    PASS = ("pass", "放行")
    REVIEW = ("review", "转人工审核")
    REJECT = ("reject", "拦截")


class RiskLevel(LabeledStrEnum):
    """E03.risk_level / E08.risk_level —— 风险等级（分档阈值见模块 05）。"""

    LOW = ("low", "低风险")
    MEDIUM = ("medium", "中风险")
    HIGH = ("high", "高风险")


# ============================================================
# 2. 规则与名单域
# ============================================================
class RuleStatus(LabeledStrEnum):
    """E05.status —— 规则启停状态。"""

    ENABLED = ("enabled", "启用")
    DISABLED = ("disabled", "停用")


class ListType(LabeledStrEnum):
    """E07.list_type —— 名单类型，取值对齐 E07。"""

    BLACK = ("black", "黑名单")
    WHITE = ("white", "白名单")
    GRAY = ("gray", "灰名单")


class ListStatus(LabeledStrEnum):
    """E07.status —— 名单条目状态。"""

    ACTIVE = ("active", "生效中")
    EXPIRED = ("expired", "已过期")
    REMOVED = ("removed", "已移除")


class EntityType(LabeledStrEnum):
    """E07.entity_type / E14.from_type / E14.to_type —— 风控五维实体。

    注意（E14 编码约束）：边的一端若为 `phone` 属非法——手机号是用户属性，
    不做独立图节点，图模块需在构造边时显式断言。
    """

    USER = ("user", "用户")
    PHONE = ("phone", "手机号")
    IP = ("ip", "IP")
    DEVICE = ("device", "设备")
    ADDRESS = ("address", "地址")


class ConditionOp(LabeledStrEnum):
    """E05.condition 节点.op —— 条件树支持的比较算子（模块 05 校验其合法性）。"""

    EQ = ("eq", "等于")
    NE = ("ne", "不等于")
    GT = ("gt", "大于")
    GTE = ("gte", "大于等于")
    LT = ("lt", "小于")
    LTE = ("lte", "小于等于")
    IN = ("in", "属于")
    NOT_IN = ("not_in", "不属于")
    EXISTS = ("exists", "存在")
    CONTAINS = ("contains", "包含")


# ============================================================
# 3. 案件与处置域
# ============================================================
class CaseStatus(LabeledStrEnum):
    """E08.status —— 案件状态。"""

    PENDING = ("pending", "待审核")
    REVIEWING = ("reviewing", "审核中")
    DISPOSED = ("disposed", "已处置")
    ARCHIVED = ("archived", "已归档")


class Conclusion(LabeledStrEnum):
    """E09.conclusion —— 审核结论。"""

    VIOLATION = ("violation", "违规")
    NORMAL = ("normal", "正常")
    SUSPICIOUS = ("suspicious", "可疑")


class ActionType(LabeledStrEnum):
    """E09.action_type —— 处置动作。"""

    PASS = ("pass", "放行")
    BLOCK_ORDER = ("block_order", "拦截订单")
    BLACKLIST_USER = ("blacklist_user", "拉黑用户")
    BAN_DEVICE = ("ban_device", "封禁设备")
    REJECT_REFUND = ("reject_refund", "拒绝退款")


class RiskTag(LabeledStrEnum):
    """E08.risk_tags / E10.risk_tags —— 风险标签。"""

    DEVICE_CLUSTER = ("device_cluster", "设备聚集")
    IP_CLUSTER = ("ip_cluster", "IP 聚集")
    ADDRESS_CLUSTER = ("address_cluster", "地址聚集")
    NEW_ACCOUNT = ("new_account", "新注册账号")
    HIGH_FREQ = ("high_freq", "高频操作")
    AFTERSALE_ABUSE = ("aftersale_abuse", "售后滥用")
    BLACKLIST_HISTORY = ("blacklist_history", "历史黑名单")
    PROXY_IP = ("proxy_ip", "代理 IP")


# ============================================================
# 4. 画像与图谱域
# ============================================================
class Level(LabeledStrEnum):
    """E10.level —— 会员等级（仅此一处使用，无歧义）。"""

    NORMAL = ("normal", "普通会员")
    SILVER = ("silver", "白银会员")
    GOLD = ("gold", "黄金会员")
    VIP = ("vip", "VIP 会员")


class UserStatus(LabeledStrEnum):
    """E10.status —— 用户账号状态。"""

    ACTIVE = ("active", "正常")
    FROZEN = ("frozen", "已冻结")
    BANNED = ("banned", "已封禁")


class Relation(LabeledStrEnum):
    """E14.relation —— 实体关联关系。"""

    USED_DEVICE = ("used_device", "共用设备")
    SHARED_IP = ("shared_ip", "共用 IP")
    SHARED_ADDRESS = ("shared_address", "共用地址")
    SAME_PHONE = ("same_phone", "同手机号")
    TRANSFERRED_TO = ("transferred_to", "案件转交")


# ============================================================
# 5. 指标与监控域
# ============================================================
class BucketType(LabeledStrEnum):
    """E15.bucket_type —— 指标桶维度。"""

    GLOBAL = ("global", "全局")
    RULE = ("rule", "按规则")
    LEVEL = ("level", "按会员等级")
    SCENE = ("scene", "按场景")


class Granularity(LabeledStrEnum):
    """E15.granularity —— 指标桶粒度。"""

    MINUTE = ("1m", "分钟")
    HOUR = ("1h", "小时")
    DAY = ("1d", "天")


# ============================================================
# 6. 系统与权限域
# ============================================================
class Role(LabeledStrEnum):
    """E19.role —— 三种角色（PRD 2.3.2）。权限矩阵见模块 01。"""

    REVIEWER = ("reviewer", "风控审核员")
    STRATEGIST = ("strategist", "风控策略师")
    ADMIN = ("admin", "系统管理员")


class SysUserStatus(LabeledStrEnum):
    """E19.status —— 系统账号状态。"""

    ACTIVE = ("active", "启用")
    DISABLED = ("disabled", "停用")


class EngineType(LabeledStrEnum):
    """E20.engine_type —— 决策引擎类型（G-01：当前仅 rule 有实现）。"""

    RULE = ("rule", "规则引擎")
    MODEL = ("model", "模型引擎")
    HYBRID = ("hybrid", "融合引擎")


class FuseMode(LabeledStrEnum):
    """E20.fuse_mode —— 规则与模型分值的融合方式（当前无模型，仅架构预留）。"""

    WEIGHTED = ("weighted", "加权求和")
    MAX = ("max", "取较大值")
    RULE_FIRST = ("rule_first", "规则优先")


class ModelStatus(LabeledStrEnum):
    """E20.status —— 模型配置状态。"""

    ENABLED = ("enabled", "启用")
    DISABLED = ("disabled", "停用")


# ============================================================
# 枚举组注册表：`/api/v1/common/enums` 的**唯一输出真源**
# ------------------------------------------------------------
# BR-00-18：这里必须与 Step1 枚举字典的 22 组**完全一致**，缺一组即为缺陷。
# 组名（字典的 key）即前端使用名，也是 `GET /common/enums` 的响应键。
# ============================================================
ENUM_GROUPS: dict[str, type[LabeledStrEnum]] = {
    "event_type": EventType,
    "decision": Decision,
    "risk_level": RiskLevel,
    "rule_status": RuleStatus,
    "list_type": ListType,
    "list_status": ListStatus,
    "entity_type": EntityType,
    "case_status": CaseStatus,
    "conclusion": Conclusion,
    "action_type": ActionType,
    "level": Level,
    "user_status": UserStatus,
    "relation": Relation,
    "bucket_type": BucketType,
    "granularity": Granularity,
    "role": Role,
    "sys_user_status": SysUserStatus,
    "engine_type": EngineType,
    "fuse_mode": FuseMode,
    "model_status": ModelStatus,
    "condition_op": ConditionOp,
    "risk_tag": RiskTag,
}

EnumT = TypeVar("EnumT", bound=LabeledStrEnum)


def options(enum_cls: type[EnumT]) -> list[dict[str, str]]:
    """把一个枚举类转成 `[{value, label}, ...]`，供接口下发。"""
    return [{"value": m.value, "label": m.label} for m in enum_cls]


def all_options() -> dict[str, list[dict[str, str]]]:
    """全部 22 组枚举的 `{组名: [{value,label}]}`。"""
    return {name: options(cls) for name, cls in ENUM_GROUPS.items()}


def label_of(enum_cls: type[EnumT], value: str, default: str = "未知") -> str:
    """取某取值的中文标签（后端生成文案时用，避免在业务代码里写死中文）。"""
    for m in enum_cls:
        if m.value == value:
            return m.label
    return default
