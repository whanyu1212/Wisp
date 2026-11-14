---
title: Environment variables
description: Runtime, provider credential, path, and terminal environment variables recognized by Wisp.
route: reference/environment.html
section: reference
order: 3
layout: docs
listed: true
searchable: true
---

# Environment variables

| Variable | Purpose |
|----------|---------|
| `WISP_PROVIDER` | Provider name: `openai-codex`, `openai`, `xai`, `deepseek`, `openai-compatible`, `anthropic`, `google`, or `fake` |
| `WISP_MODEL` | Model override; blank uses the provider default |
| `WISP_MODE` | Default mode for invocations without `--prompt`; prompt runs require explicit `--mode` |
| `WISP_RUST_TUI_BINARY` | Absolute executable path to a source-built `wisp-tui`; used when launching the TUI |
| `WISP_TUI_MOUSE` | TUI mouse navigation and selection; enabled when unset or set to `1`, `true`, or `on` (case-insensitive, surrounding whitespace ignored); any other value disables it |
| `WISP_SESSION_DIR` | Session storage directory; defaults to `~/.wisp/sessions` |
| `WISP_AUTH_FILE` | Auth file path; defaults to `~/.wisp/auth.json` |
| `WISP_OPENAI_COMPATIBLE_CONFIG` | JSON object configuring one OpenAI-compatible endpoint; overrides the user-settings `openai_compatible` object |
| `WISP_TRUST` | Trust the current project for one process: `1` to opt in, `0` to force untrusted |
| `WISP_TRUST_FILE` | Relocate the global trust store; must be absolute, but is otherwise accepted as supplied |
| `WISP_EFFORT` | Reasoning effort override |
| `WISP_RETRY_MAX_RETRIES` | Provider retry count; defaults to `2`, set `0` to disable |
| `WISP_RETRY_BASE_DELAY_SECONDS` | Initial retry delay; defaults to `0.5` |
| `WISP_RETRY_MAX_DELAY_SECONDS` | Maximum retry delay; defaults to `30` |
| `WISP_CODEX_READ_TIMEOUT_SECONDS` | Read-idle timeout for Wisp-owned `openai-codex` connections; defaults to `300`; positive finite seconds, e.g. `600` |
| `WISP_CONTEXT_RESERVE_TOKENS` | Minimum tokens reserved outside estimated input context; defaults to `16384` |
| `WISP_AUTO_COMPACTION` | Automatic threshold compaction and overflow recovery; defaults to `true` |
| `WISP_WAKE_ON_PROCESS_EXIT` | Start a run when a background process started with `notify_on_exit` stops while the session is idle, at most three times between prompts; defaults to `true` |
| `WISP_WEB_SEARCH` | Offer the provider's built-in [web search](../guide/providers.md#web-search) to the model; defaults to `false` |
| `WISP_CODEMODE` | Offer the experimental [codemode](../guide/tools-and-safety.md#scripting-tool-calls-with-codemode-experimental) tool in every run (`1`) or never (`0`); unset, it is offered whenever a run finds its MCP tools on demand |
| `WISP_SUBAGENTS` | Offer the experimental [`spawn_agent`](../guide/tools-and-safety.md#delegating-to-a-subagent-experimental) tool, which hands a read-only task to a subagent; defaults to `false` |
| `WISP_SUBAGENT_MODEL` | Model a [subagent](../guide/tools-and-safety.md#delegating-to-a-subagent-experimental) that starts fresh uses, on the session's provider; unset, subagents use the session's model |

For terminal-native selection instead of Wisp's mouse handling, launch with:

```bash
WISP_TUI_MOUSE=0 wisp tui
```

See [Mouse navigation and scrollback](../guide/tui/sessions.md#mouse-navigation-and-scrollback)
for supported interactions and terminal-specific overrides. To customize keyboard controls, use
[`tui_keybindings`](configuration.md#structured-user-settings), not an environment variable.

## Provider credentials

| Variable | Provider |
|---|---|
| `OPENAI_API_KEY` | `openai` |
| `XAI_API_KEY` | `xai` |
| `DEEPSEEK_API_KEY` | `deepseek` |
| `ANTHROPIC_API_KEY` | `anthropic` |
| `GOOGLE_API_KEY` · `GEMINI_API_KEY` | `google` |
| `<CUSTOM_PROVIDER>_API_KEY` | A custom OpenAI-compatible provider; hyphens become underscores |
| `OPENAI_COMPATIBLE_API_KEY` | Fallback for custom OpenAI-compatible providers |

Each is required only for the matching provider. See [Providers & auth](../guide/providers.md) for
storage and precedence details.

`WISP_OPENAI_COMPATIBLE_CONFIG` accepts `provider_name`, `base_url`, `default_model`, optional
`requires_api_key`, and optional absolute `ca_bundle` fields. The value must be a JSON object; invalid
JSON or unknown fields fail configuration instead of being ignored. It overrides the structured
endpoint in `~/.wisp/settings.json`, while an explicit SDK configuration value overrides the
environment.

`WISP_TRUST` and `WISP_TRUST_FILE` are read only from the real process environment, never from
project files, and `WISP_TRUST` is never persisted — see
[Tools & safety](../guide/tools-and-safety.md#project-trust).

`WISP_MODE` applies only when neither a mode nor a prompt is supplied on the command line. For
example, `WISP_MODE=json wisp -p "hello"` still uses text output; write
`wisp -p "hello" --mode json` for a machine-readable prompt run.

Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the Rust TUI. Pure-wheel installs
retain print, JSON, RPC, and SDK use; an interactive TUI command instead reports how to obtain a
native wheel or build a matching Rust binary from source. There is no Python terminal renderer.

`WISP_RUST_TUI_BINARY` is a source-development override, not a general executable search path. It
must name an existing, executable absolute path and is removed from the environment passed to the
Python RPC backend. Without an override, native-wheel installations resolve `wisp-tui` only
from the active Python environment's scripts directory; Wisp never searches `PATH`. See
[Development setup](../contributing/development.md#rust-tui-scaffold).

## Internal development controls

The following names are implementation details for Wisp's launcher, tests, or benchmarks, not
supported user configuration. They may change without compatibility guarantees; leave them unset
for normal use.

| Name | Internal purpose |
|---|---|
| `WISP_RUST_TUI_BINDINGS_JSON` | Launcher-to-frontend handoff of resolved keybindings. The Python launcher overwrites it from user settings; configure `tui_keybindings` instead. |
| `WISP_VISUAL_OUTPUT` | Output directory for the explicitly requested, ignored Rust visual-capture test; not a runtime rendering option. |
| `WISP_HYDRATION_PROFILE_DIR` | Opt-in Python/Rust saved-history benchmark timings, appended as per-process JSONL files in an existing directory. |
| `WISP_FAKE_*` | Fake-provider test controls: `WISP_FAKE_STREAM_INTERVAL_MS`, `WISP_FAKE_RESPONSE_WORDS`, `WISP_FAKE_RESPONSE_PREFIX`, and `WISP_FAKE_RESPONSE_SUFFIX`. These tune synthetic responses, not real providers. |

The historical `WISP_BRAILLE_*` controls are no longer read from the environment. The current
`WISP_BRAILLE_MINI`, `WISP_BRAILLE_NORMAL`, and `WISP_BRAILLE_FULL` names are Rust logo constants,
not environment variables.
