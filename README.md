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
WISP_SESSION_DIR=
OPENAI_API_KEY=
```

CLI flags override environment variables:

```bash
uv run wisp -p "hello" --provider fake
uv run wisp -p "hello" --provider openai --model gpt-5.5
```

`OPENAI_API_KEY` is required only when using the `openai` provider. Sessions default to OS temp storage; set `WISP_SESSION_DIR` or pass `--session-dir` to keep them somewhere durable. Never commit `.env` or real API keys.

## Default prompt and project context

Each agent turn sends a small default coding-agent system prompt plus a bounded project context message before the user prompt. The context includes:

- current working directory
- git branch and a capped short status summary when available
- detected root project files such as `pyproject.toml`, `package.json`, or `README.md`
- the tools currently exposed to the model, or that no tools are exposed

These prompt/context messages are persisted in the JSONL session so the provider-visible input is auditable. Context is informational only: it does not change the print-mode tool exposure policy described below.

## Session continuation

Print mode can continue an existing JSONL session:

```bash
uv run wisp -p "continue the work" --continue
uv run wisp -p "continue the work" --resume path/to/session.jsonl
uv run wisp -p "continue the work" --resume <session-id-prefix>
```

By default, Wisp stores sessions under a private, non-precreatable OS temp directory (`<tmp>/wisp-<user>-*/sessions`) created for the current process. `--continue` resumes the newest session in the active session directory. `--resume` accepts a JSONL path, filename, full session id, or unique id prefix. Use `--session-dir` or `WISP_SESSION_DIR` for durable session storage and cross-invocation `--continue`. Wisp rebuilds the current prompt/context for the new turn and reuses prior non-system conversation messages as history, so stale project context from earlier turns is not replayed as instructions.

Session JSONL files contain provider-facing `message` entries plus selected structured `event` entries for audit/debugging. Wisp persists tool calls, approvals, tool execution start/end, and errors, but not `token.delta` events. Session continuation reads only message entries, so audit events do not become model-visible history.

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
uv run wisp -p "run tests" --provider openai --allow-tool bash --yes
```

Read tools (`read`, `grep`, `find`, `ls`) can be enabled together with
`--allow-read-tools`. Mutating tools (`write`, `edit`) and command execution
(`bash`) require explicit `--allow-tool <name>` opt-in.

Because print mode is non-interactive, mutating and command tools are still
blocked at execution time unless you also pass `--yes` (alias:
`--allow-unsafe-tool-execution`). Without that override, the model receives a
clear tool error instead of Wisp executing the operation.

Print mode keeps assistant text on stdout and writes operational events to
stderr, including tool calls, approval decisions, tool result summaries, and the
saved session path. Stderr may include spacing for terminal readability, while
stdout remains assistant-only and pipe-friendly.

For machine-readable integrations, use JSONL event output:

```bash
uv run wisp -p "hello" --mode json
```

JSON mode writes each `WispEvent` as one JSON object per line on stdout,
including `token.delta`, tool lifecycle events, errors, and `session.saved`.
Assistant text is not written as raw text in this mode.

For long-lived integrations, use JSONL RPC mode over stdin/stdout:

```bash
printf '{"type":"prompt","prompt":"hello"}\n{"type":"shutdown"}\n' | uv run wisp --mode rpc
```

RPC mode currently supports sequential commands:

- `{"type":"prompt","prompt":"..."}` runs one agent turn and streams `WispEvent` JSONL.
- `{"type":"shutdown"}` exits cleanly.

Provider, model, tool exposure, approval, session, and max-iteration CLI flags
apply to the whole RPC process.

Wisp does not cap model/tool rounds by default, matching Pi's permissive agent
loop. If you want a non-interactive fuse for a run, pass
`--max-tool-iterations <n>`.
