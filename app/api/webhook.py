"""CD2 Webhook 接收端。

CD2 的 ``file_system_watcher`` 会在文件变化时 POST 到这里，用途见
``docs/WEBHOOK.md``：补全 gRPC 上传任务缺失的源路径，并为“取消后重传”
提供可靠的源路径依据。

端点设计要点：

* 永远返回 200（CD2 不解析响应体，但持续 4xx 可能让它禁用 webhook）；
* 解析失败不抛异常，只记录事件；
* 不触发任何破坏性操作，即使请求被伪造也只会写入源路径映射表。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.orm import Session

from ..schemas import WebhookAck
from ..services.webhook import parse_payload, record_changes
from ..utils import get_logger, log_event
from .deps import db_session, verify_webhook_token

logger = get_logger("guard.api.webhook")

router = APIRouter(prefix="/api/webhook", tags=["webhook"])


@router.post(
    "/cd2/file_notify",
    response_model=WebhookAck,
    summary="接收 CD2 文件系统变化 webhook",
)
async def cd2_file_notify(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    device_name: str = Query("", description="CD2 透传的设备名"),
    user_name: str = Query("", description="CD2 透传的用户名"),
    _: None = Depends(verify_webhook_token),
) -> WebhookAck:
    """接收文件变化事件，建立 ``文件名 → 真实源路径`` 映射。

    CD2 侧配置模板（``[file_system_watcher]``）见 docs/WEBHOOK.md。
    """
    if not isinstance(body, dict):
        return WebhookAck(ok=True, received=0, recorded=0, skipped=0)

    try:
        payload = parse_payload(body)
    except Exception as exc:  # noqa: BLE001 - 绝不让 webhook 500
        logger.warning("webhook 解析失败: %s", exc)
        log_event("warning", "webhook", "webhook 解析失败", detail=str(exc))
        return WebhookAck(ok=True)

    # 查询参数优先于 body（CD2 会在 URL 里带 device_name）
    if device_name and not payload.device_name:
        payload.device_name = device_name
    if user_name and not payload.user_name:
        payload.user_name = user_name

    if not payload.changes:
        return WebhookAck(ok=True)

    try:
        stats = record_changes(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("webhook 落库失败")
        log_event("error", "webhook", "webhook 落库失败", detail=str(exc))
        return WebhookAck(ok=True, received=len(payload.changes))

    return WebhookAck(
        ok=True,
        received=int(stats.get("received", 0)),
        recorded=int(stats.get("recorded", 0)),
        skipped=int(stats.get("skipped", 0)),
    )


@router.post(
    "/cd2/mount_notify",
    summary="接收 CD2 挂载点变化 webhook（仅记录，不参与业务）",
)
async def cd2_mount_notify(
    body: dict[str, Any] = Body(default_factory=dict),
    event_name: str = Query("", alias="type"),
    _: None = Depends(verify_webhook_token),
) -> dict[str, Any]:
    """挂载点事件目前只写事件日志，便于排查“上传突然中断”类问题。"""
    changes = body.get("data") if isinstance(body, dict) else None
    count = len(changes) if isinstance(changes, list) else 0
    log_event(
        "info",
        "webhook",
        f"收到挂载点事件 {event_name or '(未知)'}",
        detail=str(changes)[:2000],
    )
    return {"ok": True, "received": count}


@router.get("/cd2/source-paths", summary="查看已记录的源路径映射（排查用）")
def list_source_paths(
    session: Session = Depends(db_session),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    from ..models import SourcePath

    rows = (
        session.query(SourcePath)
        .order_by(SourcePath.seen_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "file_name": r.file_name,
            "source_path": r.source_path,
            "action": r.action,
            "device_name": r.device_name,
            "seen_at": r.seen_at,
        }
        for r in rows
    ]
