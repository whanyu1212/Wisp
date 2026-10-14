# Upgrading to Wisp 0.2

This guide describes the 0.2 candidate series and the checks required before stable promotion.
RC2 introduced the Rust-default trial on native wheels; RC3 removes the Python terminal renderers.
RC4 adds prompt-cache improvements, removes the remaining compatibility shims, and requires the
backend and frontend to be the same Wisp release. RC5 fixes stdio MCP servers and `wisp update` in
public installs.
Check the
[release page](https://github.com/whanyu1212/Wisp/releases) for the latest
published candidate before installing one.

## What changes for terminal users

`wisp`, `wisp tui`, and `wisp --mode tui` launch Rust. Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the binary and Python
backend. Pure-wheel installs retain print, JSON, RPC, and SDK but interactive commands fail with
actionable missing-binary guidance. Build a matching Rust binary for a source checkout and set its
absolute path in `WISP_RUST_TUI_BINARY`; see
[Development setup](../contributing/development#rust-tui-scaffold).

RC2 includes the native-wheel release pipeline, composer selection/undo/clipboard, semantic colors,
and complete saved-history loading. It is a trial of Rust as the default on the packaged platforms,
not a claim of universal terminal performance.

RC3 removes the `textual`, `fullscreen`, and `line` renderers and the `--line` flag; later 0.2
candidates also remove the `auto`/`rust` renderer selector. It also adds selectable startup logos, improves full-history
hydration, and reduces RPC streaming and literal-search overhead. Python still controls providers,
tools, permissions, and saved sessions.

RC2 published an Intel macOS native wheel; RC3 does not. Intel macOS installs receive the pure wheel,
so print, JSON, RPC, and SDK continue to work, but interactive TUI startup fails with missing-binary
guidance. RC3 has no Python TUI fallback on that platform.

Only session files written in the current entry schema (v6) load. Older files are rejected rather
than migrated. Python continues to own persistence. Back up important sessions before testing a
candidate; older releases are not promised to understand newly written records.

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
2. Connect only to a backend from the same Wisp release. Events carry no schema version and there
   is no cross-release event contract. Wait for `rpc.handshake.accepted` before sending
   commands; handle rejection as a connection failure rather than attempting legacy fallback.
3. Honor negotiated directional frame limits and strict UTF-8, LF-terminated JSON framing.
4. Use backend-owned model and connection catalogs. Credential mutations belong to backend RPC;
   frontends must not read or write Wisp credential files themselves.

Use the checked-in `schemas/live-rpc/` bundle and the typed Python transport as implementation
references. The schema bundle is a release asset, not part of the Python wheel API.

The in-process Python SDK has no serialization boundary and does not perform a wire handshake.
The Rust TUI requires the exact Python package release. Source builds use the matching
checkout; native wheels are built and published in lockstep with the Python release.

## Trying a published candidate

After RC5's artifacts are published and verified, use its exact version in an explicit pin:

```bash
uvx --python 3.12 --from "wisp-ai==0.2.0rc5" wisp --version
uvx --python 3.12 --from "wisp-ai==0.2.0rc5" wisp
```

Keep `--python 3.12` (or any newer Python). Without it, uv resolves for its default interpreter first;
if that is older than 3.12, uv selects the pure wheel and the install has no Rust TUI. See
[Installation](./installation#the-tui-does-not-start-after-installing-a-02-candidate).

This avoids replacing an existing persistent `uv tool` installation, but the running application
still uses normal Wisp configuration and session locations. Use a disposable project and back up
important state when testing. Continue using [0.1.0 installation instructions](./installation)
if you do not want to opt into prerelease testing.

To return a persistent `uv tool` installation to the published RC4 after an RC5 regression, exit
Wisp, back up important sessions, then install the exact earlier version:

```bash
uv tool install --force --python 3.12 --with "mcp==2.0.0" "wisp-ai==0.2.0rc4"
wisp --version
```

RC4 and RC5 read each other's sessions. `--with "mcp==2.0.0"` keeps stdio MCP servers working in
RC4, which RC5 fixes for newer `mcp` releases.

This replaces the installed package and its paired native binary on supported platforms. Do not
delete session files. Older versions may not understand records written by newer versions. In
particular, RC3 cannot open a session in which RC4 or RC5 saved a response from a provider that
sends a prompt-cache key (such as Codex or OpenAI); it reports a malformed session entry. Back up
sessions before going back to RC3, and test session resume before relying on a downgraded install.

## Before promoting to 0.2.0

Use the [RC5 release checklist](../contributing/rc5-release) for candidate publication, platform
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
