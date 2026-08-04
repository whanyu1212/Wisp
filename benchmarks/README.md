# Benchmarks

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
accounting while retaining only the configured output tail.
