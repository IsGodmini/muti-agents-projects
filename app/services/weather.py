"""Weather API client (QWeather / HeFeng).

Provides multi-day forecast for travel planning.
Docs: https://dev.qweather.com/docs/api/

使用两个和风天气服务：
- GeoAPI v2 城市搜索（/geo/v2/city/lookup）：把城市名解析为经纬度
- 每日天气预报 v1（/weather/v1/daily/{lat}/{lng}）：获取 1-10 天逐日预报
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def _round_temp(value: object, default: int = 25) -> int:
    """从 v1 响应中提取温度数值（temperatureMax 等为 {value, unit} 对象）。"""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (int, float)):
        return round(value)
    return default


class WeatherClient:
    """QWeather REST API client (v1 daily forecast + GeoAPI v2 city lookup)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://devapi.qweather.com/v7",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """transport 仅供测试注入 MockTransport。"""
        self.api_key = api_key
        self._transport = transport
        # base_url 允许带 /v7 后缀（历史配置），统一规整为 Host 根地址
        root = base_url.rstrip("/")
        root = root.removesuffix("/v7")
        self.host_root = root.rstrip("/")

    def _api_url(self, path: str) -> str:
        return f"{self.host_root}{path}"

    async def _get(self, url: str, params: dict[str, str]) -> dict:
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def resolve_coordinates(self, city: str) -> tuple[str, str]:
        """通过 GeoAPI v2 城市搜索把城市名解析为 (纬度, 经度)。

        如果传入的已经是 lng,lat 坐标则直接解析返回。
        """
        city = city.strip()
        if "," in city:
            lng, lat = (part.strip() for part in city.split(",", 1))
            return lat, lng

        async def lookup(extra_params: dict[str, str]) -> list[dict]:
            params = {
                "location": city,
                "number": "5",
                "key": self.api_key,
                **extra_params,
            }
            data = await self._get(self._api_url("/geo/v2/city/lookup"), params)
            if data.get("code") != "200":
                raise RuntimeError(f"QWeather GeoAPI error: code={data.get('code')}")
            return data.get("location") or []

        # 优先在国内范围搜索，找不到再放宽到全球（支持境外目的地）
        locations = await lookup({"range": "cn"})
        if not locations:
            locations = await lookup({})
        if not locations:
            raise RuntimeError(f"QWeather GeoAPI: 未找到城市 {city!r} 的位置信息")

        best = locations[0]
        logger.info(
            "GeoAPI: %s -> lat=%s lon=%s (%s)",
            city, best.get("lat"), best.get("lon"), best.get("name"),
        )
        return str(best["lat"]), str(best["lon"])

    async def get_forecast(self, location: str, days: int = 3) -> list[dict]:
        """Get a multi-day forecast for a city or `lng,lat` location.

        Returns a list of daily dicts with keys:
        date, text_day, temp_max, temp_min, wind_scale_day, humidity.
        """
        lat, lng = await self.resolve_coordinates(location)
        forecast_days = max(1, min(int(days), 10))

        params = {
            "days": str(forecast_days),
            "localTime": "true",
            "lang": "zh",
            "key": self.api_key,
        }
        data = await self._get(
            self._api_url(f"/weather/v1/daily/{lat}/{lng}"),
            params,
        )

        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"Weather API error: {err.get('status')} {err.get('title')}: {err.get('detail')}"
            )

        forecasts: list[dict] = []
        for day in data.get("days", []):
            daytime = day.get("daytime") or {}
            humidity = daytime.get("humidity")
            forecasts.append({
                "date": (day.get("forecastStartTime") or "")[:10],
                "text_day": (daytime.get("condition") or {}).get("text", "未知"),
                "temp_max": _round_temp(day.get("temperatureMax")),
                "temp_min": _round_temp(day.get("temperatureMin"), default=15),
                "wind_scale_day": str((daytime.get("wind") or {}).get("scale", "") or "-"),
                "humidity": round(humidity * 100) if isinstance(humidity, (int, float)) else 60,
            })
        return forecasts
