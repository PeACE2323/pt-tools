"""BT 流量子服务整合的单元测试。

整合架构：

    PTtools(8899) ──/btg/*──▶ 反代 ──▶ bt-traffic-guard(127.0.0.1:18080)

重点验证：

1. HTML 路径重写：Go 前端写死的 ``/api/xxx`` 必须改写成 ``/btg/api/xxx``，
   否则 iframe 里的请求会打到 PTtools 自己的根作用域。
2. 请求头过滤：hop-by-hop 头必须剔除；重写响应体后必须去掉 content-length。
3. 二进制缺失时**不应报错**，只是 BT 功能不可用（其余功能不受影响）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.btguard import (  # noqa: E402
    BTGuardManager,
    filter_headers,
    rewrite_body_bytes,
    rewrite_html,
)


# ---------------------------------------------------------------- 路径重写
def test_rewrite_double_quoted_api():
    html = '<script>fetch("/api/status")</script>'
    out = rewrite_html(html)
    assert '/btg/api/status' in out, out
    print("✓ 双引号 /api/ 重写")


def test_rewrite_single_quoted_api():
    html = "<script>fetch('/api/config')</script>"
    out = rewrite_html(html)
    assert '/btg/api/config' in out, out
    print("✓ 单引号 /api/ 重写")


def test_rewrite_template_literal():
    html = "<script>fetch(`/api/history`)</script>"
    out = rewrite_html(html)
    assert '/btg/api/history' in out, out
    print("✓ 模板字符串 /api/ 重写")


def test_rewrite_idempotent():
    """已经是 /btg/api/ 的不能被重复加前缀。"""
    html = 'fetch("/btg/api/status")'
    out = rewrite_html(html)
    assert out.count('/btg') == 1, out
    assert '/btg/btg' not in out
    print("✓ 重写幂等（不会重复加前缀）")


def test_rewrite_keeps_plain_text():
    """正文里的普通 /api 字样（无引号）不应被改写。"""
    html = "<p>调用 /api/status 接口</p>"
    out = rewrite_html(html)
    assert out == html, out
    print("✓ 正文文本不被误改")


def test_rewrite_complex_html():
    """真实结构的片段。"""
    html = """
    <script>
      const base = "/api";
      async function load(){ const r = await fetch('/api/status'); }
      const h = `<a href="/api/diagnostics">diag</a>`;
    </script>
    """
    out = rewrite_html(html)
    assert "/btg/api/status" in out
    assert "/btg/api/diagnostics" in out
    print("✓ 复杂结构正确重写")


# ---------------------------------------------------------------- 响应体
def test_rewrite_body_html_only():
    body = b'<script>fetch("/api/status")</script>'
    out = rewrite_body_bytes(body, "text/html; charset=utf-8")
    assert b"/btg/api/status" in out
    print("✓ HTML 响应体被重写")


def test_rewrite_body_json_untouched():
    body = b'{"ok":true,"url":"/api/status"}'
    out = rewrite_body_bytes(body, "application/json")
    assert out == body, "JSON 不应被改写"
    print("✓ JSON 响应体保持不变")


def test_rewrite_body_binary_safe():
    """非 UTF-8 内容不应抛异常。"""
    body = b"\xff\xfe\x00binary"
    out = rewrite_body_bytes(body, "text/html")
    assert isinstance(out, bytes)
    print("✓ 非 UTF-8 内容安全处理")


# ---------------------------------------------------------------- 头部
def test_filter_headers_drops_hop_by_hop():
    hdr = {
        "Content-Type": "text/html",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Host": "example.com",
    }
    out = filter_headers(hdr, drop_length=False)
    assert "Content-Type" in out
    for k in ("Connection", "Transfer-Encoding", "Host"):
        assert k not in out, f"{k} 应被剔除"
    print("✓ hop-by-hop 头被剔除")


def test_filter_headers_drop_length_when_rewriting():
    hdr = {"Content-Length": "1234", "Content-Type": "text/html"}
    out = filter_headers(hdr, drop_length=True)
    assert "Content-Length" not in out, "重写后长度已变，必须去掉"
    assert "Content-Type" in out
    print("✓ 重写响应时去掉 content-length")


# ---------------------------------------------------------------- 管理器
def test_missing_binary_is_not_fatal():
    """二进制缺失时应返回 False 而非抛异常。

    这是关键：镜像未包含 Go 产物时，CD2/115 功能必须照常可用。
    """
    import asyncio

    mgr = BTGuardManager()
    mgr._proc = None  # noqa: SLF001
    # 指向不存在的路径
    import app.services.btguard as mod

    old = mod.BTG_BINARY
    mod.BTG_BINARY = "/nonexistent/bt-traffic-guard"
    try:
        ok = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            mgr.start()
        )
        assert ok is False, "缺失二进制时 start 应返回 False"
        assert mgr.status.available is False
        assert "未找到" in mgr.status.last_error
    finally:
        mod.BTG_BINARY = old
    print("✓ 二进制缺失不致命（优雅降级）")


def test_default_listen_is_loopback():
    """子服务必须只监听回环地址，不得对外暴露。"""
    from app.services import btguard as mod

    assert mod.BTG_LISTEN.startswith("127.0.0.1"), mod.BTG_LISTEN
    print(f"✓ 子服务仅监听 {mod.BTG_LISTEN}")


def test_proxy_prefix():
    from app.services import btguard as mod

    assert mod.PROXY_PREFIX == "/btg"
    print("✓ 反代前缀为 /btg")


def test_routes_registered():
    """反代与状态路由必须已注册。"""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "btguard.router" in main, "main.py 未注册 btguard 路由"

    api = (ROOT / "app" / "api" / "btguard.py").read_text(encoding="utf-8")
    assert "/api/btg/status" in api
    assert "response_model=None" in api, "反代路由需要 response_model=None"
    print("✓ 路由已注册且注解正确")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print(f"✗ {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {fn.__name__}: {exc.__class__.__name__}: {exc}")
            failed += 1
    print()
    print(f"共 {len(tests)} 个测试，失败 {failed} 个")
    sys.exit(1 if failed else 0)
