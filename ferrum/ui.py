"""Terminal status output: plain step lines plus one moving line.

Everything here writes to stderr so stdout stays the content channel — the
project summary, the model's answer, and diffs. On a terminal the moving
line is a spinner with elapsed time; piped or captured output gets one
plain line per event instead, with no cursor tricks and no colours.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TICK = 0.09  # seconds between frames; fast enough to look alive

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_ERASE_LINE = "\x1b[2K"

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12


def _isatty(stream: IO[str]) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _enable_ansi(stream: IO[str]) -> bool:
    """True when escape sequences will actually render on this stream."""
    if not _isatty(stream) or os.environ.get("NO_COLOR"):
        return False
    if sys.platform != "win32":
        return True
    # Legacy conhost prints escapes literally until the flag is set.
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        std = STD_ERROR_HANDLE if stream is sys.stderr else STD_OUTPUT_HANDLE
        handle = kernel32.GetStdHandle(std)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
        )
    except Exception:  # noqa: BLE001 - colour is never worth failing over
        return False


class Console:
    """Status lines and a live label, safe to use from a worker thread."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self.stream = sys.stderr if stream is None else stream
        self.tty = _isatty(self.stream)
        self.color = _enable_ansi(self.stream)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._label = ""
        self._shown = ""
        self._depth = 0
        self._started = 0.0

    # -- plain output ---------------------------------------------------

    def step(self, text: str) -> None:
        """One fact that is already true: `· 12 files · C · Make`."""
        with self._lock:
            self._clear_locked()
            self._write_locked(f"{self._paint('·', _DIM)} {text}\n")

    def tool(self, text: str) -> None:
        """A tool call starting: `→ reading src/main.c`."""
        with self._lock:
            self._clear_locked()
            self._write_locked(f"{self._paint('→', _CYAN)} {text}\n")

    def clear(self) -> None:
        """Erase the moving line so a prompt can use the row."""
        with self._lock:
            self._clear_locked()

    # -- the moving line ------------------------------------------------

    @contextmanager
    def live(self, label: str) -> Iterator[None]:
        """Hold one line open while work happens; erase it on the way out."""
        if not self.tty:
            self.step(f"{label} ...")
            yield
            return
        self._begin(label)
        try:
            yield
        finally:
            self._end()

    def close(self) -> None:
        """Stop any live line and give the cursor back."""
        self._end()

    def _begin(self, label: str) -> None:
        with self._lock:
            self._depth += 1
            if self._thread is not None:
                self._label = label
                return
            self._label = label
            self._started = time.monotonic()
            self._stop.clear()
            if self.color:
                self._write_locked(_HIDE_CURSOR)
            self._thread = threading.Thread(
                target=self._spin, daemon=True, name="ferrum-ui"
            )
            self._thread.start()

    def _end(self) -> None:
        with self._lock:
            if self._depth > 1:
                self._depth -= 1
                return
            thread, self._thread = self._thread, None
            self._depth = 0
            self._stop.set()
        if thread is None:
            with self._lock:
                self._clear_locked()
            return
        thread.join(timeout=1.0)
        with self._lock:
            self._clear_locked()
            if self.color:
                self._write_locked(_SHOW_CURSOR)

    def _spin(self) -> None:
        index = 0
        while not self._stop.is_set():
            with self._lock:
                if self._thread is not None:
                    self._draw_locked(FRAMES[index % len(FRAMES)])
            index += 1
            self._stop.wait(TICK)

    def _draw_locked(self, frame: str) -> None:
        elapsed = int(time.monotonic() - self._started)
        body = f"{frame} {self._label}{f' {elapsed}s' if elapsed else ''}"
        self._erase_locked()
        head = self._paint(frame, _CYAN) if self.color else frame
        self._write_locked(head + body[len(frame) :] + "\r")
        self._shown = body

    # -- primitives -----------------------------------------------------

    def _clear_locked(self) -> None:
        if not self._shown:
            return
        self._erase_locked()
        self._shown = ""

    def _erase_locked(self) -> None:
        if self.color:
            self._write_locked("\r" + _ERASE_LINE)
        else:
            self._write_locked("\r" + " " * len(self._shown) + "\r")

    def _paint(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"{code}{text}{_RESET}"

    def _write_locked(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (ValueError, OSError):  # stream closed under us
            pass
