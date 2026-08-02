"""Safe LangGraph checkpoint serializer configuration."""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.models.schemas import (
    ConstraintIssue,
    ConstraintReport,
    ItineraryDay,
    ItineraryEvent,
    PlannerConversation,
    PlanRequest,
    PosterBrief,
    ProductType,
    QualityReport,
    Quote,
    QuoteItem,
    ResourceCandidate,
    TravelPace,
)

_CHECKPOINT_TYPES = (
    ProductType,
    TravelPace,
    PlanRequest,
    PlannerConversation,
    ResourceCandidate,
    ItineraryEvent,
    ItineraryDay,
    ConstraintIssue,
    ConstraintReport,
    QuoteItem,
    Quote,
    QualityReport,
    PosterBrief,
)


def create_memory_saver() -> MemorySaver:
    """Create an in-memory saver with an explicit application-type allowlist."""
    serializer = JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES)
    return MemorySaver(serde=serializer)
