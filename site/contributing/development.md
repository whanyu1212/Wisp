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

## Rust TUI {#rust-tui-scaffold}

The Rust frontend is a Cargo workspace member and is the sole interactive terminal interface.
Native wheels on macOS arm64 and Linux glibc 2.28+ x86_64 bundle it. Pure/source installs retain the
Python backend but require a matching source-built binary for interactive use. See the
[frontend boundary](../architecture/rust-tui-boundary). The repository pins Rust
1.85.0 in `rust-toolchain.toml`, and every workspace crate declares `rust-version = "1.85"` through
the workspace package settings.

Build and launch it with an absolute binary override:

```bash
cargo build -p wisp-tui
WISP_RUST_TUI_BINARY="$(pwd)/target/debug/wisp-tui" \
  uv run wisp tui
```

The tag-gated release flow now assembles verified platform-wheel candidates under #566. Installed
native wheels place `wisp-tui` in the active Python environment's scripts directory; the launcher
never searches `PATH`. Source development uses the explicit override above. A relative
`WISP_RUST_TUI_BINARY=target/debug/wisp-tui` is rejected rather than searched or resolved against the
working directory.

The Rust frontend is exact-lockstep with the Python runtime. The current package and crate
versions are `0.2.0rc2` (Python) and `0.2.0-rc.2` (Cargo); only the prerelease spelling differs.
Rust translates `-alpha.N`, `-beta.N`, and `-rc.N` to Python's `aN`, `bN`, and `rcN` before the
exact version comparison. The only accepted transport is live RPC v8 with event schema v39.
Python's models and committed schemas remain authoritative, and `wisp-protocol` generates its
private Rust projections from those schemas at compile time. Package, protocol, event-schema, or
generated-schema drift must fail a check or the startup handshake rather than degrade to another
contract.

Candidate native wheels use pinned Hatchling with `hatch_build.py`; ordinary PEP 517 and release
builds remain on `uv_build`. Set `WISP_RUST_TUI_WHEEL_TAG` only when reproducing a candidate wheel:

```bash
WISP_RUST_TUI_WHEEL_TAG=cp312-abi3-macosx_11_0_arm64 \
  uvx --from hatchling==1.27.0 hatchling build -t wheel -d candidate-dist
```

The tag is CI-owned packaging metadata, not runtime configuration. Use the exact target tag from
`rust-tui-wheels.yml`; never relabel an artifact built for another platform.

Run the Rust quality gates with the pinned toolchain:

```bash
uv run python -m wisp.rpc.protocol_schema --check
cargo fmt --all --check
cargo check --workspace --all-targets --all-features
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

## Working on these docs

Install the same documentation tools used in CI:

```bash
cargo install mdbook --version 0.5.4 --locked
cargo install mdbook-mermaid --version 0.17.1 --locked
```

Build or serve the book from the repository root:

```bash
mdbook build
mdbook serve --open
```

The source lives in `site/`, and `site/SUMMARY.md` controls chapter order. CI also checks links in the
rendered `book/` directory.

Mermaid's browser assets are generated vendor files. After upgrading the pinned plugin, refresh them
instead of editing them:

```bash
mdbook-mermaid install .
```
