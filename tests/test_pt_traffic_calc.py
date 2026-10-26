"""PT 策略「本周期流量」计算正确性测试。

## 用户反馈

    界面上显示：已用 121.42 GB / 1.00 GB = 12142%

用户的质疑完全正确：即使 24 小时全速 100 KB/s 上传，上限也只有

    100 KB/s × 86400 s ≈ 8.24 GB

121 GB 是这个理论值的 14.7 倍，物理上不可能。

## 根因（两处）

1. **数据源用错**：qBittorrent 取的是 ``alltime_ul``（全历史累计），
   Transmission 取的是 ``cumulative-stats``（全历史累计）。
   这两个值可能是几百 GB，一旦基线丢失，周期增量会变成「整个历史」。

2. **缺少合理性校验**：即使数据异常，也没有任何截断，错误数字直接
   展示给用户并触发限速。

## 修复

1. 改用**本次会话**的上传量：
   qBittorrent → ``up_info_data``（官方文档：Data uploaded this session）
   Transmission → ``current-stats.uploadedBytes``
2. 周期增量超过「按限速跑满周期」的 1.5 倍时截断并告警。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.bt_policy import PolicyConfig, PolicyEngine  # noqa: E402

KB = 1024
MB = 1024**2
GB = 1024**3


# ---------------------------------------------------------------- 数据源
def _strip_comments(code: str) -> str:
    """去掉 Python 注释，避免注释里提到的字段名造成误判。"""
    import re

    code = re.sub(r"#[^\n]*", "", code)
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    return code


def test_qbittorrent_uses_session_field():
    """qBittorrent 必须用 up_info_data（本次会话），不能是 alltime_ul。"""
    src = _strip_comments(
        (ROOT / "app" / "services" / "bt_client.py").read_text(encoding="utf-8")
    )

    idx = src.find("class QBittorrentClient")
    end = src.find("class TransmissionClient")
    qb = src[idx:end]

    assert 'info.get("up_info_data")' in qb, "未使用会话级上传量"
    assert "alltime_ul" not in qb, (
        "仍在读取 alltime_ul（全历史累计），会导致周期流量爆表"
    )
    print("✓ qBittorrent 使用 up_info_data（本次会话）")


def test_transmission_uses_current_stats():
    """Transmission 必须用 current-stats，不能是 cumulative-stats。"""
    src = _strip_comments(
        (ROOT / "app" / "services" / "bt_client.py").read_text(encoding="utf-8")
    )
    idx = src.find("class TransmissionClient")
    tx = src[idx:]

    assert '"current-stats"' in tx, "未使用 current-stats"
    assert "cumulative-stats" not in tx, (
        "仍在使用 cumulative-stats（全历史累计）"
    )
    print("✓ Transmission 使用 current-stats（本次会话）")


# ---------------------------------------------------------------- 上限校验
def _run(engine, snaps, clients=None):
    return asyncio.run(engine.evaluate(snaps, clients or {}))


def test_absurd_delta_is_capped():
    """异常的周期增量必须被截断到物理上限附近。

    模拟：100 KB/s 限速、24h 周期，增量却报了 121 GB。
    上限 = 100KB/s × 86400 × 1.5 ≈ 12.4 GB，121 GB 应被截断。
    """
    engine = PolicyEngine()
    engine.apply_config(
        [
            PolicyConfig(
                name="p",
                threshold_total=1 * GB,
                limit_total=100 * KB,
                window="24h",
                align="day",
            )
        ]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    # 首轮建立基线
    _run(engine, {"qb": {"up_total": 0, "healthy": True}})
    # 第二轮：增量 121 GB（异常）
    _run(engine, {"qb": {"up_total": 121 * GB, "healthy": True}})

    cap = int(100 * KB * 86400 * 1.5)
    assert rt.used <= cap, f"未截断：used={rt.used}，上限={cap}"

    # 进度不应再是天文数字（12142%）
    st = engine.states()[0]
    assert st.progress_total < 2000, f"进度仍异常：{st.progress_total}%"
    print(f"✓ 异常增量被截断到 {rt.used / GB:.1f} GB（原本 121 GB）")


def test_normal_delta_not_capped():
    """正常范围内的增量不应被截断。"""
    engine = PolicyEngine()
    engine.apply_config(
        [
            PolicyConfig(
                name="p",
                threshold_total=10 * GB,
                limit_total=10 * MB,
                window="24h",
                align="day",
            )
        ]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    _run(engine, {"qb": {"up_total": 0, "healthy": True}})
    _run(engine, {"qb": {"up_total": 5 * GB, "healthy": True}})

    assert rt.used == 5 * GB, f"正常增量被误截断：{rt.used}"
    print("✓ 正常增量不受影响")


def test_no_limit_means_no_cap():
    """未设置限速（limit_total=0）时不做截断。"""
    engine = PolicyEngine()
    engine.apply_config(
        [PolicyConfig(name="p", threshold_total=1 * GB, limit_total=0)]
    )
    rt = engine._runtimes["p"]  # noqa: SLF001

    _run(engine, {"qb": {"up_total": 0, "healthy": True}})
    _run(engine, {"qb": {"up_total": 50 * GB, "healthy": True}})

    assert rt.used == 50 * GB
    print("✓ 未限速时不截断")


# ---------------------------------------------------------------- 物理上限
def test_theoretical_max_matches_user_reasoning():
    """验证用户的推算：100KB/s × 24h ≈ 8.24 GB。"""
    limit = 100 * KB
    cap = limit * 86400
    assert abs(cap / GB - 8.24) < 0.01, f"理论上限 {cap / GB:.2f} GB"
    print(f"✓ 100 KB/s × 24h = {cap / GB:.2f} GB（与用户推算一致）")


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
