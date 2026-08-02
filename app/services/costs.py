"""行程节点费用归一化与展示语义。

数值 0 只代表“没有可单独计价的金额”，不再直接展示为“¥0”。
真实免费、已纳入团费、自理可选和待确认使用独立状态表达。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.models.schemas import ItineraryDay, ItineraryEvent, ResourceCandidate

MEAL_CATEGORIES = {
    "dining", "meal", "food", "restaurant", "cuisine", "餐饮", "用餐", "午餐", "晚餐",
}
TRANSPORT_CATEGORIES = {
    "transport", "transportation", "logistics", "transfer", "pickup", "交通", "集合", "接送", "返程",
}
REST_CATEGORIES = {"break", "rest", "休息", "休整"}
OPTIONAL_CATEGORIES = {"free_time", "optional", "shopping", "自由活动", "购物"}
MEAL_KEYWORDS = ("午餐", "晚餐", "早餐", "用餐", "午饭", "餐厅", "美食", "杭帮")
OPTIONAL_KEYWORDS = ("自由活动", "自由安排", "自由探索", "购物", "文创")


def _category(event: ItineraryEvent) -> str:
    return event.category.strip().lower()


def _is_meal(event: ItineraryEvent) -> bool:
    category = _category(event)
    if category in TRANSPORT_CATEGORIES:
        return False
    return category in MEAL_CATEGORIES or any(word in event.title for word in MEAL_KEYWORDS)


def _is_optional(event: ItineraryEvent) -> bool:
    return _category(event) in OPTIONAL_CATEGORIES or any(
        word in event.title for word in OPTIONAL_KEYWORDS
    )


def normalize_event_cost(
    event: ItineraryEvent,
    resource_map: Mapping[str, ResourceCandidate] | None = None,
    *,
    default_meal_cost: int = 80,
) -> ItineraryEvent:
    """回填可确定的付费节点，并为非付费节点标注明确语义。"""
    resources = resource_map or {}
    resource = resources.get(event.resource_id or "")

    if resource is not None and resource.price_per_person > 0:
        return event.model_copy(
            update={
                "cost_per_person": resource.price_per_person,
                "cost_status": "estimated",
                "cost_note": "按资源检索中的参考票价回填，请以官方实时价格为准。",
            }
        )

    category = _category(event)
    if category in TRANSPORT_CATEGORIES:
        return event.model_copy(
            update={
                "cost_per_person": 0,
                "cost_status": "included",
                "cost_note": "行程内交通按报价统一核算，不在单个节点重复计价。",
            }
        )

    if event.cost_per_person > 0:
        return event.model_copy(
            update={
                "cost_status": "estimated",
                "cost_note": event.cost_note or "行程节点的人均参考价，请以实际预订为准。",
            }
        )

    if _is_meal(event):
        return event.model_copy(
            update={
                "cost_per_person": default_meal_cost,
                "cost_status": "estimated",
                "cost_note": f"按团队正餐每人 {default_meal_cost} 元的保守标准补齐。",
            }
        )

    if resource is not None:
        return event.model_copy(
            update={
                "cost_status": "free",
                "cost_note": "当前资源检索结果未发现门票费用，出行前请再次确认。",
            }
        )

    if _is_optional(event):
        return event.model_copy(
            update={
                "cost_status": "optional",
                "cost_note": "自由活动中的购物、茶饮或自选项目由出行人按需自理。",
            }
        )
    if category in REST_CATEGORIES:
        return event.model_copy(
            update={"cost_status": "free", "cost_note": "休息时段无单独活动费。"}
        )
    return event.model_copy(
        update={
            "cost_status": "unknown",
            "cost_note": "当前节点无独立报价，如产生费用需在预订前确认。",
        }
    )


def normalize_itinerary_costs(
    itinerary: Sequence[ItineraryDay],
    resources: Sequence[ResourceCandidate] | None = None,
    *,
    default_meal_cost: int = 80,
) -> list[ItineraryDay]:
    resource_map = {resource.id: resource for resource in (resources or [])}
    return [
        day.model_copy(
            update={
                "events": [
                    normalize_event_cost(
                        event,
                        resource_map,
                        default_meal_cost=default_meal_cost,
                    )
                    for event in day.events
                ]
            }
        )
        for day in itinerary
    ]


def event_cost_label(event: ItineraryEvent, *, compact: bool = False) -> str:
    """生成无歧义费用文案；绝不把语义状态渲染为“¥0”。"""
    if event.cost_per_person > 0:
        suffix = "" if compact else "（估算）"
        return f"¥{event.cost_per_person}/人{suffix}"
    labels = {
        "free": "免费",
        "included": "已纳入团费" if not compact else "已含",
        "optional": "按需自理",
        "unknown": "待确认",
    }
    return labels.get(event.cost_status, "待确认")
