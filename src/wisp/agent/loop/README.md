# Agent loop

This package implements Wisp's provider-neutral model/tool cycle. Its public entry point is
[`run_agent_loop`](./runner.py), an async event stream for one invocation. Start there to understand
the normal flow.

The loop owns transient execution state only. It does **not** own the conversation between
invocations, persistence, compaction policy, queues, or frontend behavior. Those concerns belong to
`AgentHarness`, `CodingSession`, and the shared RPC interfaces.

For the system-level design and lifecycle diagrams, see the
[agent runtime architecture](../../../../site/architecture/agent-runtime.md).

## Execution flow

One turn is one provider response and its requested tool batch, if any:

1. Start a turn and estimate the request's context budget.
2. Stream and validate one provider response.
3. Project the response into typed Wisp events.
4. Execute any requested tool batch.
5. Update the invocation's continuation state.
6. Ask the request-boundary hook whether to stop, continue, replace history, or rebase context.

`TurnCompleted` ends a turn, not necessarily the invocation. The next turn may continue through a
provider-native cursor or through portable message history, depending on adapter capabilities and
the boundary decision.

## Module map

| Module | Read it when changing |
| --- | --- |
| [`runner.py`](./runner.py) | Top-level turn sequencing, context events, limits, and failure or cancellation transitions |
| [`model_response.py`](./model_response.py) | Provider event translation, response outcomes, deltas, and retries |
| [`provider_request.py`](./provider_request.py) | Optional provider capabilities, request invocation, overflow normalization, and owned stream cleanup |
| [`provider_lifecycle.py`](./provider_lifecycle.py) | Provider response start, retry, tool-call, and terminal validation |
| [`response_projection.py`](./response_projection.py) | Usage, cost, context observation, and completed-message projection |
| [`tool_execution.py`](./tool_execution.py) | ToolBatch facade, sequential and truncated execution, and cancellation settlement |
| [`tool_lifecycle.py`](./tool_lifecycle.py) | Shared tool event contracts, executor lifecycle validation, and the per-batch settlement record |
| [`prepared_tools.py`](./prepared_tools.py) | Two-phase preparation and bounded scheduling |
| [`continuation.py`](./continuation.py) | Provider cursors, pending tool results, injected user messages, context replacement, or rebasing |
| [`stream_cleanup.py`](./stream_cleanup.py) | Owned iterator close, cleanup exception precedence, and shielded aclose |
| [`config.py`](./config.py) | Public loop dependencies, hooks, limits, offsets, and cancellation contracts |
| [`__init__.py`](./__init__.py) | Supported public imports |

Shared contracts live outside this package:

- `wisp.agent.request_boundary` defines boundary snapshots, decisions, and hooks.
- `wisp.agent.tool_contracts` defines executor protocols.
- `wisp.agent.context_budget` estimates request size.
- `wisp.events` defines the observable event models.
- provider-native continuation and request behavior stays in `wisp.providers`.

## State boundary

`run_agent_loop` receives a base `messages` sequence but never mutates it. `ContinuationState` holds
only data produced or accepted during the current invocation:

- a native response cursor;
- pending tool results and extra user messages for the next request;
- the live assistant/tool continuation tail.

A fresh `messages` boundary decision clears native continuation. A `context_rebase` preserves the
accepted continuation tail while replacing its portable base. Unsupported structured-history
transitions fail explicitly rather than being flattened.

## Observable invariants

Preserve these rules when changing control flow:

- Every emitted `TurnStarted` has exactly one matching terminal `TurnCompleted`. The runner opens
  and closes turns through `TurnLifecycle`, which rejects a second start or completion.
- An invocation that never starts a turn emits no turn terminal, even with a nonzero offset.
- A settled tool call emits one `ToolExecutionEnded` immediately followed by one matching
  `ToolResultReady`.
- Optional approval is ordered request, resolution, then result.
- A tool batch absorbs a scope cancellation only when the run's cancellation token requested it;
  a caller's own timeout or task cancellation propagates instead of becoming a cancelled turn.
- When the run's token cancels a tool batch, every requested call still ends with exactly one
  terminal/result pair, whatever executor type ran it.
- Provider response lifecycle events remain ordered and terminal.
- A request-boundary `stop` wins over other decision fields.
- Provider capabilities are detected; new optional keywords are not sent unconditionally.

The detailed compatibility list is in
[`wisp-development/references/runtime-invariants.md`](../../skills/bundled/wisp-development/references/runtime-invariants.md).

## Tests

Use the focused suite for the phase being changed:

```bash
uv run pytest \
  tests/agent/loop/test_runner.py \
  tests/agent/loop/test_model_response.py \
  tests/agent/loop/test_tool_execution.py \
  tests/agent/loop/test_continuation.py \
  tests/agent/test_runtime_invariants.py
```

Changes at provider, session, event, or persistence boundaries require the corresponding integration
tests as well.
