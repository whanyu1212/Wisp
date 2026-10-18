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

## Python search optimization follow-up

The September 16 follow-up measured the same synthetic workload before and after preparing glob
matchers once per invocation and reusing each candidate's display path. Both runs used the current
macOS arm64 machine, CPython 3.12.2, warm filesystem caches, and three iterations. The table reports
the direct `tool.run` path; executor timings remained in the same range.

| Files | Scenario | Before wall ms | After wall ms | Change | After CPU ms |
|---:|---|---:|---:|---:|---:|
| 1,000 | sorted-prefix `find` | 325.24 | 209.73 | -35.5% | 209.52 |
| 1,000 | literal grep miss | 848.98 | 795.24 | -6.3% | 794.70 |
| 1,000 | capped regex grep | 111.13 | 105.96 | -4.7% | 105.84 |
| 5,000 | sorted-prefix `find` | 1,555.87 | 1,037.03 | -33.3% | 1,034.35 |
| 5,000 | literal grep miss | 4,033.82 | 4,123.75 | +2.2% | 4,115.98 |
| 5,000 | capped regex grep | 106.64 | 100.14 | -6.1% | 99.82 |

The profile explains the `find` improvement: its cumulative time fell from 1.56 seconds to 1.02
seconds in the 1,000-file profiling workload. `display_tool_path` calls fell from 4,200 to 2,200,
and brace expansion fell from 2,000 calls to two. A no-glob grep miss deliberately performs no
display-path conversion, so its small variation is measurement noise rather than an optimization
claim. Its remaining cost is streamed decoding, secure per-file opening, protected-path checks, and
matching.

The new `benchmarks.repository_search` workload was also run on Wisp's `src/wisp` tree, with 216
observable Python files and five iterations. The before run loaded the pre-change search module
into the same public tool calls; the after run used the optimized module. Both used the same
benchmark configuration and checked matching result counts, truncation, and output size.

| Existing-repository scenario | Before wall ms | After wall ms | Change | After CPU ms |
|---|---:|---:|---:|---:|
| sorted Python-file `find` prefix | 112.72 | 97.53 | -13.5% | 97.42 |
| literal grep miss | 498.62 | 496.74 | -0.4% | 495.34 |
| capped literal grep | 59.69 | 53.17 | -10.9% | 53.03 |

This project is small and the workloads were warm-cache; other repositories can have different
ignore rules, file shapes, protected paths, and storage behavior. The exhaustive miss still takes
about half a second here and remains CPU-bound. In the synthetic profile, streamed line splitting
is the largest individual grep cost (1.81 seconds), while secure opens and path checks also remain
substantial.

This follow-up does not justify moving tool policy or filesystem authority into Rust. It does
justify a separate bounded scanner prototype under Python-owned authorization, protected-path,
symlink, ordering, and result policy. The prototype must beat the optimized Python path on both
synthetic and representative repositories before integration is considered.

## Native literal scanner follow-up

The next experiment narrowed the boundary further after inspecting the secure walk. Python still
traverses the repository, applies ignore and protected-path policy, and opens every regular file
through the descriptor-relative no-follow path. Rust receives that live authorized descriptor and
performs only the streaming literal scan. On Linux it duplicates the descriptor through
`/proc/self/fd`; on macOS it uses `/dev/fd`. It never reopens the original repository pathname.

The scanner uses 64 KiB reads, strict UTF-8, Python-compatible `splitlines()` boundaries, bounded
context retention, one-match truncation lookahead, and an atomic cancellation flag. Regex searches
and case-insensitive literals keep using Python because Rust engines do not exactly reproduce the
existing Python `regex` and Unicode `casefold` contracts. Pure Python wheels also keep the existing
scanner as their automatic fallback.

Both backends were measured from the same checkout and optional extension, selecting the scanner
with `--grep-backend`. Runs used warm filesystem caches on the same Apple Silicon machine and
CPython 3.12.2. The existing-repository samples used seven iterations on Wisp's 216-file `src/wisp`
tree; the synthetic samples used five direct-tool iterations over 1,000 files of 4 KiB each.

| Workload | Python wall ms | Native wall ms | Change | Native CPU ms |
|---|---:|---:|---:|---:|
| Wisp literal miss | 633.09 | 365.39 | -42.3% | 364.57 |
| Wisp capped literal | 65.78 | 56.87 | -13.5% | 56.72 |
| 1,000-file literal miss | 998.51 | 744.30 | -25.5% | 742.08 |
| 1,000-file capped literal | 118.63 | 82.04 | -30.8% | 81.81 |

Counts, truncation flags, and output byte sizes matched for every paired sample. Native/Python
conformance also covers Unicode literals, every supported split-line boundary, context groups,
binary and invalid UTF-8 rejection, long-line limits, output bounds, and exact-limit lookahead.

These results clear the 25% adoption threshold for exhaustive and synthetic workloads. The shipped
boundary remains per-file scanning rather than native repository traversal: Python owns authority,
ignore precedence, glob selection, secure opening, global ordering, and final `ToolResult`
assembly. Moving traversal or regex matching needs separate compatibility and performance evidence.

## Protected-path matching after native scanning

The next profile used the shipped native scanner on Wisp's 216-file `src/wisp` tree. It showed that
`is_protected_path` matched each ordinary path against the default 25 protected globs twice: once
lexically and once after resolving symlinks, even when both paths were equal. Across the profiled
benchmark invocation, matching fell from 0.842 to 0.427 cumulative seconds after skipping the
identical second match. Resolution and target matching remain in place when a symlink changes the
path. Ignore-rule ordering took only about 0.003 seconds, so caching that ordering would have much
less impact on this workload.

Warm-cache runs on the same macOS arm64 machine and CPython 3.12.2 used seven iterations per
repository scenario, the same optional native extension, and matching result counts, truncation,
and output sizes. Wall time is per public `tool.run` call:

| Wisp `src/wisp` workload | Before wall ms | After wall ms | Change |
|---|---:|---:|---:|
| sorted Python-file `find` prefix | 99.34 | 84.79 | -14.6% |
| native literal grep miss | 301.96 | 276.27 | -8.5% |
| native capped literal grep | 47.79 | 39.18 | -18.0% |

The existing 1,000- and 5,000-file synthetic benchmark explicitly disables protected-path rules
to isolate other tool costs. Its measurements stayed within a few percent of baseline, as expected;
they do not measure this optimization. The remaining expensive boundaries are secure per-file
reopening, protected-path resolution, and ignore matching. Changing the first two requires a
separate race and symlink safety design; this result does not support moving filesystem policy into
Rust.

## Descriptor reuse during grep traversal

The follow-up kept traversal and filesystem authority in Python, but stopped reopening every grep
candidate from the filesystem root. On POSIX, the walker now opens an eligible file relative to its
already-authorized parent directory descriptor. It uses the same no-follow flags as the secure file
API and compares post-open metadata with the directory entry, skipping a file that was replaced
between enumeration and opening. The walker owns and closes the descriptor around both Python and
native scanning. Windows and path-based traversal retain the existing secure path open.

Paired warm-cache runs used the same macOS arm64 machine, CPython 3.12.2, optional native extension,
and five iterations for each synthetic scenario. The Wisp `src/wisp` runs used seven iterations.
Counts, truncation flags, and output sizes matched for every pair.

| Workload | Before wall ms | After wall ms | Change |
|---|---:|---:|---:|
| 1,000-file native literal miss | 733.63 | 318.97 | -56.5% |
| 5,000-file native literal miss | 3,345.89 | 2,042.05 | -39.0% |
| 5,000-file capped Python regex | 122.11 | 97.36 | -20.3% |
| 5,000-file capped native literal | 72.84 | 45.36 | -37.7% |
| Wisp native literal miss | 346.78 | 256.28 | -26.1% |
| Wisp capped native literal | 48.62 | 43.48 | -10.6% |

The exhaustive 5,000-file workload clears the 15% adoption threshold. This result supports reusing
Python-owned descriptors; it does not support moving traversal, ignore rules, protected-path policy,
or regex semantics into Rust. Further search migration should wait for another measured hotspot.
