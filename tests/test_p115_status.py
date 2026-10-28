"""115 云下载状态判定测试。

## 判据（对齐经实测验证的清理脚本）

    status 命中失败集合  → failed
    percentDone >= 100   → finished
    其余                 → 仍在下载

**关键：99% 属于未完成。** 曾经的实现用 `>= 99.9` 判完成，
把进度 99% 的正常下载任务误判成已完成，超时监控因此失效。

**status 不参与完成/删除判定。** 115 的 status 语义社区说法互相矛盾，
实测把 status=1 当作「已移除」导致 99% 的下载任务被标成已移除。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.services.p115_client import OfflineTask  # noqa: E402


def make_task(status: int, percent: float, hash_: str = "h1") -> OfflineTask:
    return OfflineTask(
        info_hash=hash_,
        name="test",
        size=1000,
        add_time=0,
        status=status,
        percent_done=percent,
    )


# ---------------------------------------------------------------- 进度判定
def test_99_percent_is_downloading():
    """99% 必须算「下载中」，不能算已完成。

    这是用户实际遇到的情况：SNOS-411-U 离线进度 99%，
    115 客户端显示正在下载，PTtools 却显示已移除。
    """
    t = make_task(1, 99.0)
    assert t.normalized_status == C.P115_ST_DOWNLOADING, (
        f"99% 应判为下载中，实际 {t.normalized_status}"
    )
    assert not t.is_terminal, "99% 的任务不能是终态"
    print("✓ 99% → 下载中（非终态）")


def test_100_percent_is_finished():
    """进度到 100% 才算完成。"""
    t = make_task(0, 100.0)
    assert t.normalized_status == C.P115_ST_FINISHED
    assert t.is_terminal
    print("✓ 100% → 已完成")


def test_zero_percent_is_downloading():
    """0% 也是未完成，需要参与超时判定。"""
    t = make_task(2, 0.0)
    assert t.normalized_status == C.P115_ST_DOWNLOADING
    assert not t.is_terminal
    print("✓ 0% → 下载中")


def test_status_does_not_affect_completion():
    """任何 status 都不应影响「是否完成」的判断。"""
    for status in (0, 1, 2, 3, 5, 11, 12):
        t = make_task(status, 99.0)
        assert t.normalized_status != C.P115_ST_FINISHED, (
            f"status={status} 且进度 99% 时不应判完成"
        )
        t2 = make_task(status, 100.0)
        assert t2.normalized_status == C.P115_ST_FINISHED, (
            f"status={status} 且进度 100% 时应判完成"
        )
    print("✓ status 不参与完成判定")


def test_failed_status_detected():
    """status=9 是明确的失败态。"""
    t = make_task(9, 50.0)
    assert t.normalized_status == C.P115_ST_FAILED
    assert t.is_terminal
    print("✓ status=9 → 失败")


def test_no_removed_state_from_status():
    """status 不应产生「已移除」——那是误判的来源。"""
    for status in (0, 1, 2, 3, 11, 12):
        t = make_task(status, 99.0)
        assert t.normalized_status != C.P115_ST_REMOVED, (
            f"status={status} 被判成已移除，会掩盖正在下载的任务"
        )
    print("✓ status 不再产生误判的「已移除」")


def test_constants_cleared():
    """用于误判的常量必须清空。"""
    assert C.P115_FINISHED_STATUS == set(), "不能再用 status 判完成"
    assert C.P115_REMOVED_STATUS == set(), "不能再用 status 判已移除"
    print("✓ 误判常量已清空")


def test_downloading_participates_in_timeout():
    """下载中的任务必须参与超时判定。"""
    t = make_task(1, 99.0)
    assert not t.is_terminal, "下载中必须是非终态，否则超时清理不生效"
    print("✓ 下载中参与超时判定")


# ---------------------------------------------------------------- 拉取策略
def test_fetches_running_separately():
    """应单独拉取「进行中」，避免被几百条完成记录挤掉。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "P115_STAT_RUNNING" in sched
    assert "merged" in sched
    print("✓ 单独拉取进行中任务")


# ---------------------------------------------------------------- 前端三类视图
def test_three_categories():
    """前端应分为「正在下载 / 已完成 / 取消日志」三类。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "downloadingTasks" in ui, "缺少「正在下载」分类"
    assert "finishedTasks" in ui, "缺少「已完成」分类"
    assert "p115Tab==='downloading'" in ui, "缺少「正在下载」视图"
    assert "p115Tab==='finished'" in ui, "缺少「已完成」视图"
    assert "p115Tab==='logs'" in ui, "缺少「取消日志」视图"

    # 默认进入「正在下载」
    assert "p115Tab = ref('downloading')" in ui, "默认子页签应为正在下载"
    print("✓ 三类视图（正在下载 / 已完成 / 取消日志）")


def test_finished_view_has_no_cancel():
    """已完成视图不应出现取消按钮。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    a = ui.find("p115Tab==='finished'")
    b = ui.find("p115Tab==='logs'")
    assert a > 0 and b > a
    seg = ui[a:b]
    assert "cancelOffline" not in seg, "已完成视图不应有取消操作"
    print("✓ 已完成视图无取消按钮")


def test_subtab_labels_use_computed_value():
    """JS 里读 computed 必须写 .value，否则标签显示 undefined。

    用户看到「正在下载 undefined」就是这个原因：模板里的自动解包
    只对模板生效，在 setup() 的 JS 代码中必须显式取 .value。
    """
    import re

    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    # 找出 p115SubTabs 定义块
    idx = ui.find("const p115SubTabs = computed(")
    assert idx > 0, "未找到 p115SubTabs"
    end = ui.find("]);", idx)
    block = ui[idx:end]

    # 模板字符串里引用的计数必须走 .value
    # 形如 `${downloadingTasks.value.length}`
    refs = re.findall(r"\$\{(\w+)(\.value)?\.length\}", block)
    assert refs, f"未找到计数引用: {block[:200]}"
    for var, has_value in refs:
        assert has_value, (
            f"${{{var}.length}} 缺少 .value，会渲染成 undefined"
        )
    print("✓ 子页签计数使用 .value（不会显示 undefined）")


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
