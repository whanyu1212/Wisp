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

## Local tools

Wisp registers built-in local tools through the extension API:

- `read`
- `write`
- `edit`
- `bash`
- `grep`
- `find`
- `ls`

The tool registry is available to runtime/extensions and the agent tool loop.
File tools are sandboxed to the tool context working directory by default.

Print-mode CLI does not expose tools to the model unless explicitly requested:

```bash
uv run wisp -p "list files" --provider openai --allow-read-tools
uv run wisp -p "run tests" --provider openai --allow-tool bash
```

Read tools (`read`, `grep`, `find`, `ls`) can be enabled together with
`--allow-read-tools`. Mutating tools (`write`, `edit`) and command execution
(`bash`) require explicit `--allow-tool <name>` opt-in.
