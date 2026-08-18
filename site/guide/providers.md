---
title: Providers & auth
---

# Providers & auth

| Provider | Credentials |
|---|---|
| `openai-codex` *(default)* | ChatGPT Plus/Pro via device-code OAuth — TUI `/connect` |
| `openai` | Stored API key or `OPENAI_API_KEY` |
| `xai` | Stored API key or `XAI_API_KEY` |
| `deepseek` | Stored API key or `DEEPSEEK_API_KEY` |
| Custom OpenAI-compatible name | Stored API key, `<PROVIDER_NAME>_API_KEY`, or fallback `OPENAI_COMPATIBLE_API_KEY`; endpoint configured in user settings or `WISP_OPENAI_COMPATIBLE_CONFIG` |
| `anthropic` | Stored API key or `ANTHROPIC_API_KEY` |
| `google` | Stored API key, `GOOGLE_API_KEY`, or `GEMINI_API_KEY` |
| `fake` | None — deterministic offline provider for tests and smoke runs |

```bash
wisp -p "hello" --provider anthropic --model claude-sonnet-5
wisp -p "explain this repository" --provider xai --model grok-4.6
wisp -p "review this change" --provider deepseek --model deepseek-v4-pro
```

## DeepSeek Chat Completions

The built-in `deepseek` provider uses DeepSeek's OpenAI-format Chat Completions API at
`https://api.deepseek.com`. Set `DEEPSEEK_API_KEY` or store a key with `/connect deepseek`.
Wisp enables thinking mode, streams reasoning separately from final text, and preserves DeepSeek's
native `reasoning_content` inside provider-local continuation state so thinking-mode tool rounds can
be continued correctly. Context caching is automatic on DeepSeek's service.

The model catalog lists `deepseek-v4-pro` and `deepseek-v4-flash`, each with `low`, `high`, and
`max` effort. DeepSeek's peak/off-peak prices are not currently estimated because Wisp's catalog
does not yet represent time-of-day rates.

## xAI Responses API

The built-in `xai` provider uses xAI's stateful Responses API at `https://api.x.ai/v1`, not the
legacy Chat Completions endpoint. Set `XAI_API_KEY` or store a key with `/connect xai`. Wisp streams
text and summarized reasoning, supports parallel client-defined function tools, and continues tool
rounds with xAI response IDs.

Native HTTP continuation stores response state on xAI's servers. xAI documents a 30-day retention
period for stored responses; Wisp explicitly enables storage to preserve reasoning and tool state
across continued requests. Stateless/ZDR continuation over xAI's WebSocket transport and xAI-hosted
web, X, code-execution, file-search, and MCP tools are not currently enabled by Wisp.

## OpenAI-compatible endpoints

OpenAI-compatible Chat Completions endpoints can be configured in the user settings file or with
the `WISP_OPENAI_COMPATIBLE_CONFIG` environment variable. Project settings cannot redirect requests
carrying your credentials. For example, OpenRouter in `~/.wisp/settings.json`:

```json
{
  "provider": "openrouter",
  "model": "anthropic/claude-sonnet-4",
  "openai_compatible": {
    "provider_name": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "default_model": "anthropic/claude-sonnet-4"
  }
}
```

For an environment-only deployment, set the same inner object as JSON. It overrides the
`openai_compatible` object from the user settings file:

```bash
export WISP_OPENAI_COMPATIBLE_CONFIG='{"provider_name":"openrouter","base_url":"https://openrouter.ai/api/v1","default_model":"anthropic/claude-sonnet-4"}'
```

An explicit `OpenAICompatibleSettings` value supplied by an SDK embedder has precedence over the
environment variable, which in turn has precedence over user settings.

Set `OPENROUTER_API_KEY`, use the optional `OPENAI_COMPATIBLE_API_KEY` fallback, or enter the key
with `/connect openrouter`. Provider names must start with a lowercase letter. Hyphens become
underscores in environment variables — `local-openai` uses `LOCAL_OPENAI_API_KEY`.

Local servers that do not require authentication can use a loopback HTTP endpoint with
`"requires_api_key": false`:

```json
{
  "provider": "local-openai",
  "openai_compatible": {
    "provider_name": "local-openai",
    "base_url": "http://localhost:11434/v1",
    "default_model": "qwen3-coder",
    "requires_api_key": false
  }
}
```

For a private certificate authority, set `"ca_bundle"` to an existing absolute PEM bundle path
inside `openai_compatible`. This provider-level setting overrides the default trust bundle for that
endpoint. Python HTTP clients also honor `SSL_CERT_FILE` process-wide.

Compatibility targets streaming `/chat/completions`, including client-defined function tools.
Explicit model IDs pass through unchanged; add a user-only `~/.wisp/catalog.toml` overlay when
model-picker metadata, context limits, effort tiers, or pricing are desired. The catalog provider
`name` must equal `provider_name`; list models in `models` and provider-native effort strings under
`[providers.effort_levels]`.

## Credential storage

Credentials entered through `/connect` are stored in `WISP_AUTH_FILE` when set, otherwise the
resolved `auth_path` (default `~/.wisp/auth.json`), with private permissions. A trusted project's
`.wisp/settings.json` may override `auth_path`, including with a relative path inside the working
tree, so inspect that file before connecting and never commit the selected auth file. An explicit
`WISP_AUTH_FILE` takes precedence.

Updates are serialized across cooperating Wisp processes and atomically publish a synchronized,
uniquely staged replacement; unsafe symlink, hard-link, ownership, or permission state is rejected
rather than read.

Precedence: explicit provider constructor keys, then environment variables, then stored keys.

Secrets entered in the panel are masked and never enter prompt history, transcripts, RPC events, or
session JSONL.

## Switching providers and models

In the TUI, `/model` with no arguments lists every catalog model grouped by provider. If a model id
belongs to only one registered provider, `/model <id>` switches providers to match; otherwise use
`/provider <name>` first.

## Model catalog

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

## Retry behavior

Wisp retries only requests that fail **before** the provider starts streaming, using bounded
exponential backoff with jitter. It honors reasonable `Retry-After` requests, emits retry progress
in JSON/RPC and the TUI, and never replays an already-started response.

OpenAI-family streams succeed only after the provider's native completion event. If a connection
ends first, Wisp reports a failed turn with any partial text and never executes buffered tool calls.
For Wisp-owned `openai-codex` connections, connect and pool waits are limited to 10 seconds, request
writes to 30 seconds, and response-header or between-chunk read inactivity to 300 seconds.
Caller-injected HTTP clients retain their caller-selected timeout policy.

Tune with `WISP_RETRY_MAX_RETRIES`, `WISP_RETRY_BASE_DELAY_SECONDS`, and
`WISP_RETRY_MAX_DELAY_SECONDS` — see [Environment variables](../reference/environment).
