# TUI Long-Session Scenario

Run the transcript-windowing scenario:

```bash
uv run python -m benchmarks.tui_long_session
uv run python -m benchmarks.tui_long_session --messages 5000 --page-size 100 --output before.json
```

The scenario creates a temporary JSONL session, reads its production pages, renders
them in the headless Textual TUI, streams an assistant response, and scrolls while a
managed CPU-active shell process runs. It reports JSON with page-read and rendering
durations, mounted-widget growth, stream durations, final scroll state, and process
cleanup state.

The CPU worker runs until it is cancelled immediately after the scroll measurement,
with a 60-second safety timeout. Streaming metrics therefore run without its load.

Compare absolute timings only on the same machine. `mounted_widget_counts` remains
bounded by the 300-entry history window (plus the session marker); compare page and
scroll timings separately. This intentionally executes a local shell command through
`ProcessSupervisor`; it uses a temporary directory and is cancelled before exit.
