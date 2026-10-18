"""PT 流量监控（Traffic Guard）的原生数据接口。

## 为什么是「原生接口」而不是 iframe

最初的做法是把 Go 侧的整页面板用 iframe 嵌进来。但那只是「套壳」：
用户仍在用两套界面，主题、导航、交互都不一致，也谈不上整合。

现在改为：Python 侧调用 Go 的 JSON API，**用 PTtools 自己的 UI 渲染**。

    PTtools 前端  ──/api/pt/*──▶  FastAPI  ──/api/*──▶  Go 子服务
                  （统一风格）              （纯数据源）

Go 的子服务仍保留完整面板（``/btg/`` 可直接访问，便于排错），
但日常使用全部走 PTtools 的界面。

## 与 Go 侧接口的对应

============================  =========================================
PTtools 路由                  Go 侧来源
============================  =========================================
GET  /api/pt/overview         /api/status（聚合速率、在线客户端数、限速状态）
GET  /api/pt/clients          /api/status 的 clients 字段
GET  /api/pt/policies         /api/status 的 policies 字段
GET  /api/pt/events           /api/events（并入 PTtools 事件日志一起展示）
POST /api/pt/policy/{action}  /api/policy/{resume|trigger}
GET  /api/pt/config           /api/config/raw（YAML 原文）
PUT  /api/pt/config           /api/config/raw（保存并热重载）
POST /api/pt/client/test      /api/client/test
============================  =========================================
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..services.btguard import BTGuardManager, _proxy_unavailable, get_btg_manager
from ..utils import get_logger

logger = get_logger("guard.api.pt")

router = APIRouter(prefix="/api/pt", tags=["pt-traffic"])


def _unavailable(reason: object) -> HTTPException:
    """把子服务异常统一转成 503。

    前端据此可以区分「服务没起来」（503）和「参数/配置错误」（400），
    从而给出不同的提示。
    """
    return HTTPException(status_code=503, detail=f"PT 流量子服务不可用: {reason}")


# ===========================================================================
# 概览（仪表盘 + PT 页顶部）
# ===========================================================================
@router.get("/overview", summary="PT 流量总览")
async def pt_overview(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    """供仪表盘与 PT 页展示的聚合数据。

    子服务不可用时返回 ``available: false`` 而不是报错 ——
    仪表盘不应因为 BT 没配置就整页报错。
    """
    if not mgr.status.running:
        return {
            "available": mgr.is_available(),
            "running": False,
            "last_error": mgr.status.last_error,
        }

    try:
        st = await mgr.fetch_status()
    except _proxy_unavailable as exc:
        return {
            "available": True,
            "running": False,
            "last_error": str(exc),
        }

    agg = st.get("aggregate") or {}
    clients = st.get("clients") or []
    policies = st.get("policies") or []

    online = sum(1 for c in clients if c.get("healthy"))
    limited_clients = sum(1 for c in clients if c.get("up_speed_limited"))

    return {
        "available": True,
        "running": True,
        "version": st.get("version", ""),
        "hostname": st.get("hostname", ""),
        "uptime": st.get("uptime", ""),
        "now": st.get("now", ""),
        # 聚合速率与流量
        "up_speed": agg.get("up_speed", 0),
        "dl_speed": agg.get("dl_speed", 0),
        "up_total": agg.get("up_total", 0),
        "window_bytes": agg.get("window_bytes", 0),
        "peers": agg.get("peers", 0),
        "torrents": agg.get("torrents", 0),
        # 计数
        "client_count": len(clients),
        "client_online": online,
        "policy_count": len(policies),
        "policy_limited": sum(1 for p in policies if p.get("limited")),
        "limited_clients": limited_clients,
        "config": st.get("config", {}),
        "monitor": st.get("monitor", {}),
    }


# ===========================================================================
# 客户端
# ===========================================================================
@router.get("/clients", summary="下载器明细")
async def pt_clients(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> list[dict[str, Any]]:
    if not mgr.status.running:
        raise _unavailable(mgr.status.last_error or "未运行")
    try:
        st = await mgr.fetch_status()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc
    return st.get("clients") or []


@router.post("/client/test", summary="测试下载器连通性")
async def pt_client_test(
    name: str = Query(..., description="客户端名称"),
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    if not mgr.status.running:
        raise _unavailable("子服务未运行")
    try:
        return await mgr.test_client(name)
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc


# ===========================================================================
# 策略
# ===========================================================================
@router.get("/policies", summary="限速策略状态")
async def pt_policies(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> list[dict[str, Any]]:
    if not mgr.status.running:
        raise _unavailable(mgr.status.last_error or "未运行")
    try:
        st = await mgr.fetch_status()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc
    return st.get("policies") or []


@router.post(
    "/policy/{action}",
    summary="策略操作：resume 解除限速 / trigger 手动触发",
)
async def pt_policy_action(
    action: str,
    name: str = Query(..., description="策略名称"),
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    if action not in ("resume", "trigger"):
        raise HTTPException(400, "action 只能是 resume 或 trigger")
    if not mgr.status.running:
        raise _unavailable("子服务未运行")
    try:
        return await mgr.policy_action(action, name)
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc


# ===========================================================================
# 事件
# ===========================================================================
@router.get("/events", summary="PT 策略事件")
async def pt_events(
    limit: int = Query(100, ge=1, le=500),
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> list[dict[str, Any]]:
    if not mgr.status.running:
        return []
    try:
        return await mgr.fetch_events(limit)
    except _proxy_unavailable as exc:
        logger.debug("获取 PT 事件失败: %s", exc)
        return []


# ===========================================================================
# 配置
# ===========================================================================
@router.get("/config", summary="读取 PT 配置（YAML 原文）")
async def pt_get_config(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    if not mgr.status.running:
        raise _unavailable(mgr.status.last_error or "子服务未运行，无法读取配置")
    try:
        text = await mgr.fetch_config_yaml()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc
    return {"yaml": text}


class PTConfigPayload(BaseModel):
    """保存配置请求体。"""

    yaml: str = Field(..., min_length=1, description="完整 YAML 配置文本")


@router.put("/config", summary="保存 PT 配置并热重载")
async def pt_put_config(
    payload: PTConfigPayload,
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    if not mgr.status.running:
        raise _unavailable("子服务未运行")
    try:
        return await mgr.save_config_yaml(payload.yaml)
    except _proxy_unavailable as exc:
        # 配置写错是用户可见的常见错误，返回 400 而非 503
        raise HTTPException(400, str(exc)) from exc


@router.get("/history", summary="上传速率采样曲线")
async def pt_history(
    client: str = Query("", description="客户端名称，留空为聚合曲线"),
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> list[dict[str, Any]]:
    if not mgr.status.running:
        return []
    try:
        return await mgr.fetch_history(client)
    except _proxy_unavailable:
        return []


@router.get("/config/example", summary="获取配置模板（首次配置时参考）")
async def pt_config_example() -> dict[str, Any]:
    """返回内置的示例配置，供前端「填入模板」用。"""
    from pathlib import Path

    candidates = [
        Path("/app/btguard/config.example.yaml"),
        Path(__file__).resolve().parents[2] / "btguard" / "config.example.yaml",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return {"yaml": p.read_text(encoding="utf-8")}
        except OSError:
            continue
    return {"yaml": "", "error": "未找到示例配置"}


# ===========================================================================
# 图形化配置（下载器 / 通用设置）
# ===========================================================================
@router.get("/form", summary="读取图形化配置（下载器等）")
async def pt_get_form(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    """结构化配置，用于界面表单。

    Go 侧已实现结构化视图（密码脱敏、未修改语义），直接透传即可。
    """
    if not mgr.status.running:
        raise _unavailable(mgr.status.last_error or "子服务未运行，无法读取配置")
    try:
        return await mgr.fetch_form_config()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc


@router.put("/form", summary="保存图形化配置（下载器等）")
async def pt_put_form(
    payload: dict[str, Any] = Body(...),
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    """按结构化载荷保存。

    前端只改下载器与通用项；策略在 PT 策略页单独维护 ——
    但 Go 侧要求提交完整表单，所以前端会先读一次、改完整体提交，
    未改动的策略原样带回，不会丢失。

    **通知功能已从本项目中移除**：保存时强制关闭所有通知渠道，
    即使提交的载荷里带着通知字段也不会生效。
    """
    if not mgr.status.running:
        raise _unavailable("子服务未运行")

    # 强制禁用通知：本项目不提供通知功能，也不希望它在后台偷偷发消息。
    payload = _strip_notify(payload)

    try:
        return await mgr.save_form_config(payload)
    except _proxy_unavailable as exc:
        raise HTTPException(400, str(exc)) from exc


def _strip_notify(payload: dict[str, Any]) -> dict[str, Any]:
    """把载荷里的通知配置全部置为关闭/空，并移除相关字段。

    这样即使前端或旧配置里带着 ntfy / webhook 设置，保存后也不会再
    触发任何外部请求。
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    out["notify"] = {
        "enabled": False,
        "notify_on": [],
        "ntfy_enabled": False,
        "ntfy_url": "",
        "ntfy_topic": "",
        "ntfy_token": "",
        "ntfy_priority": "",
        "webhook_enabled": False,
        "webhook_url": "",
        "webhook_method": "POST",
        "webhook_format": "json",
        "webhook_headers": {},
    }
    return out


# ===========================================================================
# 限速策略（在「PT 策略」页维护）
# ===========================================================================
@router.get("/policies/config", summary="读取策略配置（可编辑）")
async def pt_policies_config(
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> list[dict[str, Any]]:
    """返回可编辑的策略配置列表。

    与 ``/api/pt/policies``（运行状态）不同，这里是**配置**：
    窗口、阈值、限速值等，用于表单编辑。
    """
    if not mgr.status.running:
        raise _unavailable(mgr.status.last_error or "子服务未运行，无法读取策略")
    try:
        form = await mgr.fetch_form_config()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc
    return form.get("policies") or []


class PolicyListPayload(BaseModel):
    """保存整套策略配置。"""

    policies: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/policies/config", summary="保存策略配置")
async def pt_put_policies(
    payload: PolicyListPayload,
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> dict[str, Any]:
    """保存策略。

    Go 侧的表单提交要求完整配置，所以这里先读一次当前表单，
    只替换 policies 后整体提交 —— 下载器等其它配置原样保留。
    """
    if not mgr.status.running:
        raise _unavailable("子服务未运行")
    try:
        current = await mgr.fetch_form_config()
    except _proxy_unavailable as exc:
        raise _unavailable(exc) from exc

    current = _strip_notify(current)
    current["policies"] = payload.policies

    try:
        result = await mgr.save_form_config(current)
    except _proxy_unavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    return result
