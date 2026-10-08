"""`ferrum doctor`: one screen that says whether a task can run here."""

from __future__ import annotations

import shutil
import sys

from ferrum.commands import EXIT_ERROR, EXIT_OK
from ferrum.config import (
    Config,
    ConfigError,
    config_file_path,
    host_of,
    load_config,
    mask_secret,
    setting_source,
)
from ferrum.model import probe_endpoint
from ferrum.ui import Console

KEYS = ("base_url", "model", "api_key")


def run(console: Console) -> int:
    """One screen that answers: can ferrum run a task here, and if not, why?"""
    print(f"{'python':<12}{sys.version.split()[0]} (need >= 3.10)")
    config, problems, warnings = _settings_report()
    endpoint_problems, endpoint_warnings = _probe_endpoint(config, console)
    problems += endpoint_problems
    warnings += endpoint_warnings
    warnings += _toolchain_report()
    return _verdict(config, problems, warnings)


def _settings_report() -> tuple[Config, list[str], list[str]]:
    """Print profile, file, and settings; collect what is missing."""
    problems: list[str] = []
    warnings: list[str] = []
    try:
        config = load_config()
        sources = {key: setting_source(key) for key in KEYS}
    except ConfigError as exc:
        print(f"{'config':<12}broken — {exc}")
        problems.append("fix or delete the config file")
        config = Config()  # defaults, so the rest of the report still runs
        sources = {key: "default" for key in KEYS}

    print(f"{'profile':<12}{config.profile} (active)")
    path = config_file_path()
    print(f"{'config file':<12}{path}{' ' if path.exists() else ' (not created yet)'}")
    for key in KEYS:
        value = getattr(config, key)
        display = mask_secret(value) if key == "api_key" else (value or "(not set)")
        print(f"{key:<12}{display}  [{sources[key]}]")
        if key == "model" and not value:
            problems.append("model is not set — ferrum config set model <name>")
    if not config.api_key and not config.local:
        warnings.append("cloud endpoints usually need an api_key")
    return config, problems, warnings


def _probe_endpoint(config: Config, console: Console) -> tuple[list[str], list[str]]:
    """Ask the endpoint one question and print what it said."""
    problems: list[str] = []
    warnings: list[str] = []
    with console.live(f"probing {host_of(config.base_url)}"):
        models, error = probe_endpoint(
            config.base_url, timeout=10, api_key=config.api_key
        )
    if error:
        print(f"{'endpoint':<12}unreachable — {error}")
        problems.append(f"cannot reach {config.base_url} (is the server running?)")
    elif models:
        print(f"{'endpoint':<12}ok — {len(models)} models listed")
    else:
        print(f"{'endpoint':<12}answered, but listed no models")
        warnings.append("the server listed no models; chat may still work")
    return problems, warnings


def _toolchain_report() -> list[str]:
    """Print the build tools found; note what fix mode cannot verify."""
    found = [tool for tool in ("clang", "gcc", "make", "cargo", "cmake")
             if shutil.which(tool)]
    print(f"{'toolchain':<12}{', '.join(found) if found else 'nothing found'}")
    warnings: list[str] = []
    if not {"clang", "gcc"} & set(found):
        warnings.append("no C compiler — fix mode cannot verify C builds")
    if not found:
        warnings.append("nothing to verify builds with; run commands are unavailable")
    return warnings


def _verdict(config: Config, problems: list[str], warnings: list[str]) -> int:
    print()
    for line in problems:
        print(f"problem: {line}")
    for line in warnings:
        print(f"warning: {line}")
    if not problems:
        print("verdict: ready")
        return EXIT_OK
    if any("ferrum config" in line for line in problems):
        print(f"fix: ferrum config setup {config.profile}")
    print("verdict: not ready")
    return EXIT_ERROR
