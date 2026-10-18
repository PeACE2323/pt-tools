"""仪表盘聚合 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    InstantUpload,
    NonInstantTask,
    P115CancelLog,
    P115OfflineTask,
    Rule,
    UploadTaskSnapshot,
)
from ..schemas import DashboardOut
from ..utils import get_logger, local_day_start_ts, utcnow_ts
from .deps import cd2_client, db_session, p115_client, scheduler

logger = get_logger("guard.api.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOut, summary="仪表盘聚合数据")
async def dashboard(
    session: Session = Depends(db_session),
    cd2=Depends(cd2_client),
    p115=Depends(p115_client),
    sched=Depends(scheduler),
) -> DashboardOut:
    now = utcnow_ts()
    # 本地时区的今天 00:00（不能用 now % 86400，那是 UTC 零点）
    today_start = local_day_start_ts(now)

    # ---------------- CD2 ----------------
    cd2_status = cd2.status
    cd2_view = {
        "host": cd2_status.host or cd2.host,
        "configured": bool(cd2.host),
        "connected": cd2_status.connected,
        "authenticated": cd2_status.authenticated,
        "user_name": cd2_status.user_name,
        "system_ready": cd2_status.system_ready,
        "upload_count": cd2_status.upload_count,
        "message": cd2_status.message,
        "last_error": cd2_status.last_error,
        "last_poll_at": sched.stats.get("cd2_last_run", 0),
        "last_poll_ok": sched.stats.get("cd2_last_ok", False),
        "last_poll_error": sched.stats.get("cd2_last_error", ""),
    }

    # ---------------- 115 ----------------
    p115_status = p115.status
    p115_view = {
        "configured": bool(p115.configured),
        "logged_in": p115_status.logged_in,
        "user_id": p115_status.user_id,
        "task_count": p115_status.task_count,
        "message": p115_status.message,
        "last_error": p115_status.last_error,
        "last_poll_at": sched.stats.get("p115_last_run", 0),
        "last_poll_ok": sched.stats.get("p115_last_ok", False),
        "last_poll_error": sched.stats.get("p115_last_error", ""),
    }

    # ---------------- 统计 ----------------
    today_instant = (
        session.query(func.count(InstantUpload.id))
        .filter(InstantUpload.finished_at >= today_start)
        .scalar()
        or 0
    )
    total_instant = session.query(func.count(InstantUpload.id)).scalar() or 0

    non_instant = dict(
        session.query(NonInstantTask.status, func.count(NonInstantTask.id))
        .group_by(NonInstantTask.status)
        .all()
    )
    live_uploads = (
        session.query(func.count(UploadTaskSnapshot.id))
        .filter(UploadTaskSnapshot.state.in_(("active", "intercepted")))
        .scalar()
        or 0
    )

    today_cancelled = (
        session.query(func.count(P115CancelLog.id))
        .filter(P115CancelLog.cancel_at >= today_start)
        .scalar()
        or 0
    )
    total_cancelled = session.query(func.count(P115CancelLog.id)).scalar() or 0

    offline_active = (
        session.query(func.count(P115OfflineTask.id))
        .filter(
            P115OfflineTask.normalized_status.in_(
                ("waiting", "downloading", "failed")
            )
        )
        .scalar()
        or 0
    )

    enabled_rules = (
        session.query(func.count(Rule.id)).filter(Rule.enabled.is_(True)).scalar() or 0
    )
    total_rules = session.query(func.count(Rule.id)).scalar() or 0

    stats = {
        "today_instant": int(today_instant),
        "total_instant": int(total_instant),
        "live_uploads": int(live_uploads),
        "waiting_retry": int(non_instant.get("waiting", 0)),
        "retrying": int(non_instant.get("retrying", 0)),
        "retry_success": int(non_instant.get("success", 0)),
        "retry_failed": int(non_instant.get("failed", 0)),
        "cancelled_retry": int(non_instant.get("cancelled", 0)),
        "today_cancelled_offline": int(today_cancelled),
        "total_cancelled_offline": int(total_cancelled),
        "offline_active": int(offline_active),
        "enabled_rules": int(enabled_rules),
        "total_rules": int(total_rules),
    }

    # ---------------- PT 流量 ----------------
    # 总览整合进仪表盘：用户不必为了看一眼上传速率切到另一个页面。
    # PT 模块不可用时给出 running=false，不影响整页渲染。
    pt_view: dict[str, Any] = {"running": False, "configured": False}
    try:
        from ..services.bt_monitor import get_pt_monitor

        pt_view = get_pt_monitor().overview()
    except Exception as exc:  # noqa: BLE001 - PT 不可用不影响仪表盘
        logger.debug("读取 PT 数据失败: %s", exc)
        pt_view = {"running": False, "configured": False, "last_error": str(exc)}

    # ---------------- 调度器 ----------------
    jobs: list[dict[str, object]] = []
    if getattr(sched, "scheduler", None) is not None and sched.scheduler.running:
        for job in sched.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run_at": job.next_run_time.timestamp()
                    if job.next_run_time
                    else 0,
                }
            )
    scheduler_view = {
        "running": bool(
            getattr(sched, "scheduler", None) is not None and sched.scheduler.running
        ),
        "jobs": jobs,
    }

    return DashboardOut(
        cd2=cd2_view,
        p115=p115_view,
        pt=pt_view,
        stats=stats,
        scheduler=scheduler_view,
    )
