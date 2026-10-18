"""系统设置 API：CD2 / 115 配置、连接测试、Token 获取。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..models import (
    get_setting_bool,
    get_setting_float,
    get_settings_map,
    set_setting,
)
from ..schemas import (
    ConnectionTestResult,
    SettingsOut,
    SettingsUpdate,
    TokenResult,
)
from ..services.cd2_client import CD2AuthError, CD2Error
from .deps import cd2_client, db_session, p115_client, scheduler

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _mask(secret: str, keep: int = 4) -> str:
    """敏感值脱敏：保留尾部若干字符，其余用 * 代替。"""
    if not secret:
        return ""
    if len(secret) <= keep:
        return "*" * len(secret)
    return "*" * (len(secret) - keep) + secret[-keep:]


@router.get("", response_model=SettingsOut, summary="读取系统设置（敏感字段脱敏）")
def read_settings(session: Session = Depends(db_session)) -> SettingsOut:
    settings = get_settings_map(session)
    token = settings.get("cd2.token", "") or ""
    cookie = settings.get("p115.cookie", "") or ""
    webhook_token = settings.get("webhook.token", "") or ""

    return SettingsOut(
        cd2_host=settings.get("cd2.host", ""),
        cd2_token_masked=_mask(token),
        cd2_token_set=bool(token),
        cd2_username=settings.get("cd2.username", ""),
        cd2_password_set=bool(settings.get("cd2.password")),
        cd2_poll_interval=int(get_setting_float(settings, "cd2.poll_interval", 3)),
        p115_cookie_masked=_mask(cookie, keep=8),
        p115_cookie_set=bool(cookie),
        p115_poll_interval=int(get_setting_float(settings, "p115.poll_interval", 300)),
        p115_timeout_threshold=int(
            get_setting_float(settings, "p115.timeout_threshold", 86400)
        ),
        p115_auto_cancel=get_setting_bool(settings, "p115.auto_cancel", True),
        p115_task_page_size=int(get_setting_float(settings, "p115.task_page_size", 30)),
        webhook_token_set=bool(webhook_token),
        webhook_url_hint="/api/webhook/cd2/file_notify",
    )


@router.put("", response_model=SettingsOut, summary="保存系统设置")
async def update_settings(
    payload: SettingsUpdate,
    session: Session = Depends(db_session),
    sched=Depends(scheduler),
) -> SettingsOut:
    """保存设置。

    约定：字符串字段传 ``None`` 表示不修改；传空字符串表示清空。
    这样前端可以只提交改动的字段，且不会把已填的密钥误清空。
    """
    mapping = {
        "cd2_host": "cd2.host",
        "cd2_token": "cd2.token",
        "cd2_username": "cd2.username",
        "cd2_password": "cd2.password",
        "cd2_poll_interval": "cd2.poll_interval",
        "p115_cookie": "p115.cookie",
        "p115_poll_interval": "p115.poll_interval",
        "p115_timeout_threshold": "p115.timeout_threshold",
        "p115_task_page_size": "p115.task_page_size",
        "webhook_token": "webhook.token",
    }

    data = payload.model_dump(exclude_unset=True)
    for field, db_key in mapping.items():
        if field not in data:
            continue
        value = data[field]
        if value is None:
            continue
        set_setting(session, db_key, value)

    if payload.p115_auto_cancel is not None:
        set_setting(
            session, "p115.auto_cancel", "1" if payload.p115_auto_cancel else "0"
        )

    session.commit()

    # 让调度器立即应用新配置（含轮询间隔）
    await sched.reload_config()

    # 115 Cookie 变更时同步落盘
    if "p115.cookie" in data and data["p115.cookie"] is not None:
        from ..services.p115_client import P115ClientWrapper

        if data["p115.cookie"]:
            P115ClientWrapper._persist_cookie(data["p115.cookie"])

    return read_settings(session)


@router.post(
    "/test/cd2",
    response_model=ConnectionTestResult,
    summary="测试 CD2 gRPC 连接",
)
async def test_cd2(
    client=Depends(cd2_client),
    sched=Depends(scheduler),
) -> ConnectionTestResult:
    """连接测试：先 GetSystemInfo，再（有 Token 时）拉一次上传任务数。"""
    # 用最新配置刷新客户端参数
    await sched.reload_config()
    result = await client.test_connection()
    return ConnectionTestResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message", "")),
        detail=result,
    )


@router.post(
    "/test/p115",
    response_model=ConnectionTestResult,
    summary="测试 115 Cookie 是否有效",
)
async def test_p115(
    client=Depends(p115_client),
    sched=Depends(scheduler),
) -> ConnectionTestResult:
    await sched.reload_config()
    result = await client.check_login()
    return ConnectionTestResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message", "")),
        detail=result,
    )


@router.post(
    "/cd2/token",
    response_model=TokenResult,
    summary="用 CD2 账号密码换取 API Token 并保存",
)
async def fetch_cd2_token(
    session: Session = Depends(db_session),
    client=Depends(cd2_client),
    sched=Depends(scheduler),
) -> TokenResult:
    """调用 ``GetToken`` 换取 JWT 并写回配置。"""
    await sched.reload_config()
    settings = get_settings_map(session)
    username = (settings.get("cd2.username") or "").strip()
    password = settings.get("cd2.password") or ""

    if not username or not password:
        return TokenResult(
            ok=False, message="请先填写 CD2 账号与密码"
        )

    try:
        token = await client.fetch_token(username, password)
    except CD2AuthError as exc:
        return TokenResult(ok=False, message=str(exc))
    except CD2Error as exc:
        return TokenResult(ok=False, message=str(exc))

    set_setting(session, "cd2.token", token)
    session.commit()
    await sched.reload_config()

    return TokenResult(ok=True, token=_mask(token), message="已获取并保存 Token")


@router.get("/events", summary="系统事件日志")
def list_events(
    session: Session = Depends(db_session),
    limit: int = 100,
    level: str = "",
) -> list[dict[str, object]]:
    from ..models import EventLog

    query = session.query(EventLog)
    if level:
        query = query.filter(EventLog.level == level)
    rows = query.order_by(EventLog.id.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": r.id,
            "level": r.level,
            "source": r.source,
            "message": r.message,
            "detail": r.detail,
            "created_at": r.created_at.timestamp() if r.created_at else 0,
        }
        for r in rows
    ]
