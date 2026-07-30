"""TripOps AI — 命令行文旅产品智能策划 Agent"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from langgraph.types import Command

from app.agents.graph import build_planning_graph
from app.models.schemas import PlanRequest

CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

PRODUCT_TYPES = {
    "1": ("family_trip", "亲子旅行"),
    "2": ("study_tour", "研学旅行"),
    "3": ("corporate_team_building", "企业团建"),
    "4": ("senior_friendly", "银龄慢游"),
}




def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'━' * 56}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'━' * 56}{RESET}\n")


def section(text: str) -> None:
    print(f"\n{BOLD}{YELLOW}▸ {text}{RESET}")


def success(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def info(text: str) -> None:
    print(f"  {DIM}{text}{RESET}")


def error(text: str) -> None:
    print(f"  {RED}✗ {text}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    answer = input(f"  {prompt}{hint}: ").strip()
    return answer or default


def ask_choice(prompt: str, options: dict[str, tuple[str, str]], default: str = "1") -> str:
    print(f"  {prompt}")
    for key, (_, label) in options.items():
        marker = " ●" if key == default else ""
        print(f"    {key}. {label}{marker}")
    choice = input(f"  请选择 {DIM}[{default}]{RESET}: ").strip()
    return choice if choice in options else default


def ask_themes() -> list[str]:
    raw = ask("旅行主题（逗号分隔，自由输入）", "")
    if not raw:
        return []
    return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]


def collect_requirements() -> PlanRequest:
    header("TripOps AI · 文旅产品智能策划")
    print("  按提示填写产品信息，直接回车使用默认值。\n")

    # 1. Destination
    section("基础信息")
    destination = ask("目的地", "杭州")

    # 2. Product type
    type_choice = ask_choice("产品类型", PRODUCT_TYPES, "1")
    product_type, type_label = PRODUCT_TYPES[type_choice]
    success(f"已选择: {type_label}")

    # 3. Title
    default_title = f"{destination} · {type_label}产品"
    title = ask("产品名称", default_title)

    # 4. Duration
    section("行程周期")
    days = int(ask("天数", "3") or "3")
    nights = int(ask("晚数", str(max(days - 1, 0))) or str(max(days - 1, 0)))
    if nights >= days:
        nights = days - 1
        info(f"晚数已调整为 {nights}（必须小于天数）")

    # 5. Group & budget
    section("客群与预算")
    group_size = int(ask("团队人数", "30") or "30")
    budget = int(ask("人均预算上限（元）", "1800") or "1800")
    margin = float(ask("目标毛利率（%）", "15") or "15")
    audience = ask("目标客群描述", "8-12岁儿童及家长")

    # 6. Themes
    section("主题选择")
    themes = ask_themes()
    if themes:
        success(f"主题: {', '.join(themes)}")

    # 7. Constraints
    section("约束条件")
    constraints_raw = ask("硬性约束（分号分隔，可留空）", "")
    constraints = [c.strip() for c in constraints_raw.replace("，", ";").split(";") if c.strip()]

    # 8. Extra requirements
    section("补充信息")
    extra = ask("还有其他需求或特殊情况吗？（可留空）", "")
    if extra:
        constraints.append(extra)

    # Summary
    header("需求确认")
    print(f"  产品: {title}")
    print(f"  类型: {type_label}  |  目的地: {destination}")
    print(f"  周期: {days}天{nights}晚  |  团队: {group_size}人")
    print(f"  预算: ¥{budget}/人  |  毛利率: {margin:.0f}%")
    print(f"  客群: {audience}")
    print(f"  主题: {', '.join(themes)}")
    if constraints:
        print(f"  约束: {'; '.join(constraints)}")

    confirm = input(f"\n  {BOLD}确认开始策划？(y/n) {DIM}[y]{RESET}: ").strip().lower()
    if confirm in ("n", "no", "否"):
        raise KeyboardInterrupt

    return PlanRequest(
        title=title,
        product_type=product_type,
        destination=destination,
        days=days,
        nights=nights,
        group_size=group_size,
        budget_per_person=budget,
        target_margin_rate=margin / 100,
        target_audience=audience,
        themes=themes,
        constraints=constraints,
    )


def print_itinerary(data: dict) -> None:
    section("分日行程")
    for day in data.get("itinerary", []):
        print(f"\n  {BOLD}DAY {day['day']:02d}{RESET}  {day['theme']}")
        for event in day.get("events", []):
            time_range = f"{event['start_time']}–{event['end_time']}"
            cost = f"  ¥{event['cost_per_person']}/人" if event.get("cost_per_person") else ""
            print(f"    {DIM}{time_range}{RESET}  {BOLD}{event['title']}{RESET}{cost}")
            desc = event.get("description", "")
            if desc:
                print(f"    {DIM}{desc}{RESET}")


def print_quote(data: dict) -> None:
    quote = data.get("quote")
    if not quote:
        return
    section("成本与报价")
    for item in quote.get("items", []):
        print(f"    {item['category']:<8} {item['description']:<16} ¥{item['amount']:>10,}")
    print(f"  {'─' * 48}")
    print(f"    总成本          ¥{quote['total_cost']:>10,}  (人均 ¥{quote['cost_per_person']:,})")
    print(f"    建议售价         ¥{quote['sale_price_per_person']:>10,} /人")
    print(f"    预计毛利         ¥{quote['expected_profit']:>10,}  ({quote['margin_rate']:.1%})")


def print_quality(data: dict) -> None:
    quality = data.get("quality_report")
    if not quality:
        return
    section("质量审核")
    print(f"    综合评分  {BOLD}{quality['overall_score']}{RESET}/100")
    print(f"    事实溯源  {quality['fact_traceability_score']}/100")
    print(f"    可行性    {quality['feasibility_score']}/100")
    print(f"    客群匹配  {quality['audience_fit_score']}/100")
    for suggestion in quality.get("suggestions", []):
        info(f"💡 {suggestion}")
    for issue in quality.get("blocking_issues", []):
        error(f"⛔ {issue}")


def print_constraints(data: dict) -> None:
    report = data.get("constraint_report")
    if not report:
        return
    section("约束校验")
    if report["valid"]:
        success(f"校验通过 (得分 {report['score']})")
    else:
        error(f"校验未通过 (得分 {report['score']})")
    for issue in report.get("issues", []):
        icon = "⚠" if issue["severity"] == "warning" else "⛔"
        print(f"    {icon} {issue['message']}")


async def run_workflow(request: PlanRequest) -> None:
    thread_id = uuid4().hex
    plan_id = f"PLAN-{thread_id[:8].upper()}"
    config = {"configurable": {"thread_id": thread_id}}
    graph = build_planning_graph()

    section("多 Agent 工作流执行中")
    print(f"  {DIM}需求解析 → 资源检索 → 行程规划 → 约束校验 → 成本核算 → 质量审核{RESET}")
    print(f"  {DIM}Plan ID: {plan_id}{RESET}\n")

    result = await graph.ainvoke(
        {
            "thread_id": thread_id,
            "plan_id": plan_id,
            "request": request,
            "current_stage": "created",
        },
        config=config,
    )

    data = {k: v for k, v in result.items() if not k.startswith("__")}

    success(f"Skill: {data.get('selected_skill', 'unknown')}")
    success(f"资源来源: {data.get('resource_search_provider', 'unknown')}")
    success(f"检索到 {len(data.get('resources', []))} 个资源")

    print_itinerary(data)
    print_constraints(data)
    print_quote(data)
    print_quality(data)

    if data.get("current_stage") != "waiting_approval":
        error(f"工作流异常终止: {data.get('current_stage')}")
        for err in data.get("errors", []):
            error(err)
        return

    header("人工审批")
    print("  方案已通过自动审核，等待确认。\n")
    decision = input(f"  {BOLD}是否批准？(y/n) {DIM}[y]{RESET}: ").strip().lower()

    if decision in ("", "y", "yes", "是"):
        print(f"\n  {DIM}正在生成交付版本…{RESET}\n")
        final = await graph.ainvoke(
            Command(resume={
                "approved": True,
                "reviewer_id": "cli-user",
                "comment": "CLI 审批通过",
            }),
            config=config,
        )
        final_data = {k: v for k, v in final.items() if not k.startswith("__")}
        poster = final_data.get("poster_asset", {})
        success(f"方案已交付！状态: {final_data.get('current_stage')}")
        if poster:
            success(f"海报: {poster.get('status', 'unknown')}")
            if poster.get("note"):
                info(poster["note"])
    else:
        comment = input("  驳回意见（可留空）: ").strip()
        await graph.ainvoke(
            Command(resume={
                "approved": False,
                "reviewer_id": "cli-user",
                "comment": comment or "CLI 驳回",
            }),
            config=config,
        )
        error("方案已驳回。")


async def main() -> None:
    while True:
        try:
            request = collect_requirements()
            await run_workflow(request)
        except KeyboardInterrupt:
            print(f"\n\n  {DIM}再见！{RESET}\n")
            break
        except Exception as exc:  # noqa: BLE001
            error(f"执行失败: {exc}")
            import traceback
            traceback.print_exc()

        print()
        again = input(f"  {BOLD}继续策划下一个产品？(y/n) {DIM}[n]{RESET}: ").strip().lower()
        if again not in ("y", "yes", "是"):
            print(f"\n  {DIM}再见！{RESET}\n")
            break


if __name__ == "__main__":
    asyncio.run(main())
