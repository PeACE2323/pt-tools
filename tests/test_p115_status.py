"""115 云下载状态判定测试。

## 背景（用户反馈）

    「pttools 的云下载界面只监控到了完成的任务，
      但实际上 115 客户端里有两个任务正在下载」

根因：归一化状态时，只要原始 ``status`` 落在 ``{2, 11}`` 就直接判为
「已完成」，完全无视进度。结果进度 0% / 99% 的任务全被标成已完成：

  * 界面显示不对
  * 超时监控彻底失效（已完成的任务不参与超时判定）

115 的 status 数值语义在社区资料里互相矛盾，不可依赖。现在的原则是
**以进度为准，status 只用来识别明确的失败/删除态**。
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


# ---------------------------------------------------------------- 状态归一化
def test_running_task_not_marked_finished():
    """进度未满的任务不能因为 status 被判成已完成。"""
    for status in (0, 1, 2, 3, 11):
        t = make_task(status, 0.0)
        assert t.normalized_status != C.P115_ST_FINISHED, (
            f"status={status} 进度 0% 却被判为已完成"
        )
    print("✓ 0% 进度的任务不会被误判为已完成")


def test_partial_progress_is_downloading():
    """有进度但未满 → 下载中。"""
    t = make_task(2, 99.0)
    assert t.normalized_status == C.P115_ST_DOWNLOADING
    print("✓ 99% → 下载中")


def test_full_progress_is_finished():
    """进度走满才算完成。"""
    t = make_task(0, 100.0)
    assert t.normalized_status == C.P115_ST_FINISHED
    assert t.is_terminal
    print("✓ 100% → 已完成")


def test_zero_progress_is_waiting():
    """完全没有进度 → 等待中（不应是已完成）。"""
    t = make_task(3, 0.0)
    assert t.normalized_status == C.P115_ST_WAITING
    print("✓ 0% → 等待中")


def test_failed_status_detected():
    """status=9 是明确的失败态。"""
    t = make_task(9, 0.0)
    assert t.normalized_status == C.P115_ST_FAILED
    assert t.is_terminal
    print("✓ status=9 → 失败")


def test_removed_status_detected():
    """status 命中已删除集合 → removed。"""
    for status in C.P115_REMOVED_STATUS:
        t = make_task(status, 50.0)
        assert t.normalized_status == C.P115_ST_REMOVED
        assert t.is_terminal
    print("✓ 已删除状态正确识别")


def test_downloading_not_terminal():
    """下载中的任务不是终态，必须参与超时判定。"""
    t = make_task(2, 50.0)
    assert not t.is_terminal, "下载中的任务不能是终态，否则超时监控失效"
    print("✓ 下载中 → 非终态（参与超时判定）")


def test_finished_status_set_empty():
    """P115_FINISHED_STATUS 必须为空 —— 不允许再用 status 判完成。"""
    assert C.P115_FINISHED_STATUS == set(), (
        "P115_FINISHED_STATUS 应为空集；用 status 判完成会导致"
        "正在下载的任务被误标为已完成"
    )
    print("✓ 不再用 status 判定完成")


# ---------------------------------------------------------------- 拉取策略
def test_fetches_running_tasks_first():
    """应单独拉取「进行中」任务，避免被完成记录挤掉。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "P115_STAT_RUNNING" in sched, "未单独拉取进行中的任务"
    assert "merged" in sched, "未合并两组任务"
    print("✓ 单独拉取进行中的任务")


def test_stat_constants_defined():
    """stat 参数常量应齐全。"""
    assert C.P115_STAT_ALL == 0
    assert C.P115_STAT_RUNNING == 12
    print("✓ stat 常量齐全")


# ---------------------------------------------------------------- 前端
def test_finished_tasks_hide_cancel_button():
    """已完成任务不应显示「取消任务」。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    idx = ui.find("cancelOffline(t)")
    assert idx > 0
    # 找到该按钮所在的标签，应带 v-if="!t.is_terminal"
    seg = ui[max(0, idx - 400) : idx]
    assert 'v-if="!t.is_terminal"' in seg, (
        "取消按钮未按 is_terminal 隐藏，已完成任务仍可点取消"
    )
    print("✓ 已结束的任务不显示取消按钮")


def test_api_exposes_is_terminal():
    """后端应把 is_terminal 暴露给前端。"""
    sch = (ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
    assert "is_terminal" in sch

    api = (ROOT / "app" / "api" / "p115.py").read_text(encoding="utf-8")
    assert "item.is_terminal" in api, "API 层未填充 is_terminal"
    print("✓ API 暴露 is_terminal")


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
