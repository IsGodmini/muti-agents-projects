"""Gaode (Amap) Maps API client.

Provides: POI search, geocoding, route planning, nearby search.
Docs: https://lbs.amap.com/api/webservice/summary
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from app.services.tools.base import (
    DataSource,
    DataWithSource,
    GeoPoint,
    OpeningHoursPeriod,
    Place,
)

logger = logging.getLogger(__name__)


class AmapAPIError(RuntimeError):
    """Raised when Amap returns an application-level error response."""

    def __init__(self, info: str, infocode: str) -> None:
        self.info = info
        self.infocode = infocode
        super().__init__(f"Amap API error: {info} ({infocode})")


class AmapClient:
    """Gaode Maps REST API client."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://restapi.amap.com/v3",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    async def search_poi(
        self,
        keywords: str,
        city: str = "",
        types: str = "",
        limit: int = 10,
    ) -> list[Place]:
        """Text-based POI search. Returns structured Place objects."""
        params: dict = {
            "key": self.api_key,
            "keywords": keywords,
            "output": "json",
            "offset": min(limit, 25),
            "extensions": "all",
        }
        if city:
            params["city"] = city
        if types:
            params["types"] = types

        data = await self._get("/place/text", params)
        pois = data.get("pois", [])
        return [self._poi_to_place(poi) for poi in pois]

    async def travel_time(
        self,
        origin: str,
        destination: str,
        mode: str = "transit",
        city: str = "",
    ) -> int:
        """Get real travel time in minutes between two coordinates.

        origin/destination format: 'lng,lat'
        mode: driving / walking / transit
        """
        if mode == "walking":
            endpoint = "/direction/walking"
            params: dict = {"key": self.api_key, "origin": origin, "destination": destination}
        elif mode == "driving":
            endpoint = "/direction/driving"
            params = {"key": self.api_key, "origin": origin, "destination": destination}
        else:
            endpoint = "/direction/transit/integrated"
            params = {
                "key": self.api_key,
                "origin": origin,
                "destination": destination,
                "city": city or "杭州",
            }

        data = await self._get(endpoint, params)
        route = data.get("route", {})

        if mode == "transit":
            transits = route.get("transits", [])
            if transits:
                duration_sec = int(transits[0].get("duration", 1800))
                return max(5, duration_sec // 60)
        else:
            paths = route.get("paths", [])
            if paths:
                duration_sec = int(paths[0].get("duration", 1800))
                return max(5, duration_sec // 60)

        return 30

    async def weather_forecast(self, city: str, days: int = 4) -> list[dict]:
        """Return Amap's real multi-day forecast as a QWeather-compatible shape."""
        geocode = await self._get(
            "/geocode/geo",
            {"key": self.api_key, "address": city, "city": city, "output": "json"},
        )
        geocodes = geocode.get("geocodes", [])
        if not geocodes or not geocodes[0].get("adcode"):
            raise AmapAPIError(f"No adcode found for {city}", "NO_ADCODE")
        data = await self._get(
            "/weather/weatherInfo",
            {
                "key": self.api_key,
                "city": str(geocodes[0]["adcode"]),
                "extensions": "all",
                "output": "json",
            },
        )
        forecasts = data.get("forecasts", [])
        casts = forecasts[0].get("casts", []) if forecasts else []
        return [
            {
                "date": str(item.get("date", "")),
                "text_day": str(item.get("dayweather", "未知")),
                "temp_max": int(item.get("daytemp") or 25),
                "temp_min": int(item.get("nighttemp") or 15),
                "wind_scale_day": str(item.get("daypower") or "-"),
                "humidity": 60,
                "provider": "amap",
            }
            for item in casts[: max(1, min(days, 4))]
        ]

    async def _get(self, endpoint: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            response = await client.get(f"{self.base_url}{endpoint}", params=params)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "1" and data.get("infocode") != "10000":
                raise AmapAPIError(str(data.get("info", "unknown")), str(data.get("infocode", "")))
            return data

    def _poi_to_place(self, poi: dict) -> Place:
        location = poi.get("location", "")
        coords = GeoPoint()
        if location and "," in location:
            lng_str, lat_str = location.split(",")
            coords = GeoPoint(lat=float(lat_str), lng=float(lng_str))

        raw_open_time = poi.get("biz_ext", {}).get("open_time", "")
        if not isinstance(raw_open_time, str):
            raw_open_time = ""
        open_hours = self._parse_opening_hours(raw_open_time)
        rating_str = poi.get("biz_ext", {}).get("rating", "")
        rating = float(rating_str) if isinstance(rating_str, str) and rating_str and rating_str != "[]" else 0.0
        cost_str = poi.get("biz_ext", {}).get("cost", "")
        price = int(float(cost_str)) if isinstance(cost_str, str) and cost_str and cost_str != "[]" else 0

        source_url = "https://uri.amap.com/marker?" + urlencode({
            "poiid": str(poi.get("id", "")),
            "name": str(poi.get("name", "")),
            "src": "tripops",
            "callnative": "0",
        })

        return Place(
            place_id=poi.get("id", ""),
            name=poi.get("name", ""),
            coordinates=coords,
            categories=[t.strip() for t in poi.get("type", "").split(";") if t.strip()],
            address=poi.get("address", "") if isinstance(poi.get("address"), str) else "",
            estimated_duration_minutes=120,
            opening_hours=open_hours,
            opening_hours_text=DataWithSource(
                value=raw_open_time or "未知",
                source=DataSource.PLACES_API,
                confidence=0.9 if open_hours else 0.3,
                is_estimated=not open_hours,
            ),
            price=price,
            rating=rating,
            source_url=source_url,
            summary=poi.get("pname", "") + poi.get("cityname", "") + poi.get("adname", ""),
            retrieved_at=datetime.now(UTC),
            provider="amap",
        )

    def _parse_opening_hours(self, open_time: str) -> list[OpeningHoursPeriod]:
        if not open_time or open_time == "[]":
            return []
        periods = []
        try:
            if "-" in open_time:
                parts = open_time.split("-")
                open_t = parts[0].strip()[:5]
                close_t = parts[1].strip()[:5]
                for day in range(7):
                    periods.append(OpeningHoursPeriod(
                        day_of_week=day, open_time=open_t, close_time=close_t,
                    ))
        except (ValueError, IndexError):
            pass
        return periods
