"""Tests for the verifier: detection, honest command runs, outcomes."""

import shutil
import sys

import pytest

from ferrum.verifier import (
    CommandResult,
    Verifier,
    VerifyOutcome,
    run_command,
)


def test_run_command_success(tmp_path):
    result = run_command([sys.executable, "-c", "print('hi')"], tmp_path, timeout=30)
    assert result.ok
    assert result.returncode == 0
    assert "hi" in result.stdout
    assert result.render().startswith("$ ")


def test_run_command_failure_captures_stderr(tmp_path):
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
        tmp_path,
        timeout=30,
    )
    assert not result.ok
    assert result.returncode == 3
    assert "bad" in result.stderr
    assert "[exit 3]" in result.render()


def test_run_command_missing(tmp_path):
    result = run_command(["definitely-not-a-real-tool-xyz"], tmp_path, timeout=5)
    assert not result.ok
    assert result.returncode == 127
    assert "command not found" in result.stderr


def test_output_is_truncated():
    big = "x" * 20000
    result = CommandResult(("t",), 1, stdout=big)
    assert len(result.output) < 9000
    assert "truncated" in result.output


def test_detect_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    label, builds, tests = Verifier(tmp_path).detect()
    assert label == "cargo"
    assert builds == [["cargo", "build"]]
    assert tests == [["cargo", "test"]]


def test_detect_cmake_beats_make(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("", encoding="utf-8")
    (tmp_path / "Makefile").write_text("", encoding="utf-8")
    label, builds, _ = Verifier(tmp_path).detect()
    assert label == "cmake"
    assert builds[0] == ["cmake", "-S", ".", "-B", "build"]


def test_detect_make_when_available(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    monkeypatch.setattr(
        "ferrum.verifier.shutil.which", lambda name: "/usr/bin/make" if name == "make" else None
    )
    label, builds, tests = Verifier(tmp_path).detect()
    assert label == "make"
    assert builds == [["make"]]
    assert tests == [["make", "test"]]


def test_detect_clang_fallback_when_make_missing(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    (tmp_path / "util.c").write_text("int util(void){return 0;}\n", encoding="utf-8")
    include = tmp_path / "include"
    include.mkdir()
    (include / "util.h").write_text("int util(void);\n", encoding="utf-8")

    def fake_which(name):
        if name == "clang":
            return "C:/clang.exe"
        return None

    monkeypatch.setattr("ferrum.verifier.shutil.which", fake_which)
    label, builds, tests = Verifier(tmp_path).detect()
    assert label == "clang (direct)"
    assert len(builds) == 1
    cmd = builds[0]
    assert cmd[0] == "clang"
    assert "-Iinclude" in cmd
    assert "main.c" in cmd and "util.c" in cmd
    assert "-o" in cmd
    assert tests == []


def test_detect_clang_reports_missing_compiler(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    monkeypatch.setattr("ferrum.verifier.shutil.which", lambda name: None)
    label, builds, _ = Verifier(tmp_path).detect()
    assert label == "clang (direct)"
    # First candidate is kept so the run fails honestly with "not found".
    assert builds[0][0] == "clang"


def test_detect_python_with_pytest(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("ferrum.verifier.importlib.util.find_spec", lambda name: object())
    label, builds, tests = Verifier(tmp_path).detect()
    assert label == "python"
    assert builds == []
    assert tests == [[sys.executable, "-m", "pytest", "-q"]]


def test_detect_python_without_pytest(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("ferrum.verifier.importlib.util.find_spec", lambda name: None)
    label, _builds, tests = Verifier(tmp_path).detect()
    assert label == "python"
    assert tests == []


def test_detect_none(tmp_path):
    label, builds, tests = Verifier(tmp_path).detect()
    assert (label, builds, tests) == ("none", [], [])


def test_verify_with_fake_runner_pass(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        return CommandResult(tuple(cmd), 0)

    outcome = Verifier(tmp_path, runner=runner).verify()
    assert outcome.verified is True
    assert outcome.label == "cargo"
    assert len(calls) == 2
    assert "PASS" in outcome.report()


def test_verify_stops_after_build_failure(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        return CommandResult(tuple(cmd), 1, stderr="error: compile failed")

    outcome = Verifier(tmp_path, runner=runner).verify()
    assert outcome.verified is False
    assert len(calls) == 1  # tests never ran
    assert "FAIL" in outcome.report()
    assert "compile failed" in outcome.report()


def test_verify_pytest_exit_5_counts_as_no_tests(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("ferrum.verifier.importlib.util.find_spec", lambda name: object())

    def runner(cmd, cwd, timeout):
        return CommandResult(tuple(cmd), 5, stdout="no tests ran")

    outcome = Verifier(tmp_path, runner=runner).verify()
    assert outcome.tests == ()
    assert outcome.verified is False  # nothing actually ran


def test_verify_without_build_system_is_honest(tmp_path):
    outcome = Verifier(tmp_path).verify()
    assert outcome.label == "none"
    assert outcome.verified is False
    assert "SKIPPED" in outcome.report()


def test_outcome_without_commands_is_not_verified():
    assert VerifyOutcome(label="none").verified is False


def test_clang_fallback_real_compile(tmp_path):
    if not shutil.which("clang"):
        pytest.skip("clang not installed")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        '#include <stdio.h>\nint main(void){printf("ok\\n");return 0;}\n',
        encoding="utf-8",
    )
    outcome = Verifier(tmp_path, timeout=60).verify()
    assert outcome.verified is True
    assert outcome.label == "clang (direct)"
    assert "clang" in outcome.builds[0].cmd[0]


def test_clang_fallback_real_compile_failure(tmp_path):
    if not shutil.which("clang"):
        pytest.skip("clang not installed")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "main.c").write_text("int main(void){ this is broken }\n", encoding="utf-8")
    outcome = Verifier(tmp_path, timeout=60).verify()
    assert outcome.verified is False
    assert "FAIL" in outcome.report()


def test_example_c_project_verifies():
    if not shutil.which("clang"):
        pytest.skip("clang not installed")
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "examples" / "c_project"
    outcome = Verifier(root, timeout=60).verify()
    assert outcome.label == "clang (direct)"
    assert outcome.verified is True
