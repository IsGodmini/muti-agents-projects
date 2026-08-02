import pytest

from app.agents.graph import _llm_cost_estimation
from app.config import get_settings
from app.models.schemas import CostBreakdown, CostItemEstimate, PlanRequest
from app.services.model_gateway import ModelGateway


def _request() -> PlanRequest:
    return PlanRequest(
        title="杭州秋日之旅",
        product_type="family_trip",
        destination="杭州",
        departure_date="2026-10-01",
        departure_time_note="2026 年10 月1 日",
        days=4,
        nights=3,
        group_size=5,
        budget_per_person=4000,
        target_audience="朋友出游",
    )


@pytest.mark.asyncio
async def test_zero_cost_items_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_completion(self, **kwargs):
        return CostBreakdown(
            items=[
                CostItemEstimate(category="免费项", description="已包含", amount=0),
                CostItemEstimate(category="住宿", description="团队住宿", amount=3600),
            ]
        )

    monkeypatch.setattr(ModelGateway, "structured_completion", fake_completion)
    quote = await _llm_cost_estimation(
        get_settings(),
        {"request": _request(), "resources": []},
    )

    assert quote.total_cost == 3600
    assert len(quote.items) == 1
    assert all(item.amount > 0 for item in quote.items)


@pytest.mark.asyncio
async def test_all_zero_cost_items_use_nonzero_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_completion(self, **kwargs):
        return CostBreakdown(
            items=[
                CostItemEstimate(category="门票", description="免费", amount=0),
                CostItemEstimate(category="服务", description="已包含", amount=0),
            ]
        )

    monkeypatch.setattr(ModelGateway, "structured_completion", fake_completion)
    quote = await _llm_cost_estimation(
        get_settings(),
        {"request": _request(), "resources": []},
    )

    assert quote.total_cost > 0
    assert len(quote.items) >= 3
    assert all(item.amount > 0 for item in quote.items)
