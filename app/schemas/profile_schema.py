# -*- coding: utf-8 -*-
"""模块 09 的响应模型（§3.1 用户全貌画像 / §3.2 关联网络）。

## 为什么响应模型"真的被使用"

§3.1 / §3.2 的字段是模块 07 中栏（`profileCard` + `graphBox`）的渲染依据。
手写 dict 少一个 `tag_meta`、把 `is_center` 写成 `center` 都不会在服务端报错，
只会在页面上表现为"标签没有配色""中心节点不突出"。接口层把服务端组装好的 dict
过一遍模型再回包，字段名/类型对不上会**在设计阶段**暴露。

## `null` 语义（本模块的核心约定）

本模块的每个可空字段都用 `Optional`，且 **`null` 一律表示"不知道"，绝不表示 0**：

| 字段 | `null` 的含义 | 为什么不能用 0/"" 代替 |
|---|---|---|
| `age_days` | 没有 `register_at`（我们不知道这个人什么时候注册的） | `0` 是一个断言："今天刚注册" |
| `linked_user_cnt` | 该实体没有画像行 | `0` 会宣称"这个设备只属于一个人" |
| `is_proxy` | E12 没有该 IP 或字段缺失 | `false` 会宣称"这是干净住宅 IP"（决策 D46） |
| `masked_detail` | 地址行只有 `detail_hash`，没有可展示的地理前缀 | `""` 会让前端显示一个空白的"收货地址" |
| `level` / `status` | 用户没有完整画像（只由事件侧 `$inc` 建过行） | 猜一个默认等级会让画像卡撒谎 |

这与模块 04 的 `missing_features`、模块 11 的 `block_rate=null` 是**同一条原则**：
宁可显示「—」并让人知道缺口，也不给一个看起来正常的假值。

## 为什么 `GraphEdgeOut.from_` 用别名

`from` 是 Python 关键字，不能做字段名。§3.2 的响应契约要求键名是 `from`
（前端按 `edge.from` 取），因此这里用 `Field(alias="from")`，
**序列化时按别名输出**（`model_dump(by_alias=True)`，与模块 06 的既有写法一致）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.enums import RiskTag, label_of

# ============================================================
# 风险标签配色（§2.1 的"风险标签配色表"）
# ------------------------------------------------------------
# **为什么配色要由后端下发**：§3.1 明确 `tag_meta` 是"标签 → `{label, severity}`
# 的映射（供前端配色，避免前端硬编码）"。8 个标签的三档配色（红/橙/灰）是
# **规格的一部分**，前端若自己再写一份映射表，两边迟早会漂移——而漂移的表现是
# "高危标签显示成灰色"，人工研判会据此低估风险。
#
# 中文标签**不在这里再写一遍**：`app/enums.py` 的 `RiskTag` 是全项目中文标签的
# 唯一真源（BR-00-16/18），这里只用 `label_of()` 取它，本文件只补充"规格里有、
# 枚举里没有"的配色档位。
# ============================================================
#: 标签 -> 配色档位。取值依据 Spec §2.1：红底=高危、橙底=中危、灰底=其余
TAG_SEVERITY: dict[str, str] = {
    RiskTag.DEVICE_CLUSTER.value: "high",
    RiskTag.ADDRESS_CLUSTER.value: "high",
    RiskTag.AFTERSALE_ABUSE.value: "high",
    RiskTag.BLACKLIST_HISTORY.value: "high",
    RiskTag.IP_CLUSTER.value: "medium",
    RiskTag.NEW_ACCOUNT.value: "medium",
    RiskTag.PROXY_IP.value: "medium",
    RiskTag.HIGH_FREQ.value: "low",
}

#: 档位 -> 具体颜色。前端**不需要**再维护"哪档用什么颜色"的映射；
#: 同时给出档位与颜色，是为了让前端既能直接用颜色、也能按档位做排序/筛选
#: （例如"只看高危标签"），而不必自己反推档位。
SEVERITY_COLOR: dict[str, str] = {
    "high": "red",
    "medium": "orange",
    "low": "gray",
}


def build_tag_meta() -> dict[str, dict[str, str]]:
    """组装 `tag_meta`：**8 个标签全部下发**，而不是只下发命中过的。

    为什么下发全集：前端拿到 `risk_tags` 后要渲染标签组，若 `tag_meta` 只有
    命中项，前端就不得不为"没见过的标签"写兜底样式（通常是一个灰色 `span`），
    而"没见过的标签"恰恰可能是高危。全集下发让 8 个标签的配色都在服务端一处定义。
    """
    meta: dict[str, dict[str, str]] = {}
    for tag in RiskTag:
        severity = TAG_SEVERITY.get(tag.value, "low")
        meta[tag.value] = {
            "label": label_of(RiskTag, tag.value),
            "severity": severity,
            "color": SEVERITY_COLOR.get(severity, "gray"),
        }
    return meta


# ============================================================
# §3.1 用户全貌画像
# ============================================================
class ProfileUserOut(BaseModel):
    """`user`（§3.1 第 1 行）。"""

    model_config = ConfigDict(extra="allow")

    user_id: str
    #: 手机号**脱敏后**的值（BR-09-05：库里存的就是脱敏值，接口不再二次处理）
    phone_masked: Optional[str] = None
    register_at: Optional[int] = None
    #: 注册至今天数；`register_at` 缺失时为 `null`（不是 0）
    age_days: Optional[int] = None
    level: Optional[str] = None
    status: Optional[str] = None
    risk_tags: list[str] = Field(default_factory=list)


class ProfileStatOut(BaseModel):
    """`stat`（E10 的 `stat`，BR-09-02）。四个计数**恒为整数**（增量维护的产物）。"""

    order_cnt: int = 0
    aftersale_cnt: int = 0
    block_cnt: int = 0
    #: 单位分（E10 的约定）
    total_amount: int = 0


class LatestDecisionOut(BaseModel):
    """`latest_decision`（BR-09-06：只保留最近一次，覆盖式更新）。

    与 E03 的决策记录是两个东西：这里是**画像卡上的摘要**，历史在 `decisions`。
    """

    risk_score: Optional[float] = None
    risk_level: Optional[str] = None
    decision: Optional[str] = None
    decided_at: Optional[int] = None


class ProfileDeviceOut(BaseModel):
    """`device`（E11）。**N ≥ 5 标红**的判断在前端按 `linked_user_cnt` 做。"""

    device_id: str
    linked_user_cnt: int = 0
    first_seen_at: Optional[int] = None
    os: Optional[str] = None


class ProfileIpOut(BaseModel):
    """`ip`（E12）。`is_proxy=true` 时前端标橙（§2.1）。"""

    ip: str
    linked_user_cnt: int = 0
    #: E12 的 `is_proxy` / `is_idc` 合取结果；字段缺失时 `null`（**不是 false**）
    is_proxy: Optional[bool] = None
    region: Optional[str] = None
    isp: Optional[str] = None


class ProfileAddressOut(BaseModel):
    """`address`（E13）。

    `masked_detail` **绝不是明文地址**（BR-09-05）：库里只有
    `province/city/district` 与 `detail_hash`，展示值由前三者拼出并掩码。
    """

    address_id: str
    masked_detail: Optional[str] = None
    linked_user_cnt: int = 0
    aftersale_cnt: int = 0


class TagMetaOut(BaseModel):
    """单个标签的展示元数据（§3.1 的 `tag_meta[tag]`）。"""

    label: str
    #: `high` / `medium` / `low`（红 / 橙 / 灰）
    severity: str
    #: `red` / `orange` / `gray`（直接可用，前端不必自己映射档位）
    color: str


class ProfileOut(BaseModel):
    """`GET /api/v1/profiles/{user_id}` 的 `data`（§3.1 逐字段对齐）。"""

    model_config = ConfigDict(extra="allow")

    user: ProfileUserOut
    stat: ProfileStatOut = Field(default_factory=ProfileStatOut)
    latest_decision: Optional[LatestDecisionOut] = None
    device: Optional[ProfileDeviceOut] = None
    ip: Optional[ProfileIpOut] = None
    address: Optional[ProfileAddressOut] = None
    tag_meta: dict[str, TagMetaOut] = Field(default_factory=build_tag_meta)


# ============================================================
# §3.2 关联网络
# ============================================================
class GraphCenterOut(BaseModel):
    """`center`（§3.2）。"""

    type: str
    id: str
    label: str


class GraphNodeOut(BaseModel):
    """`nodes[]`（§3.2）。

    `risk_level` / `risk_tags` / `linked_user_cnt` 由服务端**批量补齐**
    （BR-09-20：不允许前端对每个节点再单独请求——那就是 N+1 换了个位置）。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    label: str
    #: 0 = 中心节点，1 = 一跳实体，2 = 二跳账号
    hop: int
    risk_level: Optional[str] = None
    risk_tags: list[str] = Field(default_factory=list)
    linked_user_cnt: Optional[int] = None
    is_center: bool = False


class GraphEdgeOut(BaseModel):
    """`edges[]`（§3.2）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: 别名 `from`（Python 关键字不能做字段名，见模块 docstring）
    from_: str = Field(alias="from")
    to: str
    #: `used_device` / `shared_ip` / `shared_address` / `same_phone` / `transferred_to`
    relation: str
    #: 关联强度（共现次数）。截断时按它降序保留（BR-09-17）
    weight: int = 0
    #: `true` 时前端画红色边
    risk_flag: bool = False
    last_seen_at: Optional[int] = None


class GraphOut(BaseModel):
    """`GET /api/v1/graph/{entity_type}/{entity_id}` 的 `data`（§3.2 逐字段对齐）。

    `truncated` / `total_nodes` / `total_edges` **必须如实**（BR-09-17/18）：
    截断时 `total_*` 给的是**截断前**的真实数量，供前端渲染
    「关系过多，已展示关联强度最高的 N 个节点（共 M 个）」。
    静默截断等于让人拿着不完整的图下结论——那是本模块最不能犯的错。
    """

    model_config = ConfigDict(extra="allow")

    center: GraphCenterOut
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    truncated: bool = False
    total_nodes: int = 0
    total_edges: int = 0
    #: 查询耗时（BR-09-19：P95 < 300ms 的度量口径）
    elapsed_ms: int = 0


__all__ = [
    "GraphCenterOut",
    "GraphEdgeOut",
    "GraphNodeOut",
    "GraphOut",
    "LatestDecisionOut",
    "ProfileAddressOut",
    "ProfileDeviceOut",
    "ProfileIpOut",
    "ProfileOut",
    "ProfileStatOut",
    "ProfileUserOut",
    "SEVERITY_COLOR",
    "TAG_SEVERITY",
    "TagMetaOut",
    "build_tag_meta",
]
