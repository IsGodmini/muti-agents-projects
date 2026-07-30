from __future__ import annotations

import json
import logging
from pathlib import Path
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
from app.skills.loader import SkillRegistry
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_SKILL_MAP = {
    "family_trip": "family_trip_planning",
    "study_tour": "study_tour_planning",
    "corporate_team_building": "corporate_team_building",
    "senior_friendly": "senior_friendly_trip",
}


def _load_skill(skill_name: str) -> tuple[dict, str]:
    """Load skill quality gates and instructions from the registry."""
    settings = get_settings()
    try:
        registry = SkillRegistry(Path(settings.skills_directory))
        registry.load_all()
        skill = registry.get(skill_name)
        return dict(skill.manifest.quality_gates), skill.instructions
    except Exception:
        logger.warning("Could not load skill %s", skill_name, exc_info=True)
        return {}, ""


# ------------------------------------------------------------------
# Node: parse_requirements
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
            skill_name = analysis.selected_skill
            gates, instructions = _load_skill(skill_name)
            logger.info("LLM requirement analysis: skill=%s gates=%s", skill_name, gates)
            return {
                "selected_skill": skill_name,
                "selected_skill_gates": gates,
                "selected_skill_instructions": instructions,
                "requirements_complete": analysis.requirements_complete,
                "missing_fields": analysis.missing_fields,
                "current_stage": "requirements_parsed",
                "retry_count": 0,
                "errors": [],
            }
        except Exception:
            logger.warning("LLM requirement analysis failed; using rule fallback", exc_info=True)

    skill_name = _SKILL_MAP[request.product_type.value]
    gates, instructions = _load_skill(skill_name)
    return {
        "selected_skill": skill_name,
        "selected_skill_gates": gates,
        "selected_skill_instructions": instructions,
        "requirements_complete": True,
        "missing_fields": [],
        "current_stage": "requirements_parsed",
        "retry_count": 0,
        "errors": [],
    }


# ------------------------------------------------------------------
# Node: retrieve_resources
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
# Node: plan_itinerary
# ------------------------------------------------------------------

async def plan_itinerary(state: PlanningState) -> dict:
    resources = state["resources"]
    request = state["request"]
    settings = get_settings()
    skill_instructions = state.get("selected_skill_instructions", "")

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
            days = await _generate_schedule(
                settings, request, optimized, resource_map, skill_instructions,
            )
            return {
                "itinerary": days,
                "route_matrix": route_matrix,
                "current_stage": "itinerary_planned",
            }
        except Exception:
            logger.warning("LLM schedule generation failed; using basic layout", exc_info=True)

    days = _basic_schedule(request, optimized, resource_map)
    return {"itinerary": days, "route_matrix": route_matrix, "current_stage": "itinerary_planned"}


async def _estimate_travel_times(settings, resources) -> dict[str, int]:
    gateway = ModelGateway(settings)
    resource_list = "\n".join(
        f"[{i}] {r.name}（{r.location}）" for i, r in enumerate(resources)
    )
    matrix = await gateway.structured_completion(
        system_prompt=TRAVEL_TIME_SYSTEM,
        user_prompt=f"资源列表（共 {len(resources)} 个）：\n{resource_list}",
        schema=TravelTimeMatrix,
        timeout_seconds=45,
    )
    travel_times: dict[str, int] = {}
    for pair in matrix.pairs:
        if 0 <= pair.from_index < len(resources) and 0 <= pair.to_index < len(resources):
            source_id = resources[pair.from_index].id
            target_id = resources[pair.to_index].id
            travel_times[f"{source_id}->{target_id}"] = pair.time
    logger.info("LLM estimated %d travel-time pairs", len(travel_times))
    return travel_times


async def _generate_schedule(
    settings, request, optimized, resource_map, skill_instructions,
) -> list[ItineraryDay]:
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
    skill_context = (
        f"\n\n策划 Skill 要求（必须遵守）：\n{skill_instructions[:800]}"
        if skill_instructions
        else ""
    )
    batch = await gateway.structured_completion(
        system_prompt=SCHEDULE_SYSTEM + skill_context,
        user_prompt=(
            f"产品：{request.title}\n"
            f"目的地：{request.destination}\n"
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
                start_time=event.start_time,
                end_time=event.end_time,
                title=event.title,
                resource_id=event.resource_id,
                category=event.category,
                description=event.description,
                cost_per_person=event.cost_per_person,
            )
            for event in day_info.events
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
                title=resource.name,
                resource_id=rid,
                category=resource.category,
                description=f"{resource.summary or resource.name}体验。",
                cost_per_person=resource.price_per_person,
            ))
            start_hour = end_minutes // 60 + 2
        days.append(ItineraryDay(
            day=day_index,
            theme=f"{request.destination}探索 · 第 {day_index} 天",
            events=events,
        ))
    return days


# ------------------------------------------------------------------
# Node: validate_constraints  (skill gates enforced)
# ------------------------------------------------------------------

def validate_constraints(state: PlanningState) -> dict:
    gates = state.get("selected_skill_gates", {})
    max_daily = int(gates.get("max_daily_minutes", 600))
    max_transport = int(gates.get("max_continuous_transport_minutes", 120))

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
        if day_span > max_daily:
            issues.append(ConstraintIssue(
                code="DAILY_DURATION_EXCEEDED", severity="warning",
                message=f"第 {day.day} 天活动跨度 {day_span} 分钟，超过 Skill 门禁 {max_daily} 分钟。",
                suggested_action="移除低优先级资源或增加休息节点。",
            ))

        for i in range(len(day.events) - 1):
            try:
                prev_end = int(day.events[i].end_time[:2]) * 60 + int(day.events[i].end_time[3:])
                next_start = int(day.events[i + 1].start_time[:2]) * 60 + int(day.events[i + 1].start_time[3:])
                gap = next_start - prev_end
                if gap > max_transport:
                    issues.append(ConstraintIssue(
                        code="TRANSPORT_GAP_EXCEEDED", severity="warning",
                        message=(
                            f"第 {day.day} 天「{day.events[i].title}」到「{day.events[i + 1].title}」"
                            f"间隔 {gap} 分钟，超过交通门禁 {max_transport} 分钟。"
                        ),
                        suggested_action="调整资源顺序或替换为更近的替代资源。",
                    ))
            except (ValueError, IndexError):
                continue

    route_matrix = state.get("route_matrix", {})
    report = ConstraintReport(
        valid=not any(issue.severity == "blocking" for issue in issues),
        score=max(70, 100 - len(issues) * 8),
        issues=issues,
        total_travel_minutes=sum(route_matrix.values()) // max(len(route_matrix), 1),
        max_daily_minutes=actual_max_daily,
    )
    return {"constraint_report": report, "current_stage": "constraints_validated"}


def constraint_route(state: PlanningState) -> Literal["quote", "repair", "failed"]:
    report = state["constraint_report"]
    if report.valid:
        return "quote"
    if state.get("retry_count", 0) < 2:
        return "repair"
    return "failed"


# ------------------------------------------------------------------
# Node: repair_plan  (emergency alternative planning)
# ------------------------------------------------------------------

async def repair_plan(state: PlanningState) -> dict:
    """Search for alternative resources to fix constraint violations."""
    retry = state.get("retry_count", 0) + 1
    request = state["request"]
    settings = get_settings()
    issues = state.get("constraint_report", ConstraintReport(
        valid=False, score=0, issues=[], total_travel_minutes=0, max_daily_minutes=0,
    )).issues

    issue_summary = "；".join(issue.message for issue in issues[:3])
    logger.info("Repair attempt %d: %s", retry, issue_summary)

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
                    combined = list(state.get("resources", [])) + new_resources[:3]
                    return {
                        "retry_count": retry,
                        "resources": combined,
                        "current_stage": "repairing",
                    }
        except Exception:
            logger.warning("Emergency alternative search failed", exc_info=True)

    return {"retry_count": retry, "current_stage": "repairing"}


# ------------------------------------------------------------------
# Node: calculate_quote
# ------------------------------------------------------------------

async def calculate_quote(state: PlanningState) -> dict:
    settings = get_settings()

    if not settings.mock_model_mode:
        try:
            quote = await _llm_cost_estimation(settings, state)
            return {"quote": quote, "current_stage": "quote_calculated"}
        except Exception:
            logger.warning("LLM cost estimation failed", exc_info=True)
            raise

    raise RuntimeError("Cost estimation requires LLM; mock mode has no hardcoded costs.")


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
            f"目的地：{request.destination}\n"
            f"天数：{request.days} 天 {request.nights} 晚\n"
            f"团队人数：{request.group_size} 人\n"
            f"人均预算上限：{request.budget_per_person} 元\n"
            f"目标毛利率：{request.target_margin_rate:.0%}\n\n"
            f"已选资源：\n{resource_info}"
        ),
        schema=CostBreakdown,
        timeout_seconds=45,
    )
    cost_items = [
        QuoteItem(category=item.category, description=item.description, amount=item.amount)
        for item in breakdown.items
    ]
    quote = tool_registry.invoke(
        "calculate_product_cost",
        {
            "group_size": request.group_size,
            "days": request.days,
            "target_margin_rate": request.target_margin_rate,
            "budget_per_person": request.budget_per_person,
            "cost_items": [item.model_dump() for item in cost_items],
        },
    )
    logger.info("LLM cost estimation: total=%d", quote.total_cost)
    return quote


# ------------------------------------------------------------------
# Node: quality_review  (skill gates included)
# ------------------------------------------------------------------

async def quality_review(state: PlanningState) -> dict:
    settings = get_settings()

    if not settings.mock_model_mode:
        try:
            report = await _llm_quality_review(settings, state)
            return {"quality_report": report, "current_stage": "waiting_approval"}
        except Exception:
            logger.warning("LLM quality review failed", exc_info=True)
            raise

    raise RuntimeError("Quality review requires LLM; mock mode has no hardcoded scores.")


async def _llm_quality_review(settings, state) -> QualityReport:
    gateway = ModelGateway(settings)
    request = state["request"]
    gates = state.get("selected_skill_gates", {})
    skill_name = state.get("selected_skill", "unknown")

    itinerary_summary = json.dumps(
        [
            {
                "day": day.day, "theme": day.theme,
                "events": [
                    {"title": e.title, "time": f"{e.start_time}-{e.end_time}", "cost": e.cost_per_person}
                    for e in day.events
                ],
            }
            for day in state["itinerary"]
        ],
        ensure_ascii=False,
    )
    quote = state["quote"]
    constraint = state["constraint_report"]
    provider = state.get("resource_search_provider", "unknown")

    gates_text = json.dumps(gates, ensure_ascii=False) if gates else "无特定门禁"

    assessment = await gateway.structured_completion(
        system_prompt=QUALITY_REVIEW_SYSTEM,
        user_prompt=(
            f"产品：{request.title}\n目的地：{request.destination}\n"
            f"目标人群：{request.target_audience}\n资源来源：{provider}\n"
            f"使用 Skill：{skill_name}\nSkill 质量门禁：{gates_text}\n\n"
            f"行程：\n{itinerary_summary}\n\n"
            f"报价：总成本 {quote.total_cost} 元，人均售价 {quote.sale_price_per_person} 元，"
            f"毛利率 {quote.margin_rate:.1%}\n\n"
            f"约束校验：{'通过' if constraint.valid else '未通过'}，"
            f"得分 {constraint.score}，问题 {len(constraint.issues)} 项\n"
            f"约束问题：{'; '.join(i.message for i in constraint.issues) or '无'}"
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
# Approval & delivery
# ------------------------------------------------------------------

def approval_gate(state: PlanningState) -> dict:
    decision = interrupt({
        "kind": "plan_approval",
        "plan_id": state["plan_id"],
        "quality_report": state["quality_report"].model_dump(),
        "message": "方案已通过自动审核，请产品经理确认。",
    })
    if not decision.get("approved", False):
        return {
            "approval": decision,
            "current_stage": "approval_rejected",
            "errors": [decision.get("comment", "人工审核驳回")],
        }
    return {"approval": decision, "current_stage": "approved"}


def approval_route(state: PlanningState) -> Literal["poster", "rejected"]:
    return "poster" if state.get("approval", {}).get("approved") else "rejected"


async def prepare_poster(state: PlanningState) -> dict:
    request = state["request"]
    brief = PosterBrief(
        destination=request.destination,
        product_theme=request.title,
        target_audience=request.target_audience,
        visual_style="自然、明亮、具有江南层次的编辑插画风",
        primary_colors=["湖水绿", "暖金色", "宣纸白"],
        visual_elements=["西湖水面", "远山", "亲子自然观察"],
        negative_elements=["文字", "Logo", "二维码", "错误地标", "人物畸变", "水印"],
        aspect_ratio="3:4",
    )
    poster = await PosterService(get_settings()).generate_background(brief)
    return {"poster_brief": brief, "poster_asset": poster, "current_stage": "poster_generated"}


def finalize_delivery(state: PlanningState) -> dict:
    snapshot = {
        "itinerary": [day.model_dump() for day in state.get("itinerary", [])],
        "quote": state["quote"].model_dump() if state.get("quote") else None,
        "quality_report": state["quality_report"].model_dump() if state.get("quality_report") else None,
        "poster_asset": state.get("poster_asset"),
        "selected_skill": state.get("selected_skill"),
    }
    plan_store.save_version(state["plan_id"], "人工审批后生成最终交付版本", snapshot)
    tool_registry.invoke(
        "submit_for_approval",
        {
            "plan_id": state["plan_id"],
            "reviewer_id": state.get("approval", {}).get("reviewer_id", "system"),
            "approved": True,
        },
    )
    return {"current_stage": "delivered"}


def mark_failed(state: PlanningState) -> dict:
    return {
        "current_stage": "failed",
        "errors": state.get("errors", []) + ["行程约束在最大重试次数内未通过。"],
    }


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
    builder.add_conditional_edges(
        "validate_constraints", constraint_route,
        {"quote": "calculate_quote", "repair": "repair_plan", "failed": "mark_failed"},
    )
    builder.add_edge("repair_plan", "plan_itinerary")
    builder.add_edge("calculate_quote", "quality_review")
    builder.add_edge("quality_review", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate", approval_route,
        {"poster": "prepare_poster", "rejected": "mark_rejected"},
    )
    builder.add_edge("prepare_poster", "finalize_delivery")
    builder.add_edge("finalize_delivery", END)
    builder.add_edge("mark_failed", END)
    builder.add_edge("mark_rejected", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
