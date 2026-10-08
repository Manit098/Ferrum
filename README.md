# Ferrum

Ferrum is an open-source coding harness for low-level and systems work.

It is built for people working close to the machine: C, C++, Rust, Assembly, debugging, compilers, memory, and other systems-heavy code. Ferrum gives an AI model the context and tools it needs to work on a real codebase instead of guessing from a chat window.

## Why Ferrum?

AI tools are great at app code, but systems programming is different. A useful systems coding agent has to understand the current code, compiler output, build behavior, and low-level details before making a change.

Ferrum is built around that idea.

```text
Understand → Change → Build → Verify
```

The goal is simple: make AI genuinely useful when the code lives near the machine.

## What it gives you

- project and source-code context
- code search and inspection
- controlled file changes
- compiler and build feedback
- testing and verification
- tools designed for systems work

Ferrum also stays model-agnostic, so the underlying AI can change without changing the harness.

## Current state

Ferrum is still early, but the core loop is the main focus right now.

The long-term direction includes things like:

- C/C++/Rust/Assembly tooling
- compiler and build-system integration
- debugger integration
- binary and executable analysis
- better project understanding
- extensible tools for systems developers

## Contributing

Ferrum is meant to be built in public.

You do not need to be an AI expert to contribute. Code, tests, documentation, examples, bug reports, ideas, and improvements are all welcome.

See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## License

Ferrum is licensed under the Apache License 2.0.

See [LICENSE](LICENSE) for the full license.
