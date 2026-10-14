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

`wisp.events` defines the typed event contract. Events carry no per-event version, and backend and
frontend must be the same release. Provider streams and tool executors have strict terminal-event
ordering. After any event, command, or handshake model change, regenerate `schemas/live-rpc/` and
run `uv run python -m wisp.rpc.protocol_schema --check`. After an event-model change, also regenerate
`tests/fixtures/event_decoding_conformance.json` and check Python/Rust decoding conformance. Inspect
both transport and persistence paths and test serialization through `model_dump_json()` and
`wisp_event_from_json()`.

Agent-loop and harness lifecycle rules that later refactors must preserve — one terminal per started
turn, Ended/Ready pairing, request-boundary decisions, queue FIFO — are listed in
`references/runtime-invariants.md`. When the run's cancellation token cancels a tool batch, both
sequential and prepared execution settle every requested call with a terminal/result pair, using an
interrupted result for unfinished calls. Caller-scope cancellation and executor errors can leave
calls unsettled; the harness repairs them before the next provider request. Named assertions live
in `tests/support/agent_runtime.py`.

Sessions persist messages and selected raw event types through independent paths. Check
`PERSISTED_SESSION_EVENT_TYPES` before deciding whether a session schema also changes. Bump a session
entry or persisted-event-envelope schema version only when older files would be misread or rejected;
a new optional field does not require a bump. Readers accept only the current schema versions and do
not upgrade older files.

## Frontend boundary

The Rust interactive terminal frontend is a process-isolated RPC client. Python remains
the sole authority for providers, MCP, tools and managed processes, trust, approvals, protected
paths, configuration, authentication, sessions, compaction, cancellation, and durable state.

Rust may own terminal input, frame scheduling, rendering, scrolling, overlays, pickers, frontend
command correlation, and disposable presentation preferences. It must use typed, bounded,
JSON-serializable commands and events; it does not read session JSONL, credential files, or project
policy directly. Python reads current-format session history and emits current live snapshots. Protocol
types are generated from Python rather than maintained as a handwritten Rust schema.

The Python launcher remains an external supervisor for a Rust invocation. Rust owns graceful RPC
shutdown, but the launcher and an OS-level process group or job must terminate the Python backend
within a fixed deadline when Rust panics, aborts, or is killed. Backend stdin reaching EOF and Rust
destructors are not fail-safe cleanup mechanisms.

`rust/wisp-tui/src/theme_catalog.json` is authoritative for curated theme order, metadata, and
semantic colors. Theme choices and terminal opacity or blur are presentation concerns and must not
enter RPC or session persistence.

Keep untrusted output bounded and terminal-safe. A frontend may choose presentation, but it must not
reconstruct runtime or safety policy from formatted output. A missing or incompatible Rust binary
fails interactive startup with actionable installation or source-build guidance; Python print, JSON,
RPC, and SDK interfaces remain available on pure-wheel installs.
