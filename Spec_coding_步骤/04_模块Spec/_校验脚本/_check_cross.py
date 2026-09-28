# -*- coding: utf-8 -*-
"""跨 14 份 Spec 的一致性检查：关键参数、关键裁定、重复实现风险。"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

D = r"D:\Aruanjian_coding_tools\agent_workspace\dsh\knowledge\01_交付产物\2.3_电商风险控制系统\04_模块Spec"
# 14_待确认事项汇总.md 由另一会话产出，不纳入本校验器的模块口径
SKIP_PREFIX = {"14"}
files = sorted(f for f in os.listdir(D) if re.match(r"\d{2}_", f) and f.endswith(".md")
               and "模块划分" not in f and f[:2] not in SKIP_PREFIX)
ds = [f for f in os.listdir(D) if re.match(r"\d{2}_", f) and f[:2] in SKIP_PREFIX]
if ds:
    print(f"[跳过非本会话文件] {ds}\n")
docs = {f[:2]: open(os.path.join(D, f), encoding="utf-8").read() for f in files}
names = {f[:2]: f[:-3] for f in files}

problems = []
print("=" * 78)
print("A. 关键数值参数一致性")
print("=" * 78)

# (标签, 正则, 期望值, 必须出现的模块)
CHECKS = [
    ("短窗口 60 分钟", r"短窗(?:口)?[^\n]{0,30}60|short_window_min\D{0,12}60", None, ["04", "13"]),
    ("长窗口 1440 分钟", r"1440", None, ["04", "13"]),
    ("窗口容量 10000", r"10000", None, ["04", "13"]),
    ("名单缓存 TTL 10 秒", r"TTL[^\n]{0,20}10|10s|10 秒", None, ["05", "13"]),
    ("决策超时 200ms", r"超时[^\n]{0,30}200|200\s*ms|200 毫秒", None, ["05", "13"]),
    ("图谱 2 跳上限", r"2\s*跳|max_hop", None, ["09"]),
    ("分值分级 0~59", r"0\s*[~-]\s*59", None, ["05"]),
    ("分值分级 60~79", r"60\s*[~-]\s*79", None, ["05"]),
    ("分值分级 80~100", r"80\s*[~-]\s*100", None, ["05"]),
]
for label, pat, _, must in CHECKS:
    hit = [m for m in docs if re.search(pat, docs[m])]
    missing = [m for m in must if m not in hit]
    status = "✔" if not missing else "✗"
    print(f"  {status} {label:24} 出现于 {sorted(hit)}" + (f"  缺 {missing}" if missing else ""))
    if missing:
        problems.append(f"{label} 未在预期模块出现: {missing}")

print("\n" + "=" * 78)
print("B. 关键裁定是否全项目一致")
print("=" * 78)
RULINGS = [
    ("不做 Challenge（三档）", lambda t: "不做 Challenge" in t or "不做 challenge" in t,
     ["05"], True),
    ("fail-closed（异常默认 review）", lambda t: "fail-closed" in t.lower(),
     ["04", "05", "12"], True),
    ("仿真不写特征窗口", lambda t: "不写入真实特征窗口" in t or "BR-10-06" in t, ["10"], True),
    ("仿真复用真实链路", lambda t: "/engine/evaluate" in t, ["10"], True),
    ("条件树校验归 05、编辑归 06", lambda t: "validate_tree" in t, ["06"], True),
    ("大盘只展示不计算", lambda t: "禁止在页面里计算" in t or "只做展示" in t, ["02"], True),
    ("审计紧操作阻断", lambda t: "AUD-5001" in t and "阻断" in t, ["12"], True),
    ("模型引擎预留空实现", lambda t: "AD-09" in t or "G-01" in t, ["05", "13"], True),
    ("聚集度归 09 唯一真源", lambda t: "get_linked_user_count" in t, ["04", "09"], True),
]
for label, fn, must, want in RULINGS:
    hit = [m for m in docs if fn(docs[m])]
    missing = [m for m in must if m not in hit]
    print(f"  {'✔' if not missing else '✗'} {label:32} 出现于 {sorted(hit)}" + (f"  缺 {missing}" if missing else ""))
    if missing:
        problems.append(f"裁定「{label}」未在 {missing} 体现")

print("\n" + "=" * 78)
print("C. 重复实现风险（同一算法/规则被两处声称实现）")
print("=" * 78)
DUP = [
    ("规则求值算法", ["05", "06"], r"条件树求值|evaluate\("),
    ("名单匹配语义", ["05", "06"], r"5 个维度依次匹配|按 5 个维度"),
    ("指标计算", ["11", "02"], r"block_rate\s*=|拦截率的计算|指标计算"),
    ("聚集度计算", ["09", "04"], r"device_user_cnt|linked_user_count"),
    ("哈希链算法", ["12", "08"], r"sha256\("),
]
# 约定：列表第一个是**归属方**（合法实现者），其余为**使用方**（应声明委托）
DELEG = (r"不重复实现|由 0\d|由模块 0\d|调用 0\d|调用模块 0\d|仅 import|只引用|转调"
         r"|依赖模块 0\d|向模块 0\d|禁止在页面里计算|由 \*\*0\d\*\*")
for label, mods, pat in DUP:
    owner, users = mods[0], mods[1:]
    hit_users = [m for m in users if re.search(pat, docs[m])]
    if not hit_users:
        print(f"  {label:16} 仅归属方 {owner} 涉及   ✔ 无重复风险")
        continue
    no_decl = [m for m in hit_users if not re.search(DELEG, docs[m])]
    if no_decl:
        print(f"  {label:16} {no_decl} 提及但未见委托声明   ⚠ 需人工确认")
    else:
        print(f"  {label:16} {hit_users} 均有委托声明   ✔ 主从清晰")

print("\n" + "=" * 78)
print("D. 错误码前缀与总纲一致")
print("=" * 78)
PREFIX = {"00": "COM", "01": "AUTH", "02": "DASH", "03": "EVT", "04": "FEA", "05": "RUL",
          "06": "CFG", "07": "CASE", "08": "DSP", "09": "GRP", "10": "SIM", "11": "MET",
          "12": "AUD", "13": "SYS"}
for m in sorted(docs):
    codes = {c for c in re.findall(r"`([A-Z]{2,4}-\d{4})`", docs[m])}
    own = {c for c in codes if c.startswith(PREFIX[m] + "-")}
    print(f"  {m} {names[m][3:]:24} 本模块码 {len(own):2} 个" + (f"  → {sorted(own)[:3]}" if own else "  ✗ 无"))
    if not own:
        problems.append(f"模块 {m} 未定义本模块错误码")

print("\n" + "=" * 78)
print("E. 全局错误码唯一性")
print("=" * 78)
seen = {}
for m in sorted(docs):
    for c in re.findall(r"`([A-Z]{2,4}-\d{4})`", docs[m]):
        seen.setdefault(c, []).append(m)
conflict = {c: v for c, v in seen.items() if len(set(v)) > 1}
print(f"  全局出现 {len(seen)} 个不同错误码")
if conflict:
    print("  ⚠ 同一错误码出现在多个模块（可能是有意引用，需确认）:")
    for c, v in sorted(conflict.items()):
        print(f"      {c}: {sorted(set(v))}")
else:
    print("  ✔ 无冲突")

print("\n" + "=" * 78)
if problems:
    print("发现问题：")
    for p in problems:
        print("  ✗", p)
else:
    print("跨文档一致性：全部通过 ✔")

# 汇总
tot_lines = sum(t.count("\n") + 1 for t in docs.values())
tot_br = sum(len(set(re.findall(r"BR-\d{2}-\d{2}", t))) for t in docs.values())
tot_v = sum(len(set(re.findall(r"V-\d{2}-\d{2}", t))) for t in docs.values())
print("\n" + "=" * 78)
print(f"合计：{len(docs)} 份文档 · {tot_lines} 行 · {tot_br} 条业务规则 · {len(seen)} 个错误码 · {tot_v} 条验收标准")
