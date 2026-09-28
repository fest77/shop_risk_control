# -*- coding: utf-8 -*-
"""可维护性 / 规范性自审（测试覆盖不到的部分）。

用法：  .venv\\Scripts\\python.exe scripts\\self_audit.py

检查项：
  1. 未使用的 import（粗粒度：名字在文件里只出现于 import 行）
  2. 前端 HTML 的 id 与 JS 里 $(\\'id\\') 的引用是否一一对得上（最容易漏的一类 bug）
  3. OpenAPI 文档是否覆盖了所有已实现端点
  4. 是否存在遗留的调试代码（print / pdb / TODO / FIXME）
  5. 是否硬编码了密钥类字面量

退出码：0 = 全部通过；1 = 发现问题（可用于 CI）
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SELF = Path(__file__).name
problems: list[str] = []
notes: list[str] = []


def _py_files() -> list[Path]:
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", ".idea")]
        out += [Path(root) / f for f in files if f.endswith(".py")]
    return sorted(out)


def check_unused_imports(files: list[Path]) -> None:
    print("=" * 76)
    print("1. 未使用的 import")
    print("=" * 76)
    for path in files:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines()
        imported: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [((a.asname or a.name).split(".")[0], node.lineno)
                             for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported += [(a.asname or a.name, node.lineno)
                             for a in node.names if a.name != "*"]
        for name, lineno in imported:
            if name == "annotations":
                # `from __future__ import annotations` 是语言开关，不是普通导入
                continue
            # 显式 `# noqa` 表示作者有意保留（例如导入即注册路由的副作用导入）：
            # 这类导入被删掉往往是静默故障，因此必须允许显式豁免，而不是逼作者绕过检查
            if "noqa" in lines[lineno - 1].lower():
                continue
            if len(re.findall(rf"\b{re.escape(name)}\b", src)) <= 1:
                problems.append(
                    f"未使用的 import：{path.relative_to(ROOT)}:{lineno} -> {name}")
    print(f"  扫描 {len(files)} 个 py 文件")


def check_frontend_ids() -> None:
    print("\n" + "=" * 76)
    print("2. 前端 id 引用与视图路径一致性")
    print("=" * 76)
    html_path = ROOT / "static" / "index.html"
    js_dir = ROOT / "static" / "js"
    if not html_path.exists() or not js_dir.is_dir():
        notes.append("未找到前端文件，跳过前端一致性检查")
        return
    html = html_path.read_text(encoding="utf-8")
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_files = sorted(js_dir.rglob("*.js"))
    refs: dict[str, set[str]] = {}
    for f in js_files:
        txt = f.read_text(encoding="utf-8")
        ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", txt))
        ids |= set(re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]", txt))
        if ids:
            refs[str(f.relative_to(ROOT))] = ids

    total = sum(len(v) for v in refs.values())
    print(f"  HTML 定义 id {len(html_ids)} 个；JS 引用 {total} 个（{len(refs)} 个文件）")
    missing: list[str] = []
    for fname, ids in sorted(refs.items()):
        lack = sorted(i for i in ids if i not in html_ids)
        if lack:
            missing.append(f"{fname} -> {lack}")
    if missing:
        problems.append(f"JS 引用了 HTML 里不存在的 id：{missing}")
    else:
        print("  ✅ JS 引用的 id 全部存在于 HTML")

    # **所有**相对 import / 动态 import 必须能解析到真实文件。
    # 写错一层目录（`./ui.js` 而不是 `../ui.js`）在浏览器里只表现为 404 +
    # "Failed to fetch dynamically imported module"，而 `node --check` 只查语法、
    # 接口 curl 只看状态码，两者都发现不了——必须由这条静态检查兜住。
    bad_imports: list[str] = []
    spec_re = re.compile(r"""(?:from\s+|import\s*\(\s*)['"](\.[^'"]+)['"]""")
    for f in js_files:
        for m in spec_re.finditer(f.read_text(encoding="utf-8")):
            target = (f.parent / m.group(1)).resolve()
            if not target.exists():
                bad_imports.append(f"{f.relative_to(ROOT)} -> {m.group(1)}")
    if bad_imports:
        problems.append(f"前端相对导入无法解析：{bad_imports}")
    else:
        print(f"  ✅ 前端 {len(js_files)} 个 JS 文件的相对导入全部可解析")


def check_openapi() -> None:
    print("\n" + "=" * 76)
    print("3. OpenAPI 文档覆盖")
    print("=" * 76)
    from app.main import app  # noqa: PLC0415

    paths = app.openapi().get("paths", {})
    expected = {
        "/api/v1/lists": {"get", "post"},
        "/api/v1/lists/scene-usage": {"get"},
        "/api/v1/lists/impact": {"get"},
        "/api/v1/lists/import": {"post"},
        "/api/v1/lists/import-template": {"get"},
        "/api/v1/lists/{entry_id}": {"delete"},
        "/api/v1/common/enums": {"get"},
        "/api/v1/common/meta": {"get"},
        "/api/v1/auth/login": {"post"},
        "/api/v1/auth/me": {"get"},
        "/api/v1/auth/logout": {"post"},
        "/api/v1/auth/password": {"post"},
        "/api/v1/audit/logs": {"get"},
        "/api/v1/audit/verify": {"get"},
        "/api/v1/audit/export": {"get"},
        "/api/v1/audit/actors": {"get"},
        "/api/v1/metrics/overview": {"get"},
        "/api/v1/metrics/trend": {"get"},
        "/api/v1/metrics/distribution": {"get"},
        "/api/v1/metrics/rule-ranking": {"get"},
        "/api/v1/metrics/throughput": {"get"},
        "/api/v1/metrics/stream": {"get"},
        "/api/v1/metrics/rollup": {"post"},
        "/api/v1/events": {"post"},
        "/api/v1/events/batch": {"post"},
        "/api/v1/events/{event_id}": {"get"},
        "/api/v1/features/meta": {"get"},
        "/api/v1/features/{event_id}": {"get"},
        # 模块 05（规则决策引擎）：单条事件决策求值（仿真/调试，权限 `sim:run`）
        "/api/v1/engine/evaluate": {"post"},
        # 模块 06-B（规则配置）：规则 CRUD / 启停用 / 条件树校验 / 批量导入
        # （权限均为 `rule:write`）
        "/api/v1/rules": {"get", "post"},
        "/api/v1/rules/validate-tree": {"post"},
        "/api/v1/rules/import": {"post"},
        "/api/v1/rules/import-template": {"get"},
        "/api/v1/rules/{rule_code}": {"get", "put", "delete"},
        "/api/v1/rules/{rule_code}/toggle": {"post"},
        "/api/v1/rules/{rule_code}/impact": {"get"},
        # 模块 09（画像与关联图谱）：两个查询接口，权限均为 `case:read`
        "/api/v1/profiles/{user_id}": {"get"},
        "/api/v1/graph/{entity_type}/{entity_id}": {"get"},
        # 模块 08（案件处置与业务联动）：处置类端点权限 `case:dispose`（仅 reviewer），
        # 读端点权限 `case:read`；`archive` 借用 `sys:config`（仅 admin，见 case_api 的说明）
        "/api/v1/cases/{case_no}/claim": {"post"},
        "/api/v1/cases/{case_no}/dispose/preview": {"post"},
        "/api/v1/cases/{case_no}/dispose": {"post"},
        "/api/v1/cases/{case_no}/biz-sync/retry": {"post"},
        "/api/v1/cases/{case_no}/archive": {"post"},
        "/api/v1/cases/{case_no}/actions": {"get"},
        # 模块 07（案件审核工作台）的**读侧**（裁定 D67：列表与详情归 07）：
        # 权限 `case:read`（仅 reviewer，冻结矩阵 D69）。router 定义在
        # `app/api/case_query_api.py`，登记为模块编号 `07`。
        "/api/v1/cases": {"get"},
        "/api/v1/cases/{case_no}": {"get"},
        "/api/v1/mock/start": {"post"},
        "/api/v1/mock/stop": {"post"},
        "/api/v1/mock/status": {"get"},
        # 模块 10（事件仿真测试）：权限均为 `sim:run`（reviewer + strategist）。
        # `POST /sim/run` 是**核心接口**——它复用 05 的 `decide()` 而不是另写判定，
        # 且默认 `affect_window=false`（仿真不写 04 的真实特征窗口，BR-10-06）。
        "/api/v1/sim/cases": {"get", "post"},
        "/api/v1/sim/run": {"post"},
        "/api/v1/sim/batch": {"post"},
        "/api/v1/sim/runs/{run_id}": {"get"},
        # 模块 13（系统设置与运行参数）：运行参数（`sys:config`）、吞吐健康度
        # （`sys:config`）、账号与角色（`account:manage`）、决策引擎配置
        # （`engine:config`）。三者都取自权限矩阵现有码，未新增权限串
        "/api/v1/system/config": {"get", "put"},
        "/api/v1/system/stats": {"get"},
        "/api/v1/system/users": {"get", "post"},
        "/api/v1/system/users/{username}": {"get", "put", "delete"},
        "/api/v1/system/users/{username}/disable": {"post"},
        "/api/v1/system/users/{username}/enable": {"post"},
        "/api/v1/system/users/{username}/reset-password": {"post"},
        "/api/v1/system/engine-config": {"get", "put"},
        "/health": {"get"},
    }
    for p, methods in expected.items():
        got = set(paths.get(p, {}).keys())
        ok = methods.issubset(got)
        print(f"  {'✅' if ok else '❌'} {p:<28} 期望 {sorted(methods)} 实际 {sorted(got)}")
        if not ok:
            problems.append(f"OpenAPI 缺少端点：{p} 期望 {sorted(methods)} 实际 {sorted(got)}")
    print(f"  OpenAPI 共 {len(paths)} 条路径")


def check_debug_residue(files: list[Path]) -> None:
    print("\n" + "=" * 76)
    print("4. 调试残留与占位")
    print("=" * 76)
    pats = [r"\bprint\(", r"\bbreakpoint\(", r"\bpdb\.", r"\bTODO\b",
            r"\bFIXME\b", r"NotImplementedError"]
    hits = []
    for path in files:
        rel = path.relative_to(ROOT)
        # 只审**运行时应用代码**（app/）。tests/ 与 scripts/ 是测试与 CLI 工具，
        # 那里的 print 就是它们的输出接口，不应判为调试残留。
        if rel.parts[0] != "app":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pat in pats:
                if re.search(pat, line):
                    hits.append(f"{rel}:{i}  {line.strip()[:70]}")
    for h in hits:
        print("  ⚠", h)
    if hits:
        problems.append(f"应用代码中存在 {len(hits)} 处调试残留/TODO/占位")
    else:
        print("  ✅ 应用代码无 print/breakpoint/TODO/FIXME/占位")


def check_hardcoded_secrets(files: list[Path]) -> None:
    print("\n" + "=" * 76)
    print("5. 硬编码密钥扫描")
    print("=" * 76)
    # 值至少 6 个字符：`long_password = "A" * n` 这类表达式里的单字符字面量
    # 不是凭据，收紧后不再误报——误报会让人逐渐无视这条检查，比漏报更危险。
    pat = re.compile(
        r"(sk-[A-Za-z0-9]{12,}"
        r"|[A-Za-z0-9_]*password\s*=\s*['\"][^'\"]{6,}['\"]"
        r"|[A-Za-z0-9_]*secret\s*=\s*['\"][^'\"]{6,}['\"]"
        r"|[A-Za-z0-9_]*api[_-]?key\s*=\s*['\"][^'\"]{6,}['\"])",
        re.IGNORECASE)
    # 第三方 vendor 包（echarts.min.js）是压缩产物，其内部字符串不属于本项目代码，
    # 扫描它只会产生噪声（例如它自带的构建脚本片段），故排除。
    frontend = [p for p in (ROOT / "static").rglob("*")
                if p.suffix in (".html", ".js", ".css") and "vendor" not in p.parts]
    targets = list(files) + frontend
    hits = []
    for path in targets:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace")
                                 .splitlines(), 1):
            # 显式豁免：用于"故意使用弱密钥/假密钥来验证拦截逻辑"的用例。
            # 必须写明理由，避免变成随手加的免检开关。
            if "noqa" in line.lower():
                continue
            if pat.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}")
    if hits:
        problems.append(f"疑似硬编码密钥：{hits}")
    else:
        print("  ✅ 未发现硬编码密钥（配置全部来自环境变量）")


def main() -> int:
    files = _py_files()
    check_unused_imports(files)
    check_frontend_ids()
    check_openapi()
    check_debug_residue(files)
    check_hardcoded_secrets(files)

    print("\n" + "=" * 76)
    print("审计结论")
    print("=" * 76)
    if notes:
        print("提示（非缺陷）：")
        for n in notes:
            print("  ·", n)
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n✅ 全部审计项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
