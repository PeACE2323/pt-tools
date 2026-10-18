"""CD2 连接诊断的单元测试。

对应真实用户反馈的错误：

    CD2 调用 GetSystemInfo 失败:
    errors resolving clouddrive2:19799:
    [field:hostname lookup error:address lookup failed ... Domain name not found]

这类错误很常见（用户在设置页填了不存在的容器名），必须给出可操作的建议，
而不是把原始 gRPC 错误直接抛给用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cd2_client import CD2Client  # noqa: E402


def test_dns_error_gives_actionable_advice():
    """DNS 解析失败应提示「容器名/网络/端口写法」三类原因。"""
    detail = (
        "errors resolving clouddrive2:19799: [field:hostname lookup error:"
        "address lookup failed for clouddrive2:19799: Domain name not found]"
    )
    msg = CD2Client.diagnose_connection_error("clouddrive2:19799", detail)

    assert "主机名解析失败" in msg
    assert "Docker 网络" in msg, "应提示 Docker 网络问题"
    assert "host.docker.internal" in msg, "应给出宿主机地址建议"
    assert "19799" in msg
    print("✓ DNS 错误给出可操作建议")


def test_is_name_resolution_error_detects():
    """应能识别各种 DNS 错误写法。"""
    cases = [
        "address lookup failed for x",
        "Domain name not found",
        "no such host",
        "errors resolving foo:19799",
    ]
    for c in cases:
        assert CD2Client._is_name_resolution_error(c), f"未识别: {c}"
    # 非 DNS 错误不应误判
    assert not CD2Client._is_name_resolution_error("Connection refused")
    assert not CD2Client._is_name_resolution_error("Deadline Exceeded")
    print("✓ DNS 错误识别准确")


def test_connection_refused_mentions_correct_port():
    """端口不通时应明确 19798 是对的、19799 不是 gRPC 端口。

    这是一次真实误判的教训：项目曾错误地认为 gRPC 走 19799，
    导致用户填 19799 连不上、填 19798 反而成功。
    官方文档：核心服务（含 gRPC）在 19798；19799 是 HTTPS 端口且默认关闭。
    """
    msg = CD2Client.diagnose_connection_error(
        "192.168.1.5:19798",
        "failed to connect to all addresses: Connection refused",
    )
    assert "连接被拒绝" in msg
    assert "19798" in msg, "应指明正确端口 19798"
    assert "19799" in msg, "应说明 19799 不是 gRPC 端口"
    assert "-p 19798:19798" in msg, "应给出端口映射写法"
    print("✓ 端口拒绝时给出正确的 19798 指引")


def test_timeout_hint():
    msg = CD2Client.diagnose_connection_error("1.2.3.4:19798", "Deadline Exceeded")
    assert "超时" in msg
    print("✓ 超时提示")


def test_unknown_error_passthrough():
    """未知错误也要带上原始信息，便于排查。"""
    msg = CD2Client.diagnose_connection_error("h:1", "some weird error")
    assert "some weird error" in msg
    print("✓ 未知错误保留原始信息")


def test_default_host_is_not_hardcoded_container_name():
    """默认配置不应硬编码容器名，且端口必须是 19798。

    两个都曾是真实故障：
    1. 默认写死 clouddrive2:19799 —— 绝大多数部署没有这个容器名，DNS 解析失败
    2. 端口误认为 19799 —— 实为 HTTPS 端口且默认关闭，连接失败
       官方文档：核心服务（含 gRPC）在 19798。
    """
    from app.config import DEFAULT_SETTINGS

    host = DEFAULT_SETTINGS["cd2.host"]
    assert "clouddrive2" not in host, f"默认值不应硬编码容器名: {host}"
    assert host.endswith(":19798"), f"默认端口必须是 19798，实际: {host}"
    print(f"✓ 默认 CD2 地址妥当: {host}")


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
