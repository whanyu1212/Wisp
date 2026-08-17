---
title: Development setup
---

# Development setup

TODO — adapt from `CLAUDE.md` § "Commands".

Cover the four gates that must pass before work is complete:

```bash
uv sync                        # install (use `uv sync --locked` to match CI)
uv run ruff format --check .   # format
uv run ruff check .            # lint
uv run mypy                    # types — no path argument
uv run pytest tests            # full suite
```

Also cover:

- Conventions: Python 3.12+, `mypy --strict`, ruff line-length 100, rules `E,F,I,UP,B`.
- Async-first with `anyio`.
- Frozen dataclasses for value objects, pydantic for anything serialized.

## Working on these docs

```bash
npm install
npm run docs:dev       # local dev server with hot reload
npm run docs:build     # production build; fails on dead internal links
npm run docs:preview   # serve the built output
```
