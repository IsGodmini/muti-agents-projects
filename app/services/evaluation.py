"""Evaluation framework for travel plan quality metrics.

Metrics categories (per the optimization spec):
- Executability: time conflicts, daily overload, budget accuracy
- Personalization: must-visit coverage, avoid violations, interest match
- Information quality: source traceability, estimated data ratio
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schemas import (
    ConstraintReport,
    ItineraryDay,
    PlanRequest,
    QualityReport,
    Quote,
    ResourceCandidate,
)
from app.services.resource_matching import matches_place_name


@dataclass
class EvalMetrics:
    """Quantitative evaluation of a generated travel plan."""

    # Executability
    time_conflict_count: int = 0
    daily_overload_count: int = 0
    budget_error_rate: float = 0.0
    empty_day_count: int = 0

    # Personalization
    must_visit_coverage: float = 1.0
    avoid_violation_count: int = 0
    interest_match_rate: float = 0.0

    # Information quality
    source_traceability: float = 0.0
    estimated_data_ratio: float = 0.0

    # Overall
    executability_score: float = 0.0
    personalization_score: float = 0.0
    info_quality_score: float = 0.0
    overall_score: float = 0.0

    details: dict = field(default_factory=dict)


def evaluate_plan(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    resources: list[ResourceCandidate],
    constraint_report: ConstraintReport | None = None,
    quote: Quote | None = None,
    quality_report: QualityReport | None = None,
) -> EvalMetrics:
    """Compute evaluation metrics for a completed plan."""
    metrics = EvalMetrics()

    _eval_executability(metrics, request, itinerary, constraint_report, quote)
    _eval_personalization(metrics, request, itinerary, resources)
    _eval_info_quality(metrics, resources)

    metrics.executability_score = _compute_executability_score(metrics)
    metrics.personalization_score = _compute_personalization_score(metrics)
    metrics.info_quality_score = _compute_info_quality_score(metrics)
    metrics.overall_score = round(
        metrics.executability_score * 0.4
        + metrics.personalization_score * 0.35
        + metrics.info_quality_score * 0.25,
        2,
    )
    return metrics


def _eval_executability(
    metrics: EvalMetrics,
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    constraint_report: ConstraintReport | None,
    quote: Quote | None,
) -> None:
    pace_limits = {"intense": 720, "moderate": 600, "relaxed": 480}
    max_daily = pace_limits.get(
        request.pace.value if hasattr(request.pace, "value") else "moderate", 600
    )

    for day in itinerary:
        if not day.events:
            metrics.empty_day_count += 1
            continue

        for i in range(len(day.events) - 1):
            try:
                curr_end = _to_minutes(day.events[i].end_time)
                next_start = _to_minutes(day.events[i + 1].start_time)
                if next_start < curr_end:
                    metrics.time_conflict_count += 1
            except (ValueError, IndexError):
                continue

        try:
            day_start = _to_minutes(day.events[0].start_time)
            day_end = _to_minutes(day.events[-1].end_time)
            if day_end - day_start > max_daily:
                metrics.daily_overload_count += 1
        except (ValueError, IndexError):
            continue

    if quote and request.budget_per_person > 0:
        metrics.budget_error_rate = max(
            0.0,
            (quote.sale_price_per_person - request.budget_per_person)
            / request.budget_per_person,
        )

    if constraint_report:
        metrics.time_conflict_count = max(
            metrics.time_conflict_count, constraint_report.time_conflict_count
        )


def _eval_personalization(
    metrics: EvalMetrics,
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    resources: list[ResourceCandidate],
) -> None:
    scheduled_names = {e.title for day in itinerary for e in day.events}
    scheduled_text = " ".join(scheduled_names).lower()

    if request.must_visit:
        covered = sum(
            1
            for must_visit in request.must_visit
            if any(matches_place_name(name, must_visit) for name in scheduled_names)
        )
        metrics.must_visit_coverage = covered / len(request.must_visit)

    if request.avoid:
        metrics.avoid_violation_count = sum(
            1 for av in request.avoid if av.lower() in scheduled_text
        )

    interests = request.interests or request.themes
    if interests and resources:
        matched = sum(
            1 for r in resources
            if any(
                interest.lower() in r.category.lower()
                or interest.lower() in " ".join(r.audience_tags).lower()
                or interest.lower() in (r.summary or "").lower()
                for interest in interests
            )
        )
        metrics.interest_match_rate = matched / len(resources)


def _eval_info_quality(
    metrics: EvalMetrics,
    resources: list[ResourceCandidate],
) -> None:
    if not resources:
        return
    with_source = sum(1 for r in resources if r.source_url)
    metrics.source_traceability = with_source / len(resources)

    estimated = sum(
        1 for r in resources
        if r.opening_hours and "确认" in r.opening_hours
    )
    metrics.estimated_data_ratio = estimated / len(resources)


def _compute_executability_score(metrics: EvalMetrics) -> float:
    score = 100.0
    score -= metrics.time_conflict_count * 20
    score -= metrics.daily_overload_count * 10
    score -= metrics.empty_day_count * 30
    score -= min(30, metrics.budget_error_rate * 100)
    return round(max(0, min(100, score)), 1)


def _compute_personalization_score(metrics: EvalMetrics) -> float:
    score = 100.0
    score -= (1 - metrics.must_visit_coverage) * 40
    score -= metrics.avoid_violation_count * 15
    score += metrics.interest_match_rate * 20
    return round(max(0, min(100, score)), 1)


def _compute_info_quality_score(metrics: EvalMetrics) -> float:
    score = metrics.source_traceability * 60
    score += (1 - metrics.estimated_data_ratio) * 40
    return round(max(0, min(100, score)), 1)


def _to_minutes(time_str: str) -> int:
    parts = time_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])
