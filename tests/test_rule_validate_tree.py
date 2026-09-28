# -*- coding: utf-8 -*-
"""模块 06-B：**证明条件树校验确实由 05 提供**（V-06-03 / BR-06-17 / BR-06-19）。

这份用例的全部价值在于**可证伪**：如果 06 自己写了一套结构校验（哪怕只是
"顺手也检查一下 field 在不在"），下面这些桩就会失去效果——桩在
`app/engine/condition.validate_tree` 上，06 的实现若绕开它，桩错误就传不出来。

因此每个用例都把桩插在 **05 侧**（而不是 `rule_service` 里），并断言：
- `/rules/validate-tree` 原样回显出桩的 `path` 与 `message`；
- 保存路径（`POST /rules` / `PUT /rules/{code}`）也因桩而拒绝，**且不落库**；
- 桩成功时，**落库的条件树是桩的返回值**而不是请求体（证明归一化来自 05）。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_rule_validate_tree.py -q
"""
from __future__ import annotations

import pytest

from app import constants, db
from app.engine import condition as condition_engine
from app.engine.condition import ConditionNode
from app.errors import InvalidConditionTreeError
from tests.conftest import WRITER

pytestmark = pytest.mark.anyio

RULES_URL = "/api/v1/rules"
VALIDATE_URL = "/api/v1/rules/validate-tree"
RULES_COLL = constants.COLL_RULES
SCENES_COLL = constants.COLL_RULE_SCENES

SCENES = [
    {"_id": "login", "name": "登录", "event_types": ["login"], "sort": 10},
    {"_id": "common", "name": "通用", "event_types": ["login"], "sort": 90},
]

TREE = {"logic": "and", "children": [
    {"field": "device_user_cnt", "op": "gte", "value": 5},
]}


@pytest.fixture(autouse=True)
async def seed_scenes():
    col = db.get_db()[SCENES_COLL]
    for row in SCENES:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    yield


def body_of(resp) -> dict:
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    assert b["ok"] is (b["code"] == "OK")
    return b


def data_of(resp) -> dict:
    return body_of(resp)["data"]


async def post_rule(client, **over):
    payload = {"name": "规则", "scene_code": "login", "condition": TREE, "score": 45}
    payload.update(over)
    return await client.post(RULES_URL, json=payload, headers=WRITER)


# ================================================================ 桩：05 报错
def _stub_reject(raw):
    """替身：无论输入什么都报一个可识别的结构错误（模拟 05 判定非法）。"""
    raise InvalidConditionTreeError("桩：叶子节点必须含非空 field（特征键）",
                                    "children[0].field")


async def test_validate_endpoint_returns_stub_error(client, monkeypatch):
    """V-06-03（前半）：`/rules/validate-tree` 原样回显 05 的桩错误，且**仍是 200**。"""
    monkeypatch.setattr(condition_engine, "validate_tree", _stub_reject)
    r = await client.post(VALIDATE_URL, json={"condition": TREE}, headers=WRITER)
    assert r.status_code == 200, "§3.1：校验失败也是 200，靠 valid=false 表达"
    got = data_of(r)
    assert got["valid"] is False
    assert got["normalized"] is None
    assert len(got["errors"]) == 1
    err = got["errors"][0]
    assert "桩：" in err["message"], "回显的必须是 05 的原文，不能是本模块另写的一句"
    assert err["path"] == "children[0].field", "path 是前端按节点定位高亮的依据"
    assert err["code"] == "RUL-4002", "错误来源保持 05 的原码（ER-02）"
    assert err["cfg_code"] == "CFG-4003", "同时给出 06 对外码，便于前端按 §5.1 映射文案"


async def test_save_uses_stub_and_does_not_persist(client, monkeypatch):
    """V-06-03（后半）/ V-06-05：保存时出现**同一个桩错误**，且 `rules` 无新增。

    这是"没有第二套校验"的关键证据：06 若自己判"这棵树看起来没问题"，
    这里就会 201 成功——而桩根本没被调用。
    """
    monkeypatch.setattr(condition_engine, "validate_tree", _stub_reject)
    r = await post_rule(client)
    assert r.status_code == 400, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4003"
    assert "桩：" in b["message"]
    assert b["data"]["errors"][0]["path"] == "children[0].field"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0, "校验不通过绝不落库"


async def test_update_uses_stub_and_does_not_persist(client, monkeypatch):
    """修改路径同样只认 05 的结论（否则会出现"编辑抽屉能过、新建不能过"的怪象）。"""
    created = await post_rule(client)
    assert created.status_code == 201, created.text
    code = data_of(created)["_id"]

    monkeypatch.setattr(condition_engine, "validate_tree", _stub_reject)
    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER,
                         json={"expected_version": 1, "condition": TREE})
    assert r.status_code == 400 and body_of(r)["code"] == "CFG-4003"
    doc = await db.get_db()[RULES_COLL].find_one({"_id": code})
    assert doc["version"] == 1, "被拒绝的修改不得动 version（否则乐观锁会错位）"
    assert doc["condition"] == TREE


# ================================================================ 桩：05 归一化
def _stub_normalize(raw):
    """替身：返回一棵与输入**无关**的树，用来证明"落库的是 05 的产物"。"""
    _stub_normalize.seen.append(raw)
    return ConditionNode.model_validate({
        "logic": "and",
        "children": [{"field": "stub_normalized_field", "op": "gte", "value": 999}],
    })


_stub_normalize.seen = []  # type: ignore[attr-defined]


async def test_saved_tree_is_the_normalized_output_of_05(client, monkeypatch):
    """落库的条件树必须等于 `validate_tree()` 的返回值，而不是请求体。

    归一层看似多余（结构合法时二者通常相同），但它保证了"编辑器里的树"
    与"求值器读到的树"是同一份；也保证了像 `exists` 这类"键在不在有语义"
    的算子不会被前端提交的形态差异带偏。
    """
    _stub_normalize.seen.clear()
    monkeypatch.setattr(condition_engine, "validate_tree", _stub_normalize)

    r = await post_rule(client, condition={"logic": "or", "children": [
        {"field": "whatever_client_sent", "op": "eq", "value": 1},
    ]})
    assert r.status_code == 201, r.text
    code = data_of(r)["_id"]

    doc = await db.get_db()[RULES_COLL].find_one({"_id": code})
    assert doc["condition"] == {
        "logic": "and",
        "children": [{"field": "stub_normalized_field", "op": "gte", "value": 999}],
    }, "落库的必须是 05 归一化后的树"
    assert _stub_normalize.seen, "05 的 validate_tree 必须被真的调用过"
    assert _stub_normalize.seen[0] == {"logic": "or", "children": [
        {"field": "whatever_client_sent", "op": "eq", "value": 1},
    ]}, "06 必须把客户端原始 JSON 原样交给 05，不得先自行加工"


async def test_validate_endpoint_returns_normalized_tree(client):
    """正常路径：`valid=true` + `normalized` 就是前端保存时应提交的树。"""
    r = await client.post(VALIDATE_URL, json={"condition": TREE}, headers=WRITER)
    assert r.status_code == 200
    got = data_of(r)
    assert got["valid"] is True and got["errors"] == []
    assert got["normalized"] == TREE


# ================================================================ 真实 05 行为
async def test_invalid_tree_reports_node_path(client):
    """BR-06-18 / §2.3：错误必须带 `path` 才能让前端定位到对应节点行。"""
    broken = {"logic": "and", "children": [
        {"field": "device_user_cnt", "op": "gte", "value": 5},
        {"op": "gte", "value": 3},                      # 缺 field
    ]}
    r = await client.post(VALIDATE_URL, json={"condition": broken}, headers=WRITER)
    assert r.status_code == 200
    got = data_of(r)
    assert got["valid"] is False
    assert got["errors"][0]["path"].startswith("children[1]"), got["errors"]


@pytest.mark.parametrize("bad_tree,why", [
    ("device_user_cnt >= 5", "字符串表达式（AD-07 禁止）"),
    ([{"field": "a", "op": "eq", "value": 1}], "根节点不是对象"),
    ({"logic": "and", "children": []}, "非叶节点 children 为空"),
    ({"field": "a", "op": "like", "value": "x"}, "算子不在 condition_op 枚举内"),
    ({"field": "", "op": "eq", "value": 1}, "field 为空"),
    ({"field": "a", "op": "eq"}, "缺少 value"),
    ({"field": "a", "op": "exists", "value": 1}, "exists 不接受 value"),
    ({"feild": "a", "op": "eq", "value": 1}, "键名拼错（多余键必须报错）"),
    ({"logic": "and", "children": [{"field": "a", "op": "eq", "value": 1}],
      "op": "eq"}, "叶与枝混用"),
])
async def test_illegal_trees_are_rejected_on_save(client, bad_tree, why):
    """BR-06-15 / BR-06-17：非法树一律 `400 CFG-4003` 且不落库。"""
    r = await post_rule(client, condition=bad_tree)
    assert r.status_code == 400, f"{why}：应被拒绝，实际 {r.status_code}"
    assert body_of(r)["code"] == "CFG-4003", why
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0, why


async def test_validation_does_not_evaluate_or_check_feature_whitelist(client):
    """BR-06-19：只做结构与枚举校验，不做求值、不校验特征键是否存在于 E02。

    `field` 取一个不存在的特征名**是合法的结构**（`condition.py` 的模块 docstring
    明确说明：该比对属"配置质量"，混进来会让 RUL-4002 的语义失焦）。因此这里
    必须 `valid=true`——若哪天有人在 06 里加了"特征白名单校验"，这条会立刻变红，
    而那正是两份校验开始漂移的第一步。
    """
    r = await client.post(VALIDATE_URL, headers=WRITER, json={
        "condition": {"logic": "and", "children": [
            {"field": "this_feature_does_not_exist", "op": "eq", "value": 1},
        ]},
    })
    assert r.status_code == 200
    got = data_of(r)
    assert got["valid"] is True, "结构校验不做特征白名单比对（那属于 04 的关注点）"


async def test_exists_operator_keeps_key_absence_semantics(client):
    """`exists` 不带 `value` 是合法的；带 `value` 才非法（键在不在有语义）。"""
    ok = await client.post(VALIDATE_URL, headers=WRITER, json={
        "condition": {"logic": "and", "children": [
            {"field": "ip_is_proxy", "op": "exists"},
        ]},
    })
    assert data_of(ok)["valid"] is True
    assert data_of(ok)["normalized"] == {"logic": "and", "children": [
        {"field": "ip_is_proxy", "op": "exists"},
    ]}, "归一化不得凭空补一个 value 键"

    created = await post_rule(client, condition={"logic": "and", "children": [
        {"field": "ip_is_proxy", "op": "exists"},
    ]})
    assert created.status_code == 201
    code = data_of(created)["_id"]
    doc = await db.get_db()[RULES_COLL].find_one({"_id": code})
    assert "value" not in doc["condition"]["children"][0]


async def test_eq_null_keeps_the_value_key(client):
    """`{"op": "eq", "value": null}` 必须保留 `value` 键（不能被归一化悄悄删掉）。"""
    tree = {"logic": "and", "children": [{"field": "a", "op": "eq", "value": None}]}
    r = await client.post(VALIDATE_URL, json={"condition": tree}, headers=WRITER)
    got = data_of(r)
    assert got["valid"] is True
    assert "value" in got["normalized"]["children"][0], (
        "用 exclude_none 会把 eq null 变成「没有比较值」，语义被静默改变"
    )


async def test_common_scene_tree_and_nested_or_roundtrip(client):
    """AND/OR 嵌套原样往返（V-06-04 的服务端半边：结构不失真）。"""
    nested = {"logic": "and", "children": [
        {"field": "device_user_cnt", "op": "gte", "value": 5},
        {"logic": "or", "children": [
            {"field": "ip_is_proxy", "op": "eq", "value": True},
            {"field": "user_age_days", "op": "lt", "value": 3},
        ]},
    ]}
    created = await post_rule(client, scene_code="common", condition=nested)
    assert created.status_code == 201, created.text
    code = data_of(created)["_id"]
    doc = await db.get_db()[RULES_COLL].find_one({"_id": code})
    assert doc["condition"] == nested
    assert data_of(created)["condition"] == nested
