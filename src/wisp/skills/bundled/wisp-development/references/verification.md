# Verification

Use the repository's declared commands from a source checkout:

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest tests
```

Use `uv run mypy` without a path so configured package discovery remains authoritative. Scope pytest
to `tests`, which holds Wisp's whole suite.

Provider-facing tests must use deterministic fake or scripted providers, not live credentials.
When an event crosses RPC, verify the real JSON round trip. For Rust changes, also run the workspace
format, Clippy, and test gates from `AGENTS.md`; frontend-boundary changes require the Python/Rust
handoff tests. Generated theme changes require
Rust theme catalog tests in `cargo test -p wisp-tui`.

For package-owned resources, build an artifact and inspect or install the wheel so a source-tree-only
success cannot hide omitted package data:

```bash
uv build --no-sources
uvx twine check dist/*
```

Report every completed check accurately. A nonzero exit or timeout is not a pass.

These checks establish local technical evidence. When the work is delivered through a GitHub pull
request, follow the delivery rules in `AGENTS.md` (or the project's own instructions outside a Wisp
checkout). Wait until every CI check on the current head commit has finished, and inspect the
unresolved review threads. Do not substitute local results for that remote evidence.
