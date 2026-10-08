"""Gitignore semantics: glob to regex, load a file, ask if ignored."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    prefix: str  # directory the .gitignore lived in, "" for the root one
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    anchored: bool

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.prefix:
            if not rel.startswith(self.prefix):
                return False
            rel = rel[len(self.prefix) :]
        if self.anchored:
            return self.regex.match(rel) is not None
        # Patterns without a slash match any name at any depth.
        return self.regex.match(PurePosixPath(rel).name) is not None


def translate(pattern: str) -> str:
    """Turn a gitignore glob into a regex. Good enough for real-world files."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*" and pattern[i : i + 2] == "**":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")  # any number of directories
                i += 3
            else:
                out.append(".*")
                i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            end = pattern.find("]", i)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):  # git uses ! where regex uses ^
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def compile_rule(line: str, base_rel: str) -> Rule | None:
    pattern = line.rstrip()
    if not pattern or pattern.startswith("#"):
        return None
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if not pattern:
        return None
    anchored = pattern.startswith("/") or "/" in pattern
    pattern = pattern.removeprefix("/")
    prefix = "" if base_rel == "." else base_rel + "/"
    return Rule(
        prefix=prefix,
        regex=re.compile("^" + translate(pattern) + "$"),
        negated=negated,
        dir_only=dir_only,
        anchored=anchored,
    )


def load_rules(directory: Path, base_rel: str) -> list[Rule]:
    path = directory / ".gitignore"
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 64 * 1024:
            log.debug("skipping oversized .gitignore: %s", path)
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return []
    rules = []
    for line in text.splitlines():
        rule = compile_rule(line, base_rel)
        if rule is not None:
            rules.append(rule)
    return rules


def is_ignored(rel: str, rules: list[Rule], is_dir: bool) -> bool:
    ignored = False
    for rule in rules:  # last matching rule wins, git-style
        if rule.matches(rel, is_dir):
            ignored = not rule.negated
    return ignored


def ancestor_rules(root: Path, start: Path) -> list[Rule]:
    """Gitignore rules from root down to (but not including) start."""
    if start == root:
        return []
    rel = start.relative_to(root)
    rules: list[Rule] = []
    for i in range(len(rel.parts)):
        base = "/".join(rel.parts[:i]) or "."
        rules.extend(load_rules(root.joinpath(*rel.parts[:i]), base))
    return rules


