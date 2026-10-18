"""PTtools 应用入口。

职责：

* 注册全部 API 路由（CD2 守卫、115 监控、BT 流量反代）
* 挂载静态 WebUI（``static/index.html`` + 依赖走 CDN）
* 生命周期钩子：启动时建库、加载配置、启动调度器、拉起 BT 子服务
* 全局异常处理，保证 API 与 Webhook 不因单点异常雪崩
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import dashboard, p115, pt, rules, settings, tasks, webhook
from .config import STATIC_DIR
from .models import init_db
from .services.scheduler import get_scheduler
from .utils import get_logger, log_event, setup_logging

logger = get_logger("guard.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动初始化 + 关闭清理。"""
    setup_logging()
    logger.info("PTtools v%s 启动中 ...", __version__)

    # 1) 建库 + 播种默认配置
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        logger.exception("数据库初始化失败")
        log_event("error", "system", "数据库初始化失败", detail=str(exc))

    # 2) 启动调度器（内部会加载配置）
    scheduler = get_scheduler()
    try:
        await scheduler.start()
        logger.info("调度器已启动")
    except Exception as exc:  # noqa: BLE001
        logger.exception("调度器启动失败")
        log_event("error", "system", "调度器启动失败", detail=str(exc))

    # 3) 初始化 PT 流量监控（读取已保存的下载器与策略）
    #    失败不阻断启动：CD2/115 功能应照常可用
    try:
        from .services.bt_monitor import get_pt_monitor

        await get_pt_monitor().reload()
        logger.info("PT 流量监控已就绪")
    except Exception as exc:  # noqa: BLE001
        logger.exception("PT 流量监控初始化失败")
        log_event("error", "system", "PT 监控初始化失败", detail=str(exc))

    logger.info("服务已就绪")

    try:
        yield
    finally:
        logger.info("正在关闭 ...")
        # 退出前必须解除所有限速：
        # 否则下载器会永久停留在限速状态，用户下次启动时
        # 会发现上传异常慢却不知道为什么。原 Go 子服务也是这样做的。
        try:
            from .services.bt_monitor import get_pt_monitor

            monitor = get_pt_monitor()
            for state in monitor.engine.states():
                if state.limited:
                    try:
                        await monitor.engine.force_resume(state.name, monitor._clients)  # noqa: SLF001
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("解除策略 %s 限速失败: %s", state.name, exc)
            await monitor.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("PT 监控关闭异常: %s", exc)
        try:
            await scheduler.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("调度器关闭异常: %s", exc)
        logger.info("已关闭")


app = FastAPI(
    title="PTtools",
    description=(
        "PT 站点运维工具箱：CloudDrive2 上传任务秒传判定 / 超时拦截 / 备份重扫重试，"
        "115 云下载超时清理，以及 BT 下载器上传流量监控与自动限速。"
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

# 同源部署，无需 CORS；若需跨域调试可放开
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# 全局异常处理
# ===========================================================================
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """未捕获异常：记录并返回 JSON，避免返回整页 HTML 堆栈。"""
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误",
            "error": f"{exc.__class__.__name__}: {exc}",
            "path": request.url.path,
        },
    )


# ===========================================================================
# 路由注册
# ===========================================================================
app.include_router(dashboard.router)
app.include_router(rules.router)
app.include_router(tasks.router)
app.include_router(p115.router)
app.include_router(settings.router)
app.include_router(webhook.router)
# PT 流量监控：完全由 Python 实现，无子进程、无反代
app.include_router(pt.router)


# ===========================================================================
# 健康检查
# ===========================================================================
@app.get("/api/health", tags=["system"], summary="健康检查")
async def health() -> dict[str, object]:
    """供 Docker HEALTHCHECK 与前端探活使用。"""
    scheduler = get_scheduler()
    return {
        "ok": True,
        "version": __version__,
        "scheduler_running": bool(
            getattr(scheduler, "scheduler", None) is not None
            and scheduler.scheduler.running
        ),
    }


@app.get("/api/version", tags=["system"], summary="版本信息")
async def version() -> dict[str, str]:
    return {"name": "CD2-115-Guard", "version": __version__}


# ===========================================================================
# 静态资源（放在最后，避免抢占 /api 前缀）
# ===========================================================================
INDEX_FILE = STATIC_DIR / "index.html"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False, response_model=None)
async def index() -> Response:
    """返回单页 WebUI。

    ``response_model=None`` 是必须的：返回类型是 Response 的子类联合，
    FastAPI 会试图把它当 Pydantic 响应模型解析并在启动时报错。
    """
    if INDEX_FILE.exists():
        # no-cache：避免改前端后浏览器拿到旧页面
        return FileResponse(
            INDEX_FILE,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
    return RedirectResponse(url="/api/docs")


@app.get("/favicon.ico", include_in_schema=False, response_model=None)
async def favicon() -> Response:
    """避免浏览器 404 噪音（WebUI 通过内联 SVG 图标展示）。"""
    return Response(status_code=204)
