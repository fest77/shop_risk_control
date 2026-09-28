# -*- coding: utf-8 -*-
"""运行参数（`system_config`）仓储：只负责与 MongoDB 交互，不含业务判断。

## 为什么是**单文档**

Spec 13 §5 要求配置保存**原子**："半个配置生效比不生效更危险——例如 TTL 改了
但超时没改，行为不可预期"。MongoDB 单机（本项目未启用副本集事务）只有
**单文档写**是原子的，因此六个参数放在同一行里，一次 `update_one` 全部生效。

## 乐观锁（决策 D42）怎么写在没有 `version` 列的集合上

E19/E20 之外的这张表没有专门的 `version` 列，本模块用 `config_version`
（每次保存 +1）兼任版本号：更新条件带上"我读到的那个 `config_version`"，
匹配 0 条即说明**在我读之后有人改过**——此时不做后写覆盖，由服务层重读重试
（`config_service` 会重试若干次，重试仍冲突则如实报 `SYS-5001`）。
这与 `case_repo` 用状态条件表达乐观锁是同一手法。

## 为什么把 PyMongoError 转成 `SYS-5001`

与 `user_repo` 把驱动异常转成 `AUTH-5001` 同一个理由：转换放在离数据库最近的
一层，保证**没有任何代码路径**能让驱动异常穿透成 500（那会丢掉"已回滚"这句
用户可见提示，而用户需要知道"到底存没存上"）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import COLL_SYSTEM_CONFIG, RUNTIME_CONFIG_ID
from app.errors import ConfigSaveFailedError


class ConfigRepo:
    def __init__(self, db: Any):
        self.col = db[COLL_SYSTEM_CONFIG]

    async def find(self) -> Optional[dict]:
        """读取运行参数单文档；从未保存过时返回 `None`（由服务层用默认值兜底）。"""
        try:
            return await self.col.find_one({"_id": RUNTIME_CONFIG_ID})
        except PyMongoError as e:
            raise ConfigSaveFailedError(f"读取运行参数失败：{type(e).__name__}: {e}") from e

    async def insert_if_absent(self, doc: dict) -> bool:
        """首次保存：插入单文档。返回是否由本次插入创建（**只**把"已存在"当 False）。

        用 `insert_one` 而不是 upsert：两个管理员同时首次保存时，后者必须撞
        `_id` 主键并退化为条件更新，而不是静默覆盖前者的写入。

        只有 `DuplicateKeyError` 才返回 `False`（那是并发，交给调用方重试）；
        其它 PyMongoError **直接抛 `SYS-5001`** —— 把它也当成"已存在"会让一次
        真实的写故障被报成"并发冲突，请重试"，用户会一直重试一个永远失败的操作。
        """
        try:
            await self.col.insert_one({**doc, "_id": RUNTIME_CONFIG_ID})
            return True
        except DuplicateKeyError:
            return False
        except PyMongoError as e:
            raise ConfigSaveFailedError(
                f"写入运行参数失败：{type(e).__name__}: {e}"
            ) from e

    async def update_with_version(self, expected_version: int, patch: dict) -> int:
        """条件更新：仅当库中 `config_version == expected_version` 时写入。

        返回匹配条数（0 = 被并发改过，由服务层决定重试还是报错）。
        """
        try:
            result = await self.col.update_one(
                {"_id": RUNTIME_CONFIG_ID, "config_version": int(expected_version)},
                {"$set": patch},
            )
        except PyMongoError as e:
            raise ConfigSaveFailedError(f"写入运行参数失败：{type(e).__name__}: {e}") from e
        return int(result.matched_count)

    async def delete_document(self) -> bool:
        """删除单文档（**只用于审计失败后的补偿**，返回是否删掉了 1 条）。

        首次保存（文档还不存在）时，回滚的语义就是"这一行不该存在"：
        删掉它，进程回到"按代码默认值运行"，与保存前逐字节一致。
        除补偿路径外**没有别的调用方**——运行参数不存在"删除"这个业务动作
        （「恢复默认」是一次把所有字段写回默认值的普通保存，BR-13-09）。
        """
        try:
            result = await self.col.delete_one({"_id": RUNTIME_CONFIG_ID})
        except PyMongoError:
            return False
        return int(result.deleted_count) > 0

    async def restore(self, version_after: int, before: dict) -> bool:
        """审计失败后的补偿：把文档按"推进后的版本号"还原成 `before`。返回是否成功。

        回滚**不再抛替代异常**（调用方已在处理审计故障，再抛一个会把真因盖掉），
        但失败必须由调用方记 error 日志：此时库里会留下一条"改了却无痕"的配置，
        需要运维据此排查（与 `rule_service._rollback_write` 同一处置）。
        """
        try:
            result = await self.col.update_one(
                {"_id": RUNTIME_CONFIG_ID, "config_version": int(version_after)},
                # `_id` 不可改（Mongo 会直接拒绝 `$set: {_id: ...}`），调用方
                # 传进来的 `before` 里即使带着它也在这一步剥掉
                {"$set": {k: v for k, v in dict(before).items() if k != "_id"}},
            )
        except PyMongoError:
            return False
        return int(result.matched_count) > 0
