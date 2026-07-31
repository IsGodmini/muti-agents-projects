"""Plan verifier: comprehensive feasibility checking.

Runs deterministic checks against the final itinerary and produces
a structured verification report with pass/fail per dimension.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.schemas import ItineraryDay, PlanRequest, Quote

logger = logging.getLogger(__name__)


@dataclass
class VerificationCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationReport:
    passed: bool
    score: int
    checks: list[VerificationCheck] = field(default_factory=list)
    blocking_count: int = 0
    warning_count: int = 0


def verify_plan(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    quote: Quote | None = None,
) -> VerificationReport:
    """Run all verification checks and produce a report."""
    checks: list[VerificationCheck] = []

    checks.append(_check_no_empty_days(itinerary))
    checks.append(_check_no_time_conflicts(itinerary))
    checks.append(_check_daily_duration(itinerary, request))
    checks.append(_check_must_visit_coverage(request, itinerary))
    checks.append(_check_avoid_violations(request, itinerary))
    checks.append(_check_budget(request, quote))
    checks.append(_check_event_count(itinerary, request))
    checks.append(_check_lunch_break(itinerary))

    blocking = sum(1 for c in checks if not c.passed and "blocking" in c.detail.lower())
    warnings = sum(1 for c in checks if not c.passed) - blocking
    passed_count = sum(1 for c in checks if c.passed)
    score = round(passed_count / len(checks) * 100) if checks else 0

    report = VerificationReport(
        passed=all(c.passed for c in checks),
        score=score,
        checks=checks,
        blocking_count=blocking,
        warning_count=warnings,
    )
    logger.info("Verification: %d/%d passed, score=%d", passed_count, len(checks), score)
    return report


def _check_no_empty_days(itinerary: list[ItineraryDay]) -> VerificationCheck:
    empty = [d.day for d in itinerary if not d.events]
    if empty:
        return VerificationCheck("no_empty_days", False, f"BLOCKING: 空白天: {empty}")
    return VerificationCheck("no_empty_days", True, "所有天都有活动")


def _check_no_time_conflicts(itinerary: list[ItineraryDay]) -> VerificationCheck:
    conflicts = []
    for day in itinerary:
        for i in range(len(day.events) - 1):
            try:
                curr_end = _to_min(day.events[i].end_time)
                next_start = _to_min(day.events[i + 1].start_time)
                if next_start < curr_end:
                    conflicts.append(f"Day{day.day}: {day.events[i].title} ↔ {day.events[i+1].title}")
            except (ValueError, IndexError):
                continue
    if conflicts:
        return VerificationCheck("no_time_conflicts", False, f"BLOCKING: {len(conflicts)} 个时间冲突")
    return VerificationCheck("no_time_conflicts", True, "无时间冲突")


def _check_daily_duration(itinerary: list[ItineraryDay], request: PlanRequest) -> VerificationCheck:
    pace_limits = {"intense": 720, "moderate": 600, "relaxed": 480}
    limit = pace_limits.get(request.pace.value if hasattr(request.pace, "value") else "moderate", 600)
    overloads = []
    for day in itinerary:
        if not day.events:
            continue
        try:
            span = _to_min(day.events[-1].end_time) - _to_min(day.events[0].start_time)
            if span > limit:
                overloads.append(f"Day{day.day}: {span}min > {limit}min")
        except (ValueError, IndexError):
            continue
    if overloads:
        return VerificationCheck("daily_duration", False, f"超负荷: {'; '.join(overloads)}")
    return VerificationCheck("daily_duration", True, f"每日时长在 {limit}min 内")


def _check_must_visit_coverage(request: PlanRequest, itinerary: list[ItineraryDay]) -> VerificationCheck:
    if not request.must_visit:
        return VerificationCheck("must_visit", True, "无必去要求")
    names = {e.title for d in itinerary for e in d.events}
    text = " ".join(names).lower()
    missing = [mv for mv in request.must_visit if mv.lower() not in text]
    if missing:
        return VerificationCheck("must_visit", False, f"BLOCKING: 未覆盖: {missing}")
    return VerificationCheck("must_visit", True, f"全部 {len(request.must_visit)} 个必去已覆盖")


def _check_avoid_violations(request: PlanRequest, itinerary: list[ItineraryDay]) -> VerificationCheck:
    if not request.avoid:
        return VerificationCheck("avoid", True, "无避雷要求")
    names = {e.title for d in itinerary for e in d.events}
    text = " ".join(names).lower()
    violations = [av for av in request.avoid if av.lower() in text]
    if violations:
        return VerificationCheck("avoid", False, f"BLOCKING: 违反避雷: {violations}")
    return VerificationCheck("avoid", True, "无避雷违反")


def _check_budget(request: PlanRequest, quote: Quote | None) -> VerificationCheck:
    if not quote:
        return VerificationCheck("budget", False, "无报价数据")
    budget_total = request.budget_per_person * request.group_size
    error_rate = abs(quote.total_cost - budget_total) / max(budget_total, 1)
    if error_rate > 0.3:
        return VerificationCheck("budget", False, f"预算偏差 {error_rate:.0%} > 30%")
    return VerificationCheck("budget", True, f"预算偏差 {error_rate:.0%}")


def _check_event_count(itinerary: list[ItineraryDay], request: PlanRequest) -> VerificationCheck:
    pace_max = {"intense": 7, "moderate": 5, "relaxed": 4}
    limit = pace_max.get(request.pace.value if hasattr(request.pace, "value") else "moderate", 5)
    overloaded = [d.day for d in itinerary if len(d.events) > limit + 2]
    if overloaded:
        return VerificationCheck("event_count", False, f"活动过多: Day {overloaded}")
    return VerificationCheck("event_count", True, "活动数量合理")


def _check_lunch_break(itinerary: list[ItineraryDay]) -> VerificationCheck:
    missing_lunch = []
    for day in itinerary:
        has_lunch = any(
            e.category in ("dining", "break", "logistics")
            or "午餐" in e.title or "休息" in e.title
            for e in day.events
        )
        if day.events and not has_lunch:
            missing_lunch.append(day.day)
    if missing_lunch:
        return VerificationCheck("lunch_break", False, f"缺少午休: Day {missing_lunch}")
    return VerificationCheck("lunch_break", True, "每天都有休息/用餐安排")


def _to_min(time_str: str) -> int:
    parts = time_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])
