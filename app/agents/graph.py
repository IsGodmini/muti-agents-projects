"""LangGraph 工作流：每个节点直接绑定所需的 Tools。

节点 → Tools 绑定：
  parse_requirements   → (LLM only)
  retrieve_resources   → search_attractions, (LLM enrichment)
  plan_itinerary       → calculate_route_matrix, optimize_itinerary, (LLM scheduling)
  validate_constraints → (deterministic)
  repair_plan          → search_attractions (emergency alternatives)
  calculate_quote      → calculate_product_cost, (LLM estimation)
  quality_review       → (LLM only)
  run_verification     → (deterministic checks)
  review_decision      → (auto approve / repair loop)
  prepare_poster       → PosterService
  approval_gate        → interrupt（人工审批，批准/驳回）
  finalize_delivery    → plan_store.save_version + submit_for_approval
  mark_rejected        → submit_for_approval（记录驳回决定）
"""

from __future__ import annotations

import asyncio
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
from app.services.guard import filter_resources as guard_filter_resources
from app.services.model_gateway import ModelGateway
from app.services.pdf_report import build_pdf_report
from app.services.plan_store import plan_store
from app.services.poster import PosterService
from app.services.ranking import score_resources
from app.services.renderer import render_markdown_report
from app.services.tools.amap import AmapClient
from app.services.verifier import verify_plan
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
                model=settings.llm_model_complex,
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
            logger.info("LLM requirement analysis complete=%s", analysis.requirements_complete)
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

    search_themes = list(dict.fromkeys([*request.themes, *request.interests]))

    tavily_task = tool_registry.ainvoke(
        "search_attractions",
        {"destination": request.destination, "themes": search_themes or request.themes,
         "audience": request.target_audience, "limit": 8},
    )
    amap_keywords = f"{request.destination} {' '.join(search_themes[:2])} 景点"
    amap_task = tool_registry.ainvoke(
        "search_poi_amap",
        {"keywords": amap_keywords, "city": request.destination, "limit": 10},
    )

    results = await asyncio.gather(tavily_task, amap_task, return_exceptions=True)
    resources = []
    for res in results:
        if isinstance(res, Exception):
            logger.warning("Search source failed: %s", res)
        elif isinstance(res, list):
            resources.extend(res)

    seen_names: set[str] = set()
    deduped = []
    for r in resources:
        if r.name not in seen_names:
            seen_names.add(r.name)
            deduped.append(r)
    resources = deduped

    # Guard: filter out resources with injection patterns
    resources, guard_warnings = guard_filter_resources(resources, scan_source="retrieve_resources")
    for w in guard_warnings:
        logger.warning("Guard: %s", w)

    if not settings.mock_model_mode and resources:
        try:
            resources = await _enrich_resources(settings, resources)
        except Exception:
            logger.warning("LLM resource enrichment failed", exc_info=True)

    resources = score_resources(resources, request)

    return {
        "resources": resources,
        "resource_search_provider": "+".join({r.provider for r in resources[:5]}) if resources else "none",
        "route_matrix": {},
        "current_stage": "resources_retrieved",
    }


async def _enrich_resources(settings, resources):
    gateway = ModelGateway(settings)
    descriptions = "\n".join(
        f"[{i}] 标题：{r.name}\n    摘要：{(r.summary or r.evidence or '')[:300]}\n    来源：{r.source_url or ''}"
        for i, r in enumerate(resources)
    )
    all_images = []
    for r in resources:
        all_images.extend(r.images[:3])

    batch = await gateway.structured_completion(
        model=settings.llm_model_multimodal,
        system_prompt=RESOURCE_ENRICHMENT_SYSTEM,
        user_prompt=(
            f"目的地资源列表：\n{descriptions}\n\n"
            + ("以下是搜索结果附带的现场图片，请结合图片内容判断景点实况、环境质量和适合人群。"
               if all_images else "")
        ),
        schema=ResourceEnrichmentBatch,
        timeout_seconds=45,
        image_urls=all_images or None,
    )
    enrichment_map = {
        (item.index if item.index >= 0 else pos): item
        for pos, item in enumerate(batch.resources)
    }
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

    weather = await tool_registry.ainvoke(
        "get_weather_forecast",
        {"city": request.destination, "days": min(request.days, 7)},
    )
    if weather:
        logger.info("Weather forecast for %s: %d days", request.destination, len(weather))

    resource_ids = [r.id for r in resources]
    resource_map = {r.id: r for r in resources}

    travel_times = await _estimate_travel_times(settings, resources)

    coordinates = {}
    for r in resources:
        if r.lng and r.lat:
            coordinates[r.id] = f"{r.lng},{r.lat}"

    route_matrix = await tool_registry.ainvoke(
        "calculate_route_matrix",
        {
            "resource_ids": resource_ids,
            "travel_times": travel_times,
            "coordinates": coordinates,
            "city": request.destination,
            "mode": "transit" if "public_transit" in request.transport_preferences else "driving",
        },
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
            days = await _generate_schedule(settings, request, optimized, resource_map, weather)
            return {
                "itinerary": days,
                "route_matrix": route_matrix,
                "weather_forecast": weather,
                "current_stage": "itinerary_planned",
            }
        except Exception:
            logger.warning("LLM schedule generation failed; using basic layout", exc_info=True)

    days = _basic_schedule(request, optimized, resource_map)
    return {
        "itinerary": days,
        "route_matrix": route_matrix,
        "weather_forecast": weather,
        "current_stage": "itinerary_planned",
    }


async def _estimate_travel_times(settings, resources) -> dict[str, int]:
    """Estimate travel times: Amap API for resources with coordinates, LLM fallback."""
    travel_times: dict[str, int] = {}

    coords_available = [r for r in resources if r.lng and r.lat]
    if settings.amap_api_key and len(coords_available) >= 2:
        client = AmapClient(settings.amap_api_key, settings.amap_base_url)
        tasks = []
        keys = []
        for source in coords_available:
            for target in coords_available:
                if source.id == target.id:
                    continue
                keys.append(f"{source.id}->{target.id}")
                tasks.append(
                    client.travel_time(
                        f"{source.lng},{source.lat}",
                        f"{target.lng},{target.lat}",
                        mode="transit",
                    )
                )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key, result in zip(keys, results, strict=True):
            travel_times[key] = result if isinstance(result, int) else 30
        logger.info("Amap real travel times: %d pairs", len(travel_times))

    needs_llm = not settings.mock_model_mode and (
        len(coords_available) < 2 or len(travel_times) < len(resources) * (len(resources) - 1) // 2
    )
    if needs_llm:
        try:
            gateway = ModelGateway(settings)
            resource_list = "\n".join(f"[{i}] {r.name}（{r.location}）" for i, r in enumerate(resources))
            matrix = await gateway.structured_completion(
                model=settings.llm_model_complex,
                system_prompt=TRAVEL_TIME_SYSTEM,
                user_prompt=f"资源列表（共 {len(resources)} 个）：\n{resource_list}",
                schema=TravelTimeMatrix,
                timeout_seconds=45,
            )
            for pair in matrix.pairs:
                if 0 <= pair.from_index < len(resources) and 0 <= pair.to_index < len(resources):
                    key = f"{resources[pair.from_index].id}->{resources[pair.to_index].id}"
                    if key not in travel_times:
                        travel_times[key] = pair.time
            logger.info("LLM estimated travel times, total pairs: %d", len(travel_times))
        except Exception:
            logger.warning("LLM travel time estimation failed", exc_info=True)

    return travel_times


async def _generate_schedule(settings, request, optimized, resource_map, weather) -> list[ItineraryDay]:
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
    weather_text = ""
    if weather:
        weather_text = "\n".join(
            f"- {w.get('date', '?')}：{w.get('text_day', '未知')}，"
            f"{w.get('temp_min', '?')}~{w.get('temp_max', '?')}℃，"
            f"风力{w.get('wind_scale_day', '-')}级，湿度{w.get('humidity', '-')}%"
            for w in weather
        )
    else:
        weather_text = "暂无天气数据"

    batch = await gateway.structured_completion(
        model=settings.llm_model_complex,
        system_prompt=SCHEDULE_SYSTEM,
        user_prompt=(
            f"产品：{request.title}\n目的地：{request.destination}\n"
            f"目标人群：{request.target_audience}\n"
            f"天数：{request.days} 天 {request.nights} 晚\n"
            f"主题：{', '.join(request.themes)}\n"
            f"约束：{', '.join(request.constraints) or '无'}\n\n"
            f"目的地天气（未来几天）：\n{weather_text}\n\n"
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
    """Comprehensive deterministic validation of the itinerary."""
    request = state["request"]
    issues: list[ConstraintIssue] = []
    actual_max_daily = 0
    time_conflict_count = 0

    pace_limits = {"intense": 720, "moderate": 600, "relaxed": 480}
    max_daily = pace_limits.get(request.pace.value if hasattr(request.pace, "value") else "moderate", 600)
    max_events = {"intense": 6, "moderate": 5, "relaxed": 4}
    event_limit = max_events.get(request.pace.value if hasattr(request.pace, "value") else "moderate", 5)

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
                message=f"第 {day.day} 天活动跨度 {day_span} 分钟，超过节奏上限 {max_daily} 分钟。",
                suggested_action="移除低优先级资源或增加休息节点。",
            ))

        if len(day.events) > event_limit:
            issues.append(ConstraintIssue(
                code="TOO_MANY_EVENTS", severity="warning",
                message=f"第 {day.day} 天安排 {len(day.events)} 个活动，超过节奏建议 {event_limit} 个。",
                suggested_action="减少活动数量，增加休息间隔。",
            ))

        for i in range(len(day.events) - 1):
            try:
                curr_end = int(day.events[i].end_time[:2]) * 60 + int(day.events[i].end_time[3:])
                next_start = int(day.events[i + 1].start_time[:2]) * 60 + int(day.events[i + 1].start_time[3:])
                if next_start < curr_end:
                    time_conflict_count += 1
                    issues.append(ConstraintIssue(
                        code="TIME_CONFLICT", severity="blocking",
                        message=f"第 {day.day} 天「{day.events[i].title}」与「{day.events[i+1].title}」时间重叠。",
                        event_title=day.events[i].title,
                        suggested_action="调整开始/结束时间消除重叠。",
                    ))
            except (ValueError, IndexError):
                continue

    must_visit = set(request.must_visit)
    scheduled_names = {e.title for day in state["itinerary"] for e in day.events}
    covered = sum(1 for mv in must_visit if any(mv in name for name in scheduled_names))
    must_visit_coverage = covered / len(must_visit) if must_visit else 1.0
    if must_visit and must_visit_coverage < 1.0:
        missing = [mv for mv in must_visit if not any(mv in name for name in scheduled_names)]
        issues.append(ConstraintIssue(
            code="MUST_VISIT_MISSING", severity="blocking",
            message=f"必去地点未覆盖: {', '.join(missing)}",
            suggested_action="将缺失的必去地点加入行程。",
        ))

    quote = state.get("quote")
    budget_accuracy = 0.0
    if quote:
        budget_total = request.budget_per_person * request.group_size
        budget_accuracy = abs(quote.total_cost - budget_total) / max(budget_total, 1)

    route_matrix = state.get("route_matrix", {})
    report = ConstraintReport(
        valid=not any(i.severity == "blocking" for i in issues),
        score=max(70, 100 - len(issues) * 8),
        issues=issues,
        total_travel_minutes=sum(route_matrix.values()) // max(len(route_matrix), 1),
        max_daily_minutes=actual_max_daily,
        must_visit_coverage=round(must_visit_coverage, 2),
        budget_accuracy=round(budget_accuracy, 4),
        time_conflict_count=time_conflict_count,
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
        model=settings.llm_model_complex,
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
        return {"quality_report": report}
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
        model=settings.llm_model_complex,
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
# review_decision → deterministic routing after verification
# ------------------------------------------------------------------

REVIEW_PASS_THRESHOLD = 60
MAX_REVIEW_REPAIRS = 2


def review_decision(state: PlanningState) -> Literal["poster", "review_repair", "failed"]:
    """Route based on verification + quality scores. No human needed."""
    verification_score = state.get("verification_score", 0)
    quality = state.get("quality_report")
    quality_score = quality.overall_score if quality else 0
    review_repairs = state.get("retry_count", 0)

    if verification_score >= REVIEW_PASS_THRESHOLD and quality_score >= REVIEW_PASS_THRESHOLD:
        return "poster"
    if review_repairs >= MAX_REVIEW_REPAIRS:
        return "failed"
    return "review_repair"


async def review_repair(state: PlanningState) -> dict:
    """Re-plan trigger after quality/verification failure.

    The actual rework happens downstream: this node only advances the
    shared ``retry_count`` and routes back to ``plan_itinerary``.
    """
    retry = state.get("retry_count", 0) + 1
    quality = state.get("quality_report")
    issues = []
    if quality:
        issues.extend(quality.blocking_issues)
        issues.extend(quality.suggestions[:3])
    constraint = state.get("constraint_report")
    if constraint:
        issues.extend(i.message for i in constraint.issues if i.severity == "blocking")

    logger.info("Review repair #%d: %s", retry, "; ".join(issues[:5]))
    return {"retry_count": retry, "current_stage": "review_repairing"}


# ------------------------------------------------------------------
# prepare_poster → PosterService
# ------------------------------------------------------------------

async def _generate_and_download(
    service: PosterService, brief: PosterBrief, plan_id: str, label: str,
) -> tuple[dict[str, str], str | None]:
    """Generate one image via ComfyUI and download it locally."""
    result = await service.generate_background(brief)
    local_path = None
    if "url" in result and not service.settings.mock_imagegen:
        try:
            local_path = await service.download_image(result["url"], plan_id)
        except Exception:
            logger.warning("Download failed for %s", label, exc_info=True)
    return result, local_path


async def prepare_poster(state: PlanningState) -> dict:
    request = state["request"]
    settings = get_settings()
    service = PosterService(settings)

    cover_brief = PosterBrief(
        destination=request.destination,
        product_theme=request.title,
        target_audience=request.target_audience,
        visual_style="自然、明亮、具有层次的编辑插画风",
        primary_colors=["湖水绿", "暖金色", "宣纸白"],
        visual_elements=["目的地标志性景观", "旅行氛围"],
        negative_elements=["文字", "Logo", "二维码", "水印"],
        aspect_ratio="3:4",
    )

    if settings.mock_imagegen:
        poster, _ = await _generate_and_download(service, cover_brief, state["plan_id"], "cover")
        return {"poster_brief": cover_brief, "poster_asset": poster,
                "day_image_paths": [], "current_stage": "poster_generated"}

    day_briefs = [
        PosterBrief(
            destination=request.destination,
            product_theme=day.theme,
            target_audience=request.target_audience,
            visual_style="水彩插画风格，柔和色调，留白充足",
            primary_colors=["湖水绿", "暖金色", "宣纸白"],
            visual_elements=[request.destination, day.theme],
            negative_elements=["文字", "Logo", "二维码", "水印", "人物"],
            aspect_ratio="3:4",
        )
        for day in state.get("itinerary", [])
    ]

    tasks = [
        _generate_and_download(service, cover_brief, state["plan_id"], "cover"),
        *[
            _generate_and_download(service, brief, f"{state['plan_id']}/day{i+1}", f"day{i+1}")
            for i, brief in enumerate(day_briefs)
        ],
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    poster: dict[str, str] = {"status": "failed"}
    day_image_paths: list[str | None] = [None] * len(day_briefs)

    for idx, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning("Image task %d failed: %s", idx, res)
            continue
        result, local_path = res
        if idx == 0:
            poster = result
            if local_path:
                poster["local_path"] = local_path
        else:
            day_image_paths[idx - 1] = local_path

    return {
        "poster_brief": cover_brief,
        "poster_asset": poster,
        "day_image_paths": day_image_paths,
        "current_stage": "poster_generated",
    }


# ------------------------------------------------------------------
# approval_gate → interrupt（人工审批）
# ------------------------------------------------------------------

def approval_gate(state: PlanningState) -> dict:
    """Pause the workflow and wait for a human approval decision.

    The graph is compiled with a checkpointer; the caller resumes the same
    thread with ``Command(resume=decision)`` where decision is an
    ``ApprovalDecision``-shaped dict.
    """
    decision = interrupt({
        "plan_id": state["plan_id"],
        "stage": state.get("current_stage", "unknown"),
        "message": "方案已生成，等待人工审批。",
    })
    approved = bool(decision.get("approved", True)) if isinstance(decision, dict) else bool(decision)
    reviewer_id = decision.get("reviewer_id", "system") if isinstance(decision, dict) else "system"
    comment = decision.get("comment") if isinstance(decision, dict) else None
    return {
        "approval": {"approved": approved, "reviewer_id": reviewer_id, "comment": comment},
        "current_stage": "approved" if approved else "rejected",
    }


def approval_route(state: PlanningState) -> Literal["finalize", "rejected"]:
    return "finalize" if state.get("approval", {}).get("approved", True) else "rejected"


# ------------------------------------------------------------------
# finalize_delivery → plan_store.save_version + submit_for_approval
# ------------------------------------------------------------------

def finalize_delivery(state: PlanningState) -> dict:
    request = state["request"]
    poster_local = state.get("poster_asset", {}).get("local_path")
    markdown = render_markdown_report(
        request=request,
        itinerary=state.get("itinerary", []),
        quote=state.get("quote"),
        quality_report=state.get("quality_report"),
        constraint_report=state.get("constraint_report"),
        weather_forecast=state.get("weather_forecast"),
        poster_local_path=poster_local,
    )
    from app.services.plan_store import DATA_DIR
    report_dir = DATA_DIR / "plans" / state["plan_id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.md"
    report_path.write_text(markdown, encoding="utf-8")
    logger.info("Markdown report → %s", report_path)

    snapshot = {
        "itinerary": [d.model_dump() for d in state.get("itinerary", [])],
        "quote": state["quote"].model_dump() if state.get("quote") else None,
        "quality_report": state["quality_report"].model_dump() if state.get("quality_report") else None,
        "poster_asset": state.get("poster_asset"),
        "verification_score": state.get("verification_score"),
    }
    plan_store.save_version(state["plan_id"], "审批后生成最终交付版本", snapshot)
    approval = state.get("approval", {}) or {}
    tool_registry.invoke(
        "submit_for_approval",
        {
            "plan_id": state["plan_id"],
            "reviewer_id": approval.get("reviewer_id", "system"),
            "approved": True,
            "comment": approval.get("comment"),
        },
    )

    pdf_path = report_dir / "report.pdf"
    try:
        build_pdf_report(
            request=request,
            itinerary=state.get("itinerary", []),
            poster_path=poster_local,
            day_image_paths=state.get("day_image_paths", []),
            output_path=str(pdf_path),
            quote=state.get("quote"),
        )
    except Exception:
        logger.warning("PDF generation failed", exc_info=True)

    return {
        "current_stage": "delivered",
        "report_markdown": markdown,
        "report_path": str(pdf_path) if pdf_path.exists() else str(report_path),
    }


def run_verification(state: PlanningState) -> dict:
    """Deterministic verification of the final plan."""
    report = verify_plan(
        request=state["request"],
        itinerary=state.get("itinerary", []),
        quote=state.get("quote"),
    )
    return {"verification_score": report.score}


def mark_failed(state: PlanningState) -> dict:
    return {"current_stage": "failed", "errors": state.get("errors", []) + ["方案在最大重试次数内未通过校验。"]}


def mark_rejected(state: PlanningState) -> dict:
    approval = state.get("approval", {}) or {}
    tool_registry.invoke(
        "submit_for_approval",
        {
            "plan_id": state["plan_id"],
            "reviewer_id": approval.get("reviewer_id", "system"),
            "approved": False,
            "comment": approval.get("comment"),
        },
    )
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
    builder.add_node("run_verification", run_verification)
    builder.add_node("review_repair", review_repair)
    builder.add_node("prepare_poster", prepare_poster)
    builder.add_node("approval_gate", approval_gate)
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
    builder.add_edge("quality_review", "run_verification")
    builder.add_conditional_edges("run_verification", review_decision,
        {"poster": "prepare_poster", "review_repair": "review_repair", "failed": "mark_failed"})
    builder.add_edge("review_repair", "plan_itinerary")
    builder.add_edge("prepare_poster", "approval_gate")
    builder.add_conditional_edges("approval_gate", approval_route,
        {"finalize": "finalize_delivery", "rejected": "mark_rejected"})
    builder.add_edge("finalize_delivery", END)
    builder.add_edge("mark_failed", END)
    builder.add_edge("mark_rejected", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
