"""Conversational requirement-gathering graph.

Multi-turn chat that collects travel requirements via natural dialogue,
then produces a structured PlanRequest when enough info is gathered.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.prompts import PARSE_USER_INPUT_SYSTEM
from app.config import get_settings
from app.models.schemas import PlannerConversation, ProductType, TravelPace
from app.services.model_gateway import ModelGateway

logger = logging.getLogger(__name__)



class ChatState(TypedDict, total=False):
    thread_id: str
    messages: list[dict[str, str]]
    conversation: PlannerConversation
    plan_request: dict[str, Any] | None
    ready: bool


async def chat_node(state: ChatState) -> dict:
    settings = get_settings()
    messages = state.get("messages", [])

    if settings.mock_model_mode:
        return {
            "ready": True,
            "conversation": PlannerConversation(
                ready=True,
                question="",
                title="Mock 旅行方案",
                destination="杭州",
                days=3,
                nights=2,
                group_size=2,
                budget_per_person=3000,
                target_audience="家庭",
            ),
        }

    gateway = ModelGateway(settings)
    history_text = "\n".join(
        f"{'用户' if m['role'] == 'user' else '顾问'}：{m['content']}"
        for m in messages[-20:]
    )

    conversation = await gateway.structured_completion(
        model=settings.llm_model_simple,
        system_prompt=PARSE_USER_INPUT_SYSTEM,
        user_prompt=f"对话历史：\n{history_text}",
        schema=PlannerConversation,
        temperature=0.4,
        timeout_seconds=30,
    )

    result: dict = {
        "conversation": conversation,
        "ready": conversation.ready,
    }

    if conversation.ready:
        result["plan_request"] = _conversation_to_request(conversation)

    return result


def _conversation_to_request(conv: PlannerConversation) -> dict:
    try:
        product_type = ProductType(conv.product_type)
    except ValueError:
        product_type = ProductType.FAMILY

    try:
        pace = TravelPace(conv.pace)
    except ValueError:
        pace = TravelPace.MODERATE

    return {
        "title": conv.title or f"{conv.destination}之旅",
        "product_type": product_type.value,
        "destination": conv.destination,
        "days": conv.days,
        "nights": conv.nights,
        "group_size": conv.group_size,
        "budget_per_person": conv.budget_per_person,
        "target_margin_rate": conv.target_margin_rate,
        "target_audience": conv.target_audience,
        "themes": conv.themes,
        "pace": pace.value,
        "transport_preferences": conv.transport_preferences or ["public_transit", "walking"],
        "interests": conv.interests,
        "must_visit": conv.must_visit,
        "avoid": conv.avoid,
        "hard_constraints": conv.hard_constraints,
        "soft_preferences": conv.soft_preferences,
        "assumptions": conv.assumptions if hasattr(conv, "assumptions") else [],
    }


def build_chat_graph(checkpointer: MemorySaver | None = None):
    builder = StateGraph(ChatState)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer or MemorySaver())
