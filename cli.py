"""TripOps AI — 命令行旅行策划 Agent"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from langgraph.types import Command

from app.agents.graph import build_planning_graph
from app.agents.prompts import PARSE_USER_INPUT_SYSTEM
from app.config import get_settings
from app.models.schemas import PlanRequest
from app.services.model_gateway import ModelGateway

CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


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


def ask(prompt: str) -> str:
    return input(f"  {prompt}: ").strip()


def collect_input() -> str:
    """Collect key info via simple questions, return combined text for LLM."""
    header("TripOps AI · 旅行策划 Agent")
    print("  回答几个问题，Agent 自动完成策划全流程。\n")

    destination = ask("去哪")
    duration = ask("几天（如：3天2晚）")
    people = ask("几个人")
    budget = ask("人均预算（元）")
    extra = ask("补充（人群、偏好、限制等，可留空）")

    parts = [f"目的地：{destination}", f"行程：{duration}", f"人数：{people}", f"人均预算：{budget}元"]
    if extra:
        parts.append(f"补充：{extra}")

    combined = "；".join(parts)

    print(f"\n  {DIM}收到：{combined}{RESET}")
    confirm = input(f"  {BOLD}开始策划？(回车确认 / n 取消) {RESET}").strip().lower()
    if confirm in ("n", "no"):
        raise KeyboardInterrupt

    return combined


async def parse_input(user_input: str) -> PlanRequest:
    settings = get_settings()
    gateway = ModelGateway(settings)
    print(f"\n  {DIM}正在理解你的需求…{RESET}")
    return await gateway.structured_completion(
        system_prompt=PARSE_USER_INPUT_SYSTEM,
        user_prompt=user_input,
        schema=PlanRequest,
        timeout_seconds=30,
    )


def print_request(request: PlanRequest) -> None:
    section("需求解析结果")
    print(f"  产品: {request.title}")
    print(f"  类型: {request.product_type.value}  |  目的地: {request.destination}")
    print(f"  周期: {request.days}天{request.nights}晚  |  人数: {request.group_size}")
    print(f"  预算: ¥{request.budget_per_person}/人  |  毛利率: {request.target_margin_rate:.0%}")
    print(f"  客群: {request.target_audience}")
    if request.themes:
        print(f"  主题: {', '.join(request.themes)}")
    if request.constraints:
        print(f"  约束: {'; '.join(request.constraints)}")


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

    print_request(request)

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

    header("确认方案")
    decision = input(f"  {BOLD}满意这份方案吗？(回车确认 / n 放弃) {RESET}").strip().lower()

    if decision in ("", "y", "yes"):
        print(f"\n  {DIM}正在存档…{RESET}\n")
        final = await graph.ainvoke(
            Command(resume={
                "approved": True,
                "reviewer_id": "cli-user",
                "comment": "用户确认",
            }),
            config=config,
        )
        final_data = {k: v for k, v in final.items() if not k.startswith("__")}
        success(f"方案已存档！状态: {final_data.get('current_stage')}")
    else:
        await graph.ainvoke(
            Command(resume={
                "approved": False,
                "reviewer_id": "cli-user",
                "comment": "用户放弃",
            }),
            config=config,
        )
        info("方案已放弃。")


async def main() -> None:
    while True:
        try:
            user_input = collect_input()
            request = await parse_input(user_input)
            await run_workflow(request)
        except KeyboardInterrupt:
            print(f"\n\n  {DIM}再见！{RESET}\n")
            break
        except Exception as exc:  # noqa: BLE001
            error(f"执行失败: {exc}")
            import traceback
            traceback.print_exc()

        print()
        again = input(f"  {BOLD}再策划一个？(回车继续 / n 退出) {RESET}").strip().lower()
        if again in ("n", "no"):
            print(f"\n  {DIM}再见！{RESET}\n")
            break


if __name__ == "__main__":
    asyncio.run(main())
