# Upgrading to Wisp 0.2

This guide describes the 0.2 candidate series and the checks required before stable promotion.
RC2 introduced the Rust-default trial on native wheels; RC3 removes the Python terminal renderers.
Check the
[release page](https://github.com/whanyu1212/Wisp/releases) for the latest
published candidate before installing one.

## What changes for terminal users

`wisp`, `wisp tui`, and `wisp --mode tui` launch Rust; the retained `auto` and `rust` selectors both
choose it. Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the binary and Python
backend. Pure-wheel installs retain print, JSON, RPC, and SDK but interactive commands fail with
actionable missing-binary guidance. Build a matching Rust binary for a source checkout and set its
absolute path in `WISP_RUST_TUI_BINARY`; see
[Development setup](../contributing/development#rust-tui-scaffold).

RC2 includes the native-wheel release pipeline, composer selection/undo/clipboard, semantic colors,
and complete saved-history loading. It is a trial of Rust as the default on the packaged platforms,
not a claim of universal terminal performance.

RC3 keeps `auto` and `rust` as compatibility selector spellings, but removes `textual`, `fullscreen`,
and `line` and the `--line` flag. It also adds selectable startup logos, improves full-history
hydration, and reduces RPC streaming and literal-search overhead. Python still controls providers,
tools, permissions, and saved sessions.

RC2 published an Intel macOS native wheel; RC3 does not. Intel macOS installs receive the pure wheel,
so print, JSON, RPC, and SDK continue to work, but interactive TUI startup fails with missing-binary
guidance. RC3 has no Python TUI fallback on that platform.

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
2. Support **live RPC v9**. Events carry no separate schema version; the protocol version is the
   single event contract. Wait for `rpc.handshake.accepted` before sending
   commands; handle rejection as a connection failure rather than attempting legacy fallback.
3. Honor negotiated directional frame limits and strict UTF-8, LF-terminated JSON framing.
4. Use backend-owned model and connection catalogs. Credential mutations belong to backend RPC;
   frontends must not read or write Wisp credential files themselves.

Use the checked-in `schemas/live-rpc/v9/` bundle and the typed Python transport as implementation
references. Versioned schema bundles are release assets, not part of the Python wheel API.
Historical bundles remain immutable. These live-connection requirements do not change the
backward-readability policy for persisted sessions.

The in-process Python SDK has no serialization boundary and does not perform a wire handshake.
The Rust TUI requires the exact Python package release. Source builds use the matching
checkout; native wheels are built and published in lockstep with the Python release.

## Trying a published candidate

After RC3's artifacts are published and verified, use its exact version in an explicit pin:

```bash
uvx --from "wisp-ai==0.2.0rc3" wisp --version
uvx --from "wisp-ai==0.2.0rc3" wisp
```

This avoids replacing an existing persistent `uv tool` installation, but the running application
still uses normal Wisp configuration and session locations. Use a disposable project and back up
important state when testing. Continue using [0.1.0 installation instructions](./installation)
if you do not want to opt into prerelease testing.

To return a persistent `uv tool` installation to the published RC2 after an RC3 regression, exit
Wisp, back up important sessions, then install the exact earlier version:

```bash
uv tool install --force "wisp-ai==0.2.0rc2"
wisp --version
```

This replaces the installed package and its paired native binary on supported platforms. Do not
delete session files. Older versions may not understand records written by newer versions, so keep
the backup and test session resume before relying on a downgraded install. On a platform without a
native wheel, RC2's frontend behavior differs; check its historical
[release notes](../contributing/rc2-release) before choosing it as a rollback.

## Before promoting to 0.2.0

Use the [RC3 release checklist](../contributing/rc3-release) for candidate publication, platform
installation, long-session measurements, and rollback gates.

- Require green CI and release-workflow verification/build checks on the exact candidate commit.
- Verify wheel and source-distribution metadata, installed SDK imports, `wisp --version`, and a
  fake-provider prompt outside the source checkout.
- Exercise Rust in real terminals: long streaming output while typing and scrolling,
  file-picker navigation, cancellation, approvals, and session resume.
- Verify pure-wheel print/JSON/RPC/SDK and the actionable Rust-unavailable error for interactive
  commands.
- Dogfood the published candidate and resolve release blockers before updating stable version
  pins or creating the final tag. Passing headless tests is not evidence of native-terminal
  visual correctness.

The [changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) records the release
scope. Publishing the candidate and publishing the final release are separate approval steps.
