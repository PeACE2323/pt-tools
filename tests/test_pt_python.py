"""PT 流量监控（纯 Python 实现）的单元测试。

这部分原先是 Go 子服务（bt-traffic-guard），现已用 Python 重写：

    bt_client.py   下载器协议（qBittorrent / Transmission）
    bt_policy.py   限速策略引擎
    bt_monitor.py  采集编排 + 配置持久化

测试重点：

1. 协议细节容易写错的点（Transmission 的 KB/s、qB 的限速字段）
2. 策略的周期边界与阈值判断
3. 累计量回退（客户端重启归零）不能导致误判
4. 不再有任何 Go 子进程 / 反代残留
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.bt_client import (  # noqa: E402
    ClientSnapshot,
    TransmissionClient,
    _parse_timeout,
    make_client,
)
from app.services.bt_policy import (  # noqa: E402
    PolicyConfig,
    _parse_bitrate,
    _parse_bytes,
    format_bytes,
    parse_duration,
    window_bounds,
)


# ---------------------------------------------------------------- 解析
def test_parse_bytes():
    assert _parse_bytes("500GiB") == 500 * 1024**3
    assert _parse_bytes("1G") == 1024**3
    assert _parse_bytes("512K") == 512 * 1024
    assert _parse_bytes("1048576") == 1048576
    assert _parse_bytes("") == 0
    assert _parse_bytes("bad") == 0
    print("✓ 字节阈值解析（500GiB / 1G / 512K）")


def test_parse_bitrate():
    """限速值与阈值同一套单位（字节/秒）。"""
    assert _parse_bitrate("1M") == 1024**2
    assert _parse_bitrate(0) == 0
    print("✓ 限速值解析")


def test_parse_duration():
    assert parse_duration("24h") == 86400
    assert parse_duration("7d") == 604800
    assert parse_duration("30m") == 1800
    assert parse_duration("3600") == 3600
    print("✓ 周期长度解析")


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1024**3) == "1.00 GB"
    print("✓ 字节格式化")


# ---------------------------------------------------------------- 窗口
def test_window_day():
    s, e = window_bounds("day", 86400, datetime(2026, 10, 18, 15, 30))
    assert (s.hour, s.minute, s.second) == (0, 0, 0)
    assert s.day == 18 and e.day == 19
    print("✓ 自然日边界")


def test_window_hour():
    s, e = window_bounds("hour", 3600, datetime(2026, 10, 18, 15, 30))
    assert s.hour == 15 and e.hour == 16
    print("✓ 自然小时边界")


def test_window_week():
    s, _e = window_bounds("week", 604800, datetime(2026, 10, 18, 15, 30))
    # 2026-10-18 是周日，所在周的周一是 10-12
    assert s.weekday() == 0, s
    assert s.day == 12, s
    print("✓ 自然周边界（周一起算）")


def test_window_month():
    s, e = window_bounds("month", 0, datetime(2026, 10, 18, 15, 30))
    assert s.day == 1 and e.month == 11
    print("✓ 自然月边界")


def test_window_rolling_has_fixed_length():
    s, e = window_bounds("rolling", 86400, datetime(2026, 10, 18, 15, 30))
    assert (e - s).total_seconds() == 86400
    print("✓ 滚动窗口长度固定")


# ---------------------------------------------------------------- 策略配置
def test_policy_from_dict():
    p = PolicyConfig.from_dict(
        {
            "name": "每日 500G",
            "enabled": True,
            "clients": ["qb"],
            "window": "24h",
            "align": "day",
            "threshold_total": "500GiB",
            "limit_total": "1M",
            "resume_on_window_reset": True,
        }
    )
    assert p.threshold_total == 500 * 1024**3
    assert p.limit_total == 1024**2
    assert p.applies_to("qb")
    assert not p.applies_to("tr")
    print("✓ 策略配置解析")


def test_policy_applies_to_all_when_empty():
    p = PolicyConfig(name="all", clients=[])
    assert p.applies_to("anything")
    print("✓ 未指定客户端时作用于全部")


def test_policy_wildcard():
    p = PolicyConfig(name="w", clients=["*"])
    assert p.applies_to("x")
    print("✓ 通配符 * 生效")


# ---------------------------------------------------------------- 客户端
def test_make_client_types():
    qb = make_client(
        {"name": "qb", "type": "qbittorrent", "url": "http://h:8080", "api_key": "k"}
    )
    assert qb.__class__.__name__ == "QBittorrentClient"
    assert qb.api_key == "k"

    tr = make_client({"name": "tr", "type": "transmission", "url": "http://h:9091"})
    assert tr.__class__.__name__ == "TransmissionClient"
    print("✓ 客户端工厂按类型创建")


def test_transmission_rpc_url():
    """用户填 /transmission/rpc 或裸地址都要能用。"""
    a = TransmissionClient("t", "http://h:9091")
    assert a.rpc_url == "http://h:9091/transmission/rpc"

    b = TransmissionClient("t", "http://h:9091/transmission/rpc")
    assert b.rpc_url == "http://h:9091/transmission/rpc"
    assert b.rpc_url.count("/transmission/rpc") == 1
    print("✓ Transmission RPC 地址推导（不重复拼接）")


def test_parse_timeout():
    assert _parse_timeout("10s") == 10.0
    assert _parse_timeout("500ms") == 0.5
    assert _parse_timeout(15) == 15.0
    assert _parse_timeout("bad") == 10.0
    print("✓ 超时解析")


def test_snapshot_to_dict():
    s = ClientSnapshot("qb", "qbittorrent", "http://h", healthy=True, up_speed=123)
    d = s.to_dict()
    assert d["name"] == "qb" and d["up_speed"] == 123
    assert d["healthy"] is True
    print("✓ 快照序列化")


# ---------------------------------------------------------------- 无 Go 残留
def test_no_go_service():
    """Go 子服务相关代码必须已彻底移除。"""
    assert not (ROOT / "btguard").exists(), "btguard 目录仍存在"
    assert not (ROOT / "app" / "services" / "btguard.py").exists()
    assert not (ROOT / "app" / "api" / "btguard.py").exists()

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "golang:" not in dockerfile, "Dockerfile 仍有多阶段 Go 构建"
    assert "BTG_BINARY" not in dockerfile
    print("✓ Go 子服务已移除")


def test_no_proxy_left():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "btguard" not in main.lower(), "main.py 仍引用 btguard"
    assert "/btg/" not in ui, "前端仍有 /btg/ 链接"
    # 排除注释里的说明文字，只看真实标签
    import re

    tags = re.sub(r"<!--.*?-->", "", ui, flags=re.DOTALL)
    tags = re.sub(r"//[^\n]*", "", tags)
    assert "<iframe" not in tags.lower(), "前端仍有 iframe 标签"
    print("✓ 反代与原始面板已移除")


def test_pt_api_uses_local_monitor():
    """PT 接口应直接读本地监控器，而不是转发 HTTP。"""
    api = (ROOT / "app" / "api" / "pt.py").read_text(encoding="utf-8")
    assert "get_pt_monitor" in api
    assert "_proxy_unavailable" not in api, "仍在转发到外部服务"
    assert "127.0.0.1" not in api
    print("✓ PT 接口走本地实现")


def test_scheduler_polls_pt():
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "poll_pt_traffic" in sched
    assert 'id="poll_pt_traffic"' in sched
    print("✓ 调度器已接入 PT 采集")


def test_shutdown_resumes_limits():
    """退出时必须解除限速，否则下载器会永久停留在限速状态。"""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "force_resume" in main, "退出时未解除限速"
    print("✓ 退出时恢复上传限速")


def test_used_not_polluting_baseline():
    """已用量必须单独存，不能塞进 baseline。

    早期实现把「本周期已用」写成 ``baseline["__used__"]``，
    结果 states() 里 ``sum(baseline.values())`` 把已用量又累加一次，
    进度条显示翻倍。这里锁死正确行为。
    """
    from app.services.bt_policy import PolicyEngine

    engine = PolicyEngine()
    engine.apply_config([PolicyConfig(name="p", threshold_total=1000)])
    rt = engine._runtimes["p"]  # noqa: SLF001

    # 模拟一轮：两个客户端，基线各 100，当前各 300 → 已用 400
    rt.baseline = {"a": 100, "b": 100}
    rt.used = 400

    states = engine.states()
    assert len(states) == 1
    assert states[0].current_total == 400, states[0].current_total
    assert "__used__" not in rt.baseline, "已用量不应污染 baseline"
    print("✓ 已用量不污染 baseline（进度不翻倍）")


def test_progress_percentage():
    """进度按 已用/阈值 计算。"""
    from app.services.bt_policy import PolicyEngine

    engine = PolicyEngine()
    engine.apply_config([PolicyConfig(name="p", threshold_total=1000)])
    rt = engine._runtimes["p"]  # noqa: SLF001
    rt.used = 250
    st = engine.states()[0]
    assert st.progress_total == 25.0, st.progress_total
    print("✓ 进度百分比正确")


def test_qb_origin_headers():
    """qBittorrent 每个请求都必须带同源 Referer/Origin。

    这是 403 Forbidden 的直接成因：qB 会校验 Referer/Origin 是否与
    请求的 Host 一致，只在登录时传是不够的 —— 后续所有 ``/api/v2/*``
    请求都要带，否则登录成功但取数据 403。

    参见官方文档：
    https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
    """
    from app.services.bt_client import QBittorrentClient

    c = QBittorrentClient(
        "qb", "http://192.168.7.101:8080", username="u", password="p"
    )
    h = c._request_headers()  # noqa: SLF001
    assert h.get("Referer") == "http://192.168.7.101:8080", h
    assert h.get("Origin") == "http://192.168.7.101:8080", h
    print("✓ qB 请求带同源 Referer/Origin（修复 403）")


def test_qb_origin_with_api_key():
    """API Key 模式同样需要同源头。"""
    from app.services.bt_client import QBittorrentClient

    c = QBittorrentClient("qb", "http://h:8080", api_key="qbt_abc")
    h = c._request_headers()  # noqa: SLF001
    assert h.get("Authorization") == "Bearer qbt_abc", h
    assert h.get("Origin") == "http://h:8080", h
    print("✓ API Key 模式也带同源头")


# ---------------------------------------------------------------- 前端语义
# 这几个是"脏标记"引发的连锁 bug，静态检查抓不到，只能靠断言源码。


def test_dirty_flag_initial_value():
    """「原始快照」初值必须是 '[]'，不能是空字符串。

    若为 ''，则 JSON.stringify([]) === '[]' !== ''，dirty 恒为真，导致：
      1. 刷新后永远不会自动加载已保存的下载器（看起来像没保存上）
      2. 一进页面就显示「有未保存的修改」
      3. 用户以为没保存成功，再添加一个，保存时把旧列表整体覆盖
    """
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "const ptFormOriginal = ref('[]')" in ui, "ptFormOriginal 初值应为 '[]'"
    assert "const ptPolicyOriginal = ref('[]')" in ui, (
        "ptPolicyOriginal 初值应为 '[]'"
    )
    print("✓ 脏标记初值为 '[]'（不会误报未保存）")


def test_pt_add_client_matches_normalize():
    """新增下载器的对象字段必须与 _normalizeClients 一致。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    # 截取 ptAddClient 与 _normalizeClients 两段
    add_idx = ui.find("const ptAddClient")
    norm_idx = ui.find("const _normalizeClients")
    assert add_idx > 0 and norm_idx > 0
    add_block = ui[add_idx : add_idx + 900]
    for field in ("clear_password", "clear_api_key", "supports_api_key"):
        assert field in add_block, f"ptAddClient 缺少 {field}（会导致脏标记误判）"
    print("✓ 新增下载器字段与规范化输出一致")


def test_policy_page_loads_clients():
    """PT 策略页必须加载下载器列表，否则「作用下载器」下拉为空。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    idx = ui.find("if (tab.value === 'pt')")
    assert idx > 0
    block = ui[idx : idx + 500]
    assert "loadPtForm()" in block, (
        "PT 策略页未加载下载器列表，「作用下载器」会无法选择"
    )
    print("✓ PT 策略页加载下载器列表")


def test_dashboard_uses_new_monitor():
    """仪表盘不得再引用已删除的 Go 模块。"""
    dash = (ROOT / "app" / "api" / "dashboard.py").read_text(encoding="utf-8")
    assert "btguard" not in dash, "dashboard 仍引用已删除的 btguard"
    assert "get_pt_monitor" in dash
    print("✓ 仪表盘使用新监控器")


def test_render_does_not_gate_on_running():
    """渲染与加载都不能依赖 pt.running 这类异步状态。

    pt.running 初始为 false，首次渲染时用它做 v-if / 前置判断会导致：
      1. 刷新页面后下载器表单整块不显示（切页签再回来才出现）
      2. loadPtForm / loadPtPolicies 被静默跳过
    """
    import re

    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    # 下载器表单容器不得用 pt.running 做条件
    idx = ui.find("<!-- 下载器列表 -->")
    assert idx > 0, "未找到下载器列表区块"
    seg = ui[idx : idx + 400]
    head = seg.split("v-for")[0]
    assert 'v-if="pt.running"' not in head, (
        "下载器列表容器仍用 pt.running 做条件，首屏会空白"
    )

    # 加载函数内部不能用 pt.running 提前 return
    for fn in ("loadPtForm", "loadPtPolicies"):
        idx = ui.find(f"const {fn} = async")
        assert idx > 0, f"未找到 {fn}"
        block = ui[idx : idx + 600]
        assert "if (!pt.running) return" not in block, (
            f"{fn} 用 pt.running 提前返回，异步加载时会被跳过"
        )
    print("✓ 渲染与加载不依赖异步状态（修复刷新空白）")


def test_policy_editor_collapsible():
    """策略编辑区应可折叠，保存后自动收起。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "ptEditorOpen" in ui, "缺少编辑区折叠状态"
    assert "v-if=\"ptEditorOpen\"" in ui, "编辑区未使用折叠条件"
    # 保存成功后收起
    idx = ui.find("const savePtPolicies")
    block = ui[idx : idx + 1400]
    assert "ptEditorOpen.value = false" in block, "保存后未收起编辑区"
    print("✓ 策略编辑区可折叠且保存后自动收起")


def test_branding_is_pttools():
    """页面标题、图标、版本接口都应显示 PTtools。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert "CD2-115-Guard" not in ui, "页面仍残留旧项目名 CD2-115-Guard"
    assert "<title>PTtools" in ui, "页面标题未改为 PTtools"
    # favicon：避免浏览器标签显示默认地球图标
    assert 'rel="icon"' in ui, "缺少 favicon"

    assert '"name": "PTtools"' in main, "/api/version 返回名未更新"
    print("✓ 品牌信息统一为 PTtools")


def test_no_version_flicker():
    """版本号初值必须是当前版本，不能是旧的 1.0.0。

    前端 version 初始值若为 '1.0.0'，页面加载后才被 /api/version 覆盖，
    用户会看到版本号先显示 1.0.0 再跳变成实际版本。
    """
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    init = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")

    import re

    m = re.search(r'const version = ref\(\'([^\']*)\'\)', ui)
    assert m, "未找到 version 初值"
    frontend_ver = m.group(1)

    v = re.search(r'__version__ = "([^"]+)"', init)
    assert v, "未找到后端版本号"
    backend_ver = v.group(1)

    assert frontend_ver == backend_ver, (
        f"前端版本初值 {frontend_ver} 与后端 {backend_ver} 不一致，会闪烁"
    )
    print(f"✓ 版本号初值一致（{backend_ver}，无闪烁）")


def test_dashboard_loading_skeleton():
    """仪表盘首屏应显示骨架，而不是「未连接/0」的错误状态。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "dashReady" in ui, "缺少首屏就绪标志"
    assert 'v-if="!dashReady"' in ui, "缺少加载骨架"
    assert "animate-pulse" in ui, "骨架缺少动画"
    print("✓ 仪表盘首屏骨架（避免状态闪烁）")


# ---------------------------------------------------------------- 状态持久化
def test_runtime_state_roundtrip():
    """策略运行时状态必须能导出并恢复。

    baseline 记录周期起点时各客户端的累计上传量。若它只在内存里，
    容器重启后第一轮采集会把「当前累计量」当成新基线，本周期流量
    从 0 重算——用户看到的就是「重启后清零」。
    """
    import json

    from app.services.bt_policy import PolicyConfig, PolicyEngine

    e1 = PolicyEngine()
    e1.apply_config([PolicyConfig(name="p", threshold_total=1000)])
    rt = e1._runtimes["p"]  # noqa: SLF001
    rt.baseline = {"qb": 5_000_000, "tr": 1_000}
    rt.window_key = "2026-10-18T00:00:00"
    rt.used = 480_000
    rt.limited = True
    rt.original_limits = {"qb": 2621440}

    blob = json.dumps(e1.export_state())

    # 模拟重启：全新引擎 + 恢复
    e2 = PolicyEngine()
    e2.apply_config([PolicyConfig(name="p", threshold_total=1000)])
    e2.import_state(json.loads(blob))

    rt2 = e2._runtimes["p"]  # noqa: SLF001
    assert rt2.baseline == rt.baseline, rt2.baseline
    assert rt2.window_key == rt.window_key
    assert rt2.used == rt.used
    assert rt2.limited is True
    assert rt2.original_limits == rt.original_limits

    st = e2.states()[0]
    assert st.current_total == 480_000, st.current_total
    print("✓ 运行时状态可持久化（重启不清零）")


def test_runtime_state_survives_new_period():
    """跨周期后应正常重置，不能把上周期进度带过来。"""
    import asyncio

    from app.services.bt_policy import PolicyConfig, PolicyEngine

    e = PolicyEngine()
    e.apply_config([PolicyConfig(name="p", threshold_total=1000, align="day")])
    e.import_state(
        {"p": {"window_key": "1999-01-01T00:00:00", "baseline": {"a": 100}}}
    )
    rt = e._runtimes["p"]  # noqa: SLF001
    rt.window_key = "1999-01-01T00:00:00"

    asyncio.run(e.evaluate({"a": {"up_total": 9999}}, {}))
    assert rt.baseline.get("a") == 9999, rt.baseline
    print("✓ 跨周期正确重置基线")


def test_runtime_persisted_to_db():
    """监控器应把运行时状态写入独立配置项。"""
    mon = (ROOT / "app" / "services" / "bt_monitor.py").read_text(encoding="utf-8")
    assert "SETTING_RUNTIME" in mon, "缺少运行时状态配置项"
    assert "def load_pt_runtime" in mon
    assert "def save_pt_runtime" in mon
    assert "def persist_pt_runtime" in mon
    # 轮询中应定期落盘
    assert "persist_pt_runtime(self.engine)" in mon
    print("✓ 运行时状态定期落盘")


def test_startup_polls_immediately():
    """启动时应立即采集一次，否则要等 30 秒才看到下载器。"""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "poll_once()" in main, "启动时未做首次采集"
    assert "首次采集" in main
    print("✓ 启动时立即采集（不必等 30 秒）")


# ---------------------------------------------------------------- 任务监控
def test_instant_badge_matches_list():
    """秒传详情角标必须与列表口径一致。

    角标原先用 today_instant（今日数），列表展示的是全部记录。
    记录不是今天产生时，角标有数字但列表为空——用户看到的就是
    「有数字但没有详细信息」。
    """
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    api = (ROOT / "app" / "api" / "tasks.py").read_text(encoding="utf-8")

    # 后端要提供总数
    assert '"total_instant"' in api, "后端未提供 total_instant"

    # 前端角标要用总数
    idx = ui.find("{ key: 'instant'")
    assert idx > 0
    seg = ui[idx : idx + 120]
    assert "total_instant" in seg, (
        f"角标未使用 total_instant，实际: {seg.strip()[:80]}"
    )
    assert "today_instant" not in seg, "角标仍用今日数，与列表口径不一致"
    print("✓ 秒传角标与列表口径一致")


def test_instant_empty_state_actionable():
    """列表为空但总数不为 0 时应给出可操作提示。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    idx = ui.find("暂无秒传记录")
    assert idx > 0
    seg = ui[max(0, idx - 700) : idx + 100]
    assert "instantPage.total > 0" in seg, "缺少「有总数但列表空」的提示分支"
    print("✓ 空列表给出可操作提示")


def test_instant_list_tolerates_bad_row():
    """单条记录序列化失败不应让整页空白。"""
    api = (ROOT / "app" / "api" / "tasks.py").read_text(encoding="utf-8")
    idx = api.find("def list_instant")
    seg = api[idx : idx + 1800]
    assert "try:" in seg and "model_validate(r)" in seg, "缺少逐条容错"
    print("✓ 列表逐条容错（单条异常不影响整页）")


def test_instant_debug_endpoint():
    """提供诊断端点，便于排查统计与查询不一致。"""
    api = (ROOT / "app" / "api" / "tasks.py").read_text(encoding="utf-8")
    assert "/instant/debug" in api, "缺少诊断端点"
    print("✓ 秒传诊断端点")


def test_qb_no_auto_redirect():
    """不能自动跟随重定向：Host 变化会让 Referer 校验失效。"""
    src = (ROOT / "app" / "services" / "bt_client.py").read_text(encoding="utf-8")
    # 定位 qB 客户端的 _client
    idx = src.find("class QBittorrentClient")
    seg = src[idx : idx + 3000]
    assert "follow_redirects=False" in seg, "qB 客户端不应自动跟随重定向"
    print("✓ qB 客户端禁用自动重定向")


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
