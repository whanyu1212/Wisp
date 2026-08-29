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
final scroll-state, and process-cleanup measurements. The wheel metric dispatches through Textual's
screen event path and waits for the resulting viewport and display update without using
`Pilot.pause()`, whose test-only full-widget drain would inflate long-transcript timings.
`first_wheel_up_ms` preserves the cold response sample, while `wheel_up_ms` records up to 20
consecutive production-style responses so periodic compositor-map rebuilds remain visible.
`wheel_up_complete_arrangement_count` counts whole-tree compositor arrangements across those
responses. Textual arranges only visible widgets on its scroll fast path, so a non-zero count means
scrolling re-lays out every mounted widget — latency that grows with session length. Unlike the
timings it is machine-independent, so it is asserted directly. After
those measurements it runs a dedicated
paged-history prepend fixture, reporting whether the probe ran plus suppressed and escaped
display-update counts. Row-coverage fields compare persisted messages
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
PageUp/PageDown, resize, 100 growing Markdown updates with closed Rust fences, and paging through a
row-budget-saturating structured diff. JSON contains only timings, bounded work counters, environment
metadata, and correctness flags—never transcript text, paths, tool payloads, identifiers, or terminal
cells.

`stream_process_cpu_ms` uses `getrusage` across the fixed update-and-draw region. `stream_stall_ms`
is synchronous mutation-plus-`Terminal::draw` wall time; it is not event-loop, PTY, terminal-write,
or perceptual latency. Compare absolute timings only from release builds on the same machine.
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

## TUI Streaming Hotpaths

Measure the real Rich Markdown stream path at the production first-page size, the production
mounted-history window, and a larger retained-history pressure case:

```bash
mkdir -p profiles
uv run python -m benchmarks.tui_stream_hotpaths --runs 5 \
  --output profiles/tui-stream-hotpaths.json
```

The default matrix retains exactly 60, 75, and 300 converted history entries. The production
window bounds mounted history while process observations may collapse several persisted rows into
one card. Larger conditions retain additional entries to isolate retained-history pressure from
mounted-widget growth. Each sample starts the same command lifecycle that keeps Wisp's 80 ms
working indicator at the transcript tail, mounts three concurrent pending tool cards, and streams
100 chunks at 20 ms intervals. Use `--pending-tool-cards 0` to compare with older captures that did
not include timer pressure. The benchmark rotates condition order between runs and reports
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

The private Textual method patches are installed and restored inside the benchmark process. The
production TUI accepts an optional synchronous diagnostics sink, but the normal app supplies none,
retains no samples, and performs no diagnostic I/O. Samples contain counts, durations, sizes, and
success flags only—never Markdown source, prompts, tool payloads, paths, credentials, or session
identifiers. Sink failures are isolated from rendering.

In addition to paced wall time, each sample reports `stream_cpu_ms`, which uses
process CPU time to exclude intentional sleeps without pretending CPU cost is a latency metric.
`layout_requests` attributes `layout=True` refresh requests by concrete widget class, while
`layout_passes_per_stream_update` shows whether paced writes trigger additional settlement layouts.
`content_height_calls` attributes Textual height measurements by concrete widget class.
`markdown_source_rebuild_count` counts successful source-to-renderable builds separately from Rich
visual renders. `markdown_source_chars_processed` sums the raw source slices parsed and
sanitized by those builds. Stable completed Markdown blocks are represented by retained tokens, so
this total exposes whether the mutable tail remains bounded instead of charging the complete growing
document on every drain; compare it only with identical streamed content.
`markdown_renders` splits visual renders between the mutable streaming widget (`active`) and
`StreamMessage` widgets mounted before streaming (`settled`). `markdown_drains` measures the
coalesced source-to-renderable write already used by production pacing. `display_updates` counts
attempted `LayoutUpdate`, `ChopsUpdate`, and other display-boundary calls. Chop-span totals separate
input, terminal-emitted, and exact duplicate spans suppressed by `_DisplayedFrame`.
`display_frame_fail_open_count` records partial updates safely passed through without exact-cell
suppression because the cache or update shape was unavailable, cursor movement disabled comparison,
or control segments could have side effects. History-prepend suppression and escaped-update counts
distinguish hidden intermediate compositor work from a paint attempted while prepend state was still
unsettled. A zero
settled Markdown count is valid when Textual reuses prior measurements during the measured phase.

The same harness now also reports a privacy-safe terminal-write model for each streamed frame.
`terminal_write_frames`, `terminal_payload_bytes`, `terminal_write_count`, and
`terminal_writes_per_displayed_frame` describe how many driver writes one logical `_display` call
would produce after `_DisplayedFrame` filtering. `posix_write_count` is the Unix model (one write
per payload); `windows_chunk_count` is derived from Textual's 8,192-character Windows split. Headless
`run_test()` never enables CSI 2026, so `sync_available_frame_count` and
`observed_driver_frame_count` stay at zero unless a live driver is wrapped. Its terminal payload
model is rendered only after stream CPU, profiler, wall-clock, and heartbeat timing stops, so this
measurement-only pass does not inflate the production hotpath distributions. Those fields are
attribution evidence for #443, not a reason to emit synchronized-output sequences from Wisp.
Compare them only on the same machine and treat them as before/after evidence for a later
prototype, not as portable CI limits.

## TUI Live Terminal Frames

Exercise the same Wisp display boundary through a real terminal driver instead of the headless
write model:

```bash
uv run python -m benchmarks.tui_terminal_frames --mode paired --runs 3 \
  --output profiles/tui-terminal-frames-paired.json

uv run python -m benchmarks.tui_terminal_frames --mode paired --runs 3 \
  --disable-synchronized-output \
  --output profiles/tui-terminal-frames-paired-disabled.json
```

Paired mode is POSIX-only. It launches each workload under a fixed-size pseudo-terminal, waits for
Textual's real `CSI ? 2026 $ p` capability query, and rotates two modes between runs. The unsupported
mode leaves the query unanswered; the supported mode returns the standard
`CSI ? 2026 ; 1 $ y` report. It never assigns Textual's private capability flag directly. A control
pipe starts fixture setup only after negotiation, while child results use a separate file so terminal
bytes never enter report JSON.

Use native mode in each representative emulator to confirm capability detection and observe visual
flicker directly:

```bash
uv run python -m benchmarks.tui_terminal_frames --mode native --runs 3 \
  --messages 20 --retained-history 10 --stream-chunks 12 \
  --stream-interval-seconds 0.03 --width 100 --height 24 \
  --pending-tool-cards 2 \
  --emulator-label "Apple Terminal 2.15 / macOS 26.5.2 / direct" \
  --output profiles/tui-terminal-frames-apple-terminal.json
```

Native mode requires an interactive terminal and lets that emulator answer Textual's query. It waits
until support is detected or `--negotiation-timeout-seconds` expires before collecting frames. Reports
contain only environment metadata, display/cache counts, payload sizes, write/flush counts,
frame-level synchronization balance, coarse out-of-band classes, and process-wide synchronization
counts from startup through restored shutdown. The observer remains attached until Textual restores
the terminal. Paired mode independently counts the raw PTY stream and rejects a sample if raw and
diagnostic begin/end, balance, or maximum-depth results disagree. The PTY reader keeps only a short
control-sequence tail and discards terminal payload bytes.

Before each native run, resize the usable terminal or multiplexer pane to exactly 100 columns by 24
rows. Native mode validates the real TTY dimensions against `--width` and `--height` and aborts rather
than recording a report with a misleading configured viewport. Keep the explicit workload arguments
above identical in every environment. Include the emulator version, host OS, and `direct` or the
multiplexer name and version in `--emulator-label`; for tmux, include both tmux and its host emulator.

Retain one row per sample when transcribing evidence. Every sample must complete its source and include
at least one full layout and one partial update. On a supporting terminal, every observed driver frame
must have one ordered synchronization pair, no unbalanced frame, and no payload write outside
synchronization. Process-wide begin/end totals must balance with a maximum depth of one. On an
unsupported terminal, capability detection and synchronization-pair counts must remain zero. A mixed
capability result across the three runs is a failure, not an aggregate pass. Repeat native mode with
`--disable-synchronized-output`; every frame and process synchronization count must then remain zero,
even when the emulator reports support.

Observe the cold full-layout frame and subsequent streaming updates separately from those automated
counts. Record whether any intermediate frame or residual whole-screen flicker was visible, then verify
that the alternate screen, cursor, keyboard input, and shell prompt are restored after the command.
Raw reports under `profiles/` remain machine-local and ignored; commit only privacy-safe numeric tables
and factual manual observations.

Counts demonstrate whether intermediate writes are exposed; they do not by themselves prove a
perceptual flicker improvement. Compare default and disabled native runs on the same machine, record
the emulator version and multiplexer, and keep manual observations separate from automated framing
evidence. Windows does not use the POSIX paired harness; if Windows Terminal is unavailable, record
that limitation and make no Windows compatibility claim.

See `benchmarks/tui_terminal_frames_evidence.md` for the current automated table, manual emulator
matrix, and the decision gate for #443.

## TUI Interactive Input Latency

Measure the interval from Textual receiving an interactive event through its handler, queued
framework work, and the first subsequently emitted terminal frame, under both idle and sustained
assistant-stream conditions:

```bash
uv run python -m benchmarks.tui_input_latency --runs 5 \
  --output profiles/tui-input-latency.json
```

The scenario covers typing, cursor movement, PageUp and wheel navigation, active-run submission,
approval selection, and cancellation. It reports separate `handler`, `queued`, `display`, and `total`
distributions for each input category and condition. Every measured gesture waits for its own
terminal-emitted diagnostic before the next gesture is dispatched, so opposite cursor movements
cannot cancel before either becomes visible. PageUp and wheel gestures each begin at the transcript
tail and are independently returned to the tail after measurement. The benchmark alternates
idle/streaming order between runs to reduce order bias. It uses the production
`TextualTui.on_event`, Markdown stream controller, transcript, decision panel, and display boundary;
it does not use a synthetic latency threshold. `stream_chunks` is the minimum workload for each
streaming run; the producer continues at the configured interval until the input exercise has also
finished.

`gesture_repetitions` defaults to 5 and is configurable with `--gesture-repetitions`. With the
default five runs, each idle and streaming condition therefore contains 50 cursor samples and 25
samples each for PageUp navigation and wheel navigation, keeping nearest-rank p95 values from being
the single worst observation. The benchmark aborts if any scripted category produces a missing or
duplicate diagnostic, so invalid event accounting cannot silently enter an evidence report.

Each streaming run also reports total streaming-phase and final-flush time, produced chunks,
expected and rendered source lengths, Markdown writes, exact source completeness, whether every
PageUp and wheel gesture remained parked until its explicit return to the tail, and the final
follow/tail state. These fields keep responsiveness evidence tied to stream progress and correctness
without turning machine-specific timings into CI limits.

For before/after evidence, run an identical benchmark-only harness in temporary worktrees rooted at
the two runtime commits. If the harness was added after the baseline, apply the same benchmark-only
commit to both worktrees before measuring. Capture both the default 20 ms stream interval and a 5 ms
stress interval, then run the independent fixed-workload hotpath benchmark:

```bash
uv run python -m benchmarks.tui_input_latency --runs 5 \
  --stream-interval-seconds 0.02 --output profiles/tui-input-default.json
uv run python -m benchmarks.tui_input_latency --runs 5 \
  --stream-interval-seconds 0.005 --output profiles/tui-input-stress.json
uv run python -m benchmarks.tui_stream_hotpaths --runs 5 \
  --output profiles/tui-stream-hotpaths.json
```

Report the exact runtime and harness commits, environment metadata, individual samples, p50/p95
latency distributions, and correctness flags. Treat a result as evidence only when the known
active-stream latency gap improves consistently without a systematic completion regression.

Input diagnostics are opt-in and privacy-safe: samples contain only a coarse event category,
durations, and display-update kind. They never retain key values, pasted text, prompt content,
coordinates, paths, tool payloads, or session identifiers. Production diagnostics permit multiple
input events to settle against the same first subsequent frame, which reflects what the terminal can
actually make visible; this benchmark deliberately serializes its measured gestures so every sample
has a distinct settlement boundary. Machine-specific values are evidence for same-environment
before/after comparisons, not portable CI limits.

Treat JSON and profile files as machine-local evidence. Compare timings only on the same machine,
Python/Textual versions, viewport, and arguments, and report individual samples alongside medians
rather than promoting one run to a portable threshold. An attempted update is a call into the app's
display boundary; an emitted update still contains terminal spans after filtering. Counts provide
attribution evidence, not portable performance thresholds.

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
