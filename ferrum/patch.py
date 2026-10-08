"""Exact-text patches against a single file, with stale-source detection.

The model must copy the old source verbatim; we verify it still matches
the file before anything is written.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


class PatchError(Exception):
    pass


class StaleSourceError(PatchError):
    pass


class AmbiguousPatchError(PatchError):
    pass


@dataclass(frozen=True)
class Patch:
    rel: str  # project-relative, for display
    old: str
    new: str
    description: str = ""

    def occurrences(self, current: str) -> int:
        return current.count(self.old)

    def verify(self, current: str) -> None:
        count = self.occurrences(current)
        if count == 0:
            raise StaleSourceError(
                f"old text not found in {self.rel}: the source changed "
                "since analysis, or the old text was not copied verbatim"
            )
        if count > 1:
            raise AmbiguousPatchError(
                f"old text matches {count} times in {self.rel}; "
                "include more surrounding context"
            )

    def apply(self, current: str) -> str:
        self.verify(current)
        return current.replace(self.old, self.new, 1)


def format_unified_diff(original: str, updated: str, rel: str) -> str:
    lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    text = "".join(lines)
    if not text:
        return f"(no text change in {rel})"
    if not text.endswith("\n"):
        text += "\n"
    return text
