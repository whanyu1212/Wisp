# TUI Long-Session Baseline

Run the current #185 baseline before and after transcript windowing:

```bash
uv run python -m benchmarks.tui_long_session
uv run python -m benchmarks.tui_long_session --messages 5000 --page-size 100 --output before.json
```

The scenario creates a temporary JSONL session, reads its production pages, renders
them in the headless Textual TUI, streams an assistant response, and scrolls while a
managed CPU-active shell process runs. It reports JSON with page-read and rendering
durations, mounted-widget growth, stream durations, final scroll state, and process
cleanup state.

The CPU worker runs for at least one second and is then cancelled after the
scroll measurement, so short smoke configurations still measure while it is active.

Compare absolute timings only on the same machine. The useful PR2 comparison is the
shape of `mounted_widget_counts` as older pages load, plus the before/after page and
scroll timings. This intentionally executes a local shell command through
`ProcessSupervisor`; it uses a temporary directory and is cancelled before exit.
