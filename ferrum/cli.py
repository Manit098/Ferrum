"""ferrum command line: the parser, the streams, and the exit path.

What each command actually does lives in ferrum.commands — this module only
decides which one runs and makes sure Ctrl+C and a broken pipe leave quietly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from ferrum import __version__
from ferrum.commands import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, doctor, task
from ferrum.commands import config as config_command
from ferrum.ui import Console

PROG = "ferrum"
DESCRIPTION = "Ferrum — a coding harness for low-level and systems programming."

EPILOG = """examples:
  ferrum .                 inspect a project
  ferrum "task"            diagnose with the model (read-only)
  ferrum fix "task"        patch, build, and verify
  ferrum fix --dry-run "t"  show the proposed patch, write nothing
  ferrum doctor            check config, endpoint, and toolchain
  ferrum config setup      enter base_url, api_key, and model interactively
  ferrum config            show both profiles and which one is active
  ferrum config local      switch to Ollama on localhost (no api key)
  ferrum config cloud      switch to the cloud endpoint
  ferrum config models     list models at the active endpoint

profiles:
  one config file holds both a local and a cloud profile, each with its own
  base_url, model, and api_key. FERRUM_PROFILE=cloud picks one for a run.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging on stderr",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with fix: show the proposed patch but never write files",
    )
    parser.add_argument(
        "target",
        nargs="?",
        metavar="TARGET",
        help="directory to inspect, or a task for the model",
    )
    parser.add_argument(
        "extra",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    return parser


def setup_logging(verbose: bool = False) -> None:
    """Quiet by default; -v turns on the full log (retries, skipped files)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    # Plain white text, always: Python 3.14+ colours argparse help and
    # tracebacks on a terminal otherwise.
    os.environ["PYTHON_COLORS"] = "0"
    _force_utf8_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)

    parser = build_parser()
    args = parser.parse_args(args_list)
    setup_logging(args.verbose)
    console = Console()
    try:
        return _dispatch(args, console)
    except KeyboardInterrupt:
        # Ctrl+C is a decision, not a crash: no traceback, no stack dump.
        console.clear()
        console.step("interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        _silence_broken_pipe()
        return EXIT_OK
    finally:
        console.close()


def _dispatch(args: argparse.Namespace, console: Console) -> int:
    if args.target is None:
        if args.dry_run:
            print(
                'error: usage: ferrum fix --dry-run "<task>"',
                file=sys.stderr,
            )
            return EXIT_ERROR
        parser = build_parser()
        parser.print_help()
        return EXIT_OK

    if args.target == "fix":
        task_text = " ".join(args.extra).strip()
        if not task_text:
            print('error: usage: ferrum fix "<task>"', file=sys.stderr)
            return EXIT_ERROR
        return task.run_task(
            task_text, edit=True, dry_run=args.dry_run, console=console
        )

    if args.dry_run:
        print(
            'error: --dry-run only applies to: ferrum fix --dry-run "<task>"',
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.target == "config":
        return config_command.run(args.extra, console)

    if args.target == "doctor":
        if args.extra:
            print("error: usage: ferrum doctor", file=sys.stderr)
            return EXIT_ERROR
        return doctor.run(console)

    return _project_or_task(args.target, args.extra, console)


def _project_or_task(target: str, extra: list[str], console: Console) -> int:
    """A directory is inspected; anything else becomes a task for the model."""
    path = Path(target)
    if path.is_dir() or path.exists():
        return task.inspect_target(target, console)
    if "/" in target or "\\" in target or target.startswith("."):
        print(f"error: no such directory: {target}", file=sys.stderr)
        return EXIT_ERROR
    return task.run_task(
        " ".join([target, *extra]),
        edit=False,
        console=console,
    )


def _force_utf8_streams() -> None:
    # Without this, the box-drawing line in the summary dies when output is
    # piped. Line buffering keeps stdout and stderr in the order they were
    # written, so `ferrum ... | tee log` still reads top to bottom.
    for stream in (sys.stdout, sys.stderr):
        # reconfigure only exists on TextIOWrapper, not on a redirected pipe.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (ValueError, OSError):
            pass


def _silence_broken_pipe() -> None:
    # Otherwise Python reports the failed stdout flush at shutdown.
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


if __name__ == "__main__":
    sys.exit(main())
