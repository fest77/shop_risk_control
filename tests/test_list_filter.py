# -*- coding: utf-8 -*-
"""5 维名单匹配、黑白优先级、灰名单不参与、TTL 缓存与降级（BR-05-01 ~ 05-07）。

## 覆盖 V-05-02 / V-05-03 / V-05-04 的名单面

这三条验收全部是**行为等价性**断言，最容易被写成"看起来对"：
`hits=[]`/`rule_score=0` 只能证明"没有加分"，证明不了"没有求值"；
灰名单的"不影响结果"必须与**同一事件不加名单**的结果逐字段比较，
而不是断言"decision == pass"（那可能恰好因为其他规则没配而通过）。
"""
from __future__ import annotations

import pytest

from app import db as db_module
from app.engine.list_filter import (
    DEFAULT_CACHE_TTL_SEC,
    LIST_CACHE,
    ListEntryCache,
    ListHit,
    match_lists,
)
from app.errors import ListServiceUnavailableError
from app.repos.list_repo import ListRepo
from app.services.list_service import invalidate_list_cache
from app.utils.mask import mask_phone

from tests.engine_testlib import (
    ANCHOR_TS,
    ExplodingListRepo,
    install_lists,
    list_doc,
    make_event,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def cache() -> ListEntryCache:
    """每个用例一个**独立**缓存：避免跨用例串味（进程内单例另有 conftest 清）。"""
    return ListEntryCache()


async def test_no_list_entry_means_no_hit(cache):
    """名单里什么都没有 → 未命中（三个描述字段都是 `None`，不是空串）。"""
    hit = await match_lists(make_event(ip="198.51.100.9"), cache=cache)
    assert hit == ListHit(hit=False)
    assert hit.to_dict() == {"hit": False, "list_type": None,
                             "entity_type": None, "entity_value": None}


async def test_black_and_white_hits_direct_through(cache):
    """黑 → `black`、白 → `white`，且**带上命中的维度与具体值**（E03 的 list_hit）。"""
    await install_lists(
        list_doc("black", "user", "U000128"),
        list_doc("white", "user", "U009999"),
    )
    black = await match_lists(make_event(user_id="U000128"), cache=cache)
    assert (black.hit, black.list_type, black.entity_type, black.entity_value) == \
        (True, "black", "user", "U000128")
    white = await match_lists(make_event(user_id="U009999"), cache=cache)
    assert (white.hit, white.list_type) == (True, "white")


async def test_black_wins_over_white_on_the_same_entity(cache):
    """V-05-03 / BR-05-04：同一实体同时命中黑白 → 黑优先。

    这是 Spec 标注的悬空点 G-04 的默认值。放行的代价（放过一个已确认的风险账号）
    远大于误拦（多一次人工复核，且可恢复），因此黑优先。
    """
    await install_lists(
        list_doc("white", "device", "D8F2A1C4", entry_id="LW1"),
        list_doc("black", "device", "D8F2A1C4", entry_id="LB1"),
    )
    hit = await match_lists(make_event(device_id="D8F2A1C4"), cache=cache)
    assert hit.hit is True and hit.list_type == "black"


async def test_black_on_a_later_dimension_still_wins(cache):
    """黑优先**跨越全部 5 个维度**：用户维度白 + 设备维度黑 → 黑。

    若实现成"先命中的那一维说了算"，这种配置会静默放行——而它恰恰是最容易
    被人为构造出来的一种组合（把内部账号加白、同时把人拉黑在一台设备上）。
    """
    await install_lists(
        list_doc("white", "user", "U000777"),
        list_doc("black", "device", "DBAD0001"),
    )
    hit = await match_lists(
        make_event(user_id="U000777", device_id="DBAD0001"), cache=cache
    )
    assert hit.list_type == "black"
    assert hit.entity_type == "device"


async def test_gray_list_does_not_participate(cache):
    """V-05-04 / BR-05-06：灰名单**不参与决策**，结果与不加名单完全一致。

    比的是整个 `ListHit`（含三个描述字段），而不是只看 `hit`：若实现把灰名单
    也读出来并在 `list_type` 里带一个 `gray`，`hit` 仍是 False，但消费方会拿到
    一个"命中了什么"的信号——那就是"参与了"。
    """
    row = make_event(user_id="U_GRAY", ip="117.136.12.88")
    baseline = await match_lists(row, cache=ListEntryCache())
    await install_lists(list_doc("gray", "ip", "117.136.12.88"))
    with_gray = await match_lists(row, cache=ListEntryCache())
    assert with_gray == baseline == ListHit(hit=False)


DIMENSION_CASES = (
    ("user", "user_id", "U000001"),
    ("phone", "phone", "138****6621"),
    ("ip", "ip", "203.0.113.10"),
    ("device", "device_id", "DN00001"),
    ("address", "address_id", "ADDR123456"),
)


@pytest.mark.parametrize("entity_type,field,value", DIMENSION_CASES,
                         ids=[c[0] for c in DIMENSION_CASES])
async def test_all_five_dimensions_are_matched(entity_type, field, value):
    """BR-05-01：`user_id → phone → ip → device_id → address_id` 五个维度都真的在匹配。"""
    await install_lists(list_doc("black", entity_type, value))
    hit = await match_lists(make_event(**{field: value}), cache=ListEntryCache())
    assert hit.hit is True, f"{entity_type} 维度没有被匹配"
    assert hit.entity_type == entity_type


async def test_address_dimension_falls_back_to_scene_extra(cache):
    """`address_id` 也可能只出现在 `scene_extra` 里（E01 的 order_create 扩展字段）。

    03 的 `validate_event` 会把它归一到顶层，但 `decide()` 也能被直接调用
    （单测、模块 10 复用），因此取值必须兜住这一层，否则"同地址聚集"类名单
    在某条调用路径上会静默失效。
    """
    await install_lists(list_doc("black", "address", "ADDR-SCENE"))
    event = make_event(event_type="order_create")
    event["scene_extra"] = {"address_id": "ADDR-SCENE"}
    hit = await match_lists(event, cache=cache)
    assert hit.hit is True and hit.entity_type == "address"


async def test_phone_is_matched_in_masked_form(cache):
    """手机号按**脱敏形态**匹配（BR-06-21：写入侧与决策侧同一份脱敏实现）。

    事件里的明文手机号必须能命中库里那条脱敏条目（03 入口已脱敏，但直接构造
    事件时会拿到明文）；反过来，库里绝不会存明文，因此明文条目必须**不**命中
    ——否则就意味着两条链路对同一份数据有两种形态。
    """
    await install_lists(list_doc("black", "phone", "138****6621"))
    hit = await match_lists(make_event(phone="13800006621"), cache=ListEntryCache())
    assert hit.hit is True and hit.entity_value == "138****6621"
    # 已脱敏的串不能被打码两次（`mask_phone` 对含 `*` 的串会再削一次）
    assert mask_phone("138****6621") != "138****6621", (
        "确认这个坑真实存在：因此 `_normalize` 只对**纯数字 11 位**再脱敏"
    )


@pytest.mark.parametrize("status", ["expired", "removed"])
async def test_inactive_entries_never_match(cache, status):
    """BR-05-05：只有 `status=active` 的条目参与匹配。"""
    await install_lists(list_doc("black", "user", "U000001", status=status))
    assert (await match_lists(make_event(user_id="U000001"), cache=cache)).hit is False


async def test_expired_entry_never_matches_even_before_cleanup(cache):
    """BR-05-05：`expire_at` 已过期的条目**即使仍是 active** 也不参与。

    清理任务是周期性跑的，在它跑之前过期条目仍是 `active`。若匹配时不过滤
    `expire_at`，一条昨天就该失效的黑名单会继续拦人，而页面上显示的是"已过期"
    ——用户按页面判断"这人早该放行了"，系统却在拦，两边永远对不上账。

    `now` 显式传 `ANCHOR_TS`：判定"是否过期"必须有一个确定的时钟，
    否则断言会随真实时间漂移（`ANCHOR_TS` 是一个固定的未来锚点）。
    """
    await install_lists(list_doc("black", "user", "U000001", expire_at=ANCHOR_TS - 1))
    hit = await match_lists(make_event(user_id="U000001"), cache=cache, now=ANCHOR_TS)
    assert hit.hit is False


async def test_future_expiry_still_matches(cache):
    """未过期的条目照常生效（反向证据：过滤条件没写反）。"""
    await install_lists(
        list_doc("black", "user", "U000001", expire_at=ANCHOR_TS + 86_400_000)
    )
    hit = await match_lists(make_event(user_id="U000001"), cache=cache, now=ANCHOR_TS)
    assert hit.hit is True


async def test_permanent_entry_has_no_expiry(cache):
    """`expire_at=None` = 永久（黑白名单默认），绝不能被当成"已过期"。"""
    await install_lists(list_doc("black", "user", "U000001", expire_at=None))
    hit = await match_lists(make_event(user_id="U000001"), cache=cache, now=ANCHOR_TS)
    assert hit.hit is True


# ============================================================
# TTL 缓存（BR-05-07 / AD-02）
# ============================================================
class CountingRepo:
    """包一层真实仓储，统计回源次数（"缓存到底有没有生效"只能这样证）。"""

    def __init__(self) -> None:
        self.inner = ListRepo(db_module.get_db())
        self.calls = 0

    async def find_active(self, list_type, entity_type, entity_value):
        self.calls += 1
        return await self.inner.find_active(list_type, entity_type, entity_value)


async def test_second_lookup_within_ttl_does_not_hit_the_database():
    """BR-05-07：TTL 内命中缓存**不回源**（AD-02）。

    断言的是"回源次数没有增长"而不是"结果一样"：后者在缓存完全失效时也成立。
    """
    repo = CountingRepo()
    cache = ListEntryCache(ttl_sec=DEFAULT_CACHE_TTL_SEC)
    event = make_event(user_id="U000001", ip="198.51.100.1")
    await match_lists(event, repo=repo, cache=cache)
    first = repo.calls
    assert first > 0
    await match_lists(event, repo=repo, cache=cache)
    assert repo.calls == first, "第二次查询必须全部命中缓存"


async def test_negative_result_is_cached_too():
    """负缓存：**没查到**也要缓存（绝大多数实体不在任何名单上）。

    不缓存"没有"就等于每次都为"没有"付一次查询代价；代价是新增条目最长
    10s 生效，由两条主动失效覆盖（见下一条与 `invalidate_list_cache`）。
    """
    repo = CountingRepo()
    cache = ListEntryCache()
    event = make_event(user_id="U_NOT_ON_ANY_LIST")
    await match_lists(event, repo=repo, cache=cache)
    first = repo.calls
    await match_lists(event, repo=repo, cache=cache)
    assert repo.calls == first


async def test_invalidate_list_cache_clears_the_engine_cache():
    """BR-06-23 的真实落点：名单写入成功后**立刻**让决策侧看到变更。

    这条是"刚拉黑的人还能继续下单"这类缺陷的唯一防线。断言的是
    `invalidate_list_cache()` 真的清空了 05 的缓存（不是只记了个时间戳），
    因此随后的一次匹配必须回源。
    """
    repo = CountingRepo()
    event = make_event(user_id="U_INVALIDATE_ME")
    await match_lists(event, repo=repo, cache=LIST_CACHE)
    first = repo.calls
    invalidate_list_cache()
    await match_lists(event, repo=repo, cache=LIST_CACHE)
    assert repo.calls > first, "失效之后必须回源（缓存没被清掉）"


async def test_flush_mark_invalidates_cache_even_without_explicit_clear():
    """兜底路径：`DEGRADED.last_flush_at` 一变，缓存自动整表失效。

    为什么需要这条兜底：将来若有人新增一条写名单的路径却忘了调
    `invalidate_list_cache()`，显式清理就不会发生——但时间戳变了，
    05 仍然会回源。两条路互为保险。
    """
    from app.core.degraded import DEGRADED

    repo = CountingRepo()
    event = make_event(user_id="U_MARK_ME")
    await match_lists(event, repo=repo, cache=LIST_CACHE)
    first = repo.calls
    # 模拟"别处写了名单、只更新了时间戳"
    DEGRADED.last_flush_at = DEGRADED.last_flush_at + 1
    await match_lists(event, repo=repo, cache=LIST_CACHE)
    assert repo.calls > first, "时间戳变化没有让缓存失效"


# ============================================================
# 降级（RUL-5001）
# ============================================================
async def test_repository_failure_raises_rul_5001(cache):
    """V-05-08 的一半：名单查询失败必须**抛降级信号**，绝不返回"未命中"。

    返回"未命中"等于把"不知道"当成"他不在此名单"——一次 Mongo 抖动就等于
    把全部黑名单停用。这正是 fail-closed 要拦住的那类缺陷。
    """
    repo = ExplodingListRepo()
    with pytest.raises(ListServiceUnavailableError) as exc:
        await match_lists(make_event(user_id="U000001"), repo=repo, cache=cache)
    assert exc.value.code == "RUL-5001"
    assert exc.value.http_status == 503
    assert repo.calls >= 1


async def test_failure_is_not_cached(cache):
    """失败**不进缓存**：一次抖动被缓存 10s 会让整段时间的决策都无依据。"""
    repo = ExplodingListRepo()
    for _ in range(2):
        with pytest.raises(ListServiceUnavailableError):
            await match_lists(make_event(user_id="U000001"), repo=repo, cache=cache)
    assert repo.calls == 2, "失败不能被负缓存吸收"
    assert cache.stats()["size"] == 0
