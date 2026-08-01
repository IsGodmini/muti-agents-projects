"""Weather API client (QWeather / HeFeng).

Provides multi-day forecast for travel planning.
Docs: https://dev.qweather.com/docs/api/

使用两个和风天气服务：
- GeoAPI v2 城市搜索（/geo/v2/city/lookup）：把城市名解析为 LocationID
- 天气 v7 每日预报（/v7/weather/{days}d）：获取 3/7 天逐日预报
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# v7 每日预报仅支持这些预报长度（3d/7d/10d/15d/30d）
SUPPORTED_DAYS = (3, 7, 10, 15, 30)


class WeatherClient:
    """QWeather REST API client (v7 weather + GeoAPI v2 city lookup)."""

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

    @staticmethod
    def _looks_like_location_id(value: str) -> bool:
        """LocationID 形如 101010100（纯数字）。"""
        return value.strip().isdigit()

    @staticmethod
    def _looks_like_coordinate(value: str) -> bool:
        """经纬度坐标形如 116.41,39.92。"""
        return "," in value

    async def resolve_location_id(self, city: str) -> str:
        """通过 GeoAPI v2 城市搜索把城市名解析为 LocationID。

        如果传入的已经是 LocationID 或 lng,lat 坐标则原样返回。
        """
        city = city.strip()
        if self._looks_like_location_id(city) or self._looks_like_coordinate(city):
            return city

        async def lookup(extra_params: dict[str, str]) -> list[dict]:
            params = {
                "location": city,
                "number": "5",
                "key": self.api_key,
                **extra_params,
            }
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.get(
                    self._api_url("/geo/v2/city/lookup"),
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
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
            "GeoAPI: %s -> LocationID=%s (%s/%s)",
            city, best.get("id"), best.get("name"), best.get("adm1"),
        )
        return best["id"]

    async def get_forecast(self, location: str, days: int = 3) -> list[dict]:
        """Get a multi-day forecast for a city or `lng,lat` location.

        Returns a list of daily dicts with keys:
        date, text_day, temp_max, temp_min, wind_scale_day, humidity.
        """
        location_id = await self.resolve_location_id(location)

        # v7 每日预报只支持 3d/7d/10d/15d/30d，这里按需向上取整（最多 7 天）
        forecast_days = 3 if days <= 3 else 7

        params = {"location": location_id, "key": self.api_key}
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
            response = await client.get(
                self._api_url(f"/v7/weather/{forecast_days}d"),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != "200":
            raise RuntimeError(f"Weather API error: code={data.get('code')}")

        forecasts: list[dict] = []
        for day in data.get("daily", []):
            forecasts.append({
                "date": day.get("fxDate", ""),
                "text_day": day.get("textDay", "未知"),
                "temp_max": int(day.get("tempMax", 25)),
                "temp_min": int(day.get("tempMin", 15)),
                "wind_scale_day": day.get("windScaleDay", "3-4"),
                "humidity": int(day.get("humidity", 60)),
            })
        return forecasts
