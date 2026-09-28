# -*- coding: utf-8 -*-
"""规则服务：规则配置（模块 06-B）的**全部业务不变量**。

不依赖 FastAPI、可注入假仓储单测（与 `list_service` 同一分层）。

已实现的业务规则（编号对齐模块 06 §4.1 / §4.2 / §4.4）：

    BR-06-01  编码由服务端生成 `R{场景码大写}{3位序号}`，同场景内递增且**不复用**
    BR-06-02  编码不可修改
    BR-06-03  版本从 1 起，每次修改（含启停用/删除）`version + 1` 且写一条审计
    BR-06-04  新建默认 `disabled`
    BR-06-05  `expected_version` 乐观锁，不匹配即 `CFG-4006`，不做后写覆盖
    BR-06-06  `score` 必须是 0~100 的整数（越界 `CFG-4004`）
    BR-06-07  `scene_code` 必须在 `rule_scenes` 里存在（**数据驱动**，D24）
    BR-06-08  `is_system=true` 不可删除（`CFG-4005`），只能停用
    BR-06-09  启用只记 `sim_verified` 标记，不硬拦截
    BR-06-10  删除是**软删**（`status=disabled` + 删除标记），文档保留
    BR-06-11  列表默认 `priority asc, _id asc`
    BR-06-12  场景内启用规则分值合计 > 100 → 非阻断提示（`score_hints`）
    BR-06-13  变更成功后**立即**失效规则缓存（不等 AD-02 的 TTL）
    BR-06-17  保存前必须调用 05 的 `validate_tree()`，本模块**不实现第二套校验**
    BR-06-36  每次成功的写操作**恰好**一条审计；写不进去就整体回滚
    §3.1 幂等  `POST /rules` 支持 `Idempotency-Key`，重复键返回首次结果

## 本模块明确不做的事（Spec §1，避免"看起来实现了"）

- **不实现条件树求值与结构校验**：唯一的校验入口是 `05` 的 `validate_tree()`
  （见 `validate_condition`），唯一的求值入口是 `05` 的 `rule_engine.evaluate_rules`。
- **不做分值累加与截断**（G-03 归 `BR-05-16`）：这里只管单条 `score ∈ [0,100]`。
- **不写 `decision_hits` 的冗余快照**（BR-05-21）：改名/改分**不回写**历史命中明细。
  那是**故意的失真防护**——旧决策必须继续按当时的名称与分值解释。
- **不自己拼审计哈希链**（AD-04）：只把结构化变更事件交给模块 12 的 `audit()`。

## 插入 / 审计分离（D41 范式）

每个写路径都拆成「`_xxx_only`（业务不变量 + 落库，**不写审计**）」+「对外方法
（写**恰好一条**审计，失败则回滚该次写并抛 `CFG-5003`）」。
把审计塞进底层方法，将来任何"批量"或"复用"路径都会变成一处两份审计——
"恰好一条"最容易正是在那里破掉。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import ValidationError

from app import constants
from app.engine import condition as condition_engine
from app.engine.condition import ConditionNode
from app.errors import (
    AppError,
    AuditRollbackError,
    ImportFormatError,
    InvalidConditionTreeError,
    InvalidQueryParamError,
    RuleCodeConflictError,
    RuleConditionInvalidError,
    RuleImmutableFieldError,
    RuleNotFoundError,
    RuleSceneNotFoundError,
    RuleScoreOutOfRangeError,
    RuleVersionConflictError,
    RuleWriteFailedError,
    SystemRuleNotDeletableError,
)
from app.logging import get_logger
from app.repos.rule_repo import RuleAdminRepo, rule_code_for
from app.schemas.list_schema import IMPORT_ENCODINGS
from app.schemas.rule_schema import (
    DEFAULT_SORT,
    RULE_CODE_PATTERN,
    RULE_IMPORT_ERROR_LIMIT,
    RULE_IMPORT_HEADER,
    RULE_IMPORT_MAX_ROWS,
    RULE_IMPORT_MODES,
    RULE_STATUSES,
    SCORE_MAX,
    SCORE_MIN,
    SCORE_SUM_HINT_LIMIT,
    RuleCreate,
    RuleDeleteResult,
    RuleImpactOut,
    RuleImportFailRow,
    RuleImportResult,
    RuleOut,
    RuleQueryResult,
    RuleToggleIn,
    RuleUpdate,
    ScoreHint,
    ValidateTreeOut,
    parse_rule_sort,
)
from app.services.audit_service import audit
from app.services.idempotency import IdempotencyStore, payload_digest
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.rule")

#: 编码重试次数。同场景序号在并发下可能被抢（两个策略师同时新建），`rules._id`
#: 是唯一主键、冲突会抛 `DuplicateKeyError`；重算序号重试即可，重试耗尽才报
#: `CFG-4002`（BR-06-01 要求"服务端重生成序号并提示"，不是直接失败）。
_CODE_RETRY = 5

#: 删除确认弹窗展示命中次数的窗口（Spec §5.2「删除规则」行：近 30 天）
IMPACT_WINDOW_DAYS = 30
DAY_MS = 24 * 3600 * 1000

_MSG_READ_UNAVAILABLE = "规则数据暂时不可用，请稍后重试"


# ============================================================
# 规则缓存失效调用点（BR-06-13 / AD-02）
# ============================================================
class _RuleCacheFlush:
    """规则缓存失效的观测状态（时刻 + 次数）。

    `flush_count` 存在的意义：BR-06-13 的验收对象是"**每个**写路径都调用了失效点"，
    而"调用过"这件事必须能被断言。把它做成可读的计数器，测试就能钉住
    "创建 / 修改 / 启停用 / 删除 各调用一次"，而不是只相信代码里有一行调用。
    """

    def __init__(self) -> None:
        self.last_flush_at: int = now_ms()
        self.flush_count: int = 0

    def mark(self) -> None:
        self.last_flush_at = now_ms()
        self.flush_count += 1

    def reset(self) -> None:
        """复位（测试逐用例使用；生产无调用点）。"""
        self.last_flush_at = now_ms()
        self.flush_count = 0


RULE_CACHE_FLUSH = _RuleCacheFlush()


def invalidate_rule_cache() -> None:
    """BR-06-13：规则变更落库成功后**立即**失效规则缓存，不等 AD-02 的 10s TTL。

    ## 当前装配下它为什么"看起来什么都没做"

    模块 05 读规则走 `app/engine/decision.py` → `RuleRepo.list_enabled_rules()`，
    **每次决策直查 Mongo**（E05 没有进程内 TTL 缓存；AD-02 的缓存只覆盖名单 E07）。
    因此"变更立即对决策侧可见"在当前装配下自动成立，没有对象需要清空。

    ## 那为什么还必须存在、还必须被每条写路径调用

    1. AD-02 / BR-06-13 要求的是一个**契约**：管理侧变更后不依赖 TTL 立即生效。
       它的落点必须在写入侧代码里显式可见——将来 05 一旦给 `rules` 加上缓存，
       改动只需发生在这**一个**函数体内，而不会漏掉某条写路径（这正是名单侧
       `invalidate_list_cache()` 的同一形态）。
    2. 它记录 `last_flush_at` / `flush_count`，让"这次变更什么时候对决策侧可见"
       可被排障回答，也让"每条写路径都调了失效点"可被测试钉住。

    **任何异常都必须向上抛**：吞掉就变成"改了库却以为缓存已失效"，
    宁可让本次写操作失败（此时状态尚未提交完成）也不留下 fail-open 的窗口。
    """
    RULE_CACHE_FLUSH.mark()


# ============================================================
# 条件树校验：**唯一的入口是 05 的 validate_tree()**（BR-06-17）
# ============================================================
def validate_condition(raw: Any) -> tuple[ConditionNode, dict]:
    """调用 05 的 `validate_tree()`，返回 `(条件树对象, 归一化 JSON)`。

    **本函数不包含任何结构判断**（没有"有没有 field""op 合不合法""深度超没超"
    的分支）——那些全部在 `app/engine/condition.py` 里，Spec §1 的边界裁定原文：
    「不做条件树的结构校验实现——`validate_tree()` 由本模块提供，06 只调用」。
    在这里补一份"顺手也检查一下"的逻辑，就必然与 05 漂移，而漂移的表现是
    **保存时通过、决策时求值失败**：一条配好了却永不生效的规则。

    ## 为什么走 `condition_engine.validate_tree` 而不是 `from ... import validate_tree`

    后者把函数对象在**导入时**绑死，于是 `monkeypatch.setattr` 到 05 的模块属性
    对保存路径不再生效——而 V-06-03 的验收方法恰恰是"在 `engine/condition.py`
    的 `validate_tree` 中插入桩，看保存时是否出现该桩错误"。走模块属性访问，
    这个桩才能真正证明"校验发生在 05 里"（与 `list_cleanup_task` 通过模块属性
    调用 `invalidate_list_cache` 是同一个理由：**可被替换**本身就是契约的一部分）。

    归一化用 `exclude_unset=True` 而不是 `exclude_none=True`：`exists` 的合法
    写法是**完全没有** `value` 键，而 `{"op": "eq", "value": null}` 是"拿 null 去
    比较"——两者的区别只存在于"键在不在"。`exclude_none` 会把后者也删掉，
    等于**静默改变用户写的语义**（`eq null` 变成"没有比较值"）。
    """
    try:
        node = condition_engine.validate_tree(raw)
    except InvalidConditionTreeError as e:
        detail = str((e.data or {}).get("detail") or e.message)
        path = str((e.data or {}).get("node_path") or "")
        raise RuleConditionInvalidError(
            detail, path,
            # `code` 保持 05 的原码（ER-02：引用别人的码不改前缀），
            # 并附上 06 对外的 `cfg_code`，让前端既能溯源又能按 §5.1 映射文案。
            errors=[{"path": path, "code": "RUL-4002", "cfg_code": "CFG-4003",
                     "message": detail}],
        ) from e
    return node, node.model_dump(mode="json", exclude_unset=True)


def check_condition(raw: Any) -> ValidateTreeOut:
    """`POST /rules/validate-tree` 的结论（**校验失败也是 200**，Spec §3.1）。"""
    try:
        _node, normalized = validate_condition(raw)
    except RuleConditionInvalidError as e:
        errors = list((e.data or {}).get("errors") or [])
        return ValidateTreeOut(valid=False, errors=errors, normalized=None)
    return ValidateTreeOut(valid=True, errors=[], normalized=normalized)


def _check_score(score: Any) -> int:
    """BR-06-06：整数且 `0 ≤ score ≤ 100`。

    `bool` 必须显式排除：`isinstance(True, int)` 为真，若不排除，`score: true`
    会被当成 1 分写进库——规则分值变成布尔值，重放时会算出莫名其妙的分。
    """
    if isinstance(score, bool) or not isinstance(score, int) or not (
        SCORE_MIN <= score <= SCORE_MAX
    ):
        raise RuleScoreOutOfRangeError(score)
    return score


def ensure_no_immutable_fields(raw: Any) -> None:
    """CFG-4007：客户端提交了不可变字段即拒绝（Spec §3.1 PUT 的 400 状态码）。

    为什么在**接口层读原始 JSON** 判而不能交给 Pydantic 的 `extra="forbid"`：
    那会把 `_id` / `is_system` 这类"改了就破坏可追溯性"的字段混同为拼写错误
    （`COM-4001` 422），而契约明确要求 `400 CFG-4007`。**静默忽略同样不行**：
    "我提交了 is_system=true，接口返回成功"会让调用方以为它生效了。

    放在服务层而不是接口层：这条判定是**业务不变量**（哪些字段由服务端独占），
    与 HTTP 无关；接口层只负责"从原始报文里把它取出来"。
    """
    from app.schemas.rule_schema import IMMUTABLE_FIELDS

    if not isinstance(raw, dict):
        return
    bad = sorted(k for k in IMMUTABLE_FIELDS if k in raw)
    if bad:
        raise RuleImmutableFieldError(bad)


def _build_doc(
    code: str, payload: RuleCreate, condition: dict, score: int,
    operator: str, ts: int,
) -> dict:
    """组装 E05 规则文档（新建与批量导入共用，避免两条路写出两种文档形状）。"""
    return {
        "_id": code,
        "name": payload.name,
        "scene_code": payload.scene_code,
        "description": payload.description,
        "condition": condition,
        "score": score,
        "priority": payload.priority,
        # BR-06-04：默认停用。传 enabled 也允许（BR-06-09 只记录不拦截）
        "status": payload.status,
        "version": 1,
        # BR-06-37：内置规则由种子初始化，页面**不提供**创建入口
        "is_system": False,
        "deleted": False,
        "created_by": operator,
        "updated_by": operator,
        "created_at": ts,
        "updated_at": ts,
    }


class RuleService:
    """规则写路径 + 管理侧读路径。"""

    def __init__(self, repo: RuleAdminRepo):
        self.repo = repo

    # ---------------- 场景字典（D24：数据驱动，不硬编码场景枚举） ----------------
    async def _scene_names(self) -> dict[str, str]:
        return await self.repo.scene_names()

    async def _ensure_scene(self, scene_code: str) -> dict[str, str]:
        names = await self._scene_names()
        if scene_code not in names:
            raise RuleSceneNotFoundError(scene_code, sorted(names))
        return names

    # ---------------- 查询 ----------------
    async def list_rules(
        self,
        *,
        scene_code: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = constants.PAGE_DEFAULT,
        page_size: int = constants.PAGE_SIZE_DEFAULT,
        sort: str = DEFAULT_SORT,
    ) -> RuleQueryResult:
        """`GET /rules`（Spec §3.1 + §3.5 列表契约）。

        分页/排序边界一律在服务层判定，错误码才是契约要求的 `CFG-4008`
        （交给 FastAPI 的 `Query(ge=1)` 会先被模块 00 拦成 `COM-4001`，
        丢掉"分页参数越界"这一具体语义，与 06-A 同一处理）。
        """
        if page < 1:
            raise InvalidQueryParamError("page 必须 ≥ 1")
        if page > constants.PAGE_MAX:
            raise InvalidQueryParamError(f"page 不能超过 {constants.PAGE_MAX}")
        if not (1 <= page_size <= constants.PAGE_SIZE_MAX):
            raise InvalidQueryParamError(
                f"page_size 必须在 1~{constants.PAGE_SIZE_MAX} 之间"
            )
        try:
            sort_pairs = parse_rule_sort(sort)
        except ValueError as e:
            raise InvalidQueryParamError(str(e)) from e

        # 软删的规则不出现在列表里（BR-06-10：文档保留是为了历史可回溯，
        # 不是为了让它们继续占据策略师的列表）
        flt: dict[str, Any] = {"deleted": {"$ne": True}}
        if scene_code:
            flt["scene_code"] = scene_code
        if status:
            if status not in RULE_STATUSES:
                raise InvalidQueryParamError(
                    f"status 仅支持 {'/'.join(RULE_STATUSES)}"
                )
            flt["status"] = status
        clauses = _keyword_clauses(keyword or "")
        if len(clauses) == 1:
            flt.update(clauses[0])
        elif clauses:
            flt["$or"] = clauses

        skip = (page - 1) * page_size
        total = await self.repo.count(flt)
        docs = await self.repo.query(flt, sort_pairs, skip, page_size)
        names = await self._scene_names()
        hints = await self._score_hints(names)

        return RuleQueryResult(
            items=[RuleOut.from_doc(d, names.get(d.get("scene_code"))) for d in docs],
            total=total,
            page=page,
            page_size=page_size,
            # 向上取整；total=0 时 pages=0（前端据此走空态而不是"第 1/1 页"）
            pages=(total + page_size - 1) // page_size if total else 0,
            as_of=now_ms(),
            score_hints=hints,
            over_limit_scenes=[h.scene_code for h in hints if h.over_limit],
        )

    async def _score_hints(self, names: dict[str, str]) -> list[ScoreHint]:
        """BR-06-12：场景内**启用**规则分值合计 > 100 时的非阻断提示数据。"""
        rows = await self.repo.enabled_score_sums()
        hints = [
            ScoreHint(
                scene_code=str(r["_id"]),
                scene_name=names.get(str(r["_id"]), ""),
                enabled_score_sum=int(r.get("score_sum") or 0),
                enabled_rule_count=int(r.get("rule_count") or 0),
                over_limit=int(r.get("score_sum") or 0) > SCORE_SUM_HINT_LIMIT,
            )
            for r in rows
        ]
        hints.sort(key=lambda h: h.scene_code)
        return hints

    async def get_rule(self, rule_code: str) -> RuleOut:
        """取单条规则（不存在或已软删 → `CFG-4001`）。

        对外暴露为一个**公开方法**而不是让接口层去调 `_load`：`_load` 是内部
        读口径（后续若加"带 deleted 的排障视图"会改它），接口层不该绑在私有
        方法上。
        """
        doc = await self._load(rule_code)
        names = await self._scene_names()
        return RuleOut.from_doc(doc, names.get(doc.get("scene_code")))

    async def impact(self, rule_code: str) -> RuleImpactOut:
        """删除确认弹窗的影响面（Spec §5.2：编码 + 名称 + 近 30 天命中次数）。"""
        doc = await self._load(rule_code)
        since = now_ms() - IMPACT_WINDOW_DAYS * DAY_MS
        hits = await self.repo.hit_count_since(rule_code, since)
        refs = await self.repo.decision_ref_count(rule_code)
        return RuleImpactOut(
            rule_code=rule_code,
            name=str(doc.get("name") or ""),
            version=int(doc.get("version") or 1),
            status=str(doc.get("status") or "disabled"),
            is_system=bool(doc.get("is_system")),
            deleted=bool(doc.get("deleted")),
            hit_count_30d=hits,
            decision_refs=refs,
            hint=(
                "系统内置规则不可删除，只能停用"
                if doc.get("is_system")
                else f"历史决策记录不受影响（命中明细为快照，共 {refs} 条决策引用过该规则）"
            ),
        )

    # ---------------- 新建（BR-06-01 / 03 / 04 / 36，§3.1 幂等） ----------------
    async def create_rule(
        self,
        payload: RuleCreate,
        operator: str,
        *,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[RuleOut, bool]:
        """新建规则。返回 `(规则, 是否幂等回放)`。

        顺序固定为 **落库 → 审计 → 失效缓存**：
        - 审计在失效缓存之前：审计写不进去就撤销这次写入，05 不会看到一条
          "没有留痕的规则"（BR-06-36：宁可不做，不可无痕地做）；
        - 失效缓存在最后：它一旦执行就意味着"本次变更对决策侧可见"，
          必须在审计已经落地之后。
        """
        replayed = await self._replay_or_reserve(idempotency_key, payload)
        if replayed is not None:
            return replayed, True
        try:
            rule = await self._insert_rule_only(payload, operator)
            try:
                await audit(
                    actor=operator, actor_role=actor_role, action="rule.create",
                    target_type="rule", target_id=rule.code,
                    before=None, after=rule.model_dump(by_alias=True),
                    ip=ip, ua=ua, strict=True,
                )
            except AppError as e:
                await self._compensate_create(rule.code)
                log.error("rule.create 审计写入失败，已撤销新增 rule=%s：%s",
                          rule.code, e)
                raise AuditRollbackError("rule.create", type(e).__name__) from e
        except BaseException:
            # 任何失败都不能留下"占着幂等键却永远没有结果"的在途项：
            # 那会让同键重试一直等一个不会到来的结果（比报错更难排查）
            self._release_idempotency(idempotency_key)
            raise
        invalidate_rule_cache()
        self._finish_idempotency(idempotency_key, rule)
        return rule, False

    async def _insert_rule_only(
        self, payload: RuleCreate, operator: str, *, rule_code: Optional[str] = None
    ) -> RuleOut:
        """只做业务不变量与落库，**不写审计**（审计由调用方决定时机）。

        `rule_code` 非空时使用**调用方指定的编码**（批量导入，§8 新增-7），
        此时不做重试：编码是用户给的，撞了就如实报 `CFG-4002`，而不是悄悄
        换一个编码写进去——那会让用户手里的文件与库里的编码对不上。
        """
        score = _check_score(payload.score)
        names = await self._ensure_scene(payload.scene_code)
        _node, condition = validate_condition(payload.condition)
        ts = now_ms()

        if rule_code:
            doc = _build_doc(rule_code, payload, condition, score, operator, ts)
            try:
                await self.repo.insert(doc)
            except DuplicateKeyError as e:
                raise RuleCodeConflictError(rule_code, 1) from e
            except PyMongoError as e:
                raise RuleWriteFailedError(f"{type(e).__name__}: {e}") from e
            return RuleOut.from_doc(doc, names.get(payload.scene_code))

        seq = await self.repo.max_seq(payload.scene_code) + 1
        last_code = ""
        for attempt in range(1, _CODE_RETRY + 1):
            code = rule_code_for(payload.scene_code, seq)
            last_code = code
            doc = _build_doc(code, payload, condition, score, operator, ts)
            try:
                await self.repo.insert(doc)
            except DuplicateKeyError:
                # 并发下同一个序号被抢先占用：按 BR-06-01 重算序号（**不复用**已删编码）
                log.warning("规则编码 %s 已被占用，重算序号（第 %d 次）", code, attempt)
                seq += 1
                continue
            except PyMongoError as e:
                raise RuleWriteFailedError(f"{type(e).__name__}: {e}") from e
            return RuleOut.from_doc(doc, names.get(payload.scene_code))
        raise RuleCodeConflictError(last_code, _CODE_RETRY)

    # ---------------- 修改（BR-06-02 / 05 / 06 / 36） ----------------
    async def update_rule(
        self,
        rule_code: str,
        payload: RuleUpdate,
        operator: str,
        *,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> RuleOut:
        """修改规则（`PUT /rules/{code}`）。**成功即 `version+1`**（BR-06-03）。

        注意：这里**不把 version 写进 `after` 的差值判断**，而是无条件推进——
        Spec §3.1 的 PUT 行为写的就是"`version = expected_version + 1`"，
        这与启停用的幂等（BR-06-03 末句 / V-06-20）是两条不同的规则：
        **改内容一定升版本**（D60：否则 `rule_versions` 快照无法重放），
        **改状态到同一状态则不动**（否则审计里会出现一堆什么都没变的记录）。
        """
        current = await self._load(rule_code)
        before_rule = RuleOut.from_doc(current)

        changes: dict[str, Any] = {"updated_by": operator, "updated_at": now_ms()}
        names = await self._scene_names()

        if "name" in payload.model_fields_set and payload.name is not None:
            changes["name"] = payload.name
        if "description" in payload.model_fields_set:
            changes["description"] = payload.description
        if "scene_code" in payload.model_fields_set and payload.scene_code is not None:
            # BR-06-02 允许改场景；改场景意味着编码前缀与场景不再匹配，
            # 但**编码本身不可改**（BR-06-01/02）。这里如实执行并通过审计留痕，
            # 不做"顺手把编码也改了"——那会让历史 decision_hits 的 rule_code 断链。
            names = await self._ensure_scene(payload.scene_code)
            changes["scene_code"] = payload.scene_code
        if "score" in payload.model_fields_set and payload.score is not None:
            changes["score"] = _check_score(payload.score)
        if "priority" in payload.model_fields_set and payload.priority is not None:
            changes["priority"] = payload.priority
        if "status" in payload.model_fields_set and payload.status is not None:
            changes["status"] = payload.status
        if "condition" in payload.model_fields_set:
            _node, condition = validate_condition(payload.condition)
            changes["condition"] = condition

        try:
            matched = await self.repo.update_with_version(
                rule_code, payload.expected_version, changes
            )
        except PyMongoError as e:
            raise RuleWriteFailedError(f"{type(e).__name__}: {e}") from e
        if matched == 0:
            await self._raise_conflict(rule_code, payload.expected_version)

        after_doc = {**current, **changes, "version": payload.expected_version + 1}
        after_rule = RuleOut.from_doc(after_doc, names.get(after_doc.get("scene_code")))
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="rule.update",
                target_type="rule", target_id=rule_code,
                before=before_rule.model_dump(by_alias=True),
                after=after_rule.model_dump(by_alias=True),
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = await self._rollback_write(
                rule_code, payload.expected_version + 1,
                # `version` 必须一起还原：只回滚内容而留着推进过的版本号，
                # 乐观锁就会错位——前端手里的 v1 会永远匹配不上库里的 v2。
                {"version": int(current.get("version") or 1),
                 **{k: current.get(k) for k in changes}},
            )
            log.error("rule.update 审计写入失败，已回滚 rule=%s：%s", rule_code, e)
            hint = "" if rolled else "；且回滚亦失败，请人工核对规则内容"
            raise AuditRollbackError("rule.update", f"{type(e).__name__}{hint}") from e

        invalidate_rule_cache()
        return after_rule

    # ---------------- 启停用（幂等，BR-06-03 / 09 / V-06-20） ----------------
    async def toggle_rule(
        self,
        rule_code: str,
        payload: RuleToggleIn,
        operator: str,
        *,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        sim_verified: bool = False,
    ) -> tuple[RuleOut, bool]:
        """启停用。返回 `(规则, changed)`。

        **幂等**（Spec §3.1 / V-06-20）：目标状态与当前一致时**不递增 version、
        不写审计、不失效缓存**，直接返回 `changed=false`。
        为什么必须这样：页面上的开关是"点一下切换一次"，重复点击（或网络重试）
        若每次都升版本并写审计，规则版本会被无意义的点击推高——而 `version` 是
        决策重放的锚（D60），它一旦失真，"这条规则当时是哪一版"就再也说不清。
        """
        current = await self._load(rule_code)
        names = await self._scene_names()
        if str(current.get("status")) == payload.status:
            return RuleOut.from_doc(current, names.get(current.get("scene_code"))), False

        changes = {
            "status": payload.status,
            "updated_by": operator,
            "updated_at": now_ms(),
        }
        try:
            matched = await self.repo.update_with_version(
                rule_code, payload.expected_version, changes
            )
        except PyMongoError as e:
            raise RuleWriteFailedError(f"{type(e).__name__}: {e}") from e
        if matched == 0:
            await self._raise_conflict(rule_code, payload.expected_version)

        version_after = payload.expected_version + 1
        before = {"status": current.get("status"),
                  "version": int(current.get("version") or 1)}
        after: dict[str, Any] = {"status": payload.status, "version": version_after}
        # BR-06-09：服务端不强制"仿真通过才能启用"，但审计里要能回答
        # "这次启用是不是带着仿真验证来的"（否则这条要求等于没有落点）。
        if payload.status == "enabled":
            after["sim_verified"] = bool(sim_verified or payload.sim_verified)
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="rule.toggle",
                target_type="rule", target_id=rule_code,
                before=before, after=after, ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = await self._rollback_write(
                rule_code, version_after,
                {"status": current.get("status"),
                 "version": int(current.get("version") or 1)},
            )
            log.error("rule.toggle 审计写入失败，已回滚 rule=%s：%s", rule_code, e)
            hint = "" if rolled else "；且回滚亦失败，请人工核对规则状态"
            raise AuditRollbackError("rule.toggle", f"{type(e).__name__}{hint}") from e

        invalidate_rule_cache()
        after_doc = {**current, **changes, "version": version_after}
        return RuleOut.from_doc(after_doc, names.get(after_doc.get("scene_code"))), True

    # ---------------- 软删除（BR-06-08 / 10 / 36） ----------------
    async def delete_rule(
        self,
        rule_code: str,
        operator: str,
        *,
        expected_version: Optional[int] = None,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> RuleDeleteResult:
        """软删除（BR-06-10）：`status=disabled` + 删除标记，**不物理删除**。

        为什么不物理删：历史 `decision_hits.rule_code` 与 `decisions.rule_versions`
        都按编码回溯（BR-05-21 / D60）。物理删除会让"这条历史命中是哪条规则"
        永远失去指向——而删除一条规则**不该**抹掉它曾经拦下过什么。

        `expected_version` 可选：携带则严格乐观锁（BR-06-05）；不携带时仍有并发
        保护——条件更新同时带 `version` 与"未删除"前提，匹配 0 条即 `409 CFG-4006`。
        """
        current = await self._load(rule_code)
        if current.get("is_system"):
            raise SystemRuleNotDeletableError(rule_code)

        version = int(current.get("version") or 1)
        if expected_version is not None and int(expected_version) != version:
            raise RuleVersionConflictError(rule_code, int(expected_version), version)

        ts = now_ms()
        changes = {
            "status": "disabled",
            "deleted": True,
            "deleted_at": ts,
            "deleted_by": operator,
            "updated_by": operator,
            "updated_at": ts,
        }
        try:
            matched = await self.repo.update_with_version(rule_code, version, changes)
        except PyMongoError as e:
            raise RuleWriteFailedError(f"{type(e).__name__}: {e}") from e
        if matched == 0:
            # 走到这里说明"读到它"之后有人改过：可能已经删了（→404），
            # 也可能只是改了别的内容（→409 版本冲突）。如实区分，不用一个码糊过去。
            fresh = await self.repo.find_by_code(rule_code)
            if fresh is None or fresh.get("deleted"):
                raise RuleNotFoundError(rule_code)
            raise RuleVersionConflictError(
                rule_code, version, int(fresh.get("version") or 1)
            )

        version_after = version + 1
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="rule.delete",
                target_type="rule", target_id=rule_code,
                before={"status": current.get("status"), "version": version,
                        "deleted": False},
                after={"status": "disabled", "version": version_after, "deleted": True,
                       "deleted_at": ts, "deleted_by": operator},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = await self._rollback_write(
                rule_code, version_after,
                {"status": current.get("status"), "deleted": False, "version": version},
                unset=("deleted_at", "deleted_by"),
            )
            log.error("rule.delete 审计写入失败，已回滚软删 rule=%s：%s", rule_code, e)
            hint = "" if rolled else "；且回滚亦失败，请人工核对规则状态"
            raise AuditRollbackError("rule.delete", f"{type(e).__name__}{hint}") from e

        invalidate_rule_cache()
        try:
            affected = await self.repo.decision_ref_count(rule_code)
        except AppError:
            # 影响面只是给页面刷新用的附加信息：一次**已经成功且已留痕**的删除
            # 不该因为它取不到而变成失败（那会诱导用户重试，而重试必然撞 404）。
            affected = -1
        return RuleDeleteResult(
            rule_code=rule_code,
            name=str(current.get("name") or ""),
            status="disabled",
            deleted=True,
            version=version_after,
            deleted_at=ts,
            deleted_by=operator,
            affected_decisions=affected,
        )

    # ---------------- 批量导入（Spec §3.1 / §8 新增-7） ----------------
    async def import_rules(
        self,
        content: bytes,
        *,
        mode: str = "partial",
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> RuleImportResult:
        """CSV 批量导入规则。

        **行号口径**：`row` 一律是**文件内真实行号**（表头是第 1 行，数据行从 2 起），
        用户在 Excel 里能直接按行号定位。

        **`atomic` 的保证范围（如实声明）**：它的语义是"**任一行校验不过则整批不写**"，
        由"先全量校验、再逐行落库"实现。落库阶段若 Mongo 中途故障，已写入的行**不会**
        被回滚（Mongo 单机没有跨文档事务，本项目也未启用副本集事务）。这与 06-A 的
        名单导入完全同语义——两处保持一致比"一边有一边没有"更不容易误导。
        """
        if mode not in RULE_IMPORT_MODES:
            raise ImportFormatError(f"mode 仅支持 {'/'.join(RULE_IMPORT_MODES)}，收到：{mode}")

        parsed = _parse_rule_import_csv(content)
        total = len(parsed)
        if total > RULE_IMPORT_MAX_ROWS:
            # 超限直接拒绝并提示分批（不静默截断：那会让用户以为剩下的也导进去了）
            raise ImportFormatError(
                f"单次导入上限 {RULE_IMPORT_MAX_ROWS} 行，本次 {total} 行，请拆分后分批导入"
            )

        failures: list[RuleImportFailRow] = []
        valid: list[tuple[int, RuleCreate, str]] = []
        known_scenes = await self._scene_names()
        for row_no, raw in parsed:
            payload, reason, code = _build_rule_row(raw, known_scenes)
            if payload is None:
                failures.append(RuleImportFailRow(row=row_no, rule_code=code,
                                                  reason=reason or "数据不合法"))
            else:
                valid.append((row_no, payload, code))

        if mode == "atomic" and failures:
            # 整批不写：一条都不落库，返回**全部**错误行（让用户一次改完）
            return _build_import_result(total, 0, failures, "atomic", [], truncated=False)

        imported: list[str] = []
        for row_no, payload, code in valid:
            try:
                rule = await self._insert_rule_only(
                    payload, operator, rule_code=code or None
                )
            except AppError as e:
                failures.append(RuleImportFailRow(row=row_no, rule_code=code,
                                                  reason=e.message))
                continue
            except Exception as e:  # noqa: BLE001 - 单行未知异常同样只废掉这一行
                failures.append(RuleImportFailRow(
                    row=row_no, rule_code=code, reason=f"写入失败：{type(e).__name__}"
                ))
                continue
            # BR-06-36：每个成功行写**恰好一条** `rule.create`（before=null，after=条目）
            try:
                await audit(
                    actor=operator, actor_role=actor_role, action="rule.create",
                    target_type="rule", target_id=rule.code,
                    before=None, after=rule.model_dump(by_alias=True),
                    ip=ip, ua=ua, strict=True,
                )
            except AppError as e:
                # 审计落不下去 -> 这一行必须撤销（宁可不做，不可无痕地做）。
                # 只撤销这一行而不是整批报 503：已留痕的行是真实且合规的，
                # 把它们一起算失败会让用户以为"一行都没进去"而重复导入。
                await self._compensate_create(rule.code)
                failures.append(RuleImportFailRow(
                    row=row_no, rule_code=rule.code,
                    reason=f"审计写入失败，该行已回滚（{e.code}）",
                ))
                continue
            imported.append(rule.code)

        if imported:
            # 缓存失效在**批末做一次**（与名单导入同一理由：逐行调用没有额外正确性
            # 收益，却会把 500 行放大成 500 次失效开销）。
            try:
                invalidate_rule_cache()
            except Exception as e:  # noqa: BLE001
                log.error("导入后规则缓存失效失败，已置告警：%s", e)

        failures.sort(key=lambda f: f.row)
        return _build_import_result(
            total, len(imported), failures, mode, imported,
            truncated=len(failures) > RULE_IMPORT_ERROR_LIMIT,
        )

    # ---------------- 内部 ----------------
    async def _load(self, rule_code: str) -> dict:
        """取规则；不存在或已被删除标记屏蔽时抛 `CFG-4001`。"""
        doc = await self.repo.find_by_code(rule_code)
        if doc is None or doc.get("deleted"):
            raise RuleNotFoundError(rule_code)
        return doc

    async def _raise_conflict(self, rule_code: str, expected: int) -> None:
        """条件更新匹配 0 条：区分"已被删除"（404）与"版本被他人推进"（409）。"""
        fresh = await self.repo.find_by_code(rule_code)
        if fresh is None or fresh.get("deleted"):
            raise RuleNotFoundError(rule_code)
        raise RuleVersionConflictError(
            rule_code, expected, int(fresh.get("version") or 1)
        )

    async def _rollback_write(
        self, rule_code: str, version_after: int, before: dict,
        unset: tuple[str, ...] = (),
    ) -> bool:
        """审计失败后的补偿：把文档按新版本号回滚成 `before`。返回是否成功。

        回滚**不再抛替代异常**（调用方已在处理审计故障，再抛一个会把真因盖掉），
        但失败必须 error 日志：此时库里会留下一条"改了却没留痕"的记录，
        需要运维据此排查。
        """
        try:
            restored = await self.repo.restore_after_failed_audit(
                rule_code, version_after, before, unset=unset
            )
        except (AppError, PyMongoError) as e:
            log.error("回滚规则写入失败 rule=%s：%s", rule_code, e)
            return False
        if restored == 0:
            log.error("回滚规则写入匹配 0 条（版本已被他人推进）rule=%s", rule_code)
            return False
        return True

    async def _compensate_create(self, rule_code: str) -> None:
        """`rule.create` 审计失败后的补偿：物理删掉刚插入的文档。

        用物理删除而不是软删：这次写入业务上等于**从未发生**（BR-06-36），
        留一条"已删除"的孤儿文档反而会污染列表与统计口径。
        """
        try:
            await self.repo.delete_by_code(rule_code)
        except PyMongoError as e:
            log.error("规则新增补偿删除失败 rule=%s：%s", rule_code, e)

    # ---------------- 幂等（Spec §3.1：无天然幂等键，用 Idempotency-Key） ----------------
    async def _replay_or_reserve(
        self, idempotency_key: Optional[str], payload: RuleCreate
    ) -> Optional[RuleOut]:
        """命中幂等键则返回**首次结果**；否则登记为首领并返回 `None`。

        `IdempotencyStore` 是 03 事件幂等的实现（TTL + LRU + single-flight），
        这里**复用同一个类、另起一个实例**（键空间不同：事件编号 vs 规则保存键）。
        复用它而不是另写一份，是因为"查—判—登记不能跨 await"这条正确性要求
        （BR-03-14 的实质）在规则保存上同样成立：跨 await 之后两个同键请求会
        双双登记、双双插入，产生两条不同编码的重复规则。
        """
        if not idempotency_key:
            return None
        key = _idem_key(idempotency_key)
        digest = payload_digest(payload.model_dump(mode="json"))
        state, body = _CREATE_IDEMPOTENCY.check(key, digest)
        if state == "hit":
            return RuleOut.model_validate(body)
        if state == "inflight":
            replayed = await self._await_leader(key)
            if replayed is not None:
                return replayed
        elif state == "conflict":
            # 同键不同载荷：Spec 未给该场景分配错误码，且"不同载荷"本就是**另一次
            # 保存**（前端每次打开抽屉生成新 UUID）。因此按新请求处理并覆盖占位，
            # 而不是凭空发明一个 409——凭空加码会与 §5.1 的码表冲突。
            _CREATE_IDEMPOTENCY.release(key)
        state, _entry = _CREATE_IDEMPOTENCY.reserve(key, digest)
        if state == "hit":
            _s, body = _CREATE_IDEMPOTENCY.check(key, digest)
            if body is not None:
                return RuleOut.model_validate(body)
        elif state == "inflight":
            replayed = await self._await_leader(key)
            if replayed is not None:
                return replayed
        return None

    async def _await_leader(self, key: str) -> Optional[RuleOut]:
        """等待在途首领的结果；挂不上（首领已失败释放）则返回 `None` 自行执行。"""
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        if not _CREATE_IDEMPOTENCY.attach_waiter(key, waiter):
            return None
        try:
            body = await waiter
        except Exception:  # noqa: BLE001 - 首领失败：退化为自己执行，绝不返回空结果
            log.warning("幂等首领失败，本次改为自行执行 key=%s", key)
            return None
        return RuleOut.model_validate(body)

    def _release_idempotency(self, idempotency_key: Optional[str]) -> None:
        if idempotency_key:
            _CREATE_IDEMPOTENCY.release(_idem_key(idempotency_key))

    def _finish_idempotency(self, idempotency_key: Optional[str], rule: RuleOut) -> None:
        if idempotency_key:
            _CREATE_IDEMPOTENCY.finish(
                _idem_key(idempotency_key), rule.model_dump(by_alias=True)
            )


# ============================================================
# 模块级辅助
# ============================================================
#: `POST /rules` 的幂等回放缓存（键空间与 03 的事件编号完全隔离）
_CREATE_IDEMPOTENCY = IdempotencyStore()


def _idem_key(raw: str) -> str:
    return f"rules.create:{raw}"


def reset_create_idempotency() -> None:
    """复位幂等缓存（测试夹具使用）。"""
    _CREATE_IDEMPOTENCY.clear()
    _CREATE_IDEMPOTENCY.stats = {
        "hits": 0, "conflicts": 0, "waits": 0, "evicted": 0, "expired": 0,
    }


def _keyword_clauses(keyword: str) -> list[dict]:
    """关键词匹配 `_id` 或 `name`（Spec §3.1 / §2.2.2 的 `rSearch`）。

    始终 `re.escape`：关键词里出现正则元字符（`.` `(` `[a-z]+`）时，若被当作
    正则执行，轻则匹配到不相关的规则，重则让一次搜索变成全表扫描。
    `$options: "i"` 让用户输入小写场景码也能搜到 `RLOGIN001`。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    esc = re.escape(kw)
    return [
        {"_id": {"$regex": esc, "$options": "i"}},
        {"name": {"$regex": esc, "$options": "i"}},
    ]


# ============================================================
# 批量导入的解析与行校验（Spec §3.1 / BR-06-28 的规则侧对应）
# ============================================================
def _decode_import_csv(content: bytes) -> str:
    """按 `utf-8-sig` → `utf-8` → `gbk` 顺序试解码（与 06-A 名单导入同一份顺序）。

    顺序有讲究：`utf-8-sig` 必须排在最前，否则 Excel「另存为 UTF-8 CSV」写入的
    BOM 会成为第一个列名的一部分（`\\ufeffrule_code`），表头比对随即失败——
    症状是"我用官方模板导的却说表头不匹配"，用户完全无从下手。
    """
    for enc in IMPORT_ENCODINGS:
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("文件编码无法识别，请另存为 UTF-8 或 GBK 编码的 CSV")


def _parse_rule_import_csv(content: bytes) -> list[tuple[int, dict[str, str]]]:
    """解析导入 CSV，返回 `[(文件行号, {列名: 去空白后的值})]`（不含表头）。

    只做"文件级"判定（空文件 / 表头不匹配 / 超行数），行的字段合法性留给逐行校验：
    两者混在一起会让 `atomic` 无法区分"文件本身就不对"（整批 400）与"其中 3 行
    条件树非法"（整批不写但仍返回 200 + 错误明细）。
    """
    if not content or not content.strip():
        raise ImportFormatError("文件为空，请使用下载的模板填写后导入")

    text = _decode_import_csv(content)
    rows = list(csv.reader(io.StringIO(text, newline="")))
    # 去掉完全空白的行（Excel 常在末尾留空行，不该算作"数据行"）
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        raise ImportFormatError("文件为空，请使用下载的模板填写后导入")

    header = [(c or "").strip().lstrip("\ufeff").lower() for c in rows[0]]
    if header != list(RULE_IMPORT_HEADER):
        # 严格比对列名与顺序：模板由 `/rules/import-template` 提供、与这里共用
        # 同一个常量，因此合法用户**不可能**遇到这条错误。
        # 放松为"按列名取用"会引入更坏的问题：用户从别处抄来的表头看着像但不完全
        # 一致（`code` 写成 `rule_id`），按列名取用会静默丢列，导入看似成功实则
        # 缺字段——静默错数据比明确报错危险得多。
        raise ImportFormatError(
            f"表头不匹配，期望：{','.join(RULE_IMPORT_HEADER)}；实际：{','.join(header)}"
        )

    out: list[tuple[int, dict[str, str]]] = []
    for index, cells in enumerate(rows[1:], start=2):  # 行号从 2 起：1 是表头
        out.append((index, {
            name: ((cells[i] if i < len(cells) else "") or "").strip()
            for i, name in enumerate(RULE_IMPORT_HEADER)
        }))
    return out


def _build_rule_row(
    raw: dict[str, str], known_scenes: dict[str, str]
) -> tuple[Optional[RuleCreate], Optional[str], str]:
    """把一行 CSV 变成 `RuleCreate`，返回 `(payload, 失败原因, 规则编码)`。

    复用 `RuleCreate` + 服务层的**同一批函数**（`_check_score` / `validate_condition`）
    而不是自己写一套行级校验：分值范围、条件树结构各自只有一处实现，两套必然漂移
    ——典型症状：页面新增会校验条件树，导入却把一棵非法树写进库，于是
    "保存时通过、决策时求值失败"。

    ## 为什么这里要**提前**把场景/分值/条件树都查一遍

    `atomic` 模式的语义是"任一行校验不过则整批不写"，而它靠的正是"先全量校验、
    再逐行落库"。若把这三项只留在落库阶段，`atomic` 就会在"第 1 行已写、第 3 行
    才报错"时留下半批数据——那不是 atomic，只是"晚一点失败"。
    提前查用的仍是同一个 `validate_tree()`（05 提供）与同一个 `_check_score`，
    因此这不是第二份校验逻辑，只是**同一个校验被调用得更早**。
    """
    code = raw.get("rule_code", "") or ""
    if code and not RULE_CODE_PATTERN.match(code):
        # BR-06-01 的格式（`R{...}{3位序号}`）。**只查格式、不强制与 scene 前缀一致**：
        # PUT 允许改场景而编码不可改（BR-06-02），二者本来就可能不一致。
        return None, f"rule_code 格式非法（应为 R 开头、3 位序号结尾），收到：{code}", code

    scene_code = (raw.get("scene_code", "") or "").strip()
    if scene_code and scene_code not in known_scenes:
        return None, (
            f"归属场景 {scene_code!r} 不存在；可选场景："
            f"{'/'.join(sorted(known_scenes)) or '（字典为空，请先灌种子）'}"
        ), code

    condition_raw = raw.get("condition", "") or ""
    if not condition_raw:
        return None, "condition 不能为空（请填条件树的 JSON）", code
    try:
        condition = json.loads(condition_raw)
    except ValueError:
        return None, "condition 不是合法 JSON（请填条件树 JSON 文本）", code

    score_raw = (raw.get("score", "") or "").strip()
    if score_raw:
        try:
            _check_score(int(score_raw))
        except ValueError:
            return None, f"score 必须是整数，收到：{score_raw}", code
        except AppError as e:
            return None, e.message, code

    priority_raw = raw.get("priority", "") or ""
    try:
        priority = int(priority_raw) if priority_raw else 10
    except ValueError:
        return None, f"priority 必须是整数，收到：{priority_raw}", code

    data: dict[str, Any] = {
        "name": raw.get("name", ""),
        "scene_code": scene_code,
        "description": raw.get("description", "") or None,
        "condition": condition,
        "score": score_raw,
        "priority": priority,
        "status": raw.get("status", "") or "disabled",
    }
    try:
        payload = RuleCreate(**data)
    except ValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ())) or "行"
        return None, f"{field}：{first.get('msg', '非法取值')}", code

    # 条件树结构交给 05 判定（BR-06-17：唯一入口，本函数不做任何结构判断）
    try:
        validate_condition(payload.condition)
    except AppError as e:
        return None, e.message, code
    return payload, None, code


def _build_import_result(
    total: int, success: int, failures: list[RuleImportFailRow], mode: str,
    imported_ids: list[str], *, truncated: bool,
) -> RuleImportResult:
    """组装导入响应。`failed` 用 `total - success` 而不是 `len(failures)`：
    错误明细会被截断到 200 条，但"失败了多少行"这个数字不能被截断带偏。"""
    return RuleImportResult(
        total=total,
        success=success,
        failed=total - success,
        mode=mode,
        rows=failures[:RULE_IMPORT_ERROR_LIMIT],
        imported_ids=imported_ids,
        rows_truncated=truncated,
    )


__all__ = [
    "RuleService", "RULE_CACHE_FLUSH", "invalidate_rule_cache", "validate_condition",
    "check_condition", "ensure_no_immutable_fields", "reset_create_idempotency",
    "IMPACT_WINDOW_DAYS",
]
