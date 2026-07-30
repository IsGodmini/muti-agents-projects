"""LangGraph 工作流：每个节点直接绑定所需的 Tools。

节点 → Tools 绑定：
  parse_requirements   → (LLM only)
  retrieve_resources   → search_attractions, (LLM enrichment)
  plan_itinerary       → calculate_route_matrix, optimize_itinerary, (LLM scheduling)
  validate_constraints → (deterministic)
  repair_plan          → search_attractions (emergency alternatives)
  calculate_quote      → calculate_product_cost, (LLM estimation)
  quality_review       → (LLM only)
  approval_gate        → (human interrupt)
  prepare_poster       → PosterService
  finalize_delivery    → save_plan_version, submit_for_approval
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.prompts import (
    COST_ESTIMATION_SYSTEM,
    QUALITY_REVIEW_SYSTEM,
    REQUIREMENT_ANALYSIS_SYSTEM,
    RESOURCE_ENRICHMENT_SYSTEM,
    SCHEDULE_SYSTEM,
    TRAVEL_TIME_SYSTEM,
)
from app.agents.state import PlanningState
from app.config import get_settings
from app.models.schemas import (
    ConstraintIssue,
    ConstraintReport,
    CostBreakdown,
    ItineraryDay,
    ItineraryEvent,
    PosterBrief,
    QualityAssessment,
    QualityReport,
    QuoteItem,
    RequirementAnalysis,
    ResourceEnrichmentBatch,
    ScheduleBatch,
    TravelTimeMatrix,
)
from app.services.model_gateway import ModelGateway
from app.services.plan_store import plan_store
from app.services.poster import PosterService
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import tool_registry

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# parse_requirements → LLM only
# ------------------------------------------------------------------

async def parse_requirements(state: PlanningState) -> dict:
    request = state["request"]
    settings = get_settings()

    if not settings.mock_model_mode:
        try:
            gateway = ModelGateway(settings)
            analysis = await gateway.structured_completion(
                system_prompt=REQUIREMENT_ANALYSIS_SYSTEM,
                user_prompt=(
                    f"产品类型：{request.product_type.value}\n"
                    f"标题：{request.title}\n"
                    f"目的地：{request.destination}\n"
                    f"天数：{request.days} 天 {request.nights} 晚\n"
                    f"团队规模：{request.group_size} 人\n"
                    f"预算：{request.budget_per_person} 元/人\n"
                    f"目标人群：{request.target_audience}\n"
                    f"主题：{', '.join(request.themes)}\n"
                    f"约束：{', '.join(request.constraints) or '无'}"
                ),
                schema=RequirementAnalysis,
            )
            logger.info("LLM requirement analysis: skill=%s", analysis.selected_skill)
            return {
                "requirements_complete": analysis.requirements_complete,
                "missing_fields": analysis.missing_fields,
                "current_stage": "requirements_parsed",
                "retry_count": 0,
                "errors": [],
            }
        except Exception:
            logger.warning("LLM requirement analysis failed", exc_info=True)

    return {
        "requirements_complete": True,
        "missing_fields": [],
        "current_stage": "requirements_parsed",
        "retry_count": 0,
        "errors": [],
    }


# ------------------------------------------------------------------
# retrieve_resources → search_attractions + LLM enrichment
# ------------------------------------------------------------------

async def retrieve_resources(state: PlanningState) -> dict:
    request = state["request"]
    settings = get_settings()

    resources = await tool_registry.ainvoke(
        "search_attractions",
        {
            "destination": request.destination,
            "themes": request.themes,
            "audience": request.target_audience,
            "limit": 8,
        },
    )

    if not settings.mock_model_mode and resources:
        try:
            resources = await _enrich_resources(settings, resources)
        except Exception:
            logger.warning("LLM resource enrichment failed", exc_info=True)

    return {
        "resources": resources,
        "resource_search_provider": resources[0].provider if resources else "none",
        "route_matrix": {},
        "current_stage": "resources_retrieved",
    }


async def _enrich_resources(settings, resources):
    gateway = ModelGateway(settings)
    descriptions = "\n".join(
        f"[{i}] 标题：{r.name}\n    摘要：{(r.summary or r.evidence or '')[:300]}\n    来源：{r.source_url or ''}"
        for i, r in enumerate(resources)
    )
    batch = await gateway.structured_completion(
        system_prompt=RESOURCE_ENRICHMENT_SYSTEM,
        user_prompt=f"目的地资源列表：\n{descriptions}",
        schema=ResourceEnrichmentBatch,
        timeout_seconds=45,
    )
    enrichment_map = {item.index: item for item in batch.resources}
    enriched = []
    for i, resource in enumerate(resources):
        info = enrichment_map.get(i)
        if info:
            resource = resource.model_copy(update={
                "category": info.category,
                "price_per_person": info.estimated_price_per_person,
                "recommended_minutes": info.recommended_minutes,
                "opening_hours": info.opening_hours,
                "summary": info.highlights,
            })
        enriched.append(resource)
    logger.info("LLM enriched %d/%d resources", len(enrichment_map), len(resources))
    return enriched


# ------------------------------------------------------------------
# plan_itinerary → calculate_route_matrix + optimize_itinerary + LLM
# ------------------------------------------------------------------

async def plan_itinerary(state: PlanningState) -> dict:
    resources = state["resources"]
    request = state["request"]
    settings = get_settings()

    resource_ids = [r.id for r in resources]
    resource_map = {r.id: r for r in resources}

    travel_times = await _estimate_travel_times(settings, resources)

    route_matrix = tool_registry.invoke(
        "calculate_route_matrix",
        {"resource_ids": resource_ids, "travel_times": travel_times},
    )
    optimized = tool_registry.invoke(
        "optimize_itinerary",
        {
            "resource_ids": resource_ids,
            "distance_matrix": route_matrix,
            "days": request.days,
            "max_daily_minutes": 480,
        },
    )

    if not settings.mock_model_mode:
        try:
            days = await _generate_schedule(settings, request, optimized, resource_map)
            return {"itinerary": days, "route_matrix": route_matrix, "current_stage": "itinerary_planned"}
        except Exception:
            logger.warning("LLM schedule generation failed; using basic layout", exc_info=True)

    days = _basic_schedule(request, optimized, resource_map)
    return {"itinerary": days, "route_matrix": route_matrix, "current_stage": "itinerary_planned"}


async def _estimate_travel_times(settings, resources) -> dict[str, int]:
    gateway = ModelGateway(settings)
    resource_list = "\n".join(f"[{i}] {r.name}（{r.location}）" for i, r in enumerate(resources))
    matrix = await gateway.structured_completion(
        system_prompt=TRAVEL_TIME_SYSTEM,
        user_prompt=f"资源列表（共 {len(resources)} 个）：\n{resource_list}",
        schema=TravelTimeMatrix,
        timeout_seconds=45,
    )
    travel_times: dict[str, int] = {}
    for pair in matrix.pairs:
        if 0 <= pair.from_index < len(resources) and 0 <= pair.to_index < len(resources):
            travel_times[f"{resources[pair.from_index].id}->{resources[pair.to_index].id}"] = pair.time
    logger.info("LLM estimated %d travel-time pairs", len(travel_times))
    return travel_times


async def _generate_schedule(settings, request, optimized, resource_map) -> list[ItineraryDay]:
    gateway = ModelGateway(settings)
    schedule_input = json.dumps(
        [
            {
                "day": day_index + 1,
                "resources": [
                    {
                        "resource_id": rid,
                        "name": resource_map[rid].name,
                        "category": resource_map[rid].category,
                        "location": resource_map[rid].location,
                        "recommended_minutes": resource_map[rid].recommended_minutes,
                        "price_per_person": resource_map[rid].price_per_person,
                        "opening_hours": resource_map[rid].opening_hours,
                    }
                    for rid in day_resources
                ],
            }
            for day_index, day_resources in enumerate(optimized)
        ],
        ensure_ascii=False,
    )
    batch = await gateway.structured_completion(
        system_prompt=SCHEDULE_SYSTEM,
        user_prompt=(
            f"产品：{request.title}\n目的地：{request.destination}\n"
            f"目标人群：{request.target_audience}\n"
            f"天数：{request.days} 天 {request.nights} 晚\n"
            f"主题：{', '.join(request.themes)}\n"
            f"约束：{', '.join(request.constraints) or '无'}\n\n"
            f"已优化分组：\n{schedule_input}"
        ),
        schema=ScheduleBatch,
        timeout_seconds=60,
    )
    days: list[ItineraryDay] = []
    for day_info in batch.days:
        events = [
            ItineraryEvent(
                start_time=e.start_time, end_time=e.end_time, title=e.title,
                resource_id=e.resource_id, category=e.category,
                description=e.description, cost_per_person=e.cost_per_person,
            )
            for e in day_info.events
        ]
        days.append(ItineraryDay(day=day_info.day, theme=day_info.theme, events=events))
    logger.info("LLM generated schedule for %d days", len(days))
    return days


def _basic_schedule(request, optimized, resource_map) -> list[ItineraryDay]:
    days: list[ItineraryDay] = []
    for day_index, day_resources in enumerate(optimized, start=1):
        events: list[ItineraryEvent] = []
        start_hour = 9
        for rid in day_resources:
            resource = resource_map[rid]
            end_minutes = start_hour * 60 + resource.recommended_minutes
            events.append(ItineraryEvent(
                start_time=f"{start_hour:02d}:00",
                end_time=f"{end_minutes // 60:02d}:{end_minutes % 60:02d}",
                title=resource.name, resource_id=rid, category=resource.category,
                description=f"{resource.summary or resource.name}体验。",
                cost_per_person=resource.price_per_person,
            ))
            start_hour = end_minutes // 60 + 2
        days.append(ItineraryDay(day=day_index, theme=f"{request.destination}探索 · 第 {day_index} 天", events=events))
    return days


# ------------------------------------------------------------------
# validate_constraints → deterministic
# ------------------------------------------------------------------

def validate_constraints(state: PlanningState) -> dict:
    issues: list[ConstraintIssue] = []
    actual_max_daily = 0

    for day in state["itinerary"]:
        if not day.events:
            issues.append(ConstraintIssue(
                code="EMPTY_DAY", severity="blocking",
                message=f"第 {day.day} 天没有活动安排。",
                suggested_action="重新分配候选资源。",
            ))
            continue
        try:
            start = int(day.events[0].start_time[:2]) * 60 + int(day.events[0].start_time[3:])
            end = int(day.events[-1].end_time[:2]) * 60 + int(day.events[-1].end_time[3:])
        except (ValueError, IndexError):
            continue
        day_span = end - start
        actual_max_daily = max(actual_max_daily, day_span)
        if day_span > 600:
            issues.append(ConstraintIssue(
                code="DAILY_DURATION_EXCEEDED", severity="warning",
                message=f"第 {day.day} 天活动跨度 {day_span} 分钟，超过 10 小时。",
                suggested_action="移除低优先级资源或增加休息节点。",
            ))

    route_matrix = state.get("route_matrix", {})
    report = ConstraintReport(
        valid=not any(i.severity == "blocking" for i in issues),
        score=max(70, 100 - len(issues) * 8),
        issues=issues,
        total_travel_minutes=sum(route_matrix.values()) // max(len(route_matrix), 1),
        max_daily_minutes=actual_max_daily,
    )
    return {"constraint_report": report, "current_stage": "constraints_validated"}


def constraint_route(state: PlanningState) -> Literal["quote", "repair", "failed"]:
    if state["constraint_report"].valid:
        return "quote"
    if state.get("retry_count", 0) < 2:
        return "repair"
    return "failed"


# ------------------------------------------------------------------
# repair_plan → search_attractions (emergency alternatives)
# ------------------------------------------------------------------

async def repair_plan(state: PlanningState) -> dict:
    retry = state.get("retry_count", 0) + 1
    request = state["request"]
    settings = get_settings()
    issues = state.get("constraint_report", ConstraintReport(
        valid=False, score=0, issues=[], total_travel_minutes=0, max_daily_minutes=0,
    )).issues

    logger.info("Repair attempt %d: %s", retry, "; ".join(i.message for i in issues[:3]))

    if not settings.mock_model_mode:
        try:
            alternatives = await tool_registry.ainvoke(
                "search_attractions",
                {
                    "destination": request.destination,
                    "themes": [*request.themes, "替代方案", "备选"],
                    "audience": request.target_audience,
                    "limit": 4,
                },
            )
            if alternatives:
                existing_ids = {r.id for r in state.get("resources", [])}
                new_resources = [r for r in alternatives if r.id not in existing_ids]
                if new_resources:
                    return {
                        "retry_count": retry,
                        "resources": list(state.get("resources", [])) + new_resources[:3],
                        "current_stage": "repairing",
                    }
        except Exception:
            logger.warning("Emergency alternative search failed", exc_info=True)

    return {"retry_count": retry, "current_stage": "repairing"}


# ------------------------------------------------------------------
# calculate_quote → calculate_product_cost + LLM estimation
# ------------------------------------------------------------------

async def calculate_quote(state: PlanningState) -> dict:
    settings = get_settings()
    if not settings.mock_model_mode:
        quote = await _llm_cost_estimation(settings, state)
        return {"quote": quote, "current_stage": "quote_calculated"}
    raise RuntimeError("Cost estimation requires LLM.")


async def _llm_cost_estimation(settings, state):
    gateway = ModelGateway(settings)
    request = state["request"]
    resources = state.get("resources", [])
    resource_info = "\n".join(
        f"- {r.name}：{r.category}，票价约 ¥{r.price_per_person}/人，时长 {r.recommended_minutes} 分钟"
        for r in resources
    )
    breakdown = await gateway.structured_completion(
        system_prompt=COST_ESTIMATION_SYSTEM,
        user_prompt=(
            f"目的地：{request.destination}\n天数：{request.days} 天 {request.nights} 晚\n"
            f"团队人数：{request.group_size} 人\n人均预算上限：{request.budget_per_person} 元\n"
            f"目标毛利率：{request.target_margin_rate:.0%}\n\n已选资源：\n{resource_info}"
        ),
        schema=CostBreakdown,
        timeout_seconds=45,
    )
    cost_items = [QuoteItem(category=i.category, description=i.description, amount=i.amount) for i in breakdown.items]
    quote = tool_registry.invoke(
        "calculate_product_cost",
        {
            "group_size": request.group_size, "days": request.days,
            "target_margin_rate": request.target_margin_rate,
            "budget_per_person": request.budget_per_person,
            "cost_items": [i.model_dump() for i in cost_items],
        },
    )
    logger.info("LLM cost estimation: total=%d", quote.total_cost)
    return quote


# ------------------------------------------------------------------
# quality_review → LLM only
# ------------------------------------------------------------------

async def quality_review(state: PlanningState) -> dict:
    settings = get_settings()
    if not settings.mock_model_mode:
        report = await _llm_quality_review(settings, state)
        return {"quality_report": report, "current_stage": "waiting_approval"}
    raise RuntimeError("Quality review requires LLM.")


async def _llm_quality_review(settings, state) -> QualityReport:
    gateway = ModelGateway(settings)
    request = state["request"]
    itinerary_summary = json.dumps(
        [{"day": d.day, "theme": d.theme, "events": [
            {"title": e.title, "time": f"{e.start_time}-{e.end_time}", "cost": e.cost_per_person}
            for e in d.events
        ]} for d in state["itinerary"]],
        ensure_ascii=False,
    )
    quote = state["quote"]
    constraint = state["constraint_report"]

    assessment = await gateway.structured_completion(
        system_prompt=QUALITY_REVIEW_SYSTEM,
        user_prompt=(
            f"产品：{request.title}\n目的地：{request.destination}\n"
            f"目标人群：{request.target_audience}\n资源来源：{state.get('resource_search_provider', 'unknown')}\n\n"
            f"行程：\n{itinerary_summary}\n\n"
            f"报价：总成本 {quote.total_cost} 元，人均售价 {quote.sale_price_per_person} 元，毛利率 {quote.margin_rate:.1%}\n\n"
            f"约束校验：{'通过' if constraint.valid else '未通过'}，得分 {constraint.score}\n"
            f"问题：{'; '.join(i.message for i in constraint.issues) or '无'}"
        ),
        schema=QualityAssessment,
        timeout_seconds=60,
    )
    logger.info("LLM quality review: overall=%d", assessment.overall_score)
    return QualityReport(
        overall_score=assessment.overall_score,
        fact_traceability_score=assessment.fact_traceability_score,
        feasibility_score=assessment.feasibility_score,
        audience_fit_score=assessment.audience_fit_score,
        blocking_issues=assessment.blocking_issues,
        suggestions=assessment.suggestions,
    )


# ------------------------------------------------------------------
# approval_gate → human interrupt
# ------------------------------------------------------------------

def approval_gate(state: PlanningState) -> dict:
    decision = interrupt({
        "kind": "plan_approval",
        "plan_id": state["plan_id"],
        "quality_report": state["quality_report"].model_dump(),
        "message": "方案已通过自动审核，请确认。",
    })
    if not decision.get("approved", False):
        return {"approval": decision, "current_stage": "approval_rejected", "errors": [decision.get("comment", "驳回")]}
    return {"approval": decision, "current_stage": "approved"}


def approval_route(state: PlanningState) -> Literal["poster", "rejected"]:
    return "poster" if state.get("approval", {}).get("approved") else "rejected"


# ------------------------------------------------------------------
# prepare_poster → PosterService
# ------------------------------------------------------------------

async def prepare_poster(state: PlanningState) -> dict:
    request = state["request"]
    brief = PosterBrief(
        destination=request.destination,
        product_theme=request.title,
        target_audience=request.target_audience,
        visual_style="自然、明亮、具有层次的编辑插画风",
        primary_colors=["湖水绿", "暖金色", "宣纸白"],
        visual_elements=["目的地标志性景观", "旅行氛围"],
        negative_elements=["文字", "Logo", "二维码", "水印"],
        aspect_ratio="3:4",
    )
    poster = await PosterService(get_settings()).generate_background(brief)
    return {"poster_brief": brief, "poster_asset": poster, "current_stage": "poster_generated"}


# ------------------------------------------------------------------
# finalize_delivery → save_plan_version + submit_for_approval
# ------------------------------------------------------------------

def finalize_delivery(state: PlanningState) -> dict:
    snapshot = {
        "itinerary": [d.model_dump() for d in state.get("itinerary", [])],
        "quote": state["quote"].model_dump() if state.get("quote") else None,
        "quality_report": state["quality_report"].model_dump() if state.get("quality_report") else None,
        "poster_asset": state.get("poster_asset"),
    }
    plan_store.save_version(state["plan_id"], "审批后生成最终交付版本", snapshot)
    tool_registry.invoke(
        "submit_for_approval",
        {"plan_id": state["plan_id"], "reviewer_id": state.get("approval", {}).get("reviewer_id", "system"), "approved": True},
    )
    return {"current_stage": "delivered"}


def mark_failed(state: PlanningState) -> dict:
    return {"current_stage": "failed", "errors": state.get("errors", []) + ["约束在最大重试次数内未通过。"]}


def mark_rejected(state: PlanningState) -> dict:
    return {"current_stage": "rejected"}


# ------------------------------------------------------------------
# Graph assembly
# ------------------------------------------------------------------

def build_planning_graph(checkpointer: MemorySaver | None = None):
    builder = StateGraph(PlanningState)
    builder.add_node("parse_requirements", parse_requirements)
    builder.add_node("retrieve_resources", retrieve_resources)
    builder.add_node("plan_itinerary", plan_itinerary)
    builder.add_node("validate_constraints", validate_constraints)
    builder.add_node("repair_plan", repair_plan)
    builder.add_node("calculate_quote", calculate_quote)
    builder.add_node("quality_review", quality_review)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("prepare_poster", prepare_poster)
    builder.add_node("finalize_delivery", finalize_delivery)
    builder.add_node("mark_failed", mark_failed)
    builder.add_node("mark_rejected", mark_rejected)

    builder.add_edge(START, "parse_requirements")
    builder.add_edge("parse_requirements", "retrieve_resources")
    builder.add_edge("retrieve_resources", "plan_itinerary")
    builder.add_edge("plan_itinerary", "validate_constraints")
    builder.add_conditional_edges("validate_constraints", constraint_route,
        {"quote": "calculate_quote", "repair": "repair_plan", "failed": "mark_failed"})
    builder.add_edge("repair_plan", "plan_itinerary")
    builder.add_edge("calculate_quote", "quality_review")
    builder.add_edge("quality_review", "approval_gate")
    builder.add_conditional_edges("approval_gate", approval_route,
        {"poster": "prepare_poster", "rejected": "mark_rejected"})
    builder.add_edge("prepare_poster", "finalize_delivery")
    builder.add_edge("finalize_delivery", END)
    builder.add_edge("mark_failed", END)
    builder.add_edge("mark_rejected", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
