# -*- coding: utf-8 -*-
"""5 维名单匹配 + 进程内 TTL 缓存 + 降级标记（BR-05-01 ~ 05-07 / AD-02）。

## 匹配顺序（BR-05-01）

`user_id → phone → ip → device_id → address_id`（对应 E07 的
`entity_type ∈ {user, phone, ip, device, address}`）。

## 为什么黑白名单都不求值任何规则（BR-05-02/03）

白名单是"这个人我们已经确认过、别再打扰他"，黑名单是"这个人已经确认有
问题、不必再算"。两者都是**人工已经下过的结论**，再让规则引擎算一遍有两个
坏处：① 浪费一次完整求值（直通本来就该是最快的路径）；② 更严重的是，若
规则恰好算出低分，白名单用户的 `rule_score` 会显示成 0 以外的值，"直通"
这件事就再也没法从数据上认出来。因此直通态的 `hits=[]`、`rule_score=0`
是**刻意的**，不是"算出来恰好为 0"。

## 为什么黑名单优先（BR-05-04，悬空点 G-04）

同一个人同时出现在黑白名单上，唯一的成因是"先被拉黑、后有人给他加白"
或者反过来。此时放行的代价（放过一个已确认的风险账号）远大于误拦的代价
（多一次人工复核），而且误拦是**可恢复**的。因此黑优先。

实现上黑名单的判定跨越全部 5 个维度（任一维度命中黑即 `reject`），
而**不是**"先命中的那一维说了算"——后者会让"用户维度白名单 + 设备维度黑名单"
这种配置静默放行，是最容易漏掉的一种组合。

## 灰名单为什么不参与决策（BR-05-06，悬空点 G-05）

灰名单的语义是"**观察**"：它既不是"已确认有问题"（黑），也不是"已确认没问题"
（白），而是"值得留意"。若让灰名单拦，它就成了黑名单；若让它放，它就成了
白名单——两种都在**伪造一个我们没有的结论**。因此本模块**根本不查询灰名单**：
不查就不会误用，"不参与"这件事由代码结构保证，而不是靠每个分支记得跳过它。
灰名单的展示归 09 的画像标注（Spec §4.1 原文"只在画像上标注"）。

## 为什么必须过滤 `status` 与 `expire_at`（BR-05-05）

E07 的 `status ∈ {active, expired, removed}`，`expire_at=null` 表示永久。
只判 `status` 是不够的：清理任务是**周期性**跑的（`list_cleanup_task`），
在它跑之前，一条已经过了 `expire_at` 的条目仍是 `active`。若匹配时不过滤，
一条本该在昨天失效的黑名单会继续拦人，而页面上显示的却是"已过期"——
用户按页面判断"这人早该放行了"，系统却在拦，两边永远对不上账。

## TTL 缓存（BR-05-07 / AD-02）：默认 10s

风控是高频路径，同一用户在同一个 IP/设备上会连续产生多条事件，逐条回源
查询名单是纯粹的浪费。10s 是可接受的滞后上限，且有两条**主动失效**兜底：

1. `list_service.invalidate_list_cache()` 在名单写入成功后调用，
   **直接清空**本缓存（BR-06-23），因此正常情况下不存在"加了黑名单还要等 10s"；
2. `DEGRADED.last_flush_at` 的时间戳比对（模块 06 的写路径已经写好了这个
   约定，见 `app/core/degraded.py` 与 `list_service` 的模块说明）。这一条是
   **保险**：即便将来有人新增一条写路径却忘了调 ①，时间戳变了缓存照样失效。

**负缓存（没查到也记 10s）是刻意的**：绝大多数实体都不在任何名单上，不缓存
"没有"就等于每次都要为"没有"付一次查询代价。代价是新增条目最长 10s 生效，
由上面两条主动失效覆盖。

## 降级（RUL-5001）

查询失败时**绝不返回"未命中"**：那等于把"不知道"当成"他不在此名单"，
一次 Mongo 抖动就等于把全部黑名单停用。这里抛 `ListServiceUnavailableError`，
由 `decision.decide()` 转成 `review` + `degraded=true` 并**照常落库建案**（D5）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app import db as db_module
from app.core.degraded import DEGRADED
from app.errors import ListServiceUnavailableError
from app.logging import get_logger
from app.repos.list_repo import ListRepo
from app.utils.mask import mask_phone
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.list_filter")

#: 参与匹配的 5 个维度，**顺序即 BR-05-01 的顺序**：`(E07.entity_type, 事件字段名)`。
#: 顺序只影响"命中明细里先报哪一条"，不影响结论（黑名单优先跨越全部维度）。
LIST_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("user", "user_id"),
    ("phone", "phone"),
    ("ip", "ip"),
    ("device", "device_id"),
    ("address", "address_id"),
)

#: 参与决策的两种名单（**刻意不含 `gray`**，见模块 docstring）
DECISION_LIST_TYPES: tuple[str, ...] = ("black", "white")

#: E07 里"生效中"的状态取值
ACTIVE_STATUS = "active"

#: TTL 缓存默认有效期（BR-05-07 / AD-02：默认 10s）
DEFAULT_CACHE_TTL_SEC = 10.0


# ============================================================
# 结果对象
# ============================================================
@dataclass(frozen=True)
class ListHit:
    """名单直通结果（E03 `list_hit` 的字段形状，Spec §3.1）。

    `hit=False` 时其余三项为 `None`——**不要**把它们补成空串：前端与 E03
    靠"有没有值"区分"名单未命中"与"命中了但值没带"，补空串会让两者同形。
    """

    hit: bool = False
    list_type: Optional[str] = None
    entity_type: Optional[str] = None
    entity_value: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "list_type": self.list_type,
            "entity_type": self.entity_type,
            "entity_value": self.entity_value,
        }


NO_HIT = ListHit()


# ============================================================
# 进程内 TTL 缓存
# ============================================================
@dataclass
class ListEntryCache:
    """`(list_type, entity_type, entity_value) -> 条目或 None` 的 TTL 缓存。

    存的是**原始条目**而不是"是否命中"：条目在缓存里，`expire_at` 的判定
    仍然每次现算（`_is_effective`）。若把"命中/未命中"当结果缓存，一条
    在 TTL 内到期的条目会继续拦人最长 10s——而到期是**可以精确知道**的。
    """

    ttl_sec: float = DEFAULT_CACHE_TTL_SEC
    _rows: dict[tuple[str, str, str], tuple[float, Optional[dict]]] = field(default_factory=dict)
    _flush_mark: int = 0
    hits: int = 0
    misses: int = 0

    def get(self, key: tuple[str, str, str]) -> tuple[bool, Optional[dict]]:
        """返回 `(是否命中缓存, 条目或 None)`。

        先做时间戳失效检查：`last_flush_at` 变了说明**别处刚写过名单**，
        整表清空再查——与 `clear()` 等价，但不需要写路径记得调用我们。
        """
        self._sync_with_flush_mark()
        row = self._rows.get(key)
        if row is None:
            self.misses += 1
            return False, None
        expires_at, doc = row
        if expires_at <= time.monotonic():
            del self._rows[key]
            self.misses += 1
            return False, None
        self.hits += 1
        return True, doc

    def put(self, key: tuple[str, str, str], doc: Optional[dict]) -> None:
        self._rows[key] = (time.monotonic() + self.ttl_sec, doc)

    def clear(self) -> None:
        self._rows.clear()

    def _sync_with_flush_mark(self) -> None:
        """`DEGRADED.last_flush_at` 变化即整表失效（模块 06 已写好的约定）。"""
        mark = int(getattr(DEGRADED, "last_flush_at", 0) or 0)
        if mark != self._flush_mark:
            self._flush_mark = mark
            self._rows.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "size": len(self._rows),
            "ttl_sec": self.ttl_sec,
            "hits": self.hits,
            "misses": self.misses,
        }


#: 进程内单例（AD-02）。模块 06 的写路径通过 `clear_list_cache()` 主动失效。
LIST_CACHE = ListEntryCache()


def clear_list_cache() -> None:
    """清空名单缓存。

    这是 `list_service.invalidate_list_cache()` 的真实落点（BR-06-23）：
    名单写入成功后**必须**让决策侧立刻看到变更，否则"刚拉黑的人还能继续下单"
    直到 TTL 过期。写成模块级函数是为了让 06 的四处写路径共用同一处实现。
    """
    LIST_CACHE.clear()


# ============================================================
# 取值与生效判定
# ============================================================
def _normalize(entity_type: str, raw: Any) -> Optional[str]:
    """把事件里的字段值归一成**与 E07 存储形态一致**的匹配键。

    - `phone`：E07 存的是**脱敏号**（list_service 写入时就脱敏，BR-06-21 要求
      写入侧与决策侧用同一份实现）。03 的 `validate_event` 已经把事件里的
      手机号脱敏，因此这里通常什么都不用做；但**原始 11 位数字**也要能对上
      （直接构造事件单测时会走到），所以对纯数字 11 位再脱敏一次。
      ⚠️ 不能无条件调用 `mask_phone`：它对已脱敏串（含 `*`）会按"5~10 位"
      分支再削一次（`139****0001` → `13****01`），把键改坏。
    - 其余维度：E07 存原值，直接用。
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if entity_type == "phone" and value.isdigit() and len(value) == 11:
        return mask_phone(value)
    return value


def _event_value(event: dict, field_name: str) -> Any:
    """取事件里该维度对应的值。

    `address_id` 有**两个来源**：03 的顶层字段，以及 `order_create` 的
    `scene_extra.address_id`（E01 的扩展字段表）。虽然 03 的 `validate_event`
    已经把两者归一到顶层，但 `decide()` 也可能被直接调用（单测、10 的复用），
    因此这里再兜一次。**不是**在替 03 做校验，只是取值的容错。
    """
    value = event.get(field_name)
    if value is None and field_name == "address_id":
        extra = event.get("scene_extra")
        if isinstance(extra, dict):
            value = extra.get("address_id")
    return value


def _is_effective(doc: dict, now: int) -> bool:
    """`status=active` 且（无 `expire_at` 或 `expire_at > now`）（BR-05-05）。"""
    if str(doc.get("status") or "") != ACTIVE_STATUS:
        return False
    expire_at = doc.get("expire_at")
    if expire_at is None:
        return True
    try:
        return int(expire_at) > now
    except (TypeError, ValueError):
        # 脏数据（例如把秒级时间戳写进来）：宁可不判过期也不放过。
        # 若这里把异常当成"已过期"，一条被写坏的黑名单会静默失效。
        log.warning("名单条目 expire_at 非法，按未过期处理 entry_id=%s", doc.get("_id"))
        return True


# ============================================================
# 匹配
# ============================================================
async def _lookup(
    repo: Any,
    cache: ListEntryCache,
    list_type: str,
    entity_type: str,
    entity_value: str,
    now: int,
) -> Optional[dict]:
    """取一条生效中的名单条目（走缓存，未命中回源）。"""
    key = (list_type, entity_type, entity_value)
    cached, doc = cache.get(key)
    if cached:
        return doc
    found = await repo.find_active(list_type, entity_type, entity_value)
    if found is not None and not _is_effective(found, now):
        found = None
    cache.put(key, found)
    return found


async def match_lists(
    event: dict,
    *,
    repo: Any = None,
    cache: Optional[ListEntryCache] = None,
    now: Optional[int] = None,
) -> ListHit:
    """按 BR-05-01~05 完成名单直通判定。

    | 参数 | 说明 |
    |---|---|
    | `repo` | 名单仓储（默认真实 `ListRepo`）。测试注入抛异常的桩来覆盖 RUL-5001 |
    | `cache` | TTL 缓存（默认进程内单例） |
    | `now` | 判定"是否过期"的时刻（默认当前毫秒） |

    **抛 `ListServiceUnavailableError`（RUL-5001）**：任何查询失败都必须降级，
    绝不能返回"未命中"（见模块 docstring 的降级一节）。
    """
    moment = now_ms() if now is None else int(now)
    repository = repo if repo is not None else ListRepo(db_module.get_db())
    store = cache if cache is not None else LIST_CACHE

    white_hit: Optional[tuple[str, str]] = None

    for entity_type, field_name in LIST_DIMENSIONS:
        value = _normalize(entity_type, _event_value(event, field_name))
        if value is None:
            continue  # 该维度本次事件没带（如 order_pay 没有 device_id）
        try:
            black = await _lookup(repository, store, "black", entity_type, value, moment)
            if black is not None:
                # BR-05-04：黑名单优先。任一维度命中黑即 reject，**不必**再往下看
                # ——继续扫描不可能改变结论（黑优先是全局的），只是浪费往返。
                return ListHit(True, "black", entity_type, value)
            if white_hit is None:
                white = await _lookup(repository, store, "white", entity_type, value, moment)
                if white is not None:
                    white_hit = (entity_type, value)
        except ListServiceUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 - 任何查询失败都必须 fail-closed
            raise ListServiceUnavailableError(
                f"{entity_type}={value} 名单查询失败：{type(e).__name__}: {e}"
            ) from e

    if white_hit is not None:
        entity_type, value = white_hit
        return ListHit(True, "white", entity_type, value)
    return NO_HIT


__all__ = [
    "ACTIVE_STATUS",
    "DECISION_LIST_TYPES",
    "DEFAULT_CACHE_TTL_SEC",
    "LIST_CACHE",
    "LIST_DIMENSIONS",
    "NO_HIT",
    "ListEntryCache",
    "ListHit",
    "clear_list_cache",
    "match_lists",
]
