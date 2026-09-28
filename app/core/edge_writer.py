# -*- coding: utf-8 -*-
"""异步建边任务：事件决策**之后**补写关联边、维护聚集度与画像（BR-09-07/11/12）。

## 为什么是"决策之后 + 异步"（BR-09-07）

建边、打标、累加统计都是**最终一致**的旁路工作：它们不改变这一次的放行结论，
却要写 MongoDB。若把它们放进同步链路，一次 Mongo 抖动就会把已经做出的决策
拖慢甚至拖超时（200ms 预算，BR-03-23）。
因此模块 03 在组装完响应之后只做一件事——`enqueue_after_decision()`：
**同步入队、零 await、立即返回**，真正的写库在后台任务里跑。

## 建图失败为什么不影响决策（BR-09-11）

后台任务失败时：① 入有界重试队列再试；② 计 `GRP-5003` 告警并置降级标记；
③ **绝不回滚已返回的决策**。理由与模块 04 的快照落库完全一致：把"证据没记上"
升级成"业务中断"是本末倒置，而且回滚到一半会出现"库里有关联、决策里没有"的
分裂状态。

## `linked_user_cnt` 只在**新边**上递增（本模块最容易写错的一处）

`update_one(..., upsert=True)` 的返回值能区分"插入"与"命中"。只有插入才递增：
否则同一对 user-device 上报 100 次就会得到 `linked_user_cnt=100`，
08 页面显示"该设备关联 100 个账号"——**一个纯属虚构的团伙规模**，
而它恰恰是审核员用来判断"要不要并案"的核心数字。

## 自动打标只发生在"新证据"上

`device_cluster` / `ip_cluster` / `address_cluster` / `proxy_ip` 四个标签只在
**新边插入**或**新 IP 行建立**时评估。这样 BR-09-04 的"误打需显式移除"才有意义：
若每次事件都重新评估并补打，人工移除的标签会在**同一条旧证据**上立刻复活，
审计里那条移除记录就等于白写了。
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from app import db as db_module
from app.core.degraded import DEGRADED
from app.errors import GRP_NOTICE
from app.logging import get_logger
from app.repos.graph_repo import GraphRepo
from app.repos.profile_repo import ProfileRepo
from app.services.profile_service import cluster_tags, is_proxy_of, stat_updates_for
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.graph.writer")

#: 待写队列上限。有界是必须的：Mongo 长时间不可用时无界的队列会把内存吃光，
#: 那时连决策链路本身都跑不动。满了**丢最旧**——排障时最近的事件最有用。
QUEUE_MAX = 2000

#: 单条失败任务的重试次数与退避（与 `event_service`/`feature_service` 同因：
#: 高频路径不无限堆积后台任务）
RETRY_ATTEMPTS = 2
RETRY_BASE_SEC = 0.05

#: 失败任务的重试队列上限（同样是"丢最旧"）
RETRY_QUEUE_MAX = 1000

#: 同一手机号最多建立多少条 `same_phone` 边。
#: 有界是必须的：脱敏手机号是"前 3 + 后 4"的低区分度键，一个热门尾号可能命中
#: 大量账号；无上限会让一次登录事件写出成百上千条边。
SAME_PHONE_MAX = 20


def _slim_event(event: Mapping[str, Any]) -> dict:
    """只留下建图需要的字段。

    为什么不整份入队：事件 dict 里此时已经挂了 `feature_snapshot`（可含 18 项特征
    与缺失原因）。队列里压着上千份快照副本是纯粹的内存浪费，而建图一个字段都用不上。
    """
    scene = event.get("scene_extra")
    scene = scene if isinstance(scene, Mapping) else {}
    return {
        "event_type": str(event.get("event_type") or ""),
        "user_id": str(event.get("user_id") or ""),
        "device_id": _opt(event.get("device_id")),
        "ip": _opt(event.get("ip")),
        "address_id": _opt(event.get("address_id")),
        "phone": _opt(event.get("phone")),
        "amount": event.get("amount"),
        "ts": event.get("ts"),
        "ua": _opt(scene.get("ua")),
    }


def _opt(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class EdgeWriter:
    """关联边的异步写入器（进程内单例，见文件末尾）。"""

    def __init__(self, *, repo: Any = None, graph: Any = None) -> None:
        #: 待写任务（`list` + 上限判断 = 满了丢最旧）
        self._pending: list[dict] = []
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._retry_queue: list[dict] = []
        #: 可注入的依赖（`None` = 每次从当前数据库取）。
        #: 注入点存在的理由：BR-09-11 的"失败入重试队列 + 告警"这条路径**必须**被
        #: 真实验证，而真让 Mongo 写失败只能靠停库（那会连带影响其它断言）。
        self._repo_override = repo
        self._graph_override = graph
        self.stats: dict[str, int] = {
            "enqueued": 0, "processed": 0, "failed": 0, "requeued": 0,
            "retried": 0, "dropped": 0, "edges": 0, "new_edges": 0,
            "tags": 0, "stat_updates": 0,
        }

    # ---------------- 装配 ----------------
    def configure(self, *, repo: Any = None, graph: Any = None) -> None:
        """替换依赖（测试注入用）。"""
        if repo is not None:
            self._repo_override = repo
        if graph is not None:
            self._graph_override = graph

    def reset_dependencies(self) -> None:
        """还原成"每次从当前数据库取"（测试收尾用）。"""
        self._repo_override = None
        self._graph_override = None

    def _repo(self) -> ProfileRepo:
        """每次取新句柄：测试会切换数据库，缓存句柄会写错库。"""
        if self._repo_override is not None:
            return self._repo_override
        return ProfileRepo(db_module.get_db())

    def _graph(self) -> GraphRepo:
        if self._graph_override is not None:
            return self._graph_override
        return GraphRepo(db_module.get_db())

    def _ensure_loop(self) -> None:
        """把内部状态绑定到当前事件循环（测试逐用例新建循环）。

        与 `AuditService`/`EventSimulator` 同一理由：上一个循环里的 worker 已经随
        循环消亡，继续引用它会让 `create_task` 抛 "attached to a different loop"。
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._worker = None
            # 不跨循环搬运待写任务：那些任务的落库句柄属于旧循环，搬过去必然报错。
            # 丢掉它们并**如实计数**（否则会以为"都写完了"）。
            if self._pending:
                self.stats["dropped"] += len(self._pending)
                log.warning("事件循环已更换，丢弃 %d 条待建边任务（原循环已消亡）",
                            len(self._pending))
            self._pending = []

    # ---------------- 入队（同步、零 await） ----------------
    def enqueue_after_decision(self, event: Mapping[str, Any],
                               decision: Optional[Mapping[str, Any]] = None) -> None:
        """由模块 03 在**决策返回之后**调用（BR-09-07）。

        本方法**不做任何 IO、不含 await**：它是同步链路的一部分，只能花
        "append + 可能要起一个后台任务"这点时间。任何写库动作都在后台任务里。
        """
        self._ensure_loop()
        job = {"event": _slim_event(event), "decision": dict(decision or {})}
        if len(self._pending) >= QUEUE_MAX:
            # 丢最旧（BR-09-11 的"计数告警"：丢了多少必须能被看到）
            self._pending.pop(0)
            self.stats["dropped"] += 1
            log.warning("[GRP-5003] 待建边队列已满（%d），丢弃最旧的一条", QUEUE_MAX)
        self._pending.append(job)
        self.stats["enqueued"] += 1
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="edge-writer")

    # ---------------- 后台执行 ----------------
    async def _worker_loop(self) -> None:
        """把队列里的任务逐条处理掉，然后**退出**。

        为什么不做常驻循环：常驻任务在 ASGI 测试里必须被显式关闭，否则每个用例
        都会留下 "Task was destroyed but it is pending" 告警。队列空即退出，
        下一次 `enqueue_after_decision` 会再起一个——代价是一次 `create_task`。
        """
        while self._pending:
            job = self._pending.pop(0)
            await self._process_with_retry(job)

    async def _process_with_retry(self, job: dict) -> bool:
        last: Optional[BaseException] = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                await self.write_for_event(job["event"], job["decision"])
                self.stats["processed"] += 1
                return True
            except Exception as e:  # noqa: BLE001 - 后台任务不允许把异常抛到无人区
                last = e
                code = getattr(e, "code", "GRP-5003")
                log.warning("[%s] 建边任务失败（第 %d/%d 次）：%s",
                            code, attempt, RETRY_ATTEMPTS, e)
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BASE_SEC * attempt)

        self.stats["failed"] += 1
        self._enqueue_retry(job)
        detail = f"{type(last).__name__}: {last}" if last is not None else "unknown"
        log.error("[GRP-5003] %s event_type=%s user_id=%s（决策已正常返回，未回滚）",
                  GRP_NOTICE["GRP-5003"], job["event"].get("event_type"),
                  job["event"].get("user_id"))
        DEGRADED.mark(f"[GRP-5003] 建边失败：{detail}")
        return False

    def _enqueue_retry(self, job: dict) -> None:
        """入有界重试队列（丢最旧 + 计数）。"""
        if len(self._retry_queue) >= RETRY_QUEUE_MAX:
            self._retry_queue.pop(0)
        self._retry_queue.append(job)
        self.stats["requeued"] += 1

    async def retry_pending(self) -> int:
        """把重试队列里的任务再跑一遍，返回成功条数。

        与模块 04 的 `retry_pending` 同因：不重试它们就永远躺在内存里，
        进程重启即丢——而"最终一致"要靠一个真实的第二次机会，不是靠愿望。
        """
        if not self._retry_queue:
            return 0
        pending = self._retry_queue
        self._retry_queue = []
        ok = 0
        for job in pending:
            if await self._process_with_retry(job):
                ok += 1
                self.stats["retried"] += 1
        return ok

    # ---------------- 真正的工作 ----------------
    async def write_for_event(self, event: Mapping[str, Any],
                              decision: Optional[Mapping[str, Any]] = None) -> dict:
        """一条事件的建图与画像增量维护（可单独调用，便于单测）。

        顺序不能乱：**先确保实体行存在，再建边，再按"是否新边"决定要不要递增
        `linked_user_cnt`**。若先递增计数，一个还没画像行的实体只能靠 upsert
        建出一个没有 `first_seen_at` 的半成品行——那行画像在画像卡上会显示成
        "首次出现时间未知"，而它其实只是我们写反了顺序。
        """
        repo = self._repo()
        graph = self._graph()
        now = now_ms()
        result: dict[str, Any] = {"new_edges": 0, "tags": [], "stat": {}}

        user_id = _opt(event.get("user_id"))
        device_id = _opt(event.get("device_id"))
        ip_value = _opt(event.get("ip"))
        address_id = _opt(event.get("address_id"))
        phone = _opt(event.get("phone"))

        # 1. IP 自动落画像（D46 的硬性要求：任何事件 IP 都要有确定的是否代理）
        ip_doc: Optional[dict] = None
        ip_created = False
        if ip_value:
            ip_created = await repo.ensure_ip(ip_value, now)
            result["ip_created"] = ip_created

        # 2. 设备 / 地址画像行（没有就不建——事件里没有设备就不该凭空出现设备）
        if device_id:
            await repo.ensure_device(device_id, now, ua=_opt(event.get("ua")))
        if address_id and user_id:
            await repo.ensure_address(address_id, user_id, now)

        # 3. 建边（BR-09-09）。**只有新边才动 linked_user_cnt**（BR-09-08）
        device_cnt: Optional[int] = None
        ip_cnt: Optional[int] = None
        address_cnt: Optional[int] = None
        if user_id and device_id:
            if await graph.upsert_edge("user", user_id, "device", device_id,
                                       "used_device", now=now):
                result["new_edges"] += 1
                self.stats["new_edges"] += 1
                device_cnt = await repo.inc_linked_user_cnt("device", device_id)
            self.stats["edges"] += 1
        if user_id and ip_value:
            if await graph.upsert_edge("user", user_id, "ip", ip_value,
                                       "shared_ip", now=now):
                result["new_edges"] += 1
                self.stats["new_edges"] += 1
                ip_cnt = await repo.inc_linked_user_cnt("ip", ip_value)
                # 只在"新证据"上读一次 IP 画像，用于判断 proxy_ip 标签
                ip_doc = await repo.get_ip(ip_value)
            self.stats["edges"] += 1
        if user_id and address_id:
            if await graph.upsert_edge("user", user_id, "address", address_id,
                                       "shared_address", now=now):
                result["new_edges"] += 1
                self.stats["new_edges"] += 1
                address_cnt = await repo.inc_linked_user_cnt("address", address_id)
            self.stats["edges"] += 1

        # 4. `same_phone`：手机号关联的多个用户之间建边（BR-09-09 的最后一条）
        if user_id and phone:
            await self._link_same_phone(repo, graph, user_id, phone, now, result)

        # 5. 累计统计（BR-09-02 / D12 的口径见 `stat_updates_for`）
        updates = stat_updates_for(event, decision)
        if user_id and updates:
            await repo.inc_stats(user_id, updates)
            self.stats["stat_updates"] += 1
            result["stat"] = updates

        # 6. 最近一次决策摘要（BR-09-06：覆盖式，不是历史数组）
        if user_id and decision:
            await repo.set_latest_decision(user_id, _decision_block(decision), now)

        # 7. 自动标签（只在"新证据"上评估，见模块 docstring）
        proxy = is_proxy_of(ip_doc) if ip_doc is not None else (
            False if ip_created else None
        )
        wanted = cluster_tags(device_cnt=device_cnt, ip_cnt=ip_cnt,
                              address_cnt=address_cnt, is_proxy=proxy)
        for tag in wanted:
            if await repo.add_tag(user_id, tag):
                result["tags"].append(tag)
                self.stats["tags"] += 1
        return result

    async def _link_same_phone(self, repo: ProfileRepo, graph: GraphRepo,
                               user_id: str, phone: str, now: int,
                               result: dict) -> None:
        """同一（脱敏）手机号关联的多个用户之间建 `same_phone` 边。

        **边是无向的，但集合里必须有唯一方向**：唯一索引建在
        `from_id + to_id + relation` 上，`A→B` 与 `B→A` 是两条不同的记录
        （图查询会把它们当成两条边，前端画出两条重叠的线、边数也翻倍）。
        因此这里始终按字典序把较小的编号放在 `from`——一条关系只留一条记录。

        ⚠️ **已知口径限制**：事件里的手机号在模块 03 入口就已脱敏
        （`139****0001`），因此这里是"按脱敏值相等"判定同号。前 3 位与后 4 位
        的组合存在碰撞可能（不同号码落到同一个掩码），本模块**无法区分**
        （明文不入库，无从比对）。这是 BRI-09-05 脱敏要求带来的固有代价，
        已如实登记在交付说明里；若需要精确判定，须引入不可逆的
        `phone_hash`（E10 当前没有该字段，属数据实体变更）。
        """
        others = await repo.find_users_by_phone(phone, exclude_user_id=user_id,
                                               limit=SAME_PHONE_MAX)
        for other in others:
            other_id = str(other.get("_id") or "")
            if not other_id:
                continue
            head, tail = sorted((user_id, other_id))
            if await graph.upsert_edge("user", head, "user", tail, "same_phone", now=now):
                result["new_edges"] += 1
                self.stats["new_edges"] += 1
            self.stats["edges"] += 1

    # ---------------- 收尾 ----------------
    async def add_edge_direct(self, from_type: str, from_id: str, to_type: str,
                              to_id: str, relation: str) -> bool:
        """立即写一条边（不经队列）。返回是否**新建**。

        与队列里的路径共用同一套"仅新边才递增 `linked_user_cnt`"规则：
        两条路径若各写一套，同一条边从不同入口建立时计数就会加两次，
        而"设备关联账号数"是所有页面的共同数字（BR-09-13 的单一真源）。
        """
        self._ensure_loop()
        now = now_ms()
        is_new = await self._graph().upsert_edge(
            from_type, from_id, to_type, to_id, relation, now=now,
        )
        if is_new and str(to_type) in ("device", "ip", "address"):
            await self._repo().inc_linked_user_cnt(str(to_type), str(to_id))
        return is_new

    async def flush(self, timeout: float = 5.0) -> bool:
        """等待在途建边任务收尾（测试与优雅关闭用）。"""
        task = self._worker
        if task is None or task.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.error("仍有建边任务未完成（等待 %.1fs 超时）", timeout)
            return False

    async def stop(self, timeout: float = 2.0) -> bool:
        """停止后台 worker 并清空待写队列（测试夹具体与优雅关闭用）。

        与 `audit_service.stop_consumer()` 同一个目的：不显式停掉会留下
        "Task was destroyed but it is pending" 告警，更糟的是把待写任务带进
        **下一个用例的事件循环**（跨测试污染）。
        """
        done = await self.flush(timeout=timeout)
        task, self._worker = self._worker, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._pending:
            self.stats["dropped"] += len(self._pending)
            self._pending = []
        return done


def _decision_block(decision: Mapping[str, Any]) -> dict:
    """把 03 的决策块摘成 E10 `latest_decision` 的四个字段。

    `risk_score` 取 `final_score`（05 的融合分）而不是 `rule_score`：画像卡上
    展示的"风险分值"应当与决策依据一致，而 05 的分档用的是 `final_score`。
    """
    score = decision.get("final_score")
    if score is None:
        score = decision.get("risk_score")
    try:
        score = None if score is None else float(score)
    except (TypeError, ValueError):
        score = None
    return {
        "risk_score": score,
        "risk_level": decision.get("risk_level"),
        "decision": decision.get("decision"),
        "decided_at": now_ms(),
    }


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_WRITER = EdgeWriter()


def get_edge_writer() -> EdgeWriter:
    return _WRITER


def enqueue_after_decision(event: Mapping[str, Any],
                           decision: Optional[Mapping[str, Any]] = None) -> None:
    """模块 03 的调用口（同步返回，建图在后台）。"""
    _WRITER.enqueue_after_decision(event, decision)


async def flush(timeout: float = 5.0) -> bool:
    return await _WRITER.flush(timeout)


async def stop(timeout: float = 2.0) -> bool:
    return await _WRITER.stop(timeout)


async def retry_pending() -> int:
    return await _WRITER.retry_pending()


async def add_edge(from_type: str, from_id: str, to_type: str, to_id: str,
                   relation: str) -> None:
    """§3.3：`add_edge(from_type, from_id, to_type, to_id, relation)` —— 03 异步补写。

    直接写一条边（不经队列、不等决策）：调用方**明确要求现在建立这条关联**
    （例如 08 的"案件转交"要建 `transferred_to` 边）。新边同样会递增
    `linked_user_cnt`，保证与事件链路建出来的边在计数上完全一致——
    同一条边不论从哪条路径来，计数只能增加一次。
    """
    await _WRITER.add_edge_direct(from_type, from_id, to_type, to_id, relation)


__all__ = [
    "EdgeWriter",
    "QUEUE_MAX",
    "RETRY_ATTEMPTS",
    "RETRY_QUEUE_MAX",
    "SAME_PHONE_MAX",
    "add_edge",
    "enqueue_after_decision",
    "flush",
    "get_edge_writer",
    "retry_pending",
    "stop",
]
