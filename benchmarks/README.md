# Benchmarks

These scenarios answer two separate questions:

- Benchmark JSON shows how cost scales and whether a change improved it.
- `cProfile` output shows which Python calls account for that cost.

Generate profile artifacts under the ignored `profiles/` directory. Compare absolute timings only
on the same machine, with the same Python build and benchmark arguments.

Measure JSONL-RPC framing and client-side typed parsing with raw one-byte provider deltas and the
production RPC coalescer:

```bash
uv run python -m benchmarks.rpc_delta_egress \
  --response-bytes 262144 --chunk-bytes 1 --iterations 3 \
  --output profiles/rpc-delta-egress.json
```

The workload preconstructs identical typed events for both modes, then measures the RPC frame
encoder, byte sink, and normal event parser. It reports frame and wire-byte counts plus exact
reconstructed-content validation. It does not include provider latency, operating-system pipe
writes, or terminal rendering. The current decision evidence is recorded in
`benchmarks/rpc_delta_coalescing_evidence.md`.

## Rust TUI acceptance (#470)

The renderer decision and the measurements that could not run are recorded in
`benchmarks/rust_tui_acceptance_evidence.md`. Refresh the in-process transcript snapshot below when
repeating that gate. Do not treat Textual-only input-latency numbers as a Rust comparison.

## Rust TUI end-to-end and interaction benchmarks

Build the Rust frontend, then run the source CLI through a real POSIX PTY with a deterministic
fake-provider prompt:

```bash
cargo +1.85.0 build --release -p wisp-tui
uv run python -m benchmarks.rust_tui_e2e \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --output profiles/rust-tui-e2e.json
uv run python -m benchmarks.rust_tui_interaction \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --output profiles/rust-tui-interaction.json
```

These scripts measure PTY-output timing, terminal restoration, and process-tree resource samples.
PTY output is a paint proxy rather than a display-photon measurement. Samples are comparable only
with the same machine, build, and workload. The historical Rust/Textual comparisons remain in
`benchmarks/rust_tui_e2e_evidence.md` and `benchmarks/rust_tui_interaction_evidence.md`; their
Textual runners were retired with the frontend.

## Rust TUI saved-history startup

Profile the source CLI's Rust startup with fresh and fully hydrated saved sessions:

```bash
uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --output profiles/rust-tui-hydration.json
```

The harness uses an opt-in `WISP_HYDRATION_PROFILE_DIR` directory for content-free Python and Rust
stage timings. It records each page read, selected-session refresh, report publication, Rust page
projection and clone, final transcript projection, and history installation alongside
launch-to-ready and observed process-tree CPU/RSS. Use `--no-profile` for the wall-clock comparison
without per-page profile file writes. Stage totals omit RPC scheduling, process startup, transport,
and drawing, so they do not sum to launch-to-ready. The before/after evidence and limitations are in
`benchmarks/rust_tui_hydration_evidence.md`.

For per-process transcript memory, add `--no-profile --ready-hold-seconds 1`. The
report records CLI launcher, Python RPC backend, and Rust TUI RSS when the
history-ready frame appears and after one idle second. The process-tree peak
still covers the whole run. Local 0/10k/50k results and a matched Rust/Textual
interaction comparison are in `benchmarks/rust_tui_memory_evidence.md`.

## Rust TUI Transcript

Measure the production Rust transcript, viewport, Markdown/syntax, tool-card, structured-detail,
and Ratatui draw paths without terminal I/O:

```bash
mkdir -p profiles
cargo +1.85.0 run --release -p wisp-tui \
  --features transcript-benchmark --example transcript_benchmark -- \
  --entries 1000,10000,100000 --runs 5 \
  --output profiles/rust-tui-transcript.json
```

The benchmark feature is disabled in production builds and adds no benchmark framework. Each
condition uses the same rich 512-entry suffix at 100x24, then measures cold and warm frames,
PageUp/PageDown, resize, 100 growing Markdown updates with closed Rust fences, and cold opening plus
paging through the full row-budget-saturating structured diff. JSON contains only timings, bounded
work counters, environment metadata, and correctness flags—never transcript text, paths, tool
payloads, identifiers, or terminal cells. Linux CPU and machine-model metadata use `/proc` and
sysfs fallbacks when the macOS `sysctl` keys are unavailable.

`stream_process_cpu_ms` uses `getrusage` across the fixed update-and-draw region. `stream_stall_ms`
is synchronous mutation-plus-`Terminal::draw` wall time. `detail_open_ms` includes eagerly formatting
the retained rows and the first detail draw. Neither metric is event-loop, PTY, terminal-write, or
perceptual latency. Compare absolute timings only from release builds on the same machine.
Machine-independent checks assert source completeness, anchor/follow state, cache reuse, bounded
incremental parsing/highlighting, and identical visible work across transcript lengths; CI must not
assert machine-specific timing thresholds.

For a macOS sampling profile, build once and repeat the unchanged 100k workload long enough for
`sample` to observe it:

```bash
cargo +1.85.0 build --release -p wisp-tui \
  --features transcript-benchmark --example transcript_benchmark
target/release/examples/transcript_benchmark \
  --entries 100000 --runs 500 --output /tmp/rust-tui-profile.json >/dev/null &
pid=$!
/usr/bin/sample "$pid" 2 1 -mayDie \
  -file profiles/rust-tui-transcript.sample.txt
wait "$pid"
```

A single-entry profiling run reports `scaling_work_independent: false` because no cross-length
comparison is available. Use Linux
`perf record -g -- target/release/examples/transcript_benchmark ...` for equivalent native sampling. Raw JSON and profiles stay under ignored `profiles/`; commit only compact numeric
evidence and factual profiler conclusions. See `benchmarks/rust_tui_transcript_evidence.md`.

## Historical Textual benchmark evidence

The Textual-only benchmark runners were removed when that frontend was retired. Their recorded
results remain as dated evidence in `tui_responsiveness_evidence.md`,
`tui_terminal_frames_evidence.md`, and other historical reports; the commands inside those reports
are not runnable against the current source tree.

## Managed Process Output

Run the deterministic bounded-output benchmark with production retention limits:

```bash
uv run python -m benchmarks.process_output
uv run python -m benchmarks.process_output --sizes 1048576,2097152 --output process-output.json
```

It reports per-size elapsed time, throughput, retained bytes, and exact dropped-byte
accounting while retaining only the configured output tail. Workloads cover ASCII, Unicode,
newline-heavy output, long lines, mixed line endings, and invalid UTF-8:

```bash
uv run python -m benchmarks.process_output --sizes 1048576 --track-memory
```

Measure the complete managed-process path, including child startup, pipe reads, retention, polling,
and cleanup:

```bash
uv run python -m benchmarks.managed_process_output \
  --sizes 1048576 --iterations 1 \
  --output profiles/managed-process-output.json
```

The child reads a fixture prepared outside the timed interval and writes it in configurable chunks.
The report includes process wall and CPU time, throughput, poll count, maximum poll duration, and
exact retained-plus-dropped source-byte accounting. With multiple iterations, the maximum poll spans
all measured runs while the count and byte totals describe the final run. This is a lifecycle
measurement; use the direct benchmark above to attribute time to the retention kernel.

Benchmark the conforming Rust retention kernel in a release build with the same input size, chunk
size, workloads, and production limits:

```bash
cargo run --release -p wisp-process-text \
  --example process_output_benchmark -- \
  --size 1048576 --chunk 8192 --iterations 5
```

Python and Rust both execute `tests/fixtures/pending_text_conformance.json`. The fixture checks text,
dropped and retained source bytes, and per-character source-byte provenance at every drain. Current
comparison results and the integration boundary are recorded in
`benchmarks/pending_text_rust_evidence.md`.

## Built-in Tools

To compare search changes on an existing checkout, run the public direct `find` and
`grep` paths on the same project root before and after the change:

```bash
uv run python -m benchmarks.repository_search --root . --iterations 3 \
  --output profiles/repository-search.json

# In a native wheel or development environment with wisp._native installed:
uv run python -m benchmarks.repository_search --root . --iterations 7 \
  --grep-backend python --output profiles/repository-search-python.json
uv run python -m benchmarks.repository_search --root . --iterations 7 \
  --grep-backend native --output profiles/repository-search-native.json
```

`--root` must be inside the current working directory. The tool's normal protected-path,
ignore, and symlink rules apply. The report records the observed Python-file count (capped
at 10,001), result counts and truncation, and wall/CPU time per iteration. It contains no
matched source text. `--common-token` (default `def `) and `--max-results` (default 100)
select the capped literal-grep workload; keep these and the output bounds identical between
runs. Warmup and file discovery are outside the measured intervals.

Measure deterministic read, directory listing, find, and grep workloads through both the public
`tool.run` interface and `ConfiguredToolExecutor`:

```bash
uv run python -m benchmarks.builtin_tools
uv run python -m benchmarks.builtin_tools \
  --file-counts 1000,5000 --file-bytes 4096 --iterations 3 \
  --output profiles/builtin-tools.json
```

Fixture construction and one warmup call per path happen outside the measured interval. The read
cases compare a first page with a page near end-of-file; `ls` and `find` retain sorted prefixes;
grep covers a full-tree literal miss plus result-capped literal and regular-expression searches.
Use `--grep-backend python|native` for a controlled scanner comparison when the native extension is
installed. All files live in a temporary directory. Every direct result is checked against the
fixture; executor samples also reject tool errors and unexpected truncation before they are
reported.

The executor layer includes registry lookup, policy, approval bypass for pre-approved read tools,
result copying, normalization, promotion, summary generation, and construction of the terminal
event. It deliberately stops before session persistence, RPC serialization, or rendering, which
already have separate benchmarks. To isolate tool work, use `--tool-only`.

Normal timing runs leave `tracemalloc` disabled. Run allocation evidence separately:

```bash
uv run python -m benchmarks.builtin_tools --file-counts 1000 --iterations 1 --track-memory
```

The current cross-layer measurements and Python/Rust ownership decision are recorded in
`benchmarks/tool_boundary_evidence.md`.

## Context Estimation

Measure the complete transcript scans performed by context estimation and fingerprinting:

```bash
uv run python -m benchmarks.context_estimation
uv run python -m benchmarks.context_estimation --messages 100,1000 --iterations 5
```

Fixture construction is outside the measured operations. The combined measurement represents the
two production scans occurring together.

Running the module directly also reports `accuracy` samples measuring fallback-estimator error
against checked-in `cl100k_base` token counts across representative workloads (source code, JSON
schema, large tool results, CJK, emoji, and mixed conversation).

## RPC Streaming Codec

Measure event construction, JSON serialization, validation, and their complete round trip while
holding response size constant and varying provider delta size:

```bash
uv run python -m benchmarks.rpc_streaming
uv run python -m benchmarks.rpc_streaming --response-bytes 65536 --chunk-sizes 1,32,256
uv run python -m benchmarks.rpc_streaming --response-bytes 65536 --chunk-sizes 1,32,1024 --iterations 2
```

This isolates codec overhead from subprocess pipes and Textual rendering, making event-count and
batching costs visible before considering native JSON work.

## Session Loading

Measure cold and warm newest-page reads, complete parsing, replay, older-page reads, and appends
after index initialization:

```bash
uv run python -m benchmarks.session_loading
uv run python -m benchmarks.session_loading --entries 2000,10000,50000 --iterations 1
```

Session generation is intentionally outside the reported measurements.

## CPU Profiles

The standard library profiler can run every scenario without an additional dependency:

```bash
mkdir -p profiles
uv run python -m cProfile -o profiles/process-output.prof -m benchmarks.process_output \
  --sizes 1048576
uv run python -m pstats profiles/process-output.prof
```

At the `pstats` prompt, use `sort cumulative` followed by `stats 30`. Profiling changes absolute
timings, so use ordinary benchmark runs for before/after comparisons and profiles for attribution.
The optional `--track-memory` flag records peak traced Python memory but also adds overhead; do not
compare those timings with runs where memory tracking is disabled.
