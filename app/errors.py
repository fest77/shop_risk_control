# -*- coding: utf-8 -*-
"""统一响应契约、错误码表与异常处理（模块 00 §3.1 / §4.4 / §5）。

## 响应契约（全项目强制）

    成功：{ "ok": true,  "code": "OK", "message": "...", "trace_id": "tr_...", "data": {...} }
    失败：{ "ok": false, "code": "COM-4001", "message": "参数校验失败", "trace_id": "tr_...", "data": {...} }

**关于 `ok` 与 `code` 并存**：模块 00 §3.1/§V-00-02 要求"响应都含 `ok`"，
而模块 07 的接口章节又明确写了响应包是 `{code, message, trace_id, data}`
（并注明"模块 00"）。两份文档的字面描述不同，但**可同时满足**：保留扁平
`code/message`（被模块 07 与全部既有测试依赖），并**追加**一个由 `code`
派生的布尔 `ok`。这样 `ok` 是唯一成功判据（BR-00-21 的前端封装依赖它），
而 `code` 供错误定位与文案映射，不存在"两处状态可能打架"的问题——`ok`
恒等于 `code == "OK"`，由同一个函数生成，不可能不一致。

## 错误码体系（BR-00-12 / 13 / 14 / 15）

`模块前缀-4位数字`，4xxx 客户端错误、5xxx 服务端或依赖错误。前缀分配见
`00_模块划分与边界` §6「一个模块一个前缀」（该文档记录了从"按模块组分配"
改为"一模块一前缀"的修订原因：`RUL-4001`、`CAS-4001` 曾各自同号不同义）。

**ER-02**：引用其他模块的错误码时必须**保持原前缀**，不得改写成自己的。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_trace_id
from app.utils.ids import new_trace_id

log = logging.getLogger("shop_risk_control.errors")

# ============================================================
# 模块前缀分配（00_模块划分与边界 §6）
# ============================================================
MODULE_PREFIXES: dict[str, str] = {
    "00": "COM",     # 公共基础
    "01": "AUTH",    # 登录与权限鉴权
    "02": "DASH",    # 态势大盘
    "03": "EVT",     # 事件接入网关与模拟器
    "04": "FEA",     # 特征计算
    "05": "RUL",     # 规则决策引擎
    "06": "CFG",     # 规则与名单配置管理
    "07": "CASE",    # 案件审核工作台
    "08": "DSP",     # 案件处置与业务联动
    "09": "GRP",     # 画像与关联图谱
    "10": "SIM",     # 仿真测试
    "11": "MET",     # 监控指标
    "12": "AUD",     # 审计日志
    "13": "SYS",     # 系统设置
}

# ============================================================
# 模块 00 自有错误码（COM-*）
# 结构：code -> (HTTP 状态码或 None, 面向用户的中文短句)
# `None` 表示不在 HTTP 链路上（如启动期配置缺失，进程直接退出）
# ============================================================
COM_CODES: dict[str, tuple[Optional[int], str]] = {
    "COM-4000": (400, "请求格式错误"),
    "COM-4001": (422, "参数校验失败"),
    "COM-4004": (404, "请求的资源不存在"),
    "COM-4005": (405, "不支持的请求方法"),
    "COM-4290": (429, "操作过于频繁，请稍后再试"),
    "COM-5000": (500, "服务内部错误，请稍后重试"),
    "COM-5001": (503, "数据库连接失败"),
    "COM-5002": (None, "配置缺失，服务无法启动"),
    "COM-5003": (500, "页面资源缺失"),
}

# 请求体原始片段落日志时的截断长度（COM-4000 的处理要求）
RAW_BODY_LOG_LIMIT = 200

# ============================================================
# 模块 01 自有错误码（AUTH-*）
# ------------------------------------------------------------
# **取值以 `01_登录与权限鉴权.md` §5 的后半张表为准**：该 Spec 的 §5 里有两张
# 表，前半张是早期草稿，其中 `AUTH-4005` 与 `AUTH-4010` 都写"账号已停用"、
# `AUTH-4006` 与 `AUTH-4020` 都写"角色越权"，同码两义违反 BR-00-13。
# 后半张与 §3.5（令牌契约）、BR-01-05（锁定）、BR-01-10（改密失效）、
# §8（密码复杂度）逐条自洽，故采用后半张，前半张作废（见 15_决策记录 D26）。
# ============================================================
AUTH_CODES: dict[str, tuple[Optional[int], str]] = {
    "AUTH-4001": (401, "账号或密码错误"),
    "AUTH-4002": (401, "请先登录"),
    "AUTH-4003": (401, "登录凭证无效，请重新登录"),
    "AUTH-4004": (401, "登录已失效，请重新登录"),
    "AUTH-4005": (429, "失败次数过多，请稍后再试"),
    "AUTH-4006": (422, "新密码不符合要求"),
    "AUTH-4007": (422, "原密码不正确"),
    "AUTH-4008": (401, "密码已修改，请重新登录"),
    "AUTH-4010": (403, "账号已停用，请联系管理员"),
    "AUTH-4020": (403, "当前角色无权执行该操作"),
    "AUTH-5001": (503, "鉴权服务暂时不可用"),
    "AUTH-5002": (None, "JWT 签名密钥缺失或过短，服务无法启动"),
}


# ============================================================
# 模块 12 自有错误码（AUD-*）
# ------------------------------------------------------------
# **只登记"HTTP 错误响应"**。模块 12 §5 的表里还混着两个"校验结论码"
# （`AUD-5003` 发现篡改、`AUD-5004` 校验超时），它们对应的 HTTP 状态是 **200**
# （校验接口本身成功了，只是结论是不一致/未完成）。若把它们放进本表，
# 就会与 BR-00-12「5xxx = 服务端/依赖错误」的段位语义冲突，`test_errors.py`
# 也会（正确地）报错。因此它们作为**结论码**出现在响应 `data` 与日志中，
# 见下方的 `VERIFY_NOTICE`（决策 D32）。
# ============================================================
AUD_CODES: dict[str, tuple[Optional[int], str]] = {
    "AUD-4001": (422, "校验参数不合法"),
    "AUD-4002": (422, "导出数据量过大，请缩小时间范围"),
    "AUD-4003": (422, "时间跨度不能超过 30 天"),
    "AUD-5001": (503, "审计服务不可用，操作已中止"),
    "AUD-5002": (None, "审计写入失败（普通操作，仅告警不阻断）"),
    "AUD-5005": (None, "检测到多进程并发写风险（启动告警）"),
}

# 校验结论码（HTTP 200，出现在 data 与日志中，不构成 HTTP 错误）
VERIFY_NOTICE: dict[str, str] = {
    "AUD-5003": "哈希链校验发现不一致",
    "AUD-5004": "校验过程超时，可分段继续",
}

# ============================================================
# 模块 11 自有错误码（MET-*）
# ------------------------------------------------------------
# 与模块 12 同样的划分原则：**本表只放真正的 HTTP 错误响应**。
# MET-5002（桶写入失败）/ MET-5003（直方图缺失）/ MET-5005（订阅队列溢出）/
# MET-5007（桶与明细复核不一致）都是"后台/连接内"的情形，没有对应的 HTTP 错误，
# 放进来会破坏 BR-00-12 的段位语义（见 MET_NOTICE）。
#
# ⚠️ Spec 内部矛盾一处：§5 把 `MET-4003`（top 越界）写成"400 拒绝"却又在提示里写
# "已按 10 返回"（那是 200 的行为）。这里**以 HTTP 状态列为准**（400 拒绝），
# 因为"静默夹取参数"会让调用方以为自己的 top 生效了。
# ============================================================
MET_CODES: dict[str, tuple[Optional[int], str]] = {
    "MET-4001": (400, "时间范围参数不合法，可选 1h/24h/7d/30d"),
    "MET-4002": (400, "时间粒度与范围不匹配"),
    "MET-4003": (400, "Top 取值需在 1~50"),
    "MET-4004": (400, "分布维度不合法"),
    "MET-4005": (400, "rollup 时间区间非法（from_ts 不能大于 to_ts）"),
    "MET-5001": (503, "指标数据暂不可用，且无可用快照"),
    "MET-5004": (503, "实时事件流连接数已满，请稍后重试"),
    "MET-5006": (500, "指标补算执行失败"),
}

# 非 HTTP 的错误/告警码（后台任务、SSE 连接内、日检告警）
MET_NOTICE: dict[str, str] = {
    "MET-5002": "指标桶写入失败（不阻断决策，由 rollup 修复）",
    "MET-5003": "延迟直方图缺失，p95 返回 null",
    "MET-5005": "SSE 订阅者队列溢出，已丢弃最旧事件",
    "MET-5007": "桶内计数与 decisions 复核不一致（日检告警）",
}

# ============================================================
# 模块 03 自有错误码（EVT-*）
# ------------------------------------------------------------
# **一处改号（决策 D43 一并登记）**：Spec §5 把"模拟器已在运行"定为 `EVT-5006`
# 却配 HTTP **409**。按 BR-00-12「4xxx = 客户端错误」，409 冲突属客户端错误，
# 5xxx 段位与状态码自相矛盾（`test_errors.py` 会正确地报错）。它在 Spec 里是
# **空号** `EVT-4008`，故改号为 `EVT-4008` 并保持 409 语义不变。
# ============================================================
EVT_CODES: dict[str, tuple[Optional[int], str]] = {
    "EVT-4001": (400, "请求体格式非法"),
    "EVT-4002": (400, "缺少必填字段：user_id"),
    "EVT-4003": (400, "不支持的事件类型"),
    "EVT-4004": (422, "按事件类型的必填字段缺失"),
    "EVT-4005": (400, "字段格式非法"),
    "EVT-4006": (400, "scene_extra 含不属于该事件类型的键"),
    "EVT-4007": (400, "amount 与 scene_extra 的金额字段不一致"),
    "EVT-4008": (409, "模拟器已在运行"),
    "EVT-4009": (409, "事件编号已存在且内容不同，疑似编号复用"),
    "EVT-4010": (400, "单批事件数超上限（最多 500 条）"),
    "EVT-4404": (404, "事件已接收，数据写入中，请稍后重试"),
    "EVT-5005": (500, "模拟器启动失败"),
}

# 降级结论码（HTTP 200：请求本身成功，只是决策被 fail-closed 降级为 review）
EVT_NOTICE: dict[str, str] = {
    "EVT-5001": "决策链路超时（>200ms），已降级 review（stage=timeout）",
    "EVT-5002": "04 特征计算不可用，已降级 review（stage=feature）",
    "EVT-5003": "05 规则决策不可用，已降级 review（stage=rule）",
    "EVT-5004": "risk_events 异步落库失败（决策照常返回，进重试队列）",
}

# ============================================================
# 模块 04 自有错误码（FEA-*）
# ------------------------------------------------------------
# 与模块 12/11 同样的划分原则：**本表只放真正的 HTTP 错误响应**。
# `FEA-5001`（计算异常降级）/ `FEA-5002`（快照落库失败）/ `FEA-5003`（窗口截断）/
# `FEA-5004`（乱序事件）四个码在 Spec §5 里写的 HTTP 状态是「—」，
# 它们的实际表现是**后台告警或快照上的提示**，请求仍是 200（降级已经发生、
# 决策照常返回）。若放进本表，就会与 BR-00-12「5xxx = 服务端/依赖错误」
# 的段位语义冲突，`test_errors.py` 也会（正确地）报错——因此它们进 `FEA_NOTICE`。
# ============================================================
FEA_CODES: dict[str, tuple[Optional[int], str]] = {
    "FEA-4001": (422, "事件编号不合法"),
    "FEA-4004": (404, "未找到该事件的特征快照"),
}

# 降级/告警结论码（HTTP 200，出现在快照与日志里，不构成 HTTP 错误）
FEA_NOTICE: dict[str, str] = {
    "FEA-5001": "特征计算异常，已降级并返回可用子集",
    "FEA-5002": "快照落库失败（后台重试）",
    "FEA-5003": "窗口数据超容量被截断，特征可能偏低",
    "FEA-5004": "检测到乱序事件",
}


# ============================================================
# 模块 05 自有错误码（RUL-*）
# ------------------------------------------------------------
# 划分原则与模块 04/11/12/09 完全一致：**`RUL_CODES` 只放真正的 HTTP 错误响应**。
# Spec §5 表里的 `RUL-5003`（单条规则求值异常）与 `RUL-5004`（决策耗时超阈值）
# 的 HTTP 列写的是 **200**——请求本身成功了，只是有一条规则被跳过 / 这次跑得慢。
# 若把它们放进本表，就会与 BR-00-12「5xxx = 服务端/依赖错误（会产生 5xx 响应）」
# 的段位语义冲突，`test_errors.py` 也会（正确地）报错，因此它们进 `RUL_NOTICE`。
#
# **`RUL-4002` 的特殊之处（必须知情）**：它的「处理策略」是"拒绝该规则，其余继续"，
# 而「用户可见提示」写的是"06 保存时提示「条件树第 N 个节点不合法」"。也就是说
# 这条 422 **不在事件决策链路上抛**——决策链路上遇到非法条件树时，正确的处置是
# 按 BR-05-13 把**那一条规则**记为求值失败（`RUL-5003`），其余规则照常累加分值；
# 若让整次决策 422，一条坏规则就能让所有事件拿不到决策（等于把一条配置错误
# 升级成全站故障）。真正的 422 出口是 `validate_tree()`，由**模块 06 在保存规则时**
# 调用（Spec §1「不做条件树的结构校验实现——`validate_tree()` 由本模块提供，06 只调用」）。
#
# **`RUL-5001/5002` 的 503 与决策 D5 的关系**：`decide()` 内部**永不把依赖故障
# 变成异常**——它会产出一个 `decision=review`、`degraded=true` 的决策块（D5：
# 降级也必须落库建案，否则这些请求无人处理）。503 只是 `/engine/evaluate` 这个
# 调试/仿真接口对外的表达方式，且**响应 `data` 里仍然带着那个降级决策块**
# （Spec §3.1「503 引擎依赖不可用（返回 degraded=true 与人工审核建议，不返回 pass）」），
# 因此两个要求同时成立。
# ============================================================
RUL_CODES: dict[str, tuple[Optional[int], str]] = {
    "RUL-4001": (400, "事件参数不合法：缺少必填字段 event_type / user_id"),
    "RUL-4002": (422, "条件树结构非法"),
    "RUL-5001": (503, "名单服务不可用，已转人工审核"),
    "RUL-5002": (503, "规则服务不可用，已转人工审核"),
}

# 降级/告警结论码（HTTP 200，出现在 `data.warnings` 与后台日志里）
RUL_NOTICE: dict[str, str] = {
    "RUL-5003": "单条规则求值异常（已跳过该规则、不计分，其余规则照常）",
    "RUL-5004": "决策耗时超过阈值 200ms（已记 slow 告警，结果照常返回）",
}


def rul_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `RUL_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = RUL_CODES.get(code, (400, "规则决策请求失败"))
    return AppError(code, message or default, status or 400, data)


# ---- 模块 05（RUL 前缀）的异常类见本文件末尾「模块 05 异常」一节 ----
# 为什么放在后面：它们必须 `class X(AppError)`，而 `AppError` 在文件后半部分
# 定义。把类提到基类之前会直接 `NameError`（模块 05 落地时踩过一次）。


# ============================================================
# 模块 09 自有错误码（GRP-*）
# ------------------------------------------------------------
# 与模块 11/12/04 同样的划分原则：**`GRP_CODES` 只放真正的 HTTP 错误响应**。
# Spec §5 表里的 `GRP-5002`（图查询超时，HTTP **200**，返回部分结果）与
# `GRP-5004`（节点属性批量补齐部分失败，HTTP **200**）都不是错误响应——请求
# 本身成功了，只是结果不完整/有缺口；`GRP-5003`（建边失败）更是**没有 HTTP**：
# 它在决策返回之后的异步旁路上发生，只入重试队列 + 告警。
# 若把这四个放进 `GRP_CODES`，就会与 BR-00-12「5xxx = 服务端/依赖错误（会产生
# 5xx 响应）」的段位语义冲突，`test_errors.py` 也会（正确地）报错——因此它们
# 进 `GRP_NOTICE`（与 `MET_NOTICE`/`FEA_NOTICE`/`EVT_NOTICE` 完全同形，
# 即 `code -> 面向用户/日志的中文短句`，**不参与 HTTP 状态声明**，
# 也因此不参与 `test_errors.py` 的「码段位 ↔ HTTP 状态」一致性校验）。
#
# 本模块**不参与放行判定**，故不涉及 fail-closed 的决策侧；但图不完整必须
# 显式告知（BR-09-18）——这是"证据链可信"的前提。
# ============================================================
GRP_CODES: dict[str, tuple[Optional[int], str]] = {
    "GRP-4001": (400, "图谱查询最多支持 2 跳"),
    "GRP-4002": (422, "不支持的实体类型"),
    "GRP-4004": (404, "未找到该用户/实体"),
    "GRP-5001": (503, "画像服务暂时不可用"),
}

# 降级/告警结论码（HTTP 200 或无 HTTP，出现在响应 data、后台日志与重试队列里）
GRP_NOTICE: dict[str, str] = {
    "GRP-5002": "图查询超时（>3s），已返回部分结果（truncated=true）",
    "GRP-5003": "建边失败（后台），已入重试队列 + 告警，不影响决策",
    "GRP-5004": "节点属性批量补齐部分失败，缺失属性显示 —",
}


def grp_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `GRP_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = GRP_CODES.get(code, (400, "画像与图谱请求失败"))
    return AppError(code, message or default, status or 400, data)


# ============================================================
# 模块 08 自有错误码（DSP-*）
# ------------------------------------------------------------
# **前缀归属**：按 `00_模块划分与边界` §6 与 D18「一个模块一个前缀」，
# `08 → DSP`（`07 → CASE`）。`MODULE_PREFIXES` 里早已如此登记，
# `tests/test_errors.py` 也直接断言了这一条；本模块**不得**改用 `CASE-`
# ——那会与模块 07（案件审核工作台）的码同前缀同号不同义（BR-00-13）。
#
# 划分原则与 04/05/09/11/12 完全一致：**`DSP_CODES` 只放真正的 HTTP 错误响应**。
# Spec §5 表里的 `DSP-5003`（业务系统同步失败）与 `DSP-5004`（审计哈希链写入失败）
# 的 HTTP 列写的是 **200** —— 处置本身已经生效，只是某个**旁路**副作用待重试
# （BR-08-31 / BR-08-32）。若把它们放进本表，就与 BR-00-12「5xxx = 服务端/依赖
# 错误（会产生 5xx 响应）」的段位语义冲突，`test_errors.py` 也会（正确地）报错；
# 因此它们进 `DSP_NOTICE`，出现在响应 `data.notice_code` 与日志里。
# ============================================================
DSP_CODES: dict[str, tuple[Optional[int], str]] = {
    "DSP-4001": (400, "请选择处理结论、至少一项处置动作并填写处置原因备注"),
    "DSP-4002": (422, "「确认为正常」不可与拦截类动作同时提交"),
    "DSP-4003": (409, "案件当前状态不允许该操作"),
    "DSP-4004": (409, "该案件已完成处置"),
    "DSP-4005": (409, "案件由他人认领，不能代为处置"),
    "DSP-4006": (422, "确认已过期或参数已变更，请重新确认"),
    "DSP-4040": (404, "案件不存在或已归档"),
    "DSP-5001": (500, "处置失败：数据库不可用，本次未生效，请重试"),
    "DSP-5002": (503, "处置未生效：黑名单写入失败，已回滚本次名单变更"),
    "DSP-5005": (500, "处置失败：流水未落库，本次未生效"),
}

# 降级结论码（HTTP 200：处置已生效，只是旁路副作用待重试）
DSP_NOTICE: dict[str, str] = {
    "DSP-5003": "业务系统同步失败（模拟业务系统未对接），处置已生效并已加入重试队列",
    "DSP-5004": "处置已生效，审计落库待重试（audit_pending）",
}


def dsp_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `DSP_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = DSP_CODES.get(code, (400, "案件处置请求失败"))
    return AppError(code, message or default, status or 400, data)


# ============================================================
# 模块 07 自有错误码（CASE-*）
# ------------------------------------------------------------
# **前缀归属**：`00_模块划分与边界` §6 / D18 把 `07 → CASE` 登记在
# `MODULE_PREFIXES` 里，`tests/test_errors.py` 直接断言了这一条。此前全项目
# 只有 08 借用了 `CASE-` 的**语义**（`Spec 07` 的码表）而码本身归 08 的
# `DSP_*`，`CASE-*` 从未被实现过——本文件是它第一次真正落地。
#
# 与 04/05/09/11/12 的划分原则一致：**只放真正的 HTTP 错误响应**。
# `Spec 07 §5.1` 的 `CASE-5001`（详情聚合超时）/ `CASE-5002`（图谱不可用）/
# `CASE-5003`（画像不可用）/ `CASE-5004`（判定数据缺失）**都不进本表**：
# 它们的 HTTP 列写的是 **200 + 分区降级**（`degraded_parts`），若放进本表就与
# BR-00-12「5xxx = 会产生 5xx 响应」冲突，`tests/test_errors.py` 也会正确地报错。
# 换句话说：`CASE-5001~5004` 在本模块的**表达方式**是 `degraded_parts`，
# 不是错误码——这与 08 把 `DSP-5003/5004` 放进 `DSP_NOTICE` 是同一条裁定。
# ============================================================
CASE_CODES: dict[str, tuple[Optional[int], str]] = {
    # `CASE-4001` 由接口层在"查无此案"时抛出（服务层复用的 `CaseNotFoundError`
    # 带的是 08 的 `DSP-4040`，那是处置侧的码）。放在本表而不是让接口层写死
    # `AppError("CASE-4001", ..., 404)`：状态码只有一处真源，否则迟早出现
    # "表里写 404、代码里传 400"——本表的这条码正是被那个 bug 逼出来的。
    "CASE-4001": (404, "案件不存在或已归档"),
    "CASE-4004": (400, "查询条件不合法"),
    # `CASE-5005`（列表接口 5xx：保留旧列表 + 错误条 + 重试）。它**不在**
    # Spec 07 §5.1 的"降级"那四条（5001~5004 都是 200 + 分区降级）里，
    # 而是列表接口自己的失败态；段位与 HTTP 都是 5xx，符合 BR-00-12。
    "CASE-5005": (503, "案件列表加载失败，请重试"),
}


def case_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `CASE_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = CASE_CODES.get(code, (400, "案件查询请求失败"))
    return AppError(code, message or default, status or 400, data)


def auth_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `AUTH_CODES` 表构造 `AppError`。

    统一走这张表而不是各处手写 HTTP 状态：状态码与错误码一旦由两处分别维护，
    迟早会出现"表里写 401、代码里传 403"的分歧，而这类分歧在联调时表现为
    前端按 401 清 token、后端其实是 403，排查成本极高。
    """
    status, default_message = AUTH_CODES.get(code, (401, "登录状态异常"))
    return AppError(code, message or default_message, status or 401, data)


# ============================================================
# 异常族
# ============================================================
class AppError(Exception):
    """所有**可预期**错误的基类。

    可预期的错误必须带错误码与用户可读文案；技术细节放 `data` 与日志，
    绝不回显堆栈（BR-00-14）。
    """

    def __init__(self, code: str, message: str, http_status: int, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data


class BadRequestError(AppError):
    """COM-4000 请求体非法（不是合法 JSON、字段类型完全不匹配等）。"""

    def __init__(self, message: str = "请求格式错误", data: Any = None):
        super().__init__("COM-4000", message, 400, data)


class NotFoundError(AppError):
    """COM-4004 资源不存在。"""

    def __init__(self, message: str = "请求的资源不存在"):
        super().__init__("COM-4004", message, 404)


class RateLimitedError(AppError):
    """COM-4290 触发限流。"""

    def __init__(self, retry_after_sec: int = 60):
        super().__init__(
            "COM-4290",
            "操作过于频繁，请稍后再试",
            429,
            {"retry_after_sec": retry_after_sec},
        )


class MongoUnavailableError(AppError):
    """COM-5001 数据库不可用。

    各模块收到它时必须遵守各自的 fail-closed 规则：**决策链路绝不因
    "查不到黑名单"而放行**（宁可转人工审核）。
    """

    def __init__(self, detail: str = ""):
        super().__init__(
            "COM-5001",
            "数据库连接失败，请稍后重试",
            503,
            {"detail": detail} if detail else None,
        )


class StaticAssetMissingError(AppError):
    """COM-5003 静态资源缺失（如 index.html 未随包发布）。"""

    def __init__(self, path: str):
        super().__init__("COM-5003", "页面资源缺失，请检查部署产物", 500, {"path": path})


# ---- 模块 01（AUTH 前缀）----
# 说明：令牌类错误（AUTH-4002/4003/4004）**不在这里定义类**，而是由
# `app/security/jwt.py` 的 TokenError 子类携带 code，再由鉴权中间件经
# `auth_error()` 统一转换——避免同一语义在两层各有一套异常类而逐渐分叉。
class BadCredentialsError(AppError):
    """AUTH-4001 账号或密码错误（**不区分账号不存在**，BR-01-02）。"""

    def __init__(self, message: Optional[str] = None):
        super().__init__("AUTH-4001", message or AUTH_CODES["AUTH-4001"][1], 401)


class LoginLockedError(AppError):
    """AUTH-4005 登录失败次数过多被锁定（BR-01-05）。"""

    def __init__(self, remain_sec: int):
        super().__init__(
            "AUTH-4005",
            f"失败次数过多，请在 {max(1, remain_sec // 60)} 分钟后再试",
            429,
            {"retry_after_sec": remain_sec},
        )


class WeakPasswordError(AppError):
    """AUTH-4006 新密码不符合要求（长度 6~64）。"""

    def __init__(self, message: Optional[str] = None):
        super().__init__("AUTH-4006", message or "新密码需 6~64 位", 422)


class WrongOldPasswordError(AppError):
    """AUTH-4007 原密码不正确。"""

    def __init__(self):
        super().__init__("AUTH-4007", "原密码不正确", 422)


class PasswordChangedError(AppError):
    """AUTH-4008 密码已变更导致令牌失效（BR-01-10）。"""

    def __init__(self):
        super().__init__("AUTH-4008", "密码已修改，请重新登录", 401)


class AccountDisabledError(AppError):
    """AUTH-4010 账号已停用（BR-01-03：密码正确也拒绝）。"""

    def __init__(self):
        super().__init__("AUTH-4010", "账号已停用，请联系管理员", 403)


class PermissionDeniedError(AppError):
    """AUTH-4020 角色无权执行该操作（BR-01-14：后端必须独立校验）。"""

    def __init__(self, permission: str, role: str):
        super().__init__(
            "AUTH-4020",
            AUTH_CODES["AUTH-4020"][1],
            403,
            {"required_permission": permission, "role": role},
        )
        self.permission = permission
        self.role = role


class AuthDependencyError(AppError):
    """AUTH-5001 鉴权依赖不可用（用户库查询失败）。

    **fail-closed**：查不到用户**绝不放行**。这一条与风控决策的 fail-closed
    同源——"不确定"必须落到"拒绝"，否则数据库一挂，整个系统就等于没有鉴权。
    """

    def __init__(self, detail: str = ""):
        super().__init__(
            "AUTH-5001",
            AUTH_CODES["AUTH-5001"][1],
            503,
            {"detail": detail} if detail else None,
        )


# ---- 模块 12（AUD 前缀）----
class AuditWriteFailedError(AppError):
    """AUD-5001 紧操作的审计写入失败 -> **阻断业务操作**（BR-12-08）。

    这是全项目最重要的一条 fail-closed：规则/名单/处置一旦执行就改变了风控行为，
    若没有留痕，事后无法追溯与问责。**宁可不做，不可无痕地做**。
    """

    def __init__(self, action: str, detail: str = ""):
        super().__init__(
            "AUD-5001",
            "审计服务不可用，操作已中止",
            503,
            {"action": action, "detail": detail} if detail else {"action": action},
        )
        self.action = action


# ============================================================
# 模块 06 自有错误码（CFG-*）
# ------------------------------------------------------------
# `CFG-4031`（无策略配置权限）**已作废**：权限拒绝自模块 01 起由权限层统一给出
# `AUTH-4020`（决策 D28）。保留 `CFG-4032` 是因为它表达的是**业务规则**而不是权限——
# 「`source=auto` 的条目必须回处置模块留痕」换任何角色都不能在本页移除。
# ============================================================
CFG_CODES: dict[str, tuple[Optional[int], str]] = {
    # --- 规则（06-B）---
    "CFG-4001": (404, "规则不存在或已被删除"),
    "CFG-4002": (409, "规则编码已占用，请重试保存"),
    "CFG-4003": (400, "条件树结构非法"),
    "CFG-4004": (400, "命中分值必须在 0~100 之间"),
    "CFG-4005": (400, "系统内置规则不可删除，只能停用"),
    "CFG-4006": (409, "数据已被他人修改，请刷新后重试"),
    "CFG-4007": (400, "规则编码不可修改"),
    "CFG-4008": (400, "查询参数不合法"),
    "CFG-4009": (409, "名单条目冲突"),
    "CFG-4010": (400, "实体类型仅支持 user / phone / ip / device / address"),
    "CFG-4011": (404, "名单条目不存在或已被移除"),
    "CFG-4012": (400, "文件为空或表头不匹配，请使用下载的模板"),
    # `CFG-4013` 是 06-B 新增（Spec §5.1 未给该场景分配码，见 RuleSceneNotFoundError
    # 的说明）：`BR-06-07` 要求「scene_code 必须存在于 rule_scenes」，但 §5.1 的
    # 表里从 4012 直接跳到 4032，没有对应的码。复用通用 `COM-4001` 会把一条**业务
    # 规则**（归属场景必须是字典里的行）降级成"参数格式不对"，前端也无从映射文案。
    "CFG-4013": (400, "归属场景不存在，请选择规则场景字典中的场景"),
    "CFG-4032": (403, "该条目由案件处置自动写入，请到案件处置模块处理"),
    "CFG-5001": (503, "规则保存失败，请稍后重试"),
    "CFG-5002": (503, "名单写入失败，已进入降级状态"),
    "CFG-5003": (503, "操作未完成（审计写入失败），已回滚，请重试"),
}

# ---- 以下为模块 06（CFG 前缀）的错误码与异常 ----
# 归属说明：这些码属于「规则与名单配置管理」，此处先集中定义以便模块 00 的
# 错误码唯一性校验（BR-00-13）能覆盖到已落地的码；模块 06 实现时在本文件
# 对应分区内扩容。跨模块引用时**保持 CFG 前缀**（ER-02）。
class ListConflictError(AppError):
    """CFG-4009 名单条目冲突（唯一约束或异名单冲突）。"""

    def __init__(self, message: str):
        super().__init__("CFG-4009", message, 409)


class InvalidEntityTypeError(AppError):
    """CFG-4010 entity_type 非法。"""

    def __init__(self, entity_type: str):
        super().__init__(
            "CFG-4010",
            f"实体类型仅支持 user / phone / ip / device / address，收到：{entity_type}",
            400,
        )


class InvalidQueryParamError(AppError):
    """CFG-4008 分页或排序参数越界。"""

    def __init__(self, message: str):
        super().__init__("CFG-4008", message, 400)


class ListEntryNotFoundError(AppError):
    """CFG-4011 名单条目不存在（已移除或 id 不存在）。"""

    def __init__(self, entry_id: str):
        super().__init__("CFG-4011", CFG_CODES["CFG-4011"][1], 404, {"entry_id": entry_id})


class AutoSourceForbiddenError(AppError):
    """CFG-4032 试图移除 `source=auto` 的名单条目（BR-06-27）。

    **这是业务规则而非权限**：换任何角色都不能在本页移除——那些条目由案件处置
    自动写入，必须回处置模块操作才留得下痕迹。
    """

    def __init__(self, entry_id: str):
        super().__init__(
            "CFG-4032", CFG_CODES["CFG-4032"][1], 403,
            {"entry_id": entry_id, "hint": "请到「案件处置」模块处理该条目"},
        )


class ImportFormatError(AppError):
    """CFG-4012 导入文件为空、表头不匹配或编码无法解析（BR-06-29）。"""

    def __init__(self, message: Optional[str] = None):
        super().__init__("CFG-4012", message or CFG_CODES["CFG-4012"][1], 400)


class ListVersionConflictError(AppError):
    """CFG-4006 条目状态在操作过程中被他人改变（并发移除 / 已过期）。

    E07 没有 `version` 列，因此这里用**状态条件更新**表达乐观锁语义：更新语句带上
    `status=active` 前提，匹配 0 条即说明状态已变。必须报冲突而不是静默成功——
    否则会出现"页面提示移除成功、库里其实早就过期了"这种对不上账的情况。
    """

    def __init__(self, entry_id: str, detail: str = ""):
        message = (f"该条目状态已被改变（{detail}），请刷新后重试"
                   if detail else CFG_CODES["CFG-4006"][1])
        super().__init__("CFG-4006", message, 409,
                         {"entry_id": entry_id, "detail": detail} if detail else {"entry_id": entry_id})


class AuditRollbackError(AppError):
    """CFG-5003 审计写入失败导致业务写被回滚（BR-06-36）。

    与 `AUD-5001` 的分工：`AUD-5001` 是审计服务自身的错误码；本码是模块 06 **对外
    表达"这次操作因为留不下痕迹而没做"**，页面必须明确告知"已回滚、可重试"，
    否则用户会以为改成功了。
    """

    def __init__(self, action: str, detail: str = ""):
        super().__init__(
            "CFG-5003", CFG_CODES["CFG-5003"][1], 503,
            {"action": action, "detail": detail} if detail else {"action": action},
        )


class ListWriteFailedError(AppError):
    """CFG-5002 名单写入失败（fail-closed：置降级标记 + 不给宽松结果）。"""

    def __init__(self, detail: str):
        super().__init__(
            "CFG-5002",
            f"名单写入失败，已进入降级状态；新增黑名单暂未生效，请重试。（{detail}）",
            503,
        )


# ---- 模块 06-B（规则配置）的异常类 ----
# 与 `CFG_CODES` 一一对应。状态码不在类里手写：一律从表里取，避免"表里 400、
# 类里 409"这类同码两义（BR-00-13；`test_errors.py` 会按运行时实例化抓出来）。
class RuleNotFoundError(AppError):
    """CFG-4001 规则不存在（或已被删除标记屏蔽）。"""

    def __init__(self, rule_code: str):
        super().__init__(
            "CFG-4001", CFG_CODES["CFG-4001"][1], 404, {"rule_code": rule_code}
        )


class RuleCodeConflictError(AppError):
    """CFG-4002 规则编码重复（并发下重生成序号仍撞上唯一 `_id`）。

    `_id` 本身就是唯一索引，因此这条是"应用层算出的序号被别人抢先占用"的兜底。
    BR-06-01 要求"同场景内序号递增且不复用"，所以不能改用别的前缀绕开，
    只能重算一次序号（`RuleService` 会重试若干次）后如实报冲突。
    """

    def __init__(self, rule_code: str, tried: int = 1):
        super().__init__(
            "CFG-4002",
            f"规则编码 {rule_code} 已被占用（已重试 {tried} 次），请稍后重试保存",
            409,
            {"rule_code": rule_code, "tried": tried},
        )


class RuleConditionInvalidError(AppError):
    """CFG-4003 条件树结构非法——**转发 05 `validate_tree()` 的结论**。

    本模块**不定义任何结构校验规则**（Spec §1 / BR-06-17）：这里只是把
    `app/engine/condition.validate_tree()` 抛出的 `RUL-4002` 换成本模块对外的
    `CFG-4003`，并原样保留 `path` 与 `message`，供编辑器按 `path` 定位到节点行
    （§2.3「校验错误回显」）。

    ## 为什么必须换码而不是直接抛 `RUL-4002`

    `RUL-4002` 的语义是"**决策引擎**读不懂这棵树"，它出现的场合是规则保存
    （Spec 05 §5 的处理策略原文就写着"06 保存时提示"）；而 `06 §3.1` 给这个
    场合分配的对外码是 `CFG-4003`。前端按 `CFG-4003` 映射「条件树有 N 处问题」
    的文案，两个码混用会让这句话只在部分情况下出现。两个码都保留 `errors[]`，
    因此"谁校验的结论"这一事实不丢失。
    """

    def __init__(self, detail: str, node_path: str = "", errors: Any = None):
        super().__init__(
            "CFG-4003",
            f"条件树结构非法：{detail}" + (f"（位置：{node_path}）" if node_path else ""),
            400,
            {"errors": errors or [{"path": node_path, "message": detail}]},
        )


class RuleScoreOutOfRangeError(AppError):
    """CFG-4004 命中分值越界（BR-06-06：整数且 0 ≤ score ≤ 100）。

    单条规则**本身**不收 >100 的分；多条累加超 100 的截断归 05 的 BR-05-16，
    不在本模块（这是 Spec §2.2.3 对悬空点 G-03 的明确切分）。
    """

    def __init__(self, score: Any):
        super().__init__(
            "CFG-4004",
            f"命中分值必须在 0~100 之间的整数，收到：{score!r}",
            400,
            {"score": score},
        )


class SystemRuleNotDeletableError(AppError):
    """CFG-4005 系统内置规则被删除（BR-06-08：只能停用）。"""

    def __init__(self, rule_code: str):
        super().__init__(
            "CFG-4005",
            f"规则 {rule_code} 是系统内置规则，不可删除，只能停用",
            400,
            {"rule_code": rule_code},
        )


class RuleVersionConflictError(AppError):
    """CFG-4006 规则版本冲突（BR-06-05：`expected_version` 不匹配）。

    **不做后写覆盖**是这条规则的实质：两个策略师同时打开抽屉时，后保存者若
    静默覆盖，前者的改动会消失且无人知晓。`data.current_version` 供前端提示
    「该规则已被他人修改（当前 v{n}），请刷新后重试」（§5.1 原文）。
    """

    def __init__(self, rule_code: str, expected: int, current: int):
        super().__init__(
            "CFG-4006",
            f"该规则已被他人修改（当前 v{current}，你提交的是 v{expected}），请刷新后重试",
            409,
            {"rule_code": rule_code, "expected_version": expected,
             "current_version": current},
        )


class RuleImmutableFieldError(AppError):
    """CFG-4007 试图修改不可变字段（`_id` / `is_system` / `created_*` / `version`）。

    这些字段由服务端独占：`_id` 生成后不可改（BR-06-02）、`is_system` 与
    `created_*` 是种子与首写的产物（BR-06-37）、`version` 是乐观锁载体
    （BR-06-03，客户端只能**声明**期望值，不能直接写值）。
    """

    def __init__(self, fields: list[str]):
        super().__init__(
            "CFG-4007",
            f"以下字段不可修改或不可由客户端指定：{'、'.join(fields)}",
            400,
            {"immutable_fields": fields},
        )


class RuleSceneNotFoundError(AppError):
    """CFG-4013 归属场景不存在（BR-06-07：必须是 `rule_scenes` 里的一行）。

    **这是 06-B 新增的码**（Spec §5.1 未分配，取证见 `CFG_CODES` 的注释）。
    校验方式是"查 E06 字典"而不是"比对代码里的场景枚举"——D24 明确场景必须
    数据驱动，`common`（D10）也因此不需要任何特判：它本来就在 `rule_scenes`
    里有一行，查得到就合法。
    """

    def __init__(self, scene_code: str, known: list[str]):
        super().__init__(
            "CFG-4013",
            f"归属场景 {scene_code!r} 不存在；可选场景：{'、'.join(known) or '（字典为空，请先灌种子）'}",
            400,
            {"scene_code": scene_code, "known_scenes": known},
        )


class RuleWriteFailedError(AppError):
    """CFG-5001 规则写入失败（Mongo 报错）。

    与名单的 `CFG-5002` 分开：名单写失败要**置降级标记**（会让 05 回源、必要时
    转人审），规则写失败则没有部分写入（单文档操作），线上规则集不变——两者的
    运维处置完全不同，因此不共用一个码（BR-00-13）。
    """

    def __init__(self, detail: str):
        super().__init__(
            "CFG-5001",
            f"规则保存失败，请稍后重试。（{detail}）",
            503,
        )


# ============================================================
# 模块 05（RUL 前缀）的异常类
# ------------------------------------------------------------
# 四个类与 `RUL_CODES` 的四个码一一对应；`RUL_NOTICE` 的两个码**刻意不建异常类**：
# `RUL-5003`（单条规则求值异常）与 `RUL-5004`（耗时超阈值）的 HTTP 都是 **200**
# ——请求成功了，只是有一条规则被跳过 / 这次跑得慢。用异常表达会让调用方以为
# 需要回滚或重试整个请求，而正确的处置是"照常使用这个决策 + 看一眼告警"。
#
# 这四个类必须定义在 `AppError` **之后**（它们在文件后半部分），否则直接 NameError。
# ============================================================
class InvalidEngineEventError(AppError):
    """RUL-4001 事件体非法（缺 `event_type` / `user_id`）。

    与 03 的 `EVT-4001~4005` 分工：03 的码描述"接入网关眼里的报文哪里不对"，
    本条描述"**决策引擎**眼里的输入缺了判定所必需的标识"。两者都在报"报文错"，
    但一个接口只给一个码——`/engine/evaluate` 的入参缺 `user_id` 时给 RUL-4001，
    更深一层的场景字段缺失仍由 03 的 `EVT-4004` 报（ER-02：引用别人的码保持原前缀）。
    """

    def __init__(self, message: str, data: Any = None):
        super().__init__("RUL-4001", message, 400, data)


class InvalidConditionTreeError(AppError):
    """RUL-4002 条件树结构非法（`op` 不支持 / 缺 `field` / `exists` 带了 `value`）。

    **由 `app/engine/condition.py::validate_tree()` 抛出，模块 06 保存规则时调用**。
    决策链路上遇到非法条件树时**不抛本异常**：那会把一条坏配置升级成整次决策
    失败（所有事件都拿不到决策），而正确处置是按 BR-05-13 只判**那一条**规则
    失败（`RUL-5003`）并继续求值其余规则。Spec §5 对它的处理策略写的也正是
    "拒绝该规则，其余继续"。
    """

    def __init__(self, detail: str, node_path: str = ""):
        super().__init__(
            "RUL-4002",
            f"条件树结构非法：{detail}" + (f"（位置：{node_path}）" if node_path else ""),
            422,
            {"node_path": node_path, "detail": detail},
        )


class ListServiceUnavailableError(AppError):
    """RUL-5001 名单依赖不可用（缓存未命中且 Mongo 查询失败）。

    fail-closed：**查不到黑名单绝不等于没有黑名单**，因此本次决策降级为
    `review`（`degraded=true`）并落库建案（决策 D5）。`decision` 参数承载那个
    降级决策块，随 503 一起返回给调用方——"返回 `degraded=true` 与人工审核建议，
    不返回 `pass`"（Spec §3.1 的状态码说明）就是靠它落地的。
    """

    def __init__(self, detail: str = "", decision: Any = None):
        data: dict[str, Any] = {"degraded": True}
        if detail:
            data["detail"] = detail
        if decision is not None:
            data["decision_block"] = decision
        super().__init__("RUL-5001", RUL_CODES["RUL-5001"][1], 503, data)


class RuleSetUnavailableError(AppError):
    """RUL-5002 规则集加载失败（E05 `rules` / E06 `rule_scenes` 读不出来）。

    与 `RUL-5001` 同样是 fail-closed 降级：**读不到规则不等于没有规则命中**。
    若这种情况下返回 `pass`，一次 Mongo 抖动就会把全部规则放开——这正是
    "宁可报不可用、转人工，也绝不编造'一切正常'"在 05 侧的落点。
    """

    def __init__(self, detail: str = "", decision: Any = None):
        data: dict[str, Any] = {"degraded": True}
        if detail:
            data["detail"] = detail
        if decision is not None:
            data["decision_block"] = decision
        super().__init__("RUL-5002", RUL_CODES["RUL-5002"][1], 503, data)


# ============================================================
# 模块 09（GRP 前缀）的错误码与异常
# ------------------------------------------------------------
# 四个类与 `GRP_CODES` 的四个码一一对应；`GRP_NOTICE` 的三个码**刻意不建异常类**：
# 它们不是"请求失败"，用异常表达会让调用方误以为需要回滚或重试整个请求
# （超时已经返回了部分结果、补齐失败已经返回了其余节点、建边失败根本不在请求里）。
# ============================================================
class HopLimitExceededError(AppError):
    """GRP-4001 `max_hop` 越界（AD-06：硬上限 2 跳）。"""

    def __init__(self, max_hop: int, limit: int = 2):
        super().__init__(
            "GRP-4001",
            f"图谱查询最多支持 {limit} 跳，收到 max_hop={max_hop}",
            400,
            {"max_hop": max_hop, "limit": limit},
        )


class GraphEntityTypeError(AppError):
    """GRP-4002 `entity_type` 非法（如 `phone`）。

    **明确拒绝、不静默降级成 `user`**：手机号是用户属性而非独立图节点
    （Step1 §4），把它当节点查会得到一张"看起来正常但少了一半节点"的图，
    而人工研判最怕的就是"以为自己看到了全貌"。
    """

    def __init__(self, entity_type: str, allowed: tuple[str, ...] = ("user", "device", "ip", "address")):
        super().__init__(
            "GRP-4002",
            f"不支持的实体类型：{entity_type or '(缺失)'}（仅支持 {'/'.join(allowed)}）",
            422,
            {"entity_type": entity_type, "allowed": list(allowed)},
        )


class ProfileNotFoundError(AppError):
    """GRP-4004 用户/实体不存在（与"孤立账号"是两件事，见 §2.2 空态）。

    两者必须用**不同的信号**表达：不存在是 404，孤立是 200 + 空 `nodes`；
    把"孤儿账号"也报 404 会让前端显示"未找到该用户"，掩盖"这个人确实存在、
    只是没有任何关联"这一有价值的事实（`V-09-09` 直接验这一点）。
    """

    def __init__(self, entity_type: str, entity_id: str):
        super().__init__(
            "GRP-4004",
            GRP_CODES["GRP-4004"][1],
            404,
            {"entity_type": entity_type, "entity_id": entity_id},
        )


class ProfileUnavailableError(AppError):
    """GRP-5001 画像聚合查询失败（依赖故障）。

    与"用户不存在"严格区分：查库失败时**绝不能**返回 404——那会把一次 Mongo
    的抖动显示成"这个人不存在"，让审核员得出"没有这个人"的错误结论。
    契约要求这里是 503 + 前端显示错误占位（§5：右栏判定摘要不受影响）。
    """

    def __init__(self, detail: str = "", op: str = ""):
        data: dict[str, Any] = {"op": op} if op else {}
        if detail:
            data["detail"] = detail
        super().__init__(
            "GRP-5001", GRP_CODES["GRP-5001"][1], 503, data or None,
        )


# ============================================================
# 模块 08（DSP 前缀）的错误码与异常
# ------------------------------------------------------------
# 十个类与 `DSP_CODES` 的十个码一一对应；`DSP_NOTICE` 的两个码**刻意不建异常类**：
# `DSP-5003` / `DSP-5004` 的 HTTP 都是 200——处置已经生效，用异常表达会让
# "回滚"看起来是正确处置，而 Spec BR-08-31/32 明确要求**不撤销**已生效的处置
# （撤销已拦截的订单会造成"先拦后放"的二次伤害），只标记 + 可重试。
#
# 状态码一律从表里取（`_status(...)`），不在类里手写：同码两义（BR-00-13）
# 是本项目最容易被 `test_errors.py` 抓出来的一类缺陷。
# ============================================================
def _dsp(code: str) -> tuple[int, str]:
    status, message = DSP_CODES[code]
    return int(status or 400), message


class DisposalParamInvalidError(AppError):
    """DSP-4001 处置参数非法（结论缺失 / 动作空 / 备注 trim 后为空或超长）。

    Spec §5 把它定为 **400**（不是 422）：这不是"报文格式不对"，而是
    "业务必填项没填全"，前端要在处置表单上逐项提示（BR-08-15 / BR-08-16）。
    """

    def __init__(self, detail: str):
        status, message = _dsp("DSP-4001")
        super().__init__("DSP-4001", message, status, {"detail": detail})


class DisposalMatrixViolationError(AppError):
    """DSP-4002 结论与联动动作不相容（BR-08-14 的相容矩阵）。

    `detail` 必须写清**哪一条**不相容：前端要据此在 `dpActions` 下方给出
    原因文案（如「结论为『确认为正常』时不可勾选拦截类动作」），
    只回一句"参数不相容"用户无从改起。
    """

    def __init__(self, detail: str, *, conclusion: str = "", action_types: Any = None):
        status, message = _dsp("DSP-4002")
        super().__init__(
            "DSP-4002", message, status,
            {"detail": detail, "conclusion": conclusion,
             "action_types": list(action_types or [])},
        )


class CaseStateConflictError(AppError):
    """DSP-4003 案件状态不允许该操作（BR-08-08 的状态机）。

    **这是本模块的乐观锁冲突出口（决策 D42）**：所有状态迁移都用
    "带前置状态的条件更新"表达，匹配 0 条即说明状态已被别处改过
    （别人先认领、已处置、已归档、或另一次处置正在进行）——
    此时必须报冲突而不是静默成功，否则页面会显示一个库里并不存在的状态。
    """

    def __init__(self, action: str, current: str, detail: str = ""):
        status, message = _dsp("DSP-4003")
        text = f"案件当前状态为『{current}』，无法{action}"
        if detail:
            text = f"{text}（{detail}）"
        super().__init__(
            "DSP-4003", text, status,
            {"action": action, "current_status": current, "detail": detail},
        )


class CaseAlreadyDisposedError(AppError):
    """DSP-4004 重复处置（BR-08-19）。

    与 `DSP-4003` 分开是刻意的：`DSP-4003` 是"状态不允许这个动作"（含认领、
    归档、并发锁定），而本条专指"这个案件已经处置完了"——页面文案不同，
    且只有它能被 `idempotency_key` 的幂等回放替代。
    """

    def __init__(self, case_no: str):
        status, message = _dsp("DSP-4004")
        super().__init__("DSP-4004", message, status, {"case_no": case_no})


class CaseNotAssigneeError(AppError):
    """DSP-4005 非当前认领人处置（BR-08-06：**admin 也不得代为处置**）。

    `assignee` 必须回传给前端：页面要显示「案件由 reviewer02 认领，请先由本人
    处置或等待超时回收」，只给一句"无权处置"用户不知道该等谁。
    """

    def __init__(self, case_no: str, assignee: str, operator: str):
        status, message = _dsp("DSP-4005")
        super().__init__(
            "DSP-4005", message, status,
            {"case_no": case_no, "assignee": assignee, "operator": operator},
        )


class ConfirmTokenInvalidError(AppError):
    """DSP-4006 二次确认令牌无效 / 过期 / 与参数不匹配（BR-08-27）。

    这是**防绕过前端**的那道闸：缺失、过期、换过参数、已用过（一次性）
    四种情形一律拒绝，且**不执行任何副作用**。
    """

    def __init__(self, reason: str, detail: str = ""):
        status, message = _dsp("DSP-4006")
        super().__init__(
            "DSP-4006", message, status,
            {"reason": reason, "detail": detail},
        )


class CaseNotFoundError(AppError):
    """DSP-4040 案件不存在。

    Spec §5 的提示是「案件不存在或已归档」——但**已归档的案件是存在的**
    （`status=archived`），对它调认领/处置应给 `DSP-4003`（状态不允许），
    只有真的查不到行才给 404。两者混用会让前端把"归档案件"误显示成
    "案件不见了"，因此本类只用于"查无此案"。
    """

    def __init__(self, case_no: str):
        status, message = _dsp("DSP-4040")
        super().__init__("DSP-4040", message, status, {"case_no": case_no})


class CaseStateWriteFailedError(AppError):
    """DSP-5001 案件状态写入失败（Mongo 不可用）。

    契约要求"已写的名单条目**回滚**"（BR-08-30 的同一条原则），由
    `DisposalService` 在抛出本异常之前完成补偿。
    """

    def __init__(self, case_no: str, detail: str = ""):
        status, message = _dsp("DSP-5001")
        super().__init__(
            "DSP-5001", message, status,
            {"case_no": case_no, "detail": detail},
        )


class ListLinkWriteFailedError(AppError):
    """DSP-5002 **紧操作**失败：名单库联动写入失败（BR-08-30 的 fail-closed）。

    本模块最重要的一条：名单写入是**紧操作**，失败必须明确报错并回滚，
    绝不允许"接口返回成功但黑名单没生效"。`rolled_back` 如实回传补偿结果
    （补偿本身也可能失败，那时页面必须提示人工核对，而不是显示"已回滚"）。
    """

    def __init__(self, detail: str, *, rolled_back: bool = False, failed_at: str = ""):
        status, message = _dsp("DSP-5002")
        super().__init__(
            "DSP-5002", message, status,
            {"detail": detail, "rolled_back": rolled_back, "failed_at": failed_at},
        )


class CaseActionWriteFailedError(AppError):
    """DSP-5005 处置流水 `case_actions` 落库失败（整体失败 + 名单回滚）。

    与 `DSP-5001` 分开：一个是"流水没落"，一个是"案件状态没落"。
    两者的排查方向完全不同（前者看 `case_actions` 的写入与索引，
    后者看 `risk_cases` 的条件更新），共用一个码会把告警归因搅在一起。
    """

    def __init__(self, case_no: str, detail: str = "", *, rolled_back: bool = False):
        status, message = _dsp("DSP-5005")
        super().__init__(
            "DSP-5005", message, status,
            {"case_no": case_no, "detail": detail, "rolled_back": rolled_back},
        )


# ============================================================
# 模块 10 自有错误码（SIM-*）
# ------------------------------------------------------------
# 划分原则与 04/05/07/08/09/11/12 完全一致：**`SIM_CODES` 只放真正的 HTTP 错误
# 响应**。`Spec 10 §5` 的六个码里 `SIM-5002`（链路记录落库失败）的 HTTP 列写的
# 是 **200** —— 仿真结果本身已经算出来并返回了，只是"回看"这一层不可用。若把它
# 放进本表，就会与 BR-00-12「5xxx = 服务端/依赖错误（会产生 5xx 响应）」冲突，
# `tests/test_errors.py` 也会（正确地）报错；因此它进 `SIM_NOTICE`，
# 以 `data.record_saved=false` + `data.notice_code` 的形式表达
# （与 08 的 `DSP_NOTICE` / 11 的 `MET_NOTICE` 完全同形）。
#
# **`SIM-4001` 与 03 的 `EVT-4xxx` 的分工**：`/sim/run` 的事件体校验**必须**走与
# `POST /events` 同一套（`event_service.validate_event`），"不得为仿真放宽"
# （任务书 §3）。因此 03 的字段级判定**保留 EVT 前缀原样透出**（ER-02），
# 只在最外层套一个 `SIM-4001` 的**结论码**，并把
# `{sim_code, source_code, missing, field}` 一起放进 `data`：
# 前端既知道"这是仿真页的步骤 1 失败"，也拿得到"缺哪个字段/哪个字段格式不对"
# 去重绘星号。若把 `missing` 吞掉只回一句"事件参数不合法"，
# 页面就只能干瞪眼——那不是更严格，而是更没用。
# ============================================================
SIM_CODES: dict[str, tuple[Optional[int], str]] = {
    "SIM-4001": (400, "事件参数不合法"),
    "SIM-4002": (422, "扩展参数不是合法 JSON"),
    "SIM-4003": (409, "已存在同名用例"),
    "SIM-4004": (404, "未找到该次仿真记录"),
    "SIM-4005": (422, "单次最多回放 200 条"),
    "SIM-5001": (503, "决策引擎暂时不可用，无法仿真"),
    "SIM-5003": (504, "执行超时，请简化参数后重试"),
}

# 降级/告警结论码（HTTP 200，出现在响应 `data` 与日志里，不构成 HTTP 错误）
SIM_NOTICE: dict[str, str] = {
    "SIM-5002": "本次结果已返回，但链路记录保存失败（可能无法回看）",
}


def sim_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `SIM_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = SIM_CODES.get(code, (400, "仿真测试请求失败"))
    return AppError(code, message or default, status or 400, data)


class SimEventInvalidError(AppError):
    """`SIM-4001` 事件体非法（任务书 §3：仿真表单与 `POST /events` 同一套校验）。

    ## 为什么要把 03 的原码与缺失字段一并带出来

    Spec §2.2 的表单校验要求"缺必填字段就标红并提示"，§4.3 的 BR-10-15 要求
    步骤 1 红、后续步骤显示"未执行"。两件事都需要**字段级**信息：
    只给一句"事件参数不合法"，前端无从知道该给哪个输入框加星号。

    因此 `data` 固定带四个键（缺失的置 `None`，不省略）：

    | 键 | 含义 |
    |---|---|
    | `sim_code` | 恒为 `SIM-4001`（前端按它分流到仿真页文案） |
    | `source_code` | 真实校验抛出的 03 原码（`EVT-4002/4004/4005/4006/4007`） |
    | `missing` | 缺失字段名清单（`EVT-4004` 有，其余为空数组） |
    | `field` | 出错的具体字段名（格式类错误有，其余为 `None`） |

    `source_code` 与 `field` 都来自 03 的 `data`，**本模块不自己猜**——猜出来的
    字段名会把"`ip` 格式非法"显示成"`device_id` 格式非法"，比不显示更糟。
    """

    def __init__(
        self,
        message: str,
        *,
        source_code: Optional[str] = None,
        missing: Optional[list[str]] = None,
        field: Optional[str] = None,
    ):
        super().__init__(
            "SIM-4001",
            message,
            400,
            {
                "sim_code": "SIM-4001",
                "source_code": source_code,
                "missing": list(missing or []),
                "field": field,
            },
        )


class SimDuplicateCaseNameError(AppError):
    """`SIM-4003` 用例名重复（Spec §5：提示改名或覆盖）。

    唯一性由 `uq_sim_case_name` **部分唯一索引**兜底（只约束 `status=active`），
    服务层先查一次只是为了让绝大多数情况给出干净的 409 而不是撞键异常；
    真正的并发防线在索引上——"先查再插"在并发下必然漏判（BR-06-20 的同一条教训）。
    """

    def __init__(self, name: str):
        super().__init__(
            "SIM-4003", SIM_CODES["SIM-4003"][1], 409, {"name": name}
        )


class SimRunNotFoundError(AppError):
    """`SIM-4004` 执行记录不存在（Spec §3.5 的 404）。

    与"记录保存失败"（`SIM-5002`）严格分开：前者是"这条 run_id 从来没有过"
    （编号写错或已被清理），后者是"本次结果已返回但没能落库"。两者对用户的
    处置完全不同（改编号 vs 别指望回看），共用一个码会让前端提示错方向。
    """

    def __init__(self, run_id: str):
        super().__init__(
            "SIM-4004", SIM_CODES["SIM-4004"][1], 404, {"run_id": run_id}
        )


class SimBatchLimitError(AppError):
    """`SIM-4005` 批量回放条数超上限（BR-10-19：默认 20，上限 200）。

    **不静默截断到 200**：那会让调用方以为 500 条都跑过了，而统计数字只覆盖
    其中 200 条——批量回放的用途正是"看误伤率"，一个来路不明的分母比报错危险得多。
    """

    def __init__(self, repeat: Any, limit: int):
        super().__init__(
            "SIM-4005",
            f"单次最多回放 {limit} 条，收到 {repeat}",
            422,
            {"repeat": repeat, "limit": limit},
        )


class SimEngineUnavailableError(AppError):
    """`SIM-5001` 决策引擎不可用（Spec §5：**不得降级为"用简化逻辑算一下"**）。

    ## 为什么连"降级返回 review"都不允许

    05 的 `decide()` 对依赖故障**从不抛异常**：它会产出一个 `review` +
    `degraded=true` 的决策块（D5，为了落库建案）。`/engine/evaluate` 的处理是
    "503 + 响应里仍带那个决策块"。而仿真页的**唯一价值**是"展示线上会怎么判"，
    一个降级块不是结论——把它当结论渲染成橙色的 `review`，等于告诉策略师
    "这条规则判转人工"，而真相是"引擎根本没读到规则集"。Spec §5 的原话就是
    「**绝不允许**用'简化判定'给出结论……降级即说谎」。

    因此本类**不携带任何决策块**（`data` 里只有 `run_id` 与"哪一步失败"），
    并且它的 `data` 里显式声明 `degraded=true` / `conclusion_available=false`，
    让前端知道"这一格不能填数字"。
    """

    def __init__(
        self,
        detail: str,
        *,
        run_id: Optional[str] = None,
        failed_step: Optional[str] = None,
        source_code: Optional[str] = None,
    ):
        super().__init__(
            "SIM-5001",
            SIM_CODES["SIM-5001"][1],
            503,
            {
                "detail": detail,
                "run_id": run_id,
                "failed_step": failed_step,
                "source_code": source_code,
                "degraded": True,
                "conclusion_available": False,
            },
        )


class SimTimeoutError(AppError):
    """`SIM-5003` 仿真超时（>5s，Spec §5 / BR-10-12 的同一预算）。

    **不返回半个结论**：超时的语义是"我们不知道线上会怎么判"。把已经跑到的
    中间结果（例如"名单未命中"）当结论展示，会让人以为链路走完了。
    """

    def __init__(self, budget_ms: int, *, run_id: Optional[str] = None,
                 failed_step: Optional[str] = None):
        super().__init__(
            "SIM-5003",
            SIM_CODES["SIM-5003"][1],
            504,
            {
                "budget_ms": int(budget_ms),
                "run_id": run_id,
                "failed_step": failed_step,
                "conclusion_available": False,
            },
        )


# ============================================================
# 模块 13 自有错误码（SYS-*）
# ------------------------------------------------------------
# 划分原则与 04/05/07/11/12 完全一致：**`SYS_CODES` 只放真正的 HTTP 错误响应**。
# Spec 13 §5 表里的 `SYS-5003`（组件探测超时）与 `SYS-5004`（热更新广播失败）
# 的 HTTP 列写的是 **200** —— 请求本身成功了，只是"某个组件探测超时"或
# "配置已保存但部分模块未生效"。若放进本表就与 BR-00-12「5xxx = 服务端/依赖
# 错误（会产生 5xx 响应）」冲突，`tests/test_errors.py` 也会（正确地）报错，
# 因此它们进 `SYS_NOTICE`，出现在响应 `data.notices` 与日志里。
#
# **`SYS-4005/4006` 的优先级（一处必须写明的裁定）**：删/停用**自己**一律先报
# `SYS-4005`（BR-13-13 是"身份"判断），再看"是否最后一个可用管理员"
# （BR-13-14 是"数量"判断）。两条同时成立时（种子环境下 admin01 是唯一管理员，
# 停用自己）以 `SYS-4005` 为准 —— 这正是 V-13-13 的验收方法；而 V-13-14 的
# "最后一个管理员"在操作者必须是可用管理员的前提下，唯一可达形态是
# **把最后一个管理员降级**（任务书 §2② 明确列出该场景），见
# `user_admin_service._assert_admin_remains`。
# ============================================================
SYS_CODES: dict[str, tuple[Optional[int], str]] = {
    "SYS-4001": (422, "参数取值超出允许范围"),
    "SYS-4002": (422, "短窗口必须小于长窗口"),
    "SYS-4003": (422, "模型引擎尚未实现（G-01）"),
    "SYS-4004": (409, "账号已存在"),
    "SYS-4005": (403, "不能停用或删除当前登录账号"),
    "SYS-4006": (403, "系统必须保留至少一个可用管理员"),
    "SYS-4007": (409, "该账号有待处置案件，请先转交"),
    "SYS-4008": (409, "请先停用该账号再删除"),
    # `SYS-4009` 是**模块 13 新增**的码（Spec 13 §5 未分配该场景）：
    # 决策 D42 要求并发冲突必须用条件更新表达并**明确报错**，而不是后写覆盖。
    # 13 的账号写入用"条件更新 + 匹配 0 条"实现乐观锁（E19 没有 `version` 列），
    # 冲突时需要一个 409 的码——复用 `SYS-4004`（账号已存在）或 `SYS-4007`
    # （有待处置案件）都会让前端提示错方向，凭空借用别的模块的码则违反 ER-02。
    "SYS-4009": (409, "账号状态已被他人修改，请刷新后重试"),
    "SYS-5001": (503, "配置保存失败，已回滚"),
    "SYS-5002": (503, "账号操作失败"),
}

# 降级/告警结论码（HTTP 200，出现在响应 `data.notices` 与日志里）
SYS_NOTICE: dict[str, str] = {
    "SYS-5003": "组件探测超时，该组件显示「探测超时」",
    "SYS-5004": "配置已保存，但部分模块未生效，建议重启",
}


def sys_error(code: str, message: Optional[str] = None, data: Any = None) -> "AppError":
    """按 `SYS_CODES` 表构造 `AppError`（状态码只有一处真源）。"""
    status, default = SYS_CODES.get(code, (400, "系统设置请求失败"))
    return AppError(code, message or default, status or 400, data)


def _sys(code: str) -> tuple[int, str]:
    """取 SYS 码的 `(HTTP 状态, 默认文案)`（状态码只在表里维护一处）。"""
    status, message = SYS_CODES[code]
    return int(status or 400), message


class ConfigValueOutOfRangeError(AppError):
    """`SYS-4001` 运行参数取值越界（BR-13-01）。

    **不静默夹取**：把 `list_cache_ttl_sec=9999` 悄悄改成 600 会让用户以为
    自己设的值生效了，而"名单最长生效延迟"这种承诺是要写进交付说明的
    （AD-02）——一个被静默改小的数字比一个明确报错危险得多。
    """

    def __init__(self, field: str, low: Any, high: Any, value: Any):
        status, message = _sys("SYS-4001")
        super().__init__(
            "SYS-4001",
            f"参数 {field} 超出允许范围（{low}~{high}），收到：{value!r}",
            status,
            {"field": field, "min": low, "max": high, "value": value},
        )
        self.field = field


class WindowOrderInvalidError(AppError):
    """`SYS-4002` 短窗口 ≥ 长窗口（BR-13-01）。

    短窗是长窗的**子区间**：短窗更长时，"近 1h 领券次数"会取到比"近 24h"
    更大的区间，两个计数的大小关系反过来，特征语义随即失效。
    """

    def __init__(self, short: Any, long: Any):
        status, message = _sys("SYS-4002")
        super().__init__(
            "SYS-4002",
            f"{message}（短窗口 {short} 分钟 ≥ 长窗口 {long} 分钟）",
            status,
            {"short_window_min": short, "long_window_min": long},
        )


class ModelEngineUnavailableError(AppError):
    """`SYS-4003` 试图切换到未实现的模型引擎（BR-13-22 / AD-09 / G-01）。

    **明确拒绝、不改配置**：AD-09 已裁定模型引擎是空实现（`model_score` 恒为
    `null`），假装成功会让评审以为系统真的支持模型决策——那正是 §2.4 要求
    用橙色警示条显著标注的原因。
    """

    def __init__(self, engine_type: Any):
        status, message = _sys("SYS-4003")
        super().__init__(
            "SYS-4003",
            message,
            status,
            {
                "engine_type": engine_type,
                "model_available": False,
                "reason": "ModelEngine 当前为空实现，恒返回 None，final_score = rule_score",
                "reference": "G-01",
            },
        )


class UsernameConflictError(AppError):
    """`SYS-4004` 账号已存在（BR-13-11：`username` 全局唯一且创建后不可修改）。"""

    def __init__(self, username: str):
        status, message = _sys("SYS-4004")
        super().__init__(
            "SYS-4004", f"账号 {username} 已存在", status, {"username": username}
        )


class SelfOperationForbiddenError(AppError):
    """`SYS-4005` 停用/删除**自己**（BR-13-13）。

    这是"不能把自己锁在门外"的第一道护栏：管理员停用自己之后，
    没有任何界面能把他启回来（启用自己的接口也要 admin 权限）。
    """

    def __init__(self, action: str, username: str = ""):
        # 文案用 Spec §5 的原句（不按 action 拼字符串）："不能停用或删除当前登录账号"
        # 是**一句完整的话**，按动作拼会在"删除自己"时拼出"不能删除或删除…"。
        status, message = _sys("SYS-4005")
        super().__init__(
            "SYS-4005",
            message,
            status,
            {"action": action, "username": username},
        )


class LastAdminProtectedError(AppError):
    """`SYS-4006` 停用/删除/降级最后一个可用管理员（BR-13-14）。

    **"可用"的定义**：`status=active`。因此"停用一个早已停用的管理员"不触发
    本码（数量没有变化）；这也让"降级最后一个管理员"成为本码唯一的非自身
    可达形态（操作者本身必须是一个可用管理员）。
    """

    def __init__(self, action: str, username: str = "", remaining: int = 0):
        status, message = _sys("SYS-4006")
        super().__init__(
            "SYS-4006",
            message,
            status,
            {"action": action, "username": username,
             "active_admins_after": int(remaining)},
        )


class PendingCasesError(AppError):
    """`SYS-4007` 账号名下有待处置案件（BR-13-15）。

    `data.cases` 返回案件清单（供页面直接做转交选择），否则管理员只知道
    "有 3 个案件"却不知道该转交哪些、转交给谁才合适。
    """

    def __init__(self, username: str, cases: Any = None):
        status, message = _sys("SYS-4007")
        rows = list(cases or []) if not isinstance(cases, str) else []
        super().__init__(
            "SYS-4007",
            f"该账号有 {len(rows)} 个待处置案件，请先转交",
            status,
            {"username": username, "pending_count": len(rows), "cases": rows},
        )


class DisableBeforeDeleteError(AppError):
    """`SYS-4008` 删除未停用的账号（BR-13-17）。

    要求"先停用再删除"不是形式主义：停用是一次**可逆**操作（能一键启用回来），
    删除是软删（账号从登录与列表消失）。多一步确认能挡住绝大多数误点。
    """

    def __init__(self, username: str, status_value: Any):
        status, message = _sys("SYS-4008")
        super().__init__(
            "SYS-4008",
            message,
            status,
            {"username": username, "status": status_value},
        )


class AccountVersionConflictError(AppError):
    """`SYS-4009` 账号状态在本次操作过程中被他人改变（决策 D42 的乐观锁冲突）。

    E19 没有 `version` 列，因此这里的乐观锁是**状态条件更新**：更新语句带上
    "我读到的那个 `status`/`role`"，匹配 0 条即说明状态已变（别人刚停用了他、
    刚改过角色）。必须报冲突而不是静默重试——两个管理员同时改同一个账号时，
    后写覆盖会让前者的改动消失且无人知晓。
    """

    def __init__(self, username: str, detail: str = ""):
        status, message = _sys("SYS-4009")
        text = f"{message}（{detail}）" if detail else message
        super().__init__(
            "SYS-4009", text, status,
            {"username": username, "detail": detail} if detail else {"username": username},
        )


class ConfigSaveFailedError(AppError):
    """`SYS-5001` 运行参数保存失败（写入失败或审计失败已回滚）。

    Spec §5 的原子性要求：**全成或全不成**。"半个配置生效"比不生效更危险——
    例如 TTL 改了但超时没改，行为不可预期。本码同时承担"审计写不进去 →
    回滚"的对外表达（与模块 06 的 `CFG-5003` 同一语义，只是前缀归 13）。
    """

    def __init__(self, detail: str = ""):
        status, message = _sys("SYS-5001")
        super().__init__("SYS-5001", message, status, {"detail": detail} if detail else None)


class AccountWriteFailedError(AppError):
    """`SYS-5002` 账号数据写入失败（BR-13-18：不留半成品）。

    账号是**认证数据源**（模块 01 读它做登录）：一次"改了一半"的写入
    （例如角色改了但审计没落地）在这张表上是安全事件，因此必须回滚。
    """

    def __init__(self, detail: str = ""):
        status, message = _sys("SYS-5002")
        super().__init__("SYS-5002", message, status, {"detail": detail} if detail else None)


# ============================================================
# 统一响应包
# ============================================================
def envelope(code: str, message: str, trace_id: str, data: Any = None) -> dict:
    """构造统一响应体。

    `ok` 由 `code` 派生，保证"唯一成功判据"与错误码不可能自相矛盾。
    """
    return {
        "ok": code == "OK",
        "code": code,
        "message": message,
        "trace_id": trace_id,
        "data": data,
    }


def _trace_of(request: Request) -> str:
    """取当前请求的 trace_id；中间件未生效时（如启动期）兜底生成一个。"""
    return (
        getattr(request.state, "trace_id", None)
        or get_trace_id()
        or new_trace_id()
    )


# ============================================================
# 处理器
# ============================================================
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """可预期业务错误。"""
    trace_id = _trace_of(request)
    if exc.http_status >= 500:
        log.warning("业务异常 %s trace_id=%s path=%s msg=%s",
                    exc.code, trace_id, request.url.path, exc.message)
    return JSONResponse(
        status_code=exc.http_status,
        content=envelope(exc.code, exc.message, trace_id, exc.data),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI/Pydantic 参数校验失败。

    两类要分开（模块 00 §5）：
    - **请求体不是合法 JSON** → `COM-4000`（400）：客户端连报文都没拼对
    - **字段级校验失败** → `COM-4001`（422）：`data.errors` 带路径与原因
    """
    trace_id = _trace_of(request)
    raw_errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in raw_errors):
        body = ""
        try:
            body = (await request.body()).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 取不到原始体不影响返回错误
            body = ""
        log.warning(
            "请求体非法 trace_id=%s path=%s raw=%r",
            trace_id, request.url.path, body[:RAW_BODY_LOG_LIMIT],
        )
        return JSONResponse(
            status_code=400,
            content=envelope("COM-4000", "请求格式错误", trace_id),
        )

    errors = [
        {
            "path": ".".join(str(p) for p in e.get("loc", []) if p not in ("body", "query", "path")),
            "message": e.get("msg", ""),
        }
        for e in raw_errors
    ]
    return JSONResponse(
        status_code=422,
        content=envelope("COM-4001", "参数校验失败", trace_id, {"errors": errors}),
    )


# Starlette 自己抛的 4xx/5xx（路由不存在、方法不允许、静态 404 等）到 COM 码的映射
_HTTP_STATUS_TO_CODE: dict[int, str] = {
    400: "COM-4000",
    404: "COM-4004",
    405: "COM-4005",
    429: "COM-4290",
}


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """把框架层 HTTPException 也纳入统一契约，避免出现"裸 404 无 trace_id"。"""
    trace_id = _trace_of(request)
    if exc.status_code in _HTTP_STATUS_TO_CODE:
        code = _HTTP_STATUS_TO_CODE[exc.status_code]
    elif exc.status_code >= 500:
        code = "COM-5000"
    else:
        code = "COM-4000"
    status, default_message = COM_CODES.get(code, (exc.status_code, "请求失败"))
    return JSONResponse(
        status_code=status or exc.status_code,
        content=envelope(code, default_message, trace_id),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未归类异常都必须落到 `COM-5000`（BR-00-15）。

    **完整堆栈只进服务端日志**，响应只给 `trace_id`——堆栈可能含库结构与
    文件路径，回显给前端等于泄露实现细节。

    **这里必须自己设置 `X-Trace-Id` 头**：未捕获异常会一路穿过业务中间件，
    由最外层的 `ServerErrorMiddleware` 直接调用本处理器生成响应，因此
    中间件末尾那段"给响应加 trace_id 头"的代码根本没有机会执行。
    不做这一步，就会出现"500 响应有 trace_id 字段、却没有同名响应头"的
    不一致——而 §5 的兜底原则要求任何 500 都必须可被 trace_id 定位。
    """
    trace_id = _trace_of(request)
    log.exception("未捕获异常 trace_id=%s path=%s", trace_id, request.url.path)
    response = JSONResponse(
        status_code=500,
        content=envelope("COM-5000", COM_CODES["COM-5000"][1], trace_id),
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


def register_exception_handlers(app: FastAPI) -> None:
    """装配全部异常处理器（模块 00 统一提供，业务模块不得自行注册）。"""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
