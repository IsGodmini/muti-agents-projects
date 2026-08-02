"""TripOps AI — 对话式旅行策划 Agent"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from langgraph.types import Command

from app.agents.chat_graph import _parse_date
from app.agents.graph import build_planning_graph
from app.agents.prompts import PARSE_USER_INPUT_SYSTEM
from app.config import get_settings
from app.models.schemas import PlannerConversation, PlanRequest
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


def agent_say(text: str) -> None:
    print(f"\n  {BOLD}🤖{RESET} {text}")


def user_say(text: str) -> None:
    print(f"  {DIM}👤 {text}{RESET}")


async def converse_until_ready() -> PlanRequest:
    """LLM-driven conversation to collect travel requirements."""
    settings = get_settings()
    gateway = ModelGateway(settings)
    history: list[str] = []

    header("TripOps AI · 旅行策划 Agent")
    info("和 Agent 聊聊你的旅行想法，它会问你几个问题。")
    info("随时可以说「开始吧」直接执行策划。\n")

    first_input = input(f"  {BOLD}👤 {RESET}").strip()
    if not first_input:
        first_input = "我想去旅行"
    user_say(first_input)
    history.append(f"用户：{first_input}")

    while True:
        conversation_text = "\n".join(history)

        try:
            response = await gateway.structured_completion(
                model=settings.llm_model,
                system_prompt=PARSE_USER_INPUT_SYSTEM,
                user_prompt=f"以下是目前的对话：\n{conversation_text}",
                schema=PlannerConversation,
                timeout_seconds=60,
            )
        except Exception as exc:  # noqa: BLE001
            error(f"理解失败 [{type(exc).__name__}]: {exc or '请检查网络连接或 API 配置'}")
            retry = input(f"  {BOLD}👤 再说一次: {RESET}").strip()
            history.append(f"用户：{retry}")
            continue

        if response.ready:
            nights = min(response.nights, response.days - 1)
            return PlanRequest(
                title=response.title or f"{response.destination}旅行",
                product_type=response.product_type,
                destination=response.destination,
                departure_date=_parse_date(response.departure_date),
                departure_time_note=response.departure_time_note or response.departure_date,
                days=response.days,
                nights=nights,
                group_size=response.group_size,
                budget_per_person=response.budget_per_person,
                target_margin_rate=response.target_margin_rate,
                target_audience=response.target_audience or "普通游客",
                themes=response.themes,
                pace=response.pace,
                interests=response.interests,
                must_visit=response.must_visit,
                avoid=response.avoid,
                transport_preferences=response.transport_preferences or ["public_transit", "walking"],
                hard_constraints=response.hard_constraints,
                soft_preferences=response.soft_preferences,
            )

        agent_say(response.question)
        history.append(f"顾问：{response.question}")
        user_input = input(f"  {BOLD}👤 {RESET}").strip()

        if not user_input:
            continue
        user_say(user_input)
        history.append(f"用户：{user_input}")


def print_request(request: PlanRequest) -> None:
    section("需求确认")
    print(f"  产品: {request.title}")
    print(f"  类型: {request.product_type.value}  |  目的地: {request.destination}")
    print(f"  周期: {request.days}天{request.nights}晚  |  人数: {request.group_size}")
    print(f"  预算: ¥{request.budget_per_person}/人")
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
    print(f"  {DIM}需求解析 → 资源检索 → 行程规划 → 约束校验 → 成本核算 → 质量审核{RESET}\n")

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

    success(f"检索到 {len(data.get('resources', []))} 个资源")

    print_itinerary(data)
    print_constraints(data)
    print_quote(data)
    print_quality(data)

    if not result.get("__interrupt__"):
        error(f"工作流异常: {data.get('current_stage')}")
        for err in data.get("errors", []):
            error(err)
        return

    header("确认方案")
    decision = input(f"  {BOLD}满意吗？(回车确认 / n 放弃) {RESET}").strip().lower()

    if decision in ("", "y", "yes"):
        print(f"\n  {DIM}正在存档…{RESET}\n")
        final = await graph.ainvoke(
            Command(resume={"approved": True, "reviewer_id": "cli-user", "comment": "确认"}),
            config=config,
        )
        success(f"方案已存档！状态: {final.get('current_stage')}")
    else:
        await graph.ainvoke(
            Command(resume={"approved": False, "reviewer_id": "cli-user", "comment": "放弃"}),
            config=config,
        )
        info("方案已放弃。")


async def main() -> None:
    while True:
        try:
            request = await converse_until_ready()
            await run_workflow(request)
        except KeyboardInterrupt:
            print(f"\n\n  {DIM}再见！{RESET}\n")
            break
        except Exception as exc:  # noqa: BLE001
            error(f"执行失败 [{type(exc).__name__}]: {exc or '请检查网络连接或 API 配置'}")
            import traceback
            traceback.print_exc()

        print()
        again = input(f"  {BOLD}再策划一个？(回车继续 / n 退出) {RESET}").strip().lower()
        if again in ("n", "no"):
            print(f"\n  {DIM}再见！{RESET}\n")
            break


if __name__ == "__main__":
    asyncio.run(main())
