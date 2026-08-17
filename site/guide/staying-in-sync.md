---
title: Staying in sync
---

# Staying in sync

TODO — the central concept page. Everything else in the guide is downstream of this.

The claim: **you can redirect Wisp while it is working**, and you can always tell
what it did. Most agents give you a turn boundary — you speak, it runs, you react.
Wisp keeps the loop open mid-run.

Cover, in this order:

## Steering versus follow-up

The distinction worth leading with, because it is the least obvious and the most
useful:

- **Steering** queues a message for the *current* run. It redirects work already in
  flight without rewriting the transcript.
- **Follow-up** queues a message for *after* the current run completes.

Both are owned by `AgentHarness` (`steer()` / `follow_up()`), and both are exposed
through every interface. Document the queue modes (`one_at_a_time` is the default)
and what happens when you queue several.

## Cancelling cleanly

Cancellation is cooperative — it requests a stop rather than killing the process, so
the session stays resumable rather than truncated mid-write. Show what the agent
emits on cancel and what state survives.

## Approvals as a sync point

An approval prompt is the agent telling you what it is about to do before it does
it. Cover the flow here at a conceptual level; the tool categories themselves live
in [Tools & safety](./tools-and-safety).

## Seeing what happened

Every action is a typed event with an enforced order, persisted to JSONL. Point at
[Sessions](./sessions) for the record and
[Event model](../architecture/events) for why the contract is shaped this way.

::: tip Where this is enforced
Steering, follow-up queues, and cancellation live in `AgentHarness` — one layer
below persistence and one above the pure loop. No frontend reimplements them, so
every interface gets the same behaviour.
:::
