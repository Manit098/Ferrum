"""`ferrum config`: show settings, switch profiles, hand setup its own module."""

from __future__ import annotations

import os
import sys

from ferrum.commands import EXIT_ERROR, EXIT_OK
from ferrum.commands import setup as setup_command
from ferrum.config import (
    CLOUD_PRESET_URL,
    INT_FIELDS,
    LOCAL_PRESET_URL,
    PROFILES,
    STR_FIELDS,
    Config,
    ConfigError,
    config_file_path,
    load_config,
    mask_secret,
    profile_values,
    setting_source,
    write_config_value,
    write_config_values,
)
from ferrum.model import ProviderError, list_models
from ferrum.ui import Console

CONFIG_USAGE = """usage:
  ferrum config                     show both profiles and the active one
  ferrum config setup [profile]     enter endpoint, api_key, and model
  ferrum config get <key>           print one setting (active profile)
  ferrum config set <key> <value>   save one setting ('' clears it)
  ferrum config models              list models at the active endpoint
  ferrum config local               use Ollama at http://localhost:11434/v1
  ferrum config cloud [url]         use the cloud endpoint
keys: base_url, model, api_key, """ + ", ".join(sorted(INT_FIELDS))


def run(words: list[str], console: Console) -> int:
    """One `ferrum config ...` invocation."""
    if not words:
        return show()
    cmd, rest = words[0], words[1:]
    if cmd == "setup":
        if len(rest) > 1:
            print("error: usage: ferrum config setup [local|cloud]", file=sys.stderr)
            return EXIT_ERROR
        return setup_command.run(console, rest[0] if rest else None)
    if cmd == "get":
        if len(rest) != 1:
            print("error: usage: ferrum config get <key>", file=sys.stderr)
            return EXIT_ERROR
        return get(rest[0])
    if cmd == "set":
        if len(rest) < 2:
            print("error: usage: ferrum config set <key> <value>", file=sys.stderr)
            return EXIT_ERROR
        return set_value(rest[0], " ".join(rest[1:]))
    if cmd == "models":
        return models()
    if cmd in {"local", "cloud"}:
        return preset(cmd, rest)
    print(f"error: unknown config command: {cmd}", file=sys.stderr)
    print(CONFIG_USAGE, file=sys.stderr)
    return EXIT_ERROR


# -- read -----------------------------------------------------------------


def show() -> int:
    try:
        config = load_config()  # the active profile, env overrides included
        path = config_file_path()
        stored = {name: profile_values(name) for name in PROFILES}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    active = config.profile
    print(f"active   {active}")
    for name in PROFILES:
        for index, key in enumerate(("base_url", "model", "api_key")):
            value, source = _displayed(config, stored, name, key, active)
            display = mask_secret(value) if key == "api_key" else (value or "(not set)")
            label = name if index == 0 else ""
            print(f"{label:<9}{key:<9} = {display}  [{source}]")
    suffix = "" if path.exists() else " (not created yet)"
    print(f"config file: {path}{suffix}")
    return EXIT_OK


def _displayed(
    config: Config,
    stored: dict[str, dict[str, str]],
    name: str,
    key: str,
    active: str,
) -> tuple[str, str]:
    """Value and source for one row: the active profile resolves, others show."""
    if name == active:
        value = getattr(config, key)
        return value, setting_source(key, name)
    value = stored[name].get(key, "")
    if not value and key == "base_url":
        preset_url = CLOUD_PRESET_URL if name == "cloud" else LOCAL_PRESET_URL
        return preset_url, "default"
    return value, "file" if value else "default"


def get(key: str) -> int:
    known = {**STR_FIELDS, **INT_FIELDS}
    if key not in known:
        print(
            f"error: unknown setting {key!r}; choose from: " + ", ".join(sorted(known)),
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        value = getattr(load_config(), key)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if value == "" or value is None:
        print(f"{key} is not set.", file=sys.stderr)
        return EXIT_ERROR
    print(value)
    return EXIT_OK


def models() -> int:
    config = _load()
    if config is None:
        return EXIT_ERROR
    try:
        available = list_models(config.base_url, timeout=15, api_key=config.api_key)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not available:
        print(
            f"no models listed at {config.base_url}"
            " (the server may not support GET /models,"
            " or the api_key is wrong)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    for name in available:
        print(name)
    print(f"\nset one: ferrum config set model {available[0]}", file=sys.stderr)
    return EXIT_OK


# -- write ----------------------------------------------------------------


def set_value(key: str, value: str) -> int:
    try:
        path = write_config_value(key, value)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if value == "":
        print(f"{key} cleared ({path})")
    else:
        display = mask_secret(value) if key == "api_key" else value
        print(f"{key} = {display}  [saved to {path}]")
    note = _env_override_note(key)
    if note:
        print(note, file=sys.stderr)
    return EXIT_OK


def preset(kind: str, rest: list[str]) -> int:
    # Each profile keeps its own api_key: Ollama ignores it, clouds need it,
    # so switching back never means re-entering anything.
    if kind == "local":
        if rest:
            print("error: usage: ferrum config local", file=sys.stderr)
            return EXIT_ERROR
        url = LOCAL_PRESET_URL
    else:
        url = rest[0] if rest else CLOUD_PRESET_URL
    try:
        path = write_config_values({"active": kind, "base_url": url})
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"switched to the {kind} profile")
    print(f"base_url = {url}  [saved to {path}]")
    note = _env_override_note("base_url")
    if note:
        print(note, file=sys.stderr)
    if kind == "cloud":
        _warn_if_cloud_key_missing()
    print("next: ferrum config setup    (api key and model, guided)", file=sys.stderr)
    print(
        "  or: ferrum config models   then  ferrum config set model <name>",
        file=sys.stderr,
    )
    return EXIT_OK


def _warn_if_cloud_key_missing() -> None:
    if os.environ.get("FERRUM_API_KEY"):
        return
    try:
        has_key = bool(profile_values("cloud").get("api_key"))
    except ConfigError:
        has_key = False
    if not has_key:
        print(
            "warning: api_key is not set — ferrum config set api_key <key>",
            file=sys.stderr,
        )


# -- shared helpers -------------------------------------------------------


def _load() -> Config | None:
    """The active profile's settings, or None after reporting the problem."""
    try:
        return load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _env_override_note(key: str) -> str | None:
    var = STR_FIELDS.get(key) or INT_FIELDS.get(key)
    if var and os.environ.get(var):
        return f"note: {var} is set in the environment and overrides the file"
    return None
