# Wisp

A Python, Pi-inspired coding agent experiment.

## Development

```bash
uv sync
uv run wisp -p "hello"
uv run pytest
```

By default, Wisp uses a deterministic fake provider so the agent core, CLI, and JSONL sessions can be tested without API keys.

## Configuration

Copy `.env.example` to `.env` for local settings:

```bash
cp .env.example .env
```

Available environment variables:

```env
WISP_PROVIDER=fake
WISP_MODEL=
OPENAI_API_KEY=
```

CLI flags override environment variables:

```bash
uv run wisp -p "hello" --provider fake
uv run wisp -p "hello" --provider openai --model gpt-5.5
```

`OPENAI_API_KEY` is required only when using the `openai` provider. Never commit `.env` or real API keys.
