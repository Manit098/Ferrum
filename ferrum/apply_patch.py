"""The apply_patch tool: guard the file, patch it, then verify the build.

Everything that touches the filesystem lives here so the agent loop stays a
conversation. The tool never writes without the user's yes (or, in dry-run,
never writes at all), and it reports what the build said afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from ferrum.patch import Patch, format_unified_diff
from ferrum.safety import PathEscapeError, is_denied, safe_join
from ferrum.tools import Tool, ToolError, ToolResult, _require_string
from ferrum.ui import Console
from ferrum.verifier import Verifier, VerifyOutcome, run_command

DRY_RUN_RESULT = (
    "Dry run: the patch was NOT applied to {rel}"
    " — the file on disk is unchanged and no verification ran. "
    "Summarize this proposed change for the user and stop."
)

DECLINED_RESULT = (
    "The user declined this patch. Do not apply it. Ask what they would prefer or stop."
)

GIT_TIMEOUT = 15


class ApplyPatch(Tool):
    name = "apply_patch"
    description = (
        "Replace exact text in a project file after showing the diff and "
        "asking the user. The old text must match the file verbatim and "
        "uniquely. After a successful patch the project is built and "
        "tested; in a git work tree the result also carries git's status "
        "and diff for the file, and the verification output comes back "
        "afterwards."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the project root.",
            },
            "old": {
                "type": "string",
                "description": "Exact text currently in the file.",
            },
            "new": {
                "type": "string",
                "description": "Replacement text.",
            },
            "description": {
                "type": "string",
                "description": "One line: why this change fixes the problem.",
            },
        },
        "required": ["path", "old", "new"],
    }

    def __init__(
        self,
        root: Path,
        confirm: Callable[[str], bool],
        ui: Callable[[str], None] = print,
        verifier: Verifier | None = None,
        dry_run: bool = False,
        console: Console | None = None,
    ) -> None:
        self.root = Path(root)
        self.confirm = confirm
        self.ui = ui
        self.verifier = verifier
        self.dry_run = dry_run
        self.console = console
        self.applied = False
        self.outcome: VerifyOutcome | None = None

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        rel = _require_string(arguments, "path")
        old = _require_string(arguments, "old")
        new, description = _new_text(arguments)

        target, current, crlf = _read_target(self.root, rel)
        updated = _apply(rel, current, old, new, description)
        self.ui(format_unified_diff(current, updated, rel))

        if self.dry_run:
            return ToolResult.success(DRY_RUN_RESULT.format(rel=rel))
        if not self.confirm("Apply patch? [y/N] "):
            return ToolResult.failure(DECLINED_RESULT)

        _write(target, updated, crlf, rel)
        self.applied = True
        return ToolResult.success(self._report(rel, description))

    def _report(self, rel: str, description: str) -> str:
        """The tool's answer: what changed, then what the build said."""
        report = f"Patch applied to {rel}."
        if description:
            report += f" ({description})"
        git_section = _git_report(self.root, rel)
        if git_section:
            report += "\n\n--- git ---\n" + git_section
        if self.verifier is None:
            return report + "\n(verification unavailable)"
        live = (
            self.console.live("building and testing")
            if self.console is not None
            else nullcontext()
        )
        with live:
            self.outcome = self.verifier.verify()
        return report + "\n\n--- verification ---\n" + self.outcome.report()


def _new_text(arguments: Mapping[str, Any]) -> tuple[str, str]:
    new = arguments.get("new")
    if not isinstance(new, str):
        raise ToolError("new is required and must be a string")
    description = arguments.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    return new, description


def _read_target(root: Path, rel: str) -> tuple[Path, str, bool]:
    """Resolve, guard, and read the file; returns text with LF endings."""
    if is_denied(PurePosixPath(rel.replace("\\", "/"))):
        raise ToolError(f"refused: {rel} is excluded (secret, binary, or ignored)")
    try:
        target = safe_join(root, rel)
    except PathEscapeError as exc:
        raise ToolError(str(exc)) from exc
    if target.is_dir():
        raise ToolError(f"is a directory: {rel}")
    if not target.exists():
        raise ToolError(f"file not found: {rel} (read it first)")

    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read {rel}: {exc}") from exc
    if b"\x00" in data[:8192]:
        raise ToolError(f"refused: {rel} looks like a binary file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"not valid UTF-8 text: {rel}") from exc

    crlf = "\r\n" in text
    return target, text.replace("\r\n", "\n").replace("\r", "\n"), crlf


def _apply(rel: str, current: str, old: str, new: str, description: str) -> str:
    patch = Patch(rel=rel, old=old, new=new, description=description)
    try:
        return patch.apply(current)
    except Exception as exc:
        raise ToolError(f"Patch rejected: {exc}. Re-read the file and retry.") from exc


def _write(target: Path, updated: str, crlf: bool, rel: str) -> None:
    out = updated.replace("\n", "\r\n") if crlf else updated
    try:
        target.write_bytes(out.encode("utf-8"))
    except OSError as exc:
        raise ToolError(f"cannot write {rel}: {exc}") from exc


def _git_report(root: Path, rel: str) -> str | None:
    """What git sees for this file after the write.

    Returns None when git is missing or root is not a work tree, so projects
    outside version control never pay for the extra sections.
    """
    probe = run_command(
        ["git", "rev-parse", "--is-inside-work-tree"], root, timeout=GIT_TIMEOUT
    )
    if not probe.ok or probe.stdout.strip() != "true":
        return None
    status = run_command(
        ["git", "status", "--porcelain", "--", rel], root, timeout=GIT_TIMEOUT
    )
    lines = [status.stdout.strip() or "(no changes against the index)"]
    diff = run_command(
        ["git", "diff", "--no-color", "--", rel], root, timeout=GIT_TIMEOUT
    )
    if diff.stdout.strip():
        lines.append(diff.stdout.strip())
    return "\n".join(lines)
