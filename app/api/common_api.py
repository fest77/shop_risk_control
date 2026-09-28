# -*- coding: utf-8 -*-
"""模块 00 提供的公共接口（模块 00 §3.3）。

    GET /health                  进程存活 + Mongo 连通性 + 版本 + 运行时长
    GET /api/v1/common/enums     一次性下发全部枚举（前后端共用单一来源）
    GET /api/v1/common/meta      server_time / api_version / env

**`/health` 走统一响应包**：模块 00 §3.1 声明响应契约"全项目强制"，V-00-02
也要求"随机抽接口，断言响应都含 `ok` 与 `trace_id`"。因此下表中的字段位于
`data` 内，而不是响应体顶层。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app import config, db, enums
from app.api import register_router
from app.constants import COLL_RULE_SCENES
from app.errors import MongoUnavailableError, envelope
from app.logging import get_trace_id
from app.protocols import get_components
from app.utils.timeutil import iso, now_ms

log = logging.getLogger("shop_risk_control.common")

router = APIRouter(tags=["公共基础"])
# 根级 router：`/health` 不带 `/api/v1` 前缀（模块 00 §3.3 约定）
root_router = APIRouter(tags=["公共基础"])

# 进程启动时刻：`uptime_sec` 用它计算。放在模块级而不是 lifespan 里，
# 这样即使只做 ASGI 测试（不跑 lifespan）也能拿到合理值。
_STARTED_AT_MS = now_ms()


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


@root_router.get("/health", summary="健康检查")
async def health(request: Request) -> dict:
    """返回 `ok` / `degraded` / `down`。

    探活是**实时**执行 mongo ping 的（见 `db.ping`），不是返回启动时的缓存：
    健康检查若只反映启动瞬间的状态，数据库中途挂掉就查不出来。
    """
    connected, latency_ms, error = await db.ping()
    now = now_ms()
    if connected:
        status = "ok"
    elif config.MONGO_URL:
        # 库不可达：进程活着但无法提供业务能力 -> degraded（模块 00 §5 COM-5001）
        status = "degraded"
    else:
        status = "down"
    data = {
        "status": status,
        "version": config.APP_VERSION,
        "mongo": {
            "connected": connected,
            "latency_ms": latency_ms,
            "db": config.MONGO_DB_NAME or None,
            "error": error or None,
        },
        "uptime_sec": int((now - _STARTED_AT_MS) / 1000),
        "time": iso(now),
    }
    # 附加诊断（不在 §3.3 必需字段内，但排查装配问题很有用）
    data["app"] = config.APP_NAME
    data["env"] = config.APP_ENV
    data["components"] = get_components().describe()
    data["feature_window"] = _feature_window_snapshot()
    body = envelope("OK", "ok", _trace(request), data)
    return body


def _feature_window_snapshot() -> dict:
    """04 滑动窗口的计数快照（供 `/health` 展示，也供交付验收读取）。

    ## 为什么把它放进 `/health`

    窗口是**进程内**状态（悬空点 G-02），`FeatureWindow` 的模块 docstring
    明确写了 `stats()` 的用途是"供 `/health` 与系统设置页展示"。而它同时是
    模块 10 数据隔离（BR-10-06）**唯一无法从数据库观察到**的那一项：
    E01~E04/E08/E15 都能直接查库对比，但"仿真有没有偷偷写窗口"只有进程内的
    这几行计数能回答。

    把它挂上 `/health` 之后，"跑仿真前后各读一次真实数据"这件事就能**纯靠
    HTTP** 做（两个时刻各打一次 `/health`），不必写一个"自己进程里另有一个
    空窗口"的假探针——后者会给出看起来正确、实则与真实服务无关的数字。

    只读 `stats()`，**没有任何副作用**：`/health` 因此仍然可以安全地被
    探针高频调用。
    """
    try:
        # 延迟导入：`feature_service` 会拉起 04 的整条装配（含画像仓储），
        # 而 `/health` 是**最先**要能回答的接口——它不该因为 04 装配出问题而失败
        from app.services.feature_service import get_feature_service

        service = get_feature_service()
        stats = service.window.stats()
        return {
            "total_entries": stats["total_entries"],
            "distinct_keys": stats["distinct_keys"],
            "dimensions": dict(stats["dimensions"]),
            "seen_ids": stats["seen_ids"],
            "duplicate_cnt": stats["duplicate_cnt"],
            "truncated_cnt": stats["truncated_cnt"],
            "out_of_order_cnt": stats["out_of_order_cnt"],
            "dropped_cnt": stats["dropped_cnt"],
            # 04 的记账：`read_only` 是"按只读模式算过几次"（仿真走的通道），
            # `window_affected` 是"真正写过窗口几次"（真实链路）。模块 10 的
            # 隔离验收就是"跑完 N 次仿真后 read_only 涨 N、window_affected 为 0"。
            "compute_stats": dict(service.stats),
        }
    except Exception as e:  # noqa: BLE001 - 健康检查必须永远能返回
        log.warning("/health 读取特征窗口状态失败（不影响探活结论）：%s", e)
        return {"error": f"{type(e).__name__}: {e}"}


@router.get("/common/enums", summary="全部枚举（前后端单一来源）")
async def common_enums(request: Request) -> dict:
    """下发 22 组静态枚举 + 数据驱动的 `rule_scenes`。

    **`rule_scenes` 为什么单独处理**：模块 02 要求场景下拉的选项来自
    `/common/enums` 的 `rule_scenes`，但它是 E06 的**数据行**而不是代码里的
    枚举（D10 明确规定 `common` 场景必须以数据行存在，禁止在代码里特判）。
    因此这里从 `rule_scenes` 集合读出，取值即场景码、标签即场景名。

    **库不可达 vs 集合为空**要区分对待：
    - 库不可达 → 报 `COM-5001`。这属于"依赖挂了"，静默返回空选项会让界面
      看起来只是"没有场景"，掩盖真实故障。
    - 库可达但集合为空 → 返回空数组。这属于"种子数据没灌"，是部署问题，
      错误信息由前端空态提示，不该伪装成数据库故障。
    """
    data: dict[str, list[dict[str, str]]] = enums.all_options()
    try:
        cursor = db.get_db()[COLL_RULE_SCENES].find({}, {"name": 1, "sort": 1}).sort("sort", 1)
        rows = await cursor.to_list(length=100)
    except Exception as e:  # noqa: BLE001 - 统一转成 COM-5001，不向上泄漏驱动细节
        log.error("读取规则场景失败：%s", f"{type(e).__name__}: {e}")
        raise MongoUnavailableError(f"{type(e).__name__}: {e}") from e
    if not rows:
        log.error("rule_scenes 集合为空——请先执行 scripts/seed.py 灌入场景与规则种子数据")
    data["rule_scenes"] = [
        {"value": str(r.get("_id")), "label": str(r.get("name") or r.get("_id"))}
        for r in rows
    ]
    return envelope("OK", "ok", _trace(request), data)


@router.get("/common/meta", summary="服务元信息")
async def common_meta(request: Request) -> dict:
    """给前端提供服务器时间（做相对时间展示）与运行环境标识。"""
    return envelope(
        "OK",
        "ok",
        _trace(request),
        {
            "server_time": iso(now_ms()),
            "server_ts": now_ms(),
            "api_version": config.API_PREFIX,
            "app_version": config.APP_VERSION,
            "env": config.APP_ENV,
        },
    )


register_router("00", router)
register_router("00", root_router, root=True)
