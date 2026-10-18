#!/usr/bin/env python3
"""CI 环境诊断：定位跨平台（Windows 本地 vs Linux CI）差异。

检查三件事：

1. 每个源文件的**字节级**特征：BOM、CRLF、首尾不可见字符
2. 用 ``ast.parse`` 逐文件解析，报告语法错误
3. 打印 Python 版本与平台，便于对照

用法::

    python tools/diag_env.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUFFIXES = {".py", ".html", ".proto", ".md", ".txt", ".yml", ".yaml", ".sh"}


def main() -> int:
    print(f"python: {sys.version.split()[0]}")
    print(f"platform: {sys.platform}")
    print(f"root: {ROOT}")
    print()

    files = sorted(
        p for p in ROOT.rglob("*")
        if p.is_file()
        and p.suffix in SUFFIXES
        and "__pycache__" not in str(p)
        and ".git" not in p.parts
    )
    print(f"扫描 {len(files)} 个文本文件")

    bom_files: list[str] = []
    crlf_files: list[str] = []
    parse_errors: list[str] = []

    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        try:
            data = p.read_bytes()
        except OSError as exc:
            print(f"  ! 读取失败 {rel}: {exc}")
            continue

        if data.startswith(b"\xef\xbb\xbf"):
            bom_files.append(rel)
        if b"\r\n" in data:
            crlf_files.append(rel)

        if p.suffix == ".py":
            try:
                ast.parse(data.decode("utf-8"), filename=rel)
            except SyntaxError as exc:
                parse_errors.append(f"{rel}:{exc.lineno} {exc.msg}")
            except UnicodeDecodeError as exc:
                parse_errors.append(f"{rel}: 非 UTF-8 编码 {exc}")

    print()
    print(f"BOM 文件 ({len(bom_files)}):")
    for f in bom_files:
        print("  -", f)
    if not bom_files:
        print("  (无)")

    print()
    # .gitattributes 已对 *.sh 指定 eol=lf，其它带 CRLF 只作提示
    shell_crlf = [f for f in crlf_files if f.endswith(".sh")]
    print(f"CRLF 文件 ({len(crlf_files)}，其中 .sh {len(shell_crlf)}):")
    if shell_crlf:
        print("  !! 以下 shell 脚本含 CRLF，容器内会 bad interpreter:")
        for f in shell_crlf:
            print("   -", f)
    for f in crlf_files[:10]:
        if not f.endswith(".sh"):
            print("  -", f)
    if len(crlf_files) > 10:
        print(f"  ... 另有 {len(crlf_files) - 10} 个")
    if not crlf_files:
        print("  (无)")

    print()
    print(f"语法错误 ({len(parse_errors)}):")
    for e in parse_errors:
        print("  -", e)
    if not parse_errors:
        print("  (无)")

    return 1 if (parse_errors or shell_crlf) else 0


if __name__ == "__main__":
    raise SystemExit(main())
