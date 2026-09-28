# -*- coding: utf-8 -*-
"""结构化日志、trace_id 全链路串联与脱敏过滤器（BR-00-09 / 10 / 11）。

**要解决的问题**：出问题时最有价值的一句话是"这次请求的日志在哪"。因此
每个请求生成一个 `trace_id` 放进 `contextvar`，日志格式化器自动把它拼进
每一行——业务代码**不需要**手动传 trace_id，也就不会漏传。

**脱敏为什么放在格式化器而不是调用处**：调用处脱敏依赖"每个开发者都记得"，
必然漏。这里在**最终输出行**上用正则兜底，无论日志怎么拼（f-string、%s、
args），出去的文本都已经过掩码，这是唯一可靠的落点。
"""
from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar, Token

from app.utils.mask import mask_address, mask_phone, mask_secret

# 每个请求一个 trace_id；未设置时用 "-" 而不是空串，便于日志里一眼看出
# "这行日志不属于任何请求"（例如启动期日志）。
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def set_trace_id(value: str) -> Token:
    """设置当前上下文的 trace_id，返回可用于复位的 token。"""
    return _trace_id.set(value)


def reset_trace_id(token: Token) -> None:
    """复位 trace_id（中间件退出时调用，避免串到下一个请求）。"""
    _trace_id.reset(token)


def get_trace_id() -> str:
    """取当前上下文的 trace_id。"""
    return _trace_id.get()


# ============================================================
# 脱敏规则（BR-00-10 / V-00-14）
# ============================================================
# 手机号：前后不能再接数字，否则会把 12 位订单号截出一段误判为手机号
_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 常见密钥前缀（sk- / ak- 等，长度 16 位以上）
_RE_SECRET = re.compile(r"\b((?:sk|ak|pk|ghp|xoxb)-[A-Za-z0-9_\-.]{16,})")
# 连接串中的密码段
_RE_URL_CRED = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)")
# 中文地址：省/市/区 之后跟门牌号（"XX省XX市XX区XX路12号"）
_RE_ADDRESS = re.compile(
    r"([\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa5]{2,10}(?:市|区|县|州))"
    r"[\u4e00-\u9fa5A-Za-z0-9\-]{3,}"
)


def scrub(text: str) -> str:
    """对任意文本做脱敏，返回可安全落日志的文本。

    顺序有讲究：先处理 URL 凭据，再处理密钥、手机号、地址。若先替换手机号，
    可能破坏 URL 结构而让凭据正则失配。
    """
    if not text:
        return text
    text = _RE_URL_CRED.sub(lambda m: f"{m.group(1)}{mask_secret(m.group(2))}{m.group(3)}", text)
    text = _RE_SECRET.sub(lambda m: mask_secret(m.group(1)), text)
    text = _RE_PHONE.sub(lambda m: mask_phone(m.group(1)), text)
    text = _RE_ADDRESS.sub(lambda m: mask_address(m.group(0)), text)
    return text


class ScrubFilter(logging.Filter):
    """兜底脱敏过滤器。

    它挂在 **handler** 上而不是 logger 上：挂在 logger 上只能覆盖本 logger
    自己产生的记录，而第三方库（uvicorn / pymongo）的记录会绕过它。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 不能改写 record.msg/args 后交给原格式化器（那样会与 %s 参数错位），
        # 因此先把消息压成字符串，再让格式化器直接使用它。
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


class TraceFormatter(logging.Formatter):
    """单行结构化日志：`时间 级别 trace_id 模块 消息`（BR-00-10）。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        return scrub(base)


_FORMAT = "%(asctime)s %(levelname)s %(trace_id)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _TraceIdInjector(logging.Filter):
    """把 contextvar 里的 trace_id 注入每一条记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = _trace_id.get()
        return True


_configured = False


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志（幂等：重复调用不会叠加 handler）。

    幂等很重要——测试里会多次导入并调用，若每次 addHandler，日志会成倍重复，
    V-00-07 的"用 trace_id 检索到完整堆栈"就会变成一堆重复行。
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TraceFormatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_TraceIdInjector())
    handler.addFilter(ScrubFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # uvicorn 自带 access log 与我们自己的访问日志重复，且格式不含 trace_id
    logging.getLogger("uvicorn.access").disabled = True
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """取模块 logger。业务代码统一用这个入口，便于将来换实现。"""
    return logging.getLogger(name)
