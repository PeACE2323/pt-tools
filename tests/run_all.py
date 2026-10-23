#!/usr/bin/env python3
"""运行全部零依赖单元测试。

这些测试不需要 SQLAlchemy / APScheduler / grpc / p115client 也能跑，
便于在没有完整依赖环境时做回归。

用法::

    python tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = [
    "test_rules.py",
    "test_webhook.py",
    "test_scope.py",
    "test_backup.py",
    "test_cd2_diag.py",
    "test_timeout_logic.py",
    "test_cd2_guard_v4.py",
    "test_health.py",
    "test_pt_python.py",
    "test_pt_baseline.py",
    "test_p115_status.py",
]


def main() -> int:
    failed: list[str] = []
    for name in SUITES:
        path = HERE / name
        if not path.exists():
            print(f"跳过（不存在）: {name}")
            continue
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        result = subprocess.run([sys.executable, str(path)])  # noqa: S603
        if result.returncode != 0:
            failed.append(name)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"失败 {len(failed)} 个测试套件: {', '.join(failed)}")
        return 1
    print(f"全部 {len(SUITES)} 个测试套件通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
