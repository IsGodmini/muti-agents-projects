from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.services.mcp_client import MCPToolError
from app.services.tavily_mcp import TavilySearchItem, parse_tavily_search_text
from app.services.tools.amap import AmapAPIError, AmapClient
from app.tools import travel
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import ToolRisk, tool_registry


def test_registered_tools_have_schemas_and_risk_levels() -> None:
    tools = tool_registry.list()
    assert len(tools) >= 6
    assert all(tool.input_model.model_json_schema() for tool in tools)
    assert {tool.risk_level for tool in tools} >= {
        ToolRisk.READ_ONLY,
        ToolRisk.WRITE_INTERNAL,
        ToolRisk.EXTERNAL_ACTION,
    }


def test_quote_tool_computes_from_provided_items() -> None:
    result = tool_registry.invoke(
        "calculate_product_cost",
        {
            "group_size": 30,
            "target_margin_rate": 0.15,
            "budget_per_person": 1800,
            "cost_items": [
                {"category": "交通", "description": "大巴", "amount": 8000},
                {"category": "住宿", "description": "2晚", "amount": 15000},
                {"category": "餐饮", "description": "团餐", "amount": 6000},
                {"category": "门票", "description": "场馆", "amount": 5000},
                {"category": "服务", "description": "领队", "amount": 3000},
            ],
        },
    )
    assert result.total_cost == 37000
    assert result.cost_per_person > 0
    assert result.sale_price_per_person <= 1800
    assert result.margin_rate > 0


def test_quote_tool_rejects_zero_cost_items() -> None:
    with pytest.raises(ValidationError):
        tool_registry.invoke(
            "calculate_product_cost",
            {
                "group_size": 5,
                "target_margin_rate": 0.15,
                "budget_per_person": 4000,
                "cost_items": [{"category": "交通", "description": "车辆", "amount": 0}],
            },
        )


@pytest.mark.parametrize("resource_ids", [[], ["only-resource"]])
def test_optimizer_preserves_requested_days_with_few_resources(
    resource_ids: list[str],
) -> None:
    result = travel.optimize_itinerary(
        travel.OptimizeRouteInput(
            resource_ids=resource_ids,
            distance_matrix={},
            days=4,
        )
    )

    assert len(result) == 4
    assert [resource for day in result for resource in day] == resource_ids


@pytest.mark.asyncio
async def test_route_matrix_uses_provided_travel_times() -> None:
    result = await tool_registry.ainvoke(
        "calculate_route_matrix",
        {
            "resource_ids": ["a", "b", "c"],
            "travel_times": {"a->b": 25, "b->a": 25, "a->c": 40, "c->a": 40, "b->c": 15, "c->b": 15},
        },
    )
    assert result["a->b"] == 25
    assert result["b->c"] == 15
    assert result["a->c"] == 40


def test_tavily_mcp_text_results_are_parsed_with_sources() -> None:
    payload = """Detailed Results:

Title: 成都武侯祠博物馆参观信息
URL: https://example.gov.cn/wuhou
Content: 包含开放时间、地址和参观须知。

Title: 成都博物馆开放公告
URL: https://example.gov.cn/chengdu-museum
Content: 参观需要提前预约。"""

    results = parse_tavily_search_text(payload)

    assert [item.title for item in results] == [
        "成都武侯祠博物馆参观信息",
        "成都博物馆开放公告",
    ]
    assert results[0].url == "https://example.gov.cn/wuhou"
    assert "开放时间" in results[0].content


@pytest.mark.asyncio
async def test_search_attractions_uses_tavily_mcp_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTavilyMCPService:
        def __init__(self, **_: object) -> None:
            pass

        async def search(self, query: str, max_results: int) -> list[TavilySearchItem]:
            assert "成都" in query
            return [
                TavilySearchItem(
                    title="成都武侯祠官方参观指南",
                    url="https://example.gov.cn/wuhou",
                    content="开放时间和预约规则以官方公告为准。",
                    score=0.93,
                    retrieved_at=datetime.now(UTC),
                )
            ]

    monkeypatch.setattr(
        travel,
        "get_settings",
        lambda: SimpleNamespace(
            tavily_search_enabled=True,
            tavily_api_key=SecretStr("test-key"),
            tavily_mcp_url="https://mcp.tavily.com/mcp",
            tavily_search_depth="advanced",
            tavily_search_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(travel, "TavilyMCPService", FakeTavilyMCPService)

    result = await tool_registry.ainvoke(
        "search_attractions",
        {"destination": "成都", "themes": ["人文"], "audience": "亲子家庭", "limit": 5},
    )

    assert result[0].provider == "tavily_mcp"
    assert result[0].source_url == "https://example.gov.cn/wuhou"


@pytest.mark.asyncio
async def test_search_attractions_raises_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        travel,
        "get_settings",
        lambda: SimpleNamespace(
            tavily_search_enabled=True,
            tavily_api_key=SecretStr(""),
        ),
    )

    with pytest.raises(MCPToolError, match="TAVILY_API_KEY"):
        await tool_registry.ainvoke(
            "search_attractions",
            {"destination": "杭州", "themes": ["自然教育"], "audience": "亲子", "limit": 5},
        )


@pytest.mark.asyncio
async def test_weather_forecast_raises_without_key(monkeypatch) -> None:
    monkeypatch.setattr(
        travel,
        "get_settings",
        lambda: SimpleNamespace(weather_api_key=""),
    )

    with pytest.raises(MCPToolError, match="WEATHER_API_KEY"):
        await tool_registry.ainvoke("get_weather_forecast", {"city": "杭州", "days": 3})


@pytest.mark.asyncio
async def test_weather_forecast_calls_weather_client(monkeypatch) -> None:
    class FakeWeatherClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def get_forecast(self, location: str, days: int) -> list[dict]:
            assert location == "杭州"
            assert days == 3
            return [{"date": "2026-08-01", "text_day": "晴", "temp_max": 32, "temp_min": 25}]

    monkeypatch.setattr(
        travel,
        "get_settings",
        lambda: SimpleNamespace(
            weather_api_key="test-key",
            weather_base_url="https://xxx.re.qweatherapi.com/v7",
            weather_geo_base_url="",
        ),
    )
    from app.services import weather as weather_module

    monkeypatch.setattr(weather_module, "WeatherClient", FakeWeatherClient)

    result = await tool_registry.ainvoke("get_weather_forecast", {"city": "杭州", "days": 3})
    assert result[0]["text_day"] == "晴"


@pytest.mark.asyncio
async def test_weather_forecast_falls_back_to_amap(monkeypatch) -> None:
    class FailingWeatherClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def get_forecast(self, location: str, days: int) -> list[dict]:
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("GET", "https://weather.example"),
                response=httpx.Response(
                    403, request=httpx.Request("GET", "https://weather.example")
                ),
            )

    class FakeAmapClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def weather_forecast(self, city: str, days: int) -> list[dict]:
            assert city == "杭州"
            assert days == 3
            return [{"date": "2026-08-01", "text_day": "小雨", "provider": "amap"}]

    monkeypatch.setattr(
        travel,
        "get_settings",
        lambda: SimpleNamespace(
            weather_api_key="invalid",
            weather_base_url="https://weather.example",
            weather_geo_base_url="",
            amap_api_key="amap-key",
            amap_base_url="https://restapi.amap.com/v3",
        ),
    )
    from app.services import weather as weather_module

    monkeypatch.setattr(weather_module, "WeatherClient", FailingWeatherClient)
    monkeypatch.setattr(travel, "AmapClient", FakeAmapClient)

    result = await tool_registry.ainvoke("get_weather_forecast", {"city": "杭州", "days": 3})
    assert result[0]["provider"] == "amap"


def test_async_tool_requires_ainvoke() -> None:
    with pytest.raises(RuntimeError, match="asynchronous"):
        tool_registry.invoke(
            "search_attractions",
            {"destination": "杭州", "themes": [], "audience": "", "limit": 5},
        )


@pytest.mark.asyncio
async def test_amap_application_error_is_not_returned_as_default_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
        )

    client = AmapClient("test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(AmapAPIError, match="10021"):
        await client.travel_time("120.1,30.1", "120.2,30.2")


@pytest.mark.asyncio
async def test_amap_poi_keeps_an_official_traceable_source_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [{
                    "id": "B023B13L9M",
                    "name": "杭州西湖风景名胜区",
                    "location": "120.148,30.242",
                    "type": "风景名胜",
                    "address": "西湖区",
                    "biz_ext": {"rating": "4.9", "cost": "0", "open_time": "00:00-24:00"},
                }],
            },
        )

    client = AmapClient("test-key", transport=httpx.MockTransport(handler))
    places = await client.search_poi("西湖", city="杭州", limit=1)
    resource = travel._place_to_resource(places[0])

    assert resource.source_url is not None
    assert resource.source_url.startswith("https://uri.amap.com/marker?")
    assert "poiid=B023B13L9M" in resource.source_url
