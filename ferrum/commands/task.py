"""The three ways to run a project: inspect it, diagnose it, or fix it."""

from __future__ import annotations

import sys
from pathlib import Path

from ferrum.agent import Agent, AgentResult
from ferrum.builtin_tools import default_tools
from ferrum.commands import EXIT_ERROR, EXIT_OK
from ferrum.config import Config, ConfigError, host_of, load_config
from ferrum.context import ProjectContext, discover, find_project_root
from ferrum.model import (
    AuthenticationError,
    OpenAICompatibleProvider,
    ProviderError,
    RateLimitError,
    list_models,
)
from ferrum.ui import Console
from ferrum.verifier import Verifier


def inspect_target(target: str, console: Console) -> int:
    """`ferrum <dir>`: print the project summary, touch nothing."""
    path = Path(target)
    if not path.is_dir():
        print(
            f"error: not a directory: {target} (pass a directory to inspect)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    with console.live("reading project"):
        context = discover(find_project_root(path), load_config())
    print(context.summary())
    return EXIT_OK


def run_task(
    task: str, *, edit: bool, dry_run: bool = False, console: Console
) -> int:
    """`ferrum "<task>"` or `ferrum fix "<task>"`: one conversation."""
    config = _load_config()
    if config is None:
        return EXIT_ERROR
    missing = config.missing()
    if missing:
        return _report_missing(config, missing)

    root = find_project_root(Path.cwd())
    context = _describe_project(root, config, console)
    agent = _build_agent(root, config, console)
    try:
        result = agent.run(
            task,
            context,
            edit=edit,
            confirm=lambda prompt: confirm(prompt, console),
            dry_run=dry_run,
        )
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _provider_hint(exc)
        return EXIT_ERROR
    return _print_result(result, dry_run)


# -- setup ----------------------------------------------------------------


def _load_config() -> Config | None:
    try:
        return load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _describe_project(root: Path, config: Config, console: Console) -> ProjectContext:
    """Read the project and show the three facts a person wants first."""
    with console.live("reading project"):
        context = discover(root, config)
    console.step(context.brief())
    host = host_of(config.base_url)
    console.step(f"model: {config.model} at {host} [{config.profile}]")
    if not config.api_key and not config.local:
        console.step(
            f"no api_key set for {host} — the endpoint may refuse the request"
        )
    return context


def _build_agent(root: Path, config: Config, console: Console) -> Agent:
    provider = OpenAICompatibleProvider(
        config.base_url,
        config.model,
        config.api_key,
        config.request_timeout,
        config.max_tokens,
        on_retry=console.step,
    )
    verifier = Verifier(
        root,
        timeout=config.command_timeout,
        on_command=lambda cmd: console.tool("running " + " ".join(cmd)),
    )
    return Agent(
        config,
        provider,
        default_tools(root, config),
        printer=print,
        verifier=verifier,
        ui=console,
    )


# -- reporting ------------------------------------------------------------


def _print_result(result: AgentResult, dry_run: bool) -> int:
    if result.text:
        print()
        print(result.text)
    if result.patched:
        print()
        if result.verified:
            print("✓ Fix verified")
        elif result.verified is False:
            print("✗ Fix not verified — build or tests did not pass")
    if dry_run:
        print()
        print("(dry run — no files were changed)")
    if result.capped:
        return EXIT_ERROR
    if result.patched and result.verified is False:
        return EXIT_ERROR
    return EXIT_OK


def _report_missing(config: Config, missing: list[str]) -> int:
    """Nothing to ask the model with — say so in commands the user can type."""
    print(f"error: {', '.join(missing)} is not set.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  ferrum config setup {config.profile:<25} base_url, api_key, and model",
          file=sys.stderr)
    print("  ferrum config models        list models at the current endpoint",
          file=sys.stderr)
    print("  ferrum config set model <name>", file=sys.stderr)
    print(f"  (or export {', '.join(missing)}=... for this shell)", file=sys.stderr)
    _print_available_models(config)
    return EXIT_ERROR


def _print_available_models(config: Config) -> None:
    print(file=sys.stderr)
    try:
        available = list_models(config.base_url, timeout=15, api_key=config.api_key)
    except ProviderError:
        available = []
    if not available:
        print(f"  ferrum cannot see any model at {config.base_url}", file=sys.stderr)
        return
    shown = ", ".join(available[:10])
    extra = "" if len(available) <= 10 else f", ... ({len(available)} total)"
    print(f"  available: {shown}{extra}", file=sys.stderr)


def _provider_hint(exc: ProviderError) -> None:
    """Turn the raw endpoint failure into the one command that fixes it."""
    if isinstance(exc, AuthenticationError):
        print("  the endpoint rejected the api key:", file=sys.stderr)
        print("    ferrum config set api_key <key>   (or: ferrum config setup)",
              file=sys.stderr)
    elif isinstance(exc, RateLimitError):
        print("  the endpoint is rate limiting — wait a moment and retry",
              file=sys.stderr)


def confirm(prompt: str, console: Console | None = None) -> bool:
    if console is not None:
        console.clear()  # never let the spinner share the prompt's row
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}
