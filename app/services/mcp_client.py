from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


class MCPToolError(RuntimeError):
    """Raised when an MCP transport or remote tool call fails."""


@dataclass(frozen=True)
class MCPToolResponse:
    text: str
    structured_content: dict[str, Any] | None = None


class RemoteMCPClient:
    """Small, request-scoped Streamable HTTP MCP client."""

    def __init__(
        self,
        *,
        server_url: str,
        bearer_token: str,
        timeout_seconds: float = 30,
        default_parameters: dict[str, Any] | None = None,
    ) -> None:
        if not bearer_token:
            raise ValueError("An MCP bearer token is required")
        self.server_url = server_url
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.default_parameters = default_parameters or {}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResponse:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Accept": "application/json, text/event-stream",
        }
        if self.default_parameters:
            headers["DEFAULT_PARAMETERS"] = json.dumps(
                self.default_parameters,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        try:
            async with (
                httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(self.timeout_seconds),
                    follow_redirects=True,
                ) as http_client,
                streamable_http_client(
                    self.server_url,
                    http_client=http_client,
                ) as (read_stream, write_stream, _),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, arguments)
        except Exception as exc:
            raise MCPToolError(f"MCP tool {name} could not be reached") from exc

        if result.isError:
            detail = "\n".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            raise MCPToolError(detail or f"MCP tool {name} returned an error")

        text = "\n".join(
            item.text for item in result.content if isinstance(item, TextContent)
        )
        structured = getattr(result, "structuredContent", None)
        if not text and not structured:
            raise MCPToolError(f"MCP tool {name} returned no content")
        return MCPToolResponse(text=text, structured_content=structured)
