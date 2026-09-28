# -*- coding: utf-8 -*-
"""FastAPI 应用装配：生命周期、trace_id、访问日志、限流、静态页、路由注册。

启动顺序刻意如此：
1. `setup_logging()` —— 后面每一步的日志都要带 trace_id 与脱敏能力
2. `config.validate()` —— 配置缺失必须**最先**失败（COM-5002），
   否则会先连库再报"缺配置"，误导排查方向
3. 静态资源自检 —— 缺 `index.html` / vendor 时给出明确告警（COM-5003）
4. `db.bootstrap()` —— 探测连通性并建索引；**失败不阻止启动**（见 db.bootstrap 注释）
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.api import api_router, root_router
from app.constants import SLOW_REQUEST_MS
from app.core import (
    biz_sync_retry,
    case_maintenance_task,
    feature_sweep_task,
    list_cleanup_task,
    metric_rollup,
)
from app.core.ratelimit import LIMITER
from app.deps import current_identity_key
from app.errors import (
    RateLimitedError,
    StaticAssetMissingError,
    envelope,
    register_exception_handlers,
)
from app.logging import get_logger, reset_trace_id, set_trace_id, setup_logging
from app.middleware.auth_middleware import AuthMiddleware
from app.services import audit_service
from app.services import config_service
from app.utils.ids import new_trace_id

setup_logging(config.LOG_LEVEL)
log = get_logger("shop_risk_control")

# 不需要限流与访问日志的路径：健康检查会被探针高频调用，静态资源在页面加载时
# 一次拉取多个文件——把它们计入用户配额会让正常使用轻易触发 COM-4290。
_EXEMPT_PREFIXES = ("/health", "/static/", "/favicon.ico")


def _is_exempt(path: str) -> bool:
    return path == "/" or any(path.startswith(p) for p in _EXEMPT_PREFIXES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # COM-5002：配置缺失在此抛出，进程退出并打印缺失键名（BR-00-02 / V-00-13）
    config.validate()
    log.info("生效配置：%s", json.dumps(config.masked_settings(), ensure_ascii=False))

    if not config.INDEX_HTML.is_file():
        log.error("[COM-5003] 单页壳缺失：%s —— 访问 / 将返回 500", config.INDEX_HTML)
    if not config.VENDOR_ECHARTS.is_file():
        log.warning("[COM-5003] 本地 vendor 缺失：%s —— 大盘图表将无法渲染（D-01 要求本地引用）",
                    config.VENDOR_ECHARTS)

    await db.bootstrap()
    # 模块 13：把库里的**运行参数**装载到各消费者（窗口时长/容量、名单缓存 TTL、
    # 决策超时、指标桶粒度）。放在这里是它唯一有意义的位置：
    #   · 必须在 `db.bootstrap()` 之后（要读 `system_config`）；
    #   · 必须在 `metric_rollup.start_rollup()` 之前——"指标桶粒度"是 BR-13-07
    #     明确要求重启才生效的那一项，它生效的**唯一**时机就是定时聚合任务
    #     建立之前。
    # 失败只告警不阻断启动：运行参数取不到时按代码默认值运行，而"库连不上"
    # 本身已有自己的处置（降级启动 + /health 上报 down）。
    await config_service.load_and_apply()
    # 模块 12：启动审计单消费者。哈希链的写入必须串行，因此收敛到一个协程；
    # 放在这里而不是各模块自己管，是为了保证"整个进程只有一个写者"（AD-04/G-10）
    await audit_service.start_consumer()
    # 模块 08：按**持久标记**恢复待重试的旁路副作用（BR-08-31 ②/32：
    # "重启后按标记恢复"）。放在审计消费者之后：恢复出来的审计重试项要靠它落库。
    await biz_sync_retry.recover_from_db()
    # 模块 11：启动指标桶 rollup 定时任务（每 60s 重算上一个已闭合 1h 桶；
    # 每日 00:05 Asia/Shanghai 重算前一日 1d 桶）。与审计消费者同理，
    # 定时任务必须由应用统一启停，否则测试与热重载会留下野任务
    await metric_rollup.start_rollup()
    # 模块 06-A：名单过期清理（BR-06-31，每 10 分钟把到期的 active 置为 expired）。
    # 与上面两个同理——定时任务统一由应用启停，避免测试/热重载留下野任务
    await list_cleanup_task.start_cleanup()
    # 模块 04：特征窗口的定时任务——每 60s 清理超出长窗（1440 分钟）的窗口数据并推进
    # 失败快照的重试队列；每日 03:00 Asia/Shanghai 从历史快照重算 E21 统计基线。
    # 与上面三个同理——定时任务统一由应用启停，否则测试/热重载会留下野任务
    await feature_sweep_task.start_sweep()
    # 模块 08：案件维护——每 60s 做三件事：超时回收（BR-08-10）、自动归档
    # （BR-08-38）、补建案（决策 D5 的兜底：应当建案却还没建的 review/reject 决策）
    await case_maintenance_task.start_maintenance()
    app.state.booted_at = time.time()
    yield
    # 关闭顺序与启动相反：先停定时任务，再让已入队的审计落库，最后断数据库
    await case_maintenance_task.stop_maintenance()
    await feature_sweep_task.stop_sweep()
    await list_cleanup_task.stop_cleanup()
    await metric_rollup.stop_rollup()
    await audit_service.stop_consumer()
    await db.close()
    log.info("MongoDB 连接已关闭")


app = FastAPI(
    title="电商风险控制系统",
    description="模块 00 公共基础与项目骨架 · 模块 01 登录与权限鉴权",
    version=config.APP_VERSION,
    lifespan=lifespan,
)

# 鉴权中间件**必须在这里注册**（在可观测中间件之前）：
# Starlette 中"先注册的在内层、后注册的在外层"，因此后注册的可观测中间件
# 会成为最外层——它先给请求分配 trace_id，鉴权失败时才有 trace_id 可记录与回填。
# 若顺序反了，401/403 响应会没有 X-Trace-Id，前端报错时无法与日志对上。
app.add_middleware(AuthMiddleware)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """trace_id 注入 + 限流 + 访问日志（BR-00-09 / 11）。

    合成一个中间件而不是三个：中间件的执行顺序由注册顺序的**逆序**决定，
    拆开后"日志里有没有 trace_id"取决于注册顺序，一旦有人调整就会退化。
    """
    trace_id = new_trace_id()
    request.state.trace_id = trace_id
    token = set_trace_id(trace_id)
    started = time.perf_counter()
    path = request.url.path

    try:
        if not _is_exempt(path):
            key = current_identity_key(request)
            if not LIMITER.allow(key):
                error = RateLimitedError(LIMITER.retry_after(key))
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                log.warning("触发限流 key=%s path=%s elapsed_ms=%d", key, path, elapsed_ms)
                response = JSONResponse(
                    status_code=error.http_status,
                    content=envelope(error.code, error.message, trace_id, error.data),
                )
                response.headers["Retry-After"] = str(error.data["retry_after_sec"])
                response.headers["X-Trace-Id"] = trace_id
                return response

        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Trace-Id"] = trace_id
        log.info(
            "access method=%s path=%s status=%d elapsed_ms=%d",
            request.method, path, response.status_code, elapsed_ms,
        )
        if elapsed_ms >= SLOW_REQUEST_MS:
            log.warning("慢请求 path=%s elapsed_ms=%d（阈值 %d ms）",
                        path, elapsed_ms, SLOW_REQUEST_MS)
        return response
    finally:
        reset_trace_id(token)


register_exception_handlers(app)
app.include_router(root_router())
app.include_router(api_router())

if config.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """返回单页壳。缺失时抛 COM-5003，而不是让 Starlette 报一个裸 404。"""
    if not config.INDEX_HTML.is_file():
        raise StaticAssetMissingError(str(config.INDEX_HTML))
    return FileResponse(str(config.INDEX_HTML))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """浏览器会自动请求 favicon。返回 **204 空响应**，避免控制台出现无意义的红色报错。

    注意必须用 `Response`（空体）而不是 `JSONResponse(None)`：204 语义上禁止响应体，
    而 `JSONResponse(None)` 会写出 4 字节的 `null`，与 `Content-Length: 0` 冲突，
    uvicorn 会抛 `RuntimeError: Response content longer than Content-Length`——
    表现为每次页面加载都在服务端日志里留一条堆栈。
    """
    return Response(status_code=204)
