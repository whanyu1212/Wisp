# Case study: Earning a Rust boundary

Once a tool works correctly, how do you decide whether part of it should move
into another language? Wisp's search and process-output paths offer two concrete
examples. Their useful lesson is the measurement process, not a blanket rule
that coding-agent tools should be native.

## The boundary and its cost

Rust appears in three places around Wisp's tools: `wisp-search` for bounded
literal grep, `wisp-process-text` for incremental decoding and output retention,
and `wisp-tui` for terminal presentation. These crates forbid unsafe Rust code.
This case study examines the first two; the
[frontend architecture](../../architecture/rust-tui-boundary.md) describes the TUI.

The native tool kernels operate on bounded byte-oriented work. Python keeps
traversal policy, approvals, protected paths, orchestration, and result assembly.
Rust receives an already-authorized open descriptor for literal search, or bytes
to decode and retain for process output.

The costs include native wheels, platform CI, dispatch and fallback code, and
semantic parity tests. Binary detection, line boundaries, truncation, and
dropped-byte accounting must agree across implementations. The optional search
and retention accelerators keep Python fallback paths; that does not imply a
Python fallback for the current Rust-only interactive TUI.

## Start with questions a benchmark can answer

Both boundaries were measured before adoption, but they followed different
sequences:

- **Search:** benchmark, profile, remove repeated Python work, then prototype a
  narrow native scanner against an adoption threshold.
- **Output retention:** benchmark and profile, then prototype the native kernel.
  The costs were spread across decoding and provenance bookkeeping rather than
  one Python hotspot that offered an obvious local fix.

The retained reports are
[`benchmarks/tool_boundary_evidence.md`](https://github.com/whanyu1212/Wisp/blob/main/benchmarks/tool_boundary_evidence.md)
and
[`benchmarks/pending_text_rust_evidence.md`](https://github.com/whanyu1212/Wisp/blob/main/benchmarks/pending_text_rust_evidence.md).
The figures below are historical observations from those workloads, not
predictions for every machine or repository.

Two harnesses ask different questions:

- `benchmarks/builtin_tools.py` builds synthetic trees and measures both direct
  `tool.run` calls and `ConfiguredToolExecutor` calls. That comparison tests
  whether policy and lifecycle orchestration add significant overhead.
- `benchmarks/repository_search.py` searches a real checkout: 216 Python files
  under `src/wisp` at the time. This catches ignore rules, protected paths, and
  file shapes that synthetics can miss.

Fixture setup and a warmup call stay outside the measured interval. Results are
checked against fixture oracles—counts, truncation flags, output sizes—before
being reported. Wall and CPU time are recorded separately with environment
metadata. Comparisons require the same machine, Python build, and arguments.
Raw profiler output stays under ignored `profiles/`; compact evidence tables are
committed.

Executor and direct timings stayed in the same range for the measured sequential,
pre-approved read workloads. That was evidence to keep orchestration in Python,
not evidence about approval waits or every possible executor configuration.

## Search: optimize repeated Python work first

The synthetic profile spread time across line splitting (about 1.7 seconds),
`Path.relative_to` (about 1.5 seconds), path resolution (about 1.3 seconds), and
secure opens (about 1 second). No single hotspot justified moving the whole
search stack into Rust.

[PR #593](https://github.com/whanyu1212/Wisp/pull/593) prepared glob matchers once
per call, reused display paths, and kept formatting lazy for no-glob misses.
Sorted-prefix `find` on 5,000 synthetic files fell from 1,555.87 ms to 1,037.03 ms.
On the real tree it fell from 112.72 ms to 97.53 ms; capped grep improved by about
11 percent. Exhaustive misses stayed CPU-bound, pointing toward decoding and
per-file access rather than glob matching.

[PR #595](https://github.com/whanyu1212/Wisp/pull/595) added a native literal,
case-sensitive scanner over an already-open descriptor. Regex and case-insensitive
search stayed in Python because duplicating Python regex and Unicode semantics
would expand the parity risk. Against a 25 percent adoption threshold, the
real-tree literal miss improved 42.3 percent and synthetic 1,000-file misses
improved 25.5–30.8 percent.

The boundary did not end Python optimization:

- [PR #597](https://github.com/whanyu1212/Wisp/pull/597) skipped duplicate
  protected-path matching when lexical and resolved paths were equal.
- [PR #599](https://github.com/whanyu1212/Wisp/pull/599) opened grep candidates
  relative to an already-authorized parent descriptor instead of rewalking from
  the root. That change alone cut the 1,000-file native miss by 56.5 percent and
  the real-tree miss by 26.1 percent, with matching counts and truncation.

A successful native kernel can make surrounding Python overhead more visible.
Profile the integrated path again rather than assuming that the remaining work
must also move languages.

## Retention: distinguish kernel wins from end-to-end wins

[PR #588](https://github.com/whanyu1212/Wisp/pull/588) established baseline
harnesses. The Python `_PendingText` path took about 1.32 seconds for 1 MiB of
short lines with 12.82 million profiler calls. The standalone Rust prototype in
[PR #589](https://github.com/whanyu1212/Wisp/pull/589) ran 15.6–42 times faster
across six byte shapes against the same 27-case conformance corpus.

After integration through the existing `ProcessSupervisor` API in
[PR #590](https://github.com/whanyu1212/Wisp/pull/590), the installed managed-process
benchmark improved 1.8–3.9 times for Unicode, short lines, mixed newlines, and
invalid UTF-8. ASCII and long-line cases sat near the polling floor, while CPU
time still fell more than eightfold.

The smaller integrated speedup is not a contradiction. The direct kernel
benchmark excludes child processes. The installed benchmark includes spawning,
pipes, polling, terminal observation, and cleanup. Only retention moved to Rust;
process ownership stayed in Python.

## State what the evidence does not show

These warm-cache measurements do not establish cold-filesystem latency. The
executor comparison excludes interactive approval waits, concurrent scheduling,
persistence, and RPC. Neither tool throughput nor deterministic conformance
measures a live model's success rate on coding tasks.

Keep three questions distinct:

1. **Correctness:** did both implementations produce the required results?
2. **Performance:** did the measured operation improve under stated conditions?
3. **Agent effectiveness:** did a model solve more tasks, with acceptable cost
   and user intervention?

This evidence supports the first two for particular workloads. Answering the
third needs task-level evaluation.

## A method to reuse

Write a benchmark with an oracle. Profile before choosing a language. Remove
avoidable work. Narrow the proposed native boundary until its semantics and
adoption threshold are explicit. Measure both the kernel and the integrated
workflow, and retain parity coverage for the fallback path.

Your agent may have different hotspots. The transferable result is knowing how
to justify the boundary—and which claim to retest when the workload changes.

Return to [chapter 2](../02-tools.md) or the [curriculum](../index.md).
