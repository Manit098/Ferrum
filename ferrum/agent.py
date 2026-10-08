"""The agent loop: the model reads (and, in fix mode, patches) until done."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ferrum.apply_patch import ApplyPatch
from ferrum.config import Config
from ferrum.context import ProjectContext
from ferrum.model import ModelProvider, ModelResponse
from ferrum.toolcalls import MALFORMED_JSON, ToolCall, extract_tool_calls
from ferrum.tools import ToolRegistry, ToolResult
from ferrum.ui import Console
from ferrum.verifier import Verifier

log = logging.getLogger(__name__)

# Inside the package, so wheel installs ship the real prompt too.
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"
FALLBACK_SYSTEM = (
    "You are Ferrum, a coding harness for systems programming. "
    "Inspect before you modify, cite file:line evidence, make the smallest "
    "change, and never claim a fix is verified unless a build or test "
    "actually passed."
)

READ_ONLY_NOTE = (
    "Read-only mode: investigate thoroughly before answering. A directory "
    "listing alone is not an answer — read the relevant source files and "
    "search for the relevant names; several tool calls are expected. You "
    "cannot modify files, so do not call apply_patch; run_command is "
    "available for builds, tests, and running the program."
)

FIX_NOTE = (
    "Fix mode: inspect thoroughly before patching — read the file you are "
    "about to change and its callers; several tool calls are expected. "
    "Reproduce the failure with run_command first when a build or test can "
    "show it. Call apply_patch only with text you have read in this session."
)

DRY_RUN_NOTE = (
    "Dry run: apply_patch will display your proposed diff, but the harness "
    "will not modify files, ask for confirmation, or run verification. "
    "Propose the one correct patch and then summarize it for the user."
)

NUDGE = (
    "You have not called any tools yet. Investigate first: call list_files, "
    "read_file or search_code and base your answer on what you read."
)

READ_NUDGE = (
    "A directory listing alone does not answer the question. Use read_file "
    "on the most relevant source file (and search_code for names) before "
    "you answer."
)

MAX_NUDGES = 2
READ_TOOLS = ("read_file", "search_code", "apply_patch")

PROGRESS = {
    "list_files": "inspecting the project",
    "read_file": "reading",
    "search_code": "searching",
    "run_command": "running",
    "apply_patch": "proposing a patch to",
}


@dataclass
class AgentResult:
    text: str
    turns: int
    capped: bool = False
    patched: bool = False
    verified: bool | None = None
    verify_report: str | None = None


class Agent:
    def __init__(
        self,
        config: Config,
        provider: ModelProvider,
        registry: ToolRegistry,
        printer: Callable[[str], None] | None = None,
        verifier: Verifier | None = None,
        ui: Console | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry
        self.printer = printer
        self.verifier = verifier
        self.ui = ui
        self.apply_tool: ApplyPatch | None = None

    @staticmethod
    def system_prompt() -> str:
        try:
            return PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            return FALLBACK_SYSTEM

    def run(
        self,
        task: str,
        context: ProjectContext,
        *,
        edit: bool = False,
        confirm: Callable[[str], bool] | None = None,
        dry_run: bool = False,
    ) -> AgentResult:
        if edit and "apply_patch" not in self.registry:
            if confirm is None:
                raise ValueError("edit mode requires a confirm callback")
            self._register_apply(context, confirm, dry_run)

        messages = self._messages(task, context, edit=edit, dry_run=dry_run)
        state = _TurnState()
        turns = 0
        for turn in range(1, self.config.max_iterations + 1):
            turns = turn
            response = self._complete(messages)
            calls = _merge_tool_calls(
                response.tool_calls, extract_tool_calls(response.content)
            )
            if calls:
                response = ModelResponse(response.content, calls)
            if not response.tool_calls:
                nudge = _nudge_for(response, state)
                if nudge:
                    messages.append(_assistant_message(response))
                    messages.append({"role": "user", "content": nudge})
                    continue
                return self._finish(response.content, turns, capped=False, edit=edit)
            messages.append(_assistant_message(response))
            messages.extend(self._run_tools(response, state))
        return self._finish(
            f"Stopped after {self.config.max_iterations} iterations "
            "without a final answer.",
            turns,
            capped=True,
            edit=edit,
        )

    def _run_tools(
        self, response: ModelResponse, state: _TurnState
    ) -> list[dict[str, Any]]:
        """Run every requested tool, one tool message per call."""
        state.used_tools = True
        messages: list[dict[str, Any]] = []
        for call in response.tool_calls:
            if call.name in READ_TOOLS:
                state.used_read = True
            self._progress(call)
            result = self._execute(call)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.to_model_message(),
                }
            )
        return messages

    def _register_apply(
        self,
        context: ProjectContext,
        confirm: Callable[[str], bool],
        dry_run: bool,
    ) -> None:
        """Give the model the apply_patch tool, wired to the user's answer."""
        self.apply_tool = ApplyPatch(
            context.root,
            confirm,
            ui=self.printer or print,
            verifier=self.verifier,
            dry_run=dry_run,
            console=self.ui,
        )
        self.registry.register(self.apply_tool)

    def _messages(
        self, task: str, context: ProjectContext, *, edit: bool, dry_run: bool
    ) -> list[dict[str, Any]]:
        """The first two messages: who the model is, and what it is being asked."""
        user = task
        user += "\n\n" + (FIX_NOTE if edit else READ_ONLY_NOTE)
        if dry_run:
            user += "\n\n" + DRY_RUN_NOTE
        user += "\n\nProject:\n" + context.summary()
        return [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user},
        ]

    def _complete(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """One round trip, with a live line so the wait is never silent."""
        tools = self.registry.schemas()
        if self.ui is None:
            return self.provider.complete(messages, tools)
        model = getattr(self.provider, "model", "") or "the model"
        with self.ui.live(f"asking {model}"):
            return self.provider.complete(messages, tools)

    def _execute(self, call: ToolCall) -> ToolResult:
        if MALFORMED_JSON in call.arguments:
            raw = str(call.arguments[MALFORMED_JSON])[:500]
            return ToolResult.failure(
                f"invalid JSON arguments for {call.name!r}: {raw!r}. "
                "Send only the argument values as a JSON object "
                '(e.g. {"path": "main.c"}); escape newlines as \\n.'
            )
        if not call.name:
            return ToolResult.failure("tool call had no name")
        return self.registry.execute(call.name, call.arguments)

    def _progress(self, call: ToolCall) -> None:
        label = PROGRESS.get(call.name, call.name)
        if call.name in ("read_file", "apply_patch"):
            detail = f" {call.arguments.get('path', '')}"
        elif call.name == "search_code":
            detail = f" {call.arguments.get('pattern', '')!r}"
        elif call.name == "run_command":
            detail = f" {str(call.arguments.get('command', ''))[:60]}"
        else:
            detail = ""
        text = f"{label}{detail}"
        if self.ui is not None:
            self.ui.tool(text)
        elif self.printer is not None:
            self.printer(f"Ferrum > {text}...")

    def _finish(
        self, text: str, turns: int, *, capped: bool, edit: bool
    ) -> AgentResult:
        if not text:
            text = "The model returned no answer."
        patched = bool(self.apply_tool and self.apply_tool.applied)
        verified: bool | None = None
        report: str | None = None
        if patched and self.apply_tool and self.apply_tool.outcome:
            verified = self.apply_tool.outcome.verified
            report = self.apply_tool.outcome.report()
        return AgentResult(
            text=text,
            turns=turns,
            capped=capped,
            patched=patched,
            verified=verified,
            verify_report=report,
        )


@dataclass
class _TurnState:
    """What the loop has seen so far, for deciding whether to nudge."""

    nudges: int = 0
    read_nudges: int = 0
    used_tools: bool = False
    used_read: bool = False


def _nudge_for(response: ModelResponse, state: _TurnState) -> str | None:
    """The one-line push this reply deserves, if any.

    Small models love answering from memory, so the first reply gets one
    look; a directory listing alone is not reading, so push once more.
    """
    if response.tool_calls:
        return None
    if not state.used_tools and state.nudges < MAX_NUDGES:
        state.nudges += 1
        return NUDGE
    if state.used_tools and not state.used_read and state.read_nudges < MAX_NUDGES:
        state.read_nudges += 1
        return READ_NUDGE
    return None


def _merge_tool_calls(
    native: list[ToolCall], extracted: list[ToolCall]
) -> list[ToolCall]:
    """Native calls first, then JSON-from-text calls that are not duplicates."""
    seen = {(c.name, json.dumps(c.arguments, sort_keys=True)) for c in native}
    merged = list(native)
    for call in extracted:
        key = (call.name, json.dumps(call.arguments, sort_keys=True))
        if key not in seen:
            seen.add(key)
            merged.append(call)
    return merged


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or "",
    }
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in response.tool_calls
        ]
    return message
