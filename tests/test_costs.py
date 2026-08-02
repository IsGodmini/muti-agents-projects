from app.models.schemas import ItineraryDay, ItineraryEvent, ResourceCandidate
from app.services.costs import event_cost_label, normalize_itinerary_costs


def test_cost_normalization_backfills_paid_resource_and_meal() -> None:
    resource = ResourceCandidate(
        id="paid",
        name="测试景点",
        category="scenic",
        location="杭州",
        price_per_person=60,
        recommended_minutes=120,
        opening_hours="09:00-17:00",
        evidence="官方信息",
        score=0.9,
    )
    itinerary = [
        ItineraryDay(
            day=1,
            theme="测试",
            events=[
                ItineraryEvent(
                    start_time="09:00",
                    end_time="11:00",
                    title="测试景点",
                    resource_id="paid",
                    category="scenic",
                    description="游览",
                ),
                ItineraryEvent(
                    start_time="11:30",
                    end_time="12:30",
                    title="午餐",
                    category="meal",
                    description="团队午餐",
                ),
            ],
        )
    ]

    normalized = normalize_itinerary_costs(itinerary, [resource])

    assert [event.cost_per_person for event in normalized[0].events] == [60, 80]
    assert all(event.cost_status == "estimated" for event in normalized[0].events)


def test_zero_cost_is_rendered_as_a_semantic_label() -> None:
    transport = ItineraryEvent(
        start_time="08:00",
        end_time="08:30",
        title="集合出发",
        category="transport",
        description="出发",
        cost_status="included",
    )
    free_time = transport.model_copy(
        update={"title": "自由活动", "category": "free_time", "cost_status": "optional"}
    )

    assert event_cost_label(transport) == "已纳入团费"
    assert event_cost_label(free_time) == "按需自理"
    assert "0" not in event_cost_label(transport)


def test_transport_to_lunch_is_not_charged_as_a_meal() -> None:
    event = ItineraryEvent(
        start_time="11:00",
        end_time="11:30",
        title="前往午餐地点",
        category="transport",
        description="乘车前往餐厅",
        cost_per_person=80,
        cost_status="estimated",
    )

    normalized = normalize_itinerary_costs([ItineraryDay(day=1, theme="测试", events=[event])])
    result = normalized[0].events[0]

    assert result.cost_per_person == 0
    assert result.cost_status == "included"
    assert event_cost_label(result) == "已纳入团费"
