"""Project discovery: find the root, walk it, summarise what's there."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from ferrum.config import Config
from ferrum.gitignore import Rule, ancestor_rules, is_ignored, load_rules
from ferrum.safety import DENIED_DIRS, is_denied, is_within

log = logging.getLogger(__name__)


class Language(str, Enum):
    C = "c"
    CPP = "cpp"
    RUST = "rust"
    ASM = "asm"
    PYTHON = "python"

    @property
    def label(self) -> str:
        return {
            Language.C: "C",
            Language.CPP: "C++",
            Language.RUST: "Rust",
            Language.ASM: "Assembly",
            Language.PYTHON: "Python",
        }[self]


class BuildSystem(str, Enum):
    MAKE = "make"
    CMAKE = "cmake"
    CARGO = "cargo"
    PYTEST = "pytest"

    @property
    def label(self) -> str:
        return {
            BuildSystem.MAKE: "Make",
            BuildSystem.CMAKE: "CMake",
            BuildSystem.CARGO: "Cargo",
            BuildSystem.PYTEST: "pytest",
        }[self]


SOURCE_EXTENSIONS: dict[str, Language] = {
    ".c": Language.C,
    ".h": Language.C,
    ".cc": Language.CPP,
    ".cpp": Language.CPP,
    ".cxx": Language.CPP,
    ".c++": Language.CPP,
    ".hh": Language.CPP,
    ".hpp": Language.CPP,
    ".hxx": Language.CPP,
    ".rs": Language.RUST,
    ".s": Language.ASM,
    ".asm": Language.ASM,
    ".py": Language.PYTHON,
}

MAKE_NAMES = frozenset({"makefile", "gnumakefile"})
CMAKE_NAMES = frozenset({"cmakelists.txt"})
CARGO_NAMES = frozenset({"cargo.toml"})
PYTEST_NAMES = frozenset({"pyproject.toml", "setup.py", "pytest.ini", "tox.ini"})
BUILD_FILE_NAMES = MAKE_NAMES | CMAKE_NAMES | CARGO_NAMES | PYTEST_NAMES

PROJECT_MARKERS = (
    ".git",
    "cargo.toml",
    "cmakelists.txt",
    "makefile",
    "gnumakefile",
    "pyproject.toml",
    "setup.py",
)

# Safety net for the accidental walk: pointing ferrum at a home directory
# full of caches must not take a minute. The budget counts every directory
# and file the walk looks at, whatever ends up being kept.
MAX_EXAMINED = 20_000


@dataclass(frozen=True)
class FileEntry:
    """A file we might show the model, path relative to the project root."""

    path: str
    size: int = 0
    language: Language | None = None


@dataclass
class ProjectContext:
    """What we know about the project after a quick look."""

    root: Path
    languages: list[Language] = field(default_factory=list)
    build_systems: list[BuildSystem] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.root.name or str(self.root)

    def display_path(self) -> str:
        """Project path relative to where the user is standing, usually."""
        try:
            rel = os.path.relpath(self.root, Path.cwd())
        except ValueError:
            return str(self.root)
        rel = rel.replace("\\", "/")
        if rel == ".":
            return "."
        if not rel.startswith("."):
            rel = "./" + rel
        return rel

    def brief(self) -> str:
        """One status line: how much was found and what it looks like."""
        if not self.files:
            return "no source files found"
        count = len(self.files)
        parts = [f"{count} file" if count == 1 else f"{count} files"]
        languages = ", ".join(lang.label for lang in self.languages)
        parts.append(languages or "language unknown")
        builds = ", ".join(b.label for b in self.build_systems)
        if builds:
            parts.append(builds)
        return " · ".join(parts)

    def summary(self) -> str:
        """The block the CLI prints after `ferrum .`."""
        languages = ", ".join(lang.label for lang in self.languages) or "unknown"
        builds = ", ".join(b.label for b in self.build_systems) or "unknown"

        lines = [
            "Ferrum",
            "─" * 6,
            "",
            f"Project: {self.display_path()}",
            f"Languages: {languages}",
            f"Build system: {builds}",
            "",
            "Files:",
        ]
        if self.files:
            lines.extend(f"  {entry.path}" for entry in self.files)
        else:
            lines.append("  (none)")
        return "\n".join(lines)


def find_project_root(start: Path) -> Path:
    """Nearest directory at or above start holding a project marker."""
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return start


def _language_of(name: str) -> Language | None:
    return SOURCE_EXTENSIONS.get(PurePosixPath(name).suffix.lower())


def _walk(
    root: Path,
    current: Path,
    base_rel: str,
    inherited: list[Rule],
    out: list[FileEntry],
    config: Config,
    left: list[int],
) -> None:
    rules = list(inherited) + load_rules(current, base_rel)
    try:
        entries = sorted(current.iterdir())
    except OSError as exc:
        log.debug("cannot list %s: %s", current, exc)
        return
    for entry in entries:
        if left[0] <= 0:
            log.debug("examination budget spent under %s, stopping", current)
            return
        left[0] -= 1
        try:
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                if entry.is_symlink() or entry.name in DENIED_DIRS or is_denied(rel):
                    continue
                if is_ignored(rel, rules, is_dir=True):
                    continue
                _walk(root, entry, rel, rules, out, config, left)
            elif entry.is_file():
                if is_denied(rel) or is_ignored(rel, rules, is_dir=False):
                    continue
                size = entry.stat().st_size
                if size > config.max_file_bytes:
                    continue
                language = _language_of(entry.name)
                if language is None and not _is_build_file(entry.name):
                    continue
                out.append(FileEntry(path=rel, size=size, language=language))
        except OSError as exc:
            log.debug("skipping %s: %s", entry, exc)


def _is_build_file(name: str) -> bool:
    return name.lower() in BUILD_FILE_NAMES


def collect_files(
    root: Path, config: Config, *, start: Path | None = None
) -> list[FileEntry]:
    """Source and build files under start (default: root), sorted by path.

    Honours every .gitignore from the root down to start, plus the always-on
    deny rules and the per-file size cap. The walk stops after MAX_EXAMINED
    entries (raised with max_files) so a stray home-directory run stays fast.
    """
    root = Path(root).resolve()
    start = Path(start).resolve() if start else root
    if not is_within(root, start):
        raise ValueError(f"start is outside the project root: {start}")
    rules = ancestor_rules(root, start)
    out: list[FileEntry] = []
    base = "." if start == root else start.relative_to(root).as_posix()
    left = [max(MAX_EXAMINED, config.max_files * 20)]
    _walk(root, start, base, rules, out, config, left)
    out.sort(key=lambda entry: entry.path)
    return out


def limit_entries(entries: list[FileEntry], config: Config) -> list[FileEntry]:
    """Cut a file list down to the context budget and the file cap."""
    limited: list[FileEntry] = []
    total = 0
    for entry in entries:
        if len(limited) >= config.max_files or total + entry.size > config.max_context_bytes:
            break
        limited.append(entry)
        total += entry.size
    return limited


def _detect_languages(entries: list[FileEntry]) -> list[Language]:
    found = {entry.language for entry in entries if entry.language}
    return [lang for lang in Language if lang in found]


def _detect_build_systems(entries: list[FileEntry]) -> list[BuildSystem]:
    found: set[BuildSystem] = set()
    for entry in entries:
        name = PurePosixPath(entry.path).name.lower()
        if name in MAKE_NAMES:
            found.add(BuildSystem.MAKE)
        elif name in CMAKE_NAMES:
            found.add(BuildSystem.CMAKE)
        elif name in CARGO_NAMES:
            found.add(BuildSystem.CARGO)
        elif name in PYTEST_NAMES:
            found.add(BuildSystem.PYTEST)
    return [build for build in BuildSystem if build in found]


def discover(root: Path, config: Config | None = None) -> ProjectContext:
    config = config or Config()
    root = Path(root).resolve()
    candidates = collect_files(root, config)
    return ProjectContext(
        root=root,
        languages=_detect_languages(candidates),
        build_systems=_detect_build_systems(candidates),
        files=limit_entries(candidates, config),
    )
