# -*- coding: utf-8 -*-
"""错误码体系校验（BR-00-12 / 13 / 14 / 15，对应 V-00-04）。

**这份测试要抓的三类真实缺陷**：
1. 前缀拼错（`CFGG-4001`）—— 永远不会被任何模块的分类逻辑命中
2. 4xxx/5xxx 语义错位（`CFG-4009` 配 HTTP 500）—— 调用方按"4xx 可重试/5xx 要告警"
   分流时会做出错误决策
3. 同一个码在不同异常类里指向不同 HTTP 状态 —— 同码两义（BR-00-13）

第 3 点无法通过静态文本可靠判定（同一个码可能合法地被多个类复用），因此
改成**运行时枚举**：把每个 `AppError` 子类真实实例化一次，记录 `code -> http_status`
映射，出现"同码不同状态"即失败。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app import config
from app import errors as errors_mod
from app.errors import AUTH_CODES, CASE_CODES, COM_CODES, MODULE_PREFIXES

pytestmark = pytest.mark.anyio

# 形如 COM-4001 / AUTH-5002 / DASH-4001
_CODE_RE = re.compile(r'["\']([A-Z]{3,4})-(\d{4})["\']')


def _walk_error_classes() -> list[type]:
    """递归收集全部 AppError 子类。"""
    seen: set[type] = set()
    out: list[type] = []
    stack: list[type] = [errors_mod.AppError]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                out.append(sub)
                stack.append(sub)
    return out


def _instantiate(cls: type) -> Exception:
    """按签名给所有参数填占位值，从而拿到真实的 (code, http_status)。

    占位值必须**按注解**给：`LoginLockedError(remain_sec: int)` 内部会做整除，
    传字符串会让"校验错误码"的用例自己抛 TypeError，掩盖真正要验的东西。
    注意 `app/errors.py` 用了 `from __future__ import annotations`，
    因此注解在运行时是**字符串**（如 `"int"`），两种形态都要认。
    """
    kwargs = {}
    for name, param in inspect.signature(cls.__init__).parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = param.annotation
        numeric = annotation in (int, float) or (
            isinstance(annotation, str) and annotation.strip() in ("int", "float")
        )
        kwargs[name] = 60 if numeric else "x"
    return cls(**kwargs)


async def test_all_error_classes_yield_expected_semantics():
    """每个异常类的码形如 PREFIX-4xxx/5xxx，且段位与 HTTP 状态一致。"""
    known_prefixes = set(MODULE_PREFIXES.values())
    code_status: dict[str, int] = {}
    problems: list[str] = []

    classes = _walk_error_classes()
    assert classes, "未发现任何 AppError 子类，测试本身失效了"

    for cls in classes:
        exc = _instantiate(cls)
        m = re.fullmatch(r"([A-Z]{3,4})-(\d{4})", exc.code)
        if not m:
            problems.append(f"{cls.__name__}.code={exc.code!r} 不符合 PREFIX-4xxx 格式")
            continue
        prefix, digits = m.group(1), m.group(2)
        if prefix not in known_prefixes:
            problems.append(f"{cls.__name__} 使用了未登记的前缀 {prefix}")
        segment = digits[0]
        if segment == "4" and not (400 <= exc.http_status <= 499):
            problems.append(f"{exc.code} 是 4xxx 但 HTTP {exc.http_status} 不在 4xx")
        if segment == "5" and not (500 <= exc.http_status <= 599):
            problems.append(f"{exc.code} 是 5xxx 但 HTTP {exc.http_status} 不在 5xx")
        if exc.code in code_status and code_status[exc.code] != exc.http_status:
            problems.append(
                f"{exc.code} 同码两义：既有 HTTP {code_status[exc.code]} 又有 {exc.http_status}"
            )
        code_status[exc.code] = exc.http_status

    assert not problems, "错误码语义问题：\n  - " + "\n  - ".join(problems)


async def test_module_prefixes_are_one_per_module_and_unique():
    """BR-00-13：一个模块一个前缀，且前缀在全项目唯一。"""
    assert set(MODULE_PREFIXES) == {f"{i:02d}" for i in range(14)}, "模块编号应为 00~13"
    values = list(MODULE_PREFIXES.values())
    assert len(values) == len(set(values)), f"前缀重复：{values}"
    # 00_模块划分与边界 §6 的裁定：一个模块一个前缀（06→CFG、07→CASE、08→DSP、02→DASH）
    assert MODULE_PREFIXES["06"] == "CFG"
    assert MODULE_PREFIXES["07"] == "CASE"
    assert MODULE_PREFIXES["08"] == "DSP"
    assert MODULE_PREFIXES["02"] == "DASH"


async def test_code_tables_are_well_formed_and_match_declared_status():
    """**全部**模块错误码表（COM/AUTH/CASE/...）：前缀已登记、段位与声明状态一致。

    用"表驱动"而不是逐个码写断言：新模块只要往表里加码，这条用例即自动覆盖，
    不需要记得再补测试——否则新增模块的错误码会长期处于无人校验的状态。
    模块 07 落地时 `CASE_CODES` 加进本表即自动受检（此前 `CASE-*` 从未被实现过）。
    """
    tables = {"COM_CODES": COM_CODES, "AUTH_CODES": AUTH_CODES,
              "CASE_CODES": CASE_CODES}
    known_prefixes = set(MODULE_PREFIXES.values())
    for table_name, table in tables.items():
        assert table, f"{table_name} 为空表"
        for code, (status, message) in table.items():
            prefix, _, digits = code.partition("-")
            assert prefix in known_prefixes, f"{table_name}:{code} 前缀未登记"
            assert len(digits) == 4 and digits.isdigit(), f"{code} 不是 4 位数字"
            assert message and message.strip(), f"{code} 缺少面向用户的中文提示（BR-00-14）"
            if status is None:
                # 仅启动期使用（如 COM-5002 配置缺失、AUTH-5002 密钥缺失），不在 HTTP 链路上
                assert digits.startswith("5"), f"{code} 不在 HTTP 链路上，应是 5xxx"
                continue
            assert (digits[0] == "4") == (400 <= status <= 499), \
                f"{code} 段位与 HTTP {status} 不一致"
            assert (digits[0] == "5") == (500 <= status <= 599), \
                f"{code} 段位与 HTTP {status} 不一致"


async def test_source_literals_use_registered_prefixes():
    """扫描全部源码里的错误码字面量，前缀必须已登记（抓 `CFGG-4001` 这类拼写错）。"""
    known_prefixes = set(MODULE_PREFIXES.values())
    unknown: set[str] = set()
    scanned = 0
    for path in Path(config.ROOT, "app").rglob("*.py"):
        scanned += 1
        for match in _CODE_RE.finditer(path.read_text(encoding="utf-8")):
            if match.group(1) not in known_prefixes:
                unknown.add(f"{path.name}: {match.group(0)}")
    assert scanned > 0, "没有扫描到任何源文件，路径推断有误"
    assert not unknown, f"发现未登记前缀的错误码：{sorted(unknown)}"


async def test_app_error_json_never_contains_stack():
    """BR-00-14：错误响应只给用户可见文案，技术细节放 detail 与日志。"""
    exc = _instantiate(errors_mod.ListWriteFailedError)
    from app.errors import envelope

    body = envelope(exc.code, exc.message, "tr_test", exc.data)
    assert "Traceback" not in str(body)
    assert set(body) == {"ok", "code", "message", "trace_id", "data"}
    assert body["ok"] is False


async def test_envelope_ok_is_derived_from_code():
    """`ok` 必须由 `code` 派生，杜绝"两处状态打架"。"""
    from app.errors import envelope

    assert envelope("OK", "成功", "tr_1", {"a": 1})["ok"] is True
    assert envelope("COM-4001", "参数校验失败", "tr_2")["ok"] is False


@pytest.mark.parametrize("code", ["COM-5000", "COM-5001", "COM-4290"])
async def test_key_com_codes_exist(code: str):
    """兜底与依赖类错误码必须在表里定义（否则 §5 的兜底原则无从落地）。"""
    assert code in COM_CODES
