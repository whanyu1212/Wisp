---
title: Wisp 0.2.0rc11 release decision and checklist
description: Release decision, prepublication checklist, and verification record for Wisp 0.2.0rc11.
route: maintainers/releases/rc11-release.html
section: maintainers
order: 11
layout: archive
listed: false
searchable: false
legacySource: contributing/rc11-release.md
---

# Wisp 0.2.0rc11 release decision and checklist

Status: release preparation, October 17, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc11`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC10 (published October 16, 2026) is the latest candidate. The [RC10 checklist](rc10-release.md)
records the decisions RC11 keeps.

## Candidate decision

RC11 is a feature and performance candidate on top of RC10. It keeps RC10's platforms (native
wheels for macOS arm64 and Linux glibc 2.28+ x86_64, plus a pure wheel) and its settings, trust, and
permission formats. The version pair is Python `0.2.0rc11` / Cargo `0.2.0-rc.11` and must match
exactly.

It contains:

- **Web search in the TUI** (HY-265, #710). `/search` shows the provider-native web search setting
  and `/search on|off` changes it for the running session without saving a default. Replies show
  bounded search evidence (queries, opened pages, failures, source titles and hosts), restored from
  saved `web.search` events when a session loads. Costs that exclude search fees show as lower
  bounds.
- **Scrollback gap** (#711). Scrolled-back prompts keep a plain row above their highlighted block.
- **Tool-call latency.** Bash skips post-exit process scans when nothing survived (#712); valid
  UTF-8 output decodes without per-character objects (#716); the Codex provider reuses one HTTP
  client and keeps its connection (#717). The prompt asks the model to batch independent read-only
  lookups (#713); `grep` and `find` in a batch run one at a time because parallel scans contend for
  the GIL (#718); calls rejected during preparation keep their batch parallel (#714); and a regex
  line that times out under load gets one retry from a per-search budget (#719). #715 extends the
  tool benchmarks and ships nothing.

#710 adds `ConfigureCommand.web_search`, `CodingSessionState.web_search`, and
`RpcMessageSnapshot.web_searches`, so the backend and frontend must be paired, which the handshake
already enforces. None of these is written to session files. Before this preparation, an RC11
session with a web search, a parallel batch, and a denied tool approval was read with the installed
RC10: `read_entries`, `read_context`, and `read_typed_events()` read it; `get_messages` over RPC
returned all 10 messages; `--continue` resumed it with the fake provider; and RC11 read the
resumed file back.

Still open and not in RC11: HY-266 (Google search), HY-261 and HY-264 (`/mcp` conveniences), HY-253
(PTY approval test), HY-254 (`edit` diff line numbers), and HY-29 (macOS handoff test flakes).

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc11` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc11"` on
  each claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc11` and
  `wisp-tui --version` reports `0.2.0-rc.11`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

- [ ] With a provider that supports web search, `/search` reports the setting, `/search on` makes a
  later reply that searches show its queries and sources, and `/search off` stops it. Restarting
  returns to the saved default. Reloading the session shows the same evidence with the reply.
- [ ] Scrolling back over earlier prompts shows a blank row between the previous reply and each
  highlighted prompt block.
- [ ] Short bash commands (`true`, `git status`) return promptly, and a long-running background job
  started from bash is still cleaned up when the command exits.
- [ ] A turn that asks for several independent lookups sends them as one batch; `grep` and `find`
  in it complete without regex time-limit errors on a busy machine.
- [ ] With the ChatGPT/Codex provider, a multi-turn tool loop completes normally and `wisp` exits
  cleanly.
- [ ] The RC10 checklist's dogfooding items (home-folder trust and YOLO, trackpad scrolling) still
  pass.

## Rollback

If RC11 regresses, exit Wisp and install RC10 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc10"`. RC10 resumes, lists, and reads RC11
sessions, including with `read_typed_events()`, but does not show their web search evidence.
`/search` toggles were never saved, so RC10 uses the `web_search` setting or `WISP_WEB_SEARCH` as
before. Check `wisp --version` and session resume before relying on the downgraded install, and
never delete or hand-edit sessions as a rollback step.
