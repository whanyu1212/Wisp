# Changelog

Notable changes to Wisp, with an emphasis on the **event-schema contract** that JSON, RPC, and SDK
consumers integrate against.

Every outbound `WispEvent` carries a `schema_version`. Consumers should branch on it and reject
versions they do not support; Wisp's typed RPC client does this automatically. The current typed
contract lives in `src/wisp/events.py`.

This log starts at schema v27. Earlier history is in the git log.

## Unreleased

- Reduced the installed-build PyPI update cache from 24 hours to six hours so new releases are
  announced sooner without checking on every TUI launch.

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

## Schema v29 — current

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

Events at schema v5 through v29 remain readable.

---

### Adding an entry

When you change the event schema, bump `EVENT_SCHEMA_VERSION` in `src/wisp/events.py`, add a named
breadcrumb constant beside it, and record the change here under a new `## Schema vN` heading. Say
what a consumer must do differently — new fields, changed meanings, dropped compatibility — not the
implementation detail.

Changes that do not touch the wire format go under `## Unreleased`.
