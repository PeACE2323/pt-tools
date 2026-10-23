"""115 云下载监控 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    P115CancelLog,
    P115OfflineTask,
    get_setting_bool,
    get_setting_float,
    get_settings_map,
)
from ..schemas import ActionResult, CancelLogOut, HashList, OfflineTaskOut, Page
from ..services.p115_client import P115AuthExpired, P115Error
from ..utils import utcnow_ts
from .deps import db_session, p115_client, scheduler

router = APIRouter(prefix="/api/p115", tags=["p115"])

# 已结束的状态：这些任务不参与“超时判定”
_TERMINAL_STATUSES = ("finished", "failed", "removed", "cancelled")


def _threshold(session: Session) -> int:
    settings = get_settings_map(session)
    return int(get_setting_float(settings, "p115.timeout_threshold", 86400))


@router.get(
    "/offline",
    response_model=list[OfflineTaskOut],
    summary="当前 115 云下载任务（含驻留时长与是否超时）",
)
def list_offline(session: Session = Depends(db_session)) -> list[OfflineTaskOut]:
    now = utcnow_ts()
    threshold = _threshold(session)

    rows = (
        session.query(P115OfflineTask)
        .order_by(P115OfflineTask.add_time.desc())
        .all()
    )

    result: list[OfflineTaskOut] = []
    for row in rows:
        item = OfflineTaskOut.model_validate(row)
        age = max(0.0, now - row.add_time) if row.add_time else 0.0
        item.age_sec = age
        # 只有“未结束”的任务才可能被判超时
        terminal = row.normalized_status in _TERMINAL_STATUSES
        item.is_terminal = terminal
        item.over_threshold = (not terminal) and age > threshold
        result.append(item)
    return result


@router.get(
    "/debug/offline-raw",
    summary="诊断：查看 115 云下载的原始状态码与进度",
)
async def debug_offline_raw(
    p115=Depends(p115_client),
) -> dict[str, object]:
    """拉取一次 115 任务列表，返回原始字段。

    排查「下载中的任务显示成已完成」时用：这里同时给出 115 返回的
    原始 ``status`` 与 ``percentDone``，以及本系统归一化后的结果，
    能直接看出是哪一步判断错了。
    """
    try:
        tasks = await p115.list_offline_tasks(page_size=50)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "tasks": []}

    items = []
    for t in tasks[:50]:
        items.append(
            {
                "name": t.name,
                "info_hash": t.info_hash,
                "raw_status": t.status,
                "percent_done": round(t.percent_done, 2),
                "normalized": t.normalized_status,
                "is_terminal": t.is_terminal,
                "add_time": t.add_time,
            }
        )
    return {"ok": True, "count": len(tasks), "tasks": items}


@router.post(
    "/offline/refresh",
    summary="立即刷新 115 云下载列表（不等待定时任务）",
)
async def refresh_offline(
    sched=Depends(scheduler),
) -> dict[str, object]:
    await sched.reap_p115_offline()
    return {
        "ok": bool(sched.stats.get("p115_last_ok")),
        "message": sched.stats.get("p115_last_error") or "已刷新",
        "tasks_seen": sched.stats.get("p115_tasks_seen", 0),
    }


@router.post(
    "/offline/cancel",
    response_model=ActionResult,
    summary="手动取消指定云下载任务（不删除已下载文件）",
)
async def cancel_offline(
    payload: HashList,
    sched=Depends(scheduler),
) -> ActionResult:
    if not payload.info_hashes:
        return ActionResult(affected=0, message="未提供 info_hash")
    result = await sched.manual_cancel_offline(payload.info_hashes)
    return ActionResult(
        affected=int(result.get("success", 0)),
        message=(
            f"成功取消 {result.get('success', 0)} 个，"
            f"失败 {result.get('failed', 0)} 个"
        ),
        details=[f"{k}: {v}" for k, v in (result.get("details") or {}).items()],
    )


@router.post(
    "/offline/restart",
    response_model=ActionResult,
    summary="重启失败的云下载任务",
)
async def restart_offline(
    payload: HashList,
    client=Depends(p115_client),
) -> ActionResult:
    ok_count = 0
    details: list[str] = []
    for info_hash in payload.info_hashes:
        try:
            ok, message = await client.restart_offline_task(info_hash)
        except P115AuthExpired as exc:
            details.append(f"{info_hash}: Cookie 失效 - {exc}")
            continue
        except P115Error as exc:
            ok, message = False, str(exc)
        if ok:
            ok_count += 1
        else:
            details.append(f"{info_hash}: {message}")
    return ActionResult(
        affected=ok_count, message=f"已重启 {ok_count} 个任务", details=details
    )


@router.get(
    "/cancel-logs",
    response_model=Page[CancelLogOut],
    summary="云下载取消日志",
)
def list_cancel_logs(
    session: Session = Depends(db_session),
    q: str = Query(""),
    trigger: str = Query("", description="auto / manual"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> Page[CancelLogOut]:
    query = session.query(P115CancelLog)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (P115CancelLog.name.like(like))
            | (P115CancelLog.info_hash.like(like))
        )
    if trigger:
        query = query.filter(P115CancelLog.trigger == trigger)

    total = query.with_entities(func.count(P115CancelLog.id)).scalar() or 0
    rows = (
        query.order_by(P115CancelLog.cancel_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Page[CancelLogOut](
        items=[CancelLogOut.model_validate(r) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/cancel-logs",
    response_model=ActionResult,
    summary="清空取消日志",
)
def clear_cancel_logs(session: Session = Depends(db_session)) -> ActionResult:
    count = session.query(P115CancelLog).delete(synchronize_session=False)
    session.commit()
    return ActionResult(affected=int(count), message=f"已删除 {count} 条日志")


@router.get("/config", summary="读取 115 监控配置")
def get_config(session: Session = Depends(db_session)) -> dict[str, object]:
    settings = get_settings_map(session)
    return {
        "poll_interval": int(get_setting_float(settings, "p115.poll_interval", 300)),
        "timeout_threshold": int(
            get_setting_float(settings, "p115.timeout_threshold", 86400)
        ),
        "auto_cancel": get_setting_bool(settings, "p115.auto_cancel", True),
        "task_page_size": int(get_setting_float(settings, "p115.task_page_size", 30)),
        "cookie_set": bool((settings.get("p115.cookie") or "").strip()),
    }
