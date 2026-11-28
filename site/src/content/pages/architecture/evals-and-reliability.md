---
title: Evals and reliability
description: Concrete evaluation, interruption and fault-injection invariants, and protocol fuzzing receipts for Wisp's runtime.
route: architecture/evals-and-reliability.html
section: inside
order: 3
layout: docs
listed: true
searchable: true
navTitle: Evals & reliability
---

# Evals and reliability

Claims about a coding agent only hold up when backed by runnable tests and pinned reports. Wisp
separates three kinds of evidence—live coding-task evaluation, deterministic runtime
fault-injection invariants, and coverage-guided protocol fuzzing—and links each directly to the
files that produced it.

## SWE-bench Pro V2 pilot

To measure how core prompt changes affect real repository fixes without relying on synthetic
toy tasks, the [November 23, 2026 SWE-bench Pro V2 pilot](https://github.com/whanyu1212/Wisp/blob/main/evals/tool_use/reports/2026-11-23-swe-bench-pro-v2.md)
ran Wisp from commit [`7cdc25d`](https://github.com/whanyu1212/Wisp/commit/7cdc25dcc171f048526b7fcc3faa96ac9fd26016)
against the pinned [SWE-bench Pro V2 release](https://github.com/scaleapi/SWE-bench_Pro-os/tree/66f92766bba642462d4bbe5479e83f91f9211862/v2):

- **Scope and controls:** 8 selected tasks out of the 642-task public suite (spanning five
  repositories and two Hard-51 tasks), with 1 attempt per task, model, and prompt arm. Each run had
  network access only to the model endpoint and a 20-minute solve cap, and every patch was graded in
  a fresh container by the official verifier under a separate 12-minute cap. On all eight tasks, the
  reference patch passed and the empty patch failed.
- **Outcome:** **32/32 attempts passed** the official verifiers across both arms (before and after
  prompt edits in
  [`evals/tool_use/guidance/`](https://github.com/whanyu1212/Wisp/tree/main/evals/tool_use/guidance)):
  - **GPT-6.2 Sol (high effort):** 8/8 before and 8/8 after; mean tool rounds per pass fell from
    **35.00 to 29.62** (280 → 237 total tool rounds).
  - **GPT-6.2 Astra (medium effort):** 8/8 before and 8/8 after; mean tool rounds per pass moved
    from **16.25 to 17.88** (130 → 143 total tool rounds).

Because this is a selected eight-task sample run once per cell, the report treats the tool-round
deltas as preliminary guidance evidence rather than a full-suite benchmark claim.

## Interruption and fault-injection invariants

Offline runtime correctness is checked independently of live model quality. In
[`tests/agent/harness/test_interruptions.py`](https://github.com/whanyu1212/Wisp/blob/main/tests/agent/harness/test_interruptions.py),
Wisp records the full event baseline for six deterministic workflows (`stream`, `sequential`,
`parallel`, `queues`, `replacement`, and `rebase`), then replays each workflow from scratch and
injects `cancel()` or explicit stream `aclose()` after **every single emitted event boundary**.
Companion fault cases (`test_failure_keeps_accepted_state_and_recovers`) inject `provider`, `tool`,
`boundary`, and `stale-rebase` errors mid-run.

One concrete invariant that must survive every interruption and fault:

- **Correlated tool-result repair and reusable harness state:** If a run is cancelled or a tool
  raises after `ToolCallRequested`, every requested call either keeps its completed result or is
  repaired on resume with an error tool message (`INTERRUPTED_TOOL_RESULT_TEXT`) placed after its
  originating assistant message—with zero duplicate tool IDs. Accepted transcript prefixes and
  queued steering/follow-up FIFO ordering stay intact, a rejected stale context rebase never
  overwrites live messages, and calling `harness.continue_()` on the same `AgentHarness` instance
  still satisfies every turn, continuation, and queue invariant.

Run the interruption and production-fault suites locally (no credentials required):

```bash
uv run pytest tests/agent/harness/test_interruptions.py tests/agent/invariant_checks/test_live_runs.py
uv run pytest tests -m production_fault --durations=20
```

See [Testing: Harness interruption and recovery](../contributing/testing.md#harness-interruption-and-recovery)
for the full matrix and `production_fault` inventory (truncated provider streams, partial session
and auth writes, stale session writers, SDK shutdown cancellation, and process-tree cleanup).

## Protocol fuzzing

The Rust frontend and Python backend communicate over strict LF-delimited JSONL frames that reject
unknown fields and duplicate keys. Wisp fuzzes that wire boundary with `cargo-fuzz` under
AddressSanitizer; see [`fuzz/README.md`](https://github.com/whanyu1212/Wisp/blob/main/fuzz/README.md).

- **Target and boundary:**
  [`server_wire`](https://github.com/whanyu1212/Wisp/blob/main/fuzz/fuzz_targets/server_wire.rs)
  feeds arbitrary raw bytes directly into Rust's live event and handshake-response decoders (paired
  with [`client_wire`](https://github.com/whanyu1212/Wisp/blob/main/fuzz/fuzz_targets/client_wire.rs)
  for commands and handshake requests). Both targets deliberately avoid generic `serde_json::Value`
  pre-parsing so duplicate object keys are never silently collapsed before typed validation.
- **Oracle and corpus:** Seeds combine every canonical conformance fixture from `schemas/live-rpc/`
  with committed `valid/` and `invalid/` edge-case files (duplicate keys, invalid UTF-8, truncated
  JSON, unknown types, and removed handshake version fields). Invalid inputs may return typed
  errors; every accepted input must survive a canonical round trip, and any panic, sanitizer error,
  timeout, or RSS breach fails the campaign.

PR workflows replay the full seed corpus and run bounded mutation campaigns (up to 10,000
executions or 60 seconds per target at a 64 KiB campaign input cap), with manual 5-, 15-, and
60-minute campaigns available on demand. Fuzzing covers wire decoding and framing properties—not
terminal layout, secret cleanup, or whole-session memory retention, which are tested separately.
