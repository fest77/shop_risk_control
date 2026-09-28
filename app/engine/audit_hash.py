# -*- coding: utf-8 -*-
"""审计哈希的计算内核（BR-12-04：**全项目唯一实现**）。

写入与校验**必须**调用本文件的同一对函数。两处各写一份必然漂移，而漂移的表现是
"明明没人改过数据，校验却报篡改"——这类假警报会让人彻底不再信任校验功能。

## 哈希规则（严格照 BR-12-01 ~ 03，不得自行"优化"）

- 创世块：首条记录的 `prev_hash` 是字符串常量 **`GENESIS`**（不是空串）
- 公式：`hash = sha256(prev_hash + "|" + canonical_payload)`，输出 64 位小写十六进制
- `canonical_payload`：字段顺序固定为
  `ts|actor|actor_role|action|target_type|target_id|before|after`，用 `|` 连接；
  `before`/`after` 以 `sort_keys=True, separators=(",",":"), ensure_ascii=False` 序列化；
  **`null` 一律序列化为空串**

## 为什么不收录 `ip` / `ua` / `chain_id`

BR-12-03 明确只列了那 8 个字段。**这是一个需要知情的取舍**：`ip`/`ua` 不参与哈希，
意味着单独篡改它们**检测不出来**（`after`/`actor` 等字段改动仍会被发现）。
本模块按 Spec 实现，并把该局限登记为待确认项——若要纳入，必须**现在就定**，
因为一旦积累大量数据后再改公式，既有记录将全部校验失败。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

# 创世哈希（BR-12-01）：常量字符串，首条记录的 prev_hash 必须等于它
GENESIS_HASH = "GENESIS"

# 参与哈希的字段与**固定顺序**（BR-12-03）。改动此元组等于改动哈希格式。
CANONICAL_FIELDS: tuple[str, ...] = (
    "ts", "actor", "actor_role", "action", "target_type", "target_id", "before", "after",
)

# 需要 JSON 序列化的字段
_JSON_FIELDS = frozenset({"before", "after"})


def canonical_json(value: Any) -> str:
    """规范化 JSON：键排序 + 紧凑分隔符 + 中文不转义。

    三个参数都不能少：`sort_keys` 保证字段顺序稳定；`separators` 去掉空格
    （默认的 `", "` 会让同一份内容因格式差异算出不同哈希）；`ensure_ascii=False`
    让中文按 UTF-8 原样参与，便于用任意工具人工复算。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_payload(doc: dict) -> str:
    """把一条审计记录规范化成哈希输入的载荷字符串（BR-12-03）。"""
    parts: list[str] = []
    for field in CANONICAL_FIELDS:
        value = doc.get(field)
        if value is None:
            # null -> 空串（BR-12-03）。注意不要用 "null" 字面量：
            # 那会让"字段缺失"和"字段值为 null"无法区分
            parts.append("")
        elif field in _JSON_FIELDS:
            parts.append(canonical_json(value))
        else:
            parts.append(str(value))
    return "|".join(parts)


def compute_hash(prev_hash: Optional[str], doc: dict) -> str:
    """计算本条记录的哈希（BR-12-02）。

    `prev_hash` 为空时按创世块处理——但**写入路径不应走到这个分支**：
    调用方必须显式传 `GENESIS_HASH`，否则"空 prev_hash"会悄悄污染链头。
    """
    prev = prev_hash if prev_hash else GENESIS_HASH
    material = f"{prev}|{canonical_payload(doc)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def short_hash(value: Optional[str], keep: int = 4) -> str:
    """缩略展示 `0x8f3a…c21d`（BR-12-23 的前端展示约定，后端日志里也用）。"""
    if not value:
        return "—"
    if len(value) <= keep * 2:
        return value
    return f"0x{value[:keep]}…{value[-keep:]}"
