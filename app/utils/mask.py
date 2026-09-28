# -*- coding: utf-8 -*-
"""脱敏工具（模块 00 统一提供）。

**为什么必须由模块 00 提供**：BR-06-21 要求名单写入侧与决策侧的过滤使用
**完全一致**的脱敏实现；BR-00-10 / V-00-14 要求日志中不得出现手机号明文与
完整地址。若各处自行实现，必然出现"某条链路漏脱敏"的审计缺口，因此这里
是全项目唯一的脱敏真源。

**边界**：本模块只做**展示/日志层面的掩码**，不承担数据加密与访问控制。
"""
from __future__ import annotations

MASK = "****"

# 地址保留的前缀长度：省(2)+市(2)+区(2) 大致覆盖到区县一级，
# 门牌、楼栋、单元、房号一律掩掉——这些字段组合起来才具备定位到人的能力。
_ADDRESS_KEEP = 6


def mask_phone(value: str) -> str:
    """中国大陆手机号脱敏：保留前 3 位与后 4 位，中间固定 4 个星号。

    13900000001 -> 139****0001

    非 11 位数字的输入不猜测语义，按"保留首尾各一段"的通用规则处理：
    - 长度 <= 4：全部打码（无法安全保留任何位）
    - 长度 5~10：保留前 2 位与后 2 位
    - 长度 >= 11 且为纯数字：保留前 3 位与后 4 位（手机号场景）
    """
    s = (value or "").strip()
    if not s:
        return s
    if len(s) <= 4:
        return MASK
    if len(s) >= 11 and s.isdigit():
        return f"{s[:3]}{MASK}{s[-4:]}"
    return f"{s[:2]}{MASK}{s[-2:]}"


def mask_address(value: str) -> str:
    """地址脱敏：仅保留前 6 个字符（约到区县），其余全部掩掉。

    浙江省杭州市西湖区文一西路 969 号 -> 浙江省杭州市****
    """
    s = (value or "").strip()
    if not s:
        return s
    if len(s) <= _ADDRESS_KEEP:
        return MASK
    return f"{s[:_ADDRESS_KEEP]}{MASK}"


def mask_secret(value: str) -> str:
    """密钥类脱敏：保留前 4 位 + `****`（对齐 BR-00-01）。

    长度 <= 4 时**整体掩掉**：再保留任何一位都等于泄露了大部分信息。
    空值返回空串，便于启动日志表达"该项未配置"。
    """
    s = value or ""
    if not s:
        return s
    if len(s) <= 4:
        return MASK
    return f"{s[:4]}{MASK}"


def mask_mongo_url(url: str) -> str:
    """Mongo 连接串脱敏：保留协议与主机端口，掩掉账号密码。

    直接对整串用 `mask_secret` 会把主机名一起掩掉，导致"连不上库时无法从
    启动日志判断到底连的哪台机器"。因此这里只针对凭据段处理：

        mongodb://user:pass@192.168.6.170:27017 -> mongodb://user:****@192.168.6.170:27017
        mongodb://192.168.6.170:27017           -> 原样返回（本机 .env 即此形态）

    **兜底原则**：只要出现无法解析的 `@` 结构，就整体掩掉——宁可少一条诊断
    信息，也不能把密码打进日志。
    """
    s = url or ""
    if "@" not in s:
        return s
    try:
        scheme, rest = s.split("://", 1)
        cred, host = rest.rsplit("@", 1)
        if ":" not in cred:
            return f"{scheme}://{MASK}@{host}"
        user, _password = cred.split(":", 1)
        return f"{scheme}://{user}:{MASK}@{host}"
    except ValueError:
        return mask_secret(s)


def mongo_target(url: str) -> str:
    """从连接串提取「主机:端口/库?参数」，**凭据一律丢弃**。

    与 `mask_mongo_url` 的区别：那个函数保留用户名（`user:****`），用于需要
    看清"用哪个账号连的"的场景；本函数进一步**把用户名也丢掉**，只留地址。

    为什么启动日志要用这个：V-00-06 的验收方式是"grep .env 里的真实值，必须
    0 命中"。当连接串不含凭据时，`mask_mongo_url` 会原样返回整串，于是
    `MONGO_URL` 的值就出现在了日志里，严格来说不过关。只打主机既保住了
    "连不上时能看出连的是哪台机器"这一诊断价值，又让原始值永不落日志。
    """
    s = (url or "").strip()
    if not s:
        return "(未配置)"
    rest = s.split("://", 1)[1] if "://" in s else s
    return rest.rsplit("@", 1)[1] if "@" in rest else rest


def mask_id(value: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    """通用标识脱敏（如 user_id / device_id）：保留首尾各一段。"""
    s = (value or "").strip()
    if not s:
        return s
    if len(s) <= keep_head + keep_tail:
        return MASK
    return f"{s[:keep_head]}{MASK}{s[-keep_tail:]}"
