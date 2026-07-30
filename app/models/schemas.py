from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProductType(StrEnum):
    FAMILY = "family_trip"
    STUDY = "study_tour"
    CORPORATE = "corporate_team_building"
    SENIOR = "senior_friendly"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    DELIVERED = "delivered"
    FAILED = "failed"


class PlanRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    product_type: ProductType
    destination: str = Field(min_length=2, max_length=80)
    days: int = Field(ge=1, le=15)
    nights: int = Field(ge=0, le=14)
    group_size: int = Field(ge=1, le=500)
    budget_per_person: int = Field(ge=100, le=100_000)
    target_margin_rate: float = Field(default=0.15, ge=0, le=0.8)
    target_audience: str = Field(min_length=3, max_length=300)
    themes: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    required_resources: list[str] = Field(default_factory=list)
    excluded_resources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_nights(self) -> PlanRequest:
        if self.nights >= self.days:
            raise ValueError("nights must be less than days")
        return self


class ResourceCandidate(BaseModel):
    id: str
    name: str
    category: str
    location: str
    price_per_person: int = 0
    recommended_minutes: int
    opening_hours: str
    audience_tags: list[str] = Field(default_factory=list)
    evidence: str
    score: float = Field(ge=0, le=1)
    source_url: str | None = None
    source_title: str | None = None
    retrieved_at: datetime | None = None
    provider: str = "internal"
    summary: str | None = None


class ItineraryEvent(BaseModel):
    start_time: str
    end_time: str
    title: str
    resource_id: str | None = None
    category: str
    description: str
    cost_per_person: int = 0


class ItineraryDay(BaseModel):
    day: int
    theme: str
    events: list[ItineraryEvent]


class ConstraintIssue(BaseModel):
    code: str
    severity: str
    message: str
    event_title: str | None = None
    suggested_action: str | None = None


class ConstraintReport(BaseModel):
    valid: bool
    score: int = Field(ge=0, le=100)
    issues: list[ConstraintIssue] = Field(default_factory=list)
    total_travel_minutes: int
    max_daily_minutes: int


class QuoteItem(BaseModel):
    category: str
    description: str
    amount: int


class Quote(BaseModel):
    items: list[QuoteItem]
    total_cost: int
    cost_per_person: int
    sale_price_per_person: int
    expected_revenue: int
    expected_profit: int
    margin_rate: float


class QualityReport(BaseModel):
    overall_score: int = Field(ge=0, le=100)
    fact_traceability_score: int = Field(ge=0, le=100)
    feasibility_score: int = Field(ge=0, le=100)
    audience_fit_score: int = Field(ge=0, le=100)
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class PosterBrief(BaseModel):
    destination: str
    product_theme: str
    target_audience: str
    visual_style: str
    primary_colors: list[str]
    visual_elements: list[str]
    negative_elements: list[str]
    aspect_ratio: str = "3:4"


class ApprovalDecision(BaseModel):
    approved: bool
    reviewer_id: str
    comment: str | None = None


class PlanRunResponse(BaseModel):
    thread_id: str
    status: PlanStatus
    current_stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SkillSummary(BaseModel):
    name: str
    version: str
    description: str
    allowed_tools: list[str]
    approval_required: bool


class ToolSummary(BaseModel):
    name: str
    description: str
    risk_level: str
    category: str


# ---------- LLM structured-output schemas ----------


class RequirementAnalysis(BaseModel):
    selected_skill: str
    requirements_complete: bool
    missing_fields: list[str] = Field(default_factory=list)
    extracted_constraints: list[str] = Field(default_factory=list)
    audience_notes: str = ""


class EnrichedResourceInfo(BaseModel):
    index: int = Field(description="资源在输入列表中的序号，从 0 开始")
    category: str
    estimated_price_per_person: int = Field(ge=0)
    recommended_minutes: int = Field(ge=30, le=480)
    opening_hours: str
    highlights: str


class ResourceEnrichmentBatch(BaseModel):
    resources: list[EnrichedResourceInfo]


class EventEnrichment(BaseModel):
    resource_id: str
    description: str
    practical_tips: str = ""


class DayEnrichment(BaseModel):
    day: int
    theme: str
    events: list[EventEnrichment]


class ItineraryEnrichmentBatch(BaseModel):
    days: list[DayEnrichment]


class QualityAssessment(BaseModel):
    overall_score: int = Field(ge=0, le=100)
    fact_traceability_score: int = Field(ge=0, le=100)
    feasibility_score: int = Field(ge=0, le=100)
    audience_fit_score: int = Field(ge=0, le=100)
    blocking_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


# ---------- LLM real-data schemas ----------


class TravelTimePair(BaseModel):
    from_index: int = Field(description="出发资源序号")
    to_index: int = Field(description="到达资源序号")
    time: int = Field(ge=5, le=300, description="预估交通时间（分钟）")


class TravelTimeMatrix(BaseModel):
    pairs: list[TravelTimePair]


class ScheduledEvent(BaseModel):
    resource_id: str
    title: str
    start_time: str = Field(description="HH:MM 格式")
    end_time: str = Field(description="HH:MM 格式")
    category: str
    description: str
    practical_tips: str = ""
    cost_per_person: int = Field(ge=0, default=0)


class DailySchedule(BaseModel):
    day: int
    theme: str
    events: list[ScheduledEvent]


class ScheduleBatch(BaseModel):
    days: list[DailySchedule]


class CostItemEstimate(BaseModel):
    category: str
    description: str
    amount: int = Field(ge=0)


class CostBreakdown(BaseModel):
    items: list[CostItemEstimate]
    cost_notes: str = ""


class PlannerConversation(BaseModel):
    ready: bool = Field(description="信息是否足够开始策划")
    question: str = Field(default="", description="不够时向用户提的下一个问题")
    title: str = Field(default="", description="产品名称")
    product_type: str = Field(default="family_trip", description="family_trip/study_tour/corporate_team_building/senior_friendly")
    destination: str = Field(default="")
    days: int = Field(default=3, ge=1, le=15)
    nights: int = Field(default=2, ge=0, le=14)
    group_size: int = Field(default=2, ge=1, le=500)
    budget_per_person: int = Field(default=2000, ge=100, le=100_000)
    target_margin_rate: float = Field(default=0.15, ge=0, le=0.8)
    target_audience: str = Field(default="")
    themes: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
