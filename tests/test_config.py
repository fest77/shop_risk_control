# -*- coding: utf-8 -*-
"""配置加载校验（BR-00-01 ~ 04，对应 V-00-06 / V-00-13）。

**为什么用子进程**：配置是**模块级常量**，在 import 时就求值了。若在进程内改
环境变量再 reload，`app.db` 等模块持有的引用会指向被"半更新"的配置，污染后续
所有测试。子进程隔离是唯一干净的办法，而且它顺带验证了真实启动路径
（`进程退出 + 打印缺失键名`），而不只是函数返回了什么。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.utils.mask import mask_mongo_url, mask_secret

pytestmark = pytest.mark.anyio

PY = sys.executable
ROOT = str(config.ROOT)


def _run(code: str, **env_overrides: str) -> subprocess.CompletedProcess:
    """在子进程里执行一段 Python，环境变量按需覆盖/删除（值为 None 表示删除）。"""
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 强制两端都用 UTF-8：子进程要输出中文错误信息，父进程若按 Windows 本地编码
    # (GBK) 解码会直接 UnicodeDecodeError，把"验证配置缺失"的用例变成"验证编码"。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PY, "-c", code], cwd=ROOT, env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=90,
    )


# 禁止读取 .env 的前缀代码：这样"必需项缺失"就只取决于进程环境变量
_NO_DOTENV = (
    "import dotenv;"
    "dotenv.load_dotenv = lambda *a, **k: None;"
)


async def test_missing_required_config_exits_and_prints_all_keys():
    """V-00-13：清空必需配置启动 -> 进程退出并**列出全部**缺失键名。"""
    proc = _run(
        _NO_DOTENV + "import app.config as c; c.validate(); print('SHOULD_NOT_REACH')",
        MONGO_URL=None,
        MONGO_DB_NAME=None,
    )
    assert proc.returncode != 0, f"缺少必需配置时必须退出，实际 returncode=0\n{proc.stdout}"
    output = (proc.stdout or "") + (proc.stderr or "")
    assert "SHOULD_NOT_REACH" not in output, "配置缺失却继续执行了"
    # 一次性列出全部缺失键，而不是补一个报一个
    assert "MONGO_URL" in output and "MONGO_DB_NAME" in output
    assert "COM-5002" in output


async def test_short_jwt_secret_fails_fast():
    """AUTH-5002 的另一半：密钥**存在但过短**同样不可运行。

    短密钥可被暴力穷举，一旦签名被伪造就能冒充任意角色——因此它和"缺失"一样
    属于不可运行的配置，必须启动即失败。
    """
    proc = _run("import app.config as c; c.validate()",
                JWT_SECRET="tooshort")  # noqa: secret - 故意的弱密钥，验证启动期拒绝
    assert proc.returncode != 0, "过短的 JWT 密钥不应启动成功"
    output = (proc.stdout or "") + (proc.stderr or "")
    assert "JWT_SECRET" in output and "字节" in output


async def test_env_overrides_dotenv():
    """BR-00-01：优先级 环境变量 > .env（override=False 的语义）。"""
    proc = _run(
        "import app.config as c; print(c.MONGO_DB_NAME)",
        MONGO_DB_NAME="priority_probe_db",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "priority_probe_db"


async def test_dotenv_is_loaded_when_env_is_absent():
    """BR-00-03：.env 通过 Path(__file__) 定位，不依赖当前工作目录。

    子进程的 cwd 故意设成项目根的**父目录**：若实现用相对路径读 .env，
    这里就会读不到，从而暴露缺陷。
    """
    env = dict(os.environ)
    env.pop("MONGO_DB_NAME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # cwd 在项目根之外，因此显式把项目根放进 sys.path——这一步是刻意的：
    # 它保证"能否读到 .env"只取决于 .env 的定位方式，而不是碰巧 cwd 对
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [PY, "-c", "import app.config as c; print(c.MONGO_DB_NAME)"],
        cwd=str(Path(ROOT).parent), env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "risk_control", "未能从项目根的 .env 读到库名"


async def test_dotenv_file_is_gitignored():
    """BR-00-04：.env 严禁入库，仓库只提供 .env.example。"""
    gitignore = config.ROOT / ".gitignore"
    assert gitignore.is_file(), "缺少 .gitignore"
    content = gitignore.read_text(encoding="utf-8")
    assert ".env" in content, ".gitignore 必须忽略 .env（BR-00-04）"
    example = config.ROOT / ".env.example"
    assert example.is_file(), "缺少 .env.example（BR-00-04 要求键齐全、值留空）"
    for key in config.REQUIRED_KEYS:
        assert key in example.read_text(encoding="utf-8"), f".env.example 缺少键 {key}"


# ============================================================ 脱敏（V-00-06 / V-00-14）
async def test_masked_settings_hides_real_secrets():
    """V-00-06：启动日志打印的配置里不得出现 .env 中的真实密钥值。"""
    shown = config.masked_settings()
    rendered = " ".join(str(v) for v in shown.values())
    for key in config.SECRET_KEYS:
        real = os.getenv(key)
        if real:
            assert real not in rendered, f"{key} 的真实值出现在启动日志配置里了"
    # 仍然要保留可诊断信息：库名、主机与端口必须可见
    assert shown["mongo_db"] == config.MONGO_DB_NAME
    assert shown["mongo_host"]
    assert shown["port"]


async def test_bootstrap_log_leaks_no_secrets(caplog):
    """V-00-06（日志侧）：真实跑一遍启动自检，断言日志文本里没有密钥明文。

    这条比"检查 masked_settings 的返回值"更强：它抓的是**模块自己**打出来的日志。
    原先 `db.bootstrap()` 直接打印 `config.MONGO_URL` 原文，本机 .env 不含凭据时
    看不出来，一旦换成 `mongodb://user:pass@host` 就会把口令写进日志。
    caplog 捕获的是**未经过脱敏过滤器**的原始记录，因此这里能发现"靠过滤器兜底"
    掩盖掉的问题——过滤器是最后一道防线，不该是唯一一道。
    """
    import logging as _logging

    from app import db as db_mod

    db_mod.use_database(config.TEST_MONGO_DB_NAME)
    caplog.clear()
    with caplog.at_level(_logging.INFO):
        await db_mod.bootstrap()
    text = caplog.text
    assert text, "未捕获到任何启动日志，用例本身失效了"
    for key in config.SECRET_KEYS:
        real = os.getenv(key)
        if real:
            assert real not in text, f"启动日志里出现了 {key} 的真实值"


async def test_mask_mongo_url_keeps_host_hides_password():
    """连接串脱敏要保留主机（否则连不上时无法定位），但必须掩掉口令。"""
    masked = mask_mongo_url("mongodb://root:SuperSecret123@192.168.6.170:27017/?authSource=admin")
    assert "SuperSecret123" not in masked
    assert "192.168.6.170:27017" in masked
    assert "root:" in masked
    # 无凭据的连接串原样返回（本机 .env 即此形态）
    plain = "mongodb://192.168.6.170:27017"
    assert mask_mongo_url(plain) == plain


async def test_mask_secret_handles_short_and_empty():
    assert mask_secret("") == ""
    assert mask_secret("ab") == "****"
    assert mask_secret("abcd") == "****"
    assert mask_secret("abcdefgh") == "abcd****"


async def test_validate_passes_with_current_env():
    """当前环境是可用配置，校验必须通过（否则后续所有测试无意义）。"""
    config.validate()
    assert config.MONGO_URL.startswith("mongodb://")


@pytest.mark.parametrize("key", ["MONGO_URL", "MONGO_DB_NAME"])
async def test_required_keys_are_declared(key: str):
    assert key in config.REQUIRED_KEYS
