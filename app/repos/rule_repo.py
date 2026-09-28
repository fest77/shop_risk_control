# -*- coding: utf-8 -*-
"""E05 `rules` / E06 `rule_scenes` 的仓储：**读取侧归 05，写入侧归 06**（Spec §6）。

## 为什么分成两个类

`RuleRepo`（本文件上半部分）是**模块 05 决策链路**的只读出口；`RuleAdminRepo`
（下半部分）是**模块 06-B 规则配置**的写入侧与管理侧读取。两者放同一个文件是
Spec §6 的文件规划（"`rule_repo.py` # rules / rule_scenes 写入侧仓储（读取侧归 05）"），
但**必须是两个类**，因为它们的故障语义相反：

- 决策链路读到规则为空 → 必须 fail-closed 降级（`RUL-5002`，绝不返回 `pass`）；
- 管理页面读到规则为空 → 只是"列表加载失败"，保留旧数据 + `COM-5001`。

一个类里混着两套语义，迟早会有人在决策链路上拿到 `CFG-` 码、或在管理页拿到
`RUL-5002`，而这两类故障的处置完全不同（前者转人审，后者重试）。

## 05 的只读出口**不含写方法**

写入（新增/改分/启停用/软删）必须带审计留痕（BR-06-36）与版本递增（BR-06-03），
因此它们只存在于 `RuleAdminRepo` 并被 `RuleService` 包裹；在 05 的只读出口上留
一堆半成品写方法，只会诱使别处绕过 06 的审计直接改规则。

## 读失败为什么必须抛 `RUL-5002` 而不是返回空列表

"读不到规则"与"没有规则命中"在数据上都表现为"零条规则"，但对风控的含义完全
相反：前者是**不知道**（可能正有一条高危规则没读到），后者是**结论**（确实是
干净的）。返回 `[]` 会把前者悄悄变成后者，于是 Mongo 抖动一次就等于把全部规则
停用——这是本模块最危险的一类缺陷，与 04 "不得用 0 冒充缺失"同源。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import (
    COLL_DECISION_HITS,
    COLL_DECISIONS,
    COLL_RULES,
    COLL_RULE_SCENES,
)
from app.errors import AppError, RuleSetUnavailableError
from app.logging import get_logger

log = get_logger("shop_risk_control.rule_repo")

#: 跨场景通用的场景码 —— **E06 里真实存在的一行数据**（决策 D10：`common`
#: 必须以数据行存在，由 `/common/enums` 下发、由种子灌入）。
#:
#: ## 它为什么不算"在代码里特判字符串"
#:
#: D10 禁止的是**行为分支**上的特判（例如 `if scene == "common": 跳过某些检查`）。
#: 这里它只是一个**数据键**：用来拼出本次要取的 `scene_code` 集合
#: （`{"$in": [本次场景, COMMON]}`），换任何场景都不改变任何代码路径——
#: 把它改成别的字符串，行为只是"取不到那一组通用规则"，与数据库里少一行
#: `common` 完全等价。这正说明通用性来自**数据**而不是代码。
#:
#: 因此它是全项目唯一一处该字面量，且**必须**与 `scripts/seed.py` 的
#: `SEED_SCENES` 保持一致（种子少灌一行，通用规则就永远不参与决策）。
COMMON_SCENE_CODE = "common"

#: 参与决策的规则状态（E05.status 只有 enabled/disabled，没有第三个值）
ENABLED_STATUS = "enabled"


class RuleRepo:
    """`rules` 与 `rule_scenes` 的唯一读取出口。"""

    def __init__(self, db: Any):
        self.db = db
        self.rules = db[COLL_RULES]
        self.scenes = db[COLL_RULE_SCENES]

    async def list_enabled_rules(self, scene_code: str) -> list[dict]:
        """取本次参与求值的规则（BR-05-09）。

        条件 = `status=enabled` **且** `scene_code ∈ {本次场景, common}`。

        ## 排序为什么是 `priority` 升序 + `_id` 升序

        `priority` **只用于稳定排序与展示，绝不用于短路**（BR-05-14）。加分制
        要求全量求值才能得到总分：一旦"高优先级命中就停下"，总分就取决于
        规则顺序而不是命中了几条，`rule_score` 与 `hit_rule_count` 会同时失真。
        所以这里排序的唯一目的是让 `hits`、`trace` 与 `rule_versions` 的输出
        **顺序稳定**（同一份数据每次跑出来的明细顺序一致，便于比对与重放）；
        `_id` 作为第二排序键是为了在 `priority` 相同时也有确定顺序——
        只按 `priority` 排时，Mongo 对同值文档的返回顺序**没有保证**。

        ## 为什么不过滤 `status` 之外的任何东西

        E05 没有软删字段（`is_system` 只是"不可删"的标记），停用就是 `disabled`。
        这里若顺手加上别的条件（例如"只取 is_system=true"），会让某些规则
        静默退出决策而无人察觉。
        """
        try:
            cursor = self.rules.find({
                "status": ENABLED_STATUS,
                "scene_code": {"$in": [scene_code, COMMON_SCENE_CODE]},
            }).sort([("priority", 1), ("_id", 1)])
            return await cursor.to_list(length=1000)
        except PyMongoError as e:
            # fail-closed：读不到规则**不是**"没有规则"（见模块 docstring）
            raise RuleSetUnavailableError(f"rules 读取失败：{type(e).__name__}: {e}") from e

    async def find_rule(self, rule_code: str) -> Optional[dict]:
        """按规则编码取一条（供排障与"改规则后回看历史决策"的验证使用）。"""
        try:
            return await self.rules.find_one({"_id": rule_code})
        except PyMongoError as e:
            raise RuleSetUnavailableError(f"rules 读取失败：{type(e).__name__}: {e}") from e

    async def list_rules(self, flt: Optional[dict] = None) -> list[dict]:
        """按条件列出规则（诊断用，不做业务筛选；排序稳定便于比对）。"""
        try:
            cursor = self.rules.find(dict(flt or {})).sort([("_id", 1)])
            return await cursor.to_list(length=1000)
        except PyMongoError as e:
            raise RuleSetUnavailableError(f"rules 读取失败：{type(e).__name__}: {e}") from e


#: 管理侧读失败统一用模块 00 的 `COM-5001`（依赖不可用），**不借 05 的 `RUL-5002`**：
#: `RUL-5002` 的语义是"决策引擎加载规则集失败 → 本次决策降级 review"，把它用在
#: 管理页的列表查询上，会让告警侧把一次"页面刷新失败"统计成一次决策降级。
#: ER-02 要求借用别人的码时保持原前缀，故这里就是 `COM-` 前缀（与 `list_service`
#: 的读路径完全同一口径）。
_MSG_ADMIN_READ_UNAVAILABLE = "规则数据暂时不可用，请稍后重试"


def _read_error(op: str, e: Exception) -> AppError:
    log.error("rules 管理侧读取失败 op=%s：%s", op, f"{type(e).__name__}: {e}")
    return AppError("COM-5001", _MSG_ADMIN_READ_UNAVAILABLE, 503)


#: 规则编码格式（BR-06-01）：`R{场景码大写}{3位序号}`。生成与"取下一个序号"共用
#: 同一份定义，避免生成用一套正则、解析用另一套（那样会出现"生成的编码解析不出序号"，
#: 下一次生成又从头开始，直接撞唯一索引）。
_RULE_CODE_RE = re.compile(r"^R(.+?)(\d{3})$")


def rule_code_for(scene_code: str, seq: int) -> str:
    """按 BR-06-01 拼出规则编码（`R` + 场景码大写 + 3 位序号）。"""
    return f"R{scene_code.upper()}{seq:03d}"


def parse_rule_seq(rule_code: str, scene_code: str) -> Optional[int]:
    """从规则编码里取序号；不属于该场景或格式不符时返回 `None`。

    取序号而不是用"条数 +1"：软删除的规则**仍留在库里**（BR-06-10），条数会
    因为已删除的规则而少算，于是新规则会复用旧编码——BR-06-01 明确禁止复用。
    因此这里按 `_id` 取**最大值**，删除过的编码自然被跳过。
    """
    if not rule_code or not rule_code.startswith("R"):
        return None
    prefix = scene_code.upper()
    body = rule_code[1:]
    if not body.startswith(prefix):
        return None
    tail = body[len(prefix):]
    if len(tail) != 3 or not tail.isdigit():
        return None
    return int(tail)


class RuleAdminRepo:
    """`rules` / `rule_scenes` 的写入侧与管理侧读取（模块 06-B）。

    本类**只做数据库交互，不含任何业务判断**（唯一性、版本、软删语义都在
    `RuleService`）：这样"业务不变量"可以注入假仓储直接单测，而不必起 Mongo。
    唯一的例外是 `insert` 对 `DuplicateKeyError` 的**透传**——那是数据库层的
    约束信号，必须由服务层决定怎么处置（重算序号重试）。
    """

    def __init__(self, db: Any):
        self.db = db
        self.rules = db[COLL_RULES]
        self.scenes = db[COLL_RULE_SCENES]
        self.hits = db[COLL_DECISION_HITS]
        self.decisions = db[COLL_DECISIONS]

    # ---------------- E06 规则场景（只读：D24 数据驱动） ----------------
    async def scene_rows(self) -> list[dict]:
        """取全部场景行（按 `sort` 升序，与 `/common/enums` 的下发顺序一致）。"""
        try:
            cursor = self.scenes.find({}, {"name": 1, "sort": 1}).sort("sort", 1)
            return await cursor.to_list(length=100)
        except PyMongoError as e:
            raise _read_error("scene_rows", e) from e

    async def scene_names(self) -> dict[str, str]:
        """`{场景码: 场景名}`，供列表页把 `scene_code` 渲染成 `scene_name`。

        一次取全量而不是逐条查：列表一页 20 条规则若逐条查场景名，就是 20 次
        多余的往返；而场景字典只有个位数行。
        """
        return {str(r["_id"]): str(r.get("name") or r["_id"]) for r in await self.scene_rows()}

    # ---------------- 管理侧读 ----------------
    async def find_by_code(self, rule_code: str) -> Optional[dict]:
        """按编码取一条（含已软删的；是否可见由服务层判定）。

        必须返回**含已删除**的文档：删除要报 `404 CFG-4001` 还是 `400 CFG-4005`
        （内置规则不可删）取决于这条规则**是否存在**；若这里把软删的过滤掉，
        对一条内置规则重复点删除就会从"不可删除"变成"不存在"，错误码随之漂移。
        """
        try:
            return await self.rules.find_one({"_id": rule_code})
        except PyMongoError as e:
            raise _read_error("find_by_code", e) from e

    async def query(
        self, flt: dict, sort: list[tuple[str, int]], skip: int, limit: int
    ) -> list[dict]:
        try:
            cursor = self.rules.find(flt).sort(sort).skip(skip).limit(limit)
            return await cursor.to_list(length=limit)
        except PyMongoError as e:
            raise _read_error("query", e) from e

    async def count(self, flt: dict) -> int:
        try:
            return await self.rules.count_documents(flt)
        except PyMongoError as e:
            raise _read_error("count", e) from e

    async def max_seq(self, scene_code: str) -> int:
        """该场景下**已用过的最大序号**（BR-06-01：递增且不复用）。

        按 `_id` 倒序取一条即可：编码同前缀时字典序与数值序一致（序号定长 3 位）。
        只投影 `_id`，不把整棵条件树拉回来。
        """
        prefix = f"R{scene_code.upper()}"
        try:
            cursor = self.rules.find(
                {"_id": {"$regex": f"^{re.escape(prefix)}\\d{{3}}$"}}, {"_id": 1}
            ).sort([("_id", -1)]).limit(1)
            rows = await cursor.to_list(length=1)
        except PyMongoError as e:
            raise _read_error("max_seq", e) from e
        if not rows:
            return 0
        seq = parse_rule_seq(str(rows[0]["_id"]), scene_code)
        return seq or 0

    async def enabled_score_sums(self) -> list[dict]:
        """按场景聚合**启用**规则的分值合计（BR-06-12 的非阻断提示）。

        聚合而不是把全部启用规则拉回进程内求和：规则数会增长，而这个数字每次
        翻页都要算一遍。
        """
        try:
            cursor = await self.rules.aggregate([
                {"$match": {"status": ENABLED_STATUS, "deleted": {"$ne": True}}},
                {"$group": {"_id": "$scene_code",
                            "score_sum": {"$sum": "$score"},
                            "rule_count": {"$sum": 1}}},
            ])
            return await cursor.to_list(length=100)
        except PyMongoError as e:
            raise _read_error("enabled_score_sums", e) from e

    # ---------------- 影响面（删除前的预览，Spec §5.2） ----------------
    async def hit_count_since(self, rule_code: str, since_ms: int) -> int:
        """近 N 天该规则的命中次数（删除确认弹窗要展示「近 30 天命中次数」）。"""
        try:
            return await self.hits.count_documents(
                {"rule_code": rule_code, "hit_at": {"$gte": int(since_ms)}}
            )
        except PyMongoError as e:
            raise _read_error("hit_count_since", e) from e

    async def decision_ref_count(self, rule_code: str) -> int:
        """引用过该规则的决策数（`decisions.rule_versions` 快照的键，D60）。

        这是"允许删除但必须给影响面预览"的依据（Spec §3.1 DELETE 约束）：历史
        决策**不受影响**（`decision_hits` 是快照，BR-05-21），但用户有权知道
        有多少条历史记录点了它的名。
        """
        if not re.fullmatch(r"[A-Za-z0-9_]+", rule_code or ""):
            # 编码里出现 `.` 会被 Mongo 当成嵌套路径，把一次统计变成一次误命中。
            # 自建编码不可能长这样（`R{场景}{3位}`），但种子或人工导入的数据可能，
            # 因此这里如实返回 0 而不是构造一条会走错的查询。
            return 0
        try:
            return await self.decisions.count_documents(
                {f"rule_versions.{rule_code}": {"$exists": True}}
            )
        except PyMongoError as e:
            raise _read_error("decision_ref_count", e) from e

    # ---------------- 写 ----------------
    async def insert(self, doc: dict) -> str:
        """插入一条规则。并发重号时由 `rules._id` 主键抛 `DuplicateKeyError`。"""
        await self.rules.insert_one(doc)
        return str(doc["_id"])

    async def update_with_version(
        self, rule_code: str, expected_version: int, changes: dict
    ) -> int:
        """**带版本前提的条件更新**，返回匹配条数（BR-06-05 的乐观锁载体）。

        E05 唯一的并发载体就是 `version` 列，因此"改动"与"版本校验"必须在
        **同一条更新语句**里完成：先查版本再更新（两条语句）在并发下必然漏判，
        两个策略师会双双通过校验、后者覆盖前者。

        `version` 用 `$inc` 而不是 `$set: expected+1`：`$inc` 与条件里的
        `version: expected` 共同保证"只有我读到的那一版才被推进到 expected+1"。
        """
        result = await self.rules.update_one(
            {"_id": rule_code, "version": int(expected_version),
             "deleted": {"$ne": True}},
            {"$set": dict(changes), "$inc": {"version": 1}},
        )
        return int(result.modified_count)

    async def restore_after_failed_audit(
        self, rule_code: str, version_after: int, before: dict,
        unset: tuple[str, ...] = (),
    ) -> int:
        """审计失败后的补偿：把文档**按新版本号**回滚成 `before` 的内容。

        为什么条件带 `version = version_after`：回滚只能撤销**本次**那一次写入。
        若期间有人又改过（版本已不是本次写出来的那一版），说明那次修改是**别人的
        变更**，回滚它会抹掉别人的成果——此时匹配 0 条并如实在响应里提示人工核对，
        比"覆盖成我记忆里的样子"正确得多（与 `list_repo.restore_active` 同一立场）。

        `unset` 用于回滚"删除标记"这类**本次新增的字段**：软删写下了
        `deleted_at` / `deleted_by`，回滚时把 `deleted` 置回 `False` 却留着
        `deleted_at`，会让"这条规则什么时候被删过"永远留下一个假时刻。
        """
        update: dict[str, Any] = {"$set": dict(before)}
        if unset:
            update["$unset"] = {field: "" for field in unset}
        result = await self.rules.update_one(
            {"_id": rule_code, "version": int(version_after)},
            update,
        )
        return int(result.modified_count)

    async def delete_by_code(self, rule_code: str) -> int:
        """**物理删除**。仅用于"插入成功但审计留不下痕迹"时的补偿回滚。

        为什么这里可以物理删：这次写入业务上等于**从未发生**（BR-06-36 宁可不做，
        不可无痕地做），留一条孤儿文档反而会让"库里为什么有一条没审计的规则"
        成为排查负担。而用户的**主动删除**走软删（BR-06-10），绝不经过本方法。
        """
        result = await self.rules.delete_one({"_id": rule_code})
        return int(result.deleted_count)


__all__ = [
    "COMMON_SCENE_CODE", "ENABLED_STATUS", "RuleRepo", "RuleAdminRepo",
    "rule_code_for", "parse_rule_seq",
]
