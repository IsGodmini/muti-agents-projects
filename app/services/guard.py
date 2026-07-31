"""Prompt Injection and content safety guard.

Scans external content (web search results, user input) for injection patterns
before they reach LLM prompts or tool execution.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Patterns that indicate prompt injection attempts
INJECTION_PATTERNS: list[tuple[str, str]] = [
    # System prompt override attempts
    (r"(?i)(ignore|forget|disregard)\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|prompts?|rules?|directives?)", "system_override"),
    (r"(?i)(you\s+are\s+now|from\s+now\s+on\s+you\s+are|your\s+new\s+role\s+is)", "role_hijack"),
    (r"(?i)(system\s*(prompt|message|instruction):|<\s*\|?\s*system\s*\|?\s*>)", "system_tag"),
    # Tool / function calling injection
    (r"(?i)(call\s+(tool|function)|execute\s+(tool|function)|invoke\s+(tool|function))", "tool_invocation"),
    (r"(?i)(<\|?\s*(tool_call|function_call|python)\s*\|?>)", "tool_tag"),
    # Output format manipulation
    (r"(?i)(respond\s+(only\s+)?with\s+\"|output\s+(only\s+)?\"|your\s+response\s+must\s+be\s+\")", "output_manipulation"),
    (r"(?i)(do\s+not\s+(answer|respond|reply|output|say)\s+(anything|at\s+all))", "silence_attack"),
    # Data exfiltration
    (r"(?i)(send\s+(this|the\s+(conversation|chat|response))\s+to|forward\s+(this|the)\s+(conversation|chat))", "exfiltration"),
    # Denial of service patterns
    (r"(?i)(repeat\s+(this|the\s+(following|above))\s+(forever|indefinitely|100|thousand|million))", "dos_loop"),
]

# Known safe domains for travel resources
SAFE_DOMAINS: set[str] = {
    "gov.cn", "travelchina.org.cn", "ctrip.com", "qunar.com",
    "mafengwo.cn", "tuniu.com", "lvmama.com", "dianping.com",
    "meituan.com", "wikipedia.org", "baike.baidu.com",
    "zhihu.com", "xiaohongshu.com", "amap.com", "ditu.amap.com",
}

# Blocked domains (ad-heavy, spam, unsafe)
BLOCKED_DOMAINS: set[str] = {
    "example.com", "test.com", "localhost", "127.0.0.1",
}


@dataclass
class GuardResult:
    safe: bool
    blocked_patterns: list[str] = field(default_factory=list)
    sanitized_content: str = ""
    warnings: list[str] = field(default_factory=list)


def scan_content(content: str, source: str = "unknown") -> GuardResult:
    """Scan external content for prompt injection patterns.

    Args:
        content: The text content to scan.
        source: Identifier for the content source (e.g., 'tavily_search', 'user_input').

    Returns:
        GuardResult with safety assessment and sanitized content.
    """
    if not content or not isinstance(content, str):
        return GuardResult(safe=True, sanitized_content=content or "")

    blocked: list[str] = []
    sanitized = content

    for pattern, pattern_name in INJECTION_PATTERNS:
        matches = re.findall(pattern, content)
        if matches:
            blocked.append(pattern_name)
            sanitized = re.sub(pattern, "[CONTENT REDACTED - safety filter]", sanitized, flags=re.IGNORECASE)

    if blocked:
        logger.warning(
            "Guard blocked %d pattern(s) in %s: %s",
            len(blocked), source, ", ".join(blocked),
        )

    return GuardResult(
        safe=len(blocked) == 0,
        blocked_patterns=blocked,
        sanitized_content=sanitized,
        warnings=[f"Blocked pattern: {p}" for p in blocked],
    )


def is_safe_url(url: str) -> tuple[bool, str]:
    """Check if a URL is from a safe domain.

    Returns:
        (is_safe, reason)
    """
    if not url:
        return True, ""

    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Check blocked domains
        for blocked in BLOCKED_DOMAINS:
            if blocked in domain:
                return False, f"Blocked domain: {blocked}"

        # Check safe domains
        for safe in SAFE_DOMAINS:
            if domain.endswith(safe):
                return True, ""

        # Unknown domain - allow but warn
        return True, f"Unknown domain: {domain}"
    except Exception:  # noqa: BLE001 - defensive parsing of external URLs
        return False, "Invalid URL"


def filter_resources(
    resources: list,
    scan_source: str = "tavily_search",
) -> tuple[list, list[str]]:
    """Filter a list of resources, removing those with injection patterns.

    Returns:
        (filtered_resources, warnings)
    """
    from app.models.schemas import ResourceCandidate

    filtered: list = []
    warnings: list[str] = []

    for r in resources:
        # Combine all text fields for scanning
        if isinstance(r, ResourceCandidate):
            text_to_scan = " ".join(filter(None, [
                r.name, r.summary or "", r.evidence or "",
                r.location or "", r.opening_hours or "",
            ]))
        else:
            text_to_scan = str(r)

        result = scan_content(text_to_scan, source=scan_source)

        if not result.safe:
            warnings.append(f"Resource blocked: {getattr(r, 'name', 'unknown')[:50]} - {result.blocked_patterns}")
            continue

        # Check URL safety
        source_url = getattr(r, 'source_url', None)
        if source_url:
            url_safe, url_reason = is_safe_url(source_url)
            if not url_safe:
                warnings.append(f"Resource URL blocked: {getattr(r, 'name', 'unknown')[:50]} - {url_reason}")
                continue

        filtered.append(r)

    if warnings:
        logger.warning("Guard filtered %d/%d resources: %s", len(warnings), len(resources) + len(warnings), warnings)

    return filtered, warnings


def safe_truncate(text: str, max_chars: int = 2000) -> str:
    """Truncate text safely, avoiding injection patterns at boundaries."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"
