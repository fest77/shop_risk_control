# -*- coding: utf-8 -*-
"""案件查询编排（模块 07 §3.1 列表 / §3.2 详情，**只读**）。

## 本文件只做三件事

| 做 | 不做 |
|---|---|
| 列表：多维筛选 + 分页 + 排序（`total` 由接口给出） | 建案 / 认领 / 处置（归 08，本文件**没有一处写库**） |
| 详情：并发取 08/03/04/05/09 的只读能力，组装**一次响应驱动三栏** | 重算分值、重定等级、算基线、做统计（全部读别人的结论） |
| 详情：`degraded_parts` 分区降级（BR-07-12 / §5.2） | 跨集合实时聚合（BR-07-27：不得对 `decision_hits`/`metric_buckets` 实时聚合） |

## 为什么列表必须"按 status 逐组查询"而不是 `$in`

BR-07-27 要求列表命中 `risk_cases` 的复合索引
**`status + risk_level + created_at desc`**（`constants.INDEX_SPECS` 的
`ix_status_level_created`）。MongoDB 的复合索引对**范围/集合**条件之后的前缀
字段就退化成"过滤而不是定位"：`status: {$in: [...]}` 用不上 `risk_level` 的
有序前缀，排序会退化成内存排序（`SORT` 阶段），深翻页时把整个候选集读进内存。

因此 `status` 多值（OR）时**逐值各查一次**，每个查询仍然是
`status = 常量` → 索引前缀成立 → `skip/limit` 在索引上走。这也解释了
`_page_slice` 的存在：逐组查询各自带自己的 `skip` 才能拼出**一整页**。

同理 `count_documents` 也逐组计数：`total` 必须是**索引能算出来的**
真实总数，而不是"把候选集拉回来数一数"。

## 为什么 `total` 一定来自本服务而不是调用方

BR-07-05b：顶部「共 N 条待审」的 N 必须来自列表接口的 `total`，
**禁止前端对当前页自行计数**。`total` 与 `items` 在同一个响应里返回，
中间没有第二个数据源——这是"看到 N 条就是 N 条"的唯一保障。

## 详情为什么"一次响应"（BR-07-06）

原型注记第 1 条要求三栏同帧更新、不得异步错位。若中栏/右栏各自发请求，
两个响应到达顺序不定，就会出现"A 的画像配 B 的判定摘要"这种**审错案件**的
中间态。因此详情把 8 块数据（案件/事件/快照/决策/命中/画像/基线/图谱）
一次性取齐、一次性返回；`BR-07-09` 的"服务端串号兜底"由响应里的
`case.case_no` 承担（前端比对当前选中案件号）。

## 分区降级（BR-07-12 / §5.2 的红线）

每一块**各自失败各自降级**，失败者只进 `degraded_parts` 并把该块置 `null`，
其余块照常返回——**不得整页失败**，也**不得静默隐藏**失败卡片
（"部分失败不隐瞒"：审核员有权知道哪些证据没看到）。

⚠️ 判定数据（`decisions`）缺失时 `decision=None` 且 `decision` 进
`degraded_parts`。页面据此显示「未取到系统判定数据，请勿据此放行」
（`CASE-5004`），**绝不显示 0 分或"放行"**——缺数据 ≠ 放行。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from pymongo.errors import PyMongoError

from app import db as db_module
from app.constants import (
    PAGE_MAX,
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
)
from app.engine.feature_baselines import baseline_meta
from app.enums import CaseStatus, RiskLevel, label_of
from app.errors import AppError, CaseNotFoundError, case_error
from app.logging import get_logger
from app.schemas.metric_schema import name_of_scene
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.case.query")

# ============================================================
# 契约常量
# ============================================================
#: 排序字段白名单（Spec 07 §3.4："其他 → 400 CASE-4004"）。
#:
#: 比 §3.4 的字面清单多了 `scene_code`：BR-07-03 的默认排序本身是
#: `risk_level desc → created_at desc`，前端若把默认排序**原样回传**
#: （`sort=risk_level:desc,created_at:desc`）也在白名单内；而 `scene_code`
#: 是列表 "用户可点列头排序" 时最自然的一列。加它只放宽了**排序字段**的
#: 校验，不改变任何默认行为——不加则前端一实现列头排序就会收到 CASE-4004。
SORTABLE_FIELDS: tuple[str, ...] = (
    "created_at", "risk_score", "risk_level", "status", "scene_code",
)

#: 默认排序（BR-07-03）：高风险在前，同档取最新。
DEFAULT_SORT: tuple[tuple[str, int], ...] = (("risk_level", -1), ("created_at", -1))

#: 风险等级的**语义序**（`high` > `medium` > `low`）。
#:
#: ⚠️ 这里必须显式建序，**不能靠 `risk_level` 的字符串比较**——这正是本模块
#: 踩过的一个真实陷阱：按字典序 `"high" < "medium"`（`h` < `m`），于是
#: `sort=risk_level:desc` 会把 `medium` 排在 `high` **前面**，最危险的案件被
#: 挤到第二页。而 Mongo 侧同样不可靠（实测 7.0.37 对该复合排序返回的次序与
#: `sortPattern` 不符），所以最终顺序一律由 `_sort_key` 在内存里裁定。
#: 取值的真源仍是 `enums.RiskLevel`（这里只给它一个可比较的序）。
RISK_LEVEL_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: 案件状态的语义序（与 E08 的生命周期同向，仅用于稳定的内存排序）。
STATUS_RANK: dict[str, int] = {
    "pending": 0, "reviewing": 1, "disposed": 2, "archived": 3,
}

#: 「序数型」字段：它们的排序键是上表的秩，而不是字段原值。
#: 加新字段时**必须**在这里做一次判断——忘了加的后果就是"排出来的顺序看着
#: 有道理、其实与业务含义相反"，而这类缺陷在页面上极难被发现。
_ORDINAL_FIELDS: dict[str, dict[str, int]] = {
    "risk_level": RISK_LEVEL_RANK,
    "status": STATUS_RANK,
}

#: 时间区间上限跨度（§3.4 / CASE-4004：`|created_to - created_from| ≤ 90 天`）。
MAX_TIME_SPAN_MS = 90 * 86_400_000

#: 详情里"分区降级"的四个块名（进 `degraded_parts`）。
#: 与 Spec §3.2 的响应键一一对应，前端据此把**对应卡片**切成失败态。
DEGRADABLE_PARTS: tuple[str, ...] = (
    "event", "snapshot", "decision", "profile", "baseline", "graph",
)

#: 事件块对外字段（E01 的子集，Spec §3.2 的 `event` 行逐字对齐）。
_EVENT_FIELDS: tuple[str, ...] = (
    "event_type", "user_id", "biz_no", "device_id", "ip", "phone",
    "address_id", "amount", "scene_extra", "ts",
)


def parse_sort(sort: Optional[str]) -> list[tuple[str, int]]:
    """解析 `字段:asc|desc`（逗号分隔多级），非法即 `CASE-4004`（§3.4）。

    解析规则与 06 的 `list_schema.parse_sort` 同形（`: ` 可省、方向缺省 `asc`），
    但错误码不同：这里是 CASE-4004，06 是 CFG-4008——**同一个语法，两个模块
    各自的契约码**，不能互相借用（ER-02：跨模块引用保持前缀）。

    `None`/空串 → 默认排序。**空串不报错**：前端"不带 sort 参数"与
    "sort=" 是同一个意图（用默认排序），报错只会让页面首次加载就 400。
    """
    text = str(sort or "").strip()
    if not text:
        return list(DEFAULT_SORT)
    pairs: list[tuple[str, int]] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        field, _, direction = token.partition(":")
        field = field.strip()
        direction = direction.strip().lower() or "asc"
        if field not in SORTABLE_FIELDS:
            raise _bad_query(
                f"排序字段不在白名单内：{field or '(空)'}",
                allowed=list(SORTABLE_FIELDS), field=field,
            )
        if direction not in ("asc", "desc"):
            raise _bad_query(
                f"排序方向非法：{direction}（仅 asc / desc）",
                field=field, received=direction,
            )
        pairs.append((field, 1 if direction == "asc" else -1))
    return pairs or list(DEFAULT_SORT)


def normalize_multi(value: Any) -> list[str]:
    """把筛选参数归一成多值列表（重复 query 参数与逗号分隔都接受）。

    Spec §3.1 写的是"多值用重复参数（OR）"，而 06 的列表接口用的是逗号分隔。
    两种写法在真实调用方那里都会出现（前端 `URLSearchParams.append` 出一个
    重复参数，脚本里习惯写逗号），因此**两种都认**：
    归一化是纯函数，成本为零，而"只认一种"会在联调时表现为"筛选静默不生效"
    （用户以为筛了，其实服务端把 `high,medium` 当成一个非法枚举值丢掉了）。
    """
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in raw:
        for piece in str(item or "").split(","):
            name = piece.strip()
            if name and name not in out:
                out.append(name)
    return out


def _bad_query(message: str, **extra: Any) -> AppError:
    """`CASE-4004`（400）构造器：非法筛选/分页/排序参数（§5.1）。"""
    data: dict[str, Any] = {"detail": message}
    data.update(extra)
    return case_error("CASE-4004", f"查询条件不合法：{message}", data)


def _require_enum(values: Iterable[str], allowed: Sequence[str], name: str) -> list[str]:
    """逐值校验枚举；非法值**显式拒绝**而不是静默忽略。

    静默忽略的代价是"页面顶着 `risk_level=超高风险` 的筛选条显示全部数据"——
    一种看起来生效了的错误，比报错难发现得多（与 `metric_service.require_scene`
    同一条立场）。
    """
    allowed_set = {str(a) for a in allowed}
    for value in values:
        if value not in allowed_set:
            raise _bad_query(
                f"{name} 取值非法：{value}",
                field=name, allowed=sorted(allowed_set), received=value,
            )
    return list(values)


def _require_page(value: Any) -> int:
    """`page` 校验：≥1 且 ≤ `PAGE_MAX(200)`，否则 `CASE-4004`（§3.4）。

    `page > 200` 拒绝而不是夹取：深翻页会让 Mongo 在索引上跳过几十万条文档，
    而"夹到 200"会让调用方以为自己拿到了第 500 页。
    """
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise _bad_query(f"page 必须是整数，收到 {value!r}", field="page") from None
    if page < 1:
        raise _bad_query(f"page 必须 ≥ 1，收到 {page}", field="page")
    if page > PAGE_MAX:
        raise _bad_query(f"page 不能超过 {PAGE_MAX}，收到 {page}", field="page",
                         limit=PAGE_MAX)
    return page


def _require_page_size(value: Any) -> int:
    """`page_size` 校验：1~100（`constants.PAGE_SIZE_MAX`），否则 `CASE-4004`。"""
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise _bad_query(
            f"page_size 必须是整数，收到 {value!r}", field="page_size"
        ) from None
    if size < 1 or size > PAGE_SIZE_MAX:
        raise _bad_query(
            f"page_size 需在 1~{PAGE_SIZE_MAX}，收到 {size}", field="page_size",
            limit=PAGE_SIZE_MAX,
        )
    return size


def _parse_ms(value: Any, name: str) -> Optional[int]:
    """毫秒时间戳参数（`created_from` / `created_to`）；非法即 `CASE-4004`。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _bad_query(
            f"{name} 必须是毫秒时间戳，收到 {value!r}", field=name
        ) from None


def _escape_regex(text: str) -> str:
    """转义用户输入后再拼正则（`keyword` 检索，防 ReDoS 与"通配整个集合"）。

    不转义的后果不只是慢：`keyword=.*` 会退化成全集合扫描，
    而 `.` / `*` / `(` 这些字符会让一次普通检索直接抛正则语法错（500）。
    """
    return re.escape(text)


class CaseQueryService:
    """案件读侧编排（只读；依赖全部可注入，便于单测注入必失败的替身）。

    `None` = "每次从当前数据库取"：测试会切换数据库（`db.use_database`），
    缓存集合句柄会把读写落到错误的库上（与 `CaseService` / `ProfileService` 同因）。
    """

    def __init__(
        self,
        *,
        case_repo: Any = None,
        event_repo: Any = None,
        decision_repo: Any = None,
        profile_service: Any = None,
        feature_service: Any = None,
        graph_service: Any = None,
    ) -> None:
        self._cases = case_repo
        self._events = event_repo
        self._decisions = decision_repo
        self._profiles = profile_service
        self._features = feature_service
        self._graphs = graph_service

    # ---------------- 依赖装配 ----------------
    def cases(self) -> Any:
        from app.repos.case_repo import CaseRepo

        return self._cases if self._cases is not None else CaseRepo(db_module.get_db())

    def events(self) -> Any:
        from app.repos.event_repo import EventRepo

        return self._events if self._events is not None else EventRepo(db_module.get_db())

    def decisions(self) -> Any:
        from app.repos.decision_repo import DecisionRepo

        return (self._decisions if self._decisions is not None
                else DecisionRepo(db_module.get_db()))

    def profiles(self) -> Any:
        if self._profiles is not None:
            return self._profiles
        from app.services.profile_service import get_profile_service

        return get_profile_service()

    def features(self) -> Any:
        if self._features is not None:
            return self._features
        from app.services.feature_service import get_feature_service

        return get_feature_service()

    def graphs(self) -> Any:
        if self._graphs is not None:
            return self._graphs
        from app.services.graph_service import get_graph_service

        return get_graph_service()

    #: 依赖键 -> 内部属性名的映射。**必须显式列出来**：不能靠
    #: `f"_{name}"` 拼接，`graph_service` / `feature_service` / `profile_service`
    #: 三个键拼出来的属性名（`_graph_service` …）全都不存在，于是 `configure()`
    #: 会**静默不生效**——测试里注入的"必失败的图谱服务"根本没装上去，断言却
    #: 因为别的原因（用户不存在、没边）照样"看起来过了"。这正是本项目最忌讳的
    #: 一类缺陷：注入失败却无声。
    _DEPENDENCIES: dict[str, str] = {
        "case_repo": "_cases",
        "event_repo": "_events",
        "decision_repo": "_decisions",
        "profile_service": "_profiles",
        "feature_service": "_features",
        "graph_service": "_graphs",
    }

    def configure(self, **kwargs: Any) -> None:
        """替换依赖（测试注入用，语义同 `ProfileService.configure`）。

        未知的键名**直接报错**而不是跳过：拼错一个依赖名（`graph_svc`）却
        静默不生效，会让用例以"看起来通过"的方式失去意义。
        """
        for name, value in kwargs.items():
            attr = self._DEPENDENCIES.get(name)
            if attr is None:
                raise TypeError(f"未知的依赖名 {name!r}，可用：{sorted(self._DEPENDENCIES)}")
            if value is not None:
                setattr(self, attr, value)

    def reset_dependencies(self) -> None:
        """还原成"每次从当前数据库/全局单例取"（测试收尾用）。"""
        for attr in self._DEPENDENCIES.values():
            setattr(self, attr, None)

    # ============================================================
    # §3.1 列表
    # ============================================================
    async def list_cases(
        self,
        *,
        risk_level: Any = None,
        scene_code: Any = None,
        status: Any = None,
        created_from: Any = None,
        created_to: Any = None,
        assignee: Optional[str] = None,
        keyword: Optional[str] = None,
        page: Any = 1,
        page_size: Any = PAGE_SIZE_DEFAULT,
        sort: Optional[str] = None,
    ) -> dict:
        """多维筛选 + 分页（§3.1 / §3.4，BR-07-01 ~ 05）。

        返回 `{items, total, page, page_size, pages, as_of}`：
        - `total` 是**满足筛选条件的全部条数**（不是当前页条数，BR-07-05b）；
        - `as_of` 是服务端时间戳（§3.4：页面右上角「数据截至 xx:xx:xx」）；
        - 越界页（`page` 超过总页数）返回**空 `items` + 正确 `total`**，不报错。
        """
        levels = _require_enum(
            normalize_multi(risk_level), [r.value for r in RiskLevel], "risk_level",
        )
        statuses = _require_enum(
            normalize_multi(status), [s.value for s in CaseStatus], "status",
        )
        scenes = normalize_multi(scene_code)
        order = parse_sort(sort)
        number = _require_page(page)
        size = _require_page_size(page_size)

        start = _parse_ms(created_from, "created_from")
        end = _parse_ms(created_to, "created_to")
        if start is not None and end is not None:
            if start > end:
                raise _bad_query("created_from 不能大于 created_to",
                                 field="created_from")
            if end - start > MAX_TIME_SPAN_MS:
                raise _bad_query(
                    f"触发时间区间跨度不能超过 {MAX_TIME_SPAN_MS // 86_400_000} 天",
                    field="created_from",
                )

        base = self._build_filter(
            levels=levels, scenes=scenes, start=start, end=end,
            assignee=assignee, keyword=keyword,
        )
        # 排序补足默认键：分页要**稳定**（`skip/limit` 在多变排序下会漏行或重行），
        # 因此把 BR-07-03 的 `risk_level desc, created_at desc` 作为末端 tiebreaker
        # 追加进去（已有的键不重复追加）。它同时保证每个分组查询都吃得上索引。
        stable_order = list(order) + [
            key for key in (("risk_level", -1), ("created_at", -1)) if key not in order
        ]
        groups = statuses or [None]

        repo = self.cases()
        try:
            # `repo.col` 就是 `risk_cases` 集合（`CaseRepo` 的公开属性）。
            # 为什么不给 `CaseRepo` 加 `count`/`query` 方法：那是 08 已交付的
            # 处置侧仓储，本模块只是**它的读用户**——为了自己的查询形状去改别人
            # 的文件，正是任务书 §0 明令避免的"重复实现/越界改动"。
            # 用集合 + **显式 filter/sort** 表达查询，还能让 V-07-21 的
            # explain 断言直接对着同一份 filter 跑（见 tests/test_case_query.py）。
            collection = repo.col
            counts = [
                await collection.count_documents({**base, **_status_filter(g)})
                for g in groups
            ]
            items, total = await self._collect_page(
                collection, base, groups, counts, stable_order, number, size,
            )
        except PyMongoError as e:
            # 列表 5xx → 503 CASE-5005（§5.1）。这里不复用 COM-5001：
            # 页面要区分"案件列表加载失败，请重试"与"数据库不可用"两句话。
            log.warning("[CASE-5005] 案件列表查询失败：%s", e)
            raise case_error(
                "CASE-5005", "案件列表加载失败，请重试", {"detail": str(e)}
            ) from e

        pages = (total + size - 1) // size if total else 0
        return {
            "items": [_item_out(doc) for doc in items],
            "total": int(total),
            "page": number,
            "page_size": size,
            "pages": pages,
            "as_of": now_ms(),
        }

    async def _collect_page(
        self,
        collection: Any,
        base: dict,
        groups: Sequence[Optional[str]],
        counts: Sequence[int],
        order: list[tuple[str, int]],
        page: int,
        size: int,
    ) -> tuple[list[dict], int]:
        """按 status 逐组取**同属一页**的切片，合并成一整页（见模块 docstring）。

        每一组的 `skip` 是"该组在合并流里被跳过的条数"：
        `offset_in_group = max(0, (page-1)*size - 该组之前的累计条数)`。
        每组最多只需再取 `size` 条——因为一页总共只有 `size` 个位置，
        组内更靠后的条目不可能进入本页。
        """
        total = sum(int(c or 0) for c in counts)
        offset = (page - 1) * size
        merged: list[dict] = []
        passed = 0
        for group, count in zip(groups, counts):
            skip = max(0, offset - passed)
            if skip < int(count or 0):
                cursor = (
                    collection.find({**base, **_status_filter(group)})
                    .sort(order).skip(skip).limit(size)
                )
                merged.extend(await cursor.to_list(length=size))
            passed += int(count or 0)
        merged.sort(key=_sort_key(order))
        return merged[:size], total

    @staticmethod
    def _build_filter(
        *,
        levels: Sequence[str],
        scenes: Sequence[str],
        start: Optional[int],
        end: Optional[int],
        assignee: Optional[str],
        keyword: Optional[str],
    ) -> dict:
        """组装**索引友好**的筛选条件（不同字段 AND，同字段多值 OR，BR-07-01）。

        刻意不写的两处：
        - 不查 `decision_hits`（现算命中数）——BR-07-27 明令禁止跨集合实时聚合；
        - 不按 `risk_tags` 过滤——E08 上的标签是冗余快照，且 Spec 把该筛选项
          标为"预留"（§3.1 的 `risk_tags` 行），先不进查询条件。
        """
        flt: dict[str, Any] = {}
        if levels:
            flt["risk_level"] = {"$in": list(levels)} if len(levels) > 1 else levels[0]
        if scenes:
            flt["scene_code"] = {"$in": list(scenes)} if len(scenes) > 1 else scenes[0]
        if start is not None or end is not None:
            bounds: dict[str, int] = {}
            if start is not None:
                bounds["$gte"] = int(start)
            if end is not None:
                # 含头含尾（§3.4）：`created_to` 是毫秒时刻本身，不做 +1 处理——
                # 页面的时间选择器给的就是"包含该毫秒"的语义。
                bounds["$lte"] = int(end)
            flt["created_at"] = bounds
        if assignee and str(assignee).strip():
            flt["assignee"] = str(assignee).strip()
        text = str(keyword or "").strip()
        if text:
            pattern = _escape_regex(text)
            flt["$or"] = [
                {"_id": {"$regex": pattern, "$options": "i"}},
                {"user_id": {"$regex": pattern, "$options": "i"}},
            ]
        return flt

    # ============================================================
    # §3.2 详情（一次响应驱动三栏）
    # ============================================================
    async def get_case_detail(self, case_no: str) -> dict:
        """一次取齐三栏所需的 8 块数据，逐块降级（BR-07-06 / BR-07-12）。"""
        case = await self._load_case(case_no)
        event_id = str(case.get("event_id") or "")
        decision_id = str(case.get("decision_id") or "")
        user_id = str(case.get("user_id") or "")

        event, snapshot, decision, hits, profile, graph = await asyncio.gather(
            self._load_event(event_id),
            self._load_snapshot(event_id),
            self._load_decision(decision_id),
            self._load_hits(decision_id),
            self._load_profile(user_id),
            self._load_graph(user_id),
        )
        baseline = _baseline_of(snapshot)

        parts = [("event", event), ("snapshot", snapshot), ("decision", decision),
                 ("profile", profile), ("graph", graph)]
        degraded = [name for name, value in parts if value is None]
        if not hits:
            # 命中明细为空**不是**故障：`pass` 与名单直通就没有命中。
            # 但"有决策、有一条命中数却没有明细行"是数据异常，如实进 degraded。
            if decision is not None and int(decision.get("hit_rule_count") or 0) > 0:
                degraded.append("hits")
        if degraded:
            log.warning("[CASE-5001] 案件详情分区降级 case_no=%s parts=%s",
                        case_no, degraded)

        return {
            "case": _item_out(case),
            "event": event,
            "snapshot": snapshot,
            "decision": decision,
            "hits": hits,
            "profile": profile,
            "baseline": baseline,
            "graph": graph,
            "degraded_parts": degraded,
        }

    async def _load_case(self, case_no: str) -> dict:
        """取案件；查不到抛 `CASE-4001`（404），读库失败抛 `CASE-5005`。

        **"查不到"与"读不出来"必须分开**：把依赖故障报成 404 会让审核员得出
        "这个案子不存在"的结论，而真相是库在抖（与 08 的 `load` 同一条原则）。
        """
        try:
            case = await self.cases().find_by_id(str(case_no))
        except PyMongoError as e:
            raise case_error(
                "CASE-5005", "案件数据暂时不可用，请稍后重试", {"detail": str(e)}
            ) from e
        if case is None:
            raise CaseNotFoundError(str(case_no))
        return case

    async def _load_event(self, event_id: str) -> Optional[dict]:
        """事件块（E01，只读）。事件有 90 天 TTL，过期后查不到属正常降级。"""
        if not event_id:
            return None
        try:
            doc = await self.events().find_by_id(event_id)
        except Exception as e:  # noqa: BLE001 - 分区降级：任何失败都不该拖垮整页
            log.warning("[CASE-5001] 事件块读取失败 event_id=%s：%s", event_id, e)
            return None
        if not doc:
            return None
        block: dict[str, Any] = {"event_id": str(doc.get("_id") or event_id)}
        for field in _EVENT_FIELDS:
            block[field] = doc.get(field)
        return block

    async def _load_snapshot(self, event_id: str) -> Optional[dict]:
        """特征快照块（走 04 的 `FeatureService.get_snapshot`，**不自己查集合**）。

        复用服务的三个理由：① 它把 04 自落的文档与 03 的嵌入式快照**归一**成
        同一种结构，自己查会漏掉第二种形态；② 字段名只有一处真源；
        ③ 快照是**异步落库**的（BR-04-22），"查不到"是正常状态（FEA-4004），
        这里如实降级为 `null` 而不是报错。
        """
        if not event_id:
            return None
        try:
            return await self.features().get_snapshot(event_id)
        except Exception as e:  # noqa: BLE001 - 分区降级
            log.warning("[CASE-5001] 快照块读取失败 event_id=%s：%s", event_id, e)
            return None

    async def _load_decision(self, decision_id: str) -> Optional[dict]:
        """判定摘要块（E03，只读）。

        ⚠️ **字段逐列点名**，与 05 的 `POST /engine/evaluate` 响应同源
        （Spec §3.2「契约冻结」）。这里**不做任何重算**：分值/等级/结论
        原样取自库里那条决策——页面展示的就是"当时按什么判的"。
        """
        if not decision_id:
            return None
        try:
            doc = await self.decisions().find_decision(decision_id)
        except Exception as e:  # noqa: BLE001 - 分区降级（CASE-5004）
            log.warning("[CASE-5001] 判定块读取失败 decision_id=%s：%s", decision_id, e)
            return None
        if not doc:
            return None
        return _decision_out(doc)

    async def _load_hits(self, decision_id: str) -> list[dict]:
        """命中明细（E04，按 `score desc`，Spec §2.4.2）。

        明细为空返回 `[]` 而不是 `None`：空数组表达"这条决策没有任何命中"，
        与"这块数据没取到"（走 `degraded_parts`）是两件事。
        """
        if not decision_id:
            return []
        try:
            rows = await self.decisions().list_hits(decision_id)
        except Exception as e:  # noqa: BLE001 - 分区降级
            log.warning("[CASE-5001] 命中明细读取失败 decision_id=%s：%s", decision_id, e)
            return []
        return [
            {
                "rule_code": row.get("rule_code"),
                # BR-05-21：规则名取 `decision_hits` 的**冗余快照**，不实时联查 rules
                "rule_name": row.get("rule_name"),
                "rule_version": row.get("rule_version"),
                "score": int(row.get("score") or 0),
                "reason": row.get("reason"),
                "matched_facts": dict(row.get("matched_facts") or {}),
            }
            for row in rows
        ]

    async def _load_profile(self, user_id: str) -> Optional[dict]:
        """画像块（调 09 的 `ProfileService.get_profile`，只渲染）。

        **不自己拼画像**：`profileCard` 的字段口径（脱敏手机号、关联账号数、
        代理标记、同地址账号数）全部是 09 的职责，本模块另写一套必然出现
        "两个页面上的画像卡不一样"（任务书 §0 明令复用 09 的取数函数）。
        """
        if not user_id:
            return None
        try:
            return await self.profiles().get_profile(user_id)
        except Exception as e:  # noqa: BLE001 - 分区降级（CASE-5003）
            log.warning("[CASE-5001] 画像块读取失败 user_id=%s：%s", user_id, e)
            return None

    async def _load_graph(self, user_id: str) -> Optional[dict]:
        """图谱块（调 09 的 `GraphService.query`，默认 **2 跳**，AD-06）。

        09 自己带 3s 超时降级（超时返回已取到的部分 + `truncated`/`timeout`
        标记），因此本模块**不重复实现超时**：那不但是第二份口径，还会把
        09 已经成功取到的部分结果丢掉（GRP-5002 的正确表达就是那个部分结果）。
        """
        if not user_id:
            return None
        try:
            return await self.graphs().query("user", user_id, max_hop=2)
        except Exception as e:  # noqa: BLE001 - 分区降级（CASE-5002）
            log.warning("[CASE-5001] 图谱块读取失败 user_id=%s：%s", user_id, e)
            return None


# ============================================================
# 纯函数：文档 -> 对外结构
# ============================================================
def _status_filter(status: Optional[str]) -> dict:
    """单个 status 的查询条件（`None` = 不筛状态，返回空条件）。"""
    return {} if status is None else {"status": status}


def _sort_key(order: Sequence[tuple[str, int]]) -> Any:
    """把 Mongo 的排序列表翻译成 Python 的 `sort(key=...)`（跨组合并时用）。

    ## 为什么**必须**在内存里再排一次（不是一个"以防万一"的兜底）

    本机 MongoDB 7.0.37 在一个真实场景下**排错了**：`status=常量` +
    `.sort([("risk_level",-1),("created_at",-1)])` 返回的第一条可能是
    `medium` 而 `high` 排在后面（对照实验：单键 `created_at:-1` 正确、
    单键 `risk_level:-1` 也错误，与 pymongo 无关——三种传参形式结果一致，
    `explain` 里的 `sortPattern` 也确实是 `{risk_level:-1, created_at:-1}`）。
    也就是说**不能把"默认排序正确"这件事托付给服务端**。

    排序是展示契约的一部分（BR-07-03：高风险的案件必须排在前面，审核员按
    这个顺序干活），排错的代价是"最危险的案子被排到第二页"。因此这里做一次
    **显式的全序排序**，把结论握在自己手里：

    - 游标那层 `.sort(...)` 仍然保留——它让**索引有序前缀**能用上
      （`status+risk_level` 等值时 MongoDB 可以直接走索引，省掉 `SORT` 阶段），
      并且 `skip/limit` 的切片语义不依赖顺序方向（同一 `skip` 取到的记录集合
      与顺序无关，只与是不是全序有关）；
    - 但**最终顺序**由本函数决定，因此上面那种服务端异常不会改变页面顺序。

    只处理本白名单里的字段：全部是**同类型可比**的标量（int / str）。
    缺失值统一给中性默认值，避免 `None` 与 `int` 比较抛 `TypeError`——
    脏数据不该让整页 500。

    `risk_level` / `status` 走 `_ORDINAL_FIELDS` 的**语义秩**而不是字符串
    （见 `RISK_LEVEL_RANK`：字典序下 `high < medium`，直接比较会把最危险的
    案件排到最后）。
    """
    defaults: dict[str, Any] = {
        "created_at": 0, "risk_score": 0, "risk_level": 0, "status": 0,
        "scene_code": "",
    }

    def key(doc: Mapping[str, Any]) -> tuple:
        out: list[Any] = []
        for field, direction in order:
            rank = _ORDINAL_FIELDS.get(field)
            if rank is not None:
                # 未登记的取值给 -1（排在所有已知等级之前）：脏数据要能一眼看见，
                # 而不是混进正常档位里冒充一个真实的等级
                value: Any = rank.get(str(doc.get(field) or ""), -1)
            else:
                value = doc.get(field)
                if value is None:
                    value = defaults.get(field, "")
            if direction < 0:
                out.append(-value if not isinstance(value, str)
                           else tuple(-ord(ch) for ch in value))
            else:
                out.append(value)
        return tuple(out)

    return key


def _item_out(doc: Mapping[str, Any]) -> dict:
    """E08 文档 -> 列表/详情的 `case` 块（Spec §3.1 的 `items[]` 逐字段对齐）。"""
    status = str(doc.get("status") or "")
    scene = str(doc.get("scene_code") or "")
    return {
        "case_no": str(doc.get("_id") or ""),
        "user_id": str(doc.get("user_id") or ""),
        "scene_code": scene,
        # 场景中文名由后端给出（BR-00-18：中文只在一处真源，前端不得硬编码）
        "scene_name": name_of_scene(scene) if scene else "",
        "risk_score": int(doc.get("risk_score") or 0),
        "risk_level": str(doc.get("risk_level") or ""),
        "risk_level_label": label_of(RiskLevel, str(doc.get("risk_level") or "")),
        "decision": str(doc.get("decision") or ""),
        "degraded": bool(doc.get("degraded")),
        "degrade_code": doc.get("degrade_code"),
        "risk_tags": [str(t) for t in (doc.get("risk_tags") or [])],
        "status": status,
        "status_label": label_of(CaseStatus, status),
        "assignee": doc.get("assignee"),
        "claimed_at": doc.get("claimed_at"),
        "claim_deadline_at": doc.get("claim_deadline_at"),
        "disposed_at": doc.get("disposed_at"),
        "created_at": int(doc.get("created_at") or 0),
        # 单位分，展示层 ÷100（§3.1 的 `estimated_loss` 行）
        "estimated_loss": int(doc.get("estimated_loss") or 0),
        "event_id": str(doc.get("event_id") or ""),
        "decision_id": str(doc.get("decision_id") or ""),
    }


def _decision_out(doc: Mapping[str, Any]) -> dict:
    """E03 文档 -> `/engine/evaluate` 同源的判定块（Spec §3.2 契约冻结）。

    键名与 `app/schemas/engine_schema.EngineEvaluateOut` 的前 12 项一致
    （去掉只属于"当场求值"的 `degraded`/`trace`/`warnings`，加上库里那条的
    `decision_id` 与 `decided_at`）。**不做任何重算**——尤其是 `final_score`
    与 `risk_level` 一律读库（V-07-14 把 `decisions.final_score` 手工改成 77
    后，页面必须显示 77 而不是重算出来的 86）。
    """
    return {
        "decision_id": str(doc.get("_id") or ""),
        "event_id": str(doc.get("event_id") or ""),
        "snapshot_id": doc.get("snapshot_id"),
        "list_hit": doc.get("list_hit"),
        "rule_score": int(doc.get("rule_score") or 0),
        # AD-09：模型引擎是空实现，`model_score` 恒为 null（不展示模型分栏位）
        "model_score": doc.get("model_score"),
        "final_score": int(doc.get("final_score") or 0),
        "risk_level": str(doc.get("risk_level") or ""),
        "decision": str(doc.get("decision") or ""),
        "hit_rule_count": int(doc.get("hit_rule_count") or 0),
        "rule_versions": dict(doc.get("rule_versions") or {}),
        "engine_version": str(doc.get("engine_version") or ""),
        "elapsed_ms": int(doc.get("elapsed_ms") or 0),
        # 决策 D5：降级产生的 review 必须让审核员看出来（建案的就是它）
        "degraded": bool(doc.get("degraded")),
        "degrade_code": doc.get("degrade_code"),
        "decided_at": doc.get("decided_at"),
    }


def _baseline_of(snapshot: Optional[Mapping[str, Any]]) -> dict:
    """快照对比列的「基线值」表（**复用 04 的静态参考区间**，裁定 D68）。

    返回 `{特征名: 基线块}`，块内字段与 `GET /api/v1/features/meta` 的
    `items[]` 逐字一致（`baseline` / `baseline_desc` / `has_baseline` /
    `direction_hint`）。

    ## 为什么不是 09 的新聚合（本模块最重要的一处裁定）

    Spec 07 §8「新增-1」建议由 09 提供"近 30 天中位数 / 全局分位兜底"。**未采纳**：
    模块 04 已交付静态参考区间并经 `GET /api/v1/features/meta` 下发
    （`app/engine/feature_baselines.py`，也是前端标签配色的同源数据）。
    为"对比列"再造一套统计聚合会让两个页面上的同一列出现两种口径，
    而它们看起来都是"基线值"——那正是复核时最需要避免的事。

    因此这里 **直接复用同一个纯函数** `feature_baselines.baseline_meta()`：
    与 `/features/meta` 的 `items[]` 同源、**不产生任何数据库读取**（它是配置表），
    且与 04 的口径今后自动保持一致（同一份配置改了两处同时变）。

    E21 的统计基线（`stat_baseline`，P50/P95）**刻意不查**：它不是"基线值"
    对比列的口径（`feature_baselines` 的模块 docstring 已把两者分开定义），
    详情页为它多读一次库既无收益，又会让"这个数字是拍的还是算的"重新混淆。
    """
    features = (snapshot or {}).get("features")
    keys: Iterable[str] = features.keys() if isinstance(features, Mapping) else ()
    return {str(key): baseline_meta(str(key)) for key in keys}


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = CaseQueryService()


def get_case_query_service() -> CaseQueryService:
    """取进程内服务单例（依赖全部按需现取，因此无跨用例状态）。"""
    return _SERVICE


async def list_cases(**kwargs: Any) -> dict:
    """模块级便捷入口：列表查询（§3.1）。"""
    return await _SERVICE.list_cases(**kwargs)


async def get_case_detail(case_no: str) -> dict:
    """模块级便捷入口：详情聚合（§3.2）。"""
    return await _SERVICE.get_case_detail(case_no)


__all__ = [
    "DEFAULT_SORT",
    "DEGRADABLE_PARTS",
    "MAX_TIME_SPAN_MS",
    "SORTABLE_FIELDS",
    "CaseQueryService",
    "get_case_detail",
    "get_case_query_service",
    "list_cases",
    "normalize_multi",
    "parse_sort",
]
