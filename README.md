<p align="center">
  <img src="assets/wisp-banner.png" alt="Wisp — A Python coding agent that stays in sync." width="100%">
</p>

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

---

**Contents** · [Quickstart](#quickstart) · [Usage modes](#usage-modes) ·
[Providers](#providers--auth) · [Tools & safety](#tools-and-safety) · [Sessions](#sessions) ·
[Configuration](#configuration) · [Project trust](#project-trust) ·
[Context](#context--compaction) · [TUI](#tui) · [Development](#development)

**Reference** · [Changelog](CHANGELOG.md)

## Quickstart

```bash
uv sync
uv run wisp auth login openai-codex
uv run wisp -p "hello"
```

Wisp defaults to the `openai-codex` provider. For an offline smoke test that needs no credentials:

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

JSON mode writes every `WispEvent` as one object per line on stdout — including `message.delta`,
tool lifecycle events, errors, and `session.saved`. Assistant text is not written as raw text in
this mode.

RPC mode, the typed Python client, and the in-process SDK share the same event and session
contracts as the CLI interfaces.

## Providers & auth

| Provider | Credentials |
|---|---|
| `openai-codex` *(default)* | ChatGPT Plus/Pro via OAuth — `uv run wisp auth login openai-codex` |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |
| `fake` | None — deterministic offline provider for tests and smoke runs |

```bash
uv run wisp -p "hello" --provider anthropic --model claude-sonnet-5
```

Codex credentials are stored in `WISP_AUTH_FILE` (default `~/.wisp/auth.json`) with private
permissions.

In the TUI, `/model` with no arguments lists every catalog model grouped by provider. If a model id
belongs to only one registered provider, `/model <id>` switches providers to match; otherwise use
`/provider <name>` first.

### Model catalog

The packaged catalog lists current text-generation models that Wisp's streaming, client-tool
adapters can use. Catalog entries are **advisory, not access control** — model access varies by
account and region, and explicitly configured unknown models still pass through to the provider.

Context windows and compaction limits are provider-scoped: the direct `openai` API and the
`openai-codex` subscription can expose the same model id with different limits. Wisp uses the
earlier of the provider-recommended compaction limit and the configured reserve; provider metadata
can make the reserve more conservative but never weaken a larger user reserve.

Pricing is optional, effective-dated, and provider-scoped, and is used only to estimate new request
costs. Add account-specific models or negotiated rates in the user-only `~/.wisp/catalog.toml`
overlay — Wisp never reads a project-local catalog.

## Tools and safety

Wisp includes built-in local tools for reading files, editing files, searching projects, and
running shell commands. File tools are sandboxed to the tool context's working directory.

| Category | Tools | Approval |
|----------|-------|----------|
| **Read** | `read` · `grep` · `find` · `ls` | Runs directly |
| **Mutating** | `write` · `edit` | Required |
| **Command** | `bash` | Required |

`bash` defaults to one-shot execution and reports stdout, stderr, truncation state, and exit code.
It also accepts `operation=start|poll|cancel` for commands needing a retained process handle; those
return a `process_id`, process state, incremental output, and per-stream truncation metadata under
the same safety category and approval policy.

**Print mode exposes no tools unless you ask.** Read tools are enabled as a group; mutating and
command tools require per-tool opt-in:

```bash
uv run wisp -p "list files" --allow-read-tools
uv run wisp -p "run tests"  --allow-tool bash --yes
```

Because print mode is non-interactive, mutating and command tools are also blocked at execution
time unless you pass `--yes` (alias `--allow-unsafe-tool-execution`). Without it the model receives
a clear tool error instead of Wisp executing the operation.

Wisp does not cap model/tool rounds by default, matching Pi's permissive agent loop. Pass
`--max-tool-iterations <n>` for a non-interactive fuse.

Extensions may attach optional `ToolPromptMetadata` when calling `ExtensionAPI.register_tool(...)`.
Wisp adds that guidance only when the tool is actually exposed for the current run, de-duplicates
and bounds it, and keeps it separate from the provider-facing tool schema. The metadata is
descriptive — it cannot alter tool policy, sandboxing, protected paths, or approval requirements.

## Sessions

Wisp persists each run as a JSONL session and can continue an existing one:

```bash
uv run wisp -p "continue the work" --continue
uv run wisp -p "continue the work" --resume path/to/session.jsonl
uv run wisp -p "continue the work" --resume <session-id-prefix>
```

- `--continue` resumes the newest session in the active session directory.
- `--resume` accepts a JSONL path, filename, full session id, or unique id prefix.
- Sessions live under `~/.wisp/sessions`; override with `--session-dir` or `WISP_SESSION_DIR`.

Session files contain provider-facing `message` entries plus selected structured `event` entries
(tool calls, approvals, tool start/end, errors) for audit. They do **not** persist `message.delta`
events. Continuation replays only the selected path's messages and compactions, so audit events
never become model-visible history.

Records form a parent-linked tree, and an append-only active-leaf record selects the root-to-leaf
path used by continuation — abandoned or cancelled work stays in the audit log without entering
model context. Legacy unversioned and v1 linear session files remain readable and are never
rewritten on load.

The typed session API can derive a new session without rewriting its source: a **clone** copies the
complete active path, a **fork** copies the path before a selected user message and returns that
prompt for editing. Copied entries retain stable IDs, parent links, timestamps, and accounting
metadata under a new session ID. These are available to RPC clients via `clone_session` /
`fork_session`; direct CLI and TUI commands are not yet exposed.

> **Deprecated:** `wisp.agent.messages.SessionEntry(...)` remains available as a factory. New
> integrations should import the concrete entry models from `wisp.sessions`.

## Configuration

Wisp reads configuration from CLI flags, environment variables, and JSON settings files.

Precedence, highest to lowest:

```text
CLI flag > environment variable > project ./.wisp/settings.json > user ~/.wisp/settings.json > built-in default
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `WISP_PROVIDER` | Provider name: `openai-codex`, `openai`, `anthropic`, `google`, or `fake` |
| `WISP_MODEL` | Model override; blank uses the provider default |
| `WISP_MODE` | Default mode; set to `tui` to open the TUI directly |
| `WISP_TUI_RENDERER` | TUI renderer: `line`, `fullscreen`, or `textual` |
| `WISP_SESSION_DIR` | Session storage directory; defaults to `~/.wisp/sessions` |
| `WISP_AUTH_FILE` | Auth file path; defaults to `~/.wisp/auth.json` |
| `WISP_RETRY_MAX_RETRIES` | Provider retry count; defaults to `2`, set `0` to disable |
| `WISP_RETRY_BASE_DELAY_SECONDS` | Initial retry delay; defaults to `0.5` |
| `WISP_RETRY_MAX_DELAY_SECONDS` | Maximum retry delay; defaults to `30` |
| `WISP_CONTEXT_RESERVE_TOKENS` | Minimum tokens reserved outside estimated input context; defaults to `16384` |
| `WISP_AUTO_COMPACTION` | Automatic threshold compaction and overflow recovery; defaults to `true` |
| `OPENAI_API_KEY` · `ANTHROPIC_API_KEY` · `GOOGLE_API_KEY` | Required only for the matching provider |

### Settings files

For durable defaults, use a settings file. The user-level file lives at `~/.wisp/settings.json`; a
project may add `./.wisp/settings.json`, applied only after you trust the project.

```json
{
  "provider": "openai",
  "model": "gpt-5.6-sol",
  "effort": "high",
  "session_dir": "~/.wisp/sessions",
  "context_reserve_tokens": 16384,
  "auto_compaction_enabled": true,
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

Some fields are **user-only** and a project file can never set them: `protected_paths`, `retry`,
`effort`, `context_reserve_tokens`, and `auto_compaction_enabled`. A repository cannot increase your
API spending, prolong waits, or weaken the secret guard.

After a successful TUI `/model` or `/provider` change, Wisp atomically records the active provider,
model, and effort as user defaults, reused next launch unless a higher-precedence source overrides
them. Failed changes, trusted-project configuration, CLI flags, and external RPC configuration do
not rewrite these preferences.

Never commit auth files or real API keys.

> **Migration note:** Wisp no longer reads a project `.env` file. Move any values you kept there
> into your shell environment or `~/.wisp/settings.json`. A project `.env` on disk is still treated
> as a secret and is never surfaced to the model.

### Retry behavior

Wisp retries only requests that fail before the provider starts streaming, using bounded
exponential backoff with jitter. It honors reasonable `Retry-After` requests, emits retry progress
in JSON/RPC and the TUI, and never replays an already-started response.

OpenAI-family streams succeed only after the provider's native completion event. If a connection
ends first, Wisp reports a failed turn with any partial text and never executes buffered tool calls.
For Wisp-owned `openai-codex` connections, connect and pool waits are limited to 10 seconds,
request writes to 30 seconds, and response-header or between-chunk read inactivity to 300 seconds.
Caller-injected HTTP clients retain their caller-selected timeout policy.

## Project trust

Project-local settings, context files (`AGENTS.md` / `CLAUDE.md`), and project extensions are
loaded only after the project is trusted. Untrusted projects remain fully usable — Wisp simply
ignores their local configuration and instructions.

The first run in an untrusted directory asks `Do you trust the files in /path/to/project?`. Answer
yes and the decision is remembered globally in `~/.wisp/trust.json`, keyed by resolved path.

```bash
uv run wisp trust status [path]   # trusted, untrusted, or undecided
uv run wisp trust allow [path]    # persistently trust a project
uv run wisp trust revoke [path]   # persistently mark a project untrusted
uv run wisp trust forget [path]   # remove the decision so Wisp can prompt again
```

Security notes:

- **Non-interactive runs** (CI, scripts, standalone RPC) default to untrusted. The interactive TUI
  asks before entering the interface. Set `WISP_TRUST=1` to opt in for one process, or
  `WISP_TRUST=0` to force untrusted mode.
- `WISP_TRUST` is read only from the real process environment, never from project files, and is
  never persisted.
- `WISP_TRUST_FILE` may relocate the global trust store, but only to an absolute path outside the
  repository. A relative value is rejected.

## Context & compaction

Each turn sends a default coding-agent system prompt plus a bounded project-context message before
the user prompt: working directory, git branch and capped status summary, detected root files,
tools exposed to the model, and trusted project instructions.

Context files load from the trusted context root down to the working directory, parent instructions
first. In each directory Wisp uses the first Pi-compatible match: `AGENTS.md`, `AGENTS.MD`,
`CLAUDE.md`, `CLAUDE.MD`. Symlinked, protected, or out-of-scope files are skipped. Project
instructions are bounded separately from the tool list, so a large instruction file cannot hide the
available tools.

Project context is trust-gated — in untrusted projects Wisp reads no local instruction files or
settings. This is stricter than Pi, and keeps project guidance inside the same boundary as project
settings and future extensions.

### Accounting

Before each request Wisp emits `context.estimated`, a deterministic approximation of the system
prompt, active messages, pending tool results, and tool schemas (a conservative `ceil(chars / 4)`
heuristic). When the catalog provides a context window, the event also reports the reserve,
estimated percentage, remaining budget, and whether the estimate crossed it. Unknown models remain
permissive.

Provider-reported `usage.total_tokens` is kept separately as the authoritative observation for a
completed request. Session statistics sum provider totals exactly as reported and never reconstruct
totals from input/output categories.

### Compaction

`/compact [instructions]` replaces older provider-visible turns with a structured checkpoint while
retaining the latest complete user turn verbatim. The summary request uses the active provider,
model, and effort without tools. If the model cannot produce a complete summary, compaction fails
without changing replay.

Compaction is **append-only** and lossy only at replay time: original messages stay in the JSONL
audit log while later prompts receive the checkpoint plus retained recent context. Wisp never splits
a tool call from its result.

Automatic threshold compaction is enabled by default and runs after a completed prompt when active
context exceeds the reserved input budget. It triggers only when usage is strictly greater than
`context_window - context_reserve_tokens`. If an automatic summary fails, Wisp preserves the
completed prompt and leaves replay unchanged. Disable with `WISP_AUTO_COMPACTION=0` or
`"auto_compaction_enabled": false`.

When a provider explicitly rejects an input for context overflow, Wisp can compact and retry the
same prompt once. Recovery is skipped after mutating or command tools, or after deltas have already
reached an interface, because side effects and partial responses cannot be safely repeated.

## TUI

```bash
uv run wisp tui
```

A fullscreen Textual TUI built on the same RPC controller other integrations use. The footer shows
the working directory/session, status, queued follow-ups, provider/model, context use, and
cumulative cost.

- `ctx 12k/128k` is a current provider observation; `ctx ~12k/128k` is an estimate.
- `cost $0.042` is complete accounting; `cost ≥$0.042` includes unpriced requests. Estimates are
  not invoices — subscription-backed Codex, custom pricing, and unknown models remain unpriced.

Unlike print mode, **the TUI exposes the full tool registry by default** — otherwise it would be a
chatbot that can't read files or run commands. Mutating and command tools still pause for approval:
approve once, allow that tool for the session, YOLO all mutating/command tools for the process
(requires a second confirmation, never persisted), or deny.

### Slash commands

```text
/help                       show help
/auth [provider]            show credential status
/login [provider] [device-code]
/logout [provider]
/provider [provider]        switch provider (resets model to default)
/model [model] [effort]     switch model and optional reasoning effort
/new                        start a fresh session and clear the screen
/resume [session-id]        browse or resume a persisted session
/compact [instructions]     summarize older context while preserving the JSONL audit
/context [auto on|off]      show or toggle compaction policy
/plan                       switch to read-only planning mode
/build                      switch to normal build mode
/history                    search prompts submitted in this TUI run
/quit, /exit
```

Type `/` to filter commands inline. Type `@` to reference a project file — an inline picker filters
as you type, matching loosely so `@tuiapp` finds `src/wisp/tui/textual_app.py`. Only the path is
inserted; Wisp does not inline file contents, and the listing honors the same `protected_paths`
policy, so secrets are never offered.

### Keybindings

| Key | Action |
|---|---|
| `Enter` | Submit |
| `Shift+Enter` / `Ctrl+J` | Insert newline (`Ctrl+J` in the live fullscreen renderer) |
| `Shift+Tab` | Toggle plan/build mode |
| `Ctrl+G` | Toggle contextual help for the focused Textual surface |
| `Ctrl+R` | Search prompt history for this TUI run |
| `Escape` | Dismiss nearest menu or overlay, then cancel an active prompt |
| `Ctrl+C` | Copy selection; otherwise press twice within 1.5s to quit |
| `Ctrl+D` | Delete right; EOF only from an empty editor |

In the Textual TUI, `Ctrl+G` and `/help` open the same native contextual guide. It follows focus
across the editor, tool cards, pickers, context reports, and safety decisions; its key reference is
derived from live bindings. The panel moves below the conversation on narrow terminals and never
runs a tool, changes the session, or resolves an approval. Line and fallback fullscreen modes keep
their textual `/help` summary.

Prompt history holds up to 100 unique prompts and is **memory-only** — never written to session
JSONL, configuration, or a cache, so prompts containing secrets are not silently persisted.

**Plan mode** applies to future prompts in the current process. It exposes only read-only tools that
were already authorized at startup; `write`, `edit`, `bash`, and non-read extension tools are
unavailable. Use `/build` to restore. The mode is not persisted in session JSONL.

**`/new`** preserves the current JSONL session for `/resume`, clears the transcript and screen, and
creates the next session lazily. Provider, model, effort, mode, tool permissions, trust, and
compaction settings are retained.

### Flags and renderers

```bash
uv run wisp tui --continue
uv run wisp tui --resume <session-id-prefix>
uv run wisp tui --no-all-tools                  # opt-in tool filter instead of the full registry
uv run wisp tui --yes                           # auto-approve mutating/command tools
uv run wisp tui --line                          # simple line renderer, for fallback/debugging
```

On `--continue` or `--resume`, the TUI hydrates up to 500 active-path persisted messages through the
same RPC `get_messages` command available to other frontends before accepting input.

The Textual TUI targets truecolor terminals and degrades gracefully — 256-color and 16-color
terminals are handled by Textual's own detection. Setting `NO_COLOR` switches to deterministic
grayscale.

The legacy `--mode tui` entrypoint remains for compatibility and honors
`--tui-renderer line|fullscreen|textual` plus `WISP_TUI_RENDERER`.

## Development

```bash
uv sync                                                              # install
uv run ruff format --check . && uv run ruff check . && uv run mypy   # quality gates
uv run pytest tests                                                  # complete suite
uv run pytest tests -m 'not (slow or tui or process or benchmark)'   # faster core checks
```

The complete suite runs against the deterministic `fake` provider, so the agent core, CLI, and JSONL
sessions are exercised without API keys or network access. Run the complete command before
considering a change verified.

The coding runtime is split into explicit inward-facing layers:

```text
CLI / JSONL-RPC / SDK adapters → RPC command host → CodingSession → AgentHarness → run_agent_loop
```

Each layer adds exactly one concern, and the TUI consumes the same runtime through RPC events —
there is only ever one agent loop. Local agent instruction files remain untracked so contributors
can tailor them to their own workflows.

## License

See [LICENSE](LICENSE).
