# Agent runtime invariants

Later extracts of `run_agent_loop` and `AgentHarness` must preserve the
compatibility contracts below. They may change incidental internals. Named
assertions live in `tests/support/agent_runtime.py`; focused coverage is
`tests/agent/test_runtime_invariants.py`.

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
  `Ready`, and Ready is a payload projection of Ended (shared result fields
  match). The same `call_id` may be reused across sequential rounds after
  the previous pair has closed (Google fallback IDs are `call-{name}-{index}`
  per response). Denied approval ends in an error result. Optional approval
  is request then resolution then result.
- Provider stream order already enforced by the loop: optional retries, then
  either one start → deltas/tool calls → one completed/failed, or a startless
  `ProviderResponseFailed` when the request never opened. OpenAI's
  request-creation path can yield that failure with no `ProviderResponseStarted`;
  `_ProviderResponseLifecycle.complete()` accepts it. Deltas, tool calls, and
  successful completion still require a prior start.
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

- Private `_AgentLoopState` field layout and helper names in `loop/runner.py`.
- Nested request-boundary / overflow hook coordination and nonlocal variables
  in `harness/runner.py` and `harness/boundaries.py`.
- Exact control-flow nesting of the `while` loop.
- Duplicated scenario fixtures across loop and harness tests.
- Log lines, comment wording, and test function names.

## When tool calls are settled

When the run's cancellation token cancels a tool batch, sequential and prepared
execution settle unfinished calls by distinct `call_id` within the batch:
a real result when available, or an interrupted result. `ToolBatchSettlement`
tracks requested and published IDs, not occurrences. Duplicate IDs within one
response are deduplicated during settlement: a published result for an ID
suppresses settlement of later occurrences, even if a later occurrence was
requested. This is not a per-occurrence guarantee for duplicate IDs. Reusing an
ID across separate batches does not share settlement state.

Focused coverage includes `test_sequential_cancellation_settles_every_requested_call`,
`test_cancel_settles_blocked_sequential_batch`, and
`test_cancel_skips_already_settled_duplicate`.

Apart from the duplicate-ID qualification above, calls can still be left
unsettled in two cases: a cancellation the run did not request (the caller's
own scope, timeout, or task cancellation) propagates
without settlement, and an executor error propagates after earlier calls were
requested. The harness repairs those interrupted calls before the next provider
request.

`tests/conftest.py` applies `assert_turn_invariants` (which includes
`assert_turn_terminals`) and `assert_tool_result_pairing` to every loop stream
a test drives. Opt out only with `loop_invariants_exempt(reason=...)`. Helpers
split the rules on purpose:

- `assert_turn_terminals` — every started turn has exactly one terminal.
- `assert_tool_result_pairing` — Ended/Ready pairing and payload projection
  when a tool terminal is present; requested-but-unsettled calls are allowed.
- `assert_settled_tool_calls` — only on paths that promise settlement
  (token-cancelled batches, prepared batches, truncated batches). Listed
  `call_ids` are counted by occurrence, so a reused fallback ID from two rounds
  needs two terminals.
