# -*- coding: utf-8 -*-
"""审计查询参数模型（模块 12 §3.1）。

**只做"格式"层面的约束，语义边界留给服务层**：例如"时间跨度不超过 30 天"
（`AUD-4003`）必须在服务层判，因为那是业务规则；若用 `Query(le=...)` 在模型层拦，
返回的会是通用 `COM-4001`，丢掉契约要求的错误码。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# 动作类型（§2.2 的下拉选项）：取自 E16 + 各模块已登记的动作
ACTION_OPTIONS: tuple[str, ...] = (
    "rule.create", "rule.update", "rule.toggle", "rule.delete",
    "list.add", "list.remove", "list.import",
    "case.claim", "case.dispose",
    "config.update", "user.create", "user.disable", "user.role_change",
    "event.receive", "sim.run",
    "auth.login", "auth.logout", "auth.denied",
    "audit.export", "audit.tamper_detected",
    # 模块 13（系统设置）实际写入的动作。**追加而不是替换**上面的 `user.*`：
    # 那几个是本表早期登记的写法，可能已被历史数据使用（筛选器里必须有对应项，
    # 否则"有记录却筛不出来"）；而 13 按任务书 §4 的例子用 `account.*` 前缀
    # （账号管理）+ `engine.config.update`（决策引擎配置），因此两种前缀都保留。
    "account.create", "account.update", "account.role_change", "account.disable",
    "account.enable", "account.reset_password", "account.delete",
    "engine.config.update",
)

# 目标类型（§2.2 的下拉选项）
# `list_entry` 是模块 06 实际写入的类型（比 `list` 更精确：名单是"条目"级资源）。
# §2.2 的下拉只笼统列了 `list`，这里按**实际写入的取值**补齐——筛选器里有选项却
# 查不到任何记录，比没有这个选项更让人困惑。
TARGET_TYPES: tuple[str, ...] = (
    "rule", "list", "list_entry", "case", "user", "config", "permission", "audit",
)


class AuditQuery(BaseModel):
    """`GET /audit/logs` 的查询参数。"""

    actor: Optional[str] = Field(default=None, max_length=64)
    action: Optional[str] = Field(default=None, max_length=64, description="支持前缀匹配，如 rule.")
    target_type: Optional[str] = Field(default=None, max_length=32)
    target_id: Optional[str] = Field(default=None, max_length=128)
    from_ms: Optional[int] = Field(default=None, alias="from", description="毫秒时间戳")
    to_ms: Optional[int] = Field(default=None, alias="to")
    page: int = 1
    page_size: int = Field(default=20, alias="page_size")

    model_config = {"populate_by_name": True}


__all__ = ["ACTION_OPTIONS", "TARGET_TYPES", "AuditQuery"]
