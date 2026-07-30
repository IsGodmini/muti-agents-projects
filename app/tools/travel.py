from __future__ import annotations

import hashlib
import logging

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.schemas import Quote, QuoteItem, ResourceCandidate
from app.services.mcp_client import MCPToolError
from app.services.tavily_mcp import TavilyMCPService, TavilySearchItem
from app.tools.registry import ToolRisk, tool_registry

logger = logging.getLogger(__name__)


class SearchResourcesInput(BaseModel):
    destination: str
    themes: list[str] = Field(default_factory=list)
    audience: str = ""
    limit: int = Field(default=8, ge=1, le=30)


@tool_registry.register(
    name="search_attractions",
    description="Search destination resources through Tavily MCP with source evidence.",
    category="mcp_search",
    risk_level=ToolRisk.READ_ONLY,
    input_model=SearchResourcesInput,
)
async def search_attractions(payload: SearchResourcesInput) -> list[ResourceCandidate]:
    settings = get_settings()
    api_key = (
        settings.tavily_api_key.get_secret_value().strip()
        if settings.tavily_api_key is not None
        else ""
    )
    if not settings.tavily_search_enabled or not api_key:
        raise MCPToolError(
            "Tavily MCP search requires TAVILY_SEARCH_ENABLED=true and a valid TAVILY_API_KEY"
        )

    service = TavilyMCPService(
        server_url=settings.tavily_mcp_url,
        api_key=api_key,
        search_depth=settings.tavily_search_depth,
        timeout_seconds=settings.tavily_search_timeout_seconds,
    )
    themes = "、".join(payload.themes) or "当地文化与代表性体验"
    query = (
        f"{payload.destination} 旅游景点 活动 官方信息 开放时间 门票 地址；"
        f"主题：{themes}；适合人群：{payload.audience or '普通游客'}"
    )
    results = await service.search(query, max_results=payload.limit)
    return [
        _web_result_to_resource(
            result,
            destination=payload.destination,
            themes=payload.themes,
            audience=payload.audience,
        )
        for result in results
    ]


def _web_result_to_resource(
    result: TavilySearchItem,
    *,
    destination: str,
    themes: list[str],
    audience: str,
) -> ResourceCandidate:
    resource_id = hashlib.sha256(result.url.encode("utf-8")).hexdigest()[:12]
    audience_tags = list(dict.fromkeys([*themes, audience] if audience else themes))
    return ResourceCandidate(
        id=f"web-{resource_id}",
        name=result.title[:120],
        category="web_resource",
        location=destination,
        price_per_person=0,
        recommended_minutes=120,
        opening_hours="需在官方渠道二次确认",
        audience_tags=audience_tags,
        evidence=(
            f"Tavily MCP / {result.url} / "
            f"{result.retrieved_at.date().isoformat()}"
        ),
        score=result.score,
        source_url=result.url,
        source_title=result.title,
        retrieved_at=result.retrieved_at,
        provider="tavily_mcp",
        summary=result.content[:300],
    )


class RouteMatrixInput(BaseModel):
    resource_ids: list[str]
    travel_times: dict[str, int] = Field(
        description="LLM 估算的交通时间，格式 'id->id': minutes",
    )


@tool_registry.register(
    name="calculate_route_matrix",
    description="Validate and normalize a travel-time matrix estimated by the LLM.",
    category="geo_compute",
    risk_level=ToolRisk.READ_ONLY,
    input_model=RouteMatrixInput,
)
def calculate_route_matrix(payload: RouteMatrixInput) -> dict[str, int]:
    matrix: dict[str, int] = {}
    for source in payload.resource_ids:
        for target in payload.resource_ids:
            if source != target:
                key = f"{source}->{target}"
                matrix[key] = payload.travel_times.get(key, 30)
    return matrix


class OptimizeRouteInput(BaseModel):
    resource_ids: list[str]
    distance_matrix: dict[str, int] = Field(
        description="真实交通时间矩阵，格式 'id->id': minutes",
    )
    days: int = Field(ge=1, le=15)
    max_daily_minutes: int = Field(default=480, ge=120, le=720)


@tool_registry.register(
    name="optimize_itinerary",
    description="Produce a stable visit order using OR-Tools with real travel times.",
    category="optimization",
    risk_level=ToolRisk.WRITE_INTERNAL,
    input_model=OptimizeRouteInput,
)
def optimize_itinerary(payload: OptimizeRouteInput) -> list[list[str]]:
    resource_count = len(payload.resource_ids)
    if resource_count <= 1:
        return [payload.resource_ids]


    manager = pywrapcp.RoutingIndexManager(resource_count, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index: int, to_index: int) -> int:
        source = manager.IndexToNode(from_index)
        target = manager.IndexToNode(to_index)
        source_id = payload.resource_ids[source]
        target_id = payload.resource_ids[target]
        return payload.distance_matrix.get(f"{source_id}->{target_id}", 30)

    transit_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        raise RuntimeError("OR-Tools could not produce a route")

    ordered: list[str] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        ordered.append(payload.resource_ids[manager.IndexToNode(index)])
        index = solution.Value(routing.NextVar(index))

    base_size, remainder = divmod(len(ordered), payload.days)
    days: list[list[str]] = []
    cursor = 0
    for day_index in range(payload.days):
        day_size = base_size + (1 if day_index < remainder else 0)
        days.append(ordered[cursor : cursor + day_size])
        cursor += day_size
    return days


class QuoteInput(BaseModel):
    group_size: int = Field(ge=1, le=500)
    days: int = Field(ge=1, le=15)
    target_margin_rate: float = Field(ge=0, le=0.8)
    budget_per_person: int = Field(ge=100)
    cost_items: list[QuoteItem] = Field(
        description="LLM 估算的成本明细",
    )


@tool_registry.register(
    name="calculate_product_cost",
    description="Compute sale price and margin from LLM-estimated cost items.",
    category="pricing",
    risk_level=ToolRisk.READ_ONLY,
    input_model=QuoteInput,
)
def calculate_product_cost(payload: QuoteInput) -> Quote:
    from math import ceil

    total_cost = sum(item.amount for item in payload.cost_items)
    cost_per_person = ceil(total_cost / payload.group_size)
    calculated_sale = ceil(cost_per_person / (1 - payload.target_margin_rate))
    sale_price = min(calculated_sale, payload.budget_per_person)
    revenue = sale_price * payload.group_size
    profit = revenue - total_cost
    return Quote(
        items=payload.cost_items,
        total_cost=total_cost,
        cost_per_person=cost_per_person,
        sale_price_per_person=sale_price,
        expected_revenue=revenue,
        expected_profit=profit,
        margin_rate=round(profit / revenue, 4) if revenue else 0,
    )


class SaveVersionInput(BaseModel):
    plan_id: str
    reason: str


@tool_registry.register(
    name="save_plan_version",
    description="Persist a new immutable plan version to disk.",
    category="versioning",
    risk_level=ToolRisk.WRITE_INTERNAL,
    input_model=SaveVersionInput,
)
def save_plan_version(payload: SaveVersionInput) -> dict[str, str]:
    from app.services.plan_store import plan_store

    return plan_store.save_version(payload.plan_id, payload.reason, {})


class ApprovalInput(BaseModel):
    plan_id: str
    reviewer_id: str
    approved: bool


@tool_registry.register(
    name="submit_for_approval",
    description="Persist a plan approval decision to disk.",
    category="approval",
    risk_level=ToolRisk.EXTERNAL_ACTION,
    input_model=ApprovalInput,
)
def submit_for_approval(payload: ApprovalInput) -> dict[str, str | bool]:
    from app.services.plan_store import plan_store

    plan_store.save_approval(
        payload.plan_id,
        {"reviewer_id": payload.reviewer_id, "approved": payload.approved},
    )
    return {
        "plan_id": payload.plan_id,
        "reviewer_id": payload.reviewer_id,
        "approved": payload.approved,
    }
