# Changelog

Notable changes to Wisp, with an emphasis on the **event-schema contract** that JSON, RPC, and SDK
consumers integrate against.

Every outbound `WispEvent` carries a `schema_version`. Consumers should branch on it and reject
versions they do not support; Wisp's typed RPC client does this automatically. The current typed
contract lives in `src/wisp/events.py`.

This log starts at schema v27. Earlier history is in the git log.

## Unreleased

_Nothing yet._

## 0.1.0a1 — 2026-08-07

Initial PyPI alpha release of Wisp's shared CLI, JSON, RPC, SDK, and Textual TUI runtime.

- Installs the `wisp` command through the `wisp-ai` package.
- Launches the fullscreen TUI from a bare interactive `wisp` invocation.
- Includes provider integrations, local tools, persistent sessions, compaction, project trust,
  protected paths, and explicit unsafe-tool approvals.
- Publishes provider-neutral lifecycle events at schema v27.

## Schema v27 — current

Adds `mode` (`"plan" | "build"`, default `"build"`) to `CodingSessionState`, so RPC `get_state`
reports the active agent mode. The field is stripped for consumers reading at an older schema
version.

Events at schema v5 through v27 remain readable.

---

### Adding an entry

When you change the event schema, bump `EVENT_SCHEMA_VERSION` in `src/wisp/events.py`, add a named
breadcrumb constant beside it, and record the change here under a new `## Schema vN` heading. Say
what a consumer must do differently — new fields, changed meanings, dropped compatibility — not the
implementation detail.

Changes that do not touch the wire format go under `## Unreleased`.
