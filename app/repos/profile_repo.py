# -*- coding: utf-8 -*-
"""E10~E13（`users` / `devices` / `ip_pool` / `user_addresses`）的读写（模块 09 §6）。

## 本文件是 E10~E13 的**唯一写入口**

模块 04 的 `MongoProfileReader` 只读这四张表（它的 docstring 写明"只读是硬边界，
写入归模块 09"）。因此"谁把画像写歪了"这个问题在本文件之外无解——所有写入
（`$inc` / `$addToSet` / `$ifNull` 兜底）都收在这里，便于逐条对照 BR-09-01~06。

## 为什么用「聚合管道更新」而不是 `$setOnInsert`

D46 要求：**任何事件 IP 都必须能解析出确定的 `ip_is_proxy`**，而这要求 E12 的
`is_proxy` / `is_idc` 是**显式布尔值**（`feature_repo._pick_proxy` 只有看到 bool
才会给出 `False`；字段缺失返回 `None`，04 据此判"缺失"并短路降级）。

难点在于"既要补默认值，又绝不能覆盖已有的真值"：

| 写法 | 问题 |
|---|---|
| `$setOnInsert: {is_proxy: False}` | 只在**插入**时生效。若该行已存在但**缺** `is_proxy`（例如别的模块/历史数据建的行），永远补不上，D46 的保证出现空洞 |
| `$set: {is_proxy: False}` | 会把真实的 `is_proxy=true`（代理 IP！）**覆盖成 false**，等于亲手抹掉最危险的信号 |
| 先读再写 | 亚稳态：两次往返之间别人可能改成 `true`，我们随后按旧值写回 |

聚合管道更新（MongoDB 4.2+，本机 7.0）一次原子操作解决三者：
`{"$ifNull": ["$is_proxy", False]}` = 有值就保持原值（含 `true`），缺失/`null` 才填
`False`。`first_seen_at` 用同一手法实现"只在首次写入"。
**一次命令、幂等、不覆盖真值。**

## 为什么 `linked_user_cnt` 的递增不在这里做判断

"只在**新边**上递增"是模块 09 的核心不变式（BR-09-08 +
"重复上报不得把计数刷爆"）。判断依据是 `entity_edges` 的 upsert 返回值，
那是 `graph_repo` 的职责，因此本文件只提供
`inc_linked_user_cnt()` 这个"纯递增"原语，由调用方保证只在首次插入时调用。
把判断塞进这里会需要回查边集合，反而制造第二个判定点。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.constants import (
    COLL_DEVICES,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.errors import ProfileUnavailableError
from app.logging import get_logger

log = get_logger("shop_risk_control.profile.repo")

#: 图谱/画像支持的实体类型 -> 集合名（§3.2 的 `entity_type` 取值**不含 phone**）
ENTITY_COLLECTIONS: dict[str, str] = {
    "user": COLL_USERS,
    "device": COLL_DEVICES,
    "ip": COLL_IP_POOL,
    "address": COLL_USER_ADDRESSES,
}

#: 非法地址明文字段名。读到就**丢弃**（BR-09-05：地址只存 `detail_hash`）。
#:
#: 为什么要在读侧再兜一次：写入侧已经保证不存明文，但历史数据/人工修库/将来
#: 某个模块图省事直接 `insert_one` 都可能带进明文，而"响应里出现明文地址"
#: 是一次真实的数据泄露（`V-09-03` 直接验这一点）。纵深防御的成本只有一次 `pop`。
_ADDRESS_PLAINTEXT_FIELDS: tuple[str, ...] = (
    "detail", "detail_text", "address_detail", "full_address",
)

#: `risk_score_history` 保留的最近次数（E10 的"最近 N 次决策分数"，画风险趋势用）。
#: 有界是必须的：无界数组会随每一次决策增长，最终把文档顶到 16MB 上限。
RISK_SCORE_HISTORY_MAX = 20


def _unavailable(detail: str, *, op: str) -> ProfileUnavailableError:
    """把驱动异常转成 `GRP-5001`（§5：画像聚合查询失败 → 503）。

    **绝不转成 404**：把 Mongo 抖动显示成"该用户不存在"会让审核员得出
    "没有这个人"的错误结论（见 `ProfileNotFoundError` 的说明）。
    """
    return ProfileUnavailableError(detail=f"{detail}", op=op)


def sanitize_address(doc: Optional[dict]) -> Optional[dict]:
    """丢掉地址文档里的明文字段（BR-09-05 的读侧兜底）。

    返回**新 dict**（不就地改库里的对象）：就地改会让调用方以为库里的数据被清理了。
    """
    if doc is None:
        return None
    clean = dict(doc)
    for field in _ADDRESS_PLAINTEXT_FIELDS:
        clean.pop(field, None)
    return clean


class ProfileRepo:
    """E10~E13 的唯一读写出口。"""

    def __init__(self, db: Any):
        self.db = db
        self.users = db[COLL_USERS]
        self.devices = db[COLL_DEVICES]
        self.ip_pool = db[COLL_IP_POOL]
        self.addresses = db[COLL_USER_ADDRESSES]

    # ---------------- 集合选择 ----------------
    def collection_for(self, entity_type: str) -> Any:
        """按实体类型取集合；未知类型返回 `None`（判定与报错在服务层）。"""
        name = ENTITY_COLLECTIONS.get(entity_type)
        return None if name is None else self.db[name]

    # ---------------- 读（§3.1 画像卡的四个来源） ----------------
    async def get_user(self, user_id: str) -> Optional[dict]:
        return await self._find_one(self.users, user_id, op="get_user")

    async def get_device(self, device_id: str) -> Optional[dict]:
        return await self._find_one(self.devices, device_id, op="get_device")

    async def get_ip(self, ip: str) -> Optional[dict]:
        return await self._find_one(self.ip_pool, ip, op="get_ip")

    async def get_address(self, address_id: str) -> Optional[dict]:
        doc = await self._find_one(self.addresses, address_id, op="get_address")
        return sanitize_address(doc)

    async def find_address_of_user(self, user_id: str) -> Optional[dict]:
        """按 `user_id` 反查收货地址（走 `ix_user`，E13 的主键是 `address_id`）。

        取**最近见过**的一条（`last_seen_at` 降序）：一个账号可能先后用过多个
        收货地址，画像卡只展示一个位置，展示最近使用的那个最不容易误导——
        而"最近"这个依据必须来自数据（`last_seen_at`），不能靠随机取一条。
        """
        try:
            cursor = (
                self.addresses.find({"user_id": user_id})
                .sort([("last_seen_at", -1), ("_id", 1)])
                .limit(1)
            )
            rows = await cursor.to_list(length=1)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="find_address_of_user") from e
        return sanitize_address(rows[0]) if rows else None

    async def find_users_by_phone(self, phone: str, *, exclude_user_id: Optional[str] = None,
                                  limit: int = 20) -> list[dict]:
        """按**脱敏手机号**找其它账号（建 `same_phone` 边用，BR-09-09）。

        E10 的 `phone` 存的就是脱敏值（BR-09-05），因此这里比对的是掩码串。
        没有建 `phone` 索引：脱敏手机号是低区分度键（前 3 + 后 4），
        单键索引的收益有限，而"带 `limit` 的全集合扫描"在演示规模下足够；
        真要上量应当引入不可逆的 `phone_hash`（属数据实体变更，见交付说明）。

        `limit` 由调用方给定且**必须给**：一次登录事件不该有能力拖出成千上万条边。
        """
        flt: dict[str, Any] = {"phone": str(phone)}
        if exclude_user_id:
            flt["_id"] = {"$ne": str(exclude_user_id)}
        try:
            cursor = self.users.find(flt, {"_id": 1, "phone": 1}).limit(max(1, int(limit)))
            return await cursor.to_list(length=max(1, int(limit)))
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="find_users_by_phone") from e

    async def _find_one(self, col: Any, key: str, *, op: str) -> Optional[dict]:
        try:
            return await col.find_one({"_id": str(key)})
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op=op) from e

    # ---------------- 写：实体行的"最小够用"创建 ----------------
    async def ensure_ip(self, ip: str, now: int) -> bool:
        """确保 E12 有该 IP 的行，且 `is_proxy` / `is_idc` **是显式布尔值**。

        返回 `True` 表示本次**新建**了该行（调用方据此决定要不要处理归属/代理
        判定——当前无外部 IP 库，见模块 docstring 的说明）。
        """
        try:
            result = await self.ip_pool.update_one(
                {"_id": str(ip)},
                [{"$set": {
                    # D46 的落地处：显式 `False` 而不是"字段缺失"。
                    # `$ifNull` 保证已有的 `true`（真实代理 IP）绝不被覆盖。
                    "is_proxy": {"$ifNull": ["$is_proxy", False]},
                    "is_idc": {"$ifNull": ["$is_idc", False]},
                    "linked_user_cnt": {"$ifNull": ["$linked_user_cnt", 0]},
                    "first_seen_at": {"$ifNull": ["$first_seen_at", now]},
                    "last_seen_at": now,
                    # `region` / `isp` 没有数据源（无外部 IP 库）：显式占位为 None，
                    # **不编造**。前端按 `null` 显示「—」。
                    "region": {"$ifNull": ["$region", None]},
                    "isp": {"$ifNull": ["$isp", None]},
                }}],
                upsert=True,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="ensure_ip") from e
        return result.upserted_id is not None

    async def ensure_device(self, device_id: str, now: int,
                            *, ua: Optional[str] = None) -> bool:
        """确保 E11 有该设备的行。返回是否新建。

        `fingerprint` / `os` 没有数据源（事件里只有 `device_id`，`login` 事件带
        `ua`）：有 `ua` 就写入，没有就留 `None`——**不编造指纹**。
        """
        fields: dict[str, Any] = {
            "linked_user_cnt": {"$ifNull": ["$linked_user_cnt", 0]},
            "first_seen_at": {"$ifNull": ["$first_seen_at", now]},
            "last_seen_at": now,
            "risk_level": {"$ifNull": ["$risk_level", None]},
            "fingerprint": {"$ifNull": ["$fingerprint", None]},
            "os": {"$ifNull": ["$os", None]},
        }
        if ua:
            # 只在"还没记录过"时补 ua：设备指纹类字段随第一次观测定下来，
            # 后续被换 UA 覆盖会让画像失真
            fields["ua"] = {"$ifNull": ["$ua", str(ua)[:200]]}
        try:
            result = await self.devices.update_one(
                {"_id": str(device_id)}, [{"$set": fields}], upsert=True,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="ensure_device") from e
        return result.upserted_id is not None

    async def ensure_address(self, address_id: str, user_id: str, now: int) -> bool:
        """确保 E13 有该地址的行。返回是否新建。

        **不写任何明文地址**（BR-09-05）：事件里只有 `address_id`，没有收货详细
        地址，因此这里只登记归属与计数，`detail_hash` / 省市区的填充留给真正
        拿到地址文本的链路（或种子数据）。`user_id` 用 `$ifNull` 保留**首个**
        观测到的归属账号，避免同一地址在多个账号间反复改主。
        """
        try:
            result = await self.addresses.update_one(
                {"_id": str(address_id)},
                [{"$set": {
                    "user_id": {"$ifNull": ["$user_id", str(user_id)]},
                    "linked_user_cnt": {"$ifNull": ["$linked_user_cnt", 0]},
                    "aftersale_cnt": {"$ifNull": ["$aftersale_cnt", 0]},
                    "first_seen_at": {"$ifNull": ["$first_seen_at", now]},
                    "last_seen_at": now,
                    # 无地址文本可哈希 → 显式 None，绝不写入明文或猜测的哈希
                    "detail_hash": {"$ifNull": ["$detail_hash", None]},
                }}],
                upsert=True,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="ensure_address") from e
        return result.upserted_id is not None

    # ---------------- 写：冗余计数与累计统计 ----------------
    async def inc_linked_user_cnt(self, entity_type: str, entity_id: str,
                                  delta: int = 1) -> int:
        """`linked_user_cnt` 递增（BR-09-12 的冗余计数）。

        ⚠️ **只允许在"边首次插入"时调用**（BR-09-08）。重复上报必须**不**递增，
        否则同一对 user-device 上报 100 次就会得到 `linked_user_cnt=100`，
        08 页面上显示"该设备关联 100 个账号"——一个纯属虚构的团伙规模。
        判断"是否首次"的依据是 `GraphRepo.upsert_edge()` 的返回值。

        返回本次文档的最终计数（取不到时返回 0，仅用于日志）。
        """
        col = self.collection_for(entity_type)
        if col is None:
            raise _unavailable(f"未知实体类型 {entity_type}", op="inc_linked_user_cnt")
        try:
            doc = await col.find_one_and_update(
                {"_id": str(entity_id)},
                {"$inc": {"linked_user_cnt": int(delta)}},
                upsert=False,
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="inc_linked_user_cnt") from e
        return int((doc or {}).get("linked_user_cnt") or 0)

    async def inc_stats(self, user_id: str, updates: Mapping[str, int]) -> None:
        """按字段批量累加 `users.stat`（BR-09-01/02）。

        一次 `$inc` 带多个字段而不是逐字段往返：`order_create` 要同时加
        `order_cnt` 与 `total_amount`，分两次更新会出现"下单数加了、金额没加"
        的中间态（后台任务失败时尤其明显），对账时表现为金额与单数不匹配。

        `upsert=True`：事件侧出现的账号可能还没有画像行（注册资料在业务系统里，
        我们只知道它产生过事件）。**只建 `stat`，不编造 `register_at`/`level`**——
        画像卡对缺失项显示「—」（§3.1 的 null 语义）。
        """
        if not updates:
            return
        try:
            await self.users.update_one(
                {"_id": str(user_id)},
                {"$inc": {f"stat.{field}": int(delta) for field, delta in updates.items()}},
                upsert=True,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="inc_stats") from e

    async def inc_stat(self, user_id: str, field: str, delta: int = 1) -> None:
        """单字段累加（§3.3 的 `bump_stat(user_id, field, delta)` 契约）。"""
        await self.inc_stats(user_id, {field: delta})

    # ---------------- 写：标签（集合语义） ----------------
    async def add_tag(self, user_id: str, tag: str) -> bool:
        """打标签（`$addToSet`，BR-09-03）。返回**是否新增**。

        `$addToSet` 天然去重：同一标签打两次，第二次 `modified_count == 0`，
        数组里仍只有一个（`V-09-13` 前半段直接验这一点）。
        用 `$addToSet` 而不是"先读数组再决定是否 push"：后者在并发下必然漏判。
        """
        try:
            result = await self.users.update_one(
                {"_id": str(user_id)},
                {"$addToSet": {"risk_tags": str(tag)}},
                upsert=True,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="add_tag") from e
        return int(getattr(result, "modified_count", 0)) > 0

    async def remove_tag(self, user_id: str, tag: str) -> bool:
        """显式移除标签（BR-09-04 的"误打需通过管理操作显式移除"）。

        `upsert=False`：给一个不存在的用户"移除标签"不该凭空建出一行来。
        返回是否真的移除了（`False` 时调用方据此决定要不要写审计）。
        """
        try:
            result = await self.users.update_one(
                {"_id": str(user_id)}, {"$pull": {"risk_tags": str(tag)}}, upsert=False,
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="remove_tag") from e
        return int(getattr(result, "modified_count", 0)) > 0

    # ---------------- 写：最近决策（覆盖式） ----------------
    async def set_latest_decision(self, user_id: str, block: Mapping[str, Any],
                                  decided_at: int, *, history_max: int = RISK_SCORE_HISTORY_MAX) -> None:
        """覆盖式写 `users.latest_decision`（BR-09-06：只保留最近一次）。

        **历史不做数组**（E03 `decisions` 才是历史真源），但 E10 另有一个
        `risk_score_history`（"最近 N 次决策分数，画风险趋势"），因此这里顺手
        用 `$push + $slice` 维护它——有界（`history_max`）是必须的，无界数组
        会把文档顶到 16MB 上限，而"风险趋势"只需要最近若干次。

        `$slice: -N` 表示"只保留最后 N 个"：Mongo 在 `$push` 的同一次原子操作里
        完成截断，不需要先读再写。
        """
        doc: dict[str, Any] = {"latest_decision": dict(block)}
        update: dict[str, Any] = {"$set": doc}
        score = block.get("risk_score")
        if score is not None and not isinstance(score, bool):
            update["$push"] = {
                "risk_score_history": {
                    "$each": [{"score": score, "decided_at": int(decided_at)}],
                    "$slice": -int(history_max),
                }
            }
        try:
            await self.users.update_one({"_id": str(user_id)}, update, upsert=True)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="set_latest_decision") from e

    # ---------------- 供测试与排障使用 ----------------
    async def clear_user_documents(self, user_ids: Iterable[str]) -> int:
        """删除若干用户画像行（仅供测试夹具体复位使用）。"""
        wanted = [str(u) for u in user_ids]
        if not wanted:
            return 0
        result = await self.users.delete_many({"_id": {"$in": wanted}})
        return int(result.deleted_count)


__all__ = [
    "ENTITY_COLLECTIONS",
    "ProfileRepo",
    "RISK_SCORE_HISTORY_MAX",
    "sanitize_address",
]
