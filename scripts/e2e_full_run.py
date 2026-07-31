#!/usr/bin/env python3
"""End-to-end full workflow test with real LLM / Tavily / ComfyUI services.

Usage:
    uv run python scripts/e2e_full_run.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.agents.graph import build_planning_graph
from app.config import get_settings
from app.models.schemas import PlanRequest, ProductType
from app.services.evaluation import evaluate_plan


def banner(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


async def main() -> int:
    settings = get_settings()
    print(f"LLM (complex):  {settings.llm_model_complex}")
    print(f"LLM (simple):   {settings.llm_model_simple}")
    print(f"LLM (multimodal): {settings.llm_model_multimodal}")
    print(f"ComfyUI:        {settings.imagegen_api_url}")
    print(f"Mock imagegen:  {settings.mock_imagegen}")
    print(f"Mock model:     {settings.mock_model_mode}")
    print(f"Tavily search:  {settings.tavily_search_enabled}")

    request = PlanRequest(
        title="杭州两天一夜亲子研学之旅",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=2,
        nights=1,
        group_size=30,
        budget_per_person=1800,
        target_margin_rate=0.15,
        target_audience="8-12岁儿童及家长",
        themes=["自然教育", "历史文化"],
        hard_constraints=["连续乘车不超过90分钟"],
        interests=["自然教育", "历史文化"],
        must_visit=[],
        avoid=[],
        pace="moderate",
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

    total_time = time.time() - start

    if result["current_stage"] != "delivered":
        print(f"❌ 最终状态: {result['current_stage']}")
        if result.get("errors"):
            for err in result["errors"]:
                print(f"   错误: {err}")
        return 1

    banner(f"全流程完成 ({total_time:.1f}s) → delivered")

    resources = result.get("resources", [])
    print(f"\n📍 搜索到 {len(resources)} 个资源:")
    for r in resources[:8]:
        print(f"   - {r.name} ({r.category}) ¥{r.price_per_person}/人 {r.recommended_minutes}min")

    itinerary = result.get("itinerary", [])
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

    report_path = result.get("report_path", "")
    if report_path:
        print(f"\n📄 Markdown 报告: {report_path}")

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
