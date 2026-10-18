"""CD2 规则管理 API。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..models import Rule
from ..schemas import (
    RuleCreate,
    RuleOut,
    RuleToggle,
    RuleUpdate,
    RuleValidateRequest,
    RuleValidateResult,
)
from ..services.rules import CANCEL_ACTION, REUPLOAD_STRATEGY, build_matcher
from .deps import db_session, scheduler

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _to_out(rule: Rule) -> RuleOut:
    data = RuleOut.model_validate(rule)
    data.intercept_action = CANCEL_ACTION
    data.retry_strategy = REUPLOAD_STRATEGY
    return data


@router.get("", response_model=list[RuleOut], summary="列出全部规则")
def list_rules(session: Session = Depends(db_session)) -> list[RuleOut]:
    rules = (
        session.query(Rule)
        .order_by(Rule.priority.asc(), Rule.id.asc())
        .all()
    )
    return [_to_out(r) for r in rules]


@router.post(
    "",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建规则",
)
async def create_rule(
    payload: RuleCreate,
    session: Session = Depends(db_session),
) -> RuleOut:
    rule = Rule(
        name=payload.name,
        enabled=payload.enabled,
        source_keywords=payload.source_keywords,
        is_regex=payload.is_regex,
        timeout_threshold=payload.timeout_threshold,
        # 备份场景下策略固定，忽略用户传入
        intercept_action=CANCEL_ACTION,
        retry_strategy=REUPLOAD_STRATEGY,
        max_retry=payload.max_retry,
        retry_interval=payload.retry_interval,
        priority=payload.priority,
        operator_filter=payload.operator_filter,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return _to_out(rule)


@router.get("/{rule_id}", response_model=RuleOut, summary="获取单条规则")
def get_rule(rule_id: int, session: Session = Depends(db_session)) -> RuleOut:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则不存在")
    return _to_out(rule)


@router.put("/{rule_id}", response_model=RuleOut, summary="更新规则")
async def update_rule(
    rule_id: int,
    payload: RuleUpdate,
    session: Session = Depends(db_session),
) -> RuleOut:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则不存在")

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in data.items():
        setattr(rule, field, value)
    # 策略始终固定
    rule.intercept_action = CANCEL_ACTION
    rule.retry_strategy = REUPLOAD_STRATEGY
    rule.updated_at = datetime.now(timezone.utc)

    session.commit()
    session.refresh(rule)
    return _to_out(rule)


@router.patch("/{rule_id}/toggle", response_model=RuleOut, summary="启用/禁用规则")
async def toggle_rule(
    rule_id: int,
    payload: RuleToggle,
    session: Session = Depends(db_session),
) -> RuleOut:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则不存在")
    rule.enabled = payload.enabled
    rule.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(rule)
    return _to_out(rule)


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除规则",
)
async def delete_rule(rule_id: int, session: Session = Depends(db_session)) -> None:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则不存在")
    session.delete(rule)
    session.commit()


@router.post(
    "/validate",
    response_model=RuleValidateResult,
    summary="校验关键字（正则合法性 + 路径命中预览）",
)
def validate_keywords(payload: RuleValidateRequest) -> RuleValidateResult:
    """规则编辑页的实时校验：检查正则能否编译，并预览哪些路径会命中。"""

    result = RuleValidateResult()

    lines = [ln.strip() for ln in (payload.source_keywords or "").splitlines() if ln.strip()]
    if not lines:
        result.errors.append("关键字为空：该规则不会匹配任何任务")
        return result

    # 用一个临时对象复用 build_matcher 的编译逻辑
    class _Tmp:
        source_keywords = payload.source_keywords
        is_regex = payload.is_regex

        def keyword_list(self):
            return lines

    import re

    if payload.is_regex:
        for line in lines:
            try:
                re.compile(line)
            except re.error as exc:
                result.errors.append(f"正则无效 {line!r}: {exc}（将降级为子串匹配）")

    matcher = build_matcher(_Tmp())
    if matcher is None:
        result.errors.append("未能构造匹配器，请检查关键字")
        return result

    for path in payload.test_paths:
        (result.matched if matcher(path) else result.unmatched).append(path)

    return result


@router.post("/reload", summary="让调度器立即重载规则与配置")
async def reload_rules(sched=Depends(scheduler)) -> dict[str, str]:
    await sched.reload_config()
    return {"message": "配置已重新加载"}
