# Wisp 0.2.0rc3 release decision and checklist

Status: release preparation, September 17, 2026. This PR prepares the candidate; it does not
create `v0.2.0rc3`, publish packages, or change the stable 0.1.0 installation pin. At preparation
time, RC2 is the latest published candidate. The [RC2 checklist](./rc2-release) records its earlier
Rust-default trial and Python fallback; those renderer commands do not describe RC3.

## Candidate decision

Rust is RC3's only interactive terminal interface. `wisp`, `wisp tui`, and `wisp --mode tui` launch
the Rust frontend; `auto` and `rust` remain accepted selector spellings. Textual, Python fullscreen,
line mode, and `--line` have been removed. There is no Python renderer to fall back to if a native
binary is missing or fails. The launcher reports a recoverable error instead of silently changing
interfaces.

The macOS arm64 and Linux glibc 2.28+ x86_64 native wheels contain both the Rust TUI and the Python
backend. Intel macOS, Linux arm64, musl/Alpine, and Windows have no claimed native TUI wheel. A pure
wheel or source install still supports print, JSON, RPC, and the Python SDK; starting an interactive
TUI there explains how to obtain a compatible native wheel or build a matching Rust binary. A source
checkout on a supported OS can set an absolute `WISP_RUST_TUI_BINARY` path. The launcher does not
search `PATH`. Python remains authoritative for agent execution, authentication, tools, trust,
permissions, and append-only session persistence. The version pair is Python `0.2.0rc3` / Cargo
`0.2.0-rc.3` and must match exactly.

RC3 also includes the mdBook documentation migration, selectable startup logos, Rust pending-text
integration, faster full-history hydration, RPC delta coalescing, and measured search-path and
literal-`grep` optimizations. The
[changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) gives the full candidate scope.
These changes did not alter the live RPC v8 / event schema v39 contract that RC3 shipped, and did
not require a saved session migration. (Later releases replaced the per-event schema version with
protocol v9; see [Compatibility and versioning](../reference/compatibility.md).) Large saved histories remain fully available; startup time and memory can still
grow with the amount of retained content.

Transcript search, built-in arbitrary transcript copying, clickable Markdown links, and an
integrated update/restart dialog remain unavailable in Rust. Terminal-native selection depends on
the terminal and mouse-capture settings. These are accepted candidate limitations to assess during
dogfooding, not evidence of feature parity with RC2's Python renderer.

## Prepublication acceptance

- [ ] The exact PR head has terminal green CI and no actionable review threads.
- [ ] Python formatting, lint, configured mypy, full tests, and the live RPC schema check pass.
- [ ] Rust formatting, Clippy, workspace tests, build, and Python/Rust handoff smoke tests pass.
- [ ] `mdbook build` succeeds, and the rendered book has no broken internal links.
- [ ] The release workflow's dry-run build verifies project/runtime version parity, one sdist, one
  pure fallback wheel, two native wheels, wheel metadata/content parity, checksums, SBOMs, and the
  live RPC schema bundle. No RC2 version remains in RC3 artifact metadata.
- [ ] Native-wheel CI runs `scripts/verify_rust_tui_install_lifecycle.py` on both claimed targets.
  Its installed-package checks include no-Cargo launch, an empty-history and 10,001-message PTY
  prompt, terminal restoration, native-to-pure replacement, actionable pure-TUI failure, working
  print/JSON/RPC/SDK, corrupt and non-executable binary errors, native restoration, offline
  reinstall, and clean uninstall. Inspect its JSON evidence rather than treating a local source
  build as installed-wheel evidence.
- [ ] Exercise a real terminal on each claimed native target: first launch, provider connection,
  approvals, cancellation, file selection, long streaming while typing and scrolling, and session
  resume. Confirm `/update` points to the external update command. Record terminal-specific
  copying/accessibility limits and unresolved regressions.

A session-content loss, wrong trust or approval decision, failed launch on a claimed native target,
broken package replacement, or terminal/process cleanup regression blocks the candidate. A measured
speedup in one benchmark is not a release-wide performance guarantee.

## Publication and public-install acceptance

After this preparation PR merges, publishing requires a separate `v0.2.0rc3` tag through the
existing release workflow. Do not manually upload a partial set of distributions.

- [ ] Confirm the tag points to the accepted commit and the workflow finishes successfully through
  provenance attestation, trusted PyPI publication, and GitHub release creation.
- [ ] Verify PyPI has one sdist, one pure wheel, and native wheels for macOS arm64 and Linux glibc
  2.28+ x86_64. Verify the GitHub release has those distributions plus the expected hashes, SBOMs,
  schema bundle, and per-target install evidence; verify provenance attestations for the artifacts.
- [ ] Outside the source checkout, install `wisp-ai==0.2.0rc3` from the public index without Cargo
  on each claimed native target. Check `wisp --version`, an interactive fake-provider prompt, session
  resume, and clean exit with terminal attributes restored.
- [ ] Install the public pure wheel on an unclaimed target or in a forced pure-wheel environment.
  Check print, JSON, RPC, SDK, and the actionable interactive error.
- [ ] Record artifact links and public-install results, then update the upgrade guide's publication
  status. Dogfood the candidate before deciding on 0.2.0 stable promotion.

## Rollback

If RC3's TUI regresses, exit Wisp, back up important sessions, and reinstall the published RC2
package with `uv tool install --force "wisp-ai==0.2.0rc2"`. Check `wisp --version` and session resume
before relying on the downgraded install. On the claimed native targets, the package replacement
also replaces the paired Rust binary; do not copy only one component between versions. RC2 retains
its own renderer behavior, including the Python fallback. Older versions may not read records
written by newer ones, so preserve the backup and never delete or migrate sessions as a rollback
step. For an isolated comparison that does not replace a persistent tool, use
`uvx --from "wisp-ai==0.2.0rc2" wisp` outside the source checkout.
