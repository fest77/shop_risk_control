# -*- coding: utf-8 -*-
"""E20 `model_configs` 仓储（**架构预留**，AD-09 / 悬空点 G-01）。

## 这一层为什么现在就要存在

Spec 13 §6 把 E20 的读写列为交付物，而模块 13 是它**唯一**的读写方。
它的价值不在"存了什么"，而在把"当前用的是哪套决策引擎"这件事变成**可读的
事实**：`GET /system/engine-config` 读的就是这里，页面据此显示
「rule-engine-v1 · 模型引擎未接入（G-01）」。

## 为什么不做成"可开关但无效果"的假开关

`app/protocols.py` 的 `NullModelEngine` 恒返回 `None`（AD-09），因此
`model_score` 永远是 `null`、`final_score ≡ rule_score`。若这里允许把
`engine_type` 改成 `model` 并"保存成功"，页面会显示"模型引擎已启用"，
而决策链路里**没有任何一行代码会因此改变**——那是最坏的一类缺陷：
它让人相信一件没有发生的事（BR-13-22/23 要求明确拒绝并显著标注）。
因此：`engine_type` 只接受 `rule`，其余由服务层抛 `SYS-4003`。

## 唯一索引为什么不要

E20 的 `_id` 固定为 `DEFAULT_CONFIG_ID`，`_id` 本身即唯一键（
`constants.INDEX_SPECS[COLL_MODEL_CONFIGS]` 里已注明不另建索引）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import COLL_MODEL_CONFIGS
from app.errors import ConfigSaveFailedError

#: E20 默认记录的 `_id`（只有一行，见 BR-13-21）
DEFAULT_CONFIG_ID = "default"


class ModelConfigRepo:
    def __init__(self, db: Any):
        self.col = db[COLL_MODEL_CONFIGS]

    async def find_default(self) -> Optional[dict]:
        """读默认记录；未灌种子时返回 `None`（由服务层给代码默认值）。"""
        try:
            return await self.col.find_one({"_id": DEFAULT_CONFIG_ID})
        except PyMongoError as e:
            raise ConfigSaveFailedError(
                f"读取决策引擎配置失败：{type(e).__name__}: {e}"
            ) from e

    async def insert_if_absent(self, doc: dict) -> bool:
        """首次写入：插入默认记录。返回是否由本次插入创建。"""
        try:
            await self.col.insert_one({**doc, "_id": DEFAULT_CONFIG_ID})
            return True
        except DuplicateKeyError:
            return False
        except PyMongoError as e:
            raise ConfigSaveFailedError(
                f"写入决策引擎配置失败：{type(e).__name__}: {e}"
            ) from e

    async def update(self, patch: dict) -> int:
        """按 `_id` 更新（单行集合，无并发版本问题：冲突由服务层的读-改-写重试兜住）。"""
        try:
            result = await self.col.update_one({"_id": DEFAULT_CONFIG_ID}, {"$set": patch})
        except PyMongoError as e:
            raise ConfigSaveFailedError(
                f"写入决策引擎配置失败：{type(e).__name__}: {e}"
            ) from e
        return int(result.matched_count)

    async def restore(self, before: dict) -> bool:
        """审计失败后的补偿：还原成 `before`。返回是否成功。"""
        try:
            result = await self.col.update_one(
                {"_id": DEFAULT_CONFIG_ID},
                {"$set": {k: v for k, v in dict(before).items() if k != "_id"}},
            )
        except PyMongoError:
            return False
        return int(result.matched_count) > 0
