#!/usr/bin/env python3
"""前后端接口一致性检查。

Web 前端最容易犯的一类错误：**调用的 HTTP 方法与后端注册的不一致**，
表现为运行时 405 Method Not Allowed —— 静态检查（AST/模板）查不出来。

本脚本把后端路由表与前端 api() 调用对照一遍，找出：

1. 方法不匹配（后端只注册 POST，前端却发 GET）
2. 前端调用了后端不存在的路径

用法::

    python tools/check_api_methods.py [项目根目录]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent

#: 装饰器名 -> HTTP 方法
VERB_MAP = {
    "get": {"GET"},
    "post": {"POST"},
    "put": {"PUT"},
    "patch": {"PATCH"},
    "delete": {"DELETE"},
}


def parse_backend_routes() -> dict[str, set[str]]:
    """解析 app/api/*.py 里的路由：{完整路径: {方法}}。"""
    routes: dict[str, set[str]] = {}

    for py in sorted((ROOT / "app" / "api").glob("*.py")):
        if py.name.startswith("__"):
            continue
        src = py.read_text(encoding="utf-8")

        # 路由前缀（可能省略）
        m = re.search(r'APIRouter\(prefix="([^"]*)"', src)
        prefix = m.group(1) if m else ""

        # 逐个装饰器解析。api_route 需要读 methods=[...]
        for dm in re.finditer(
            r'@router\.(\w+)\(\s*\n?\s*(?:f?)"([^"]*)"(.*?)(?=\n@|\ndef |\nasync def )',
            src,
            re.DOTALL,
        ):
            verb, path, tail = dm.group(1), dm.group(2), dm.group(3)
            full = prefix + path
            # 统一成 {param} 形式，便于比对
            full = re.sub(r"\{[^}]*\}", "{p}", full)

            if verb in VERB_MAP:
                methods = VERB_MAP[verb]
            elif verb == "api_route":
                mm = re.search(r"methods=\[([^\]]*)\]", tail)
                methods = (
                    set(x.strip().strip("\"'") for x in mm.group(1).split(","))
                    if mm
                    else set()
                )
            else:
                continue

            routes.setdefault(full, set()).update(methods)

    # main.py 里的系统路由（/api/health、/api/version 等）
    main_py = ROOT / "app" / "main.py"
    if main_py.is_file():
        src = main_py.read_text(encoding="utf-8")
        for dm in re.finditer(r'@app\.(\w+)\("([^"]+)"', src):
            verb, path = dm.group(1), dm.group(2)
            if verb in VERB_MAP:
                routes.setdefault(re.sub(r"\{[^}]*\}", "{p}", path), set()).update(
                    VERB_MAP[verb]
                )

    return routes


def parse_frontend_calls() -> set[tuple[str, str]]:
    """解析 static/index.html 里的 api(...) 调用。"""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    calls: set[tuple[str, str]] = set()

    # 两种写法都要覆盖：
    #   api('GET', '/api/xxx')
    #   api('PUT', '/api/rules/' + ruleEditor.id)     ← 字符串拼接
    #   api('PATCH', `/api/rules/${r.id}/toggle`)     ← 模板字符串
    for m in re.finditer(r"""api\(\s*['"](\w+)['"]\s*,\s*([^\n,]+?)\s*[,)]""", html):
        method = m.group(1).upper()
        expr = m.group(2)

        # 只处理以 /api/ 开头的表达式，跳过变量传入的情况
        if "/api/" not in expr:
            continue

        # 取第一个字符串字面量作为路径
        lit = re.search(r"""[`'"](/api/[^`'"]*)""", expr)
        if not lit:
            continue
        raw_lit = lit.group(0)
        path = lit.group(1)

        # 模板字符串里的 ${...} 已包含在字面量内部，直接归一化
        path = re.sub(r"\$\{[^}]*\}", "{p}", path)

        # 拼接写法（'/api/rules/' + id）说明后面还有动态值，补一段 {p}。
        # 注意：模板字符串（反引号）已自带 ${}，不能再补，否则会重复。
        is_template = raw_lit.startswith("`")
        trailing = expr[len(raw_lit):]
        if not is_template and re.search(r"\+\s*[\w.$]", trailing):
            if not path.rstrip("/").endswith("/{p}"):
                path = path.rstrip("/") + "/{p}"

        path = re.sub(r"\{[^}]*\}", "{p}", path)
        path = path.rstrip("/") or "/"
        calls.add((method, path))

    return calls


def main() -> int:
    routes = parse_backend_routes()
    calls = parse_frontend_calls()

    problems: list[str] = []

    for method, path in sorted(calls):
        if path in routes:
            if method not in routes[path]:
                problems.append(
                    f"方法不匹配: 前端 {method} {path}，"
                    f"后端仅支持 {','.join(sorted(routes[path])) or '(无)'}"
                )
            continue

        # 路径可能有动态段差异，做一次宽松匹配
        loose = [
            (p, ms) for p, ms in routes.items()
            if p.startswith(path.rstrip("/") + "/") or path.startswith(p.replace("{p}", ""))
        ]
        if loose:
            matched_any = any(method in ms for _p, ms in loose)
            if not matched_any:
                problems.append(
                    f"方法不匹配: 前端 {method} {path}，"
                    f"后端相近路由仅支持 "
                    f"{','.join(sorted(set().union(*(ms for _p, ms in loose))))}"
                )
            continue

        problems.append(f"后端缺失路由: 前端调用 {method} {path}")

    print(f"后端路由 {len(routes)} 条，前端调用 {len(calls)} 处")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗", p)
    else:
        print("✓ 前后端接口一致")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
