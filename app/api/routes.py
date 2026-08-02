from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agents.chat_graph import build_chat_graph
from app.agents.graph import build_planning_graph
from app.config import get_settings
from app.models.schemas import (
    ApprovalDecision,
    PlanRequest,
    PlanRunResponse,
    PlanStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter()
graph = build_planning_graph()
chat_graph = build_chat_graph()

WORKFLOW_NODES = (
    {
        "id": "retrieve_resources",
        "label": "搜索资源",
        "group": "main",
        "activities": ["搜索景点与真实 POI", "过滤不安全内容", "按需求评分和筛选"],
    },
    {
        "id": "plan_itinerary",
        "label": "编排行程",
        "group": "main",
        "activities": ["获取天气预报", "计算真实交通时间", "优化分日路线与时间表"],
    },
    {
        "id": "validate_constraints",
        "label": "检查约束",
        "group": "main",
        "activities": ["检查时间冲突", "核对必去地点", "检查节奏、用餐与预算"],
    },
    {
        "id": "repair_plan",
        "label": "修复方案",
        "group": "repair",
        "activities": ["定位阻断问题", "搜索替代资源", "返回行程节点重新编排"],
    },
    {
        "id": "calculate_quote",
        "label": "核算成本",
        "group": "main",
        "activities": ["估算交通、住宿、餐饮与门票", "核对人均预算", "计算售价与毛利"],
    },
    {
        "id": "quality_review",
        "label": "质量审核",
        "group": "main",
        "activities": ["评估事实可追溯性", "评估可执行性", "评估人群匹配度"],
    },
    {
        "id": "run_verification",
        "label": "最终验证",
        "group": "main",
        "activities": ["执行确定性检查", "核对报价与行程完整性", "判定是否可以交付"],
    },
    {
        "id": "review_repair",
        "label": "审核修复",
        "group": "repair",
        "activities": ["汇总审核与验证问题", "生成修复反馈", "返回行程节点重新策划"],
    },
    {
        "id": "prepare_poster",
        "label": "生成图片",
        "group": "main",
        "activities": ["生成封面图", "按天生成行程配图", "失败时使用真实照片或地图备用"],
    },
    {
        "id": "approval_gate",
        "label": "等待确认",
        "group": "main",
        "activities": ["暂停自动工作流", "展示待交付方案", "等待用户批准或驳回"],
    },
    {
        "id": "finalize_delivery",
        "label": "生成交付",
        "group": "main",
        "activities": ["生成详细 Markdown", "生成带图 PDF", "保存方案版本与审批记录"],
    },
    {
        "id": "mark_failed",
        "label": "策划失败",
        "group": "terminal",
        "activities": ["停止工作流", "保留错误原因", "不生成不可靠交付物"],
    },
    {
        "id": "mark_rejected",
        "label": "记录驳回",
        "group": "terminal",
        "activities": ["记录驳回意见", "保存审批决定", "结束本次策划"],
    },
)
WORKFLOW_NODE_MAP = {node["id"]: node for node in WORKFLOW_NODES}


@router.get("/health")
def health() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "status": "ok",
        "mock_model_mode": settings.mock_model_mode,
    }


# ------------------------------------------------------------------
# Conversational requirement gathering
# ------------------------------------------------------------------


class ChatMessage(BaseModel):
    thread_id: str = Field(default="", description="留空则创建新会话")
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    ready: bool
    stage: str
    plan_request: dict | None = None


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatMessage) -> ChatResponse:
    thread_id = body.thread_id or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    state = await chat_graph.aget_state(config)
    existing_messages = state.values.get("messages", []) if state and state.values else []
    messages = [*existing_messages, {"role": "user", "content": body.message}]

    try:
        result = await chat_graph.ainvoke(
            {"thread_id": thread_id, "messages": messages},
            config=config,
        )
    except Exception as exc:
        logger.exception("Chat requirement extraction failed")
        raise HTTPException(
            status_code=502,
            detail=_friendly_chat_exception(exc),
        ) from exc

    ready = result.get("ready", False)

    return ChatResponse(
        thread_id=thread_id,
        reply=result.get("reply", "请告诉我更多细节。"),
        ready=ready,
        stage=result.get("stage", "collecting"),
        plan_request=result.get("plan_request"),
    )


# ------------------------------------------------------------------
# Conversation-gated plan execution
# ------------------------------------------------------------------


@router.post("/chat/{chat_thread_id}/plan/stream")
async def run_conversation_plan_stream(chat_thread_id: str) -> StreamingResponse:
    """只能从已由大模型判定信息充分的对话会话启动策划。"""
    chat_config = {"configurable": {"thread_id": chat_thread_id}}
    chat_state = await chat_graph.aget_state(chat_config)
    chat_values = chat_state.values if chat_state and chat_state.values else {}
    if not chat_values.get("ready") or not chat_values.get("plan_request"):
        raise HTTPException(
            status_code=409,
            detail="对话信息尚未收集完整，不能启动策划。",
        )

    request = PlanRequest.model_validate(chat_values["plan_request"])
    planning_thread_id = f"{chat_thread_id}-plan-{uuid4().hex[:8]}"
    plan_id = f"PLAN-{uuid4().hex[:8].upper()}"
    config = {"configurable": {"thread_id": planning_thread_id}}

    async def event_generator():
        active_node_name = ""
        yield _sse_event(
            "started",
            {
                "thread_id": planning_thread_id,
                "chat_thread_id": chat_thread_id,
                "plan_id": plan_id,
                "workflow": WORKFLOW_NODES,
            },
        )

        try:
            async for event in graph.astream_events(
                {
                    "thread_id": planning_thread_id,
                    "plan_id": plan_id,
                    "request": request,
                    "current_stage": "created",
                },
                config=config,
                version="v2",
            ):
                kind = event.get("event", "")
                node_name = event.get("name", "")
                node = WORKFLOW_NODE_MAP.get(node_name)
                if not node:
                    continue
                if kind == "on_chain_start":
                    active_node_name = node_name
                    yield _sse_event("node_start", node)
                elif kind == "on_chain_end":
                    yield _sse_event("node_end", {"id": node_name})

            state = await graph.aget_state(config)
            final_stage = state.values.get("current_stage", "unknown") if state else "unknown"
            completion = {
                "thread_id": planning_thread_id,
                "plan_id": plan_id,
                "current_stage": final_stage,
                "requires_approval": final_stage == "poster_generated",
                "recoverable": final_stage == "failed",
            }
            if final_stage == "failed":
                completion["failure_reasons"] = _collect_failure_reasons(state.values)
            yield _sse_event("completed", completion)
        except Exception as exc:
            logger.exception("Stream plan execution failed")
            reason = _friendly_workflow_exception(exc, active_node_name)
            yield _sse_event(
                "error",
                {
                    "message": reason,
                    "failure_reasons": [reason],
                    "failed_node": active_node_name,
                    "recoverable": True,
                },
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_event(event_type: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _collect_failure_reasons(values: dict) -> list[str]:
    candidates: list[str] = []
    candidates.extend(str(error) for error in values.get("errors", []) if error)

    constraint_report = values.get("constraint_report")
    if constraint_report:
        candidates.extend(
            issue.message
            for issue in constraint_report.issues
            if issue.severity == "blocking"
        )

    quality_report = values.get("quality_report")
    if quality_report:
        candidates.extend(quality_report.blocking_issues)
    candidates.extend(values.get("verification_issues", []))

    reasons: list[str] = []
    for candidate in candidates:
        friendly = _friendly_failure_reason(candidate)
        if friendly and friendly not in reasons:
            reasons.append(friendly)
        if len(reasons) >= 5:
            break
    return reasons or ["方案在当前条件下未通过可执行性检查。"]


def _friendly_failure_reason(reason: str) -> str:
    normalized = reason.strip()
    lowered = normalized.lower()
    if "costbreakdown" in lowered or "greater than 0" in lowered:
        return "成本明细数据不完整，无法生成可靠报价。"
    if "all resource providers failed" in lowered or "no usable resources" in lowered:
        return "当前目的地未检索到足够的可用旅行资源。"
    if "image" in lowered or "poster" in lowered or "comfyui" in lowered:
        return "行程配图未能全部生成。"
    if len(normalized) > 180 or "traceback" in lowered:
        return "工作流内部校验未通过。"
    return normalized


def _friendly_workflow_exception(exc: Exception, node_name: str) -> str:
    detail = str(exc)
    lowered = detail.lower()
    node_label = WORKFLOW_NODE_MAP.get(node_name, {}).get("label", "当前节点")
    if "costbreakdown" in lowered or "greater than 0" in lowered:
        cause = "成本明细格式不完整"
    elif "validation error" in lowered:
        cause = "模型返回的结构化数据不完整"
    elif "timeout" in lowered or "timed out" in lowered:
        cause = "外部服务响应超时"
    elif "resource" in lowered or "provider" in lowered:
        cause = "未获取到足够的可用旅行资源"
    elif "image" in lowered or "poster" in lowered or "comfyui" in lowered:
        cause = "行程配图生成未完成"
    else:
        cause = "执行过程出现异常"
    return f"{node_label}未能完成：{cause}。"


def _friendly_chat_exception(exc: Exception) -> str:
    detail = str(exc).lower()
    if "validation error" in detail or "literal_error" in detail:
        return "需求理解结果格式不完整，请重新表述刚才的回答。"
    if "timeout" in detail or "timed out" in detail:
        return "需求理解服务响应超时，请稍后重试。"
    if "401" in detail or "403" in detail:
        return "需求理解服务认证失败，请检查模型配置。"
    return "需求理解服务暂时不可用，请稍后重试。"


# ------------------------------------------------------------------
# Approval
# ------------------------------------------------------------------


@router.post("/plans/{thread_id}/approval", response_model=PlanRunResponse)
async def approve_plan(thread_id: str, decision: ApprovalDecision) -> PlanRunResponse:
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(Command(resume=decision.model_dump()), config=config)
    delivered = result.get("current_stage") == "delivered"
    return PlanRunResponse(
        thread_id=thread_id,
        status=PlanStatus.DELIVERED if delivered else PlanStatus.DRAFT,
        current_stage=result.get("current_stage", "unknown"),
        message="交付包已生成。" if delivered else "方案已驳回并保留审核意见。",
        data=jsonable_encoder(result),
    )
