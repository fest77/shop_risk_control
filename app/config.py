# -*- coding: utf-8 -*-
"""配置加载（BR-00-01 ~ 04）。

**优先级**：环境变量 > `.env` 文件 > 代码默认值。
`load_dotenv(..., override=False)` 正是这个语义：进程环境里已有的值不会被
`.env` 覆盖。CI/生产注入真实密钥时，`.env` 里的示例值不会把它顶掉。

**`.env` 定位方式**：用 `Path(__file__)` 反推项目根（BR-00-03），不依赖
当前工作目录——否则从 `scripts/` 或从 IDE 启动会读到不同的 `.env`，
表现为"同一份代码在两台机器上连了不同的库"。

**fail-fast**：`MONGO_URL` / `MONGO_DB_NAME` 是必需项，缺失时
`validate()` 抛出 `ConfigError` 并**一次性列出全部缺失键名**（BR-00-02 /
COM-5002）。只报第一个缺失项会导致"补一个、再启动、再报下一个"的拉锯。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from app.utils.mask import mask_secret, mongo_target
from app import constants as _constants

# 项目根 = 本文件的上上级（app/config.py -> app/ -> 项目根）
ROOT = Path(__file__).resolve().parent.parent
# override=False：环境变量优先于 .env（BR-00-01）
load_dotenv(ROOT / ".env", override=False)

APP_NAME = "shop_risk_control"
APP_VERSION = "0.1.0"          # 硬编码（模块 00 §8：不从 git 取版本，避免额外依赖）
API_PREFIX = "/api/v1"
APP_ENV = os.getenv("APP_ENV", "dev")          # dev / prod
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---- 必需配置：缺失即启动失败 ----
REQUIRED_KEYS: tuple[str, ...] = ("MONGO_URL", "MONGO_DB_NAME", "JWT_SECRET")

# ---- 数据库 ----
# 这里给空串默认值而不是"合理的默认地址"：若给默认值，配置缺失时会静默连到
# 某个地址，验收时表现为"数据莫名其妙不见了"；给空串则 validate() 必然报错。
MONGO_URL: str = os.getenv("MONGO_URL", "")
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "")
# 测试用独立库，避免污染业务库
TEST_MONGO_DB_NAME: str = os.getenv("TEST_MONGO_DB_NAME") or (MONGO_DB_NAME + "_test" if MONGO_DB_NAME else "")

# ---- 服务 ----
PORT = int(os.getenv("APP_PORT", "8101"))

# ---- 鉴权（模块 01）----
# JWT 签名密钥：从 .env 读取，**缺失或过短则启动失败**（BR-01-08 / AUTH-5002）。
# 之所以强制长度：PyJWT 对 <32 字节的 HMAC 密钥会报 InsecureKeyLengthWarning，
# 而"短密钥"意味着可被暴力穷举——签名一旦被伪造，任何角色都能被冒充。
JWT_SECRET: str = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_MIN_SECRET_BYTES = 32
# 令牌有效期默认 8 小时（BR-01-07：覆盖一个工作日，不提供刷新令牌）
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))
# 允许的角色集合（BR-01-11：仅三角色，不允许自定义角色）
VALID_ROLES: tuple[str, ...] = ("reviewer", "strategist", "admin")
# 登录失败锁定（BR-01-05，PRD 未要求；§8 已登记为可关闭项）
# 用户于 2026-09-24 确认取 10 次 / 1 分钟（见 15_决策记录 D31）：
# 原定 5 次 / 10 分钟在答辩场景下有个真实风险——模块 13 才做解锁界面，
# 老师连错 5 次就会把账号锁死 10 分钟且无人能解。
# 注意窗口与锁定时长取同一个值："连续失败"以该窗口计，
# 因此 10 次 / 60 秒的实际效果是"最多约 10 次尝试/分钟"，仍足以挡住爆破。
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "10"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "60"))

# ---- 案件处置（模块 08）----
# Spec BR-08-12：`case_claim_timeout_min` 由**模块 13 系统设置**下发，
# 取 0 表示关闭超时回收。13 尚未落地，故这里按 06/01 的既有做法给出
# "带环境变量覆盖的进程默认值"（默认 30 分钟，Spec 对悬空点 G-06 的取值）；
# 13 落地后把下发值写进同一处即可，调用方（`case_service`）不必改。
# 默认值本身仍留在 `constants.CASE_CLAIM_TIMEOUT_MIN_DEFAULT`：阈值只应有一处真源，
# 而 constants 是全项目阈值的声明处（BR-00-07 的同一条原则）。
CASE_CLAIM_TIMEOUT_MIN = int(
    os.getenv("CASE_CLAIM_TIMEOUT_MIN", str(_constants.CASE_CLAIM_TIMEOUT_MIN_DEFAULT))
)

# ---- 对象存储（模块 00 §8：当前不启用，仅保留配置位，不参与启动校验）----
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME", "")

# 密钥类配置（启动日志必须脱敏，BR-00-01）
SECRET_KEYS: tuple[str, ...] = (
    "MONGO_URL", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "JWT_SECRET",
)

# ---- 静态资源 ----
STATIC_DIR = ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
# 本地 vendor（D-01：不从 CDN 加载）。启动时校验存在性（COM-5003 / §5）
VENDOR_ECHARTS = STATIC_DIR / "vendor" / "echarts.min.js"


class ConfigError(RuntimeError):
    """启动期配置缺失（COM-5002）。

    故意不是 `AppError`：它发生在 HTTP 服务起来之前，没有请求、没有
    trace_id，也不可能返回响应体——进程直接退出才是正确行为。
    """

    code = "COM-5002"

    def __init__(self, missing: list[str]):
        self.missing = missing
        detail = "、".join(missing)
        super().__init__(
            f"[{self.code}] 缺少必需配置：{detail}。"
            f"请在 {ROOT / '.env'} 或进程环境变量中补齐（参考 .env.example）"
        )


def missing_keys() -> list[str]:
    """返回当前缺失的必需配置键名（已去空白，全角空格也算空）。"""
    result: list[str] = []
    for key in REQUIRED_KEYS:
        value = os.getenv(key, "")
        if not value or not value.strip():
            result.append(key)
    return result


def validate() -> None:
    """启动期校验：缺失必需配置则抛 `ConfigError`（BR-00-02）。

    除"存在性"外还校验 JWT 密钥长度：短密钥可被暴力穷举，一旦签名被伪造
    就能冒充任意角色，因此它和"缺失"一样属于**不可运行的配置**。
    长度问题不混进 `missing_keys()`（用于打印"缺失键名"），而是单独给提示，
    否则用户会看到"缺少 JWT_SECRET"却明明已经填了。
    """
    missing = missing_keys()
    if missing:
        raise ConfigError(missing)
    secret_bytes = len(JWT_SECRET.encode("utf-8"))
    if secret_bytes < JWT_MIN_SECRET_BYTES:
        raise ConfigError([
            f"JWT_SECRET（当前仅 {secret_bytes} 字节，"
            f"至少需 {JWT_MIN_SECRET_BYTES} 字节）"
        ])


def masked_settings() -> dict[str, str]:
    """启动日志要打印的生效配置（密钥已脱敏，BR-00-01 / V-00-06）。

    **只打印本项目自己的键**：`.env` 是与其它项目共用的文件，里面还有
    LLM / Milvus / MinerU 等与本项目无关的密钥，把整个 env dump 出来
    既无用又扩大泄露面。
    """
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "env": APP_ENV,
        "port": str(PORT),
        # 只打主机：原始连接串（含凭据）绝不进日志（V-00-06 的 0 命中要求）
        "mongo_host": mongo_target(MONGO_URL),
        "mongo_db": MONGO_DB_NAME,
        "minio_endpoint": MINIO_ENDPOINT or "(未配置)",
        "minio_access_key": mask_secret(MINIO_ACCESS_KEY) if MINIO_ACCESS_KEY else "(未配置)",
        "minio_secret_key": mask_secret(MINIO_SECRET_KEY) if MINIO_SECRET_KEY else "(未配置)",
        "jwt_secret": mask_secret(JWT_SECRET) if JWT_SECRET else "(未配置)",
        "jwt_expire_minutes": str(JWT_EXPIRE_MINUTES),
        "log_level": LOG_LEVEL,
    }
