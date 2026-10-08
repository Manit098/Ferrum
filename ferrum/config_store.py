"""The config file as bytes on disk: its shape, not what the settings mean.

Pure functions only — no environment, no defaults, no knowledge of which
profile is active. ferrum.config owns those and passes a path in here, so
the file format can be tested on its own and swapped without touching the
settings that live above it.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# The vocabulary the file format is written in: keys, profiles, and the
# error both layers raise. config.py imports these rather than the other
# way round, so neither module depends on the other's behaviour.


class ConfigError(Exception):
    """The config file, a setting, or an endpoint URL cannot be used."""


INT_FIELDS = {
    "max_file_bytes": "FERRUM_MAX_FILE_BYTES",
    "max_context_bytes": "FERRUM_MAX_CONTEXT_BYTES",
    "max_files": "FERRUM_MAX_FILES",
    "max_search_results": "FERRUM_MAX_SEARCH_RESULTS",
    "max_iterations": "FERRUM_MAX_ITERATIONS",
    "max_tokens": "FERRUM_MAX_TOKENS",
    "request_timeout": "FERRUM_TIMEOUT",
    "command_timeout": "FERRUM_COMMAND_TIMEOUT",
}
STR_FIELDS = {
    "base_url": "FERRUM_BASE_URL",
    "model": "FERRUM_MODEL",
    "api_key": "FERRUM_API_KEY",
}

PROFILES = ("local", "cloud")
DEFAULT_PROFILE = "local"


def load(path: Path) -> dict[str, object]:
    """The file at ``path``, migrated from older shapes and validated."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    data = migrate(data)
    validate(data, path)
    return data


def save(path: Path, data: dict[str, object]) -> None:
    """Write the file whole; parents are created on the way."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_legacy(data: dict[str, object]) -> bool:
    """Flat files from before profiles: the string settings sat at the top."""
    return any(key in data for key in STR_FIELDS)


def migrate(data: dict[str, object]) -> dict[str, object]:
    """Fold a flat file into the profile its base_url points at."""
    if not is_legacy(data):
        return data
    url = data.get("base_url")
    profile = (
        DEFAULT_PROFILE
        if not isinstance(url, str)
        else (DEFAULT_PROFILE if is_local_endpoint(url) else PROFILES[1])
    )
    out = {key: value for key, value in data.items() if key in INT_FIELDS}
    existing = out.get(profile)
    merged: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    for key in STR_FIELDS:
        if data.get(key):
            merged[key] = data[key]
    if merged:
        out[profile] = merged
    out.setdefault("active", profile)
    return out


def validate(data: dict[str, object], path: Path) -> None:
    """Reject anything the settings layer would misread later."""
    for key, value in data.items():
        if key in INT_FIELDS:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"{path}: {key} must be an integer")
        elif key == "active":
            if value not in PROFILES:
                raise ConfigError(
                    f"{path}: active must be one of: " + ", ".join(PROFILES)
                )
        elif key in PROFILES:
            _validate_profile(value, key, path)
        else:
            raise ConfigError(f"{path}: unknown setting {key!r}")


def _validate_profile(value: object, name: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: {name} must be a JSON object")
    for key, raw in value.items():
        if key not in STR_FIELDS:
            raise ConfigError(f"{path}: unknown setting {key!r}")
        if not isinstance(raw, str):
            raise ConfigError(f"{path}: {key} must be a string")


def normalize_base_url(value: str) -> str:
    """Turn what people actually type into a usable OpenAI-style endpoint.

    ``api.openai.com`` gains a scheme and ``/v1``, ``localhost:11434`` gains
    ``http`` and ``/v1``, and a trailing slash never survives to the request.
    """
    url = value.strip()
    if not url:
        raise ConfigError("base_url must be a non-empty string")
    if "://" not in url:
        url = f"{_default_scheme(url)}://{url}"
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError(f"base_url must be an http(s) URL, got {value!r}")
    path = parts.path.rstrip("/") or "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _default_scheme(url: str) -> str:
    host = url.split("/")[0].split("@")[-1].split(":")[0]
    if host in {"", "localhost", "0", "0.0.0.0"} or host.startswith("127."):
        return "http"
    return "https"


def is_local_endpoint(base_url: str) -> bool:
    """Loopback endpoints are the only ones that routinely skip an api_key."""
    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.startswith(
        "127."
    )
