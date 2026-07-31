"""Candidate resource scoring and ranking.

Composite score = interest_match + quality + combo_value
                  - distance_penalty - budget_penalty - crowd_penalty
"""
from __future__ import annotations

import logging

from app.models.schemas import PlanRequest, ResourceCandidate

logger = logging.getLogger(__name__)

CROWD_PENALTY = {"low": 0.0, "medium": 0.05, "high": 0.12, "unknown": 0.03}
WEATHER_PENALTY = {"low": 0.0, "medium": 0.03, "high": 0.08}


def score_resources(
    resources: list[ResourceCandidate],
    request: PlanRequest,
) -> list[ResourceCandidate]:
    """Score and rank resources against the TripSpec. Returns sorted list."""
    interests = set(request.interests or request.themes)
    must_visit = {name.lower() for name in request.must_visit}
    avoid_keywords = {name.lower() for name in request.avoid}
    budget = request.budget_per_person

    scored: list[ResourceCandidate] = []
    for resource in resources:
        name_lower = resource.name.lower()

        if any(kw in name_lower for kw in avoid_keywords if kw):
            resource = resource.model_copy(update={"composite_score": -1.0})
            scored.append(resource)
            continue

        interest_match = _interest_overlap(resource, interests)
        quality = resource.score
        must_visit_bonus = 0.3 if any(mv in name_lower for mv in must_visit if mv) else 0.0
        audience_match = _audience_overlap(resource, request.target_audience)

        budget_penalty = 0.0
        if budget > 0 and resource.price_per_person > budget * 0.3:
            budget_penalty = min(0.2, resource.price_per_person / budget * 0.15)

        crowd_pen = CROWD_PENALTY.get(resource.crowd_risk, 0.03)
        weather_pen = WEATHER_PENALTY.get(resource.weather_dependency, 0.0)

        composite = (
            interest_match * 0.30
            + quality * 0.25
            + audience_match * 0.15
            + must_visit_bonus
            - budget_penalty
            - crowd_pen
            - weather_pen
        )
        composite = max(0.0, min(1.0, composite))

        resource = resource.model_copy(update={
            "composite_score": round(composite, 4),
            "interest_score": round(interest_match, 4),
        })
        scored.append(resource)

    scored.sort(key=lambda r: r.composite_score, reverse=True)
    logger.info(
        "Ranked %d resources: top=%s (%.3f)",
        len(scored),
        scored[0].name[:30] if scored else "none",
        scored[0].composite_score if scored else 0,
    )
    return scored


def _interest_overlap(resource: ResourceCandidate, interests: set[str]) -> float:
    if not interests:
        return 0.5
    tags = {t.lower() for t in resource.audience_tags}
    category = resource.category.lower()
    summary_lower = (resource.summary or "").lower()
    matched = sum(
        1 for interest in interests
        if interest.lower() in tags
        or interest.lower() in category
        or interest.lower() in summary_lower
    )
    return min(1.0, matched / len(interests) + 0.2)


def _audience_overlap(resource: ResourceCandidate, audience: str) -> float:
    if not audience:
        return 0.5
    audience_lower = audience.lower()
    tags_lower = " ".join(t.lower() for t in resource.audience_tags)
    if audience_lower in tags_lower or tags_lower in audience_lower:
        return 0.8
    keywords = [w for w in audience_lower.replace("及", " ").split() if len(w) >= 2]
    matched = sum(1 for kw in keywords if kw in tags_lower)
    return min(1.0, matched / max(len(keywords), 1) * 0.6 + 0.2)
