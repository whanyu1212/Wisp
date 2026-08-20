# Changelog

Notable changes to Wisp, with an emphasis on the **event-schema contract** that JSON, RPC, and SDK
consumers integrate against.

Every outbound `WispEvent` carries a `schema_version`. Consumers should branch on it and reject
versions they do not support; Wisp's typed RPC client does this automatically. The current typed
contract lives in `src/wisp/events.py`.

This log starts at schema v27. Earlier history is in the git log.

## Unreleased

- Added deterministic, tested Python SDK examples for prompting, safety requests, live control,
  persisted-session workflows, and process-isolated JSONL RPC.
- Defined and wheel-verified the supported Python SDK import surface, including the typed project
  trust command used by external RPC clients.
- Limited routine Textual transcript scrolling to partial viewport updates, preventing unchanged
  composer and status rows from flickering during wheel and keyboard navigation.
- Kept complete resumed Textual histories covered until composer layout and tail positioning settle,
  preventing a transient viewport jump when the loading surface closes.

## 0.1.0rc4 — 2026-08-20

- Preserved accepted and pending TUI input across RPC handoff, startup, failure, and cancellation,
  keeping early drafts editable instead of silently queueing or losing them.
- Added an animated startup notice above the composer and hid provider-facing system prompts from
  TUI history while preserving persistence, RPC, provider context, and complete-history pagination.
- Excluded `cryptography` versions before 50.0.0 from runtime dependency resolution to avoid the
  PKCS#7 decryption vulnerability fixed in 50.0.0.

## 0.1.0rc3 — 2026-08-19

- Added bounded Markdown structure, inline-code, and fenced-code highlighting to the Textual prompt
  editor without changing submitted prompt text.
- Kept complete Textual conversation and tool history reachable through wheel, PageUp/PageDown,
  Home, and End navigation during long and tool-heavy turns without unbounded widget mounting.
- Added first-class DeepSeek support with streamed thinking, reasoning-preserving tool
  continuations, API-key authentication, automatic cache telemetry, and model catalog metadata.
- Added first-class xAI Grok support through the stateful Responses API, including streamed
  reasoning, native tool-call continuation, API-key authentication, and model catalog metadata.
- Preserved RPC command ordering when changing providers or models so configuration waits for
  active session work and takes effect before later prompts.
- Rejected ambiguous bare model selections when the active provider cannot disambiguate them,
  preventing cataloged models from being silently applied to an unrelated provider.

## 0.1.0rc2 — 2026-08-18

- Stabilized streamed TUI output and resumable process-card lifecycles across polling, replay,
  denial, interruption, concurrent calls, pagination, reload, and bounded retention.
- Added an asynchronous TUI update prompt that defaults to updating and restarting supported
  persistent installs while preserving explicit checks, skip/later choices, and safe relaunch state.
- Removed obsolete synchronous and CLI-owned RPC compatibility paths so queueing, cancellation,
  trust, approvals, and shutdown consistently use the shared async command host.
- Centralized context-budget construction in the shared accounting policy and strengthened CI with
  a required production fault-injection partition plus broader platform-sensitive coverage.
- Added a VitePress documentation site, reduced the README to a focused entry point, and added
  release, safety, provider, session, interface, and contributor references.
- Refined Vapor, Orchid, Ember, and Paper; added Grove, Wave, and Dawn; and made semantic transcript
  surfaces meet normal-text contrast requirements across every curated theme.

## 0.1.0rc1 — 2026-08-17

- Kept compaction, steering, follow-up, provider continuation, and overflow recovery inside one
  long-lived agent loop with typed request-boundary transitions.
- Separated provider thinking from assistant content, made provider failures terminal stream events,
  and improved context-budget accounting and actionable tool-failure feedback.
- Prevented truncated or inconsistent tool-call batches from reaching execution and added bounded
  concurrency for explicitly parallel-safe batches while preserving deterministic approvals,
  source-ordered results, sequential fallback, and complete cancellation.
- Preserved validated historical tool call/result pairs for native replay, with deterministic safe
  fallbacks for provider switches, opaque provider state, malformed history, and interrupted calls.
- Hardened filesystem tools and skill loading against races and path escapes, bounded read/search
  work, and fixed tool working-directory behavior for selected workspaces.
- Added richer diff layouts, transcript recovery, prompt highlighting, project-tree file selection,
  approval safety surfaces, and additional curated TUI themes.
- Fixed tool-heavy historical session replay repeatedly reloading its latest page, flickering the
  Textual screen and preventing normal interaction.

## 0.1.0b4 — 2026-08-12

- Added the package-owned `wisp-development` skill with progressively loaded architecture,
  extension API, safety, authoring, and verification guidance, plus a deterministic static
  extension example for Python embedders.
- Added named OpenAI-compatible providers with provider-scoped authentication, model metadata,
  reasoning effort, and private CA support, and improved Codex recovery from context overflow while
  preserving tool context.
- Added session-scoped prompt-cache keys, explicit stable-prefix cache boundaries for supported
  OpenAI models, and cache read/write telemetry in context usage views.
- Replaced TUI tool-card chrome with compact, expandable action and result trees that retain
  lifecycle state, bounded previews, structured diffs, and restored-session presentation.

## 0.1.0b3 — 2026-08-12

- Added configurable OpenAI-compatible endpoints for using provider APIs beyond the built-in OpenAI
  service.
- Added a project initialization command and refined TUI themes, permission prompts, transcript
  surfaces, streamed Markdown, diffs, and startup presentation.
- Fixed resumed and completed assistant response rendering, working-indicator visibility, truthful
  RPC command lifecycles, and recovery from incomplete session JSONL appends.

## 0.1.0b2 — 2026-08-11

- Added typed MCP status RPC snapshots and a TUI `/mcp` command that reports configured servers,
  registered tools, live connection state, and sanitized startup failures without reconnecting
  servers.

## 0.1.0b1 — 2026-08-10

- Reduced the installed-build PyPI update cache from 24 hours to six hours so new releases are
  announced sooner without checking on every TUI launch.
- Added immediate manual update checks through `wisp update --check` and `/update`, with confirmed
  installation for persistent `uv tool` environments through `wisp update` or `/update install`.
- Isolated self-updates from project-controlled uv configuration and indexes, kept provenance checks
  cancellable, and shielded active environment replacement from interruption.
- Added bounded, user-only configuration for multiple MCP stdio servers, including explicit
  arguments, environment forwarding, and exact per-tool safety overrides.
- Added MCP tool adaptation through the official SDK with deterministic namespacing, bounded schemas
  and text results, and command safety by default through Wisp's existing approval policy.
- Added concurrent MCP startup, paginated tool discovery, atomic catalog registration, sanitized
  failure diagnostics, and runtime-owned connection cleanup across CLI, RPC, SDK, and TUI modes.
- Added pre-parse MCP frame limits and aggregate server, tool, definition, and discovery limits so
  malformed or oversized servers fail independently without exposing payloads or credentials.

## 0.1.0a3 — 2026-08-08

- Added `wisp --version` to print the installed package version without starting the TUI.
- Added strict, trust-aware Agent Skills metadata discovery from user and project locations, with a
  `wisp skills` command for inspecting the catalog and isolated diagnostics.
- Added a bounded model-facing Agent Skills index and read-only `skill` tool for progressively
  loading instructions and contained supporting resources without weakening tool policy.
- Added explicit `/skill:<name>` invocation with typed replay evidence for prompts, steering, and
  follow-ups.
- Added typed skill-catalog RPC snapshots, deterministic `/skill:` TUI completion, cached `/skills`
  inspection, trust-refresh updates, and compact live and historical invocation presentation.
- Added an opt-in `wisp-code-review` example skill with user/project installation instructions and
  a progressively loaded Wisp review checklist.

## 0.1.0a2 — 2026-08-07

- Replaced TUI `/login` with an OpenCode-style `/connect` panel for ChatGPT subscription access and
  masked OpenAI, Anthropic, and Google API-key entry; `/disconnect` removes stored credentials.
- Removed the standalone browser-based `wisp auth login` command and its unusable localhost callback
  flow. ChatGPT subscription authentication now uses device-code OAuth exclusively.
- Added request-time stored API-key resolution for OpenAI, Anthropic, and Google while preserving
  explicit and environment credential precedence.
- Added a cached, non-blocking TUI notice when a newer applicable Wisp release is available on PyPI.

## 0.1.0a1 — 2026-08-07

Initial PyPI alpha release of Wisp's shared CLI, JSON, RPC, SDK, and Textual TUI runtime.

- Installs the `wisp` command through the `wisp-ai` package.
- Launches the fullscreen TUI from a bare interactive `wisp` invocation.
- Includes provider integrations, local tools, persistent sessions, compaction, project trust,
  protected paths, and explicit unsafe-tool approvals.
- Publishes provider-neutral lifecycle events at schema v27.

## Schema v34 — current

Adds `next_after_entry_id` to `rpc.messages` results so clients can paginate forward from a known
transcript entry.

## Schema v33

Adds optional `failure_code`, `retryable`, and `recovery_hint` metadata to failed tool results so
clients can present structured recovery guidance.

## Schema v32

Adds `trailing_estimated_tokens`, `effective_tokens`, and `accounting_method` to context budget
snapshots so clients can distinguish provider usage from local estimates.

## Schema v31

Adds `package:wisp` as a skill catalog source for package-owned skills. RPC clients that
exhaustively validate skill sources must accept the new value.

## Schema v30

Adds the `rpc.mcp` event carrying configured server connection status, registered tool names, and
sanitized startup failures. RPC clients can issue `get_mcp_status`; consumers that exhaustively
match event types must handle or ignore `rpc.mcp`.

## Schema v29

Adds `rpc.skills` and `skill.catalog.updated` events carrying typed skill descriptors, isolated
discovery diagnostics, and project-trust state. RPC clients can issue `get_skills` for the active
immutable catalog; TUI clients should replace their cached snapshot when `skill.catalog.updated`
arrives.

## Schema v28

Adds `skill.invoked` and typed skill-invocation evidence to queue updates and persisted RPC message
snapshots. Consumers can present the original directive and request without parsing the bounded
provider-visible expansion.

## Schema v27

Adds `mode` (`"plan" | "build"`, default `"build"`) to `CodingSessionState`, so RPC `get_state`
reports the active agent mode. The field is stripped for consumers reading at an older schema
version.

Events at schema v5 through v34 remain readable.

---

### Adding an entry

When you change the event schema, bump `EVENT_SCHEMA_VERSION` in `src/wisp/events.py`, add a named
breadcrumb constant beside it, and record the change here under a new `## Schema vN` heading. Say
what a consumer must do differently — new fields, changed meanings, dropped compatibility — not the
implementation detail.

Changes that do not touch the wire format go under `## Unreleased`.
