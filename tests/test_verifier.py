from app.models.schemas import ItineraryDay, ItineraryEvent, PlanRequest
from app.services.verifier import verify_plan


def test_verifier_blocks_incomplete_day_coverage() -> None:
    request = PlanRequest(
        title="西安四日游",
        product_type="family_trip",
        destination="西安",
        days=4,
        nights=3,
        group_size=1,
        budget_per_person=4000,
        target_audience="单人年轻游客",
    )
    itinerary = [
        ItineraryDay(
            day=1,
            theme="古都文化",
            events=[
                ItineraryEvent(
                    start_time="09:00",
                    end_time="11:00",
                    title="西安博物院",
                    category="museum",
                    description="参观",
                ),
                ItineraryEvent(
                    start_time="11:30",
                    end_time="12:30",
                    title="午餐",
                    category="dining",
                    description="用餐",
                ),
            ],
        )
    ]

    report = verify_plan(request, itinerary)

    coverage = next(check for check in report.checks if check.name == "day_coverage")
    assert coverage.passed is False
    assert "Day [2, 3, 4]" in coverage.detail
    assert report.blocking_count >= 1
