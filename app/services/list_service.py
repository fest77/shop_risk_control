# -*- coding: utf-8 -*-
"""名单服务：承载本模块的**全部业务不变量**，不依赖 FastAPI，可注入假仓储单测。

已实现的业务规则（编号对齐模块 06 §4）：
  BR-06-20 唯一约束与异名单冲突（含 force 语义）
  BR-06-21 phone 维度入库前脱敏；**搜索时同样先脱敏再匹配**
  BR-06-22 默认有效期（黑/白永久、灰 30 天）
  BR-06-24 写入失败 fail-closed：置降级标记 + 返回 CFG-5002
  §3.5     列表分页 / 排序白名单 / 复合筛选 / counts / as_of
  §5.3     Mongo 不可用：读失败 503 COM-5001、写失败 503 CFG-5002

**不在本切片范围**（明确声明，避免被误认为已实现）：
  - 名单缓存**本体**（模块 05 的 TTL 缓存，BR-06-23）：本模块只保留并调用
    `invalidate_list_cache()` 这一**失效调用点**（模块 05 落地后实现其内部），
    绝不做第二套缓存。

## 模块 08 的调用契约（Spec 08 §3.2）

处置联动写入名单只有**一个**入口：`ListService.add_auto()`（BR-08-20~24），
配套的回滚是 `rollback_auto()`（BR-08-30）。08 **不得**直接写 `list_entries`
——两套写路径必然漂移，而漂移的表现是"页面处置过了、黑名单里没有"。
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import ValidationError

from app import constants
from app.core.degraded import DEGRADED
from app.errors import (
    AppError,
    AuditRollbackError,
    AutoSourceForbiddenError,
    ImportFormatError,
    InvalidEntityTypeError,
    InvalidQueryParamError,
    ListConflictError,
    ListEntryNotFoundError,
    ListVersionConflictError,
    ListWriteFailedError,
)
from app.logging import get_logger
from app.repos.list_repo import ListRepo, new_entry_id
from app.schemas.list_schema import (
    ENTITY_TYPES,
    ENTRY_STATUSES,
    IMPORT_ENCODINGS,
    IMPORT_ERROR_LIMIT,
    IMPORT_HEADER,
    IMPORT_MAX_ROWS,
    IMPORT_MODES,
    LIST_TYPES,
    CountsOut,
    ImportFailRow,
    ImportResult,
    ListEntryCreate,
    ListEntryOut,
    ListEntryRemoveResult,
    ListQueryResult,
    parse_import_list_type_default,
    parse_sort,
)
from app.services.audit_service import audit
from app.utils.mask import mask_phone
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.list")

GRAY_MS = constants.GRAY_DEFAULT_EXPIRE_DAYS * 24 * 3600 * 1000

#: 案件处置联动**只允许**写这两类名单（BR-08-20 的请求约束）。白名单是
#: "免风控"的授权，绝不能由处置链路自动授予——那是把一次拦截误操作
#: 升级成"以后全放行"的通道。
AUTO_LIST_TYPES: tuple[str, ...] = ("black", "gray")

_MSG_READ_UNAVAILABLE = "名单服务暂时不可用，请稍后重试"


# ============================================================
# 缓存失效调用点（BR-06-23 / BR-06-25）
# ============================================================
def invalidate_list_cache() -> None:
    """**失效调用点**：写入成功后必须让决策侧立刻看到变更（AD-02 / BR-06-23）。

    模块 05 落地后，这里做两件事：

    1. **直接清空 05 的进程内 TTL 缓存**（`app/engine/list_filter.LIST_CACHE`）。
       不清它，刚被拉黑的账号会继续按旧名单放行，最长到 TTL（默认 10s）为止——
       对"下单/支付"这类高频事件，10 秒足够跑掉一批单子。
    2. 记录失效时刻（`last_flush_at`）。05 的缓存也会比对这个时间戳自动整表失效
       （见 `ListEntryCache._sync_with_flush_mark`）——两条路互为兜底：将来若有
       人新增一条写路径却忘了调本函数，时间戳一变缓存照样失效。

    为什么写成**独立模块级函数**而不是内联在调用处：
    - 模块 05 落地时只改这一个函数体，不必在四个写路径里各找一处；
    - 测试可把它替换成抛异常的桩，从而**真实验证** BR-06-25 的回滚路径
      （内联写法下这条规则无法被证伪，等于没有实现）。

    **任何异常都必须向上抛**，绝不允许 `except: pass` 吞掉：吞掉就是
    "状态改了但缓存没失效"，决策侧会继续用旧名单放行——这是 fail-open。
    """
    # 局部导入：`list_filter` 会 import `list_repo`（属 06 侧），模块级导入会形成
    # 06 服务层 ↔ 05 引擎层的循环。这个函数本来就是低频写路径，一次导入可忽略。
    from app.engine.list_filter import clear_list_cache

    clear_list_cache()
    DEGRADED.last_flush_at = now_ms()


def _validate_list_type(value: str) -> str:
    v = (value or "").strip()
    if v not in LIST_TYPES:
        # 模块 06 §5.1 未定义 list_type 非法的错误码；本切片复用 CFG-4010
        # （同为"枚举维度取值非法"语义），并在交付说明中登记为 Spec 待补项。
        raise AppError(
            "CFG-4010", f"名单类型仅支持 black / white / gray，收到：{value}", 400
        )
    return v


def _validate_entity_type(value: str) -> str:
    v = (value or "").strip()
    if v not in ENTITY_TYPES:
        raise InvalidEntityTypeError(value)
    return v


def _decode_csv(content: bytes) -> str:
    """按 `utf-8-sig` → `utf-8` → `gbk` 顺序试解码。

    顺序有讲究：`utf-8-sig` 必须排在最前，否则 Excel「另存为 UTF-8 CSV」写入的
    BOM 会成为第一个列名的一部分（`\\ufefflist_type`），表头比对随即失败——
    症状是"我用官方模板导的却说表头不匹配"，用户完全无从下手。
    `gbk` 是 Windows 中文 Excel 的默认另存编码，不兼容它等于把最常见的
    国产办公流程排除在外。
    """
    for enc in IMPORT_ENCODINGS:
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("文件编码无法识别，请另存为 UTF-8 或 GBK 编码的 CSV")


def _parse_import_csv(content: bytes) -> list[tuple[int, dict[str, str]]]:
    """解析导入 CSV，返回 `[(文件行号, {列名: 去空白后的值})]`（不含表头）。

    只做"文件级"判定（空文件 / 表头不匹配），**行的字段合法性留给逐行校验**：
    两者混在一起会让 `atomic` 模式无法区分"文件本身就不对"（该整批 400）
    与"其中 3 行实体类型不合法"（该整批不写但仍返回 200 + 错误明细）。
    """
    if not content or not content.strip():
        raise ImportFormatError("文件为空，请使用下载的模板填写后导入")

    text = _decode_csv(content)
    rows = list(csv.reader(io.StringIO(text, newline="")))
    # 去掉完全空白的行（Excel 常在末尾留下空行，不该算作"数据行"）
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        raise ImportFormatError("文件为空，请使用下载的模板填写后导入")

    header = [(c or "").strip().lstrip("\ufeff").lower() for c in rows[0]]
    if header != list(IMPORT_HEADER):
        # 严格比对列名与顺序：模板由 `/lists/import-template` 提供、与这里共用
        # 同一个常量，因此合法用户**不可能**遇到这条错误。
        # 放松为"按列名取用"会引入一个新问题：用户从别处抄来的表头看着像但不完全
        # 一致（如 `phone` 写成 `mobile`），按列名取用会静默丢列，导入看似成功
        # 实则名单缺项——静默错数据比明确报错危险得多。
        raise ImportFormatError(
            f"表头不匹配，期望：{','.join(IMPORT_HEADER)}；实际：{','.join(header)}"
        )

    out: list[tuple[int, dict[str, str]]] = []
    for index, cells in enumerate(rows[1:], start=2):  # 行号从 2 起：1 是表头
        record = {
            name: ((cells[i] if i < len(cells) else "") or "").strip()
            for i, name in enumerate(IMPORT_HEADER)
        }
        out.append((index, record))
    return out


def _build_row_payload(
    raw: dict[str, str], default_list_type: Optional[str], force: bool
) -> tuple[Optional[ListEntryCreate], Optional[str]]:
    """把一行 CSV 变成 `ListEntryCreate`，返回 `(payload, 失败原因)`。

    复用 `ListEntryCreate` + `create_entry` 而不是自己写一套行级校验：
    唯一约束、脱敏、默认有效期、force 语义全在服务层，两套实现必然漂移
    （典型症状：页面新增会脱敏，导入却把明文手机号写进库）。这里只负责
    "文件字段 → 请求模型字段"的翻译，以及把 Pydantic 的报错压成一句中文。
    """
    data: dict[str, Any] = {
        "list_type": raw.get("list_type", "") or (default_list_type or ""),
        "entity_type": raw.get("entity_type", ""),
        "entity_value": raw.get("entity_value", ""),
        "reason": raw.get("reason", ""),
        "force": force,
    }
    expire_raw = raw.get("expire_at", "")
    if expire_raw:
        # 只接受纯整数毫秒时间戳。放过 "2026-01-01" 这类写法会变成静默的语义分歧
        # （用户以为按日期算，实际存进去的是别的时刻），宁可报错让他填时间戳。
        try:
            data["expire_at"] = int(expire_raw)
        except ValueError:
            return None, f"expire_at 必须是毫秒时间戳整数，收到：{expire_raw}"
    else:
        data["expire_at"] = None  # 留空 = 走 BR-06-22 的默认有效期

    try:
        payload = ListEntryCreate(**data)
    except ValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ())) or "行"
        return None, f"{field}：{first.get('msg', '非法取值')}"

    # 枚举维度在**模型层是宽松的 str**（见 list_schema 的模块说明：用 Enum 会被
    # Pydantic 先拦成 COM-4001，拿不到契约要求的 CFG-4010）。因此这里必须显式调用
    # 与 `create_entry` **同一个**校验函数——否则导入会把 `imei` 这种非法实体类型
    # 带进服务层，等到落库时才报错，且 `atomic` 模式失去了"先全量校验"的依据。
    try:
        _validate_list_type(payload.list_type)
        _validate_entity_type(payload.entity_type)
    except AppError as e:
        return None, e.message
    return payload, None


def _build_result(
    total: int,
    success: int,
    failures: list[ImportFailRow],
    mode: str,
    imported_ids: list[str],
    *,
    truncated: bool,
) -> ImportResult:
    """组装导入响应。`failed` 用 `total - success` 而不是 `len(failures)`：
    错误明细会被截断到 200 条，但"失败了多少行"这个数字不能被截断带偏。"""
    return ImportResult(
        total=total,
        success=success,
        failed=total - success,
        mode=mode,
        rows=failures[:IMPORT_ERROR_LIMIT],
        imported_ids=imported_ids,
        rows_truncated=truncated,
    )


def _keyword_clauses(keyword: str) -> list[dict]:
    """构造 entity_value 的匹配条件。

    BR-06-21 要求「搜索时同样先脱敏再匹配」：用户手里的往往是**明文手机号**，
    而库里存的是 `139****0001`。因此当关键词形如明文手机号时，同时尝试脱敏后的形态。

    始终用 `re.escape` 转义，避免关键词里的正则元字符被当表达式执行。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    variants = {kw}
    if kw.isdigit() and len(kw) >= 11:
        variants.add(mask_phone(kw))
    return [{"entity_value": {"$regex": re.escape(v)}} for v in sorted(variants)]


class ListService:
    def __init__(self, repo: ListRepo):
        self.repo = repo

    # ---------------- 查询 ----------------
    async def list_entries(
        self,
        *,
        list_type: str,
        entity_type: Optional[str] = None,
        keyword: Optional[str] = None,
        status: str = "active",
        expire_before: Optional[int] = None,
        page: int = 1,
        page_size: int = constants.PAGE_SIZE_DEFAULT,
        sort: str = "effective_at:desc",
    ) -> ListQueryResult:
        list_type = _validate_list_type(list_type)

        # 分页/排序的边界一律在服务层判定，保证错误码是契约要求的 CFG-4008
        # （若交给 FastAPI 的 Query(ge=1)，会先被通用校验拦成模块 00 的 COM-4001，
        #  丢掉"分页参数越界"这一具体语义）
        if page < 1:
            raise InvalidQueryParamError("page 必须 ≥ 1")
        if page > constants.PAGE_MAX:
            raise InvalidQueryParamError(f"page 不能超过 {constants.PAGE_MAX}")
        if not (1 <= page_size <= constants.PAGE_SIZE_MAX):
            raise InvalidQueryParamError(
                f"page_size 必须在 1~{constants.PAGE_SIZE_MAX} 之间"
            )
        try:
            sort_pairs = parse_sort(sort)
        except ValueError as e:
            raise InvalidQueryParamError(str(e)) from e

        flt: dict[str, Any] = {"list_type": list_type}
        if status != "all":
            if status not in ENTRY_STATUSES:
                raise InvalidQueryParamError(
                    f"status 仅支持 {'/'.join(ENTRY_STATUSES)} 或 all"
                )
            flt["status"] = status
        if entity_type:
            flt["entity_type"] = _validate_entity_type(entity_type)

        clauses = _keyword_clauses(keyword or "")
        if len(clauses) == 1:
            flt.update(clauses[0])
        elif clauses:
            flt["$or"] = clauses

        if expire_before is not None:
            flt["expire_at"] = {"$ne": None, "$lte": int(expire_before)}

        skip = (page - 1) * page_size
        try:
            total = await self.repo.count(flt)
            docs = await self.repo.query(flt, sort_pairs, skip, page_size)
            counts = await self.repo.counts_by_list_type()
        except PyMongoError as e:
            # 概要设计 §5.3：Mongo 不可用属于服务不可用 -> 503（不是 500）。
            # 错误码取模块 00 的 COM-5001（"数据库连接失败"）：模块 06 的 CFG-5001
            # 语义是"**写规则**失败"，借它表示读失败会造成同码两义（违背 BR-00-13）。
            # ER-02 要求引用其他模块的码时保持原前缀，故此处就是 COM- 前缀。
            raise AppError("COM-5001", _MSG_READ_UNAVAILABLE, 503) from e

        return ListQueryResult(
            items=[ListEntryOut.from_doc(d) for d in docs],
            total=total,
            page=page,
            page_size=page_size,
            # 向上取整；total=0 时 pages=0（前端据此走空态而不是"第 1/1 页"）
            pages=(total + page_size - 1) // page_size if total else 0,
            counts=CountsOut(**counts),
            as_of=now_ms(),
        )

    # ---------------- 新增 ----------------
    async def create_entry(
        self, payload: ListEntryCreate, operator: str, *,
        actor_role: str = "", ip: Optional[str] = None, ua: Optional[str] = None,
    ) -> ListEntryOut:
        """新增一条名单（`POST /lists` 的唯一入口）。

        **BR-06-36**：成功的写操作必须"**恰好**留下一条"审计；留不下就整体回滚
        （宁可不做，不可无痕地做）。

        因此这里把「插入」与「审计」分成两步：插入复用 `_insert_entry_only`，
        而导入路径也用同一个插入方法、自己写自己的审计。若把审计直接塞进插入方法，
        导入就会**每行两份审计**（一份来自插入方法、一份来自导入循环）——
        这正是"恰好一条"最容易破的地方。
        """
        entry = await self._insert_entry_only(payload, operator)
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="list.add",
                target_type="list_entry", target_id=entry.id,
                before=None, after=entry.model_dump(by_alias=True),
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            await self._compensate_insert(entry.id)
            log.error("list.add 审计写入失败，已撤销新增 entry_id=%s：%s", entry.id, e)
            raise AuditRollbackError("list.add", type(e).__name__) from e
        return entry

    async def _insert_entry_only(
        self, payload: ListEntryCreate, operator: str
    ) -> ListEntryOut:
        """只做业务不变量与落库，**不写审计**（审计由调用方按场景决定时机）。

        调用方：
        - `create_entry`（单条新增）→ 写一条 `list.add`
        - `import_entries`（批量导入）→ 逐行写 `list.add`，失败则撤销该行
        """
        list_type = _validate_list_type(payload.list_type)
        entity_type = _validate_entity_type(payload.entity_type)

        raw_value = payload.entity_value          # 模型层已 strip 并拒绝空白
        # BR-06-21：phone 维度强制脱敏后再入库，且与 05 名单过滤共用同一函数
        entity_value = mask_phone(raw_value) if entity_type == "phone" else raw_value

        # BR-06-22：默认有效期
        if payload.expire_at is not None:
            expire_at: Optional[int] = int(payload.expire_at)
        elif list_type == "gray":
            expire_at = now_ms() + GRAY_MS
        else:
            expire_at = None  # 黑 / 白名单默认永久

        ts = now_ms()
        doc = {
            "_id": new_entry_id(),
            "list_type": list_type,
            "entity_type": entity_type,
            "entity_value": entity_value,
            "reason": payload.reason,             # 模型层已 strip
            "source": "manual",
            "related_case_no": None,
            "effective_at": ts,
            # 说明：模块 06 §3.2 的响应契约包含 created_at，而 E07 字段表未列该字段。
            # 本切片按响应契约落库（与 effective_at 同值），并登记为 Spec 不一致待确认项。
            "created_at": ts,
            "expire_at": expire_at,
            "status": "active",
            "operator": operator,
        }

        try:
            # BR-06-20 ①：同名单类型内重复 -> 明确提示
            if await self.repo.find_active(list_type, entity_type, entity_value):
                raise ListConflictError(
                    f"该实体已存在于本名单（{list_type} / {entity_type} / {entity_value}）"
                )

            # BR-06-20 ②：同实体已在另一名单类型 -> 默认拒绝，force=true 才写入
            others = await self.repo.find_active_other_lists(
                entity_type, entity_value, list_type
            )
            if others and not payload.force:
                other_types = "、".join(sorted({o["list_type"] for o in others}))
                raise ListConflictError(
                    f"该实体已在「{other_types}」名单中；黑白名单同时命中时黑名单优先"
                    f"（悬空点 G-04 当前取值）。确认要同时加入「{list_type}」名单，"
                    f"请带 force=true 重试。"
                )

            await self.repo.insert(doc)
        except DuplicateKeyError as e:
            # 并发下的兜底：应用层「先查再插」必然漏判，唯一索引才是真正的约束
            raise ListConflictError(
                "该实体已存在于本名单（并发写入被唯一索引拦截）"
            ) from e
        except PyMongoError as e:
            # BR-06-24 / §5.3：写入失败绝不静默吞掉
            DEGRADED.mark(str(e))
            raise ListWriteFailedError(type(e).__name__) from e

        # 写成功视为数据库恢复健康，清除降级标记
        DEGRADED.clear()
        # BR-06-23：新增成功后立即失效名单缓存。放在 `DEGRADED.clear()` 之后，
        # 保证"失效时刻"不会把降级标记一起清掉（两者语义不同，不可互相覆盖）。
        invalidate_list_cache()
        return ListEntryOut.from_doc(doc)

    # ---------------- 移除（BR-06-25 / 27 / 36） ----------------
    async def remove_entry(
        self,
        entry_id: str,
        operator: str,
        *,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> ListEntryRemoveResult:
        """软删一条名单条目。顺序固定为：**先软删标记 → 再失效缓存 → 再写审计**。

        顺序为什么不能换（BR-06-25 的实质）：
        - 先失效缓存再改状态：缓存清空到状态落库之间存在一个窗口，此刻决策侧
          回源查库仍会看到 `active`，会**继续拦截/放行**——等于这次移除没生效，
          而页面显示成功。窗口虽小，但风控判定的正确性不接受这种窗口。
        - 先改状态再清缓存：若清缓存失败，决策侧可能仍命中旧缓存。因此这里
          **失败就把状态改回 `active`**——要么两边都生效，要么两边都不动，
          不允许出现"状态改了但缓存没失效"或反之。
        """
        current = await self.repo.find_by_id(entry_id)
        if current is None:
            raise ListEntryNotFoundError(entry_id)
        # BR-06-27：auto 条目必须回 08 处置模块留痕。**这条先于状态判定**——
        # 对 auto 条目，即使它已不是 active，也不该告知调用方"状态冲突可重试"，
        # 而应直接引导去处置模块（否则用户会反复刷新重试一个永远失败的按钮）。
        if current.get("source") == "auto":
            raise AutoSourceForbiddenError(entry_id)

        ts = now_ms()
        try:
            matched = await self.repo.soft_remove(entry_id, ts, operator)
        except PyMongoError as e:
            # 软删本身写不进去：条目原样保持 active，属 CFG-5002 的 fail-closed
            DEGRADED.mark(str(e))
            raise ListWriteFailedError(type(e).__name__) from e
        if matched == 0:
            # 读到它之后状态被别处改过（并发移除 / 清理任务置 expired）
            raise ListVersionConflictError(entry_id, f"当前状态：{current.get('status')}")

        # ② 失效缓存。失败 -> 回滚软删（BR-06-25）
        try:
            invalidate_list_cache()
        except Exception as e:  # noqa: BLE001 - 任何失效失败都必须回滚，不能吞
            await self._rollback_removal(entry_id)
            log.error("名单缓存失效失败，已回滚软删 entry_id=%s：%s", entry_id, e)
            raise ListWriteFailedError(f"缓存失效失败：{type(e).__name__}") from e

        # ③ 审计（BR-06-36：写不进去就不算做过）
        before = {"status": current.get("status")}
        after = {"status": "removed", "removed_at": ts, "removed_by": operator}
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="list.remove",
                target_type="list_entry", target_id=entry_id,
                before=before, after=after, ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled_back = await self._rollback_removal(entry_id)
            log.error("list.remove 审计写入失败，回滚软删 entry_id=%s：%s", entry_id, e)
            hint = "" if rolled_back else "；且回滚亦失败，条目可能仍为 removed，请人工核对"
            raise AuditRollbackError(
                "list.remove", f"{type(e).__name__}{hint}"
            ) from e

        try:
            remaining = await self.repo.count_active_by_entity(
                current["entity_type"], current["entity_value"]
            )
        except PyMongoError:
            # 影响面只是给页面刷新用的附加信息，取不到不应让一次**已经成功且已留痕**
            # 的移除变成失败——那会诱导用户重试，而重试必然撞 CFG-4006。
            remaining = -1

        return ListEntryRemoveResult(
            id=entry_id,
            list_type=current["list_type"],
            entity_type=current["entity_type"],
            entity_value=current["entity_value"],
            source=current.get("source") or "manual",
            status="removed",
            removed_at=ts,
            removed_by=operator,
            remaining_active=remaining,
        )

    async def _rollback_removal(self, entry_id: str) -> bool:
        """把软删改回 `active`。返回是否回滚成功。

        回滚失败**不再抛替代异常**（调用方已经在处理审计/缓存故障，再抛一个会把
        真因覆盖掉），但必须置降级标记并 error 日志：此时库里可能留下一条
        "已 removed 却没有审计"的条目，运维需要据此排查。
        """
        try:
            restored = await self.repo.restore_active(entry_id)
        except PyMongoError as e:
            DEGRADED.mark(f"回滚软删失败：{e}")
            log.error("回滚软删失败 entry_id=%s：%s", entry_id, e)
            return False
        if restored == 0:
            # 状态已不是 removed：可能被并发改成了 expired，属"别人的变更"，
            # 不该由本次请求覆盖。如实返回失败，让调用方在响应里提示人工核对。
            log.error("回滚软删匹配 0 条（状态已被改变）entry_id=%s", entry_id)
            return False
        return True

    # ---------------- 批量导入（BR-06-28 / 29 / 30） ----------------
    async def import_entries(
        self,
        content: bytes,
        *,
        mode: str = "partial",
        force: bool = False,
        default_list_type: Optional[str] = None,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> ImportResult:
        """CSV 批量导入。

        **行号口径**：`row` 一律是**文件内真实行号**，表头是第 1 行，因此数据行
        从第 2 行开始。用真实行号而不是"第 N 条数据"，是为了让用户在 Excel 里
        直接按行号定位——报"第 3 条数据错了"还要自己 +1，是最容易被抱怨的体验。
        """
        if mode not in IMPORT_MODES:
            raise ImportFormatError(f"mode 仅支持 {'/'.join(IMPORT_MODES)}，收到：{mode}")
        default_type = parse_import_list_type_default(default_list_type)

        # ① 解析（表头/空文件/超限都在这步出结果）
        parsed = _parse_import_csv(content)
        total = len(parsed)
        if total > IMPORT_MAX_ROWS:
            # BR-06-30：超限直接拒绝并提示分批。不静默截断——截断会让用户以为
            # "剩下的也导进去了"，而名单少一条就等于少拦一个实体。
            raise ImportFormatError(
                f"单次导入上限 {IMPORT_MAX_ROWS} 行，本次 {total} 行，请拆分后分批导入"
            )

        # ② 逐行校验：`atomic` 要靠这一步先拿到全量结论，才谈得上"整批不写"
        prepared: list[tuple[int, Optional[ListEntryCreate], Optional[str]]] = []
        # 行号 -> 原始 entity_value：即使某行**整体**校验不过（连 entity_value 都
        # 缺了），错误明细里也要能回显它，否则用户拿到"第 7 行不合法"却不知道是哪条
        raw_values: dict[int, str] = {}
        for row_no, raw in parsed:
            raw_values[row_no] = raw.get("entity_value", "")
            payload, reason = _build_row_payload(raw, default_type, force)
            prepared.append((row_no, payload, reason))

        failures: list[ImportFailRow] = []
        valid: list[tuple[int, ListEntryCreate]] = []
        for row_no, payload, reason in prepared:
            if payload is None:
                failures.append(
                    ImportFailRow(row=row_no, entity_value=raw_values[row_no],
                                  reason=reason or "数据不合法")
                )
            else:
                valid.append((row_no, payload))

        if mode == "atomic" and failures:
            # 整批不写：一条都不落库，返回**全部**错误行（让用户一次改完）
            return _build_result(total, 0, failures, "atomic", [], truncated=False)

        # ③ 落库。atomic 走到这里说明全量校验已通过；partial 则逐行独立成败。
        imported_ids: list[str] = []
        for row_no, payload in valid:
            try:
                entry = await self._insert_entry_only(payload, operator=operator)
            except AppError as e:
                # 逐行失败不打断整批：这正是 partial 模式的语义（BR-06-28）
                failures.append(
                    ImportFailRow(row=row_no, entity_value=payload.entity_value,
                                  reason=e.message)
                )
                continue
            except Exception as e:  # noqa: BLE001 - 单行未知异常同样只废掉这一行
                failures.append(
                    ImportFailRow(row=row_no, entity_value=payload.entity_value,
                                  reason=f"写入失败：{type(e).__name__}")
                )
                continue
            # BR-06-36：每个成功行写一条 list.add 审计（before=null，after=条目）
            try:
                await audit(
                    actor=operator, actor_role=actor_role, action="list.add",
                    target_type="list_entry", target_id=entry.id,
                    before=None, after=entry.model_dump(by_alias=True),
                    ip=ip, ua=ua, strict=True,
                )
            except AppError as e:
                # 审计落不下去 -> 这一行**必须撤销**（"宁可不做，不可无痕地做"）。
                # 逐行撤销而不是整批报 503：已留痕的那些行是真实且合规的，
                # 把它们一起算失败反而会让用户以为"一行都没进去"而重复导入。
                await self._compensate_insert(entry.id)
                failures.append(
                    ImportFailRow(row=row_no, entity_value=payload.entity_value,
                                  reason=f"审计写入失败，该行已回滚（{e.code}）")
                )
                continue
            imported_ids.append(entry.id)

        success = len(imported_ids)
        if success:
            # 缓存失效在**批末做一次**：逐行调用没有额外正确性收益（缓存本来就是
            # 粗粒度整体失效），却会把 5000 行放大成 5000 次失效开销。
            try:
                invalidate_list_cache()
            except Exception as e:  # noqa: BLE001
                # 已落库 5000 行时"整批回滚"比"缓存没失效"危害更大（那会让用户
                # 丢数据却看到失败）。此处只能置降级 + 大声告警，让决策侧回源。
                DEGRADED.mark(f"导入后缓存失效失败：{e}")
                log.error("导入后缓存失效失败，已置降级：%s", e)

        failures.sort(key=lambda f: f.row)
        truncated = len(failures) > IMPORT_ERROR_LIMIT
        return _build_result(
            total, success, failures, mode, imported_ids, truncated=truncated
        )

    async def _compensate_insert(self, entry_id: str) -> None:
        """审计失败后的补偿：物理删掉刚插入的那一行。

        这里用**物理删除**而不是软删：软删会留下一条 `removed` 的孤儿文档，
        而这次写入本就不该发生（业务上等于"从未导入"）。物理删除才是真正的
        回滚语义；失败只记日志（补偿失败不改变"该行未生效"的对外结论，
        但需要运维可见）。
        """
        try:
            await self.repo.delete_by_id(entry_id)
        except PyMongoError as e:
            DEGRADED.mark(f"导入补偿删除失败：{e}")
            log.error("导入补偿删除失败 entry_id=%s：%s", entry_id, e)

    async def impact_of_entity(self, entity_type: str, entity_value: str) -> int:
        """移除确认弹窗所需的「关联名单条数」（BR-06-26 影响面）。"""
        _validate_entity_type(entity_type)
        value = mask_phone(entity_value) if entity_type == "phone" else entity_value
        try:
            return await self.repo.count_active_by_entity(entity_type, value)
        except PyMongoError as e:
            # 同上：读失败统一用模块 00 的 COM-5001（ER-02 保持原前缀）
            raise AppError("COM-5001", _MSG_READ_UNAVAILABLE, 503) from e

    # ---------------- 案件处置的自动写入（模块 08 → 06，Spec 08 §3.2） ----------------
    async def add_auto(
        self,
        *,
        list_type: str,
        entity_type: str,
        entity_value: str,
        reason: str,
        related_case_no: str,
        operator: str = "system",
        effective_at: Optional[int] = None,
        expire_at: Optional[int] = None,
    ) -> dict:
        """**处置联动写入名单**（BR-08-20 ~ 08-24），返回写入结果。

        这是 08 处置唯一允许的名单写入入口（08 不得自己写 `list_entries`）。
        返回 `{entry_id, list_type, entity_type, entity_value, reused, effective_at}`；
        `reused=True` 表示命中了 BR-08-22 的复用路径（**没有新增**条目）。

        ## 四条业务规则逐条落地

        - **BR-08-21**：`source="auto"`、`related_case_no=案件号`、
          `operator="system"`、`effective_at=now`、`expire_at` 缺省 `None`（永久）。
        - **BR-08-22**：同 `list_type+entity_type+entity_value` 已有 `active` 条目
          → **不新增**，改为刷新 `reason` / `related_case_no` / `effective_at`，
          并返回**既有** `entry_id`（唯一索引 `uq_active_entry` 本来也不允许重复）。
        - **BR-08-23**：空 `entity_value` 一律拒绝（"跳过"的判定在 08 侧完成，
          08 不得把空值传进来——写进去会得到一条"谁都拦不住却在页面上看起来正常"的黑名单）。
        - **BR-08-24**：写入成功后**立即失效 AD-02 的名单缓存**，使处置即时生效，
          而不是等最多 10s 的 TTL。

        ## 刻意不做的一件事：跨名单类型冲突**不拒绝**

        `create_entry`（人工新增）遇到"该实体已在另一名单类型中"会拒绝并提示
        `CFG-4009`（除非 `force=true`）。这里**不适用**：处置写入是
        **风控结论的落地**，而 G-04 已裁定"黑白同时命中时黑名单优先"——
        若因为"该用户在白名单里"就拒绝写黑名单，等于让一张人工白名单**否掉**
        一次人工研判，且处置会以 `DSP-5002` 整体失败回滚。因此这条路径直接写入，
        由判定侧的优先级规则（BR-08-25）处理并存关系。

        ## 为什么**不写审计**

        D41 / BR-08-36 的"每次成功的写操作**恰好一条**审计"以**一次对外操作**
        为单位：这次名单写入是 `/cases/{no}/dispose` 这一次操作的第 ② 步，
        它的留痕在 `case.dispose` 那一条审计的 `after.list_writes` 里
        （含 `entry_id` / `list_type` / `entity_value`），再加上
        `list_entries.related_case_no` 的反查，追溯链是完整的。
        在这里再写一条 `list.add` 会让**一次处置产生 N+1 条审计**，
        恰恰破坏"恰好一条"。

        ⚠️ 这与 Spec 08 的 `V-08-12`（"断言 `audit_logs` 存在 `case.dispose`
        与 `list.add` 记录"）**不一致**，已在交付报告中登记：V-08-12 衡量的是
        "联动写入也留了痕"，而本次裁定（D41 的"恰好一条"）把它落在
        `case.dispose` 的同一条记录里。若评审要求按 V-08-12 逐字实现，
        只需在本方法内加一条 `audit(action="list.add", strict=True)` 与失败补偿。
        """
        list_type = _validate_list_type(list_type)
        if list_type not in AUTO_LIST_TYPES:
            # 白名单是"免风控"的授权，**绝不能**由处置链路自动授予
            # （BR-08-20 的请求约束只允许 black / gray 覆盖）
            raise AppError(
                "CFG-4009",
                f"处置联动只能写 {'/'.join(AUTO_LIST_TYPES)} 名单，收到：{list_type}",
                409,
            )
        entity_type = _validate_entity_type(entity_type)

        raw_value = str(entity_value or "").strip()
        if not raw_value:
            # 见 BR-08-23：空值必须在上游跳过，能传到这里就是调用方的缺陷。
            # 用 COM-4001（参数校验失败）而不是 5xx：这是调用方写错了，
            # 归因到"名单服务不可用"会把一个编程错误变成一次线上告警。
            raise AppError(
                "COM-4001",
                f"参数校验失败：{entity_type} 维度的 entity_value 不能为空",
                422,
                {"errors": [{"path": "entity_value", "message": "不能为空"}]},
            )
        # BR-06-21：phone 维度入库前脱敏（与人工新增、与 05 的名单过滤共用同一函数）
        value = mask_phone(raw_value) if entity_type == "phone" else raw_value

        moment = now_ms()
        effective = int(effective_at if effective_at is not None else moment)
        expire = None if expire_at is None else int(expire_at)

        try:
            existing = await self.repo.find_active(list_type, entity_type, value)
            if existing is not None:
                touched = await self.repo.touch_auto(
                    existing["_id"], reason=reason, related_case_no=related_case_no,
                    effective_at=effective, operator=operator,
                )
                if touched == 0:
                    # 读它到改它之间被清理任务置成 expired：如实报失败，由 08
                    # fail-closed 回滚；**不**把它硬改回 active（见 touch_auto 的说明）
                    raise ListWriteFailedError(
                        f"既有条目 {existing['_id']} 已不是 active，无法复用"
                    )
                invalidate_list_cache()
                return {
                    "entry_id": str(existing["_id"]),
                    "list_type": list_type, "entity_type": entity_type,
                    "entity_value": value, "reused": True, "effective_at": effective,
                }

            entry_id = new_entry_id()
            doc = {
                "_id": entry_id,
                "list_type": list_type,
                "entity_type": entity_type,
                "entity_value": value,
                "reason": str(reason),
                "source": "auto",
                "related_case_no": str(related_case_no),
                "effective_at": effective,
                "created_at": moment,
                "expire_at": expire,
                "status": "active",
                "operator": str(operator),
            }
            try:
                await self.repo.insert(doc)
            except DuplicateKeyError:
                # 并发下的兜底：应用层"先查再插"必然漏判，唯一索引才是真约束。
                # 与 BR-08-22 同语义——回读那一条并复用它，而不是报错。
                again = await self.repo.find_active(list_type, entity_type, value)
                if again is None:
                    raise
                await self.repo.touch_auto(
                    again["_id"], reason=reason, related_case_no=related_case_no,
                    effective_at=effective, operator=operator,
                )
                invalidate_list_cache()
                return {
                    "entry_id": str(again["_id"]),
                    "list_type": list_type, "entity_type": entity_type,
                    "entity_value": value, "reused": True, "effective_at": effective,
                }
        except PyMongoError as e:
            # BR-06-24 / BR-08-30：写入失败绝不静默吞掉。08 收到它之后必须
            # fail-closed（回滚本次已写的条目 + 案件保持 reviewing）
            DEGRADED.mark(str(e))
            raise ListWriteFailedError(f"{type(e).__name__}: {e}") from e

        DEGRADED.clear()
        # BR-08-24：写入成功后立即失效名单缓存，处置即时生效
        invalidate_list_cache()
        return {
            "entry_id": entry_id,
            "list_type": list_type, "entity_type": entity_type,
            "entity_value": value, "reused": False, "effective_at": effective,
        }

    async def rollback_auto(self, entry_id: str, operator: str) -> bool:
        """**系统回滚**一条处置写入的名单条目（BR-08-30 的补偿动作）。

        为什么必须存在：BR-08-30 要求"名单写入失败 → 回滚本次已写入的名单条目
        （按本次返回的 `entry_id` 置 `status=removed`）"。而人工侧的
        `remove_entry` **刻意拒绝** `source=auto`（`CFG-4032`：必须回处置模块
        操作才留得下痕迹）——那条守卫针对的是**人工移除**，本方法正是它所指的
        "处置模块的留痕路径"，因此这里显式绕过 `source=auto` 守卫。

        ## 三个刻意的取舍

        1. **软删而不是物理删**：Spec 原文是"置 `status=removed`"。留一条
           `removed` 的文档才能回答"那次失败的处置到底动过什么"。
        2. **不写审计**：本次处置**失败**了——它的 `case.dispose` 审计（步骤 ⑥）
           根本没走到，因此不存在"需要被补偿的审计"。在这里额外写一条回滚审计
           反而会让审计链里出现一条**没有对应成功操作**的记录。
        3. **失败不抛**（返回 `False`）：调用方已经在处理失败路径，再抛一个异常
           会把真因盖住；但必须让调用方把 `rolled_back=false` 如实回传给页面，
           由页面提示人工核对——**绝不能显示"已回滚"**。
        """
        try:
            removed = await self.repo.soft_remove(entry_id, now_ms(), str(operator))
        except PyMongoError as e:
            DEGRADED.mark(f"处置回滚名单条目失败：{e}")
            log.error("处置回滚名单条目失败 entry_id=%s：%s", entry_id, e)
            return False
        if removed:
            try:
                invalidate_list_cache()
            except Exception as e:  # noqa: BLE001 - 回滚已经生效，缓存失效失败只降级
                DEGRADED.mark(f"回滚后名单缓存失效失败：{e}")
                log.error("回滚后名单缓存失效失败（已置降级）entry_id=%s：%s", entry_id, e)
        return bool(removed)
