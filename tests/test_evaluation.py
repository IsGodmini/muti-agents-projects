"""Tests for the evaluation framework with fixed scenarios."""
from __future__ import annotations

from app.models.schemas import (
    ItineraryDay,
    ItineraryEvent,
    PlanRequest,
    ResourceCandidate,
)
from app.services.evaluation import evaluate_plan


def _make_request(**overrides) -> PlanRequest:
    defaults = {
        "title": "杭州亲子研学",
        "product_type": "family_trip",
        "destination": "杭州",
        "days": 2,
        "nights": 1,
        "group_size": 30,
        "budget_per_person": 1800,
        "target_audience": "8-12岁儿童及家长",
        "themes": ["自然教育"],
        "must_visit": ["西湖"],
        "avoid": ["购物店"],
        "interests": ["自然教育", "历史文化"],
    }
    defaults.update(overrides)
    return PlanRequest(**defaults)


def _make_event(title: str, start: str, end: str, cost: int = 50) -> ItineraryEvent:
    return ItineraryEvent(
        start_time=start, end_time=end, title=title,
        category="scenic", description=f"{title}体验", cost_per_person=cost,
    )


def _make_resource(name: str, category: str = "scenic", **kw) -> ResourceCandidate:
    defaults = {
        "id": f"res-{name[:4]}", "name": name, "category": category,
        "location": "杭州", "price_per_person": 50, "recommended_minutes": 120,
        "opening_hours": "09:00-17:00", "evidence": "test", "score": 0.8,
        "provider": "tavily_mcp", "source_url": "https://example.com",
        "audience_tags": ["亲子"],
    }
    defaults.update(kw)
    return ResourceCandidate(**defaults)


class TestExecutability:
    def test_perfect_plan_scores_high(self) -> None:
        request = _make_request()
        itinerary = [
            ItineraryDay(day=1, theme="自然", events=[
                _make_event("西湖游船", "09:00", "11:00"),
                _make_event("午餐", "11:30", "13:00", 0),
                _make_event("湿地探索", "13:30", "16:00"),
            ]),
        ]
        resources = [_make_resource("西湖游船"), _make_resource("湿地探索", "outdoor")]
        metrics = evaluate_plan(request, itinerary, resources)
        assert metrics.time_conflict_count == 0
        assert metrics.executability_score >= 80

    def test_time_conflict_detected(self) -> None:
        request = _make_request()
        itinerary = [
            ItineraryDay(day=1, theme="冲突", events=[
                _make_event("景点A", "09:00", "11:00"),
                _make_event("景点B", "10:30", "12:00"),
            ]),
        ]
        metrics = evaluate_plan(request, itinerary, [])
        assert metrics.time_conflict_count == 1
        assert metrics.executability_score < 100

    def test_daily_overload(self) -> None:
        request = _make_request(pace="relaxed")
        itinerary = [
            ItineraryDay(day=1, theme="超长", events=[
                _make_event("早", "06:00", "08:00"),
                _make_event("晚", "20:00", "22:00"),
            ]),
        ]
        metrics = evaluate_plan(request, itinerary, [])
        assert metrics.daily_overload_count == 1


class TestPersonalization:
    def test_must_visit_covered(self) -> None:
        request = _make_request(must_visit=["西湖"])
        itinerary = [
            ItineraryDay(day=1, theme="ok", events=[
                _make_event("西湖游船", "09:00", "11:00"),
            ]),
        ]
        metrics = evaluate_plan(request, itinerary, [])
        assert metrics.must_visit_coverage == 1.0

    def test_must_visit_missing(self) -> None:
        request = _make_request(must_visit=["灵隐寺"])
        itinerary = [
            ItineraryDay(day=1, theme="no", events=[
                _make_event("西湖", "09:00", "11:00"),
            ]),
        ]
        metrics = evaluate_plan(request, itinerary, [])
        assert metrics.must_visit_coverage == 0.0
        assert metrics.personalization_score < 100

    def test_avoid_violation(self) -> None:
        request = _make_request(avoid=["购物店"])
        itinerary = [
            ItineraryDay(day=1, theme="bad", events=[
                _make_event("购物店", "09:00", "11:00"),
            ]),
        ]
        metrics = evaluate_plan(request, itinerary, [])
        assert metrics.avoid_violation_count == 1


class TestInfoQuality:
    def test_source_traceability(self) -> None:
        resources = [
            _make_resource("A", source_url="https://a.com"),
            _make_resource("B", source_url=None),
        ]
        metrics = evaluate_plan(_make_request(), [], resources)
        assert metrics.source_traceability == 0.5

    def test_estimated_data(self) -> None:
        resources = [
            _make_resource("A", opening_hours="09:00-17:00"),
            _make_resource("B", opening_hours="需在官方渠道二次确认"),
        ]
        metrics = evaluate_plan(_make_request(), [], resources)
        assert metrics.estimated_data_ratio == 0.5
