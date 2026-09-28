# -*- coding: utf-8 -*-
"""MongoDB 连接与索引初始化（BR-00-05 / 06 / 07）。

**为什么不用 motor**：pymongo 4.18 自带 `AsyncMongoClient`，motor 已进入维护
模式。少一个依赖就少一处版本冲突（要求见技术栈冻结）。

**单例为什么用全局变量而不是 `lru_cache`**：`lru_cache` 无法在测试里切换
数据库与释放连接（`use_database` / `close` 需要可变状态）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app import config
from app.constants import INDEX_SPECS
from app.utils.mask import mongo_target

log = logging.getLogger("shop_risk_control.db")

_client: Optional[AsyncMongoClient] = None
_db: Optional[AsyncDatabase] = None


def get_client() -> AsyncMongoClient:
    """进程内唯一的 Mongo 客户端（BR-00-05）。"""
    global _client
    if _client is None:
        _client = AsyncMongoClient(config.MONGO_URL, serverSelectionTimeoutMS=5000)
    return _client


def get_db() -> AsyncDatabase:
    """当前生效的数据库句柄。"""
    global _db
    if _db is None:
        _db = get_client()[config.MONGO_DB_NAME]
    return _db


def use_database(name: str) -> None:
    """切换数据库（测试与多租户隔离用）。"""
    global _db
    _db = get_client()[name]


async def ping() -> tuple[bool, int, str]:
    """探测连通性，返回 `(是否连通, 耗时毫秒, 错误信息)`。

    `/health` 每次调用都实时探测：健康检查的价值就在于"反映此刻状态"，
    若返回启动时缓存的结论，数据库中途挂掉也照样报 ok。
    """
    import time

    started = time.perf_counter()
    try:
        await get_client().admin.command("ping")
        elapsed = int((time.perf_counter() - started) * 1000)
        return True, elapsed, ""
    except Exception as e:  # noqa: BLE001 - 健康检查需要把所有失败都反映出来
        elapsed = int((time.perf_counter() - started) * 1000)
        return False, elapsed, f"{type(e).__name__}: {e}"


async def ensure_indexes() -> list[str]:
    """按 `constants.INDEX_SPECS` 建索引，返回已确认存在的索引名列表。

    索引声明集中在 `constants.py`（BR-00-07），本函数只负责执行。
    `create_index` 是幂等的：索引已存在且定义一致时不做任何改动。
    """
    created: list[str] = []
    db = get_db()
    for coll_name, specs in INDEX_SPECS.items():
        for spec in specs:
            # 只为"确实声明了"的选项传参：Mongo 会把显式的 null 当作非法值
            # （The field 'partialFilterExpression' must be an object, but got null），
            # 而 `unique=False` 虽合法却会让后续调参更易出错，故一并按需传递。
            options: dict[str, Any] = {"name": spec["name"]}
            if spec.get("unique"):
                options["unique"] = True
            if spec.get("partialFilterExpression"):
                options["partialFilterExpression"] = spec["partialFilterExpression"]
            # TTL 索引（模块 11 的指标桶保留策略）：0 表示"以文档自带的 expire_at 为准"，
            # 也是"每类粒度可不同"的唯一实现方式（集合级只有一份 TTL 配置）
            if spec.get("expireAfterSeconds") is not None:
                options["expireAfterSeconds"] = spec["expireAfterSeconds"]
            await db[coll_name].create_index(spec["keys"], **options)
            created.append(f"{coll_name}.{spec['name']}")
    return created


async def close() -> None:
    """关闭连接（应用 lifespan 退出时调用）。"""
    global _client, _db
    if _client is not None:
        await _client.close()
    _client = None
    _db = None


async def bootstrap() -> dict[str, Any]:
    """启动期数据库自检：探测连通性 + 建索引。

    **连不上不阻止启动**，只把结果记为降级。原因（对齐模块 00 §5 与 V-00-01）：
    `COM-5001` 的处置是"/health 返回 down"，即服务**要在库不可用时仍能起来**
    并如实上报；若启动即退出，健康检查本身就没了，运维无法区分"进程没起来"
    与"库挂了"。真正需要拦住的是配置缺失（COM-5002），那属于"无论如何都跑不了"。
    """
    ok, latency_ms, error = await ping()
    result: dict[str, Any] = {
        "connected": ok,
        "latency_ms": latency_ms,
        "indexes": [],
        "error": error,
    }
    if not ok:
        # 只打主机：原始连接串可能含账号口令，绝不进日志（V-00-06）
        log.error("MongoDB 不可达（%s）：%s —— 服务以降级状态启动，/health 将上报 down",
                  mongo_target(config.MONGO_URL), error)
        return result
    log.info("MongoDB 已连接：%s / 库=%s（%d ms）",
             mongo_target(config.MONGO_URL), config.MONGO_DB_NAME, latency_ms)
    try:
        result["indexes"] = await ensure_indexes()
        log.info("索引就绪：%s", "、".join(result["indexes"]) or "（无声明）")
    except Exception as e:  # noqa: BLE001 - 建索引失败不该拖垮启动
        result["error"] = f"建索引失败：{type(e).__name__}: {e}"
        log.error("建索引失败：%s", result["error"])
    return result
