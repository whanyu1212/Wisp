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

The Textual TUI is a pure RPC client running against a subprocess. Arbitrary Python objects do not
cross that boundary: contributions needed by the TUI must be represented by typed, bounded,
JSON-serializable events. Keep untrusted output in literal text surfaces rather than Markdown.
