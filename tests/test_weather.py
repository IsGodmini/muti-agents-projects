"""WeatherClient 单元测试（GeoAPI 城市解析 + v1 每日预报）。"""
from __future__ import annotations

import httpx
import pytest

from app.services.weather import WeatherClient

GEO_OK = {
    "code": "200",
    "location": [
        {"name": "杭州", "id": "101210101", "lat": "30.2741", "lon": "120.1552",
         "adm1": "浙江省", "country": "中国"},
    ],
}
WEATHER_OK = {
    "metadata": {"tag": "t"},
    "days": [
        {
            "forecastStartTime": "2026-08-01T00:00+08:00",
            "temperatureMax": {"value": 32.4, "unit": "°C"},
            "temperatureMin": {"value": 24.8, "unit": "°C"},
            "daytime": {
                "condition": {"text": "晴", "code": "100"},
                "wind": {"scale": 2},
                "humidity": 0.6,
            },
        },
    ],
}


def _make(handler) -> WeatherClient:
    transport = httpx.MockTransport(handler)
    return WeatherClient(
        api_key="test-key",
        base_url="https://xxx.re.qweatherapi.com/v7",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_city_name_resolved_via_geoapi() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["X-QW-Api-Key"] == "test-key"
        if "/geo/v2/city/lookup" in request.url.path:
            return httpx.Response(200, json=GEO_OK)
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    result = await client.get_forecast("杭州", days=3)

    assert len(seen) == 2
    assert "/geo/v2/city/lookup" in seen[0]
    assert "range=cn" in seen[0]
    # v1 每日预报使用经纬度路径参数
    assert "/weather/v1/daily/30.2741/120.1552" in seen[1]
    assert "days=3" in seen[1]
    assert "localTime=true" in seen[1]
    assert "lang=zh" in seen[1]
    assert result[0]["date"] == "2026-08-01"
    assert result[0]["text_day"] == "晴"
    assert result[0]["temp_max"] == 32
    assert result[0]["temp_min"] == 25
    assert result[0]["wind_scale_day"] == "2"
    assert result[0]["humidity"] == 60
    assert result[0]["provider"] == "qweather"


@pytest.mark.asyncio
async def test_days_passed_through_1_to_10() -> None:
    query_days: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query_days.append(request.url.params.get("days", ""))
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    await client.get_forecast("116.41,39.92", days=5)
    await client.get_forecast("116.41,39.92", days=1)
    await client.get_forecast("116.41,39.92", days=10)

    assert query_days == ["5", "1", "10"]


@pytest.mark.asyncio
async def test_coordinate_skips_geoapi() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    await client.get_forecast("120.1552,30.2741", days=3)
    assert paths == ["/weather/v1/daily/30.2741/120.1552"]


@pytest.mark.asyncio
async def test_no_location_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "200", "location": []})

    client = _make(handler)
    with pytest.raises(RuntimeError, match="未找到城市"):
        await client.get_forecast("不存在的地方xyz", days=3)


@pytest.mark.asyncio
async def test_weather_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/geo/v2/city/lookup" in request.url.path:
            return httpx.Response(200, json=GEO_OK)
        return httpx.Response(200, json={"error": {"status": 403, "title": "Invalid Host"}})

    client = _make(handler)
    with pytest.raises(RuntimeError, match="Invalid Host"):
        await client.get_forecast("杭州", days=3)
