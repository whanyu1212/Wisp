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

1. Validates invocation offsets before repairing or appending transcript rows.
2. Repairs interrupted assistant/tool exchanges and prepares the boundary coordinator.
3. Enters the guarded run lifetime and appends the new user message, if supplied.
4. Creates an `AgentLoopConfig` and normalizes a detached transcript for the provider.
5. Streams `run_agent_loop` events to its caller.
6. Appends completed assistant messages and terminal tool outputs to the in-memory transcript.
7. After each completed turn, drains an eligible queue and prepares the next request boundary.
8. Releases cancellation and running state when the stream ends, fails, or is closed, even if
   startup or inner-stream cleanup raises.

`config._build_loop_config` maps harness settings and invocation-specific hooks and offsets into
the loop configuration. `_run` calls it after entering the guarded lifetime and accepting the prompt.

`AgentHarness._next_loop_step` advances the loop within the cancellation scope and returns either
an event or a control outcome. It interrupts the first cancelled advance and shields later advances
to let tool settlement finish. `_HarnessRunState` retains that drain state across turns. `_run` owns
every event yield, transcript update, queue injection, and boundary arm; no cancellation scope spans
its public event yields.

The returned async generator is lazy: creating it does not append the prompt or mark the harness as
running. `prompt_message()` snapshots its input at creation, so later caller mutations cannot change
the pending prompt. The caller must consume or close the stream so the `finally` cleanup runs.

Invalid offsets leave the transcript and queues unchanged. A valid prompt accepted before a runtime
failure stays in the transcript; cleanup does not roll back accepted user input or completed tool
output. Failures still propagate, but the harness returns to idle and can be reused.

## Message ownership

The harness copies incoming messages and returns detached transcript and queue snapshots. Message
models are frozen, but nested JSON values such as tool arguments are not recursively immutable:
callers may change their own copy without changing retained state.

Completed events are converted to detached messages before exposure. The same conversion protects
session persistence from later event-consumer mutations. Request-boundary callbacks receive detached
context; their returned decisions are copied before handoff and copied into the transcript only when
the next turn accepts the transition. Copies preserve runtime-only provider metadata.

Internal queue-drain batches retain entry identity so edits during draining remain observable.
Removing and re-enqueuing a message creates a new entry for a later boundary, not a member of the old
batch. Count and byte inspection do not construct public deep snapshots, and the boundary coordinator
reuses the already-detached public transcript snapshot rather than copying it again.

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
- Startup and cleanup failures release run state without silently swallowing the failure.
- Caller-owned messages, returned snapshots, and completion events cannot mutate retained messages.
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
  tests/test_agent_harness_interruptions.py \
  tests/test_agent_runtime_invariants.py
```

`test_agent_harness_interruptions.py` records a successful baseline for six small workflows:
streaming, sequential tools, parallel prepared tools, queues, transcript replacement, and context
rebase. It then runs each workflow afresh, cancelling or closing after every emitted event. Failure
notes identify the workflow, action, event type, and occurrence.

Cancellation is drained and checked for settlement; explicit closure cannot emit terminal events,
so it is checked through retained state and a subsequent run of the same harness. Both paths must
preserve exposed outputs and unconsumed queues, repair missing tool results without duplicates, and
apply only accepted transcript transitions. Separate cases exercise provider, tool, and boundary
failures plus a rejected stale rebase. The parallel fixture requires overlap and reversed completion
using event barriers, not sleeps.

This is a bounded event-boundary matrix, not exhaustive coverage of task scheduling or real provider
implementations. Keep targeted in-flight cancellation tests in `test_agent_harness.py` alongside it.

Changes to boundary preparation or transcript persistence should also run
`tests/test_coding_session.py` and `tests/test_compaction.py`.
