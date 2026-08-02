"""Conversational requirement-gathering graph.

Multi-turn chat that collects travel requirements via natural dialogue,
then produces a structured PlanRequest when enough info is gathered.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.checkpoint import create_memory_saver
from app.agents.prompts import PARSE_USER_INPUT_SYSTEM
from app.config import get_settings
from app.models.schemas import PlannerConversation, PlanRequest, ProductType, TravelPace
from app.services.model_gateway import ModelGateway

logger = logging.getLogger(__name__)

CONFIRMATION_MARKER = "请确认以上需求"

class ChatState(TypedDict, total=False):
    thread_id: str
    messages: list[dict[str, str]]
    conversation: PlannerConversation
    plan_request: dict[str, Any] | None
    ready: bool
    stage: str
    reply: str


async def chat_node(state: ChatState) -> dict:
    settings = get_settings()
    messages = state.get("messages", [])

    if settings.mock_model_mode:
        conversation = _mock_conversation(messages)
    else:
        gateway = ModelGateway(settings)
        history_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '顾问'}：{m['content']}"
            for m in messages[-20:]
        )
        conversation = await gateway.structured_completion(
            model=settings.llm_model_simple,
            system_prompt=PARSE_USER_INPUT_SYSTEM,
            user_prompt=(
                f"当前日期：{datetime.now(UTC).astimezone().date().isoformat()}"
                f"\n\n对话历史：\n{history_text}"
            ),
            schema=PlannerConversation,
            temperature=0.4,
            timeout_seconds=30,
        )

    return _build_chat_result(conversation, messages)


def _mock_conversation(messages: list[dict[str, str]]) -> PlannerConversation:
    user_turns = sum(message.get("role") == "user" for message in messages)
    confirmed = user_turns >= 3 and _has_explicit_confirmation(messages)
    if confirmed:
        stage = "ready"
        question = ""
    elif user_turns >= 2:
        stage = "confirming"
        question = ""
    else:
        stage = "notes"
        question = "必要信息已经齐全。还有没有其他注意事项或需要避免的情况？"
    return PlannerConversation(
        ready=confirmed,
        stage=stage,
        question=question,
        departure_date="2026-10-01",
        departure_time_note="2026 年10 月1 日",
        notes_collected=user_turns >= 2,
        user_confirmed=confirmed,
        title="Mock 旅行方案",
        destination="杭州",
        days=3,
        nights=2,
        group_size=2,
        budget_per_person=3000,
        target_audience="家庭游客",
    )


def _build_chat_result(
    conversation: PlannerConversation,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    stage = conversation.stage
    plan_request: dict[str, Any] | None = None
    if stage in {"confirming", "ready"}:
        has_departure_time = bool(
            conversation.departure_date.strip()
            or conversation.departure_time_note.strip()
        )
        if not has_departure_time:
            stage = "collecting"
        elif not conversation.notes_collected:
            stage = "notes"
        else:
            candidate = _conversation_to_request(conversation)
            try:
                PlanRequest.model_validate(candidate)
                plan_request = candidate
            except ValueError as exc:
                logger.warning("Conversation produced an invalid plan request: %s", exc)
                stage = "collecting"

    explicitly_confirmed = _has_explicit_confirmation(messages)
    ready = bool(
        plan_request
        and stage in {"confirming", "ready"}
        and explicitly_confirmed
    )
    if ready:
        stage = "ready"
    elif stage == "ready":
        stage = "confirming"

    conversation = conversation.model_copy(
        update={
            "ready": ready,
            "stage": stage,
            "user_confirmed": ready,
        }
    )
    reply = _conversation_reply(conversation, plan_request)
    result: dict[str, Any] = {
        "conversation": conversation,
        "ready": ready,
        "stage": stage,
        "reply": reply,
        "messages": [*messages, {"role": "assistant", "content": reply}],
    }
    if plan_request:
        result["plan_request"] = plan_request
    return result


def _conversation_reply(
    conversation: PlannerConversation,
    plan_request: dict[str, Any] | None,
) -> str:
    if conversation.stage == "ready":
        return "需求已确认，我现在开始完整策划。"
    if conversation.stage == "confirming" and plan_request:
        return _format_confirmation(plan_request)
    if conversation.stage == "notes":
        return conversation.question or "还有没有其他注意事项或需要避免的情况？"
    return conversation.question or _missing_requirement_question(conversation)


def _format_confirmation(request: dict[str, Any]) -> str:
    departure = request.get("departure_date") or request.get("departure_time_note") or "待确认"
    notes = [
        *request.get("hard_constraints", []),
        *request.get("soft_preferences", []),
        *request.get("avoid", []),
    ]
    notes_text = "、".join(notes) if notes else "无额外注意事项"
    return (
        f"{CONFIRMATION_MARKER}：\n"
        f"目的地：{request['destination']}\n"
        f"出发时间：{departure}\n"
        f"行程：{request['days']} 天 {request['nights']} 晚\n"
        f"出行人数：{request['group_size']} 人（{request['target_audience']}）\n"
        f"人均预算：¥{request['budget_per_person']:,}\n"
        f"其他注意事项：{notes_text}\n"
        "确认后我才会开始生成旅行攻略。"
    )


def _missing_requirement_question(conversation: PlannerConversation) -> str:
    if len(conversation.destination.strip()) < 2:
        return "你想去哪个目的地？"
    if not conversation.departure_date.strip() and not conversation.departure_time_note.strip():
        return "大概什么时候出发？具体日期或大致时段都可以。"
    if not conversation.target_audience.strip():
        return "这次都有谁出行，大概多少人？"
    return "还需要补充大致天数和人均预算。"


def _has_explicit_confirmation(messages: list[dict[str, str]]) -> bool:
    latest_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        -1,
    )
    if latest_user_index <= 0:
        return False
    previous_assistant = next(
        (
            message.get("content", "")
            for message in reversed(messages[:latest_user_index])
            if message.get("role") == "assistant"
        ),
        "",
    )
    if CONFIRMATION_MARKER not in previous_assistant:
        return False
    raw = messages[latest_user_index].get("content", "").strip().lower()
    normalized = re.sub(r"[\s，。！！!,.]", "", raw)
    affirmative = {
        "确认",
        "确认并开始",
        "没问题",
        "没有问题",
        "可以开始",
        "就按这个来",
        "按以上需求开始策划",
    }
    return len(normalized) <= 30 and (
        normalized in affirmative
        or normalized.startswith("确认按以上需求")
    )


def _parse_date(value: str) -> str | None:
    """Normalize a YYYY-MM-DD date string; return None if missing/invalid."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _conversation_to_request(conv: PlannerConversation) -> dict:
    try:
        product_type = ProductType(conv.product_type)
    except ValueError:
        product_type = ProductType.FAMILY

    try:
        pace = TravelPace(conv.pace)
    except ValueError:
        pace = TravelPace.MODERATE

    title = conv.title.strip()
    if len(title) < 3:
        title = f"{conv.destination}之旅"

    target_audience = conv.target_audience.strip() or "普通游客"
    if len(target_audience) < 3:
        target_audience = f"{target_audience}游客"

    nights = min(max(0, conv.nights), max(0, conv.days - 1))

    return {
        "title": title,
        "departure_date": _parse_date(conv.departure_date),
        "departure_time_note": conv.departure_time_note.strip() or conv.departure_date.strip(),
        "product_type": product_type.value,
        "destination": conv.destination,
        "days": conv.days,
        "nights": nights,
        "group_size": conv.group_size,
        "budget_per_person": conv.budget_per_person,
        "target_margin_rate": conv.target_margin_rate,
        "target_audience": target_audience,
        "themes": conv.themes,
        "pace": pace.value,
        "transport_preferences": conv.transport_preferences or ["public_transit", "walking"],
        "interests": conv.interests,
        "must_visit": conv.must_visit,
        "avoid": conv.avoid,
        "hard_constraints": conv.hard_constraints,
        "soft_preferences": conv.soft_preferences,
        "assumptions": conv.assumptions,
    }


def build_chat_graph(checkpointer: MemorySaver | None = None):
    builder = StateGraph(ChatState)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer or create_memory_saver())
