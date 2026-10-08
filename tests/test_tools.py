"""Tests for the tool interface, registry, and the built-in tools."""

import os
import sys
from typing import Any, ClassVar

import pytest

from ferrum.builtin_tools import (
    ListFiles,
    ReadFile,
    RunCommand,
    SearchCode,
    default_tools,
)
from ferrum.config import Config
from ferrum.tools import Tool, ToolError, ToolRegistry, ToolResult
from ferrum.verifier import CommandResult


class EchoTool(Tool):
    name = "echo"
    description = "Echo back the given text."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, arguments):
        return ToolResult.success(str(arguments.get("text", "")))


class FailingTool(Tool):
    name = "failing"
    description = "Always reports a controlled failure."

    def execute(self, arguments):
        raise ToolError("file not found: nope.c")


class CrashingTool(Tool):
    name = "crashing"
    description = "Raises an unexpected exception."

    def execute(self, arguments):
        raise RuntimeError("boom")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "main.c").write_text(
        "#include <stdio.h>\nint buffer[10];\nint main(void) { return buffer[0]; }\n",
        encoding="utf-8",
    )
    (tmp_path / "util.c").write_text(
        "int helper(void) { return 42; }\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn borrow() {}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("noise\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    return tmp_path


def test_tool_is_abstract():
    with pytest.raises(TypeError):
        Tool()


def test_schema_shape():
    schema = EchoTool().schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["description"]
    assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_registry_register_and_dispatch():
    registry = ToolRegistry()
    registry.register(EchoTool())
    assert len(registry) == 1
    assert "echo" in registry
    result = registry.execute("echo", {"text": "hi"})
    assert result.ok
    assert result.to_model_message() == "hi"


def test_duplicate_registration_rejected():
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())


def test_unknown_tool_lists_available():
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = registry.execute("nope", {})
    assert not result.ok
    assert "unknown tool" in result.text
    assert "echo" in result.text


def test_expected_tool_error_is_contained():
    registry = ToolRegistry()
    registry.register(FailingTool())
    result = registry.execute("failing", {})
    assert not result.ok
    assert result.text == "file not found: nope.c"
    assert result.to_model_message() == "ERROR: file not found: nope.c"


def test_unexpected_crash_is_contained():
    registry = ToolRegistry()
    registry.register(CrashingTool())
    result = registry.execute("crashing", {})
    assert not result.ok
    assert "internal tool error" in result.text
    assert "boom" in result.text


def test_non_mapping_arguments_rejected():
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = registry.execute("echo", ["not", "a", "mapping"])
    assert not result.ok
    assert "expects an object" in result.text


def test_default_tools_names(project):
    registry = default_tools(project)
    assert registry.names() == [
        "list_files",
        "read_file",
        "search_code",
        "run_command",
    ]


def test_list_files_happy_path(project):
    result = ListFiles(project).execute({})
    assert result.ok
    assert "main.c" in result.text
    assert "src/lib.rs" in result.text
    assert "Makefile" in result.text
    assert "file(s)" in result.text


def test_list_files_respects_gitignore_and_denies(project):
    result = ListFiles(project).execute({})
    assert "debug.log" not in result.text
    assert ".env" not in result.text


def test_list_files_subdir(project):
    result = ListFiles(project).execute({"path": "src"})
    assert result.ok
    assert "src/lib.rs" in result.text
    assert "main.c" not in result.text


def test_list_files_escape_rejected(project):
    with pytest.raises(ToolError, match="escapes project root"):
        ListFiles(project).execute({"path": "../outside"})


def test_list_files_not_a_directory(project):
    with pytest.raises(ToolError, match="not a directory"):
        ListFiles(project).execute({"path": "main.c"})


def test_read_file_returns_exact_content(project):
    result = ReadFile(project).execute({"path": "main.c"})
    assert result.ok
    assert result.text == (project / "main.c").read_text(encoding="utf-8")


def test_read_file_missing(project):
    with pytest.raises(ToolError, match="file not found"):
        ReadFile(project).execute({"path": "nope.c"})


def test_read_file_directory(project):
    with pytest.raises(ToolError, match="use list_files"):
        ReadFile(project).execute({"path": "src"})


def test_read_file_denied_secret(project):
    with pytest.raises(ToolError, match="refused"):
        ReadFile(project).execute({"path": ".env"})


def test_read_file_escape(project):
    with pytest.raises(ToolError, match="escapes project root"):
        ReadFile(project).execute({"path": "../outside.txt"})


def test_read_file_too_large(project):
    tool = ReadFile(project, Config(max_file_bytes=5))
    with pytest.raises(ToolError, match="file too large"):
        tool.execute({"path": "main.c"})


def test_read_file_binary_refused(project):
    (project / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ToolError, match="binary"):
        ReadFile(project).execute({"path": "blob.bin"})


def test_read_file_invalid_utf8_refused(project):
    (project / "bad.txt").write_bytes(b"\xc3\x28invalid")
    with pytest.raises(ToolError, match="UTF-8"):
        ReadFile(project).execute({"path": "bad.txt"})


def test_read_file_requires_path(project):
    with pytest.raises(ToolError, match="path is required"):
        ReadFile(project).execute({})


def test_search_literal_finds_matches(project):
    result = SearchCode(project).execute({"pattern": "buffer"})
    assert result.ok
    assert "main.c:2:" in result.text
    assert "buffer" in result.text


def test_search_literal_is_not_regex(project):
    result = SearchCode(project).execute({"pattern": "buffer[10]"})
    assert result.ok
    assert "main.c:2:" in result.text


def test_search_regex_mode(project):
    result = SearchCode(project).execute({"pattern": r"fn \w+", "regex": True})
    assert result.ok
    assert "src/lib.rs:1:" in result.text


def test_search_invalid_regex(project):
    with pytest.raises(ToolError, match="invalid regular expression"):
        SearchCode(project).execute({"pattern": "(", "regex": True})


def test_search_no_matches(project):
    result = SearchCode(project).execute({"pattern": "zzz_not_here"})
    assert result.ok
    assert "no matches" in result.text


def test_search_subdir_scope(project):
    result = SearchCode(project).execute({"pattern": "buffer", "path": "src"})
    assert result.ok
    assert "no matches" in result.text


def test_search_respects_gitignore(project):
    result = SearchCode(project).execute({"pattern": "noise"})
    assert "no matches" in result.text


def test_search_result_cap(project):
    tool = SearchCode(project, Config(max_search_results=1))
    result = tool.execute({"pattern": "return"})
    assert result.ok
    assert "truncated at 1 results" in result.text


def test_search_requires_pattern(project):
    with pytest.raises(ToolError, match="pattern is required"):
        SearchCode(project).execute({})


# --- run_command ---


def _python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_run_command_runs_and_reports(project):
    result = RunCommand(project).execute({"command": _python("print(6*7)")})
    assert result.ok
    assert "42" in result.text
    assert "[exit 0]" in result.text
    assert result.text.startswith("$ ")


def test_run_command_failure_still_returns_output(project):
    result = RunCommand(project).execute(
        {"command": _python("import sys; sys.stderr.write('bad'); sys.exit(3)")}
    )
    assert result.ok  # a failing command is a result, not a tool error
    assert "bad" in result.text
    assert "[exit 3]" in result.text


def test_run_command_requires_command(project):
    with pytest.raises(ToolError, match="command is required"):
        RunCommand(project).execute({})


def test_run_command_rejects_shell_features(project):
    tool = RunCommand(project)
    for command in (
        "echo a | grep a",
        "make && make test",
        "ls > out.txt",
        "true; echo nope",
        "echo `hostname`",
    ):
        with pytest.raises(ToolError, match="shell features"):
            tool.execute({"command": command})


def test_run_command_cwd_subdirectory(project):
    (project / "sub").mkdir()
    result = RunCommand(project).execute(
        {
            "command": _python("import os; print(os.path.basename(os.getcwd()))"),
            "cwd": "sub",
        }
    )
    assert "sub" in result.text


def test_run_command_cwd_escape_rejected(project):
    with pytest.raises(ToolError, match="escapes project root"):
        RunCommand(project).execute({"command": "make", "cwd": "../"})


def test_run_command_cwd_not_a_directory(project):
    with pytest.raises(ToolError, match="not a directory"):
        RunCommand(project).execute({"command": "make", "cwd": "main.c"})


def test_run_command_rejects_bad_timeout(project):
    tool = RunCommand(project)
    for bad in ("soon", 0, -5, True):
        with pytest.raises(ToolError, match="timeout must be a positive integer"):
            tool.execute({"command": "make", "timeout": bad})


def test_run_command_timeout_is_capped(project, monkeypatch):
    seen = {}

    def fake(argv, cwd, timeout=300):
        seen["timeout"] = timeout
        return CommandResult(tuple(argv), 0)

    monkeypatch.setattr("ferrum.builtin_tools.run_command", fake)
    RunCommand(project, Config(command_timeout=7)).execute(
        {"command": "make", "timeout": 999}
    )
    assert seen["timeout"] == 7


@pytest.mark.skipif(os.name != "nt", reason="windows command-line semantics")
def test_split_command_keeps_windows_backslashes():
    from ferrum.builtin_tools import _split_command

    assert _split_command("clang -IC:\\src\\inc main.c") == [
        "clang",
        "-IC:\\src\\inc",
        "main.c",
    ]


@pytest.mark.skipif(os.name != "nt", reason="windows command-line semantics")
def test_split_command_strips_windows_quotes():
    from ferrum.builtin_tools import _split_command

    assert _split_command('gcc -o "my main" main.c') == [
        "gcc",
        "-o",
        "my main",
        "main.c",
    ]


@pytest.mark.skipif(os.name == "nt", reason="posix shell semantics")
def test_split_command_posix_quotes():
    from ferrum.builtin_tools import _split_command

    assert _split_command('gcc -o "my main" main.c') == [
        "gcc",
        "-o",
        "my main",
        "main.c",
    ]
