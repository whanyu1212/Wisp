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

Malformed settings files are skipped with a warning, never fatal.

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
`auto_compaction_enabled` · `web_search` · `wake_on_process_exit` · `codemode` · `subagents` ·
`subagent_model` ·
`mcp_servers` · `mcp_tool_exposure` · `openai_compatible` · `tui_keybindings`

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
| `tui_keybindings` | Object mapping action IDs to arrays of key chords. See [Keybinding preferences](../guide/tui/keybindings.md#keybinding-preferences) for an example, available actions, and restart requirements. |
| `mcp_servers` | Object keyed by server name, with either a stdio `command` or an HTTP `url` per entry. See [MCP servers](../guide/tools-and-safety.md#mcp-tools) for complete JSON examples and authentication options. |
| `openai_compatible` | Object with `provider_name`, `base_url`, and `default_model`, plus optional `requires_api_key` and `ca_bundle`. See [OpenAI-compatible endpoints](../guide/providers.md#openai-compatible-endpoints) for hosted and local examples. |
| `protected_paths` | Array of path-pattern strings, replacing the default list rather than extending it. See the example below and [Protected paths](../guide/tools-and-safety.md#protected-paths) for limits. |

For example, this replaces the default protected-path patterns with a custom list:

```json
{
  "protected_paths": [".env", "*.pem", "*.key", "private/credentials.json"]
}
```

Bare patterns match a basename at any depth; patterns containing `/` match a path suffix. Replacing
this list drops defaults you do not repeat. `[]` disables the configurable patterns, but Wisp's
credential, settings, and permission files remain protected. These rules apply to file tools, not
`bash` or MCP tools; they are not a sandbox.

## Remembered preferences

After a successful TUI `/model` or `/provider` change, Wisp atomically records the active provider,
model, and effort as user defaults, reused next launch unless a higher-precedence source overrides
them. Failed changes, trusted-project configuration, CLI flags, and external RPC configuration do not
rewrite these preferences. If `~/.wisp/settings.json` is not valid JSON, Wisp leaves it untouched and
warns that the preference was not saved; fix the file to resume saving.

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
