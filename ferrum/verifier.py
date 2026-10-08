"""Run the project's real build and tests, honestly.

Detection order: cargo, cmake, make (with a direct-compiler fallback when
make is missing), then python/pytest. Whatever runs - or fails to run -
is reported verbatim so the model can learn from it. Nothing here ever
claims a fix passed unless a command actually exited 0.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ferrum.safety import is_denied

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
C_SOURCES = {".c"}
CXX_SOURCES = {".cc", ".cpp", ".cxx"}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")
MAX_OUTPUT_CHARS = 8000

# Conventional exits for infrastructure failures.
EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124
EXIT_BAD_EXEC = 126


@dataclass(frozen=True)
class CommandResult:
    cmd: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        chunks = [text for text in (self.stdout, self.stderr) if text]
        text = "\n".join(chunks)
        if len(text) > MAX_OUTPUT_CHARS:
            text = (
                text[:3000]
                + "\n... truncated ...\n"
                + text[-MAX_OUTPUT_CHARS + 3000 - 20 :]
            )
        return text

    def render(self) -> str:
        line = "$ " + " ".join(self.cmd)
        if self.returncode is None:
            return f"{line}\n{self.stderr or 'did not run'}"
        body = self.output.strip()
        if not body:
            return f"{line} [exit {self.returncode}]"
        return f"{line}\n{body}\n[exit {self.returncode}]"


def run_command(cmd: Sequence[str], cwd: Path, timeout: int = 300) -> CommandResult:
    cmd = tuple(cmd)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            cmd,
            proc.returncode,
            proc.stdout,
            proc.stderr,
            time.perf_counter() - started,
        )
    except FileNotFoundError:
        return CommandResult(cmd, EXIT_NOT_FOUND, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(cmd, EXIT_TIMEOUT, "", f"timed out after {timeout}s")
    except OSError as exc:
        return CommandResult(cmd, EXIT_BAD_EXEC, "", str(exc))


@dataclass(frozen=True)
class VerifyOutcome:
    label: str
    builds: tuple[CommandResult, ...] = ()
    tests: tuple[CommandResult, ...] = ()

    @property
    def ran_anything(self) -> bool:
        return bool(self.builds or self.tests)

    @property
    def verified(self) -> bool:
        if not self.ran_anything:
            return False
        return all(r.ok for r in (*self.builds, *self.tests))

    @staticmethod
    def _section(name: str, results: tuple[CommandResult, ...]) -> str:
        if not results:
            return f"{name}: SKIPPED (nothing ran)"
        status = "PASS" if all(r.ok for r in results) else "FAIL"
        body = "\n".join(r.render() for r in results)
        return f"{name}: {status}\n{body}"

    def report(self) -> str:
        parts = [f"project type: {self.label}"]
        parts.append(self._section("build", self.builds))
        parts.append(self._section("test", self.tests))
        return "\n".join(parts)


class Verifier:
    def __init__(
        self,
        root: Path,
        timeout: int = 300,
        runner: Callable[..., CommandResult] = run_command,
        on_command: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.timeout = timeout
        self.runner = runner
        self.on_command = on_command

    def detect(self) -> tuple[str, list[list[str]], list[list[str]]]:
        root = self.root
        if (root / "Cargo.toml").exists():
            return "cargo", [["cargo", "build"]], [["cargo", "test"]]
        if (root / "CMakeLists.txt").exists():
            return (
                "cmake",
                [["cmake", "-S", ".", "-B", "build"], ["cmake", "--build", "build"]],
                [["ctest", "--test-dir", "build", "--output-on-failure"]],
            )
        if (root / "Makefile").exists() or (root / "makefile").exists():
            if shutil.which("make"):
                return "make", [["make"]], [["make", "test"]]
            return self._direct_compile()
        if self._python_project():
            tests = []
            if importlib.util.find_spec("pytest") is not None:
                tests = [[sys.executable, "-m", "pytest", "-q"]]
            return "python", [], tests
        return "none", [], []

    def _python_project(self) -> bool:
        return any((self.root / marker).exists() for marker in PYTHON_MARKERS)

    def _direct_compile(self) -> tuple[str, list[list[str]], list[list[str]]]:
        """make is missing but a Makefile exists: compile the sources directly."""
        sources: list[str] = []
        header_dirs: set[str] = set()
        has_cxx = False
        for path in self.root.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in C_SOURCES | CXX_SOURCES | HEADER_SUFFIXES
            ):
                continue
            rel = path.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts) or is_denied(
                rel.as_posix()
            ):
                continue
            posix = rel.as_posix()
            if path.suffix in HEADER_SUFFIXES:
                header_dirs.add(posix.rsplit("/", 1)[0] if "/" in posix else "")
            elif path.suffix in CXX_SOURCES:
                has_cxx = True
                sources.append(posix)
            else:
                sources.append(posix)
        if not sources:
            return "clang", [], []
        candidates = ["clang++", "g++", "c++"] if has_cxx else ["clang", "gcc", "cc"]
        compiler = next((c for c in candidates if shutil.which(c)), candidates[0])
        suffix = ".exe" if sys.platform == "win32" else ""
        out = (
            Path(tempfile.gettempdir())
            / f"ferrum_verify_{uuid.uuid4().hex[:8]}{suffix}"
        )
        cmd = [compiler]
        for directory in sorted(d for d in header_dirs if d):
            cmd.append(f"-I{directory}")
        cmd += sorted(sources) + ["-o", str(out)]
        return "clang (direct)", [cmd], []

    def verify(self) -> VerifyOutcome:
        label, builds, tests = self.detect()
        build_results: list[CommandResult] = []
        for cmd in builds:
            result = self._run(cmd)
            build_results.append(result)
            if not result.ok:
                return VerifyOutcome(label, tuple(build_results), ())
        test_results: list[CommandResult] = []
        for cmd in tests:
            result = self._run(cmd)
            if result.returncode == 5 and "pytest" in cmd:
                continue  # pytest exit 5: no tests collected - not a failure
            test_results.append(result)
            if not result.ok:
                break
        return VerifyOutcome(label, tuple(build_results), tuple(test_results))

    def _run(self, cmd: list[str]) -> CommandResult:
        if self.on_command is not None:
            self.on_command(list(cmd))
        return self.runner(cmd, cwd=self.root, timeout=self.timeout)
