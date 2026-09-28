# -*- coding: utf-8 -*-
"""模块 03：五类事件的入参模型、类型必填集与对外响应模型（§2.1 / §3.1）。

## 为什么 `event_type` 用 `str` 而不是 `Enum`

与 `list_schema.py` / `metric_schema.py` 同一条原则：**语义边界留给服务层**。
若在模型层写成 `Literal["login", ...]`，非法取值会先被 FastAPI 拦成通用
`COM-4001`（422），而 BR-03-02 要求的是 **`EVT-4003`（400）**。调用方按状态码
分流时就会走错分支（400 = 报文可修，422 = 字段可修）。因此模型层只做宽松的
结构约束，五类枚举的判定在 `event_service.validate_event` 里按 `EVT_CODES`
表给出确定的错误码。

## 为什么用「一个扁平模型 + 模型外的必填集」而不是判别联合

Spec §3.5 写的是「五类事件模型，`event_type` 为判别字段」。这里实现为：
`EventIn` 承载五类字段的并集（全部可选），`REQUIRED_BY_TYPE` /
`SCENE_EXTRA_BY_TYPE` 两张表承载「按类型的必填集」（Spec 表 3.1）。

这样做的原因是 BR-03-03 的硬要求：类型必填字段「缺失 → `422 EVT-4004`，
响应必须**逐字段列出缺失名**」。若用 Pydantic 的 `Field(...)`（判别联合）表达必填，
缺失会先被 Pydantic 拦成 `COM-4001`，响应里只有 Pydantic 的英文路径与消息，
**既拿不到 `EVT-4004`，也列不出中文缺失名**。必填性因此必须在模型之外表达，
由服务层在读完原始 dict 之后统一裁决。

## 为什么 `scene_extra` 是开放 dict 而不是每类一个模型

理由同上：`scene_extra` 的白名单违例对应 `EVT-4006`（未知键）与 `EVT-4007`
（金额不一致）。若用判别联合 + `extra="forbid"`，Pydantic 会先报 `COM-4001`
并把错误码吃掉。故白名单以**数据表**（`SCENE_EXTRA_BY_TYPE`）形式声明在本文件
（本模块仍是事件 Schema 的唯一所有者，模块 10 的仿真页表单也从它派生），
判定在服务层。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.enums import EventType

# ============================================================
# 五类事件与「按类型的必填集」（Spec §3.1 表，固化自悬空点 G-07）
# ============================================================
# 取值唯一真源是 `enums.EventType`（BR-00-16）：这里只做一次导出，
# 不在本文件重新写一遍字面量——两份字面量迟早会漂移。
EVENT_TYPES: tuple[str, ...] = tuple(m.value for m in EventType)

# `event_type -> 基础必填字段`（Spec 表 3.1 的「基础必填」列）
REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "login": ("user_id", "device_id", "ip"),
    "coupon_receive": ("user_id", "device_id", "ip", "amount"),
    "order_create": ("user_id", "device_id", "ip", "address_id", "amount"),
    "order_pay": ("user_id", "amount"),
    "after_sale_apply": ("user_id", "biz_no", "amount"),
}

# `event_type -> (scene_extra 必填键, scene_extra 可选键)`（Spec 表 3.1 后两列）
SCENE_EXTRA_BY_TYPE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "login": (("login_type",), ("ua", "success")),
    "coupon_receive": (("coupon_id", "activity_id"), ("face_value", "batch_id")),
    "order_create": (("order_no", "sku_count"), ("total_amount", "address_id")),
    "order_pay": (("order_no", "pay_channel"), ("pay_amount", "card_tail")),
    "after_sale_apply": (
        ("after_sale_no", "order_no", "reason_code"),
        ("refund_amount", "received_goods"),
    ),
}

# `event_type -> 必须与顶层 `amount` 一致的 scene_extra 金额字段`（BR-03-06）。
# 五类里只有 `login` 没有金额语义，故不在此表。
AMOUNT_FIELD_BY_TYPE: dict[str, str] = {
    "coupon_receive": "face_value",
    "order_create": "total_amount",
    "order_pay": "pay_amount",
    "after_sale_apply": "refund_amount",
}

# `login_type` 的取值（Spec 表 3.1：pwd / sms / scan）
LOGIN_TYPES: tuple[str, ...] = ("pwd", "sms", "scan")
# 售后原因码（BR-03-28 的 `refund` 模式固定用 `not_received`）
REASON_CODES: tuple[str, ...] = (
    "not_received", "damaged", "wrong_item", "quality", "other",
)

# `source` 的可选值（§2.1）：页面手填与模拟器两条来路必须可区分，
# 否则事后无法回答「这条事件是真人投的还是模拟器刷的」。
SOURCE_MANUAL_SIM = "manual_sim"
SOURCE_MOCK_BIZ = "mock_biz"
SOURCES: tuple[str, ...] = (SOURCE_MANUAL_SIM, SOURCE_MOCK_BIZ)

# `event_id` 格式（BR-03-09）：`EVT{yyyyMMdd}{12位序列}`
EVENT_ID_PATTERN = r"^EVT\d{8}\d{12}$"

# 字段级约束（BR-03-04 / BR-03-07）
USER_ID_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
DEVICE_ID_MIN, DEVICE_ID_MAX = 3, 64
ADDRESS_ID_MIN, ADDRESS_ID_MAX = 3, 32
BIZ_NO_MAX = 64
PHONE_PATTERN = r"^1[3-9]\d{9}$"
# `ts` 不得超前于 `received_at` 超过 60s（BR-03-07：时钟超前）
TS_MAX_AHEAD_MS = 60_000

# 批量上限（BR-03-16 / N-03-6）
BATCH_MAX = 500

# 响应信封里决策块所占的字段名（= 顶层字段，见 `EventIngestOut`）
SCENE_EXTRA_OPTIONAL_NOTE = "scene_extra 的可选键服务于模块 05 的特征提取，缺失不影响接入"


def scene_extra_spec(event_type: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """取某事件类型的 `(scene_extra 必填键, scene_extra 可选键)`。

    未知类型返回 `((), ())`：调用方（服务层）在此之前已用 `EVENT_TYPES`
    判定过 `EVT-4003`，这里不重复报错。
    """
    return SCENE_EXTRA_BY_TYPE.get(event_type, ((), ()))


def required_fields(event_type: str) -> tuple[str, ...]:
    """取某事件类型的基础必填字段（Spec 表 3.1）。"""
    return REQUIRED_BY_TYPE.get(event_type, ("user_id",))


# ============================================================
# 请求模型
# ============================================================
class EventIn(BaseModel):
    """单条事件入参（§3.1 请求表）。

    全部字段可选，**必填性由 `required_fields()` 在服务层裁决**（理由见模块
    docstring）。`extra="allow"` 是必要的：顶层多余字段要么落进 `scene_extra`
    的未知键判定（`EVT-4006`），要么被如实体现在载荷哈希里；模型层直接丢弃
    会让这两件事永远做不到。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # 幂等键。Spec §3.1 请求表里叫 `event_id`，决策记录 D3 在入参侧改名为
    # `client_event_id`（`event_id` 变成响应里服务端生成的编号）。
    # 为不改动既有验收口径，这里**两个名字都接受**：`event_id` 是别名，
    # 谁传都算幂等键（解析见 `resolve_client_event_id`）。
    client_event_id: Optional[str] = Field(default=None, alias="event_id")
    event_type: Optional[str] = None
    user_id: Optional[str] = None
    biz_no: Optional[str] = None
    device_id: Optional[str] = None
    ip: Optional[str] = None
    phone: Optional[str] = None
    address_id: Optional[str] = None
    amount: Optional[int] = None
    scene_extra: Optional[dict] = None
    ts: Optional[int] = None
    source: Optional[str] = None

    def extra_keys(self) -> dict:
        """顶层多余字段（不在 §3.1 请求表里的键）。"""
        return dict(self.model_extra or {})

    def raw_payload(self) -> dict:
        """入参的原始形态（含顶层多余字段），用于计算幂等载荷哈希。"""
        raw = self.model_dump(exclude_none=True, by_alias=False)
        # 幂等键本身不参与载荷哈希：否则「同编号同内容」会因为键名不同
        # （event_id / client_event_id）被算成不同载荷，误报 EVT-4009。
        raw.pop("client_event_id", None)
        raw.update(self.extra_keys())
        return raw

    def resolve_client_event_id(self) -> Optional[str]:
        """取调用方提供的幂等键（`event_id` 或 `client_event_id`）。"""
        return self.client_event_id


class BatchIn(BaseModel):
    """`POST /events/batch` 请求体（§3.2）。

    `events` 用 `list[Any]` 而不是 `list[EventIn]`：批内单条校验失败必须
    **只计入 `failed_cnt` 而不影响其余条目**（BR-03-16），因此每条的校验错误
    都要能被逐条捕获并翻译成模块内错误码，不能由 Pydantic 整批拦掉。
    """

    model_config = ConfigDict(extra="allow")

    events: list[Any] = Field(default_factory=list)
    verbose: bool = False


# ============================================================
# 响应模型（§3.1 响应表逐字段对齐）
# ============================================================
# 决策块的 12 个字段（§3.1：`rule_score` 起至 `engine_version` 止）。
# **契约冻结**：正常路径下这些字段一律来自 05 的返回值（V-03-06），本模块只做
# 透传与字段名归一；只有降级路径才由 `event_service.degraded_decision()` 生成
# ——那种情况下 05 根本没机会产出任何东西。
DECISION_FIELDS: tuple[str, ...] = (
    "list_hit", "rule_score", "model_score", "final_score", "risk_level",
    "decision", "hit_rule_count", "hits", "rule_versions", "engine_version",
    "snapshot_id", "elapsed_ms",
)


class ListHit(BaseModel):
    """名单直通结果（Spec 05 §3.1 / E03 `list_hit`）。

    ## 为什么是**对象**而不是 `bool`（契约裁定）

    Spec 05 §3.1 把它定义为 `{hit: bool, list_type, entity_type, entity_value}`，
    而 §3.2 明确「决策块契约由 05 拥有、03 不得自行拼装」——因此形状以 05 的
    定义为准，统一为对象，**任何情况下都不是 bool**：

    - 真实判定：`hit` 为 `true`/`false`，命中时给出名单类型与具体实体；
    - **03 的降级兜底块也必须用对象**（`hit=false` + 三个描述字段为 `null`）：
      降级意味着"名单过滤根本没做"，用 `hit=false` 表达恰好正确；
    - 未命中：同样 `hit=false`。

    理由是**消费方成本**：一个字段时而是 bool 时而是对象，会让每一个消费方
    （前端判定摘要、事件仿真页、E2E、未来的 07/08）都不得不写类型分支，而
    分支里必然有一边无人验证——"真名单直通被渲染成普通的 0 分判定"正是
    这类缺陷的典型表现。统一成对象后，`hit` 字段本身承担布尔语义，
    信息只多不少（还要多出"命中在哪个维度"这条复核线索）。

    E03 的 `list_hit` 同样是对象（`{hit:bool, list_type, entity_type, entity_value}`），
    因此这个形状从响应一路到落库都是同一个，不存在第二种形态。

    ⚠️ 定义顺序有要求：它必须排在 `DecisionBlock` / `EventIngestOut` **之前**
    （两者都把它当字段类型）。放到后面会直接 `NameError`。
    """

    model_config = ConfigDict(extra="allow")

    hit: bool = False
    list_type: Optional[str] = None
    entity_type: Optional[str] = None
    entity_value: Optional[str] = None


def list_hit_miss() -> dict:
    """未命中 / 降级时的 `list_hit` 对象（**唯一构造处**）。

    集中在这里而不是各处手写 `{"hit": False, ...}`：少写一个键就会让响应体
    的 `list_hit` 在两次请求之间长度不同，而前端与 E2E 是按固定形状断言的。
    """
    return {"hit": False, "list_type": None, "entity_type": None, "entity_value": None}


def coerce_list_hit(raw: Any) -> dict:
    """把任意来源的 `list_hit` 归一成**对象**（契约兜底）。

    为什么需要兜底：`normalize_decision` 的语义是"**只补缺，不覆盖**"（V-03-06：
    05 给的取值必须原样透传）。但"原样透传一个 bool"会破坏响应契约——那是
    契约违例，不是有效取值。因此这里做**唯一的**一次形状归一：已经不是对象
    的（历史 bool 形态、脏数据、上游笔误）一律落成"未命中"对象，而**对象形态
    的内容一字不改**（`hit`/`list_type` 等全部原样保留）。
    """
    if isinstance(raw, dict):
        return dict(raw)
    return list_hit_miss()


class DecisionBlock(BaseModel):
    """决策块（字段与 05 §3.1 逐字段一致）。"""

    model_config = ConfigDict(extra="allow")

    # `list_hit` 是**对象**（见 `ListHit` 的说明）：任何情况下都不是 bool。
    list_hit: ListHit = Field(default_factory=ListHit)
    rule_score: int = 0
    model_score: Optional[float] = None
    final_score: int = 0
    risk_level: str = "low"
    decision: str = "review"
    hit_rule_count: int = 0
    hits: list[dict] = Field(default_factory=list)
    rule_versions: dict = Field(default_factory=dict)
    engine_version: str = ""
    snapshot_id: Optional[str] = None
    elapsed_ms: int = 0


class DegradeInfo(BaseModel):
    """降级信封（§3.1 的 `degrade` 字段 / BR-03-21~23）。"""

    degraded: bool = True
    reason: str = ""
    stage: str = "feature"     # feature / rule / timeout


class EventIngestOut(BaseModel):
    """`POST /events` 响应体（§3.1 响应表）。

    决策块的 12 个字段**直接摊在顶层**：§3.1 把 `event_id`/`received_at`/
    `duplicate`/`persisted`/`degrade` 与决策块并列为响应字段。
    """

    model_config = ConfigDict(extra="allow")

    event_id: str
    received_at: int
    duplicate: bool = False
    persisted: bool = False
    degrade: Optional[DegradeInfo] = None
    list_hit: ListHit = Field(default_factory=ListHit)
    rule_score: int = 0
    model_score: Optional[float] = None
    final_score: int = 0
    risk_level: str = "low"
    decision: str = "review"
    hit_rule_count: int = 0
    hits: list[dict] = Field(default_factory=list)
    rule_versions: dict = Field(default_factory=dict)
    engine_version: str = ""
    snapshot_id: Optional[str] = None
    elapsed_ms: int = 0


class BatchItemResult(BaseModel):
    """批内单条结果（`verbose=true` 时返回）。"""

    event_id: Optional[str] = None
    decision: Optional[str] = None
    ok: bool = True
    code: str = "OK"
    message: str = ""
    # 校验失败时的字段名清单（EVT-4004 逐字段列出缺失名）
    missing: list[str] = Field(default_factory=list)


class BatchOut(BaseModel):
    """`POST /events/batch` 响应体（§3.2）。"""

    total: int
    pass_cnt: int
    review_cnt: int
    reject_cnt: int
    failed_cnt: int
    elapsed_ms: int
    results: Optional[list[BatchItemResult]] = None


class EventDetailOut(BaseModel):
    """`GET /events/{event_id}` 响应体（§3.3：四段聚合读）。"""

    event: dict
    snapshot: Optional[dict] = None
    decision: Optional[dict] = None
    hits: list[dict] = Field(default_factory=list)


class SimulatorStartIn(BaseModel):
    """模拟器启动参数（§3.4 `start` 请求）。

    `mode` / `rate` 的**枚举与区间判定放在服务层**：非法 `mode` 按 §5 归
    `EVT-5005`（500，启动失败并释放资源），越界的 `rate` 归 `COM-4001`
    （报文参数拼错）——两者状态码不同，交给 Pydantic 会全部塌缩成 `COM-4001`。
    """

    mode: str = "normal"
    rate: float = 5.0
    seed: int = 42
    max_events: int = 500
    duration_sec: int = 60
    anchor_ts: Optional[int] = None


class SimulatorStatusOut(BaseModel):
    """`GET /mock/status` 响应体（§3.4）。"""

    running: bool
    mode: Optional[str] = None
    rate: Optional[float] = None
    seed: Optional[int] = None
    anchor_ts: Optional[int] = None
    emitted: int = 0
    emitted_total: int = 0
    decision_counts: dict = Field(default_factory=dict)
    started_at: Optional[int] = None
    last_error: Optional[str] = None


__all__ = [
    "ADDRESS_ID_MAX", "ADDRESS_ID_MIN", "AMOUNT_FIELD_BY_TYPE", "BATCH_MAX",
    "BIZ_NO_MAX", "BatchIn", "BatchItemResult", "BatchOut", "DECISION_FIELDS",
    "DEVICE_ID_MAX", "DEVICE_ID_MIN", "DecisionBlock", "DegradeInfo",
    "EVENT_ID_PATTERN", "EVENT_TYPES", "EventDetailOut", "EventIn",
    "EventIngestOut", "LOGIN_TYPES", "PHONE_PATTERN", "REASON_CODES",
    "REQUIRED_BY_TYPE", "SCENE_EXTRA_BY_TYPE", "SCENE_EXTRA_OPTIONAL_NOTE",
    "SOURCES", "SOURCE_MANUAL_SIM", "SOURCE_MOCK_BIZ", "SimulatorStartIn",
    "SimulatorStatusOut", "TS_MAX_AHEAD_MS", "USER_ID_PATTERN", "ListHit",
    "coerce_list_hit", "list_hit_miss", "required_fields", "scene_extra_spec",
]
