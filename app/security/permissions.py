# -*- coding: utf-8 -*-
"""权限矩阵：**唯一真源**（模块 01 §2.2 / BR-01-12）。

前端菜单过滤、后端 403 判定、`GET /auth/me` 下发的 `permissions` 全部读本文件，
**任何人都不得再维护第二份**——两份必然漂移，而漂移的表现就是"菜单看不见、
接口却能调通"（等于没有权限控制）。

**本文件刻意不 import FastAPI/errors/pymongo**：它是纯数据 + 纯函数，
因此既能被后端依赖注入使用，也能被测试直接断言，还能安全地整份下发给前端。

## 取值依据（G-11）

§2.2 的矩阵从 PRD 2.3.2 的三段文字描述推导而来，并与 `01_数据实体` E19 的
权限表、模块 00 §2.1 的菜单表三处一致。

**与模块 06 的 `BR-06-34` 冲突已裁定**（15_决策记录 D26）：BR-06-34 写的是
「strategist 与 admin 可写名单/规则」，而 §2.2 与 E19 均把该能力**只给
strategist**。BR-01-12 明确 §2.2 是唯一真源，且两处旁证支持它，故采用
**名单/规则写 = 仅 strategist**，BR-06-34 的正文已同步更正。
"""
from __future__ import annotations

from app.enums import Role

# ============================================================
# 权限串（细粒度、功能级；格式 `资源:动作`）
# ============================================================
P_DASHBOARD_READ = "dashboard:read"    # 态势大盘
P_CASE_READ = "case:read"              # 审核工作台：案件列表/画像/证据链
P_CASE_DISPOSE = "case:dispose"        # 案件认领与处置提交
P_RULE_WRITE = "rule:write"            # 策略与规则配置（规则 CRUD / 启停用）
P_LIST_READ = "list:read"              # 名单只读查看
P_LIST_WRITE = "list:write"            # 多维名单库维护 / 批量导入
P_SIM_RUN = "sim:run"                  # 事件仿真测试 / 批量回放
P_AUDIT_READ = "audit:read"            # 审计日志页 + 哈希链完整性校验
P_SYS_CONFIG = "sys:config"            # 系统设置（运行参数 / 吞吐健康度）
P_ACCOUNT_MANAGE = "account:manage"    # 账号与角色管理
P_ENGINE_CONFIG = "engine:config"      # 决策引擎（含模型引擎）配置

ALL_PERMISSIONS: tuple[str, ...] = (
    P_DASHBOARD_READ, P_CASE_READ, P_CASE_DISPOSE, P_RULE_WRITE, P_LIST_READ,
    P_LIST_WRITE, P_SIM_RUN, P_AUDIT_READ, P_SYS_CONFIG, P_ACCOUNT_MANAGE,
    P_ENGINE_CONFIG,
)

# ============================================================
# 权限 -> 角色（§2.2 矩阵的机器可读形式）
# ------------------------------------------------------------
# 每一项都是"该权限允许哪些角色"。显式列出全部角色集合便于一眼核对：
# 出现两个角色的项只应是 SHARED_PERMISSIONS（否则违反 BR-01-13 职责分离）。
# ============================================================
PERMISSION_MATRIX: dict[str, tuple[str, ...]] = {
    P_DASHBOARD_READ: (Role.REVIEWER.value, Role.STRATEGIST.value, Role.ADMIN.value),
    P_CASE_READ: (Role.REVIEWER.value,),
    P_CASE_DISPOSE: (Role.REVIEWER.value,),
    P_RULE_WRITE: (Role.STRATEGIST.value,),
    # 名单只读对全部登录角色开放（BR-06-35：审核员可只读查看名单），
    # 而"维护/批量导入"才是 strategist 专属——页面可见性与接口读权限是两个层次
    P_LIST_READ: (Role.REVIEWER.value, Role.STRATEGIST.value, Role.ADMIN.value),
    P_LIST_WRITE: (Role.STRATEGIST.value,),
    P_SIM_RUN: (Role.REVIEWER.value, Role.STRATEGIST.value),
    P_AUDIT_READ: (Role.ADMIN.value,),
    P_SYS_CONFIG: (Role.ADMIN.value,),
    P_ACCOUNT_MANAGE: (Role.ADMIN.value,),
    P_ENGINE_CONFIG: (Role.ADMIN.value,),
}

# 允许多个角色共有的权限（BR-01-13 的"仿真测试例外"，外加只读类）
# 其余权限必须**只属于一个角色**，由 test_auth.py 强制断言——这是"职责分离"
# 不被悄悄破坏的守卫：将来有人给 admin 顺手加上 rule:write，测试立刻变红。
SHARED_PERMISSIONS: frozenset[str] = frozenset({
    P_DASHBOARD_READ, P_LIST_READ, P_SIM_RUN,
})

# ============================================================
# 菜单（hash）-> 所需权限
# ------------------------------------------------------------
# 前端**不自行推导**菜单可见性，而是拿 /auth/me 下发的 permissions 与本表比对；
# 本表同时也由后端用于"无权限 hash 拦截"的一致性校验。
# ============================================================
MENU_PERMISSIONS: dict[str, str] = {
    "#/dashboard": P_DASHBOARD_READ,
    "#/cases": P_CASE_READ,
    "#/rules": P_RULE_WRITE,
    "#/sim": P_SIM_RUN,
    "#/audit": P_AUDIT_READ,
    "#/settings": P_SYS_CONFIG,
}


def roles_for(permission: str) -> tuple[str, ...]:
    """某权限允许的角色集合。未知权限返回空元组（**默认拒绝**，fail-closed）。"""
    return PERMISSION_MATRIX.get(permission, ())


def permissions_for(role: str) -> list[str]:
    """某角色的权限串列表（按 `ALL_PERMISSIONS` 顺序，便于前端稳定渲染菜单）。"""
    return [p for p in ALL_PERMISSIONS if role in roles_for(p)]


def role_has(role: str, permission: str) -> bool:
    """角色是否具备某权限。未知权限一律 False（fail-closed）。"""
    return role in roles_for(permission)


def menu_for(permissions: list[str]) -> list[str]:
    """由权限串推出可见菜单 hash 列表（顺序固定，与模块 00 §2.1 一致）。"""
    return [h for h, perm in MENU_PERMISSIONS.items() if perm in permissions]


def assert_matrix_is_sane() -> None:
    """自检矩阵内部一致性（由启动期与测试调用）。

    校验三件事：
    1. 矩阵覆盖了 `ALL_PERMISSIONS` 的每一项，且没有多余项
    2. 所有角色取值合法
    3. 除 `SHARED_PERMISSIONS` 外，每项权限只属于一个角色（BR-01-13）
    """
    if set(PERMISSION_MATRIX) != set(ALL_PERMISSIONS):
        missing = set(ALL_PERMISSIONS) - set(PERMISSION_MATRIX)
        extra = set(PERMISSION_MATRIX) - set(ALL_PERMISSIONS)
        raise AssertionError(f"权限矩阵与 ALL_PERMISSIONS 不一致：缺 {missing}，多 {extra}")

    valid_roles = {r.value for r in Role}
    for perm, roles in PERMISSION_MATRIX.items():
        unknown = set(roles) - valid_roles
        if unknown:
            raise AssertionError(f"权限 {perm} 含未知角色：{unknown}")
        if not roles:
            raise AssertionError(f"权限 {perm} 未授予任何角色，属死权限")
        if len(roles) > 1 and perm not in SHARED_PERMISSIONS:
            raise AssertionError(
                f"权限 {perm} 被授予多个角色 {roles}，违反 BR-01-13 职责分离；"
                f"若确为共享权限，请显式加入 SHARED_PERMISSIONS"
            )

    unknown_menu = set(MENU_PERMISSIONS.values()) - set(ALL_PERMISSIONS)
    if unknown_menu:
        raise AssertionError(f"菜单引用了未定义的权限：{unknown_menu}")
