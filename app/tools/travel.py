from __future__ import annotations

import asyncio
import hashlib
import logging

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.schemas import Quote, QuoteItem, ResourceCandidate
from app.services.mcp_client import MCPToolError
from app.services.tavily_mcp import TavilyMCPService, TavilySearchItem
from app.services.tools.amap import AmapClient
from app.tools.registry import ToolRisk, tool_registry

logger = logging.getLogger(__name__)


class AmapPOISearchInput(BaseModel):
    keywords: str
    city: str = ""
    types: str = ""
    limit: int = Field(default=10, ge=1, le=25)


@tool_registry.register(
    name="search_poi_amap",
    description="Search structured POI data via Gaode Maps (coordinates, hours, rating).",
    category="geo_search",
    risk_level=ToolRisk.READ_ONLY,
    input_model=AmapPOISearchInput,
)
async def search_poi_amap(payload: AmapPOISearchInput) -> list[ResourceCandidate]:
    settings = get_settings()
    if not settings.amap_api_key:
        return []
    client = AmapClient(settings.amap_api_key, settings.amap_base_url)
    places = await client.search_poi(payload.keywords, city=payload.city, types=payload.types, limit=payload.limit)
    return [_place_to_resource(p) for p in places]


def _place_to_resource(place) -> ResourceCandidate:
    from app.services.tools.base import Place
    assert isinstance(place, Place)
    return ResourceCandidate(
        id=f"amap-{place.place_id}",
        name=place.name,
        category=place.categories[0] if place.categories else "poi",
        location=place.address or place.summary or "",
        price_per_person=place.price,
        recommended_minutes=place.estimated_duration_minutes,
        opening_hours=place.opening_hours_text.value if place.opening_hours_text else "未知",
        audience_tags=place.categories[:3],
        evidence=f"Gaode POI / {place.place_id}",
        score=min(1.0, place.rating / 5.0) if place.rating else 0.5,
        provider="amap",
        summary=place.summary,
        lng=place.coordinates.lng if place.coordinates.lng else None,
        lat=place.coordinates.lat if place.coordinates.lat else None,
    )


class WeatherForecastInput(BaseModel):
    city: str = Field(description="城市名称或 lng,lat 坐标")
    days: int = Field(default=3, ge=1, le=7)


@tool_registry.register(
    name="get_weather_forecast",
    description="Get multi-day weather forecast for travel planning via QWeather.",
    category="geo_search",
    risk_level=ToolRisk.READ_ONLY,
    input_model=WeatherForecastInput,
)
async def get_weather_forecast(payload: WeatherForecastInput) -> list[dict]:
    settings = get_settings()
    if not settings.weather_api_key:
        return []
    from app.services.weather import WeatherClient

    client = WeatherClient(settings.weather_api_key, settings.weather_base_url)
    return await client.get_forecast(payload.city, days=payload.days)


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
        timeout_seconds=settings.tavily_search_timeout_seconds,
        search_depth=settings.tavily_search_depth,
    )
    query_parts = [payload.destination]
    if payload.themes:
        query_parts.extend(payload.themes[:3])
    if payload.audience:
        query_parts.append(payload.audience)
    query_parts.append("旅游攻略 景点推荐")
    query = " ".join(query_parts)

    items: list[TavilySearchItem] = await service.search(query, max_results=payload.limit)

    resources: list[ResourceCandidate] = []
    for item in items:
        resource_id = f"tavily-{hashlib.md5(item.url.encode()).hexdigest()[:10]}"
        resources.append(ResourceCandidate(
            id=resource_id,
            name=item.title[:80],
            category="search_result",
            location=payload.destination,
            recommended_minutes=120,
            opening_hours="需确认",
            evidence=f"Tavily: {item.url}",
            score=min(1.0, (item.score or 0.5)),
            source_url=item.url,
            source_title=item.title,
            provider="tavily_mcp",
            summary=item.content[:500] if item.content else "",
            images=item.images[:3] if item.images else [],
        ))
    return resources



# ------------------------------------------------------------------
# Route matrix — now calls Amap for real travel times
# ------------------------------------------------------------------

class RouteMatrixInput(BaseModel):
    resource_ids: list[str]
    coordinates: dict[str, str] = Field(
        default_factory=dict,
        description="资源坐标映射 'id': 'lng,lat'，有坐标时调用高德 API",
    )
    travel_times: dict[str, int] = Field(
        default_factory=dict,
        description="已有的交通时间（LLM 估算），作为 fallback",
    )
    city: str = Field(default="", description="城市名，用于公交规划")
    mode: str = Field(default="transit", description="transit/driving/walking")


@tool_registry.register(
    name="calculate_route_matrix",
    description="Build travel-time matrix using Amap API when coordinates available, LLM estimates as fallback.",
    category="geo_compute",
    risk_level=ToolRisk.READ_ONLY,
    input_model=RouteMatrixInput,
)
async def calculate_route_matrix(payload: RouteMatrixInput) -> dict[str, int]:
    settings = get_settings()
    matrix: dict[str, int] = {}

    pairs_to_fetch: list[tuple[str, str]] = []
    for source in payload.resource_ids:
        for target in payload.resource_ids:
            if source == target:
                continue
            key = f"{source}->{target}"
            if key in payload.travel_times:
                matrix[key] = payload.travel_times[key]
            elif (
                settings.amap_api_key
                and source in payload.coordinates
                and target in payload.coordinates
            ):
                pairs_to_fetch.append((source, target))
            else:
                matrix[key] = 30

    if pairs_to_fetch and settings.amap_api_key:
        client = AmapClient(settings.amap_api_key, settings.amap_base_url)

        async def _fetch(src: str, tgt: str) -> tuple[str, int]:
            key = f"{src}->{tgt}"
            try:
                minutes = await client.travel_time(
                    payload.coordinates[src],
                    payload.coordinates[tgt],
                    mode=payload.mode,
                    city=payload.city,
                )
                return key, minutes
            except Exception:  # noqa: BLE001 - Amap failures fall back to estimates
                logger.warning("Amap travel_time failed for %s", key)
                return key, payload.travel_times.get(key, 30)

        tasks = [_fetch(src, tgt) for src, tgt in pairs_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, tuple):
                matrix[result[0]] = result[1]

    return matrix


class OptimizeRouteInput(BaseModel):
    resource_ids: list[str]
    distance_matrix: dict[str, int] = Field(
        description="真实交通时间矩阵，格式 'id->id': minutes",
    )
    days: int = Field(ge=1, le=15)


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


class ApprovalInput(BaseModel):
    plan_id: str
    reviewer_id: str
    approved: bool
    comment: str | None = None


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
        {"reviewer_id": payload.reviewer_id, "approved": payload.approved, "comment": payload.comment},
    )
    return {
        "plan_id": payload.plan_id,
        "reviewer_id": payload.reviewer_id,
        "approved": payload.approved,
        "comment": payload.comment,
    }
