---
title: Upgrading to Wisp 0.2
---

# Upgrading to Wisp 0.2

**0.2.0rc1 was published on September 10, 2026.** It is an opt-in release candidate, not a stable
release. See the [release notes](https://github.com/whanyu1212/Wisp/releases/tag/v0.2.0rc1)
and [PyPI package](https://pypi.org/project/wisp-ai/0.2.0rc1/).
The latest stable release remains 0.1.0.
This page describes the candidate's upgrade impact and the checks to complete before 0.2.0.

## What changes for terminal users

Textual remains the default TUI. The candidate improves streaming order when scrolling away from
the live tail, input responsiveness during Markdown rendering, and file-picker responsiveness.
It also fixes stale results when reopening a bare `@` mention and stale expand/collapse arrows.
Supported terminals can use synchronized output to reduce partial-frame flicker.

The Rust frontend remains experimental, source-build opt-in on macOS and Linux. Python wheels do
not contain a Rust binary, and explicit Rust selection never silently falls back to Textual.
There is no need to switch renderers to benefit from this release.

Existing supported JSONL sessions remain readable without manual migration. Python continues to
load historical data and provides current-version snapshots to frontends. As with any upgrade,
keep backups of important sessions; forward readability does not promise that an older Wisp version
can read new records written by a newer one.

## Python integrations

This is a minor release with an announced breaking API cleanup, not a patch release.

- The deprecated `wisp.agent.messages.SessionEntry(...)` factory has been removed as an explicit
  exception to the normal deprecation window. Use `MessageSessionEntry`, `EventSessionEntry`, or
  `CompactionSessionEntry` from `wisp.sessions`.
- For event entries, pass `PersistedEventEnvelope(payload=raw_event)` rather than a raw dictionary
  as the `event` value. This changes Python construction, not existing JSONL files.
- Code importing agent internals must update removed module paths. Use `wisp.agent.harness`,
  `wisp.agent.loop`, and `wisp.agent.prompt` as package entry points; history helpers now live in
  `wisp.agent.history`. Do not rely on removed history-helper re-exports from `wisp.agent.messages`.
- Prefer the documented [Python SDK import surface](../reference/sdk) when embedding Wisp.
  See [Compatibility & versioning](../reference/compatibility) for the public API boundary and
  the early-removal exception.

## External JSONL-RPC clients

Update external clients together with the backend:

1. Send `rpc.handshake.request` as the first frame, before ordinary commands.
2. Support **live RPC v5 and event schema v36**. Wait for `rpc.handshake.accepted` before sending
   commands; handle rejection as a connection failure rather than attempting legacy fallback.
3. Honor negotiated directional frame limits and strict UTF-8, LF-terminated JSON framing.
4. Use backend-owned model and connection catalogs. Credential mutations belong to backend RPC;
   frontends must not read or write Wisp credential files themselves.

Use the checked-in `schemas/live-rpc/v5/` bundle and the typed Python transport as implementation
references. Versioned schema bundles are release assets, not part of the Python wheel API.
Historical bundles remain immutable. These live-connection requirements do not change the
backward-readability policy for persisted sessions.

The in-process Python SDK has no serialization boundary and does not perform a wire handshake.
The experimental Rust TUI additionally requires the exact Python package release: build the
frontend from the same checkout, using Cargo version `0.2.0-rc.1` for Python `0.2.0rc1`.

## Trying the candidate

An explicit version pin opts into the published candidate:

```bash
uvx --from "wisp-ai==0.2.0rc1" wisp --version
uvx --from "wisp-ai==0.2.0rc1" wisp
```

This avoids replacing an existing persistent `uv tool` installation, but the running application
still uses normal Wisp configuration and session locations. Use a disposable project and back up
important state when testing. Continue using [0.1.0 installation instructions](./installation)
if you do not want to opt into prerelease testing.

## Before promoting to 0.2.0

- Require green CI and release-workflow verification/build checks on the exact candidate commit.
- Verify wheel and source-distribution metadata, installed SDK imports, `wisp --version`, and a
  fake-provider prompt outside the source checkout.
- Exercise Textual in a real terminal: long streaming output while typing and scrolling,
  file-picker navigation, cancellation, approvals, and session resume.
- Dogfood the published candidate and resolve release blockers before updating stable version
  pins or creating the final tag. Passing headless tests is not evidence of native-terminal
  visual correctness.

The [changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) records the release
scope. Publishing the candidate and publishing the final release are separate approval steps.
