---
title: Wisp 0.2.0rc6 release decision and checklist
description: Release decision, prepublication checklist, and verification record for Wisp 0.2.0rc6.
route: maintainers/releases/rc6-release.html
section: maintainers
order: 16
layout: archive
listed: false
searchable: false
legacySource: contributing/rc6-release.md
---

# Wisp 0.2.0rc6 release decision and checklist

Status: release preparation, October 14, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc6`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC5 (published October 14, 2026) is the latest candidate. The [RC5 checklist](rc5-release.md) records
the decisions RC6 keeps.

## Candidate decision

RC6 is a presentation candidate on top of RC5. It keeps RC5's platforms (native wheels for macOS
arm64 and Linux glibc 2.28+ x86_64, plus a pure wheel), its live RPC contract, and its session format.
The version pair is Python `0.2.0rc6` / Cargo `0.2.0-rc.6` and must match exactly.

It refreshes the Rust TUI surfaces found rough while dogfooding RC5 (HY-247), and fixes one bug:

- **Dropped trackpad scrolling** (HY-246, #685). Tool output queued ahead of a wheel gesture
  invalidated it, so scrolling often did nothing while the agent worked. The remaining HY-246 items
  (ticks lost right after a resize, and stopping silently at the live-history gap in long
  sessions) are not in RC6.
- **Approval, trust, and `/permissions` dock above the composer** (HY-248, #686), with `↑`/`↓` and
  `Enter` selection and no default choice.
- **Cleaner diff cards** (HY-249, #687): counts on the action line, `⋯` between hunks, a `write`
  line-number gutter, and changed words marked. `edit` cards have no gutter yet (HY-254).
- **Shape-coded tool markers, one-row action lines, and the browse hint in the footer** (HY-250,
  #689).
- **The working row** shows the phase, elapsed time, and interrupt key (HY-251, #690).
- A refreshed provider model catalog (#684) and a redrawn Braille startup logo (#688).

None of these touch `src/wisp/events`, `src/wisp/sessions`, `src/wisp/agent/messages.py`, or
`schemas/`, so RC4, RC5, and RC6 read each other's sessions. RC3 still cannot open sessions in which a
later candidate saved a response with a prompt-cache key; see the
[RC4 checklist](rc4-release.md#candidate-decision).

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc6` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc6"` on each
  claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc6` and
  `wisp-tui --version` reports `0.2.0-rc.6`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

RC6's changes are visual and were verified with buffer and PTY tests only. Before treating RC6 as a
stable-promotion candidate, use the published install in a real terminal (for example Ghostty and
the macOS Terminal) with a real provider, in a project set to "Ask before changes":

- [ ] Trackpad scrolling reaches earlier turns while tools are running, not only while idle.
- [ ] A shell approval docks above the footer; `↓ ↓ Enter` allows the tool for the session, `4`
  denies, and a bare `Enter` right after the panel appears only shows the notice.
- [ ] `/permissions` docks above the composer, marks the current mode, and applies a mode only after
  it is highlighted.
- [ ] An `edit` that rewraps lines marks only the moved words; a `write` over an existing file shows
  line numbers; Enter-details still shows the full unified diff.
- [ ] Running tool cards spin and settle to `✓`, `✗`, or `⊘`; a long command stays on one row and
  Right in browse shows it in full.
- [ ] The working row shows the phase and elapsed time, and drops `Esc to interrupt` after Esc.
- [ ] `NO_COLOR=1`: markers, the spinner, diff signs, and changed words stay distinguishable.

## Rollback

If RC6 regresses, exit Wisp and install RC5 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc5"`. RC5 opens RC6 sessions. Check
`wisp --version` and session resume before relying on the downgraded install, and never delete or
hand-edit sessions as a rollback step.
