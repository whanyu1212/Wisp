# Wisp

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Package manager](https://img.shields.io/badge/package%20manager-uv-purple.svg)](https://docs.astral.sh/uv/)
[![Code style](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://docs.astral.sh/ruff/)

**A Python-native coding agent with CLI, JSON, RPC, and TUI interfaces.**

Wisp is an event-driven coding-agent runtime designed for local tool use, persistent
sessions, explicit approval flows, and embeddable integrations. The same core powers
interactive CLI use, machine-readable JSON output, long-lived RPC sessions, and the TUI.

Wisp uses Pi as a behavioral reference while implementing its runtime, safety model,
and extension surface in Python.

- **Event-driven** — agent activity is exposed as structured `WispEvent` streams.
- **Auditable** — provider-visible messages and key runtime events are persisted as JSONL.
- **Safe by default** — tools are opt-in, and mutating or command execution requires approval.
- **Embeddable** — a typed RPC client/controller provides a stable integration boundary.

> **Status:** Wisp is early-stage software. APIs and CLI behavior may change while the
> agent loop, session model, RPC layer, and TUI stabilize.
>
> Requires **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/).

## Quickstart

```bash
uv sync
uv run wisp auth login openai-codex
uv run wisp -p "hello"
```

Wisp defaults to the `openai-codex` provider. For an offline smoke test that does not
require credentials, use the deterministic `fake` provider:

```bash
uv run wisp -p "hello" --provider fake
```

## Usage modes

Wisp runs the same agent core in four modes:

| Mode | Command | Output | Best for |
|------|---------|--------|----------|
| **Print** (default) | `wisp -p "…"` | Assistant text on stdout, events on stderr | One-shot prompts and shell workflows |
| **JSON** | `wisp -p "…" --mode json` | One `WispEvent` JSON object per line | Scripts and event consumers |
| **RPC** | `wisp --mode rpc` | JSONL commands in, `WispEvent` JSONL out | Long-lived integrations |
| **TUI** | `wisp tui` | Fullscreen Textual UI | Interactive development sessions |

## Providers & auth

Wisp supports four provider modes:

```bash
uv run wisp -p "hello" --provider openai-codex --model gpt-5.5
uv run wisp -p "hello" --provider openai --model gpt-5.5
uv run wisp -p "hello" --provider anthropic --model claude-sonnet-5
uv run wisp -p "hello" --provider fake
```

- **`openai-codex`** (default) — use a ChatGPT Plus/Pro subscription via OAuth:

  ```bash
  uv run wisp auth login openai-codex
  ```

  Credentials are stored in `WISP_AUTH_FILE` (default `~/.wisp/auth.json`) with private
  permissions.

- **`openai`** — set `OPENAI_API_KEY`.
- **`anthropic`** — set `ANTHROPIC_API_KEY`.
- **`fake`** — a deterministic offline provider for tests and no-credential smoke runs.

Use `/model` with no arguments in the TUI to list every catalog model grouped by provider. If a
given model id belongs to only one registered provider, `/model <id>` switches the active provider
to match; otherwise use `/provider <name>` first.

Sessions persist to `~/.wisp/sessions` by default so transcripts survive across runs and can be
resumed. Set `WISP_SESSION_DIR` or pass `--session-dir` to store them elsewhere, including a
temporary path for ephemeral sessions.

## Tools and safety

Wisp includes built-in local tools for reading files, editing files, searching projects, and
running shell commands. File tools are sandboxed to the tool context's working directory by
default, and tools are exposed to the model only when enabled.

| Category | Tools |
|----------|-------|
| **Read** | `read` · `grep` · `find` · `ls` |
| **Mutating** | `write` · `edit` |
| **Command** | `bash` |

**Print mode exposes no tools to the model unless you ask.** Read tools are enabled as a group;
mutating and command tools require per-tool opt-in:

```bash
uv run wisp -p "list files" --provider openai --allow-read-tools
uv run wisp -p "run tests"  --provider openai --allow-tool bash --yes
```

Because print mode is non-interactive, mutating and command tools are also blocked at execution
time unless you pass `--yes` (alias `--allow-unsafe-tool-execution`). Without it, the model
receives a clear tool error instead of Wisp executing the operation.

Wisp does not cap model/tool rounds by default, matching Pi's permissive agent loop. Pass
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
- By default sessions live under `~/.wisp/sessions`. Use `--session-dir` or
  `WISP_SESSION_DIR` to override this location.

Session files contain provider-facing `message` entries plus selected structured `event` entries
(tool calls, approvals, tool start/end, errors) for audit. They do **not** persist
`message.delta` events. Continuation reads only message entries, so audit events never become
model-visible history, and stale project context from earlier turns is not replayed as
instructions.

## Configuration

Wisp reads configuration from CLI flags, environment variables, and JSON settings files.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `WISP_PROVIDER` | Provider name: `openai-codex`, `openai`, `anthropic`, or `fake` |
| `WISP_MODEL` | Model override; blank uses the provider default |
| `WISP_MODE` | Default mode; set to `tui` to open the TUI directly |
| `WISP_TUI_RENDERER` | TUI renderer: `line`, `fullscreen`, or `textual` |
| `WISP_SESSION_DIR` | Session storage directory; defaults to `~/.wisp/sessions` |
| `WISP_AUTH_FILE` | Auth file path; defaults to `~/.wisp/auth.json` |
| `WISP_RETRY_MAX_RETRIES` | Provider retry count; defaults to `2`, set `0` to disable |
| `WISP_RETRY_BASE_DELAY_SECONDS` | Initial retry delay; defaults to `0.5` |
| `WISP_RETRY_MAX_DELAY_SECONDS` | Maximum retry delay; defaults to `30` |
| `OPENAI_API_KEY` | Required only for the `openai` provider |
| `ANTHROPIC_API_KEY` | Required only for the `anthropic` provider |

### Settings files

For durable defaults, use a settings file instead of exporting every session. The user-level file
lives at `~/.wisp/settings.json`; a project may add `./.wisp/settings.json`, applied only after
you trust the project.

```json
{
  "provider": "openai",
  "model": "gpt-5.5",
  "session_dir": "~/.wisp/sessions",
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

Configuration precedence, highest to lowest:

```text
CLI flag > environment variable > project ./.wisp/settings.json > user ~/.wisp/settings.json > built-in default
```

Never commit auth files or real API keys.

### Retry behavior

Retry settings are user-only, even in a trusted project: a repository cannot increase API spending
or prolong waits. Wisp retries only requests that fail before the provider starts streaming. It
uses bounded exponential backoff with small jitter, honors reasonable `Retry-After` requests, and
emits retry progress in JSON/RPC and the TUI. It never replays an already-started response.

> **Migration note:** Wisp no longer reads a project `.env` file. Move any values you kept there
> into your shell environment or `~/.wisp/settings.json`. A project `.env` on disk is still
> treated as a secret and is never surfaced to the model.

## Project trust

Project-local settings, context files (`AGENTS.md` / `CLAUDE.md`), and project extensions are
loaded only after the project is trusted. Untrusted projects can still be used, but Wisp ignores
their local configuration and instructions.

The first time you run Wisp in an untrusted directory it asks:

```text
Do you trust the files in /path/to/project?
```

Answer **yes** and the decision is remembered globally in `~/.wisp/trust.json`, keyed by resolved
path. Manage persisted decisions with:

```bash
uv run wisp trust status [path]   # trusted, untrusted, or undecided
uv run wisp trust allow [path]    # persistently trust a project
uv run wisp trust revoke [path]   # persistently mark a project untrusted
uv run wisp trust forget [path]   # remove the decision so Wisp can prompt again
```

Security notes:

- **Non-interactive runs** (CI, scripts, standalone RPC) default to untrusted. The interactive
  TUI asks before entering the interface. Set `WISP_TRUST=1` to opt in for one process, or
  `WISP_TRUST=0` to force untrusted mode.
- `WISP_TRUST` is read only from the real process environment, never from project files, and is
  not persisted.
- The `protected_paths` secret guard is user-only: a project settings file can never weaken it,
  even once trusted.
- `WISP_TRUST_FILE` may relocate the global trust store, but only to an absolute path outside the
  repository. A relative value is rejected.

## How Wisp builds context

Each turn sends a default coding-agent system prompt plus a bounded project-context message before
the user prompt. The context includes the working directory, git branch and capped status summary,
detected root files (`pyproject.toml`, `package.json`, `README.md`, …), tools exposed to the
model, and trusted project instructions from context files.

Context files are loaded from the trusted context root down to the current working directory, with
parent instructions before nested ones. In each directory Wisp uses the first Pi-compatible match
in this order: `AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, `CLAUDE.MD`. Symlinked, protected, or
out-of-scope context files are skipped. Project instructions are bounded separately from the tool
list so large instruction files cannot hide the tools available to the model.

Project context is trust-gated. In untrusted projects, Wisp does not read local instruction files
or project settings. This is stricter than Pi's broader context loading, but keeps project
guidance inside the same trust boundary as project settings and future project extensions.

## TUI

```bash
uv run wisp tui
```

A fullscreen Textual TUI built on the same RPC controller other integrations use. Its compact
Pi-style footer shows the current working directory/session plus status, queued follow-ups, and
provider/model; completed tool cards include bounded multiline output previews. Token/cost/context
metrics will appear once Wisp exposes usage events. Adjust runtime settings with slash commands
instead of up-front flags. The prompt editor accepts multiline text: press Enter to submit, or
Shift+Enter / Ctrl+J to insert a newline. Pasted newlines are preserved.

Available slash commands:

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
CLI (`uv run wisp auth login openai-codex`). `/model` with no arguments lists every catalog model
grouped by provider; there is no interactive fuzzy-picker yet.

Unlike print mode, the interactive TUI exposes the **full tool registry by
default** — otherwise it would be a chatbot that can't read files or run
commands. Mutating and command tools (`write`, `edit`, `bash`) still pause for an
approval decision. Choose **approve once**, **allow this exact tool for the TUI
session**, **YOLO for all mutating/command tools in this TUI process**, or **deny**.
YOLO requires a second explicit confirmation and is never persisted. Pass `--yes`
to auto-approve from startup, or `--no-all-tools` to fall back to the opt-in
`--allow-read-tools` / `--allow-tool` filter.

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

### Terminal capabilities

The Textual TUI targets truecolor terminals but degrades gracefully. 256-color and
16-color/ANSI terminals are handled automatically by Textual's own color-system
detection — no Wisp-specific configuration needed. Setting `NO_COLOR` (any value)
switches the whole interface to a deterministic grayscale rendering, inherited
automatically from Textual. Wisp is working toward every semantic state (approval,
denial, error, retry, tool status) carrying a non-color cue — a distinct glyph, text
label, or border form — so the interface stays legible and structurally correct with
color disabled; see the open accessibility issues for current coverage.

## Machine-readable output

Every outbound `WispEvent` includes `"schema_version": 5`. A successful prompt follows this
lifecycle (tool events repeat inside a turn when the model requests tools):

```text
agent.started
  turn.started
    provider.retrying *
    message.started
    message.delta *
    message.completed
    tool.call -> tool.execution.started -> approval events -> tool.execution.ended -> tool.result
  turn.completed
session.saved
agent.completed
```

`message.delta` distinguishes `text` from `thinking` with `content_kind`.
`message.completed` carries the assembled content, finish reason, response id, and completed tool
calls. A failed provider response or tool loop emits `error`, a failed `turn.completed`, and a
failed `agent.completed`; it does not emit `message.completed` for an incomplete response.

Schema v5 adds `model.provider_auto_switched`, emitted during an RPC `configure` command
immediately before its `rpc.command.finished` when a model-only `/model <id>` request resolves
(via the model registry) to a provider other than the one currently active — it carries the
`command_id` it belongs to, the `provider` switched to, and the `model` that triggered the switch,
so a client tracking provider state client-side can resync instead of assuming its own explicit
`/provider` requests are the only way the active provider ever changes. Schema v4 adds an optional
`exit_code` (`int | null`) to `tool.execution.ended` and `tool.result`, carrying a shell-like
tool's process exit status for presentation; it is `null` for tools without exit-code semantics.
Schema v3 added `provider.retrying` before `message.started`, with the next attempt number,
bounded delay, retry reason, and optional HTTP status. All retain schema v2's explicit
turn/message lifecycle and tool ordering. JSON/RPC consumers should branch on `schema_version` and
reject versions they do not support; Wisp's typed RPC client does this automatically.

### JSON mode

```bash
uv run wisp -p "hello" --mode json
```

Writes each `WispEvent` as one JSON object per line on stdout — including `message.delta`, tool
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
| `{"id":"approval-1","type":"approval","call_id":"call-1","approved":true,"scope":"tool_session"}` | Approve/deny a pending tool request |
| `{"id":"trust-1","type":"trust","request_id":"req-1","trusted":true}` | Answer a project-trust request |
| `{"id":"cmd-2","type":"shutdown"}` | Exit cleanly |

Each command emits `rpc.command.started` / `rpc.command.finished` so clients can group the events
between them. Prompts run sequentially; `cancel` and `approval` are handled while a prompt runs.
When an allowed mutating/command tool needs approval, Wisp emits `tool.approval.requested` with a
`call_id`; respond with an `approval` command carrying that `call_id`, a boolean `approved`, and an
optional approval `scope`: `once` (the default), `tool_session` (the exact tool name for this RPC
process), or `all_session` (all mutating/command tools for this RPC process). Scoped denials are
rejected. An optional `reason` describes a denial. When an undecided project needs trust, Wisp emits `trust.requested` with a
`request_id`; respond with a `trust` command carrying that `request_id`, a boolean `trusted`, and an
optional denial `reason`. Denials are remembered unless the command includes `"transient": true`
(for example, a UI closing before the user answered). Cancellation is best-effort. Provider, model,
tool-exposure, approval, session, and max-iteration flags apply to the whole RPC process.

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

The coding runtime is split into explicit inward-facing layers:

```text
CLI / RPC / TUI -> CodingSession -> AgentHarness -> run_agent_loop
```

`wisp.coding.session.CodingSession` owns project prompts, persistence, trust state, tool policy,
and application event publication. `AgentHarness` owns the in-memory transcript and cancellation,
while `run_agent_loop` remains independent of sessions and frontends. CLI and RPC frontends import
the coding-session coordinator directly; the TUI consumes that same runtime through RPC events.

Providers yield typed events from `wisp.providers`. A stream may emit zero or more
`ProviderRetrying` events before exactly one `ProviderResponseStarted`, followed by zero or more
text/thinking deltas and completed tool calls, then exactly one `ProviderResponseCompleted` or
`ProviderResponseFailed`. The agent rejects retries after start, missing or duplicate terminal
boundaries, post-terminal events, and mismatched tool-call summaries. Configuration and
request-opening failures may raise before the start event; after a response starts, adapters
normalize expected transport and provider failures into a failed terminal event. Use
`ScriptedProvider` to exercise deterministic multi-turn and failure cases without a live model.

```bash
uv sync            # install
uv run pytest      # test
```

The test suite runs against the deterministic `fake` provider, so the agent core, CLI, and JSONL
sessions can be exercised without API keys or network access.
