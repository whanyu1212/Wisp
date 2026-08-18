---
title: Staying in sync
---

# Staying in sync

Wisp keeps control and evidence in the same loop as the work. A live client can redirect an active
run at a safe boundary, every frontend uses the same approval and cancellation policy, and the
append-only session shows what actually happened.

The shared runtime guarantees consistent semantics, but frontend controls differ:

| Interface | Live controls |
|---|---|
| Textual/line TUI | Queue follow-up prompts, cancel the active command, and answer approvals |
| JSONL RPC | Steer, queue follow-ups, inspect/edit queues, cancel by command id, and answer approvals |
| Python SDK | The same live queue, cancellation, and approval capabilities exposed as typed methods |
| Print / JSON (`wisp -p`) | One prompt per process; no channel for new input while it runs |

JSON mode changes print mode's output format, not its interactivity. Use RPC or the SDK when an
automation needs to redirect work already in progress.

## Steering versus follow-up

Both operations append ordinary user messages at controlled request boundaries; neither edits or
reorders the existing transcript.

- **Steering** targets the active run. Wisp injects the message after the current assistant/tool
  batch and before the next provider request, so completed tool work remains visible and the model
  sees the correction before continuing.
- **Follow-up** waits until the run would otherwise stop, then continues with the queued message.
  In the TUI, text submitted while a prompt is running is queued this way and the footer/transcript
  reports how many follow-ups remain.

Each queue is FIFO. The default `one_at_a_time` mode injects one message at each eligible boundary;
live RPC and SDK clients can switch a queue to `all` to inject the current batch together. They can
also inspect queue counts, remove the newest item, or clear one or both queues before injection.

The in-process `InProcessWisp` controller exposes these as `steer()`, `follow_up()`,
`get_queue_state()`, `set_queue_mode()`, `pop_queue()`, and `clear_queue()`. Raw JSONL clients use
commands with the same names. See the [RPC reference](../reference/rpc) for the wire contract.

## Cancelling cleanly

Cancellation requests a cooperative stop through the command host instead of killing the process.
The active provider/tool path unwinds, lifecycle events record the cancelled outcome, and durable
JSONL entries already committed remain valid. You can resume the session instead of reconstructing
state from a half-written transcript.

- In the TUI, dismiss any open overlay first, then press `Escape` to cancel the active prompt.
- In RPC, send `{"type":"cancel","target_id":"<running-command-id>"}`.
- In the SDK, call `InProcessWisp.cancel(target_id)` for the active command.

Cancellation does not pretend that external side effects never happened. A command that already
changed the filesystem or a remote service stays represented in the event stream; inspect its tool
result before continuing.

## Approvals as a sync point

Read tools can run directly. Mutating and command tools pause before execution and emit a typed
approval request describing the proposed call. The user or controlling client may deny it, approve
that call once, allow the same tool for the session, or allow all unsafe tools for the process.

The approval decision is supplied outside the model conversation. Prompt content cannot forge it
or lower a tool's safety category. Print/JSON mode has no interactive approval channel, so unsafe
execution is blocked unless the process started with `--yes`.

See [Tools & safety](./tools-and-safety) for tool categories, protected paths, trust, and MCP policy.

## Seeing what happened

Wisp represents model output, tool calls and results, queue changes, approvals, cancellation,
compaction, usage, and command completion as typed `WispEvent` values. Interfaces render those
events differently, but they do not invent a second lifecycle.

Durable sessions append JSONL entries in order. That record supports resume, branching, audit, and
recovery from an interrupted process without silently rewriting earlier history. Read
[Sessions](./sessions) for persistence behavior and [Event model](../architecture/events) for the
ordering contract.

::: tip Where this is enforced
Steering, follow-up queues, and cooperative cancellation live in `AgentHarness`, one layer below
persistence and one above the provider-neutral loop. `CodingSession` adds durability and policy;
frontends expose the subset of controls their transport can accept.
:::
