"""详细纯文字 Markdown 交付报告。"""
from __future__ import annotations

from datetime import timedelta

from app.models.schemas import (
    ConstraintReport,
    ItineraryDay,
    PlanRequest,
    QualityReport,
    Quote,
    ResourceCandidate,
)
from app.services.costs import event_cost_label


def _cell(value: object) -> str:
    return str(value or "-").replace("|", "\\|").replace("\n", "<br>")


def _join(values: list[str], fallback: str = "无") -> str:
    return "、".join(value for value in values if value) or fallback


def _pace_label(value: object) -> str:
    raw = value.value if hasattr(value, "value") else str(value)
    return {"intense": "紧凑", "moderate": "适中", "relaxed": "舒缓"}.get(raw, raw)


def _weather_source(weather_forecast: list[dict] | None) -> str:
    providers = sorted({str(day.get("provider", "qweather")) for day in weather_forecast or []})
    labels = {"qweather": "和风天气 API", "amap": "高德天气 API"}
    return " + ".join(labels.get(provider, provider) for provider in providers) or "未获取"


def _quote_description(category: str, description: str) -> str:
    if description.strip():
        return description.strip()
    defaults = {
        "交通": "团队行程接驳、市内交通及相关车辆费用估算",
        "住宿": "按团队规模与行程晚数估算的住宿费用",
        "餐饮": "按人数与行程天数估算的团队餐饮费用",
        "门票及课程": "已选景点门票及体验项目的团队总费用",
        "服务": "领队、保险、物料与组织服务的综合估算",
    }
    return defaults.get(category, "该类别的团队综合市场估价")


def render_markdown_report(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    quote: Quote | None = None,
    quality_report: QualityReport | None = None,
    constraint_report: ConstraintReport | None = None,
    weather_forecast: list[dict] | None = None,
    poster_local_path: str | None = None,
    day_image_paths: list[list[str] | str] | None = None,
    resources: list[ResourceCandidate] | None = None,
) -> str:
    """生成便于执行、审核和二次编辑的详细纯文字报告。

    图片参数仅为兼容旧调用保留，本交付不嵌入任何 Markdown 图片。
    """
    del poster_local_path, day_image_paths
    lines: list[str] = [f"# {request.title}", ""]
    lines.append(
        f"> {request.destination}｜{request.days}天{request.nights}晚｜{request.group_size}人｜"
        f"人均预算不高于 ¥{request.budget_per_person:,}｜{request.target_audience}"
    )
    lines.extend(["", "本文档为执行与审核版，包含详细时间表、费用口径、天气、资源来源、约束检查与出行提醒。", ""])

    lines.extend(["## 一、需求与方案概览", "", "| 项目 | 内容 |", "|---|---|"])
    overview = [
        ("目的地", request.destination),
        (
            "出发时间",
            request.departure_date.isoformat()
            if request.departure_date
            else request.departure_time_note or "待定",
        ),
        ("行程周期", f"{request.days}天{request.nights}晚"),
        ("团队规模", f"{request.group_size}人"),
        ("客群", request.target_audience),
        ("行程节奏", _pace_label(request.pace)),
        ("主题", _join(request.themes, "综合观光")),
        ("兴趣偏好", _join(request.interests)),
        ("交通偏好", _join(request.transport_preferences)),
        ("必去地点", _join(request.must_visit)),
        ("避免项", _join(request.avoid)),
    ]
    lines.extend(f"| {_cell(label)} | {_cell(value)} |" for label, value in overview)
    lines.append("")

    if request.hard_constraints or request.soft_preferences or request.assumptions:
        lines.extend(["### 约束与假设", ""])
        lines.append(f"- 硬约束：{_join(request.hard_constraints)}")
        lines.append(f"- 软偏好：{_join(request.soft_preferences)}")
        lines.append(f"- 方案假设：{_join(request.assumptions)}")
        lines.append("")

    lines.extend(["## 二、费用口径说明", ""])
    lines.extend([
        "- “¥X/人（估算）”：节点的人均参考价，用于解释消费水平，不应再与整团报价重复相加。",
        "- “免费”：当前资源信息未显示门票；如官方政策变更，以出行时信息为准。",
        "- “已纳入团费”：如行程内交通，费用在整团报价中统一核算，节点不重复列价。",
        "- “按需自理”：自由活动、购物、茶饮等可选消费，不计入节点必选费用。",
        "- “待确认”：暂无可靠单项报价，需在预订前向官方或供应商复核。",
        "",
    ])

    if weather_forecast:
        lines.extend([
            f"## 三、行程天气（来源：{_weather_source(weather_forecast)}）",
            "",
            "| 日期 | 白天天气 | 气温 | 风力 | 湿度 |",
            "|---|---|---|---|---|",
        ])
        for day in weather_forecast:
            lines.append(
                f"| {_cell(day.get('date', '?'))} | {_cell(day.get('text_day', '未知'))} | "
                f"{_cell(day.get('temp_min', '?'))}~{_cell(day.get('temp_max', '?'))}℃ | "
                f"{_cell(day.get('wind_scale_day', '-'))}级 | {_cell(day.get('humidity', '-'))}% |"
            )
        lines.extend(["", "> 天气预报会动态变化，建议出发前 24–48 小时再次核对，并根据高温、降雨与风力调整户外活动。", ""])
    else:
        lines.extend(["## 三、行程天气", "", "本次未获取到有效预报，需在出发前补充核对。", ""])

    lines.extend(["## 四、详细分日行程", ""])
    for day in itinerary:
        date_text = ""
        if request.departure_date:
            date_text = (request.departure_date + timedelta(days=day.day - 1)).isoformat()
        heading = f"### Day {day.day}"
        if date_text:
            heading += f" · {date_text}"
        heading += f" · {day.theme}"
        lines.extend([
            heading,
            "",
            "| 时间 | 活动 | 类型 | 费用口径 | 执行说明 |",
            "|---|---|---|---|---|",
        ])
        for event in day.events:
            lines.append(
                f"| {event.start_time}–{event.end_time} | {_cell(event.title)} | "
                f"{_cell(event.category)} | {_cell(event_cost_label(event))} | {_cell(event.description)} |"
            )
        subtotal = sum(event.cost_per_person for event in day.events)
        lines.extend([
            "",
            f"- 当日可数值化节点参考价合计：¥{subtotal:,}/人。该数值仅用于解释节点费用，整体付款口径以“五、团队报价”为准。",
            "",
        ])

    lines.extend(["## 五、团队报价与预算", ""])
    if quote:
        lines.extend(["| 类别 | 核算口径 | 团队金额 |", "|---|---|---:|"])
        for item in quote.items:
            lines.append(
                f"| {_cell(item.category)} | {_cell(_quote_description(item.category, item.description))} | "
                f"¥{item.amount:,} |"
            )
        lines.extend([
            f"| **总成本** | {request.group_size}人团队合计 | **¥{quote.total_cost:,}** |",
            "",
            f"- 人均成本：¥{quote.cost_per_person:,}",
            f"- 建议人均售价：¥{quote.sale_price_per_person:,}",
            f"- 预计营业收入：¥{quote.expected_revenue:,}",
            f"- 预计毛利：¥{quote.expected_profit:,}",
            f"- 毛利率：{quote.margin_rate:.1%}",
            f"- 与人均预算的余量：¥{request.budget_per_person - quote.sale_price_per_person:,}/人",
            "",
            "> 上述报价为当前资源与市场价的方案估算，并非最终采购合同。车辆、房型、餐标、门票政策和保险项目确认后，应重新锁价。",
            "",
        ])
    else:
        lines.extend(["当前未生成可用的团队报价，不应对外使用成本数字。", ""])

    lines.extend(["## 六、约束、可行性与质量审核", ""])
    if constraint_report:
        lines.append(f"- 约束校验：{'通过' if constraint_report.valid else '未通过'}，{constraint_report.score}/100。")
        lines.append(f"- 每日最长活动跨度：{constraint_report.max_daily_minutes} 分钟。")
        lines.append(f"- 全程交通时间：{constraint_report.total_travel_minutes} 分钟。")
        lines.append(f"- 必去资源覆盖率：{constraint_report.must_visit_coverage:.0%}。")
        lines.append(f"- 时间冲突数：{constraint_report.time_conflict_count}。")
        if constraint_report.issues:
            lines.append("- 需关注的约束项：")
            for issue in constraint_report.issues:
                lines.append(f"  - [{issue.severity}] {issue.message}")
    else:
        lines.append("- 未附带约束校验结果。")
    if quality_report:
        lines.extend([
            f"- 综合质量：{quality_report.overall_score}/100。",
            f"- 事实溯源：{quality_report.fact_traceability_score}/100。",
            f"- 可行性：{quality_report.feasibility_score}/100。",
            f"- 客群匹配：{quality_report.audience_fit_score}/100。",
        ])
        if quality_report.blocking_issues:
            lines.append("- 阻断问题：")
            lines.extend(f"  - {issue}" for issue in quality_report.blocking_issues)
        if quality_report.suggestions:
            lines.append("- 审核建议：")
            lines.extend(f"  - {suggestion}" for suggestion in quality_report.suggestions)
    lines.append("")

    if resources:
        lines.extend([
            "## 七、已选资源与溯源信息",
            "",
            "| 资源 | 类型 | 位置 | 开放时间 | 参考价/人 | 来源 |",
            "|---|---|---|---|---:|---|",
        ])
        for resource in resources:
            source = (
                f"[查看来源]({resource.source_url})"
                if resource.source_url
                else resource.provider
            )
            price = f"¥{resource.price_per_person}" if resource.price_per_person else "免费/待官方确认"
            lines.append(
                f"| {_cell(resource.name)} | {_cell(resource.category)} | {_cell(resource.location)} | "
                f"{_cell(resource.opening_hours)} | {_cell(price)} | {_cell(source)} |"
            )
        lines.extend(["", "资源价格、开放时间、预约规则和优惠政策需在出行前通过官方渠道二次确认。", ""])

    lines.extend([
        "## 八、执行前确认清单",
        "",
        "1. 出发前 24–48 小时更新天气，高温或降雨时压缩户外暴露时间。",
        "2. 逐一复核景点开放时间、实名预约、门票、取消规则与团队政策。",
        "3. 确认住宿房型、早餐、单房差、押金与入住/退房时间。",
        "4. 确认车型、座位、行李容量、用车时长、司机食宿与超时计价。",
        "5. 按餐标和忌口确认菜单，将节点餐费与报价中的餐饮总额对齐。",
        "6. 为高温、降雨、人流限制和交通延误准备可替换的室内或自由活动方案。",
        "",
        "## 九、数据来源与责任边界",
        "",
        "| 数据类型 | 来源与属性 |",
        "|---|---|",
        "| 景点与攻略资源 | Tavily 实时网页检索与高德 POI，优先保留来源链接 |",
        f"| 天气 | {_weather_source(weather_forecast)}，为生成时点的预报 |",
        "| 票价、建议时长、开放时间 | 检索结果与模型整理，需官方二次确认 |",
        "| 交通时间 | 高德路径规划与估算兜底 |",
        "| 团队成本 | 模型市场估价后由确定性规则计算人均售价和毛利 |",
        "| 行程编排 | AI 生成后经时间、约束与预算规则校验 |",
        "",
        "---",
        "本方案由 TripOps AI 多 Agent 工作流生成，用于行程讨论与询价，不替代旅游合同、官方公告或供应商确认单。",
    ])
    return "\n".join(lines)
