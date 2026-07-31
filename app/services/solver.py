"""Deterministic constraint solver for itinerary scheduling.

The LLM decides *what* to include; this module decides *when*.
Time calculation, conflict detection, and schedule assembly are
pure deterministic operations — no LLM involved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.schemas import ItineraryDay, ItineraryEvent, PlanRequest
from app.services.tools.base import Place

logger = logging.getLogger(__name__)

PACE_CONFIG = {
    "intense": {"start": 420, "end": 1320, "max_events": 7, "break_min": 15, "lunch": (690, 780)},
    "moderate": {"start": 480, "end": 1200, "max_events": 5, "break_min": 30, "lunch": (690, 780)},
    "relaxed": {"start": 510, "end": 1080, "max_events": 4, "break_min": 45, "lunch": (690, 780)},
}


@dataclass
class ScheduleSlot:
    place: Place
    day: int
    start_minutes: int = 0
    end_minutes: int = 0
    travel_from_prev: int = 0


@dataclass
class SolverResult:
    days: list[ItineraryDay] = field(default_factory=list)
    feasible: bool = True
    conflicts: list[str] = field(default_factory=list)
    unscheduled: list[str] = field(default_factory=list)


def solve_schedule(
    request: PlanRequest,
    day_assignments: list[list[Place]],
    travel_times: dict[str, int],
) -> SolverResult:
    """Build a time-feasible schedule from day-grouped places.

    This is the deterministic core: given which places go on which day
    (decided by OR-Tools + LLM), compute exact start/end times respecting
    pace, breaks, lunch, and travel times.
    """
    pace_key = request.pace.value if hasattr(request.pace, "value") else "moderate"
    config = PACE_CONFIG.get(pace_key, PACE_CONFIG["moderate"])
    result = SolverResult()

    for day_index, places in enumerate(day_assignments):
        day_number = day_index + 1
        cursor = config["start"]
        events: list[ItineraryEvent] = []
        prev_place: Place | None = None

        if day_index == 0:
            events.append(ItineraryEvent(
                start_time=_fmt(cursor), end_time=_fmt(cursor + 30),
                title="集合 / 抵达", category="logistics",
                description="团队集合，确认行程。",
            ))
            cursor += 30

        for place in places[:config["max_events"]]:
            travel = 0
            if prev_place:
                key = f"{prev_place.place_id}->{place.place_id}"
                travel = travel_times.get(key, 30)
                cursor += travel

            lunch_start, lunch_end = config["lunch"]
            if cursor < lunch_end and cursor + place.estimated_duration_minutes > lunch_start:
                if cursor < lunch_start:
                    gap = lunch_start - cursor
                    if gap >= 15:
                        events.append(ItineraryEvent(
                            start_time=_fmt(cursor), end_time=_fmt(lunch_start),
                            title="自由休息", category="break",
                            description="休息、拍照、自由探索。",
                        ))
                cursor = lunch_end
                events.append(ItineraryEvent(
                    start_time=_fmt(lunch_start), end_time=_fmt(lunch_end),
                    title="午餐", category="dining",
                    description="团队午餐。",
                ))

            duration = max(30, place.estimated_duration_minutes)
            end_minutes = cursor + duration

            if end_minutes > config["end"]:
                result.unscheduled.append(f"Day{day_number}: {place.name} (超出时间窗口)")
                continue

            events.append(ItineraryEvent(
                start_time=_fmt(cursor),
                end_time=_fmt(end_minutes),
                title=place.name,
                resource_id=place.place_id,
                category=place.categories[0] if place.categories else "activity",
                description=place.summary or f"{place.name}体验。",
                cost_per_person=place.price,
            ))

            cursor = end_minutes + config["break_min"]
            prev_place = place

        if day_index == len(day_assignments) - 1:
            cursor += 30
            events.append(ItineraryEvent(
                start_time=_fmt(cursor), end_time=_fmt(cursor + 60),
                title="返程", category="logistics",
                description="预留返程时间。",
            ))

        result.days.append(ItineraryDay(
            day=day_number,
            theme=f"{request.destination}探索 · 第 {day_number} 天",
            events=events,
        ))

    result.feasible = len(result.unscheduled) == 0 and len(result.conflicts) == 0
    return result


def _fmt(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"
