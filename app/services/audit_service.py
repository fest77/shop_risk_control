# -*- coding: utf-8 -*-
"""审计服务：对外 `audit()` 接口 + 内存队列 + 单消费者 + 重试 + `strict` 语义。

对应模块 12 §3.4 / §4.2。

## 为什么要有队列（BR-12-07）

哈希链的写入必须**串行**（读链头 → 算哈希 → 插入，这一整段不能被并发插入打断，
否则两条记录会读到同一个 `prev_hash`，链就此分叉——AD-04 / G-10）。
与其让每个业务线程去抢锁，不如把写入收敛到**一个消费者协程**：
顺序天然成立，业务侧只在 `strict=True` 时等待结果。

## `strict` 的语义（BR-12-16/17，全项目最重要的 fail-closed 之一）

- `strict=True`（规则/名单/处置/配置变更）：**等待落库确认**；重试 3 次仍失败则抛
  `AUD-5001`，**业务操作必须中止并回滚**。理由：这些操作一旦执行就改变了风控行为，
  没有留痕就无法追溯与问责——**宁可不做，不可无痕地做**。
- `strict=False`（事件接入、越权拒绝、登录、导出）：只入队不等待；失败重试 3 次后
  告警（`AUD-5002`），**不阻断**业务。

## 已知局限（如实记录，不掩盖）

队列在内存里：进程崩溃会丢掉尚未落库的 `strict=False` 记录。而 `auth.denied`
（越权尝试）恰好属于 `strict=False`（BR-12-17），也就是说**崩溃会丢掉安全事件**。
若要保证不丢，需把这两类提升为 `strict=True`（代价是每次越权都要等一次写库），
或把队列换成持久化队列。已登记为待确认项。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pymongo.errors import PyMongoError

from app import db as db_module
from app.constants import COLL_AUDIT_LOGS
from app.engine.audit_hash import GENESIS_HASH, compute_hash, short_hash
from app.errors import AuditWriteFailedError
from app.logging import get_logger
from app.repos.audit_repo import AuditRepo
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.audit")

# 队列上限：超过说明写库速度远跟不上业务，此时丢新记录并告警，
# 而不是无限堆积把进程内存吃光
QUEUE_MAX = 20_000
# 写库重试次数与退避基数（BR-12-08：指数退避）
MAX_RETRY = 3
RETRY_BASE_SEC = 0.05
# 查询时间跨度上限（AUD-4003：防全表扫描）
MAX_SPAN_DAYS = 30
# 导出条数上限（BR-12-21 / AUD-4002）
EXPORT_LIMIT = 10_000
# 单页最大条数（BR-12-21）
MAX_PAGE_SIZE = 100
# 单次校验的墙钟预算，超过则返回"已校验前 N 条"（AUD-5004）
VERIFY_BUDGET_MS = 10_000


@dataclass
class _Job:
    """一条待写入的审计记录。"""

    payload: dict
    strict: bool
    future: Optional[asyncio.Future] = field(default=None)


class AuditService:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._worker: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # **只是"正在关闭"，不是"已关闭"**：这个区别很关键。
        # 若做成永久标志，`stop()` 之后这个对象就再也无法使用——
        # 测试之间、以及"先关一次再重开"的场景都会被永久拒掉。
        self._stopping = False
        self.stats = {"written": 0, "failed": 0, "dropped": 0}

    def _ensure_loop(self) -> None:
        """把内部 asyncio 原语绑定到**当前**事件循环。

        `asyncio.Queue` / `Lock` 并非"任意循环可用"：它们会绑定到首次使用它们的
        循环，换循环再用就抛 `is bound to a different event loop`。
        生产是单循环，这个分支永远不会触发；但进程内单例必须能在新循环下重新
        装配（测试每个用例一个新循环、以及测试框架的复用场景），否则会以
        "莫名其妙的 RuntimeError"形式崩掉。重建时旧队列里的任务本就属于已死的
        循环，丢弃是唯一正确的处置。
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
            self._lock = asyncio.Lock()
            self._worker = None

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """启动单消费者协程（由应用 lifespan 调用；幂等）。"""
        self._ensure_loop()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._consume(), name="audit-consumer")
            log.info("审计单消费者已启动（队列上限 %d）", QUEUE_MAX)

    async def stop(self) -> None:
        """优雅关闭：先把已入队的记录写完，避免"关服丢审计"。"""
        self._ensure_loop()
        self._stopping = True
        try:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except asyncio.TimeoutError:
                log.error("关闭时仍有 %d 条审计未落库（等待 5s 超时）", self._queue.qsize())
            if self._worker is not None:
                self._worker.cancel()
                try:
                    await self._worker
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._worker = None
        finally:
            # 关闭流程结束后解除标志：对象回到"空闲可用"状态
            self._stopping = False

    # ---------------- 对外接口（§3.4） ----------------
    async def audit(
        self,
        actor: str,
        actor_role: str,
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        before: Optional[dict] = None,
        after: Optional[dict] = None,
        ip: Optional[str] = None,
        ua: Optional[str] = None,
        strict: bool = False,
    ) -> Optional[str]:
        """记录一条审计。`strict=True` 时返回 `log_id` 并保证已落库。"""
        self._ensure_loop()
        payload = {
            "ts": now_ms(),
            "actor": actor or "system",
            "actor_role": actor_role or "",
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "before": before,
            "after": after,
            # ip/ua 存库但不参与哈希（BR-12-03），见 audit_hash.py 的说明
            "ip": ip,
            "ua": ua,
            "chain_id": "main",
        }
        loop = asyncio.get_running_loop()
        job = _Job(payload=payload, strict=strict,
                   future=loop.create_future() if strict else None)

        # 关闭进行中才拒绝；关闭完成后允许自动重启消费者（见 _stopping 的说明）
        if self._stopping:
            return await self._reject_when_closing(action, strict, job)

        # 消费者没在跑就补启一个：否则记录会永远躺在队列里，
        # `strict=True` 的调用方会一直挂住（比报错更难排查）。
        # 生产由 lifespan 启动；测试与脚本不跑 lifespan，靠这里兜底。
        if self._worker is None or self._worker.done():
            await self.start()

        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            log.error("审计队列已满（%d），丢弃 action=%s", QUEUE_MAX, action)
            if strict:
                raise AuditWriteFailedError(action, "审计队列已满")
            return None

        if strict:
            assert job.future is not None
            return await job.future
        return None

    async def _reject_when_closing(self, action: str, strict: bool, job: _Job) -> Optional[str]:
        if strict:
            raise AuditWriteFailedError(action, "服务正在关闭，审计不可用")
        log.warning("服务关闭中，丢弃审计记录 action=%s", action)
        self.stats["dropped"] += 1
        return None
    async def flush(self, timeout: float = 5.0) -> bool:
        """等待队列排空（测试与导出前使用）。"""
        self._ensure_loop()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------- 单消费者 ----------------
    async def _consume(self) -> None:
        while True:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._write_with_retry(job)
            except Exception as e:  # noqa: BLE001 - 消费者绝不能因单条失败而退出
                log.exception("审计消费者异常：%s", e)
            finally:
                self._queue.task_done()

    async def _write_with_retry(self, job: _Job) -> Optional[str]:
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                log_id = await self._write_one(job.payload)
                self.stats["written"] += 1
                if job.future is not None and not job.future.done():
                    job.future.set_result(log_id)
                return log_id
            except PyMongoError as e:
                last_error = e
                log.warning("审计写入失败（第 %d/%d 次）action=%s：%s",
                            attempt, MAX_RETRY, job.payload.get("action"), e)
                if attempt < MAX_RETRY:
                    # 指数退避：瞬时故障（网络抖动/主从切换）通常几百毫秒内恢复
                    await asyncio.sleep(RETRY_BASE_SEC * (2 ** (attempt - 1)))

        # 重试耗尽
        self.stats["failed"] += 1
        action = str(job.payload.get("action"))
        if job.future is not None and not job.future.done():
            # strict：把错误交给业务侧，由它中止并回滚（AUD-5001）
            job.future.set_exception(
                AuditWriteFailedError(action, f"{type(last_error).__name__}: {last_error}")
            )
        else:
            # 普通操作：告警但不阻断（AUD-5002）
            log.error("审计写入最终失败（AUD-5002，不阻断业务）action=%s：%s",
                      action, last_error)
        return None

    async def _write_one(self, payload: dict) -> str:
        """读链头 → 算哈希 → 插入，整段持锁（BR-12-06）。

        消费者本身已是单写者，但仍加锁：`flush()` 之外，测试与将来可能的
        直接调用都会走到这里，而"跨 await 释放锁"是链分叉的唯一成因。
        注意锁只包住这一段，中间**没有**多余的 await。
        """
        repo = _repo()
        async with self._lock:
            prev_hash = await repo.genesis_or_head_hash()
            doc = dict(payload)
            doc["prev_hash"] = prev_hash
            doc["hash"] = compute_hash(prev_hash, doc)
            doc["_id"] = await repo.next_id()
            await repo.insert(doc)
            return doc["_id"]

    # ---------------- 校验（§3.2，核心接口） ----------------
    async def verify_chain(self, *, from_seq: int = 0, limit: Optional[int] = None) -> dict:
        """真实逐条重算全链，返回首个不一致位置。

        **不是"只查长度"**（V-12-05 专门验证这一点）：每条记录都用
        `engine.audit_hash.compute_hash` 以它自己的 `prev_hash` 重新计算，
        再与库中 `hash` 比对；同时校验"本条 prev_hash 是否等于上一条的 hash"。
        """
        started = time.perf_counter()
        repo = _repo()
        # 固定校验终点：期间新写入的记录不参与，否则并发写入会被误报成篡改
        up_to_total = await repo.count()
        rows = await repo.scan_chain(from_seq=from_seq, limit=limit, up_to_total=up_to_total)

        expected_prev = GENESIS_HASH if from_seq == 0 else None
        broken: Optional[dict] = None
        checked = 0
        head_hash = ""
        for index, row in enumerate(rows):
            seq = from_seq + index + 1
            actual_hash = row.get("hash")
            recomputed = compute_hash(row.get("prev_hash"), row)
            prev_ok = True
            if expected_prev is not None:
                prev_ok = row.get("prev_hash") == expected_prev
            if recomputed != actual_hash or not prev_ok:
                broken = {
                    "log_id": row.get("_id"),
                    "ts": row.get("ts"),
                    "seq": seq,
                    "expected_hash": recomputed,
                    "actual_hash": actual_hash,
                    "expected_prev_hash": expected_prev,
                    "actual_prev_hash": row.get("prev_hash"),
                    "reason": "prev_hash 与上一条不连续" if not prev_ok else "内容哈希不匹配",
                }
                break
            expected_prev = actual_hash
            head_hash = actual_hash or ""
            checked += 1
            if (time.perf_counter() - started) * 1000 > VERIFY_BUDGET_MS:
                # AUD-5004：链太长，返回已校验段，支持 from_seq 分段继续
                return self._verify_result(
                    ok=None, checked=checked, total=up_to_total, broken=None,
                    head_hash=head_hash, started=started, truncated=True, from_seq=from_seq,
                )

        result = self._verify_result(
            ok=broken is None, checked=checked, total=up_to_total, broken=broken,
            head_hash=head_hash, started=started, truncated=False, from_seq=from_seq,
        )
        if broken is not None:
            # BR-12-13：发现篡改必须留痕（这条本身也进链），且**在校验完成后**再写，
            # 否则会把自己算进被校验的范围里
            log.error("审计链校验失败：%s（第 %s 条）", broken["log_id"], broken["seq"])
            await self.audit(
                actor="system", actor_role="system", action="audit.tamper_detected",
                target_type="audit", target_id=str(broken["log_id"]),
                after={k: broken[k] for k in ("seq", "expected_hash", "actual_hash", "reason")},
                strict=False,
            )
        return result

    @staticmethod
    def _verify_result(*, ok: Optional[bool], checked: int, total: int, broken: Optional[dict],
                       head_hash: str, started: float, truncated: bool, from_seq: int) -> dict:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        data: dict[str, Any] = {
            "ok": ok,
            "chain_length": checked,
            "chain_total": total,
            "genesis_hash": GENESIS_HASH,
            "head_hash": head_hash,
            "broken_at": broken,
            "elapsed_ms": elapsed_ms,
            "truncated": truncated,
        }
        if truncated:
            data["notice_code"] = "AUD-5004"
            data["next_from_seq"] = from_seq + checked
        elif broken is not None:
            data["notice_code"] = "AUD-5003"
        return data

    # ---------------- 查询（§3.1） ----------------
    async def query_logs(self, *, actor=None, action=None, target_type=None, target_id=None,
                         from_ms=None, to_ms=None, page: int = 1, page_size: int = 20) -> dict:
        from app.constants import PAGE_MAX, PAGE_SIZE_MAX

        if from_ms is not None and to_ms is not None and to_ms - from_ms > MAX_SPAN_DAYS * 86_400_000:
            # AUD-4003：跨度太大等于全表扫描，直接拒绝并提示缩小范围
            raise _aud("AUD-4003")
        if page < 1 or page > PAGE_MAX:
            raise _aud("AUD-4001", f"page 必须在 1~{PAGE_MAX} 之间")
        if not (1 <= page_size <= PAGE_SIZE_MAX):
            raise _aud("AUD-4001", f"page_size 必须在 1~{PAGE_SIZE_MAX} 之间")

        repo = _repo()
        flt = repo.build_filter(actor=actor, action=action, target_type=target_type,
                               target_id=target_id, from_ms=from_ms, to_ms=to_ms)
        rows, total = await repo.query(flt, page, page_size)
        return {
            "items": [_to_item(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0,
        }

    async def actors(self, limit: int = 100) -> list[str]:
        return await _repo().distinct_actors(limit)

    # ---------------- 导出（§3.3） ----------------
    async def export(self, *, fmt: str, actor_filter=None, action=None, target_type=None,
                     target_id=None, from_ms=None, to_ms=None,
                     actor: str = "system", actor_role: str = "") -> tuple[str, str]:
        """导出当前筛选结果为 CSV / Markdown，返回 `(文件名, 文本内容)`。

        BR-12-15：导出操作本身**也要审计**——否则"谁把数据导走了"无迹可查。
        """
        repo = _repo()
        flt = repo.build_filter(actor=actor_filter, action=action, target_type=target_type,
                               target_id=target_id, from_ms=from_ms, to_ms=to_ms)
        total = await repo.col.count_documents(flt)
        if total > EXPORT_LIMIT:
            raise _aud("AUD-4002", f"匹配 {total} 条，超过导出上限 {EXPORT_LIMIT} 条")
        rows = await repo.scan_filtered(flt, EXPORT_LIMIT)
        items = [_to_item(r) for r in rows]

        if fmt == "markdown":
            content = _to_markdown(items)
            filename = f"audit_{now_ms()}.md"
        else:
            content = _to_csv(items)
            filename = f"audit_{now_ms()}.csv"

        await self.audit(
            actor=actor, actor_role=actor_role, action="audit.export",
            target_type="audit", target_id=fmt,
            after={"count": len(items), "filters": {
                "actor": actor_filter, "action": action, "target_type": target_type,
                "target_id": target_id, "from": from_ms, "to": to_ms,
            }},
            strict=False,
        )
        return filename, content


# ============================================================
# 辅助
# ============================================================
def _repo() -> AuditRepo:
    """每次操作都重新取数据库句柄。

    **不缓存 repo**：测试会切换数据库（`db.use_database`），若在服务里缓存了
    某个库的集合句柄，测试就会把审计写到业务库里——这类污染极难发现。
    """
    return AuditRepo(db_module.get_db())


def _aud(code: str, message: Optional[str] = None) -> Exception:
    from app.errors import AUD_CODES, AppError

    status, default = AUD_CODES.get(code, (422, "参数不合法"))
    return AppError(code, message or default, status or 422)


def _to_item(row: dict) -> dict:
    """库文档 -> §3.1 的 items 结构（`_id` 对外叫 `log_id`）。"""
    return {
        "log_id": row.get("_id"),
        "ts": row.get("ts"),
        "actor": row.get("actor"),
        "actor_role": row.get("actor_role"),
        "action": row.get("action"),
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "before": row.get("before"),
        "after": row.get("after"),
        "ip": row.get("ip"),
        "ua": row.get("ua"),
        "hash": row.get("hash"),
        "prev_hash": row.get("prev_hash"),
        "hash_short": short_hash(row.get("hash")),
        "prev_hash_short": short_hash(row.get("prev_hash")),
    }


_CSV_FIELDS = ("log_id", "ts", "actor", "actor_role", "action", "target_type",
               "target_id", "before", "after", "ip", "hash", "prev_hash")


def _to_csv(items: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_CSV_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for it in items:
        row = dict(it)
        for key in ("before", "after"):
            row[key] = "" if it.get(key) is None else json.dumps(it[key], ensure_ascii=False,
                                                                 sort_keys=True)
        writer.writerow(row)
    return buf.getvalue()


def _to_markdown(items: list[dict]) -> str:
    from app.utils.timeutil import iso

    lines = [
        "# 审计流水导出",
        "",
        f"- 导出时间：{iso(now_ms())}",
        f"- 记录数：{len(items)}",
        "- 说明：审计日志为只读，哈希链可经 `/api/v1/audit/verify` 校验",
        "",
        "| 时间 | 操作人 | 角色 | 动作 | 目标 | 变更 | 本条哈希 |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in items:
        before_after = it.get("before"), it.get("after")
        summary = "—"
        if any(before_after):
            summary = (f"`{json.dumps(it.get('before'), ensure_ascii=False)}` → "
                       f"`{json.dumps(it.get('after'), ensure_ascii=False)}`")
        lines.append(
            f"| {iso(int(it['ts']))} | {it.get('actor')} | {it.get('actor_role')} | "
            f"`{it.get('action')}` | {it.get('target_id') or '—'} | {summary} | "
            f"`{it.get('hash_short')}` |"
        )
    return "\n".join(lines) + "\n"


# ============================================================
# 进程内单例 + 模块级便捷入口（§3.4 的 `audit(...)` 签名）
# ============================================================
_SERVICE = AuditService()


def get_audit_service() -> AuditService:
    return _SERVICE


async def audit(
    actor: str,
    actor_role: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    ip: Optional[str] = None,
    ua: Optional[str] = None,
    strict: bool = False,
) -> Optional[str]:
    """各业务模块调用审计的统一入口（§3.4）。"""
    return await _SERVICE.audit(
        actor=actor, actor_role=actor_role, action=action,
        target_type=target_type, target_id=target_id, before=before, after=after,
        ip=ip, ua=ua, strict=strict,
    )


async def start_consumer() -> None:
    await _SERVICE.start()


async def stop_consumer() -> None:
    await _SERVICE.stop()


async def flush(timeout: float = 5.0) -> bool:
    return await _SERVICE.flush(timeout)


__all__ = [
    "AuditService", "audit", "flush", "get_audit_service", "start_consumer", "stop_consumer",
    "COLL_AUDIT_LOGS", "MAX_PAGE_SIZE", "EXPORT_LIMIT", "MAX_SPAN_DAYS",
]
