"""景点名称别名与必去覆盖判定。"""
from __future__ import annotations

import re

PLACE_SUFFIXES = (
    "风景名胜区",
    "文化旅游区",
    "文化展示中心",
    "旅游景区",
    "旅游区",
    "遗址公园",
    "展示中心",
    "博物院",
    "博物馆",
    "公园",
    "景区",
    "旅游度假区",
)


def _normalize(value: str) -> str:
    return re.sub(r"[\s·•·—_\-()（）]", "", value.lower())


def _aliases(value: str) -> set[str]:
    normalized = _normalize(value)
    aliases = {normalized}
    changed = True
    while changed:
        changed = False
        for alias in tuple(aliases):
            for suffix in PLACE_SUFFIXES:
                if alias.endswith(suffix) and len(alias) - len(suffix) >= 3:
                    shortened = alias.removesuffix(suffix)
                    if shortened not in aliases:
                        aliases.add(shortened)
                        changed = True
    return aliases


def matches_place_name(candidate: str, required: str) -> bool:
    """容忍“景区/旅游区/展示中心”等官方名称差异。"""
    candidate_aliases = _aliases(candidate)
    required_aliases = _aliases(required)
    return any(
        left in right or right in left
        for left in candidate_aliases
        for right in required_aliases
        if len(left) >= 2 and len(right) >= 2
    )
