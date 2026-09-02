---
title: Development setup
---

# Development setup

Wisp targets Python 3.12 or newer and uses `uv` for its locked development environment. Clone the
repository, then install the package and development dependencies:

```bash
git clone https://github.com/whanyu1212/Wisp.git
cd Wisp
uv sync --locked
```

Run the four quality and test gates before considering a change complete:

```bash
uv sync                        # install (use `uv sync --locked` to match CI)
uv run ruff format --check .   # format
uv run ruff check .            # lint
uv run mypy                    # types — no path argument
uv run pytest tests            # full suite
```

The project uses strict mypy checking and Ruff with a 100-character line length and the
`E`, `F`, `I`, `UP`, and `B` rule sets. Prefer async-first APIs with `anyio`, frozen dataclasses for
internal value objects, and Pydantic models for serialized boundaries.

## Rust TUI scaffold

The experimental Rust transport scaffold is a Cargo workspace member and is supported for source
development on macOS and Linux. The repository pins Rust 1.85.0 in `rust-toolchain.toml`, and every
workspace crate declares `rust-version = "1.85"` through the workspace package settings.

Build and launch it with an absolute binary override:

```bash
cargo build -p wisp-tui
WISP_RUST_TUI_BINARY="$(pwd)/target/debug/wisp-tui" \
  uv run wisp tui --renderer rust
```

The Python packages do not currently bundle this executable. A relative
`WISP_RUST_TUI_BINARY=target/debug/wisp-tui` is rejected rather than searched or resolved against the
working directory.

The scaffold is exact-lockstep with the Python runtime. The current package and crate versions are
both `0.1.0`; the only accepted transport is live RPC v3 with event schema v35. Python's models and
committed schemas remain authoritative, and `wisp-protocol` generates its private Rust projections
from those schemas at compile time. Package, protocol, event-schema, or generated-schema drift must
fail a check or the startup handshake rather than degrade to another contract.

Run the Rust quality gates with the pinned toolchain:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo check --workspace --all-targets --all-features
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

## Working on these docs

```bash
npm install
npm run docs:dev       # local dev server with hot reload
npm run docs:build     # production build; fails on dead internal links
npm run docs:preview   # serve the built output
```
