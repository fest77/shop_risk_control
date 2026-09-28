# -*- coding: utf-8 -*-
"""通用：校验 04_模块Spec 下所有模块文档（不只 05）。"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

D = r"D:\Aruanjian_coding_tools\agent_workspace\dsh\knowledge\01_交付产物\2.3_电商风险控制系统"
SPEC = os.path.join(D, "04_模块Spec")

S1 = open(os.path.join(D, "01_数据实体", "数据实体设计.md"), encoding="utf-8").read()
S2 = open(os.path.join(D, "02_概要设计", "概要设计.md"), encoding="utf-8").read()
entities = set(re.findall(r"\|\s*(E\d{2})\s*\|", S1))
gaps = {f"G-{n:02d}" for n in range(1, 13)}
ads = {f"AD-{n:02d}" for n in range(1, 10)}

# 模块号 -> 错误码前缀
PREFIX = {"00": "COM", "01": "AUTH", "02": "DASH", "03": "EVT", "04": "FEA",
          "05": "RUL", "06": "CFG", "07": "CASE", "08": "DSP", "09": "GRP",
          "10": "SIM", "11": "MET", "12": "AUD", "13": "SYS"}

SECTIONS = ["## 1. 模块定位", "## 2. 页面结构", "## 3. 接口字段", "## 4. 业务规则",
            "## 5. 异常处理", "## 6. 文件规划", "## 7. 验收标准", "## 8. 本模块遗留与待确认"]

# 14_待确认事项汇总.md 由另一会话产出，不纳入本校验器的模块口径
SKIP_PREFIX = {"14"}
files = sorted(f for f in os.listdir(SPEC)
               if f.endswith(".md") and not f.startswith("_")
               and re.match(r"\d{2}_", f) and f[:2] not in SKIP_PREFIX
               and "模块划分" not in f)   # 总纲不是模块文档，不按八章检查

_skipped = [f for f in os.listdir(SPEC)
            if re.match(r"\d{2}_", f) and f[:2] in SKIP_PREFIX]
if _skipped:
    print(f"[跳过非本会话文件] {_skipped}\n")

print(f"发现 {len(files)} 份模块文档\n")
summary = []
for fn in files:
    mod = fn[:2]
    path = os.path.join(SPEC, fn)
    t = open(path, encoding="utf-8").read()
    lines = t.count("\n") + 1
    errs = []
    notes = []

    # 1) 章节
    miss_sec = [s for s in SECTIONS if s not in t]
    if miss_sec:
        errs.append(f"缺章节 {len(miss_sec)}个: {[s.split('. ')[1] for s in miss_sec]}")

    # 2) 业务规则编号
    brs = sorted({int(x) for x in re.findall(rf"BR-{mod}-(\d{{2}})", t)})
    if not brs:
        errs.append("无 BR 业务规则编号")
    else:
        g = [n for n in range(1, max(brs) + 1) if n not in brs]
        if g:
            errs.append(f"BR 编号不连续，缺 {g}")

    # 3) 错误码：必须**定义**本模块前缀的码；引用别模块的码只作提示，不算错
    codes = sorted({c for c in re.findall(r"`([A-Z]{2,4}-\d{4})`", t)})
    own = [c for c in codes if c.startswith(PREFIX[mod] + "-")]
    foreign = [c for c in codes if not c.startswith(PREFIX[mod] + "-")]
    if not own:
        errs.append(f"未定义本模块错误码（应含 {PREFIX[mod]}-4xxx/5xxx）")
    if foreign:
        notes.append(f"引用他模块错误码（需为有意引用）: {foreign[:3]}")

    # 4) 验收项
    vs = sorted({int(x) for x in re.findall(rf"V-{mod}-(\d{{2}})", t)})
    if not vs:
        errs.append("无 V 验收项编号")

    # 5) 交叉引用真实性
    bad_e = sorted({f"E{n}" for n in re.findall(r"\bE(\d{2})\b", t)} - entities)
    bad_g = sorted(set(re.findall(r"\bG-\d{2}\b", t)) - gaps)
    bad_ad = sorted(set(re.findall(r"\bAD-\d{2}\b", t)) - ads)
    if bad_e:
        errs.append(f"引用不存在的实体 {bad_e}")
    if bad_g:
        errs.append(f"引用不存在的悬空点 {bad_g}")
    if bad_ad:
        errs.append(f"引用不存在的架构决策 {bad_ad}")

    # 6) 未完成痕迹（用真正的占位写法，不能拿"策略"里的"略"字误判）
    if re.search(r"TODO|待补充|待填写|此处省略|（略）|\(略\)|XXX|待完成|尚未撰写", t):
        m = re.search(r"TODO|待补充|待填写|此处省略|（略）|\(略\)|XXX|待完成|尚未撰写", t)
        errs.append(f"含未完成痕迹「{m.group(0)}」")

    summary.append((fn, lines, len(brs), len(own), len(vs), errs, notes))

print(f"{'文件':<40}{'行数':>6}{'BR':>5}{'错误码':>7}{'验收':>5}  状态")
print("-" * 90)
for fn, ln, br, cd, v, errs, notes in summary:
    st = "✔ 合规" if not errs else "✗ " + "; ".join(errs)[:60]
    print(f"{fn:<40}{ln:>6}{br:>5}{cd:>7}{v:>5}  {st}")

bad_total = sum(1 for s in summary if s[5])
print("\n" + "=" * 90)
print(f"合规 {len(summary) - bad_total} / {len(summary)} 份")
if bad_total:
    print("\n不合规明细：")
    for fn, ln, br, cd, v, errs, notes in summary:
        if errs:
            print(f"  【{fn}】")
            for e in errs:
                print(f"      - {e}")
for fn, ln, br, cd, v, errs, notes in summary:
    if notes:
        print(f"\n  · {fn}")
        for n in notes:
            print(f"      {n}")

allmods = [f"{i:02d}" for i in range(14)]
have = {f[:2] for f in files}
missing = [m for m in allmods if m not in have]
print(f"\n缺失模块 {len(missing)} 个: {missing}")
