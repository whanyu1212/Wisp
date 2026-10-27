---
title: Wisp 0.2.0rc8 release decision and checklist
description: Release decision, prepublication checklist, and verification record for Wisp 0.2.0rc8.
route: maintainers/releases/rc8-release.html
section: maintainers
order: 14
layout: archive
listed: false
searchable: false
legacySource: contributing/rc8-release.md
---

# Wisp 0.2.0rc8 release decision and checklist

Status: release preparation, October 16, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc8`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC7 (published October 15, 2026) is the latest candidate. The [RC7 checklist](rc7-release.md) records
the decisions RC8 keeps.

## Candidate decision

RC8 is a feature candidate on top of RC7. It keeps RC7's platforms (native wheels for macOS arm64 and
Linux glibc 2.28+ x86_64, plus a pure wheel). The version pair is Python `0.2.0rc8` / Cargo
`0.2.0-rc.8` and must match exactly.

It completes the MCP management work (HY-256) and adds web search:

- **MCP servers without editing `settings.json`** (HY-257, HY-258, HY-260; #695–#698): `wisp mcp`
  commands, live RPC changes, and the `/mcp` manager, with confirmations that show exactly what a
  server runs.
- **Asking Wisp to set up a server** (HY-259, #699): confirm-every-call proposal tools that no grant
  or permission mode approves, with secret and hidden-field checks in both the backend and the TUI.
- **Hosted MCP authentication** (HY-262, #701–#703): `headers` and `headers_from` pinned to the
  server's origin, OAuth sign-in through the MCP SDK's provider with tokens in
  `~/.wisp/mcp-auth.json`, and `/mcp` sign-in.
- **Provider-native web search** (HY-263, #700): a user-only, default-off `web_search` setting for
  Anthropic, OpenAI, xAI, and the Codex login.

Unlike RC5–RC7, RC8 changes the live RPC contract (new commands, events, and fields), so its backend
and frontend must be paired, as the handshake already enforces. Release preparation made
`UsageCost.web_searches` optional so that sessions in which no web search ran stay readable by RC7;
see [Rollback](#rollback).

Still open and not in RC8: HY-265 (web searches in the TUI and `/search`), HY-266 (Google search),
HY-252 (home-folder YOLO and trust), HY-261 and HY-264 (`/mcp` conveniences), HY-253 (PTY approval
test), and HY-254 (`edit` diff line numbers).

## Prepublication acceptance

- [ ] The exact PR head has terminal green CI and no actionable review threads.
- [ ] Python formatting, lint, configured mypy, full tests, and the live RPC schema check pass.
- [ ] The dependency-drift workflow passes on the newest allowed dependencies.
- [ ] Rust formatting, Clippy, workspace tests, build, and the Python/Rust handoff tests pass. The
  macOS handoff step still runs with `continue-on-error` (HY-29); check its failure annotations.
- [ ] `mdbook build` succeeds.
- [ ] The release workflow's dry run verifies version parity, one sdist, one pure wheel, two native
  wheels, wheel content parity, checksums, SBOMs, and the live RPC schema bundle.

## Publication and public-install acceptance

After this preparation PR merges, publishing requires a separate `v0.2.0rc8` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc8"` on each
  claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc8` and
  `wisp-tui --version` reports `0.2.0-rc.8`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved; RC8's OAuth support relies on the SDK's
  `OAuthClientProvider`.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

RC8's features were verified with unit, reducer, PTY, and fixture-server tests, not against live
services. Before treating RC8 as a stable-promotion candidate, use the published install in a real
terminal with real providers:

- [ ] `/mcp` → `a`, paste a published stdio snippet (for example GitHub's), choose forward or enter
  for each variable, confirm, and see the server connect with its tools; `e`, `d`, and `c` behave as
  described.
- [ ] `wisp mcp add`, `import`, `list`, `enable`, `disable`, and `remove` round-trip, and `list`
  never prints a value.
- [ ] Ask Wisp to "add the GitHub MCP server": the proposal panel shows the exact command, offers
  only Confirm or Deny, and the server's tools are available from the next message.
- [ ] With `web_search` on: Anthropic, OpenAI, and the Codex login each answer a question that needs
  a search; the reply's cost shows `≥`; a session with a search resumes. Note which models rejected
  the search tool, if any.
- [ ] A header-authenticated HTTP server (`--header-from "Authorization=Bearer VAR"`) connects; with
  the variable unset it shows a value-free "unavailable" reason.
- [ ] OAuth: add Linear's MCP server by URL, press `l` in `/mcp`, finish in the browser, and see it
  connect; restart Wisp and confirm it reconnects without a browser; `o` signs out and the server
  shows "sign in required". Note whether the authorization server required a pre-registered client
  (not yet supported).
- [ ] `Esc` during a sign-in cancels it; a prompt sent during a sign-in gets a notice instead of
  waiting.

## Rollback

If RC8 regresses, exit Wisp and install RC7 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc7"`. Before that:

- remove any MCP server that uses `headers` or `headers_from` from `~/.wisp/settings.json` (RC7
  ignores the whole file otherwise), noting them to re-add later;
- expect RC7 to report sessions in which a web search ran as malformed; sessions without one open
  normally.

`~/.wisp/mcp-auth.json` is ignored by RC7 and can stay. Check `wisp --version` and session resume
before relying on the downgraded install, and never delete or hand-edit sessions as a rollback step.
