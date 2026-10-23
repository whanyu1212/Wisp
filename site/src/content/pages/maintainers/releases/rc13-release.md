---
title: Wisp 0.2.0rc13 release decision and checklist
description: 'Status: release preparation, October 18, 2026. This PR prepares the candidate; it does not create v0.2.0rc13, publish packages, or change the stable 0.1.0 installation pin. At…'
route: maintainers/releases/rc13-release.html
section: maintainers
order: 43
layout: archive
listed: false
searchable: false
legacySource: contributing/rc13-release.md
---

# Wisp 0.2.0rc13 release decision and checklist

Status: release preparation, October 18, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc13`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC12 (published October 17, 2026) is the latest candidate. The [RC12 checklist](rc12-release.md)
records the decisions RC13 keeps.

## Candidate decision

RC13 is a Python runtime candidate on top of RC12, with one Rust TUI fix. It keeps RC12's platforms
(native wheels for macOS arm64 and Linux glibc 2.28+ x86_64, plus a pure wheel) and its settings,
trust, and permission formats. The version pair is Python `0.2.0rc13` / Cargo `0.2.0-rc.13` and
must match exactly.

It contains:

- **No flicker when a reply finishes** (#726). The transcript keeps its layout between the reply
  completing and the command finishing.
- **The Git snapshot is saved as session data** (#732). A trusted run saves the snapshot its project
  context used on that message entry, and a resume reads it back instead of parsing the prompt text.
- **Faster session loading** (#736). Each saved entry is parsed once instead of twice.
- **Custom prompts get the web-search guidance** (#733), as the default prompt does.
- **Agent package cleanup** (#727–#731, #734, #737). `wisp.agent.prompt`, `wisp.agent.mode`,
  `wisp.agent.messages`, and `wisp.agent.history` moved to `wisp.coding.prompt`, `wisp.events`, and
  `wisp.messages` with no re-export. None is in the documented SDK surface; the
  [upgrade guide](../../guide/upgrading.md#python-integrations) lists the new paths.
- **Benchmarks run again** (#735), and CI runs their PTY tests.

No command, event, or handshake model changed, and the live RPC schema bundle is unchanged. The
session entry gains one optional field, so `schema_version` stays 6 and every earlier session still
loads. The reverse is not true: **RC12 and earlier cannot resume a session a trusted RC13 run
wrote**. The backend and frontend must still be the same release, which the handshake enforces.

Still open and not in RC13: HY-266 (Google search), HY-261 and HY-264 (`/mcp` conveniences), HY-253
(PTY approval test), HY-254 (`edit` diff line numbers), HY-289 (further resume cuts), and HY-29
(macOS handoff test flakes).

## Prepublication acceptance

- [ ] The exact PR head has terminal green CI and no actionable review threads.
- [ ] Python formatting, lint, configured mypy, full tests, and the live RPC schema check pass.
- [ ] The dependency-drift workflow passes on the newest allowed dependencies.
- [ ] Rust formatting, Clippy, workspace tests, build, and the Python/Rust handoff and benchmark PTY
  tests pass. The macOS handoff step still runs with `continue-on-error` (HY-29); check its failure
  annotations.
- [ ] `mdbook build` succeeds.
- [ ] The release workflow's dry run verifies version parity, one sdist, one pure wheel, two native
  wheels, wheel content parity, checksums, SBOMs, and the live RPC schema bundle.

## Publication and public-install acceptance

After this preparation PR merges, publishing requires a separate `v0.2.0rc13` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc13"` on
  each claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc13` and
  `wisp-tui --version` reports `0.2.0-rc.13`.
- [ ] In the installed package, `from wisp.messages import Message` and
  `from wisp.coding.prompt import build_prompt_messages` work, and `import wisp.agent.messages`
  fails.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

- [ ] A streamed reply finishes without the transcript jumping or the working row flashing back,
  at the default size and in a short terminal.
- [ ] In a trusted Git project, start a session, edit a file outside Wisp, quit, and resume: the
  resumed prompt still shows the original Git snapshot, and `git status` through Wisp shows the
  edit.
- [ ] Resuming a long saved session reaches the composer and keeps every earlier prompt reachable
  by scrolling.
- [ ] Plan mode and `/search` still behave as in RC12.

## Rollback

If RC13 regresses, exit Wisp and install RC12 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc12"`. RC12 reads RC13 settings and trust
records unchanged. It lists every RC13 session but reports one that a trusted RC13 run wrote as
malformed when resuming it; sessions from untrusted projects, and sessions written before RC13,
still load. Back up sessions before testing RC13, start a new session after going back, check
`wisp --version` and session resume, and never delete or hand-edit sessions as a rollback step.
