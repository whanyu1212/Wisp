# Development & Bug Reports

Wisp is personal software built for daily terminal use and to explore agent engineering.

- **Bug reports:** GitHub issues are open for reproducible bug reports.
- **Feature requests & PRs:** Feature requests and unsolicited pull requests aren't being sought.
  Feel free to fork the repository if you want to take it in your own direction.

The notes below record the local development setup and verification gates used to maintain Wisp (or
to verify a fork or agreed-upon bug fix). The longer version lives on the documentation site under
[Contributing](https://whanyu1212.github.io/Wisp/contributing/index.html) (source in
[`site/src/content/pages/contributing/`](site/src/content/pages/contributing/)).

## Set up a checkout

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/whanyu1212/Wisp.git
cd Wisp
uv sync --locked
```

Rust work uses the toolchain pinned in `rust-toolchain.toml` (1.88.0), which rustup selects
automatically.

## Verification checks

Run the checks that match what changed. For Python code or tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy            # uses the configured scope; don't pass a path
uv run pytest tests
```

Changes to Rust, the RPC protocol, or the event models also need:

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
checks. GitHub Pages publishes the Astro build under the `/Wisp/` base path.

## Architecture & review conventions

- Read [AGENTS.md](AGENTS.md) before changing the agent loop, sessions, RPC, or event models. It
  documents the ownership boundaries and invariants, including the event and schema rules and the
  docstring style.
- Changes to approvals, protected paths, cancellation, retries, or process cleanup need adversarial
  regression tests.

## Security issues

Please don't report vulnerabilities in public issues. [SECURITY.md](SECURITY.md) explains how to
report them privately.
