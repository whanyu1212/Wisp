---
title: Wisp 0.2.0rc4 release decision and checklist
description: 'Status: release preparation, October 13, 2026. This PR prepares the candidate; it does not create v0.2.0rc4, publish packages, or change the stable 0.1.0 installation pin. At…'
route: maintainers/releases/rc4-release.html
section: maintainers
order: 50
layout: archive
listed: false
searchable: false
legacySource: contributing/rc4-release.md
---

# Wisp 0.2.0rc4 release decision and checklist

Status: release preparation, October 13, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc4`, publish packages, or change the stable 0.1.0 installation pin. At preparation time, RC3
(published September 17, 2026) is the latest candidate. The [RC3 checklist](rc3-release.md) records
its decisions; RC4 keeps them unless stated below.

## Candidate decision

RC4 keeps RC3's shape. Rust is the only interactive terminal interface. Native wheels for macOS
arm64 and Linux glibc 2.28+ x86_64 bundle the Rust TUI and the Python backend; pure wheels and source
installs keep print, JSON, RPC, and the Python SDK. The version pair is Python `0.2.0rc4` / Cargo
`0.2.0-rc.4` and must match exactly.

RC4 collects 69 commits since RC3. The
[changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) gives the full scope. The
changes users and integrators are most likely to notice:

- **One release, one contract.** Events no longer carry a per-event schema version and the handshake
  no longer carries a protocol version. A backend and frontend must be the same Wisp release: the
  Rust TUI and the Python SDK both reject a mismatched backend at connect time.
- **Removed compatibility shims.** Deprecated Python APIs and alias modules are gone, session files
  are read only in the current format, and catalog overlays must declare `schema_version = 2`.
- **Prompt caching.** Codex and xAI replay each response's own output items, Codex sends a
  `session_id` header, the project-context git status stays stable across prompts and restarts, and
  Anthropic requests now ask for caching (with a 1-hour lifetime in hosted sessions).
- **TUI.** Searchable, paged session catalog; `/queue` management; a Linux fix for keys pressed
  during a resize; and a fix for the RPC backend aborting at shutdown.
- **Runtime.** A caller's own cancellation during a tool batch now propagates; clone and fork no
  longer fail validation after a prompt in the same process.

The live RPC contract is the checked-in `schemas/live-rpc/` bundle; regenerate and check it with
`uv run python -m wisp.rpc.protocol_schema --check`. Saved sessions keep entry schema v6, so RC4
reads RC3 sessions without migration.

**Rollback compatibility.** RC3 cannot open a session in which RC4 saved a response carrying a
`prompt_cache_key` (responses from providers that send cache keys, such as Codex and OpenAI): RC3's
`ContextObservation` rejects the unknown field and reports a malformed session entry. Sessions RC4
only reads, and sessions with no such response, still open in RC3. This follows the session policy
(a new optional field does not bump the schema), but it makes the backup step in
[Rollback](#rollback) mandatory rather than precautionary.

Known limitations carried from RC3: no transcript search or built-in arbitrary transcript copying,
no clickable Markdown links, and no integrated update/restart dialog in the Rust TUI.

## Prepublication acceptance

- [ ] The exact PR head has terminal green CI and no actionable review threads.
- [ ] Python formatting, lint, configured mypy, full tests, and the live RPC schema check pass.
- [ ] Rust formatting, Clippy, workspace tests, build, and the Python/Rust handoff tests pass. The
  macOS handoff step still runs with `continue-on-error` (HY-29), so check its failure annotations,
  not only the job result.
- [ ] `mdbook build` succeeds, and the rendered book has no broken internal links.
- [ ] The release workflow's dry-run build verifies project/runtime version parity, one sdist, one
  pure fallback wheel, two native wheels, wheel metadata/content parity, checksums, SBOMs, and the
  live RPC schema bundle. No RC3 version remains in RC4 artifact metadata.
- [ ] Native-wheel CI runs `scripts/verify_rust_tui_install_lifecycle.py` on both claimed targets;
  inspect its JSON evidence rather than treating a local source build as installed-wheel evidence.
- [ ] Exercise a real terminal on each claimed native target with a real provider: first launch,
  provider connection, approvals, cancellation, queued steering and follow-ups, compaction, session
  search and resume, long streaming while typing and scrolling, shutdown, and terminal restoration.
  Use Anthropic for at least one multi-prompt session and record its cache reads with
  `scripts/cache_reuse_report.py`; that also sizes HY-33.

A session-content loss, wrong trust or approval decision, failed launch on a claimed native target,
broken package replacement, or terminal/process cleanup regression blocks the candidate.

## Publication and public-install acceptance

After this preparation PR merges, publishing requires a separate `v0.2.0rc4` tag through the
existing release workflow. Do not manually upload a partial set of distributions.

- [ ] Confirm the tag points to the accepted commit and the workflow finishes successfully through
  provenance attestation, trusted PyPI publication, and GitHub release creation.
- [ ] Verify PyPI has one sdist, one pure wheel, and native wheels for macOS arm64 and Linux glibc
  2.28+ x86_64. Verify the GitHub release has those distributions plus the expected hashes, SBOMs,
  schema bundle, and per-target install evidence; verify provenance attestations.
- [ ] Outside the source checkout, install `wisp-ai==0.2.0rc4` from the public index without Cargo
  on each claimed native target, with `--python 3.12` (see HY-235: without it, uv can select the pure
  wheel when its default interpreter is older than 3.12). Confirm `wisp-tui --version` reports
  `0.2.0-rc.4`. Check `wisp --version`, an interactive fake-provider prompt, session
  resume, and clean exit with terminal attributes restored.
- [ ] Install the public pure wheel in a forced pure-wheel environment. Check print, JSON, RPC, SDK,
  and the actionable interactive error.
- [ ] Record artifact links and public-install results, then update the upgrade guide's publication
  status. Dogfood the candidate before deciding on 0.2.0 stable promotion.

## Rollback

If RC4 regresses, exit Wisp and install the published RC3 package with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc3"`. Restore the session backup taken before trying RC4:
RC3 cannot open sessions in which RC4 saved a response with a prompt-cache key (see
[Candidate decision](#candidate-decision)). Check `wisp --version` and session resume before relying
on the downgraded install. On the claimed native targets, the package replacement also replaces the
paired Rust binary; do not copy only one component between versions. Never delete or hand-edit
sessions as a rollback step. For an isolated comparison that does not replace a persistent tool, use
`uvx --python 3.12 --from "wisp-ai==0.2.0rc3" wisp` outside the source checkout.
