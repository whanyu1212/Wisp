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

## Working on these docs

```bash
npm install
npm run docs:dev       # local dev server with hot reload
npm run docs:build     # production build; fails on dead internal links
npm run docs:preview   # serve the built output
```
