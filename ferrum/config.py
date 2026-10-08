"""Harness settings: two model profiles plus the limits that keep Ferrum small.

One file holds both ways of running Ferrum — a local Ollama server and a
cloud endpoint. Each profile has its own base_url, model, and api_key, and
one ``active`` profile says which is in use. Local models need no api_key;
cloud ones need base_url, model, and api_key.

Settings resolve in layers: environment variable > config file > default.
The file lives outside the project (Windows: %APPDATA%\\ferrum\\config.json,
else $XDG_CONFIG_HOME/ferrum/config.json), so switching between local and
cloud is one ``ferrum config`` command away.

Shape of the file:

    {
      "active": "cloud",
      "local": {"base_url": "http://localhost:11434/v1", "model": "qwen3:4b"},
      "cloud": {"base_url": "https://api.example/v1",
                "model": "some-model", "api_key": "sk-..."},
      "max_tokens": 2048
    }

Older flat files (base_url / model / api_key at the top) are read as the
profile their base_url points at and rewritten in the new shape on the next
``ferrum config`` command. The file format itself lives in
ferrum.config_store; this module is the layer that decides what a setting
actually means.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from ferrum.config_store import (
    DEFAULT_PROFILE,
    INT_FIELDS,
    PROFILES,
    STR_FIELDS,
    ConfigError,
    is_local_endpoint,
    normalize_base_url,
)
from ferrum.config_store import (
    load as load_file,
)
from ferrum.config_store import (
    save as save_file,
)

# Explicit public surface: the names other modules are meant to import.
# (mypy's no_implicit_reexport only honours re-exports listed here.)
__all__ = [
    "CLOUD_PRESET_URL",
    "DEFAULT_PROFILE",
    "INT_FIELDS",
    "LOCAL_PRESET_URL",
    "PROFILES",
    "PROFILE_ENV",
    "STR_FIELDS",
    "Config",
    "ConfigError",
    "config_file_path",
    "host_of",
    "is_local_endpoint",
    "load_config",
    "mask_secret",
    "normalize_base_url",
    "profile_values",
    "read_config_file",
    "setting_source",
    "write_config_value",
    "write_config_values",
]

LOCAL_PRESET_URL = "http://localhost:11434/v1"
CLOUD_PRESET_URL = "https://api.routeway.ai/v1"

PROFILE_ENV = "FERRUM_PROFILE"


def config_file_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ferrum" / "config.json"


def read_config_file() -> dict[str, object]:
    """The config file as the rest of the package sees it: profile-shaped."""
    return load_file(config_file_path())


def _active(data: dict[str, object]) -> str:
    name = data.get("active")
    return name if isinstance(name, str) and name in PROFILES else DEFAULT_PROFILE


def profile_values(profile: str | None = None) -> dict[str, str]:
    """String settings of one profile (default: the active one)."""
    data = read_config_file()
    name = profile or _active(data)
    if name not in PROFILES:
        raise ConfigError(
            f"unknown profile {name!r}; choose from: " + ", ".join(PROFILES)
        )
    values = data.get(name)
    stored = values if isinstance(values, dict) else {}
    return {
        key: value
        for key, value in stored.items()
        if key in STR_FIELDS and isinstance(value, str)
    }


# -- writing --------------------------------------------------------------


def write_config_values(values: dict[str, str], *, profile: str | None = None) -> Path:
    """Merge settings into the config file; an empty value removes the key.

    String settings land in ``profile`` (or the profile this call switches
    to, or the active one), integer limits are file-wide, and an ``active``
    key moves the switch.
    """
    _check_keys(values)
    data = read_config_file()
    target = _target_profile(data, values, profile)
    stored = data.get(target)
    settings = dict(stored) if isinstance(stored, dict) else {}

    for key, value in values.items():
        if key == "active":
            data["active"] = _profile_name(value)
        elif key in INT_FIELDS:
            _set_int(data, key, value)
        else:
            _set_string(settings, key, value)

    if settings:
        data[target] = settings
    else:
        data.pop(target, None)
    path = config_file_path()
    save_file(path, data)
    return path


def write_config_value(key: str, value: str, *, profile: str | None = None) -> Path:
    return write_config_values({key: value}, profile=profile)


def _check_keys(values: dict[str, str]) -> None:
    known = {*STR_FIELDS, *INT_FIELDS, "active"}
    for key in values:
        if key not in known:
            raise ConfigError(
                f"unknown setting {key!r}; choose from: " + ", ".join(sorted(known))
            )


def _target_profile(
    data: dict[str, object], values: dict[str, str], profile: str | None
) -> str:
    name = profile or values.get("active") or _active(data)
    return _profile_name(name)


def _profile_name(name: object) -> str:
    if name not in PROFILES:
        raise ConfigError(
            f"unknown profile {name!r}; choose from: " + ", ".join(PROFILES)
        )
    return str(name)


def _set_int(data: dict[str, object], key: str, value: str) -> None:
    if value == "":
        data.pop(key, None)
        return
    try:
        data[key] = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {value!r}") from exc


def _set_string(settings: dict[str, str], key: str, value: str) -> None:
    if value == "":
        settings.pop(key, None)
    else:
        settings[key] = value


# -- secrets and hosts ----------------------------------------------------


def mask_secret(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:6] + "..." + value[-4:]


def host_of(base_url: str) -> str:
    """The part a person recognises in a status line: host[:port]."""
    return urlsplit(base_url).netloc or base_url


def _default_base_url(profile: str) -> str:
    return CLOUD_PRESET_URL if profile == "cloud" else LOCAL_PRESET_URL


# -- the settings object --------------------------------------------------


@dataclass(frozen=True)
class Config:
    max_file_bytes: int = 256 * 1024  # one file
    max_context_bytes: int = 400_000  # whole listing
    max_files: int = 500
    max_search_results: int = 200
    max_iterations: int = 25  # agent loop cap
    max_tokens: int = 2048  # per model response
    request_timeout: int = 120  # per model request (CPU-served models are slow)
    command_timeout: int = 300  # per build/test command

    profile: str = DEFAULT_PROFILE  # which of the two profiles is in use

    base_url: str = LOCAL_PRESET_URL
    model: str = ""
    api_key: str = ""

    def __post_init__(self) -> None:
        for name in INT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if self.profile not in PROFILES:
            raise ConfigError("profile must be one of: " + ", ".join(PROFILES))
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ConfigError(
                f"base_url must be a non-empty string, got {self.base_url!r}"
            )
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigError(f"base_url must be an http(s) URL, got {self.base_url!r}")

    def missing(self) -> list[str]:
        """Env/file keys that must be set before the model can be used."""
        missing = []
        if not self.model:
            missing.append(STR_FIELDS["model"])
        return missing

    @property
    def local(self) -> bool:
        return is_local_endpoint(self.base_url)


def setting_source(name: str, profile: str | None = None) -> str:
    """Where a setting's effective value comes from: env, file, or default."""
    var = STR_FIELDS.get(name) or INT_FIELDS.get(name)
    data = read_config_file()
    active = _active(data)
    profile = profile or active
    # Env vars describe the profile actually in use, never a bystander.
    if var and os.environ.get(var) and (name in INT_FIELDS or profile == active):
        return f"env {var}"
    if name in INT_FIELDS:
        return "file" if name in data else "default"
    values = data.get(profile, {})
    if isinstance(values, dict) and name in values:
        return "file"
    return "default"


# -- resolution -----------------------------------------------------------


def load_config(**overrides: object) -> Config:
    """One Config from file, environment, and the overrides passed in."""
    data = read_config_file()
    values: dict[str, object] = dict(_file_limits(data))

    requested = overrides.pop("profile", None)
    profile = _resolve_profile(data, requested)
    values.update(_file_profile(data, profile))
    values.update(_env_values())
    values["profile"] = profile
    values.update(overrides)
    if values.get("base_url"):
        # Env and hand-edited files get the same treatment as `config set`.
        values["base_url"] = normalize_base_url(str(values["base_url"]))
    return _build(values)


def _file_limits(data: dict[str, object]) -> dict[str, object]:
    path = config_file_path()
    limits: dict[str, object] = {}
    for name in INT_FIELDS:
        if name in data:
            raw = data[name]
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise ConfigError(f"{path}: {name} must be an integer")
            limits[name] = raw
    return limits


def _resolve_profile(data: dict[str, object], requested: object) -> str:
    profile = requested or os.environ.get(PROFILE_ENV) or _active(data)
    if profile not in PROFILES:
        raise ConfigError(
            f"unknown profile {profile!r}; choose from: " + ", ".join(PROFILES)
        )
    return str(profile)


def _file_profile(data: dict[str, object], profile: str) -> dict[str, object]:
    stored = data.get(profile, {})
    values: dict[str, object] = {}
    if isinstance(stored, dict):
        for name, raw in stored.items():
            if name in STR_FIELDS and raw:
                values[name] = raw
    values.setdefault("base_url", _default_base_url(profile))
    return values


def _env_values() -> dict[str, object]:
    values: dict[str, object] = {}
    for name, var in STR_FIELDS.items():
        raw = os.environ.get(var)
        if raw:
            values[name] = raw
    for name, var in INT_FIELDS.items():
        raw = os.environ.get(var)
        if raw:
            try:
                values[name] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{var} must be an integer, got {raw!r}") from exc
    return values


def _build(values: dict[str, object]) -> Config:
    try:
        # The keys already come from the schema vocabulary; the constructor
        # validates every value, so the loose dict is intentional here.
        return Config(**cast(dict[str, Any], values))
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc
