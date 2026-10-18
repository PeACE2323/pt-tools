"""FastAPI 依赖注入。

集中管理三个单例（DB 会话、CD2 客户端、115 客户端、调度引擎），
API 层通过 ``Depends`` 取用，避免各路由自行实例化造成状态分裂。
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..models import get_db as _get_db
from ..models import get_settings_map
from ..services.cd2_client import CD2Client, get_cd2_client
from ..services.p115_client import P115ClientWrapper, get_p115_client
from ..services.scheduler import GuardScheduler, get_scheduler


# ---------------------------------------------------------------- 数据库
def db_session() -> Iterator[Session]:
    """SQLAlchemy 会话（请求结束自动关闭）。"""
    yield from _get_db()


# ---------------------------------------------------------------- 服务单例
def cd2_client() -> CD2Client:
    return get_cd2_client()


def p115_client() -> P115ClientWrapper:
    return get_p115_client()


def scheduler() -> GuardScheduler:
    return get_scheduler()


def app_settings(session: Session = Depends(db_session)) -> dict[str, str]:
    """当前配置（请求内一次性读取）。"""
    return get_settings_map(session)


# ---------------------------------------------------------------- Webhook 鉴权
async def verify_webhook_token(
    authorization: str | None = Header(default=None),
    x_webhook_token: str | None = Header(default=None),
    session: Session = Depends(db_session),
) -> None:
    """校验 CD2 webhook 的来源。

    CD2 在 ``[global_params.default_headers]`` 里可以配置 ``authorization``，
    会把原值透传过来（模板里是 ``"basic usernamepassword"``）。这里做统一校验：

    * 未配置 ``webhook.token`` → 不做鉴权（便于内网快速接入）；
    * 已配置 → 要求请求头中出现该 token，支持三种形式：
      ``Authorization: <token>``、``Authorization: Bearer <token>``、
      ``X-Webhook-Token: <token>``。

    校验失败返回 401。该端点只写源路径映射表，即使被伪造也无法造成数据损失，
    但仍建议在生产环境配置 token。
    """
    settings = get_settings_map(session)
    expected = (settings.get("webhook.token") or "").strip()
    if not expected:
        return

    candidates: list[str] = []
    if authorization:
        candidates.append(authorization.strip())
        if authorization.lower().startswith("bearer "):
            candidates.append(authorization[7:].strip())
    if x_webhook_token:
        candidates.append(x_webhook_token.strip())

    if any(c == expected for c in candidates):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="webhook token 校验失败",
    )
