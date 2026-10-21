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
from ..utils import get_logger, local_day_start_ts, utcnow_ts
from .deps import db_session, scheduler

logger = get_logger("guard.api.tasks")

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ===========================================================================
# CD2 守卫：待观察名单
# ===========================================================================
@router.get("/watching", summary="待观察名单（有上传速度、正在计数的任务）")
def list_watching() -> list[dict]:
    """返回当前被观察的任务。

    判定逻辑在 ``services/cd2_guard.py``：每 30 秒轮询一次，
    有上传速度的任务进入名单，连续两次仍在名单上即自动取消。
    这里只做展示，不改变任何状态。
    """
    from ..services.scheduler import get_scheduler

    try:
        return get_scheduler().watching_snapshot()
    except Exception as exc:  # noqa: BLE001 - 纯展示接口，失败返回空
        logger.debug("读取待观察名单失败: %s", exc)
        return []


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

    # 显式逐条构造，避免依赖 ORM 对象被 Pydantic 直接读取。
    # 若某条记录有异常字段（如 None），单条失败不应让整页空白，
    # 这里逐条容错并记录，保证「有数据就能看到」。
    items: list[InstantUploadOut] = []
    for r in rows:
        try:
            items.append(InstantUploadOut.model_validate(r))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "秒传记录 %s 序列化失败，已跳过: %s",
                getattr(r, "id", "?"),
                exc,
            )

    return Page[InstantUploadOut](
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.get("/instant/debug", summary="诊断：秒传记录统计与查询是否一致")
async def debug_instant(
    session: Session = Depends(db_session),
) -> dict:
    """排查「角标有数字但列表为空」。

    返回数据库里实际有多少条、今日多少条、以及列表接口会返回的前几条的
    关键字段，便于判断问题出在统计口径、查询还是序列化。
    """
    from sqlalchemy import text

    day_start = local_day_start_ts()

    total = (
        session.query(func.count(InstantUpload.id)).scalar() or 0
    )
    today = (
        session.query(func.count(InstantUpload.id))
        .filter(InstantUpload.finished_at >= day_start)
        .scalar()
        or 0
    )

    # 直接走原生 SQL 取样，绕过 ORM，确认表里确实有数据
    sample: list[dict] = []
    try:
        rows = session.execute(
            text(
                "SELECT id, file_name, size, transferred_bytes, "
                "finished_at, rule_name FROM instant_uploads "
                "ORDER BY finished_at DESC LIMIT 5"
            )
        ).mappings()
        sample = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        sample = [{"error": str(exc)}]

    return {
        "total": int(total),
        "today": int(today),
        "day_start_ts": day_start,
        "now_ts": utcnow_ts(),
        "sample": sample,
    }


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
    # 秒传记录总数：标签页角标用它，与列表页口径保持一致。
    # 之前角标用「今日数」、列表用「全部数」，两者会对不上：
    # 用户看到角标有数字，点进去列表却是空的（记录不是今天的）。
    total_instant = (
        session.query(func.count(InstantUpload.id)).scalar() or 0
    )

    return {
        "live": int(live_count),
        "today_instant": int(today_instant),
        "total_instant": int(total_instant),
        "non_instant": {
            "waiting": int(non_instant_counts.get("waiting", 0)),
            "retrying": int(non_instant_counts.get("retrying", 0)),
            "success": int(non_instant_counts.get("success", 0)),
            "failed": int(non_instant_counts.get("failed", 0)),
            "cancelled": int(non_instant_counts.get("cancelled", 0)),
        },
    }
