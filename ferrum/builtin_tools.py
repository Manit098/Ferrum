"""The tools the model calls: list, read, search, and run one command."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from ferrum.config import Config
from ferrum.context import collect_files, limit_entries
from ferrum.safety import PathEscapeError, is_denied, safe_join
from ferrum.tools import Tool, ToolError, ToolRegistry, ToolResult, _require_string
from ferrum.verifier import run_command

MAX_LINE_CHARS = 300

# Shell plumbing is refused: commands run as one program with its arguments,
# so a model cannot chain, pipe, or redirect its way past the project root.
SHELL_CHARS = frozenset(";|&<>`\n")


class ListFiles(Tool):
    name = "list_files"
    description = (
        "List source and build files in the project, respecting .gitignore "
        "and size limits. Returns one relative path per line."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional subdirectory relative to the project root.",
            }
        },
    }

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = root
        self.config = config or Config()

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        sub = arguments.get("path") or ""
        if not isinstance(sub, str):
            raise ToolError("path must be a string")
        try:
            start = safe_join(self.root, sub) if sub else self.root
        except PathEscapeError as exc:
            raise ToolError(str(exc)) from exc
        if not start.is_dir():
            raise ToolError(f"not a directory: {sub}")
        entries = limit_entries(
            collect_files(self.root, self.config, start=start), self.config
        )
        if not entries:
            return ToolResult.success("(no files matched)")
        paths = [entry.path for entry in entries]
        return ToolResult.success("\n".join(paths) + f"\n\n{len(paths)} file(s)")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a single project file as UTF-8 text. Paths are relative to the "
        "project root; secrets, binaries and oversized files are refused. "
        "Line endings are normalized to \\n."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the project root.",
            }
        },
        "required": ["path"],
    }

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = root
        self.config = config or Config()

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        rel = _require_string(arguments, "path")
        if is_denied(PurePosixPath(rel.replace("\\", "/"))):
            raise ToolError(f"refused: {rel} is excluded (secret, binary, or ignored)")
        try:
            target = safe_join(self.root, rel)
        except PathEscapeError as exc:
            raise ToolError(str(exc)) from exc
        if target.is_dir():
            raise ToolError(f"is a directory: {rel} (use list_files)")
        if not target.exists():
            raise ToolError(f"file not found: {rel}")
        try:
            size = target.stat().st_size
            if size > self.config.max_file_bytes:
                raise ToolError(
                    f"file too large: {rel} ({size} bytes > limit "
                    f"{self.config.max_file_bytes})"
                )
            data = target.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read {rel}: {exc}") from exc
        if b"\x00" in data[:8192]:
            raise ToolError(f"refused: {rel} looks like a binary file")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"not valid UTF-8 text: {rel}") from exc
        return ToolResult.success(text.replace("\r\n", "\n").replace("\r", "\n"))


class SearchCode(Tool):
    name = "search_code"
    description = (
        "Search project source files for a string or regular expression. "
        "Returns matches as path:line: content. Case-sensitive by default."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Text or regular expression to search for.",
            },
            "path": {
                "type": "string",
                "description": "Optional subdirectory to restrict the search.",
            },
            "regex": {
                "type": "boolean",
                "description": "Treat pattern as a regular expression (default false).",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = root
        self.config = config or Config()

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        pattern, start, rx = self._plan(arguments)
        matches, truncated = self._search(rx, start)
        if not matches:
            return ToolResult.success(f"no matches for {pattern!r}")
        text = "\n".join(matches)
        if truncated:
            text += f"\n... truncated at {self.config.max_search_results} results"
        return ToolResult.success(text)

    def _plan(self, arguments: Mapping[str, Any]) -> tuple[str, Path, re.Pattern[str]]:
        """Check the arguments, resolve the scope, and compile the pattern."""
        pattern = _require_string(arguments, "pattern")
        sub = arguments.get("path") or ""
        if not isinstance(sub, str):
            raise ToolError("path must be a string")
        regex_mode = arguments.get("regex", False)
        if not isinstance(regex_mode, bool):
            raise ToolError("regex must be a boolean")
        try:
            start = safe_join(self.root, sub) if sub else self.root
        except PathEscapeError as exc:
            raise ToolError(str(exc)) from exc
        if not start.is_dir():
            raise ToolError(f"not a directory: {sub}")
        try:
            rx = re.compile(pattern if regex_mode else re.escape(pattern))
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        return pattern, start, rx

    def _search(self, rx: re.Pattern[str], start: Path) -> tuple[list[str], bool]:
        """path:line snippets, stopping once the result cap is reached."""
        matches: list[str] = []
        truncated = False
        for entry in collect_files(self.root, self.config, start=start):
            try:
                content = (self.root / entry.path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            for lineno, line in enumerate(content.splitlines(), start=1):
                if rx.search(line):
                    snippet = line.strip()
                    if len(snippet) > MAX_LINE_CHARS:
                        snippet = snippet[: MAX_LINE_CHARS - 3] + "..."
                    matches.append(f"{entry.path}:{lineno}: {snippet}")
                    if len(matches) >= self.config.max_search_results:
                        truncated = True
                        break
            if truncated:
                break
        return matches, truncated


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run one build, test, or analysis command in the project and return "
        "its output and exit code. One command per call; shell features "
        "(pipes, redirection, chaining) are not supported. Use it to "
        "reproduce a failure, read real compiler diagnostics, or run the "
        "program before and after a patch."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The command line to run, e.g. `make test` or "
                    "`clang -Wall -o main main.c`."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Optional subdirectory to run in, relative to the project root."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds to wait before killing the command "
                    "(capped at the configured command timeout)."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = Path(root)
        self.config = config or Config()

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        argv = _split_command(_require_string(arguments, "command"))
        cwd = self._cwd(arguments)
        timeout = self._timeout(arguments)
        return ToolResult.success(run_command(argv, cwd, timeout=timeout).render())

    def _cwd(self, arguments: Mapping[str, Any]) -> Path:
        sub = arguments.get("cwd") or ""
        if not isinstance(sub, str):
            raise ToolError("cwd must be a string")
        if not sub:
            return self.root
        if is_denied(PurePosixPath(sub.replace("\\", "/"))):
            raise ToolError(f"refused: {sub} is excluded")
        try:
            start = safe_join(self.root, sub)
        except PathEscapeError as exc:
            raise ToolError(str(exc)) from exc
        if not start.is_dir():
            raise ToolError(f"not a directory: {sub}")
        return start

    def _timeout(self, arguments: Mapping[str, Any]) -> int:
        limit = self.config.command_timeout
        raw = arguments.get("timeout")
        if raw is None:
            return limit
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ToolError(f"timeout must be a positive integer, got {raw!r}")
        return min(raw, limit)


def _split_command(command: str) -> list[str]:
    """One command line -> argv, refusing shell plumbing outside quotes."""
    outside = _unquoted(command)
    if (SHELL_CHARS & set(outside)) or "$(" in outside:
        raise ToolError(
            "shell features are not supported (pipes, redirection, chaining); "
            "run a single command with its arguments"
        )
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ToolError(f"cannot parse command: {exc}") from exc
    if os.name == "nt":
        # posix=False keeps the quotes in the token; drop the matched pair.
        argv = [
            token[1:-1]
            if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'"
            else token
            for token in argv
        ]
    if not argv:
        raise ToolError("command is empty")
    return argv


def _unquoted(command: str) -> str:
    """The command with quoted spans removed, so payload text is not scanned."""
    out: list[str] = []
    quote: str | None = None
    for char in command:
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        out.append(char)
    return "".join(out)


def default_tools(root: Path, config: Config | None = None) -> ToolRegistry:
    """The standard set: three readers plus one runner."""
    config = config or Config()
    registry = ToolRegistry()
    for tool in (
        ListFiles(root, config),
        ReadFile(root, config),
        SearchCode(root, config),
        RunCommand(root, config),
    ):
        registry.register(tool)
    return registry
