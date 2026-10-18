#!/usr/bin/env python3
"""拉取 / 编译 clouddrive.proto。

策略（按优先级）：
1. 若 ``--offline`` 未指定且网络可用，则从官方地址下载最新 clouddrive.proto
   覆盖 ``app/proto/clouddrive.proto``（下载失败一律回退到内置副本，构建不中断）。
2. 使用 ``grpc_tools.protoc`` 编译生成 ``clouddrive_pb2.py`` 与
   ``clouddrive_pb2_grpc.py``。
3. 若 grpcio-tools 不可用，则直接报错退出（这两份文件是硬依赖）。

用法::

    python tools/fetch_proto.py [--out app/proto] [--offline] [--url URL]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

OFFICIAL_PROTO_URL = "https://www.clouddrive2.com/api/clouddrive.proto"
PROTO_NAME = "clouddrive.proto"

# protoc 生成代码中的 "import clouddrive_pb2 as ..." 需要 <out> 目录可被
# 直接 import（我们把它做成一个包），因此补一个 __init__.py。
PKG_INIT = '''"""CloudDrive2 gRPC 生成代码（由 tools/fetch_proto.py 自动生成）。"""
from . import clouddrive_pb2  # noqa: F401
from . import clouddrive_pb2_grpc  # noqa: F401
'''


def log(msg: str) -> None:
    print(f"[proto] {msg}", flush=True)


def is_complete_proto(text: str) -> bool:
    """粗校验：必须含 package 声明、service 定义，且大括号配平。

    官方文档站点在限流或异常时会返回 HTML 或截断内容，直接编译会失败，
    这里先做一次结构校验，不通过就视为无效并回退到内置副本。
    """
    if "package clouddrive;" not in text:
        return False
    if "service CloudDriveFileSrv" not in text:
        return False
    if "GetUploadFileList" not in text:
        return False
    # 去掉注释里的花括号干扰后再配平
    stripped = re.sub(r"//[^\n]*", "", text)
    if stripped.count("{") != stripped.count("}"):
        return False
    return True


def try_download(url: str, timeout: float = 20.0) -> str | None:
    log(f"尝试下载官方 proto: {url}")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "cd2-115-guard/proto-fetcher"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - 任何网络异常都回退
        log(f"下载失败（将使用内置副本）: {exc.__class__.__name__}: {exc}")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        log("下载内容不是 UTF-8 文本，忽略")
        return None
    if not is_complete_proto(text):
        log("下载内容校验未通过（可能被截断或返回了 HTML），忽略")
        return None
    log(f"下载成功，{len(text)} 字节，版本标识: "
        f"{re.search(r'option [(]version[)] = \"([^\"]+)\"', text).group(1) if re.search(r'option [(]version[)] = \"([^\"]+)\"', text) else 'unknown'}")
    return text


def compile_proto(out_dir: Path) -> None:
    try:
        # grpc_tools.protoc 以模块方式调用，避免依赖 PATH 中的 protoc
        from grpc_tools import protoc  # noqa: F401
    except ImportError:
        log("未安装 grpcio-tools，无法编译 proto")
        log("请执行: pip install grpcio-tools")
        raise SystemExit(1)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{out_dir}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        "--pyi_out=" + str(out_dir),
        str(out_dir / PROTO_NAME),
    ]
    log("执行: " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if result.stdout.strip():
        log("stdout: " + result.stdout.strip())
    if result.returncode != 0:
        log("编译失败 stderr: " + result.stderr.strip())
        raise SystemExit(result.returncode)
    log("编译完成: clouddrive_pb2.py / clouddrive_pb2_grpc.py / clouddrive_pb2.pyi")


def patch_grpc_imports(out_dir: Path) -> None:
    """把生成文件里的绝对导入改成包内相对导入。

    protoc 生成的 ``clouddrive_pb2_grpc.py`` 会写 ``import clouddrive_pb2 as
    clouddrive__pb2``；当我们以 ``app.proto.clouddrive_pb2_grpc`` 方式导入时
    该绝对导入会失效，因此改写为 ``from . import clouddrive_pb2``。
    """
    target = out_dir / "clouddrive_pb2_grpc.py"
    if not target.exists():
        return
    text = target.read_text(encoding="utf-8")
    new = re.sub(
        r"^import clouddrive_pb2 as (\w+)$",
        r"from . import clouddrive_pb2 as \1",
        text,
        flags=re.MULTILINE,
    )
    if new != text:
        target.write_text(new, encoding="utf-8")
        log("已修正 clouddrive_pb2_grpc.py 的导入语句为相对导入")


def main() -> int:
    here = Path(__file__).resolve().parent
    default_out = here.parent / "app" / "proto"

    ap = argparse.ArgumentParser(description="拉取并编译 clouddrive.proto")
    ap.add_argument("--out", default=str(default_out), help="输出目录")
    ap.add_argument("--url", default=OFFICIAL_PROTO_URL, help="官方 proto 地址")
    ap.add_argument("--offline", action="store_true", help="跳过下载，仅编译内置副本")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    proto_path = out_dir / PROTO_NAME

    if not proto_path.exists():
        log(f"内置 proto 不存在: {proto_path}")
        return 1
    log(f"内置 proto: {proto_path} ({proto_path.stat().st_size} 字节)")

    if not args.offline:
        text = try_download(args.url)
        if text is not None:
            # 先写临时文件再原子替换，避免半截文件落盘
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, suffix=".proto"
            ) as fp:
                fp.write(text)
                tmp = Path(fp.name)
            shutil.move(str(tmp), proto_path)
            log("已用官方最新 proto 覆盖内置副本")

    compile_proto(out_dir)
    patch_grpc_imports(out_dir)
    (out_dir / "__init__.py").write_text(PKG_INIT, encoding="utf-8")
    log(f"输出目录: {out_dir}")
    for f in sorted(out_dir.glob("clouddrive_pb2*")):
        log(f"  - {f.name} ({f.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
