# -*- coding: utf-8 -*-
"""统一路由注册器（模块 00 §6）。

**约定**：各模块的 router **只声明相对 `API_PREFIX` 的路径**（如 `/lists`），
前缀由本注册器统一加。这样"改前缀"是改一处，而不是在 14 个模块里搜
`/api/v1`。

**为什么要注册器而不是在每个模块里 `app.include_router`**：`app/main.py`
一旦被各模块 import 来注册路由，就会形成 `main -> 模块 -> main` 的循环导入，
而 `main.py` 需要在 lifespan 里引用这些模块。注册器把"装配"从"定义"里拆开，
循环即被切断。
"""
from __future__ import annotations

import importlib

from fastapi import APIRouter

from app import config

# 各模块的 api 模块清单（导入即完成路由登记）。
# 为什么用「函数级 + importlib」而不是模块级 `from app.api import list_api  # noqa`：
# 那种写法会被静态检查（包括本项目 scripts/self_audit.py）判为"未使用的 import"，
# 而一旦有人按提示删掉它，整组路由会**静默消失**（接口全部 404，导入却不报错）。
# 改成被真正调用的代码后，它既不会被误判，也不会被误删。
#
# **顺序不可随意调整**：`list_api`（06-A 名单）与 `rule_api`（06-B 规则）共用模块
# 编号 `06`——后者以 `merge=True` 并入前者登记的同一个 router，因此 `list_api`
# 必须先被导入。顺序错了会**大声报错**（`merge` 找不到目标模块即抛），不会静默丢路由。
_API_MODULES: tuple[str, ...] = (
    "common_api", "auth_api", "list_api", "rule_api", "audit_api", "metric_api",
    "event_api", "feature_api", "profile_api", "graph_api", "case_api", "engine_api",
    "sim_api",
    # 模块 13（系统设置与运行参数）：`/system/config`、`/system/stats`、
    # `/system/users*`、`/system/engine-config`。排在这里即可——它不 merge 到
    # 任何已登记的模块，也不被别的 api 模块依赖。
    "system_api",
    # ⚠️ 模块 07 的读侧（`case_query_api`）**不在这个元组里**：它的 router 定义
    # 依赖本文件的 `register_router`，import 顺序上必须排在循环**之后**，
    # 由 `_register_cross_module_routers()` 显式登记。详见该函数。
)


def _register_cross_module_routers() -> None:
    """登记**定义依赖本文件**的 api 模块（目前只有模块 07 的读侧）。

    ## 为什么不能把 `case_query_api` 放进 `_API_MODULES`

    模块 07 的读侧端点（`GET /cases`、`GET /cases/{case_no}`）按任务书裁定
    （D67）归 07，而 `case_api.py` 已经用 `register_router("08", ...)` 登记过
    ——"模块编号只能登记一次"是硬约束，07 只能**另起一个 router**登记。
    它不能写进 `case_api.py` 自登记：那个文件是 08 交付的处置侧，往里加一次
    07 的登记会让"08 的 router 端点集合"变得不确定。

    于是 07 的 router 定义在 `app/api/case_query_api.py`，而该文件要
    `from app.api import register_router`——若它出现在 `_API_MODULES` 循环里，
    就是"本模块**执行到一半**时被 import"，`register_router` 在那种时序下
    虽已定义（Python 的函数定义先于模块末尾），但可读性与可维护性都很差
    （"为什么这个模块必须排在最后"要靠数 import 顺序才能回答）。改成循环后
    的一次**显式函数调用**，顺序就是代码里写出来的顺序。
    """
    # 函数内 import：本模块的 router 依赖本文件，模块级 import 会形成环
    if "07" not in MODULE_ROUTERS:
        importlib.import_module("app.api.case_query_api")


# 模块编号 -> router。键用模块号便于启动日志按模块顺序打印，排查"哪个模块没挂上"
MODULE_ROUTERS: dict[str, APIRouter] = {}
# 根级 router：不带 `/api/v1` 前缀。目前只有 `/health`——探针与监控系统约定俗成
# 打根路径，把它挪到 `/api/v1/health` 会让部署方按惯例配置的健康检查直接 404。
ROOT_ROUTERS: dict[str, APIRouter] = {}


def _load_api_modules() -> None:
    """导入各模块的 api 模块（导入即登记路由）。幂等（`import` 缓存 + `07` 判存）。"""
    for name in _API_MODULES:
        importlib.import_module(f"app.api.{name}")
    _register_cross_module_routers()


def register_router(
    module_id: str, router: APIRouter, *, root: bool = False, merge: bool = False
) -> None:
    """登记一个模块的路由。

    重复登记直接报错而不是覆盖：两个模块用了同一个编号，说明编号约定被破坏，
    静默覆盖会让后登记者的接口凭空消失。

    `merge=True` 用于**同一模块的第二个 api 文件**（模块 06 分 06-A 名单 /
    06-B 规则两片交付）：把路由并入该模块已登记的 router，而不是占用第二个编号。
    为什么不让它独立登记一个新编号（如 "06B"）：编号是「一个模块一个前缀」的
    载体，凭空多一个编号会让 `/health` 的装配清单、前缀映射与文档三处同时失真；
    为什么不让 06-B 自己去 include 06-A 的 router：那会把"谁先谁后"变成模块间的
    隐式依赖，而这里由注册器统一裁定（且顺序错会立刻报错，不会静默丢路由）。
    """
    if merge:
        if root or module_id not in MODULE_ROUTERS:
            raise ValueError(
                f"模块 {module_id} 尚无已登记的路由，无法合并——"
                f"请确认它在 _API_MODULES 中排在本文件之前"
            )
        MODULE_ROUTERS[module_id].include_router(router)
        return
    target = ROOT_ROUTERS if root else MODULE_ROUTERS
    if module_id in target:
        kind = "根级" if root else "带前缀"
        raise ValueError(f"模块 {module_id} 的{kind}路由已登记，编号重复")
    target[module_id] = router


def api_router() -> APIRouter:
    """汇总全部模块路由，统一加上 `/api/v1` 前缀。"""
    _load_api_modules()
    root = APIRouter(prefix=config.API_PREFIX)
    for module_id in sorted(MODULE_ROUTERS):
        root.include_router(MODULE_ROUTERS[module_id])
    return root


def root_router() -> APIRouter:
    """汇总根级路由（无前缀）。"""
    _load_api_modules()
    root = APIRouter()
    for module_id in sorted(ROOT_ROUTERS):
        root.include_router(ROOT_ROUTERS[module_id])
    return root


def registered_modules() -> list[str]:
    """已登记路由的模块编号（供 `/health` 与启动日志展示装配完整性）。"""
    _load_api_modules()
    return sorted(set(MODULE_ROUTERS) | set(ROOT_ROUTERS))
