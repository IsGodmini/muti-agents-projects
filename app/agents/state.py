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
    selected_skill: str
    selected_skill_gates: dict[str, Any]
    selected_skill_instructions: str
    requirements_complete: bool
    missing_fields: list[str]
    resources: list[ResourceCandidate]
    resource_search_provider: str
    route_matrix: dict[str, int]
    itinerary: list[ItineraryDay]
    constraint_report: ConstraintReport
    quote: Quote
    quality_report: QualityReport
    approval: dict[str, Any]
    poster_brief: PosterBrief
    poster_asset: dict[str, str]
    current_stage: str
    retry_count: int
    errors: list[str]
