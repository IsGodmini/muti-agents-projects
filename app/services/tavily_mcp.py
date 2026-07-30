from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.services.mcp_client import MCPToolError, RemoteMCPClient

RESULT_PATTERN = re.compile(
    r"(?:^|\n)Title:\s*(?P<title>[^\n]+)\n"
    r"URL:\s*(?P<url>https?://[^\s]+)\n"
    r"Content:\s*(?P<content>.*?)(?=\nTitle:|\Z)",
    flags=re.DOTALL,
)


class TavilySearchItem(BaseModel):
    title: str
    url: str
    content: str
    score: float = Field(ge=0, le=1)
    retrieved_at: datetime


def parse_tavily_search_text(text: str) -> list[TavilySearchItem]:
    """Parse the text envelope returned by the official Tavily MCP server."""

    retrieved_at = datetime.now(UTC)
    items: list[TavilySearchItem] = []
    for index, match in enumerate(RESULT_PATTERN.finditer(text)):
        content = match.group("content").strip()
        if "\nRaw Content:" in content:
            content = content.split("\nRaw Content:", maxsplit=1)[0].strip()
        items.append(
            TavilySearchItem(
                title=match.group("title").strip(),
                url=match.group("url").strip(),
                content=content,
                score=max(0.55, 0.95 - index * 0.05),
                retrieved_at=retrieved_at,
            )
        )
    return items


def parse_structured_results(
    structured_content: dict[str, Any] | None,
) -> list[TavilySearchItem]:
    if not structured_content:
        return []
    raw_results = structured_content.get("results")
    if not isinstance(raw_results, list):
        return []

    retrieved_at = datetime.now(UTC)
    parsed: list[TavilySearchItem] = []
    for index, raw in enumerate(raw_results):
        if not isinstance(raw, dict) or not raw.get("title") or not raw.get("url"):
            continue
        parsed.append(
            TavilySearchItem(
                title=str(raw["title"]),
                url=str(raw["url"]),
                content=str(raw.get("content", "")),
                score=float(raw.get("score", max(0.55, 0.95 - index * 0.05))),
                retrieved_at=retrieved_at,
            )
        )
    return parsed


class TavilyMCPService:
    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        search_depth: str = "advanced",
        timeout_seconds: float = 30,
    ) -> None:
        self.search_depth = search_depth
        self.client = RemoteMCPClient(
            server_url=server_url,
            bearer_token=api_key,
            timeout_seconds=timeout_seconds,
        )

    async def search(self, query: str, max_results: int = 8) -> list[TavilySearchItem]:
        response = await self.client.call_tool(
            "tavily_search",
            {
                "query": query,
                "topic": "general",
                "search_depth": self.search_depth,
                "max_results": min(20, max(5, max_results)),
                "include_images": False,
                "include_raw_content": False,
            },
        )
        results = parse_structured_results(response.structured_content)
        if not results:
            results = parse_tavily_search_text(response.text)
        if not results:
            raise MCPToolError("Tavily MCP search returned no parseable results")
        return results[:max_results]
