from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import APIRouter
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
    ToolSummary,
)
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()
graph = build_planning_graph()
chat_graph = build_chat_graph()
settings = get_settings()


@router.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
        "mock_model_mode": settings.mock_model_mode,
        "tavily_mcp_enabled": settings.tavily_search_enabled,
        "tavily_mcp_configured": bool(
            settings.tavily_api_key
            and settings.tavily_api_key.get_secret_value().strip()
        ),
        "weather_configured": bool(settings.weather_api_key),
    }


@router.get("/tools", response_model=list[ToolSummary])
def list_tools() -> list[ToolSummary]:
    return [
        ToolSummary(
            name=tool.name,
            description=tool.description,
            risk_level=tool.risk_level.value,
            category=tool.category,
        )
        for tool in tool_registry.list()
    ]


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
    plan_request: dict | None = None


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatMessage) -> ChatResponse:
    thread_id = body.thread_id or uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    state = await chat_graph.aget_state(config)
    existing_messages = state.values.get("messages", []) if state and state.values else []
    messages = [*existing_messages, {"role": "user", "content": body.message}]

    result = await chat_graph.ainvoke(
        {"thread_id": thread_id, "messages": messages},
        config=config,
    )

    conversation = result.get("conversation")
    ready = result.get("ready", False)
    reply = ""
    if conversation:
        if ready:
            reply = "好的，信息足够了，我来为你生成方案！"
        else:
            reply = conversation.question or "请告诉我更多细节。"

    return ChatResponse(
        thread_id=thread_id,
        reply=reply,
        ready=ready,
        plan_request=result.get("plan_request"),
    )


# ------------------------------------------------------------------
# Plan execution
# ------------------------------------------------------------------

@router.post("/plans/run", response_model=PlanRunResponse)
async def run_plan(request: PlanRequest) -> PlanRunResponse:
    thread_id = uuid4().hex
    plan_id = f"PLAN-{thread_id[:8].upper()}"
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(
        {
            "thread_id": thread_id,
            "plan_id": plan_id,
            "request": request,
            "current_stage": "created",
        },
        config=config,
    )
    if result.get("current_stage") == "failed":
        return PlanRunResponse(
            thread_id=thread_id,
            status=PlanStatus.FAILED,
            current_stage="failed",
            message="方案未通过校验，执行失败。",
            data=jsonable_encoder(result),
        )
    return PlanRunResponse(
        thread_id=thread_id,
        status=PlanStatus.WAITING_APPROVAL,
        current_stage="waiting_approval",
        message="方案已通过自动审核，等待人工审批。",
        data=jsonable_encoder(result),
    )


# ------------------------------------------------------------------
# SSE streaming plan execution
# ------------------------------------------------------------------

@router.post("/plans/run/stream")
async def run_plan_stream(request: PlanRequest) -> StreamingResponse:
    thread_id = uuid4().hex
    plan_id = f"PLAN-{thread_id[:8].upper()}"
    config = {"configurable": {"thread_id": thread_id}}

    async def event_generator():
        yield _sse_event("started", {"thread_id": thread_id, "plan_id": plan_id})

        try:
            async for event in graph.astream_events(
                {
                    "thread_id": thread_id,
                    "plan_id": plan_id,
                    "request": request,
                    "current_stage": "created",
                },
                config=config,
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_chain_start":
                    node_name = event.get("name", "")
                    if node_name and node_name not in ("LangGraph", "__start__"):
                        yield _sse_event("node_start", {"node": node_name})
                elif kind == "on_chain_end":
                    node_name = event.get("name", "")
                    if node_name and node_name not in ("LangGraph", "__start__"):
                        yield _sse_event("node_end", {"node": node_name})

            state = await graph.aget_state(config)
            final_stage = state.values.get("current_stage", "unknown") if state else "unknown"
            yield _sse_event("completed", {
                "thread_id": thread_id,
                "plan_id": plan_id,
                "current_stage": final_stage,
            })
        except Exception as exc:
            logger.exception("Stream plan execution failed")
            yield _sse_event("error", {"message": str(exc)})

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
