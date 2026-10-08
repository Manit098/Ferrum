"""The read-only tools the model calls: list, read, search."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from ferrum.config import Config
from ferrum.context import collect_files, limit_entries
from ferrum.safety import PathEscapeError, is_denied, safe_join
from ferrum.tools import Tool, ToolError, ToolRegistry, ToolResult, _require_string

MAX_LINE_CHARS = 300


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

    def _search(
        self, rx: re.Pattern[str], start: Path
    ) -> tuple[list[str], bool]:
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


def default_tools(root: Path, config: Config | None = None) -> ToolRegistry:
    """The standard read-only set."""
    config = config or Config()
    registry = ToolRegistry()
    for tool in (ListFiles(root, config), ReadFile(root, config), SearchCode(root, config)):
        registry.register(tool)
    return registry
