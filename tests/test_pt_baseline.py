"""PT 限速策略的基线计算测试（防止进度爆表）。

用户反馈：本周期流量显示 5.58 GB / 1.00 GB = 557.6%。

根因：进入新周期时，代码给**所有**快照建立基线，包括那些本轮
采集失败的客户端。缺失的客户端拿到 0 基线，等它下次上线时
「当前累计量 − 0」把全部历史流量算进本周期。

本测试锁定修复后的行为：
1. 采集失败的客户端不建立基线
2. 它下次采到时才初始化基线，不计入历史量
3. 正常客户端不受影响

用法：

    python tests/test_pt_baseline.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.bt_policy import PolicyConfig, PolicyEngine  # noqa: E402


def _snap(up_total: int, healthy: bool = True) -> dict:
    return {"up_total": up_total, "healthy": healthy}


def test_unhealthy_client_not_baselined():
    """采集失败的客户端不应拿到 0 基线。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=1000, align="day")]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    # 第一轮：两个客户端，其中一个不健康（离线）
    snaps = {"a": _snap(1000), "b": _snap(0, healthy=False)}
    asyncio.run(engine.evaluate(snaps, {}))

    assert "a" in rt.baseline, "健康客户端应有基线"
    assert "b" not in rt.baseline, (
        "不健康的客户端不应建立基线（否则会把历史量算进来）"
    )
    print("✓ 采集失败的客户端不建立基线")


def test_late_client_does_not_inflate():
    """客户端恢复后应以其当前值为基线，不能把历史量算入。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=1000, align="day")]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    # 第一轮：b 离线
    asyncio.run(engine.evaluate({"a": _snap(0), "b": _snap(0, healthy=False)}, {}))
    # 第二轮：b 上线，历史上传 5,000,000 字节
    asyncio.run(engine.evaluate({"a": _snap(0), "b": _snap(5_000_000)}, {}))

    used = rt.used
    assert used == 0, (
        f"b 刚上线时不应计入历史量，实际 used={used}"
    )
    print("✓ 后上线客户端不虚增本周期流量")


def test_normal_increment_counted():
    """正常增长应正确计入。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=1000, align="day")]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    asyncio.run(engine.evaluate({"a": _snap(1000)}, {}))
    asyncio.run(engine.evaluate({"a": _snap(1600)}, {}))

    assert rt.used == 600, f"应计入 600 字节，实际 {rt.used}"
    print("✓ 正常增量正确计入")


def test_rollback_resets_baseline():
    """客户端重启导致累计量回退时，基线应重新对齐。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=1000, align="day")]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    asyncio.run(engine.evaluate({"a": _snap(1_000_000)}, {}))
    # 客户端重启，累计量归零
    asyncio.run(engine.evaluate({"a": _snap(0)}, {}))
    assert rt.baseline["a"] == 0, "回退后基线应重置为当前值"

    # 之后正常增长
    asyncio.run(engine.evaluate({"a": _snap(500)}, {}))
    assert rt.used == 500, f"应计入 500，实际 {rt.used}"
    print("✓ 累计量回退后正确重算")


def test_progress_not_exploded():
    """即使数据异常，进度也不应出现天文数字。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=100, align="day")]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001
    rt.used = 500

    st = engine.states()[0]
    # 进度本身允许 >100%（表示超额），但前端会截断显示
    assert st.progress_total == 500.0
    assert st.current_total == 500
    print("✓ 超额时进度为 500%（前端截断显示）")


def test_ui_has_uniform_content_width():
    """所有页面应共用同一套内容宽度约束。

    之前各页面宽度不一致：仪表盘与 CD2 守卫内部有栅格撑开，观感正常；
    而 115 / PT 策略 / 系统设置 / 事件日志直接顶到两侧边缘。
    """
    import re

    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    # 存在统一的内容宽度容器
    assert "max-w-[1400px]" in ui, "缺少统一的内容宽度约束"
    assert "mx-auto" in ui, "内容未居中"

    # 该容器必须包住所有页面（在第一个页面之前、</main> 之前闭合）
    first_page = ui.find("tab==='dashboard'")
    wrap = ui.find("max-w-[1400px]")
    assert 0 < wrap < first_page, "宽度容器未包住页面内容"
    print("✓ 所有页面共用统一内容宽度")


def test_div_tags_balanced():
    """div 标签必须配对。

    之前合并页面时多/少写了一个 </div>，会导致后面的页面整体
    结构错位（样式错乱、内容溢出）。
    """
    import re

    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    b = ui.find("<body")
    e = ui.find("<script>")
    seg = re.sub(r"<!--.*?-->", "", ui[b:e], flags=re.DOTALL)
    opened = len(re.findall(r"<div\b", seg))
    closed = len(re.findall(r"</div>", seg))
    assert opened == closed, f"div 不配对: {opened} 开 / {closed} 闭"
    print(f"✓ div 标签配对（{opened} 对）")


def test_pt_page_has_skeleton():
    """PT 策略页首屏应有骨架，避免上半部分空白。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "ptReady" in ui, "缺少 PT 首屏就绪标志"
    assert 'v-if="!ptReady"' in ui, "缺少 PT 首屏骨架"
    print("✓ PT 策略页首屏骨架")


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
