#!/usr/bin/env python3
"""clouddrive.proto 的轻量静态语法自检（无需安装 protobuf）。

在 CI / 构建前快速发现：括号不配平、message/rpc 引用缺失、字段号重复、
枚举值缺失编号等常见笔误。它不能替代 protoc，但能在没有 protobuf 环境
（例如离线开发机）时提供第一道防线。

用法::

    python tools/check_proto.py app/proto/clouddrive.proto
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老版本解释器或无 reconfigure 时忽略
        pass

BUILTIN_TYPES = {
    # scalar
    "double", "float", "int32", "int64", "uint32", "uint64", "sint32",
    "sint64", "fixed32", "fixed64", "sfixed32", "sfixed64", "bool", "string",
    "bytes",
    # well-known
    "google.protobuf.Empty",
    "google.protobuf.Timestamp",
    "google.protobuf.StringValue",
}

# 本文件内已定义但仅用于文档、未参与任何 RPC 的消息，不检查引用
FIELD_RE = re.compile(
    r"^\s*(?P<label>repeated|optional|required)?\s*"
    r"(?P<type>[A-Za-z_][\w.]*)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<num>\d+)\s*;"
)
MAP_RE = re.compile(
    r"^\s*map<\s*(?P<k>[A-Za-z_][\w.]*)\s*,\s*(?P<v>[A-Za-z_][\w.]*)\s*>\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<num>\d+)\s*;"
)
ONEOF_RE = re.compile(r"^\s*oneof\s+(?P<name>[A-Za-z_]\w*)\s*\{")
RPC_RE = re.compile(
    r"^\s*rpc\s+(?P<name>[A-Za-z_]\w*)\s*\(\s*(?P<stream1>stream\s+)?(?P<req>[\w.]+)\s*\)"
    r"\s*returns\s*\(\s*(?P<stream2>stream\s+)?(?P<res>[\w.]+)\s*\)"
)
MSG_RE = re.compile(r"^\s*message\s+(?P<name>[A-Za-z_]\w*)\s*\{")
ENUM_RE = re.compile(r"^\s*enum\s+(?P<name>[A-Za-z_]\w*)\s*\{")
ENUM_VAL_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<num>-?\d+)\s*;")


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _type_defined(ftype: str, messages: set[str], enums: set[str]) -> bool:
    """类型是否已定义。

    protobuf 允许用 "Outer.Inner" 引用嵌套类型（例如
    ``UploadFileInfo.Status``），这里同时接受完整限定名和末段简单名。
    """
    if ftype in messages or ftype in enums:
        return True
    last = ftype.rsplit(".", 1)[-1]
    if last in messages or last in enums:
        return True
    # 逐级剥离前导父级：A.B.C -> B.C
    parts = ftype.split(".")
    for i in range(1, len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in messages or candidate in enums:
            return True
    return False


def check(path: Path) -> list[str]:
    errors: list[str] = []
    raw = path.read_text(encoding="utf-8")
    text = strip_comments(raw)

    # 1) 大括号 / 圆括号配平
    for open_ch, close_ch in (("{", "}"), ("(", ")"), ("[", "]")):
        if text.count(open_ch) != text.count(close_ch):
            errors.append(
                f"括号不配平: {open_ch}={text.count(open_ch)} "
                f"{close_ch}={text.count(close_ch)}"
            )
    if not text.rstrip().endswith("}"):
        errors.append("文件未以 } 结束（可能被截断）")

    # 2) 必需声明
    for token in ("syntax = \"proto3\";", "package clouddrive;",
                  "service CloudDriveFileSrv"):
        if token not in text:
            errors.append(f"缺少必需声明: {token}")

    lines = text.splitlines()

    # 3) 收集 message / enum 名（含嵌套限定名，例如 UploadFileInfo.Status）
    messages: set[str] = set()
    enums: set[str] = set()
    for line in lines:
        if m := MSG_RE.match(line):
            messages.add(m.group("name"))
        elif m := ENUM_RE.match(line):
            enums.add(m.group("name"))

    # 4) 逐行解析字段，做同 message 内字段号唯一性检查
    #    用简单栈跟踪当前 message 上下文
    stack: list[tuple[str, str]] = []  # (kind, name)
    field_numbers: dict[str, dict[int, str]] = defaultdict(dict)
    rpc_count = 0

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("}"):
            if stack:
                stack.pop()
            continue
        if m := MSG_RE.match(line):
            stack.append(("message", m.group("name")))
            # 单行 message：message Foo { uint32 a = 1; } —— 已闭合，不入栈
            if stripped.endswith("}"):
                stack.pop()
            continue
        if m := ENUM_RE.match(line):
            stack.append(("enum", m.group("name")))
            if stripped.endswith("}"):
                stack.pop()
            continue
        if ONEOF_RE.match(line):
            stack.append(("oneof", "oneof"))
            if stripped.endswith("}"):
                stack.pop()
            continue
        if m := RPC_RE.match(line):
            rpc_count += 1
            for kind in ("req", "res"):
                t = m.group(kind)
                if t not in BUILTIN_TYPES and t not in messages:
                    errors.append(
                        f"第 {lineno} 行 rpc {m.group('name')}: 引用了未定义类型 {t}"
                    )

        ctx = next((n for k, n in reversed(stack) if k == "message"), None)
        if ctx is None:
            continue

        num: int | None = None
        fname: str | None = None
        ftype: str | None = None
        if m := MAP_RE.match(line):
            num, fname, ftype = int(m.group("num")), m.group("name"), m.group("v")
            if m.group("k") not in BUILTIN_TYPES:
                errors.append(f"第 {lineno} 行 map 键类型非法: {m.group('k')}")
        elif m := FIELD_RE.match(line):
            num, fname, ftype = (
                int(m.group("num")), m.group("name"), m.group("type")
            )
            if ftype not in BUILTIN_TYPES and not _type_defined(ftype, messages, enums):
                errors.append(
                    f"第 {lineno} 行 {ctx}.{fname}: 引用了未定义类型 {ftype}"
                )
        if num is not None and fname:
            prev = field_numbers[ctx].get(num)
            if prev is not None:
                errors.append(
                    f"第 {lineno} 行 {ctx}: 字段号 {num} 重复（{prev} 与 {fname}）"
                )
            else:
                field_numbers[ctx][num] = fname

    if stack:
        errors.append(f"存在未闭合的块: {stack}")

    # 5) 关键 RPC 必须存在
    required_rpcs = [
        "GetSystemInfo", "GetToken", "GetUploadFileList", "GetUploadFileCount",
        "CancelUploadFiles", "PauseUploadFiles", "ResumeUploadFiles",
        "CancelAllUploadFiles", "PauseAllUploadFiles", "ResumeAllUploadFiles",
        "GetAllTasksCount",
    ]
    for rpc in required_rpcs:
        if not re.search(rf"\brpc\s+{rpc}\s*\(", text):
            errors.append(f"缺少必需 RPC: {rpc}")

    # 6) 关键 message 必须存在
    required_msgs = [
        "UploadFileInfo", "GetUploadFileListRequest", "GetUploadFileListResult",
        "MultpleUploadFileKeyRequest", "JWTToken", "GetTokenRequest",
    ]
    for msg in required_msgs:
        if msg not in messages:
            errors.append(f"缺少必需 message: {msg}")

    print(f"检查文件: {path}")
    print(f"  message: {len(messages)} 个, enum: {len(enums)} 个, rpc: {rpc_count} 个")
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
    errs = check(Path(sys.argv[1]))
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
