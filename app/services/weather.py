"""Weather API client (QWeather / HeFeng).

Provides a multi-day forecast for travel planning.
Docs: https://dev.qweather.com/docs/api/
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class WeatherClient:
    """QWeather REST API client."""

    def __init__(self, api_key: str, base_url: str = "https://devapi.qweather.com/v7") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def get_forecast(self, location: str, days: int = 3) -> list[dict]:
        """Get a multi-day forecast for a city or `lng,lat` location.

        Returns a list of daily dicts with keys:
        date, text_day, temp_max, temp_min, wind_scale_day, humidity.
        """
        params = {"location": location, "key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/weather/{days}d",
                    params=params,
                )
                response.raise_for_status()
                data = response.json()

            if data.get("code") != "200":
                logger.warning("Weather API error: code=%s", data.get("code"))
                return []

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
        except Exception:
            logger.warning("Weather API call failed for %s", location, exc_info=True)
            return []
