"""CD2 任务监控与记录 API。

包含三类数据：

1. 实时上传任务（``upload_tasks`` 中 state=active/intercepted 的快照）
2. 秒传任务详情（``instant_uploads``）
3. 不能秒传 / 重试队列（``non_instant_tasks``），支持手动重试、取消重试、删除
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import InstantUpload, NonInstantTask, UploadTaskSnapshot
from ..schemas import (
    ActionResult,
    IdList,
    InstantUploadOut,
    NonInstantTaskOut,
    Page,
    UploadTaskOut,
)
from ..utils import local_day_start_ts, utcnow_ts
from .deps import db_session, scheduler

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ===========================================================================
# 实时上传任务
# ===========================================================================
@router.get(
    "/live",
    response_model=list[UploadTaskOut],
    summary="当前正在 CD2 上传队列中的任务（仅监控范围内的）",
)
def list_live_tasks(
    session: Session = Depends(db_session),
) -> list[UploadTaskOut]:
    now = utcnow_ts()
    rows = (
        session.query(UploadTaskSnapshot)
        # queued 也要展示：CD2 有并发上限，排队中的任务在界面上是正常的
        .filter(UploadTaskSnapshot.state.in_(("queued", "active", "intercepted")))
        .order_by(UploadTaskSnapshot.first_seen_at.asc())
        .all()
    )

    result: list[UploadTaskOut] = []
    for row in rows:
        item = UploadTaskOut.model_validate(row)
        total = max(0.0, now - (row.first_seen_at or now))
        queued = float(row.queued_sec or 0.0)
        item.elapsed_sec = total
        item.queued_sec = queued
        # 真正在传的耗时 = 总耗时 - 排队耗时
        item.transfer_sec = max(0.0, total - queued)
        if row.size > 0:
            item.progress = min(100.0, row.transferred / row.size * 100.0)
        result.append(item)
    return result


# ===========================================================================
# 秒传记录
# ===========================================================================
@router.get(
    "/instant",
    response_model=Page[InstantUploadOut],
    summary="秒传任务详情",
)
def list_instant(
    session: Session = Depends(db_session),
    q: str = Query("", description="按文件名/路径模糊搜索"),
    rule_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> Page[InstantUploadOut]:
    query = session.query(InstantUpload)

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                InstantUpload.file_name.like(like),
                InstantUpload.source_path.like(like),
                InstantUpload.dest_path.like(like),
            )
        )
    if rule_id is not None:
        query = query.filter(InstantUpload.rule_id == rule_id)

    total = query.with_entities(func.count(InstantUpload.id)).scalar() or 0
    rows = (
        query.order_by(InstantUpload.finished_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Page[InstantUploadOut](
        items=[InstantUploadOut.model_validate(r) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.delete("/instant", response_model=ActionResult, summary="清空秒传记录")
async def clear_instant(
    session: Session = Depends(db_session),
    keep_days: int | None = Query(
        None, ge=0, description="只保留最近 N 天；不传则全清"
    ),
) -> ActionResult:
    query = session.query(InstantUpload)
    if keep_days is not None:
        cutoff = utcnow_ts() - keep_days * 86400
        query = query.filter(InstantUpload.finished_at < cutoff)
    count = query.delete(synchronize_session=False)
    session.commit()
    return ActionResult(affected=int(count), message=f"已删除 {count} 条秒传记录")


# ===========================================================================
# 不能秒传 / 重试队列
# ===========================================================================
@router.get(
    "/non-instant",
    response_model=Page[NonInstantTaskOut],
    summary="不能秒传任务 / 重试队列",
)
def list_non_instant(
    session: Session = Depends(db_session),
    q: str = Query(""),
    status_filter: str = Query(
        "",
        alias="status",
        description="waiting/retrying/success/failed/cancelled，逗号分隔",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> Page[NonInstantTaskOut]:
    query = session.query(NonInstantTask)

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                NonInstantTask.file_name.like(like),
                NonInstantTask.source_path.like(like),
                NonInstantTask.dest_path.like(like),
            )
        )
    if status_filter:
        wanted = [s.strip() for s in status_filter.split(",") if s.strip()]
        if wanted:
            query = query.filter(NonInstantTask.status.in_(wanted))

    total = query.with_entities(func.count(NonInstantTask.id)).scalar() or 0
    rows = (
        query.order_by(NonInstantTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Page[NonInstantTaskOut](
        items=[NonInstantTaskOut.model_validate(r) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.post(
    "/non-instant/retry",
    response_model=ActionResult,
    summary="手动立即重试（触发备份重扫）",
)
async def retry_non_instant(
    payload: IdList,
    sched=Depends(scheduler),
) -> ActionResult:
    if not payload.ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ids 不能为空")

    result = await sched.manual_retry(payload.ids)
    return ActionResult(
        affected=int(result.get("triggered", 0)),
        message=f"已触发 {result.get('triggered', 0)} 个，失败 {result.get('failed', 0)} 个",
        details=list(result.get("details", [])),
    )


@router.post(
    "/non-instant/cancel",
    response_model=ActionResult,
    summary="取消重试（标记为已取消，不再自动重试）",
)
async def cancel_non_instant(
    payload: IdList,
    sched=Depends(scheduler),
) -> ActionResult:
    if not payload.ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ids 不能为空")

    count = await sched.manual_cancel_retry(payload.ids)
    return ActionResult(affected=int(count), message=f"已取消 {count} 条重试")


@router.delete(
    "/non-instant",
    response_model=ActionResult,
    summary="删除记录（按 id，或按状态批量）",
)
async def delete_non_instant(
    session: Session = Depends(db_session),
    ids: str = Query("", description="逗号分隔的 id，留空则按 status 批量删除"),
    status_filter: str = Query("", alias="status"),
) -> ActionResult:
    query = session.query(NonInstantTask)

    if ids:
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "ids 格式错误") from None
        query = query.filter(NonInstantTask.id.in_(id_list))
    elif status_filter:
        wanted = [s.strip() for s in status_filter.split(",") if s.strip()]
        if not wanted:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "status 为空")
        query = query.filter(NonInstantTask.status.in_(wanted))
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "必须提供 ids 或 status"
        )

    count = query.delete(synchronize_session=False)
    session.commit()
    return ActionResult(affected=int(count), message=f"已删除 {count} 条记录")


@router.get(
    "/summary",
    summary="任务统计（供仪表盘与标签页角标）",
)
def task_summary(session: Session = Depends(db_session)) -> dict[str, object]:
    now = utcnow_ts()
    # 按服务端本地时区的“今天 00:00”统计，而不是 UTC 零点
    today_start = local_day_start_ts(now)

    non_instant_counts = dict(
        session.query(NonInstantTask.status, func.count(NonInstantTask.id))
        .group_by(NonInstantTask.status)
        .all()
    )
    live_count = (
        session.query(func.count(UploadTaskSnapshot.id))
        .filter(
            UploadTaskSnapshot.state.in_(("queued", "active", "intercepted"))
        )
        .scalar()
        or 0
    )
    today_instant = (
        session.query(func.count(InstantUpload.id))
        .filter(InstantUpload.finished_at >= today_start)
        .scalar()
        or 0
    )

    return {
        "live": int(live_count),
        "today_instant": int(today_instant),
        "non_instant": {
            "waiting": int(non_instant_counts.get("waiting", 0)),
            "retrying": int(non_instant_counts.get("retrying", 0)),
            "success": int(non_instant_counts.get("success", 0)),
            "failed": int(non_instant_counts.get("failed", 0)),
            "cancelled": int(non_instant_counts.get("cancelled", 0)),
        },
    }
