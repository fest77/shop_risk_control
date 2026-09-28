# -*- coding: utf-8 -*-
"""关联网络查询：1 跳 + 2 跳两批查询、裁剪、节点属性批量补齐（模块 09 §4.4 / §6）。

## 查询次数为什么是**常数 3 次**（BR-09-16 / `V-09-07`）

| # | 命令 | 说明 |
|---|---|---|
| 1 | `entity_edges.find($or[from, to])` | 第 1 跳：与中心直接相连的边（一次往返，两个分支各走 `ix_from`/`ix_to`） |
| 2 | `entity_edges.find($or[from_id $in, to_id $in])` | 第 2 跳：与**任意**一跳节点相连的边（一条命令覆盖任意多个一跳节点） |
| 3 | `users.aggregate([... $unionWith devices/ip_pool/user_addresses])` | 节点属性批量补齐（BR-09-20） |

三者都不随节点数增长：第 2 跳用 `$in` 而不是"逐个一跳节点查一次"，属性补齐用
`$unionWith`（MongoDB 4.4+，本机 7.0）而不是"四个集合各查一次"。
**疑似截断时**额外 1 次聚合（第 4 次）取真实的 `total_*`——见下面的说明。

## 为什么"疑似截断"要多花一次查询

BR-09-17 要求截断时 `total_*` 给的是**截断前的真实数量**（前端文案
「已展示关联强度最高的 200 个节点（共 N 个）」里那个 N）。若为了省一次往返而
只报"≥ 上限"，文案就会变成"共 501 个"这种**看起来精确的假数字**——人工研判
会据此低估团伙规模。因此：
- **常规情况**：边全部取回，`total_*` 直接由取回结果数出，共 3 次查询；
- **疑似截断**（某次取回条数触及上限）：追加 1 条 `$facet` 聚合，
  一次算准边总数与去重端点数，共 4 次查询（仍是常数，`V-09-07` 的 ≤4 成立）。

## 图的空态与 404 是两件事（`V-09-09`）

- 实体存在但没有任何边 → **200** + `nodes=[center]`、`edges=[]`
  （§2.2：显示「该用户暂无关联实体（孤立账号）」）；
- 实体不存在 → **404 `GRP-4004`**（显示「未找到该用户/实体」）。

判定"实体是否存在"用的是第 3 条命令的结果（中心节点本来就必须取属性）。
**但属性批量补齐失败时（`GRP-5004`）绝不能因此报 404**——那会把一次查询故障
说成"这个人不存在"。此时回落到一次单点存在性查询，宁可多一次往返。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Mapping, Optional

from app import db as db_module
from app.errors import AppError, GraphEntityTypeError, HopLimitExceededError, grp_error
from app.logging import get_logger
from app.repos.graph_repo import GraphRepo, edge_endpoints, other_endpoint
from app.services.profile_service import masked_detail_of

log = get_logger("shop_risk_control.graph.service")

#: `entity_type` 的合法取值（§3.2）。**不含 `phone`**：手机号是用户属性，
#: 不作为独立图节点（Step1 §4 / E14 的编码约束），因此显式拒绝而不是降级。
ALLOWED_ENTITY_TYPES: tuple[str, ...] = ("user", "device", "ip", "address")

#: 跳数硬上限（AD-06 / BR-09-15）。>2 直接拒绝：更高跳数会指数膨胀，
#: 而 2 跳已覆盖"同设备 → 这些账号的其他设备"这一团伙发现主场景。
MAX_HOP = 2
DEFAULT_MAX_HOP = 2

#: 节点/边上限（§3.2 参数表）
DEFAULT_MAX_NODES = 200
MAX_NODES_CAP = 500
DEFAULT_MAX_EDGES = 500
MAX_EDGES_CAP = 1000

#: 图查询超时（§5 的 `GRP-5002`：>3s 返回**部分结果** + `truncated=true`）
QUERY_TIMEOUT_SEC = 3.0


def _bad_param(message: str, path: str, extra: Optional[dict] = None) -> AppError:
    """参数越界 → `COM-4001`（422）。

    为什么是拒绝而不是**静默夹取**：调用方传了 `max_nodes=9999` 却拿到 500 个节点，
    会以为自己看到的是一张完整的图（`max_nodes` 是"我接受多少"的声明）。
    模块 11 的 `MET-4003`（top 越界）已经就同一问题做过裁定：**以拒绝为准**。
    """
    data: dict[str, Any] = {"errors": [{"path": path, "message": message}]}
    if extra:
        data.update(extra)
    return AppError("COM-4001", f"参数校验失败：{message}", 422, data)


class GraphService:
    """图查询服务（进程内单例，见文件末尾）。"""

    def __init__(self, repo: Optional[GraphRepo] = None,
                 profile_repo: Any = None) -> None:
        self._repo = repo
        #: 存在性兜底查询用（属性批量补齐失败时）。`None` = 每次从当前库取。
        self._profile_repo = profile_repo

    # ---------------- 依赖装配 ----------------
    def _r(self) -> GraphRepo:
        return self._repo if self._repo is not None else GraphRepo(db_module.get_db())

    def _p(self) -> Any:
        if self._profile_repo is not None:
            return self._profile_repo
        from app.repos.profile_repo import ProfileRepo

        return ProfileRepo(db_module.get_db())

    def configure(self, *, repo: Optional[GraphRepo] = None,
                  profile_repo: Any = None) -> None:
        """替换依赖（测试注入用：可计数的 repo 代理就走这里）。"""
        if repo is not None:
            self._repo = repo
        if profile_repo is not None:
            self._profile_repo = profile_repo

    def reset_dependencies(self) -> None:
        """还原成"每次从当前数据库取"（测试收尾用，理由同 `ProfileService`）。"""
        self._repo = None
        self._profile_repo = None

    # ---------------- §3.2 主查询 ----------------
    async def query(
        self,
        entity_type: str,
        entity_id: str,
        *,
        max_hop: int = DEFAULT_MAX_HOP,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_edges: int = DEFAULT_MAX_EDGES,
        risk_only: bool = False,
    ) -> dict:
        """查 `entity_type/entity_id` 的关联网络（1~2 跳）。"""
        started = time.perf_counter()
        etype = str(entity_type or "").strip()
        eid = str(entity_id or "").strip()
        if etype not in ALLOWED_ENTITY_TYPES:
            raise GraphEntityTypeError(etype, ALLOWED_ENTITY_TYPES)
        if not eid:
            raise grp_error("GRP-4004", "未找到该用户/实体",
                            {"entity_type": etype, "entity_id": eid})
        if isinstance(max_hop, bool) or int(max_hop) < 1 or int(max_hop) > MAX_HOP:
            raise HopLimitExceededError(int(max_hop), MAX_HOP)
        if not (1 <= int(max_nodes) <= MAX_NODES_CAP):
            raise _bad_param(f"max_nodes 需在 1~{MAX_NODES_CAP}", "max_nodes",
                             {"max_nodes": int(max_nodes), "limit": MAX_NODES_CAP})
        if not (1 <= int(max_edges) <= MAX_EDGES_CAP):
            raise _bad_param(f"max_edges 需在 1~{MAX_EDGES_CAP}", "max_edges",
                             {"max_edges": int(max_edges), "limit": MAX_EDGES_CAP})

        result = await self._collect(
            etype, eid, int(max_hop), int(max_nodes), int(max_edges), bool(risk_only),
            started,
        )
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    async def _collect(
        self, etype: str, eid: str, max_hop: int, max_nodes: int, max_edges: int,
        risk_only: bool, started: float,
    ) -> dict:
        """三次（必要时四次）命令把图取回来，再做**纯内存**的裁剪与组装。

        超时预算用"剩余时间"逐命令递减：每一条命令都受同一个总预算约束，
        而不是每条各给 3 秒（那会让总耗时无上界）。
        """
        repo = self._r()
        deadline = time.monotonic() + QUERY_TIMEOUT_SEC
        timed_out = False

        async def _limited(coro: Any) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(coro, remaining)

        # ---------------- 第 1 跳：与中心直接相连的边 ----------------
        # 取 `max_edges + 1` 条：多出来的那一条**只为判定"是否还有更多"**
        # （取回条数触及上限即说明发生了截断，此时才去补真实总数）
        fetch_limit = max_edges + 1
        raw_edges: list[dict] = []
        try:
            hop1_raw = await _limited(
                repo.edges_touching(etype, eid, limit=fetch_limit, risk_only=risk_only)
            )
        except asyncio.TimeoutError:
            hop1_raw, timed_out = [], True
            log.warning("[GRP-5002] 图查询超时（第 1 跳 >%.1fs）center=%s/%s",
                        QUERY_TIMEOUT_SEC, etype, eid)
        raw_edges.extend(hop1_raw)

        hop1_nodes: set[tuple[str, str]] = set()
        for edge in hop1_raw:
            other = other_endpoint(edge, etype, eid)
            if other is not None:
                hop1_nodes.add(other)

        # ---------------- 第 2 跳：批量取一跳节点的邻居 ----------------
        hop2_nodes: set[tuple[str, str]] = set()
        hop2_raw: list[dict] = []
        if max_hop >= 2 and hop1_nodes and not timed_out:
            try:
                hop2_raw = await _limited(repo.edges_among_pairs(
                    hop1_nodes, limit=fetch_limit, risk_only=risk_only,
                ))
            except asyncio.TimeoutError:
                timed_out = True
                log.warning("[GRP-5002] 图查询超时（第 2 跳 >%.1fs）center=%s/%s",
                            QUERY_TIMEOUT_SEC, etype, eid)
            raw_edges.extend(hop2_raw)
            for edge in hop2_raw:
                for endpoint in edge_endpoints(edge):
                    if endpoint != (etype, eid) and endpoint not in hop1_nodes:
                        hop2_nodes.add(endpoint)

        # 按 `_id` 去重（一跳边会同时出现在两批结果里：一跳节点自己也连着别的边）
        edges_by_id: dict[str, dict] = {}
        for edge in raw_edges:
            key = str(edge.get("_id"))
            edges_by_id.setdefault(key, edge)
        all_edges = list(edges_by_id.values())

        node_keys: list[tuple[str, str]] = [(etype, eid)]
        node_keys += sorted(hop1_nodes)
        node_keys += sorted(hop2_nodes - hop1_nodes)
        node_hop: dict[tuple[str, str], int] = {
            (etype, eid): 0,
            **{key: 1 for key in hop1_nodes},
            **{key: 2 for key in hop2_nodes if key not in hop1_nodes},
        }

        # ---------------- 第 3 跳命令：节点属性批量补齐（一条命令） ----------------
        ids_by_type: dict[str, list[str]] = {}
        for ntype, nid in node_keys:
            ids_by_type.setdefault(ntype, []).append(nid)
        attr_map, attrs_partial = await repo.load_node_attributes(ids_by_type)

        # ---------------- 存在性判定（404 还是"孤立账号"） ----------------
        if (etype, eid) not in attr_map:
            if attrs_partial:
                # 属性批量补齐失败时**不能**据此判"不存在"（见模块 docstring）：
                # 回落一次单点查询，宁可多一次往返也不能把故障说成"查无此人"
                exists = await self._exists(etype, eid)
            else:
                exists = False
            if not exists:
                raise grp_error(
                    "GRP-4004", "未找到该用户/实体",
                    {"entity_type": etype, "entity_id": eid},
                )

        # ---------------- 裁剪（BR-09-17：按 weight 降序保留 + 如实报总数） ----------------
        total_edges = len(all_edges)
        total_nodes = len(node_keys)
        fetch_capped = len(hop1_raw) > max_edges or len(hop2_raw) > max_edges
        lower_bound = False
        if fetch_capped:
            # 疑似截断：补一次聚合把**真实**总数算准（边数 + 去重端点数）
            flt = self._totals_filter(
                repo, etype, eid, hop1_nodes, max_hop, risk_only,
            )
            exact_edges, exact_endpoints = await repo.count_and_distinct_nodes(flt)
            total_edges = max(total_edges, exact_edges)
            # 中心节点若没有任何边（孤立账号），它不在去重端点集合里，要补 1
            total_nodes = max(total_nodes, exact_endpoints + (0 if hop1_raw else 1))
            if len(hop1_raw) > max_edges:
                # 连**一跳**都没取全：2 跳的展开基于被裁剪过的一跳集合，
                # 此时 `total_*` 是可达子图的下界（**如实标注**，不假装精确）
                lower_bound = True
                log.warning("[GRP-5002] 一跳边数超过上限，total_* 为可达子图下界 center=%s/%s",
                            etype, eid)

        kept_edges, kept_node_keys, truncated = _prune(
            all_edges, node_keys, (etype, eid), max_nodes, max_edges,
        )
        truncated = truncated or fetch_capped or timed_out

        # ---------------- 组装（节点属性已批量在手，不再逐节点查询） ----------------
        nodes = [
            _node_block(key, node_hop.get(key, 2), attr_map.get(key),
                        is_center=(key == (etype, eid)))
            for key in kept_node_keys
        ]
        edges = [_edge_block(edge) for edge in kept_edges]
        data: dict[str, Any] = {
            "center": {
                "type": etype,
                "id": eid,
                "label": _label_of(etype, eid, attr_map.get((etype, eid))),
            },
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "total_nodes": total_nodes,
            "total_edges": total_edges,
        }
        if timed_out:
            # §5 的 `GRP-5002`：超时不报错，返回已取到的部分 + 明确告知不完整
            data["timeout"] = True
            data["notice"] = "关系过于复杂，已返回部分结果"
        if attrs_partial:
            # §5 的 `GRP-5004`：属性缺失显示 —，图结构照常返回
            data["attributes_partial"] = True
        if lower_bound:
            data["total_is_lower_bound"] = True
        return data

    async def _exists(self, entity_type: str, entity_id: str) -> bool:
        """单点存在性查询（只在属性批量补齐失败时使用）。"""
        repo = self._p()
        getter = {
            "user": repo.get_user, "device": repo.get_device,
            "ip": repo.get_ip, "address": repo.get_address,
        }.get(entity_type)
        if getter is None:
            return False
        return await getter(str(entity_id)) is not None

    @staticmethod
    def _totals_filter(repo: GraphRepo, etype: str, eid: str,
                       hop1_nodes: Iterable[tuple[str, str]], max_hop: int,
                       risk_only: bool) -> dict:
        """给"真实总数"聚合用的过滤条件（与取边时**完全同口径**）。

        口径必须一致：拿一个更宽的过滤条件去数总数，会得出一个比实际展示的图
        大得多的数字，前端文案就会说"共 5000 个"而实际连 500 个都不到。
        `E1 ⊆ E2`（一跳边一定连着某个一跳节点），因此 2 跳时用 E2 的条件即可
        覆盖两批边的并集。
        """
        pairs = list(hop1_nodes)
        if max_hop >= 2 and pairs:
            types = sorted({t for t, _ in pairs})
            ids = sorted({i for _, i in pairs})
            flt: dict[str, Any] = {"$or": [
                {"from_type": {"$in": types}, "from_id": {"$in": ids}},
                {"to_type": {"$in": types}, "to_id": {"$in": ids}},
            ]}
        else:
            flt = GraphRepo._touching_filter(etype, eid)
        if risk_only:
            flt["risk_flag"] = True
        return flt


def _prune(
    all_edges: list[dict], node_keys: list[tuple[str, str]],
    center: tuple[str, str], max_nodes: int, max_edges: int,
) -> tuple[list[dict], list[tuple[str, str]], bool]:
    """按 `weight` 降序裁剪节点与边，返回 `(保留的边, 保留的节点, 是否截断)`。

    三步，且**顺序不能换**：

    1. **先裁节点**：每个节点的"强度"取它所有关联边的最大 `weight`
       （§2.2：超出时取关联强度最高的前若干条）。中心节点永远保留——它是这张图
       之所以被打开的原因，把它裁掉等于返回一张没有主角的图。
    2. **再按保留的节点裁边**：两端都还在的边才留下。**不允许悬空边**：
       `{from: U1, to: D9}` 里的 `D9` 已经不在节点表里，前端渲染时要么报错、
       要么画出一个没有任何数据支撑的孤立点（力导向图会把它丢到角落，
       看起来像一个"未知同伙"）。
    3. **最后按边裁节点**：边数超限后，某些节点可能一条边都不剩（除中心外），
       把它们一并去掉。不这样做会出现"节点 200 个、边 500 条但有 30 个节点
       没有任何连线"——人工研判会去猜这些点是什么，而它们其实什么都没连。

    截断的判据是"**确实丢了东西**"（保留数 < 总数），而不是"参数曾触顶"：
    参数触顶但节点本来就没那么多时，`truncated=true` 会让前端显示一条
    "关系过多"的假警告（狼来了），而真正的截断就不再引起注意。
    """
    strength: dict[tuple[str, str], int] = {}
    for edge in all_edges:
        weight = int(edge.get("weight") or 0)
        for endpoint in edge_endpoints(edge):
            strength[endpoint] = max(strength.get(endpoint, 0), weight)

    ranked_nodes = sorted(
        node_keys,
        key=lambda key: (
            0 if key == center else 1,          # 中心节点永远排最前
            -strength.get(key, 0),              # 强度降序
            key[1],                             # 稳定次序（便于断言与复现）
        ),
    )
    kept_nodes = set(ranked_nodes[:max_nodes])

    edges_kept_by_node = [
        edge for edge in all_edges
        if edge_endpoints(edge)[0] in kept_nodes and edge_endpoints(edge)[1] in kept_nodes
    ]
    ranked_edges = sorted(
        edges_kept_by_node,
        key=lambda edge: (-int(edge.get("weight") or 0), str(edge.get("_id"))),
    )
    kept_edges = ranked_edges[:max_edges]

    referenced: set[tuple[str, str]] = {center}
    for edge in kept_edges:
        referenced.update(edge_endpoints(edge))
    final_nodes = [key for key in ranked_nodes if key in referenced]

    truncated = len(kept_edges) < len(all_edges) or len(final_nodes) < len(node_keys)
    return kept_edges, final_nodes, truncated


def _label_of(entity_type: str, entity_id: str, doc: Optional[Mapping[str, Any]]) -> str:
    """节点展示名。

    用户/设备/IP 的展示名就是它的编号（系统里没有"昵称"这一概念，编一个出来
    会让 hover 提示与真实数据对不上）。地址优先用**掩码后的地理前缀**——
    它比 `ADDR-7712` 这种内部编号更能让研判者一眼认出"同一个收货点"。
    """
    if entity_type == "address":
        detail = masked_detail_of(doc)
        if detail:
            return detail
    return str(entity_id)


def _node_block(key: tuple[str, str], hop: int, doc: Optional[Mapping[str, Any]],
                *, is_center: bool) -> dict:
    """组装一个节点（§3.2 的 `nodes[]`）。

    属性缺失时给 `null`/`[]` 而不是编造：
    - `linked_user_cnt`：E10 `users` **没有**这个字段（它只在 E11/E12/E13 上），
      因此用户节点如实返回 `null`（前端显示「—」）。用"该用户的关联实体数"
      冒充"关联账号数"是两个不同的量，混用会让团伙规模看起来对不上。
    - `risk_level`：用户取最近一次决策的等级（§2.1 的口径），设备取 E11 的字段；
      IP/地址没有这个字段 → `null`。
    - `risk_tags`：只有 `users` 有（E10）。
    """
    doc = doc or {}
    entity_type, entity_id = key
    linked = doc.get("linked_user_cnt")
    linked = int(linked) if isinstance(linked, int) and not isinstance(linked, bool) else None
    if entity_type == "user":
        latest = doc.get("latest_decision")
        latest = latest if isinstance(latest, Mapping) else {}
        risk_level = latest.get("risk_level") or doc.get("risk_level")
        tags = doc.get("risk_tags")
    else:
        risk_level = doc.get("risk_level")
        tags = None
    return {
        "id": entity_id,
        "type": entity_type,
        "label": _label_of(entity_type, entity_id, doc),
        "hop": hop,
        "risk_level": str(risk_level) if risk_level else None,
        "risk_tags": [str(t) for t in tags] if isinstance(tags, (list, tuple)) else [],
        "linked_user_cnt": linked,
        "is_center": is_center,
    }


def _edge_block(edge: Mapping[str, Any]) -> dict:
    """组装一条边（§3.2 的 `edges[]`）。"""
    return {
        "from": str(edge.get("from_id")),
        "to": str(edge.get("to_id")),
        "relation": str(edge.get("relation")),
        "weight": int(edge.get("weight") or 0),
        "risk_flag": bool(edge.get("risk_flag")),
        "last_seen_at": edge.get("last_seen_at"),
    }


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_SERVICE = GraphService()


def get_graph_service() -> GraphService:
    return _SERVICE


async def query(entity_type: str, entity_id: str, **kwargs: Any) -> dict:
    """模块级便捷入口：`query(entity_type, entity_id, max_hop=2, ...)`。"""
    return await _SERVICE.query(entity_type, entity_id, **kwargs)


__all__ = [
    "ALLOWED_ENTITY_TYPES",
    "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_HOP",
    "DEFAULT_MAX_NODES",
    "GraphService",
    "MAX_EDGES_CAP",
    "MAX_HOP",
    "MAX_NODES_CAP",
    "QUERY_TIMEOUT_SEC",
    "get_graph_service",
    "query",
]
