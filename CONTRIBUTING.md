# Contributing to Ferrum

Thanks for helping out.

Ferrum is an open-source project, and we want it to stay welcoming, practical, and easy to contribute to.

## What can I contribute?

Almost anything that makes Ferrum better.

Some ideas:

- new tools
- C/C++/Rust/Assembly support
- compiler or build-system integrations
- tests
- bug fixes
- documentation
- examples
- performance improvements
- better error handling
- ideas for improving the agent

Not sure where to start? Check the open issues or start a discussion.

## Before you begin

For bigger changes, please open an issue first so we can talk through the idea before you spend time implementing it.

Small fixes and docs updates can usually go straight into a pull request.

## Pull requests

1. Fork the repository.
2. Create a branch for your change.
3. Make your edits.
4. Add or update tests when it makes sense.
5. Run the checks below and make sure they pass.
6. Keep the PR focused on one thing.
7. Open a pull request with a clear explanation of what changed and why.

Please do not submit generated code you do not understand.

## Development

The project uses [uv](https://docs.astral.sh/uv/) (a `uv.lock` is committed);
plain `pip install -e .` plus installing the dev tools also works.

```sh
uv sync                  # create .venv and install pytest + ruff + mypy

uv run pytest             # run the test suite (tests/)
uv run ruff check .       # lint
uv run ruff format .      # format (CI runs ruff format --check)
uv run mypy               # type-check the ferrum package (strict)
```

Run all four before opening a pull request — that is exactly what CI runs.

## Code style

Keep the code simple and readable.

Prefer small functions, clear names, and straightforward implementations over unnecessary abstractions.

## Questions

If you are unsure about something, open an issue or start a discussion.

There are no bad questions here.
