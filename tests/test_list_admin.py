# -*- coding: utf-8 -*-
"""模块 06-A「名单库收尾」验收测试：移除条目 / 批量导入 / 模板下载 / 过期清理。

覆盖 Spec：`06_规则与名单配置管理.md` §3.2 名单接口、BR-06-25/27/28/29/30/31/36、
§5.1 错误码、§5.2/§5.3 破坏性操作与 fail-closed、V-06-13 / V-06-16 / V-06-17 / V-06-18。

**所有断言都落在真实行为上**（查库状态、真实 audit_logs 文档、真实 CSV 往返），
不使用"调用过某函数"这类空壳断言——空壳断言在实现被删掉后依然是绿的。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_list_admin.py -q
"""
from __future__ import annotations

import csv
import io

import pytest
from pymongo.errors import PyMongoError

from app import constants, db
from app.core import list_cleanup_task
from app.repos.audit_repo import AuditRepo
from app.schemas.list_schema import IMPORT_HEADER
from app.services import list_service
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

LIST_URL = "/api/v1/lists"
IMPORT_URL = "/api/v1/lists/import"
TEMPLATE_URL = "/api/v1/lists/import-template"
COLL = constants.COLL_LIST_ENTRIES
DAY_MS = 24 * 3600 * 1000


def body_of(resp) -> dict:
    """统一响应包结构断言，返回整个包（与 test_list_slice.py 同一口径）。"""
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    assert b["ok"] is (b["code"] == "OK"), "ok 必须由 code 派生，不允许两处状态打架"
    assert isinstance(b["trace_id"], str) and b["trace_id"]
    return b


def data_of(resp) -> dict:
    return body_of(resp)["data"]


async def create(client, headers=WRITER, **over) -> str:
    """建一条名单条目，返回 `_id`（默认黑名单 + 设备维度）。"""
    payload = {
        "list_type": "black",
        "entity_type": "device",
        "entity_value": "D8F2A1C4",
        "reason": "羊毛党设备聚集，已确认批量套券",
    }
    payload.update(over)
    r = await client.post(LIST_URL, json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return data_of(r)["_id"]


async def read_entry(entry_id: str) -> dict:
    doc = await db.get_db()[COLL].find_one({"_id": entry_id})
    assert doc is not None, f"条目 {entry_id} 不存在"
    return doc


async def audit_rows(action: str) -> list[dict]:
    cursor = db.get_db()[constants.COLL_AUDIT_LOGS].find({"action": action})
    return await cursor.to_list(length=1000)


def csv_bytes(rows: list[list[str]], header: list[str] | None = None) -> bytes:
    """构造导入用的 CSV 字节流（默认用契约表头）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header if header is not None else list(IMPORT_HEADER))
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def ok_row(value: str, **over) -> list[str]:
    row = {
        "list_type": "black", "entity_type": "device",
        "entity_value": value, "reason": "导入：批量套券", "expire_at": "",
    }
    row.update(over)
    return [row[c] for c in IMPORT_HEADER]


async def upload(client, content: bytes, headers=WRITER, **form):
    files = {"file": ("entries.csv", content, "text/csv")}
    return await client.post(IMPORT_URL, files=files, data=form, headers=headers)


# ================================================================ 1 移除条目
async def test_auto_source_entry_cannot_be_removed(client):
    """① BR-06-27：`source=auto` 的条目不允许在本页移除 -> 403 CFG-4032。

    `auto` 条目只能由 08 处置模块写入，因此这里直接改库造出该来源
    （不通过接口——接口本就**不该**提供"手工写成 auto"的入口）。
    """
    entry_id = await create(client)
    await db.get_db()[COLL].update_one({"_id": entry_id}, {"$set": {"source": "auto"}})

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    assert r.status_code == 403, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4032"
    assert "案件处置" in b["message"]
    assert b["data"]["entry_id"] == entry_id

    doc = await read_entry(entry_id)
    assert doc["status"] == "active", "被拒绝的移除绝不能改动条目状态"
    assert await audit_rows("list.remove") == []


async def test_remove_unknown_id_returns_404(client):
    """② 不存在的 id -> 404 CFG-4011。"""
    r = await client.delete(f"{LIST_URL}/L_NOT_EXIST_0001", headers=WRITER)
    assert r.status_code == 404, r.text
    assert body_of(r)["code"] == "CFG-4011"


async def test_remove_entry_whose_status_changed_returns_409(client):
    """③ 状态已变（并发移除 / 已被清理任务置 expired）-> 409 CFG-4006。

    这里不等真并发：把状态改成 `expired` 就等价于"读到 active 之后被人改掉了"，
    而实现的判据正是"条件更新匹配 0 条"，两条路径走的是同一段代码。
    """
    entry_id = await create(client)
    await db.get_db()[COLL].update_one({"_id": entry_id}, {"$set": {"status": "expired"}})

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    assert r.status_code == 409, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4006"
    assert "expired" in b["message"], "冲突提示应带上服务端当前状态"
    assert (await read_entry(entry_id))["status"] == "expired", "冲突时不得写入 removed"


async def test_remove_writes_soft_delete_and_audit(client):
    """④ 移除成功：库里 status=removed，且 audit_logs 有一条含 before/after 的 list.remove。"""
    entry_id = await create(client, entity_type="ip", entity_value="117.136.12.88",
                            list_type="gray")

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    assert r.status_code == 200, r.text
    d = data_of(r)
    assert d["_id"] == entry_id and d["status"] == "removed"
    assert d["removed_by"] == "strategy01"      # 操作人来自令牌身份
    assert isinstance(d["removed_at"], int) and d["removed_at"] > 0
    assert d["remaining_active"] == 0           # 影响面：移除后该实体已无在效条目

    doc = await read_entry(entry_id)
    assert doc["status"] == "removed"
    assert doc["removed_by"] == "strategy01"
    assert doc["removed_at"] == d["removed_at"]
    assert "deleted_at" not in doc, "必须是软删，不允许物理删除（历史命中明细要能回溯）"

    logs = await audit_rows("list.remove")
    assert len(logs) == 1
    log = logs[0]
    assert log["target_type"] == "list_entry" and log["target_id"] == entry_id
    assert log["actor"] == "strategy01" and log["actor_role"] == "strategist"
    assert log["before"] == {"status": "active"}
    assert log["after"]["status"] == "removed"
    assert log["after"]["removed_by"] == "strategy01"
    # 审计链本身也被真实写入（prev_hash/hash 由模块 12 生成，本模块不拼）
    assert log["hash"] and log["prev_hash"]

    # 移除后不再计入 active 计数（§3.2 的 counts 只统计 active）
    listed = await client.get(LIST_URL, params={"list_type": "gray"}, headers=WRITER)
    assert data_of(listed)["counts"] == {"black": 0, "white": 0, "gray": 0}
    assert data_of(listed)["total"] == 0


async def test_audit_failure_rolls_back_removal(client, monkeypatch):
    """⑤ BR-06-36：审计写失败 -> 回滚软删，条目仍 active，接口返回 CFG-5003。"""

    entry_id = await create(client)

    async def boom(self, doc):
        raise PyMongoError("simulated audit db down")

    monkeypatch.setattr(AuditRepo, "insert", boom)

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    assert r.status_code == 503, r.text
    b = body_of(r)
    assert b["code"] == "CFG-5003"
    assert "回滚" in b["message"]

    doc = await read_entry(entry_id)
    assert doc["status"] == "active", "审计没留痕就不能生效，状态必须回滚"
    assert "removed_at" not in doc and "removed_by" not in doc, "移除痕迹必须一并清掉"
    assert await audit_rows("list.remove") == []
    # 条目回到可用状态：列表里仍能看到它
    listed = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    assert data_of(listed)["total"] == 1


async def test_cache_invalidation_failure_rolls_back_removal(client, monkeypatch):
    """BR-06-25：缓存失效失败同样回滚软删（不允许"状态改了但缓存没失效"）。
    """

    entry_id = await create(client)

    def boom() -> None:
        raise RuntimeError("simulated cache flush failure")

    monkeypatch.setattr(list_service, "invalidate_list_cache", boom)

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    assert r.status_code == 503, r.text
    assert body_of(r)["code"] == "CFG-5002"
    assert (await read_entry(entry_id))["status"] == "active"


async def test_remove_invalidates_cache_on_success(client):
    """BR-06-23：移除成功后**确实**走到了缓存失效那一步。

    断言降级状态里的 `last_flush_at` 被推后——本项目还没有名单缓存
    （属模块 05），`last_flush_at` 就是"失效调用点被执行过"的唯一可观测痕迹。
    """
    from app.core.degraded import DEGRADED

    DEGRADED.last_flush_at = 0
    entry_id = await create(client)
    DEGRADED.last_flush_at = 0          # 排除 create 自身那次失效的干扰

    assert (await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)).status_code == 200
    assert DEGRADED.last_flush_at > 0, "移除成功后必须调用缓存失效"


async def test_remove_reviewer_denied(client):
    """⑪ 权限：reviewer 调 DELETE -> 403 AUTH-4020（越权统一由权限层给出）。"""
    entry_id = await create(client)

    r = await client.delete(f"{LIST_URL}/{entry_id}", headers=READER)
    assert r.status_code == 403, r.text
    assert body_of(r)["code"] == "AUTH-4020"
    assert (await read_entry(entry_id))["status"] == "active"

    # admin 同样无 list:write（决策 D26 的矩阵裁定）
    r2 = await client.delete(f"{LIST_URL}/{entry_id}", headers=ADMIN)
    assert r2.status_code == 403 and body_of(r2)["code"] == "AUTH-4020"


# ================================================================ 2 批量导入
async def test_import_partial_keeps_success_rows_and_reports_failures(client):
    """⑥ V-06-13：3 行合法 + 2 行非法 -> success=3 / failed=2，明细含**行号**与原因。"""
    rows = [
        ok_row("D0001"),                                        # 第 2 行
        ok_row("D0002", entity_type="imei"),                    # 第 3 行：实体类型非法
        ok_row("D0003"),                                        # 第 4 行
        ok_row("D0004", reason=""),                             # 第 5 行：原因缺失
        ok_row("D0005"),                                        # 第 6 行
    ]
    r = await upload(client, csv_bytes(rows))
    assert r.status_code == 200, r.text
    d = data_of(r)

    assert d["total"] == 5 and d["success"] == 3 and d["failed"] == 2
    assert d["mode"] == "partial"
    assert [e["row"] for e in d["rows"]] == [3, 5], "错误明细必须用文件真实行号（表头=1）"
    assert d["rows"][0]["entity_value"] == "D0002"
    assert "实体类型" in d["rows"][0]["reason"]
    assert d["rows"][1]["entity_value"] == "D0004"
    assert "reason" in d["rows"][1]["reason"]

    # partial：合法行确实入库了
    stored = await db.get_db()[COLL].find({"source": "manual"}).to_list(length=100)
    assert sorted(x["entity_value"] for x in stored) == ["D0001", "D0003", "D0005"]
    assert {x["status"] for x in stored} == {"active"}
    assert len(d["imported_ids"]) == 3
    assert set(d["imported_ids"]) == {x["_id"] for x in stored}

    # BR-06-36：**每个成功行**一条 list.add（before=null，after=条目）
    logs = await audit_rows("list.add")
    assert len(logs) == 3
    for log in logs:
        assert log["before"] is None
        assert log["after"]["_id"] in d["imported_ids"]
        assert log["after"]["status"] == "active"
    assert await audit_rows("list.remove") == []


async def test_import_partial_reuses_create_invariants(client):
    """导入必须复用 `create_entry` 的业务不变量，不得各写一套。

    这里一次性验证三条最容易"两套实现漂移"的地方：
    手机号脱敏（BR-06-21）、灰名单默认 30 天（BR-06-22）、唯一约束（BR-06-20）。
    第二轮的断言是"全部失败且原因都是已存在"——这正是唯一约束在导入路径上
    生效的证据（若导入另写一套校验，第二轮会照旧写入并把唯一索引撞成 500）。
    """
    rows = [
        ok_row("13900000001", entity_type="phone"),                       # 需脱敏
        ok_row("10.0.0.9", entity_type="ip", list_type="gray"),           # 需默认 30 天
        ok_row("DUP001"),                                                 # 这 2 行自身重复
        ok_row("DUP001"),
    ]
    first = data_of(await upload(client, csv_bytes(rows)))
    # 同批内的重复由唯一索引在第二行拦住（应用层"先查再插"看不出同批冲突）
    assert first["success"] == 3 and first["failed"] == 1
    assert first["rows"][0]["row"] == 5

    second = data_of(await upload(client, csv_bytes(rows)))
    assert second["success"] == 0 and second["failed"] == 4
    assert [e["row"] for e in second["rows"]] == [2, 3, 4, 5]
    assert all("已存在" in e["reason"] for e in second["rows"])

    doc = await db.get_db()[COLL].find_one({"entity_type": "phone"})
    assert doc["entity_value"] == "139****0001", "导入也必须脱敏后再入库"

    gray = await db.get_db()[COLL].find_one({"list_type": "gray"})
    delta = gray["expire_at"] - now_ms()
    assert 29 * DAY_MS < delta <= 30 * DAY_MS + 60_000, "灰名单导入行应走 30 天默认有效期"


async def test_import_atomic_prevalidates_before_writing(client):
    """atomic 的"先全量校验"必须真的在写库前发生。

    判据不只是"最终 0 条"：`success` 必须是 0（若先写后回滚，success 会算错），
    且**错误明细包含全部非法行**（否则用户改一轮导一轮）。
    """
    rows = [
        ok_row("P0001"),
        ok_row("P0002", entity_type="imei"),                    # 实体类型非法
        ok_row("P0003"),
        ok_row("P0004", expire_at="2026-01-01"),                # expire_at 非整数
        ok_row("P0005", list_type="purple"),                    # 名单类型非法
    ]
    r = await upload(client, csv_bytes(rows), mode="atomic")
    assert r.status_code == 200, r.text
    d = data_of(r)
    assert d["mode"] == "atomic"
    assert d["total"] == 5 and d["success"] == 0 and d["failed"] == 5
    assert [e["row"] for e in d["rows"]] == [3, 5, 6]
    assert "实体类型" in d["rows"][0]["reason"]
    assert "毫秒时间戳" in d["rows"][1]["reason"]
    assert "名单类型" in d["rows"][2]["reason"]
    assert d["imported_ids"] == []

    assert await db.get_db()[COLL].count_documents({}) == 0, "atomic 模式不得写入任何一行"
    assert await audit_rows("list.add") == []


async def test_import_atomic_writes_nothing_when_any_row_invalid(client):
    """⑦ atomic：任一行非法 -> 整批不写，但**错误明细返回全部错误行**。"""
    rows = [
        ok_row("A0001"),
        ok_row("A0002", entity_type="imei"),   # 非法
        ok_row("A0003"),
        ok_row("A0004", list_type="purple"),   # 非法
    ]
    r = await upload(client, csv_bytes(rows), mode="atomic")
    assert r.status_code == 200, r.text
    d = data_of(r)
    assert d["mode"] == "atomic"
    assert d["total"] == 4 and d["success"] == 0 and d["failed"] == 4
    assert [e["row"] for e in d["rows"]] == [3, 5]
    assert d["imported_ids"] == []

    assert await db.get_db()[COLL].count_documents({}) == 0, "atomic 模式不得写入任何一行"
    assert await audit_rows("list.add") == []


async def test_import_atomic_writes_all_when_every_row_valid(client):
    """atomic 的正例：全合法时行为与 partial 一致（否则"整批回滚"会被误当成"从不导入"）。"""
    rows = [ok_row("B0001"), ok_row("B0002")]
    d = data_of(await upload(client, csv_bytes(rows), mode="atomic"))
    assert d["success"] == 2 and d["failed"] == 0 and d["rows"] == []
    assert await db.get_db()[COLL].count_documents({}) == 2


async def test_import_bad_header_returns_400(client):
    """⑧ 表头不匹配 / 空文件 -> 400 CFG-4012。"""
    bad = csv_bytes([ok_row("C0001")], header=["listType", "entityType",
                                               "entityValue", "reason", "expireAt"])
    r = await upload(client, bad)
    assert r.status_code == 400, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4012"
    assert "表头" in b["message"]

    empty = await upload(client, b"")
    assert empty.status_code == 400 and body_of(empty)["code"] == "CFG-4012"

    only_header = await upload(client, csv_bytes([]))
    d = data_of(only_header)
    assert d["total"] == 0 and d["success"] == 0 and d["failed"] == 0

    assert await db.get_db()[COLL].count_documents({}) == 0


async def test_import_accepts_gbk_and_bom_encoding(client):
    """编码兼容：GBK（Windows 中文 Excel 默认）与带 BOM 的 UTF-8 都必须能导。

    BOM 那条尤其重要：若解码顺序把 `utf-8` 排在 `utf-8-sig` 之前，
    Excel 另存为 UTF-8 CSV 的文件第一个列名会带上 `\\ufeff`，
    表现为"用官方模板填了却说表头不匹配"。
    """
    text = "list_type,entity_type,entity_value,reason,expire_at\r\n" \
           "black,device,E0001,GBK 编码导入,\r\n"
    r = await upload(client, text.encode("gbk"))
    assert r.status_code == 200, r.text
    assert data_of(r)["success"] == 1

    bom = "\ufefflist_type,entity_type,entity_value,reason,expire_at\r\n" \
          "black,device,E0002,BOM 编码导入,\r\n"
    r2 = await upload(client, bom.encode("utf-8"))
    assert r2.status_code == 200, r2.text
    assert data_of(r2)["success"] == 1


async def test_import_row_limit_and_mode_validation(client):
    """BR-06-30：超 5000 行直接拒绝并提示分批；mode 非法 -> 422 表单参数错误。"""
    rows = [ok_row(f"F{i:05d}") for i in range(5001)]
    r = await upload(client, csv_bytes(rows))
    assert r.status_code == 400, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4012"
    assert "5000" in b["message"] and "分批" in b["message"]
    assert await db.get_db()[COLL].count_documents({}) == 0

    r2 = await upload(client, csv_bytes([ok_row("G0001")]), mode="all_or_nothing")
    assert r2.status_code == 422 and body_of(r2)["code"] == "COM-4001"


async def test_import_rejects_oversized_file_before_parsing(client):
    """模块 06 §3.2：文件 ≤5MB，超限 413。

    必须在**读文件之前**判大小：否则一个 200MB 的上传会先进内存再被拒，
    攻击者用一个请求就能把进程内存打满。
    """
    huge = b"list_type,entity_type,entity_value,reason,expire_at\r\n" + b"x" * (5 * 1024 * 1024 + 1)
    r = await upload(client, huge)
    assert r.status_code == 413, r.status_code
    assert body_of(r)["code"] == "COM-4000"
    assert "过大" in body_of(r)["message"]


async def test_import_form_defaults_fill_blank_cells(client):
    """表单级默认值：行内 `list_type` 留空时取表单 `list_type`；`force` 透传到 BR-06-20。"""
    await create(client, list_type="white", entity_type="user", entity_value="U000128")

    blank_type = ok_row("U000128", list_type="", entity_type="user")
    # 不加 force：跨名单冲突 -> 该行失败，明细里应带 G-04 的黑名单优先提示
    d = data_of(await upload(client, csv_bytes([blank_type]), list_type="black"))
    assert d["success"] == 0 and d["failed"] == 1
    assert "黑名单优先" in d["rows"][0]["reason"]

    # 带 force=true：同一行应当写成功（表单参数确实透传到了 create_entry）
    d2 = data_of(await upload(client, csv_bytes([blank_type]), list_type="black",
                              force="true"))
    assert d2["success"] == 1 and d2["failed"] == 0
    assert await db.get_db()[COLL].find_one({"list_type": "black"}) is not None


async def test_import_audit_failure_rolls_back_that_row_only(client, monkeypatch):
    """导入的每一行也要遵守 BR-06-36：审计留不下痕迹的那一行必须撤销。

    **只撤销那一行**：同一批里已成功且已留痕的行是真实且合规的，把它们
    一起算失败会让用户以为"一行都没进去"而重复导入。
    """
    original = list_service.audit
    calls: list[str] = []

    async def flaky(action, *args, **kwargs):
        """桩签名与 `audit()` 一致：第一个位置参数是 `action`（业务侧只传位置参数）。

        转发时 `action` 必须按**关键字**传回：`audit()` 的形参顺序是
        `(actor, actor_role, action, ...)`，若写成 `original(action, *args, **kwargs)`
        就会把 `action` 的值绑到 `actor` 形参上，而 kwargs 里恰好也有 `actor`
        ——直接 TypeError。这个坑正是靠本桩才暴露出来的。
        """
        calls.append(action)
        if len(calls) == 2:      # 只让第二行的审计失败
            from app.errors import AuditWriteFailedError

            raise AuditWriteFailedError(action, "simulated")
        return await original(*args, action=action, **kwargs)

    monkeypatch.setattr(list_service, "audit", flaky)

    rows = [ok_row("H0001"), ok_row("H0002"), ok_row("H0003")]
    d = data_of(await upload(client, csv_bytes(rows)))
    assert d["success"] == 2 and d["failed"] == 1
    assert "审计写入失败" in d["rows"][0]["reason"]
    # 被撤销的那一行**物理**消失，不留 removed 孤儿文档
    left = await db.get_db()[COLL].find({}).to_list(length=10)
    assert sorted(x["entity_value"] for x in left) == ["H0001", "H0003"]
    assert await audit_rows("list.add") != []


async def test_import_reviewer_denied_but_can_download_template(client):
    """⑪ 权限：reviewer 调 import -> 403 AUTH-4020；模板下载属只读，允许。"""
    r = await upload(client, csv_bytes([ok_row("I0001")]), headers=READER)
    assert r.status_code == 403, r.text
    assert body_of(r)["code"] == "AUTH-4020"
    assert await db.get_db()[COLL].count_documents({}) == 0

    t = await client.get(TEMPLATE_URL, headers=READER)
    assert t.status_code == 200, t.text


# ================================================================ 3 模板下载
async def test_template_download_matches_import_parser(client):
    """⑨ 模板是 CSV 附件，且**表头与导入解析完全一致**（否则 CFG-4012 的成因就成立）。"""
    r = await client.get(TEMPLATE_URL, headers=WRITER)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "list_import_template" in r.headers["content-disposition"]
    # 文件流不套统一响应包（与审计导出同理）：内容就是 CSV 本体
    assert not r.text.lstrip().startswith("{")

    rows = list(csv.reader(io.StringIO(r.text)))
    assert [c.strip() for c in rows[0]] == list(IMPORT_HEADER)
    assert rows[0] == ["list_type", "entity_type", "entity_value", "reason", "expire_at"]
    # 示例行 + 枚举注释行；注释行以 # 开头，用户删除后即为可导入文件
    assert rows[1][0] == "black" and rows[1][1] == "phone"
    assert rows[2][0].startswith("#")

    # **往返验证**：把模板的示例行当作导入文件提交，必须能被解析（表头一致）
    roundtrip = csv_bytes([rows[1]])
    d = data_of(await upload(client, roundtrip))
    assert d["total"] == 1 and d["success"] == 1, d
    stored = await db.get_db()[COLL].find_one({"entity_type": "phone"})
    assert stored["entity_value"] == "139****0001", "模板示例里的明文手机号同样要脱敏"


# ================================================================ 4 过期清理
async def test_cleanup_task_expires_only_overdue_entries(client):
    """⑩ V-06-17：跑一轮清理 -> 过期条目 status=expired，永久条目与未到期条目不受影响。

    "永久条目不受影响"不是附带断言而是**核心断言**：黑/白名单默认 `expire_at=null`，
    而 Mongo 在比较时把 null 当作小于任何数字——漏掉 `$ne: None` 的实现会把
    永久黑名单静默清空（风控最严重的失效形态且没有任何报错）。
    """
    from app.utils.timeutil import now_ms

    overdue = await create(client, entity_value="EXPIRED01")
    future = await create(client, entity_value="FUTURE01",
                          expire_at=now_ms() + 7 * DAY_MS)
    permanent = await create(client, entity_value="PERMANENT01")   # 黑名单默认永久
    # 把 overdue 改成"已到期"：`expire_at` 在接口层只校验类型（业务上"补一条
    # 已过期记录"没有意义，只可能是历史数据清理滞后），因此这里直接改库构造。
    await db.get_db()[COLL].update_one(
        {"_id": overdue}, {"$set": {"expire_at": now_ms() - 60_000}}
    )

    result = await list_cleanup_task.run_cleanup_once()
    assert result["error"] is None
    assert result["expired"] == 1, f"只应清理 1 条，实际 {result}"

    assert (await read_entry(overdue))["status"] == "expired"
    assert (await read_entry(future))["status"] == "active"
    assert (await read_entry(permanent))["status"] == "active", \
        "永久条目（expire_at=null）绝不能被清理任务碰到"

    # 幂等：再跑一轮没有可清理的，状态不变
    again = await list_cleanup_task.run_cleanup_once()
    assert again["expired"] == 0
    assert (await read_entry(overdue))["status"] == "expired"

    # 已 expired 的条目不再计入三 tab 的 active 计数（§3.2）
    listed = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    assert data_of(listed)["counts"]["black"] == 2


async def test_cleanup_task_does_not_override_concurrent_removal(client):
    """并发正确性：已被用户移除的条目，清理任务不得覆盖成 expired。

    覆盖会抹掉 `removed_by` 留痕，审计上表现为"没人删过，它自己过期了"。
    """
    from app.utils.timeutil import now_ms

    entry_id = await create(client, entity_value="RACE0001")
    await client.delete(f"{LIST_URL}/{entry_id}", headers=WRITER)
    doc = await read_entry(entry_id)
    assert doc["status"] == "removed"

    # 让它同时满足"过期"条件，再跑清理
    await db.get_db()[COLL].update_one(
        {"_id": entry_id},
        {"$set": {"expire_at": now_ms() - 60_000, "status": "removed"}},
    )
    result = await list_cleanup_task.run_cleanup_once()
    assert result["expired"] == 0
    final = await read_entry(entry_id)
    assert final["status"] == "removed" and final["removed_by"] == "strategy01"


async def test_cleanup_task_failure_is_swallowed_and_retried_later(client, monkeypatch):
    """失败只记日志、下一轮重试：定时任务没有调用方，抛异常等于此后再也不清理。"""

    async def boom(self, now, limit):
        raise PyMongoError("simulated mongo failure")

    monkeypatch.setattr(list_service.ListRepo, "mark_expired", boom)
    result = await list_cleanup_task.run_cleanup_once()
    assert result["expired"] == 0
    assert result["error"] and "simulated mongo failure" in result["error"]


async def test_cleanup_scheduler_start_stop_is_idempotent(client):
    """`start_cleanup` / `stop_cleanup` 重复调用必须安全（不得起两份并发清理）。"""
    await create(client, entity_value="SCHED0001")

    sched = list_cleanup_task.get_cleanup_scheduler()
    await list_cleanup_task.start_cleanup()
    await list_cleanup_task.start_cleanup()      # 重复启动：幂等
    import asyncio

    # 让后台协程把"立即执行的第一轮"跑完
    for _ in range(50):
        if sched.stats["runs"]:
            break
        await asyncio.sleep(0.05)
    assert sched.stats["runs"] >= 1, "启动后应立即跑一轮（覆盖停机期间到期的条目）"

    await list_cleanup_task.stop_cleanup()
    await list_cleanup_task.stop_cleanup()       # 重复停止：安全
    assert sched._task is None

    # 停机期间到期的条目在下次启动时被清掉（第一轮立即执行的意义）
    doc = await db.get_db()[COLL].find_one({"entity_value": "SCHED0001"})
    await db.get_db()[COLL].update_one(
        {"_id": doc["_id"]}, {"$set": {"expire_at": now_ms() - 1000}}
    )
    await list_cleanup_task.start_cleanup()
    for _ in range(100):
        if (await read_entry(doc["_id"]))["status"] == "expired":
            break
        await asyncio.sleep(0.05)
    await list_cleanup_task.stop_cleanup()
    assert (await read_entry(doc["_id"]))["status"] == "expired"
