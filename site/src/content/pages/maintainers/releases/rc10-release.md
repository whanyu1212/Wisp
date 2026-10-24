---
title: Wisp 0.2.0rc10 release decision and checklist
description: Release decision, prepublication checklist, and verification record for Wisp 0.2.0rc10.
route: maintainers/releases/rc10-release.html
section: maintainers
order: 11
layout: archive
listed: false
searchable: false
legacySource: contributing/rc10-release.md
---

# Wisp 0.2.0rc10 release decision and checklist

Status: release preparation, October 16, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc10`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC9 (published October 16, 2026) is the latest candidate. The [RC9 checklist](rc9-release.md) records
the decisions RC10 keeps.

## Candidate decision

RC10 is a fix candidate on top of RC9. It keeps RC9's platforms (native wheels for macOS arm64 and
Linux glibc 2.28+ x86_64, plus a pure wheel) and its settings format. The version pair is Python
`0.2.0rc10` / Cargo `0.2.0-rc.10` and must match exactly.

It contains two fixes:

- **Home-folder YOLO and trust** (HY-252, #707). YOLO and trust were saved per project folder, so
  choosing them in `~`, or in a folder whose project root resolved to `~` through a stray marker or a
  dotfiles repository, applied to every project below it. Broad folders (home, its ancestors,
  filesystem and drive roots, temporary folders) now keep neither; the stores refuse to save them,
  older records are ignored with a startup notice, and the project-root search stops below home.
  Folders are matched by file identity, so other letter cases and macOS firmlinks are covered.
- **Trackpad scrolling** (HY-246, #708). Wheel input scrolled only with the pointer over the
  transcript rows and a painted frame available; with the pointer over the composer, or right after
  a resize, gestures were dropped. It now scrolls from anywhere in the conversation, and a scroll
  that waits on history during a reply says so. #685 (in RC6) fixed the earlier cause, input dropped
  while the agent worked.

#707 adds `session_only_yolo` to `ToolApprovalRequested` and `PermissionState`, so the backend and
frontend must be paired, which the handshake already enforces. Before this preparation, an RC10
session with a tool approval was read with the installed RC9: it resumed with `--continue`, listed,
and returned its messages over RPC; only `read_typed_events()` rejected it. Settings, trust, and
permission files keep their formats.

Still open and not in RC10: HY-265 (web searches in the TUI and `/search`), HY-266 (Google search),
HY-261 and HY-264 (`/mcp` conveniences), HY-253 (PTY approval test), HY-254 (`edit` diff line
numbers), and HY-29 (macOS handoff test flakes).

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc10` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc10"` on
  each claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc10` and
  `wisp-tui --version` reports `0.2.0-rc.10`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

- [ ] Start `wisp` in `~`: a startup notice names the folder; the trust prompt appears; choosing
  approval choice 3 reads "YOLO for this session" and nothing is written under
  `~/.wisp/permissions/`; restarting asks for trust again.
- [ ] `wisp trust allow ~` exits 1 with the `WISP_TRUST=1` hint; `wisp trust status ~` reports
  "asked each session".
- [ ] In a project folder below `~`, YOLO and trust are still saved and survive a restart.
- [ ] In Ghostty with a trackpad on macOS: after a long reply, scrolling up with the pointer over
  the composer reaches earlier turns, also while a run streams tool output and right after resizing
  the window or a split. (HY-246's remaining acceptance item.)
- [ ] The RC9 checklist's dogfooding items (Linear by URL with OAuth, proposals by URL) still pass.

## Rollback

If RC10 regresses, exit Wisp and install RC9 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc9"`. RC9 resumes RC10 sessions; its SDK's
`read_typed_events()` rejects one in which a tool approval was requested. RC9 also honors YOLO or
trust that a release before RC10 saved for the home folder, which RC10 kept but ignored; remove
those first with `wisp trust forget ~` and Ask in `/permissions` from `~`. Check `wisp --version`
and session resume before relying on the downgraded install, and never delete or hand-edit sessions
as a rollback step.
