# -*- coding: utf-8 -*-
"""画像聚合与增量维护（模块 09 §4.1 / §6）。

## 职责边界

| 做什么 | 不做什么 |
|---|---|
| 组装 §3.1 的全貌画像（用户 + stat + 最近决策 + 设备/IP/地址） | 特征计算（归 04）——本模块的聚集度是**全量长期**口径 |
| 增量维护 `stat`（`$inc`）、`risk_tags`（`$addToSet`/`$pull`）、`latest_decision` | 案件的查看与联动（归 07）、处置动作（归 08） |

## 画像卡上的"设备/IP/地址"从哪来（一个必须说清的取数口径）

E10 `users` 里**只有** `phone/level/status/risk_tags/risk_score_history/stat`，
**没有**"最近用的设备/IP/地址"字段。因此画像卡上的这三项只能从别处取，有两个候选：

1. 读 E01 `risk_events` 里该用户最近一条事件；
2. 读 E14 `entity_edges` 里该用户最近共现的关联实体。

实现选 **2**，理由：`entity_edges` 是"该用户与哪些实体有关联"的**长期真源**
（E01 有 90 天 TTL，且事件会因为迟到/重放等原因不代表当前画像）；并且 §3.1 需要的
正是"这个账号当前挂在哪个设备/IP/地址上"——那正是最近一次共现。E01 是模块 03 的
集合，跨模块直读还会把 09 绑死在事件表的结构上。

## 为什么查询失败必须是 503 而不是 404

见 `ProfileNotFoundError` 的说明：`GRP-5001`（503）表示"我们现在答不上来"，
`GRP-4004`（404）表示"这个人不存在"。把前者报成后者，等于让审核员在数据库抖动时
得出"查无此人"的结论——那是本模块最危险的一类错误呈现。
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from app import db as db_module
from app.constants import COLL_DEVICES, COLL_IP_POOL, COLL_USER_ADDRESSES
from app.enums import RiskTag
from app.errors import AppError, ProfileNotFoundError, ProfileUnavailableError, grp_error
from app.logging import get_logger
from app.repos import graph_repo as graph_repo_mod
from app.repos.profile_repo import ProfileRepo
from app.schemas.profile_schema import build_tag_meta
from app.utils.mask import mask_address, mask_phone
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.profile.service")

#: 聚集度告警阈值：同一设备/IP/地址关联的账号数 ≥ 该值时标红（§2.1 的
#: "**N ≥ 5 时数值标红**"）。这是**规格里的数字**，不是随手定的：
#: 它同时是"自动打 `*_cluster` 标签"的门槛——若两处各写一个数字，
#: 页面标红了但标签没打上（或反之），研判人员会以为系统自相矛盾。
CLUSTER_ALERT_THRESHOLD = 5

#: 画像卡取"最近共现实体"时扫描的边数上限。有界是必须的：一个刷单团伙的
#: 核心设备可能有上千条边，而画像卡只需每类各一条。
PROFILE_EDGE_SCAN = 50

#: `relation` -> 它指向的实体类型（BR-09-09 的四种关联）。
#: `same_phone` **刻意不在此表**：它是 user↔user 的边（手机号不是独立图节点），
#: 画像卡上的"手机号"取自 E10 的 `phone` 字段，不来自边。
RELATION_ENTITY_TYPE: dict[str, str] = {
    "used_device": "device",
    "shared_ip": "ip",
    "shared_address": "address",
}

#: 事件类型 -> `users.stat` 的字段增量（BR-09-02）
#: ------------------------------------------------------------
#: ⚠️ **决策 D12**：事件类型枚举里 `order_pay` 归一为 `pay` **场景**（场景码取自
#: E06），但事件类型本身**仍然是字面量 `order_pay`**（`app/enums.EventType`）。
#: 因此这里的键必须是 `order_pay`——若照抄"pay"，该分支永远不会命中，
#: 累计金额恒为 0，而且不会有任何报错（静默失效是本项目最忌讳的一类缺陷）。
#:
#: `total_amount` 记在 **`order_pay`** 而不是 `order_create`：`order_create` 只是
#: 创建订单（可能永不支付），金额计入会让"累计消费"虚高；同时挂在两个事件上则会
#: 把同一笔订单的钱**算两遍**。口径选"实际支付金额"，并在此写明。
#: `block_cnt` 不在这里——它取决于**决策结果**而不是事件类型，见 `stat_updates_for`。
STAT_FIELD_BY_EVENT: dict[str, str] = {
    "order_create": "order_cnt",
    "order_pay": "total_amount",
    "after_sale_apply": "aftersale_cnt",
}

#: `stat` 的四个字段（BR-09-02）。未知字段一律拒绝：让拼错的字段名在**写入前**
#: 暴露，而不是在库里留下一个没人读的 `stat.odrer_cnt`。
STAT_FIELDS: tuple[str, ...] = ("order_cnt", "aftersale_cnt", "block_cnt", "total_amount")


def stat_updates_for(event: Mapping[str, Any],
                     decision: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """由一条事件（+ 它的决策）推出 `users.stat` 的增量（BR-09-02）。

    纯函数，因此可以被逐条断言（哪些事件加什么、加多少、是否重复计数）。
    - `order_create` → `order_cnt + 1`
    - `order_pay` → `total_amount += amount`（实际支付金额，单位分）
    - `after_sale_apply` → `aftersale_cnt + 1`
    - `decision == "reject"` → `block_cnt + 1`（"被拦截数"，与事件类型无关：
      `login` 也可能被拦截，因此它是**叠加**在事件类型增量之上的第二维）
    - 其余（`login` / `coupon_receive` / 降级为 `review`）→ 不改 `stat`。

    降级（`decision=review`，`engine_version=degraded`）**不计 block_cnt**：
    它表示"没算出来、转人工"，而不是"拦住了"。两者混一起会让拦截率虚高。
    """
    updates: dict[str, int] = {}
    event_type = str(event.get("event_type") or "")
    field = STAT_FIELD_BY_EVENT.get(event_type)
    if field == "total_amount":
        try:
            amount = int(event.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            # 负数/零金额不进累计：`amount` 已被 03 校验为正整数，这里是纵深防御
            updates[field] = amount
    elif field:
        updates[field] = 1

    if str((decision or {}).get("decision") or "") == "reject":
        updates["block_cnt"] = 1
    return updates


def cluster_tags(*, device_cnt: Optional[int] = None,
                 ip_cnt: Optional[int] = None,
                 address_cnt: Optional[int] = None,
                 is_proxy: Optional[bool] = None) -> list[str]:
    """由聚集度与代理标记推出应当打上的标签（§2.1 的 8 个标签里的 4 个）。

    纯函数：输入即结论，便于逐条断言阈值边界（`4 → 不标`、`5 → 标`）。

    **只覆盖有数据依据的 4 个标签**，其余 4 个刻意不在此自动打：
    - `new_account`（新账号）需要 04 的 `user_age_days` 口径与判定阈值，归 04/05；
    - `high_freq`（高频行为）需要窗口频次，归 04/05；
    - `aftersale_abuse`（售后滥用）需要退款率阈值，归 05；
    - `blacklist_history`（历史黑名单）需要名单命中结论，归 05/06。
    在这里凭"事件里出现的次数"顺手打上它们，等于把 05 的判定逻辑偷偷搬进 09，
    两处口径必然漂移（`V-09-13` 只要求标签不重复、可移除，不要求谁来打）。
    """
    tags: list[str] = []
    if device_cnt is not None and int(device_cnt) >= CLUSTER_ALERT_THRESHOLD:
        tags.append(RiskTag.DEVICE_CLUSTER.value)
    if address_cnt is not None and int(address_cnt) >= CLUSTER_ALERT_THRESHOLD:
        tags.append(RiskTag.ADDRESS_CLUSTER.value)
    if ip_cnt is not None and int(ip_cnt) >= CLUSTER_ALERT_THRESHOLD:
        tags.append(RiskTag.IP_CLUSTER.value)
    if is_proxy is True:
        tags.append(RiskTag.PROXY_IP.value)
    return tags


def masked_phone_of(value: Any) -> Optional[str]:
    """手机号的展示值：**已脱敏的原样返回，未脱敏的当场掩码**。

    为什么读侧还要兜一次（与 `sanitize_address` 同因）：BR-09-05 要求"脱敏存储"，
    正常路径下库里存的就是 `139****0001`。但历史数据、人工修库、将来某个模块
    图省事直接写明文，都可能让明文手机号进到响应里——而"响应含明文手机号"
    就是一次真实的数据泄露（`V-09-03` 直接验它）。

    **必须幂等**：`mask_phone("139****0001")` 会按"非 11 位纯数字"的通用规则
    掩成 `13****01`，把已经正确的脱敏值**破坏掉**。因此含 `*` 的一律原样返回。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "*" in text:
        return text
    return mask_phone(text)


def age_days_of(register_at: Any, at_ms: Optional[int] = None) -> Optional[int]:
    """注册至今天数（§2.1 的"注册 42 天"）。

    `register_at` 缺失/非法 → `None`（**不是 0**）：`age_days=0` 是一个断言
    "今天刚注册"，而真相是"我们不知道"。前端按 §3.1 的 null 语义显示「—」。
    """
    try:
        register_at_ms = int(register_at)
    except (TypeError, ValueError):
        return None
    if register_at_ms <= 0:
        return None
    now = now_ms() if at_ms is None else int(at_ms)
    # 未来时间（时钟不准/脏数据）按 0 天处理而不是负数：负的"注册天数"没有任何
    # 可解释的含义，显示出来只会让人怀疑系统坏了
    return max(0, (now - register_at_ms) // 86_400_000)


def masked_detail_of(doc: Optional[Mapping[str, Any]]) -> Optional[str]:
    """地址展示值：只由 `province/city/district` 拼出并掩码（BR-09-05）。

    **绝不解码 `detail_hash`、绝不读 `detail`**：哈希不可逆（这正是它的意义），
    而任何明文字段的存在都意味着一次泄露。
    """
    if not doc:
        return None
    explicit = doc.get("masked_detail")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    prefix = "".join(str(doc.get(k) or "") for k in ("province", "city", "district"))
    return mask_address(prefix) if prefix else None


def is_proxy_of(doc: Optional[Mapping[str, Any]]) -> Optional[bool]:
    """E12 的 `is_proxy` / `is_idc` 合取（口径与 `feature_repo._pick_proxy` 一致）。

    两处必须同口径：画像卡显示"代理 IP"而特征快照里 `ip_is_proxy=false`
    （或反之）会让研判人员无法判断到底信哪个。都取 `is_proxy or is_idc`。
    **两个字段都缺失 → `None`**（不是 `false`，D46）。
    """
    if not doc:
        return None
    seen = False
    for key in ("is_proxy", "is_idc"):
        value = doc.get(key)
        if isinstance(value, bool):
            seen = True
            if value:
                return True
    return False if seen else None


class ProfileService:
    """画像聚合与增量维护（进程内单例，见文件末尾）。"""

    def __init__(self, repo: Optional[ProfileRepo] = None,
                 graph: Optional[graph_repo_mod.GraphRepo] = None) -> None:
        #: `None` 表示"每次从当前数据库取"（测试会切换库，缓存句柄会写错库）
        self._repo = repo
        self._graph = graph

    # ---------------- 依赖装配 ----------------
    def _r(self) -> ProfileRepo:
        return self._repo if self._repo is not None else ProfileRepo(db_module.get_db())

    def _g(self) -> graph_repo_mod.GraphRepo:
        return self._graph if self._graph is not None else graph_repo_mod.GraphRepo(db_module.get_db())

    def configure(self, *, repo: Optional[ProfileRepo] = None,
                  graph: Optional[graph_repo_mod.GraphRepo] = None) -> None:
        """替换依赖（测试注入用；`V-09-14` 靠它注入一个必失败的 repo）。"""
        if repo is not None:
            self._repo = repo
        if graph is not None:
            self._graph = graph

    def reset_dependencies(self) -> None:
        """还原成"每次从当前数据库取"（测试收尾用）。

        为什么需要它：`configure()` 用的是"非 None 才替换"的语义（避免误清依赖），
        因此**没有**办法把依赖恢复成默认。测试注入假 repo 之后若不还原，
        后续用例会一路用到那个假实现（跨用例污染，本项目的幽灵缺陷来源之一）。
        """
        self._repo = None
        self._graph = None

    # ---------------- §3.1 画像组装 ----------------
    async def get_profile(self, user_id: str) -> dict:
        """组装 §3.1 的全貌画像。查不到用户 → `GRP-4004`；依赖故障 → `GRP-5001`。

        `tag_meta` 每次一并下发（§3.1）：它是"标签 → 配色"的唯一真源，
        前端不得硬编码（见 `profile_schema.build_tag_meta`）。
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise grp_error("GRP-4004", "未找到该用户/实体", {"entity_type": "user"})
        repo = self._r()
        user = await repo.get_user(uid)
        if user is None:
            raise ProfileNotFoundError("user", uid)

        # 2 跳之外只取"每类各一条"：画像卡只展示最近共现的那个设备/IP/地址
        entities = await self._latest_entities(uid)
        device_id = entities.get("device")
        ip_value = entities.get("ip")
        address_id = entities.get("address")

        # 三类实体的读取彼此独立 → 并发（串行会让画像卡的响应时间线性叠加）
        device_task = asyncio.create_task(repo.get_device(device_id)) if device_id else None
        ip_task = asyncio.create_task(repo.get_ip(ip_value)) if ip_value else None
        if address_id:
            addr_task = asyncio.create_task(repo.get_address(address_id))
        else:
            # 没有 `shared_address` 边时按 `user_id` 反查一次（种子/历史数据可能只有
            # 地址行而没有边）。这只是**兜底**，正常路径仍以边为准。
            addr_task = asyncio.create_task(repo.find_address_of_user(uid))

        gathered = await asyncio.gather(
            *[t for t in (device_task, ip_task, addr_task) if t is not None],
            return_exceptions=True,
        )
        results: list[Any] = list(gathered)
        for item in results:
            if isinstance(item, BaseException):
                # 单类读失败不该让整张卡消失，但**也不能悄悄当成"没有"**：
                # 记日志（GRP-5001）后该区块显示为空，其余区块照常返回
                log.warning("[GRP-5001] 画像子项读取失败（该区块将显示空）：%s", item)
        offset = 0
        device_doc = ip_doc = address_doc = None
        if device_task is not None:
            device_doc = _clean(results[offset]); offset += 1
        if ip_task is not None:
            ip_doc = _clean(results[offset]); offset += 1
        address_doc = _clean(results[offset])

        return {
            "user": self._user_block(uid, user),
            "stat": self._stat_block(user),
            "latest_decision": self._latest_decision_block(user),
            "device": self._device_block(device_id, device_doc),
            "ip": self._ip_block(ip_value, ip_doc),
            "address": self._address_block(address_id, address_doc),
            "tag_meta": build_tag_meta(),
        }

    async def _latest_entities(self, user_id: str) -> dict[str, str]:
        """取该用户最近共现的设备/IP/地址各一个（按 `last_seen_at` 降序）。

        为什么要按 `last_seen_at`（而不是 `weight`）：画像卡回答的是
        "这个账号**现在**挂在什么上面"。`weight` 高的边可能是半年前刷出来的，
        把旧设备显示成"当前设备"会误导研判（`last_seen_at` 才是时间证据）。
        """
        try:
            edges = await self._g().edges_touching(
                "user", user_id, limit=PROFILE_EDGE_SCAN, sort_by="last_seen_at",
            )
        except Exception as e:  # noqa: BLE001 - 关联实体是画像卡的**可选增强**，
            # 它的读取失败不该让整张卡消失（画像的其余部分仍然可看）。
            # 这里刻意捕获**所有**异常而不只是 `GRP-5001`：一次意外故障（例如
            # 仓储签名变化）同样不该让"用户/stat/标签"这三块已经拿到的数据被丢掉；
            # 但**必须如实记日志**，否则就成了静默降级。
            log.warning("[GRP-5001] 画像关联实体读取失败（画像卡将只显示用户信息）"
                        "user_id=%s：%s", user_id, e)
            return {}
        out: dict[str, str] = {}
        for edge in edges:
            relation = str(edge.get("relation") or "")
            entity_type = RELATION_ENTITY_TYPE.get(relation)
            if entity_type is None or entity_type in out:
                continue
            other = graph_repo_mod.other_endpoint(edge, "user", user_id)
            if other is None:
                continue
            out[entity_type] = other[1]
        return out

    @staticmethod
    def _user_block(user_id: str, user: Mapping[str, Any]) -> dict:
        tags = user.get("risk_tags")
        return {
            "user_id": user_id,
            "phone_masked": masked_phone_of(user.get("phone")),
            "register_at": _opt_int(user.get("register_at")),
            "age_days": age_days_of(user.get("register_at")),
            "level": _opt_str(user.get("level")),
            "status": _opt_str(user.get("status")),
            "risk_tags": [str(t) for t in tags] if isinstance(tags, (list, tuple)) else [],
        }

    @staticmethod
    def _stat_block(user: Mapping[str, Any]) -> dict:
        stat = user.get("stat")
        stat = stat if isinstance(stat, Mapping) else {}
        return {field: _opt_int(stat.get(field)) or 0 for field in STAT_FIELDS}

    @staticmethod
    def _latest_decision_block(user: Mapping[str, Any]) -> Optional[dict]:
        latest = user.get("latest_decision")
        if not isinstance(latest, Mapping) or not latest:
            return None
        score = latest.get("risk_score")
        try:
            score = None if score is None else float(score)
        except (TypeError, ValueError):
            score = None
        return {
            "risk_score": score,
            "risk_level": _opt_str(latest.get("risk_level")),
            "decision": _opt_str(latest.get("decision")),
            "decided_at": _opt_int(latest.get("decided_at")),
        }

    @staticmethod
    def _device_block(device_id: Optional[str], doc: Optional[Mapping[str, Any]]) -> Optional[dict]:
        if not device_id and not doc:
            return None
        doc = doc or {}
        return {
            "device_id": str(device_id or doc.get("_id") or ""),
            "linked_user_cnt": _opt_int(doc.get("linked_user_cnt")) or 0,
            "first_seen_at": _opt_int(doc.get("first_seen_at")),
            "os": _opt_str(doc.get("os")),
        }

    @staticmethod
    def _ip_block(ip_value: Optional[str], doc: Optional[Mapping[str, Any]]) -> Optional[dict]:
        if not ip_value and not doc:
            return None
        doc = doc or {}
        return {
            "ip": str(ip_value or doc.get("_id") or ""),
            "linked_user_cnt": _opt_int(doc.get("linked_user_cnt")) or 0,
            # 传 `doc` 而不是 `doc or {}`：**行不存在时必须给 `None`**，
            # 给 `{}` 会算出 `False`——那是对 05 宣称"这个 IP 不是代理"（D46）
            "is_proxy": is_proxy_of(doc) if doc else None,
            "region": _opt_str(doc.get("region")),
            "isp": _opt_str(doc.get("isp")),
        }

    @staticmethod
    def _address_block(address_id: Optional[str],
                       doc: Optional[Mapping[str, Any]]) -> Optional[dict]:
        if not address_id and not doc:
            return None
        doc = doc or {}
        return {
            "address_id": str(address_id or doc.get("_id") or ""),
            "masked_detail": masked_detail_of(doc),
            "linked_user_cnt": _opt_int(doc.get("linked_user_cnt")) or 0,
            "aftersale_cnt": _opt_int(doc.get("aftersale_cnt")) or 0,
        }

    # ---------------- §3.3 内部接口：增量维护 ----------------
    async def bump_stat(self, user_id: str, field: str, delta: int = 1) -> None:
        """累加 `users.stat.<field>`（§3.3 的 `bump_stat` 契约）。

        未知字段直接拒绝（`COM-4001`，422）而不是静默写入：写进一个没人读的
        `stat.xxx` 会让"累计统计"看起来正常但实际少了一维，而这类缺陷在页面上
        完全没有症状。
        """
        if field not in STAT_FIELDS:
            raise _bad_stat_field(field)
        await self._r().inc_stat(str(user_id), str(field), int(delta))

    async def bump_stats(self, user_id: str, updates: Mapping[str, int]) -> None:
        """按字段批量累加（一次 `$inc`，避免"单数加了金额没加"的中间态）。"""
        clean = {k: int(v) for k, v in updates.items() if k in STAT_FIELDS and int(v)}
        unknown = sorted(set(updates) - set(STAT_FIELDS))
        if unknown:
            raise _bad_stat_field(unknown[0])
        if clean:
            await self._r().inc_stats(str(user_id), clean)

    async def tag_user(self, user_id: str, tag: str) -> bool:
        """打风险标签（`$addToSet`，BR-09-03）。返回是否**新增**。

        未知标签拒绝（`COM-4001`，参数错误）：标签是前端配色的键（`tag_meta`
        只有 8 项），写入一个 `tag_meta` 里没有的标签会让前端拿到一个没有颜色的
        标签——通常表现为一个突兀的灰块，而它可能代表高危含义。
        """
        if tag not in build_tag_meta():
            raise _bad_tag(tag)
        return await self._r().add_tag(str(user_id), str(tag))

    async def untag_user(self, user_id: str, tag: str, *, actor: str,
                         actor_role: str = "", ip: Optional[str] = None,
                         ua: Optional[str] = None) -> bool:
        """显式移除标签**并写审计**（BR-09-04）。

        BR-09-04 的原文是"标签一旦打上**不自动移除**；误打需通过管理操作显式移除
        并写审计"。因此本方法做两件事：① 真移除；② 写一条 `profile.tag.remove`
        审计（`strict=False`：留不下痕也要让移除生效，否则运维会陷入"想改改不掉"）。

        `before`/`after` 都带上标签数组：只记一句"移除了 device_cluster" 无法回答
        "移除前它还有哪些标签"——而误打的常见情形恰恰是"顺手多打了几个"，
        排查时需要看到完整的变更前后。
        """
        uid = str(user_id)
        repo = self._r()
        before_doc = await repo.get_user(uid)
        before_tags = list(before_doc.get("risk_tags") or []) if before_doc else None
        removed = await repo.remove_tag(uid, str(tag))
        after_doc = await repo.get_user(uid)
        after_tags = list(after_doc.get("risk_tags") or []) if after_doc else None

        from app.services import audit_service  # 延迟导入：避免 09 ↘ 12 的模块级环

        await audit_service.audit(
            actor=actor or "system", actor_role=actor_role,
            action="profile.tag.remove",
            target_type="user", target_id=uid,
            before={"risk_tags": before_tags},
            after={"risk_tags": after_tags, "removed_tag": str(tag), "removed": removed},
            ip=ip, ua=ua, strict=False,
        )
        return removed

    async def set_latest_decision(self, user_id: str, block: Mapping[str, Any],
                                  *, decided_at: Optional[int] = None) -> None:
        """覆盖式写"最近一次决策摘要"（BR-09-06）。"""
        await self._r().set_latest_decision(
            str(user_id), block, int(decided_at if decided_at is not None else now_ms()),
        )

    # ---------------- 只读辅助 ----------------
    @staticmethod
    def tag_meta() -> dict[str, dict[str, str]]:
        """`tag_meta` 的唯一出口（供 07 的其它区块与测试复用）。"""
        return build_tag_meta()


class MongoLinkedUserCountProvider:
    """模块 09 对 04 的实现：`LinkedUserCountProvider`（BR-09-13/14 的单一真源）。

    装配在 `app/protocols.Components.linked_user_count_provider` 上（决策 D45：
    默认装配必须体现**真实存在**的组件——04 的三项聚集度特征因此不再进
    `missing_features`，而是拿到 09 维护的真实数字）。

    ## 为什么读"冗余计数"而不是每次去数边

    `device_user_cnt` / `ip_user_cnt` / `address_user_cnt` 是 04 每次决策都要取的
    三项，而决策链路只有 200ms 预算（BR-03-23）。`linked_user_cnt` 这个冗余字段
    存在的唯一理由就是"避免每次查询都全表统计"（BR-09-12）。
    冗余计数的维护方只有一处：`edge_writer` 在**边首次插入**时 `$inc`。
    因此这里读它是安全的——写入侧的唯一性由 `uq_edge` 索引 + `upserted_id` 保证。

    ## 三种返回值的语义（必须严格区分）

    | 情形 | 返回 | 理由 |
    |---|---|---|
    | 查到画像行 | 该行的 `linked_user_cnt` | 09 维护的真值 |
    | 没有画像行 | **按边集合如实数一次** | 没有画像行不等于"没有关联"；数出来是 0 就返回 0（"这个设备没有任何关联账号"是**可以证实**的结论，不是猜测） |
    | 未知实体类型 / 查库失败 | `None` | `None` = "**无法计算**"（04 会写进 `missing_features`），而 0 = 一个断言。这个区分正是 D46 的同一条原则在计数上的应用 |

    ⚠️ 与"缺失不得用 0 冒充"（BR-04-09）的区别：那里禁止的是**用 0 代替未知**；
    这里的前提是"关联集合本身可以被完整计数"，数出来 0 就是 0。
    """

    #: 供 `/health` 与启动日志区分"真实实现"与占位实现
    available = True

    #: 支持的三类实体（§3.3：`entity_type ∈ {device, ip, address}`）
    ENTITY_COLLECTIONS: dict[str, str] = {
        "device": COLL_DEVICES,
        "ip": COLL_IP_POOL,
        "address": COLL_USER_ADDRESSES,
    }

    def __init__(self, repo: Optional[ProfileRepo] = None,
                 graph: Optional[graph_repo_mod.GraphRepo] = None) -> None:
        self._repo = repo
        self._graph = graph

    def _r(self) -> ProfileRepo:
        return self._repo if self._repo is not None else ProfileRepo(db_module.get_db())

    def _g(self) -> graph_repo_mod.GraphRepo:
        return (self._graph if self._graph is not None
                else graph_repo_mod.GraphRepo(db_module.get_db()))

    async def get_linked_user_count(self, entity_type: str,
                                    entity_id: str) -> Optional[int]:
        coll_name = self.ENTITY_COLLECTIONS.get(str(entity_type))
        if coll_name is None or not str(entity_id or "").strip():
            # 未知类型/空 id 属"无法计算"：`phone` 不是图节点，永远给不出关联账号数
            return None
        eid = str(entity_id).strip()
        try:
            doc = await self._r().db[coll_name].find_one(
                {"_id": eid}, {"linked_user_cnt": 1}
            )
            if doc is not None:
                value = doc.get("linked_user_cnt")
                return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
            # 没有画像行：按边集合如实数（09 内部的口径转换，不是第二个真源）
            return await self._count_edges(entity_type, eid)
        except ProfileUnavailableError as e:
            log.warning("[GRP-5001] 关联账号数查询失败（按'无法计算'处理）：%s", e)
            return None
        except Exception as e:  # noqa: BLE001 - 04 契约要求"不可用时返回 None"，不得抛出
            log.warning("[GRP-5001] 关联账号数查询异常（按'无法计算'处理）：%s", e)
            return None

    async def _count_edges(self, entity_type: str, entity_id: str) -> int:
        """数一数"该实体另一端是用户"的边有多少条（= 关联账号数）。

        为什么限定"另一端是 user"：边集合里还可能有 `transferred_to` 等
        user↔user 的边，若只按"碰到该实体"来数，会把与账号数无关的关联算进来。
        按定义数（另一端是账号）才与 `linked_user_cnt` 同口径。
        """
        try:
            cursor = self._g().col.find({
                "$or": [
                    {"from_type": str(entity_type), "from_id": str(entity_id),
                     "to_type": "user"},
                    {"to_type": str(entity_type), "to_id": str(entity_id),
                     "from_type": "user"},
                ]
            })
            rows = await cursor.to_list(length=None)
        except Exception as e:  # noqa: BLE001 - 同上：不可用返回 None 由调用方处理
            log.warning("[GRP-5001] 关联账号数按边统计失败：%s", e)
            raise
        # 去重：同一账号与同一实体之间只应有一条边（`uq_edge`），这里再按 to_id
        # 去一次是为了对"数据被人工改过"的情况保持诚实——数的是**账号**，不是边
        users: set[str] = set()
        for row in rows:
            other = graph_repo_mod.other_endpoint(row, entity_type, entity_id)
            if other is not None and other[0] == "user":
                users.add(other[1])
        return len(users)


def _bad_stat_field(field: str) -> AppError:
    """`stat` 字段名非法 → `COM-4001`（422）。

    这是调用方把字段名写错了（属**参数校验失败**），不是服务端故障：
    用 5xx 会让 03/05 以为"09 挂了"并触发告警，而真相是一个拼写错误。
    """
    return AppError(
        "COM-4001",
        f"参数校验失败：未知的 stat 字段 {field}（仅支持 {'/'.join(STAT_FIELDS)}）",
        422,
        {"errors": [{"path": "field", "message": "不支持的 stat 字段名"}]},
    )


def _bad_tag(tag: str) -> AppError:
    """未知标签 → `COM-4001`（422，参数错误），理由同 `_bad_stat_field`。"""
    return AppError(
        "COM-4001",
        f"参数校验失败：未知风险标签 {tag}（仅支持 {'/'.join(sorted(build_tag_meta()))}）",
        422,
        {"errors": [{"path": "tag", "message": "不支持的标签"}]},
    )


def _clean(value: Any) -> Optional[Any]:
    """把 `asyncio.gather` 的异常结果归一成 `None`（并已在上游记过日志）。"""
    if isinstance(value, BaseException) or value is None:
        return None
    return value


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> Optional[int]:
    """整数归一：`bool` 显式排除（`True` 是 `int` 的子类，会把计数变成 1）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# 进程内单例 + 模块级入口（§3.3 的内部接口契约）
# ============================================================
_SERVICE = ProfileService()


def get_profile_service() -> ProfileService:
    return _SERVICE


async def get_profile(user_id: str) -> dict:
    return await _SERVICE.get_profile(user_id)


async def bump_stat(user_id: str, field: str, delta: int = 1) -> None:
    """§3.3：`bump_stat(user_id, field, delta=1)` —— 03/05 累加 `users.stat`。"""
    await _SERVICE.bump_stat(user_id, field, delta)


async def tag_user(user_id: str, tag: str) -> None:
    """§3.3：`tag_user(user_id, tag)` —— 05/08 打风险标签。

    返回 `None`（契约如此），**不返回是否新增**：调用方（05/08）不需要关心
    "这个标签是不是第一次打"，而去重由 `$addToSet` 在存储层保证（BR-09-03）。
    需要这个信息的内部流程请直接用 `ProfileService.tag_user()`。
    """
    await _SERVICE.tag_user(user_id, tag)


__all__ = [
    "CLUSTER_ALERT_THRESHOLD",
    "MongoLinkedUserCountProvider",
    "PROFILE_EDGE_SCAN",
    "RELATION_ENTITY_TYPE",
    "STAT_FIELDS",
    "STAT_FIELD_BY_EVENT",
    "ProfileService",
    "age_days_of",
    "bump_stat",
    "cluster_tags",
    "get_profile",
    "get_profile_service",
    "is_proxy_of",
    "masked_detail_of",
    "masked_phone_of",
    "stat_updates_for",
    "tag_user",
]
