"""WeatherClient 单元测试（GeoAPI 城市解析 + v7 每日预报）。"""
from __future__ import annotations

import httpx
import pytest

from app.services.weather import WeatherClient

GEO_OK = {
    "code": "200",
    "location": [
        {"name": "杭州", "id": "101210101", "adm1": "浙江省", "country": "中国"},
    ],
}
WEATHER_OK = {
    "code": "200",
    "daily": [
        {"fxDate": "2026-08-01", "textDay": "晴", "tempMax": "32", "tempMin": "25",
         "windScaleDay": "3-4", "humidity": "60"},
    ],
}


def _make(handler) -> WeatherClient:
    transport = httpx.MockTransport(handler)
    return WeatherClient(api_key="test-key", base_url="https://xxx.re.qweatherapi.com/v7", transport=transport)


@pytest.mark.asyncio
async def test_city_name_resolved_via_geoapi() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/geo/v2/city/lookup" in request.url.path:
            return httpx.Response(200, json=GEO_OK)
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    result = await client.get_forecast("杭州", days=3)

    assert len(seen) == 2
    assert "/geo/v2/city/lookup" in seen[0]
    assert "range=cn" in seen[0]
    assert "/v7/weather/3d" in seen[1]
    assert "location=101210101" in seen[1]
    assert result[0]["date"] == "2026-08-01"
    assert result[0]["text_day"] == "晴"


@pytest.mark.asyncio
async def test_days_mapped_to_supported_lengths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    await client.get_forecast("116.41,39.92", days=5)   # 5 天行程 → 7d
    await client.get_forecast("116.41,39.92", days=1)   # 1 天行程 → 3d
    await client.get_forecast("116.41,39.92", days=7)   # 7 天行程 → 7d

    assert paths == [
        "/v7/weather/7d",
        "/v7/weather/3d",
        "/v7/weather/7d",
    ]


@pytest.mark.asyncio
async def test_coordinate_skips_geoapi() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=WEATHER_OK)

    client = _make(handler)
    await client.get_forecast("120.1552,30.2741", days=3)
    assert paths == ["/v7/weather/3d"]


@pytest.mark.asyncio
async def test_no_location_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "200", "location": []})

    client = _make(handler)
    with pytest.raises(RuntimeError, match="未找到城市"):
        await client.get_forecast("不存在的地方xyz", days=3)


@pytest.mark.asyncio
async def test_weather_error_code_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/geo/v2/city/lookup" in request.url.path:
            return httpx.Response(200, json=GEO_OK)
        return httpx.Response(200, json={"code": "403", "refer": {"sources": []}})

    client = _make(handler)
    with pytest.raises(RuntimeError, match="code=403"):
        await client.get_forecast("杭州", days=3)
