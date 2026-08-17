---
title: Configuration
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
  "update_check_enabled": true,
  "retry": { "max_retries": 2, "base_delay_seconds": 0.5, "max_delay_seconds": 30 }
}
```

Malformed settings files are skipped with a warning, never fatal.

## User-only fields

Some fields are **user-only** and a project file can never set them:

`protected_paths` · `retry` · `effort` · `context_reserve_tokens` · `auto_compaction_enabled` ·
`update_check_enabled` · `mcp_servers` · `openai_compatible`

A repository cannot increase your API spending, prolong waits, trigger network update checks, launch
an MCP command, receive forwarded credentials, or weaken the secret guard.

## Remembered preferences

After a successful TUI `/model` or `/provider` change, Wisp atomically records the active provider,
model, and effort as user defaults, reused next launch unless a higher-precedence source overrides
them. Failed changes, trusted-project configuration, CLI flags, and external RPC configuration do not
rewrite these preferences.

## Secrets

Never commit auth files or real API keys.

::: warning Migration note
Wisp no longer reads a project `.env` file. Move any values you kept there into your shell
environment or `~/.wisp/settings.json`. A project `.env` on disk is still treated as a secret and is
never surfaced to the model.
:::

See also [Environment variables](./environment) and
[Tools & safety](../guide/tools-and-safety#project-trust).
