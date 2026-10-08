"""The commands behind `ferrum config`, `ferrum doctor`, and a task run.

cli.py owns the argument parser, the streams, and the exit path; each
command lives in its own module with its own imports and helpers. Nothing
here imports cli.py, so the exit codes live in this package and the
dependency between the two stays one-way.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130

__all__ = ["EXIT_ERROR", "EXIT_INTERRUPTED", "EXIT_OK"]
