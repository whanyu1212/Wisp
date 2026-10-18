# Wisp 0.2.0rc9 release decision and checklist

Status: release preparation, October 16, 2026. This PR prepares the candidate; it does not create
`v0.2.0rc9`, publish packages, or change the stable 0.1.0 installation pin. At preparation time,
RC8 (published October 16, 2026) is the latest candidate. The [RC8 checklist](./rc8-release) records
the decisions RC9 keeps.

## Candidate decision

RC9 is a fix candidate on top of RC8. It keeps RC8's platforms (native wheels for macOS arm64 and
Linux glibc 2.28+ x86_64, plus a pure wheel), its live RPC contract, and its session and settings
formats. The version pair is Python `0.2.0rc9` / Cargo `0.2.0-rc.9` and must match exactly.

It fixes two problems found while dogfooding RC8 with Linear's hosted MCP server (#705):

- **Large tool catalogs were rejected.** Linear exposes 68 tools; RC8 capped one server at 64 and
  rejected the whole catalog, reporting it as "invalid" because the MCP client's task group wrapped
  the real reason. The per-server count is removed (definition size, discovery pages, and the
  256-tool total remain), and rejections report their real reason.
- **Wisp proposed a bridge instead of the URL.** Asked to add Linear, RC8 proposed
  `npx -y mcp-remote https://mcp.linear.app/mcp`, which waits for its own browser sign-in and timed
  out at startup, and which `l` could not sign in. Proposals now use a hosted server's URL with
  Wisp's own OAuth; a bridge to a Streamable HTTP endpoint is refused, while a bridge to a legacy
  SSE-only server stays allowed.

Before this preparation, the fix was verified against Linear's real server with a stored sign-in:
all 68 tools registered on `mcp` 2.0.0 and 2.3.0, an expired access token refreshed without a
browser, and an authenticated `list_teams` call succeeded.

None of these touch `src/wisp/events`, `src/wisp/sessions`, `src/wisp/agent/messages.py`,
`src/wisp/config`, or `schemas/`, so RC8 and RC9 read each other's sessions and settings.

Still open and not in RC9: HY-265 (web searches in the TUI and `/search`), HY-266 (Google search),
HY-252 (home-folder YOLO and trust), HY-261 and HY-264 (`/mcp` conveniences), HY-253, and HY-254.

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

After this preparation PR merges, publishing requires a separate `v0.2.0rc9` tag through the
existing release workflow.

- [ ] The tag points to the accepted commit, and every release job succeeds, including attestation,
  PyPI publication, and the GitHub release.
- [ ] PyPI and the GitHub release have the four distributions, with matching SHA-256 hashes, and
  `gh attestation verify` succeeds for each.
- [ ] Outside the source checkout, `uv tool install --force --python 3.12 "wisp-ai==0.2.0rc9"` on each
  claimed native target installs the native wheel: `wisp --version` reports `0.2.0rc9` and
  `wisp-tui --version` reports `0.2.0-rc.9`.
- [ ] With a stdio MCP server configured, the published install starts it and registers its tools.
  Record the `mcp` version the install resolved.
- [ ] The pure wheel in a clean environment: print, JSON, SDK, and RPC work; interactive `wisp`
  reports the actionable missing-TUI error.

## Real-terminal dogfooding

- [ ] With Linear added by URL and signed in, `/mcp` shows it connected with all its tools and
  "signed in", and a prompt can use a Linear tool.
- [ ] Remove Linear, then ask Wisp to "add the Linear MCP server": it proposes
  `https://mcp.linear.app/mcp` by URL, the confirmation shows "connects to", and after confirming,
  `l` signs in.
- [ ] `l` on a command-based server shows how to add it by URL.
- [ ] The RC8 checklist's remaining dogfooding items (`/mcp` paste flow, `wisp mcp` commands, web
  search with real providers) still pass.

## Rollback

If RC9 regresses, exit Wisp and install RC8 with
`uv tool install --force --python 3.12 "wisp-ai==0.2.0rc8"`. RC8 reads RC9 sessions and settings,
but rejects an MCP server with more than 64 tools (such as Linear's). Check `wisp --version` and
session resume before relying on the downgraded install, and never delete or hand-edit sessions as a
rollback step.
