---
title: Testing
---

# Testing

```bash
uv run pytest tests                                                  # complete suite
uv run pytest tests -m 'not (slow or tui or process or benchmark)'   # faster core checks
```

The complete suite runs against deterministic fake or scripted providers, so the agent core, CLI, and
JSONL sessions are exercised without API keys, live model calls, or provider credentials. Run the
complete command before considering a change verified.

## Test selection

CI splits the suite into three marker-based jobs; mirror them locally when triaging:

```bash
uv run pytest tests -m 'not (slow or tui or process or benchmark)'   # core
uv run pytest tests -m 'tui'                                         # headless Textual
uv run pytest tests -m '(slow or process or benchmark) and not tui'  # system + slow
```

Markers are declared in `pyproject.toml`: `tui`, `process`, `benchmark`, `slow`. TUI, process, and
benchmark files declare them file-wide via `pytestmark`.

## Isolation

`tests/conftest.py` has an autouse fixture that clears every `WISP_*` environment variable and
repoints `HOME` and the working directory to temporary directories for each test. Tests opt into
configuration explicitly, so a local `~/.wisp` config can never affect results. If a test needs
trust, set it via `monkeypatch.setenv`.

Prefer `ScriptedProvider` / `FakeProvider` from `wisp.providers.fake` for new provider-facing tests
rather than live models.

## CI policy

CI runs for pull requests targeting `main` or `develop`, for direct updates to `main`, and by manual
dispatch.

Linux is authoritative for the complete locked-environment quality and test suite: Ruff formatting
and lint, configured `uv run mypy`, and `tests/`-only pytest partitions.

The `production_fault` partition is a required deterministic regression contract:

```bash
uv run pytest tests -m production_fault --durations=20
```

That contract inventories provider streams truncated before native completion, partial session and
auth writes, stale session writers, cancellation during SDK shutdown, and bounded process-tree
cleanup.

A focused macOS job covers auth/session locking and durability, subprocess and MCP cleanup, RPC/stdin
transport, secure filesystem operations, and a fake-provider CLI smoke test. The complete suite is
not duplicated on macOS because the remaining tests exercise platform-neutral contracts. Windows
remains best-effort until it has dedicated CI coverage.

CI additionally sets `WISP_TRUST=1`, `WISP_EFFORT=xhigh`, `WISP_CONTEXT_RESERVE_TOKENS=4096`, and
`WISP_AUTO_COMPACTION=0`. Match these if a test passes locally but fails in CI.
