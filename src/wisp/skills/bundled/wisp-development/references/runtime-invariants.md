# Agent runtime invariants

Later extracts of `run_agent_loop` and `AgentHarness` must preserve the
compatibility contracts below. They may change incidental internals. Named
assertions live in `tests/agent_runtime.py`; focused coverage is
`tests/test_agent_runtime_invariants.py`.

## Compatibility contracts

Later slices must preserve:

- Public `AgentLoopConfig` / `AgentHarnessConfig` fields and legacy positional
  order.
- Public `AgentLoopEvent` union and observable event order.
- One started turn → exactly one terminal `TurnCompleted` (`completed` |
  `failed` | `cancelled`). A request-boundary hook or overflow failure after
  that terminal must not emit a second one.
- Unstarted turns (including nonzero `turn_offset` before the first
  `TurnStarted`) emit no matching `TurnCompleted`.
- Each tool execution occurrence has one `ToolExecutionEnded` and one
  `ToolResultReady` when a terminal is present; `Ended` immediately precedes
  `Ready`. The same `call_id` may be reused across sequential rounds after
  the previous pair has closed (Google fallback IDs are `call-{name}-{index}`
  per response). Denied approval ends in an error result. Optional approval
  is request then resolution then result.
- Provider stream order already enforced by the loop: retries → one start →
  deltas/tool calls → one completed/failed.
- Request-boundary decisions: `stop` wins; `messages` is a fresh replacement
  that discards native continuation; `context_rebase` keeps cursor/tool tail
  and rejects stale snapshots; `extra_messages` are plain user messages;
  cursor-less structured tool history is rejected rather than flattened.
- Harness queues: steering is distinct from and drains before follow-up; FIFO
  within a queue; snapshot drain; edits during drain; shared capacity;
  cancellation/close must not inject unexposed messages.
- One live harness run; overlapping `prompt()` is rejected.
- Interrupted-tool transcript repair before the next provider request.

## Incidental internals

Do not freeze these as compatibility contracts:

- Private `_AgentLoopState` field layout and helper names in `loop.py`.
- Nested request-boundary / overflow hook coordination and nonlocal variables
  in `harness.py`.
- Exact control-flow nesting of the `while` loop.
- Duplicated scenario fixtures across loop and harness tests.
- Log lines, comment wording, and test function names.

## Sequential cancellation is not a synthetic-result contract

A sequential `ToolExecutor.execute` path may emit `ToolCallRequested` or
`ToolExecutionStarted` and then cancel the turn without a tool terminal. That
is current observed behavior, not a promise to synthesize interrupted tool
results.

Prepared batches do synthesize one terminal result per requested call. Helpers
split the rules on purpose:

- `assert_turn_terminals` — every started turn has exactly one terminal.
- `assert_tool_result_pairing` — Ended/Ready pairing when a tool terminal is
  present; requested-but-unsettled calls are allowed.
- `assert_settled_tool_calls` — only on paths that promise settlement
  (prepared batches, truncated batches). Listed `call_ids` are counted by
  occurrence, so a reused fallback ID from two rounds needs two terminals.
