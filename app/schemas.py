"""Pydantic 请求/响应模型。

约定：

* 时间字段对外一律以 **Unix 时间戳（秒，float）** 传输，前端自行本地化，
  避免服务端 TZ 与浏览器 TZ 不一致。
* 列表接口统一返回 ``{items, total}``，便于前端分页。
* 所有字段均给默认值，减少前端判空分支。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class ORMModel(BaseModel):
    """可从 ORM 对象直接构造的基础模型。"""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """分页响应包装。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


# ===========================================================================
# 规则
# ===========================================================================
class RuleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="规则名称")
    enabled: bool = True
    source_keywords: str = Field(
        "",
        description="源文件夹路径关键字，多行文本，每行一个关键字（或正则）",
    )
    is_regex: bool = Field(False, description="是否把关键字作为正则表达式解释")
    timeout_threshold: int = Field(
        60, ge=1, le=7 * 24 * 3600, description="超时判定阈值（秒）"
    )
    max_retry: int = Field(3, ge=0, le=100, description="最大重试次数")
    retry_interval: int = Field(
        300, ge=1, le=7 * 24 * 3600, description="重试间隔（秒）"
    )
    priority: int = Field(100, ge=0, le=10000, description="优先级，数值越小越先匹配")
    operator_filter: Literal["any", "mount", "copy", "backup", "remote"] = "any"

    @field_validator("source_keywords")
    @classmethod
    def _strip_keywords(cls, value: str) -> str:
        """逐行去空白，丢弃空行，避免“空行即空关键字”。"""
        if not value:
            return ""
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return "\n".join(lines)

    @field_validator("is_regex")
    @classmethod
    def _validate_regex_flag(cls, value: bool) -> bool:
        return bool(value)


class RuleCreate(RuleBase):
    pass


class RuleUpdate(BaseModel):
    """部分更新：所有字段可选。"""

    name: str | None = Field(None, min_length=1, max_length=128)
    enabled: bool | None = None
    source_keywords: str | None = None
    is_regex: bool | None = None
    timeout_threshold: int | None = Field(None, ge=1, le=7 * 24 * 3600)
    max_retry: int | None = Field(None, ge=0, le=100)
    retry_interval: int | None = Field(None, ge=1, le=7 * 24 * 3600)
    priority: int | None = Field(None, ge=0, le=10000)
    operator_filter: Literal["any", "mount", "copy", "backup", "remote"] | None = None

    @field_validator("source_keywords")
    @classmethod
    def _strip_keywords(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return "\n".join(lines)


class RuleOut(ORMModel):
    id: int
    name: str
    enabled: bool
    source_keywords: str
    is_regex: bool
    timeout_threshold: int
    max_retry: int
    retry_interval: int
    priority: int
    operator_filter: str
    # CD2 备份场景下这两项恒为 cancel / cancel_reupload，仅用于前端展示
    intercept_action: str = "cancel"
    retry_strategy: str = "cancel_reupload"
    created_at: Any = None
    updated_at: Any = None


class RuleToggle(BaseModel):
    enabled: bool


class RuleValidateRequest(BaseModel):
    """校验关键字是否命中给定路径（规则编辑页实时预览用）。"""

    source_keywords: str = ""
    is_regex: bool = False
    test_paths: list[str] = Field(default_factory=list)


class RuleValidateResult(BaseModel):
    errors: list[str] = Field(default_factory=list)
    matched: list[str] = Field(default_factory=list)
    unmatched: list[str] = Field(default_factory=list)


# ===========================================================================
# 实时上传任务
# ===========================================================================
class UploadTaskOut(ORMModel):
    id: int
    task_key: str
    file_name: str
    source_path: str
    dest_path: str
    size: int
    transferred: int
    #: 生命周期内最大传输量。CD2 任务完成后常把 transferred 归零，
    #: 用它能看出「到底传没传过数据」。
    transferred_peak: int = 0
    status_text: str
    status_enum: int
    operator_type: int
    matched_rule_id: int | None = None
    matched_rule_name: str = ""
    first_seen_at: float
    last_seen_at: float
    state: str
    error_message: str = ""

    # 派生字段（由 API 层填充）
    elapsed_sec: float = 0.0
    progress: float = 0.0
    # 排队累计时长 / 实际传输时长：区分「排队等待」与「真的在传」，
    # 避免用户误以为排队中的任务是卡住了
    queued_sec: float = 0.0
    transfer_sec: float = 0.0


# ===========================================================================
# 秒传记录
# ===========================================================================
class InstantUploadOut(ORMModel):
    id: int
    task_key: str
    file_name: str
    source_path: str
    dest_path: str
    size: int
    rule_id: int | None = None
    rule_name: str = ""
    first_seen_at: float
    finished_at: float
    duration_sec: float
    # 完成时实际传输的字节数：接近 0 说明确实没传数据（真秒传）
    transferred_bytes: int = 0


# ===========================================================================
# 不能秒传 / 重试队列
# ===========================================================================
class NonInstantTaskOut(ORMModel):
    id: int
    task_key: str
    file_name: str
    source_path: str
    dest_path: str
    size: int
    rule_id: int | None = None
    rule_name: str = ""
    status: str
    retry_count: int
    max_retry: int
    retry_interval: int
    retry_strategy: str
    intercept_action: str
    first_seen_at: float
    intercepted_at: float | None = None
    timeout_threshold: int
    last_uploaded_sec: float
    next_retry_at: float | None = None
    last_retry_at: float | None = None
    finished_at: float | None = None
    error_message: str = ""


class IdList(BaseModel):
    """批量操作请求体。"""

    ids: list[int] = Field(default_factory=list, description="目标记录 id 列表")


class ActionResult(BaseModel):
    """批量操作结果。"""

    affected: int = 0
    message: str = ""
    details: list[str] = Field(default_factory=list)


# ===========================================================================
# 115 云下载
# ===========================================================================
class OfflineTaskOut(ORMModel):
    id: int
    info_hash: str
    name: str
    size: int
    add_time: int
    status: int
    percent_done: float
    normalized_status: str
    first_seen_at: float
    last_seen_at: float

    # 派生字段
    age_sec: float = 0.0
    over_threshold: bool = False


class CancelLogOut(ORMModel):
    id: int
    info_hash: str
    name: str
    add_time: int
    cancel_at: float
    percent_done: float
    status: int
    age_sec: float
    threshold_sec: int
    success: bool
    message: str = ""
    trigger: str


class HashList(BaseModel):
    info_hashes: list[str] = Field(default_factory=list)


# ===========================================================================
# 系统设置
# ===========================================================================
class SettingsOut(BaseModel):
    """设置读取：敏感字段脱敏。"""

    cd2_host: str = ""
    cd2_token_masked: str = ""
    cd2_token_set: bool = False
    cd2_username: str = ""
    cd2_password_set: bool = False
    cd2_poll_interval: int = 3

    p115_cookie_masked: str = ""
    p115_cookie_set: bool = False
    p115_poll_interval: int = 300
    p115_timeout_threshold: int = 86400
    p115_auto_cancel: bool = True
    p115_task_page_size: int = 30

    webhook_token_set: bool = False
    webhook_url_hint: str = ""


class SettingsUpdate(BaseModel):
    """设置写入：留空表示不修改该字段。"""

    cd2_host: str | None = None
    cd2_token: str | None = None
    cd2_username: str | None = None
    cd2_password: str | None = None
    cd2_poll_interval: int | None = Field(None, ge=2, le=3600)

    p115_cookie: str | None = None
    p115_poll_interval: int | None = Field(None, ge=30, le=86400)
    p115_timeout_threshold: int | None = Field(None, ge=60, le=90 * 86400)
    p115_auto_cancel: bool | None = None
    p115_task_page_size: int | None = Field(None, ge=10, le=200)

    webhook_token: str | None = None


class ConnectionTestResult(BaseModel):
    ok: bool = False
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class TokenResult(BaseModel):
    ok: bool = False
    token: str = ""
    message: str = ""


# ===========================================================================
# 仪表盘
# ===========================================================================
class DashboardOut(BaseModel):
    cd2: dict[str, Any] = Field(default_factory=dict)
    p115: dict[str, Any] = Field(default_factory=dict)
    #: PT 流量总览（来自 Go 子服务），不可用时 available=false
    pt: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    scheduler: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# 事件日志
# ===========================================================================
class EventLogOut(ORMModel):
    id: int
    level: str
    source: str
    message: str
    detail: str = ""
    created_at: Any = None


# ===========================================================================
# Webhook
# ===========================================================================
class WebhookAck(BaseModel):
    """CD2 webhook 回调响应（CD2 不解析响应体，返回简洁即可）。"""

    ok: bool = True
    received: int = 0
    recorded: int = 0
    skipped: int = 0
