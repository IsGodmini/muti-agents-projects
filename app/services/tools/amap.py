"""Gaode (Amap) Maps API client.

Provides: POI search, geocoding, route planning, nearby search.
Docs: https://lbs.amap.com/api/webservice/summary
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from app.services.tools.base import (
    DataSource,
    DataWithSource,
    GeoPoint,
    OpeningHoursPeriod,
    Place,
)

logger = logging.getLogger(__name__)


class AmapClient:
    """Gaode Maps REST API client."""

    def __init__(self, api_key: str, base_url: str = "https://restapi.amap.com/v3") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

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

    async def search_nearby(
        self,
        location: str,
        keywords: str = "",
        types: str = "",
        radius: int = 3000,
        limit: int = 10,
    ) -> list[Place]:
        """Search POIs around a coordinate. location format: 'lng,lat'."""
        params: dict = {
            "key": self.api_key,
            "location": location,
            "radius": radius,
            "output": "json",
            "offset": min(limit, 25),
            "extensions": "all",
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types

        data = await self._get("/place/around", params)
        pois = data.get("pois", [])
        return [self._poi_to_place(poi) for poi in pois]

    async def geocode(self, address: str, city: str = "") -> GeoPoint | None:
        """Convert address to coordinates."""
        params: dict = {"key": self.api_key, "address": address, "output": "json"}
        if city:
            params["city"] = city
        data = await self._get("/geocode/geo", params)
        geocodes = data.get("geocodes", [])
        if geocodes and geocodes[0].get("location"):
            lng, lat = geocodes[0]["location"].split(",")
            return GeoPoint(lat=float(lat), lng=float(lng))
        return None

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

    async def _get(self, endpoint: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.base_url}{endpoint}", params=params)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "1" and data.get("infocode") != "10000":
                logger.warning("Amap API error: %s %s", data.get("info"), data.get("infocode"))
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
            source_url=None,
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
