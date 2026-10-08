"""The agent loop, read-only mode, and the apply_patch tool."""

import pytest

from ferrum.agent import Agent
from ferrum.apply_patch import ApplyPatch
from ferrum.builtin_tools import default_tools
from ferrum.config import Config
from ferrum.context import discover
from ferrum.model import ModelProvider, ModelResponse, ProviderError
from ferrum.toolcalls import MALFORMED_JSON, ToolCall
from ferrum.tools import ToolError, ToolRegistry
from ferrum.verifier import CommandResult, VerifyOutcome

PASSING = VerifyOutcome(
    label="fake",
    builds=(CommandResult(("build",), 0),),
    tests=(CommandResult(("test",), 0),),
)
FAILING = VerifyOutcome(
    label="fake",
    builds=(CommandResult(("build",), 1, stderr="error: boom"),),
)


class FakeProvider(ModelProvider):
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.schemas = []

    def complete(self, messages, tools):
        self.calls.append(list(messages))
        self.schemas.append(tools)
        if not self.queue:
            raise AssertionError("provider called more times than scripted")
        response = self.queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeVerifier:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def verify(self):
        self.calls += 1
        return self.outcome


@pytest.fixture
def project(tmp_path):
    (tmp_path / "main.c").write_text(
        "int main(void)\n{\n    return 0;\n}\n", encoding="utf-8"
    )
    return discover(tmp_path, Config())


def _agent(tmp_path, provider, **kwargs):
    config = kwargs.pop("config", Config())
    return Agent(
        config,
        provider,
        default_tools(tmp_path, config),
        **kwargs,
    )


def test_read_only_loop_reads_and_answers(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse("main.c always returns 0.", []),
    )
    result = _agent(tmp_path, provider).run("what does main.c do?", project)
    assert result.text == "main.c always returns 0."
    assert result.turns == 2
    assert not result.patched
    assert result.capped is False

    second = provider.calls[1]
    tool_messages = [m for m in second if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "return 0" in tool_messages[0]["content"]
    assert tool_messages[0]["tool_call_id"] == "1"

    user_message = provider.calls[0][1]
    assert "Read-only mode" in user_message["content"]
    assert "Project:" in user_message["content"]


def test_read_only_never_offers_apply_patch(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse("done", []),
    )
    agent = _agent(tmp_path, provider)
    agent.run("look around", project)
    tool_names = [fn["function"]["name"] for fn in provider.schemas[0]]
    assert "apply_patch" not in tool_names
    assert set(tool_names) == {
        "list_files",
        "read_file",
        "search_code",
        "run_command",
    }


def test_unknown_tool_is_reported_back(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "drop_tables", {})]),
        ModelResponse("", [ToolCall("2", "read_file", {"path": "main.c"})]),
        ModelResponse("ok then.", []),
    )
    result = _agent(tmp_path, provider).run("go", project)
    assert result.text == "ok then."
    tool_message = next(m for m in provider.calls[1] if m["role"] == "tool")
    assert "unknown tool" in tool_message["content"]


def test_malformed_json_arguments_get_actionable_feedback(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "apply_patch", {MALFORMED_JSON: '{"old":'})]),
        ModelResponse("understood.", []),
    )
    _agent(tmp_path, provider).run("go", project)
    tool_message = next(m for m in provider.calls[1] if m["role"] == "tool")
    assert "invalid JSON arguments" in tool_message["content"]
    assert tool_message["content"].startswith("ERROR:")


def test_iteration_cap_stops_the_loop(tmp_path, project):
    scripted = [
        ModelResponse("", [ToolCall(str(i), "list_files", {})]) for i in range(5)
    ]
    provider = FakeProvider(*scripted)
    result = _agent(tmp_path, provider, config=Config(max_iterations=2)).run(
        "loop forever", project
    )
    assert result.capped is True
    assert result.turns == 2
    assert "Stopped after 2 iterations" in result.text


def test_provider_error_propagates(tmp_path, project):
    provider = FakeProvider(ProviderError("server exploded"))
    with pytest.raises(ProviderError, match="server exploded"):
        _agent(tmp_path, provider).run("go", project)


def test_text_json_tool_calls_are_executed(tmp_path, project):
    provider = FakeProvider(
        ModelResponse(
            'Reading it now: {"name": "read_file", "arguments": {"path": "main.c"}}',
            [],
        ),
        ModelResponse("main.c returns 0.", []),
    )
    result = _agent(tmp_path, provider).run("go", project)
    assert result.text == "main.c returns 0."
    second = provider.calls[1]
    tool_message = next(m for m in second if m["role"] == "tool")
    assert "return 0" in tool_message["content"]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert "Reading it now" in assistant["content"]


def test_duplicate_native_and_text_call_runs_once(tmp_path, project):
    json_text = '{"name": "list_files", "arguments": {}}'
    provider = FakeProvider(
        ModelResponse(json_text, [ToolCall("1", "list_files", {})]),
        ModelResponse("", [ToolCall("2", "read_file", {"path": "main.c"})]),
        ModelResponse("done", []),
    )
    _agent(tmp_path, provider).run("go", project)
    tool_messages = [m for m in provider.calls[1] if m["role"] == "tool"]
    assert len(tool_messages) == 1


def test_listing_only_answer_gets_read_nudge(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "list_files", {})]),
        ModelResponse("Judging from the listing, done.", []),
        ModelResponse("", [ToolCall("2", "read_file", {"path": "main.c"})]),
        ModelResponse("Now I have read it.", []),
    )
    result = _agent(tmp_path, provider).run("what is in main.c?", project)
    assert result.text == "Now I have read it."
    assert result.turns == 4
    third_turn = provider.calls[2]
    user_messages = [m for m in third_turn if m["role"] == "user"]
    assert any("listing alone" in m["content"] for m in user_messages)


def test_search_code_counts_as_reading(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "search_code", {"pattern": "return 0"})]),
        ModelResponse("Found it.", []),
    )
    result = _agent(tmp_path, provider).run("go", project)
    assert result.text == "Found it."
    assert result.turns == 2


def test_edit_mode_requires_confirm(tmp_path, project):
    provider = FakeProvider()
    with pytest.raises(ValueError, match="confirm"):
        _agent(tmp_path, provider).run("go", project, edit=True)


def test_system_prompt_is_loaded(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse("hi", []),
    )
    _agent(tmp_path, provider).run("go", project)
    system = provider.calls[0][0]
    assert system["role"] == "system"
    assert "Ferrum" in system["content"]


def test_memory_answer_is_nudged_into_using_tools(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("I know this from memory.", []),
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse("Read main.c: returns 0.", []),
    )
    result = _agent(tmp_path, provider).run("what does main.c do?", project)
    assert result.text == "Read main.c: returns 0."
    assert result.turns == 3
    second_turn = provider.calls[1]
    user_messages = [m for m in second_turn if m["role"] == "user"]
    assert any("not called any tools" in m["content"] for m in user_messages)


def test_nudge_happens_at_most_twice(tmp_path, project):
    provider = FakeProvider(
        ModelResponse("answer one", []),
        ModelResponse("answer two", []),
        ModelResponse("final answer", []),
    )
    result = _agent(tmp_path, provider).run("anything", project)
    assert result.text == "final answer"
    assert result.turns == 3
    final_messages = provider.calls[-1]
    nudges = [
        m
        for m in final_messages
        if m["role"] == "user" and "not called any tools" in str(m["content"])
    ]
    assert len(nudges) == 2


def test_edit_mode_loop_patches_and_verifies(tmp_path, project):
    original = (tmp_path / "main.c").read_text(encoding="utf-8")
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse(
            "",
            [
                ToolCall(
                    "2",
                    "apply_patch",
                    {
                        "path": "main.c",
                        "old": "return 0;",
                        "new": "return 1;",
                        "description": "wrong return value",
                    },
                )
            ],
        ),
        ModelResponse("Changed the return value.", []),
    )
    verifier = FakeVerifier(PASSING)
    printed = []
    agent = _agent(tmp_path, provider, verifier=verifier, printer=printed.append)
    result = agent.run(
        "make main.c return 1", project, edit=True, confirm=lambda p: True
    )

    assert (tmp_path / "main.c").read_text(encoding="utf-8") != original
    assert "return 1;" in (tmp_path / "main.c").read_text(encoding="utf-8")
    assert result.patched is True
    assert result.verified is True
    assert result.text == "Changed the return value."
    assert verifier.calls == 1
    assert any("--- a/main.c" in line for line in printed)

    tool_messages = [m for m in provider.calls[2] if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert "return 0" in tool_messages[0]["content"]  # the earlier read
    assert "--- verification ---" in tool_messages[1]["content"]
    assert "PASS" in tool_messages[1]["content"]


def test_declined_patch_is_not_applied(tmp_path, project):
    provider = FakeProvider(
        ModelResponse(
            "",
            [
                ToolCall(
                    "1",
                    "apply_patch",
                    {"path": "main.c", "old": "return 0;", "new": "return 9;"},
                )
            ],
        ),
        ModelResponse("User said no.", []),
    )
    verifier = FakeVerifier(PASSING)
    agent = _agent(tmp_path, provider, verifier=verifier)
    result = agent.run("change it", project, edit=True, confirm=lambda p: False)
    assert "return 0;" in (tmp_path / "main.c").read_text(encoding="utf-8")
    assert result.patched is False
    assert result.verified is None
    assert verifier.calls == 0
    tool_message = next(m for m in provider.calls[1] if m["role"] == "tool")
    assert "declined" in tool_message["content"].lower()


def test_dry_run_shows_diff_but_never_writes(tmp_path, project):
    original = (tmp_path / "main.c").read_text(encoding="utf-8")
    provider = FakeProvider(
        ModelResponse("", [ToolCall("1", "read_file", {"path": "main.c"})]),
        ModelResponse(
            "",
            [
                ToolCall(
                    "2",
                    "apply_patch",
                    {"path": "main.c", "old": "return 0;", "new": "return 1;"},
                )
            ],
        ),
        ModelResponse("Proposed: return 1.", []),
    )
    verifier = FakeVerifier(PASSING)
    printed = []
    agent = _agent(tmp_path, provider, verifier=verifier, printer=printed.append)

    def confirm_never(prompt):
        raise AssertionError("dry run must not ask for confirmation")

    result = agent.run(
        "change it", project, edit=True, confirm=confirm_never, dry_run=True
    )

    assert (tmp_path / "main.c").read_text(encoding="utf-8") == original
    assert result.patched is False
    assert result.verified is None
    assert verifier.calls == 0
    assert any("--- a/main.c" in line for line in printed)
    tool_messages = [m for m in provider.calls[2] if m["role"] == "tool"]
    assert "NOT applied" in tool_messages[-1]["content"]
    user_message = provider.calls[0][1]
    assert "Dry run" in user_message["content"]


def test_dry_run_tool_in_isolation(source):
    ui_lines = []
    tool = ApplyPatch(
        source,
        lambda p: True,
        ui=ui_lines.append,
        verifier=FakeVerifier(PASSING),
        dry_run=True,
    )
    result = tool.execute({"path": "main.c", "old": "return 0;", "new": "return 1;"})
    assert result.ok
    assert "return 0;" in (source / "main.c").read_text(encoding="utf-8")
    assert tool.applied is False
    assert tool.outcome is None
    assert "--- a/main.c" in "".join(ui_lines)


# --- apply_patch tool in isolation ---


@pytest.fixture
def source(tmp_path):
    (tmp_path / "main.c").write_text(
        "int main(void)\n{\n    return 0;\n}\n", encoding="utf-8"
    )
    return tmp_path


def _tool(tmp_path, confirm=lambda p: True, verifier=None, ui=None):
    return ApplyPatch(
        tmp_path,
        confirm,
        ui=ui if ui is not None else (lambda s: None),
        verifier=verifier,
    )


def test_apply_patch_writes_and_verifies(source):
    ui_lines = []
    tool = _tool(source, verifier=FakeVerifier(PASSING), ui=ui_lines.append)
    result = tool.execute(
        {
            "path": "main.c",
            "old": "return 0;",
            "new": "return 1;",
            "description": "flip it",
        }
    )
    assert result.ok
    assert "return 1;" in (source / "main.c").read_text(encoding="utf-8")
    assert tool.applied is True
    assert tool.outcome is PASSING
    assert "verification" in result.text
    assert "PASS" in result.text
    diff = "".join(ui_lines)
    assert "--- a/main.c" in diff
    assert "-    return 0;" in diff
    assert "+    return 1;" in diff


def test_apply_patch_stale_source_rejected(source):
    tool = _tool(source)
    with pytest.raises(ToolError, match="Patch rejected"):
        tool.execute({"path": "main.c", "old": "return 42;", "new": "return 1;"})


def test_apply_patch_ambiguous_source_rejected(tmp_path):
    (tmp_path / "main.c").write_text("x = 1;\nx = 1;\n", encoding="utf-8")
    tool = _tool(tmp_path)
    with pytest.raises(ToolError, match="matches 2 times"):
        tool.execute({"path": "main.c", "old": "x = 1;", "new": "x = 2;"})


def test_apply_patch_missing_file_rejected(tmp_path):
    tool = _tool(tmp_path)
    with pytest.raises(ToolError, match="file not found"):
        tool.execute({"path": "nope.c", "old": "a", "new": "b"})


def test_apply_patch_escape_rejected(source):
    tool = _tool(source)
    with pytest.raises(ToolError):
        tool.execute({"path": "../outside.c", "old": "a", "new": "b"})


def test_apply_patch_missing_arguments(source):
    tool = _tool(source)
    with pytest.raises(ToolError, match="old is required"):
        tool.execute({"path": "main.c", "new": "b"})


def test_apply_patch_failure_feeds_back_through_registry(source):
    registry = ToolRegistry()
    registry.register(_tool(source))
    result = registry.execute(
        "apply_patch", {"path": "main.c", "old": "nothing", "new": "x"}
    )
    assert result.ok is False
    assert result.to_model_message().startswith("ERROR:")


# --- git integration ---


def _git(*args, root):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=False
    )


def _in_work_tree(root) -> bool:
    import shutil as _shutil

    if _shutil.which("git") is None:
        return False
    return _git("rev-parse", "--is-inside-work-tree", root=root).returncode == 0


def test_apply_patch_reports_git_status_and_diff(source):
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    assert _git("init", "-q", root=source).returncode == 0
    assert _git("add", "main.c", root=source).returncode == 0

    ui_lines = []
    tool = _tool(source, verifier=FakeVerifier(PASSING), ui=ui_lines.append)
    result = tool.execute({"path": "main.c", "old": "return 0;", "new": "return 1;"})

    assert result.ok
    assert "--- git ---" in result.text
    assert "main.c" in result.text  # porcelain status names the file
    assert "+    return 1;" in result.text  # the real diff git computed
    # the pre-write diff still goes to the user on stdout
    assert "--- a/main.c" in "".join(ui_lines)


def test_apply_patch_omits_git_section_outside_a_repo(source):
    if _in_work_tree(source):
        pytest.skip("tmp_path sits inside a git work tree")
    tool = _tool(source, verifier=FakeVerifier(PASSING))
    result = tool.execute({"path": "main.c", "old": "return 0;", "new": "return 1;"})
    assert result.ok
    assert "--- git ---" not in result.text
    assert "--- verification ---" in result.text
