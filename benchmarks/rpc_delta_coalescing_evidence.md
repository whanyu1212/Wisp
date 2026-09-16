# JSONL-RPC Delta Coalescing Evidence

This report records the September 16, 2026 JSONL-RPC output-boundary optimization. The agent loop,
in-process SDK, persisted events, and ordinary JSON CLI retain their original event streams. Only
the JSONL-RPC adapter used by the Rust TUI combines adjacent compatible `message.delta` events.

## Boundary and invariants

The RPC writer receives events after the host establishes their order. It combines deltas only
when schema version, turn, role, content index, and content kind match. It flushes before every
other event and before a differing delta, and preserves the first event's timestamp. A batch is
bounded by 8 KiB of UTF-8 payload, 256 source deltas, or eight milliseconds from its first delta.
Shutdown flushes a final partial batch. These limits keep live text visible while reducing JSON
serialization, stdout writes, Rust framing, parsing, and bounded-queue admissions.

The coalescer remains outside `RpcHost` and the agent runtime. This keeps the transport-independent
host, SDK subscriptions, provider events, persistence, and protocol schema unchanged.

## Measurement

The RPC codec benchmark preconstructed a 256 KiB response as 262,144 one-byte typed
deltas. Both modes used the normal bounded RPC frame encoder and current event parser with three
iterations on the same macOS arm64 machine and CPython 3.12.2. The byte sink was in memory, so the
measurement excludes operating-system pipe, Rust parsing, and terminal-rendering cost. Exact
reconstructed content matched in both modes. Separate Python-backend/Rust-frontend handoff tests
exercise the production process boundary for compatibility, but do not produce timing evidence.

| Mode | Frames | Wire bytes | Wall ms/iteration | CPU ms/iteration |
|---|---:|---:|---:|---:|
| Raw deltas | 262,144 | 43,778,048 | 1,970.95 | 1,940.50 |
| Coalesced RPC | 1,024 | 432,128 | 178.66 | 175.27 |

Frame count fell 99.6%, wire bytes fell 99.0%, wall time fell 90.9%, and CPU time fell 91.0% in
this deliberately adversarial workload. The production gain depends on provider chunk size and
cadence: already-large or slowly spaced deltas will coalesce less. The eight-millisecond deadline
limits added first-frame latency for paced streams.

This result supports Python-side batching at the JSONL-RPC adapter. It does not support moving RPC,
event models, or the agent loop into Rust.
