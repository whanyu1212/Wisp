# Development & bug reports

Wisp is personal software built for my own daily use and experiments. Reproducible bug reports are welcome in GitHub issues; I'm not looking for feature requests or unsolicited pull requests, though you're welcome to fork it.

The commands below are the local setup and checks I run. Longer notes live in the [Contributing docs](https://whanyu1212.github.io/Wisp/contributing/index.html) and [AGENTS.md](AGENTS.md).

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (Rust uses the toolchain pinned in `rust-toolchain.toml`):

```bash
git clone https://github.com/whanyu1212/Wisp.git
cd Wisp
uv sync --locked
```

## Checks

Python code or tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest tests
```

Rust, RPC protocol, or event models:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

Docs-only changes: run `uv run pytest tests/docs` and the Astro checks in [`site/README.md`](site/README.md).

Please report security issues privately via [SECURITY.md](SECURITY.md), not in public issues.
