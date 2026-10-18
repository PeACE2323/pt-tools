#!/usr/bin/env python3
"""前端单页 WebUI 的静态自检。

无需浏览器即可发现常见问题：

1. ``<script>`` 块本身的语法（交给 node --check，见 run_all 的说明）；
2. Vue 模板里引用了但 ``setup()`` 没有 return 的标识符（运行时才报错，很难查）；
3. ``setup()`` 里定义了但模板从未使用的死代码；
4. HTML 标签配对。

用法::

    python tools/check_ui.py static/index.html
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


# Vue 内置与 JS 关键字，出现在模板里不算未定义
BUILTINS = {
    "true", "false", "null", "undefined", "Math", "Number", "String", "Object",
    "Array", "JSON", "Date", "console", "window", "document", "fetch",
    "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "decodeURIComponent",
    "length", "toFixed", "toUpperCase", "toLowerCase", "filter", "map", "join",
    "split", "trim", "slice", "indexOf", "push", "forEach", "includes",
    "padStart", "round", "floor", "ceil", "max", "min", "abs", "now",
    "value", "key", "item", "index", "e", "t", "r", "l", "m", "u", "n", "s",
    "c", "d", "p", "j", "k", "v", "f", "g", "x", "y", "q", "w", "$event",
    "if", "else", "in", "of", "new", "typeof", "return",
}

TAG_RE = re.compile(r"</?([A-Za-z][\w-]*)")
IDENT_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\b")
MUSTACHE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
BINDING_RE = re.compile(
    r'(?:v-if|v-else-if|v-for|v-show|v-model|v-bind|:[\w-]+|@[\w.-]+|v-on:[\w-]+)\s*=\s*"([^"]*)"'
)
VOID_TAGS = {
    "input", "img", "br", "hr", "meta", "link", "source", "area", "base",
    "col", "embed", "param", "track", "wbr",
}


def extract_script(text: str) -> str:
    m = re.search(r"<script>\s*(const \{ createApp.*?)</script>", text, re.DOTALL)
    return m.group(1) if m else ""


def template_section(text: str) -> str:
    """取 <div id="app"> 到其后第一个 <script> 之间的内容（即 Vue 模板区）。

    不能截到文件末尾，否则会把 <script>/<style> 当成模板标签参与配对检查，
    产生“未闭合标签”之类的误报。
    """
    start = text.find('<div id="app"')
    if start < 0:
        return ""
    tail = text[start:]
    # 模板区在下一个 <script> 处结束
    cut = tail.find("<script")
    if cut > 0:
        tail = tail[:cut]
    # 兜底：去掉末尾的 modal 之外的注释
    return tail


def returned_identifiers(script: str) -> set[str]:
    """解析 setup() 末尾的 ``return {...}`` 块，得到暴露给模板的名字。"""
    idx = script.rfind("return {")
    if idx < 0:
        return set()
    end = script.find("};", idx)
    if end < 0:
        return set()
    block = script[idx + len("return {"): end]
    names: set[str] = set()
    for raw in block.split(","):
        token = raw.strip()
        if not token:
            continue
        # 支持 "a, b, c" / "x: y" / "x," 形式
        if ":" in token:
            token = token.split(":")[-1]
        token = token.strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", token):
            names.add(token)
    return names


def defined_identifiers(script: str) -> set[str]:
    """粗提取 setup 内用 const/let/function 定义的名字。"""
    names: set[str] = set()
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", script):
        names.add(m.group(1))
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)", script):
        names.add(m.group(1))
    # 解构 const { a, b } = ...
    for m in re.finditer(r"\b(?:const|let|var)\s*\{([^}]*)\}", script):
        for part in m.group(1).split(","):
            name = part.split(":")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                names.add(name)
    return names


def check_html_tags(template: str) -> list[str]:
    errors: list[str] = []
    stack: list[str] = []
    for m in TAG_RE.finditer(template):
        raw = m.group(0)
        tag = m.group(1).lower()
        if tag in VOID_TAGS:
            continue
        if raw.startswith("</"):
            if not stack:
                errors.append(f"多余的结束标签 </{tag}>")
            elif stack[-1] != tag:
                errors.append(f"标签不匹配：期望 </{stack[-1]}>，遇到 </{tag}>")
                if tag in stack:
                    while stack and stack[-1] != tag:
                        stack.pop()
                    if stack:
                        stack.pop()
            else:
                stack.pop()
        else:
            # 自闭合
            close = template.rfind(">", m.end())
            segment = template[m.end():close if close > 0 else m.end() + 200]
            if not segment.rstrip().endswith("/"):
                stack.append(tag)
    if stack:
        errors.append(f"未闭合标签: {stack[:5]}")
    return errors


def check(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")

    script = extract_script(text)
    if not script:
        return ["未找到 <script> 中的 createApp 代码块"]

    template = template_section(text)
    if not template:
        return ["未找到 <div id=\"app\"> 模板区"]

    returned = returned_identifiers(script)
    defined = defined_identifiers(script)

    # ---- 收集模板里出现的标识符 ----
    used: set[str] = set()

    def collect(expr: str) -> None:
        """收集表达式里的顶层标识符。

        需要排除 ``$event.target.value`` 这类 **属性访问链** 中的
        ``target``/``value`` —— 它们不是 setup() 需要暴露的名字，
        否则会产生误报。
        """
        # 去掉所有 .prop 与 $event 派生访问
        cleaned = re.sub(r"\.\s*[A-Za-z_$][\w$]*", "", expr)
        cleaned = cleaned.replace("$event", "")
        # 去掉对象字面量的键名 key:
        cleaned = re.sub(r"\b[A-Za-z_$][\w$]*\s*:(?!:)", "", cleaned)
        used.update(IDENT_RE.findall(cleaned))

    for m in MUSTACHE_RE.finditer(template):
        collect(m.group(1))
    for m in BINDING_RE.finditer(template):
        collect(m.group(1))

    # v-for 的循环变量是局部作用域，不算未定义
    local_vars: set[str] = set()
    for m in re.finditer(r'v-for="\(?\s*([^)"]*?)\s*\)?\s+in\s', template):
        for part in m.group(1).split(","):
            name = part.strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                local_vars.add(name)

    # 属性名本身不算（如 :title 里的 title）——上面正则只取引号内，已规避

    missing = sorted(
        name for name in used
        if name not in returned
        and name not in BUILTINS
        and name not in local_vars
        and not name.isdigit()
        and name not in {"template"}
    )
    # 过滤掉明显的 CSS/HTML 词与属性后缀
    noise = {
        "px", "em", "rem", "vw", "vh", "auto", "none", "block", "flex", "grid",
        "w", "h", "px", "text", "bg", "border", "rounded", "shadow", "font",
        "size", "duration", "transition", "opacity", "cursor", "pointer",
        "timeout", "limit", "encode", "width", "height", "style", "class",
        "plaintext", "javascript", "button", "submit", "checkbox", "number",
        "password", "text", "string", "error", "info", "warning", "success",
        "visible", "hidden", "absolute", "relative", "fixed", "sticky",
        "group", "hover", "focus", "active", "disabled", "checked", "model",
        "change", "input", "click", "keyup", "enter", "self", "prevent", "stop",
        "truncate", "break", "hidden", "sr", "only", "whitespace", "nowrap",
        "uppercase", "mono", "tracking", "leading", "align", "justify",
        "between", "center", "items", "gap", "space", "shrink", "grow",
    }
    missing = [n for n in missing if n not in noise and len(n) > 1]

    # 只报告“看起来像业务变量”的（在 defined 里存在但没 return 的最可疑）
    suspicious = [n for n in missing if n in defined or n in {
        "dash", "rules", "liveTasks", "form", "settings", "events",
    }]
    if suspicious:
        errors.append(
            "模板引用了 setup() 未 return 的标识符: " + ", ".join(suspicious)
        )

    # ---- 死代码：定义了但既没 return 也没在 script 内部使用 ----
    unused = []
    for name in sorted(defined - returned):
        # 排除局部临时变量（在 script 中出现超过 1 次说明内部在用）
        occurrences = len(re.findall(rf"\b{re.escape(name)}\b", script))
        if occurrences <= 1 and not name.startswith("_"):
            unused.append(name)
    if unused:
        errors.append("疑似未使用的定义: " + ", ".join(unused[:10]))

    # ---- HTML 标签配对 ----
    errors.extend(check_html_tags(template))

    # ---- 基础存在性 ----
    for token in ("createApp", "mount('#app')", "v-cloak"):
        if token not in text:
            errors.append(f"缺少必需片段: {token}")

    print(f"检查文件: {path}")
    print(
        f"  模板引用 {len(used)} 个标识符 / setup 暴露 {len(returned)} 个 / "
        f"定义 {len(defined)} 个"
    )
    if errors:
        print(f"  发现 {len(errors)} 个问题:")
        for e in errors:
            print("   ✗", e)
    else:
        print("  ✓ 静态检查通过")
    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    return 1 if check(Path(sys.argv[1])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
