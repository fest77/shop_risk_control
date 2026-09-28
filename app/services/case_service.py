# -*- coding: utf-8 -*-
"""案件服务：建案 / 认领 / 归档 / 超时回收 + 交付给 07 的处置视图（模块 08 §4.1/4.2/4.7）。

处置的**副作用编排**在 `disposal_service.py`；本文件只负责案件这条主线。

## 建案为什么由 05 的落库路径触发，而不是 03

决策 **D5** 明确：降级（`RUL-5001/5002`）时 05 **仍写一条 `decisions`**
（`decision=review`、`degraded=true`），08 据此**正常建案**；而 03 在
`stage="feature"` 的快照不完整短路上**不写 decisions**（已知缺口 N-03-3，
由 03 负责）。因此建案的触发点只能挂在"**决策落库成功**"这一处
（`app/engine/decision.py::_persist_with_retry`），挂在事件接入侧就会漏掉
降级那条路——而"漏掉降级请求"正是 D5 要防的事。

## 建案幂等的两道闸（BR-08-02）

1. **应用层**：先按 `event_id` 查一次（快路径，绝大多数重复走这里就返回了）；
2. **数据库层**：`risk_cases.event_id` 上的**唯一索引**。并发/重试下"先查后插"
   必然漏判，第二路插入会被数据库拦下（`DuplicateKeyError`），服务层回读并
   返回**既有案件**——而不是报错。

## 建案的审计为什么是 `strict=False`

D41 要求"每次成功的写操作恰好一条审计"，建案也是写操作，所以**要写**
`case.create`。但它**不能**是 strict：strict 的语义是"审计写不进去就回滚这次写"，
而这里回滚掉一个案件 = **把这个不确定的请求丢掉**，与 D5 的 fail-closed
（"不确定就交给人工"）直接冲突。因此建案审计失败只告警 + 置降级标记，
案件照常存在，`degraded` 与维护任务的补建案扫描兜住它。
处置/认领/归档那三条**用户发起的**写路径则严格执行 D41（见文件内各方法）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app import config, db as db_module
from app.constants import (
    CASE_ARCHIVE_AFTER_DAYS,
    CASE_DISPOSE_LOCK_STALE_MS,
    COLL_DECISIONS,
)
from app.core.degraded import DEGRADED
from app.engine import case_state
from app.enums import label_of, ActionType, CaseStatus, Decision, RiskLevel
from app.errors import (
    AppError,
    AuditWriteFailedError,
    CaseNotFoundError,
    CaseNotAssigneeError,
    CaseStateConflictError,
    CaseStateWriteFailedError,
)
from app.logging import get_logger
from app.repos.case_action_repo import CaseActionRepo
from app.repos.case_repo import CaseRepo
from app.repos.decision_repo import DecisionRepo
from app.repos.event_repo import EventRepo
from app.repos.profile_repo import ProfileRepo
from app.services.audit_service import audit
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.case")

#: 会建案的决策结论（BR-08-01：`pass` 不建案）
CASE_DECISIONS: tuple[str, ...] = (Decision.REVIEW.value, Decision.REJECT.value)

#: 建案时把画像上的风险标签**快照**进案件。上限与 E10 的标签数一致（8 种，
#: `enums.RiskTag`），这里不做去重/校验——标签的合法性由 09 负责（BR-09-03）。
TAG_SNAPSHOT_MAX = 8

_MSG_CASE_READ_UNAVAILABLE = "案件数据暂时不可用，请稍后重试"


class CaseService:
    """案件主线的全部业务不变量（不依赖 FastAPI，可注入假仓储单测）。"""

    def __init__(
        self,
        case_repo: Optional[CaseRepo] = None,
        action_repo: Optional[CaseActionRepo] = None,
        decision_repo: Optional[DecisionRepo] = None,
        event_repo: Optional[EventRepo] = None,
        profile_repo: Optional[ProfileRepo] = None,
    ) -> None:
        # `None` = "每次从当前数据库取"（测试会切换库，缓存句柄会写错库）
        self._cases = case_repo
        self._actions = action_repo
        self._decisions = decision_repo
        self._events = event_repo
        self._profiles = profile_repo

    # ---------------- 依赖装配 ----------------
    def _db(self) -> Any:
        return db_module.get_db()

    def cases(self) -> CaseRepo:
        return self._cases if self._cases is not None else CaseRepo(self._db())

    def actions(self) -> CaseActionRepo:
        return self._actions if self._actions is not None else CaseActionRepo(self._db())

    def decisions(self) -> DecisionRepo:
        return self._decisions if self._decisions is not None else DecisionRepo(self._db())

    def events(self) -> EventRepo:
        return self._events if self._events is not None else EventRepo(self._db())

    def profiles(self) -> ProfileRepo:
        return self._profiles if self._profiles is not None else ProfileRepo(self._db())

    def configure(
        self,
        *,
        case_repo: Optional[CaseRepo] = None,
        action_repo: Optional[CaseActionRepo] = None,
        decision_repo: Optional[DecisionRepo] = None,
        event_repo: Optional[EventRepo] = None,
        profile_repo: Optional[ProfileRepo] = None,
    ) -> None:
        """替换依赖（测试注入用）。"""
        if case_repo is not None:
            self._cases = case_repo
        if action_repo is not None:
            self._actions = action_repo
        if decision_repo is not None:
            self._decisions = decision_repo
        if event_repo is not None:
            self._events = event_repo
        if profile_repo is not None:
            self._profiles = profile_repo

    def reset_dependencies(self) -> None:
        """还原成"每次从当前数据库取"（测试收尾用，理由同 `ProfileService`）。"""
        self._cases = None
        self._actions = None
        self._decisions = None
        self._events = None
        self._profiles = None

    # ============================================================
    # 建案（BR-08-01 ~ 03，D5）
    # ============================================================
    async def create_from_decision(
        self, decision_id: str, *, decision_doc: Optional[dict] = None
    ) -> Optional[str]:
        """由一条决策建案（05 → 08 的冻结契约，Spec §3.2）。返回案件编号。

        - 决策不是 `review`/`reject` → 返回 `None`（`pass` 不建案，BR-08-01）；
        - 同一 `event_id` 已有案件 → 返回既有编号（BR-08-02 幂等）；
        - 建案失败 → 返回 `None` 且**不抛异常**：调用方是 05 的**后台落库任务**，
          抛异常会把"决策已经算出来了"这件事变成一次落库失败
          （`_persist_with_retry` 的重试会连决策一起重试，而决策其实已经写好了）。
          漏掉的案件由维护任务的补建案扫描兜住（`recover_missing_cases`）。

        `decision_doc` 可选：05 的落库任务手里已经有这份文档，传进来可以省一次
        往返（且避免"刚写完还没读到"的窗口）。不传则按 `decision_id` 回读。
        """
        decision = decision_doc
        if decision is None:
            try:
                decision = await self.decisions().find_decision(str(decision_id))
            except PyMongoError as e:
                log.warning("建案读取决策失败 decision_id=%s：%s", decision_id, e)
                return None
        if not decision:
            log.warning("建案找不到决策 decision_id=%s（不建案）", decision_id)
            return None

        verdict = str(decision.get("decision") or "")
        if verdict not in CASE_DECISIONS:
            return None

        event_id = str(decision.get("event_id") or "")
        if not event_id:
            log.warning("建案缺少 event_id decision_id=%s（不建案）", decision_id)
            return None

        repo = self.cases()
        try:
            existing = await repo.find_by_event(event_id)
            if existing is None:
                existing = await repo.find_by_decision(str(decision_id))
        except PyMongoError as e:
            log.warning("建案幂等查询失败 event_id=%s：%s", event_id, e)
            return None
        if existing is not None:
            return str(existing["_id"])

        event: Optional[dict] = None
        try:
            event = await self.events().find_by_id(event_id)
        except PyMongoError as e:
            # E01 读不到不是不建案的理由：案件是"事后长期保留"的对象，而 E01 有
            # 90 天 TTL；缺的只是 `estimated_loss` 与 `device_id` 两个冗余快照
            log.warning("建案读取事件失败 event_id=%s（按缺省值继续）：%s", event_id, e)

        user_id = str(decision.get("user_id") or (event or {}).get("user_id") or "")
        if not user_id:
            log.warning("建案缺少 user_id event_id=%s（不建案）", event_id)
            return None

        risk_tags = await self._snapshot_tags(user_id)
        amount = _amount_of(event)
        created_at = now_ms()
        doc = {
            "event_id": event_id,
            "decision_id": str(decision_id),
            "user_id": user_id,
            "scene_code": str(decision.get("scene_code") or ""),
            "risk_score": int(decision.get("final_score") or 0),
            "risk_level": str(decision.get("risk_level") or RiskLevel.MEDIUM.value),
            "decision": verdict,
            # 决策 D5：降级也是 review，这一位让审核员知道"这条是算不出来转来的"
            "degraded": bool(decision.get("degraded")),
            "degrade_code": decision.get("degrade_code"),
            "risk_tags": risk_tags,
            "status": CaseStatus.PENDING.value,
            "assignee": None,
            "claimed_at": None,
            "claim_deadline_at": None,
            "disposed_at": None,
            "archived_at": None,
            # BR-08-03：取事件金额（无金额则 0），供 11 的资损口径使用（G-09）
            "estimated_loss": amount,
            "audit_pending": False,
            "biz_sync_pending": False,
            "created_at": created_at,
        }
        try:
            case_no = await repo.new_id(created_at)
        except PyMongoError as e:
            log.warning("建案取号失败 event_id=%s：%s", event_id, e)
            return None
        doc["_id"] = case_no
        try:
            await repo.insert(doc)
        except DuplicateKeyError:
            # 唯一索引兜底：另一个进程/重试路径已经建好了这个事件的案件
            again = await repo.find_by_event(event_id)
            if again is not None:
                return str(again["_id"])
            log.warning("建案撞唯一索引但回读不到 event_id=%s", event_id)
            return None
        except PyMongoError as e:
            log.warning("建案写入失败 event_id=%s：%s（补建案扫描会重试）", event_id, e)
            return None

        await self._audit_case_created(doc)
        return case_no

    async def _snapshot_tags(self, user_id: str) -> list[str]:
        """建案时把 E10 画像上的风险标签**快照**进案件（E08.`risk_tags`）。

        ## 为什么快照画像标签而不是"留空"

        E08 有 `risk_tags` 字段、07 的列表也要按标签筛（BR-07-02 明确
        "案件上的字段是**冗余快照**，不用当前数据重算"）。而决策块（E03）里
        没有标签——标签的真源是 09（BR-09-03）。因此建案时取一次画像标签存下来：
        之后 09 的标签怎么变，这条案件记录的都是"建案那一刻"的画像。
        这与 `risk_score` / `risk_level` 的快照口径完全一致。

        读失败返回 `[]`（**不是**编造，也不阻断建案）：标签是附加信息，
        而案件本身是"必须有人处理"的对象。
        """
        try:
            user = await self.profiles().get_user(str(user_id))
        except AppError as e:
            log.warning("[GRP-5001] 建案快照画像标签失败 user_id=%s：%s", user_id, e)
            return []
        except PyMongoError as e:
            log.warning("建案快照画像标签失败 user_id=%s：%s", user_id, e)
            return []
        tags = (user or {}).get("risk_tags")
        if not isinstance(tags, (list, tuple)):
            return []
        return [str(t) for t in tags][:TAG_SNAPSHOT_MAX]

    async def _audit_case_created(self, doc: dict) -> None:
        """建案留痕（`case.create`，`strict=False`，理由见模块 docstring）。"""
        try:
            await audit(
                actor="system", actor_role="system", action="case.create",
                target_type="case", target_id=str(doc.get("_id")),
                before=None,
                after={
                    "case_no": doc.get("_id"), "event_id": doc.get("event_id"),
                    "decision_id": doc.get("decision_id"), "user_id": doc.get("user_id"),
                    "decision": doc.get("decision"), "risk_score": doc.get("risk_score"),
                    "risk_level": doc.get("risk_level"), "degraded": doc.get("degraded"),
                    "estimated_loss": doc.get("estimated_loss"),
                },
                strict=False,
            )
        except Exception as e:  # noqa: BLE001 - 见模块 docstring：绝不因此丢掉案件
            DEGRADED.mark(f"案件建案审计写入失败：{e}")
            log.error("[AUD-5002] case.create 审计写入失败 case_no=%s：%s",
                      doc.get("_id"), e)

    async def recover_missing_cases(self, *, limit: int = 200) -> list[str]:
        """补建案扫描：把"应当建案却还没建"的决策补上（D5 的兜底）。

        每轮只看**最近 `limit` 条** `review`/`reject` 决策：一轮的扫描量必须有界
        （维护任务每 60s 跑一次），而"很久以前的决策还没建案"只可能来自
        "连续多轮都失败"，那种情况下日志里已经有连续告警可查。
        """
        db = self._db()
        try:
            cursor = db[COLL_DECISIONS].find(
                {"decision": {"$in": list(CASE_DECISIONS)}}, {"_id": 1}
            ).sort([("decided_at", -1)]).limit(limit)
            rows = await cursor.to_list(length=limit)
        except PyMongoError as e:
            log.warning("补建案扫描读取决策失败：%s", e)
            return []
        ids = [str(r["_id"]) for r in rows]
        try:
            missing = await self.cases().find_decisions_without_case(ids)
        except PyMongoError as e:
            log.warning("补建案扫描比对案件失败：%s", e)
            return []
        created: list[str] = []
        for decision_id in missing:
            case_no = await self.create_from_decision(decision_id)
            if case_no:
                created.append(case_no)
        if created:
            log.info("补建案扫描：为 %d 条决策补建了案件 %s", len(created), created)
        return created

    # ============================================================
    # 认领（BR-08-04 / 05，D41 的严格审计）
    # ============================================================
    async def claim(
        self, case_no: str, operator: str, *,
        actor_role: str = "", ip: Optional[str] = None, ua: Optional[str] = None,
    ) -> dict:
        """认领案件（原子）。同一 `assignee` 重复认领**幂等**（BR-08-04 的幂等条款）。

        幂等分支不刷新 `claimed_at`、**不写审计**：状态没有发生任何变化，
        没有"写"发生，写一条审计会与"每次成功的**写**操作恰好一条"矛盾，
        也会让审核员误以为超时计时被重置了。
        """
        case = await self.load(case_no)
        status = str(case.get("status") or "")
        assignee = case.get("assignee")
        if status == CaseStatus.REVIEWING.value:
            if str(assignee or "") == str(operator):
                payload = self.claim_payload(case)
                payload["changed"] = False
                return payload
            # 已被**他人**认领 → `DSP-4005`（"案件由 reviewer02 认领，请先由本人处置
            # 或等待超时回收"），**不是** `DSP-4003`（"状态不允许认领"）。
            # `DSP-4003` 在这句话上是假的：状态完全允许认领，只是有人先到了；
            # 两者给审核员的下一步指引完全不同（换个人/等回收 vs 刷新看看）。
            raise CaseNotAssigneeError(case_no, str(assignee or ""), str(operator))

        case_state.assert_can_perform("claim", status)

        claimed_at = now_ms()
        deadline = self.claim_deadline(claimed_at)
        try:
            claimed = await self.cases().claim(case_no, operator, claimed_at, deadline)
        except PyMongoError as e:
            raise CaseStateWriteFailedError(case_no, f"{type(e).__name__}: {e}") from e
        if claimed is None:
            # 条件更新匹配 0 条：读到它之后状态被别处改过。
            # **必须回读一次**才能给出正确的错误码（已被别人认领 vs 已处置）
            try:
                await self._raise_claim_conflict(case_no, operator)
            except _IdempotentClaim as signal:
                # 极端并发下的**同人**重复认领：语义是幂等成功（见该异常类说明）
                return signal.payload

        try:
            await audit(
                actor=operator, actor_role=actor_role, action="case.claim",
                target_type="case", target_id=case_no,
                before={"status": status, "assignee": assignee},
                after={"status": claimed.get("status"), "assignee": claimed.get("assignee"),
                       "claimed_at": claimed.get("claimed_at"),
                       "claim_deadline_at": claimed.get("claim_deadline_at")},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = await self._rollback_claim(case_no, operator, claimed_at)
            log.error("case.claim 审计写入失败，已回滚认领 case_no=%s：%s", case_no, e)
            raise AuditWriteFailedError(
                "case.claim",
                f"{e.code}；"
                + ("已回滚该次认领（案件保持 pending）" if rolled
                   else "且回滚亦失败，案件可能仍为 reviewing，请人工核对"),
            ) from e

        payload = self.claim_payload(claimed)
        payload["changed"] = True
        return payload

    async def _raise_claim_conflict(self, case_no: str, operator: str) -> None:
        """认领条件更新匹配 0 条：区分三种原因（D42：冲突必须给人话提示）。"""
        fresh = await self.cases().find_by_id(case_no)
        if fresh is None:
            raise CaseNotFoundError(case_no)
        status = str(fresh.get("status") or "")
        assignee = str(fresh.get("assignee") or "")
        if status == CaseStatus.REVIEWING.value and assignee and assignee != str(operator):
            # 已被他人认领（V-08-03：并发 10 次认领恰好 1 次成功，其余都是它）
            raise CaseNotAssigneeError(case_no, assignee, str(operator))
        if status == CaseStatus.REVIEWING.value and assignee == str(operator):
            # 极端并发：两个同人请求同时通过条件更新，后者匹配 0 条。语义上是幂等成功
            payload = self.claim_payload(fresh)
            payload["changed"] = False
            raise _IdempotentClaim(payload)
        raise CaseStateConflictError(
            "认领", status or "未知",
            "案件已不在待认领状态，请刷新后重试",
        )

    async def _rollback_claim(self, case_no: str, operator: str, claimed_at: int) -> bool:
        """认领审计失败后的补偿（D41）。返回是否回滚成功。"""
        try:
            restored = await self.cases().rollback_claim(case_no, operator, claimed_at)
        except PyMongoError as e:
            log.error("回滚认领失败 case_no=%s：%s", case_no, e)
            return False
        if restored == 0:
            log.error("回滚认领匹配 0 条（状态已被他人改变）case_no=%s", case_no)
            return False
        return True

    # ============================================================
    # 归档（BR-08-36 / 38，BR-08-09：状态迁移必须留痕）
    # ============================================================
    async def archive(
        self, case_no: str, operator: str, *, remark: str = "",
        actor_role: str = "", ip: Optional[str] = None, ua: Optional[str] = None,
    ) -> dict:
        """归档（仅 `disposed` 可归档，BR-08-08 / 36）。审计 `case.archive` 严格。

        权限（`admin`）由接口层的 `require_permission` 把关，本方法只管状态机
        与留痕：**"谁能不能做"与"现在能不能做"是两件事**，混在一处会让
        权限矩阵的单一真源失效（D26/D28）。
        """
        case = await self.load(case_no)
        status = str(case.get("status") or "")
        case_state.assert_can_perform("archive", status)

        archived_at = now_ms()
        try:
            matched = await self.cases().mark_archived(case_no, archived_at, by=operator)
        except PyMongoError as e:
            raise CaseStateWriteFailedError(case_no, f"{type(e).__name__}: {e}") from e
        if matched == 0:
            fresh = await self.cases().find_by_id(case_no)
            if fresh is None:
                raise CaseNotFoundError(case_no)
            raise CaseStateConflictError(
                "归档", str(fresh.get("status") or ""), "案件状态已被他人改变，请刷新后重试"
            )

        try:
            await audit(
                actor=operator, actor_role=actor_role, action="case.archive",
                target_type="case", target_id=case_no,
                before={"status": status, "disposed_at": case.get("disposed_at")},
                after={"status": CaseStatus.ARCHIVED.value, "archived_at": archived_at,
                       "archived_by": operator, "remark": remark},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = await self._rollback_archive(case_no, archived_at)
            log.error("case.archive 审计写入失败，已回滚归档 case_no=%s：%s", case_no, e)
            raise AuditWriteFailedError(
                "case.archive",
                f"{e.code}；"
                + ("已回滚该次归档（案件保持 disposed）" if rolled
                   else "且回滚亦失败，案件可能仍为 archived，请人工核对"),
            ) from e

        return {
            "case_no": case_no,
            "status": CaseStatus.ARCHIVED.value,
            "archived_at": archived_at,
            "archived_by": operator,
            "remark": remark,
        }

    async def _rollback_archive(self, case_no: str, archived_at: int) -> bool:
        try:
            restored = await self.cases().rollback_archive(case_no, archived_at)
        except PyMongoError as e:
            log.error("回滚归档失败 case_no=%s：%s", case_no, e)
            return False
        if restored == 0:
            log.error("回滚归档匹配 0 条（归档时刻已被他人改变）case_no=%s", case_no)
            return False
        return True

    # ============================================================
    # 后台：超时回收（BR-08-10 / 11）与自动归档（BR-08-38）
    # ============================================================
    async def recycle_timeouts(self, *, now: Optional[int] = None,
                               limit: int = 200) -> list[str]:
        """把认领超时的案件回收为 `pending`（BR-08-10）。

        BR-08-11 的"**不写 `case_actions`**"是刻意的：E09 的 `action_type`
        枚举里没有"回收"这个值（Spec §8「G-06 衍生」建议增加 `case_recycle`），
        凭空塞一个枚举外的值会让 07 的流水渲染与 E09 的字典同时失真。
        因此这里只写 `audit_logs`（`case.recycle`）。

        审计用 `strict=False`：本方法由后台任务调用，**没有调用方可以接收错误**，
        用 strict 只会让"审计瞬时不可用"变成"案件卡在某个人名下"——
        而回收的目的恰恰是别让案件卡住。失败置降级标记 + 告警，下一轮重试。
        """
        moment = now if now is not None else now_ms()
        try:
            rows = await self.cases().find_claim_timeouts(moment, limit)
        except PyMongoError as e:
            log.warning("[DSP-5001] 超时案件扫描失败，下一轮重试：%s", e)
            return []

        recycled: list[str] = []
        for row in rows:
            case_no = str(row.get("_id"))
            try:
                before = await self.cases().recycle(case_no, moment)
            except PyMongoError as e:
                log.warning("[DSP-5001] 回收案件失败 case_no=%s：%s", case_no, e)
                continue
            if before is None:
                # 条件更新匹配 0 条：期间被处置/已被回收，如实跳过（不是错误）
                continue
            recycled.append(case_no)
            try:
                await audit(
                    actor="system", actor_role="system", action="case.recycle",
                    target_type="case", target_id=case_no,
                    before={"status": before.get("status"), "assignee": before.get("assignee"),
                            "claimed_at": before.get("claimed_at")},
                    after={"status": CaseStatus.PENDING.value, "assignee": None,
                           "recycled_at": moment},
                    strict=False,
                )
            except Exception as e:  # noqa: BLE001 - 见 docstring：不阻断回收
                DEGRADED.mark(f"case.recycle 审计写入失败：{e}")
                log.error("[AUD-5002] case.recycle 审计写入失败 case_no=%s：%s", case_no, e)
        if recycled:
            log.info("超时回收：%d 个案件回到待审队列 %s", len(recycled), recycled)
        return recycled

    async def auto_archive(self, *, now: Optional[int] = None,
                           limit: int = 200) -> list[str]:
        """自动归档 `disposed` 超过 `CASE_ARCHIVE_AFTER_DAYS` 天的案件（BR-08-38）。

        写 `archived_at` + 审计 `case.archive`（actor=system）——BR-08-09 要求
        `→ archived` 必须留痕，且这条留痕不能因为"是定时任务做的"就省掉。
        """
        moment = now if now is not None else now_ms()
        deadline = moment - CASE_ARCHIVE_AFTER_DAYS * 86_400_000
        try:
            rows = await self.cases().find_archivable(deadline, limit)
        except PyMongoError as e:
            log.warning("[DSP-5001] 待归档案件扫描失败，下一轮重试：%s", e)
            return []

        archived: list[str] = []
        for row in rows:
            case_no = str(row.get("_id"))
            try:
                matched = await self.cases().mark_archived(case_no, moment, by="system")
            except PyMongoError as e:
                log.warning("[DSP-5001] 自动归档失败 case_no=%s：%s", case_no, e)
                continue
            if matched == 0:
                continue
            archived.append(case_no)
            try:
                await audit(
                    actor="system", actor_role="system", action="case.archive",
                    target_type="case", target_id=case_no,
                    before={"status": CaseStatus.DISPOSED.value,
                            "disposed_at": row.get("disposed_at")},
                    after={"status": CaseStatus.ARCHIVED.value, "archived_at": moment,
                           "archived_by": "system",
                           "reason": f"disposed 后 {CASE_ARCHIVE_AFTER_DAYS} 天自动归档（BR-08-38）"},
                    strict=False,
                )
            except Exception as e:  # noqa: BLE001 - 后台任务不得因此中断
                DEGRADED.mark(f"自动归档审计写入失败：{e}")
                log.error("[AUD-5002] 自动归档审计写入失败 case_no=%s：%s", case_no, e)
        if archived:
            log.info("自动归档：%d 个案件（disposed 超过 %d 天）%s",
                     len(archived), CASE_ARCHIVE_AFTER_DAYS, archived)
        return archived

    # ============================================================
    # 交付给 07 的读契约（Spec §3.2 冻结）
    # ============================================================
    async def get_disposal_view(self, case_no: str) -> dict:
        """`{case_no, status, conclusion, action_types, remark, claim_deadline_at, latest_action}`。

        07 的右栏靠它渲染判定/处置区；`conclusion` / `action_types` / `remark`
        取自**最近一条** `case_actions`（没处置过就是空）。
        """
        case = await self.load(case_no)
        items = await self.actions().list_by_case(case_no)
        latest = items[-1] if items else None
        return {
            "case_no": str(case.get("_id")),
            "status": str(case.get("status") or ""),
            "status_label": label_of(CaseStatus, str(case.get("status") or "")),
            "conclusion": (latest or {}).get("conclusion"),
            "action_types": list((latest or {}).get("action_types")
                                 or ([(latest or {}).get("action_type")]
                                     if (latest or {}).get("action_type") else [])),
            "remark": (latest or {}).get("remark"),
            "assignee": case.get("assignee"),
            "claim_deadline_at": case.get("claim_deadline_at"),
            "disposed_at": case.get("disposed_at"),
            "latest_action": _action_out(latest) if latest else None,
        }

    async def list_actions(self, case_no: str) -> dict:
        """处置流水（07 详情页复用，Spec §3.1 `GET /cases/{no}/actions`）。"""
        await self.load(case_no)
        rows = await self.actions().list_by_case(case_no)
        return {"case_no": case_no, "items": [_action_out(r) for r in rows],
                "total": len(rows)}

    # ============================================================
    # 内部
    # ============================================================
    async def load(self, case_no: str) -> dict:
        """取案件；查不到抛 `DSP-4040`。

        读失败（Mongo 不可用）与"查不到"必须分开：把依赖故障报成 404 会让审核员
        得出"这个案子不存在"的结论——与 09 的 `GRP-5001` vs `GRP-4004` 同一条原则。
        """
        try:
            case = await self.cases().find_by_id(str(case_no))
        except PyMongoError as e:
            raise AppError("COM-5001", _MSG_CASE_READ_UNAVAILABLE, 503) from e
        if case is None:
            raise CaseNotFoundError(case_no)
        return case

    @staticmethod
    def claim_deadline(claimed_at: int, *, timeout_min: Optional[int] = None) -> Optional[int]:
        """`claim_deadline_at = claimed_at + case_claim_timeout_min × 60s`（BR-08-05）。

        `case_claim_timeout_min = 0` 表示**关闭超时回收**（BR-08-12），此时返回
        `None`——`claim_deadline_at=null` 的语义就是"永不超时"，而不是"立刻超时"。
        Mongo 比较时 `null` 小于任何数字，因此回收扫描必须显式排除 `null`
        （见 `case_repo.find_claim_timeouts`）。
        """
        minutes = config.CASE_CLAIM_TIMEOUT_MIN if timeout_min is None else int(timeout_min)
        if int(minutes) <= 0:
            return None
        return int(claimed_at) + int(minutes) * 60_000

    @staticmethod
    def claim_payload(case: dict) -> dict:
        """认领响应（Spec §3.1：`{case_no, status, assignee, claimed_at, claim_deadline_at}`）。"""
        return {
            "case_no": str(case.get("_id")),
            "status": str(case.get("status") or ""),
            "assignee": case.get("assignee"),
            "claimed_at": case.get("claimed_at"),
            "claim_deadline_at": case.get("claim_deadline_at"),
        }

    @staticmethod
    def disposal_lock_stale_before(moment: int) -> int:
        """内部处置锁的过期时刻（见 `constants.CASE_DISPOSE_LOCK_STALE_MS`）。"""
        return int(moment) - CASE_DISPOSE_LOCK_STALE_MS


class _IdempotentClaim(Exception):
    """内部信号：并发下的**同人重复认领**应视为幂等成功，而不是冲突。

    它只在 `ClaimService.claim` 内部被抛出并**立即**就地捕获（见 `claim` 的
    调用处），不跨越任何调用边界——用异常是因为此时已经进入"条件更新匹配 0 条"
    的错误分支，正常返回需要穿透两层函数；用一个私有异常把结论带回去，
    比让内部方法返回"错误码 + 载荷"的联合类型清楚得多。
    """

    def __init__(self, payload: dict) -> None:
        super().__init__("idempotent claim")
        self.payload = payload


def _amount_of(event: Optional[dict]) -> int:
    """事件金额（E01.`amount`，单位分）。缺失/非法一律 0（BR-08-03）。"""
    if not event:
        return 0
    try:
        amount = int(event.get("amount") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, amount)


def _action_out(doc: Optional[dict]) -> Optional[dict]:
    """E09 文档 -> 对外结构（`_id` 对外叫 `action_id`，与 Spec §3.1 的响应字段一致）。"""
    if not doc:
        return None
    return {
        "action_id": doc.get("_id"),
        "case_no": doc.get("case_no"),
        "action_type": doc.get("action_type"),
        "action_label": label_of(ActionType, str(doc.get("action_type") or "")),
        "conclusion": doc.get("conclusion"),
        "remark": doc.get("remark"),
        "evidence_refs": list(doc.get("evidence_refs") or []),
        "list_writes": list(doc.get("list_writes") or []),
        "operator": doc.get("operator"),
        "operator_role": doc.get("operator_role"),
        "acted_at": doc.get("acted_at"),
        "biz_sync_result": doc.get("biz_sync_result"),
    }


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = CaseService()


def get_case_service() -> CaseService:
    return _SERVICE


def reset_service() -> None:
    """还原依赖装配（测试收尾用）。"""
    _SERVICE.reset_dependencies()


__all__ = [
    "CASE_DECISIONS",
    "CaseService",
    "get_case_service",
    "reset_service",
]
