"""Response renderer: generate user-readable Markdown travel report."""
from __future__ import annotations

from app.models.schemas import (
    ConstraintReport,
    ItineraryDay,
    PlanRequest,
    QualityReport,
    Quote,
)


def render_markdown_report(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    quote: Quote | None = None,
    quality_report: QualityReport | None = None,
    constraint_report: ConstraintReport | None = None,
    weather_forecast: list[dict] | None = None,
    poster_local_path: str | None = None,
) -> str:
    """Render the complete travel plan as a Markdown document."""
    lines: list[str] = []
    lines.append(f"# {request.title}")
    lines.append("")
    lines.append(f"> {request.destination} · {request.days}天{request.nights}晚 · "
                 f"{request.group_size}人 · {request.target_audience}")
    lines.append("")

    lines.append("## 行程概览")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("|---|---|")
    lines.append(f"| 目的地 | {request.destination} |")
    lines.append(f"| 天数 | {request.days}天{request.nights}晚 |")
    lines.append(f"| 人数 | {request.group_size}人 |")
    lines.append(f"| 节奏 | {request.pace.value if hasattr(request.pace, 'value') else request.pace} |")
    lines.append(f"| 主题 | {', '.join(request.themes) or '综合'} |")
    if request.must_visit:
        lines.append(f"| 必去 | {', '.join(request.must_visit)} |")
    lines.append("")

    if weather_forecast:
        lines.append("## 天气")
        lines.append("")
        lines.append("| 日期 | 天气 | 温度 | 风力 | 湿度 |")
        lines.append("|---|---|---|---|---|")
        for day in weather_forecast:
            lines.append(
                f"| {day.get('date', '?')} | {day.get('text_day', '未知')} | "
                f"{day.get('temp_min', '?')}~{day.get('temp_max', '?')}℃ | "
                f"{day.get('wind_scale_day', '-')}级 | {day.get('humidity', '-')}% |"
            )
        lines.append("")

    for day in itinerary:
        lines.append(f"## Day {day.day}: {day.theme}")
        lines.append("")
        lines.append("| 时间 | 活动 | 类型 | 费用/人 |")
        lines.append("|---|---|---|---|")
        for event in day.events:
            cost = f"¥{event.cost_per_person}" if event.cost_per_person else "-"
            lines.append(f"| {event.start_time}-{event.end_time} | {event.title} | {event.category} | {cost} |")
        lines.append("")
        for event in day.events:
            if event.description and event.category not in ("logistics", "break"):
                lines.append(f"**{event.title}**: {event.description}")
                lines.append("")

    if constraint_report:
        lines.append("## 约束校验")
        lines.append("")
        lines.append(f"- 校验结果: {'✅ 通过' if constraint_report.valid else '⛔ 未通过'}（得分 {constraint_report.score}/100）")
        lines.append(f"- 每日最长跨度: {constraint_report.max_daily_minutes} 分钟")
        lines.append(f"- 全程交通时间: {constraint_report.total_travel_minutes} 分钟")
        if constraint_report.must_visit_coverage < 1:
            lines.append(f"- 必去覆盖: {constraint_report.must_visit_coverage:.0%}")
        if constraint_report.time_conflict_count:
            lines.append(f"- 时间冲突: {constraint_report.time_conflict_count} 处")
        if constraint_report.issues:
            lines.append("")
            lines.append("**待关注问题:**")
            for issue in constraint_report.issues:
                lines.append(f"- [{issue.severity}] {issue.message}")
        lines.append("")

    if quote:
        lines.append("## 预算明细")
        lines.append("")
        lines.append("| 类别 | 说明 | 金额 |")
        lines.append("|---|---|---|")
        for item in quote.items:
            lines.append(f"| {item.category} | {item.description} | ¥{item.amount:,} |")
        lines.append(f"| **合计** | | **¥{quote.total_cost:,}** |")
        lines.append("")
        lines.append(f"- 人均成本: ¥{quote.cost_per_person:,}")
        lines.append(f"- 人均售价: ¥{quote.sale_price_per_person:,}")
        lines.append(f"- 毛利率: {quote.margin_rate:.1%}")
        lines.append("")

    if quality_report:
        lines.append("## 质量评估")
        lines.append("")
        lines.append("| 维度 | 得分 |")
        lines.append("|---|---|")
        lines.append(f"| 综合 | {quality_report.overall_score}/100 |")
        lines.append(f"| 事实溯源 | {quality_report.fact_traceability_score}/100 |")
        lines.append(f"| 可行性 | {quality_report.feasibility_score}/100 |")
        lines.append(f"| 客群匹配 | {quality_report.audience_fit_score}/100 |")
        if quality_report.suggestions:
            lines.append("")
            lines.append("**改进建议:**")
            for suggestion in quality_report.suggestions:
                lines.append(f"- {suggestion}")
        lines.append("")

    if poster_local_path:
        lines.append("## 海报")
        lines.append("")
        lines.append(f"![旅行海报]({poster_local_path})")
        lines.append("")

    lines.append("---")
    lines.append("*由 TripOps AI 多 Agent 工作流自动生成*")
    return "\n".join(lines)
