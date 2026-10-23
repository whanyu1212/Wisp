# Contributing to Wisp

Thanks for helping. The full contributor guide is on the documentation site:
[Contributing](https://whanyu1212.github.io/Wisp/contributing/index.html) covers development setup,
testing and [how Wisp is built](https://whanyu1212.github.io/Wisp/contributing/building.html). Its
source lives in [`site/src/content/pages/contributing/`](site/src/content/pages/contributing/). This page is the short version.

## Set up a checkout

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/whanyu1212/Wisp.git
cd Wisp
uv sync --locked
```

Rust work uses the toolchain pinned in `rust-toolchain.toml` (1.85.0), which rustup selects
automatically.

## Before you open a pull request

Run the checks that match what you changed. For Python code or tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy            # uses the configured scope; don't pass a path
uv run pytest tests
```

Changes to Rust, the RPC protocol or the event models also need:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

Documentation-only changes don't need the application test suite. Run `uv run pytest tests/docs`
and, from `site/`, `npm ci`, `npm test`, `npm run check`, `npm run format:check`, `npm run build`,
and `npm run test:browser` (install Chromium with `npm exec playwright install chromium`).
See [`site/README.md`](site/README.md) for the pinned Node toolchain, source layout, and deployment
checks. GitHub Pages publishes the Astro build under the existing `/Wisp/` base path.

## Pull requests

- Keep each pull request to one focused change, and fill in the template, including the commands
  you ran and their results.
- Read [AGENTS.md](AGENTS.md) before changing the agent loop, sessions, RPC or the event models. It
  holds the architecture boundaries and conventions, including the event and schema rules and the
  docstring style.
- Changes to approvals, protected paths, cancellation, retries or process cleanup need adversarial
  regression tests.
- `CI passed` is the one required check, and Codex reviews each pull request automatically.

## Security issues

Please don't report vulnerabilities in public issues. [SECURITY.md](SECURITY.md) explains how to
report them privately.
