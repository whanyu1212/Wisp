---
title: Wisp 0.2.0rc7 release decision and checklist
description: 'Status: release preparation, October 15, 2026. This PR prepares the candidate; it does not create v0.2.0rc7, publish packages, or change the stable 0.1.0 installation pin. At…'
route: maintainers/releases/rc7-release.html
section: maintainers
order: 49
layout: archive
listed: false
searchable: false
legacySource: contributing/rc7-release.md
---

# Wisp 0.2.0rc7 release decision and checklist

Status: release preparation, October 15, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc7`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC6 (published October 14, 2026) is the latest candidate. The [RC6 checklist](rc6-release.md) records
the decisions RC7 keeps.

## Candidate decision

RC7 is a fix candidate on top of RC6. It keeps RC6's platforms (native wheels for macOS arm64 and
Linux glibc 2.28+ x86_64, plus a pure wheel), its live RPC contract, and its session format. The
version pair is Python `0.2.0rc7` / Cargo `0.2.0-rc.7` and must match exactly.

It fixes one dead end found while dogfooding RC6, and polishes the screen that resolves it:

- **A failed prompt locked the composer** (HY-255, #693). Without credentials for the selected
  provider, the first prompt left "The prompt failed. Ctrl-C exits." as the only way out, so
  `/connect` was unreachable. A failed prompt now returns to idle and keeps its reason as a
  `✗ Prompt failed: …` row until the next command or a session change. A closed backend connection
  still ends the session.
- **The `/connect` API-key entry** (#693) docks above the composer, shows a cursor and a character
  count, and warns when an environment variable already supplies the key.
- **Bundled skill guidance** for `wisp-development` and `github-pr-delivery` (#692). No runtime
  change.

None of these touch `src/wisp/events`, `src/wisp/sessions`, `src/wisp/agent/messages.py`, or
`schemas/`, so RC4 through RC7 read each other's sessions.

Still open and not in RC7: HY-246's remaining scroll gaps, HY-252 (home-folder YOLO and trust),
HY-253 (PTY approval test), and HY-254 (`edit` diff line numbers).

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc7` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc7"` on each
  claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc7` and
  `wisp-tui --version` reports `0.2.0-rc.7`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

RC6's TUI changes and RC7's fixes were verified with buffer, reducer, and PTY tests only, and no
RC6 dogfooding results were recorded beyond the failed-prompt dead end that RC7 fixes. Before
treating RC7 as a stable-promotion candidate, use the published install in a real terminal (for
example Ghostty and the macOS Terminal) with a real provider, in a project set to "Ask before
changes":

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
- [ ] With no credentials for the selected provider (and its environment variable unset), the first
  prompt shows `✗ Prompt failed: … run /connect …`, the composer stays usable, `/connect` opens, the
  API-key entry docks above the composer with a visible cursor and character count, and the next
  prompt succeeds after saving the key.
- [ ] With the provider's environment variable set, the API-key entry says it takes precedence.
- [ ] After a failed prompt, `/new` or `/resume` drops the `✗ Prompt failed` row.

## Rollback

If RC7 regresses, exit Wisp and install RC6 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc6"`. RC6 opens RC7 sessions. Check
`wisp --version` and session resume before relying on the downgraded install, and never delete or
hand-edit sessions as a rollback step.
