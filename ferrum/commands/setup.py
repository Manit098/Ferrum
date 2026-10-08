"""The guided `ferrum config setup` walk: profile, endpoint, key, model.

Kept apart from the other config commands because it is the only part that
talks to a person — it prompts, hides the key, lists what the endpoint
offers, and saves as it goes. Everything here assumes a terminal.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from ferrum.commands import EXIT_ERROR, EXIT_OK
from ferrum.config import (
    PROFILES,
    Config,
    ConfigError,
    host_of,
    is_local_endpoint,
    load_config,
    mask_secret,
    normalize_base_url,
    write_config_values,
)
from ferrum.model import ProviderError, list_models
from ferrum.ui import Console


def run(console: Console, profile: str | None = None) -> int:
    """One walk through profile, endpoint, api key, and model."""
    if profile is not None and profile not in PROFILES:
        _print_unknown_profile(profile)
        return EXIT_ERROR
    if not _interactive():
        _print_needs_terminal()
        return EXIT_ERROR
    profile, current = _setup_target(profile)
    if profile is None or current is None:
        return EXIT_ERROR

    print(f"Ferrum setup — {profile} profile, Enter keeps [brackets].")
    print()
    base_url = _ask_endpoint(current.base_url)
    if base_url is None:
        return EXIT_ERROR
    values = {"active": profile, "base_url": base_url}
    api_key = _ask_api_key(base_url, current.api_key, values)
    path = _save(values, profile)
    if path is None:
        return EXIT_ERROR
    print(f"base_url = {base_url}")
    print(f"api_key  = {mask_secret(api_key)}")
    print(f"saved to {path}")
    print()

    model = _ask_model(console, base_url, api_key, current.model)
    if not model:
        print(
            "error: no model given — run `ferrum config setup` again.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    path = _save({"model": model}, profile)
    if path is None:
        return EXIT_ERROR
    print(f"model    = {model}  [saved to {path}]")
    print()
    print(f"active profile: {profile}")
    print("next: ferrum doctor   (endpoint, toolchain, and settings)")
    return EXIT_OK


# -- steps ----------------------------------------------------------------


def _setup_target(profile: str | None) -> tuple[str | None, Config | None]:
    """Resolve which profile is being edited (asking when not given)."""
    current = _load(profile)
    if current is None:
        return None, None
    if profile is not None:
        return profile, current
    profile = _ask("profile to edit", current.profile)
    if profile not in PROFILES:
        _print_unknown_profile(profile)
        return None, None
    current = _load(profile)
    if current is None:
        return None, None
    return profile, current


def _load(profile: str | None = None) -> Config | None:
    try:
        return load_config(profile=profile) if profile else load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _ask_endpoint(current_url: str) -> str | None:
    try:
        return normalize_base_url(_ask("OpenAI-compatible base URL", current_url))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _ask_api_key(base_url: str, current_key: str, values: dict[str, str]) -> str:
    if is_local_endpoint(base_url):
        print(f"local endpoint ({host_of(base_url)}) — no api key needed.")
        return current_key
    typed = getpass.getpass("api key (hidden, Enter keeps current): ").strip()
    if typed:
        values["api_key"] = typed
        return typed
    return current_key


def _ask_model(console: Console, base_url: str, api_key: str, default: str) -> str:
    with console.live(f"listing models at {host_of(base_url)}"):
        try:
            available = list_models(base_url, timeout=15, api_key=api_key)
        except ProviderError:
            available = []
    if not available:
        print(f"no models listed at {base_url}")
        print("the server may not implement GET /models, or the api key is wrong.")
    return _pick_model(available, default)


def _save(values: dict[str, str], profile: str) -> Path | None:
    try:
        return write_config_values(values, profile=profile)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


# -- prompts and messages -------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _pick_model(available: list[str], default: str) -> str:
    if not available:
        print("this endpoint listed no models; type the one to use.")
        return _ask("model name", default)
    print()
    print("models at this endpoint:")
    width = len(str(len(available)))
    for index, name in enumerate(available, start=1):
        print(f"  {index:>{width}}  {name}")
    print()
    hint = default if default in available else "1"
    choice = _ask("model (number or full name)", hint)
    if choice.isdigit() and 1 <= int(choice) <= len(available):
        return available[int(choice) - 1]
    return choice


def _interactive() -> bool:
    """Ask questions only when a person is attached to both ends."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _print_unknown_profile(profile: str) -> None:
    print(
        f"error: unknown profile {profile!r}; choose from: " + ", ".join(PROFILES),
        file=sys.stderr,
    )


def _print_needs_terminal() -> None:
    print("error: 'ferrum config setup' needs a terminal.", file=sys.stderr)
    print("set the same three values by hand:", file=sys.stderr)
    print("  ferrum config local | cloud   pick the profile first", file=sys.stderr)
    print("  ferrum config set base_url <url>", file=sys.stderr)
    print(
        "  ferrum config set api_key <key>    (the local profile skips this)",
        file=sys.stderr,
    )
    print("  ferrum config set model <name>", file=sys.stderr)
