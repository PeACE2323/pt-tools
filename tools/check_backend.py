#!/usr/bin/env python3
"""后端源码静态自检（无第三方依赖也能跑）。

在没有安装 fastapi / sqlalchemy 的环境里，``import`` 验证是不可行的，
但仍然可以做很多有价值的静态检查：

1. 语法（ast.parse）
2. **未定义的全局名**：例如漏 import、拼错函数名（这类错误只在运行时暴露）
3. **未使用的导入**：死代码
4. 路由前缀与 ``main.py`` 注册的一致性
5. 前端调用路径与后端路由的对应关系

用法::

    python tools/check_backend.py [项目根目录]
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


class ModuleCollector(ast.NodeVisitor):
    """收集模块内定义的全局名。"""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self.names.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        """收集函数参数名。

        不用 ``visit_arguments``：``ast.arguments`` 不是 NodeVisitor 的标准
        分派目标，不同 Python 版本下 generic_visit 是否触达它并不一致，
        依赖它会导致跨版本结果不同（曾在 3.11 上误报“未定义名字”）。
        """
        self.names.add(node.arg)
        self.generic_visit(node)


def collect_module_names(tree: ast.Module) -> set[str]:
    collector = ModuleCollector()
    collector.visit(tree)
    return collector.names


def collect_all_scoped_names(tree: ast.AST) -> set[str]:
    """递归收集所有作用域里出现的名字（更宽松，用于判定“是否未定义”）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            a = node.args if hasattr(node, "args") else None
            if a is not None:
                for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                    names.add(arg.arg)
                if a.vararg:
                    names.add(a.vararg.arg)
                if a.kwarg:
                    names.add(a.kwarg.arg)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            names.update(node.names)
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_unused_imports(tree: ast.Module, source: str) -> list[str]:
    """检测未使用的 import。

    跳过：
    * ``from __future__ import ...``（编译器指令，本就不是“引用”）
    * 带 ``noqa`` 的行（显式豁免）
    """
    problems: list[str] = []
    imported: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append((alias.asname or alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # __future__ 是编译指令，不产生运行时引用
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported.append((alias.asname or alias.name, node.lineno))

    lines = source.splitlines()
    body_names = collect_all_scoped_names(tree)

    for name, lineno in imported:
        if len(re.findall(rf"\b{re.escape(name)}\b", source)) <= 1:
            line_noqa = lines[lineno - 1] if lineno - 1 < len(lines) else ""
            if "noqa" in line_noqa:
                continue
            problems.append(f"第 {lineno} 行: 未使用的导入 {name}")
        elif name not in body_names:
            problems.append(f"第 {lineno} 行: 导入 {name} 未被引用")
    return problems


def check_undefined_names(tree: ast.Module, module_names: set[str], path: Path) -> list[str]:
    """检测使用了但未定义也未导入的全局名。

    只检查模块顶层函数的函数体（不含嵌套作用域的误判），
    并对属性访问、方法调用一律跳过。
    """
    problems: list[str] = []
    allowed = module_names | set(dir(builtins)) | {"annotations"}

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = set(collect_all_scoped_names(node))
        for child in ast.walk(node):
            if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load):
                continue
            name = child.id
            if name in local or name in allowed:
                continue
            problems.append(
                f"{path.name}:{child.lineno} 函数 {node.name} 中使用了未定义的名字 {name!r}"
            )
    # 去重
    return sorted(set(problems))


def check_routes() -> list[str]:
    """检查路由前缀与 main.py 注册是否一致，并列出全部 API 路径。"""
    problems: list[str] = []
    prefixes: dict[str, str] = {}

    for py in sorted((APP / "api").glob("*.py")):
        if py.name.startswith("__"):
            continue
        src = py.read_text(encoding="utf-8")
        m = re.search(r'APIRouter\(prefix="([^"]+)"', src)
        if m:
            prefixes[py.stem] = m.group(1)

    main_src = (APP / "main.py").read_text(encoding="utf-8")
    for module in prefixes:
        if f"{module}.router" not in main_src:
            problems.append(f"main.py 未注册 {module}.router")

    # 收集所有 endpoint 路径
    routes: list[str] = []
    for py in sorted((APP / "api").glob("*.py")):
        if py.name.startswith("__"):
            continue
        src = py.read_text(encoding="utf-8")
        prefix = prefixes.get(py.stem, "")
        for m in re.finditer(r'@router\.(get|post|put|patch|delete)\(\s*\n?\s*"([^"]*)"', src):
            method, sub = m.group(1), m.group(2)
            routes.append(f"{method.upper():6} {prefix}{sub}")

    print(f"\n共 {len(routes)} 个 API 路由：")
    for r in sorted(set(routes)):
        print("  ", r)

    return problems


def check_frontend_api_calls() -> list[str]:
    """前端调用的 API 路径必须都能在后端找到。"""
    problems: list[str] = []
    ui = ROOT / "static" / "index.html"
    if not ui.exists():
        return problems

    html = ui.read_text(encoding="utf-8")
    calls = set(re.findall(r"api\(\s*'(\w+)'\s*,\s*[`'\"]([^`'\"]+)", html))

    # 收集后端路由（含动态段）
    backend: set[tuple[str, str]] = set()
    for py in sorted((APP / "api").glob("*.py")):
        if py.name.startswith("__"):
            continue
        src = py.read_text(encoding="utf-8")
        m = re.search(r'APIRouter\(prefix="([^"]+)"', src)
        prefix = m.group(1) if m else ""
        for mm in re.finditer(r'@router\.(get|post|put|patch|delete)\(\s*\n?\s*"([^"]*)"', src):
            backend.add((mm.group(1).upper(), prefix + mm.group(2)))

    # main.py 里的系统路由
    main_src = (APP / "main.py").read_text(encoding="utf-8")
    for mm in re.finditer(r'@app\.(get|post)\("([^"]+)"', main_src):
        backend.add((mm.group(1).upper(), mm.group(2)))

    for method, path in sorted(calls):
        # 前端的动态段可能是模板字符串或 + 号拼接，归一化后再比对
        base = path.split("?")[0]
        base = re.sub(r"\$\{[^}]*\}", "{id}", base)
        base = re.sub(r"/+$", "", base)

        found = False
        for bm, bp in backend:
            if bm != method.upper():
                continue
            bp_norm = re.sub(r"\{[^}]+\}", "{id}", bp)
            # 前端 "/api/rules/" + id 会拼成 "/api/rules/"，
            # 归一化后与 "/api/rules/{id}" 的前缀一致，需要双向容忍
            if (
                bp_norm == base
                or base.startswith(bp_norm)
                or bp_norm.startswith(base + "/")
                or bp_norm.startswith(base)
            ):
                found = True
                break
        if not found:
            problems.append(f"前端调用 {method.upper()} {path} 在后端未找到对应路由")

    print(f"\n前端共调用 {len(calls)} 个 API 端点")
    return problems


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    problems: list[str] = []
    py_files = sorted(p for p in target.rglob("*.py") if "__pycache__" not in str(p))

    print(f"检查 {len(py_files)} 个 Python 文件\n")

    for path in py_files:
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(target)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            problems.append(f"{rel}:{exc.lineno} 语法错误: {exc.msg}")
            continue

        if path.name.startswith("__"):
            continue

        module_names = collect_module_names(tree)
        for p in check_unused_imports(tree, source):
            problems.append(f"{rel}: {p}")
        for p in check_undefined_names(tree, module_names, path):
            if path.name != "config.py":  # config 里大量常量互相引用
                problems.append(p.replace(str(path), str(rel)))

    problems.extend(check_routes())
    problems.extend(check_frontend_api_calls())

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗", p)
        print(f"\n[debug] Python {sys.version.split()[0]} on {sys.platform}")
    else:
        print("✓ 后端静态检查通过")
    return 1 if problems else 0


def _write_report(text: str) -> None:
    """把结果写到文件，CI 里作为 artifact 上传。

    无论正常还是异常都写，保证问题可定位。
    """
    try:
        out = ROOT / "backend_check_report.txt"
        out.write_text(text, encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    import traceback

    try:
        code = main()
    except BaseException:  # noqa: BLE001 - 兜底：把 traceback 也落到报告里
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        _write_report(
            "check_backend.py 崩溃\n"
            f"Python {sys.version}\nplatform={sys.platform}\n\n{detail}"
        )
        raise SystemExit(2)
    else:
        if code != 0:
            _write_report("后端静态检查未通过（详见 stdout）\n")
        raise SystemExit(code)
