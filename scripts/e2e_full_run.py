#!/usr/bin/env python3
"""End-to-end full workflow test with real LLM / Tavily / ComfyUI services.

Usage:
    uv run python scripts/e2e_full_run.py
    uv run python scripts/e2e_full_run.py --destination 杭州 --days 5 --nights 4 \
        --group-size 5 --budget-per-person 4000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.agents.graph import build_planning_graph
from app.config import get_settings
from app.models.schemas import PlanRequest, ProductType, TravelPace
from app.services.evaluation import evaluate_plan


def banner(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行真实旅行规划端到端验收")
    parser.add_argument("--title", default="杭州两天一夜亲子研学之旅")
    parser.add_argument("--product-type", choices=[item.value for item in ProductType], default=ProductType.FAMILY.value)
    parser.add_argument("--destination", default="杭州")
    parser.add_argument("--departure-date", help="YYYY-MM-DD，省略时从当天开始")
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--nights", type=int, default=1)
    parser.add_argument("--group-size", type=int, default=30)
    parser.add_argument("--budget-per-person", type=int, default=1800)
    parser.add_argument("--target-margin-rate", type=float, default=0.15)
    parser.add_argument("--target-audience", default="8-12岁儿童及家长")
    parser.add_argument("--themes", default="自然教育,历史文化")
    parser.add_argument("--interests", default="自然教育,历史文化")
    parser.add_argument("--must-visit", default="")
    parser.add_argument("--avoid", default="")
    parser.add_argument("--hard-constraints", default="连续乘车不超过90分钟")
    parser.add_argument("--pace", choices=[item.value for item in TravelPace], default=TravelPace.MODERATE.value)
    parser.add_argument("--transport-preferences", default="public_transit,walking")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    print(f"LLM (complex):  {settings.llm_model_complex}")
    print(f"LLM (simple):   {settings.llm_model_simple}")
    print(f"LLM (multimodal): {settings.llm_model_multimodal}")
    print(f"ComfyUI:        {settings.imagegen_api_url}")
    print(f"Mock imagegen:  {settings.mock_imagegen}")
    print(f"Mock model:     {settings.mock_model_mode}")
    print(f"Tavily search:  {settings.tavily_search_enabled}")

    request = PlanRequest(
        title=args.title,
        product_type=ProductType(args.product_type),
        destination=args.destination,
        departure_date=date.fromisoformat(args.departure_date) if args.departure_date else None,
        days=args.days,
        nights=args.nights,
        group_size=args.group_size,
        budget_per_person=args.budget_per_person,
        target_margin_rate=args.target_margin_rate,
        target_audience=args.target_audience,
        themes=_csv(args.themes),
        hard_constraints=_csv(args.hard_constraints),
        interests=_csv(args.interests),
        must_visit=_csv(args.must_visit),
        avoid=_csv(args.avoid),
        pace=TravelPace(args.pace),
        transport_preferences=_csv(args.transport_preferences),
    )

    plan_id = f"E2E-{int(time.time())}"
    graph = build_planning_graph()
    config = {"configurable": {"thread_id": plan_id}}

    banner(f"全流程自动执行 (plan_id={plan_id})")
    start = time.time()

    result = await graph.ainvoke(
        {
            "thread_id": plan_id,
            "plan_id": plan_id,
            "request": request,
            "current_stage": "created",
        },
        config=config,
    )

    if result.get("__interrupt__"):
        print("\n👤 自动验收：批准方案并恢复工作流")
        result = await graph.ainvoke(
            Command(
                resume={
                    "approved": True,
                    "reviewer_id": "e2e-validator",
                    "comment": "真实端到端自动验收",
                }
            ),
            config=config,
        )

    total_time = time.time() - start

    if result["current_stage"] != "delivered":
        print(f"❌ 最终状态: {result['current_stage']}")
        if result.get("errors"):
            for err in result["errors"]:
                print(f"   错误: {err}")
        constraint = result.get("constraint_report")
        if constraint:
            print(f"   约束得分: {constraint.score}，通过: {constraint.valid}")
            for issue in constraint.issues:
                print(f"   [{issue.severity}] {issue.code}: {issue.message}")
        itinerary = result.get("itinerary", [])
        if itinerary:
            print(f"   失败前已生成 {len(itinerary)} 天行程:")
            for day in itinerary:
                events = " | ".join(
                    f"{event.start_time}-{event.end_time} {event.title}({event.resource_id or '-'})"
                    for event in day.events
                )
                print(f"   Day {day.day}: {events}")
        return 1

    acceptance_failures: list[str] = []
    constraint = result.get("constraint_report")
    quality = result.get("quality_report")
    if not constraint or not constraint.valid:
        acceptance_failures.append("constraint_report 未通过")
    if result.get("verification_blocking_count", 0):
        acceptance_failures.append("仍存在确定性阻断问题")
    if quality and quality.blocking_issues:
        acceptance_failures.append(f"LLM 仍报告阻断问题: {quality.blocking_issues}")
    weather_forecast = result.get("weather_forecast", [])
    if len(weather_forecast) < request.days:
        acceptance_failures.append(f"天气数据不足: 需要 {request.days} 天，实际 {len(weather_forecast)} 天")
    itinerary = result.get("itinerary", [])
    if len(itinerary) != request.days:
        acceptance_failures.append(f"行程天数错误: 需要 {request.days} 天，实际 {len(itinerary)} 天")
    day_image_paths = result.get("day_image_paths", [])
    if len(day_image_paths) != request.days or any(not paths for paths in day_image_paths):
        acceptance_failures.append(f"每日配图不完整: 需要 {request.days} 天，实际 {len(day_image_paths)} 组")
    missing_images = [
        image_path
        for paths in day_image_paths
        for image_path in paths
        if not Path(image_path).is_file()
    ]
    if missing_images:
        acceptance_failures.append(f"本地配图文件不存在: {missing_images}")
    report_path = result.get("report_path", "")
    if not report_path or not Path(report_path).is_file():
        acceptance_failures.append("PDF 报告文件不存在")
    if not weather_forecast:
        acceptance_failures.append("天气数据为空")
    bad_title_markers = ("攻略", "top 10", "top 100", "淘宝", "天猫", "instagram")
    bad_resources = [
        resource.name for resource in result.get("resources", [])
        if any(marker in resource.name.lower() for marker in bad_title_markers)
    ]
    if bad_resources:
        acceptance_failures.append(f"不可游览网页被当作资源: {bad_resources}")
    if acceptance_failures:
        print("\n❌ 交付状态虽完成，但验收失败:")
        for failure in acceptance_failures:
            print(f"   - {failure}")
        return 1

    banner(f"全流程完成 ({total_time:.1f}s) → delivered")

    resources = result.get("resources", [])
    print(f"\n📍 搜索到 {len(resources)} 个资源:")
    for r in resources[:8]:
        print(f"   - {r.name} ({r.category}) ¥{r.price_per_person}/人 {r.recommended_minutes}min")

    print(f"\n📅 行程 ({len(itinerary)} 天):")
    for day in itinerary:
        print(f"   Day {day.day}: {day.theme}")
        for event in day.events:
            print(f"     {event.start_time}-{event.end_time} {event.title} ¥{event.cost_per_person}")

    quote = result.get("quote")
    if quote:
        print("\n💰 报价:")
        print(f"   总成本: ¥{quote.total_cost}")
        print(f"   人均售价: ¥{quote.sale_price_per_person}")
        print(f"   毛利率: {quote.margin_rate:.1%}")

    qr = result.get("quality_report")
    if qr:
        print("\n✅ 质量审核 (LLM 自动):")
        print(f"   综合: {qr.overall_score} | 事实溯源: {qr.fact_traceability_score} | 可行性: {qr.feasibility_score} | 客群匹配: {qr.audience_fit_score}")

    print(f"\n🔍 验证得分: {result.get('verification_score', 'N/A')}/100")

    poster = result.get("poster_asset", {})
    print("\n🎨 海报:")
    print(f"   状态: {poster.get('status')}")
    print(f"   远程: {poster.get('url', 'N/A')}")
    local_path = poster.get("local_path", "")
    if local_path:
        size_kb = Path(local_path).stat().st_size / 1024
        print(f"   本地: {local_path} ({size_kb:.0f} KB)")

    if report_path:
        print(f"\n📄 最终报告: {report_path}")

    # Check saved report
    data_dir = Path(__file__).resolve().parents[1] / "data" / "plans" / plan_id
    versions = sorted(data_dir.glob("v*.json")) if data_dir.exists() else []
    if versions:
        report = json.loads(versions[-1].read_text(encoding="utf-8"))
        print(f"\n📄 报告已保存: {versions[-1]}")
        snapshot = report.get("snapshot", {})
        poster_in_report = snapshot.get("poster_asset", {})
        if poster_in_report.get("local_path"):
            print("   报告含海报本地路径: ✓")

    banner(f"🎉 端到端测试通过! 总耗时: {total_time:.1f}s")

    # Evaluation metrics
    metrics = evaluate_plan(
        request=request,
        itinerary=result.get("itinerary", []),
        resources=result.get("resources", []),
        constraint_report=result.get("constraint_report"),
        quote=result.get("quote"),
        quality_report=result.get("quality_report"),
    )
    banner("📊 评测指标")
    print(f"  可执行性: {metrics.executability_score}/100")
    print(f"    时间冲突: {metrics.time_conflict_count} | 日超负荷: {metrics.daily_overload_count} | 空天: {metrics.empty_day_count}")
    print(f"    预算偏差: {metrics.budget_error_rate:.1%}")
    print(f"  个性化: {metrics.personalization_score}/100")
    print(f"    必去覆盖: {metrics.must_visit_coverage:.0%} | 避雷违反: {metrics.avoid_violation_count} | 兴趣匹配: {metrics.interest_match_rate:.0%}")
    print(f"  信息质量: {metrics.info_quality_score}/100")
    print(f"    来源可追溯: {metrics.source_traceability:.0%} | 估算数据: {metrics.estimated_data_ratio:.0%}")
    print(f"  综合得分: {metrics.overall_score}/100")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
