# Wisp

A Python, Pi-inspired coding agent experiment.

## Development

```bash
uv sync
uv run wisp -p "hello"
uv run pytest
```

The first milestone intentionally uses a fake provider so the agent core, CLI, and JSONL sessions can be built before real model SDK integrations.
