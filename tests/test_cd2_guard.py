"""CD2 超时守卫的核心逻辑测试。

## 架构变更（v3）

系统已放弃「秒传统计」，只做一件真正可靠的事：

    监控上传任务 → 超过阈值（默认 60 秒）仍未完成 → 取消该文件上传
                 → 写入重试队列 → 间隔到期后触发备份重扫

原因：CD2 的 ``GetUploadFileList`` 只返回**当前队列**，任务一旦完成就
立刻从列表消失，也没有任何「已完成任务历史」接口。因此「秒传有多少个」
这类统计必然不完整 —— 与其展示不可靠的数字，不如不展示。

本测试锁定改造后的行为：
1. 任务完成时不再写秒传记录
2. 任务消失时不做秒传判定
3. 超时判定仍然保留（这是核心功能）
4. 前端不再有「实时上传 / 秒传详情」页签
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHED = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
UI = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CFG = (ROOT / "app" / "config.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 后端
def test_no_instant_recording():
    """完成的任务不应再写秒传记录。"""
    assert "_record_instant_upload" not in SCHED, "秒传记录逻辑未移除"
    assert "InstantUpload(" not in SCHED, "仍在写入秒传表"
    print("✓ 已移除秒传记录写入")


def test_vanished_no_instant_judgement():
    """任务消失时不再判定秒传。"""
    idx = SCHED.find("def _handle_task_vanished")
    assert idx > 0
    body = SCHED[idx : idx + 900]
    assert "transferred_peak" not in body, "消失处理仍在判定秒传"
    assert "秒传完成" not in body, "消失处理仍记录秒传"
    print("✓ 任务消失不再判定秒传")


def test_timeout_detection_kept():
    """超时判定是核心功能，必须保留。"""
    assert "CD2_STATUS_TRANSFERRING" in SCHED, "缺少传输中状态判定"
    assert "C.CD2_STATUS_QUEUED" in SCHED, "缺少排队状态判定"
    assert "intercept_queue" in SCHED, "缺少拦截队列"
    # 只有真正传输中才计入超时
    idx = SCHED.find("if task.status_enum in C.CD2_STATUS_TRANSFERRING:")
    assert idx > 0, "未找到传输中判定"
    seg = SCHED[idx : idx + 400]
    assert "timeout_threshold" in seg, "传输中判定未使用超时阈值"
    print("✓ 超时判定保留（传输中才计时）")


def test_default_timeout_is_one_minute():
    """默认超时阈值应为 60 秒（1 分钟）。"""
    models = (ROOT / "app" / "models.py").read_text(encoding="utf-8")
    idx = models.find("timeout_threshold")
    seg = models[idx : idx + 200]
    assert "default=60" in seg, f"默认阈值不是 60 秒: {seg[:100]}"
    print("✓ 默认超时 60 秒")


def test_intercept_writes_retry_queue():
    """超时任务必须写入重试队列。"""
    idx = SCHED.find("async def _intercept_tasks")
    assert idx > 0
    body = SCHED[idx : idx + 2600]
    assert "NonInstantTask(" in body, "拦截后未写重试队列"
    assert "cancel" in body.lower(), "拦截未执行取消动作"
    print("✓ 超时后写重试队列并取消上传")


# ---------------------------------------------------------------- 前端
def test_no_live_or_instant_subtabs():
    """前端不应再有「实时上传」与「秒传详情」页签。

    注意：注释里会提到这些词（解释为什么移除），所以只检查**非注释**部分。
    """
    import re

    # 去掉 HTML 注释与 JS 行注释
    stripped = re.sub(r"<!--.*?-->", "", UI, flags=re.DOTALL)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"^\s*\*.*$", "", stripped, flags=re.MULTILINE)

    assert "实时上传" not in stripped, "仍存在「实时上传」"
    assert "秒传详情" not in stripped, "仍存在「秒传详情」"
    assert "taskTab==='live'" not in stripped
    assert "taskTab==='instant'" not in stripped
    print("✓ 前端已移除实时上传与秒传详情")


def test_rules_merged_into_tasks():
    """CD2 规则与任务监控应合并为同一页签。"""
    import re

    # 只检查页签定义区（tabs 数组）
    idx = UI.find("const tabs = [")
    end = UI.find("];", idx)
    seg = UI[idx:end]
    assert "key: 'rules'" not in seg, "CD2 规则仍是独立页签"
    assert "key: 'tasks'" in seg
    assert "CD2 守卫" in seg, "页签名未更新为 CD2 守卫"

    # 规则作为子页签
    assert "taskTab==='rules'" in UI, "规则未并入子页签"
    print("✓ 规则与任务已合并为「CD2 守卫」")


def test_task_subtabs_keys():
    """子页签只保留规则与拦截重试。"""
    idx = UI.find("const TASK_SUB_KEYS")
    assert idx > 0
    seg = UI[idx : idx + 120]
    assert "'rules'" in seg and "'retry'" in seg, seg
    assert "'live'" not in seg and "'instant'" not in seg, seg
    print("✓ 子页签为「监控规则 / 拦截与重试」")


def test_dashboard_no_instant_stats():
    """仪表盘不再展示秒传指标。"""
    assert "today_instant" not in UI, "仪表盘仍显示今日秒传"
    assert "total_instant" not in UI, "仪表盘仍显示累计秒传"
    assert "超时守卫" in UI, "仪表盘卡片未改名"
    print("✓ 仪表盘改为超时守卫指标")


def test_dashboard_stats_has_no_instant():
    """后端统计不再返回秒传字段。"""
    dash = (ROOT / "app" / "api" / "dashboard.py").read_text(encoding="utf-8")
    assert "today_instant" not in dash, "后端仍统计秒传"
    assert "live_uploads" in dash, "缺少监控中任务数"
    print("✓ 后端统计不再含秒传")


# ---------------------------------------------------------------- 配置
def test_gone_grace_still_tiered():
    """任务消失宽限仍应分档（状态要及时跟上 CD2）。"""
    assert "TASK_GONE_FAST_SEC" in CFG
    assert "TASK_GONE_GRACE_SEC" in CFG
    assert "TASK_GONE_FAST_SEC" in SCHED, "调度器未使用快速宽限"
    print("✓ 消失宽限分档保留")


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
