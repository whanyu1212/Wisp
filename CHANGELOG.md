# Changelog

Notable changes to Wisp, with an emphasis on the **event-schema contract** that JSON, RPC, and SDK
consumers integrate against.

Every outbound `WispEvent` carries a `schema_version`. Consumers should branch on it and reject
versions they do not support; Wisp's typed RPC client does this automatically. For the current
contract, see [`docs/reference/events.md`](docs/reference/events.md).

This log starts at schema v27. Earlier history is in the git log.

## Unreleased

_Nothing yet._

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
