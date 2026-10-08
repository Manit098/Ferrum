"""Status output: plain step lines, one moving line, nothing leaked."""

import io
import sys

from ferrum.ui import Console


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_default_stream_is_stderr():
    # stdout is the content channel; status never competes with it.
    assert Console().stream is sys.stderr


def test_step_and_tool_write_one_line_each(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    console = Console(buf)
    console.step("12 files · C · Make")
    console.tool("reading ferrum/cli.py")
    out = buf.getvalue()
    assert "· 12 files · C · Make\n" in out
    assert "→ reading ferrum/cli.py\n" in out


def test_non_tty_live_is_one_plain_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    console = Console(buf)
    with console.live("reading project"):
        console.step("24 files · C, Python")
    out = buf.getvalue()
    assert out.startswith("· reading project ...\n")
    assert out.endswith("· 24 files · C, Python\n")
    assert out.count("reading project") == 1


def test_terminal_live_stops_when_it_exits(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = FakeTTY()
    console = Console(buf)
    with console.live("asking deepseek-v4-flash:free"):
        assert console._thread is not None
    assert console._thread is None
    assert console._shown == ""


def test_nested_live_keeps_the_outer_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = FakeTTY()
    console = Console(buf)
    with console.live("outer"):
        with console.live("inner"):
            assert console._thread is not None
        assert console._thread is not None
    assert console._thread is None
    assert console._shown == ""


def test_step_blanks_the_live_line_before_writing(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    console = Console(buf)
    console._shown = "asking 1s"  # what the spinner would have painted
    console.step("12 files · C")
    out = buf.getvalue()
    assert " " * len("asking 1s") in out  # erased, not left on screen
    assert out.endswith("· 12 files · C\n")


def test_close_is_safe_when_nothing_ran():
    buf = io.StringIO()
    console = Console(buf)
    console.close()
    console.close()  # twice, idempotent
    assert buf.getvalue() == ""


def test_a_closed_stream_never_raises():
    class Broken(io.StringIO):
        def write(self, text):
            raise ValueError("stream closed under us")

    console = Console(Broken())
    console.step("still fine")
    console.tool("still fine")
    console.clear()
