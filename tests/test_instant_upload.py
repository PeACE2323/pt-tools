"""秒传判定逻辑测试。

秒传的**正确概念**（来自 CD2 官方文档与通用秒传机制）：

    上传前客户端先算出文件哈希发个服务器；服务器发现该文件已存在
    （任何用户的网盘里都有相同哈希），就**不接收文件数据**，
    只建一条指向已有数据的记录。

因此秒传的可靠特征不是「耗时短」，而是 **没有真正传输过数据**：

    transferedBytes ≈ 0   （相对于文件大小）

之前用「耗时 ≤ 阈值」判定，导致：
  * 排队久的秒传任务被误判成非秒传 -> 秒传记录为空
  * 网络快时的普通上传被误算成秒传

参考：https://juejin.cn/post/7321049399282827274
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cd2_client import UploadTask  # noqa: E402


def make_task(size: int, transferred: int, status: int = 5) -> UploadTask:
    """构造一个已完成的上传任务。"""
    return UploadTask(
        key="k1",
        dest_path="/115/CloudDrive/PT/movies/a.mkv",
        size=size,
        transferred_bytes=transferred,
        status_text="Finish",
        status_enum=status,
        operator_type=2,
        error_message="",
    )


# ---------------------------------------------------------------- 字节判据
def test_instant_upload_zero_bytes():
    """典型秒传：完全没有传输数据（1GB 文件，传了 0 字节）。"""
    task = make_task(size=1024**3, transferred=0)
    assert task.is_instant_by_bytes is True
    assert task.is_real_upload_by_bytes is False
    print("✓ 0 字节传输 → 判定秒传")


def test_instant_upload_tiny_overhead():
    """秒传允许极少量的内部读取（预处理统计），仍应判为秒传。"""
    size = 1024**3  # 1GB
    task = make_task(size=size, transferred=int(size * 0.005))  # 0.5%
    assert task.is_instant_by_bytes is True
    print("✓ 0.5% 传输量 → 仍判秒传（容差内）")


def test_real_upload_fully_transferred():
    """正常上传：传输量等于文件大小。"""
    size = 500 * 1024**2
    task = make_task(size=size, transferred=size)
    assert task.is_real_upload_by_bytes is True
    assert task.is_instant_by_bytes is False
    print("✓ 100% 传输量 → 判定为真实上传")


def test_real_upload_partial_before_finish():
    """即使只传了一部分，只要超过容差就不是秒传。"""
    size = 100 * 1024**2
    task = make_task(size=size, transferred=int(size * 0.5))
    assert task.is_real_upload_by_bytes is True
    print("✓ 50% 传输量 → 判定为真实上传")


def test_unknown_size_falls_back():
    """size 为 0 时无法用字节判定，返回 True 交由调用方用耗时兜底。"""
    task = make_task(size=0, transferred=0)
    assert task.is_instant_by_bytes is True, "无大小时应允许耗时兜底判定"
    print("✓ size=0 → 交由耗时兜底")


def test_transferred_ratio():
    size = 200
    task = make_task(size=size, transferred=50)
    assert abs(task.transferred_ratio - 0.25) < 1e-9
    print("✓ 传输比例计算正确")


# ---------------------------------------------------------------- 调度器逻辑
def test_scheduler_uses_bytes_not_duration():
    """调度器必须以字节数为主判据，耗时只做兜底。"""
    src = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "services"
        / "scheduler.py"
    ).read_text(encoding="utf-8")

    assert "transferred" in src and "size" in src, "应基于传输字节数判定"
    assert "ratio >= 0.01" in src, "应有 1% 容差判定"
    assert "no-data-transfer" in src, "应标记判定依据"
    print("✓ 调度器以字节数为主判据")


def test_vanished_task_handled():
    """任务从列表消失时必须判定是否为秒传，而不是直接丢弃。

    秒传可能太快，在两次轮询之间就完成并消失 —— 若直接标 gone 会漏记。
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "services"
        / "scheduler.py"
    ).read_text(encoding="utf-8")

    assert "_handle_task_vanished" in src, "应处理任务消失的情况"
    assert "任务消失且未传输数据" in src, "应识别消失+零传输=秒传"
    print("✓ 已处理「秒传快到看不见 Finish」的情况")


def test_vanished_failed_task_not_instant():
    """消失前状态是失败的，不应记为秒传。"""
    src = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "services"
        / "scheduler.py"
    ).read_text(encoding="utf-8")

    # 消失处理函数里应检查失败状态
    idx = src.find("_handle_task_vanished")
    body = src[idx: idx + 1600]
    assert "CD2_STATUS_FAILED" in body, "消失判定应排除失败状态"
    assert "matched_rule_id is None" in body, "消失判定应排除未命中规则的任务"
    print("✓ 消失判定已排除失败任务与未命中规则的任务")


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
