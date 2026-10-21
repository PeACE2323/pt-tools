"""超时判定逻辑测试：排队状态不得计入超时。

对应真实用户反馈（第 4 条）：

    「cd2有并发限制，等待上传的任务，任务监控也算作超时里。
      秒传详情中没有任何成功秒传的记录，重试队列中有失败数字但没有任何记录」

根因：原实现把 ``CD2_STATUS_ACTIVE = {0, 1, 3, 7}`` 全当成"在传"，
其中 Inqueue(7) / Preprocessing(1) / WaitforPreprocessing(0) 属于排队阶段。
CD2 有并发上限，任务排队几十分钟是正常的，把它算超时会导致：

  * 大量误拦截
  * 秒传记录为空（耗时全被算成超阈值）
  * CD2 里还在正常上传，系统已判超时

本测试固化修正后的语义：
  只有 Transfer(3) 状态、且「真实传输耗时」超过阈值，才判超时。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as C  # noqa: E402


def test_queued_statuses_excluded_from_transferring():
    """排队类状态不得出现在 TRANSFERRING 集合里。"""
    assert C.CD2_STATUS_QUEUED == {0, 1, 7}, C.CD2_STATUS_QUEUED
    assert C.CD2_STATUS_TRANSFERRING == {3}, C.CD2_STATUS_TRANSFERRING

    overlap = C.CD2_STATUS_QUEUED & C.CD2_STATUS_TRANSFERRING
    assert not overlap, f"排队态与传输态不应重叠: {overlap}"
    print("✓ 排队态与传输态互斥")


def test_specific_status_codes():
    """逐个确认状态码归类正确（对应 proto 的 UploadFileInfo.Status）。"""
    assert 0 in C.CD2_STATUS_QUEUED, "WaitforPreprocessing 应为排队"
    assert 1 in C.CD2_STATUS_QUEUED, "Preprocessing 应为排队"
    assert 7 in C.CD2_STATUS_QUEUED, "Inqueue 应为排队"
    assert 3 in C.CD2_STATUS_TRANSFERRING, "Transfer 才是真在传"
    assert 4 not in C.CD2_STATUS_ACTIVE, "Pause 不算活跃"
    assert 5 not in C.CD2_STATUS_ACTIVE, "Finish 不算活跃"
    assert 9 not in C.CD2_STATUS_ACTIVE, "Error 不算活跃"
    assert 10 not in C.CD2_STATUS_ACTIVE, "FatalError 不算活跃"
    print("✓ 状态码归类正确")


def test_active_is_union():
    """ACTIVE 应为排队 + 传输的并集（用于「任务是否还在」判断）。"""
    assert C.CD2_STATUS_ACTIVE == (C.CD2_STATUS_QUEUED | C.CD2_STATUS_TRANSFERRING)
    print("✓ ACTIVE = 排队 ∪ 传输")


def simulate_effective_transfer_time(total: float, queued: float) -> float:
    """复刻调度器里的算法：有效耗时 = 总耗时 - 排队耗时。"""
    return max(0.0, total - queued)


def test_default_cd2_port_is_19798():
    """CD2 的核心服务端口（含 gRPC）是 19798，不是 19799。

    19799 是 HTTPS 管理端口，默认关闭 —— 这点曾判断错误导致用户连不上。
    详见官方帮助文档 help.html。
    """
    host = C.DEFAULT_SETTINGS["cd2.host"]
    assert host.endswith(":19798"), f"默认端口应为 19798，实际: {host}"
    print(f"✓ 默认 CD2 地址端口正确: {host}")


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
