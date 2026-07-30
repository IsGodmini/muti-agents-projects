from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable
from typing import Any, TypeVar

from pydantic import BaseModel


class ToolRisk(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE_INTERNAL = "WRITE_INTERNAL"
    EXTERNAL_ACTION = "EXTERNAL_ACTION"


InputT = TypeVar("InputT", bound=BaseModel)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    category: str
    risk_level: ToolRisk
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Any]

    def invoke(self, payload: dict[str, Any]) -> Any:
        validated = self.input_model.model_validate(payload)
        result = self.handler(validated)
        if isawaitable(result):
            if hasattr(result, "close"):
                result.close()
            raise RuntimeError(f"Tool {self.name} is asynchronous; use ToolRegistry.ainvoke")
        return result

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        validated = self.input_model.model_validate(payload)
        result = self.handler(validated)
        return await result if isawaitable(result) else result


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        category: str,
        risk_level: ToolRisk,
        input_model: type[InputT],
    ) -> Callable[[Callable[[InputT], Any]], Callable[[InputT], Any]]:
        def decorator(handler: Callable[[InputT], Any]) -> Callable[[InputT], Any]:
            if name in self._tools:
                raise ValueError(f"Tool already registered: {name}")
            self._tools[name] = RegisteredTool(
                name=name,
                description=description,
                category=category,
                risk_level=risk_level,
                input_model=input_model,
                handler=handler,
            )
            return handler

        return decorator

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def invoke(self, name: str, payload: dict[str, Any]) -> Any:
        return self.get(name).invoke(payload)

    async def ainvoke(self, name: str, payload: dict[str, Any]) -> Any:
        return await self.get(name).ainvoke(payload)

    def list(self) -> list[RegisteredTool]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)


tool_registry = ToolRegistry()
