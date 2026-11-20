"""115 云下载状态判定测试。

## 判据（首选 115 服务端分类，拿不到时按进度判断）

    在 stat=12（进行中）列表里          → downloading
    不在进行中列表且进度未满 100%       → waiting
    percentDone >= 100                  → finished
    status 命中失败集合（=9）           → failed

**关键：99% 属于未完成。** 曾经的实现用 `>= 99.9` 判完成，
把进度 99% 的正常下载任务误判成已完成，超时监控因此失效。

**关键：不能只按进度判断「是否在下载」。** 115 的「全部」列表会连历史
记录一起返回，其中大量老任务 percentDone 缺失或为 0，只按进度判断会把
它们永久计入「正在下载」—— 实测界面显示 25 个而网盘只有 4 个。

**status 不参与完成/删除判定。** 115 的 status 语义社区说法互相矛盾，
实测把 status=1 当作「已移除」导致 99% 的下载任务被标成已移除。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402
from app.services.p115_client import (  # noqa: E402
    OfflineTask,
    P115ClientWrapper,
    P115Error,
)


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
    """应单独拉取「进行中」（stat=12），并以服务端分类为准。

    115 的「全部」列表会连历史记录一起返回，仅凭 percentDone 判断会把
    大量老任务算成「下载中」——实测界面 25 个、网盘只有 4 个。
    stat=12 是 115 服务端自己的「正在下载」分类，必须用它来确认。
    """
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "collect_offline_snapshot" in sched, "未使用带服务端归类的采集"
    assert "is_running" in sched, "超时清理应只针对服务端确认在下载的任务"

    client = (ROOT / "app" / "services" / "p115_client.py").read_text(encoding="utf-8")
    assert "P115_STAT_RUNNING" in client, "未拉取「进行中」分类"
    assert "stat_bucket" in client, "缺服务端归字段"
    print("✓ 以 115 服务端 stat=12 分类为准")


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

    # 找出 p115SubTabs 定义块（到下一个顶层声明为止）
    idx = ui.find("const p115SubTabs = computed(")
    assert idx > 0, "未找到 p115SubTabs"
    end = ui.find("const thresholdHours", idx)
    assert end > idx, "无法确定 p115SubTabs 块的结束位置"
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


def _mk(hash_: str, status: int = 1, percent: float = 0.0) -> OfflineTask:
    return OfflineTask(
        info_hash=hash_,
        name=hash_,
        size=1,
        add_time=0,
        status=status,
        percent_done=percent,
    )


def _fake_client(
    running, all_tasks, running_exc=None, complete: bool = True
) -> P115ClientWrapper:
    """构造一个只实现 _list_offline_tasks 的假客户端。"""
    client = object.__new__(P115ClientWrapper)
    client.calls = []  # type: ignore[attr-defined]

    async def fake(page_size=30, max_pages=50, stat=0):
        client.calls.append(  # type: ignore[attr-defined]
            {"page_size": page_size, "max_pages": max_pages, "stat": stat}
        )
        if stat == C.P115_STAT_RUNNING:
            if running_exc is not None:
                raise running_exc
            return list(running), True
        return list(all_tasks), complete

    client._list_offline_tasks = fake  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------- 服务端归类
def test_server_bucket_wins():
    """有服务端归类时，进度判断不能再覆盖它。"""
    t = _mk("h", 1, 0.0)
    t.stat_bucket = C.P115_ST_WAITING
    assert t.normalized_status == C.P115_ST_WAITING, "服务端归类应优先"

    t2 = _mk("h2", 1, 50.0)
    t2.stat_bucket = C.P115_ST_DOWNLOADING
    assert t2.is_running, "stat=12 命中即「正在下载」"
    assert not t2.is_terminal

    print("✓ 服务端归类优先于进度判断")


def test_snapshot_classifies_by_stat12():
    """采集快照应按 stat=12 归类，而不是把低进度任务全算下载中。

    构造：running 只有 a；全部列表里还有 b（0%，不在进行中）、
    c（100%）、d（status=9）。期望 b→waiting、c→finished、d→failed。
    """
    client = _fake_client(
        running=[_mk("a", 1, 42.0)],
        all_tasks=[
            _mk("a", 1, 42.0),
            _mk("b", 1, 0.0),
            _mk("c", 1, 100.0),
            _mk("d", 9, 30.0),
        ],
    )
    snap = asyncio.run(client.collect_offline_snapshot())
    by = {t.info_hash: t for t in snap.tasks}

    assert len(snap.tasks) == 4, f"去重后应 4 条，实际 {len(snap.tasks)}"
    assert snap.running_count == 1
    assert snap.running_ok is True

    assert by["a"].normalized_status == C.P115_ST_DOWNLOADING
    assert by["b"].normalized_status == C.P115_ST_WAITING, (
        "不在 stat=12 且进度未满的任务应为 waiting，"
        "否则历史记录会被永久算作「正在下载」"
    )
    assert by["c"].normalized_status == C.P115_ST_FINISHED
    assert by["d"].normalized_status == C.P115_ST_FAILED

    print("✓ 按 stat=12 归类（downloading/waiting/finished/failed）")


def test_snapshot_falls_back_when_running_fails():
    """拉取「进行中」失败时回退到进度判断，不能把整表当成非下载。"""
    client = _fake_client(
        running=[],
        all_tasks=[_mk("x", 1, 5.0)],
        running_exc=P115Error("boom"),
    )
    snap = asyncio.run(client.collect_offline_snapshot())

    assert snap.running_ok is False, "应记录归类失败"
    assert snap.tasks[0].normalized_status == C.P115_ST_DOWNLOADING, (
        "拿不到服务端分类时必须回退到进度判断"
    )
    print("✓ 归类失败时回退到进度判断")


def test_snapshot_raises_page_size():
    """采集时必须把页大小抬到至少 100。

    用户设置里的页大小可能只有 30，配上 50 页上限就只能翻 1500 条；
    115 的历史记录很容易超过这个数，一旦翻不完 complete=False，
    消失判定会被整体跳过 —— 那又会回到「界面 25 个、网盘 4 个」。
    """
    client = _fake_client(running=[], all_tasks=[_mk("z", 1, 0.0)])
    asyncio.run(client.collect_offline_snapshot(page_size=30))

    assert client.calls, "未发起任何请求"
    for call in client.calls:
        assert call["page_size"] >= 100, f"页大小未抬高: {call}"
    print("✓ 采集页大小不低于 100")


def test_snapshot_reports_incomplete():
    """翻页被截断时必须如实报告，交给上层跳过消失判定。"""
    client = _fake_client(running=[], all_tasks=[_mk("z", 1, 0.0)], complete=False)
    snap = asyncio.run(client.collect_offline_snapshot())

    assert snap.complete is False, "未如实报告列表不完整"
    print("✓ 列表不完整时如实上报")


def test_vanish_needs_complete_list():
    """「全部」列表没翻完时不能判定任务消失。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "if not complete:" in sched, "缺少完整翻页保护"
    assert "complete=snapshot.complete" in sched, "未把 complete 传入同步"
    assert "P115_VANISH_GRACE_SEC" in sched, "消失判定未使用统一宽限期"
    assert C.P115_VANISH_GRACE_SEC <= 3600, "宽限期过长会让已取消任务长期占位"
    print("✓ 消失判定受完整翻页与宽限期约束")


def test_waiting_not_auto_cancelled():
    """自动取消只针对服务端确认在下载的任务。

    waiting 里可能混着 115 的历史记录，批量取消会误删。
    """
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    idx = sched.find("expired = [")
    assert idx > 0, "未找到超时筛选"
    seg = sched[idx : idx + 260]
    assert "t.is_running" in seg, "超时筛选应限定 is_running"
    assert "not t.is_terminal" not in seg, "不应再用「非终态」作为筛选条件"
    print("✓ 自动取消只针对服务端确认在下载的任务")


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
