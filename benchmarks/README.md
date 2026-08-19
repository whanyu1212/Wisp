# Benchmarks

These scenarios answer two separate questions:

- Benchmark JSON shows how cost scales and whether a change improved it.
- `cProfile` output shows which Python calls account for that cost.

Generate profile artifacts under the ignored `profiles/` directory. Compare absolute timings only
on the same machine, with the same Python build and benchmark arguments.

Run the complete-history hydration scenario:

```bash
uv run python -m benchmarks.tui_long_session
uv run python -m benchmarks.tui_long_session --messages 5000 --page-size 100 --output before.json
```

The scenario creates a temporary JSONL session, reads every production history page,
converts and mounts the complete transcript through the Textual hydration path, streams
an assistant response, and scrolls while a managed CPU-active shell process runs. It
reports JSON with page-read, conversion, complete-mount, first-wheel response, stream,
final scroll-state, and process-cleanup measurements. Row-coverage fields compare persisted messages
with represented row IDs, while `hydrated_entry_count`, `mounted_widget_count`, and
`persisted_rows_per_widget` expose the reduction from logical process grouping. The fixture includes
repeated process polls so a regression to one widget per persisted row is visible. With no `--messages` argument it
runs both the 2,000- and 5,000-message scenarios; repeat `--messages` to choose a
different suite.

This benchmark measures the chosen `/resume` tradeoff: all structural rows are read and retained up
front, so conversion and mount time scale with session length, while scrolling should respond without
requesting or mounting another durable page. Text and arguments remain preview-bounded during that
initial phase; exact persisted output is an interactive, on-demand path and is intentionally outside
the cold-mount timing.

The CPU worker runs until it is cancelled immediately after the scroll measurement,
with a 60-second safety timeout. Streaming metrics therefore run without its load.

## TUI Streaming Hotpaths

Measure the real Rich Markdown stream path at the production first-page size, the production
mounted-history window, and a larger retained-history pressure case:

```bash
mkdir -p profiles
uv run python -m benchmarks.tui_stream_hotpaths --runs 5 \
  --output profiles/tui-stream-hotpaths.json
```

The default matrix retains exactly 60, 75, and 300 history entries. The 60-entry production
window means every condition mounts 60 history widgets; the latter two retain additional entries
to isolate retained-history pressure from mounted-widget growth before starting the same command
lifecycle that keeps Wisp's 80 ms working indicator at the transcript tail while streaming 100
chunks at 20 ms intervals. It rotates condition order between runs and reports
individual samples plus per-condition medians. `event_loop_delay` comes from a separate 10 ms
absolute-deadline heartbeat. `layout_passes` wraps Textual's private `_refresh_layout` seam and
`compositor_renders` wraps `_compositor_refresh` only during the streaming phase. These timings
may overlap and must not be added together.

Capture a profile for one unambiguous streaming condition without import, fixture-construction,
or initial-history-render noise:

```bash
uv run python -m benchmarks.tui_stream_hotpaths --retained-history 300 --runs 1 \
  --profile-output profiles/tui-stream-300.prof
uv run python -m pstats profiles/tui-stream-300.prof
```

`--mounted-history` remains accepted as a compatibility alias for `--retained-history`.

Compare production Rich Markdown streaming with the literal-text floor through the same Textual
controller, transcript, pacing, and follow behavior:

```bash
uv run python -m benchmarks.tui_stream_renderers --messages 2000 --runs 3
```

The harness rotates mode order between runs and restores its temporary plain-render patch after
each scenario.

The instrumentation is installed and restored inside the benchmark process; production TUI code
is unchanged. In addition to paced wall time, each sample reports `stream_cpu_ms`, which uses
process CPU time to exclude intentional sleeps without pretending CPU cost is a latency metric.
`layout_requests` attributes `layout=True` refresh requests by concrete widget class, while
`layout_passes_per_stream_update` shows whether paced writes trigger additional settlement layouts.
`content_height_calls` attributes Textual height measurements by concrete widget class.
`markdown_source_rebuild_count` counts source-to-renderable rebuilds separately from Rich visual
renders. `markdown_source_chars_processed` sums the full source length at each rebuild, exposing
repeated whole-document work as the response grows; compare it only with identical streamed content.
`markdown_renders` splits visual renders between the mutable streaming widget (`active`) and
`StreamMessage` widgets mounted before streaming (`settled`). A zero settled count is valid when
Textual reuses prior measurements during the measured phase.

Treat JSON and profile files as machine-local evidence. Compare timings only on the same machine,
Python/Textual versions, viewport, and arguments, and report individual samples alongside medians
rather than promoting one run to a portable threshold. Counts provide attribution evidence, not
portable performance thresholds.

Compare absolute timings only on the same machine. `warm_newest_page_read_ms` and
`older_page_read_ms` measure cache-backed paging after the cold initial read.
`mounted_widget_counts` remains bounded by the 60-entry history window (plus the
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
For ordinary workloads, `configured_size` selects the line count; for `long_line`, it selects the
line's character length. Every sample separately reports its actual `line_count` and
`longest_line_chars`.

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
