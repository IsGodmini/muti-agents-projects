from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ProductType(StrEnum):
    FAMILY = "family_trip"
    STUDY = "study_tour"
    CORPORATE = "corporate_team_building"
    SENIOR = "senior_friendly"


class TravelPace(StrEnum):
    INTENSE = "intense"
    MODERATE = "moderate"
    RELAXED = "relaxed"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    DELIVERED = "delivered"
    FAILED = "failed"


class PlanRequest(BaseModel):
    """TripSpec: structured travel requirement specification."""

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

    pace: TravelPace = Field(default=TravelPace.MODERATE, description="旅行节奏")
    transport_preferences: list[str] = Field(
        default_factory=lambda: ["public_transit", "walking"],
        description="交通偏好: walking/public_transit/driving/charter",
    )
    interests: list[str] = Field(default_factory=list, description="兴趣标签")
    must_visit: list[str] = Field(default_factory=list, description="必去地点")
    avoid: list[str] = Field(default_factory=list, description="避雷/不感兴趣")

    hard_constraints: list[str] = Field(
        default_factory=list, description="硬约束：不能违反"
    )
    soft_preferences: list[str] = Field(
        default_factory=list, description="软偏好：尽量满足"
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Agent 做出的假设，需用户确认"
    )

    @property
    def constraints(self) -> list[str]:
        return self.hard_constraints + self.soft_preferences

    @property
    def required_resources(self) -> list[str]:
        return self.must_visit

    @property
    def excluded_resources(self) -> list[str]:
        return self.avoid

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
    images: list[str] = Field(default_factory=list)

    lng: float | None = Field(default=None, description="经度 (来自高德地图)")
    lat: float | None = Field(default=None, description="纬度 (来自高德地图)")

    interest_score: float = Field(default=0.5, ge=0, le=1, description="兴趣匹配度")
    crowd_risk: str = Field(default="unknown", description="low/medium/high/unknown")
    weather_dependency: str = Field(default="low", description="low/medium/high")
    composite_score: float = Field(default=0.0, description="综合排序得分")


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
    severity: str = Field(description="blocking / warning / info")
    message: str
    event_title: str | None = None
    suggested_action: str | None = None


class ConstraintReport(BaseModel):
    valid: bool
    score: int = Field(ge=0, le=100)
    issues: list[ConstraintIssue] = Field(default_factory=list)
    total_travel_minutes: int
    max_daily_minutes: int
    must_visit_coverage: float = Field(default=0.0, ge=0, le=1, description="必去地点覆盖率")
    budget_accuracy: float = Field(default=0.0, description="预算偏差率")
    time_conflict_count: int = Field(default=0, description="时间冲突数")


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




class ToolSummary(BaseModel):
    name: str
    description: str
    risk_level: str
    category: str


# ---------- LLM structured-output schemas ----------


class EnrichedResourceInfo(BaseModel):
    index: int = Field(default=-1, description="资源在输入列表中的序号，从 0 开始")
    category: str
    estimated_price_per_person: int = Field(ge=0)
    recommended_minutes: int = Field(default=120, ge=0, le=480)
    opening_hours: str
    highlights: str


class ResourceEnrichmentBatch(BaseModel):
    resources: list[EnrichedResourceInfo]


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
    time: int = Field(
        ge=5, le=300,
        description="预估交通时间（分钟）",
        validation_alias=AliasChoices("time", "duration", "travel_time", "duration_min"),
    )


class TravelTimeMatrix(BaseModel):
    pairs: list[TravelTimePair] = Field(default_factory=list)


class ScheduledEvent(BaseModel):
    resource_id: str = ""
    title: str = ""
    start_time: str = Field(description="HH:MM 格式")
    end_time: str = Field(description="HH:MM 格式")
    category: str = "activity"
    description: str = ""
    cost_per_person: int = Field(ge=0, default=0)
    activity_name: str = Field(
        default="",
        validation_alias=AliasChoices("activity_name", "name", "activity", "title"),
    )

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def backfill_title(self) -> ScheduledEvent:
        if not self.title and self.activity_name:
            self.title = self.activity_name
        return self


class DailySchedule(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    day: int
    theme: str = ""
    events: list[ScheduledEvent] = Field(
        validation_alias=AliasChoices("events", "activities", "schedule"),
    )


class ScheduleBatch(BaseModel):
    days: list[DailySchedule]


class CostItemEstimate(BaseModel):
    category: str
    description: str = ""
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
    pace: str = Field(default="moderate", description="intense/moderate/relaxed")
    interests: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    transport_preferences: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, description="Agent 做出的假设，需用户确认")
