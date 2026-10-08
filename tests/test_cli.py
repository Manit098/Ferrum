"""Tests for the CLI entry point."""

import json

import pytest

from ferrum import __version__
from ferrum.cli import EXIT_INTERRUPTED, EXIT_OK, build_parser, main


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "usage: ferrum" in out


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == EXIT_OK
    assert f"ferrum {__version__}" in capsys.readouterr().out


def test_short_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-V"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bare_invocation_prints_help(capsys):
    assert main([]) == EXIT_OK
    assert "usage: ferrum" in capsys.readouterr().out


def test_inspect_current_directory(project, capsys):
    assert main(["."]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Ferrum" in out
    assert "Project: ." in out
    assert "Languages: C" in out
    assert "Build system: Make" in out
    assert "  main.c" in out
    assert "  Makefile" in out


def test_inspect_explicit_path(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main([str(proj)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Project: ./demo" in out
    assert "  main.c" in out


def test_missing_directory_is_an_error(capsys):
    code = main(["./does-not-exist"])
    assert code == 1
    assert "no such directory" in capsys.readouterr().err


def test_task_mode_requires_model(monkeypatch, capsys):
    monkeypatch.delenv("FERRUM_MODEL", raising=False)
    code = main(["find", "the", "crash"])
    assert code == 1
    assert "FERRUM_MODEL" in capsys.readouterr().err


def test_task_mode_reports_unreachable_model(monkeypatch, capsys):
    monkeypatch.setenv("FERRUM_MODEL", "test-model")
    monkeypatch.setenv("FERRUM_BASE_URL", "http://127.0.0.1:1/v1")
    code = main(["check", "this", "out"])
    assert code == 1
    err = capsys.readouterr().err
    assert "cannot reach model" in err
    assert "http://127.0.0.1:1/v1" in err


def test_fix_without_task_is_usage_error(capsys):
    assert main(["fix"]) == 1
    assert 'usage: ferrum fix "<task>"' in capsys.readouterr().err


def test_fix_mode_requires_model(monkeypatch, capsys):
    monkeypatch.delenv("FERRUM_MODEL", raising=False)
    assert main(["fix", "the", "crash"]) == 1
    err = capsys.readouterr().err
    assert "FERRUM_MODEL" in err


def test_confirm_answers(monkeypatch):
    from ferrum.commands.task import confirm as _confirm

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert _confirm("Apply patch? [y/N] ") is True
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert _confirm("Apply patch? [y/N] ") is False

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert _confirm("Apply patch? [y/N] ") is False


def test_file_target_rejected(project, capsys):
    code = main(["main.c"])
    assert code == 1
    assert "not a directory" in capsys.readouterr().err


def test_unknown_flag_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--bogus"])
    assert exc.value.code == 2


def test_parser_prog_name():
    assert build_parser().prog == "ferrum"


def test_help_output_is_never_colored(monkeypatch, capsys):
    # Even if the environment asks for color, ferrum stays plain text.
    monkeypatch.setenv("PYTHON_COLORS", "1")
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "usage: ferrum" in out
    assert "\x1b" not in out


def test_verbose_flag_accepted(capsys):
    assert main(["-v"]) == EXIT_OK
    assert "usage: ferrum" in capsys.readouterr().out


def test_config_show_defaults(capsys):
    assert main(["config"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "base_url  = http://localhost:11434/v1  [default]" in out
    assert "model     = (not set)  [default]" in out
    assert "config file:" in out


def test_config_show_reports_sources(isolated_config, monkeypatch, capsys):
    monkeypatch.setenv("FERRUM_BASE_URL", "https://cloud/v1")
    main(["config", "set", "model", "m-file"])
    capsys.readouterr()
    main(["config"])
    out = capsys.readouterr().out
    assert "[env FERRUM_BASE_URL]" in out
    assert "model     = m-file  [file]" in out


def test_config_set_get_roundtrip(isolated_config, capsys):
    assert main(["config", "set", "model", "my-coder"]) == EXIT_OK
    assert main(["config", "get", "model"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "model = my-coder" in out
    assert json.loads(isolated_config.read_text(encoding="utf-8")) == {
        "local": {"model": "my-coder"}
    }


def test_config_set_multiword_value(isolated_config, capsys):
    assert main(["config", "set", "model", "deepseek", "v4", "flash"]) == EXIT_OK
    assert main(["config", "get", "model"]) == EXIT_OK
    assert "deepseek v4 flash" in capsys.readouterr().out


def test_config_set_api_key_is_masked(isolated_config, capsys):
    assert main(["config", "set", "api_key", "sk-secretvalue1234"]) == EXIT_OK
    assert main(["config", "get", "api_key"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "sk-secretvalue1234" not in out.split("saved to")[0]
    assert out.strip().splitlines()[-1] == "sk-secretvalue1234"
    assert "sk-sec...1234" in out


def test_config_get_unset_exits_error(capsys):
    assert main(["config", "get", "model"]) == 1
    assert "not set" in capsys.readouterr().err


def test_config_get_unknown_key(capsys):
    assert main(["config", "get", "bogus"]) == 1
    assert "unknown setting" in capsys.readouterr().err


def test_config_set_missing_value(capsys):
    assert main(["config", "set", "model"]) == 1
    assert "usage: ferrum config set" in capsys.readouterr().err


def test_config_set_clears_with_empty(isolated_config, capsys):
    main(["config", "set", "model", "m"])
    capsys.readouterr()
    assert main(["config", "set", "model", ""]) == EXIT_OK
    assert "cleared" in capsys.readouterr().out
    assert main(["config", "get", "model"]) == 1


def test_config_set_warns_when_env_overrides(monkeypatch, capsys):
    monkeypatch.setenv("FERRUM_MODEL", "env-wins")
    assert main(["config", "set", "model", "file-loses"]) == EXIT_OK
    assert "overrides the file" in capsys.readouterr().err


def test_config_unknown_subcommand(capsys):
    assert main(["config", "frobnicate"]) == 1
    err = capsys.readouterr().err
    assert "unknown config command" in err
    assert "ferrum config models" in err


def test_config_models_lists(capsys, monkeypatch):
    monkeypatch.setattr(
        "ferrum.commands.config.list_models",
        lambda url, timeout=5, api_key="": ["model-a", "model-b"],
    )
    assert main(["config", "models"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "model-a" in out and "model-b" in out


def test_config_models_empty_is_error(capsys, monkeypatch):
    monkeypatch.setattr(
        "ferrum.commands.config.list_models",
        lambda url, timeout=5, api_key="": [],
    )
    assert main(["config", "models"]) == 1
    err = capsys.readouterr().err
    assert "no models listed" in err


def test_config_local_preset(isolated_config, capsys):
    main(["config", "set", "api_key", "k"])
    capsys.readouterr()
    assert main(["config", "local"]) == EXIT_OK
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["active"] == "local"
    assert data["local"]["base_url"] == "http://localhost:11434/v1"
    assert data["local"]["api_key"] == "k"  # kept, so switching back is one command
    assert "ferrum config models" in capsys.readouterr().err


def test_config_cloud_preset_warns_without_key(isolated_config, capsys):
    assert main(["config", "cloud"]) == EXIT_OK
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["active"] == "cloud"
    assert data["cloud"]["base_url"] == "https://api.routeway.ai/v1"
    err = capsys.readouterr().err
    assert "api_key is not set" in err


def test_config_cloud_custom_url(isolated_config):
    assert main(["config", "cloud", "https://other.example/v1"]) == EXIT_OK
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["cloud"]["base_url"] == "https://other.example/v1"
    assert data["active"] == "cloud"


def test_config_command_beats_directory_named_config(project, capsys):
    (project / "config").mkdir()
    assert main(["config"]) == EXIT_OK
    assert "base_url" in capsys.readouterr().out
    assert main(["./config"]) == EXIT_OK
    assert "Project:" in capsys.readouterr().out


def test_dry_run_without_fix_is_usage_error(capsys):
    assert main(["--dry-run", ".", "extra"]) == 1
    assert "only applies to: ferrum fix" in capsys.readouterr().err


def test_dry_run_bare_is_usage_error(capsys):
    assert main(["--dry-run"]) == 1
    assert "ferrum fix --dry-run" in capsys.readouterr().err


def test_doctor_ready_when_model_and_endpoint_work(monkeypatch, capsys):
    from ferrum.config import write_config_value

    write_config_value("model", "m1")
    monkeypatch.setattr(
        "ferrum.commands.doctor.probe_endpoint",
        lambda url, timeout=5, api_key="": (["m1", "m2"], None),
    )
    assert main(["doctor"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ok — 2 models listed" in out
    assert "verdict: ready" in out


def test_doctor_not_ready_without_model(monkeypatch, capsys):
    monkeypatch.setattr(
        "ferrum.commands.doctor.probe_endpoint",
        lambda url, timeout=5, api_key="": ([], None),
    )
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "model is not set" in out
    assert "verdict: not ready" in out


def test_doctor_reports_unreachable_endpoint(monkeypatch, capsys):
    from ferrum.config import write_config_value

    write_config_value("model", "m1")
    monkeypatch.setattr(
        "ferrum.commands.doctor.probe_endpoint",
        lambda url, timeout=5, api_key="": ([], "HTTP 502"),
    )
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "unreachable — HTTP 502" in out
    assert "verdict: not ready" in out


def test_doctor_survives_broken_config_file(monkeypatch, capsys, isolated_config):
    isolated_config.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        "ferrum.commands.doctor.probe_endpoint",
        lambda url, timeout=5, api_key="": ([], None),
    )
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out
    assert "toolchain" in out


def test_doctor_with_extra_args_is_usage_error(capsys):
    assert main(["doctor", "now"]) == 1
    assert "usage: ferrum doctor" in capsys.readouterr().err


def test_interrupt_is_exit_130_without_traceback(monkeypatch, capsys):
    monkeypatch.setenv("FERRUM_MODEL", "test-model")

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("ferrum.commands.task.discover", boom)
    assert main(["check", "the", "build"]) == EXIT_INTERRUPTED
    captured = capsys.readouterr()
    assert "interrupted" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_status_lines_go_to_stderr(project, capsys):
    assert main([str(project)]) == EXIT_OK
    captured = capsys.readouterr()
    assert "reading project" in captured.err
    assert "reading project" not in captured.out
    assert "Files:" in captured.out  # the summary stays on stdout


def test_config_show_lists_both_profiles(isolated_config, capsys):
    assert main(["config", "set", "model", "local-model"]) == EXIT_OK
    assert main(["config", "cloud"]) == EXIT_OK
    capsys.readouterr()
    assert main(["config"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "active   cloud" in out
    assert "local-model" in out
    assert "http://localhost:11434/v1" in out
    assert "https://api.routeway.ai/v1" in out
    assert "[file]" in out
    assert "[default]" in out


def test_config_setup_needs_a_terminal(capsys, monkeypatch):
    monkeypatch.setattr("ferrum.commands.setup._interactive", lambda: False)
    assert main(["config", "setup", "cloud"]) == 1
    err = capsys.readouterr().err
    assert "needs a terminal" in err
    assert "ferrum config set model" in err


def test_config_setup_rejects_unknown_profile(capsys):
    assert main(["config", "setup", "staging"]) == 1
    assert "unknown profile" in capsys.readouterr().err
