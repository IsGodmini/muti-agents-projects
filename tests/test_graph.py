"""Test workflow logic with mocked external services."""

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.types import Command

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
        price_per_person=30, recommended_time=120, opening_hours="09:00-17:00",
        audience_tags=["亲子"], evidence="test", score=0.9, provider="tavily_mcp",
    ),
    ResourceCandidate(
        id="res-2", name="测试湿地", category="outdoor", location="杭州",
        price_per_person=80, recommended_time=180, opening_hours="08:30-17:30",
        audience_tags=["自然教育"], evidence="test", score=0.85, provider="tavily_mcp",
    ),
    ResourceCandidate(
        id="res-3", name="测试工坊", category="workshop", location="杭州",
        price_per_person=60, recommended_time=90, opening_hours="09:00-18:00",
        audience_tags=["手作"], evidence="test", score=0.8, provider="tavily_mcp",
    ),
]


async def _mock_structured_completion(self, *, schema, **kwargs):
    """Return appropriate test data based on the requested schema."""
    if schema is RequirementAnalysis:
        return RequirementAnalysis(
            selected_skill="family_trip_planning",
            requirements_complete=True,
        )
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
async def test_graph_pauses_for_approval_and_resumes_to_delivery(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_MODEL_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    with (
        patch(
            "app.agents.graph.tool_registry.ainvoke",
            new_callable=AsyncMock,
            return_value=TEST_RESOURCES,
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
            constraints=["连续乘车不超过90分钟"],
        )

        paused = await graph.ainvoke(
            {
                "thread_id": "test-family-plan",
                "plan_id": "PLAN-TEST-001",
                "request": request,
                "current_stage": "created",
            },
            config=config,
        )

        assert paused["current_stage"] == "waiting_approval"
        assert "__interrupt__" in paused
        assert paused["constraint_report"].valid is True
        assert paused["quote"].total_cost == 26000

        delivered = await graph.ainvoke(
            Command(resume={
                "approved": True,
                "reviewer_id": "product-manager-01",
                "comment": "方案通过",
            }),
            config=config,
        )

        assert delivered["current_stage"] == "delivered"
        assert delivered["poster_asset"]["status"] == "generated"

    get_settings.cache_clear()
