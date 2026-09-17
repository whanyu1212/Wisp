# Upgrading to Wisp 0.2

This guide describes the 0.2 candidate series and the checks required before stable promotion.
RC2 introduced the Rust-default trial on native wheels; the current development tree also retires
Textual. Check the [release page](https://github.com/whanyu1212/Wisp/releases) for the latest
published candidate before installing one.

## What changes for terminal users

`wisp`, `wisp tui`, and `wisp --mode tui` use `auto`: they select Rust when a native binary is
installed on macOS/Linux, and the prompt-toolkit fullscreen renderer otherwise. Native wheels cover
macOS arm64 and Linux glibc 2.28+ x86_64. Pure/source installs, Intel macOS, and other platforms use
prompt-toolkit fullscreen by default. Explicit CLI selection takes precedence over
`WISP_TUI_RENDERER`, which takes precedence over `auto`. `WISP_RUST_TUI_BINARY` also selects Rust in
auto mode on macOS/Linux for source development. Missing or damaged declared binaries and Rust
launch/runtime failures report an error; they never silently switch frontends.

Use `wisp tui --renderer fullscreen` or `WISP_TUI_RENDERER=fullscreen` to select the Python
fullscreen renderer explicitly. Both frontends use the same Python runtime, permissions, providers,
and saved sessions.

RC2 includes the native-wheel release pipeline, composer selection/undo/clipboard, semantic colors,
and complete saved-history loading. It is a trial of Rust as the default on the packaged platforms,
not a claim of identical Python fullscreen controls or universal terminal performance.

Existing supported JSONL sessions remain readable without manual migration. Python continues to
own persistence. Back up important sessions before testing a candidate; older releases are not
promised to understand newly written records.

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
2. Support **live RPC v8 and event schema v39**. Wait for `rpc.handshake.accepted` before sending
   commands; handle rejection as a connection failure rather than attempting legacy fallback.
3. Honor negotiated directional frame limits and strict UTF-8, LF-terminated JSON framing.
4. Use backend-owned model and connection catalogs. Credential mutations belong to backend RPC;
   frontends must not read or write Wisp credential files themselves.

Use the checked-in `schemas/live-rpc/v8/` bundle and the typed Python transport as implementation
references. Versioned schema bundles are release assets, not part of the Python wheel API.
Historical bundles remain immutable. These live-connection requirements do not change the
backward-readability policy for persisted sessions.

The in-process Python SDK has no serialization boundary and does not perform a wire handshake.
The Rust TUI requires the exact Python package release. Source builds use the matching
checkout; native wheels are built and published in lockstep with the Python release.

## Trying a published candidate

After a candidate's artifacts are verified, use its exact published version in an explicit pin:

```bash
uvx --from "wisp-ai==<published-version>" wisp --version
uvx --from "wisp-ai==<published-version>" wisp
```

This avoids replacing an existing persistent `uv tool` installation, but the running application
still uses normal Wisp configuration and session locations. Use a disposable project and back up
important state when testing. Continue using [0.1.0 installation instructions](./installation)
if you do not want to opt into prerelease testing.

## Before promoting to 0.2.0

Use the [RC2 release checklist](https://github.com/whanyu1212/Wisp/blob/main/site/contributing/rc2-release.md)
for candidate publication, platform installation, long-session measurements, and rollback gates.

- Require green CI and release-workflow verification/build checks on the exact candidate commit.
- Verify wheel and source-distribution metadata, installed SDK imports, `wisp --version`, and a
  fake-provider prompt outside the source checkout.
- Exercise Rust and the prompt-toolkit fullscreen fallback in real terminals: long streaming output while typing and scrolling,
  file-picker navigation, cancellation, approvals, and session resume.
- Dogfood the published candidate and resolve release blockers before updating stable version
  pins or creating the final tag. Passing headless tests is not evidence of native-terminal
  visual correctness.

The [changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) records the release
scope. Publishing the candidate and publishing the final release are separate approval steps.
