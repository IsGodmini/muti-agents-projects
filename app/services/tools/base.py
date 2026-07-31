"""Data provenance models for the tool layer.

Every piece of dynamic data carries source, timestamp, and confidence
so downstream consumers can distinguish verified facts from estimates.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DataSource(StrEnum):
    PLACES_API = "places_api"
    MAP_API = "map_api"
    WEATHER_API = "weather_api"
    TAVILY_SEARCH = "tavily_search"
    LLM_ESTIMATE = "llm_estimate"
    USER_INPUT = "user_input"
    INTERNAL = "internal"


class DataWithSource(BaseModel):
    """A value annotated with its provenance."""
    value: str
    source: DataSource
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = Field(default=0.5, ge=0, le=1)
    is_estimated: bool = False


class GeoPoint(BaseModel):
    lat: float = 0.0
    lng: float = 0.0


class OpeningHoursPeriod(BaseModel):
    day_of_week: int = Field(ge=0, le=6, description="0=Monday")
    open_time: str = "09:00"
    close_time: str = "17:00"
    is_closed: bool = False


class Place(BaseModel):
    """Unified candidate resource with structured data and provenance."""
    place_id: str
    name: str
    coordinates: GeoPoint = Field(default_factory=GeoPoint)
    categories: list[str] = Field(default_factory=list)
    address: str = ""
    estimated_duration_minutes: int = 120
    opening_hours: list[OpeningHoursPeriod] = Field(default_factory=list)
    opening_hours_text: DataWithSource | None = None
    price: int = 0
    price_source: DataWithSource | None = None
    rating: float = 0.0
    reservation_required: bool = False
    interest_score: float = 0.5
    crowd_risk: str = "unknown"
    weather_dependency: str = "low"
    composite_score: float = 0.0
    source_url: str | None = None
    summary: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str = "internal"
