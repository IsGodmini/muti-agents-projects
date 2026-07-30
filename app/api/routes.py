from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from langgraph.types import Command

from app.agents.graph import build_planning_graph
from app.config import get_settings
from app.models.schemas import (
    ApprovalDecision,
    PlanRequest,
    PlanRunResponse,
    PlanStatus,
    ToolSummary,
)

# Ensure domain tools are registered.
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import tool_registry

router = APIRouter()
graph = build_planning_graph()
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
    return PlanRunResponse(
        thread_id=thread_id,
        status=PlanStatus.WAITING_APPROVAL,
        current_stage="waiting_approval",
        message="方案已通过自动审核，等待人工审批。",
        data=jsonable_encoder(result),
    )


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
