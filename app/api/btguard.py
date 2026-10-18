"""BT 流量监控（bt-traffic-guard）的反向代理与状态接口。

路径约定：

* ``/btg/*``        —— 透明转发到子服务的同名路径
* ``/api/btg/status`` —— 子服务运行状态（供仪表盘与前端展示）

设计要点见 ``app/services/btguard.py`` 的模块文档。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..services.btguard import (
    PROXY_PREFIX,
    BTGuardManager,
    _proxy_unavailable,
    get_btg_manager,
    proxy_request,
)
from ..utils import get_logger

logger = get_logger("guard.api.btg")

router = APIRouter(tags=["btguard"])


# ===========================================================================
# 状态接口
# ===========================================================================
@router.get("/api/btg/status", summary="BT 流量子服务运行状态")
async def btg_status(mgr: BTGuardManager = Depends(get_btg_manager)) -> dict[str, Any]:
    st = mgr.status
    healthy = await mgr.health() if st.running else False
    return {
        "available": mgr.is_available(),
        "running": st.running,
        "healthy": healthy,
        "managed": st.managed,
        "enabled": mgr.enabled,
        "pid": st.pid,
        "listen": st.listen,
        "started_at": st.started_at,
        "restart_count": st.restart_count,
        "last_error": st.last_error,
        "prefix": PROXY_PREFIX,
    }


@router.post("/api/btg/restart", summary="重启 BT 流量子服务")
async def btg_restart(mgr: BTGuardManager = Depends(get_btg_manager)) -> dict[str, Any]:
    """手动重启子服务（配置保存后可能需要）。"""
    await mgr.stop()
    mgr.set_enabled(True)
    mgr.reset_restart_counter()
    ok = await mgr.start()
    return {
        "ok": ok,
        "message": "已启动" if ok else (mgr.status.last_error or "启动失败"),
    }


# ===========================================================================
# 反向代理
# ===========================================================================
@router.api_route(
    f"{PROXY_PREFIX}/{{full_path:path}}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
    response_model=None,
)
async def btg_proxy(
    request: Request,
    full_path: str,
    mgr: BTGuardManager = Depends(get_btg_manager),
) -> Response:
    """把 ``/btg/*`` 转发到 BT 子服务。

    子服务未运行时返回 503 + 明确提示，而不是让前端拿到
    一堆连接错误。
    """
    if not mgr.status.running:
        available = mgr.is_available()
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "BT 流量子服务未运行"
                    if available
                    else "BT 流量子服务未安装（镜像构建时未包含 Go 产物）"
                ),
                "available": available,
                "last_error": mgr.status.last_error,
            },
        )

    path = "/" + full_path.lstrip("/") if full_path else "/"
    body = await request.body()

    # 透传请求头，去掉 Host 等
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection")
    }

    try:
        status, headers, content = await proxy_request(
            mgr,
            request.method,
            path,
            query=request.url.query or "",
            body=body or None,
            headers=fwd_headers,
        )
    except _proxy_unavailable as exc:
        logger.warning("BT 反代失败: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": f"BT 子服务无响应: {exc}"},
        )

    return Response(content=content, status_code=status, headers=headers)
