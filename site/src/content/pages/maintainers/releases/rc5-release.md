---
title: Wisp 0.2.0rc5 release decision and checklist
description: Release decision, prepublication checklist, and verification record for Wisp 0.2.0rc5.
route: maintainers/releases/rc5-release.html
section: maintainers
order: 16
layout: archive
listed: false
searchable: false
legacySource: contributing/rc5-release.md
---

# Wisp 0.2.0rc5 release decision and checklist

Status: release preparation, October 14, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc5`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC4 (published October 14, 2026) is the latest candidate. The [RC4 checklist](rc4-release.md) records
the decisions RC5 keeps.

## Candidate decision

RC5 is a fix-only candidate on top of RC4. It keeps RC4's platforms (native wheels for macOS arm64
and Linux glibc 2.28+ x86_64, plus a pure wheel), its live RPC contract, and its session format. The
version pair is Python `0.2.0rc5` / Cargo `0.2.0-rc.5` and must match exactly.

It fixes two problems found by checking the published RC4 install:

- **Stdio MCP servers failed to start** in every public RC3 and RC4 install (HY-236). Public installs
  resolve `mcp` 2.3.0, where a private helper Wisp called became async; the HTTP transport's shutdown
  deadline was also bypassed from `mcp` 2.2.0. CI did not see either, because `uv.lock` pins
  `mcp` 2.0.0. RC5 no longer depends on those helpers, and a new scheduled
  [dependency-drift workflow](https://github.com/whanyu1212/Wisp/blob/main/.github/workflows/dependency-drift.yml)
  tests the newest dependency versions that public installs receive.
- **`wisp update` could drop the Rust TUI** (HY-235). When uv's default Python is older than 3.12,
  `uv tool install` selects the pure wheel. `wisp update` now passes the running Python version, and
  the documented install commands add `--python 3.12`.

No session, event, or protocol format changed since RC4, so RC4 and RC5 read each other's sessions.
RC3 still cannot open sessions in which RC4 or RC5 saved a response with a prompt-cache key; see the
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

After this preparation PR merges, publishing requires a separate `v0.2.0rc5` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc5"` on each
  claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc5` and
  `wisp-tui --version` reports `0.2.0-rc.5`.
- [ ] With a stdio MCP server configured (for example `npx -y exa-mcp-server`), the published install
  starts it and registers its tools. Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Rollback

If RC5 regresses, exit Wisp and install RC4 with
`uv tool install --force --python 3.12 --with "mcp==2.0.0" "wisp-ai==0.2.0rc4"`. The `mcp` pin keeps
stdio MCP servers working in RC4. RC4 opens RC5 sessions. Check `wisp --version` and session resume
before relying on the downgraded install, and never delete or hand-edit sessions as a rollback step.
