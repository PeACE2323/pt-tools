"""规则匹配的纯逻辑（不依赖数据库、不依赖任何第三方库）。

抽出来单独成模块的原因：

1. 纯函数，便于单元测试（不需要 SQLAlchemy / APScheduler / grpc）。
2. 调度器与 API 层可共用同一套匹配语义，避免两处实现漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

# UploadFileInfo.OperatorType 映射
OPERATOR_MAP: dict[str, int] = {
    "mount": 0,
    "copy": 1,
    "backup": 2,
    "remote": 3,
}

# 本项目的上传来自 CD2 备份任务，因此拦截动作与重试策略是固定的：
#   取消该文件的上传任务 → 等待间隔 → BackupRestartWalkingThrough 让备份重扫
# 规则里不再提供 pause/恢复 选项（暂停再恢复不会改变能否秒传）。
CANCEL_ACTION = "cancel"
REUPLOAD_STRATEGY = "cancel_reupload"


class RuleLike(Protocol):
    """规则对象需要满足的最小接口（真实实现是 models.Rule）。"""

    id: int
    name: str
    enabled: bool
    source_keywords: str
    is_regex: bool
    timeout_threshold: int
    intercept_action: str
    retry_strategy: str
    max_retry: int
    retry_interval: int
    operator_filter: str

    def keyword_list(self) -> list[str]: ...


class TaskLike(Protocol):
    """任务对象需要满足的最小接口（真实实现是 cd2_client.UploadTask）。"""

    key: str
    dest_path: str
    source_path: str
    operator_type: int


@dataclass(slots=True)
class MatchResult:
    """规则命中结果：把规则的判定参数固化成不可变快照。"""

    rule_id: int
    rule_name: str
    timeout_threshold: int
    intercept_action: str
    retry_strategy: str
    max_retry: int
    retry_interval: int


def keyword_list_of(rule: Any) -> list[str]:
    """兼容两种规则对象：有 keyword_list() 方法，或只有 source_keywords 字段。"""
    getter = getattr(rule, "keyword_list", None)
    if callable(getter):
        return getter()
    raw = getattr(rule, "source_keywords", "") or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def compile_keyword(keyword: str, is_regex: bool) -> Callable[[str], bool] | None:
    """把单个关键字编译成 ``path -> bool`` 匹配器。

    正则非法时**降级为普通子串匹配**而不是抛异常，避免一条坏规则
    导致整轮扫描失败。
    """
    text = (keyword or "").strip()
    if not text:
        return None

    if is_regex:
        try:
            pattern = re.compile(text, re.IGNORECASE)
        except re.error:
            lowered = text.lower()
            return lambda path: lowered in path.lower()
        return lambda path: bool(pattern.search(path))

    lowered = text.lower()
    return lambda path: lowered in path.lower()


def build_matcher(rule: Any) -> Callable[[str], bool] | None:
    """为一条规则构造匹配函数；没有有效关键字则返回 None。

    多关键字之间是 **或** 关系：命中任意一个即视为命中该规则。
    """
    keywords = keyword_list_of(rule)
    if not keywords:
        return None

    is_regex = bool(getattr(rule, "is_regex", False))
    matchers = [m for m in (compile_keyword(k, is_regex) for k in keywords) if m]
    if not matchers:
        return None

    return lambda path: any(m(path) for m in matchers)


def match_rule(task: TaskLike, rules: Sequence[Any]) -> MatchResult | None:
    """按给定顺序（调用方需已按 priority 排序）返回第一条命中的规则。"""
    text = task.source_path or task.dest_path or task.key
    operator_type = getattr(task, "operator_type", 0)

    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue

        operator_filter = getattr(rule, "operator_filter", "any") or "any"
        if operator_filter != "any":
            expected = OPERATOR_MAP.get(operator_filter)
            if expected is not None and operator_type != expected:
                continue

        matcher = build_matcher(rule)
        if matcher is None:
            continue
        if not matcher(text):
            continue

        return MatchResult(
            rule_id=int(rule.id),
            rule_name=str(rule.name),
            timeout_threshold=max(1, int(getattr(rule, "timeout_threshold", 60) or 60)),
            # 上传来自 CD2 备份任务，拦截动作固定为“取消该文件的上传任务”
            intercept_action=CANCEL_ACTION,
            retry_strategy=REUPLOAD_STRATEGY,
            max_retry=max(0, int(getattr(rule, "max_retry", 3) or 0)),
            retry_interval=max(1, int(getattr(rule, "retry_interval", 300) or 300)),
        )
    return None
