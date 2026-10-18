"""日志与事件持久化工具。

设计目标：

1. 标准 logging 输出到 stdout（Docker 日志可见）**并**落盘到
   ``DATA_DIR/logs/guard.log``（按大小轮转）。
2. 关键异常同时写进 SQLite 的 ``event_logs`` 表，供 WebUI 展示。
   写库失败绝不能影响业务主流程，因此所有 DB 写入都包了 try/except。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Any

from .config import LOG_DIR

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根 logger（幂等，重复调用无副作用）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.setLevel(level)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "guard.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # 降低第三方库噪音
    for noisy in ("apscheduler.executors.default", "httpx", "urllib3", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取业务 logger。"""
    return logging.getLogger(name)


def utcnow_ts() -> float:
    """当前 UTC Unix 时间戳（秒，float）。

    全系统的时间比较都基于这个函数，避免容器 TZ 与宿主机不一致。
    """
    return datetime.now(timezone.utc).timestamp()


def ts_to_text(ts: float | None) -> str:
    """时间戳转本地可读字符串（用于日志与导出）。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def local_day_start_ts(ts: float | None = None) -> float:
    """返回“本地时区今天 00:00:00”对应的 Unix 时间戳。

    不能用 ``ts - (ts % 86400)``：那得到的是 UTC 零点，在东八区会偏差 8 小时，
    导致“今日秒传数”统计错位。
    """
    current = datetime.fromtimestamp(ts if ts is not None else utcnow_ts())
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def log_event(
    level: str,
    source: str,
    message: str,
    detail: Any = "",
    *,
    session=None,
) -> None:
    """写入一条事件到 ``event_logs``。

    :param session: 可选的 SQLAlchemy Session。传入则复用（同事务），
                    不传则自建短会话。任何异常都会被吞掉并降级为日志。
    """
    logger = get_logger("guard.event")
    text = f"[{source}] {message}"
    if level == "error":
        logger.error(text)
    elif level == "warning":
        logger.warning(text)
    else:
        logger.info(text)

    # ---- 延迟导入，避免 models <-> logging 循环依赖 ----
    try:
        from .models import EventLog
    except Exception:  # pragma: no cover - models 尚未就绪时忽略
        return

    try:
        detail_text = "" if detail in (None, "") else str(detail)
        if len(detail_text) > 8000:
            detail_text = detail_text[:8000] + "...(truncated)"

        if session is not None:
            session.add(
                EventLog(
                    level=level,
                    source=source,
                    message=text[:2000],
                    detail=detail_text,
                    created_at=datetime.now(timezone.utc),
                )
            )
        else:
            from .models import session_scope

            with session_scope() as s:
                s.add(
                    EventLog(
                        level=level,
                        source=source,
                        message=text[:2000],
                        detail=detail_text,
                        created_at=datetime.now(timezone.utc),
                    )
                )
    except Exception as exc:  # noqa: BLE001 - 日志链路绝不影响主流程
        logger.debug("写入 event_logs 失败（已忽略）: %s", exc)
