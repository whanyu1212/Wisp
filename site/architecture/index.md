---
title: Architecture
---

# Architecture

How Wisp is put together, and why. This section explains design decisions; the
[Reference](../reference/) documents exact surfaces.

The single most important idea: **every interface drives the same agent loop.** Shared session,
approval, cancellation, and event contracts live below the frontends, while each frontend exposes
only the live controls its transport can support. See
[Staying in sync](../guide/staying-in-sync) for those interface differences.

```mermaid
flowchart LR
  CLI[CLI] --> Host
  Textual[Textual TUI] --> Host
  Rust[Rust TUI] --> Host
  RPC[JSONL RPC] --> Host
  SDK[SDK] --> Host
  Host[RPC command host] --> Session[CodingSession]
  Session --> Harness[AgentHarness]
  Harness --> Loop[run_agent_loop]
```

Each layer adds one concern:

- `run_agent_loop` owns the provider/tool cycle and remains provider-neutral.
- `AgentHarness` owns the in-memory transcript, queues, and continuation of a run.
- `CodingSession` adds durable state, compaction, trust, and safety policy.
- The RPC command host exposes those capabilities as typed commands.
- CLI, JSONL-RPC, SDK, and TUI adapters translate their transports into the shared commands and
  render typed events back to users.

This boundary keeps persistence and frontend concerns out of the provider loop, while allowing
provider adapters to preserve their own request, replay, continuation, and usage semantics.

See [Agent runtime](./agent-runtime) for the loop and harness lifecycles, ownership boundaries,
request-boundary handshake, and source navigation.

## Terminal frontend boundary

In 0.2.0rc2, `wisp`, `wisp tui`, and `wisp --mode tui` use `auto`: prefer the Rust frontend
on macOS/Linux when the active installation declares its native binary, otherwise use Textual.
Native wheels cover macOS arm64 and Linux glibc 2.28+ x86_64. Pure/source installs, Intel macOS, and
other platforms keep Textual. Explicit CLI selection takes precedence over `WISP_TUI_RENDERER`,
which takes precedence over `auto`. `WISP_RUST_TUI_BINARY` also selects Rust in auto mode on
macOS/Linux for source development. Missing or damaged declared binaries and Rust launch/runtime
failures report an error; they never silently switch frontends.

Use `wisp tui --renderer textual` or `WISP_TUI_RENDERER=textual` for the maintained Python
fallback. Textual keeps compatibility and critical fixes; new frontend work prioritizes Rust.
Both clients use the same Python runtime, permissions, providers, and saved sessions.

See the [Rust terminal frontend boundary](./rust-tui-boundary) for the RC2 decision and ownership.

## Resumed transcript hydration

The Textual TUI completely hydrates the selected session's active path after an explicit interactive
`/resume`. Rust startup and session selection also load the entire saved active path, projecting it once
in chronological order; transport and rendering caches remain bounded. This is an intentional UX
tradeoff: a long session takes longer to select, but upward scrolling no longer crosses asynchronous
page-mount boundaries that can change scroll geometry underneath the reader.

Complete does not mean one top-level widget per JSONL record. The RPC layer returns every message and
every nested tool-call identity, with bounded text and argument previews. The TUI verifies that every
active-path message row survives conversion, then groups request/result pairs into tool cards and all
observations of one managed process into a single lifecycle card. System and empty assistant rows get
explicit transcript representations instead of disappearing. Mounting occurs in responsive batches
behind a progress overlay and the replacement becomes visible only after layout settles.

Process cards retain the IDs and bounded one-line previews of every represented update. Expansion
renders only a fixed-size timeline window; selecting an update performs an exact, active-path
`get_messages` lookup for that row. This avoids eagerly duplicating potentially large stdout bodies in
both the session snapshot and widget tree. The costs are an O(rows) metadata read, conversion, and
retention during `/resume`, plus detail-fetch latency on the first inspection of an output row. Exact
lookup bypasses frontend preview limits but preserves the persisted tool-level `truncated` marker,
because bytes discarded before JSONL persistence cannot be reconstructed.

Session identity and row identity are validated again when an exact-detail response arrives. Pending
lookups are invalidated on `/new` or another `/resume`, so a late response cannot populate a card from
the wrong session. Pagination cursor repetition, duplicate rows, omitted row representations, and
mount failures abort the committed hydration rather than exposing a partial transcript.
