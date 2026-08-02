"""LangGraph 工作流：每个节点直接绑定所需的 Tools。

节点 → Tools 绑定：
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

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.checkpoint import create_memory_saver
from app.agents.prompts import (
    COST_ESTIMATION_SYSTEM,
    QUALITY_REVIEW_SYSTEM,
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
    ResourceEnrichmentBatch,
    ScheduleBatch,
    TravelTimeMatrix,
)
from app.services.costs import normalize_event_cost, normalize_itinerary_costs
from app.services.guard import filter_resources as guard_filter_resources
from app.services.model_gateway import ModelGateway, fetch_images_as_data_urls
from app.services.pdf_report import build_pdf_report
from app.services.plan_store import plan_store
from app.services.poster import PosterService
from app.services.ranking import score_resources
from app.services.renderer import render_markdown_report
from app.services.resource_matching import matches_place_name
from app.services.tools.amap import AmapAPIError, AmapClient
from app.services.verifier import verify_plan
from app.tools import travel as _travel_tools  # noqa: F401
from app.tools.registry import tool_registry

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# retrieve_resources → search_attractions + LLM enrichment
# ------------------------------------------------------------------

async def retrieve_resources(state: PlanningState) -> dict:
    request = state["request"]
    settings = get_settings()

    # Keep the general search broad; hard requirements have dedicated exact
    # Amap queries below and should not consume the whole discovery query.
    search_themes = list(dict.fromkeys([*request.themes, *request.interests]))

    provider_tasks = [
        tool_registry.ainvoke(
            "search_attractions",
            {"destination": request.destination, "themes": search_themes or request.themes,
             "audience": request.target_audience, "limit": 8},
        )
    ]
    amap_keywords = f"{request.destination} {' '.join(search_themes[:2])} 景点"
    provider_tasks.append(
        tool_registry.ainvoke(
            "search_poi_amap",
            {"keywords": amap_keywords, "city": request.destination, "limit": 10},
        )
    )
    provider_tasks.extend(
        tool_registry.ainvoke(
            "search_poi_amap",
            {
                "keywords": f"{request.destination} {must_visit}",
                "city": request.destination,
                "limit": 3,
            },
        )
        for must_visit in request.must_visit
    )

    results = await asyncio.gather(*provider_tasks, return_exceptions=True)
    resources: list = []
    search_errors: list[str] = []
    for res in results:
        if isinstance(res, BaseException):
            message = f"{type(res).__name__}: {str(res)[:200]}"
            search_errors.append(message)
            logger.warning("Resource provider unavailable: %s", message)
            continue
        if isinstance(res, list):
            resources.extend(res)
    if not resources:
        raise RuntimeError(f"All resource providers failed: {'; '.join(search_errors)}")

    seen_names: set[str] = set()
    deduped = []
    for r in resources:
        if r.name not in seen_names:
            seen_names.add(r.name)
            deduped.append(r)
    resources = deduped
    resources = [resource for resource in resources if _is_visitable_resource(resource)]

    # Guard: filter out resources with injection patterns
    resources, guard_warnings = guard_filter_resources(resources, scan_source="retrieve_resources")
    for w in guard_warnings:
        logger.warning("Guard: %s", w)

    # First select on provider metadata so expensive multimodal enrichment only
    # processes candidates that can realistically fit into the itinerary.
    resources = _select_resources(score_resources(resources, request), request)
    if not settings.mock_model_mode and resources:
        resources = await _enrich_resources(settings, resources)
        resources = [
            resource for resource in resources
            if _is_visitable_resource(resource)
        ]
        resources = _select_resources(score_resources(resources, request), request)
    if not resources:
        raise RuntimeError("No usable resources remained after safety and preference filtering")

    return {
        "resources": resources,
        "resource_search_provider": "+".join(sorted({r.provider for r in resources})),
        "route_matrix": {},
        "errors": state.get("errors", []) + search_errors,
        "current_stage": "resources_retrieved",
    }


def _select_resources(resources, request):
    """Keep a realistic number of high-quality resources while enforcing avoid rules."""
    usable = [resource for resource in resources if resource.composite_score >= 0]
    limit = max(request.days * 2, len(request.must_visit))
    must_visit = [name.lower() for name in request.must_visit]
    required = []
    for name in must_visit:
        matches = [resource for resource in usable if _matches_must_visit(resource.name, name)]
        matches.sort(
            key=lambda resource: (
                resource.name.lower() != name,
                resource.provider != "amap",
                len(resource.name),
                -resource.composite_score,
            )
        )
        if matches and all(existing.id != matches[0].id for existing in required):
            required.append(matches[0])
    selected = list(required)
    selected_ids = {resource.id for resource in selected}
    structured = [resource for resource in usable if resource.provider == "amap"]
    web = [resource for resource in usable if resource.provider != "amap"]
    candidates = structured if len(structured) >= request.days else [*structured, *web]
    for resource in candidates:
        if resource.id in selected_ids:
            continue
        if any(_matches_must_visit(resource.name, name) for name in must_visit):
            continue
        if any(matches_place_name(resource.name, existing.name) for existing in selected):
            continue
        selected.append(resource)
        selected_ids.add(resource.id)
        if len(selected) >= limit:
            break
    return selected


def _is_visitable_resource_name(name: str) -> bool:
    normalized = name.lower()
    non_visitable_markers = (
        "攻略",
        "资讯",
        "top 10",
        "top 100",
        "排行榜",
        "淘宝",
        "天猫",
        "instagram",
        "酒店",
        "宾馆",
        "民宿",
        "旅行社",
        "旅游线路",
        "景点推荐",
        "自由行",
    )
    return not any(marker in normalized for marker in non_visitable_markers)


def _is_visitable_resource(resource) -> bool:
    return _is_visitable_resource_name(resource.name) and _is_visitable_resource_name(
        resource.source_title or ""
    )


def _matches_must_visit(resource_name: str, must_visit: str) -> bool:
    return matches_place_name(resource_name, must_visit)


async def _enrich_resources(settings, resources):
    gateway = ModelGateway(settings)
    enriched = []
    enriched_count = 0
    batch_size = 4
    for start in range(0, len(resources), batch_size):
        current = resources[start : start + batch_size]
        descriptions = "\n".join(
            f"[{i}] 标题：{resource.name}\n"
            f"    摘要：{(resource.summary or resource.evidence or '')[:300]}\n"
            f"    来源：{resource.source_url or ''}"
            for i, resource in enumerate(current)
        )
        image_urls = [image for resource in current for image in resource.images[:2]]
        image_data_urls = await fetch_images_as_data_urls(
            image_urls, max_images=4
        ) if image_urls else []
        try:
            batch = await gateway.structured_completion(
                model=settings.llm_model_multimodal if image_data_urls else settings.llm_model,
                system_prompt=RESOURCE_ENRICHMENT_SYSTEM,
                user_prompt=(
                    f"目的地资源列表：\n{descriptions}\n\n"
                    + (
                        "以下图片仅对应本批资源，请结合图片判断环境质量和适合人群。"
                        if image_data_urls else ""
                    )
                ),
                schema=ResourceEnrichmentBatch,
                timeout_seconds=90,
                image_urls=image_data_urls or None,
                max_attempts=1,
            )
        except Exception:  # noqa: BLE001 - multimodal-to-text boundary
            logger.warning("Multimodal enrichment failed for batch %d; retrying as text", start)
            batch = await gateway.structured_completion(
                model=settings.llm_model,
                system_prompt=RESOURCE_ENRICHMENT_SYSTEM,
                user_prompt=f"目的地资源列表：\n{descriptions}\n\n",
                schema=ResourceEnrichmentBatch,
                timeout_seconds=90,
            )
        enrichment_map = {
            (item.index if item.index >= 0 else pos): item
            for pos, item in enumerate(batch.resources)
        }
        for index, resource in enumerate(current):
            info = enrichment_map.get(index)
            if info:
                if not info.is_visitable and resource.provider != "amap":
                    logger.info("Discard non-visitable search result: %s", resource.name[:80])
                    continue
                enriched_count += 1
                resource = resource.model_copy(update={
                    "name": info.normalized_name or resource.name,
                    "category": info.category,
                    "price_per_person": info.estimated_price_per_person,
                    "recommended_minutes": info.recommended_minutes,
                    "opening_hours": info.opening_hours,
                    "summary": info.highlights,
                })
            enriched.append(resource)
    logger.info("LLM enriched %d/%d resources", enriched_count, len(resources))
    return enriched


# ------------------------------------------------------------------
# plan_itinerary → calculate_route_matrix + optimize_itinerary + LLM
# ------------------------------------------------------------------

async def plan_itinerary(state: PlanningState) -> dict:
    resources = state["resources"]
    request = state["request"]
    settings = get_settings()

    if "weather_forecast" in state:
        weather = state.get("weather_forecast", [])
    else:
        try:
            weather = await tool_registry.ainvoke(
                "get_weather_forecast",
                {"city": request.destination, "days": min(request.days, 7)},
            )
            if weather:
                logger.info(
                    "Weather forecast for %s: %d days", request.destination, len(weather)
                )
        except (httpx.HTTPError, RuntimeError):
            logger.warning("Weather API unavailable, proceeding without forecast")
            weather = []

    resource_ids = [r.id for r in resources]
    resource_map = {r.id: r for r in resources}

    expected_pairs = {
        f"{source}->{target}"
        for source in resource_ids
        for target in resource_ids
        if source != target
    }
    cached_matrix = state.get("route_matrix", {})
    if expected_pairs.issubset(cached_matrix):
        route_matrix = {key: cached_matrix[key] for key in expected_pairs}
        cached_sources = state.get("travel_time_sources", {})
        travel_time_sources = {
            key: cached_sources.get(key, "cached") for key in expected_pairs
        }
    else:
        travel_times, travel_time_sources = await _estimate_travel_times(settings, resources)

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
                "mode": (
                    "transit"
                    if "public_transit" in request.transport_preferences
                    else "driving"
                ),
            },
        )
    optimized = tool_registry.invoke(
        "optimize_itinerary",
        {
            "resource_ids": resource_ids,
            "distance_matrix": route_matrix,
            "days": request.days,
        },
    )
    if len(optimized) < request.days:
        optimized = [*optimized, *([[]] * (request.days - len(optimized)))]

    if not settings.mock_model_mode:
        days = await _generate_schedule(
            settings,
            request,
            optimized,
            resource_map,
            weather,
            route_matrix,
            state.get("repair_feedback", []),
        )
    else:
        days = _basic_schedule(request, optimized, resource_map)
    return {
        "itinerary": days,
        "route_matrix": route_matrix,
        "travel_time_sources": travel_time_sources,
        "weather_forecast": weather,
        "current_stage": "itinerary_planned",
    }


async def _estimate_travel_times(settings, resources) -> tuple[dict[str, int], dict[str, str]]:
    """Estimate travel times: Amap API for resources with coordinates, LLM fallback."""
    travel_times: dict[str, int] = {}
    sources: dict[str, str] = {}

    coords_available = [r for r in resources if r.lng and r.lat]
    if settings.amap_api_key and len(coords_available) >= 2:
        client = AmapClient(settings.amap_api_key, settings.amap_base_url)
        pairs = []
        for source in coords_available:
            for target in coords_available:
                if source.id == target.id:
                    continue
                pairs.append(
                    (
                        f"{source.id}->{target.id}",
                        f"{source.lng},{source.lat}",
                        f"{target.lng},{target.lat}",
                    )
                )
        for start in range(0, len(pairs), 3):
            current = pairs[start : start + 3]
            results = await asyncio.gather(
                *(
                    client.travel_time(origin, destination, mode="transit")
                    for _, origin, destination in current
                ),
                return_exceptions=True,
            )
            for (key, _, _), result in zip(current, results, strict=True):
                if isinstance(result, int):
                    travel_times[key] = result
                    sources[key] = "amap"
                elif isinstance(result, (AmapAPIError, httpx.HTTPError)):
                    logger.warning("Amap travel time unavailable for %s: %s", key, result)
                else:
                    logger.warning("Unexpected Amap result for %s: %r", key, result)
            if start + 3 < len(pairs):
                await asyncio.sleep(1.05)
        logger.info("Amap real travel times: %d pairs", len(travel_times))

    needs_llm = not settings.mock_model_mode and (
        len(travel_times) < len(resources) * (len(resources) - 1)
    )
    if needs_llm:
        gateway = ModelGateway(settings)
        resource_list = "\n".join(f"[{i}] {r.name}（{r.location}）" for i, r in enumerate(resources))
        matrix = await gateway.structured_completion(
            model=settings.llm_model_complex,
            system_prompt=TRAVEL_TIME_SYSTEM,
            user_prompt=f"资源列表（共 {len(resources)} 个）：\n{resource_list}",
            schema=TravelTimeMatrix,
            timeout_seconds=120,
        )
        for pair in matrix.pairs:
            if 0 <= pair.from_index < len(resources) and 0 <= pair.to_index < len(resources):
                key = f"{resources[pair.from_index].id}->{resources[pair.to_index].id}"
                if key not in travel_times:
                    travel_times[key] = pair.time
                    sources[key] = "llm_estimate"
        logger.info("LLM estimated travel times, total pairs: %d", len(travel_times))

    for source in resources:
        for target in resources:
            if source.id == target.id:
                continue
            key = f"{source.id}->{target.id}"
            if key not in travel_times:
                travel_times[key] = 30
                sources[key] = "default_estimate"

    return travel_times, sources


async def _generate_schedule(
    settings,
    request,
    optimized,
    resource_map,
    weather,
    route_matrix=None,
    repair_feedback=None,
) -> list[ItineraryDay]:
    """Generate one day per model call to keep latency and output size bounded."""
    gateway = ModelGateway(settings)
    route_matrix = route_matrix or {}
    repair_feedback = repair_feedback or []
    if weather:
        weather_text = "\n".join(
            f"- {w.get('date', '?')}：{w.get('text_day', '未知')}，"
            f"{w.get('temp_min', '?')}~{w.get('temp_max', '?')}℃，"
            f"风力{w.get('wind_scale_day', '-')}级，湿度{w.get('humidity', '-')}%"
            for w in weather
        )
    else:
        weather_text = "暂无天气数据"

    days: list[ItineraryDay] = []
    for day_index, day_resources in enumerate(optimized, start=1):
        if not day_resources:
            days.append(_free_day(day_index))
            continue
        resources_payload = [
            {
                "resource_id": resource_id,
                "name": resource_map[resource_id].name,
                "category": resource_map[resource_id].category,
                "location": resource_map[resource_id].location,
                "recommended_minutes": resource_map[resource_id].recommended_minutes,
                "price_per_person": resource_map[resource_id].price_per_person,
                "opening_hours": resource_map[resource_id].opening_hours,
            }
            for resource_id in day_resources
        ]
        relevant_times = {
            key: value
            for key, value in route_matrix.items()
            if key.split("->", 1)[0] in day_resources and key.split("->", 1)[1] in day_resources
        }
        schedule_input = json.dumps(
            {
                "day": day_index,
                "resources": resources_payload,
                "travel_minutes": relevant_times,
            },
            ensure_ascii=False,
        )
        feedback_text = "；".join(repair_feedback[:8]) or "无"
        batch = await gateway.structured_completion(
            model=settings.llm_model_complex,
            system_prompt=SCHEDULE_SYSTEM,
            user_prompt=(
                f"产品：{request.title}\n目的地：{request.destination}\n"
                f"目标人群：{request.target_audience}\n"
                f"总行程：{request.days} 天 {request.nights} 晚；本次只生成第 {day_index} 天。\n"
                f"主题：{', '.join(request.themes)}\n"
                f"约束：{', '.join(request.constraints) or '无'}\n"
                f"上轮修复意见：{feedback_text}\n\n"
                f"目的地天气：\n{weather_text}\n\n"
                f"本日资源与交通时间：\n{schedule_input}\n\n"
                f"只输出 day={day_index} 的一个日程，不要输出其他天。"
            ),
            schema=ScheduleBatch,
            timeout_seconds=120,
        )
        if not batch.days:
            raise RuntimeError(f"LLM returned no schedule for day {day_index}")
        day_info = next((item for item in batch.days if item.day == day_index), batch.days[0])
        events = [
            ItineraryEvent(
                start_time=e.start_time, end_time=e.end_time, title=e.title,
                resource_id=e.resource_id, category=e.category,
                description=e.description, cost_per_person=e.cost_per_person,
            )
            for e in day_info.events
        ]
        pace_limits = {"intense": 720, "moderate": 600, "relaxed": 480}
        pace_value = request.pace.value if hasattr(request.pace, "value") else "moderate"
        events = _normalize_schedule_events(
            events,
            resource_map,
            max_span_minutes=pace_limits.get(pace_value, 600),
        )
        if not events:
            raise RuntimeError(f"LLM returned an empty schedule for day {day_index}")
        days.append(
            ItineraryDay(
                day=day_index,
                theme=day_info.theme or f"{request.destination}探索 · 第 {day_index} 天",
                events=events,
            )
        )

    logger.info("LLM generated schedule for %d days", len(days))
    return days


def _event_minutes(value: str) -> int | None:
    try:
        hour, minute = value.split(":", 1)
        parsed = int(hour) * 60 + int(minute)
        return parsed if 0 <= parsed < 24 * 60 else None
    except (TypeError, ValueError):
        return None


def _normalize_schedule_events(
    events: list[ItineraryEvent],
    resource_map: dict,
    *,
    max_span_minutes: int,
) -> list[ItineraryEvent]:
    """Repair harmless model omissions and enforce the deterministic pace window."""
    normalized: list[ItineraryEvent] = []
    for event in events:
        start = _event_minutes(event.start_time)
        end = _event_minutes(event.end_time)
        if start is None or end is None:
            normalized.append(event)
            continue
        resource = resource_map.get(event.resource_id)
        title = event.title.strip()
        category = event.category.strip() or "activity"
        if not title:
            if resource is not None:
                title = resource.name
                category = resource.category
            elif start < 13 * 60 + 30 and end > 11 * 60 + 30 and end - start >= 30:
                title = "午餐与午休"
                category = "dining"
            else:
                title = "交通与休息"
                category = "transport"
        if end <= start:
            if resource is None:
                continue
            end = min(start + max(30, min(resource.recommended_minutes, 60)), 23 * 60 + 59)
        normalized.append(
            event.model_copy(
                update={
                    "title": title,
                    "category": category,
                    "end_time": f"{end // 60:02d}:{end % 60:02d}",
                }
            )
        )

    normalized.sort(key=lambda event: _event_minutes(event.start_time) or 0)
    lunch_categories = {"dining", "break", "food", "restaurant", "cuisine"}
    transport_categories = {"transport", "logistics", "transfer", "交通", "集合"}
    lunch_keywords = ("午餐", "午休", "午饭", "用餐", "美食", "餐厅")
    has_lunch = any(
        (
            event.category.lower() not in transport_categories
            and (
                event.category.lower() in lunch_categories
                or any(keyword in event.title for keyword in lunch_keywords)
            )
        )
        and (_event_minutes(event.start_time) or 0) < 13 * 60 + 30
        and (_event_minutes(event.end_time) or 0) > 11 * 60 + 30
        for event in normalized
    )
    if not has_lunch:
        lunch_start = _find_lunch_gap(normalized)
        if lunch_start is not None:
            normalized.append(
                ItineraryEvent(
                    start_time=f"{lunch_start // 60:02d}:{lunch_start % 60:02d}",
                    end_time=f"{(lunch_start + 60) // 60:02d}:{(lunch_start + 60) % 60:02d}",
                    title="午餐与午休",
                    category="dining",
                    description="预留用餐与休息时间。",
                    cost_per_person=80,
                )
            )
            normalized.sort(key=lambda event: _event_minutes(event.start_time) or 0)
    normalized = _repair_schedule_overlaps(normalized)
    if not normalized:
        return normalized
    day_start = _event_minutes(normalized[0].start_time)
    if day_start is None:
        return normalized
    cutoff = day_start + max_span_minutes
    while normalized:
        last = normalized[-1]
        last_end = _event_minutes(last.end_time)
        if last_end is None or last_end <= cutoff:
            break
        last_start = _event_minutes(last.start_time)
        if not last.resource_id:
            normalized.pop()
            continue
        if last_start is not None and last_start < cutoff:
            normalized[-1] = last.model_copy(
                update={"end_time": f"{cutoff // 60:02d}:{cutoff % 60:02d}"}
            )
        break
    return [normalize_event_cost(event, resource_map) for event in normalized]


def _is_lunch_event(event: ItineraryEvent) -> bool:
    category = event.category.lower()
    if category in {"transport", "logistics", "transfer", "交通", "集合"}:
        return False
    return category in {"dining", "meal", "food", "restaurant", "cuisine", "餐饮", "用餐"} or any(
        keyword in event.title for keyword in ("午餐", "午休", "午饭", "用餐")
    )


def _repair_schedule_overlaps(events: list[ItineraryEvent]) -> list[ItineraryEvent]:
    """修复模型生成的相邻时间重叠，优先保留独立午餐时段。"""
    repaired: list[ItineraryEvent] = []
    for event in events:
        start = _event_minutes(event.start_time)
        end = _event_minutes(event.end_time)
        if start is None or end is None or not repaired:
            repaired.append(event)
            continue
        previous = repaired[-1]
        previous_start = _event_minutes(previous.start_time)
        previous_end = _event_minutes(previous.end_time)
        if previous_start is None or previous_end is None or start >= previous_end:
            repaired.append(event)
            continue

        if _is_lunch_event(event) and not _is_lunch_event(previous) and start - previous_start >= 30:
            repaired[-1] = previous.model_copy(
                update={"end_time": f"{start // 60:02d}:{start % 60:02d}"}
            )
            repaired.append(event)
            continue

        duration = max(30, end - start)
        shifted_start = previous_end
        shifted_end = min(23 * 60 + 59, shifted_start + duration)
        repaired.append(
            event.model_copy(
                update={
                    "start_time": f"{shifted_start // 60:02d}:{shifted_start % 60:02d}",
                    "end_time": f"{shifted_end // 60:02d}:{shifted_end % 60:02d}",
                }
            )
        )
    return repaired


def _find_lunch_gap(events: list[ItineraryEvent]) -> int | None:
    cursor = 11 * 60 + 30
    window_end = 13 * 60 + 30
    for event in events:
        start = _event_minutes(event.start_time)
        end = _event_minutes(event.end_time)
        if start is None or end is None or end <= cursor or start >= window_end:
            continue
        if start - cursor >= 60:
            return cursor
        cursor = max(cursor, end)
        if cursor >= window_end:
            return None
    return cursor if window_end - cursor >= 60 else None


def _basic_schedule(request, optimized, resource_map) -> list[ItineraryDay]:
    days: list[ItineraryDay] = []
    for day_index, day_resources in enumerate(optimized, start=1):
        if not day_resources:
            days.append(_free_day(day_index))
            continue
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


def _free_day(day_number: int) -> ItineraryDay:
    """占位天：资源不足时的自由活动/休整安排，避免空天导致校验失败。"""
    return ItineraryDay(
        day=day_number,
        theme="自由活动 · 休整",
        events=[
            ItineraryEvent(
                start_time="09:00", end_time="11:00", title="自由活动",
                category="break", description="预留自由活动时间，可按兴趣自行安排。", cost_per_person=0,
            ),
            ItineraryEvent(
                start_time="11:30", end_time="12:30", title="午餐与午休",
                category="dining", description="预留用餐与休息时间。", cost_per_person=80,
            ),
            ItineraryEvent(
                start_time="14:00", end_time="17:00", title="休整 / 自由安排",
                category="break", description="预留休整、购物或返程时间。", cost_per_person=0,
            ),
        ],
    )


# ------------------------------------------------------------------
# validate_constraints → deterministic
# ------------------------------------------------------------------

def validate_constraints(state: PlanningState) -> dict:
    """Comprehensive deterministic validation of the itinerary."""
    request = state["request"]
    issues: list[ConstraintIssue] = []
    actual_max_daily = 0
    time_conflict_count = 0
    scheduled_resource_days: dict[str, int] = {}
    known_resource_ids = {resource.id for resource in state.get("resources", [])}

    expected_days = set(range(1, request.days + 1))
    actual_days = {day.day for day in state.get("itinerary", [])}
    missing_days = sorted(expected_days - actual_days)
    unexpected_days = sorted(actual_days - expected_days)
    if missing_days:
        issues.append(
            ConstraintIssue(
                code="MISSING_ITINERARY_DAYS",
                severity="blocking",
                message=f"行程缺少第 {', '.join(map(str, missing_days))} 天的安排。",
                suggested_action="为缺失日期补充可执行的活动、用餐与休息安排。",
            )
        )
    if unexpected_days:
        issues.append(
            ConstraintIssue(
                code="UNEXPECTED_ITINERARY_DAYS",
                severity="blocking",
                message=f"行程包含超出请求范围的第 {', '.join(map(str, unexpected_days))} 天。",
                suggested_action="移除超出用户请求天数的行程。",
            )
        )
    if len(state.get("itinerary", [])) != request.days and not missing_days and not unexpected_days:
        issues.append(
            ConstraintIssue(
                code="DUPLICATE_ITINERARY_DAYS",
                severity="blocking",
                message="行程日序号存在重复，无法完整覆盖用户请求的天数。",
                suggested_action="重新按连续日序号生成分日行程。",
            )
        )

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
                code="DAILY_DURATION_EXCEEDED", severity="blocking",
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

        lunch_categories = {"dining", "break", "food", "restaurant", "cuisine"}
        lunch_keywords = ("午餐", "午休", "午饭", "用餐", "美食", "餐厅")
        has_lunch = False
        for event in day.events:
            food_like = (
                event.category.lower() in lunch_categories
                or any(keyword in event.title for keyword in lunch_keywords)
            )
            if not food_like:
                continue
            try:
                event_start = int(event.start_time[:2]) * 60 + int(event.start_time[3:])
                event_end = int(event.end_time[:2]) * 60 + int(event.end_time[3:])
                if event_start < 13 * 60 + 30 and event_end > 11 * 60 + 30:
                    has_lunch = True
                    break
            except (ValueError, IndexError):
                if "午餐" in event.title or "午休" in event.title:
                    has_lunch = True
                    break
        if not has_lunch:
            issues.append(ConstraintIssue(
                code="LUNCH_MISSING",
                severity="blocking",
                message=f"第 {day.day} 天缺少明确的午餐或午休安排。",
                suggested_action="在 11:30-13:30 之间增加午餐或午休节点。",
            ))

        for event in day.events:
            if not event.resource_id:
                continue
            if event.resource_id not in known_resource_ids:
                issues.append(ConstraintIssue(
                    code="UNKNOWN_RESOURCE",
                    severity="blocking",
                    message=f"第 {day.day} 天活动「{event.title}」引用了未知资源。",
                    event_title=event.title,
                ))
                continue
            previous_day = scheduled_resource_days.get(event.resource_id)
            if previous_day is not None:
                location = (
                    f"第 {previous_day} 天和第 {day.day} 天"
                    if previous_day != day.day
                    else f"第 {day.day} 天内"
                )
                issues.append(ConstraintIssue(
                    code="DUPLICATE_RESOURCE",
                    severity="blocking",
                    message=f"活动「{event.title}」在{location}重复安排。",
                    event_title=event.title,
                ))
            scheduled_resource_days[event.resource_id] = day.day

    must_visit = set(request.must_visit)
    scheduled_names = {e.title for day in state["itinerary"] for e in day.events}
    covered = sum(
        1 for must_visit_name in must_visit
        if any(matches_place_name(name, must_visit_name) for name in scheduled_names)
    )
    must_visit_coverage = covered / len(must_visit) if must_visit else 1.0
    if must_visit and must_visit_coverage < 1.0:
        missing = [
            must_visit_name
            for must_visit_name in must_visit
            if not any(matches_place_name(name, must_visit_name) for name in scheduled_names)
        ]
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
    if state.get("constraint_retry_count", 0) < 2:
        return "repair"
    return "failed"


# ------------------------------------------------------------------
# repair_plan → search_attractions (emergency alternatives)
# ------------------------------------------------------------------

async def repair_plan(state: PlanningState) -> dict:
    constraint_retry = state.get("constraint_retry_count", 0) + 1
    retry = state.get("retry_count", 0) + 1
    request = state["request"]
    settings = get_settings()
    issues = state.get("constraint_report", ConstraintReport(
        valid=False, score=0, issues=[], total_travel_minutes=0, max_daily_minutes=0,
    )).issues

    feedback = [issue.message for issue in issues]
    logger.info("Constraint repair attempt %d: %s", constraint_retry, "; ".join(feedback[:3]))

    needs_resource_search = any(
        issue.code in {"EMPTY_DAY", "MUST_VISIT_MISSING"} for issue in issues
    )
    if not settings.mock_model_mode and needs_resource_search:
        try:
            missing_must_visits = [
                must_visit
                for must_visit in request.must_visit
                if any(
                    issue.code == "MUST_VISIT_MISSING" and must_visit in issue.message
                    for issue in issues
                )
            ]
            search_tasks = [
                tool_registry.ainvoke(
                    "search_attractions",
                    {
                        "destination": request.destination,
                        "themes": [
                            *missing_must_visits,
                            *request.must_visit,
                            *request.themes,
                            "替代方案",
                        ],
                        "audience": request.target_audience,
                        "limit": 6,
                    },
                )
            ]
            search_tasks.extend(
                tool_registry.ainvoke(
                    "search_poi_amap",
                    {
                        "keywords": f"{request.destination} {must_visit}",
                        "city": request.destination,
                        "limit": 3,
                    },
                )
                for must_visit in missing_must_visits
            )
            search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
            alternatives = [
                resource
                for result in search_results
                if isinstance(result, list)
                for resource in result
            ]
            if alternatives:
                existing_ids = {r.id for r in state.get("resources", [])}
                new_resources, guard_warnings = guard_filter_resources(
                    [r for r in alternatives if r.id not in existing_ids],
                    scan_source="repair_plan",
                )
                for w in guard_warnings:
                    logger.warning("Guard: %s", w)
                if new_resources:
                    combined = list(state.get("resources", [])) + new_resources[:3]
                    combined = _select_resources(score_resources(combined, request), request)
                    return {
                        "retry_count": retry,
                        "constraint_retry_count": constraint_retry,
                        "repair_feedback": feedback,
                        "resources": combined,
                        "current_stage": "repairing",
                    }
        except (httpx.HTTPError, RuntimeError):
            logger.warning("Emergency alternative search failed", exc_info=True)

    return {
        "retry_count": retry,
        "constraint_retry_count": constraint_retry,
        "repair_feedback": feedback,
        "current_stage": "repairing",
    }


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
        timeout_seconds=120,
    )
    cost_items = [
        QuoteItem(category=item.category, description=item.description, amount=item.amount)
        for item in breakdown.items
        if item.amount > 0
    ]
    if len(cost_items) < len(breakdown.items):
        logger.info(
            "Ignored %d zero-amount cost items returned by the model",
            len(breakdown.items) - len(cost_items),
        )
    if not cost_items:
        logger.warning("Model returned no positive cost items; using deterministic fallback")
        cost_items = _fallback_cost_items(request, resources)
    quote = tool_registry.invoke(
        "calculate_product_cost",
        {
            "group_size": request.group_size,
            "target_margin_rate": request.target_margin_rate,
            "budget_per_person": request.budget_per_person,
            "cost_items": [i.model_dump() for i in cost_items],
        },
    )
    logger.info("LLM cost estimation: total=%d", quote.total_cost)
    return quote


def _fallback_cost_items(request, resources) -> list[QuoteItem]:
    """Build a conservative non-zero estimate when every model item is free/invalid."""
    from math import ceil

    rooms = ceil(request.group_size / 2)
    items = [
        QuoteItem(
            category="交通",
            description="市内交通与接送预留",
            amount=max(800, request.group_size * request.days * 60),
        ),
        QuoteItem(
            category="餐饮",
            description="早餐及正餐预留",
            amount=max(300, request.group_size * request.days * 120),
        ),
        QuoteItem(
            category="服务",
            description="保险、导游与综合服务",
            amount=max(300, request.group_size * request.days * 40),
        ),
    ]
    if request.nights > 0:
        items.append(
            QuoteItem(
                category="住宿",
                description="标准双人间估算",
                amount=max(400, rooms * request.nights * 400),
            )
        )
    ticket_total = sum(resource.price_per_person for resource in resources) * request.group_size
    if ticket_total > 0:
        items.append(
            QuoteItem(
                category="门票及课程",
                description="按已选资源参考价汇总",
                amount=ticket_total,
            )
        )
    return items


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
    """Block delivery on any deterministic or LLM-reported blocking issue."""
    verification_score = state.get("verification_score", 0)
    quality = state.get("quality_report")
    quality_score = quality.overall_score if quality else 0
    review_repairs = state.get("review_retry_count", 0)
    has_blocking_issues = bool(quality and quality.blocking_issues)
    constraint_valid = bool(state.get("constraint_report") and state["constraint_report"].valid)

    if (
        state.get("verification_blocking_count", 0) == 0
        and not has_blocking_issues
        and constraint_valid
        and verification_score >= REVIEW_PASS_THRESHOLD
        and quality_score >= REVIEW_PASS_THRESHOLD
    ):
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
    review_retry = state.get("review_retry_count", 0) + 1
    quality = state.get("quality_report")
    issues = []
    if quality:
        issues.extend(quality.blocking_issues)
        issues.extend(quality.suggestions[:3])
    constraint = state.get("constraint_report")
    if constraint:
        issues.extend(i.message for i in constraint.issues if i.severity == "blocking")
    issues.extend(state.get("verification_issues", []))

    logger.info("Review repair #%d: %s", review_retry, "; ".join(issues[:5]))
    return {
        "retry_count": retry,
        "review_retry_count": review_retry,
        "repair_feedback": issues,
        "current_stage": "review_repairing",
    }


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
            local_path = await service.download_image(result["url"], plan_id, name=label)
        except Exception:
            logger.warning("Download failed for %s", label, exc_info=True)
    return result, local_path


async def prepare_poster(state: PlanningState) -> dict:
    request = state["request"]
    settings = get_settings()
    service = PosterService(settings)

    # 封面：整体规划视角（目的地 + 产品名 + 必去地标/主题）
    cover_elements = ["目的地标志性景观", "旅行氛围"]
    if request.must_visit:
        cover_elements.insert(0, request.must_visit[0][:12])
    elif request.themes:
        cover_elements.insert(0, request.themes[0][:12])

    cover_brief = PosterBrief(
        destination=request.destination,
        product_theme=request.title,
        target_audience=request.target_audience,
        visual_style="自然、明亮、具有层次的编辑插画风",
        primary_colors=["湖水绿", "暖金色", "宣纸白"],
        visual_elements=cover_elements,
        negative_elements=["文字", "Logo", "二维码", "水印"],
        aspect_ratio="3:4",
    )

    if settings.mock_imagegen:
        poster, _ = await _generate_and_download(service, cover_brief, state["plan_id"], "cover")
        mock_day_paths = [
            ["/demo-assets/hangzhou-poster-background.png"]
            for _ in state.get("itinerary", [])
        ]
        return {"poster_brief": cover_brief, "poster_asset": poster,
                "day_image_paths": mock_day_paths, "poster_ready": True,
                "current_stage": "poster_generated"}

    # ComfyUI typically executes one GPU job at a time. Submit sequentially so
    # pending jobs do not consume their execution timeout while waiting in queue.
    try:
        poster, cover_path = await _generate_required_image(
            service, cover_brief, state["plan_id"], "cover"
        )
    except Exception as exc:  # noqa: BLE001 - external image-generation boundary
        message = f"cover: {type(exc).__name__}: {str(exc)[:200]}"
        logger.error("Required cover generation failed: %s", message)
        return await _prepare_real_image_fallback(
            state,
            service,
            cover_brief,
            reason=message,
        )
    poster["local_path"] = cover_path

    days = state.get("itinerary", [])
    day_image_paths: list[list[str]] = [[] for _ in days]
    image_errors: list[str] = []
    for day_index, day in enumerate(days):
        count = _day_image_count(day)
        for image_index, brief in enumerate(_day_briefs(request, day, count)):
            label = f"day{day.day}" if image_index == 0 else f"day{day.day}-{image_index + 1}"
            try:
                _, local_path = await _generate_required_image(
                    service, brief, state["plan_id"], label
                )
                day_image_paths[day_index].append(local_path)
            except Exception as exc:  # noqa: BLE001 - external image-generation boundary
                message = f"{label}: {type(exc).__name__}: {str(exc)[:160]}"
                image_errors.append(message)
                logger.error("Required day image failed: %s", message)
                break

    poster_ready = not image_errors and all(day_image_paths)

    return {
        "poster_brief": cover_brief,
        "poster_asset": poster,
        "day_image_paths": day_image_paths,
        "poster_ready": poster_ready,
        "errors": state.get("errors", []) + image_errors,
        "current_stage": "poster_generated" if poster_ready else "poster_failed",
    }


async def _prepare_real_image_fallback(
    state: PlanningState,
    service: PosterService,
    cover_brief: PosterBrief,
    *,
    reason: str,
) -> dict:
    """Use real resource photos or Amap static maps when ComfyUI is unavailable."""
    resources = list(state.get("resources", []))
    resource_map = {resource.id: resource for resource in resources}
    used_urls: set[str] = set()
    cover_candidates = sorted(
        resources,
        key=lambda resource: not any(
            _matches_must_visit(resource.name, name.lower())
            for name in state["request"].must_visit
        ),
    )
    cover_path, cover_provider = await _download_real_fallback_image(
        service,
        cover_candidates,
        state["plan_id"],
        "cover",
        used_urls,
    )
    if not cover_path:
        return {
            "poster_brief": cover_brief,
            "poster_ready": False,
            "errors": state.get("errors", []) + [f"image fallback failed: {reason}"],
            "current_stage": "poster_failed",
        }

    day_image_paths: list[list[str]] = []
    for index, day in enumerate(state.get("itinerary", [])):
        day_resources = [
            resource_map[event.resource_id]
            for event in day.events
            if event.resource_id in resource_map
        ]
        if not day_resources and resources:
            offset = index % len(resources)
            day_resources = [*resources[offset:], *resources[:offset]]
        local_path, _ = await _download_real_fallback_image(
            service,
            day_resources or resources,
            state["plan_id"],
            f"day{day.day}",
            used_urls,
        )
        day_image_paths.append([local_path] if local_path else [])

    poster_ready = bool(day_image_paths) and all(day_image_paths)
    return {
        "poster_brief": cover_brief,
        "poster_asset": {
            "asset_id": f"fallback-{state['plan_id']}",
            "status": "generated",
            "provider": cover_provider,
            "local_path": cover_path,
            "url": cover_path,
            "note": reason,
        },
        "day_image_paths": day_image_paths,
        "poster_ready": poster_ready,
        "errors": state.get("errors", []) + ([] if poster_ready else [f"day image fallback incomplete: {reason}"]),
        "current_stage": "poster_generated" if poster_ready else "poster_failed",
    }


async def _download_real_fallback_image(
    service: PosterService,
    resources: list,
    plan_id: str,
    label: str,
    used_urls: set[str],
) -> tuple[str, str]:
    for resource in resources:
        for image_url in resource.images:
            if image_url in used_urls:
                continue
            try:
                local_path = await service.download_image(image_url, plan_id, name=label)
                used_urls.add(image_url)
                return local_path, "resource_photo"
            except (httpx.HTTPError, RuntimeError):
                logger.warning("Fallback resource image unavailable for %s", resource.name)

    settings = service.settings
    if settings.amap_api_key:
        for resource in resources:
            if resource.lng is None or resource.lat is None:
                continue
            params = {
                "key": settings.amap_api_key,
                "location": f"{resource.lng},{resource.lat}",
                "zoom": 13,
                "size": "750*500",
                "markers": f"mid,,A:{resource.lng},{resource.lat}",
            }
            image_url = str(httpx.URL(f"{settings.amap_base_url.rstrip('/')}/staticmap", params=params))
            try:
                local_path = await service.download_image(image_url, plan_id, name=label)
                return local_path, "amap_static_map"
            except (httpx.HTTPError, RuntimeError):
                logger.warning("Amap static-map fallback unavailable for %s", resource.name)
    return "", ""


async def _generate_required_image(
    service: PosterService, brief: PosterBrief, plan_id: str, label: str,
) -> tuple[dict[str, str], str]:
    """Generate and download a required artwork with bounded retries."""
    attempts = getattr(service.settings, "imagegen_max_attempts", 2)
    failures: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            result, local_path = await _generate_and_download(service, brief, plan_id, label)
            if local_path:
                return result, local_path
            raise RuntimeError("image download returned no local path")
        except Exception as exc:  # noqa: BLE001 - retry boundary for remote service
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:120]}")
            if attempt < attempts:
                logger.warning("Retrying required image %s after attempt %d", label, attempt)
                await asyncio.sleep(min(2**attempt, 5))
    raise RuntimeError("; ".join(failures))


def poster_route(state: PlanningState) -> Literal["approval", "failed"]:
    """Only expose a plan for approval after every required artwork exists."""
    return "approval" if state.get("poster_ready", False) else "failed"


def _day_image_count(day) -> int:
    """Decide how many images a day needs based on its richness."""
    active = [event for event in day.events if event.resource_id]
    if len(active) >= 6:
        return 3
    if len(active) >= 4:
        return 2
    return 1


def _day_briefs(request, day, count: int) -> list[PosterBrief]:
    """Build one brief per image for a day; extra briefs highlight a landmark."""
    active = [event for event in day.events if event.resource_id]
    briefs = [
        PosterBrief(
            destination=request.destination,
            product_theme=day.theme,
            target_audience=request.target_audience,
            visual_style="水彩插画风格，柔和色调，留白充足",
            primary_colors=["湖水绿", "暖金色", "宣纸白"],
            visual_elements=[request.destination, day.theme],
            negative_elements=["文字", "Logo", "二维码", "水印", "人物"],
            aspect_ratio="4:3",
        )
    ]
    for index in range(1, count):
        highlight = active[index - 1].title[:8] if index - 1 < len(active) else day.theme
        briefs.append(
            PosterBrief(
                destination=request.destination,
                product_theme=f"{day.theme} · 亮点",
                target_audience=request.target_audience,
                visual_style="水彩插画风格，柔和色调，留白充足",
                primary_colors=["湖水绿", "暖金色", "宣纸白"],
                visual_elements=[request.destination, day.theme, highlight],
                negative_elements=["文字", "Logo", "二维码", "水印", "人物"],
                aspect_ratio="4:3",
            )
        )
    return briefs


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
    itinerary = normalize_itinerary_costs(
        state.get("itinerary", []),
        state.get("resources", []),
    )
    markdown = render_markdown_report(
        request=request,
        itinerary=itinerary,
        quote=state.get("quote"),
        quality_report=state.get("quality_report"),
        constraint_report=state.get("constraint_report"),
        weather_forecast=state.get("weather_forecast"),
        poster_local_path=poster_local,
        day_image_paths=state.get("day_image_paths", []),
        resources=state.get("resources", []),
    )
    from app.services.plan_store import DATA_DIR
    report_dir = DATA_DIR / "plans" / state["plan_id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.md"
    report_path.write_text(markdown, encoding="utf-8")
    logger.info("Markdown report → %s", report_path)

    snapshot = {
        "request": request.model_dump(mode="json"),
        "resources": [resource.model_dump(mode="json") for resource in state.get("resources", [])],
        "itinerary": [d.model_dump() for d in itinerary],
        "quote": state["quote"].model_dump() if state.get("quote") else None,
        "quality_report": state["quality_report"].model_dump() if state.get("quality_report") else None,
        "constraint_report": (
            state["constraint_report"].model_dump() if state.get("constraint_report") else None
        ),
        "poster_asset": state.get("poster_asset"),
        "day_image_paths": state.get("day_image_paths", []),
        "verification_score": state.get("verification_score"),
        "weather_forecast": state.get("weather_forecast"),
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
    build_pdf_report(
        request=request,
        itinerary=itinerary,
        poster_path=poster_local,
        day_image_paths=state.get("day_image_paths", []),
        output_path=str(pdf_path),
        quote=state.get("quote"),
        weather_forecast=state.get("weather_forecast"),
    )

    return {
        "itinerary": itinerary,
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
    return {
        "verification_score": report.score,
        "verification_passed": report.passed,
        "verification_blocking_count": report.blocking_count,
        "verification_issues": [check.detail for check in report.checks if not check.passed],
    }


def mark_failed(state: PlanningState) -> dict:
    errors = state.get("errors", [])
    if not errors:
        errors = ["方案在最大重试次数内未通过校验。"]
    return {"current_stage": "failed", "errors": errors}


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
    builder.add_edge(START, "retrieve_resources")
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
    builder.add_conditional_edges(
        "prepare_poster",
        poster_route,
        {"approval": "approval_gate", "failed": "mark_failed"},
    )
    builder.add_conditional_edges("approval_gate", approval_route,
        {"finalize": "finalize_delivery", "rejected": "mark_rejected"})
    builder.add_edge("finalize_delivery", END)
    builder.add_edge("mark_failed", END)
    builder.add_edge("mark_rejected", END)

    return builder.compile(checkpointer=checkpointer or create_memory_saver())
