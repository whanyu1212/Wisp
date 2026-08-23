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
to `tests`; `refs/` contains reference repositories, not Wisp's suite.

Provider-facing tests must use deterministic fake or scripted providers, not live credentials.
When an event crosses RPC, verify the real JSON round trip. For package-owned resources, build an
artifact and inspect or install the wheel so a source-tree-only success cannot hide omitted package
data:

```bash
uv build --no-sources
uvx twine check dist/*
```

Report every completed check accurately. A nonzero exit or timeout is not a pass.

These checks establish local technical evidence. When the work is delivered through a GitHub pull
request, also use the bundled `github-pr-delivery` skill for authorization boundaries, exact-head CI,
thread-aware review, and final merge-readiness. Do not substitute local results for current remote
evidence or duplicate that skill's delivery procedure here.
