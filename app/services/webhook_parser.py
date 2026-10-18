"""Webhook 数据解析（零第三方依赖的纯逻辑）。

拆出来的原因：

1. 可脱离 SQLAlchemy 单独单元测试；
2. 解析规则与落库逻辑分离，互不影响。

CD2 ``file_system_watcher`` webhook 的 body（来自官方配置模板）：

    {
        "device_name": "...", "user_name": "...", "version": "...",
        "event_category": "file", "event_name": "notify",
        "event_time": "...", "send_time": "...",
        "data": [
            {"action": "create", "is_dir": "false",
             "source_file": "/mnt/PT/a.mkv", "destination_file": ""}
        ]
    }

两个易错点：

* ``is_dir`` 在模板里是**字符串** ``"false"``/``"true"``；
* ``event_time``/``send_time`` 受 CD2 的 ``time_format`` 控制，可能是
  epoch 秒、毫秒、RFC3339 或自定义 ``strftime`` 格式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 只关心这些动作（move 是 rename 的别名）
KNOWN_ACTIONS = {"create", "modify", "delete", "rename", "move"}


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass(slots=True)
class FileChange:
    """单个文件变化事件（已归一化）。"""

    action: str
    is_dir: bool
    source_file: str
    destination_file: str = ""
    event_time: float = 0.0

    @property
    def is_file(self) -> bool:
        return not self.is_dir

    @property
    def effective_path(self) -> str:
        """rename 时取目标路径，否则取源路径。"""
        if self.action == "rename" and self.destination_file:
            return self.destination_file
        return self.source_file

    @property
    def file_name(self) -> str:
        path = self.effective_path.rstrip("/\\")
        return os.path.basename(path) if path else ""


@dataclass(slots=True)
class WebhookPayload:
    """整条 webhook 事件。"""

    device_name: str = ""
    user_name: str = ""
    version: str = ""
    event_category: str = ""
    event_name: str = ""
    event_time: float = 0.0
    changes: list[FileChange] = field(default_factory=list)


# ===========================================================================
# 字段归一化
# ===========================================================================
def to_bool(value: Any) -> bool:
    """把 ``"true"``/``"false"``/``True``/``1`` 等归一化为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return False


def to_timestamp(value: Any) -> float:
    """尽力把时间字段转成 Unix 时间戳；失败返回 0（不抛异常）。"""
    if value is None or value == "":
        return 0.0

    if isinstance(value, (int, float)):
        number = float(value)
        # 毫秒级时间戳兜底
        if number > 1e11:
            number /= 1000.0
        return number

    text = str(value).strip()
    if not text:
        return 0.0

    if text.replace(".", "", 1).isdigit():
        return to_timestamp(float(text))

    # RFC3339 / ISO8601
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue

    return 0.0


# ===========================================================================
# 解析
# ===========================================================================
def parse_payload(body: dict[str, Any]) -> WebhookPayload:
    """把 webhook 请求体解析为 ``WebhookPayload``（全程容错）。"""
    if not isinstance(body, dict):
        return WebhookPayload()

    payload = WebhookPayload(
        device_name=str(body.get("device_name") or ""),
        user_name=str(body.get("user_name") or ""),
        version=str(body.get("version") or ""),
        event_category=str(body.get("event_category") or ""),
        event_name=str(body.get("event_name") or ""),
        event_time=to_timestamp(body.get("event_time")),
    )

    raw_items = body.get("data")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return payload

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        action = str(item.get("action") or "").strip().lower()
        if action == "move":
            action = "rename"
        if action not in KNOWN_ACTIONS:
            continue

        change = FileChange(
            action=action,
            is_dir=to_bool(item.get("is_dir")),
            source_file=str(item.get("source_file") or "").strip(),
            destination_file=str(item.get("destination_file") or "").strip(),
            event_time=payload.event_time,
        )
        if not change.effective_path:
            continue
        payload.changes.append(change)

    return payload
