# Python/Rust Tool Boundary Evidence

This report records the September 16, 2026 decision workload for the built-in tool boundary. It
uses the `af13bf1` `main` baseline on Apple Silicon with macOS 27.0, CPython 3.12.2, warm local
filesystem caches, and `tracemalloc` disabled. Fixture creation and one warmup call per path are
outside the measured intervals for the built-in tool benchmark. The process-output benchmark does
not add a separate warmup. Values are per-iteration means across three iterations, so they are
attribution evidence for this machine rather than portable performance thresholds.

## Commands

```bash
uv run python -m benchmarks.builtin_tools \
  --file-counts 1000,5000 --file-bytes 4096 --iterations 3 \
  --output profiles/builtin-tools-current.json
uv run python -m benchmarks.builtin_tools \
  --file-counts 100 --file-bytes 40960 --iterations 3 --tool-only \
  --output profiles/builtin-tools-100x40k.json
uv run python -m benchmarks.process_output \
  --sizes 1048576 --iterations 3 --output profiles/process-output-current.json
uv run python -m cProfile -o profiles/builtin-tools.prof \
  -m benchmarks.builtin_tools --file-counts 1000 --file-bytes 4096 \
  --iterations 1 --tool-only --output profiles/builtin-tools-profile.json
uv run python -m cProfile -o profiles/process-output.prof \
  -m benchmarks.process_output --sizes 1048576 --workloads short_lines \
  --iterations 1 --output profiles/process-output-profile.json
```

Raw JSON and profiler data remain ignored under `profiles/`.

## Built-in tool results

The executor samples include registry lookup, policy, pre-approved approval handling, result
copying and normalization, summary promotion, and the terminal event. Times are milliseconds.

| Files | Scenario | Input | `tool.run` wall | Executor wall | `tool.run` CPU |
|---:|---|---:|---:|---:|---:|
| 1,000 | read first 100 lines | 3.91 MiB file | 0.49 | 0.50 | 0.47 |
| 1,000 | read final 100 lines | 3.91 MiB file | 5.72 | 5.90 | 5.67 |
| 1,000 | sorted `ls` | 1,000 entries | 1.62 | 1.61 | 1.58 |
| 1,000 | sorted-prefix `find` | 1,000 files | 314.16 | 313.55 | 313.88 |
| 1,000 | literal grep miss | 3.91 MiB | 781.87 | 785.11 | 781.15 |
| 1,000 | capped regex grep | first 100 matches | 104.02 | 104.05 | 103.95 |
| 5,000 | read first 100 lines | 19.53 MiB file | 0.48 | 0.46 | 0.46 |
| 5,000 | read final 100 lines | 19.53 MiB file | 27.82 | 27.64 | 27.76 |
| 5,000 | sorted `ls` | 5,000 entries | 6.97 | 7.35 | 6.93 |
| 5,000 | sorted-prefix `find` | 5,000 files | 1,533.92 | 1,549.00 | 1,532.12 |
| 5,000 | literal grep miss | 19.53 MiB | 3,989.09 | 3,977.90 | 3,980.74 |
| 5,000 | capped regex grep | first 100 matches | 103.55 | 103.78 | 103.45 |

Executor-wrapped and direct timings stay in the same range. The normalization and event boundary
therefore do not justify native code. Full-tree `find` and a grep miss scale approximately with
the fixture, and their CPU time nearly equals wall time on a warm cache.

File shape matters. Holding grep input near 3.91 MiB, a miss took 397.31 ms across 100 files of
40 KiB and 781.87 ms across 1,000 files of 4 KiB. That 1.97x difference points to substantial
per-file traversal, secure-open, and path-normalization work in addition to byte scanning. The
profile agrees: `_python_grep` accounted for 4.355 seconds across its four warmup/measured calls,
incremental line splitting for 1.745 seconds, `Path.relative_to` for 1.510 seconds,
`_python_find` for 1.492 seconds, path resolution for 1.308 seconds, and secure file opening for
0.972 seconds. Search cost is distributed rather than isolated in the regular expression engine.

## Managed-process output results

The production `_PendingText` path consumed one MiB in 8 KiB chunks while retaining the normal
bounded tail.

| Workload | Wall ms | Throughput MiB/s | Slowest chunk ms |
|---|---:|---:|---:|
| ASCII lines | 212.54 | 4.70 | 2.18 |
| Unicode | 982.10 | 1.02 | 12.85 |
| Short lines | 1,321.90 | 0.76 | 15.34 |
| Long line | 258.18 | 3.87 | 2.16 |
| Mixed newlines | 568.64 | 1.76 | 9.13 |
| Invalid UTF-8 | 1,382.75 | 0.72 | 15.70 |

The short-line profile made 12.82 million calls. All 3.94 profiled seconds were below
`_PendingText.append_bytes`; fragment flushing consumed 2.12 seconds cumulatively, trimming
0.63 seconds, current-line reconstruction 0.56 seconds, and per-unit appends 0.52 seconds. This is
a bounded byte-to-text state machine with exact source-byte accounting, and it is the cleanest
native kernel in the tool stack.

## Limits of this evidence

The read and search fixtures are synthetic and warm-cache. They isolate Wisp's scaling and Python
CPU cost; they do not predict cold filesystem latency or the file shapes of every repository.
Write and edit are omitted because their contract deliberately includes atomic replacement,
metadata preservation, and `fsync`, and there is no measured CPU kernel that would justify putting
those security-sensitive operations behind a native boundary.

The managed-output benchmark isolates `_PendingText`; it does not include child startup, pipe
reads, polling, executor normalization, persistence, RPC, or rendering. Its result is sufficient to
justify a bounded prototype, not to ship a native extension by itself. The prototype must add an
end-to-end managed-process comparison before adoption so the retained-output share of user-visible
latency is measured directly.

The executor comparison covers sequential, pre-approved read tools. It does not measure approval
waits, mutating-tool normalization, concurrent scheduling, persistence, RPC, or repositories with
protected paths, symlinks, and complex ignore files. The ownership decisions for those areas follow
their existing correctness responsibilities; they are not claims of measured speedups.

## Boundary decision

| Area | Decision | Evidence and constraint |
|---|---|---|
| Tool registry, policy, approvals, and orchestration | Keep in Python | Executor overhead is small, and these layers own authority and event ordering. |
| Protected paths and secure filesystem writes | Keep in Python | Read/write/edit semantics depend on descriptor-relative access, no-follow checks, atomic replace, metadata, and `fsync`; the measured reads are not a priority. |
| Process creation, cancellation, timeout, polling, and tree cleanup | Keep in Python | These are lifecycle and safety responsibilities rather than the measured compute hotspot. |
| Managed-process output retention | Prototype in Rust first | It is isolated, CPU-bound, call-heavy, and slow across several byte shapes. Preserve the Python `ProcessSupervisor` API and exact retention accounting, with a Python fallback. |
| Search traversal and matching | Optimize and remeasure in Python next | Current cost is meaningful, but the profile spreads it across traversal, secure opens, path normalization, decoding, and matching. Reduce repeated path work before accepting PyO3 and wheel complexity. |
| Session persistence, RPC contracts, provider adapters, and agent runtime | Keep in Python | They define durable and provider-specific semantics. Continue batching RPC deltas before considering native transport. |
| TUI rendering | Rust | The Rust TUI remains the default presentation client over the shared RPC boundary. |

The next native experiment should replace only `_PendingText`'s bounded state machine behind its
existing Python-facing contract. It must pass the current Unicode, invalid-byte, newline,
retained-byte, dropped-byte, cancellation, and process-cleanup tests, and it must retain a pure
Python fallback. A production PyO3 extension also needs an explicit wheel and ABI plan because the
current native wheel packages a standalone Rust executable rather than an imported extension.

Search is the second performance track, but this evidence does not support moving it wholesale.
First remove avoidable repeated resolution/display-path work without weakening descriptor-relative
access or symlink rules. If representative repositories remain CPU-bound, benchmark a batched Rust
scanner that receives a Python-authorized root and bounded search specification and returns bounded
matches. Python should continue to own permission, protected-path, cancellation, and result-ordering
policy even if that inner scanner eventually becomes native.
