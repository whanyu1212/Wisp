# Wisp Architecture

## Layer ownership

Wisp has one agent loop and a strict adapter stack:

```text
CLI / JSONL-RPC / SDK adapters -> RPC command host -> CodingSession -> AgentHarness -> run_agent_loop
```

- `run_agent_loop` owns the provider/tool-call cycle and context estimation. It does not know about
  sessions, persistence, or frontends.
- `AgentHarness` owns the in-memory transcript, queued steering and follow-ups, and cancellation.
- `CodingSession` owns project prompts, persistence, trust, tool policy, compaction, and cost.
- The RPC command host owns command scheduling, approvals, selected-session state, and runtime
  transitions.
- CLI, SDK, RPC, and TUI adapters parse input and render typed events; they do not own agent policy.

Change behavior at the narrowest layer that can express it. Do not add a frontend-specific agent
loop or move runtime behavior into an adapter.

## Events and persistence

`wisp.events` defines the typed, versioned event contract. Provider streams and tool executors have
strict terminal-event ordering. When an event field or meaning changes, inspect both transport and
persistence paths, bump the relevant schema version, and test serialization through
`model_dump_json()` and `wisp_event_from_json()`.

Agent-loop and harness lifecycle rules that later refactors must preserve — one terminal per started
turn, Ended/Ready pairing, request-boundary decisions, queue FIFO — are listed in
`references/runtime-invariants.md`. Sequential execute cancellation is not a synthetic tool-result
contract; named assertions live in `tests/agent_runtime.py`.

Sessions persist messages and selected raw event types through independent paths. Check
`PERSISTED_SESSION_EVENT_TYPES` before deciding whether a session schema also changes.

## Frontend boundary

Interactive terminal frontends are process-isolated RPC clients. Textual is the current supported
implementation. Wisp has accepted an experiment for an optional Rust frontend, but Python remains
the sole authority for providers, MCP, tools and managed processes, trust, approvals, protected
paths, configuration, authentication, sessions, compaction, cancellation, and durable state.

Rust may own terminal input, frame scheduling, rendering, scrolling, overlays, pickers, frontend
command correlation, and disposable presentation preferences. It must use typed, bounded,
JSON-serializable commands and events; it does not read session JSONL, credential files, or project
policy directly. Python loads historical durable formats and emits current live snapshots. Protocol
types are generated from Python rather than maintained as a handwritten Rust schema.

The Python launcher remains an external supervisor for a Rust invocation. Rust owns graceful RPC
shutdown, but the launcher and an OS-level process group or job must terminate the Python backend
within a fixed deadline when Rust panics, aborts, or is killed. Backend stdin reaching EOF and Rust
destructors are not fail-safe cleanup mechanisms.

Keep untrusted output bounded and terminal-safe. A frontend may choose presentation, but it must not
reconstruct runtime or safety policy from formatted output. Textual remains a supported fallback;
making Rust the default or removing Textual requires a later evidence-backed decision.
