# -*- coding: utf-8 -*-
"""仿真链路编排：分步调用 03/04/05，收集 trace，比对预期（模块 10 §6）。

## 本模块唯一的"核心价值"：**不复制判定逻辑**（BR-10-01）

Spec 05 §3.1 的原话是「仿真模块（10）**必须调用此接口**（`/engine/evaluate`）
以复用真实链路，**禁止另写判定逻辑**」。05 又把这句实体化成 `sim_api.py` 的
同一句话：「仿真走 `/engine/evaluate`（或 05 的同一编排入口）」。

本模块走的是后者：**`app.engine.decision.decide()` 就是 05 的那个编排入口**
（`/engine/evaluate` 只是它的一层 HTTP 壳）。调用它的**唯一实测入口**，
因此：

| 本模块**绝对不做**的事 | 由谁做 |
|---|---|
| 条件树求值 | `app/engine/evaluator.evaluate_tree`（05） |
| 分值累加与截断 | `app/engine/arbiter.arbitrate`（05，经 `rule_engine.finalize`） |
| 三档仲裁 | 同上 |
| 名单匹配 | `app/engine/list_filter.match_lists`（05） |
| 事件体校验 | `app/services/event_service.validate_event`（03，与 `POST /events` 同一函数） |
| 场景映射 | `app/engine/rule_engine.scene_for`（05） |

本模块**只做三件**：① 按顺序调用它们；② 记录每步的耗时与结论（`engine/tracer`）；
③ 把事件**只读地**交给 04 取特征（`FeatureService.compute(affect_window=False)`）。

## 一致性是怎么被"结构性"保证的（不是靠对齐代码）

`decide()` 接受一个 `features` 参数：非 `None` 时它**不再自己向 04 取特征**。
本模块正是用这个参数把"只读算出来的特征"喂给真实编排：

    features = await 04.compute(event, affect_window=False, persist=False)  # 唯一额外动作
    outcome  = await 05.decide(event, features, dry_run=True, ...)          # 真实链路

于是"仿真结果"与"直接调 `/engine/evaluate`"之间的**唯一**差别是
"特征从哪来"，而两边的特征都来自**同一个 `FeatureService.compute`**
（同一个窗口、同一批画像、同一份 18 项纯计算）。分值、命中、决策、规则版本
全部由 05 现算——**没有任何一处需要人工保持同步**，因此也不可能漂移。

## 数据隔离（BR-10-05/06，Spec §4.2 的"极易出错的点"）

| 项 | 落点 |
|---|---|
| `decisions` / `decision_hits` | `decide(dry_run=True)`——05 的 `_finish()` 在 dry_run 时直接 return |
| **04 的特征窗口** | `compute(affect_window=False)`——**不 `ingest`**。这是本模块**必须自己解决**的一处：`/engine/evaluate` 的请求体里没有这个开关，因为它的宿主就是模块 10（决策 D11 把 `affect_window` 指派给了本模块） |
| E02 `feature_snapshots` | `compute(persist=False)`——仿真不是一次接入，不产出"没有对应事件"的快照 |
| E15 指标桶（D66） | 不落 `decisions` ⇒ `_record_metrics` 根本不会被调用（05 的 `dry_run` 分支在它之前就 return）。本模块**不自己绕过**任何东西 |
| `risk_events` / `risk_cases` / 名单 / 图 | 本模块对它们**没有写调用**（`sim_runs` 是唯一被写入的集合，BR-10-08） |

## 只读特征的两个如实代价（不掩盖）

1. **"含本次"的窗口计数会比真实链路少 1**：真实链路先 `ingest` 再 `compute`
   （BR-04-02），只读模式没有这一笔。要抹平这个差就必须写窗口，而那正是
   BR-10-06 禁止的。因此如实偏低，并由 `window_premise` 声明这个前提。
2. **`elapsed_ms` 的口径**：`arbitrate` 步的耗时是"最终决策块的组装 + 仲裁"，
   不是"05 内部仲裁那一行的纳秒数"。05 没有为这一步单独打点，
   本模块**不编造**这个数字（任务书 §3：「缺哪一步耗时数据就报告」）——
   它由真实执行的耗时差表达，并在 `payload.timing_source` 里写明来源。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app import db as db_module
from app.constants import (
    SIM_BATCH_AMOUNT_RIPPLE,
    SIM_BATCH_DEFAULT_REPEAT,
    SIM_BATCH_MAX_REPEAT,
)
from app.engine import list_filter, rule_engine
from app.engine.decision import DecisionOutcome
from app.engine.rule_engine import RuleEvaluation
from app.engine.tracer import SimTracer
from app.errors import (
    AppError,
    SimBatchLimitError,
    SimDuplicateCaseNameError,
    SimEngineUnavailableError,
    SimEventInvalidError,
    SimRunNotFoundError,
    SimTimeoutError,
    sim_error,
)
from app.logging import get_logger
from app.repos.rule_repo import RuleRepo
from app.repos.sim_case_repo import STATUS_ACTIVE, SimCaseRepo
from app.repos.sim_run_repo import SimRunRepo
from app.schemas.event_schema import SOURCE_MANUAL_SIM
from app.schemas.sim_schema import (
    CASE_CATEGORIES,
    EXPECTED_DECISIONS,
    STEP_LABELS,
    STEP_NAMES,
    parse_scene_extra,
)
from app.services.audit_service import audit
from app.utils.ids import new_sim_case_id
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.sim")

# ============================================================
# 步骤序号（与 `STEP_NAMES` 一一对应，写死为常量避免"魔法数字"散落）
# ============================================================
STEP_VALIDATE = 1
STEP_FEATURES = 2
STEP_LIST = 3
STEP_RULES = 4
STEP_FINAL = 5

#: `window_premise`（BR-10-07）：仿真特征是基于**当前真实窗口**的只读快照。
#: 它是给页面看的**前提声明**，不是可以省略的注释——窗口为空时特征偏低，
#: 没有这句话，用户会把"偏低"当成"规则没生效"。
WINDOW_PREMISE = (
    "特征基于当前真实特征窗口的只读快照（未写入本次事件）。"
    "真实链路的窗口含当前这一笔，因此仿真里'含本次'的计数可能少 1。"
)

#: `affect_window=true` 时的前提声明（D11 的开关，默认关闭）
WINDOW_PREMISE_AFFECTED = (
    "本次仿真已按 affect_window=true 把事件写入 04 的真实特征窗口"
    "（会改变后续真实事件的判定，仅排障时使用）。"
)


# ============================================================
# 结果对象
# ============================================================
@dataclass
class SimOutcome:
    """一次仿真执行的完整产物（服务层返回给接口层的载荷）。

    它不是"响应模型"：接口层还要包信封、还要按 `degraded` 决定抛哪个码。
    这里只承载**已经算出来的事实**，因此 `record_saved` / `degraded` 这些
    "关于本次执行"的位也在这里，而不是让接口层再去猜。
    """

    run_id: Optional[str] = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    missing_features: list[str] = field(default_factory=list)
    list_hit: dict[str, Any] = field(default_factory=dict)
    hits: list[dict[str, Any]] = field(default_factory=list)
    rule_score: int = 0
    final_score: int = 0
    #: 命中规则条数。**必须与 05 的块同源**（不是 `len(hits)` 就地算出来）：
    #: `len(hits)` 在"命中但明细缺失"的异常数据下会与 05 报的数不一致，
    #: 而 07/08、前端与 E2E 都按这个数字渲染"命中 N 条"。
    hit_rule_count: int = 0
    risk_level: str = "low"
    decision: str = "review"
    expected_decision: Optional[str] = None
    matched_expected: Optional[bool] = None
    elapsed_ms: int = 0
    rule_versions: dict[str, Any] = field(default_factory=dict)
    engine_version: str = ""
    event_id: str = ""
    case_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    affects_window: bool = False
    degraded: bool = False
    degrade_code: Optional[str] = None
    degrade_reason: Optional[str] = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    record_saved: bool = True
    #: BR-10-12 的结论块（总耗时 / 各步之和 / 编排开销 / 是否超线）
    timing: dict[str, Any] = field(default_factory=dict)
    window_premise: str = WINDOW_PREMISE

    def to_data(self) -> dict[str, Any]:
        """组装 Spec §3.3 的响应 `data`（冻结字段 + 审计性附加字段）。"""
        return {
            "run_id": self.run_id,
            "steps": self.steps,
            "features": self.features,
            "missing_features": self.missing_features,
            "list_hit": self.list_hit,
            "hits": self.hits,
            "rule_score": self.rule_score,
            "final_score": self.final_score,
            "hit_rule_count": self.hit_rule_count,
            "risk_level": self.risk_level,
            "decision": self.decision,
            "expected_decision": self.expected_decision,
            "matched_expected": self.matched_expected,
            "elapsed_ms": self.elapsed_ms,
            # Spec §3.3：「`dry_run` | bool | 恒 `true`（仿真不落业务库）」
            "dry_run": True,
            # BR-10-04：结论基于哪一版规则（D60 的全量快照）
            "rule_versions": self.rule_versions,
            "engine_version": self.engine_version,
            # ---- 以下为附加的审计性字段（不在冻结表里，但页面必须看得到） ----
            "event_id": self.event_id,
            "case_id": self.case_id,
            "snapshot_id": self.snapshot_id,
            "affects_window": self.affects_window,
            "window_premise": self.window_premise,
            "degraded": self.degraded,
            "degrade_code": self.degrade_code,
            "degrade_reason": self.degrade_reason,
            "warnings": self.warnings,
            "record_saved": self.record_saved,
            "timing": self.timing,
        }


@dataclass
class BatchOutcome:
    """一次批量回放的产物（Spec §3.4）。"""

    total: int = 0
    matched: int = 0
    mismatched: int = 0
    false_positive: int = 0
    mismatch_samples: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    case_id: Optional[str] = None
    expected_decision: Optional[str] = None
    seed: int = 0
    repeat: int = 0
    decision_counts: dict[str, int] = field(default_factory=dict)
    run_ids: list[str] = field(default_factory=list)
    affects_window: bool = False
    #: 有多少条**没能产出结论**（引擎不可用 / 校验失败）。
    #: 它不是 Spec 的字段，但必须回传：`total=20 / matched+ mismatched=15`
    #: 这种账目对不上的响应，如果不解释，用户会以为统计逻辑坏了。
    failed: int = 0

    def to_data(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "false_positive": self.false_positive,
            "mismatch_samples": self.mismatch_samples,
            "elapsed_ms": self.elapsed_ms,
            "case_id": self.case_id,
            "expected_decision": self.expected_decision,
            "seed": self.seed,
            "repeat": self.repeat,
            "decision_counts": self.decision_counts,
            "run_ids": self.run_ids,
            "affects_window": self.affects_window,
            "failed": self.failed,
            "definition": (
                "matched/mismatched 按『实际 decision 是否等于用例 expected_decision』统计；"
                "false_positive = 预期 pass 但被判 review/reject 的条数（误伤）。"
                "expected_decision 为空时 matched/mismatched/false_positive 恒为 0"
                "（无误伤标注可依据，不臆造结论）。"
            ),
        }


# ============================================================
# 步骤记录里用到的小工具（纯函数，便于单测）
# ============================================================
def _event_field_of(exc: AppError) -> Optional[str]:
    """从 03 的校验异常里取出**出错的具体字段名**。

    03 的 `_check_formats` 在 `data` 里带了 `field`（`EVT-4005`），
    `EVT-4002/4004` 带的是 `missing` 清单。**本模块不自己猜字段名**——
    猜错会把"`ip` 格式非法"显示成"`device_id` 格式非法"，
    对用户的伤害大于不显示。

    取不到就返回 `None`（前端据此只渲染一条通用提示），**不编一个默认值**。
    """
    data = exc.data if isinstance(exc.data, dict) else {}
    field = data.get("field")
    if isinstance(field, str) and field.strip():
        return field.strip()
    missing = data.get("missing")
    if isinstance(missing, list) and missing:
        first = missing[0]
        return str(first) if first else None
    return None


def rule_block(
    *,
    list_hit: dict[str, Any],
    score: int,
    level: str,
    decision: str,
    rule_versions: dict[str, Any],
    engine_version: str,
    elapsed_ms: int,
    hits: Optional[list[dict[str, Any]]] = None,
    snapshot_id: Optional[str] = None,
) -> dict[str, Any]:
    """组装决策块（与 `app/engine/decision.py::_empty_block` 的字段形状逐项一致）。

    ## 为什么需要它（而不是"顺手改一下 05 让它把块给我"）

    本模块**不复制任何判定**，但**必须**在步骤 3（名单直通）那两条分支上
    自己组装那个块——因为那两条分支下 05 的 `decide()` 会正常返回一个完整的块，
    而本模块要的是"同一个块 + 由本模块实测的各步耗时"。为此：

    - 分值 / 等级 / 决策**全部来自** `rule_engine.finalize()`（= `arbiter.arbitrate`），
      本函数一个标点都不改；
    - 字段名与 05 的 `_empty_block` 逐项对齐（包括 `model_score: None` ——
      AD-09 明确它是**结论**"本次没有模型分"，不是"字段没填"）；
    - `_empty_block` 是 05 的**私有**函数（下划线前缀），跨模块直接调它会把
      "05 内部实现"变成"10 的依赖"；而它公开的对应物 `build_decision_doc`
      是**落库文档**（多出 `decided_at`/`source` 等 E03 列），不是响应块。
      因此这里按**冻结契约**（Spec 05 §3.1 的 12 字段）组装，并由
      `tests/test_sim_consistency.py` 断言"仿真块的每个字段与 `/engine/evaluate`
      的逐字段相等"——**一致性由测试钉住，而不是靠两边代码长得像**。
    """
    block: dict[str, Any] = {
        "list_hit": dict(list_hit),
        "rule_score": int(score),
        "model_score": None,
        "final_score": int(score),
        "risk_level": str(level),
        "decision": str(decision),
        "hit_rule_count": len(hits or []),
        "hits": [dict(h) for h in (hits or [])],
        "rule_versions": dict(rule_versions or {}),
        "engine_version": str(engine_version),
        "elapsed_ms": int(elapsed_ms),
    }
    if snapshot_id is not None:
        block["snapshot_id"] = snapshot_id
    return block


# ============================================================
# 内部信号：让 `_run_steps` 能把"步骤记录"带回给调用方（再翻成 SIM-5001）
# ============================================================
class _DegradedSignal(Exception):
    """引擎降级——**内部信号，不对外**。

    它携带已经跑出来的 `SimOutcome`（含五步的记录），由 `simulate()` 落进
    `sim_runs` 之后再翻成 `SIM-5001`。为什么不直接抛 `SIM-5001`：
    那样 `run_id` 与失败步骤就会在异常穿过三层时丢掉，
    而 Spec §5 的 `SIM-5001` 处置要求"整体失败并提示"——
    提示里必须能说清**哪一步**不可用（名单服务 vs 规则集），否则排障只能从日志里翻。
    """

    def __init__(self, outcome: "SimOutcome", code: str, reason: str, step: str):
        super().__init__(reason)
        self.outcome = outcome
        self.code = code
        self.reason = reason
        self.step = step


# ============================================================
# 服务
# ============================================================
class SimService:
    """仿真用例与执行的编排服务（Spec §6 的 `sim_service.py`）。

    依赖以构造参数注入（`case_repo` / `run_repo` / `audit_fn` / `feature`），
    与 `RuleService` 同一分层：**不依赖 FastAPI**，可以注入假仓储与假审计单测。
    """

    def __init__(
        self,
        case_repo: Optional[SimCaseRepo] = None,
        run_repo: Optional[SimRunRepo] = None,
        *,
        audit_fn: Any = None,
        feature_service: Any = None,
    ) -> None:
        self._case_repo = case_repo
        self._run_repo = run_repo
        self._audit_fn = audit_fn or audit
        self._feature = feature_service

    # ---------------- 依赖装配（每次取新句柄：测试会切库） ----------------
    def case_repo(self) -> SimCaseRepo:
        if self._case_repo is not None:
            return self._case_repo
        return SimCaseRepo(db_module.get_db())

    def run_repo(self) -> SimRunRepo:
        if self._run_repo is not None:
            return self._run_repo
        return SimRunRepo(db_module.get_db())

    def feature(self) -> Any:
        if self._feature is not None:
            return self._feature
        # 模块级导入：`feature_service` 不 import `decision`，
        # 因此这条边不参与任何循环（与 `decision.py` 需要函数内导入不同）
        from app.services import feature_service

        return feature_service.get_feature_service()

    # ============================================================
    # §3.1 用例列表
    # ============================================================
    async def list_cases(self) -> dict[str, Any]:
        """列出未归档用例（Spec §3.1）。"""
        repo = self.case_repo()
        docs = await repo.list_active()
        total = await repo.count_active()
        return {
            "items": [_case_out(d) for d in docs],
            "total": int(total),
        }

    # ============================================================
    # §3.2 保存用例
    # ============================================================
    async def save_case(
        self,
        payload: Any,
        operator: str,
        *,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        """保存一条用例。返回 `(响应 data, case_id)`。

        ## 审计恰好一条（D41 / BR-06-36 范式）

        结构上照 `RuleService.create_rule`：**先落库、再写审计、审计失败就补偿删除**。
        把审计塞进底层插入方法，将来任何"批量保存"路径都会变成一处两份审计
        ——"恰好一条"最容易正是在那里破掉。

        ## 保存即校验（任务书 BR-10-17 的引申）

        `event_template` 在**保存时**就用 03 的 `validate_event` 校验一次：
        BR-10-17 要求"用例模板必须包含完整事件体，载入即填充整个表单"，
        而一条存进来就跑不通的用例只会在演示现场暴露。校验失败**仍然保存**
        （用例可以是一个"待补全的草稿"，Spec 没有禁止），但把结论回填到
        `validation` 里——页面据此在该用例上显示一个黄点，
        而不是让用户点开才发现跑不了。
        """
        validation = self._validate_template(payload.event_template)
        event_template = validation.pop("event_template")

        repo = self.case_repo()
        # 友好判定（真正的防线是 `uq_sim_case_name` 部分唯一索引）
        if await repo.find_active_by_name(payload.name) is not None:
            raise SimDuplicateCaseNameError(payload.name)

        ts = now_ms()
        case_id = await new_sim_case_id(db_module.get_db(), ts)
        doc = {
            "_id": case_id,
            "name": payload.name,
            "category": payload.category,
            "expected_decision": payload.expected_decision,
            "description": payload.description,
            "event_template": event_template,
            "created_by": operator,
            "created_at": ts,
            "status": STATUS_ACTIVE,
        }
        try:
            await repo.insert(doc)
        except DuplicateKeyError as e:
            # 并发下被别人抢先占用同名：`SIM-4003` 的**真正**来源
            raise SimDuplicateCaseNameError(payload.name) from e
        except PyMongoError as e:
            raise AppError(
                "COM-5001", "仿真用例保存失败，请稍后重试", 503,
                {"detail": f"{type(e).__name__}: {e}"},
            ) from e

        try:
            await self._audit_fn(
                actor=operator, actor_role=actor_role, action="sim.case.create",
                target_type="sim_case", target_id=case_id,
                before=None,
                after={
                    "case_id": case_id, "name": doc["name"],
                    "category": doc["category"],
                    "expected_decision": doc["expected_decision"],
                    "created_at": ts,
                },
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            # 留不下痕就不做（D41）：物理删掉刚插入的文档，让这次写入等于从未发生。
            #
            # **错误码为什么用 `AUD-5001` 而不是本模块的 `SIM-` 码**：
            # Spec §5 的 SIM 码表是**冻结的 7 条**，里面没有"审计失败导致回滚"这一
            # 场景（模块 06 有 `CFG-5003`，10 没有对应码）。按 BR-00-13 不能自造
            # `SIM-5004`（Spec 无此号），而借 06 的 `CFG-5003` 会让前端把仿真页的
            # 失败按规则页的文案渲染。`AUD-5001` 是**模块 12 自己的**码、语义就是
            # "审计服务不可用"，与 ER-02「引用其他模块的错误码时保持原前缀」
            # 完全吻合——这里引用的就是审计模块的失败原因本身，无需再包一层。
            removed = await self._compensate_case(case_id)
            log.error("sim.case.create 审计写入失败，已撤销新增 case_id=%s removed=%s：%s",
                      case_id, removed, e)
            raise AppError(
                "AUD-5001",
                "审计服务不可用，本次用例保存已回滚，请稍后重试",
                503,
                {"action": "sim.case.create", "rolled_back": bool(removed),
                 "detail": f"{type(e).__name__}: {e}"},
            ) from e

        return (
            {
                "case_id": case_id,
                "created_at": ts,
                "name": doc["name"],
                "validation": validation,
            },
            case_id,
        )

    async def _compensate_case(self, case_id: str) -> bool:
        """审计失败后的补偿：物理删掉刚插入的用例（见 `save_case` 的说明）。"""
        try:
            return await self.case_repo().delete_by_id(case_id) > 0
        except AppError as e:
            log.error("用例新增补偿删除失败 case_id=%s：%s", case_id, e)
            return False

    def _validate_template(self, raw: Any) -> dict[str, Any]:
        """用 03 的真实校验器验一遍用例模板（**不抛异常**，结论回填）。

        返回 `{event_template, valid, source_code, missing, field, message}`。
        归一后的 `event_template` 是**原样入参**（只做 `scene_extra` 归一），
        不做脱敏、不补 `ts`、不生成 `event_id`——用例是**模板**，
        载入表单要回显用户当初填的东西（BR-10-17）。
        """
        # 函数内导入：`event_service` 会拉起 03 的整条装配（含 09 的协议组件）
        from app.services.event_service import validate_event

        normalized, reason = parse_scene_extra(raw if isinstance(raw, dict) else {})
        template = normalized if isinstance(normalized, dict) else {}
        result: dict[str, Any] = {
            "event_template": template,
            "valid": False,
            "source_code": None,
            "missing": [],
            "field": None,
            "message": "",
        }
        if reason:
            result["source_code"] = "SIM-4002"
            result["message"] = reason
            return result
        try:
            validate_event(dict(template), now_ms())
        except AppError as e:
            result["source_code"] = e.code
            data = e.data if isinstance(e.data, dict) else {}
            missing = data.get("missing")
            result["missing"] = [str(m) for m in missing] if isinstance(missing, list) else []
            result["field"] = _event_field_of(e)
            result["message"] = e.message
            return result
        except Exception as e:  # noqa: BLE001 - 校验器的意外异常仍属"模板不合法"
            result["source_code"] = "EVT-4001"
            result["message"] = f"{type(e).__name__}: {e}"
            return result
        result["valid"] = True
        result["message"] = "模板合法（已用与 POST /events 相同的校验器验过）"
        return result

    # ============================================================
    # §3.3 单条仿真执行（**核心**）
    # ============================================================
    async def simulate(
        self,
        event: Any,
        *,
        case: Optional[dict] = None,
        case_id: Optional[str] = None,
        affect_window: bool = False,
        operator: str = "",
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        audit_run: bool = True,
    ) -> SimOutcome:
        """跑一次完整仿真并落 `sim_runs`（Spec §3.3）。

        **五步的顺序与语义**（`STEP_NAMES`）：

        1. `event_validate` —— 03 的 `validate_event`（与 `POST /events` 同一函数）。
           失败：记 `failed` 步 + `mark_not_executed(1)`，抛 `SIM-4001`（`data` 带
           03 的原码与缺失字段，见 `SimEventInvalidError`）。
        2. `feature_extract` —— 04 的 `compute(affect_window=?, persist=False)`。
           **只读**是默认（D11 / BR-10-06）；`affect_window=true` 时才写窗口。
        3. `list_filter` —— 05 的 `match_lists`。
        4. `rule_evaluate` —— 05 的 `evaluate_rules`（**只为拿这一步的实测耗时**）。
        5. `arbitrate` + 组装 —— 结论本身来自 **05 的 `decide()`**（真实编排入口）；
           本模块不重算任何分数。
        """
        started = time.perf_counter()
        tracer = SimTracer(STEP_NAMES, STEP_LABELS)

        # ---------- 结论之前：确认用例（预期比对依据，BR-10-17） ----------
        resolved_case = case
        if resolved_case is None and case_id:
            resolved_case = await self.case_repo().find_by_id(case_id)
            # **归档用例仍然可用**：BR-10-20 的软删是为了"让历史 sim_runs 可追溯"，
            # 若这里 404，历史 run 的预期决策就永远比对不出来。
        expected_decision = _expected_of(resolved_case)

        # ---------- 步骤 1：事件校验（与 POST /events 同一套） ----------
        tracer.start(STEP_VALIDATE)
        normalized, failure = _validate_event_body(event)
        if failure is not None:
            _record_validate_failure(tracer, failure, started=started)
            outcome = self._empty_outcome(
                tracer, case_id=case_id, expected_decision=expected_decision,
                affects_window=affect_window, case=resolved_case,
                elapsed_ms=_ms(started),
            )
            await self._write_run(outcome, normalized, status="failed",
                                  audit_run=audit_run, operator=operator,
                                  actor_role=actor_role, ip=ip, ua=ua)
            raise _event_invalid_error(failure)
        event_id = normalized.get("_id") or ""
        tracer.finish(
            STEP_VALIDATE,
            f"通过 · event_id={event_id or '(未给)'} · "
            f"耗时 {tracer.elapsed_since(STEP_VALIDATE)}ms",
            payload={
                "event_id": event_id,
                "event_type": normalized.get("event_type"),
                "user_id": normalized.get("user_id"),
                "scene": rule_engine.scene_for(str(normalized.get("event_type") or "")),
                # 如实标注校验发生在哪里：03 的同一个函数（任务书 §3 的硬要求）
                "validator": "app.services.event_service.validate_event",
                "source_codes": "EVT-4001~4007 由 03 原样判定（本模块不放宽）",
            },
        )

        try:
            outcome = await self._run_steps(
                tracer, normalized, resolved_case, expected_decision, affect_window,
            )
        except _DegradedSignal as signal:
            # 引擎降级（RUL-5001/5002 或 FEA-5001）：**落记录后再把结论作废**。
            # `_DegradedSignal` 是内部信号，必须翻成对外的 `SIM-5001`——
            # 否则接口层会把它当未捕获异常兜成 `COM-5000`，与 Spec §5 的
            # 「503 引擎不可用」不符，前端也拿不到"哪一步失败"。
            degraded_outcome = signal.outcome
            await self._write_run(degraded_outcome, normalized, status="degraded",
                                  audit_run=audit_run, operator=operator,
                                  actor_role=actor_role, ip=ip, ua=ua)
            raise SimEngineUnavailableError(
                f"{signal.step} 失败：{signal.reason}",
                run_id=degraded_outcome.run_id,
                failed_step=signal.step,
                source_code=signal.code,
            ) from signal
        except AppError:
            # 已经是对外的 SIM-* / 03 的 EVT-* 码：原样透出（ER-02），不重复包装
            raise
        except Exception:
            # 未预期异常：不在这里吞（交给 00 的兜底成 COM-5000），但要留日志。
            # **不写一条"已失败"的 sim_runs**：这条路径的成因是"我们自己有 bug"，
            # 此时 `outcome` 根本不存在，硬拼一条只有半截字段的记录，
            # 反而会让 BR-10-13（回看一致性）拿到一份结构不完整的 trace。
            log.exception("[SIM-5001] 仿真执行出现未预期异常 event_id=%s", event_id)
            raise
        # 成功路径的落库在这里**唯一**发生（`_run_steps` 不写库，见它的 docstring）
        outcome.record_saved = await self._write_run(
            outcome, normalized, status="ok", audit_run=audit_run,
            operator=operator, actor_role=actor_role, ip=ip, ua=ua,
        )
        return outcome

    async def _run_steps(
        self,
        tracer: SimTracer,
        event: dict,
        resolved_case: Optional[dict],
        expected_decision: Optional[str],
        affect_window: bool,
    ) -> SimOutcome:
        """步骤 2~5 的主体（抽成独立方法，让 `simulate` 的失败分支保持扁平）。

        **成功路径不在这里落库**：`sim_runs` 的写入统一由调用方 `simulate()`
        负责，失败路径由它的 `except` 负责——"谁写记录"只有一个答案，
        否则将来加一条新分支时必然出现"某条路径没写"或"写了两条"。
        """
        # ---------- 步骤 2：特征提取（04，只读） ----------
        tracer.start(STEP_FEATURES)
        snapshot = await self.feature().compute(
            event, affect_window=bool(affect_window), persist=False,
        )
        features = snapshot.get("features") or {}
        missing = list(snapshot.get("missing_features") or [])
        snapshot_id = snapshot.get("snapshot_id")
        feature_status = "ok" if snapshot.get("status") == "ok" else "failed"
        tracer.finish(
            STEP_FEATURES,
            f"完成 · 耗时 {tracer.elapsed_since(STEP_FEATURES)}ms · 缺失 {len(missing)} 项"
            + ("" if feature_status == "ok" else
               f" · {snapshot.get('status')}：{snapshot.get('error') or ''}"),
            status=feature_status,
            payload={
                "missing_features": missing,
                "missing_reasons": snapshot.get("missing_reasons") or {},
                "degrade_suggested": bool(snapshot.get("degrade_suggested")),
                "degrade_reasons": snapshot.get("degrade_reasons") or [],
                "window_config": snapshot.get("window_config") or {},
                "affects_window": bool(affect_window),
                "feature_count": len(features),
                # 缺哪一项耗时数据就说清楚：这里是 04 自己的 `compute_ms`
                "compute_ms": snapshot.get("compute_ms"),
                "timing_source": "04.compute 的墙钟（compute_ms 由 04 自报）",
            },
        )

        # ---------- 步骤 3：名单过滤（05 的 match_lists） ----------
        tracer.start(STEP_LIST)
        list_hit_obj = await list_filter.match_lists(event)
        list_hit = list_hit_obj.to_dict()
        if list_hit_obj.hit:
            kind = "白名单" if list_hit_obj.list_type == "white" else "黑名单"
            advice = "直接放行" if list_hit_obj.list_type == "white" else "直接拦截"
            list_detail = (
                f"命中{kind} · {list_hit_obj.entity_type}={list_hit_obj.entity_value}"
                f" · {advice}（本轮不求值任何规则）"
            )
        else:
            list_detail = "未命中黑白名单 · 继续规则判定"
        tracer.finish(STEP_LIST, list_detail, payload={"list_hit": list_hit})

        # ---------- 步骤 4 的耗时与明细：05 的规则求值 ----------
        # 这里**只服务于"步骤 4 的实测耗时"与"逐条规则求值明细"**——分值、命中、
        # 决策一律不从它取（避免"两边各算一遍"的分叉风险），全部由下面的
        # `decide()` 给出。`evaluate_rules` 是纯函数，两份结果必然相等，
        # 这一点由 `tests/test_sim_consistency.py` 显式断言，而不是靠"看起来一样"。
        rules = await self._load_rules(event, tracer, list_hit_obj)
        if rules is None:
            # 规则集读不出来：RUL-5002（fail-closed）——不产任何结论
            outcome = self._empty_outcome(
                tracer, case_id=(resolved_case or {}).get("_id"),
                expected_decision=expected_decision, affects_window=affect_window,
                case=resolved_case, elapsed_ms=_ms_from(tracer),
                event_id=event.get("_id", ""),
                snapshot_id=snapshot_id, features=features, missing=missing,
                list_hit=list_hit,
            )
            raise _DegradedSignal(outcome, "RUL-5002", "规则集加载失败", "rule_evaluate")

        evaluated_ms, rule_evaluation = await self._timed_evaluate(rules, features)

        # ---------- 步骤 5 + 真实编排：05 的 `decide()`（**结论的唯一来源**） ----------
        tracer.start(STEP_FINAL)
        decide = _decision_module().decide
        outcome_d: DecisionOutcome = await decide(
            event,
            features,             # 只读算出来的特征：05 因此不再自己向 04 取（不重复写窗口）
            snapshot_id=snapshot_id,
            dry_run=True,         # BR-10-09：服务端强制，不信任前端传参
            trace=True,           # 本模块需要链路明细
        )
        final_elapsed = tracer.elapsed_since(STEP_FINAL)
        degraded = bool(outcome_d.degraded)

        # ---------- 步骤 4 的正式记录：名单直通 → skipped（BR-10-11） ----------
        if list_hit_obj.hit:
            tracer.skip(
                STEP_RULES,
                "未执行（名单直通，按 BR-05-02/03 不求值任何规则）",
                payload={
                    "evaluated_rules": 0,
                    "hit_rule_count": 0,
                    "rule_score": 0,
                    "list_hit": list_hit,
                    "timing_source": "not_executed",
                },
            )
        else:
            block_hits = list(outcome_d.block.get("hits") or [])
            score_from_engine = int(outcome_d.block.get("rule_score") or 0)
            tracer.finish(
                STEP_RULES,
                f"命中 {len(block_hits)} 条 · 累计 {score_from_engine} 分"
                + (f" · {len(rule_evaluation.failures)} 条求值失败"
                   if rule_evaluation.failures else ""),
                elapsed_ms=evaluated_ms,
                status="failed" if rule_evaluation.failures and not block_hits else "ok",
                payload={
                    "evaluated_rules": rule_evaluation.evaluated_rules,
                    "hit_rule_count": len(block_hits),
                    "rule_score": score_from_engine,
                    "hits": block_hits,
                    # D60：本次**生效的全量规则集**版本（不只命中项）
                    "rule_versions": dict(outcome_d.block.get("rule_versions") or {}),
                    "failures": [f.to_warning() for f in rule_evaluation.failures],
                    "rule_codes": [h.rule_code for h in rule_evaluation.hits],
                    "timing_source": "wallclock_evaluate_rules",
                },
            )

        # ---------- 步骤 5 的正式记录 ----------
        final_block = outcome_d.block
        tracer.finish(
            STEP_FINAL,
            f"score={int(final_block.get('final_score') or 0)} · "
            f"level={final_block.get('risk_level')} · "
            f"decision={str(final_block.get('decision') or '').upper()} · "
            f"总耗时 {tracer.elapsed_since(STEP_FINAL) + tracer.step_sum_ms}ms",
            status="failed" if degraded else "ok",
            elapsed_ms=final_elapsed,
            payload={
                "decision": final_block.get("decision"),
                "risk_level": final_block.get("risk_level"),
                "rule_score": int(final_block.get("rule_score") or 0),
                "final_score": int(final_block.get("final_score") or 0),
                "hit_rule_count": int(final_block.get("hit_rule_count") or 0),
                "engine_version": final_block.get("engine_version"),
                "degraded": degraded,
                "degrade_code": outcome_d.degrade_code,
                # 如实标注：这一步的耗时是"最终决策块组装 + 仲裁"的实测墙钟，
                # 05 没有为 `arbiter.arbitrate` 单独打点（任务书 §3：缺就打报告）
                "timing_source": "wallclock_decide_and_block_assembly",
                "engine_source": "app.engine.decision.decide(dry_run=True)",
            },
        )
        if degraded:
            # 降级时**没有结论**：这一步的正确表达不是"失败"，而是"没产出可用结论"。
            # 但它的状态位已经在上面写成 failed（那是给页面标红用的），
            # 这里不再 `mark_not_executed`——后面已经没有步骤了，
            # 而把最后一步改写成"未执行"会丢掉"引擎返回了降级块"这条事实。
            pass

        outcome = self._build_outcome(
            tracer, event=event, snapshot=snapshot, snapshot_id=snapshot_id,
            features=features, missing=missing, list_hit=list_hit,
            block=final_block, degraded=degraded,
            degrade_code=outcome_d.degrade_code, degrade_reason=outcome_d.degrade_reason,
            warnings=list(outcome_d.warnings or []),
            case=resolved_case, expected_decision=expected_decision,
            affects_window=affect_window, elapsed_ms=_ms_from(tracer),
        )
        if degraded:
            raise _DegradedSignal(
                outcome, outcome_d.degrade_code or "SIM-5001",
                outcome_d.degrade_reason or "引擎降级，未产出可用结论",
                "arbitrate",
            )
        return outcome

    async def _load_rules(
        self, event: dict, tracer: SimTracer, list_hit: list_filter.ListHit
    ) -> Optional[list[dict]]:
        """取本次参与求值的规则（BR-05-08/09：本场景 + `common`，只取 enabled）。

        **走 05 的 `RuleRepo`，不自己写查询**：规则集的读口径（含 `common` 的
        并入与 `priority, _id` 稳定排序）归 05，重写一份必然在"某天加了软删字段"
        时与决策链路分叉。

        失败返回 `None`（由调用方翻成 `RUL-5002`），**不返回空列表**——
        "读不到规则"与"没有规则"在风控上含义相反（`RuleRepo` 的模块说明已论证）。
        """
        if list_hit.hit:
            # 名单直通：**规则一条都不取**（真正的"未执行"，连读库都不做）
            return []
        scene_code = rule_engine.scene_for(str(event.get("event_type") or ""))
        try:
            return await RuleRepo(db_module.get_db()).list_enabled_rules(scene_code)
        except AppError as e:
            # 05 的 `RuleRepo` 正常会给 `RUL-5002`（`RuleSetUnavailableError`）
            code, message = e.code, e.message
        except Exception as e:  # noqa: BLE001 - **任何**读规则失败都必须 fail-closed
            # ## 为什么这里必须兜住"非 AppError"
            #
            # `RuleRepo.list_enabled_rules` 只把 `PyMongoError` 翻成 `RUL-5002`，
            # 别的异常（驱动换版后的新异常类型、注入的桩、装配错误、
            # `RuntimeError`）会**原样冒出去**。若本模块不兜，一次规则集读失败
            # 就会变成接口的 `500 COM-5000`，而 Spec §5 对这个场景的裁定是
            # **`503 SIM-5001`（决策引擎不可用）**：
            #
            # - 两者的**用户可见文案**不同（"服务内部错误" vs "决策引擎暂时不可用，
            #   请稍后重试"），处置也不同；
            # - 更要紧的是 `SIM-5001` 带 `conclusion_available=false`，
            #   前端据此**不渲染任何结论**；而 `COM-5000` 会让页面进入
            #   "未知错误"分支，既没有结论提示、也没有"稍后重试"的指引。
            #
            # 兜底**不改变 fail-closed 的实质**：两种分支都返回 `None`，
            # 调用方一律产"无结论"的 `SIM-5001`，绝不给出 `pass`。
            code = "RUL-5002"
            message = f"规则集加载失败（{type(e).__name__}: {e}）"
            log.warning("[SIM-5001] 规则集读取抛出非预期异常，按引擎不可用处理：%s", e)
        tracer.start(STEP_RULES)
        tracer.fail(
            STEP_RULES,
            f"规则集加载失败：{message}",
            payload={"source_code": code, "timing_source": "not_executed"},
        )
        return None

    async def _timed_evaluate(
        self, rules: list[dict], features: dict[str, Any]
    ) -> tuple[int, RuleEvaluation]:
        """跑一次 `evaluate_rules`（05 的纯函数）并返回 `(实测耗时ms, 求值结果)`。"""
        began = time.perf_counter()
        evaluation = rule_engine.evaluate_rules(rules, features, trace=True)
        elapsed = max(0, int((time.perf_counter() - began) * 1000))
        return elapsed, evaluation

    def _build_outcome(
        self,
        tracer: SimTracer,
        *,
        event: dict,
        snapshot: dict,
        snapshot_id: Optional[str],
        features: dict[str, Any],
        missing: list[str],
        list_hit: dict[str, Any],
        block: dict[str, Any],
        degraded: bool,
        degrade_code: Optional[str],
        degrade_reason: Optional[str],
        warnings: list[dict[str, Any]],
        case: Optional[dict],
        expected_decision: Optional[str],
        affects_window: bool,
        elapsed_ms: int,
    ) -> SimOutcome:
        """把五步记录与决策块组装成 `SimOutcome`（**不落库**）。"""
        decision = str(block.get("decision") or "review")
        total = tracer.finish_all() or elapsed_ms
        return SimOutcome(
            run_id=None,                    # 落库时才取号（见 `_write_run`）
            steps=tracer.steps(),
            features=dict(features),
            missing_features=list(missing),
            list_hit=dict(list_hit),
            hits=[dict(h) for h in (block.get("hits") or [])],
            rule_score=int(block.get("rule_score") or 0),
            final_score=int(block.get("final_score") or 0),
            hit_rule_count=int(block.get("hit_rule_count") or 0),
            risk_level=str(block.get("risk_level") or "low"),
            decision=decision,
            expected_decision=expected_decision,
            matched_expected=(
                None if expected_decision is None else decision == expected_decision
            ),
            elapsed_ms=int(total),
            rule_versions=dict(block.get("rule_versions") or {}),
            engine_version=str(block.get("engine_version") or ""),
            event_id=str(event.get("_id") or ""),
            case_id=str((case or {}).get("_id") or "") or None,
            snapshot_id=snapshot_id,
            affects_window=bool(affects_window),
            degraded=degraded,
            degrade_code=degrade_code,
            degrade_reason=degrade_reason,
            warnings=warnings,
            record_saved=True,
            timing=tracer.summary(total),
            window_premise=(
                WINDOW_PREMISE_AFFECTED if affects_window else WINDOW_PREMISE
            ),
        )

    def _empty_outcome(
        self,
        tracer: SimTracer,
        *,
        case_id: Optional[str],
        expected_decision: Optional[str],
        affects_window: bool,
        case: Optional[dict],
        elapsed_ms: int,
        event_id: str = "",
        snapshot_id: Optional[str] = None,
        features: Optional[dict[str, Any]] = None,
        missing: Optional[list[str]] = None,
        list_hit: Optional[dict[str, Any]] = None,
    ) -> SimOutcome:
        """失败路径上的"无结论"产物（**绝不填任何决策结论**，Spec §5 的硬要求）。"""
        total = tracer.finish_all() or elapsed_ms
        return SimOutcome(
            run_id=None,
            steps=tracer.steps(),
            features=dict(features or {}),
            missing_features=list(missing or []),
            list_hit=dict(list_hit or {"hit": False, "list_type": None,
                                       "entity_type": None, "entity_value": None}),
            hits=[],
            rule_score=0,
            final_score=0,
            hit_rule_count=0,
            risk_level="unknown",
            decision="",              # 空串：**没有结论**，不是 review
            expected_decision=expected_decision,
            matched_expected=None,
            elapsed_ms=int(total),
            rule_versions={},
            engine_version="",
            event_id=event_id,
            case_id=str(case_id or (case or {}).get("_id") or "") or None,
            snapshot_id=snapshot_id,
            affects_window=bool(affects_window),
            degraded=True,
            record_saved=True,
            timing=tracer.summary(total),
            window_premise=(
                WINDOW_PREMISE_AFFECTED if affects_window else WINDOW_PREMISE
            ),
        )

    # ============================================================
    # `sim_runs` 落库 + 审计（**唯一被写入的集合**，BR-10-08）
    # ============================================================
    async def _write_run(
        self,
        outcome: SimOutcome,
        event: dict,
        *,
        status: str,
        audit_run: bool,
        operator: str,
        actor_role: str,
        ip: Optional[str],
        ua: Optional[str],
    ) -> bool:
        """写一条 `sim_runs` + 恰好一条 `sim.run` 审计。返回 `record_saved`。

        **绝不抛异常**（Spec §5 的 `SIM-5002` 是 200："结果照常展示，
        提示可能无法回看"）：一次 Mongo 抖动不该让一次**已经算出来**的仿真结论
        消失（与 05 的 AD-01「落库失败不回滚决策」同一原则）。
        """
        repo = self.run_repo()
        ts = now_ms()
        # 编号：拿不到就**降级**而不是失败。
        # `new_id` 走 `seq_counters` 的原子 `$inc`，它会真实碰库；一次取号失败
        # 若冒泡出去，一次**已经算出来**的仿真就会以 500 结束——而 Spec §5 对
        # "记录落库失败"的处置是 200 + 提示（`SIM-5002`），取号失败属于同一类。
        # 兜底编号用时间戳而不是随机值：同一毫秒内的两条会撞键，而撞键会被下面的
        # `insert` 捕获成 `record_saved=false`——**这是刻意的**：宁可如实告诉用户
        # "这次可能无法回看"，也不要悄悄写进一条编号来路不明的记录。
        if not outcome.run_id:
            try:
                outcome.run_id = await repo.new_id(ts)
            except Exception as e:  # noqa: BLE001 - 见上：取号失败只降级
                outcome.run_id = f"SIMR{ts}"
                log.warning("[SIM-5002] 仿真编号取号失败，已用兜底编号 run_id=%s：%s",
                            outcome.run_id, e)

        doc = {
            "_id": outcome.run_id,
            "case_id": outcome.case_id,
            "input_event": dict(event or {}),
            "trace": {
                "steps": outcome.steps,
                "features": outcome.features,
                "missing_features": outcome.missing_features,
                "list_hit": outcome.list_hit,
                "timing": outcome.timing,
                "affects_window": outcome.affects_window,
                "window_premise": outcome.window_premise,
            },
            "final_decision": outcome.decision,
            "final_score": outcome.final_score,
            "rule_score": outcome.rule_score,
            "hit_rule_count": outcome.hit_rule_count,
            "risk_level": outcome.risk_level,
            "hits": outcome.hits,
            "rule_versions": outcome.rule_versions,
            "engine_version": outcome.engine_version,
            "elapsed_ms": outcome.elapsed_ms,
            "expected_decision": outcome.expected_decision,
            "matched_expected": outcome.matched_expected,
            "degraded": outcome.degraded,
            "degrade_code": outcome.degrade_code,
            "status": status,
            "run_by": operator,
            "run_at": ts,
        }
        saved = True
        try:
            await repo.insert(doc)
        except Exception as e:  # noqa: BLE001 - SIM-5002：只置标记 + 告警
            saved = False
            log.warning("[SIM-5002] sim_runs 落库失败（结果照常返回）run_id=%s：%s",
                        outcome.run_id, e)
        if audit_run:
            try:
                await self._audit_fn(
                    actor=operator, actor_role=actor_role, action="sim.run",
                    target_type="sim_run", target_id=outcome.run_id,
                    before=None,
                    after={
                        "run_id": outcome.run_id,
                        "case_id": outcome.case_id,
                        "decision": outcome.decision,
                        "final_score": outcome.final_score,
                        "degraded": outcome.degraded,
                        "status": status,
                        "affects_window": outcome.affects_window,
                        # 审计里如实记下"本次有没有动真实窗口"：事后追查
                        # "为什么线上特征被抬高了"时，这一行是唯一线索
                        "record_saved": saved,
                    },
                    ip=ip, ua=ua,
                    # `strict=False`：与 03 的模拟器启停同类（低频管理动作 + 不阻断业务）。
                    # 若用 strict=True，一次审计故障会让**已经算出来的仿真结论**失败，
                    # 而仿真不改变任何风控行为，没有"必须留痕否则不做"的必要
                    # （BR-12-16 的 strict=True 范围是"改变风控行为"的操作）。
                    strict=False,
                )
            except Exception as e:  # noqa: BLE001 - 审计失败不改结论（同上）
                log.warning("[AUD-5002] sim.run 审计写入失败 run_id=%s：%s",
                            outcome.run_id, e)
        return saved

    # ============================================================
    # §3.5 历史执行详情
    # ============================================================
    async def get_run(self, run_id: str) -> dict[str, Any]:
        """按 `run_id` 还原一次执行的 `data`（Spec §3.5 / BR-10-13）。

        **刷新页面后仍可完整回看**靠的是 `trace` 整体落库：这里不做任何重算，
        把 `sim_runs.trace` 原样展开。重算会让"当时看到的那一屏"与"事后回看的"
        出现差别，而那正是 BR-10-13 要防的事。
        """
        doc = await self.run_repo().find_by_id(run_id)
        if doc is None:
            raise SimRunNotFoundError(run_id)
        trace = doc.get("trace") if isinstance(doc.get("trace"), dict) else {}
        outcome = SimOutcome(
            run_id=str(doc.get("_id") or ""),
            steps=list(trace.get("steps") or []),
            features=dict(trace.get("features") or {}),
            missing_features=list(trace.get("missing_features") or []),
            list_hit=dict(trace.get("list_hit") or {}),
            hits=list(doc.get("hits") or []),
            rule_score=int(doc.get("rule_score") or 0),
            final_score=int(doc.get("final_score") or 0),
            hit_rule_count=int(doc.get("hit_rule_count") or 0),
            risk_level=str(doc.get("risk_level") or "low"),
            decision=str(doc.get("final_decision") or ""),
            expected_decision=doc.get("expected_decision"),
            matched_expected=doc.get("matched_expected"),
            elapsed_ms=int(doc.get("elapsed_ms") or 0),
            rule_versions=dict(doc.get("rule_versions") or {}),
            engine_version=str(doc.get("engine_version") or ""),
            event_id=str((doc.get("input_event") or {}).get("_id") or ""),
            case_id=doc.get("case_id"),
            affects_window=bool(trace.get("affects_window")),
            degraded=bool(doc.get("degraded")),
            degrade_code=doc.get("degrade_code"),
            record_saved=True,
            timing=dict(trace.get("timing") or {}),
            window_premise=str(trace.get("window_premise") or WINDOW_PREMISE),
        )
        data = outcome.to_data()
        data["status"] = doc.get("status")
        data["run_by"] = doc.get("run_by")
        data["run_at"] = doc.get("run_at")
        data["input_event"] = dict(doc.get("input_event") or {})
        return data

    # ============================================================
    # §3.4 批量回放
    # ============================================================
    async def replay(
        self,
        *,
        case_id: Optional[str] = None,
        event: Optional[dict[str, Any]] = None,
        repeat: int = SIM_BATCH_DEFAULT_REPEAT,
        seed: int = 42,
        operator: str = "",
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> BatchOutcome:
        """批量回放一个用例（Spec §3.4 / BR-10-18/19）。

        ## `affect_window` **强制 false**（决策 D11 的原话）

        D11 的裁定是「提供 `affect_window` 开关，但**批量回放强制为 false**」。
        理由很直接：单条仿真误开开关最多污染一条窗口记录，而批量回放一次就是
        200 条——那足以把 `device_order_cnt_1h` 之类的聚集度特征整体抬高一个量级，
        而窗口是**只活在进程内存里**的（悬空点 G-02），事后连回滚都做不到。
        因此本方法**没有** `affect_window` 参数：不是默认关闭，而是**不可开启**。

        ## `seed` 的可复现语义（BR-10-18）

        「同一 `seed` + 同一用例必须产生完全一致的结果」。本模块的实现是
        `ripple_event(event, seed + index)`：**只用 `seed` 播种的局部随机源**
        （不碰全局 `random`），因此两次调用产生逐字节相同的事件序列与统计数字。
        刻意**不使用**"随机标注"之类的机制：仿真没有 ground truth，
        任何"随机造标签"都会让 `false_positive` 变成一个自己编出来的数字。

        ## 逐条串行（不是并发）

        与 03 的 `POST /events/batch` 同一口径（BR-03-16「批内逐条串行」）：
        并发跑会让"同一 seed 结果一致"变成一句空话（完成顺序影响不了数值，
        但 `run_ids` 的顺序与 `decision_counts` 的累加会乱），
        也让批量回放的耗时变得不可解释。20~200 条串行的实测代价在秒级以内，
        而这一页是人工触发的。
        """
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
            raise SimBatchLimitError(repeat, SIM_BATCH_MAX_REPEAT)
        if repeat > SIM_BATCH_MAX_REPEAT:
            raise SimBatchLimitError(repeat, SIM_BATCH_MAX_REPEAT)

        resolved_case: Optional[dict] = None
        if case_id:
            resolved_case = await self.case_repo().find_by_id(case_id)
            if resolved_case is None:
                raise SimRunNotFoundError(case_id)
        template = event if isinstance(event, dict) and event else (
            (resolved_case or {}).get("event_template") or {}
        )
        if not template:
            raise sim_error(
                "SIM-4001",
                "批量回放缺少事件体：请提供 event 或指定一个带完整模板的 case_id",
                {"sim_code": "SIM-4001", "source_code": None,
                 "missing": ["event"], "field": "event"},
            )

        expected = _expected_of(resolved_case)
        began = time.perf_counter()
        result = BatchOutcome(
            case_id=case_id, expected_decision=expected, seed=int(seed), repeat=int(repeat),
            affects_window=False,
        )
        counts: dict[str, int] = {}

        for index in range(int(repeat)):
            payload = ripple_event(template, int(seed) + index)
            try:
                outcome = await self.simulate(
                    payload, case=resolved_case, case_id=case_id,
                    affect_window=False,      # **不可开启**（D11）
                    operator=operator, actor_role=actor_role, ip=ip, ua=ua,
                )
            except (SimEngineUnavailableError, SimEventInvalidError, SimTimeoutError) as e:
                # 单条失败**不终止整批**（与 03 的批内隔离同一原则）：
                # 统计里如实计入 `failed`，否则 `total` 与 `matched+missmatched`
                # 会对不上账，而一个对不上账的响应比一次明确失败更难排查。
                result.total += 1
                result.failed += 1
                result.run_ids.append(str((getattr(e, "data", None) or {}).get("run_id") or ""))
                log.warning("[SIM-5001] 批量回放第 %d/%d 条未产出结论：%s",
                            index + 1, repeat, e.code)
                continue

            result.total += 1
            result.run_ids.append(outcome.run_id)
            counts[outcome.decision] = counts.get(outcome.decision, 0) + 1
            if expected is None:
                continue
            if outcome.decision == expected:
                result.matched += 1
                continue
            result.mismatched += 1
            # 误伤（Spec §2.4）：预期 pass 但被判 review/reject。
            # 口径的**局限如实声明**：这里的"预期"来自用例的 expected_decision
            # 字段，不是真实世界的人工标注——换句话说它衡量的是
            # "规则集是否与用例作者的预期一致"，不是"这条规则误伤了多少真实好人"。
            if expected == "pass" and outcome.decision in ("review", "reject"):
                result.false_positive += 1
            if len(result.mismatch_samples) < 3:
                result.mismatch_samples.append({
                    "run_id": outcome.run_id,
                    "decision": outcome.decision,
                    "expected_decision": expected,
                })

        result.elapsed_ms = max(0, int((time.perf_counter() - began) * 1000))
        result.decision_counts = counts

        if operator:
            await self._audit_batch(result, operator, actor_role, ip, ua)
        return result

    async def _audit_batch(
        self, result: BatchOutcome, operator: str, actor_role: str,
        ip: Optional[str], ua: Optional[str],
    ) -> None:
        """批量回放的**恰好一条**聚合审计（不逐条写）。

        为什么聚合而不是逐条：一次 200 条的批量若逐条写审计，会瞬间把审计队列
        打出 200 条同质记录，把真正的变更记录淹掉；而每次单条仿真已经有自己的
        `sim.run`（`_write_run`）。聚合这条回答的是"谁在什么时候跑了多大一批"。
        """
        try:
            await self._audit_fn(
                actor=operator, actor_role=actor_role, action="sim.batch",
                target_type="sim_case", target_id=result.case_id or "ad-hoc",
                before=None,
                after={
                    "case_id": result.case_id,
                    "repeat": result.repeat,
                    "seed": result.seed,
                    "total": result.total,
                    "matched": result.matched,
                    "mismatched": result.mismatched,
                    "false_positive": result.false_positive,
                    "failed": result.failed,
                    # D11：批量回放**强制**不动真实窗口，审计里留证
                    "affects_window": False,
                },
                ip=ip, ua=ua, strict=False,
            )
        except Exception as e:  # noqa: BLE001 - 审计失败不改统计结果
            log.warning("[AUD-5002] sim.batch 审计写入失败：%s", e)


# ============================================================
# 模块级工具
# ============================================================
def _decision_module() -> Any:
    """取 05 的判定模块。

    ## 为什么是**函数内**导入（不是模块级）

    `app/protocols.py` 在模块末尾执行 `_COMPONENTS = Components()`，而
    `Components.decision_provider` 的默认工厂会 import `decision_provider`
    → `decision`。若本模块在**模块级**再 `import decision`，就成了
    `protocols(执行中) → ... → decision → sim_service → decision` 这类环，
    而环的后果是"默认装配静默换成 `UnavailableDecisionProvider`"
    （`decision.py` 的模块 docstring 记录了这次真实踩过的坑）。

    本文件本来不在 `protocols` 的链上，但**本文件 import 了 `rule_engine` /
    `list_filter`，而它们在链上**——保持"函数内导入 05"这条纪律，
    就不必去论证"我这一条边恰好不参与环"。
    """
    from app.engine import decision as decision_module

    return decision_module


def ripple_event(event: Any, seed: int) -> dict[str, Any]:
    """按 `seed` 对事件做**有界的确定性扰动**（BR-10-18 的可复现基础）。

    ## 为什么批量回放必须扰动

    Spec §2.2 的按钮是"批量回放此用例 × 20"，§2.4 要统计命中/未命中/误伤。
    若 20 次跑的是**逐字节相同**的输入，那 20 次必然得到同一个决策，
    统计数字恒为 `0/20/0` 或 `20/0/0`——"误伤统计"这项功能就永远看不到非零值，
    而它恰恰是规则调优最关心的那一格。

    ## 扰动的边界（刻意保守）

    只动**金额类数值**（顶层 `amount` 与 `scene_extra` 的金额字段），
    别的字段一个都不碰：

    - 金额是**唯一一类**既能影响规则求值（`amount` 与部分 `scene_extra` 数值
      参与特征/条件）又不会改变"这是哪个业务场景"的字段。改 `scene_extra` 的
      必填键会直接触发 `EVT-4004`；改 `event_type` 更是换了一个场景；
    - **不动 `ts`**：`ts` 的扰动会改变"这一笔在窗口里的位置"，而批量回放
      本来就不写窗口（`affect_window` 强制 false），动了它只是白白引入
      "与 `validate_event` 的 `BR-03-07` 超前判定擦边"的风险。
      时间维度的变化由"这一轮是第几条"自然表达（`run_at` 逐条递增）。

    ## 可复现（BR-10-18）

    用 `random.Random(seed)`——**局部实例**，绝不碰全局 `random` 状态：
    全局 `random` 会被同进程的其它模块（模拟器、测试夹具）共享，
    那样"同一 seed 结果一致"就依赖于"期间没有别人抽过随机数"，
    而这是个不可能被验证的假设。

    入参不是 dict 时原样返回（那属于 03 的 `EVT-4001`，本函数不越界）。
    """
    import copy
    import random

    # 函数内导入：`event_schema` 会拉起 `enums`/`pydantic`，而本函数是纯函数、
    # 被单测大量直接调用，没必要在模块导入期就付这笔账
    from app.schemas.event_schema import AMOUNT_FIELD_BY_TYPE

    if not isinstance(event, dict):
        return event
    rng = random.Random(int(seed))
    out = copy.deepcopy(event)
    # 扰动幅度 ±20%（`SIM_BATCH_AMOUNT_RIPPLE`）：足以让金额维度的条件产生分叉，
    # 又不至于把一笔 99 元的订单变成 8 万的大额可疑交易
    factor = 1.0 + rng.uniform(-SIM_BATCH_AMOUNT_RIPPLE, SIM_BATCH_AMOUNT_RIPPLE)

    def _ripple_amount(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return value
        # 至少 1 分：金额 0 会被 03 的 `EVT-4005`（需正整数）拒绝，
        # 那会把一次正常的批量回放变成一堆校验失败
        return max(1, int(round(value * factor)))

    if "amount" in out:
        out["amount"] = _ripple_amount(out["amount"])
    event_type = str(out.get("event_type") or "")
    amount_field = AMOUNT_FIELD_BY_TYPE.get(event_type)
    scene = out.get("scene_extra")
    if amount_field and isinstance(scene, dict) and amount_field in scene:
        scene[amount_field] = _ripple_amount(scene[amount_field])
    return out


def _expected_of(case: Optional[dict]) -> Optional[str]:
    """取用例的预期决策（无用例或字段缺失 → `None`，**不猜一个默认值**）。

    为什么不默认 `pass`：BR-10-21/§2.4 的"误伤"定义是"预期 pass 但被拦"，
    若把"没有预期"当成"预期 pass"，那么每一条**没有用例**的仿真都会被算成
    一次误伤——`false_positive` 会凭空涨起来，而它正是规则调优要看的那一格。
    """
    if not isinstance(case, dict):
        return None
    value = case.get("expected_decision")
    text = str(value).strip() if value is not None else ""
    return text if text in EXPECTED_DECISIONS else None


def _case_out(doc: dict) -> dict[str, Any]:
    """把 E17 文档摊成 Spec §3.1 的列表项（**不含内部状态字段**）。"""
    return {
        "case_id": str(doc.get("_id") or ""),
        "name": str(doc.get("name") or ""),
        "category": str(doc.get("category") or "other"),
        "expected_decision": str(doc.get("expected_decision") or ""),
        "description": doc.get("description"),
        "event_template": dict(doc.get("event_template") or {}),
        "created_by": str(doc.get("created_by") or ""),
        "created_at": int(doc.get("created_at") or 0),
    }


def validate_case_fields(category: Any, expected_decision: Any) -> None:
    """`category` / `expected_decision` 的枚举边界（Spec §3.2）。

    放在服务层而不是 Pydantic 的 `Literal[...]`：错误码要是本模块的 `COM-4001`
    （字段级参数错误）而**不是**别的语义码；用 `Literal` 与既有 06 的
    `RuleCreate` 处理方式会不一致（那边也是服务层判 `status`）。
    """
    if category not in CASE_CATEGORIES:
        raise AppError(
            "COM-4001",
            f"category 仅支持 {'/'.join(CASE_CATEGORIES)}，收到：{category}",
            422,
            {"field": "category", "allowed": list(CASE_CATEGORIES)},
        )
    if expected_decision not in EXPECTED_DECISIONS:
        raise AppError(
            "COM-4001",
            f"expected_decision 仅支持 {'/'.join(EXPECTED_DECISIONS)}，收到：{expected_decision}",
            422,
            {"field": "expected_decision", "allowed": list(EXPECTED_DECISIONS)},
        )


# ============================================================
# 事件校验的适配层（**唯一的**校验入口，03 的函数在这里被调用）
# ============================================================
@dataclass
class _ValidateFailure:
    """步骤 1 失败的事实（不含任何结论）。

    ⚠️ 字段名是 `bad_field` 而**不是** `field`：`dataclasses.field` 是 dataclass 的
    声明工具，而本模块在下面定义了一个**模块级函数** `_event_field_of`；
    若把属性也叫 `field`，`field(default_factory=...)` 就会解析成
    "模块里那个名字"（`NameError` 或 `TypeError: 'NoneType' object is not callable`
    ——本文件第一次落地时真的踩了这条）。属性名与工具名重名是一类静默的坑，
    这里显式避开并留下记录。
    """

    code: str                 # 03 的原码（EVT-xxxx）或 SIM-4002
    message: str
    missing: list[str] = field(default_factory=list)
    bad_field: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)


def _validate_event_body(raw: Any) -> tuple[dict[str, Any], Optional[_ValidateFailure]]:
    """把仿真入参校验并归一成内部事件 dict（**与 `POST /events` 同一套**）。

    ## 为什么这里不能"顺手放宽"

    任务书 §3 的原话：「仿真表单提交的事件**必须走与 `POST /events` 同一套校验**
    （含 `scene_extra` 防串味那套规则），**不得**为仿真放宽——否则仿真通过、
    真实入口 422，一致性就名存实亡」。

    因此本函数**只做三件事**，且每一件都是为了"能交给 03"：

    1. `scene_extra` 若是 JSON 字符串则解析一次（`SIM-4002`）——
       仿真页的表单字段是多行文本，而 `POST /events` 直接收对象；
       这是**格式适配**，不是放宽（解析后交出的对象与真实入口收到的完全同形）；
    2. `source` 缺省时补 `manual_sim`（仿真页手填的来路就是它，
       与 `scripts/seed.py` 的演示事件同一取值）；
    3. `event_id` 缺省时取一个（与 `/engine/evaluate` 的 `_normalize_event` 同一
       处置，注释同样要贴：**编号必须每轮不同**，否则 `affect_window=true` 时
       `FeatureWindow` 的去重会让第二次仿真静默不计入窗口）。
       **没有这一步，`affect_window=true` 的语义就是不成立的。**

    其余字段一个都不碰，全部交给 03。
    """
    from app.services.event_service import validate_event

    if not isinstance(raw, dict) or not raw:
        return {}, _ValidateFailure(
            code="SIM-4001",
            message="事件参数不合法：`event` 必须是 JSON 对象",
            data={"reason": "event_not_object"},
        )
    payload, reason = parse_scene_extra(raw)
    if reason:
        return {}, _ValidateFailure(code="SIM-4002", message=reason,
                                    bad_field="scene_extra",
                                    data={"reason": "scene_extra_not_json"})
    candidate = dict(payload)
    candidate.setdefault("source", SOURCE_MANUAL_SIM)
    try:
        event = validate_event(candidate, now_ms())
    except AppError as e:
        return {}, _ValidateFailure(
            code=e.code,
            message=e.message,
            missing=_missing_of(e),
            bad_field=_event_field_of(e),
            data=dict(e.data) if isinstance(e.data, dict) else {},
        )
    except Exception as e:  # noqa: BLE001 - 校验器的意外异常仍属"报文不合法"
        return {}, _ValidateFailure(
            code="SIM-4001",
            message=f"事件参数不合法：{type(e).__name__}: {e}",
            data={"reason": "validator_error"},
        )
    # 编号：请求里给了就用（便于与真实事件对照），没给就现取一个。
    # ⚠️ **必须每轮不同**（见 docstring 第 3 点）：`FeatureWindow.ingest` 按
    # `event_id` 去重，复用编号会让 `affect_window=true` 的第二次仿真静默不计入窗口
    # ——那正是"仿真是否影响真实数据"的实测最容易得出错误结论的地方。
    given = str(candidate.get("event_id") or candidate.get("_id") or "").strip()
    event["_id"] = given
    return event, None


def _missing_of(exc: AppError) -> list[str]:
    """取 03 的缺失字段清单（`EVT-4004` 带 `missing`，其余为空）。"""
    data = exc.data if isinstance(exc.data, dict) else {}
    missing = data.get("missing")
    if isinstance(missing, list):
        return [str(m) for m in missing]
    return []


def _event_invalid_error(failure: _ValidateFailure) -> AppError:
    """把步骤 1 的失败翻成 `SIM-4001`（**保留 03 的原码与字段清单**）。

    `SIM-4002`（`scene_extra` 非合法 JSON）**不套 SIM-4001**：Spec §5 给它单列了
    `422` 与「表单该字段标红」，套上 400 会让前端把高亮打到一个不存在的字段上。
    """
    if failure.code == "SIM-4002":
        return sim_error("SIM-4002", failure.message,
                         {"field": failure.bad_field, "reason": failure.data.get("reason")})
    prefix = "事件参数不合法"
    text = failure.message
    if failure.missing:
        text = f"{prefix}：缺少 {'、'.join(failure.missing)}"
    elif failure.bad_field:
        text = f"{prefix}：{failure.message}"
    elif failure.code not in ("SIM-4001",):
        text = f"{prefix}：{failure.message}"
    return SimEventInvalidError(
        text, source_code=failure.code, missing=failure.missing, field=failure.bad_field,
    )


def _record_validate_failure(
    tracer: SimTracer, failure: _ValidateFailure, *, started: float
) -> None:
    """步骤 1 失败的记录（BR-10-15：红的步骤 1 + 后续"未执行"）。

    `elapsed_ms` 用**外部传入的 `started`** 而不是 `tracer.elapsed_since`：
    `started` 是整次 `simulate()` 的起点，因此这一步的耗时包含"解析 + 校验"，
    与用户感知的"点下去到步骤 1 变红"一致。用 `tracer.elapsed_since` 会少算
    掉 `simulate` 开头那几行（取用例、算预期决策）的时间。
    """
    if tracer.step(STEP_VALIDATE) is None:
        tracer.start(STEP_VALIDATE)
    tracer.fail(
        STEP_VALIDATE,
        f"失败（{failure.code}）：{failure.message}",
        payload={
            "source_code": failure.code,
            "missing": failure.missing,
            "field": failure.bad_field,
            "detail": failure.data,
        },
        elapsed_ms=_ms(started),
    )
    tracer.mark_not_executed(STEP_VALIDATE)


def _ms(started: float) -> int:
    """自 `started` 起的毫秒数（单调时钟，见 `engine/tracer` 的说明）。"""
    return max(0, int((time.perf_counter() - started) * 1000))


def _ms_from(tracer: SimTracer) -> int:
    """自 tracer 创建起的毫秒数（**不落定**总耗时，由 `finish_all` 收尾）。"""
    import time as _time

    return max(0, int((_time.perf_counter() - tracer.started_at) * 1000))


__all__ = [
    "STEP_FEATURES",
    "STEP_FINAL",
    "STEP_LIST",
    "STEP_RULES",
    "STEP_VALIDATE",
    "WINDOW_PREMISE",
    "WINDOW_PREMISE_AFFECTED",
    "BatchOutcome",
    "SimOutcome",
    "SimService",
    "ripple_event",
    "rule_block",
    "validate_case_fields",
]





