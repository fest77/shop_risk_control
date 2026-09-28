# -*- coding: utf-8 -*-
"""决策编排：名单 → 特征 → 规则 → 仲裁 → 组装 `Decision`（模块 05 §6）。

## 链路（D4：名单与特征**并行**）

    名单过滤 ─┐
              ├─→ （命中黑/白 → 直通，不求值任何规则）
    特征获取 ─┘
              └─→ 取规则（scene + common）→ 逐条求值累加 → 仲裁 → 组装 → 异步落库

D4 把「名单过滤」与「特征计算」定为并行：两者互相独立（名单只看事件的 5 个
维度，特征只看窗口与画像），串行会让 05 的耗时直接相加。名单在 05 手上
（BR-03-15 的 04→05 串行只约束模块之间，不约束 05 内部），因此并行在这里落地。

## 降级为什么**必须落库建案**（决策 D5，本模块最容易被漏掉的一条）

`RUL-5001`（名单依赖不可用）/ `RUL-5002`（规则集加载失败）时，本模块**仍然
写入一条 `decisions`**：`decision=review`、`degraded=true`，模块 08 据此
正常建案。

理由：fail-closed 的全部意义就是"把不确定的请求交给人工"。若降级只在响应里
写一个 `review` 而不落库，那么 08 看不到案件、07 的列表里没有这一条、11 的
统计里也没有——**这些请求将无人处理**，等于变相丢弃。而这恰恰是 fail-closed
最不该出现的结果：我们明明识别出了"这里不确定"，却让它静默消失。

⚠️ **与 03 的边界（不要越界替 03 补）**：03 在 `stage="feature"` 的短路
（决策 D44：快照不完整）**不写** decisions 行——那时 05 根本没被调用，
"没写"是 03 自己的已知缺口（N-03-3）。本模块只在**自己被调用且判定降级**时
才写，绝不替 03 补那一条。两者的区别是"05 判了 review"与"05 没跑"。

## `snapshot_id` 为什么"知道才写"

03 的 `DecisionProvider.evaluate(event, features)` 契约里**没有**快照编号
（03 手里才有）。因此本模块在没有 `snapshot_id` 时**不写这个键**，交给 03 的
`normalize_decision` 用真实快照编号补上（`setdefault` 语义）。
写 `None` 会让 `setdefault` 失效，响应里的 `snapshot_id` 就永久是 null——
那比"少一个键"糟得多。`/engine/evaluate` 自己算快照，因此它总是带这个键。

## `engine_version` 在降级时为什么仍然是 `rule-engine-v1`

03 自己的降级块把 `engine_version` 写成 `"degraded"`（它没有别的字段可用）。
本模块**有** `degraded` 这个字段（E03 为此专门加了它，见 D5），因此不需要
污染版本号：`degraded=true` 就是"这条不是真实结论"的标记。混用两种标记方式
会让"这条决策是哪个引擎出的"与"它是不是降级"两件事纠缠在一起。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app import db as db_module
from app.constants import DECISION_TIMEOUT_MS
from app.engine import list_filter, rule_engine
from app.engine.list_filter import ListHit
from app.engine.rule_engine import RuleEvaluation
from app.errors import RUL_NOTICE
from app.logging import get_logger
from app.repos.decision_repo import DecisionRepo
from app.repos.rule_repo import RuleRepo
from app.utils.timeutil import now_ms

# ⚠️ **不要**在模块级 `from app.protocols import get_components`：
# `app/protocols.py` 在模块末尾执行 `_COMPONENTS = Components()`，而
# `Components.decision_provider` 的默认工厂会 import 本模块（`decision_provider.py`
# → `decision.py`）。若本模块在模块级再回头 import `protocols`，那条链就是
# `protocols(执行中) → decision_provider → decision → protocols(未初始化)`，
# 于是 `get_components` 还不存在 → 工厂里的 `except Exception` 兜底**静默**装成
# `UnavailableDecisionProvider`，表现是"05 明明落地了，默认链路却还在 stage=rule"。
# （这个坑真实发生过，见 `default_decision_provider` 的日志兜底。）
# 改成函数内导入，循环即被切断。

log = get_logger("shop_risk_control.decision")

#: 决策引擎版本（E03.engine_version；模型引擎接入后应升到 v2，见 AD-09）
ENGINE_VERSION = "rule-engine-v1"

#: `RUL-5004` 的告警阈值（Spec §8：概要设计的 P95<50ms 取 4 倍作告警线）
SLOW_THRESHOLD_MS = DECISION_TIMEOUT_MS

#: 落库重试次数（AD-01 的异步旁路；失败不回滚决策，只告警并计数）。
#: 只重试 2 次：这是高频路径，每次重试占一个后台任务；Mongo 长时间不可用时
#: 真正兜底的是告警 + 决策照常返回，而不是无限堆积任务。
PERSIST_RETRY = 2
PERSIST_RETRY_BASE_SEC = 0.05

#: 进程内统计（供测试与排障；进程重启即清零，它不是业务数据）
STATS: dict[str, int] = {
    "decided": 0,
    "degraded": 0,
    "persisted": 0,
    "persist_failed": 0,
    "slow": 0,
    "rule_failures": 0,
    # 【05 → 08】落库后成功建案的条数（D5：review/reject 必须建案）。
    # 它与 `persisted` 的差值是"应当建案却还没建"的可观测信号，
    # 也是 `case_maintenance_task` 补建案扫描的旁证。
    "cases_created": 0,
    # 【05 → 11】落库之后成功记入指标桶的条数（§3.9 的 `record_decision`）。
    # 与 `persisted` 分开计数是刻意的：`persisted` 说的是"决策进库了"，
    # 这一项说的是"大盘看得到它"。前者涨、后者不涨 = 指标写入这一环出了问题。
    # ⚠️ 11 自己的 `stats["write_fail"]` **发现不了**这个缺口——那个计数只在
    # "写入被调用过"之后才可能增长，而 D66 的成因恰恰是"根本没有调用方"。
    "metrics_recorded": 0,
}

_persist_tasks: set[asyncio.Task] = set()


# ============================================================
# 结果对象
# ============================================================
@dataclass
class DecisionOutcome:
    """一次决策的完整产物（响应块 + 落库 + 告警）。

    `block` 是**冻结契约**的那一份（Spec §3.1，`rule_score` 起到 `engine_version`
    止，见 `app/protocols.py` 的 `DecisionProvider` 注释）；其余字段是
    `/engine/evaluate` 与落库路径需要的附加信息，**不进 `block`**。
    """

    block: dict[str, Any]
    event_id: str = ""
    degraded: bool = False
    degrade_code: Optional[str] = None
    degrade_reason: Optional[str] = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    trace: Optional[list[dict]] = None
    decision_id: Optional[str] = None
    list_hit: ListHit = list_filter.NO_HIT
    saved: bool = False

    @property
    def decision(self) -> str:
        return str(self.block.get("decision") or "review")


def _empty_block(
    snapshot_id: Optional[str],
    *,
    decision: str,
    risk_level: str,
    score: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    """组装一个"没有规则参与"的决策块（名单直通 / 降级共用）。

    `model_score` **恒为 None**（AD-09：模型引擎是空实现，`final_score` 因此
    恒等于 `rule_score`）。这里写成显式的字面量而不是"省略该键"：E03 的
    `model_score` 是一个**结论**——"本次没有模型分"，不是"这个字段没填"。
    """
    block: dict[str, Any] = {
        "list_hit": {"hit": False, "list_type": None, "entity_type": None, "entity_value": None},
        "rule_score": score,
        "model_score": None,
        "final_score": score,
        "risk_level": risk_level,
        "decision": decision,
        "hit_rule_count": 0,
        "hits": [],
        "rule_versions": {},
        "engine_version": ENGINE_VERSION,
        "elapsed_ms": elapsed_ms,
    }
    if snapshot_id is not None:
        # 只在知道时才写（见模块 docstring：写 None 会让 03 的 setdefault 失效）
        block["snapshot_id"] = snapshot_id
    return block


def _degraded_outcome(
    event: dict,
    *,
    code: str,
    reason: str,
    snapshot_id: Optional[str],
    elapsed_ms: int,
) -> DecisionOutcome:
    """组装降级结果（`review` + `degraded=True`，**绝不 pass**）。

    `risk_level` 取 `high` 而不是 `low`：0 分在本模块的分档里属低风险，但这里
    的 0 分含义是"**没算出来**"。用 `high` 才能让 08 建案与 07 的列表按高风险
    优先处理——真正决定放不放行的是 `decision=review`，等级只影响排序与标签。
    """
    block = _empty_block(snapshot_id, decision="review", risk_level="high",
                         score=0, elapsed_ms=elapsed_ms)
    return DecisionOutcome(
        block=block,
        event_id=_event_id(event),
        degraded=True,
        degrade_code=code,
        degrade_reason=reason,
        warnings=[{"code": code, "message": reason}],
        list_hit=list_filter.NO_HIT,
    )


def _event_id(event: dict) -> str:
    """取事件编号（03 用 `_id`，直接构造的事件可能用 `event_id`）。"""
    return str(event.get("_id") or event.get("event_id") or "")


# ============================================================
# 特征获取
# ============================================================
async def _acquire_features(
    event: dict, provider: Any
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], list[dict[str, Any]]]:
    """向 04 取特征快照，返回 `(features, snapshot_id, 降级原因, warnings)`。

    **只在 `/engine/evaluate` 这条路上用到**：03 的 `evaluate(event, features)`
    已经把特征算好了再交给 05（BR-03-15：04 先出快照）。这里重复一次获取逻辑，
    是为了让 `POST /engine/evaluate` 能独立工作（Spec §3.1 的请求体只有 `event`）。

    04 的 `compute()` **契约上永不抛异常**（失败会转成 `degrade_suggested=True`
    的可用子集）。但契约是契约：这里仍然兜住异常——一个不守契约的 04 不该让
    `/engine/evaluate` 返回 500（那会让人以为"规则引擎坏了"，而真相是特征侧）。

    `degrade_suggested=True` 时**同样按 fail-closed 处理**：本次快照不完整，
    缺的正是判定要用的特征（当前是 `ip_is_proxy`）。若照常求值，缺失特征会让
    相关条件恒为假（BR-05-12）→ 总分偏低 → 很可能输出 `pass`，而 `pass` 在
    这里等于"因为不知道，所以放行"。因此返回降级原因，由调用方转 `review`。
    """
    try:
        snapshot = await provider.compute(event)
    except Exception as e:  # noqa: BLE001 - 04 的任何失败都必须 fail-closed
        return None, None, f"特征计算不可用（{type(e).__name__}: {e}）", [
            # 保持 FEA 前缀（ER-02）：这是**特征侧**的故障，不是规则侧的
            {"code": "FEA-5001", "message": f"特征计算不可用：{type(e).__name__}: {e}"},
        ]
    if not isinstance(snapshot, dict):
        return None, None, f"特征快照结构非法（{type(snapshot).__name__}）", [
            {"code": "FEA-5001", "message": f"特征快照结构非法：{type(snapshot).__name__}"},
        ]
    features = snapshot.get("features")
    if not isinstance(features, dict) or not features:
        return None, None, "特征快照为空，无法据此决策", [
            {"code": "FEA-5001", "message": "特征快照为空（无可用于判定的特征）"},
        ]
    reasons = snapshot.get("degrade_reasons") or []
    if snapshot.get("degrade_suggested"):
        detail = "；".join(str(r) for r in reasons) or "快照不完整（degrade_suggested=true）"
        return None, snapshot.get("snapshot_id"), f"特征快照不完整：{detail}", [
            {"code": "FEA-5001", "message": f"特征快照不完整：{detail}"},
        ]
    return features, snapshot.get("snapshot_id"), None, []


# ============================================================
# 编排
# ============================================================
async def decide(
    event: dict,
    features: Optional[dict[str, Any]] = None,
    *,
    snapshot_id: Optional[str] = None,
    dry_run: bool = False,
    trace: bool = False,
    list_repo: Any = None,
    rule_repo: Any = None,
    decision_repo: Any = None,
    feature_provider: Any = None,
    list_cache: Optional[list_filter.ListEntryCache] = None,
) -> DecisionOutcome:
    """对一条事件求值并给出决策（Spec §3.1 的全过程）。

    | 参数 | 说明 |
    |---|---|
    | `features` | 04 的快照特征。**为 `None` 时自行向 04 获取**（`/engine/evaluate` 用） |
    | `snapshot_id` | 已知的快照编号；未知时块里**不写该键**（见模块 docstring） |
    | `dry_run` | `True` 时**不落库**（Spec §3.1：仿真/调试用） |
    | `trace` | `True` 时返回逐规则求值过程（默认 `None`） |
    | `list_repo` / `rule_repo` / `decision_repo` / `feature_provider` / `list_cache` | 测试注入点（V-05-08 靠它注入抛异常的名单 repo） |

    **本函数对"依赖不可用"永不抛异常**：一律产出 `review` + `degraded=true`
    的决策块并落库（D5）。抛异常会让 03 走它自己的降级路径（`EVT-5003`），
    而那条路径**不写 decisions**——于是降级请求无人处理，正是 D5 要避免的事。
    """
    started = time.perf_counter()
    event_id = _event_id(event)
    warnings: list[dict[str, Any]] = []

    if snapshot_id is None:
        # 03 的调用契约 `evaluate(event, features)` 里没有快照编号，但它会把
        # 04 的快照挂在事件上（`event["feature_snapshot"]`）带过来。取不到时
        # **不写这个键**，由 03 的 `normalize_decision` 用真实编号补上——
        # 详见模块 docstring 关于 `snapshot_id` 的说明。
        attached = event.get("feature_snapshot")
        if isinstance(attached, dict):
            snapshot_id = attached.get("snapshot_id")

    # ---------- 1. 名单 与 特征：并行（D4） ----------
    list_task = asyncio.ensure_future(
        list_filter.match_lists(event, repo=list_repo, cache=list_cache)
    )
    want_features = features is None
    feature_task: Optional[asyncio.Future] = None
    if want_features:
        # 函数内导入：见文件顶部关于循环导入的说明
        from app.protocols import get_components

        provider = feature_provider if feature_provider is not None else get_components().feature_provider
        feature_task = asyncio.ensure_future(_acquire_features(event, provider))

    # 名单失败**不取消**特征任务：让它跑完（结果丢弃即可），因为取消一个正在
    # 写特征窗口的协程会留下半截窗口状态（04 的 `compute` 内部会 `ingest`）。
    if feature_task is not None:
        gathered = await asyncio.gather(list_task, feature_task, return_exceptions=True)
        list_result, degrade_features = gathered[0], gathered[1]
    else:
        list_result, degrade_features = await _await_list(list_task), None

    elapsed_ms = _elapsed(started)

    # ---------- 2. 名单依赖不可用 → RUL-5001（fail-closed，仍落库） ----------
    if isinstance(list_result, BaseException):
        reason = str(list_result) or type(list_result).__name__
        outcome = _degraded_outcome(
            event, code="RUL-5001", reason=f"名单服务不可用，已转人工审核（{reason}）",
            snapshot_id=snapshot_id, elapsed_ms=elapsed_ms,
        )
        await _finish(event, outcome, dry_run=dry_run)
        return outcome

    hit: ListHit = list_result

    # ---------- 3. 特征不可用 → 同样 fail-closed（不是"放行"） ----------
    if feature_task is not None:
        if isinstance(degrade_features, BaseException):
            reason = f"特征计算不可用（{type(degrade_features).__name__}: {degrade_features}）"
            outcome = _degraded_outcome(
                event, code="FEA-5001", reason=reason,
                snapshot_id=snapshot_id, elapsed_ms=elapsed_ms,
            )
            outcome.warnings = [{"code": "FEA-5001", "message": reason}]
            await _finish(event, outcome, dry_run=dry_run)
            return outcome
        acquired, got_snapshot_id, degrade_reason, feat_warnings = degrade_features
        snapshot_id = snapshot_id or got_snapshot_id
        if degrade_reason is not None:
            # 特征不完整：**绝不用缺失特征去算一个 pass**（BR-05-12 会让缺失的
            # 条件恒假 → 分数偏低 → 很可能 pass，那等于"因为不知道所以放行"）。
            outcome = _degraded_outcome(
                event, code="FEA-5001", reason=degrade_reason,
                snapshot_id=snapshot_id, elapsed_ms=elapsed_ms,
            )
            outcome.warnings = feat_warnings
            await _finish(event, outcome, dry_run=dry_run)
            return outcome
        features = acquired or {}

    assert features is not None  # 由上面的分支保证

    # ---------- 4. 名单直通：不求值任何规则（BR-05-02/03） ----------
    if hit.hit:
        block = _empty_block(
            snapshot_id,
            decision="pass" if hit.list_type == "white" else "reject",
            # 直通态的等级由**直通本身**决定，不是算出来的分值：白名单确认过是
            # 干净的（low），黑名单是已确认的风险（high）。这里刻意不去套
            # `band_for(0)`——那会给黑名单直通贴上"低风险"标签。
            risk_level="low" if hit.list_type == "white" else "high",
            score=0,
            elapsed_ms=elapsed_ms,
        )
        block["list_hit"] = hit.to_dict()
        outcome = DecisionOutcome(
            block=block, event_id=event_id, list_hit=hit,
            trace=[] if trace else None,
        )
        _apply_slow_warning(outcome, started)
        await _finish(event, outcome, dry_run=dry_run)
        return outcome

    # ---------- 5. 取规则（scene + common，BR-05-08/09） ----------
    scene_code = rule_engine.scene_for(str(event.get("event_type") or ""))
    repository = rule_repo if rule_repo is not None else RuleRepo(db_module.get_db())
    try:
        rules = await repository.list_enabled_rules(scene_code)
    except Exception as e:  # noqa: BLE001 - RUL-5002：读不到规则**不是**"没有规则"
        outcome = _degraded_outcome(
            event, code="RUL-5002",
            reason=f"规则服务不可用，已转人工审核（{type(e).__name__}: {e}）",
            snapshot_id=snapshot_id, elapsed_ms=_elapsed(started),
        )
        await _finish(event, outcome, dry_run=dry_run)
        return outcome

    # ---------- 6. 逐条求值 + 累加（全量求值，priority 不短路） ----------
    evaluation: RuleEvaluation = rule_engine.evaluate_rules(rules, features, trace=trace)
    if evaluation.failures:
        STATS["rule_failures"] += len(evaluation.failures)
        warnings.extend(f.to_warning() for f in evaluation.failures)
        for failure in evaluation.failures:
            # BR-05-13：写告警日志（页面提示由 `/engine/evaluate` 的 warnings 承担）
            log.warning("[RUL-5003] 规则求值异常 rule=%s name=%s err=%s event_id=%s",
                        failure.rule_code, failure.rule_name, failure.message, event_id)

    score, level, decision = rule_engine.finalize(evaluation)
    elapsed_ms = _elapsed(started)

    hits = _sorted_hits(evaluation)
    block = {
        "list_hit": hit.to_dict(),
        "rule_score": score,
        "model_score": None,
        "final_score": score,
        "risk_level": level,
        "decision": decision,
        "hit_rule_count": len(evaluation.hits),
        "hits": [h.to_dict() for h in hits],
        # BR-05-20：本次**生效规则集**的版本快照（不只命中的那些）。
        # 只记命中的规则无法重放：重放时要先知道"当时有哪些规则参与"，
        # 否则一条后来被停用的规则会让重放结果与历史分数对不上。
        "rule_versions": dict(evaluation.rule_versions),
        "engine_version": ENGINE_VERSION,
        "elapsed_ms": elapsed_ms,
    }
    if snapshot_id is not None:
        block["snapshot_id"] = snapshot_id

    outcome = DecisionOutcome(
        block=block, event_id=event_id, warnings=warnings,
        trace=evaluation.trace, list_hit=hit,
    )
    _apply_slow_warning(outcome, started)
    await _finish(event, outcome, dry_run=dry_run)
    return outcome


async def _await_list(task: asyncio.Future) -> Any:
    """无特征任务时等待名单任务（保持与 `gather` 分支一样的返回形状）。

    `CancelledError` **必须原样抛出**：03 把「04 + 05」整体包在
    `asyncio.wait_for` 里，超时靠取消下游协程止损（BR-03-23）。若在这里把
    取消吞成"返回值"，05 会在预算耗尽后继续跑并可能写库，出现"接口已返回
    review、稍后库里多出一条 pass 决策"的分裂状态。
    """
    try:
        return await task
    except asyncio.CancelledError:
        raise
    except BaseException as e:  # noqa: BLE001 - 交给统一分支处理
        return e


def _sorted_hits(evaluation: RuleEvaluation) -> list[Any]:
    """命中明细按 `score` **降序**（Spec §2.2）。

    并列时保持规则顺序（`sort` 是稳定排序，输入已按 `priority, _id` 排好），
    因此同一份数据每次输出的明细顺序完全一致——可复现是这里唯一的硬要求，
    因为 `decision_hits` 是事后复核的证据。

    排序放在**后端**而不是只交给前端：`hits` 会同时出现在 `/engine/evaluate`
    的响应、事件的响应与 10 的仿真链路里，三处各自排序迟早有一处忘了排；
    而后端排一次，三处天然一致（前端的排序成为幂等的兜底）。
    """
    return sorted(evaluation.hits, key=lambda h: h.score, reverse=True)


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _apply_slow_warning(outcome: DecisionOutcome, started: float) -> None:
    """`RUL-5004`：决策耗时超过阈值仍返回结果，但记 `slow` 告警（Spec §5）。

    用**重新测量**的耗时而不是块里那个 `elapsed_ms`：块里的值在组装时就固定了，
    而"慢"这件事要包括组装本身。两者可能差 1ms，但告警线是 200ms，差异无意义；
    真正重要的是**不要**因为"块里写着 180"就漏掉一次实际 205ms 的慢请求。
    """
    total = _elapsed(started)
    if total <= SLOW_THRESHOLD_MS:
        return
    STATS["slow"] += 1
    message = f"{RUL_NOTICE['RUL-5004']}（实际 {total}ms）"
    outcome.warnings.append({"code": "RUL-5004", "message": message})
    outcome.block["elapsed_ms"] = max(int(outcome.block.get("elapsed_ms") or 0), total)
    log.warning("[RUL-5004] 决策耗时 %sms event_id=%s", total, outcome.event_id)


# ============================================================
# 落库（AD-01：异步旁路 / D5：降级也必须落库）
# ============================================================
def build_decision_doc(
    event: dict, outcome: DecisionOutcome, decision_id: str, *, decided_at: int
) -> dict[str, Any]:
    """组装 E03 `decisions` 文档。

    **字段是"逐列点名"写的，不是 `dict(outcome.block)` 展开**：块里将来若
    多出内部键（`degraded`/`warnings` 之类）会被一起写进库，而 E03 的列是
    定稿的——多写的列没人读，却会让"库里有哪些列"逐渐失去意义。
    """
    block = outcome.block
    return {
        "_id": decision_id,
        "event_id": outcome.event_id or _event_id(event),
        "snapshot_id": block.get("snapshot_id"),
        "list_hit": block.get("list_hit"),
        "rule_score": int(block.get("rule_score") or 0),
        "model_score": None,
        "final_score": int(block.get("final_score") or 0),
        "risk_level": block.get("risk_level"),
        "decision": block.get("decision"),
        "hit_rule_count": int(block.get("hit_rule_count") or 0),
        "rule_versions": dict(block.get("rule_versions") or {}),
        "engine_version": block.get("engine_version") or ENGINE_VERSION,
        # 决策 D5：降级时这一位是 true，08 据此建案（decision 仍写 review）
        "degraded": bool(outcome.degraded),
        "degrade_code": outcome.degrade_code,
        "degrade_reason": outcome.degrade_reason,
        "elapsed_ms": int(block.get("elapsed_ms") or 0),
        "decided_at": decided_at,
        # user_id / scene 是**冗余**，供 07/08 不联查事件就能建案。
        # 冗余不是设计缺陷：案件是"事后要长期保留"的对象，而 risk_events 有
        # 90 天 TTL（E01），案件却可能更久——联查会在 90 天后查不到。
        "user_id": str(event.get("user_id") or ""),
        "scene_code": rule_engine.scene_for(str(event.get("event_type") or "")),
        "event_type": str(event.get("event_type") or ""),
        "source": "rule_engine",
    }


def build_hit_docs(
    outcome: DecisionOutcome, decision_id: str, *, hit_at: int
) -> list[dict[str, Any]]:
    """组装 E04 `decision_hits` 文档（BR-05-21：**冗余快照** `rule_name` 与 `score`）。"""
    rows: list[dict[str, Any]] = []
    for index, hit in enumerate(outcome.block.get("hits") or []):
        rows.append({
            # E04 的 `_id` 没有格式约定（"明细编号"）。这里用
            # `{decision_id}-{序号}`：确定性、可读、且天然满足"同一决策内唯一"，
            # 重试写入时也不会因为随机 id 而重复插一条。
            "_id": f"{decision_id}-{index + 1:03d}",
            "decision_id": decision_id,
            "event_id": outcome.event_id,
            "rule_code": hit.get("rule_code"),
            "rule_name": hit.get("rule_name"),
            "rule_version": hit.get("rule_version"),
            "score": int(hit.get("score") or 0),
            "reason": hit.get("reason"),
            "matched_facts": dict(hit.get("matched_facts") or {}),
            "hit_at": hit_at,
        })
    return rows


async def _finish(event: dict, outcome: DecisionOutcome, *, dry_run: bool) -> None:
    """落地一次决策：统计 + （非 dry_run 时）异步落库。

    **本函数绝不抛异常**：它在"决策已经算完、正准备返回"的位置上，此时任何
    异常都会让 03 走它自己的降级路径（`EVT-5003`），而那条路径**不写
    decisions**——决策已经算出来了却被丢掉，正是 D5 要避免的事。因此取号
    失败只告警 + 计数，决策照常返回（调用方仍拿到 `review/reject/pass`）。
    """
    STATS["decided"] += 1
    if outcome.degraded:
        STATS["degraded"] += 1
    if dry_run:
        # Spec §3.1：`dry_run=true` 时**不写** decisions / decision_hits。
        # 这不是"可选优化"：仿真页（模块 10）会对同一批事件反复求值，
        # 若每次都落库，07 的案件列表与 11 的统计会被仿真数据污染，
        # 演示变得不可复现（与决策 D11 同一考虑）。
        return
    try:
        repo = DecisionRepo(db_module.get_db())
        decision_id = await repo.new_id()
        doc = build_decision_doc(event, outcome, decision_id, decided_at=now_ms())
        rows = build_hit_docs(outcome, decision_id, hit_at=doc["decided_at"])
    except Exception as e:  # noqa: BLE001 - 见 docstring：绝不因此丢掉已算出的决策
        STATS["persist_failed"] += 1
        log.warning(
            "决策落库准备失败（决策照常返回，但该请求不会进 08 的案件列表）"
            " event_id=%s err=%s", outcome.event_id, e,
        )
        return
    outcome.decision_id = decision_id
    _spawn_persist(doc, rows, decision_event(event, outcome))


def decision_event(event: dict, outcome: DecisionOutcome) -> dict:
    """抽出决策事件在**落库之后**仍需要的那几个字段（纯函数）。

    ## 为什么必须另拷一份、不能把入参 `event` 存进后台任务

    `event` 是调用方（03 的事件网关）传进来的对象，`/engine/evaluate` 那条路上
    还带着 `feature_snapshot`（一整个快照）、`scene_extra`、`idempotency_key` 等
    一整份报文。后台任务会活到本次请求结束之后，把整份报文挂在一个 Task 上等于
    把请求体一起留在内存里（高频路径上这是真实的放大）。

    这几个键恰好是**模块 11 §3.9 的入参契约**（`build_payloads` 只读它们），
    因此这里做的是"按契约裁剪"，不是"挑几个看起来有用的"：

    | 键 | 用途（11 的口径） |
    |---|---|
    | `event_id` | 幂等键（BR-11-10 at-most-once）与 SSE 帧 |
    | `event_type` | 场景映射（BR-11-06）与资损类型（BR-11-14） |
    | `ts` | **桶归属时刻**（`bucket_ts = align(event.ts)`，不是决策时刻） |
    | `amount` | `estimated_saved_amount`（仅 reject 计入） |
    | `user_id` / `biz_no` | SSE 实时帧的展示字段 |

    `event_id` 与 `_event_id(event)` 同源：03 用 `_id`，直接构造的事件可能用
    `event_id`，两者都要认（否则指标会带着空 `event_id` 落库，重放去重随即失效）。
    """
    return {
        "event_id": outcome.event_id or _event_id(event),
        "event_type": str(event.get("event_type") or ""),
        "user_id": str(event.get("user_id") or ""),
        "ts": event.get("ts"),
        "amount": event.get("amount"),
        "biz_no": event.get("biz_no"),
    }


def _spawn_persist(doc: dict, rows: list[dict], event: dict) -> None:
    """把落库丢到后台任务（AD-01），**不阻塞决策返回**。

    `dry_run=false` 时决策必须落库，但"决策已返回、库还没写"是允许的
    （BR-05-19 明文：决策同步返回、落库异步）。失败不回滚决策——决策一旦
    产生就不再回滚（与 BR-03-18 同源），失败只告警 + 计数。

    `event` 是 `decision_event()` 裁过的**事件摘要**（见该函数的说明），
    只服务于落库之后的两件旁路：建案（D5）与指标（§3.9 / D66）。
    """
    task = asyncio.create_task(
        _persist_with_retry(doc, rows, event), name="decision-persist"
    )
    _persist_tasks.add(task)
    task.add_done_callback(_persist_tasks.discard)


async def _persist_with_retry(doc: dict, rows: list[dict], event: dict) -> bool:
    """后台落库（有限次重试）。

    重试时**先判决策是否已在库**：一次"写入成功但响应超时"的失败会让重试撞
    `_id` 主键。撞主键说明**数据其实已经在了**，此时应当继续写明细而不是
    把整次落库判为失败——否则会出现"决策在库、命中明细永久缺失"的残缺记录，
    而 07 的页面正是靠 `decision_hits` 解释"为什么扣了分"。
    """
    repo = DecisionRepo(db_module.get_db())
    last: Optional[BaseException] = None
    for attempt in range(1, PERSIST_RETRY + 1):
        try:
            existing = await repo.find_decision(doc["_id"])
            if existing is None:
                await repo.insert_decision(doc)
            if rows and not await repo.list_hits(doc["_id"]):
                await repo.insert_hits(rows)
            STATS["persisted"] += 1
            # 【05 → 08】决策落库成功之后建案（Spec 08 §3.2 的冻结契约，
            # 决策 D5：降级产生的 review 也必须建案）。放在这里而不是 03：
            # 03 在 `stage="feature"` 的短路上**不写 decisions**（已知缺口
            # N-03-3，归 03），而 05 的降级**一定**写 decisions——建案只能挂在
            # "决策已落库"这一处，否则降级请求会无人处理（D5 要防的正是这个）。
            # 它**不影响**本次落库的成败：建案自身绝不抛异常，失败只告警，
            # 由 `case_maintenance_task` 的补建案扫描兜底。
            case_no = await _create_case_if_needed(doc)
            # 【05 → 11】同一处再补指标写入（§3.9 的冻结契约 / D66）。
            # 它排在最后：既不影响落库成败，也不影响建案。
            await _record_metrics(event, doc, rows, case_no=case_no)
            return True
        except Exception as e:  # noqa: BLE001 - 落库失败绝不回滚决策（AD-01）
            last = e
            if attempt < PERSIST_RETRY:
                await asyncio.sleep(PERSIST_RETRY_BASE_SEC * attempt)
    STATS["persist_failed"] += 1
    log.warning(
        "决策落库失败（决策照常返回，不入库将导致该请求无人处理）"
        " decision_id=%s err=%s",
        doc.get("_id"), last,
    )
    return False


async def _record_metrics(
    event: dict, doc: dict, rows: list[dict], *, case_no: Optional[str] = None,
) -> None:
    """把这次决策记入指标桶 + 实时流（**模块 11 §3.9 的调用点**，决策 D66）。

    ## 这是"漏了一次调用"，不是"缺机制"

    `metric_service.record_decision(event, decision, hits)` 的契约早就冻结好了
    （docstring 原文：「供模块 05 / 08 的异步落库任务调用（§3.9）」），
    但在此之前 `app/` 下**没有任何生产调用点**——于是大盘的
    `event_cnt` 恒为 0、`block_rate` 恒为 `null`（分母为 0），
    而这两个数字在页面上"看起来只是没有流量"。接上这一处调用，缺口才闭合。

    ## 为什么挂在这里（落库成功之后）

    1. **at-most-once 的权威保障在调用方**（BR-11-10 / 11 的模块说明）：
       只有"决策确实进了库"才该记指标，否则一次落库失败会让指标虚高；
    2. 与建案（D5）同在"决策已落库"这一处收尾，两条旁路的时序天然一致；
    3. 它 **`await` 在后台任务里**，决策早就在 200ms 预算内返回了
       （BR-03-23）——本函数不占用任何同步路径的时间。

    ## `dry_run=true` 为什么到不了这里

    `_finish()` 在 `dry_run` 时**已经 return**，根本不会调 `_spawn_persist()`，
    因此仿真链路（模块 10）不会写 `decisions`，也不会写指标桶——这正是
    模块 10 §4.2「数据隔离」与决策 D11 的要求。本函数仍然显式再判一次：
    指标污染**无法撤销**（桶是增量聚合，事后分不清哪条是仿真的），
    这条防线值得多一行代码。

    ## 失败处理（BR-11-11 / MET-5002）

    `record_decision` 契约上**永不抛异常**；即便如此这里仍然兜住所有异常：
    它跑在落库任务里，任何逸出的异常都会被 `_persist_with_retry` 当成
    "落库失败"并**重试整条落库**（决策其实已经写好），最终把 `persist_failed`
    打高——让"决策丢了"与"指标没记上"两件事在告警里混成一件。
    """
    if not event or not doc:
        return
    try:
        # 延迟导入：`metric_service` 会拉起指标引擎与实时总线，
        # 而本模块在 `protocols` 的默认装配路径上被 import（见文件顶部说明），
        # 模块级导入会把这条链拖进循环导入的风险里。
        from app.services.metric_service import record_decision

        decision = dict(doc)
        if case_no:
            # 实时帧要带案件号（`_publish` 读 `decision["case_no"]`）：
            # 大盘的实时流据此把"这一条被拦了，并已建案 CASE..."串起来。
            # 它是**响应期信息**，不写进 E03 文档（那是 08 的集合契约）。
            decision["case_no"] = case_no
        await record_decision(event, decision, rows)
        STATS["metrics_recorded"] += 1
    except Exception as e:  # noqa: BLE001 - 见 docstring：绝不影响落库与决策
        log.warning("[MET-5002] 决策指标记录失败（决策与建案均已生效）"
                    " decision_id=%s：%s", doc.get("_id"), e)


async def _create_case_if_needed(doc: dict) -> Optional[str]:
    """`review` / `reject` 的决策落库后建案（D5）。返回案件编号或 `None`。

    **本函数绝不抛异常**：调用点在"决策已经写进库、正准备收尾"的位置上，
    抛出去会被 `_persist_with_retry` 当成落库失败并**重试整条落库**
    （决策已存在，重试只会白跑），最终还会把 `persist_failed` 计数打高，
    让"决策丢了"与"案件没建"两件事在告警里混成一件。
    因此失败只告警，补救交给 `CaseService.recover_missing_cases`。
    """
    if str(doc.get("decision") or "") not in ("review", "reject"):
        return None
    try:
        # 延迟导入：`case_service` 会 import 仓储与服务，模块级导入会与
        # `app.engine.decision` ↔ `app.services` 形成循环
        from app.services.case_service import get_case_service

        case_no = await get_case_service().create_from_decision(
            str(doc.get("_id")), decision_doc=doc
        )
    except Exception as e:  # noqa: BLE001 - 见 docstring：绝不影响决策落库
        log.warning("[DSP] 建案失败（决策已落库，补建案扫描会重试）decision_id=%s：%s",
                    doc.get("_id"), e)
        return None
    if case_no:
        STATS["cases_created"] = STATS.get("cases_created", 0) + 1
    return case_no


async def flush(timeout: float = 5.0) -> bool:
    """等待全部后台落库任务收尾（测试与关闭流程使用）。

    为什么必须有它：`asyncio.create_task` 留下的任务若跨事件循环存活，
    下一个用例会拿到带着**旧循环**的库句柄并在 `db.close()` 之后报错，
    表现为"单独跑通过、一起跑报错"的幽灵缺陷（与 03/09 的 `flush` 同源）。
    """
    deadline = time.monotonic() + timeout
    while _persist_tasks:
        pending = [t for t in _persist_tasks if not t.done()]
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.wait(pending, timeout=remaining)
    return not _persist_tasks


def reset_stats() -> None:
    """清零进程内统计（测试夹具逐用例调用）。"""
    for key in STATS:
        STATS[key] = 0


__all__ = [
    "ENGINE_VERSION",
    "PERSIST_RETRY",
    "SLOW_THRESHOLD_MS",
    "STATS",
    "DecisionOutcome",
    "build_decision_doc",
    "build_hit_docs",
    "decide",
    "decision_event",
    "flush",
    "reset_stats",
]
