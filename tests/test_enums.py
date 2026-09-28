# -*- coding: utf-8 -*-
"""枚举单一真源校验（BR-00-16 / 17 / 18，对应 V-00-05）。

**为什么值得单独测**：枚举是前后端共用的字典，漏一组或改一个取值，前端下拉框
会静默少一个选项、后端校验会静默拒绝合法输入——两种都是"看起来正常但功能缺失"
的缺陷。因此这里与 `01_数据实体` §4 的 22 组**逐组**比对。
"""
from __future__ import annotations

import pytest

from app import enums
from app.enums import LabeledStrEnum

pytestmark = pytest.mark.anyio

# 逐字取自 `01_数据实体/数据实体设计.md` §4「枚举字典」（共 22 组）
EXPECTED_GROUPS: set[str] = {
    "event_type", "decision", "risk_level", "rule_status", "list_type", "list_status",
    "entity_type", "case_status", "conclusion", "action_type", "level", "user_status",
    "relation", "bucket_type", "granularity", "role", "sys_user_status", "engine_type",
    "fuse_mode", "model_status", "condition_op", "risk_tag",
}

# 六个实体各自的 status 取值域（BR-00-17：禁止共用一个通用 StatusEnum）
STATUS_GROUPS = ["rule_status", "list_status", "case_status", "user_status",
                 "sys_user_status", "model_status"]


async def test_group_set_matches_entity_dictionary_exactly():
    """BR-00-18：组集合必须与 Step1 枚举字典完全一致，缺一组/多一组都是缺陷。"""
    assert set(enums.ENUM_GROUPS) == EXPECTED_GROUPS
    assert len(enums.ENUM_GROUPS) == 22


@pytest.mark.parametrize("group", sorted(EXPECTED_GROUPS))
async def test_each_group_is_complete(group: str):
    """每组：非空、标签齐全、取值唯一且为字符串。"""
    members = list(enums.ENUM_GROUPS[group])
    assert members, f"{group} 是空枚举"
    for m in members:
        assert isinstance(m.value, str) and m.value, f"{group}.{m.name} 取值非法"
        assert isinstance(m.label, str) and m.label.strip(), f"{group}.{m.name} 缺少中文标签"
    values = [m.value for m in members]
    assert len(values) == len(set(values)), f"{group} 取值重复：{values}"


async def test_status_enums_are_distinct_classes():
    """BR-00-17：六个实体各自定义 status 枚举，禁止共用通用 StatusEnum。

    `active` 在 ListStatus / UserStatus / SysUserStatus 里都出现，但**语义不同**
    （名单生效中 / 用户正常 / 账号启用）。若共用一个类，改一处会连带改掉其它实体
    的取值域，正是该条规则要防的脏数据来源。
    """
    classes = {name: enums.ENUM_GROUPS[name] for name in STATUS_GROUPS}
    assert len(set(classes.values())) == len(STATUS_GROUPS), "存在被多个实体共用的 status 枚举"
    # rule_status 与 case_status 的取值域必须完全不相交（enabled/disabled vs pending/...）
    rule_values = {m.value for m in classes["rule_status"]}
    case_values = {m.value for m in classes["case_status"]}
    assert not (rule_values & case_values), "规则状态与案件状态取值域不应有交集"


async def test_enum_values_are_usable_as_plain_strings():
    """枚举要能与库中字符串直接比较、直接序列化（否则每个比较点都要 .value）。"""
    assert enums.Decision.REJECT == "reject"
    assert enums.ListType.BLACK.value == "black"
    assert f"{enums.EventType.LOGIN}" == "login"
    assert enums.RiskLevel.HIGH in ("low", "medium", "high")


async def test_options_shape_matches_frontend_contract():
    """`/common/enums` 的 `{value,label}` 结构（§3.3）。"""
    opts = enums.options(enums.ListType)
    assert opts == [
        {"value": "black", "label": "黑名单"},
        {"value": "white", "label": "白名单"},
        {"value": "gray", "label": "灰名单"},
    ]
    assert set(enums.all_options()) == EXPECTED_GROUPS


async def test_label_of_falls_back_without_raising():
    """后端生成文案时用到；未知取值不能抛异常（否则会把只读接口打成 500）。"""
    assert enums.label_of(enums.ListType, "black") == "黑名单"
    assert enums.label_of(enums.ListType, "purple") == "未知"
    assert enums.label_of(enums.ListType, "purple", "其他") == "其他"


async def test_labeled_str_enum_rejects_non_tuple_members():
    """守住基类契约：成员必须是 (取值, 标签) 二元组，写错要立刻报错。"""
    with pytest.raises(TypeError):
        class Broken(LabeledStrEnum):  # noqa: D101 - 故意写错以验证基类行为
            X = "only_value"  # type: ignore[assignment]


# ============================================================ 接口层
async def test_common_enums_endpoint(client):
    """V-00-05：接口返回 22 组静态枚举 + 数据驱动的 rule_scenes。"""
    r = await client.get("/api/v1/common/enums")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["code"] == "OK"
    data = body["data"]
    static_groups = set(data) - {"rule_scenes"}
    assert static_groups == EXPECTED_GROUPS
    for group in EXPECTED_GROUPS:
        assert isinstance(data[group], list) and data[group], group
        assert set(data[group][0]) == {"value", "label"}
    # rule_scenes 是 E06 的数据行（D10），不是代码枚举，因此单独校验其结构
    assert isinstance(data["rule_scenes"], list)


async def test_common_meta_endpoint(client):
    """§3.3：返回 server_time / api_version / env。"""
    r = await client.get("/api/v1/common/meta")
    assert r.status_code == 200
    data = r.json()["data"]
    assert isinstance(data["server_time"], str) and "T" in data["server_time"]
    assert data["api_version"] == "/api/v1"
    assert data["env"] in ("dev", "prod", "test")
