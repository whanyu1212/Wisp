---
title: Environment variables
---

# Environment variables

| Variable | Purpose |
|----------|---------|
| `WISP_PROVIDER` | Provider name: `openai-codex`, `openai`, `xai`, `openai-compatible`, `anthropic`, `google`, or `fake` |
| `WISP_MODEL` | Model override; blank uses the provider default |
| `WISP_MODE` | Default mode for invocations without `--prompt`; prompt runs require explicit `--mode` |
| `WISP_TUI_RENDERER` | TUI renderer: `line`, `fullscreen`, or `textual` |
| `WISP_SESSION_DIR` | Session storage directory; defaults to `~/.wisp/sessions` |
| `WISP_AUTH_FILE` | Auth file path; defaults to `~/.wisp/auth.json` |
| `WISP_OPENAI_COMPATIBLE_CONFIG` | JSON object configuring one OpenAI-compatible endpoint; overrides the user-settings `openai_compatible` object |
| `WISP_TRUST` | Trust the current project for one process: `1` to opt in, `0` to force untrusted |
| `WISP_TRUST_FILE` | Relocate the global trust store; must be absolute, but is otherwise accepted as supplied |
| `WISP_EFFORT` | Reasoning effort override |
| `WISP_RETRY_MAX_RETRIES` | Provider retry count; defaults to `2`, set `0` to disable |
| `WISP_RETRY_BASE_DELAY_SECONDS` | Initial retry delay; defaults to `0.5` |
| `WISP_RETRY_MAX_DELAY_SECONDS` | Maximum retry delay; defaults to `30` |
| `WISP_CONTEXT_RESERVE_TOKENS` | Minimum tokens reserved outside estimated input context; defaults to `16384` |
| `WISP_AUTO_COMPACTION` | Automatic threshold compaction and overflow recovery; defaults to `true` |
| `WISP_UPDATE_CHECK` | Six-hour non-blocking PyPI update notice; defaults to `true` |

## Provider credentials

| Variable | Provider |
|---|---|
| `OPENAI_API_KEY` | `openai` |
| `XAI_API_KEY` | `xai` |
| `ANTHROPIC_API_KEY` | `anthropic` |
| `GOOGLE_API_KEY` · `GEMINI_API_KEY` | `google` |
| `<CUSTOM_PROVIDER>_API_KEY` | A custom OpenAI-compatible provider; hyphens become underscores |
| `OPENAI_COMPATIBLE_API_KEY` | Fallback for custom OpenAI-compatible providers |

Each is required only for the matching provider. See [Providers & auth](../guide/providers) for
storage and precedence details.

`WISP_OPENAI_COMPATIBLE_CONFIG` accepts `provider_name`, `base_url`, `default_model`, optional
`requires_api_key`, and optional absolute `ca_bundle` fields. The value must be a JSON object; invalid
JSON or unknown fields fail configuration instead of being ignored. It overrides the structured
endpoint in `~/.wisp/settings.json`, while an explicit SDK configuration value overrides the
environment.

`WISP_TRUST` and `WISP_TRUST_FILE` are read only from the real process environment, never from
project files, and `WISP_TRUST` is never persisted — see
[Tools & safety](../guide/tools-and-safety#project-trust).

`WISP_MODE` applies only when neither a mode nor a prompt is supplied on the command line. For
example, `WISP_MODE=json wisp -p "hello"` still uses text output; write
`wisp -p "hello" --mode json` for a machine-readable prompt run.
