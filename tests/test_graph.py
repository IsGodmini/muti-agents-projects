"""Test workflow logic with mocked external services."""

from unittest.mock import patch

import pytest

from app.agents.graph import build_planning_graph
from app.models.schemas import (
    CostBreakdown,
    CostItemEstimate,
    DailySchedule,
    PlanRequest,
    ProductType,
    QualityAssessment,
    RequirementAnalysis,
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


async def _mock_structured_completion(self, *, schema, **kwargs):
    """Return appropriate test data based on the requested schema."""
    if schema is RequirementAnalysis:
        return RequirementAnalysis(requirements_complete=True)
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
        return ScheduleBatch(days=[
            DailySchedule(day=1, theme="自然探索", events=[
                ScheduledEvent(
                    resource_id="res-1", title="测试博物馆",
                    start_time="09:00", end_time="11:00",
                    category="museum", description="参观测试博物馆。",
                    cost_per_person=30,
                ),
                ScheduledEvent(
                    resource_id="res-2", title="测试湿地",
                    start_time="13:00", end_time="16:00",
                    category="outdoor", description="湿地自然观察。",
                    cost_per_person=80,
                ),
            ]),
            DailySchedule(day=2, theme="手作体验", events=[
                ScheduledEvent(
                    resource_id="res-3", title="测试工坊",
                    start_time="09:30", end_time="11:00",
                    category="workshop", description="手作体验课程。",
                    cost_per_person=60,
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
        assert result["quote"].total_cost == 26000
        assert result["verification_score"] >= 60
        assert result["poster_asset"]["status"] == "generated"
        assert "report_path" in result

    get_settings.cache_clear()


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
        assert any(
            rec["plan_id"] == "PLAN-REJECT-001" and rec["decision"]["approved"] is False
            for rec in recorded
        )

    get_settings.cache_clear()
