"""Guards for poking around in someone else's repository.

Every path is resolved against the project root and checked against a deny
list: no .. escapes, no .env files, no binaries.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# Directories that never hold anything we want to show the model.
DENIED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "node_modules",
        "target",
        "build",
        "dist",
    }
)

# Generated binaries and other junk that isn't source.
DENIED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".o",
    ".obj",
    ".so",
    ".dll",
    ".dylib",
    ".a",
    ".lib",
    ".exe",
    ".pdb",
    ".class",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
)

# Might contain secrets. Never hand these to a model.
SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.keystore",
)


class SafetyError(Exception):
    pass


class PathEscapeError(SafetyError):
    pass


def is_within(root: Path, path: Path) -> bool:
    """True if path resolves to somewhere inside root."""
    try:
        root = root.resolve()
        path = path.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def safe_join(root: Path, *parts: str) -> Path:
    """Join parts onto root, refusing anything that lands outside it."""
    root = root.resolve()
    target = root
    for part in parts:
        piece = str(part)
        if not piece:
            continue
        p = Path(piece)
        if p.is_absolute() or p.drive or p.root:
            raise PathEscapeError(f"absolute path is not allowed here: {piece!r}")
        target = target / p
    target = target.resolve()
    if target != root and root not in target.parents:
        raise PathEscapeError(
            f"path escapes project root: {'/'.join(parts)!r} -> {target}"
        )
    return target


def is_denied(rel_path: str | PurePosixPath) -> bool:
    """True if this root-relative path should never reach the model."""
    rel = PurePosixPath(str(rel_path).replace("\\", "/"))
    if any(part in DENIED_DIRS for part in rel.parts):
        return True
    if any(
        rel.match(p) or PurePosixPath(rel.as_posix().lower()).match(p)
        for p in SENSITIVE_PATTERNS
    ):
        return True
    name = rel.name.lower()
    return any(name.endswith(suffix) for suffix in DENIED_SUFFIXES)
