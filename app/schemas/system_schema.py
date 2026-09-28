# -*- coding: utf-8 -*-
"""系统设置的请求 / 响应模型（模块 13，Spec 13 §3.1 ~ §3.5）。

## 校验为什么"宽严分明"（与模块 06 的同一取舍）

| 字段 | 位置 | 理由 |
|---|---|---|
| `username` 形态（4~32、`[A-Za-z0-9_.-]`） | **模型层**（→ `COM-4001`） | 纯格式问题，与登录接口的同一份约束（直接复用 `auth_schema.USERNAME_PATTERN`，避免两处各写一个正则） |
| `role` 取值域 | **模型层**（`Literal`） | 枚举写错属 `COM-4001`；BR-13-12 的"不允许自定义角色"由"取值域只有三个"直接落地 |
| 六个运行参数的**取值域** | **服务层**（→ `SYS-4001`/`SYS-4002`） | BR-13-01 是业务规则且有专属码；若写进 `Field(ge=..., le=...)`，越界会先被 Pydantic 拦成 `COM-4001`，Spec §5 要求的 `SYS-4001` 就永远拿不到（V-13-02 会失败） |
| `engine_type` | **服务层**（→ `SYS-4003`） | 模型引擎未实现是**业务裁定**（AD-09/G-01），必须给出"模型引擎尚未实现"这句解释，而不是通用参数错误 |
| 口令长度 | **服务层**（→ `AUTH-4006`） | BR-13-19 明确"与 01 一致"，01 的口令校验在服务层（`AuthService.validate_new_password`），本模块复用它而**不复制一份规则** |

`initial_password` 与 `new_password` 因此只声明"非空"，长度判断交给
`AuthService.validate_new_password`——两处各写一份长度规则，迟早出现
"这里放行、那里拒绝"。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import USER_STATUS_DELETED
from app.enums import EngineType, FuseMode, Role, SysUserStatus
from app.schemas.auth_schema import USERNAME_PATTERN

#: E19 `role` 取值域（BR-13-12：仅三者之一，**不允许自定义角色**）
ROLES: tuple[str, ...] = tuple(r.value for r in Role)
#: E19 `status` 取值域 + 软删除态（BR-13-17；`deleted` 不在 E19 的枚举字典里，
#: 是 Spec §8「新增」行的裁定，因此这里显式列出并配中文标签）
USER_STATUSES: tuple[str, ...] = (
    SysUserStatus.ACTIVE.value, SysUserStatus.DISABLED.value, USER_STATUS_DELETED,
)
#: E20 `engine_type` / `fuse_mode` 取值域（§2.4 的下拉）
ENGINE_TYPES: tuple[str, ...] = tuple(e.value for e in EngineType)
FUSE_MODES: tuple[str, ...] = tuple(f.value for f in FuseMode)

REAL_NAME_MAX = 32
#: 运行参数里可**立即生效**的字段（BR-13-02 的 `applied`）
IMMEDIATE_FIELDS: tuple[str, ...] = (
    "short_window_min", "long_window_min", "window_capacity",
    "list_cache_ttl_sec", "decision_timeout_ms",
)
#: 需**重启**才生效的字段（BR-13-07 的 `requires_restart`）
RESTART_FIELDS: tuple[str, ...] = ("metric_bucket_granularity",)
#: 六个运行参数的名字（顺序即页面 §2.1 的呈现顺序）
CONFIG_FIELDS: tuple[str, ...] = IMMEDIATE_FIELDS + RESTART_FIELDS


def _clean_required(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise ValueError("不能为空白")
    return s


# ============================================================
# 运行参数（§3.1 / §3.2）
# ============================================================
class RuntimeConfigUpdate(BaseModel):
    """`PUT /system/config` 请求体：**部分更新**（未出现的字段保持原值）。

    刻意不给每个字段写 `ge/le`：越界必须落到 `SYS-4001`（见模块 docstring）。
    `metric_bucket_granularity` 用 `Literal` 约束取值域——它是枚举且 Spec 没有
    为"粒度写错"分配 `SYS-` 码，属通用参数错误（`COM-4001`）。
    """

    short_window_min: Optional[int] = Field(default=None, description="1~1440 分钟")
    long_window_min: Optional[int] = Field(default=None, description="1~10080 分钟")
    window_capacity: Optional[int] = Field(default=None, description="1000~1000000 条/维度")
    list_cache_ttl_sec: Optional[int] = Field(default=None, description="1~600 秒")
    decision_timeout_ms: Optional[int] = Field(default=None, description="50~5000 毫秒")
    metric_bucket_granularity: Optional[
        Literal["1m", "1h", "1d"]
    ] = Field(default=None, description="1m / 1h / 1d；变更需重启（BR-13-07）")

    @field_validator(
        "short_window_min", "long_window_min", "window_capacity",
        "list_cache_ttl_sec", "decision_timeout_ms", mode="before",
    )
    @classmethod
    def _reject_bool(cls, v: Any) -> Any:
        """布尔值不是参数值：`true` 会被 Pydantic 宽松地转成 1，
        而"窗口容量=true"这种数据一旦入库，排障时没人能解释它从哪来。
        """
        if isinstance(v, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return v


class RuntimeConfigOut(BaseModel):
    """`GET /system/config` 的 `data`（§3.1 的全部字段）。"""

    short_window_min: int
    long_window_min: int
    window_capacity: int
    list_cache_ttl_sec: int
    decision_timeout_ms: int
    metric_bucket_granularity: str
    effective_at: int = Field(description="本次配置生效时间戳（毫秒）")
    #: Spec §3.1 把这个字段的类型写作 **string**（"每次保存 +1"）。
    #: 取值形如 `w1`（从未保存过，等于模块默认）→ `w2` → `w3`…，与 04 的
    #: `WINDOW_CONFIG_VERSION="w1"` **是同一个字符串**：快照里的
    #: `window_config.config_version` 读的就是它（BR-13-08 的"随快照留存"
    #: 因此不需要在两处各维护一个版本号）。
    #: `config_version_num` 提供整数形态（`w3` → 3），方便前后端做 +1 断言
    config_version: str
    config_version_num: int
    #: 参数取值域与默认值（供前端做输入框的 min/max 与「恢复默认」，
    #: 避免前端再抄一份数字：BR-00-07 的单一来源原则同样适用于参数域）
    ranges: dict[str, list[int]] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    updated_by: Optional[str] = None


class RuntimeConfigSaveOut(BaseModel):
    """`PUT /system/config` 的 `data`（§3.2：**必须返回生效方式**）。"""

    config_version: str
    config_version_num: int
    effective_at: int
    #: 本次请求里**已经立即生效**的参数名
    applied: list[str] = Field(default_factory=list)
    #: 本次请求里**需要重启**才生效的参数名
    requires_restart: list[str] = Field(default_factory=list)
    #: 已保存但"没能完全生效"的如实告知（`SYS-5004`，BR-13-02 的延伸：
    #: 不得静默保存后不告知）。空数组表示没有任何保留意见
    notices: list[dict] = Field(default_factory=list)
    #: 结构化的 before/after（供前端在二次确认里展示"哪些值真的变了"）
    changed: dict[str, dict] = Field(default_factory=dict)


# ============================================================
# 吞吐与健康度（§3.3）
# ============================================================
class ComponentOut(BaseModel):
    """组件状态（§2.2 的组件状态表）。"""

    name: str = Field(description="组件名（与 §2.2 的表格逐字一致）")
    status: str = Field(
        description="机器可读状态：ok/error/unused/running/stopped/timeout/reserved",
    )
    status_label: str = Field(description="中文状态（正常/异常/未使用/运行中/已停止/探测超时/未接入）")
    detail: str = Field(default="", description="说明列（如「库 risk_control · 21 个集合」）")


class WindowStatsOut(BaseModel):
    """04 滑动窗口的运行状态（BR-04-15 的口径，模块 13 只做展示）。"""

    total_events: int = 0
    per_dimension_max: int = 0
    est_mem_mb: float = 0.0
    last_sweep_at: Optional[int] = None
    # 以下四项是"能回答排障问题"的附加信息（不在 Spec 的必需字段内）
    capacity: int = 0
    distinct_keys: int = 0
    dimensions: dict[str, int] = Field(default_factory=dict)
    memory_warned: bool = False
    #: 读不到窗口状态时的原因（不吞掉：`/health` 也是同一处置，只告警但可见）
    error: Optional[str] = None


class MongoStatsOut(BaseModel):
    """Mongo 连通性（§2.2 第 4 张指标卡读的就是它）。"""

    connected: bool
    latency_ms: Optional[int] = None
    db: Optional[str] = None
    collections: Optional[int] = None
    error: Optional[str] = None
    timeout: bool = False


class SystemStatsOut(BaseModel):
    """`GET /system/stats` 的 `data`（§3.3）。"""

    qps: Optional[float] = Field(default=None, description="近 1 分钟事件 QPS（11 的口径）")
    decision_p50_ms: Optional[float] = None
    decision_p95_ms: Optional[float] = None
    decision_p99_ms: Optional[float] = None
    #: 名单缓存命中率。**无样本时为 `null` 而不是 0**：0% 会显示成橙色告警，
    #: 而"还没有人查过名单"不是异常（与 BR-11-08「没有样本不给 0」同一原则）
    list_cache_hit_rate: Optional[float] = None
    list_cache_samples: int = 0
    window: WindowStatsOut = Field(default_factory=WindowStatsOut)
    mongo: MongoStatsOut
    components: list[ComponentOut] = Field(default_factory=list)
    uptime_sec: int = 0
    #: 配色阈值（§2.2：P95 > 50ms 标红、命中率 < 90% 标橙）。由后端下发，
    #: 前端不得硬编码——阈值改一次要能只改一处
    thresholds: dict[str, float] = Field(default_factory=dict)
    #: 指标数据来源（11 的 `/metrics/throughput`，说明本接口没有另算一份）
    metrics_source: dict[str, Any] = Field(default_factory=dict)
    #: 降级/告警结论码（如 `MET-5001` 指标不可用、`SYS-5003` 探测超时）
    notices: list[dict] = Field(default_factory=list)
    probed_at: int = 0


# ============================================================
# 账号与角色（§3.4）
# ============================================================
class UserCreateIn(BaseModel):
    """`POST /system/users` 请求体（§3.4）。"""

    username: str = Field(
        ..., min_length=4, max_length=32, pattern=USERNAME_PATTERN,
        description="账号（4~32 字符，字母/数字/_ . -；BR-13-11：全局唯一且创建后不可改）",
    )
    real_name: str = Field(..., min_length=1, max_length=REAL_NAME_MAX)
    role: Literal["reviewer", "strategist", "admin"]
    initial_password: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
        description="不传则由后端生成（≥12 位，仅在响应中返回一次，BR-13-19）",
    )

    @field_validator("username", "real_name")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _clean_required(v)

    @field_validator("initial_password")
    @classmethod
    def _clean_password(cls, v: Optional[str]) -> Optional[str]:
        # 口令**不 strip**：首尾空格是口令的一部分，静默去掉会让用户
        # "明明复制对了却登录不上"。只把"全空白"当作没传
        if v is not None and not v.strip():
            return None
        return v


class UserUpdateIn(BaseModel):
    """`PUT /system/users/{username}` 请求体（§3.4：只改姓名与角色）。

    `username` **不在请求体里**（BR-13-11：创建后不可修改）——想改账号只能
    新建一个再停用旧的，这样审计链上"谁曾经是哪个账号"始终可读。
    """

    real_name: Optional[str] = Field(default=None, min_length=1, max_length=REAL_NAME_MAX)
    role: Optional[Literal["reviewer", "strategist", "admin"]] = None

    @field_validator("real_name")
    @classmethod
    def _clean(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_required(v)


class UserActionIn(BaseModel):
    """停用 / 删除的请求体（可选）：`{"transfer_to": "reviewer02"}`。

    两者都可能需要"名下未处置案件先转交"（BR-13-15 / BR-13-17），因此共用
    同一形状。`transfer_to` 必须是**状态为启用的 reviewer 账号**（Spec §8 新增行）。
    """

    transfer_to: Optional[str] = Field(default=None, max_length=32, description="案件接收人（reviewer）")
    reason: Optional[str] = Field(default=None, max_length=200, description="留痕备注（可选）")

    @field_validator("transfer_to", "reason")
    @classmethod
    def _clean_optional(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip() or None


class ResetPasswordIn(BaseModel):
    """`POST /system/users/{username}/reset-password` 请求体。"""

    new_password: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
        description="不传则由后端生成（≥12 位，仅返回一次）",
    )
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("new_password")
    @classmethod
    def _clean_password(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip() or None


class UserOut(BaseModel):
    """账号列表/详情项（BR-13-20：**绝不包含 `password_hash`**）。"""

    username: str
    real_name: str = ""
    role: str = ""
    role_label: str = ""
    status: str = ""
    status_label: str = ""
    last_login_at: Optional[int] = None
    password_changed_at: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    #: 是否当前登录账号（前端据此禁用"停用/删除"按钮；后端**仍然独立校验**，
    #: BR-01-14：前端不渲染按钮只是体验优化）
    is_self: bool = False

    @classmethod
    def from_doc(cls, doc: dict, *, current_user: str = "") -> "UserOut":
        from app.constants import USER_STATUS_LABELS
        from app.enums import Role, label_of

        username = str(doc.get("_id") or doc.get("username") or "")
        role = str(doc.get("role") or "")
        status = str(doc.get("status") or "")
        # 角色标签读枚举（BR-00-18 的单一来源）；数据脏时回落成原值而不是"未知"，
        # 让排障一眼能看到库里到底存了什么
        role_label = label_of(Role, role, role)
        status_label = USER_STATUS_LABELS.get(status, status)
        return cls(
            username=username,
            real_name=str(doc.get("real_name") or ""),
            role=role,
            role_label=role_label,
            status=status,
            status_label=status_label,
            last_login_at=doc.get("last_login_at"),
            password_changed_at=doc.get("password_changed_at"),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
            is_self=bool(current_user) and username == current_user,
        )


class UserListOut(BaseModel):
    """`GET /system/users` 的 `data`（分页契约与全项目一致，模块 00 §3.2）。"""

    items: list[UserOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0
    role_counts: dict[str, int] = Field(default_factory=dict)


class UserCreateOut(BaseModel):
    """`POST /system/users` 的 `data`（§3.4：口令**只在此返回一次**）。"""

    username: str
    real_name: str = ""
    role: str = ""
    status: str = "active"
    generated_password: Optional[str] = Field(
        default=None, description="仅当未传 initial_password 时出现，且只返回这一次",
    )
    user: Optional[UserOut] = None


class UserPasswordResetOut(BaseModel):
    """`POST /system/users/{username}/reset-password` 的 `data`。"""

    username: str
    generated_password: Optional[str] = None
    password_changed_at: int = 0
    #: BR-13-16：重置后该账号**所有令牌立即失效**，需重新登录
    tokens_invalidated: bool = True
    note: str = ""


# ============================================================
# 决策引擎配置（§3.5）
# ============================================================
class EngineConfigIn(BaseModel):
    """`PUT /system/engine-config` 请求体（§3.5）。

    `engine_type` 这里**不做 Literal 约束**：取值非法与"取值合法但未实现"
    （`model`/`hybrid`）必须给不同的答复——后者要返回 `SYS-4003`
    「模型引擎尚未实现（G-01）」，用 Literal 会先被拦成 `COM-4001`。
    """

    engine_type: str = Field(..., description="rule / model / hybrid；当前仅 rule 可用")
    fuse_mode: Optional[Literal["weighted", "max", "rule_first"]] = None
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("engine_type")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _clean_required(v)


class EngineConfigOut(BaseModel):
    """`GET /system/engine-config` 的 `data`（§3.5）。

    `model_available` **恒为 false**（AD-09）：这个字段存在的意义就是让页面
    有一个明确的判据去画那根橙色警示条，而不是靠前端硬编码"我们知道模型没接"。
    """

    engine_type: str = EngineType.RULE.value
    fuse_mode: str = FuseMode.RULE_FIRST.value
    rule_weight: float = 1.0
    model_weight: float = 0.0
    model_available: bool = False
    status: str = "enabled"
    engine_version: str = ""
    model_engine: str = ""
    #: 融合方式在 `model_weight=0` 时对结果**没有影响**（三种方式都等于 rule_score）。
    #: 如实告知，避免把"融合方式下拉"误读成一个真正在起作用的开关
    fuse_mode_effective: bool = False
    note: str = ""
    reference: str = "G-01"
    updated_at: Optional[int] = None
    updated_by: Optional[str] = None
    defaults: dict[str, Any] = Field(default_factory=dict)
