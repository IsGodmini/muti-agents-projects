from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api import routes
from app.main import app
from app.models.schemas import PlannerConversation


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


async def _complete_chat(
    client: httpx.AsyncClient,
    initial_message: str,
) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
    first = await client.post("/api/v1/chat", json={"message": initial_message})
    thread_id = first.json()["thread_id"]
    second = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "没有其他注意事项"},
    )
    third = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "确认，按以上需求开始策划"},
    )
    return first, second, third


@pytest.mark.asyncio
async def test_chat_builds_ready_plan_request(client: httpx.AsyncClient) -> None:
    async with client:
        notes_response, confirmation_response, response = await _complete_chat(
            client,
            "2026 年10 月1 日，5 人去杭州 5 天，人均预算 4000 元",
        )

    assert notes_response.json()["stage"] == "notes"
    assert notes_response.json()["ready"] is False
    assert "其他注意事项" in notes_response.json()["reply"]
    assert confirmation_response.json()["stage"] == "confirming"
    assert confirmation_response.json()["ready"] is False
    assert confirmation_response.json()["plan_request"]["departure_time_note"]
    assert "请确认以上需求" in confirmation_response.json()["reply"]
    assert response.status_code == 200
    payload = response.json()
    assert payload["thread_id"]
    assert payload["ready"] is True
    assert payload["stage"] == "ready"
    assert payload["plan_request"]["destination"]
    assert payload["reply"] == "需求已确认，我现在开始完整策划。"

    state = await routes.chat_graph.aget_state(
        {"configurable": {"thread_id": payload["thread_id"]}}
    )
    assert state.values["messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_direct_plan_routes_are_removed(client: httpx.AsyncClient) -> None:
    async with client:
        tools_response = await client.get("/api/v1/tools")
        run_response = await client.post("/api/v1/plans/run", json={})
        stream_response = await client.post("/api/v1/plans/run/stream", json={})

    assert tools_response.status_code == 404
    assert run_response.status_code == 404
    assert stream_response.status_code == 404


@pytest.mark.asyncio
async def test_plan_stream_rejects_unknown_conversation(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.post("/api/v1/chat/unknown/plan/stream")

    assert response.status_code == 409
    assert response.json()["detail"] == "对话信息尚未收集完整，不能启动策划。"


@pytest.mark.asyncio
async def test_confirmation_stage_cannot_start_plan(client: httpx.AsyncClient) -> None:
    async with client:
        first = await client.post(
            "/api/v1/chat",
            json={"message": "2026 年9 月1 日，4 人去内蒙古 4 天，人均 3000 元"},
        )
        thread_id = first.json()["thread_id"]
        confirmation = await client.post(
            "/api/v1/chat",
            json={"thread_id": thread_id, "message": "没有其他注意事项"},
        )
        response = await client.post(f"/api/v1/chat/{thread_id}/plan/stream")

    assert confirmation.json()["stage"] == "confirming"
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_chat_failure_is_returned_as_json(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingChatGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(values={})

        async def ainvoke(self, *_args, **_kwargs):
            raise ValueError("validation error for PlannerConversation stage")

    monkeypatch.setattr(routes, "chat_graph", FailingChatGraph())
    async with client:
        response = await client.post(
            "/api/v1/chat",
            json={"message": "确认需求"},
        )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "需求理解结果格式不完整，请重新表述刚才的回答。"


@pytest.mark.asyncio
async def test_ready_conversation_can_start_stream(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePlanningGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {"event": "on_chain_start", "name": "retrieve_resources"}
            yield {"event": "on_chain_end", "name": "retrieve_resources"}

        async def aget_state(self, _config):
            return SimpleNamespace(values={"current_stage": "poster_generated"})

    async with client:
        _, _, chat_response = await _complete_chat(
            client,
            "2026 年9 月1 日，4 人去内蒙古 4 天，人均预算 3000 元",
        )
        thread_id = chat_response.json()["thread_id"]
        monkeypatch.setattr(routes, "graph", FakePlanningGraph())
        response = await client.post(f"/api/v1/chat/{thread_id}/plan/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: started" in body
    assert "event: node_start" in body
    assert "event: completed" in body
    assert '"id": "retrieve_resources"' in body
    assert '"activities"' in body
    assert '"workflow"' in body
    assert '"requires_approval": true' in body


@pytest.mark.asyncio
async def test_failed_plan_returns_user_facing_recovery_reasons(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFailedGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {"event": "on_chain_start", "name": "mark_failed"}
            yield {"event": "on_chain_end", "name": "mark_failed"}

        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "current_stage": "failed",
                    "errors": ["方案在最大重试次数内未通过校验。"],
                }
            )

    async with client:
        _, _, chat_response = await _complete_chat(
            client,
            "2026 年10 月1 日，5 人去杭州 5 天，人均预算 4000 元",
        )
        thread_id = chat_response.json()["thread_id"]
        monkeypatch.setattr(routes, "graph", FakeFailedGraph())
        response = await client.post(f"/api/v1/chat/{thread_id}/plan/stream")

    assert response.status_code == 200
    assert '"current_stage": "failed"' in response.text
    assert '"recoverable": true' in response.text
    assert '"failure_reasons"' in response.text
    assert "最大重试次数内未通过校验" in response.text


def test_frontend_only_exposes_conversation_entry() -> None:
    html_path = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert "直接策划" not in html
    assert "/plans/run" not in html
    assert "/api/v1/chat/" in html
    assert "旅行顾问对话" in html
    assert "旅行方案生成流程" in html
    assert 'id="current-execution"' in html
    assert "clearCurrentNode();" in html
    assert "enableFailureRecovery" in html
    assert "readResponseJson" in html
    assert "调整预算" in html
    assert "修改后我会重新整理摘要" in html


def test_cost_validation_exception_is_explained_without_raw_traceback() -> None:
    message = routes._friendly_workflow_exception(
        ValueError("2 validation errors for CostBreakdown: amount greater than 0"),
        "calculate_quote",
    )

    assert message == "核算成本未能完成：成本明细格式不完整。"
    assert "validation errors" not in message


@pytest.mark.parametrize(
    ("raw_stage", "expected"),
    [
        ("收集信息", "collecting"),
        ("注意事项", "notes"),
        ("等待确认", "confirming"),
        ("已确认", "ready"),
        ("confirmed", "ready"),
    ],
)
def test_planner_conversation_normalizes_model_stage(
    raw_stage: str,
    expected: str,
) -> None:
    conversation = PlannerConversation(ready=False, stage=raw_stage)

    assert conversation.stage == expected
