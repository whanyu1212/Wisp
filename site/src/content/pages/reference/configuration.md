---
title: Configuration
description: Wisp reads configuration from CLI flags, environment variables, and JSON settings files.
route: reference/configuration.html
section: reference
order: 28
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
  "codex_read_timeout_seconds": 300,
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

Malformed settings files are skipped with a warning, never fatal.

`codex_read_timeout_seconds` bounds read inactivity on Wisp-owned `openai-codex` connections,
not the total response duration. It defaults to `300`; use `600` for longer waits. Values must be
positive and finite. `WISP_CODEX_READ_TIMEOUT_SECONDS` overrides the user setting; an explicit SDK
`WispConfig.from_env(codex_read_timeout_seconds=...)` argument overrides both. There is no dedicated
CLI flag. See [Provider retry behavior](../guide/providers.md#retry-behavior) for recovery details.

## User-only fields

Some fields are **user-only** and a project file can never set them:

`protected_paths` · `retry` · `codex_read_timeout_seconds` · `effort` · `context_reserve_tokens` ·
`auto_compaction_enabled` · `web_search` · `mcp_servers` · `openai_compatible`

A repository cannot increase your API spending, prolong waits, launch an MCP command, configure a
credential-receiving provider endpoint, or weaken the secret guard.

Project settings may set `provider`, `model`, `session_dir`, and `auth_path` after you trust the
project. In particular, `auth_path` can redirect credentials entered through `/connect` to a path
inside the working tree. Inspect trusted project settings before authenticating, pass `--auth-file`
or set `WISP_AUTH_FILE` for a deliberate higher-precedence override, and never commit the selected
auth file.

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
