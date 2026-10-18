#!/usr/bin/env python3
"""FastAPI 路由装饰器的「启动期」静态检查。

背景：有一类错误 **静态检查抓不到、CI 也抓不到**，只在 ``uvicorn`` 真正
import 应用时才炸。典型例子：

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse | RedirectResponse:
        ...

FastAPI 会尝试把返回注解当 Pydantic 响应模型解析，于是启动即报：

    fastapi.exceptions.FastAPIError: Invalid args for response field!
    Hint: check that ... is a valid Pydantic field type ...

本脚本在没有安装 fastapi 的环境里，用 AST 复现「FastAPI 会检查哪些注解」，
把这类问题提前到提交前发现。

规则（与 FastAPI 实际行为对齐）：

* 只要返回注解里出现 ``Response`` 或其子类（FileResponse / JSONResponse /
  RedirectResponse / StreamingResponse / PlainTextResponse ...），
  就必须显式给 ``response_model=None``，否则会被当成 Pydantic 模型解析。
* 返回注解是 ``dict`` / ``list`` / ``None`` / Pydantic 模型的，正常。

用法::

    python tools/check_fastapi.py [项目根目录]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent

# Response 家族：FastAPI 不会把它们当 Pydantic 模型，但也因此
# 如果只写了注解而没给 response_model=None，FastAPI 会尝试解析并失败。
RESPONSE_TYPES = {
    "Response",
    "FileResponse",
    "HTMLResponse",
    "JSONResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "StreamingResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "Response_",  # 兜底
}

ROUTE_DECORATORS = {
    "get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route",
}


def decorator_name(dec: ast.AST) -> str | None:
    """把 ``@app.get(...)`` / ``@router.post(...)`` 解析出方法名。"""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if isinstance(func, ast.Attribute) and func.attr in ROUTE_DECORATORS:
        return func.attr
    return None


def has_response_model_none(dec: ast.Call) -> bool:
    """检查装饰器是否显式设置了 ``response_model=None``。"""
    for kw in dec.keywords:
        if kw.arg == "response_model":
            return isinstance(kw.value, ast.Constant) and kw.value.value is None
    return False


def annotation_mentions_response(node: ast.AST | None) -> str | None:
    """返回注解中出现的 Response 类型名（若有）。"""
    if node is None:
        return None
    for child in ast.walk(node):
        name: str | None = None
        if isinstance(child, ast.Name):
            name = child.id
        elif isinstance(child, ast.Attribute):
            name = child.attr
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # 字符串注解，如 -> "Response"
            for token in RESPONSE_TYPES:
                if token in child.value:
                    return token
        if name and name in RESPONSE_TYPES:
            return name
    return None


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path.name}:{exc.lineno} 语法错误: {exc.msg}"]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for dec in node.decorator_list:
            method = decorator_name(dec)
            if method is None:
                continue
            assert isinstance(dec, ast.Call)

            ret = annotation_mentions_response(node.returns)
            if ret and not has_response_model_none(dec):
                problems.append(
                    f"{path.name}:{node.lineno} 路由 {node.name}() 的返回注解含 "
                    f"{ret}，但装饰器未设置 response_model=None —— "
                    f"FastAPI 启动时会把它当 Pydantic 模型解析并抛 FastAPIError"
                )
            break

    # 额外检查：装饰器参数里可能传了不合法的 response_model
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if decorator_name(node) is None:
            continue
        for kw in node.keywords:
            if kw.arg == "response_model" and isinstance(kw.value, ast.Name):
                if kw.value.id in RESPONSE_TYPES:
                    problems.append(
                        f"{path.name}:{node.lineno} response_model 不能是 "
                        f"Response 类型（{kw.value.id}）"
                    )

    return problems


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    files = sorted(
        p for p in target.rglob("*.py") if "__pycache__" not in str(p)
    )

    problems: list[str] = []
    route_count = 0

    for path in files:
        for line in check_file(path):
            problems.append(line)
        # 统计路由数量，确认确实扫到了东西
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(decorator_name(d) for d in node.decorator_list):
                    route_count += 1

    print(f"检查 {len(files)} 个 Python 文件，发现 {route_count} 个路由处理函数")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗", p)
    else:
        print("✓ FastAPI 路由检查通过")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
