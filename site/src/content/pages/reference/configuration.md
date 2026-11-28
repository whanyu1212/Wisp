---
title: Configuration
description: Settings precedence across CLI flags, environment variables, project .wisp/settings.json, and user ~/.wisp/settings.json.
route: reference/configuration.html
section: reference
order: 2
layout: docs
listed: true
searchable: true
---

# Configuration

Wisp reads configuration from CLI flags, environment variables, and JSON settings files.

Precedence, highest to lowest:

```text
CLI flag > environment variable > project ./.wisp/settings.json > user ~/.wisp/settings.json > built-in default
```

## Settings files

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
  "web_search": false,
  "wake_on_process_exit": true,
  "codex_read_timeout_seconds": 300,
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

At startup, malformed settings files are skipped with a warning rather than making Wisp unusable.
Manual [`/reload`](../guide/tui/commands.md#reloading-settings) is stricter: it rejects malformed or
unreadable files and keeps the live configuration. Supported settings apply only while idle;
explicit session choices and CLI/environment precedence are preserved. The reload report names
changed startup-only settings that still require a restart, without printing their values.

`wake_on_process_exit` lets a background process that stops while the session is idle start a run
by itself. Left unset, it is on in the CLI and TUI and off for in-process SDK hosts.

`codex_read_timeout_seconds` bounds read inactivity on Wisp-owned `openai-codex` connections,
not the total response duration. It defaults to `300`; use `600` for longer waits. Values must be
positive and finite. `WISP_CODEX_READ_TIMEOUT_SECONDS` overrides the user setting; an explicit SDK
`WispConfig.from_env(codex_read_timeout_seconds=...)` argument overrides both. There is no dedicated
CLI flag. See [Provider retry behavior](../guide/providers.md#retry-behavior) for recovery details.

## User-only fields

Some fields are **user-only** and a project file can never set them:

`protected_paths` · `retry` · `codex_read_timeout_seconds` · `effort` · `context_reserve_tokens` ·
`auto_compaction_enabled` · `web_search` · `wake_on_process_exit` · `codemode` · `codemode_presentation` · `subagents` ·
`subagent_model` ·
`mcp_servers` · `mcp_tool_exposure` · `openai_compatible` · `tui_keybindings` · `tui_images` ·
`browser_search_engine` · `browser_default`

A repository cannot increase your API spending, prolong waits, launch an MCP command, configure a
credential-receiving provider endpoint, or weaken the secret guard.

Project settings may set `provider`, `model`, `session_dir`, and `auth_path` after you trust the
project. In particular, `auth_path` can redirect credentials entered through `/connect` to a path
inside the working tree. Inspect trusted project settings before authenticating, pass `--auth-file`
or set `WISP_AUTH_FILE` for a deliberate higher-precedence override, and never commit the selected
auth file.

## Structured user settings

These settings belong in `~/.wisp/settings.json`, not the project file:

| Field | Format and example |
|---|---|
| `tui_images` | Preferred terminal image protocol: `auto` (default), `kitty-unicode`, `kitty`, `iterm2`, `sixel`, or `halfblock`. Restart Wisp to apply. `WISP_TUI_IMAGES` overrides this setting. |
| `browser_search_engine` | Where the live browser searches text you type that is not an address: `google` (default), `duckduckgo`, or `bing`. Restart Wisp to apply. |
| `browser_default` | Which browser the agent prefers when both Wisp's live browser and a browser MCP server are available: `live` (default), `mcp`, or `none`. `WISP_BROWSER_DEFAULT` overrides this setting. |
| `tui_keybindings` | Object mapping action IDs to arrays of key chords. See [Keybinding preferences](../guide/tui/keybindings.md#keybinding-preferences) for an example, available actions, and restart requirements. |
| `mcp_servers` | Object keyed by server name, with either a stdio `command` or an HTTP `url` per entry. See [MCP servers](../guide/mcp.md#add-a-server) for complete JSON examples and authentication options. |
| `codemode_presentation` | User-only model presentation: `"direct_and_codemode"` (default) or `"codemode_only"`. Separate from availability; falls back to direct tools when codemode is not offered. |
| `mcp_tool_exposure` · `codemode` | Discovery mode (`"auto"`, `"direct"`, or `"on_demand"`) and script availability (`true`, `false`, or automatic when omitted/`null`). See [MCP with codemode](../guide/mcp.md#mcp-with-codemode) for their interaction and prerequisites. |
| `openai_compatible` | Object with `provider_name`, `base_url`, and `default_model`, plus optional `requires_api_key` and `ca_bundle`. See [OpenAI-compatible endpoints](../guide/providers.md#openai-compatible-endpoints) for hosted and local examples. |
| `protected_paths` | Array of path-pattern strings, replacing the default list rather than extending it. See the example below and [Protected paths](../guide/tools-and-safety.md#protected-paths) for limits. |

For example, this replaces the default protected-path patterns with a custom list:

```json
{
  "protected_paths": [".env", "*.pem", "*.key", "private/credentials.json"]
}
```

Bare patterns match a basename at any depth; patterns containing `/` match a path suffix. Replacing
this list drops defaults you do not repeat. `[]` disables the configurable patterns. Wisp always
protects its credentials, settings, permissions, and saved browser data. These rules apply to file
tools, not `bash` or MCP tools; they are not a sandbox.

## Saved live browser profile

Wisp's live browser keeps site sign-ins, cookies, and other browser data across launches in its
own profile, `~/.wisp/browser-profile`. The profile is separate from your everyday Chrome, so sign
in inside Wisp's live tab to use a site. Keeping this data also means sites that check for bots
challenge the live tab less often, and a challenge you solve stays solved.

The profile is shared across your Wisp sessions and projects. Only one Wisp instance can use it at
a time. On macOS, Chrome may ask for Keychain access to encrypt and restore saved browser data.
The profile's permissions protect saved data on disk, and Chrome exposes its debugger on the
loopback network interface while the live tab is open.

The profile also holds the last 20 addresses you opened in the live tab, which the Browser view
offers again; searches are not kept.

To clear the saved data, close the live browser and run `wisp browser clear`. This removes Wisp's
local copy, including the recent addresses, and does not sign you out on the sites themselves.

## Remembered preferences

After a successful TUI `/model` or `/provider` change, Wisp atomically records the active provider,
model, and effort as user defaults, reused next launch unless a higher-precedence source overrides
them. Failed changes, trusted-project configuration, CLI flags, and external RPC configuration do not
rewrite these preferences. If `~/.wisp/settings.json` is not valid JSON, Wisp leaves it untouched and
warns that the preference was not saved; fix the file to resume saving.

## Tool, MCP, codemode, and subagent limits

Exact operational caps, timeouts, and storage limits referenced from [Tools & safety](../guide/tools-and-safety.md) and [MCP servers & codemode](../guide/mcp.md):

### File, search, and shell tools

| Surface | Limit or contract |
|---|---|
| `grep`, `find`, `ls` | `30 s` search deadline (returns partial results); fails after `5 s` extra if a filesystem call remains blocked; max `100` directory levels below start |
| `edit` | Refuses files over `16 MiB`; requires prior read in the current conversation with no unseen modification |
| `bash` timeouts | One-shot `timeout` ≤ `3,600 s`; started process `lifetime_seconds` ≤ `86,400 s` (24 h); `yield_seconds` / `wait_seconds` ≤ `600 s`; backgrounded via `Ctrl+B` runs up to `1 h` |
| Auto-wake on process exit | At most `3` automatic wake runs between user prompts (`wake_on_process_exit` / `WISP_WAKE_ON_PROCESS_EXIT`; on by default in CLI/TUI, off in in-process SDK) |
| Live shell progress | Starts after `0.5 s`, throttled to ≤ `4` updates/s when output changes; transient (`tool.execution.progress`), never sent to the model or saved |
| Truncated command preview | Default tool output budget `50,000` bytes / `2,000` lines; head `64` lines (≤ `4,000` bytes) + tail `192` lines (≤ `8,000` bytes), scaled down for smaller budgets (tail-only below ~`1,000` bytes or `8` lines) |
| Saved output (`read_output`) | First `8 MiB` per stream (stdout/stderr stored separately), max `64 MiB` and `32` saved streams per runtime in private `~/.wisp/tool-output/`, deleted on exit (or by next process after crash) |
| Environment & process cleanup | Withholds provider API key variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, and custom endpoint key vars). Injects random `WISP_COMMAND_TOKEN` to clean up detached children; macOS/Linux watchdog cleans up managed processes on crash/`SIGKILL` (Windows crash leaves them running) |

### macOS `Auto commands` (Seatbelt & reviewer)

| Surface | Limit or contract |
|---|---|
| Consent & platform | Separate `sandbox_commands_v1` consent; macOS only with Seatbelt available (earlier `command_review_v1` is not reused) |
| Fast-path test/build runners | Supported `pytest`, `python -m pytest`/`unittest`, `uv run --no-sync pytest`/`unittest`, `npm test`/`run test`/`run build`, and Cargo `test`/`check`/`build` run without model review. Test fast paths (`cargo test` requires an ancestor `Cargo.lock`) run with a read-only workspace, fresh per-command `TMPDIR`/`HOME`/`UV_CACHE_DIR` deleted on exit, and an isolated session Cargo target cache |
| Model reviewer | Fresh no-tools request on the active provider/model (receives user request, exact arguments, and `cwd` only—no conversation/instructions/files); max `8` review requests per run, `20 s` deadline each |
| Containment & environment | Controlled env (`PYTHONUNBUFFERED=1`, `CARGO_NET_OFFLINE=true`); denies `chflags` and TCP/UDP network access; Unix sockets allowed only inside private `TMPDIR`; fails closed to human approval if sandboxing is unavailable (never retries unrestricted). Known limitation: pre-existing hardlinks under allowed names are not isolated |
| `--auto-command-read-dir` | Grants one absolute directory for the initial session in the launch project: allows `/bin/ls` on one directory or `/bin/cat` on one regular file (no flags, workspace `cwd`); excludes symlinks, hardlinks, and protected paths; expires on session/project switch or leaving Auto commands |

### MCP servers, OAuth, and discovery

| Surface | Limit or contract |
|---|---|
| Configured catalog caps | Max `16` servers, `64` discovery pages/server, `256` MCP tools overall, `1 MiB` definitions/server, `4 MiB` overall, `2 MiB` per protocol frame before parsing; `10 s` per-server connection/discovery deadline; atomic per-server registration |
| Transport & HTTP rules | Exactly one transport per server (`command` stdio or `url` Streamable HTTP). Remote URLs require HTTPS (HTTP allowed only on loopback) with no credentials, query string, or fragment; redirects are not followed; responses must be uncompressed |
| Headers & stdio env | `headers_from` allows only `"${VAR}"` or `"<Scheme> ${VAR}"`; headers go only to the configured URL origin and cannot override transport-managed headers (`Host`, `Accept-Encoding`, `Mcp-Session-Id`). Stdio servers run from `$HOME` with the MCP SDK's minimal safe baseline env plus `env`/`env_from` and suppressed stderr |
| OAuth sign-in | `5 min` wait for one-time `http://127.0.0.1` PKCE callback; tokens stored in private `~/.wisp/mcp-auth.json`, bound to the server URL/origin, auto-refreshed; pre-registered OAuth clients are not supported |
| Discovery & hints | `mcp_tool_exposure: "auto"` switches to on-demand at `2,000` estimated tokens of eligible MCP definitions. In `/mcp`, `h` marks up to `8` `readOnlyHint` tools at once after confirmation |

### Codemode and subagents

| Surface | Limit or contract |
|---|---|
| Codemode execution | Max `200` tool calls, `2 min` wall clock (includes approval waits), `10 s` script CPU time, `128 MiB` memory, `1 MiB` combined printed + return output per script; runs in isolated `monty` process (no filesystem, network, env, or third-party imports); automatic overflow replay is disabled |
| Codemode nested results & discovery | Nested tool results cap at `512 KiB` and `50,000` lines (marked `[result incomplete]` when truncated); structured MCP output schemas also cap at `512 KiB`. `tool_search` returns `8` results by default (max `20`); `tool_search`/`tool_describe` JSON responses cap at `16 KiB` with `omitted_fields` + JSON Pointer `schema_path` paging |
| Codemode branch state (`store`/`load`/`delete`) | Max `128` keys/branch (`128`-byte UTF-8 key names), `64 KiB`/value, `256 KiB` total compact UTF-8 JSON, max `32` nested containers; transactional for state only (file/shell/MCP side effects are never rolled back); commits only when the script succeeds and saves |
| Codemode saved output | Shares the `read_output` store (`32` outputs, `8 MiB`/output, `64 MiB` total, process-local); supports 1-indexed `offset`/`limit` or 0-indexed `character_offset`/`character_limit` |
| Subagents (`spawn_agent`) | Up to `8` tasks per call (`4` concurrent), max `63` tool rounds per subagent, `500`-word target / `8,000`-byte report truncation cap; read-only tools only, text-only (images omitted); model usage billed to session totals |

## Secrets

Never commit auth files or real API keys.

> [!WARNING]
> **Migration note**
>
> Wisp no longer reads a project `.env` file. Move any values you kept there into your shell
> environment or `~/.wisp/settings.json`. A project `.env` on disk is still treated as a secret and is
> never surfaced to the model.

See also [Environment variables](environment.md) and
[Tools & safety](../guide/tools-and-safety.md#project-trust).
