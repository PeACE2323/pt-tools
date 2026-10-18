"""PT 流量监控「原生整合」的测试。

整合演进过程：

1. 最初：把 Go 侧整页面板用 iframe 嵌进来 —— 只是套壳，
   用户仍在用两套界面，观感与交互都不一致。
2. 现在：Python 侧调用 Go 的 JSON API，用 PTtools 自己的 UI 渲染。
   Go 面板仍保留在 /btg/ 作为排错入口。

本测试锁定第 2 种形态的关键约定：

* 存在 /api/pt/* 原生接口，且覆盖总览/客户端/策略/事件/配置
* 仪表盘聚合里带 pt 字段
* PT 事件会写进统一的 event_logs（侧边栏「事件日志」能看到）
* 侧边栏命名是「PT 策略」而不是「BT 流量」
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PT_API = (ROOT / "app" / "api" / "pt.py").read_text(encoding="utf-8")
BTG_SVC = (ROOT / "app" / "services" / "btguard.py").read_text(encoding="utf-8")
DASH = (ROOT / "app" / "api" / "dashboard.py").read_text(encoding="utf-8")
SCHED = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
SCHEMAS = (ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
UI = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 原生接口
def test_native_api_routes():
    """/api/pt/* 必须覆盖全部所需能力。"""
    required = [
        "/overview",
        "/clients",
        "/policies",
        "/events",
        "/config",
        "/policy/{action}",
        "/client/test",
    ]
    for r in required:
        assert f'"{r}"' in PT_API, f"缺少路由 {r}"
    print(f"✓ 原生接口齐全（{len(required)} 个）")


def test_pt_router_registered():
    assert "pt.router" in MAIN, "main.py 未注册 pt 路由"
    print("✓ PT 路由已注册")


def test_service_has_data_methods():
    """管理器需要提供读取 Go 数据的方法。"""
    for m in (
        "fetch_status",
        "fetch_events",
        "fetch_config_yaml",
        "save_config_yaml",
        "policy_action",
        "test_client",
    ):
        assert f"async def {m}" in BTG_SVC, f"缺少方法 {m}"
    print("✓ 数据访问方法齐全")


def test_config_uses_yaml():
    """配置读写走 YAML 原文。

    Go 侧配置结构字段很多（客户端/策略/通知/鉴权），
    逐字段映射既易漏又易随版本漂移，直接用 YAML 最稳。
    """
    assert "/api/config/raw" in BTG_SVC, "应使用 raw YAML 接口"
    assert '{"yaml": yaml_text}' in BTG_SVC or '"yaml": yaml_text' in BTG_SVC
    print("✓ 配置走 YAML 原文")


# ---------------------------------------------------------------- 仪表盘
def test_dashboard_has_pt():
    assert "pt_view" in DASH, "仪表盘未包含 PT 数据"
    assert 'pt=pt_view' in DASH, "DashboardOut 未传入 pt"
    assert "pt:" in SCHEMAS, "DashboardOut 缺少 pt 字段"
    print("✓ 仪表盘整合 PT 总览")


def test_dashboard_pt_degrades_gracefully():
    """PT 不可用不能让仪表盘整页失败。"""
    idx = DASH.find("# ---------------- PT 流量")
    body = DASH[idx: idx + 2200]
    assert "except Exception" in body, "PT 读取失败应被捕获"
    assert '"available": False' in body or '"available": False,' in body
    print("✓ PT 不可用时仪表盘优雅降级")


# ---------------------------------------------------------------- 事件汇总
def test_pt_events_merged_into_event_logs():
    """PT 事件必须汇入统一事件日志。"""
    assert "_import_pt_events" in SCHED, "缺少 PT 事件导入"
    assert 'log_event(' in SCHED
    assert '"pt-traffic"' in SCHED, "事件来源应标记为 pt-traffic"
    print("✓ PT 事件汇入统一事件日志")


def test_pt_events_dedup():
    """事件导入需要去重，避免每轮重复写入。"""
    idx = SCHED.find("async def _import_pt_events")
    body = SCHED[idx: idx + 2200]
    assert "_pt_seen_events" in body, "缺少去重集合"
    assert "if key in seen" in body
    print("✓ PT 事件去重")


# ---------------------------------------------------------------- 前端
def test_sidebar_named_pt():
    """侧边栏应叫「PT 策略」，不是「BT 流量」。"""
    assert "'PT 策略'" in UI, "侧边栏命名应为 PT 策略"
    assert "'BT 流量'" not in UI, "不应残留 BT 流量命名"
    assert "'pt'" in UI and "'bt'" not in UI
    print("✓ 侧边栏命名为 PT 策略")


def test_ui_uses_native_render_not_iframe():
    """不能再把整页 iframe 当作主界面。"""
    assert "<iframe" not in UI, "仍有 iframe 套壳"
    assert "/api/pt/overview" in UI, "前端应调用原生接口"
    print("✓ 前端用原生接口渲染（无 iframe）")


def test_ui_has_policy_and_config():
    """策略页与设置页都要有可用的表单操作。"""
    # PT 策略页：策略编辑
    assert "ptPolicyAction" in UI, "缺少策略运行操作（解除限速/触发）"
    assert "savePtPolicies" in UI, "缺少策略配置保存"
    assert "ptPolicies" in UI
    # 系统设置：下载器表单
    assert "savePtForm" in UI, "缺少下载器配置保存"
    assert "ptClients" in UI
    print("✓ 策略编辑与下载器表单已就位")


def test_no_yaml_editor():
    """配置不再用 YAML 文本框，改为图形化表单。"""
    assert "ptCfgYaml" not in UI, "仍残留 YAML 编辑器"
    assert 'placeholder="点击「填入配置模板」' not in UI
    print("✓ 已移除 YAML 编辑器")


def test_notify_removed():
    """通知功能已移除：保存时强制关闭。"""
    assert "_strip_notify" in PT_API, "缺少通知剥离逻辑"
    body = PT_API[PT_API.find("def _strip_notify"):]
    assert '"enabled": False' in body
    assert "ntfy_enabled" in body and "webhook_enabled" in body
    print("✓ 通知功能已禁用")


def test_policies_config_routes():
    """策略要通过原生接口读写。"""
    assert "/policies/config" in PT_API
    assert "def pt_put_policies" in PT_API
    print("✓ 策略配置接口存在")


def test_form_routes():
    """下载器配置走结构化表单接口。"""
    assert "/form" in PT_API
    assert "fetch_form_config" in BTG_SVC
    assert "save_form_config" in BTG_SVC
    print("✓ 图形化配置接口存在")


def test_dashboard_redesigned():
    """仪表盘改版：服务状态卡 + 分组指标 + 可折叠详情。"""
    assert "serviceCards" in UI, "缺少服务状态卡"
    assert "dashboardErrors" in UI, "缺少异常聚合"
    assert "dashDetailOpen" in UI, "缺少可折叠的运行详情"
    assert "dashboardCards" not in UI, "旧的一排指标卡应已移除"
    print("✓ 仪表盘已重构")


def test_tab_keys_updated():
    """页签键名从 bt 改为 pt。"""
    assert "'pt'" in UI
    assert "tab==='bt'" not in UI
    print("✓ 页签键名已更新")


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
