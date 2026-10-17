# Wisp 0.2.0rc12 release decision and checklist

Status: release preparation, October 17, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc12`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC11 (published October 17, 2026) is the latest candidate. The [RC11 checklist](./rc11-release)
records the decisions RC12 keeps.

## Candidate decision

RC12 is a Rust TUI candidate on top of RC11. It keeps RC11's platforms (native wheels for macOS
arm64 and Linux glibc 2.28+ x86_64, plus a pure wheel) and its settings, trust, permission, and
session formats. The version pair is Python `0.2.0rc12` / Cargo `0.2.0-rc.12` and must match
exactly.

It contains:

- **Scrolling keeps prompts visible** (#722). A history page that lands while the row above an
  earlier prompt is at the top no longer blanks the view, and the latest prompt stays pinned while
  you scroll back through its reply.
- **Drag to copy** (#722). Dragging over transcript text highlights it and releasing copies it
  through the native clipboard and OSC 52. Mouse capture now also requests button-drag reports
  (`?1002h`). A selection ends on any press, wheel scroll, resize, or layout change, and a release
  never acts as a click or replaces a recovery notice.
- **Scrolling during decisions** (#722). The wheel and transcript scroll keys move the conversation
  while an approval or trust prompt is docked. Clicks stay ignored, keys the prompt uses keep their
  meaning, and scrolling never replaces the decision's notice.
- **Jump to latest** (#722). A clickable `↓ latest · Ctrl+End` label sits on the blank row above
  the composer while scrolled back. The footer no longer shows the session name or path.
- **Tool-only costs** (#721). Responses with only tool calls no longer show `cost unknown`.

No command, event, or session-file model changed, and no Python backend file changed since
`v0.2.0rc11` apart from the version. The backend and frontend must still be the same release, which
the handshake enforces.

Still open and not in RC12: HY-266 (Google search), HY-261 and HY-264 (`/mcp` conveniences), HY-253
(PTY approval test), HY-254 (`edit` diff line numbers), and HY-29 (macOS handoff test flakes).
Drag selection copies on-screen rows only: wrapped lines copy as separate lines, and dragging past
the edge does not scroll.

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc12` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc12"` on
  each claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc12` and
  `wisp-tui --version` reports `0.2.0-rc.12`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

Check in Ghostty and at least one of macOS Terminal, iTerm2, or tmux, since `?1002h` drag reporting
is new.

- [ ] Scrolling up one wheel tick from a long reply keeps its prompt pinned at the top until the
  prompt itself is reached; earlier prompts never disappear while scrolling.
- [ ] Dragging over transcript text highlights it; releasing shows "Copied N chars to clipboard."
  and the text pastes elsewhere. A plain click copies nothing, and the terminal's own bypass
  (Shift-drag in Ghostty) still selects natively. Inside tmux, copying works with
  `set-clipboard on`.
- [ ] During a run that asks for approval, the wheel and `PgUp`/`Ctrl+End` scroll the conversation
  while the prompt stays docked, and Up/Down/Enter and the number keys still answer it.
- [ ] Scrolled back, `↓ latest · Ctrl+End` appears centered above the composer, turns into
  `↓ new output` when output arrives, and clicking it returns to the latest output.
- [ ] A tool-only response shows no `cost unknown` label, live or after resuming the session.
- [ ] Wisp restores the terminal's mouse modes on exit: after quitting, the shell's own selection
  and scrolling work normally.

## Rollback

If RC12 regresses, exit Wisp and install RC11 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc11"`. RC12 changes only the Rust TUI, so
RC11 reads its sessions, settings, and trust records unchanged. RC11 has no drag-to-copy; use the
terminal's own selection or `WISP_TUI_MOUSE=0`. Check `wisp --version` and session resume before
relying on the downgraded install, and never delete or hand-edit sessions as a rollback step.
