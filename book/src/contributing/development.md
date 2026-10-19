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

The tag-gated release flow assembles verified platform-wheel candidates. Installed
native wheels place `wisp-tui` in the active Python environment's scripts directory; the launcher
never searches `PATH`. Source development uses the explicit override above. A relative
`WISP_RUST_TUI_BINARY=target/debug/wisp-tui` is rejected rather than searched or resolved against the
working directory.

The Rust frontend is exact-lockstep with the Python runtime. The current package and crate
version is `0.2.2`; for a prerelease only the spelling differs, such as `0.2.0rc13` (Python) and
`0.2.0-rc.13` (Cargo). Rust translates `-alpha.N`, `-beta.N`, and `-rc.N` to Python's `aN`, `bN`, and `rcN` before the
exact version comparison. Neither the handshake nor events carry a protocol or schema version. Python's models and committed schemas remain authoritative, and `wisp-protocol`
generates its private Rust projections from those schemas at compile time. Package, protocol, or
generated-schema drift must fail a check or the startup handshake rather than degrade to another
contract.

Native wheels are built with pinned Hatchling and `packaging/hatch_build.py`; the source
distribution and pure wheel stay on `uv_build`. [How Wisp is built](building.md) explains the two
backends, what each release file contains, and how to reproduce a candidate wheel.

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
mdbook build book
mdbook serve book --open
```

Everything for the book lives in `book/`: `book/book.toml`, the Markdown source in `book/src/`
(`book/src/SUMMARY.md` controls chapter order), and the theme in `book/theme/`. Output goes to the
ignored `book/build/`. CI fails the build if mdBook logs an error, such as an include whose file
cannot be read, and checks links in the rendered output.

Mermaid's browser assets are generated vendor files. After upgrading the pinned plugin, refresh them
instead of editing them:

```bash
mdbook-mermaid install book
```
