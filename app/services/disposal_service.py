# -*- coding: utf-8 -*-
"""处置编排：一次处置的全部副作用与它们的失败语义（模块 08 §4.6，BR-08-29 ~ 35）。

## 执行顺序是**固定**的（BR-08-29），且**不可并行**（BR-08-34）

    ① 校验令牌 + 原子锁定案件（reviewing + 内部锁）
    ② 写名单库          ← **紧操作**：失败 → 整体回滚 + `DSP-5002`
    ③ 调 BizAdapter     ← 旁路：失败 → 保留处置 + 标记 + 重试队列
    ④ 写 case_actions   ← 紧操作：失败 → 回滚名单 + `DSP-5005`
    ⑤ 更新案件 disposed ← 紧操作：失败 → 回滚名单与流水 + `DSP-5001`
    ⑥ 写审计哈希链       ← 旁路：失败 → 保留处置 + `audit_pending` + `DSP-5004`
    ⑦ 发布 case.disposed ← 旁路：失败只记日志/降级

**为什么禁止 `asyncio.gather`**：并行之后"回滚本次已写的名单条目"就不再成立
——名单写入与案件状态更新同时进行时，任何一方失败都无法确定另一方做到哪一步。
单协程顺序执行的代价是耗时是各项之和（几十毫秒），换来的是**每一步的成败都可判定**。

## 紧操作 fail-closed 的完整语义（BR-08-30）

名单写入失败 → 回滚本次**新增**的名单条目（置 `status=removed`）、案件保持
`reviewing`、`disposed_at` 不写、不落成功态 `case_actions`，返回 `DSP-5002`。
**唯一例外**：BR-08-22 的"复用既有条目"路径**不回滚**——那条目在这次处置之前
就已经生效（是别人更早的判断），把它置 `removed` 等于撤销一个**不属于本次**
的风控结论，那比"处置失败"严重得多。响应里的 `rolled_back` 只反映新增条目的
回滚结果，复用的条目在 `list_writes[].reused=true` 上如实标出。

## 为什么处置的审计是"恰好一条"`case.dispose`

D41 要求"每次成功的写操作**必须且仅写一条**审计"，而一次处置是**一次操作**：
它的第 ② 步写了 N 条名单，但那些写入的留痕就在这**同一条** `case.dispose` 的
`after.list_writes` 里（含 `entry_id`）。再加上 `list_entries.related_case_no`
的反查与 `case_actions.list_writes`，追溯链完整，而审计不会被动作数放大。
（Spec 的 `V-08-12` 期望另有 `list.add` 记录，已在交付报告中登记为待裁定项——
改动只需在 `ListService.add_auto` 里加一行 `audit(...)`。）

## `preview` 为什么也在这里

`/dispose/preview` **不产生任何副作用**（BR-08-28），但它必须算出**与
`/dispose` 完全一致**的副作用清单与名单预览（BR-08-26：弹窗内容由服务端权威
生成，漏列副作用就是安全漏洞）。两者共用 `_plan_list_writes` / `build_side_effects`
与同一个令牌签发函数，因此"弹窗上写的"与"真正做的"在结构上不可能不一致。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from pymongo.errors import PyMongoError

from app import db as db_module
from app.core import confirm_token
from app.core.biz_sync_retry import enqueue_audit, enqueue_biz_sync, get_queue
from app.core.case_events import publish_case_disposed
from app.core.degraded import DEGRADED
from app.enums import ActionType, CaseStatus, Conclusion, RiskLevel, label_of
from app.errors import (
    AppError,
    CaseActionWriteFailedError,
    CaseAlreadyDisposedError,
    CaseNotFoundError,
    CaseNotAssigneeError,
    CaseStateConflictError,
    CaseStateWriteFailedError,
    DisposalParamInvalidError,
    ListLinkWriteFailedError,
    dsp_error,
)
from app.logging import get_logger, get_trace_id
from app.repos.audit_repo import AuditRepo
from app.repos.case_action_repo import CaseActionRepo
from app.repos.case_repo import CaseRepo
from app.schemas.case_schema import (
    ACTION_TYPES,
    CONCLUSIONS,
    CONFIRM_TTL_SEC,
    OVERRIDE_LIST_TYPES,
    REMARK_MAX,
    TIGHT_ACTIONS,
    DisposeIn,
    DisposePreviewIn,
    assert_compatible,
    build_side_effects,
    default_list_writes,
    entity_types,
    normalize_action_types,
)
from app.services.audit_service import audit
from app.services.case_service import CaseService, get_case_service
from app.services.idempotency import IdempotencyStore, payload_digest
from app.services.list_service import ListService
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.disposal")

#: 处置的幂等回放缓存（键空间与 03 的事件编号、06 的规则保存完全隔离）。
#: TTL 用 24h，与 D6 的业界通行窗口一致：处置的重试往往发生在网络抖动后的
#: 几秒到几分钟内，24h 足够覆盖"用户刷新页面重放"的全部现实场景。
_DISPOSE_IDEMPOTENCY = IdempotencyStore()


class DisposalService:
    """处置编排（不依赖 FastAPI，可注入假依赖单测）。"""

    def __init__(
        self,
        case_service: Optional[CaseService] = None,
        list_service: Optional[ListService] = None,
        adapter: Any = None,
    ) -> None:
        self._case_service = case_service
        self._list_service = list_service
        self._adapter = adapter

    # ---------------- 依赖装配 ----------------
    def cases(self) -> CaseService:
        return self._case_service if self._case_service is not None else get_case_service()

    def lists(self) -> ListService:
        if self._list_service is None:
            from app.repos.list_repo import ListRepo

            self._list_service = ListService(ListRepo(db_module.get_db()))
        return self._list_service

    def adapter(self) -> Any:
        """取业务适配器：**默认装的是 `MockBizAdapter`**（AD-08 / G-08）。

        从 `protocols.get_components()` 取而不是自己 new 一个：装配只有一处
        （`app/main.py` 的 lifespan / `configure_components`），换了真实实现
        本模块不需要改任何代码——这正是 AD-08 "可插拔"的含义。
        """
        if self._adapter is not None:
            return self._adapter
        from app.protocols import get_components

        return get_components().biz_adapter

    def configure(
        self,
        *,
        case_service: Optional[CaseService] = None,
        list_service: Optional[ListService] = None,
        adapter: Any = None,
    ) -> None:
        if case_service is not None:
            self._case_service = case_service
        if list_service is not None:
            self._list_service = list_service
        if adapter is not None:
            self._adapter = adapter

    def reset_dependencies(self) -> None:
        """还原默认装配（测试收尾用）。"""
        self._case_service = None
        self._list_service = None
        self._adapter = None

    # ============================================================
    # 参数校验（DSP-4001 / DSP-4002）
    # ============================================================
    @staticmethod
    def validate_conclusion_and_actions(
        conclusion: Any, action_types: Any
    ) -> tuple[str, list[str]]:
        """校验结论与动作并归一化，返回 `(conclusion, 去重排序后的 actions)`。

        与 `case_schema` 的分工：schema 提供**纯判定**（相容矩阵），
        这里负责"补齐错误码 + 决定顺序"。校验顺序是刻意的：
        **先"有没有"、再"合不合法"、最后"相不相容"**——只有一项都没填时，
        用户最需要看到的是"至少选一项动作"，而不是"violation 需要紧动作"。
        """
        verdict = str(conclusion or "").strip()
        if not verdict:
            raise DisposalParamInvalidError("处理结论必填（violation / normal / suspicious）")
        if verdict not in CONCLUSIONS:
            raise DisposalParamInvalidError(
                f"处理结论取值非法：{verdict}（仅支持 {'/'.join(CONCLUSIONS)}）"
            )
        actions = normalize_action_types(action_types)
        if not actions:
            raise DisposalParamInvalidError("请至少选择一项联动处置动作")
        unknown = [a for a in actions if a not in ACTION_TYPES]
        if unknown:
            raise DisposalParamInvalidError(
                f"不支持的处置动作：{'、'.join(unknown)}（仅支持 {'/'.join(ACTION_TYPES)}）"
            )
        assert_compatible(verdict, actions)
        return verdict, actions

    @staticmethod
    def validate_remark(remark: Any) -> str:
        """备注校验（BR-08-16）：**trim 后非空且 ≤500 字**；返回**原样**的值。

        "原样写入"是 Spec 的明文要求（`case_actions.remark` 与审计 `after`
        都写原值）：审核员写下的备注是**证据**，服务端不替他把空格删掉。
        校验用 trim 后的长度，因为"全是空格"必须被拒（否则等于没填）。
        """
        if remark is None:
            raise DisposalParamInvalidError("请填写处置原因备注")
        text = str(remark)
        if not text.strip():
            raise DisposalParamInvalidError("处置原因备注不能为空白")
        if len(text.strip()) > REMARK_MAX:
            raise DisposalParamInvalidError(
                f"处置原因备注最多 {REMARK_MAX} 字，当前 {len(text.strip())} 字"
            )
        return text

    # ============================================================
    # 预览（BR-08-26 / 27 / 28，**无副作用**）
    # ============================================================
    async def preview(
        self, case_no: str, payload: DisposePreviewIn, operator: str
    ) -> dict:
        """签发二次确认令牌 + 返回副作用清单（Spec §3.1）。

        **绝不写库、绝不发事件**（BR-08-28）：取消弹窗时不得留下任何痕迹。
        因此本方法里对名单/业务/审计的调用**一个都没有**——它只有读与计算。
        """
        case = await self.cases().load(case_no)
        verdict, actions = self.validate_conclusion_and_actions(
            payload.conclusion, payload.action_types
        )
        self._ensure_disposable(case, operator)

        event = await self._load_event(case)
        device_id = _device_of(event)
        planned, skipped = self._plan_list_writes(verdict, actions, case, device_id)
        writes_preview = [
            {"list_type": w["list_type"], "entity_type": w["entity_type"],
             "entity_value": w["entity_value"], "expire_at": w.get("expire_at")}
            for w in planned
        ]
        token = confirm_token.issue(
            case_no, verdict, actions, operator, ttl_sec=CONFIRM_TTL_SEC
        )
        return {
            "case_no": case_no,
            "summary": await self._summary(case, verdict, actions),
            "side_effects": build_side_effects(verdict, actions, writes_preview,
                                               skipped=skipped),
            "list_writes_preview": writes_preview,
            "list_writes_skipped": skipped,
            "confirm_token": token["token"],
            "expires_in": token["expires_in"],
            "expires_at": token["expires_at"],
            # 页面必须能说明"这次确认会发生什么"；`pass` 也要说清它只调业务系统
            "tight_actions": sorted(set(actions) & TIGHT_ACTIONS),
        }

    async def _summary(self, case: dict, conclusion: str, actions: list[str]) -> dict:
        """Spec §2.2 的 `ds1`~`ds5`（弹窗的每一行都由服务端算好，前端不拼）。"""
        user_id = str(case.get("user_id") or "")
        age_days: Optional[int] = None
        try:
            user = await self.cases().profiles().get_user(user_id)
        except (AppError, PyMongoError) as e:
            # 年龄只是弹窗上的一句附注，取不到显示「—」而不是 0（D46 同源：
            # 0 天是一个断言"今天刚注册"，而真相是"我们不知道"）
            log.warning("[GRP-5001] 处置预览读取画像失败 user_id=%s：%s", user_id, e)
            user = None
        if user is not None:
            from app.services.profile_service import age_days_of

            age_days = age_days_of(user.get("register_at"))
        return {
            "case_no": str(case.get("_id")),
            "user_id": user_id,
            "user_age_days": age_days,
            "user_text": (
                f"{user_id}（注册 {age_days} 天）" if age_days is not None else user_id
            ),
            "risk_score": int(case.get("risk_score") or 0),
            "risk_level": case.get("risk_level"),
            "risk_level_label": label_of(RiskLevel, str(case.get("risk_level") or "")),
            "decision": case.get("decision"),
            "degraded": bool(case.get("degraded")),
            "conclusion": conclusion,
            "conclusion_label": label_of(Conclusion, conclusion),
            "action_types": list(actions),
            "action_labels": [label_of(ActionType, a) for a in actions],
        }

    # ============================================================
    # 执行处置（核心）
    # ============================================================
    async def dispose(
        self, case_no: str, payload: DisposeIn, operator: str, *,
        actor_role: str = "", ip: Optional[str] = None, ua: Optional[str] = None,
    ) -> dict:
        """执行处置（Spec §3.1 `POST /cases/{case_no}/dispose`）。"""
        verdict, actions = self.validate_conclusion_and_actions(
            payload.conclusion, payload.action_types
        )
        remark = self.validate_remark(payload.remark)

        # ---- 幂等（BR-08-19）：**先于状态判定** ----
        # 处置成功的案件再次提交会命中 DSP-4004，但携带同一 idempotency_key
        # 时必须返回首次结果——因此命中缓存的判断必须排在状态检查之前。
        idem_key, digest = self._idempotency(case_no, payload, verdict, actions, remark)
        if idem_key is not None:
            state, body = _DISPOSE_IDEMPOTENCY.check(idem_key, digest)
            if state == "hit" and body is not None:
                return {**body, "idempotent_replay": True}

        case = await self.cases().load(case_no)

        # ---- ①a 二次确认令牌（BR-08-27）：缺失/过期/参数不符一律拒绝 ----
        # **令牌判定先于状态判定**，这是刻意的（BR-08-29 把"校验令牌 + 锁定案件"
        # 并列为第 ① 步，V-08-07 则要求三种令牌故障一律 `DSP-4006`）：
        # ① 没有有效令牌的调用方**不得**从处理顺序上得到任何与案件状态有关的
        #    信息（"先查状态再说令牌不对"等于把状态回给了未授权者）；
        # ② 令牌是防绕过的唯一闸门，让它在任何副作用之前、也在任何业务分支之前。
        # 代价是"对已处置案件重复提交"会先看到 `DSP-4006` 而不是 `DSP-4004`，
        # 前端据此回到弹窗重签，重签时（`/dispose/preview`，无令牌）才会拿到
        # `DSP-4004`「该案件已完成处置」——最终提示仍然正确。
        confirm_token.consume(payload.confirm_token, case_no, verdict, actions, operator)

        self._ensure_disposable(case, operator)

        if idem_key is not None:
            state, _entry = _DISPOSE_IDEMPOTENCY.reserve(idem_key, digest)
            if state == "hit":
                _, body = _DISPOSE_IDEMPOTENCY.check(idem_key, digest)
                if body is not None:
                    return {**body, "idempotent_replay": True}
            elif state == "inflight":
                # 同键并发：**不并行执行第二次**（那会产生两套副作用）。
                # 报冲突而不是静默等待，是因为等待会让请求悬在这里直到对方
                # 走完 30s 的令牌 TTL，前端体验与"卡死"无异
                raise CaseStateConflictError(
                    "重复处置", str(case.get("status") or ""),
                    "同一幂等键的处置正在执行，请稍后重试",
                )

        # ---- 前置数据：事件（device_id / 金额）与名单计划 ----
        event = await self._load_event(case)
        device_id = self._device_target(payload, event)
        planned, skipped = self._plan_list_writes(
            verdict, actions, case, device_id, override=payload.list_writes
        )

        # ---- ①b 原子锁定（BR-08-29 ①） ----
        acted_at = now_ms()
        try:
            locked = await self.case_repo().lock_for_disposal(
                case_no, operator, acted_at,
                stale_before=self.cases().disposal_lock_stale_before(acted_at),
            )
        except PyMongoError as e:
            self._release_idempotency(idem_key)
            raise CaseStateWriteFailedError(case_no, f"{type(e).__name__}: {e}") from e
        if locked == 0:
            self._release_idempotency(idem_key)
            await self._raise_lock_conflict(case_no, operator)

        # ---- ② 名单写入（紧操作，fail-closed） ----
        performed: list[dict] = []
        try:
            for plan in planned:
                result = await self.lists().add_auto(
                    list_type=plan["list_type"], entity_type=plan["entity_type"],
                    entity_value=plan["entity_value"],
                    reason=plan["reason"], related_case_no=case_no,
                    operator="system", effective_at=plan.get("effective_at"),
                    expire_at=plan.get("expire_at"),
                )
                performed.append(result)
        except BaseException as e:  # noqa: BLE001 - 含取消：见下方分支
            rolled = await self._rollback_lists(performed, operator)
            await self._release_lock(case_no, operator)
            self._release_idempotency(idem_key)
            if _is_cancellation(e):
                # 客户端断开/进程取消：补偿已经做完，**必须原样抛出**
                # （吞掉 CancelledError 会让任务无法被真正取消）
                log.error("处置被取消，已回滚本次名单写入 case_no=%s entry_ids=%s",
                          case_no, [w.get("entry_id") for w in performed])
                raise
            log.error("[DSP-5002] 名单写入失败，已回滚 case_no=%s：%s", case_no, e)
            raise ListLinkWriteFailedError(
                f"{_brief(e)}；本次命中「{_first_failed_plan(planned, performed)}」",
                rolled_back=rolled,
                failed_at=_brief(e),
            ) from e

        # ---- ③ BizAdapter（旁路，失败不阻断） ----
        biz_attempts, biz_failed = await self._sync_biz(
            actions, case, payload, device_id, event
        )
        biz_pending = bool(biz_failed)

        # ---- ④ 处置流水（紧操作） ----
        action_ids: list[str] = []
        try:
            action_ids = await self._write_actions(
                case, payload, verdict, actions, remark, operator, actor_role,
                acted_at, performed, device_id, biz_attempts,
            )
        except BaseException as e:  # noqa: BLE001
            if not _is_cancellation(e):
                await self._compensate_actions(action_ids)
            rolled = await self._rollback_lists(performed, operator)
            await self._release_lock(case_no, operator)
            self._release_idempotency(idem_key)
            if _is_cancellation(e):
                log.error("处置被取消，已回滚流水与名单 case_no=%s", case_no)
                raise
            log.error("[DSP-5005] 处置流水落库失败，已回滚 case_no=%s：%s", case_no, e)
            raise CaseActionWriteFailedError(
                case_no, f"{type(e).__name__}: {e}", rolled_back=rolled
            ) from e

        # ---- ⑤ 案件状态（紧操作） ----
        try:
            matched = await self.case_repo().mark_disposed(
                case_no, operator, acted_at, biz_sync_pending=biz_pending,
            )
        except PyMongoError as e:
            await self._compensate_actions(action_ids)
            rolled = await self._rollback_lists(performed, operator)
            await self._release_lock(case_no, operator)
            self._release_idempotency(idem_key)
            raise CaseStateWriteFailedError(
                case_no, f"{type(e).__name__}: {e}（已回滚名单：{rolled}）"
            ) from e
        if matched == 0:
            await self._compensate_actions(action_ids)
            rolled = await self._rollback_lists(performed, operator)
            await self._release_lock(case_no, operator)
            self._release_idempotency(idem_key)
            raise CaseStateWriteFailedError(
                case_no, f"案件状态已被他人改变（已回滚名单：{rolled}）"
            )

        # ---- ⑥ 审计（旁路：失败**不撤销**已生效的处置，BR-08-32） ----
        audit_info, audit_pending = await self._audit_disposal(
            case, verdict, actions, remark, operator, actor_role, acted_at,
            ip=ip, ua=ua, performed=performed, biz_attempts=biz_attempts,
        )
        if biz_pending:
            for action_id in action_ids:
                enqueue_biz_sync(case_no, action_id, operator=operator,
                                 action_types=actions,
                                 detail="处置时业务系统同步失败")
            try:
                await self.case_repo().set_biz_sync_pending(case_no, True)
            except PyMongoError as e:
                log.warning("标记 biz_sync_pending 失败 case_no=%s：%s", case_no, e)

        # ---- ⑦ 领域事件（旁路：消费方失败不影响已生效的处置） ----
        published = await publish_case_disposed(
            case_no=case_no, user_id=str(case.get("user_id") or ""),
            device_id=device_id, conclusion=verdict, action_types=actions,
            acted_at=acted_at, list_writes=performed,
        )

        response = self._response(
            case=case, case_no=case_no, verdict=verdict, actions=actions,
            remark=remark, operator=operator, actor_role=actor_role,
            acted_at=acted_at, action_ids=action_ids, performed=performed,
            skipped=skipped, biz_attempts=biz_attempts, biz_pending=biz_pending,
            audit_info=audit_info, audit_pending=audit_pending,
            published=published, device_id=device_id, trace_id=get_trace_id(),
        )
        self._finish_idempotency(idem_key, response)
        log.info(
            "处置完成 case_no=%s operator=%s conclusion=%s actions=%s "
            "writes=%d biz_failed=%s audit_pending=%s trace_id=%s",
            case_no, operator, verdict, actions, len(performed), biz_failed,
            audit_pending, get_trace_id(),
        )
        return response

    # ============================================================
    # 业务系统同步重试（Spec §3.1 `/biz-sync/retry`）
    # ============================================================
    async def retry_biz_sync(
        self, case_no: str, action_id: Optional[str], operator: str, *,
        actor_role: str = "", ip: Optional[str] = None, ua: Optional[str] = None,
    ) -> dict:
        """重试业务系统同步（**不重放名单/状态**）。

        重试只针对 `BizAdapter` 这一件事：名单与案件状态在处置时就已生效，
        再动它们等于重复执行一次不可撤销的处置（BR-08-18 没有 undo）。
        `attempt_no` 递增，且每次重试**追加**一条审计（Spec §3.1 明文），
        不覆盖原记录。
        """
        case = await self.cases().load(case_no)
        status = str(case.get("status") or "")
        if status not in (CaseStatus.DISPOSED.value, CaseStatus.ARCHIVED.value):
            raise CaseStateConflictError(
                "重试业务同步", status, "案件尚未处置，没有可重试的联动记录"
            )

        repo = CaseActionRepo(db_module.get_db())
        if action_id:
            target = await repo.find_by_id(str(action_id))
            if target is None or str(target.get("case_no")) != case_no:
                raise dsp_error("DSP-4040", "未找到该案件的这条处置流水")
            targets = [target]
        else:
            targets = await repo.list_failed_biz_sync(case_no)
        if not targets:
            # 没有待重试项是**正常结果**（Spec：`action_id` 可为空表示重试全部
            # 未成功的联动；全都成功了自然没有可重试的东西），返回 200
            return {
                "case_no": case_no, "action_id": action_id,
                "biz_sync": {"target": "none", "status": "ok",
                             "message": "没有待重试的业务联动", "retryable": False},
                "retried_at": now_ms(), "attempt_no": 0, "attempts": [],
                "degraded": bool(case.get("audit_pending")),
                "retry_hint": {"biz_sync": False, "audit": bool(case.get("audit_pending"))},
            }

        attempts: list[dict] = []
        this_round_failed = False
        for row in targets:
            this_id = str(row.get("_id"))
            previous = row.get("biz_sync_result") or {}
            attempt_no = int(previous.get("attempt_no") or 0) + 1
            result = await self._call_adapter(
                str(row.get("action_type") or ""), case, row.get("biz_targets") or {},
            )
            result["attempt_no"] = attempt_no
            result["retried_at"] = now_ms()
            try:
                await repo.update_biz_sync(this_id, result)
            except PyMongoError as e:
                log.warning("[DSP-5003] 重试结果落库失败 action_id=%s：%s", this_id, e)
            get_queue().mark_retried()
            if str(result.get("status")) != "ok":
                this_round_failed = True
                enqueue_biz_sync(case_no, this_id, operator=operator,
                                 detail=f"第 {attempt_no} 次重试仍失败")
            attempts.append({
                "action_id": this_id, "action_type": row.get("action_type"),
                "biz_sync": result, "attempt_no": attempt_no,
            })
            try:
                await audit(
                    actor=operator, actor_role=actor_role, action="case.biz_sync.retry",
                    target_type="case_action", target_id=this_id,
                    before={"biz_sync_result": previous},
                    after={"biz_sync_result": result},
                    ip=ip, ua=ua, strict=False,
                )
            except Exception as e:  # noqa: BLE001 - 重试结果不因留痕失败而丢失
                DEGRADED.mark(f"业务同步重试审计写入失败：{e}")
                log.error("[AUD-5002] 重试审计写入失败 action_id=%s：%s", this_id, e)

        # 案件级标记必须按**该案件剩余的全部失败**重算，而不是"本次重试的这一批"：
        # 只重试 1 条时，若按本批判断就会把"另外 3 条还失败着"变成
        # `biz_sync_pending=false`——页面从此不再提示重试，而库里仍有 3 条没同步。
        try:
            remaining = await repo.list_failed_biz_sync(case_no)
        except PyMongoError as e:
            log.warning("统计剩余待重试联动失败 case_no=%s：%s", case_no, e)
            remaining = targets
        still_failed = bool(remaining)
        try:
            await self.case_repo().set_biz_sync_pending(case_no, still_failed)
        except PyMongoError as e:
            log.warning("更新 biz_sync_pending 失败 case_no=%s：%s", case_no, e)

        last = attempts[-1]
        payload: dict[str, Any] = {
            "case_no": case_no,
            "action_id": last["action_id"],
            "biz_sync": last["biz_sync"],
            "retried_at": last["biz_sync"].get("retried_at"),
            "attempt_no": last["attempt_no"],
            "attempts": attempts,
            "degraded": bool(still_failed or case.get("audit_pending")),
            "retry_hint": {"biz_sync": bool(still_failed),
                           "audit": bool(case.get("audit_pending"))},
        }
        if this_round_failed:
            # DSP-5003 的 HTTP 是 200（Spec §5）：重试这个**动作**成功了，
            # 结论是"业务系统仍未同步成功"——用 503 表达会让前端把它当成
            # "重试接口挂了"，从而不去显示"仍失败、可再试"
            payload["notice_code"] = "DSP-5003"
            payload["notice"] = "业务系统同步仍未成功（模拟业务系统未对接），可稍后再次重试"
        return payload

    # ============================================================
    # 内部：编排的每一小步
    # ============================================================
    def case_repo(self) -> CaseRepo:
        return self.cases().cases()

    async def _load_event(self, case: dict) -> Optional[dict]:
        """读该案件关联的 E01（取 `device_id`；E01 有 90 天 TTL，读不到是正常的）。"""
        event_id = str(case.get("event_id") or "")
        if not event_id:
            return None
        try:
            return await self.cases().events().find_by_id(event_id)
        except PyMongoError as e:
            log.warning("处置读取事件失败 event_id=%s（按缺省处理）：%s", event_id, e)
            return None

    def _device_target(self, payload: DisposeIn, event: Optional[dict]) -> Optional[str]:
        """`biz_targets.device_id` 优先，其次取事件的 `device_id`（Spec §3.1）。"""
        if payload.biz_targets is not None and payload.biz_targets.device_id:
            return str(payload.biz_targets.device_id)
        return _device_of(event)

    def _plan_list_writes(
        self, conclusion: str, actions: list[str], case: dict,
        device_id: Optional[str], *, override: Optional[list] = None,
    ) -> tuple[list[dict], list[str]]:
        """算出本次要写哪些名单（默认映射 + 可选覆盖，BR-08-20 ~ 23）。

        覆盖（`payload.list_writes`）只允许 `black` / `gray`：白名单是"免风控"
        的授权，**绝不能**由处置链路授予（那会把一次拦截误操作升级成
        "以后全放行"的通道）。`entity_type` 必须是 E07 的五种之一（复用 06 的
        唯一真源），`entity_value` 必须非空（BR-08-23）。
        """
        user_id = str(case.get("user_id") or "")
        if override:
            reason = f"案件 {case.get('_id')} 处置联动（人工覆盖名单）"
            writes: list[dict] = []
            skipped: list[str] = []
            for idx, item in enumerate(override, start=1):
                list_type = str(getattr(item, "list_type", "") or "").strip()
                entity_type = str(getattr(item, "entity_type", "") or "").strip()
                entity_value = str(getattr(item, "entity_value", "") or "").strip()
                if list_type not in OVERRIDE_LIST_TYPES:
                    raise DisposalParamInvalidError(
                        f"list_writes[{idx}].list_type 仅支持 "
                        f"{' / '.join(OVERRIDE_LIST_TYPES)}，收到：{list_type!r}"
                    )
                if entity_type not in entity_types():
                    raise DisposalParamInvalidError(
                        f"list_writes[{idx}].entity_type 仅支持 "
                        f"{'/'.join(entity_types())}，收到：{entity_type!r}"
                    )
                if not entity_value:
                    raise DisposalParamInvalidError(
                        f"list_writes[{idx}].entity_value 不能为空"
                    )
                expire_at = getattr(item, "expire_at", None)
                writes.append({
                    "list_type": list_type, "entity_type": entity_type,
                    "entity_value": entity_value,
                    "reason": reason, "expire_at": expire_at,
                    "effective_at": None,
                })
            if not writes:
                skipped.append("本次未指定任何名单写入（list_writes 为空数组）")
            return writes, skipped

        default, skipped = default_list_writes(
            conclusion, actions, user_id=user_id, device_id=device_id
        )
        reason = "、".join(label_of(ActionType, a) for a in actions)
        plans = [
            {**w, "reason": f"案件 {case.get('_id')} 处置：{reason}",
             "effective_at": None}
            for w in default
        ]
        return plans, skipped

    def _ensure_disposable(self, case: dict, operator: str) -> None:
        """处置的前置条件（BR-08-06 / 08）：状态 + 认领人。

        判定顺序是"**先状态、后认领人**"：
        - 已处置的案件不管认领人是谁都应给 `DSP-4004`（重复处置），
          给 `DSP-4005`（非认领人）会让审核员以为自己抢错了案子；
        - `pending` 的案件给 `DSP-4003`（先认领），而不是 `DSP-4005`
          （它还没有认领人，说"被别人认领了"是假话）。
        """
        status = str(case.get("status") or "")
        if status == CaseStatus.DISPOSED.value:
            raise CaseAlreadyDisposedError(str(case.get("_id")))
        if status != CaseStatus.REVIEWING.value:
            raise CaseStateConflictError(
                "处置", status,
                "案件尚未认领" if status == CaseStatus.PENDING.value
                else "案件已归档，不可处置",
            )
        assignee = str(case.get("assignee") or "")
        if assignee != str(operator):
            raise CaseNotAssigneeError(str(case.get("_id")), assignee, str(operator))

    async def _raise_lock_conflict(self, case_no: str, operator: str) -> None:
        """锁定失败（条件更新匹配 0 条）：回读区分原因，给出人话提示（D42）。"""
        fresh = await self.case_repo().find_by_id(case_no)
        if fresh is None:
            raise CaseNotFoundError(case_no)
        status = str(fresh.get("status") or "")
        if status == CaseStatus.DISPOSED.value:
            raise CaseAlreadyDisposedError(case_no)
        if status != CaseStatus.REVIEWING.value:
            raise CaseStateConflictError("处置", status, "案件状态已被他人改变，请刷新后重试")
        assignee = str(fresh.get("assignee") or "")
        if assignee != str(operator):
            raise CaseNotAssigneeError(case_no, assignee, str(operator))
        raise CaseStateConflictError(
            "处置", status, "该案件正在被处置，请稍后重试（若长时间如此请联系管理员）"
        )

    async def _sync_biz(
        self, actions: list[str], case: dict, payload: DisposeIn,
        device_id: Optional[str], event: Optional[dict],
    ) -> tuple[dict[str, dict], bool]:
        """调 `BizAdapter`（BR-08-31）。返回 `({action_type: 结果}, 是否有失败)`。

        每个动作**独立**调用并独立记结果：一个动作失败不该让其余动作的结果丢失
        （`case_actions.biz_sync_result` 是逐条流水的字段）。失败**不抛异常**
        ——它是旁路副作用，处置必须继续（BR-08-31）。
        """
        targets = {
            "order_no": (payload.biz_targets.order_no if payload.biz_targets else None)
            or (event or {}).get("order_no"),
            "after_sale_no": (payload.biz_targets.after_sale_no
                              if payload.biz_targets else None),
            "device_id": device_id,
            "user_id": case.get("user_id"),
            "case_no": case.get("_id"),
            "event_id": case.get("event_id"),
        }
        results: dict[str, dict] = {}
        failed = False
        for action in actions:
            result = await self._call_adapter(action, case, targets)
            results[action] = result
            if str(result.get("status")) != "ok":
                failed = True
                log.warning(
                    "[DSP-5003] 业务系统同步失败 case_no=%s action=%s：%s",
                    case.get("_id"), action, result.get("message"),
                )
        return results, failed

    async def _call_adapter(self, action: str, case: dict, targets: dict) -> dict:
        """单次 `BizAdapter.sync` 调用，**永不抛异常**（异常被收成 failed 结果）。

        `parse` 结果统一成 Spec §3.1 的 `{target, status, message, retryable}`：
        适配器是**可插拔**的，真实实现可能返回自己的对象；这里做一次归一，
        保证 `case_actions.biz_sync_result` 的形状不随适配器而变。
        """
        from app.utils.timeutil import now_ms as _now

        payload = {"case_no": case.get("_id"), "user_id": case.get("user_id"),
                   **{k: v for k, v in (targets or {}).items() if v}}
        try:
            result = await self.adapter().sync(action, payload)
        except Exception as e:  # noqa: BLE001 - 旁路失败绝不阻断处置（BR-08-31）
            return {"target": "unknown", "status": "failed",
                    "message": f"{type(e).__name__}: {e}", "retryable": True,
                    "attempt_no": 0, "retried_at": _now()}
        if hasattr(result, "to_dict"):
            data = dict(result.to_dict())
        elif isinstance(result, dict):
            data = dict(result)
        else:  # 兜底：适配器返回了不可识别的类型
            data = {"target": type(result).__name__, "status": "failed",
                    "message": "适配器返回了不可识别的结果类型", "retryable": True}
        data.setdefault("attempt_no", 0)
        data["retried_at"] = _now()
        return data

    async def _write_actions(
        self, case: dict, payload: DisposeIn, conclusion: str, actions: list[str],
        remark: str, operator: str, actor_role: str, acted_at: int,
        performed: list[dict], device_id: Optional[str],
        biz_attempts: dict[str, dict],
    ) -> list[str]:
        """写 N 条流水（BR-08-37：一动作一条）。返回流水号列表（按动作顺序）。"""
        repo = self.cases().actions()
        docs: list[dict] = []
        trace_id = get_trace_id()
        for action in actions:
            action_id = await repo.new_id()
            docs.append({
                "_id": action_id,
                "case_no": str(case.get("_id")),
                "event_id": case.get("event_id"),
                "decision_id": case.get("decision_id"),
                "user_id": case.get("user_id"),
                "action_type": action,
                # `action_types` 冗余整份动作集：单看一条流水也能知道"这次处置
                # 一共做了哪几件事"（07 的详情页要按动作分组展示）
                "action_types": list(actions),
                "conclusion": conclusion,
                "remark": remark,
                "evidence_refs": list(payload.evidence_refs or []),
                "list_writes": _writes_for_action(action, performed),
                "operator": str(operator),
                "operator_role": str(actor_role),
                "acted_at": int(acted_at),
                "biz_targets": {
                    "order_no": (payload.biz_targets.order_no
                                 if payload.biz_targets else None),
                    "after_sale_no": (payload.biz_targets.after_sale_no
                                      if payload.biz_targets else None),
                    "device_id": device_id,
                },
                "biz_sync_result": biz_attempts.get(action) or {
                    "target": "none", "status": "skipped",
                    "message": "未调用业务系统", "retryable": False, "attempt_no": 0,
                },
                # BR-08-35：处置全程带 trace_id，写进流水之外的独立字段，
                # 供"这条流水属于哪一次请求"的问题定位
                "trace_id": trace_id,
            })
        await repo.insert_many(docs)
        return [str(d["_id"]) for d in docs]

    async def _audit_disposal(
        self, case: dict, conclusion: str, actions: list[str], remark: str,
        operator: str, actor_role: str, acted_at: int, *,
        ip: Optional[str], ua: Optional[str], performed: list[dict],
        biz_attempts: dict[str, dict],
    ) -> tuple[Optional[dict], bool]:
        """写**恰好一条** `case.dispose` 审计。返回 `(审计信息, 是否待重试)`。

        失败**不撤销**处置（BR-08-32）：置 `audit_pending=true`、入重试队列、
        响应 `degraded=true` + `DSP-5004`。理由见 Spec §8「新增 2」：
        撤销已生效的拦截会造成"先拦后放"的二次伤害。
        """
        case_no = str(case.get("_id"))
        after = {
            "case_no": case_no,
            "status": CaseStatus.DISPOSED.value,
            "conclusion": conclusion,
            "action_types": list(actions),
            "remark": remark,
            "acted_at": acted_at,
            # 名单写入的留痕就在这一条里（含 entry_id）：见模块 docstring
            "list_writes": [
                {"list_type": w.get("list_type"), "entity_type": w.get("entity_type"),
                 "entity_value": w.get("entity_value"), "entry_id": w.get("entry_id"),
                 "reused": bool(w.get("reused"))}
                for w in performed
            ],
            "biz_sync": {k: v.get("status") for k, v in biz_attempts.items()},
        }
        try:
            log_id = await audit(
                actor=operator, actor_role=actor_role, action="case.dispose",
                target_type="case", target_id=case_no,
                before={"status": case.get("status"), "assignee": case.get("assignee"),
                        "claim_deadline_at": case.get("claim_deadline_at")},
                after=after, ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            DEGRADED.mark(f"case.dispose 审计写入失败：{e}")
            log.error("[DSP-5004] case.dispose 审计写入失败（处置保留待重试）"
                      " case_no=%s：%s", case_no, e)
            try:
                await self.case_repo().set_audit_pending(case_no, True)
            except PyMongoError as inner:
                log.error("置 audit_pending 失败 case_no=%s：%s", case_no, inner)
            enqueue_audit(case_no, action="case.dispose", detail=_brief(e))
            return None, True

        info: Optional[dict] = None
        try:
            doc = await AuditRepo(db_module.get_db()).find_by_id(str(log_id))
        except PyMongoError as e:
            # 审计**已经写进去了**，只是回读不到指纹：响应少三个展示字段，
            # 绝不能因此把一次成功的处置报成失败
            log.warning("回读审计指纹失败 log_id=%s：%s", log_id, e)
            doc = None
        if doc is not None:
            info = {"log_id": doc.get("_id"), "hash": doc.get("hash"),
                    "prev_hash": doc.get("prev_hash")}
        else:
            info = {"log_id": log_id, "hash": None, "prev_hash": None}
        return info, False

    async def _rollback_lists(self, performed: list[dict], operator: str) -> bool:
        """回滚本次**新增**的名单条目（BR-08-30）。返回全部回滚成功与否。

        **复用（`reused=true`）的条目一律不回滚**：它在本次处置之前就已生效
        （是别人更早的判断），把它置 `removed` 等于撤销一个不属于本次的结论。
        这条取舍必须显式写出来——"回滚"两个字最容易让人以为要恢复原状。
        """
        created = [w for w in performed if not w.get("reused")]
        if not created:
            return True
        ok = True
        for write in created:
            entry_id = str(write.get("entry_id") or "")
            if not entry_id:
                ok = False
                continue
            rolled = await self.lists().rollback_auto(entry_id, operator="system")
            if not rolled:
                ok = False
            log.warning("[DSP-5002] 已回滚处置写入的名单条目 entry_id=%s（%s）",
                        entry_id, "成功" if rolled else "失败，需人工核对")
        return ok

    async def _compensate_actions(self, action_ids: list[str]) -> None:
        """删除本次刚写入的流水（"本次未生效"的一致性补偿，DSP-5001/5005）。"""
        if not action_ids:
            return
        try:
            deleted = await self.cases().actions().delete_by_ids(action_ids)
            log.warning("[DSP-5005] 已删除本次写入的处置流水 %d 条（处置未生效）", deleted)
        except PyMongoError as e:
            log.error("补偿删除处置流水失败 action_ids=%s：%s", action_ids, e)

    async def _release_lock(self, case_no: str, operator: str) -> None:
        try:
            await self.case_repo().release_lock(case_no, operator)
        except PyMongoError as e:
            log.error("释放处置内部锁失败 case_no=%s：%s", case_no, e)

    # ---------------- 响应组装 ----------------
    def _response(
        self, *, case: dict, case_no: str, verdict: str, actions: list[str],
        remark: str, operator: str, actor_role: str, acted_at: int,
        action_ids: list[str], performed: list[dict], skipped: list[str],
        biz_attempts: dict[str, dict], biz_pending: bool,
        audit_info: Optional[dict], audit_pending: bool, published: dict,
        device_id: Optional[str], trace_id: Optional[str],
    ) -> dict:
        """按 Spec §3.1 组装处置响应（字段名逐字对齐）。"""
        biz = _aggregate_biz(biz_attempts, actions)
        degraded = bool(biz_pending or audit_pending)
        notice_code: Optional[str] = None
        if audit_pending:
            notice_code = "DSP-5004"
        elif biz_pending:
            notice_code = "DSP-5003"
        return {
            "case_no": case_no,
            "status": CaseStatus.DISPOSED.value,
            # Spec §3.1 只定义了单数 `action_id`，而 BR-08-37 要求"一动作一条流水"。
            # 这里两者都给：`action_id` = 第一条（兼容契约与"点一下复制流水号"），
            # `action_ids` = 全部（页面要逐条展示）。见交付报告"新增 4"的记录。
            "action_id": action_ids[0] if action_ids else None,
            "action_ids": action_ids,
            "conclusion": verdict,
            "conclusion_label": label_of(Conclusion, verdict),
            "action_types": list(actions),
            "action_labels": [label_of(ActionType, a) for a in actions],
            "remark": remark,
            "operator": operator,
            "operator_role": actor_role,
            "acted_at": acted_at,
            "user_id": case.get("user_id"),
            "device_id": device_id,
            "list_writes": [
                {"list_type": w.get("list_type"), "entity_type": w.get("entity_type"),
                 "entity_value": w.get("entity_value"), "entry_id": w.get("entry_id"),
                 "reused": bool(w.get("reused")), "status": "active"}
                for w in performed
            ],
            "list_writes_skipped": skipped,
            "biz_sync": biz,
            "audit": audit_info,
            "degraded": degraded,
            "retry_hint": {"biz_sync": bool(biz_pending), "audit": bool(audit_pending)},
            "notice_code": notice_code,
            "notice": NOTICE_TEXT.get(notice_code or ""),
            "trace_id": trace_id,
            "event_published": published,
            "idempotent_replay": False,
        }

    # ---------------- 幂等 ----------------
    def _idempotency(
        self, case_no: str, payload: DisposeIn, conclusion: str,
        actions: list[str], remark: str,
    ) -> tuple[Optional[str], str]:
        """构造幂等键与载荷指纹（同键不同载荷按新请求处理，见下）。

        键空间 `case.dispose:{案件}:{客户端键}`。**不做"同键不同载荷报 409"**：
        处置没有天然幂等键（客户端每次打开弹窗生成一个），而同键不同载荷
        在现实中就是"用户改了参数又提交了一次"——那是**另一次处置**，
        而第一次十有八九还没成功（成功了状态就不是 reviewing 了）。
        真正需要防的是"同键同载荷被重复执行两次副作用"，那由指纹比对拦住。
        """
        raw = payload.idempotency_key
        if not raw:
            return None, ""
        key = f"case.dispose:{case_no}:{raw}"
        digest = payload_digest({
            "case_no": case_no, "conclusion": conclusion, "action_types": actions,
            "remark": remark,
            "list_writes": [w.model_dump() for w in (payload.list_writes or [])],
            "evidence_refs": list(payload.evidence_refs or []),
        })
        return key, digest

    def _release_idempotency(self, key: Optional[str]) -> None:
        if key:
            _DISPOSE_IDEMPOTENCY.release(key)

    def _finish_idempotency(self, key: Optional[str], body: dict) -> None:
        if key:
            _DISPOSE_IDEMPOTENCY.finish(key, dict(body))


# ============================================================
# 模块级辅助
# ============================================================
#: 处置响应的降级提示文案（唯一真源；前端按 `notice_code` 亦可自行映射）
NOTICE_TEXT: dict[str, str] = {
    "DSP-5003": "处置已生效，业务系统同步失败（模拟业务系统未对接），已加入重试队列",
    "DSP-5004": "处置已生效，审计落库待重试（audit_pending）",
}


def _device_of(event: Optional[dict]) -> Optional[str]:
    """取事件的 `device_id`（BR-08-23：缺失时**不臆造**，返回 `None`）。"""
    if not event:
        return None
    value = event.get("device_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _writes_for_action(action: str, performed: list[dict]) -> list[dict]:
    """把本次写入的名单条目**按动作归属**（一条流水只看它自己引起的写入）。

    归属规则与 BR-08-20 的默认映射一一对应：拉黑用户 → `black/user`，
    封禁设备 → `black/device`，放行（存疑）→ `gray/user`；
    `block_order` / `reject_refund` 不写名单，因此它们的流水 `list_writes` 为空。
    """
    def matched(write: dict) -> bool:
        list_type = str(write.get("list_type") or "")
        entity_type = str(write.get("entity_type") or "")
        if action == ActionType.BLACKLIST_USER.value:
            return list_type == "black" and entity_type == "user"
        if action == ActionType.BAN_DEVICE.value:
            return list_type == "black" and entity_type == "device"
        if action == ActionType.PASS.value:
            return list_type == "gray"
        return False

    return [
        {"list_type": w.get("list_type"), "entity_type": w.get("entity_type"),
         "entity_value": w.get("entity_value"), "entry_id": w.get("entry_id"),
         "reused": bool(w.get("reused"))}
        for w in performed if matched(w)
    ]


def _aggregate_biz(biz_attempts: dict[str, dict], actions: list[str]) -> dict:
    """把逐动作的业务同步结果聚合成响应里的一个 `biz_sync`。

    有失败 → 取**第一条失败**的 message（页面要显示"为什么失败"）；
    全成功 → 取第一条动作的结果（`target` 通常是 `mock`，页面据此标注「模拟同步」）。
    """
    if not biz_attempts:
        return {"target": "none", "status": "skipped", "message": "本次无业务联动动作",
                "retryable": False}
    for action in actions:
        result = biz_attempts.get(action)
        if result and str(result.get("status")) != "ok":
            return {"target": result.get("target"), "status": "failed",
                    "message": result.get("message"), "retryable": True}
    first = biz_attempts.get(actions[0]) if actions else None
    if first is None:
        first = next(iter(biz_attempts.values()))
    return {"target": first.get("target"), "status": first.get("status"),
            "message": first.get("message"), "retryable": bool(first.get("retryable"))}


def _first_failed_plan(planned: list[dict], performed: list[dict]) -> str:
    """拼出"失败在那一条"的可读描述（响应 `detail` 用）。"""
    if len(performed) < len(planned):
        item = planned[len(performed)]
        return (f"{item.get('list_type')}/{item.get('entity_type')}"
                f"={item.get('entity_value')}")
    return "（未识别到具体条目）"


def _brief(error: BaseException) -> str:
    """把异常压成一句可读文案（用户可见，不能回显堆栈，BR-00-14）。"""
    if isinstance(error, AppError):
        return f"{error.code}：{error.message}"
    return f"{type(error).__name__}: {error}"


def _is_cancellation(error: BaseException) -> bool:
    """是否是"任务被取消/进程退出"这一类 `BaseException`。

    它们必须**原样抛出**，不能被我方包装成业务错误码：包装会让
    `asyncio` 的取消语义失效（任务无法真正取消），而补偿动作已经在抛出前做完。
    """
    return isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))


def reset_idempotency() -> None:
    """复位处置幂等缓存（测试夹具使用）。"""
    _DISPOSE_IDEMPOTENCY.clear()
    _DISPOSE_IDEMPOTENCY.stats = {
        "hits": 0, "conflicts": 0, "waits": 0, "evicted": 0, "expired": 0,
    }


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = DisposalService()


def get_disposal_service() -> DisposalService:
    return _SERVICE


def reset_service() -> None:
    _SERVICE.reset_dependencies()


__all__ = [
    "DisposalService",
    "NOTICE_TEXT",
    "get_disposal_service",
    "reset_idempotency",
    "reset_service",
]
