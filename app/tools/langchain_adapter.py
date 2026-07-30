from __future__ import annotations

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode

from app.tools.registry import ToolRegistry, tool_registry


def build_langchain_tools(registry: ToolRegistry = tool_registry) -> list[StructuredTool]:
    """Expose registered domain tools to LangGraph's ToolNode."""

    tools: list[StructuredTool] = []
    for registered in registry.list():
        tools.append(
            StructuredTool.from_function(
                func=lambda _name=registered.name, **kwargs: registry.invoke(_name, kwargs),
                name=registered.name,
                description=registered.description,
                args_schema=registered.input_model,
            )
        )
    return tools


def build_tool_node(registry: ToolRegistry = tool_registry) -> ToolNode:
    return ToolNode(build_langchain_tools(registry), handle_tool_errors=True)
