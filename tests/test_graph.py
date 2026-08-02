"""Test workflow logic with mocked external services."""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.graph import (
    _free_day,
    _is_visitable_resource,
    _is_visitable_resource_name,
    _normalize_schedule_events,
    _select_resources,
    build_planning_graph,
    plan_itinerary,
    poster_route,
    review_decision,
    validate_constraints,
)
from app.models.schemas import (
    ConstraintReport,
    CostBreakdown,
    CostItemEstimate,
    DailySchedule,
    ItineraryDay,
    ItineraryEvent,
    PlanRequest,
    ProductType,
    QualityAssessment,
    QualityReport,
    ResourceCandidate,
    ResourceEnrichmentBatch,
    ScheduleBatch,
    ScheduledEvent,
    TravelTimeMatrix,
    TravelTimePair,
)

TEST_RESOURCES = [
    ResourceCandidate(
        id="res-1", name="测试博物馆", category="museum", location="杭州",
        price_per_person=30, recommended_minutes=120, opening_hours="09:00-17:00",
        audience_tags=["亲子"], evidence="test", score=0.9, provider="tavily_mcp",
    ),
    ResourceCandidate(
        id="res-2", name="测试湿地", category="outdoor", location="杭州",
        price_per_person=80, recommended_minutes=180, opening_hours="08:30-17:30",
        audience_tags=["自然教育"], evidence="test", score=0.85, provider="tavily_mcp",
    ),
    ResourceCandidate(
        id="res-3", name="测试工坊", category="workshop", location="杭州",
        price_per_person=60, recommended_minutes=90, opening_hours="09:00-18:00",
        audience_tags=["手作"], evidence="test", score=0.8, provider="tavily_mcp",
    ),
]


@pytest.mark.parametrize(
    "time_field",
    ["duration_minutes", "estimated_time_minutes", "estimated_duration_minutes", "travel_time_minutes"],
)
def test_travel_time_pair_accepts_model_minute_aliases(time_field: str) -> None:
    pair = TravelTimePair.model_validate(
        {"from_index": 0, "to_index": 1, time_field: 25}
    )

    assert pair.time == 25


async def _mock_structured_completion(self, *, schema, **kwargs):
    """Return appropriate test data based on the requested schema."""
    if schema is ResourceEnrichmentBatch:
        return ResourceEnrichmentBatch(resources=[])
    if schema is TravelTimeMatrix:
        return TravelTimeMatrix(pairs=[
            TravelTimePair(from_index=0, to_index=1, time=25),
            TravelTimePair(from_index=1, to_index=0, time=25),
            TravelTimePair(from_index=0, to_index=2, time=40),
            TravelTimePair(from_index=2, to_index=0, time=40),
            TravelTimePair(from_index=1, to_index=2, time=20),
            TravelTimePair(from_index=2, to_index=1, time=20),
        ])
    if schema is ScheduleBatch:
        day_number = 2 if "第 2 天" in kwargs.get("user_prompt", "") else 1
        resource_id = "res-3" if day_number == 2 else "res-1"
        title = "测试工坊" if day_number == 2 else "测试博物馆"
        return ScheduleBatch(days=[
            DailySchedule(day=day_number, theme="自然探索", events=[
                ScheduledEvent(
                    resource_id=resource_id, title=title,
                    start_time="09:00", end_time="11:00",
                    category="museum", description="参观测试博物馆。",
                    cost_per_person=30,
                ),
                ScheduledEvent(
                    resource_id="", title="午餐与午休",
                    start_time="11:30", end_time="12:30",
                    category="dining", description="午餐并休息。",
                    cost_per_person=50,
                ),
            ]),
        ])
    if schema is CostBreakdown:
        return CostBreakdown(items=[
            CostItemEstimate(category="交通", description="旅游大巴", amount=5000),
            CostItemEstimate(category="住宿", description="1晚住宿", amount=9000),
            CostItemEstimate(category="餐饮", description="团队餐饮", amount=4500),
            CostItemEstimate(category="门票及课程", description="场馆门票", amount=5100),
            CostItemEstimate(category="服务", description="领队保险", amount=2400),
        ])
    if schema is QualityAssessment:
        return QualityAssessment(
            overall_score=88,
            fact_traceability_score=85,
            feasibility_score=90,
            audience_fit_score=87,
            suggestions=["建议确认场馆档期"],
        )
    raise ValueError(f"Unexpected schema: {schema}")


@pytest.mark.asyncio
async def test_graph_runs_end_to_end_with_auto_review(monkeypatch) -> None:
    """Graph runs fully automatically: LLM review replaces human approval."""
    monkeypatch.setenv("MOCK_MODEL_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    async def _mock_ainvoke(name, payload):
        if name in ("search_attractions", "search_poi_amap"):
            return TEST_RESOURCES
        if name == "get_weather_forecast":
            return [{"date": "2026-08-01", "text_day": "晴", "temp_max": 32, "temp_min": 25, "wind_scale_day": "3", "humidity": 60}]
        if name == "calculate_route_matrix":
            ids = payload.get("resource_ids", [])
            times = payload.get("travel_times", {})
            matrix = {}
            for s in ids:
                for t in ids:
                    if s != t:
                        matrix[f"{s}->{t}"] = times.get(f"{s}->{t}", 30)
            return matrix
        return []

    with (
        patch(
            "app.agents.graph.tool_registry.ainvoke",
            side_effect=_mock_ainvoke,
        ),
        patch(
            "app.services.model_gateway.ModelGateway.structured_completion",
            new=_mock_structured_completion,
        ),
    ):
        graph = build_planning_graph()
        config = {"configurable": {"thread_id": "test-family-plan"}}
        request = PlanRequest(
            title="杭州两天一夜亲子研学",
            product_type=ProductType.FAMILY,
            destination="杭州",
            days=2,
            nights=1,
            group_size=30,
            budget_per_person=1800,
            target_margin_rate=0.15,
            target_audience="8-12岁儿童及家长",
            themes=["自然教育"],
            hard_constraints=["连续乘车不超过90分钟"],
        )

        from langgraph.types import Command

        result = await graph.ainvoke(
            {
                "thread_id": "test-family-plan",
                "plan_id": "PLAN-TEST-001",
                "request": request,
                "current_stage": "created",
            },
            config=config,
        )

        # Graph interrupts before finalize_delivery for approval
        assert result["current_stage"] == "poster_generated"

        # Resume with approval to complete delivery
        result = await graph.ainvoke(
            Command(resume={"approved": True, "reviewer_id": "test-reviewer"}),
            config=config,
        )

        assert result["current_stage"] == "delivered"
        assert result["constraint_report"].valid is True
        assert result["weather_forecast"][0]["text_day"] == "晴"
        assert len(result["itinerary"]) == 2
        assert result["itinerary"][1].day == 2
        assert result["itinerary"][1].events
        assert result["quote"].total_cost == 26000
        assert result["verification_score"] >= 60
        assert result["poster_asset"]["status"] == "generated"
        assert "report_path" in result

    get_settings.cache_clear()


def test_review_decision_blocks_llm_blocking_issues() -> None:
    state = {
        "constraint_report": ConstraintReport(
            valid=True,
            score=100,
            total_travel_minutes=30,
            max_daily_minutes=480,
        ),
        "quality_report": QualityReport(
            overall_score=88,
            fact_traceability_score=80,
            feasibility_score=90,
            audience_fit_score=90,
            blocking_issues=["违反硬约束"],
        ),
        "verification_score": 88,
        "verification_passed": True,
        "verification_blocking_count": 0,
    }
    assert review_decision(state) == "review_repair"


def test_review_decision_allows_non_blocking_warnings() -> None:
    state = {
        "constraint_report": ConstraintReport(
            valid=True,
            score=92,
            total_travel_minutes=30,
            max_daily_minutes=480,
        ),
        "quality_report": QualityReport(
            overall_score=80,
            fact_traceability_score=75,
            feasibility_score=80,
            audience_fit_score=85,
            suggestions=["确认开放时间"],
        ),
        "verification_score": 75,
        "verification_passed": False,
        "verification_blocking_count": 0,
    }
    assert review_decision(state) == "poster"


def test_resource_selection_enforces_avoid_and_daily_limit() -> None:
    request = PlanRequest(
        title="杭州两日游",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=2,
        nights=1,
        group_size=3,
        budget_per_person=2000,
        target_audience="亲子家庭",
        avoid=["购物店"],
    )
    resources = [
        TEST_RESOURCES[0].model_copy(update={"id": f"safe-{index}", "composite_score": 0.8})
        for index in range(8)
    ]
    resources = [
        resource.model_copy(update={"name": f"安全景点 {index}"})
        for index, resource in enumerate(resources)
    ]
    resources.append(
        TEST_RESOURCES[0].model_copy(
            update={"id": "avoid", "name": "强制购物店", "composite_score": -1.0}
        )
    )
    selected = _select_resources(resources, request)
    assert len(selected) == 4
    assert all("购物店" not in resource.name for resource in selected)


def test_constraint_gate_blocks_duplicate_resource_within_same_day() -> None:
    request = PlanRequest(
        title="杭州一日游",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=1,
        nights=0,
        group_size=3,
        budget_per_person=1000,
        target_audience="亲子家庭",
    )
    events = [
        ItineraryEvent(
            resource_id="res-1", title="测试博物馆上午场",
            start_time="09:00", end_time="11:00", category="museum", description="参观",
        ),
        ItineraryEvent(
            resource_id="", title="午餐", start_time="11:30", end_time="12:30",
            category="dining", description="用餐",
        ),
        ItineraryEvent(
            resource_id="res-1", title="测试博物馆下午场",
            start_time="13:00", end_time="15:00", category="museum", description="参观",
        ),
    ]

    result = validate_constraints({
        "request": request,
        "resources": [TEST_RESOURCES[0]],
        "itinerary": [ItineraryDay(day=1, theme="博物馆", events=events)],
        "route_matrix": {},
    })

    assert result["constraint_report"].valid is False
    assert any(
        issue.code == "DUPLICATE_RESOURCE"
        for issue in result["constraint_report"].issues
    )


def test_constraint_gate_recognizes_food_experience_as_lunch() -> None:
    request = PlanRequest(
        title="杭州一日美食游",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=1,
        nights=0,
        group_size=5,
        budget_per_person=1000,
        target_audience="成年朋友小团",
    )
    food_resource = ResourceCandidate(
        id="food-1",
        name="杭帮美食体验",
        category="food",
        location="杭州",
        price_per_person=100,
        recommended_minutes=120,
        opening_hours="11:00-14:00",
        provider="tavily_mcp",
        evidence="真实美食资源",
        score=0.9,
    )
    events = [
        ItineraryEvent(
            resource_id="food-1",
            title="杭帮美食体验",
            start_time="11:40",
            end_time="13:40",
            category="food",
            description="品尝本地菜",
        )
    ]

    result = validate_constraints({
        "request": request,
        "resources": [food_resource],
        "itinerary": [ItineraryDay(day=1, theme="美食", events=events)],
        "route_matrix": {},
    })

    assert result["constraint_report"].valid is True
    assert not any(
        issue.code == "LUNCH_MISSING"
        for issue in result["constraint_report"].issues
    )


def test_constraint_gate_blocks_missing_itinerary_days() -> None:
    request = PlanRequest(
        title="西安四日游",
        product_type=ProductType.FAMILY,
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

    result = validate_constraints(
        {
            "request": request,
            "resources": [],
            "itinerary": itinerary,
            "route_matrix": {},
        }
    )

    report = result["constraint_report"]
    assert report.valid is False
    issue = next(item for item in report.issues if item.code == "MISSING_ITINERARY_DAYS")
    assert "2, 3, 4" in issue.message


def test_schedule_normalization_backfills_lunch_and_trims_late_logistics() -> None:
    events = [
        ItineraryEvent(
            start_time="09:00", end_time="11:30", title="西湖", resource_id="res-1",
            category="outdoor", description="游览",
        ),
        ItineraryEvent(
            start_time="11:30", end_time="13:00", title="", resource_id="",
            category="", description="",
        ),
        ItineraryEvent(
            start_time="18:30", end_time="19:30", title="返回酒店", resource_id="",
            category="transport", description="返程",
        ),
    ]

    normalized = _normalize_schedule_events(
        events,
        {"res-1": TEST_RESOURCES[0]},
        max_span_minutes=600,
    )

    assert [event.title for event in normalized] == ["西湖", "午餐与午休"]
    assert normalized[1].category == "dining"
    assert normalized[0].cost_per_person == TEST_RESOURCES[0].price_per_person
    assert normalized[1].cost_per_person == 80
    assert all(event.cost_status != "unknown" for event in normalized)


def test_schedule_normalization_inserts_lunch_into_midday_gap() -> None:
    events = [
        ItineraryEvent(
            start_time="09:00", end_time="11:00", title="自由活动",
            category="activity", description="上午活动",
        ),
        ItineraryEvent(
            start_time="14:00", end_time="17:00", title="休整",
            category="break", description="下午休整",
        ),
    ]

    normalized = _normalize_schedule_events(events, {}, max_span_minutes=600)

    lunch = next(event for event in normalized if event.title == "午餐与午休")
    assert (lunch.start_time, lunch.end_time) == ("11:30", "12:30")
    assert lunch.cost_per_person == 80


def test_schedule_normalization_truncates_long_activity_before_lunch() -> None:
    events = [
        ItineraryEvent(
            start_time="09:30",
            end_time="14:30",
            title="游览哈素海旅游景区",
            resource_id="res-1",
            category="scenic",
            description="游览",
        ),
        ItineraryEvent(
            start_time="12:00",
            end_time="13:00",
            title="午餐",
            category="dining",
            description="用餐",
        ),
    ]

    normalized = _normalize_schedule_events(
        events,
        {"res-1": TEST_RESOURCES[0]},
        max_span_minutes=600,
    )

    assert normalized[0].end_time == "12:00"
    assert normalized[1].start_time == "12:00"


def test_select_resources_keeps_one_best_poi_per_must_visit() -> None:
    request = PlanRequest(
        title="杭州五日游", product_type=ProductType.FAMILY, destination="杭州",
        days=1, nights=0, group_size=5, budget_per_person=1000,
        target_audience="成年朋友小团", must_visit=["良渚古城遗址公园"],
    )
    variants = [
        ResourceCandidate(
            id="main", name="良渚古城遗址公园", category="attraction", location="杭州",
            evidence="高德", score=0.9, composite_score=0.8, provider="amap",
            recommended_minutes=180, opening_hours="09:00-17:00",
        ),
        ResourceCandidate(
            id="wall", name="良渚古城遗址夯筑城墙", category="attraction", location="杭州",
            evidence="高德", score=0.9, composite_score=0.9, provider="amap",
            recommended_minutes=60, opening_hours="09:00-17:00",
        ),
        TEST_RESOURCES[0].model_copy(update={"composite_score": 0.7}),
    ]

    selected = _select_resources(variants, request)

    assert selected[0].id == "main"
    assert not any(resource.id == "wall" for resource in selected)


def test_non_visitable_search_titles_are_filtered() -> None:
    assert _is_visitable_resource_name("西湖风景名胜区") is True
    assert _is_visitable_resource_name("杭州旅游攻略资讯研读") is False
    article = TEST_RESOURCES[0].model_copy(
        update={"name": "呼和浩特", "source_title": "呼和浩特旅游全攻略"}
    )
    assert _is_visitable_resource(article) is False


def test_free_day_contains_explicit_lunch() -> None:
    free_day = _free_day(5)

    assert any(event.category == "dining" for event in free_day.events)


def test_poster_route_requires_all_artwork_to_be_ready() -> None:
    assert poster_route({"poster_ready": True}) == "approval"
    assert poster_route({"poster_ready": False}) == "failed"
    assert poster_route({}) == "failed"


@pytest.mark.asyncio
async def test_repair_reuses_cached_weather_and_route_matrix() -> None:
    request = PlanRequest(
        title="杭州两日游",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=2,
        nights=1,
        group_size=3,
        budget_per_person=2000,
        target_audience="亲子家庭",
    )
    route_matrix = {
        f"{source.id}->{target.id}": 30
        for source in TEST_RESOURCES
        for target in TEST_RESOURCES
        if source.id != target.id
    }
    schedules = [
        ItineraryDay(day=1, theme="第一天", events=[]),
        ItineraryDay(day=2, theme="第二天", events=[]),
    ]

    with (
        patch("app.agents.graph.tool_registry.ainvoke", new=AsyncMock()) as ainvoke,
        patch(
            "app.agents.graph.tool_registry.invoke",
            return_value=[["res-1", "res-2"], ["res-3"]],
        ),
        patch("app.agents.graph._estimate_travel_times", new=AsyncMock()) as estimate,
        patch(
            "app.agents.graph._generate_schedule",
            new=AsyncMock(return_value=schedules),
        ),
    ):
        result = await plan_itinerary({
            "request": request,
            "resources": TEST_RESOURCES,
            "weather_forecast": [{"date": "2026-08-01", "text_day": "晴"}],
            "route_matrix": route_matrix,
            "travel_time_sources": {key: "amap" for key in route_matrix},
            "repair_feedback": ["调整节奏"],
        })

    ainvoke.assert_not_awaited()
    estimate.assert_not_awaited()
    assert result["route_matrix"] == route_matrix
    assert result["weather_forecast"][0]["text_day"] == "晴"


@pytest.mark.asyncio
async def test_graph_rejection_records_decision_and_ends(monkeypatch) -> None:
    """Rejecting the plan records the decision and ends without delivery."""
    monkeypatch.setenv("MOCK_MODEL_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    async def _mock_ainvoke(name, payload):
        if name in ("search_attractions", "search_poi_amap"):
            return TEST_RESOURCES
        if name == "get_weather_forecast":
            return [{"date": "2026-08-01", "text_day": "晴", "temp_max": 32, "temp_min": 25, "wind_scale_day": "3", "humidity": 60}]
        if name == "calculate_route_matrix":
            ids = payload.get("resource_ids", [])
            times = payload.get("travel_times", {})
            return {f"{s}->{t}": times.get(f"{s}->{t}", 30) for s in ids for t in ids if s != t}
        return []

    from app.services import plan_store as ps

    recorded: list[dict] = []
    orig_save_approval = ps.plan_store.save_approval

    def _spy_save_approval(plan_id: str, decision: dict) -> dict[str, str]:
        recorded.append({"plan_id": plan_id, "decision": decision})
        return orig_save_approval(plan_id, decision)

    with (
        patch("app.agents.graph.tool_registry.ainvoke", side_effect=_mock_ainvoke),
        patch(
            "app.services.model_gateway.ModelGateway.structured_completion",
            new=_mock_structured_completion,
        ),
        patch.object(ps.plan_store, "save_approval", side_effect=_spy_save_approval),
    ):
        graph = build_planning_graph()
        config = {"configurable": {"thread_id": "test-reject-plan"}}
        request = PlanRequest(
            title="杭州两天一夜亲子研学",
            product_type=ProductType.FAMILY,
            destination="杭州",
            days=2,
            nights=1,
            group_size=30,
            budget_per_person=1800,
            target_margin_rate=0.15,
            target_audience="8-12岁儿童及家长",
            themes=["自然教育"],
        )

        from langgraph.types import Command

        result = await graph.ainvoke(
            {
                "thread_id": "test-reject-plan",
                "plan_id": "PLAN-REJECT-001",
                "request": request,
                "current_stage": "created",
            },
            config=config,
        )
        assert result["current_stage"] == "poster_generated"
        assert result.get("__interrupt__")

        result = await graph.ainvoke(
            Command(resume={"approved": False, "reviewer_id": "test-reviewer", "comment": "预算超支"}),
            config=config,
        )

        assert result["current_stage"] == "rejected"
        assert result["approval"]["approved"] is False
        assert len(result["itinerary"]) == 2
        assert any(
            rec["plan_id"] == "PLAN-REJECT-001" and rec["decision"]["approved"] is False
            for rec in recorded
        )

    get_settings.cache_clear()
