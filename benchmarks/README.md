# Benchmarks

These scenarios answer two separate questions:

- Benchmark JSON shows how cost scales and whether a change improved it.
- `cProfile` output shows which Python calls account for that cost.

Generate profile artifacts under the ignored `profiles/` directory. Compare absolute timings only
on the same machine, with the same Python build and benchmark arguments.

Run the transcript-windowing scenario:

```bash
uv run python -m benchmarks.tui_long_session
uv run python -m benchmarks.tui_long_session --messages 5000 --page-size 100 --output before.json
```

The scenario creates a temporary JSONL session, reads its production pages, renders
them in the headless Textual TUI, streams an assistant response, and scrolls while a
managed CPU-active shell process runs. It reports JSON with page-read and rendering
durations, mounted-widget growth, retained-history growth, stream durations, final
scroll state, and process cleanup state. With no `--messages` argument it runs both
the 2,000- and 5,000-message scenarios; repeat `--messages` to choose a different
suite.

The CPU worker runs until it is cancelled immediately after the scroll measurement,
with a 60-second safety timeout. Streaming metrics therefore run without its load.

Compare absolute timings only on the same machine. `warm_newest_page_read_ms` and
`older_page_read_ms` measure cache-backed paging after the cold initial read.
`mounted_widget_counts` remains bounded by the 300-entry history window (plus the
session marker), while `retained_entry_counts` remains bounded by the 1,200-entry
history retention limit. This intentionally executes a local shell command through
`ProcessSupervisor`; it uses a temporary directory and is cancelled before exit.

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

## Context Estimation

Measure the complete transcript scans performed by context estimation and fingerprinting:

```bash
uv run python -m benchmarks.context_estimation
uv run python -m benchmarks.context_estimation --messages 100,1000 --iterations 5
```

Fixture construction is outside the measured operations. The combined measurement represents the
two production scans occurring together.

## RPC Streaming Codec

Measure event construction, JSON serialization, validation, and their complete round trip while
holding response size constant and varying provider delta size:

```bash
uv run python -m benchmarks.rpc_streaming
uv run python -m benchmarks.rpc_streaming --response-bytes 65536 --chunk-sizes 1,32,256
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

## Diff Generation

Measure production structured-diff generation for localized edits, replacements, repeated lines,
and long lines:

```bash
uv run python -m benchmarks.diff_generation
uv run python -m benchmarks.diff_generation --line-counts 1000,3500,10000 --track-memory
```

Inputs refused by the production event-loop safety guard are reported with `guarded: true` and no
retained rows. This keeps the guard cost visible without misrepresenting it as diff computation.

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
