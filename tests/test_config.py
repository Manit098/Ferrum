"""Tests for configuration: limits, endpoint, environment, config file."""

import dataclasses
import json

import pytest

from ferrum.config import (
    Config,
    ConfigError,
    load_config,
    mask_secret,
    setting_source,
    write_config_value,
    write_config_values,
)


def test_defaults_are_sane():
    cfg = Config()
    assert cfg.max_file_bytes == 256 * 1024
    assert cfg.max_context_bytes == 400_000
    assert cfg.max_files == 500
    assert cfg.max_search_results == 200
    assert cfg.max_iterations == 25
    assert cfg.max_tokens == 2048
    assert cfg.request_timeout == 120
    assert cfg.command_timeout == 300
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.model == ""
    assert cfg.api_key == ""


def test_custom_values():
    cfg = Config(max_file_bytes=10, max_files=3)
    assert cfg.max_file_bytes == 10
    assert cfg.max_files == 3


def test_config_is_immutable():
    cfg = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_files = 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": 0},
        {"max_context_bytes": -1},
        {"max_files": 0},
        {"max_search_results": "many"},
        {"max_iterations": 0},
        {"request_timeout": -5},
        {"command_timeout": 0},
        {"base_url": ""},
        {"base_url": "  "},
    ],
)
def test_invalid_values_raise(kwargs):
    with pytest.raises(ConfigError):
        Config(**kwargs)


def test_load_config_defaults():
    assert load_config() == Config()


def test_load_config_overrides():
    cfg = load_config(max_files=7)
    assert cfg.max_files == 7


def test_load_config_unknown_key():
    with pytest.raises(ConfigError):
        load_config(bogus=1)


def test_env_model_and_endpoint(monkeypatch):
    monkeypatch.setenv("FERRUM_MODEL", "my-coder")
    monkeypatch.setenv("FERRUM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("FERRUM_API_KEY", "k")
    cfg = load_config()
    assert cfg.model == "my-coder"
    assert cfg.base_url == "http://127.0.0.1:9999/v1"
    assert cfg.api_key == "k"


def test_env_integer_fields(monkeypatch):
    monkeypatch.setenv("FERRUM_MAX_ITERATIONS", "5")
    monkeypatch.setenv("FERRUM_MAX_TOKENS", "512")
    monkeypatch.setenv("FERRUM_TIMEOUT", "10")
    monkeypatch.setenv("FERRUM_COMMAND_TIMEOUT", "60")
    cfg = load_config()
    assert cfg.max_iterations == 5
    assert cfg.max_tokens == 512
    assert cfg.request_timeout == 10
    assert cfg.command_timeout == 60


def test_env_bad_integer_raises(monkeypatch):
    monkeypatch.setenv("FERRUM_MAX_ITERATIONS", "lots")
    with pytest.raises(ConfigError, match="FERRUM_MAX_ITERATIONS"):
        load_config()


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("FERRUM_MAX_FILES", "100")
    assert load_config(max_files=3).max_files == 3


def test_missing_reports_model(monkeypatch):
    assert Config().missing() == ["FERRUM_MODEL"]
    assert Config(model="x").missing() == []


def test_file_roundtrip(isolated_config):
    write_config_value("model", "file-model")
    assert load_config().model == "file-model"
    assert isolated_config.exists()
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data == {"local": {"model": "file-model"}}


def test_env_beats_file(isolated_config, monkeypatch):
    write_config_value("model", "file-model")
    monkeypatch.setenv("FERRUM_MODEL", "env-model")
    assert load_config().model == "env-model"


def test_empty_value_clears_file_key(isolated_config):
    write_config_value("model", "file-model")
    write_config_value("model", "")
    assert load_config().model == ""
    assert "model" not in json.loads(isolated_config.read_text(encoding="utf-8"))


def test_write_values_merges_keys(isolated_config):
    write_config_value("model", "m1")
    write_config_value("base_url", "https://example/v1")
    assert load_config().model == "m1"
    assert load_config().base_url == "https://example/v1"


def test_write_int_coerces_to_integer(isolated_config):
    write_config_value("max_tokens", "512")
    assert load_config().max_tokens == 512


def test_write_unknown_key_raises():
    with pytest.raises(ConfigError, match="unknown setting"):
        write_config_value("bogus", "x")


def test_write_bad_int_raises():
    with pytest.raises(ConfigError, match="integer"):
        write_config_value("max_tokens", "lots")


def test_corrupt_file_raises(isolated_config):
    isolated_config.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_file_unknown_setting_raises(isolated_config):
    isolated_config.write_text('{"bogus": 1}', encoding="utf-8")
    with pytest.raises(ConfigError, match="bogus"):
        load_config()


def test_file_wrong_type_raises(isolated_config):
    isolated_config.write_text('{"model": 42}', encoding="utf-8")
    with pytest.raises(ConfigError, match="model"):
        load_config()


def test_setting_sources(isolated_config, monkeypatch):
    assert setting_source("model") == "default"
    write_config_value("model", "m")
    assert setting_source("model") == "file"
    monkeypatch.setenv("FERRUM_MODEL", "m-env")
    assert setting_source("model") == "env FERRUM_MODEL"


def test_mask_secret():
    assert mask_secret("") == "(not set)"
    assert mask_secret("short12") == "*******"  # 7 chars, all masked
    masked = mask_secret("sk-abcdefghijkl1234")
    assert masked.startswith("sk-abc")
    assert masked.endswith("1234")
    assert "abcdefghijkl" not in masked


# -- two profiles --------------------------------------------------------


def _write_both_profiles():
    write_config_values(
        {"base_url": "http://localhost:11434/v1", "model": "qwen3:4b"},
        profile="local",
    )
    write_config_values(
        {
            "active": "cloud",
            "base_url": "https://api.routeway.ai/v1",
            "model": "deepseek-v4-flash:free",
            "api_key": "sk-secret",
        },
        profile="cloud",
    )


def test_profiles_hold_separate_settings(isolated_config):
    _write_both_profiles()
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["active"] == "cloud"
    assert data["local"] == {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:4b",
    }
    assert data["cloud"]["api_key"] == "sk-secret"
    assert "api_key" not in data["local"]  # Ollama never wants one

    active = load_config()
    assert (active.profile, active.model, active.api_key) == (
        "cloud",
        "deepseek-v4-flash:free",
        "sk-secret",
    )
    local = load_config(profile="local")
    assert (local.profile, local.model, local.api_key) == (
        "local",
        "qwen3:4b",
        "",
    )


def test_write_keeps_the_other_profile_untouched(isolated_config):
    _write_both_profiles()
    write_config_value("model", "llama3.2:1b", profile="local")
    assert load_config().model == "deepseek-v4-flash:free"  # active
    assert load_config(profile="local").model == "llama3.2:1b"
    assert load_config(profile="cloud").api_key == "sk-secret"


def test_profile_env_selects_for_this_run(isolated_config, monkeypatch):
    write_config_value("model", "m-local", profile="local")
    write_config_value("model", "m-cloud", profile="cloud")
    monkeypatch.setenv("FERRUM_PROFILE", "cloud")
    assert load_config().model == "m-cloud"
    monkeypatch.setenv("FERRUM_PROFILE", "local")
    assert load_config().model == "m-local"


def test_unknown_profile_env_raises(isolated_config, monkeypatch):
    monkeypatch.setenv("FERRUM_PROFILE", "staging")
    with pytest.raises(ConfigError, match="unknown profile"):
        load_config()


def test_int_limits_are_shared_by_both_profiles(isolated_config):
    write_config_value("max_tokens", "77", profile="local")
    assert load_config(profile="cloud").max_tokens == 77
    assert load_config().max_tokens == 77


def test_legacy_flat_file_reads_as_local(isolated_config):
    isolated_config.write_text(
        json.dumps(
            {
                "base_url": "http://localhost:11434/v1",
                "model": "old-model",
                "api_key": "sk-old",
            }
        ),
        encoding="utf-8",
    )
    config = load_config()
    assert (config.profile, config.model, config.api_key) == (
        "local",
        "old-model",
        "sk-old",
    )


def test_legacy_flat_file_with_cloud_url_reads_as_cloud(isolated_config):
    isolated_config.write_text(
        json.dumps({"base_url": "https://api.routeway.ai/v1", "model": "cloud-model"}),
        encoding="utf-8",
    )
    config = load_config()
    assert config.profile == "cloud"
    assert config.model == "cloud-model"


def test_legacy_file_is_rewritten_in_profile_shape(isolated_config):
    isolated_config.write_text(
        json.dumps(
            {
                "base_url": "https://api.routeway.ai/v1",
                "model": "cloud-model",
                "api_key": "sk-old",
                "max_files": 123,
            }
        ),
        encoding="utf-8",
    )
    write_config_value("max_tokens", "100")  # any config write migrates the file
    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert "model" not in data  # nothing flat left at the top
    assert data["active"] == "cloud"
    assert data["cloud"]["model"] == "cloud-model"
    assert data["cloud"]["api_key"] == "sk-old"
    assert data["max_tokens"] == 100  # limits stay file-wide
    assert data["max_files"] == 123


def test_setting_source_is_per_profile(isolated_config, monkeypatch):
    write_config_value("model", "m-file", profile="local")
    write_config_value("model", "m-cloud", profile="cloud")
    assert setting_source("model", "local") == "file"
    monkeypatch.setenv("FERRUM_MODEL", "m-env")
    assert setting_source("model") == "env FERRUM_MODEL"
    # A bystander profile never claims the environment's value.
    assert setting_source("model", "cloud") == "file"
    write_config_values({"active": "cloud"})
    assert setting_source("model") == "env FERRUM_MODEL"


def test_write_rejects_unknown_profile(isolated_config):
    with pytest.raises(ConfigError, match="unknown profile"):
        write_config_value("model", "m", profile="staging")
