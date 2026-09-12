# Rust TUI transport pressure (#468)

This focused slice builds on `e0c7548` (#549). It replaces immediate queue-full failure with
bounded waiting, makes the live loop fair under input/output traffic, and checks transport failures
without requiring another outgoing command. It does not close #468 or authorize a default switch.
Textual remains the supported default; Rust remains experimental on macOS/Linux.

## Transport contract

- The inbound queue retains at most 64 events. Its semaphore accounts for at most 64 MiB of encoded
  event content, excluding line delimiters. A dequeued event retains its byte permit through dispatch.
- The reader may hold one additional bounded, undecoded frame while waiting. It reserves a queue
  slot and byte permits before decoding/projecting, using one absolute five-second deadline across
  both waits. A recovering consumer resumes FIFO delivery; deadline expiry is terminal overload.
- Frame reads request at most 8 KiB and the remaining frame-plus-CRLF allowance. Incremental delimiter
  scanning avoids rescanning a large accumulated frame on every read, including after cancellation.
- There are no priority lanes, event coalescing, or selective control-event drops. Controls retain
  their place in the same FIFO as text and tool output.
- Outbound queue admission has a five-second deadline. The writer has a separate five-second
  deadline for each complete frame, newline and flush. Confirmed queue submissions retain their
  existing combined five-second admission/acknowledgement deadline. A failed or possibly partial
  frame is terminal and is never retried by the frontend.
- Valid frames with both admission permits publish through their reserved slot even if fatal cleanup
  closes the receiver during decoding, so abandonment diagnostics include their event/wire-byte counts.
- User exit observes/releases any held event, then drains inbound events while admitting shutdown and
  awaiting the writer outcome. Per-operation transport deadlines remain in force; the short task-join
  timeout applies only after the writer has reported completion. Ready reader failures take precedence.
  Successful shutdown also requires the reader's clean EOF, so a zero backend exit cannot hide a
  trailing protocol error.
- EOF drains the finite admitted prefix before projecting transport closure. Fatal reader/writer
  errors abandon the admitted prefix explicitly, project closure without dispatching that prefix,
  report bounded sanitized diagnostics and abandoned event/wire-byte counts, then use existing
  terminal/process cleanup and a nonzero exit.

These are encoded-wire and queue bounds, not a 64-MiB process-memory claim. Decoded objects,
framing allocation, the pending frame, rendering caches and retained transcript have separate costs.
Live transcript lifetime retention is separate follow-up work; a finite pressure test does not prove
that an arbitrarily long session has bounded RSS.

## Scheduling and activation

Each turn checks transport outcomes and external SIGINT, dispatches at most eight FIFO events,
handles at most one input, and offers at most one due redraw. The input channel holds at most
16 items, with at most one additional pending input. On dequeue, that input captures the finite
admitted event-prefix length, including the reader's reserved slot while decoding; subsequently
admitted events cannot extend its barrier.

The pending input retains its original workflow revision, decision/entry identity and painted
selection, or mouse geometry. A redraw during the barrier cannot authorize an unseen or replacement
choice. No credential buffer is copied into that snapshot. Ordinary draft editing survives unrelated
tool output; Escape, Ctrl+C and negative decisions retain their recovery handlers.

This guarantees opportunities between operations, not a wall-clock frame or cancellation SLA.
Dispatch can await bounded transport operations, JSON decoding/projection is synchronous, and a
terminal draw cannot be preempted. Continuous traffic therefore cannot skip scheduler turns, but a
blocked terminal itself is outside that fairness guarantee.

## Evidence inventory

| Scenario | Evidence | What it establishes |
|---|---|---|
| Count, byte and combined pressure | `transport::tests::{count_pressure_recovers_before_the_admission_deadline,byte_pressure_recovers_before_the_admission_deadline,combined_count_and_byte_pressure_recovers_fifo,count_and_bytes_share_one_absolute_deadline}` | Paused-clock recovery and a shared deadline; permit restoration |
| Closure, invalid frames and writer stalls | Remaining `transport::tests`, existing reader/writer tests in `lib.rs` | Closed receiver cancellation, invalid/oversized input accounting, admission/write/shutdown deadlines, partial-frame terminal failure |
| Bounded generated framing/protocol cases | `framing::tests`, `transport::tests::generated_*` | CRLF/EOF splits, read allowance, cancellation cursor, six size/chunk pairs and three invalid protocol cases |
| Fair turns and finite input barriers | `event_loop::tests::{saturated_fifo_gets_a_paint_after_eight_events_and_does_not_starve_input,later_events_do_not_extend_the_captured_input_prefix}` | First paint after eight events, FIFO state, input progress while a finite 2,048-event producer is still active |
| Deferred activation | `event_loop::tests` approval/trust/model, first-paint and browse tests; existing file/mouse/overlay tests | Original decision, catalog, visibility and entry ownership survive an intervening redraw |
| EOF/fatal outcome handling | `event_loop::tests` EOF and writer-outcome tests | FIFO drain on EOF; fatal failure preempts input/events even without another command; held event remains accounted |
| Process/terminal pressure | `tests/test_rust_tui_pressure.py` | 192 × 1-KiB burst, 1,024 × 1-KiB burst with external SIGINT, synchronized EOF/malformed frame, malformed output after shutdown success, visible final state, terminal restoration and backend PID cleanup |
| Shared control ordering | `tests/fixtures/tui_traces/approval_resolution_then_cancel.json`, existing cancel-before-approval/trust and decision traces | Python and Rust agree on exact commands and terminal state; these reducer traces do not test live channel scheduling |
| Existing retained history/process bounds | `transcript.rs`, `history.rs`, `tool_cards.rs` tests; Python `test_tui_process_lifecycle.py` and `test_process_manager.py` | Existing bounded history windows, process-card tails/indexes and presentation budgets remain intact; no whole-session memory claim |

The generated cases are deterministic, finite smoke tests in ordinary Cargo CI. They are not a
standalone coverage-guided fuzz campaign. Broader fuzz targets/policy, Unicode/terminal injection,
secret cleanup, remaining lifecycle races and platform recovery remain under #468.

## Local verification

The full Rust workspace passed 608 tests with all features. Cargo formatting and Clippy passed.
The handoff suite passed all 35 cases, including five pressure cases, against the final rebuilt binary.
Shared traces/RPC contract checks passed (including the
new four-way parametrized trace), and process-retention/shared-trace checks passed 229 cases with
one existing skip. Ruff formatting/lint, configured mypy, immutable-schema verification against
`e0c7548`, and the documentation build passed. The sandbox blocked `ps` in the existing Rust
process-group test; the complete workspace gate passed when run with process inspection enabled.

## Reproduction

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
cargo build -p wisp-tui
RUST_TUI_BINARY_UNDER_TEST="$PWD/target/debug/wisp-tui" uv run pytest \
  tests/test_rust_tui_smoke.py tests/test_rust_tui_themes.py \
  tests/test_rust_tui_mouse.py tests/test_rust_tui_readiness.py tests/test_rust_tui_pressure.py
uv run pytest tests/test_tui_traces.py tests/test_rpc_protocol.py tests/test_rpc_protocol_schema.py \
  tests/test_tui_process_lifecycle.py tests/test_process_manager.py
uv run python -m wisp.rpc.protocol_schema --check --immutable-base e0c7548
```

The handoff suite is configured on both Linux and macOS. Local PTY results alone do not establish
both platforms passed; use the PR's current-head CI results. No protocol/schema/persistence changes,
distribution work, dependency-policy rollout or Textual-deprecation decision is included.
