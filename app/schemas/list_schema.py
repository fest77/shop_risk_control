# -*- coding: utf-8 -*-
"""名单条目的请求 / 响应模型（对齐 01_数据实体 E07 与模块 06 §3.2）。

设计取舍：`list_type` / `entity_type` 在模型层用 **str** 而不是 Enum。
原因：模块 06 §5.1 要求非法 `entity_type` 返回 `400 CFG-4010`。
若用 Enum，Pydantic 会先拦下来变成通用校验错误（模块 00 的 COM-4001），拿不到契约要求的错误码。
因此模型层宽松、服务层显式校验并抛对应错误码。

唯一例外是"空白字符串"这类**纯格式问题**：在模型层直接用 validator 拦掉，
报 `COM-4001`（参数校验失败），因为"空白"不属于任何枚举语义。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.errors import InvalidQueryParamError

LIST_TYPES = ("black", "white", "gray")
ENTITY_TYPES = ("user", "phone", "ip", "device", "address")
ENTRY_STATUSES = ("active", "expired", "removed")

# 排序字段白名单（模块 06 §3.5：字段必须在白名单内，否则 CFG-4008）
SORTABLE_FIELDS = ("effective_at", "expire_at", "created_at",
                   "list_type", "entity_type", "status")


class ListEntryCreate(BaseModel):
    """POST /api/v1/lists 请求体。"""

    list_type: str = Field(..., description="black / white / gray")
    entity_type: str = Field(..., description="user / phone / ip / device / address")
    entity_value: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=200)
    expire_at: Optional[int] = Field(
        default=None, description="毫秒时间戳；null = 永久（灰名单默认 30 天）"
    )
    force: bool = Field(
        default=False,
        description="true 时才允许覆盖「同实体已在另一名单类型」的冲突（BR-06-20）",
    )

    @field_validator("entity_value", "reason")
    @classmethod
    def _strip_and_reject_blank(cls, v: str) -> str:
        """去掉首尾空白，并拒绝纯空白。

        仅靠 `min_length=1` 挡不住 `"   "`（长度是 3）。
        """
        s = (v or "").strip()
        if not s:
            raise ValueError("不能为空白")
        return s


class ListEntryOut(BaseModel):
    """名单条目对外表示（字段与模块 06 §3.2 的 items 契约逐字对齐）。

    注意：Pydantic v2 会把以 `_` 开头的字段当作**私有属性**，不会进入 `model_dump()`。
    因此这里用 `id` 承载、以 `alias="_id"` 输出，序列化时必须 `by_alias=True`。
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    list_type: str
    entity_type: str
    entity_value: str
    reason: Optional[str] = None
    source: Optional[str] = None
    related_case_no: Optional[str] = None
    effective_at: int
    expire_at: Optional[int] = None
    status: str
    operator: Optional[str] = None
    created_at: int

    @classmethod
    def from_doc(cls, doc: dict) -> "ListEntryOut":
        return cls(
            id=doc["_id"],
            list_type=doc["list_type"],
            entity_type=doc["entity_type"],
            entity_value=doc["entity_value"],
            reason=doc.get("reason"),
            source=doc.get("source"),
            related_case_no=doc.get("related_case_no"),
            effective_at=doc["effective_at"],
            expire_at=doc.get("expire_at"),
            status=doc["status"],
            operator=doc.get("operator"),
            created_at=doc["created_at"],
        )


class CountsOut(BaseModel):
    black: int = 0
    white: int = 0
    gray: int = 0


class ListQueryResult(BaseModel):
    """列表响应体（模块 00 §3.2 分页契约：items / total / page / page_size / pages）。"""

    items: list[ListEntryOut]
    total: int
    page: int
    page_size: int
    # 总页数。total=0 时为 0，前端据此展示空态而不是"第 1/1 页"。
    pages: int
    counts: CountsOut
    as_of: int


# ============================================================
# 批量导入（BR-06-28 / 29 / 30）
# ============================================================
# **列名只在这里出现一次**：模板下载接口与导入解析共用同一个元组，
# 因此"模板给的表头"与"导入认的表头"在物理上不可能漂移——这正是
# CFG-4012（表头不匹配）最容易被搞错的地方：两处各写一份字面量，
# 改了模板忘了改解析，用户就会拿着官方模板却被告知表头不匹配。
IMPORT_HEADER: tuple[str, ...] = (
    "list_type", "entity_type", "entity_value", "reason", "expire_at",
)
# `expire_at` 留空 = 永久（黑/白）或走默认（灰 30 天），与 POST /lists 的语义一致
IMPORT_OPTIONAL_HEADER: tuple[str, ...] = ("expire_at",)

# 单次导入行数上限（BR-06-30 取值）。不含表头。
IMPORT_MAX_ROWS = 5000
# 错误行明细回传上限（模块 06 §3.2）：超出只回传前 N 条并给出提示，
# 否则一次全错（5000 行）的响应体本身就是几 MB，页面渲染与传输都无意义。
IMPORT_ERROR_LIMIT = 200

IMPORT_MODES = ("partial", "atomic")

# 解析 CSV 时依次尝试的编码。顺序有讲究：`utf-8-sig` 必须排在 `utf-8` 之前，
# 否则 Excel 另存为 UTF-8 CSV 时写入的 BOM 会变成第一个列名的一部分
# （`\ufefflist_type`），表头比对随即失败。
IMPORT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk")


class ListEntryRemoveResult(BaseModel):
    """`DELETE /lists/{entry_id}` 的响应体。

    `impact` 是 BR-06-26「影响面提示」落库后的复核值：前端在二次确认弹窗里
    展示的是**移除前**的条数，这里回传移除后的条数，便于页面刷新缓存。
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    list_type: str
    entity_type: str
    entity_value: str
    source: str
    status: str
    removed_at: int
    removed_by: str
    remaining_active: int


class ImportFailRow(BaseModel):
    """一行导入失败明细（`row` 为**文件内真实行号**，表头是第 1 行）。"""

    row: int
    entity_value: str
    reason: str


class ImportResult(BaseModel):
    """`POST /lists/import` 的响应体（模块 06 §3.2：总数/成功/失败 + 错误明细）。"""

    total: int
    success: int
    failed: int
    mode: str
    rows: list[ImportFailRow]
    imported_ids: list[str]
    # 错误明细被截断时为 True，页面提示"下载错误明细"
    rows_truncated: bool = False


def parse_import_list_type_default(value: Optional[str]) -> Optional[str]:
    """解析导入表单的 `list_type` 默认值。

    非空但取值非法时**立即报错**而不是逐行报错：这是表单级参数错误
    （用户没选对默认名单类型），逐行重复 5000 次同一条原因没有信息量。
    """
    v = (value or "").strip()
    if not v:
        return None
    if v not in LIST_TYPES:
        raise InvalidQueryParamError(
            f"list_type 默认值仅支持 {'/'.join(LIST_TYPES)}，收到：{value}"
        )
    return v


def parse_sort(sort: str) -> list[tuple[str, int]]:
    """解析 `字段:asc|desc`，逗号分隔多级；非法字段抛 ValueError 由服务层转 CFG-4008。"""
    pairs: list[tuple[str, int]] = []
    for token in (sort or "").split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            field, _, direction = token.partition(":")
        else:
            field, direction = token, "asc"
        field = field.strip()
        direction = direction.strip().lower()
        if field not in SORTABLE_FIELDS:
            raise ValueError(f"排序字段不在白名单内：{field}")
        if direction not in ("asc", "desc"):
            raise ValueError(f"排序方向非法：{direction}")
        pairs.append((field, 1 if direction == "asc" else -1))
    if not pairs:
        pairs = [("effective_at", -1)]
    return pairs


__all__ = [
    "LIST_TYPES", "ENTITY_TYPES", "ENTRY_STATUSES", "SORTABLE_FIELDS",
    "IMPORT_HEADER", "IMPORT_OPTIONAL_HEADER", "IMPORT_MAX_ROWS",
    "IMPORT_ERROR_LIMIT", "IMPORT_MODES", "IMPORT_ENCODINGS",
    "ListEntryCreate", "ListEntryOut", "CountsOut", "ListQueryResult",
    "ListEntryRemoveResult", "ImportFailRow", "ImportResult",
    "parse_sort", "parse_import_list_type_default",
]
