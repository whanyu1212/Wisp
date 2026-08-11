<p align="center">
  <img src="https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner.png" alt="Wisp — A Python coding agent that stays in sync." width="100%">
</p>

# Wisp

<p align="center">
  <strong>A terminal-first coding agent with one typed, event-driven core.</strong>
</p>

<p align="center">
  <a href="#install">Install</a>
  ·
  <a href="#quickstart">Quickstart</a>
  ·
  <a href="#architecture">Architecture</a>
  ·
  <a href="https://pypi.org/project/wisp-ai/">PyPI</a>
  ·
  <a href="https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md">Changelog</a>
  ·
  <a href="https://github.com/whanyu1212/Wisp/issues">Issues</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/wisp-ai/"><img src="https://img.shields.io/pypi/v/wisp-ai?label=PyPI" alt="PyPI version" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12+" /></a>
  <a href="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml"><img src="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://github.com/whanyu1212/Wisp/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License" /></a>
</p>

> **Beta status:** Wisp is under active development. Interfaces may change while the runtime and
> TUI stabilize.

## What is Wisp?

**Wisp is a coding agent that runs in your terminal.** Ask it to inspect a repository, explain an
architecture, edit code, run commands, or continue a previous session. Wisp streams its work into a
fullscreen Textual interface and keeps an inspectable JSONL record of the conversation and tool
activity.

Wisp is also an embeddable Python runtime. Its CLI, TUI, JSONL RPC process, and in-process SDK all
drive the same typed command host and agent loop rather than maintaining separate implementations.

Wisp takes behavioral inspiration from Pi while putting explicit trust, protected-path, approval,
and persistence boundaries around local coding-agent work.

## Install

Wisp is published on PyPI as `wisp-ai`, installs a `wisp` command, and requires Python 3.12 or
newer. The current release is a beta, so request it explicitly:

```bash
uv tool install "wisp-ai==0.1.0b2"
```

If `wisp` is not on your `PATH`, run `uv tool update-shell` once and restart your shell.

To run Wisp without installing it:

```bash
uvx --from "wisp-ai==0.1.0b2" wisp
```

Check the installed version with `wisp --version`.

Installed builds check PyPI at most once every six hours after TUI startup. When a newer applicable
release is available, Wisp prints a non-blocking update command; it never installs updates
automatically. Run `wisp update --check` to bypass the cache and check immediately, or `wisp update`
to check and confirm installation. Set `WISP_UPDATE_CHECK=0` to disable automatic checks; explicit
checks still run. Automatic installation is available only when Wisp is running from a persistent
`uv tool` installation; `uvx`, local-source, and other package-manager installs are never replaced.

## Quickstart

Run Wisp from the project you want it to work on:

```bash
cd path/to/project
wisp
```

Wisp defaults to OpenAI Codex subscription access. From the TUI, open the provider connection panel:

```text
/connect
```

Select **OpenAI**, then **ChatGPT Plus/Pro**, and complete the displayed device-code flow. The same
panel accepts masked API keys for OpenAI, Anthropic, and Google. Then enter a request such as:

```text
explain the architecture of this repository
```

For one-shot prompts and scripts, use print mode:

```bash
wisp -p "summarize the current changes"
```

An offline smoke test is available without credentials or network model calls:

```bash
wisp -p "hello" --provider fake
```

## What Wisp can do

- Fullscreen Textual TUI plus text, JSONL, and RPC modes.
- Built-in `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls` tools.
- OpenAI Codex, OpenAI API, Anthropic, Google, and deterministic fake providers.
- Append-only JSONL sessions with resume, branching, compaction, usage, and cost accounting.
- Project instructions from trusted `AGENTS.md` and `CLAUDE.md` files.
- Protected secret paths, cwd-constrained file tools, and explicit unsafe-tool approvals.
- Typed RPC and in-process SDK surfaces for custom frontends and integrations.

## Architecture

Wisp has one event-driven runtime shared by every interface:

```text
CLI / JSONL-RPC / SDK adapters → RPC command host → CodingSession → AgentHarness → run_agent_loop
```

Each layer adds one concern. The provider/tool cycle does not know about sessions or frontends; the
harness owns in-memory conversation state; the coding session adds persistence and safety policy;
and interfaces consume typed `WispEvent` values. The TUI is an RPC client, not a second agent loop.

## Interfaces

| Mode | Command | Output | Best for |
|------|---------|--------|----------|
| **TUI** | `wisp` (or `wisp tui`) | Fullscreen Textual UI | Interactive development |
| **Print** | `wisp -p "…"` | Assistant text on stdout, events on stderr | One-shot prompts and scripts |
| **JSON** | `wisp -p "…" --mode json` | One `WispEvent` JSON object per line | Machine-readable automation |
| **RPC** | `wisp --mode rpc` | Typed JSONL commands and events | Long-lived integrations |

JSON mode writes every event as one JSON object per line. RPC mode and the in-process SDK expose the
same command, event, session, trust, and approval contracts used by the built-in interfaces.

RPC command IDs must be unique while a command is running or queued. A duplicate is rejected before
dispatch under a fresh server-generated ID, with the conflicting requested ID named in the error;
this keeps every outstanding ID correlated with exactly one terminal `rpc.command.finished` event.
An ID may be reused after its earlier completion has been processed. Ordinary command exceptions
produce a failed terminal event and leave the long-running host available for later commands.

## Providers & auth

| Provider | Credentials |
|---|---|
| `openai-codex` *(default)* | ChatGPT Plus/Pro via device-code OAuth — TUI `/connect` |
| `openai` | Stored API key or `OPENAI_API_KEY` |
| `anthropic` | Stored API key or `ANTHROPIC_API_KEY` |
| `google` | Stored API key, `GOOGLE_API_KEY`, or `GEMINI_API_KEY` |
| `fake` | None — deterministic offline provider for tests and smoke runs |

```bash
wisp -p "hello" --provider anthropic --model claude-sonnet-5
```

Credentials entered through `/connect` are stored in `WISP_AUTH_FILE` (default
`~/.wisp/auth.json`) with private permissions. Explicit provider constructor keys take precedence,
followed by environment variables and then stored keys. Secrets entered in the panel are masked and
never enter prompt history, transcripts, RPC events, or session JSONL.

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
wisp -p "list files" --allow-read-tools
wisp -p "run tests"  --allow-tool bash --yes
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

### MCP tools

Wisp can connect to user-configured [Model Context Protocol](https://modelcontextprotocol.io/)
servers over stdio. Add servers only to the user settings file at `~/.wisp/settings.json`:

```json
{
  "mcp_servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"READ_ONLY": "1"},
      "env_from": ["GITHUB_TOKEN"],
      "tool_safety": {"search_repositories": "read"}
    }
  }
}
```

`env` contains literal user-owned values. `env_from` forwards only the named variables from Wisp's
process environment; if one is missing, that server is skipped. Server processes otherwise receive
only the MCP SDK's small safe environment baseline, run from the user's home directory rather than
the active project, and have stderr suppressed. Commands, arguments, environment values, stderr,
and transport errors are never included in MCP startup diagnostics.

Discovered tools are named `mcp__<server>__<tool>`, with deterministic normalization and hashing
when needed. They follow the same exposure flags as built-ins: use `--allow-tool <name>` or
`--all-tools`, while `--allow-read-tools` also includes MCP tools explicitly assigned `read` safety.
Remote tools default to `command` safety and require approval; server-provided annotations cannot
weaken this policy. `tool_safety` is the only way to assign `read` or `mutating` safety and matches
the remote tool name exactly.

Startup is failure-isolated: an unavailable or malformed server produces a sanitized error event
while healthy servers and built-in tools remain available. Wisp accepts at most 16 configured
servers, 64 discovery pages and 64 tools per server, 256 MCP tools overall, 1 MiB of definitions per
server, 4 MiB overall, and 2 MiB per protocol frame before parsing. Connection and discovery have a
10-second per-server deadline. A server's catalog is registered atomically, so invalid definitions,
duplicate names, collisions, or limit violations expose none of that server's tools.

Run `/mcp` in the TUI to inspect configured server status, registered tool names, and sanitized
startup failures. The command reads the current runtime snapshot and does not reconnect servers.

The 16-server ceiling is intentional: every stdio server is a separate local process, so startup
time and memory use grow with the number and implementation of the configured servers. Wisp may
revisit this limit when it can avoid eagerly starting every local server, rather than raising it
without a lifecycle or lazy-start solution.

Current MCP support covers stdio tool discovery and bounded text results. Resources, prompts,
dynamic `tools/list_changed` updates, HTTP/SSE transports, OAuth, and interactive authentication are
not yet supported.

## Sessions

Wisp persists each run as a JSONL session and can continue an existing one:

```bash
wisp -p "continue the work" --continue
wisp -p "continue the work" --resume path/to/session.jsonl
wisp -p "continue the work" --resume <session-id-prefix>
```

- `--continue` resumes the newest session in the active session directory.
- `--resume` accepts a JSONL path, filename, full session id, or unique id prefix.
- Sessions live under `~/.wisp/sessions`; override with `--session-dir` or `WISP_SESSION_DIR`.

Session files contain provider-facing `message` entries plus selected structured `event` entries
(tool calls, approvals, tool start/end, errors) for audit. They do **not** persist `message.delta`
events. Continuation replays only the selected path's messages and compactions, so audit events
never become model-visible history.

Wisp treats a JSONL record as committed only when it is newline-terminated. A successful append
also synchronizes the session file before returning. Appends are serialized across cooperating Wisp
processes and rolled back to the previous committed size if writing or synchronization fails. On the
next read, Wisp discards any unterminated final bytes left by an interrupted writer—even if those
bytes happen to form valid JSON—while preserving all newline-terminated records. A malformed
newline-terminated record remains a session error rather than being silently removed. Newly created
session files and recovery deletions also synchronize the parent directory on supported POSIX
systems.

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
| `WISP_UPDATE_CHECK` | Six-hour non-blocking PyPI update notice; defaults to `true` |
| `OPENAI_API_KEY` · `ANTHROPIC_API_KEY` · `GOOGLE_API_KEY` · `GEMINI_API_KEY` | Required only for the matching provider |

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
  "update_check_enabled": true,
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

Some fields are **user-only** and a project file can never set them: `protected_paths`, `retry`,
`effort`, `context_reserve_tokens`, `auto_compaction_enabled`, `update_check_enabled`, and
`mcp_servers`. A repository cannot increase your API spending, prolong waits, trigger network update
checks, launch an MCP command, receive forwarded credentials, or weaken the secret guard.

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

Project-local settings, context files (`AGENTS.md` / `CLAUDE.md`), skills, and project extensions
are loaded only after the project is trusted. Untrusted projects remain fully usable — Wisp simply
ignores their local configuration and instructions.

The first run in an untrusted directory asks `Do you trust the files in /path/to/project?`. Answer
yes and the decision is remembered globally in `~/.wisp/trust.json`, keyed by resolved path.

```bash
wisp trust status [path]   # trusted, untrusted, or undecided
wisp trust allow [path]    # persistently trust a project
wisp trust revoke [path]   # persistently mark a project untrusted
wisp trust forget [path]   # remove the decision so Wisp can prompt again
```

Security notes:

- **Non-interactive runs** (CI, scripts, standalone RPC) default to untrusted. The interactive TUI
  asks before entering the interface. Set `WISP_TRUST=1` to opt in for one process, or
  `WISP_TRUST=0` to force untrusted mode.
- `WISP_TRUST` is read only from the real process environment, never from project files, and is
  never persisted.
- `WISP_TRUST_FILE` may relocate the global trust store, but only to an absolute path outside the
  repository. A relative value is rejected.

## Agent Skills

Wisp discovers metadata from directories that follow the
[Agent Skills specification](https://agentskills.io/specification). Inspect the current catalog and
isolated validation diagnostics with:

```bash
wisp skills [project-path]
```

| Precedence | Location |
|---|---|
| 1 (highest) | `<project>/.wisp/skills/<name>/SKILL.md` |
| 2 | `<project>/.agents/skills/<name>/SKILL.md` |
| 3 | `~/.wisp/skills/<name>/SKILL.md` |
| 4 | `~/.agents/skills/<name>/SKILL.md` |

Each `SKILL.md` must begin with bounded YAML frontmatter containing a specification-valid `name`
and `description`; the declared name must match its parent directory. Invalid skills are skipped
individually and reported without hiding valid entries. Symlinked, protected, out-of-root, and
oversized metadata is rejected. Project locations are not scanned until project trust is granted;
user locations remain available in untrusted projects.

For a complete opt-in example, including installation instructions and a progressively loaded
review checklist, see [`examples/skills/wisp-code-review`](examples/skills/wisp-code-review/).

When the read-only `skill` tool is exposed, Wisp adds a separately bounded index of escaped skill
names and descriptions to model context. The model can call `skill` with `name` to load the selected
`SKILL.md` instructions, or add a forward-slash `resource` path to read a supporting file inside the
same skill directory. Enable it with `--allow-read-tools`, `--allow-tool skill`, or `--all-tools`,
following the same exposure rules as other tools. Print mode continues to expose no tools unless
one of those options is selected.

Instruction and resource reads are UTF-8, bounded, protected-path aware, and reject absolute paths,
traversal, symlinks, junctions, non-regular files, and targets outside the selected skill. Absolute
skill paths are not shown to the model. Bundled scripts are returned only as text and never execute
automatically; execution still requires the normal command tool and approval policy. The optional
`allowed-tools` metadata field is descriptive only and cannot grant tool access or approval.

The active operation keeps one immutable catalog snapshot. First-time project trust refreshes that
snapshot before the pending provider request begins. Invoke a cataloged skill explicitly from any
CLI, JSON/RPC, SDK, or TUI prompt flow with:

```text
/skill:<name> [additional instructions]
```

The directive must begin at the first character; names are case-sensitive, and the optional request
may span multiple lines. Wisp securely loads the bounded `SKILL.md` body, expands it into the
provider-visible user message, and applies the same policy to initial prompts, steering, and
follow-ups. Explicit invocation does not require exposing the `skill` tool and does not grant tool
access or approval.

The TUI fetches the active immutable snapshot at startup. Type `/skill:` to see deterministic
prefix completions for the available names, or run `/skills` to inspect the cached catalog and its
discovery diagnostics without rescanning the filesystem. Both surfaces refresh after first-time
project trust is applied and remain available while a prompt is running. Skill descriptions,
diagnostics, paths, and requests are displayed as literal text rather than terminal markup.

Sessions retain the exact submitted directive, additional request, instruction-content SHA-256,
truncation state, and provider-visible expansion as typed data. Replay uses that persisted expansion
even if the source skill later changes or disappears; a new invocation reads the current resource
and records a new hash. Live and restored TUI transcripts show a compact invocation row from that
typed metadata instead of exposing the provider-visible expansion. Skill installation, hot reload,
bundled-script execution, fuzzy completion, and skill-management UI remain unsupported.

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
wisp
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
/connect [provider]          connect a provider or open the provider panel
/disconnect [provider]       remove stored credentials (`/logout` alias)
/provider [provider]        switch provider (resets model to default)
/model [model] [effort]     switch model and optional reasoning effort
/new                        start a fresh session and clear the screen
/resume [session-id]        browse or resume a persisted session
/compact [instructions]     summarize older context while preserving the JSONL audit
/context [auto on|off]      show or toggle compaction policy
/plan                       switch to read-only planning mode
/build                      switch to normal build mode
/history                    search prompts submitted in this TUI run
/update [check|install]     check immediately or explicitly install an update
/skills                     inspect loaded skills and discovery diagnostics
/mcp                        show configured MCP servers and registered tools
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
wisp tui --continue
wisp tui --resume <session-id-prefix>
wisp tui --no-all-tools                  # opt-in tool filter instead of the full registry
wisp tui --yes                           # auto-approve mutating/command tools
wisp tui --line                          # simple line renderer, for fallback/debugging
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

Changes should preserve the layer boundaries described in [Architecture](#architecture). Local
agent instruction files remain untracked so contributors can tailor them to their own workflows.

## License

See [LICENSE](https://github.com/whanyu1212/Wisp/blob/main/LICENSE).
