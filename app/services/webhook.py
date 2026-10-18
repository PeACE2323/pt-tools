"""CloudDrive2 Webhook 落库与源路径映射。

解析逻辑在 ``webhook_parser.py``（零依赖、可单测），本模块只负责：

1. 把文件变化写入 ``source_paths`` 映射表；
2. 按文件名反查真实源路径，供规则匹配与重新上传使用。

为什么需要这套机制见 ``docs/WEBHOOK.md``：CD2 的 gRPC 上传任务不含源路径，
也没有“重新上传本地文件”的 RPC，而 webhook 的 ``source_file`` 正好补上。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..utils import get_logger, utcnow_ts
from .webhook_parser import (  # noqa: F401  re-export 便于调用方统一导入
    FileChange,
    WebhookPayload,
    parse_payload,
    to_bool,
    to_timestamp,
)

logger = get_logger("guard.webhook")


# ===========================================================================
# 落库
# ===========================================================================
def record_changes(payload: WebhookPayload) -> dict[str, int]:
    """把文件变化写入 ``source_paths`` 映射表。

    只关心**文件**（目录对上传匹配无意义），并按 ``file_name`` 去重：
    同一文件名只保留最新一条，避免表无限膨胀。
    """
    stats = {"received": len(payload.changes), "recorded": 0, "skipped": 0}
    if not payload.changes:
        return stats

    # 延迟导入：让本模块在无 SQLAlchemy 环境仍可被解析层测试导入
    from ..models import SourcePath, session_scope

    with session_scope() as session:
        for change in payload.changes:
            if change.is_dir:
                stats["skipped"] += 1
                continue

            name = change.file_name
            if not name:
                stats["skipped"] += 1
                continue

            path = change.effective_path
            existing = (
                session.query(SourcePath)
                .filter(SourcePath.file_name == name)
                .order_by(SourcePath.seen_at.desc())
                .first()
            )

            if existing is None:
                session.add(
                    SourcePath(
                        file_name=name,
                        source_path=path,
                        dest_path=(
                            change.destination_file
                            if change.action == "rename"
                            else ""
                        ),
                        file_size=0,
                        action=change.action,
                        device_name=payload.device_name,
                        is_dir=False,
                        seen_at=change.event_time or utcnow_ts(),
                    )
                )
            else:
                existing.source_path = path
                existing.action = change.action
                existing.device_name = payload.device_name or existing.device_name
                existing.seen_at = change.event_time or utcnow_ts()
                if change.action == "rename" and change.destination_file:
                    existing.dest_path = change.destination_file

            stats["recorded"] += 1

    logger.info(
        "webhook 记录: 收到 %d 条，入库 %d 条，跳过 %d 条",
        stats["received"], stats["recorded"], stats["skipped"],
    )
    return stats


# ===========================================================================
# 查询
# ===========================================================================
def lookup_source_paths(file_names: Iterable[str]) -> dict[str, str]:
    """按文件名批量查真实源路径，返回 ``{file_name: source_path}``。"""
    names = [n for n in file_names if n]
    if not names:
        return {}

    from ..models import SourcePath, session_scope

    with session_scope() as session:
        rows = (
            session.query(SourcePath)
            .filter(SourcePath.file_name.in_(names))
            .order_by(SourcePath.seen_at.desc())
            .all()
        )

    result: dict[str, str] = {}
    for row in rows:
        # 已按 seen_at 降序排列，setdefault 保留最新的那条
        result.setdefault(row.file_name, row.source_path)
    return result


def lookup_source_path(file_name: str) -> str | None:
    """按单个文件名查最新源路径。"""
    if not file_name:
        return None
    return lookup_source_paths([file_name]).get(file_name)


def enrich_tasks(tasks: list[Any]) -> int:
    """用映射表补全任务对象的源路径（原地修改），返回补全数量。

    只有当任务自身无法得出可信源路径时才覆盖；webhook 记录优先。
    """
    if not tasks:
        return 0

    names = [getattr(t, "file_name", "") for t in tasks]
    mapping = lookup_source_paths(names)
    if not mapping:
        return 0

    patched = 0
    for task in tasks:
        real = mapping.get(getattr(task, "file_name", ""))
        if not real:
            continue
        setter = getattr(task, "set_source_override", None)
        if callable(setter):
            setter(real)
            patched += 1
    return patched
