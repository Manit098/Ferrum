"""The tool contract: results, base class, and the registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

log = logging.getLogger(__name__)

MAX_LINE_CHARS = 300


class ToolError(Exception):
    """A normal, reportable failure: bad path, denied file, that sort of thing."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str

    @classmethod
    def success(cls, text: str) -> ToolResult:
        return cls(ok=True, text=text)

    @classmethod
    def failure(cls, text: str) -> ToolResult:
        return cls(ok=False, text=text)

    def to_model_message(self) -> str:
        return self.text if self.ok else f"ERROR: {self.text}"


class Tool(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        """Do the thing. Expected failures should raise ToolError."""

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function schema, which is what most APIs expect."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Looks tools up by name and turns their mistakes into error results."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool must define a non-empty name")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(
                f"unknown tool {name!r}; available tools: "
                + (", ".join(sorted(self._tools)) or "(none)")
            )
        if not isinstance(arguments, Mapping):
            return ToolResult.failure(
                f"tool {name!r} expects an object of arguments, "
                f"got {type(arguments).__name__}"
            )
        try:
            return tool.execute(arguments)
        except ToolError as exc:
            log.info("tool %s reported: %s", name, exc)
            return ToolResult.failure(str(exc))
        except Exception as exc:
            log.exception("tool %s crashed", name)
            return ToolResult.failure(f"internal tool error: {exc}")

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} is required and must be a non-empty string")
    return value
