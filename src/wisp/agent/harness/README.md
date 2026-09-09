# Agent harness

This package adds conversation state and live user controls around the provider-neutral agent loop.
Its main entry point is [`AgentHarness`](./runner.py). Start with `AgentHarness._run` to follow the
complete orchestration path.

The harness owns the in-memory transcript across `prompt()`, `prompt_message()`, and `continue_()`
calls, plus steering, follow-up, and cancellation state. It does **not** own durable persistence,
compaction or safety policy, provider-native behavior, or frontend rendering.

For the system-level design and lifecycle diagrams, see the
[agent runtime architecture](../../../../site/architecture/agent-runtime.md). The lower-level
execution cycle is documented in the sibling [loop README](../loop/README.md).

## Run lifecycle

For each invocation, the harness:

1. Repairs interrupted assistant/tool exchanges in the existing transcript.
2. Appends the new user message, if the caller supplied one.
3. Normalizes the transcript for the configured provider.
4. Creates an `AgentLoopConfig` and a boundary coordinator.
5. Streams `run_agent_loop` events to its caller.
6. Appends completed assistant messages and terminal tool outputs to the in-memory transcript.
7. After each completed turn, drains an eligible queue and prepares the next request boundary.
8. Releases cancellation and running state when the stream ends or is closed.

The returned async generator is lazy: creating it does not append the prompt or mark the harness as
running. The caller must consume or close it so the `finally` cleanup runs.

## Module map

| Module | Read it when changing |
| --- | --- |
| [`runner.py`](./runner.py) | Prompt and continuation flow, transcript updates, queue behavior, cancellation, or loop-event projection |
| [`boundaries.py`](./boundaries.py) | Session-owned compaction decisions, context-overflow recovery, or synchronization between loop context and the harness transcript |
| [`config.py`](./config.py) | Provider/tool dependencies, runtime limits, queue modes, or queue capacity |
| [`__init__.py`](./__init__.py) | Supported public imports |

Related ownership boundaries:

- `wisp.agent.loop` owns provider and tool execution within one invocation.
- `wisp.agent.transcript_repair` plans synthetic results for interrupted tool exchanges.
- `wisp.providers.base.prepare_provider_history` converts the transcript for a provider.
- `wisp.coding.CodingSession` owns persistence, compaction orchestration, trust, and safety policy.
- RPC and frontend layers consume typed events rather than reproducing harness policy.

## Queue behavior

The two queues have distinct continuation semantics:

| Queue | Eligible boundary | Priority |
| --- | --- | --- |
| Steering | After any successfully completed turn | First |
| Follow-up | After a completed turn with no tool calls, when the run would otherwise stop | After steering |

Both queues are FIFO and share message-count and serialized-byte limits. Their mode selects either
the first queued message or the current queue snapshot. Messages added after a drain snapshot wait
for a later boundary; edits and cancellation during draining must not inject an unexposed message.

Only one invocation may be live. Use `steer()` or `follow_up()` while it runs instead of starting an
overlapping prompt.

## Request-boundary handshake

`_HarnessBoundaryCoordinator` connects three owners without merging their responsibilities:

1. The harness emits queue effects and arms the coordinator after `TurnCompleted`.
2. The loop calls the coordinator before constructing another provider request.
3. An optional session-owned `HarnessBoundaryPreparer` may return a complete stop, replacement, or
   rebase decision.
4. The loop applies that decision to its request state.
5. When the next `TurnStarted` arrives, the harness applies the matching transcript transition.

The delayed transition keeps the provider-visible request and harness-visible transcript aligned.
The coordinator prepares and remembers decisions; `runner.py` remains the only module that mutates
the harness transcript.

## Observable invariants

Preserve these rules when changing orchestration:

- There is at most one live harness invocation.
- Completed assistant messages and terminal tool outputs are retained before their events are
  exposed to a caller that may close the stream.
- Steering drains before follow-up, with FIFO order within each queue.
- Queue draining uses a snapshot and cancellation does not inject unseen messages.
- A cancellation path does not emit a second terminal for an already completed turn.
- Interrupted tool exchanges are repaired before the next provider request.
- Transcript replacements are applied only for accepted boundary transitions.

The detailed compatibility list is in
[`wisp-development/references/runtime-invariants.md`](../../skills/bundled/wisp-development/references/runtime-invariants.md).

## Tests

The focused harness checks are:

```bash
uv run pytest \
  tests/test_agent_harness.py \
  tests/test_agent_runtime_invariants.py
```

Changes to boundary preparation or transcript persistence should also run
`tests/test_coding_session.py` and `tests/test_compaction.py`.
