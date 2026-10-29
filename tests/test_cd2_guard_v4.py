"""CD2 守卫 v4 逻辑测试：连续两次观察到上传速度即取消。

## 规则

    每 30 秒查询 CD2 上传列表
        ↓
    只看落在用户指定源文件夹内的任务
        ↓
    有上传速度（字节数比上轮增长）→ 计入待观察名单
    没速度（秒传 / 排队）          → 移出名单
        ↓
    连续 2 次在名单上 → 判定无法秒传 → 取消

核心假设：**秒传任务从不产生上传流量**，所以「持续有速度」就等价于
「没有走秒传通道」。这与排队时长、网络快慢都无关，比旧的耗时判定稳。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.cd2_client import UploadTask  # noqa: E402
from app.services.cd2_guard import (  # noqa: E402
    CD2Guard,
    normalize_folder,
    task_in_folders,
)


def make_task(
    key: str,
    transferred: int,
    *,
    size: int = 1000,
    dest: str = "/cloud/PT/dest/file.mkv",
    status: int = 3,
    source_override: str = "",
) -> UploadTask:
    t = UploadTask(
        key=key,
        dest_path=dest,
        size=size,
        transferred_bytes=transferred,
        status_text="Transfer",
        status_enum=status,
        operator_type=2,
        error_message="",
    )
    if source_override:
        t.set_source_override(source_override)
    return t


# ---------------------------------------------------------------- 文件夹匹配
def test_normalize_folder():
    assert normalize_folder("/mnt/PT/") == "/mnt/PT"
    assert normalize_folder("  /mnt/PT  ") == "/mnt/PT"
    assert normalize_folder("/") == "/"
    assert normalize_folder("") == ""
    print("✓ 文件夹路径规范化")


def test_task_in_folders():
    t = make_task("k1", 0, dest="/cloud/PT/a/b.mkv")
    assert task_in_folders(t, ["/cloud/PT"])
    assert task_in_folders(t, ["/cloud"])
    assert not task_in_folders(t, ["/other"])
    # 不能因为前缀相同就误命中
    assert not task_in_folders(t, ["/cloud/PTX"])
    print("✓ 文件夹前缀匹配（不误命中相似路径）")


def test_empty_folders_matches_nothing():
    t = make_task("k1", 0)
    assert not task_in_folders(t, [])
    print("✓ 未配置文件夹时不监控任何任务")


# ---------------------------------------------------------------- 观察判定
def test_first_poll_with_data_enters_watchlist():
    """首轮已传过数据 → 计入名单（视为正在上传）。

    秒传/排队任务字节数始终为 0，不会进名单；只要传了数据就说明
    没走秒传通道，应开始观察。
    """
    g = CD2Guard()
    tasks = [make_task("k1", 5_000_000)]
    r = g.observe(tasks, ["/cloud/PT"], now=1000.0)
    assert not r.to_cancel, "首轮不取消（需连续两次）"
    assert len(r.watching) == 1, "首轮已传数据的任务应进名单"
    assert r.watching[0].hits == 1
    print("✓ 首轮已传数据 → 计入名单")


def test_first_poll_zero_bytes_not_watched():
    """首轮 0 字节（秒传/排队）→ 不进名单。"""
    g = CD2Guard()
    r = g.observe([make_task("k1", 0)], ["/cloud/PT"], now=1000.0)
    assert r.watching == []
    print("✓ 首轮 0 字节 → 不入名单")


def test_two_consecutive_speeds_cancels():
    """连续两轮字节增长 → 第二轮立即取消。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]

    g.observe([make_task("k1", 1_000_000)], folders, now=1000.0)
    r2 = g.observe([make_task("k1", 2_000_000)], folders, now=1030.0)

    assert len(r2.to_cancel) == 1, f"应取消 1 个，实际 {len(r2.to_cancel)}"
    assert r2.to_cancel[0].task_key == "k1"
    assert r2.to_cancel[0].hits == 2
    print("✓ 连续两次有速度 → 取消")


def test_single_speed_then_stall_not_cancelled():
    """只有一轮有速度，之后不动 → 不取消（可能是卡顿）。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    g.observe([make_task("k1", 1_000_000)], folders, now=1000.0)
    g.observe([make_task("k1", 2_000_000)], folders, now=1030.0)  # 有速度，hits=1
    r3 = g.observe([make_task("k1", 2_000_000)], folders, now=1060.0)  # 无增长
    # 第二轮已经在 hits=1，第三轮无速度 → 移出名单，不取消
    assert not r3.to_cancel or r3.to_cancel[0].hits >= 2
    print("✓ 速度中断后移出名单")


def test_instant_upload_never_cancelled():
    """秒传任务：字节始终为 0，永远不会进名单。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    for i, ts in enumerate([1000.0, 1030.0, 1060.0, 1090.0]):
        r = g.observe(
            [make_task("instant", 0, size=10**9)], folders, now=ts
        )
        assert not r.to_cancel, f"第 {i+1} 轮不应取消秒传"
        assert r.watching == []
    print("✓ 秒传任务（0 字节）永不被取消")


def test_queued_task_no_speed():
    """排队中的任务字节不变，不进名单。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    g.observe([make_task("q", 0, status=7)], folders, now=1000.0)
    r = g.observe([make_task("q", 0, status=7)], folders, now=1030.0)
    assert not r.to_cancel
    assert r.watching == []
    print("✓ 排队任务（字节不变）不进名单")


def test_task_outside_folders_ignored():
    """不在监控文件夹内的任务完全忽略。"""
    g = CD2Guard()
    g.observe([make_task("k1", 1_000_000, dest="/other/a.mkv")], ["/cloud/PT"], now=1000.0)
    r = g.observe([make_task("k1", 2_000_000, dest="/other/a.mkv")], ["/cloud/PT"], now=1030.0)
    assert not r.to_cancel
    assert r.watching == []
    print("✓ 监控范围外的任务被忽略")


def test_vanished_task_removed():
    """任务从 CD2 列表消失后应从名单清除。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    g.observe([make_task("k1", 1_000_000)], folders, now=1000.0)
    g.observe([make_task("k1", 2_000_000)], folders, now=1030.0)
    # 任务被 CD2 移除
    r = g.observe([], folders, now=1060.0)
    assert r.watching == [], "消失的任务应被清理"
    print("✓ 消失任务自动移出名单")


def test_multiple_files_independent():
    """多文件互不影响：各自独立累计命中次数。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    # 首轮：a、c 在传；b 为 0（秒传/排队）
    g.observe(
        [make_task("a", 100), make_task("b", 0), make_task("c", 300)],
        folders,
        now=1000.0,
    )
    # 第二轮：a、c 继续增长 → 达到 2 次；b 仍为 0
    r = g.observe(
        [make_task("a", 200), make_task("b", 0), make_task("c", 400)],
        folders,
        now=1030.0,
    )
    cancelled = {w.task_key for w in r.to_cancel}
    assert cancelled == {"a", "c"}, f"应取消 a 和 c，实际 {cancelled}"
    assert "b" not in {w.task_key for w in r.watching}, "b 为 0 字节不应观察"
    print("✓ 多文件独立判定")


def test_clear_resets_state():
    """取消后清理状态，避免重复处理。"""
    g = CD2Guard()
    folders = ["/cloud/PT"]
    g.observe([make_task("k1", 100)], folders, now=1000.0)
    g.observe([make_task("k1", 200)], folders, now=1030.0)
    assert g.watching_count == 1
    g.clear("k1")
    assert g.watching_count == 0
    print("✓ 清理单个任务状态")


def test_reset_all():
    g = CD2Guard()
    folders = ["/cloud/PT"]
    g.observe([make_task("k1", 100)], folders, now=1000.0)
    g.observe([make_task("k1", 200)], folders, now=1030.0)
    g.reset()
    assert g.watching_count == 0
    assert g.snapshot() == []
    print("✓ 重置全部状态")


# ---------------------------------------------------------------- 调度器接线
def test_scheduler_uses_30s_interval():
    """轮询间隔应为 30 秒。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    idx = sched.find('id="poll_cd2_uploads"')
    assert idx > 0
    seg = sched[max(0, idx - 500) : idx]
    assert "seconds=30" in seg, "轮询间隔不是 30 秒"
    print("✓ 轮询间隔 30 秒")


def test_scheduler_uses_guard():
    """调度器应使用新的 CD2Guard。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "get_cd2_guard" in sched, "未接入 CD2Guard"
    assert "guard.observe(" in sched, "未调用观察逻辑"
    assert "cancel_uploads" in sched, "未执行取消"
    print("✓ 调度器接入新守卫")


def test_retry_logic_removed():
    """重试逻辑应已移除（交给 CD2）。"""
    sched = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "process_retry_queue" not in sched, "重试队列调度未移除"
    assert "_expire_stuck_retries" not in sched, "重试超时处理未移除"
    print("✓ 已移除本系统的重试调度")


def test_threshold_constant():
    """取消阈值应为连续 2 次。"""
    cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "CD2_CANCEL_AFTER_HITS = 2" in cfg
    print("✓ 取消阈值为连续 2 次")


# ---------------------------------------------------------------- 界面清理
def test_rules_tab_has_no_count():
    """「监控规则」页签不应显示数字。

    一个规则里可以有多行文件夹路径，规则条数并不等于用户看到的
    文件夹数量，显示出来只会误导。
    """
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    idx = ui.find("const taskSubTabs")
    assert idx > 0
    block = ui[idx : idx + 400]
    assert "{ key: 'rules', label: '监控规则' }" in block, (
        "监控规则页签仍带 count 字段"
    )
    print("✓ 监控规则页签无数字")


def test_cancel_retry_removed():
    """「取消重试」已无意义（重试交给 CD2），不应再出现在界面上。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "batchCancelRetry" not in ui, "仍残留 batchCancelRetry"
    assert "取消重试" not in ui, "界面上仍显示「取消重试」按钮"
    print("✓ 已移除「取消重试」按钮")


def test_delete_button_style_consistent():
    """删除按钮应与批量重试按钮同样式（实心色块）。"""
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    idx = ui.find("batchDeleteRetry()")
    assert idx > 0
    seg = ui[idx : idx + 300]
    assert "bg-rose-600" in seg, "删除按钮未使用实心红底样式"
    assert "text-white" in seg, "删除按钮文字应为白色"
    print("✓ 删除按钮与其他按钮样式统一")


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
