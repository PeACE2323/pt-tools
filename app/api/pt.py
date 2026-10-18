"""PT 流量监控 API（Python 原生实现）。

原先这些接口是转发到 Go 子服务的代理；现在直接由本进程的
``PTMonitor`` 提供数据，不再有跨进程调用。

模块划分：

* ``bt_client.py``  —— 下载器协议（qB / Transmission）
* ``bt_policy.py``  —— 限速策略引擎
* ``bt_monitor.py`` —— 采集编排 + 配置持久化
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.bt_client import ClientError
from ..services.bt_monitor import get_pt_monitor, save_pt_config
from ..utils import get_logger, log_event
from .deps import db_session

logger = get_logger("guard.api.pt")

router = APIRouter(prefix="/api/pt", tags=["pt-traffic"])


def _monitor():
    return get_pt_monitor()


# ===========================================================================
# 概览
# ===========================================================================
@router.get("/overview", summary="PT 流量总览")
async def pt_overview() -> dict[str, Any]:
    """供仪表盘与 PT 页展示的聚合数据。"""
    return _monitor().overview()


@router.get("/clients", summary="下载器明细")
async def pt_clients() -> list[dict[str, Any]]:
    return _monitor().clients_view()


@router.get("/policies", summary="限速策略状态")
async def pt_policies() -> list[dict[str, Any]]:
    return _monitor().policies_view()


@router.get("/events", summary="PT 策略事件")
async def pt_events(
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _monitor().events(limit)


# ===========================================================================
# 手动操作
# ===========================================================================
@router.api_route(
    "/client/test", methods=["GET", "POST"], summary="测试下载器连通性"
)
async def pt_client_test(
    name: str = Query(..., description="客户端名称"),
) -> dict[str, Any]:
    try:
        return await _monitor().test_client(name)
    except ClientError as exc:
        return {"ok": False, "message": str(exc), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("测试客户端失败")
        raise HTTPException(500, f"测试失败: {exc}") from exc


@router.post(
    "/policy/{action}", summary="策略操作：resume 解除限速 / trigger 手动触发"
)
async def pt_policy_action(
    action: str,
    name: str = Query(..., description="策略名称"),
) -> dict[str, Any]:
    if action not in ("resume", "trigger"):
        raise HTTPException(400, "action 只能是 resume 或 trigger")
    try:
        return await _monitor().policy_action(action, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"操作失败: {exc}") from exc


# ===========================================================================
# 配置：下载器
# ===========================================================================
@router.get("/form", summary="读取下载器配置")
async def pt_get_form(session: Session = Depends(db_session)) -> dict[str, Any]:
    from ..services.bt_monitor import load_pt_config

    clients, policies = load_pt_config(session)
    monitor = _monitor()
    # 合并运行状态，前端可直接展示「在线/离线」
    live = {c["name"]: c for c in monitor.clients_view()}
    out = []
    for cfg in clients:
        item = dict(cfg)
        # 不回传密码与 API Key，只标记是否已设置
        item["has_password"] = bool(cfg.get("password"))
        item["has_api_key"] = bool(cfg.get("api_key"))
        item["api_key_hint"] = _mask(cfg.get("api_key") or "")
        item.pop("password", None)
        item.pop("api_key", None)
        snap = live.get(item.get("name") or "")
        item["healthy"] = bool(snap and snap.get("healthy"))
        item["last_error"] = (snap or {}).get("last_error", "")
        item["up_speed"] = (snap or {}).get("up_speed", 0)
        item["current_up_limit"] = (snap or {}).get("current_up_limit", 0)
        item["supports_api_key"] = (item.get("type") or "").lower() == "qbittorrent"
        out.append(item)
    return {"clients": out, "policies": policies}


class ClientsPayload(BaseModel):
    """保存下载器配置。"""

    clients: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/form", summary="保存下载器配置")
async def pt_put_form(
    payload: ClientsPayload = Body(...),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """保存下载器。

    密码/API Key 的语义：

    * 前端未传该字段（或传空串）→ 保留原值
    * 传 ``clear_password`` / ``clear_api_key`` = true → 清空
    * 传了新值 → 覆盖
    """
    from ..services.bt_monitor import load_pt_config

    old_clients, _ = load_pt_config(session)
    old_by_name = {c.get("name"): c for c in old_clients}

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in payload.clients:
        name = str(c.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "下载器名称不能为空")
        if name in seen:
            raise HTTPException(400, f"下载器名称重复: {name}")
        seen.add(name)

        url = str(c.get("url") or "").strip()
        if not url:
            raise HTTPException(400, f"「{name}」的地址不能为空")

        prev = old_by_name.get(name) or {}
        password = _resolve_secret(
            c.get("password"), c.get("clear_password"), prev.get("password", "")
        )
        api_key = _resolve_secret(
            c.get("api_key"), c.get("clear_api_key"), prev.get("api_key", "")
        )

        cleaned.append(
            {
                "name": name,
                "type": str(c.get("type") or "qbittorrent").lower(),
                "url": url,
                "username": str(c.get("username") or ""),
                "password": password,
                "api_key": api_key,
                "enabled": bool(c.get("enabled", True)),
                "insecure_skip_verify": bool(c.get("insecure_skip_verify", False)),
                "timeout": str(c.get("timeout") or "10s"),
            }
        )

    save_pt_config(session, clients=cleaned)
    session.commit()

    # 立即热重载，让新配置生效
    await _monitor().reload()
    log_event(
        "info", "pt-traffic", f"已保存 {len(cleaned)} 个下载器配置"
    )
    return {"ok": True, "message": "下载器配置已保存", "count": len(cleaned)}


# ===========================================================================
# 配置：策略
# ===========================================================================
@router.get("/policies/config", summary="读取策略配置（可编辑）")
async def pt_policies_config(
    session: Session = Depends(db_session),
) -> list[dict[str, Any]]:
    from ..services.bt_monitor import load_pt_config

    _, policies = load_pt_config(session)
    return policies


class PoliciesPayload(BaseModel):
    """保存策略配置。"""

    policies: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/policies/config", summary="保存策略配置")
async def pt_put_policies(
    payload: PoliciesPayload = Body(...),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    from ..services.bt_policy import PolicyConfig, _parse_bitrate, _parse_bytes

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in payload.policies:
        name = str(p.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "策略名称不能为空")
        if name in seen:
            raise HTTPException(400, f"策略名称重复: {name}")
        seen.add(name)

        threshold = _parse_bytes(p.get("threshold_total"))
        if threshold <= 0:
            raise HTTPException(400, f"策略「{name}」的上传阈值必须大于 0")

        cleaned.append(
            {
                "name": name,
                "enabled": bool(p.get("enabled", True)),
                "clients": list(p.get("clients") or []),
                "window": str(p.get("window") or "24h"),
                "align": str(p.get("align") or "day"),
                "threshold_total": threshold,
                "limit_total": _parse_bitrate(p.get("limit_total")),
                "resume_limit_total": _parse_bitrate(p.get("resume_limit_total")),
                "resume_on_window_reset": bool(
                    p.get("resume_on_window_reset", True)
                ),
                "cooldown": str(p.get("cooldown") or "0s"),
            }
        )

    save_pt_config(session, policies=cleaned)
    session.commit()

    # 热重载策略（保留当前周期进度与限速状态）
    monitor = _monitor()
    monitor.engine.apply_config(
        [PolicyConfig.from_dict(p) for p in cleaned]
    )
    log_event("info", "pt-traffic", f"已保存 {len(cleaned)} 条限速策略")
    return {"ok": True, "message": "策略已保存", "count": len(cleaned)}


# ===========================================================================
# 辅助
# ===========================================================================
def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:6] + "..." + secret[-4:]


def _resolve_secret(submitted: Any, clear: Any, previous: str) -> str:
    """三态语义：未传/空串 = 保留原值；clear = 清空；其它 = 覆盖。"""
    if clear:
        return ""
    text = str(submitted or "").strip()
    if not text:
        return previous or ""
    return text
