# -*- coding: utf-8 -*-
"""可替换组件的 Protocol 抽象（AD-05 / AD-08 / AD-09，模块 00 §4.2 BR-00-08）。

**为什么要抽象**：这三个组件在演示环境里都"没有真东西"——没有真实风控模型
（G-01）、没有真实业务系统可联动（G-08）、特征窗口只在进程内存里（G-02）。
如果不做抽象，模块 05/07/08 就会直接 `import` 具体实现，等到需要接入真实
组件时必须在业务代码里到处改。抽象之后，业务模块只依赖 Protocol，
`app/main.py` 负责把当前实现"装配"进去。

**为什么用 Protocol 而不是 ABC**：业务模块不需要继承任何基类（避免为测试
而被迫 mock 继承链），结构化类型即可——只要方法签名一致就算实现。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger("shop_risk_control.protocols")

#: 兜底装配时的统一提示。**必须留痕**，理由见 `_fallback`。
_FALLBACK_NOTE = (
    "默认装配兜底：%s 的真实实现导入失败，已退回占位实现（%s）。"
    "这会让链路出现本该不存在的降级——若这是意料之外的，说明存在**循环导入**"
    "或依赖缺失，请查上面的异常。"
)


def _fallback(name: str, placeholder: Any, error: BaseException) -> Any:
    """真实实现导入失败时的兜底：**记一条 WARNING 再返回占位实现**。

    ## 为什么兜底必须留痕（一次真实的踩坑）

    模块 05 落地时，`Components.decision_provider` 的默认值改成了
    `default_decision_provider()`，但它的 `except Exception` 把一次**循环导入**
    静默吞掉了：`app/protocols` 在模块末尾执行 `_COMPONENTS = Components()`，
    而该工厂会 import `app.engine.decision_provider → app.engine.decision`，
    后者在模块级又回头 `from app.protocols import get_components`——此刻
    `protocols` 还没执行到 `get_components` 的定义处，于是 `ImportError` 被
    兜底吃掉，装上的仍是占位实现。

    症状是"代码明明改对了、`/health` 却仍显示 `UnavailableDecisionProvider`"，
    而**没有任何报错**。这类"静默退回占位"正是决策 D45 要防的事
    （默认装配必须体现真实组件），因此兜底可以存在（宁可显式降级，也不要
    `ImportError` 让应用起不来），但**必须留下可查的痕迹**。
    """
    log.warning(_FALLBACK_NOTE, name, type(placeholder).__name__, exc_info=error)
    return placeholder


# ============================================================
# 1. 特征存储 FeatureStore（AD-05）
# ============================================================
@runtime_checkable
class FeatureStore(Protocol):
    """特征读写抽象。

    默认实现为进程内滑动窗口（模块 04 提供）。换成 Redis 时只需替换实现，
    模块 04/05 的代码不变。
    """

    async def record(self, entity_type: str, entity_id: str, event: dict) -> None:
        """把一条事件计入窗口（在事件接入后调用）。"""

    async def window_stats(
        self, entity_type: str, entity_id: str, minutes: int
    ) -> dict[str, Any]:
        """取该实体最近 `minutes` 分钟的聚合特征（计数、金额、去重维度等）。"""

    async def baseline(self, feature_name: str, segment: str) -> Optional[dict]:
        """取 E21 特征基线（p50/p95）。**取不到必须返回 None**。

        E21 编码约束：审核工作台查不到基线时展示「—」，**严禁用 0 或本次值
        冒充基线**——那会让研判人员误判，因此接口层用 None 而不是 0 表达缺失。
        """


class NullFeatureStore:
    """空实现（默认装配）。

    返回**空特征而不是伪造特征**：伪造（例如流量为 0）会被规则引擎当成
    "该用户从未下单"这类事实参与判定，从而放过风险。宁可让特征缺失，
    由模块 04 判断"特征不可用 → fail-closed"，也不给假数据。
    """

    async def record(self, entity_type: str, entity_id: str, event: dict) -> None:
        return None

    async def window_stats(
        self, entity_type: str, entity_id: str, minutes: int
    ) -> dict[str, Any]:
        return {}

    async def baseline(self, feature_name: str, segment: str) -> Optional[dict]:
        return None


# ============================================================
# 2. 业务适配器 BizAdapter（AD-08，悬空点 G-08）
# ============================================================
@dataclass(frozen=True)
class BizSyncResult:
    """业务联动结果。**返回值永不具权威性**（模块 08 §三）——页面必须标注为「模拟同步」。"""

    target: str
    status: str          # ok / failed
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "status": self.status,
            "message": self.message,
            "retryable": self.retryable,
        }


@runtime_checkable
class BizAdapter(Protocol):
    """与外部业务系统（订单/售后/会员）联动的抽象。"""

    async def sync(self, action: str, payload: dict) -> BizSyncResult:
        """把处置动作同步给业务系统。"""


class MockBizAdapter:
    """默认实现（AD-08）：**不调用任何外部系统**，只写结构化日志并返回固定结果。

    演示环境没有可联动的真实电商后端，若假装成功且界面不标注，评委会误以为
    系统真的拦住了订单。因此 message 明确写 `no real biz system`，
    并由前端统一显示「模拟同步」。
    """

    async def sync(self, action: str, payload: dict) -> BizSyncResult:
        return BizSyncResult(
            target="mock",
            status="ok",
            message="no real biz system",
            retryable=False,
        )


# ============================================================
# 3. 模型引擎 ModelEngine（AD-09，悬空点 G-01）
# ============================================================
@runtime_checkable
class ModelEngine(Protocol):
    """模型打分抽象。"""

    async def score(self, features: dict, context: dict) -> Optional[float]:
        """返回模型分（0~100），**不可用时返回 None**。"""


class NullModelEngine:
    """空实现（默认装配，G-01）。

    恒返回 None，因此 E03 的 `model_score` 保持为 null、`final_score` 恒等于
    `rule_score`。**这是刻意的**：PRD 只有一句"规则与模型双引擎"，没有算法、
    特征、训练数据与融合方式，凭空编一个模型比留空更糟——留空可被验收为
    "架构预留"，编造则属于虚假实现。
    """

    async def score(self, features: dict, context: dict) -> Optional[float]:
        return None


# ============================================================
# 4. 特征计算提供者 FeatureProvider（模块 03 → 04 的调用契约）
# ============================================================
# **为什么由 03 定义、04 去实现**：04（特征计算引擎）排在 03 之后开发。
# 03 是主链路起点，必须能独立跑通、独立验收，因此它只依赖一个方法签名，
# 由 04 上线时提供实现并替换装配。若 03 直接 import 04 的具体模块，
# 顺序就被颠倒了，03 的测试也会连带失败。
#
# **返回值契约**（与 Spec §3.5 的 `FeatureSnapshot` 对齐，dict 表达）：
#   {snapshot_id, features, missing_features, window_config, compute_ms,
#    degrade_suggested}
# `features` 是特征名 -> 值的映射；`missing_features` 列出本次缺失的特征名
# （缺失**不代表 0**，05 必须据此判定，绝不能把缺失当"没发生过"）。
@runtime_checkable
class FeatureProvider(Protocol):
    """特征快照计算抽象（模块 04 实现）。"""

    async def compute(self, event: dict) -> dict:
        """算一条事件的特征快照。**不可用时必须抛异常**，不得返回空快照。"""


class UnavailableFeatureProvider:
    """默认实现：**明确不可用**（模块 04 尚未实现）。

    为什么必须"不可用"而不是返回空特征：BR-03-21 要求 04 不可用时本模块
    fail-closed 降级为 `review`。空特征（例如 `order_cnt_1h: 0`）会被 05 当成
    "该用户从未下单"这类**事实**参与判定，从而放过风险——那等于用一个假的
    "一切正常"替换"不知道"，是风控系统里最危险的一类缺陷。
    **宁可报不可用、转人工，也绝不编造特征。**

    因此这里抛异常而不是返回 `{}`：异常会被 03 捕获并转成 `EVT-5002` +
    `degrade.stage="feature"`，链路上留下的是"特征服务不可用"这一事实。
    """

    #: 供 `/health` 与启动日志一眼看出"这个组件是真的还是占位"
    available = False

    async def compute(self, event: dict) -> dict:
        raise RuntimeError(
            "特征计算引擎（模块 04）尚未接入：FeatureProvider 不可用。"
            "按 BR-03-21 fail-closed，本次事件降级为 review。"
        )


# ============================================================
# 4.5 关联账号数提供者 LinkedUserCountProvider（模块 09 → 04 的调用契约）
# ============================================================
# **为什么由 04 定义、09 去实现**：开发顺序上 04 排在 09 之前（09 是画像与
# 关联图谱，需要先有事件与特征）。04 的 `device_user_cnt` / `ip_user_cnt` /
# `address_user_cnt` 三项按 BR-04-07 **一律向 09 取**，本模块**不得自建去重
# 集合**——同一指标只能有一个真源（对齐 09 的 BR-09-13/14）。
# 若 04 自己维护一份"设备→账号"映射，两处口径必然漂移，届时"团伙核心指标"
# 在特征快照与图谱页上会显示出两个不同的数字，而没人能说清哪个对。
#
# 因此 04 只依赖这一个方法签名；09 落地时提供实现并替换装配。
#
# **返回 `None` 的含义**：`None` 是"**无法计算**"，不是 0。
# `0` 是一个断言（"这个设备没有关联任何账号"），而 `None` 是"不知道"。
# 04 会把 `None` 的项写进 `missing_features`（BR-04-09：不得用 0 冒充），
# 后续由 05 的条件求值语义（BR-05-12）决定怎么处置。
@runtime_checkable
class LinkedUserCountProvider(Protocol):
    """关联账号数抽象（模块 09 实现）。"""

    async def get_linked_user_count(self, entity_type: str, entity_id: str) -> Optional[int]:
        """取该实体关联的**去重账号数**。

        `entity_type ∈ {device, ip, address}`；**无法计算时必须返回 `None`**，
        不得用 0 或估算值冒充。
        """


class UnavailableLinkedUserCountProvider:
    """默认实现：**明确不可用**（模块 09 尚未实现）。

    恒返回 `None`，因此 04 的 `device_user_cnt` / `ip_user_cnt` /
    `address_user_cnt` 三项会如实进入 `missing_features`，界面上显示
    「数据不足」（Spec §2.1 的缺失处理），而不是一个看起来正常的 `0`。

    **为什么不能默认返回 0**：`device_user_cnt = 0` 在规则里会被读成
    "这台设备是干净的、只属于一个人"，而真相是"我们根本没有关联图谱"。
    团伙识别恰恰依赖这三项，用假 0 把团伙判成正常，是 04 最严重的一类缺陷。
    """

    available = False

    async def get_linked_user_count(
        self, entity_type: str, entity_id: str
    ) -> Optional[int]:
        return None


# ============================================================
# 5. 规则决策提供者 DecisionProvider（模块 03 → 05 的调用契约）
# ============================================================
# **返回值契约**（与 Spec §3.1 的决策块逐字段一致，dict 表达）：
#   {list_hit, rule_score, model_score, final_score, risk_level, decision,
#    hit_rule_count, hits, rule_versions, engine_version, snapshot_id, elapsed_ms}
# 其中 `hits` 是命中明细（E04 的待写入内容）。
#
# **契约冻结**：03 **不得自行拼装**决策块，必须直接透传本方法的返回值
# （V-03-06 / `00` §3.3）。03 里唯一会"自己造"决策块的地方是降级路径，
# 而那条路径恰恰是 05 完全没产出的时候。
@runtime_checkable
class DecisionProvider(Protocol):
    """规则决策抽象（模块 05 实现）。"""

    async def evaluate(self, event: dict, features: dict) -> dict:
        """对一条事件求值并给出决策块。**不可用时必须抛异常**。"""


class UnavailableDecisionProvider:
    """**明确不可用**的决策提供者（兜底装配，不再是默认值）。

    模块 05 已落地，`Components.decision_provider` 的默认值指向真实实现
    `RuleDecisionProvider`（决策 D45）。本类保留两个用途：

    1. **兜底**：05 的依赖在某个部署里缺失时回落到它（宁可每个事件都
       `review`，也不要 `ImportError` 让应用起不来）；
    2. **测试**：用例显式装回它，才能继续钉住"05 被摘除 → `stage=rule`"
       这条契约（默认装配已经不是它了，不再能顺带覆盖）。

    与 `UnavailableFeatureProvider` 同理，且更危险：05 是唯一有权给出
    `pass/reject` 的组件。若这里返回一个伪造的决策块（哪怕写着 `review`），
    调用方就无法区分"规则引擎判了 review"与"规则引擎根本没跑"——前者是结论，
    后者是故障，两者的处置与追责完全不同。因此抛异常，由 03 转成
    `EVT-5003` + `degrade.stage="rule"`，并**绝不允许**变成 `pass`
    （BR-03-22：fail-closed 硬要求）。
    """

    available = False

    async def evaluate(self, event: dict, features: dict) -> dict:
        raise RuntimeError(
            "规则决策引擎（模块 05）尚未接入：DecisionProvider 不可用。"
            "按 BR-03-22 fail-closed，本次事件降级为 review（绝不返回 pass）。"
        )


# ============================================================
# 组件装配（由 app/main.py 在 lifespan 中调用）
# ============================================================
def default_feature_provider() -> FeatureProvider:
    """默认的特征提供者：**真实的模块 04**（FeatureService）。

    ## 为什么这里可以直接指向具体实现

    `FeatureService`（`app/services/feature_service.py`）实现本文件的
    `FeatureProvider` 契约，并且**永不抛异常**：任何失败都转成
    `degrade_suggested=True` 的可用子集，由 03 按 fail-closed 转 `review`。
    因此把它作为默认装配**不会**让链路变成"每个事件都 500"。

    ## 为什么采用「延迟导入 + 兜底」

    定义在本文件里而不是 04 的模块里，是为了让"默认装的是谁"只有一处可看；
    用延迟导入是为了避免 `protocols → services.feature_service → protocols`
    的循环导入（`FeatureService._linked()` 要读本文件的全局组件）。
    兜底到 `UnavailableFeatureProvider` 覆盖两种情况：
    ① 04 的依赖在某个部署里缺失（宁可显式降级，也不要 `ImportError` 让应用起不来）；
    ② 04 的装配被显式摘除（回退到"每个事件都 review"的保守行为）。

    ## 为什么 `FeatureStore` 仍然默认是 `NullFeatureStore`

    `feature_store` 与 `feature_provider` 是**两个不同的契约**：
    前者是"按任意实体取任意窗口统计"的通用聚合口（AD-05 预留的 Redis 替换点），
    后者是"算 18 项特征快照"的冻结契约。模块 04 的 `FeatureService` 虽然也提供了
    `window_stats` / `baseline` 兼容面，但把它同时装进 `feature_store` 会让
    "窗口状态存在几份"变得含糊——窗口是**有状态的单例**，两处引用应当是同一个
    对象，而这件事由 `FeatureService` 自身保证即可，不必经 `feature_store` 再暴露一次。
    """
    try:
        from app.services.feature_service import get_feature_service

        return get_feature_service()
    except Exception as e:  # noqa: BLE001 - 04 不可用时必须显式降级，而不是让应用起不来
        return _fallback("FeatureProvider（模块 04）", UnavailableFeatureProvider(), e)


def default_linked_user_count_provider() -> "LinkedUserCountProvider":
    """默认的关联账号数提供者：**真实的模块 09**（决策 D45）。

    D45 的原话是"默认装配必须体现真实组件"。09 落地之前这里装
    `UnavailableLinkedUserCountProvider` 是正确的（那时候确实没有实现），
    继续装着它就会把一个**已经存在的真实组件**藏起来：04 的三项聚集度特征
    会永远进 `missing_features`，界面上永远显示「数据不足」，
    而"设备关联账号数"恰恰是团伙识别的核心指标。

    与 `default_feature_provider` 同样采用「延迟导入 + 兜底」：
    延迟导入避免 `protocols → services.profile_service → repos → ...` 的
    模块级循环；兜底覆盖"09 的依赖在某个部署里缺失"（宁可显式降级为"无法计算"，
    也不要 `ImportError` 让应用起不来）。
    """
    try:
        from app.services.profile_service import MongoLinkedUserCountProvider

        return MongoLinkedUserCountProvider()
    except Exception as e:  # noqa: BLE001 - 09 不可用时必须显式降级，而不是让应用起不来
        return _fallback(
            "LinkedUserCountProvider（模块 09）", UnavailableLinkedUserCountProvider(), e
        )


def default_decision_provider() -> "DecisionProvider":
    """默认的规则决策提供者：**真实的模块 05**（决策 D45）。

    D45 的原话是"默认装配必须体现真实组件"。05 落地之前这里装
    `UnavailableDecisionProvider` 是正确的（那时候确实没有实现）；继续装着它
    就会把一个**已经存在的真实组件**藏起来——每个事件都会停在
    `degrade.stage="rule"`，`decisions`/`decision_hits` 永远是空表，
    07 的判定摘要、08 的建案、11 的规则命中排行全部没有数据可展示。

    与 `default_feature_provider` / `default_linked_user_count_provider` 同样
    采用「延迟导入 + 兜底」：
    - 延迟导入避免 `protocols → engine.decision → repos → services → protocols`
      的模块级循环（`decision.py` 要读本文件的 `get_components()`）；
    - 兜底覆盖"05 的依赖在某个部署里缺失"（宁可显式降级为"不可用 → 每个事件
      都 review"，也不要 `ImportError` 让应用起不来）。
    """
    try:
        from app.engine.decision_provider import RuleDecisionProvider

        return RuleDecisionProvider()
    except Exception as e:  # noqa: BLE001 - 05 不可用时必须显式降级，而不是让应用起不来
        return _fallback("DecisionProvider（模块 05）", UnavailableDecisionProvider(), e)


@dataclass
class Components:
    """当前生效的可替换组件集合。业务模块通过 `get_components()` 取用。"""

    feature_store: FeatureStore = field(default_factory=NullFeatureStore)
    biz_adapter: BizAdapter = field(default_factory=MockBizAdapter)
    model_engine: ModelEngine = field(default_factory=NullModelEngine)
    # 模块 03 的两个下游（04 / 05）**都已落地**，因此两格都指向真实实现
    # （见 `default_feature_provider` / `default_decision_provider`，决策 D45）。
    # 03 里唯一还会"自己造决策块"的地方是它自己的降级路径（04 短路、超时、
    # 下游抛异常），那种情况下 05 根本没有机会产出任何东西。
    feature_provider: FeatureProvider = field(default_factory=default_feature_provider)
    decision_provider: DecisionProvider = field(default_factory=default_decision_provider)
    # 模块 04 → 09 的调用口（BR-04-07）：04 的三项聚集度特征一律向 09 取。
    # 09 已落地，因此默认装配指向它的真实实现（`MongoLinkedUserCountProvider`）。
    linked_user_count_provider: LinkedUserCountProvider = field(
        default_factory=default_linked_user_count_provider
    )

    def describe(self) -> dict[str, str]:
        """供 `/health` 与启动日志说明"现在装的是哪套实现"。"""
        return {
            "feature_store": type(self.feature_store).__name__,
            "biz_adapter": type(self.biz_adapter).__name__,
            "model_engine": type(self.model_engine).__name__,
            "feature_provider": type(self.feature_provider).__name__,
            "decision_provider": type(self.decision_provider).__name__,
            "linked_user_count_provider": type(
                self.linked_user_count_provider
            ).__name__,
        }


_COMPONENTS = Components()


def get_components() -> Components:
    """取当前组件集合（进程内单例）。"""
    return _COMPONENTS


def configure_components(**kwargs: Any) -> None:
    """替换组件实现（测试或后续模块接入真实实现时使用）。"""
    for name, value in kwargs.items():
        if not hasattr(_COMPONENTS, name):
            raise AttributeError(f"未知组件：{name}")
        setattr(_COMPONENTS, name, value)
