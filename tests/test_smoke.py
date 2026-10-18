"""应用启动冒烟测试。

静态检查最大的盲区是「import 应用时才会暴露的错误」，例如：

* FastAPI 把返回注解当 Pydantic 模型解析失败
* 循环导入
* 模块级代码抛异常
* 路由重复注册

本测试直接 import ``app.main`` 并构造 TestClient，能在 CI 阶段就发现
「镜像跑不起来」这类问题。

依赖 fastapi / sqlalchemy —— 这些是镜像里的必需依赖，CI 若未安装则跳过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 用临时目录做数据目录，避免污染仓库
_TMP = Path(tempfile.mkdtemp(prefix="guard-smoke-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["GUARD_DB_PATH"] = str(_TMP / "guard.db")
os.environ["P115_COOKIE_FILE"] = str(_TMP / "115-cookies.txt")


class _Skipped(Exception):
    """内部使用：表示该测试被跳过。"""


def _skip(reason: str) -> None:
    """标记跳过并抛出，由主循环处理。

    不能直接 sys.exit(0)：那会终止整个测试进程，
    让后面的测试也没机会运行，看起来像「全过了」。
    """
    raise _Skipped(reason)


def test_import_app() -> None:
    """import 应用本身不应抛异常（这是最关键的检查）。"""
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
    except ImportError as exc:
        _skip(f"缺少依赖（{exc.name}），跳过启动冒烟测试")

    from app.main import app

    assert app is not None
    print(f"✓ app import 成功，共 {len(app.routes)} 条路由")


def test_openapi_schema() -> None:
    """生成 OpenAPI schema —— 会遍历所有路由的 response_model。

    FastAPI 的 FastAPIError（response field 非法）在 add_api_route 时
    就已抛出，这里再生成一次 schema 可覆盖更多路径。
    """
    try:
        import fastapi  # noqa: F401
    except ImportError:
        _skip("缺少 fastapi")

    from app.main import app

    schema = app.openapi()
    assert "paths" in schema
    print(f"✓ OpenAPI schema 生成成功，{len(schema['paths'])} 个路径")


def test_index_route_returns_html() -> None:
    """首页应返回 HTML（而非 500 或 JSON）。"""
    try:
        import fastapi  # noqa: F401
        from fastapi.testclient import TestClient
    except ImportError:
        _skip("缺少 fastapi/httpx")

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200, f"GET / 返回 {resp.status_code}"
        body = resp.text or ""
        assert "CD2-115-Guard" in body or "app" in body, "首页内容异常"
        print(f"✓ GET / -> {resp.status_code}, {len(body)} 字节")

        # 健康检查
        health = client.get("/api/health")
        assert health.status_code == 200, f"GET /api/health 返回 {health.status_code}"
        data = health.json()
        assert data.get("ok") is True
        print(f"✓ GET /api/health -> {data}")

        # 静态资源挂载
        favicon = client.get("/favicon.ico")
        assert favicon.status_code in (200, 204)
        print(f"✓ GET /favicon.ico -> {favicon.status_code}")


def test_core_api_reachable() -> None:
    """核心只读 API 应能正常响应（不要求数据，只要不 500）。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        _skip("缺少 fastapi/httpx")

    from app.main import app

    endpoints = [
        "/api/version",
        "/api/dashboard",
        "/api/rules",
        "/api/tasks/live",
        "/api/tasks/summary",
        "/api/p115/offline",
        "/api/p115/config",
        "/api/settings",
    ]
    with TestClient(app) as client:
        for path in endpoints:
            resp = client.get(path)
            assert resp.status_code < 500, f"GET {path} 返回 {resp.status_code}: {resp.text[:300]}"
            print(f"✓ GET {path} -> {resp.status_code}")


def test_webhook_accepts_payload() -> None:
    """CD2 webhook 端点应接受官方格式的 payload。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        _skip("缺少 fastapi/httpx")

    from app.main import app

    payload = {
        "device_name": "smoke",
        "user_name": "tester",
        "version": "1.0.17",
        "event_category": "file",
        "event_name": "notify",
        "event_time": "1700000000",
        "send_time": "1700000001",
        "data": [
            {
                "action": "create",
                "is_dir": "false",
                "source_file": "/mnt/local/PT/smoke-test.mkv",
                "destination_file": "",
            }
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/webhook/cd2/file_notify", json=payload)
        assert resp.status_code == 200, f"webhook 返回 {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("received", 0) == 1, f"未接收事件: {data}"
        print(f"✓ POST webhook -> {data}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    skipped = 0
    for fn in tests:
        try:
            fn()
        except _Skipped as exc:
            print(f"SKIP {fn.__name__}: {exc}")
            skipped += 1
        except AssertionError as exc:
            print(f"✗ {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"✗ {fn.__name__}: {exc.__class__.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print()
    print(f"共 {len(tests)} 个测试，失败 {failed} 个，跳过 {skipped} 个")
    if skipped == len(tests):
        print("警告：全部测试被跳过，冒烟验证未真正执行！")
        sys.exit(1)
    sys.exit(1 if failed else 0)
