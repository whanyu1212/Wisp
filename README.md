# Wisp

**A Python, Pi-inspired coding agent.**

Wisp is a small, auditable coding agent built around one agent core exposed through four
interchangeable interfaces — a print CLI, machine-readable JSON, a long-lived JSONL-RPC
protocol, and a fullscreen Textual TUI.

- **Auditable** — every provider-visible message and key event is persisted as JSONL.
- **Safe by default** — print mode exposes no tools to the model unless you opt in, and
  mutating tools require explicit approval.
- **Embeddable** — a typed RPC client/controller is the stable integration layer.

> Requires **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/).

## Quickstart

```bash
uv sync                                    # install dependencies
uv run wisp auth login openai-codex        # authenticate a provider
uv run wisp -p "hello"                      # run one turn
```

Wisp defaults to the `openai-codex` provider. See [Providers & auth](#providers--auth) for
other options. To try Wisp without any credentials, use the offline `fake` provider:

```bash
uv run wisp -p "hello" --provider fake
```

## Interfaces

Wisp runs the same agent core in four modes:

| Mode | Command | Output | Use for |
|------|---------|--------|---------|
| **Print** (default) | `wisp -p "…"` | Assistant text on stdout, events on stderr | Interactive/CLI use, piping |
| **JSON** | `wisp -p "…" --mode json` | One `WispEvent` JSON object per line | Machine-readable integrations |
| **RPC** | `wisp --mode rpc` | JSONL commands in, `WispEvent` JSONL out | Long-lived integrations |
| **TUI** | `wisp tui` | Fullscreen Textual UI | Day-to-day interactive sessions |

## Configuration

Copy the example env file and edit it for local defaults:

```bash
cp .env.example .env
```

```env
WISP_PROVIDER=            # openai-codex (default) | openai | fake
WISP_MODEL=               # provider default when blank
WISP_MODE=                # blank = help/text; set to tui to open the TUI directly
WISP_TUI_RENDERER=        # line | fullscreen | textual
WISP_SESSION_DIR=         # where transcripts are stored (default: ~/.wisp/sessions)
WISP_AUTH_FILE=~/.wisp/auth.json
OPENAI_API_KEY=           # required only for the openai provider
```

**CLI flags always override environment variables.** Never commit `.env`, auth files, or real
API keys.

## Providers & auth

```bash
uv run wisp -p "hello" --provider openai-codex --model gpt-5.5
uv run wisp -p "hello" --provider openai --model gpt-5.5
uv run wisp -p "hello" --provider fake
```

- **`openai-codex`** (default) — use a ChatGPT Plus/Pro subscription via OAuth:

  ```bash
  uv run wisp auth login openai-codex
  ```

  Credentials are stored in `WISP_AUTH_FILE` (default `~/.wisp/auth.json`) with private
  permissions.

- **`openai`** — set `OPENAI_API_KEY`.
- **`fake`** — a deterministic offline provider for tests and no-credential smoke runs; it
  echoes a canned response and needs no key.

Sessions persist to `~/.wisp/sessions` by default so transcripts survive across runs and can be
resumed; set `WISP_SESSION_DIR` (or pass `--session-dir`) to store them elsewhere (including a
temp path for ephemeral sessions).

## Tools

Wisp registers built-in local tools through its extension API. File tools are sandboxed to the
tool context's working directory by default.

| | Tools |
|---|---|
| **Read** | `read` · `grep` · `find` · `ls` |
| **Mutating** | `write` · `edit` |
| **Command** | `bash` |

**Print mode exposes no tools to the model unless you ask.** Read tools are enabled as a group;
mutating and command tools require per-tool opt-in:

```bash
uv run wisp -p "list files" --provider openai --allow-read-tools
uv run wisp -p "run tests"  --provider openai --allow-tool bash --yes
```

Because print mode is non-interactive, mutating and command tools are **also** blocked at
execution time unless you pass `--yes` (alias `--allow-unsafe-tool-execution`). Without it, the
model receives a clear tool error instead of Wisp executing the operation.

Wisp does not cap model/tool rounds by default (matching Pi's permissive agent loop). Pass
`--max-tool-iterations <n>` for a non-interactive fuse.

## Sessions

Wisp persists each run as a JSONL session and can continue an existing one:

```bash
uv run wisp -p "continue the work" --continue
uv run wisp -p "continue the work" --resume path/to/session.jsonl
uv run wisp -p "continue the work" --resume <session-id-prefix>
```

- `--continue` resumes the newest session in the active session directory.
- `--resume` accepts a JSONL path, filename, full session id, or unique id prefix.
- By default sessions live under `~/.wisp/sessions`, so `--continue`/`--resume` work across
  invocations. Point `--session-dir` or `WISP_SESSION_DIR` elsewhere to override.

Session files contain provider-facing `message` entries plus selected structured `event` entries
(tool calls, approvals, tool start/end, errors) for audit — but **not** `token.delta` events.
Continuation reads only message entries, so audit events never become model-visible history, and
stale project context from earlier turns is not replayed as instructions.

### Prompt & project context

Each turn sends a small default coding-agent system prompt plus a bounded project-context message
before the user prompt. The context includes the working directory, git branch and a capped
status summary, detected root files (`pyproject.toml`, `package.json`, `README.md`, …), and the
tools currently exposed to the model. It is informational only and is persisted to the session so
the provider-visible input stays auditable.

## TUI

```bash
uv run wisp tui
```

A fullscreen Textual TUI built on the same RPC controller other integrations use. Its compact
Pi-style footer shows the current working directory/session plus status, queued follow-ups, and
provider/model; token/cost/context metrics will appear once Wisp exposes usage events. Adjust runtime
settings with slash commands instead of up-front flags:

```text
/help                       show commands
/auth [provider]            show credential status
/login [provider] [device-code]
/logout [provider]
/provider [provider]        switch provider for future prompts (resets model to default)
/model [model]              switch model for future prompts
/quit, /exit
```

TUI login currently uses the `openai-codex` device-code flow; browser login is available from the
CLI (`uv run wisp auth login openai-codex`). A Pi-style model picker/catalog is not implemented yet.

Unlike print mode, the interactive TUI exposes the **full tool registry by
default** — otherwise it would be a chatbot that can't read files or run
commands. Mutating and command tools (`write`, `edit`, `bash`) still pause for a
y/N approval prompt on each call; pass `--yes` to auto-approve, or `--no-all-tools`
to fall back to the opt-in `--allow-read-tools` / `--allow-tool` filter.

Session and tool flags work with the `tui` command too:

```bash
uv run wisp tui --continue
uv run wisp tui --resume <session-id-prefix>
uv run wisp tui --no-all-tools                  # opt-in tool filter instead of the full registry
uv run wisp tui --no-all-tools --allow-read-tools
uv run wisp tui --yes                           # auto-approve mutating/command tools
uv run wisp tui --line          # simple line renderer, for fallback/debugging
```

The legacy `--mode tui` entrypoint remains for compatibility and honors
`--tui-renderer line|fullscreen|textual` plus `WISP_TUI_RENDERER`.

## Machine-readable output

### JSON mode

```bash
uv run wisp -p "hello" --mode json
```

Writes each `WispEvent` as one JSON object per line on stdout — including `token.delta`, tool
lifecycle events, errors, and `session.saved`. Assistant text is not written as raw text in this
mode.

### RPC mode

For long-lived integrations, drive Wisp over JSONL-RPC on stdin/stdout:

```bash
printf '{"type":"prompt","prompt":"hello"}\n{"type":"shutdown"}\n' | uv run wisp --mode rpc
```

Commands (the `id` field is optional — Wisp generates one when omitted):

| Command | Effect |
|---------|--------|
| `{"id":"cmd-1","type":"prompt","prompt":"…"}` | Run one agent turn, streaming `WispEvent` JSONL |
| `{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}` | Request cancellation of the running prompt |
| `{"id":"approval-1","type":"approval","call_id":"call-1","approved":true}` | Approve/deny a pending tool request |
| `{"id":"cmd-2","type":"shutdown"}` | Exit cleanly |

Each command emits `rpc.command.started` / `rpc.command.finished` so clients can group the events
between them. Prompts run sequentially; `cancel` and `approval` are handled while a prompt runs.
When an allowed mutating/command tool needs approval, Wisp emits `tool.approval.requested` with a
`call_id`; respond with an `approval` command carrying that `call_id`, a boolean `approved`, and an
optional denial `reason`. Cancellation is best-effort. Provider, model, tool-exposure, approval,
session, and max-iteration flags apply to the whole RPC process.

### Typed RPC client

Python integrations can skip hand-rolling JSONL and use the typed controller — the intended stable
integration layer:

```python
from wisp.events import RpcCommandFinished
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController

transport = await JsonlSubprocessRpcTransport.start()
controller = RpcController(transport)
prompt_id = await controller.prompt("hello")
shutdown_id = None
try:
    async for event in controller.events():
        ...
        if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
            shutdown_id = await controller.shutdown()
        elif (
            isinstance(event, RpcCommandFinished)
            and shutdown_id is not None
            and event.command_id == shutdown_id
        ):
            break
finally:
    await controller.close()
```

`RpcController` exposes typed `prompt`, `cancel`, `approve`, `configure`, and `shutdown` methods
and yields parsed `WispEvent` objects.

## Development

```bash
uv sync            # install
uv run pytest      # test
```

The test suite runs against the deterministic `fake` provider, so the agent core, CLI, and JSONL
sessions can be exercised without API keys or network access.
