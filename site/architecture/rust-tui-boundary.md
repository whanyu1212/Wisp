---
title: Rust terminal frontend boundary
---

# Rust terminal frontend boundary

| Field | Decision |
|---|---|
| Status | Accepted; experimental opt-in frontend, Textual remains default |
| Date | 2026-09-03 (renderer decision); boundary accepted 2026-08-24 |
| Tracking | [#456](https://github.com/whanyu1212/Wisp/issues/456), [#457](https://github.com/whanyu1212/Wisp/issues/457), [#463](https://github.com/whanyu1212/Wisp/issues/463), [#470](https://github.com/whanyu1212/Wisp/issues/470) |
| Renderer decision | Closed by [#470](https://github.com/whanyu1212/Wisp/issues/470): Textual remains the default; Rust remains experimental opt-in |

Wisp has an optional Rust terminal frontend over the existing Python JSONL-RPC runtime. The language
boundary is fixed: Rust does not become the default, Textual is not removed, and the agent runtime
stays in Python.

The #463 slice started as a transport-only experiment. The current experimental frontend accepts
prompts, approvals, trust answers, cancellation, steering and follow-up queues, bounded session
history, and provider connection flows. It does not claim Textual workflow or product parity.
Textual remains the default and the supported product frontend.

The governing rule is:

> Rust decides how frontend state is presented. Python decides what is allowed, what is durable,
> and what commands and events mean.

## Renderer decision (#470)

[#470](https://github.com/whanyu1212/Wisp/issues/470) is the closed renderer decision, not a deferred
gate.

1. Textual remains the default and the supported product frontend.
2. Rust is an experimental opt-in on macOS and Linux, source-build only. Current Python
   distributions do not include the binary.
3. Rollout stage is **2** (explicit experimental renderer flag). Stages 3–5 — supported opt-in with
   shipped artifacts, a default switch, and Textual deprecation — are not authorized.
4. Explicit Rust selection does not fall back to Textual. A missing, non-executable, incompatible,
   or failing binary is an actionable non-zero error. Users who want Textual pass
   `--renderer textual` or unset `WISP_TUI_RENDERER`.
5. Line and legacy fullscreen remain Python-only debug and compatibility paths. They have no Rust
   parity requirement.
6. Both frontends remain until a later explicit issue. [#418](https://github.com/whanyu1212/Wisp/issues/418)
   stays in scope because Textual remains supported.
7. Two-language lockstep cost is accepted only while Rust stays experimental. Supported opt-in
   (stage 3) requires [#467](https://github.com/whanyu1212/Wisp/issues/467),
   [#468](https://github.com/whanyu1212/Wisp/issues/468), and
   [#469](https://github.com/whanyu1212/Wisp/issues/469).
8. Rollback triggers for this decision: silent fallback to Textual, flipping the default without a
   new issue, or docs that recast Rust as supported or default.

A later default switch or Textual removal must be filed as a **new** issue. Completing #467, #468, or
#469 does not itself change the default.

Evidence for this hold, including measurements that could not run, is recorded in
`benchmarks/rust_tui_acceptance_evidence.md`.

## Context

The current Textual TUI already runs as a client of a separate `wisp --mode rpc` process. That
separation gives Wisp a useful migration seam: a Rust frontend can replace the latency-sensitive
terminal client without creating a second agent implementation.

Profiling behind [#270](https://github.com/whanyu1212/Wisp/issues/270),
[#418](https://github.com/whanyu1212/Wisp/issues/418), and
[#442](https://github.com/whanyu1212/Wisp/issues/442) attributes the largest recurring interactive
stalls to Textual layout and compositor work rather than provider execution or Markdown parsing.
The experiment therefore targets terminal presentation first. It is not evidence for rewriting
providers, tools, sessions, or the Python SDK.

Python remains the stronger ownership boundary for Wisp's semantic engine because the existing
runtime, provider integrations, extension model, public SDK, durability rules, and safety controls
are already typed and extensively tested there. Moving those concerns would multiply risk without
addressing the measured terminal bottleneck.

## Process topology

### Current Textual path

```mermaid
flowchart LR
  Launcher[Python launcher] --> Textual[Python TUI process<br/>TuiShell + Textual]
  Textual -->|spawns and owns| Backend[Python JSONL-RPC backend]
  Textual <-->|typed commands and events| Backend
  Backend --> Host[RPC command host]
  Host --> Session[CodingSession]
  Session --> Harness[AgentHarness]
  Harness --> Loop[run_agent_loop]
```

The current frontend process also contains several responsibilities that cannot be copied safely
into Rust: model-catalog derivation, credential operations, protected-path-aware file indexing, and
update operations. Those become backend capabilities or launcher responsibilities before the Rust
frontend reaches parity.

### Implemented optional Rust frontend path

```mermaid
flowchart LR
  Launcher[Python launcher] -->|starts and supervises| Rust[Rust terminal frontend]
  Launcher -.->|shared killable process group or job| Backend[Python JSONL-RPC backend]
  Rust -->|spawns for graceful protocol ownership| Backend
  Rust <-->|negotiated, bounded JSONL-RPC| Backend
  Backend --> Host[RPC command host]
  Host --> Session[CodingSession]
  Session --> Harness[AgentHarness]
  Harness --> Loop[run_agent_loop]
```

The Python launcher remains the trusted entrypoint. It resolves the selected renderer and Rust
executable, supplies the exact Python interpreter and opaque `wisp --mode rpc` backend command, and
runs preflight before terminal handoff. The experimental frontend does not ship a Rust binary in
Wisp's Python distributions; source development therefore supplies an absolute `WISP_RUST_TUI_BINARY`.

On macOS and Linux, the launcher starts Rust in a new process group and transfers the foreground
terminal to it. Rust spawns the Python backend in that inherited group, owns negotiated protocol
shutdown, and restores its raw-mode and alternate-screen changes. The Python launcher remains alive
as the external supervisor. On every exit path it checks and, if necessary, signals the entire
process group, then restores the original foreground process group, termios attributes, and a known
ANSI terminal baseline. Backend stdin reaching EOF is not treated as a cleanup guarantee. Windows
is rejected before binary resolution; no Windows supervision path is implemented.

Textual remains a separate, supported path through the same Python runtime. Explicit Rust selection
that cannot start because the binary is missing, incompatible, or fails during negotiation returns
an actionable non-zero error. It does not silently substitute Textual. Users can explicitly select
Textual. #470 closed without authorizing automatic fallback.

## Subsystem ownership

| Area | Authoritative owner | Frontend boundary |
|---|---|---|
| Agent loop and provider/tool continuation | Python | Rust receives typed lifecycle events only. |
| Harness transcript, steering, follow-ups, and cancellation state | Python | Rust projects authoritative queue and run state. |
| Durable sessions, branching, replay, and compaction | Python | Rust requests bounded current-version snapshots and never reads session JSONL. |
| RPC scheduling and command semantics | Python | Rust correlates command IDs but does not reinterpret command policy. |
| Providers, model execution, usage, and retries | Python | Rust presents sanitized catalog and lifecycle data. |
| MCP discovery and execution | Python | Rust presents typed status and tool events. |
| Tools and managed processes | Python | Rust renders calls and results; it never executes or supervises tools. |
| Trust and protected paths | Python | Rust solicits an explicit answer and sends it for authoritative validation. |
| Approval policy and scopes | Python | Rust cannot infer safety or approve a different call ID. |
| Authentication storage and refresh | Python | Rust accepts masked input and submits secrets through a dedicated command. |
| Effective provider/model/effort catalog | Python | Rust builds pickers from an authoritative snapshot. |
| Runtime and project configuration | Python | Rust displays effective state and sends typed configuration requests. |
| Project-file discovery | Python | Rust receives bounded, relative, policy-filtered suggestion data. |
| Update check and installation capability | Python | Rust presents notices, choices, progress, and restart guidance. |
| CLI print/JSON modes and Python SDK | Python | They remain independent supported interfaces. |
| Frontend reducer and command correlation | Rust | State is disposable and reconstructable from local actions plus RPC events. |
| Terminal input, resize, mouse, paste, and clipboard presentation | Rust | No runtime or safety policy is inferred from terminal input. |
| Frame pacing, rendering, scrolling, selection, and overlays | Rust | Rendering may coalesce eligible presentation work, never control semantics. |
| Markdown, syntax, diff, and tool-card presentation | Rust | All untrusted data is bounded and terminal-sanitized. |
| Themes, keymaps, layout, and other presentation preferences | Rust | Preferences cannot alter backend behavior or become session authority. |
| Rust binary selection and backend command construction | Python launcher | Rust does not discover an arbitrary interpreter or rebuild trusted arguments. |
| Invocation supervision and fail-safe cleanup | Python launcher and OS supervision primitive | Rust attempts graceful shutdown; the launcher terminates the shared process group or job when Rust cannot unwind. |
| Textual TUI | Python default frontend | It remains the default and supported frontend; Rust failures do not select it automatically. Removal requires a new explicit issue. |

## Migration map for the current TUI

This map classifies responsibilities, not files. The Rust frontend should not reproduce the Python
module graph or translate Textual widgets line by line.

| Current responsibility | Current location | Target disposition |
|---|---|---|
| Preflight, trusted configuration, backend argv/environment, renderer selection, external supervision | `tui/launch.py`, `tui/app.py` | Retain in the Python launcher. Add a shared killable process boundary for Rust and its backend without moving trust resolution into Rust. |
| Input/event coordination, command correlation, visible status, pending local submissions | `tui/shell.py`, `tui/state.py` | Reimplement as a terminal-independent Rust reducer driven by local actions and typed events. Python queue/run state remains authoritative. |
| Session catalog, selection, history hydration, paging, detail lookup | `tui/shell.py`, `tui/history.py` | Port client correlation and viewport projection. Continue loading and validating durable state through Python RPC. |
| Slash-command parsing and command catalog | `tui/commands.py`, `tui/shell.py` | Rust owns local dispatch and presentation; executable command metadata comes from Python. Local-only actions such as help and theme remain frontend-owned. |
| Model lookup, ambiguity handling, effort filtering, selection persistence | `tui/shell.py` | Move effective semantics behind the backend contract in #460 and #405. Rust renders and submits selections without copying the catalog. |
| Credential status, API-key persistence, disconnect, device-code login | `tui/auth_commands.py`, `tui/connections.py` | Move behind the secure Python contract in #461. Rust owns masked entry and progress presentation only. |
| Protected-path-aware snapshot construction and path ranking | `tui/file_index.py`, `tui/file_suggest.py` | Move safe discovery behind #462. Rust presents returned relative suggestions and may not independently walk the workspace. |
| Update checking, install capability, and update execution | `tui/update_commands.py`, `wisp.update_check` | Keep Python-owned. Rust presents notices and requests supported actions according to the launcher/distribution contract. |
| Terminal lifecycle, composer, overlays, pickers, mouse, focus, resize | Textual app, controllers, and widgets | Reimplement behavior in Rust. Do not port Textual private APIs, CSS, widget identity, or compositor workarounds. |
| Streaming cadence, transcript window, history viewport, card identity | Textual stream/history/transcript controllers | Reimplement bounded client-side state in Rust and validate semantic behavior with #459 traces. |
| Markdown, tool output, file results, process cards, and diffs | TUI presentation helpers and widgets | Reimplement bounded presentation from structured events. Execution and process lifecycle remain Python-owned. |
| Themes, theme persistence, keybindings, prompt-history search | TUI preference and input modules | Keep frontend-local and disposable. Any new persistent prompt-history policy requires separate privacy and lifecycle design. |
| Privacy-safe render and input-latency diagnostics | `tui/diagnostics.py` and benchmarks | Implement frontend-specific instrumentation with comparable scenarios, never content-bearing telemetry. |
| Line and legacy fullscreen renderers | `tui/rendering.py`, `tui/live.py` | Keep Python-only. No Rust parity requirement is implied. |
| Textual implementation and regression suite | `src/wisp/tui`, TUI tests | Retain as the default frontend and behavioral evidence. Removal requires a new explicit issue; #470 did not authorize it. |

## Wire-boundary rules

The live boundary permits only typed, bounded, JSON-serializable data required for frontend
interaction:

- current-version command envelopes, events, capability snapshots, and command results;
- explicit user prompts, queue actions, approval/trust answers, and configuration intents;
- bounded transcript, tool, process, catalog, status, and suggestion projections;
- relative display-safe project metadata that has already passed backend policy;
- a dedicated client-to-backend secret command for credentials, with redaction requirements.

The following never become Rust-owned data sources:

- session JSONL files or historical event schemas;
- credential files, refresh tokens, or provider SDK objects;
- project configuration files used to derive trust or protected paths;
- tool executors, managed subprocess handles, or approval policy;
- Python extension instances, MCP clients, or arbitrary in-process objects.

Secret-bearing commands are one-way submissions. Raw API keys, access tokens, and refresh tokens
must not be echoed in results or events, retained in prompt history, persisted in sessions, included
in fixtures, or written to logs and diagnostics.

## Live protocol and durable compatibility

The live frontend protocol and durable session formats are separate compatibility domains. The
experimental frontend is exact-lockstep rather than range-compatible:

- Python models are the source of truth for the live command and event schema.
- The committed schemas generate Rust data-transfer types at compile time; Rust types are not a
  handwritten second schema.
- The Python package/runtime and `wisp-tui` crate are currently both version `0.1.0`. The launcher
  passes the Python version to Rust, Rust checks it against `CARGO_PKG_VERSION` before spawning the
  backend, and the backend repeats its package version in the handshake.
- The only accepted live contract is RPC protocol v4 with event schema v36 and no negotiated
  capabilities. The frontend consumes current live event output, including backend-owned
  connection-catalog snapshots, and never reads credential files itself.
- A package, protocol, or event-schema mismatch fails before ordinary terminal interaction. The
  experimental frontend does not negotiate older live contracts.
- Python retains backward parsing, migration, and replay of persisted session and event versions.
- Rust receives current-version snapshots after Python has loaded historical data. It never needs
  implementations for old persisted schemas.

The committed v4 schema manifest and generated projections define the current handshake fields,
frame limits, strict event variants, and UTF-8 JSON representation. See
[Compatibility and versioning](../reference/compatibility) for Wisp's durable contracts.

## Lifecycle and failure ownership

| Failure or transition | Required owner and outcome |
|---|---|
| Rust binary missing or incompatible | The Python launcher reports an actionable non-zero failure for Rust selection. Textual remains explicitly selectable, but is not selected automatically. |
| Backend spawn failure | Rust restores the terminal and reports the sanitized failure; the launcher verifies the supervised boundary is empty and exits non-zero. |
| Protocol version mismatch | Negotiation fails before ordinary commands or terminal interaction; neither side continues optimistically. |
| Backend exits or stdout closes | Rust stops accepting commands, preserves any bounded partial presentation, restores the terminal, and reports truthful status. |
| Broken stdin pipe | Rust treats the backend as unavailable, stops writes, and begins bounded cleanup. |
| Rust panic, abort, or abrupt kill | Rust cleanup is best-effort only. The launcher observes the exit, restores a known terminal baseline, and terminates the shared process group or job within a fixed deadline, including the backend. |
| Interrupt, termination signal, or normal quit | Rust attempts terminal restoration and graceful backend shutdown. The launcher enforces the cleanup deadline and propagates a truthful outcome. |
| Failed update or restart | Python reports failure without leaving a mixed-version process pair. The user retains an explicit Textual path. |
| Textual failure | Existing Python cleanup and RPC ownership remain unchanged by the Rust experiment. |

The experimental frontend implements bounded handshake, graceful shutdown, task join, and
signal-escalation deadlines. It does not rely on frontend destructors or backend EOF for fail-safe
cleanup, transfer semantic authority, or permit unbounded cleanup.

## Feature-parity matrix

Rows are classified for promotion past experimental opt-in, not as a claim that Rust is unfinished
as a stage-2 experiment. **Blocker for stage 3** means supported opt-in is not authorized until the
owning issue lands. **Acceptable difference** is an intentional or documented gap that does not by
itself block remaining at stage 2. **Deferred noncritical** is polish that can wait after stage 3.

| Surface | Status | Classification |
|---|---|---|
| Prompts, approvals, trust, cancel, steering and follow-up queues | Present in Rust | Acceptable difference while experimental |
| Virtual Markdown, tool-card, and structured-diff transcript | Present in Rust | Acceptable difference while experimental |
| Bounded history paging, `/resume`, `/new` | Present in Rust | Acceptable difference while experimental |
| `/clone`, `/tree`, `/unrevert`, `/name` | Present in Rust; not in Textual | Acceptable difference |
| `/connect` API-key and device-code flows | Present in Rust | Acceptable difference while experimental |
| Live RPC v4 / event schema v36 lockstep | Enforced at handshake | Acceptable difference while experimental |
| Keyboard-only operation; no mouse | Intentional | Acceptable difference |
| No transcript search | Intentional while experimental | Acceptable difference |
| No automatic fallback to Textual | Intentional; #470 closed this way | Acceptable difference |
| Source-build only; no wheel binary | Current packaging | Blocker for stage 3 ([#469](https://github.com/whanyu1212/Wisp/issues/469)) |
| Windows | Rejected before binary resolution | Acceptable difference; not a claimed target |
| Model/effort picker interaction | Rust validates the catalog; picker UX is incomplete | Blocker for stage 3 ([#467](https://github.com/whanyu1212/Wisp/issues/467)) |
| Protected-path-aware file suggestions | Backend-owned; Rust must not walk the workspace | Blocker for stage 3 ([#467](https://github.com/whanyu1212/Wisp/issues/467), [#462](https://github.com/whanyu1212/Wisp/issues/462)) |
| Skills, command catalog, MCP status UX | Typed catalogs must come from Python | Blocker for stage 3 ([#467](https://github.com/whanyu1212/Wisp/issues/467)) |
| Configurable keybindings, themes, prompt-history search | Frontend-local | Blocker for stage 3 ([#467](https://github.com/whanyu1212/Wisp/issues/467), [#445](https://github.com/whanyu1212/Wisp/issues/445)) |
| Fuzz, backpressure, terminal sanitization, panic/PTY restore | Incomplete | Blocker for stage 3 ([#468](https://github.com/whanyu1212/Wisp/issues/468)) |
| Narrow-layout and mouse polish | Not required for stage 2 | Deferred noncritical |
| Comparative PTY input-to-frame vs Textual | No dual-renderer harness | Stop condition against a default switch; not claimed |

## Relationship to existing work

- [#270](https://github.com/whanyu1212/Wisp/issues/270) owns evidence-based Python/PyO3
  acceleration candidates. Native Python acceleration is not part of the Rust TUI boundary.
- [#399](https://github.com/whanyu1212/Wisp/issues/399) keeps the Python SDK a supported product
  surface rather than treating Python as an internal implementation detail.
- [#405](https://github.com/whanyu1212/Wisp/issues/405) and the RPC capability issues should share
  model, authentication, and settings semantics instead of creating frontend-only policy.
- [#409](https://github.com/whanyu1212/Wisp/issues/409) coordinates package boundaries and any
  evidence-based lightweight distribution decision.
- [#418](https://github.com/whanyu1212/Wisp/issues/418) continues to own Textual architecture and
  performance work while Textual is supported.
- [#442](https://github.com/whanyu1212/Wisp/issues/442) supplies the near-term Textual input-latency
  baseline used for a later comparison.
- [#443](https://github.com/whanyu1212/Wisp/issues/443) remains a Textual synchronized-frame
  experiment rather than a prerequisite for Rust.
- [#445](https://github.com/whanyu1212/Wisp/issues/445) should define stable action identifiers that
  can inform frontend-local keymaps without sharing renderer implementation.
- [#467](https://github.com/whanyu1212/Wisp/issues/467),
  [#468](https://github.com/whanyu1212/Wisp/issues/468), and
  [#469](https://github.com/whanyu1212/Wisp/issues/469) are the remaining blockers for supported
  opt-in (stage 3). They do not reopen the #470 default-renderer decision.
- [#470](https://github.com/whanyu1212/Wisp/issues/470) is the closed decision to remain at
  experimental opt-in with Textual as default.

## Consequences and reconsideration

The experiment adds a second frontend language, cross-language fixtures, and a lockstep compatibility
obligation. Shipping platform binaries would add a separate distribution cost; #469 owns that work
and is not done. Those costs are accepted only while Rust stays experimental. They do not justify
moving unrelated Python systems.

#470 closed as a stage-2 hold because several reconsideration conditions still hold:

- there is no comparative PTY input-to-frame measurement against Textual;
- UX parity for model/effort pickers, file suggestions, skills, MCP, and keybindings is incomplete
  ([#467](https://github.com/whanyu1212/Wisp/issues/467));
- hardening, backpressure, and terminal-safety evidence is incomplete
  ([#468](https://github.com/whanyu1212/Wisp/issues/468));
- supported platforms cannot install the frontend without a local Rust toolchain
  ([#469](https://github.com/whanyu1212/Wisp/issues/469)).

A default-renderer proposal remains out of scope until those conditions are re-measured. Textual
removal always requires a separate explicit issue; #470 did not file one.
