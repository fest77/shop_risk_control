# -*- coding: utf-8 -*-
"""模块 13 的组件探测注册表（Spec §6 规划的 `app/tasks/health_probe.py`）。

## 为什么这里没有"每 10 秒跑一次的定时任务"

Spec §6 把这个文件放在 `app/tasks/` 下，§2.2/BR-13-26 又写了"卡片自动刷新
10 秒"。本实现把它落地为**请求内按需探测**，而不是进程内每 10s 定时刷新一份
快照，理由有三条（属于刻意的实现选择，已登记在交付报告里）：

1. **避免出现第二个"组件健康"真源**。`/health` 每次调用都**实时** ping Mongo
   （`db.ping` 的注释写得很明确："若返回启动时缓存的结论，数据库中途挂掉也
   查不出来"）。再造一份 10s 缓存，就会出现"`/health` 说 down、`/system/stats`
   说正常"的自相矛盾——运维现场最怕的正是两个界面各说各话。
2. **BR-13-28 的要求本身就落在请求路径上**："单个组件 ≤1s 超时，任一组件探测
   超时不得导致整卡加载失败"。这一条由 `asyncio.wait_for` 逐组件兜住，
   与"探测是不是定时的"无关。
3. **避免野任务**。本项目所有后台任务都由 `main.py` 的 lifespan 统一启停
   （审计消费者、rollup、名单清理、sweep、案件维护），而测试不跑 lifespan，
   多一个后台任务就多一处"跨用例残留"的风险。探测是**只读且幂等**的，
   放在请求里没有任何代价。

前端每 10 秒拉一次 `/system/stats`（BR-13-26）就等价于"每 10 秒探测一次"，
且每次拿到的一定是当下的事实。

## 探测为什么必须是"超时 + 降级"而不是"try/except 一把抓"

某个组件（例如 MinIO 或模拟器）探测挂住时，`SYS-5003` 的处置是**该组件显示
「探测超时」、其余照常**。若把四个探测串行放在一次 try 里，一个组件的
`await` 挂住就会让整卡转圈——那正是 BR-13-28 明令禁止的形态。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app import config, db
from app.constants import COLLECTION_NAMES, HEALTH_PROBE_TIMEOUT_SEC
from app.logging import get_logger

log = get_logger("shop_risk_control.system.probe")

#: 组件状态取值（机器可读）-> 中文标签（§2.2 的"状态"列）
STATUS_LABELS: dict[str, str] = {
    "ok": "正常",
    "error": "异常",
    "unused": "未使用",
    "running": "运行中",
    "stopped": "已停止",
    "timeout": "探测超时",
    "reserved": "未接入",
}

#: 组件名（与 §2.2 的组件状态表逐字一致；前端不得自己拼名字）
NAME_MONGO = "MongoDB"
NAME_MINIO = "MinIO"
NAME_SIMULATOR = "事件流模拟器"
NAME_ENGINE = "决策引擎"


def _component(name: str, status: str, detail: str = "") -> dict:
    return {
        "name": name,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "detail": detail,
    }


def _timeout_component(name: str) -> dict:
    """超时组件：状态为 `timeout`、说明为「探测超时」（SYS-5003 的落点）。"""
    return _component(name, "timeout", "探测超时")


# ============================================================
# 单个组件的探测（都只读、都不抛）
# ============================================================
async def probe_mongo() -> dict:
    """MongoDB：真实 ping + 集合数（§2.2 的说明列「库 risk_control · N 个集合」）。

    返回里多带 `latency_ms` / `collections` / `connected` 三个**结构化**字段，
    供 `health_service` 组装 `data.mongo`（§2.2 的第 4 张指标卡读的是
    `mongo.connected`）。它们在对外响应里不出现（`ComponentOut` 只有
    name/status/status_label/detail 四列），因此不改变 §2.2 的形状。
    """
    connected, latency_ms, error = await db.ping()
    detail = f"库 {config.MONGO_DB_NAME or '（未配置）'}"
    if not connected:
        item = _component(NAME_MONGO, "error", f"{detail} · {error}")
        item.update({"connected": False, "latency_ms": latency_ms,
                     "collections": None, "error": error})
        return item
    collections: Optional[int] = None
    try:
        names = await db.get_db().list_collection_names()
        collections = len(names)
    except Exception as e:  # noqa: BLE001 - 集合数只是说明列，取不到不影响连通结论
        log.warning("列举集合失败（不影响 Mongo 连通结论）：%s: %s", type(e).__name__, e)
    if collections is not None:
        # 只报"实际存在的集合数"，不把 COLLECTION_NAMES 的长度当成集合数
        # （"声明了 22 个集合"和"库里真的有 22 个集合"是两件事）
        detail = f"{detail} · {collections} 个集合"
    item = _component(NAME_MONGO, "ok", f"{detail} · {latency_ms}ms")
    item.update({"connected": True, "latency_ms": latency_ms,
                 "collections": collections, "error": None})
    return item


async def probe_minio() -> dict:
    """MinIO：**当前不启用**（Step2 §1.3）——如实显示"未使用"，不假装正常。

    `MINIO_ENDPOINT` 为空是**设计如此**（`app/config.py` 的注释：对象存储当前
    不启用，仅保留配置位）。因此这里不是"探测失败"，而是"这个组件没有参与
    运行"——两者在运维眼里的处置完全不同。
    """
    if not config.MINIO_ENDPOINT:
        return _component(NAME_MINIO, "unused", "当前不启用（Step2 §1.3：对象存储仅保留配置位）")
    return _component(NAME_MINIO, "unused", f"已配置 {config.MINIO_ENDPOINT}，但本版本未接入读写")


async def probe_simulator() -> dict:
    """事件流模拟器：运行中/已停止 + `模式=羊毛党 · 5 events/sec · seed=42`。"""
    from app.core.event_simulator import MODE_LABELS, get_simulator

    status = get_simulator().status()
    if not status.get("running"):
        emitted = int(status.get("emitted_total") or 0)
        return _component(
            NAME_SIMULATOR, "stopped",
            f"已停止（累计发出 {emitted} 条）",
        )
    mode = str(status.get("mode") or "")
    label = MODE_LABELS.get(mode, mode or "未知")
    rate = status.get("rate")
    seed = status.get("seed")
    return _component(
        NAME_SIMULATOR, "running",
        f"模式={label} · {rate} events/sec · seed={seed}",
    )


async def probe_engine() -> dict:
    """决策引擎：当前版本 + **模型引擎未启用的如实标注**（§2.2 的说明列）。

    `model_engine` 取**实际装配**的类名（D45）：装配一旦换成真实模型引擎，
    这一格会立刻变化，不需要有人记得回来改文案。
    """
    from app.engine.decision import ENGINE_VERSION
    from app.protocols import get_components

    model_engine = type(get_components().model_engine).__name__
    return _component(
        NAME_ENGINE, "ok",
        f"{ENGINE_VERSION} · 模型引擎未启用（G-01，装配={model_engine}）",
    )


#: 探测清单（顺序即页面显示顺序，与 §2.2 的表格一致）
PROBES: tuple[tuple[str, Any], ...] = (
    (NAME_MONGO, probe_mongo),
    (NAME_MINIO, probe_minio),
    (NAME_SIMULATOR, probe_simulator),
    (NAME_ENGINE, probe_engine),
)


async def _run_one(name: str, probe: Any, timeout: float) -> dict:
    """跑一个探测：**单项 ≤1s**，超时/异常都退化为该组件的状态而不是抛出。"""
    try:
        return await asyncio.wait_for(probe(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("[SYS-5003] 组件探测超时（>%.1fs）：%s", timeout, name)
        return _timeout_component(name)
    except Exception as e:  # noqa: BLE001 - 任一组件探测异常不得拖垮整卡（BR-13-28）
        log.warning("[SYS-5003] 组件探测异常：%s：%s: %s", name, type(e).__name__, e)
        return _component(name, "error", f"探测异常：{type(e).__name__}")


async def probe_components(
    *, timeout: float = HEALTH_PROBE_TIMEOUT_SEC, probes: Optional[tuple] = None
) -> tuple[list[dict], list[dict]]:
    """并发探测全部组件，返回 `(components, notices)`（**永不抛异常**）。

    并发（`asyncio.gather`）而不是串行：四个组件各自最多 1s，串行最坏 4s，
    而这张卡是每 10 秒刷新一次的运维看板；并发下最坏仍是 1s 出头。
    `probes` 可注入（单测用假探针验 BR-13-28 的超时分支）。
    """
    items = probes if probes is not None else PROBES
    results = await asyncio.gather(
        *[_run_one(name, probe, timeout) for name, probe in items]
    )
    components = list(results)
    notices: list[dict] = [
        {"code": "SYS-5003", "message": f"{c['name']} 探测超时", "component": c["name"]}
        for c in components if c.get("status") == "timeout"
    ]
    return components, notices


def probe_components_declaration() -> dict:
    """装配自检（同步、无 IO）：报告探测超时预算与**已声明的集合数**。

    `/system/stats` 把它放在 `metrics_source` 旁边，用于回答"这张卡是按什么
    预算探测的"。它不是探测本身（真正的探测是 `probe_components()`），
    因此没有网络访问、也不可能超时。
    """
    return {
        "declared_collections": len(COLLECTION_NAMES),
        "probe_timeout_sec": HEALTH_PROBE_TIMEOUT_SEC,
        "component_names": [name for name, _probe in PROBES],
    }


__all__ = [
    "NAME_ENGINE", "NAME_MINIO", "NAME_MONGO", "NAME_SIMULATOR", "PROBES",
    "STATUS_LABELS", "probe_components", "probe_components_declaration",
    "probe_engine", "probe_minio", "probe_mongo", "probe_simulator",
]
