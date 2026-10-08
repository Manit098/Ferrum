"""Tool calls however the model chose to write them.

Three shapes arrive: the structured ``tool_calls`` field, a JSON object
dropped into the prose, or the tools schema echoed back instead of real
values. Everything here normalises those into plain ``ToolCall`` values so
the rest of the harness only sees one shape — and so a mangled call comes
back to the model as an error it can fix on the next turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MALFORMED_JSON = "malformed_json"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


def coerce_arguments(raw: Any) -> dict[str, Any]:
    """Whatever the model sent for ``arguments``, as a dict of values."""
    if isinstance(raw, dict):
        value = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            # Small models emit unescaped newlines; let the agent feed the
            # error back instead of killing the session.
            return {MALFORMED_JSON: raw}
        value = parsed if isinstance(parsed, dict) else {"_": parsed}
    else:
        value = {"_": raw}
    return normalize_arguments(value)


def normalize_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Small models often echo the tools schema instead of real values.

    Unwrap the shapes they produce; anything schema-shaped becomes a
    MALFORMED marker so the harness can ask the model to retry with values.
    """
    single = args.get("arguments")
    if set(args) == {"arguments"} and isinstance(single, dict):
        return normalize_arguments(single)
    parameters = args.get("parameters")
    if isinstance(parameters, dict):
        if "properties" in parameters:
            return {MALFORMED_JSON: json.dumps(args)}
        return parameters
    if "properties" in args or "required" in args:
        return {MALFORMED_JSON: json.dumps(args)}
    return args


def extract_tool_calls(content: str) -> list[ToolCall]:
    """Fallback for models that write tool calls as plain JSON in their text.

    Scans for JSON objects that name a tool; anything else in the prose is
    ignored. The harness reports unknown names back, so invented tools get
    corrected on the next turn.
    """
    if not content:
        return []
    decoder = json.JSONDecoder()
    calls: list[ToolCall] = []
    position = 0
    while True:
        start = content.find("{", position)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(content, start)
        except ValueError:
            position = start + 1
            continue
        position = end
        if not isinstance(obj, dict):
            continue
        name = _call_name(obj)
        if not name:
            continue
        raw = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        calls.append(
            ToolCall(
                id=f"text-{len(calls)}",
                name=name,
                arguments=coerce_arguments(raw),
            )
        )
    return calls


def _call_name(obj: dict[str, Any]) -> str:
    """The tool name from one JSON object, or "" when it names none."""
    name = obj.get("name") or obj.get("tool")
    if not name and isinstance(obj.get("function"), str):
        name = obj["function"]
    return name if isinstance(name, str) else ""
