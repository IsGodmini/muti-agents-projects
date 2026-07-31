from __future__ import annotations

from typing import Any, TypedDict

from app.models.schemas import (
    ConstraintReport,
    ItineraryDay,
    PlanRequest,
    PosterBrief,
    QualityReport,
    Quote,
    ResourceCandidate,
)


class PlanningState(TypedDict, total=False):
    thread_id: str
    plan_id: str
    request: PlanRequest
    resources: list[ResourceCandidate]
    resource_search_provider: str
    weather_forecast: list[dict[str, Any]]
    route_matrix: dict[str, int]
    itinerary: list[ItineraryDay]
    constraint_report: ConstraintReport
    quote: Quote
    quality_report: QualityReport
    verification_score: int
    approval: dict[str, Any]
    poster_brief: PosterBrief
    poster_asset: dict[str, str]
    day_image_paths: list[list[str]]
    report_markdown: str
    report_path: str
    current_stage: str
    retry_count: int
    errors: list[str]
