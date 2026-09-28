# -*- coding: utf-8 -*-
"""E19 `sys_users` 仓储：只负责与 MongoDB 交互，不含业务判断。

**为什么在仓储层就把 PyMongoError 转成 AUTH-5001**：鉴权是 fail-closed 的——
"查不到用户"必须变成"拒绝"，而不是让驱动异常穿透成 500。转换放在离数据库
最近的一层，可以保证**没有任何代码路径**能绕过这个约定（若放在服务层，
将来新增一个调用点忘了 try 就会漏）。

## 模块 13 为什么在这里加方法（而不是另起一个 repo）

BR-13-10 规定"本模块是 `sys_users` 的**唯一写入方**；01 只读"，两者共用同一张
表。共用**同一个仓储类**是刻意的：账号文档的形状（`_id` 即 username、
`password_hash` 的写法、`password_changed_at` 的语义）只有一处定义，
否则"13 写出来的文档 01 认不出来"这类缺陷会以"某类账号登录不了"的形式出现。

**读写两类的异常不同**（这是本文件里唯一需要留意的分界线）：
读路径（鉴权）失败 → `AUTH-5001`（fail-closed，绝不放行）；
写路径（账号管理）失败 → `SYS-5002`（账号操作失败，不留半成品）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import COLL_SYS_USERS, USER_STATUS_DELETED
from app.errors import AccountWriteFailedError, AuthDependencyError, UsernameConflictError


class UserRepo:
    def __init__(self, db: Any):
        self.col = db[COLL_SYS_USERS]

    async def find_by_username(self, username: str) -> Optional[dict]:
        """按账号读取用户（`_id` 即 username，见 E19）。

        **软删除的账号在这一层就被过滤掉**（BR-13-17 的状态是 `deleted`）：
        它必须对**鉴权链路**等同于"不存在"，否则会出现两种坏结果之一——
        登录时落到"账号已停用"（用户以为还能找管理员开通），或者令牌回源时
        把已删除的账号当成停用账号继续拒绝。过滤放在这里，login 与
        `load_user_for_token` 两条路径同时受益，不需要在 01 里加分支。
        """
        try:
            return await self.col.find_one(
                {"_id": username, "status": {"$ne": USER_STATUS_DELETED}}
            )
        except PyMongoError as e:
            raise AuthDependencyError(f"{type(e).__name__}: {e}") from e

    async def find_raw(self, username: str) -> Optional[dict]:
        """**不过滤状态**地读取账号（账号管理侧用：需要看到已停用/已删除的行）。

        与 `find_by_username` 并列而不是加一个 `include_deleted=False` 开关：
        两个调用方的语义完全不同（"能不能登录"vs"这个账号存不存在"），
        用开关会让某天有人顺手传错参数，把已删除账号放回鉴权链路。
        """
        try:
            return await self.col.find_one({"_id": username})
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"读取账号失败：{type(e).__name__}: {e}"
            ) from e

    async def query_users(
        self, flt: dict, skip: int = 0, limit: int = 20,
    ) -> list[dict]:
        """账号列表（模块 13 §3.4，分页由服务层算好）。"""
        try:
            cursor = (
                self.col.find(flt)
                .sort([("role", 1), ("_id", 1)])
                .skip(int(skip))
                .limit(int(limit))
            )
            return await cursor.to_list(length=int(limit))
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"读取账号列表失败：{type(e).__name__}: {e}"
            ) from e

    async def count_users(self, flt: dict) -> int:
        try:
            return int(await self.col.count_documents(flt))
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"统计账号数量失败：{type(e).__name__}: {e}"
            ) from e

    async def count_active_admins(self, exclude: Optional[str] = None) -> int:
        """可用管理员数量（BR-13-14 的判定依据）。

        "可用" = `status=active`；`exclude` 用来算"**去掉目标账号之后**还剩几个"，
        这正是避免"把自己锁在门外"的那个问题。
        """
        flt: dict[str, Any] = {"role": "admin", "status": "active"}
        if exclude:
            flt["_id"] = {"$ne": exclude}
        try:
            return int(await self.col.count_documents(flt))
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"统计可用管理员失败：{type(e).__name__}: {e}"
            ) from e

    async def insert_user(self, doc: dict) -> None:
        """新建账号。`_id` 唯一即 BR-13-11 的落地点（并发下"先查再插"必然漏判）。"""
        try:
            await self.col.insert_one(dict(doc))
        except DuplicateKeyError as e:
            raise UsernameConflictError(str(doc.get("_id"))) from e
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"账号写入失败：{type(e).__name__}: {e}"
            ) from e

    async def update_fields(
        self, username: str, patch: dict, *, expected: Optional[dict] = None,
    ) -> int:
        """条件更新账号字段，返回匹配条数（决策 D42 的乐观锁落点）。

        `expected` 是"我读到的那份状态"（如 `{"status": "active", "role": "admin"}`）：
        条件更新匹配 0 条即说明在我读之后有人改过（别人刚停用了他、或刚改过角色），
        由服务层如实报冲突而不是后写覆盖——两个管理员同时改同一个账号时，
        静默覆盖会让前者的改动消失且无人知晓。
        """
        cond: dict[str, Any] = {"_id": username}
        if expected:
            cond.update(dict(expected))
        try:
            result = await self.col.update_one(cond, {"$set": dict(patch)})
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"账号写入失败：{type(e).__name__}: {e}"
            ) from e
        return int(result.matched_count)

    async def restore_fields(self, username: str, before: dict) -> bool:
        """审计/后置校验失败后的补偿：把账号还原成 `before`。返回是否成功。

        回滚**不再抛替代异常**（调用方正在处理原故障），失败由调用方记
        error 日志——此时库里会留下一个"改了却无痕"的账号，属安全事件，
        必须让人能在日志里看到。
        """
        try:
            result = await self.col.update_one(
                {"_id": username},
                {"$set": {k: v for k, v in dict(before).items() if k != "_id"}},
            )
        except PyMongoError:
            return False
        return int(result.matched_count) > 0

    async def hard_delete(self, username: str) -> bool:
        """**物理**删除账号文档。返回是否删掉了 1 条。

        ⚠️ 只用于一条补偿路径：`account.create` 的审计写不进去时（BR-13-18），
        本次新增在业务上等于**从未发生**，物理删掉它才是干净的回滚——留一个
        "已创建但无痕"的账号，等于系统里多了一个没人知道的口子。

        **删除账号的业务动作是软删除**（BR-13-17：`status=deleted`，文档保留），
        走的不是这里。这个方法的名字刻意带 `hard_`，让 review 时一眼能看出
        它不是那个业务动作。
        """
        try:
            result = await self.col.delete_one({"_id": username})
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"补偿删除账号失败：{type(e).__name__}: {e}"
            ) from e
        return int(result.deleted_count) > 0

    async def touch_last_login(self, username: str, ts_ms: int) -> None:
        """BR-01-04：登录成功更新 `last_login_at`。

        此处的失败**不影响登录成功**：登录已经完成、token 已经签发，
        为了一个统计字段把用户挡在门外是得不偿失。只记日志。
        """
        try:
            await self.col.update_one({"_id": username}, {"$set": {"last_login_at": ts_ms}})
        except PyMongoError:
            raise  # 交由调用方决定是否容忍（服务层会吞掉并记日志）

    async def update_password(self, username: str, password_hash: str, changed_at_ms: int) -> None:
        """改密：同时写 `password_changed_at`，供 BR-01-10 判定旧令牌失效。

        这里**必须**成功（不像 last_login_at）：写了一半（改了哈希却没记时间）
        会导致旧令牌继续有效，属安全缺陷，因此失败直接抛 AUTH-5001。
        """
        try:
            result = await self.col.update_one(
                {"_id": username},
                {"$set": {"password_hash": password_hash, "password_changed_at": changed_at_ms}},
            )
        except PyMongoError as e:
            raise AuthDependencyError(f"{type(e).__name__}: {e}") from e
        if result.matched_count == 0:
            # 账号在改密过程中被删除：不能静默成功，否则用户以为改了密码
            raise AuthDependencyError("账号不存在，改密未生效")

    async def count(self) -> int:
        """账号总数（供系统设置与自检使用）。"""
        try:
            return await self.col.count_documents({})
        except PyMongoError as e:
            raise AuthDependencyError(f"{type(e).__name__}: {e}") from e
