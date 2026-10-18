"""健康检查与状态维护测试。

对应用户反馈（第 1 条）：

    「重启后仪表盘的 CD2 连接状态和 115 账号状态都会失败，
      必须到系统设置中手动点击测试连接后才恢复正常」

根因：仪表盘读的是 ``cd2.status.connected`` / ``p115.status.logged_in``，
而这两个字段原先**只在手动调用 test_connection() / check_login() 时赋值**。
定时任务虽然一直在跑，但只写 ``stats``，不更新 ``status``。
于是容器重启后仪表盘永远是初始值（失败），直到用户手动点一次测试。

本测试确保：
1. 存在定时健康检查任务，且启动时立即执行
2. 轮询成功/失败会同步更新 status
3. status 更新方法本身行为正确
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHED = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
CD2 = (ROOT / "app" / "services" / "cd2_client.py").read_text(encoding="utf-8")
P115 = (ROOT / "app" / "services" / "p115_client.py").read_text(encoding="utf-8")
DASH = (ROOT / "app" / "api" / "dashboard.py").read_text(encoding="utf-8")


def test_health_check_job_registered():
    """必须注册定时健康检查任务。"""
    assert "id=\"health_check\"" in SCHED, "缺少 health_check 任务"
    assert "async def health_check" in SCHED
    print("✓ 已注册 health_check 定时任务")


def test_health_check_runs_on_startup():
    """启动时必须立即跑一次，否则首屏仍是错误状态。"""
    idx = SCHED.find("async def start")
    body = SCHED[idx: idx + 4000]
    assert "await self.health_check()" in body, "启动时未立即执行健康检查"
    print("✓ 启动时立即执行健康检查")


def test_cd2_mark_status_exists():
    """CD2 客户端需要能由外部更新状态。"""
    assert "def mark_status" in CD2, "CD2Client 缺少 mark_status"
    print("✓ CD2Client.mark_status 存在")


def test_p115_mark_status_exists():
    assert "def mark_status" in P115, "P115ClientWrapper 缺少 mark_status"
    print("✓ P115ClientWrapper.mark_status 存在")


def test_poll_updates_cd2_status():
    """CD2 轮询结果必须同步到 status（不只是 stats）。"""
    idx = SCHED.find("async def poll_cd2_uploads")
    body = SCHED[idx: idx + 3500]

    assert "mark_status" in body, "轮询未更新 status"
    # 成功与失败路径都要更新
    assert body.count("mark_status") >= 2, "成功/失败路径都应更新 status"
    print("✓ CD2 轮询同步更新 status")


def test_dashboard_reads_status_fields():
    """仪表盘必须读取 status（而非仅 stats），确认两端一致。"""
    assert "cd2_status.connected" in DASH
    assert "p115_status.logged_in" in DASH
    print("✓ 仪表盘读取 status 字段")


def test_no_full_status_reset_in_check_login():
    """check_login 不得整体重置 status。

    原先写 ``self._status = P115Status(...)``，会把 task_count 等信息清空。
    """
    idx = P115.find("async def check_login")
    body = P115[idx: idx + 1200]
    assert "self._status = P115Status(" not in body, "check_login 仍会整体重置 status"
    print("✓ check_login 不再整体重置状态")


# ---------------------------------------------------------------- 行为测试
def test_mark_status_updates_fields():
    """直接用真实类验证 mark_status 的赋值行为。"""
    from app.services.cd2_client import CD2Client

    c = CD2Client()
    c._status.connected = False  # noqa: SLF001
    c.mark_status(connected=True, authenticated=True, upload_count=5)
    assert c.status.connected is True
    assert c.status.authenticated is True
    assert c.status.upload_count == 5
    assert c.status.checked_at > 0
    print("✓ CD2 mark_status 赋值正确")


def test_mark_status_partial_update():
    """只传部分字段时，其余字段保持不变。"""
    from app.services.cd2_client import CD2Client

    c = CD2Client()
    c.mark_status(connected=True, authenticated=True, user_name="alice")
    c.mark_status(connected=False)  # 只更新连通性
    assert c.status.connected is False
    assert c.status.authenticated is True, "未传入的字段不应被覆盖"
    assert c.status.user_name == "alice"
    print("✓ mark_status 支持部分更新")


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
