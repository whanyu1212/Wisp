# Rust and Textual interaction evidence

Captured on 2026-09-17 from base commit `7223eaf` with the benchmark in this change. The Rust
frontend was built with `cargo build --release -p wisp-tui`; the source-checkout CLI used Python
3.12.2 on macOS arm64. Each renderer ran three times at 100×24 in fresh sessions and with 10,000
saved messages (5.77 MiB of JSONL). Renderer and history order alternated. Each session sent five
unique typing probes while idle and five during a paced, 400-word fake response, then PageUp and
PageDown while streaming. The fake provider waited 20 ms per word and appended a response-only
completion sentinel. Each saved-history sample observed the history sentinel before probing. All
samples observed the complete reply, clean exit, terminal restoration, and nonzero process-tree
resource samples.

```bash
uv run python -m benchmarks.rust_tui_interaction \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --prompt-words 400 --stream-interval-ms 20 --input-probes 5 \
  --timeout-seconds 180 --output profiles/rust-tui-interaction.json
```

## Results

Times are milliseconds; cells are median / interpolated p95. Input distributions pool 15 probes
per renderer/history condition. Startup and resource distributions use three independent sessions.
The input metric ends when the unique 12-character random marker suffix appears contiguously in PTY
output, before terminal-emulator display. A renderer can split earlier marker characters across
differential frames; the benchmark does not reconstruct terminal cells.

| Renderer | Saved messages | Idle input | Input during stream | Launch to ready | Observed tree CPU | Observed peak tree RSS, MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Rust | 0 | 17.7 / 21.6 | 15.9 / 18.5 | 1078.3 / 1239.8 | 1371 / 1385 | 161.6 / 163.4 |
| Textual | 0 | 5.1 / 23.4 | 6.0 / 28.5 | 1266.9 / 1292.1 | 3857 / 3882 | 169.1 / 173.2 |
| Rust | 10,000 | 17.6 / 18.9 | 8.7 / 18.9 | 2695.3 / 2779.8 | 3356 / 3358 | 284.0 / 287.1 |
| Textual | 10,000 | 5.3 / 48.3 | 5.7 / 25.4 | 1623.5 / 1674.4 | 5765 / 5806 | 241.1 / 241.5 |

The complete response was observed after a median 8.60 s in fresh Rust, 10.29 s in fresh Textual,
9.13 s in saved-history Rust, and 10.85 s in saved-history Textual. These exceed the nominal
8-second provider pacing, consistent with the response-only completion check. Median first-response
times were 176 ms, 1830 ms, 712 ms, and 2396 ms respectively in the same order. All 12 sessions
completed; the smallest process-tree sample count was 319.

PageUp/PageDown observations are recorded in the raw JSON but excluded from the performance verdict.
The next PTY write after a navigation key occurred within 29 ms in these runs. A concurrent stream
update can cause that write, so the metric does not establish that navigation changed the viewport.

## Interpretation and limits

Both renderers kept typing responsive in the long-history condition: every streaming input marker
appeared in PTY output within 32 ms. This does not support moving more of the Python agent runtime
to Rust for interactive latency. Rust used less observed process-tree CPU in both conditions (about
64% less fresh and 42% less with history), but 10,000-message launch-to-ready was slower (2.70 s
versus 1.62 s) and observed peak tree RSS was higher (284 versus 241 MiB). The next useful
optimization investigation is the Rust `/resume` handoff and transcript hydration path, including
copies retained across the Python backend and Rust frontend, before considering more Python-to-Rust
ports. These measurements locate a symptom; they do not identify a specific allocation or stage.

CPU is the sum of the highest sampled counters for each process seen under the source CLI. Peak RSS
is the highest simultaneous sum across sampled descendants. The 20 ms sampler can miss short-lived
processes or peaks between observations. PTY output is a paint proxy, not perceived latency, and
three runs on one macOS arm64 machine are a local baseline rather than a cross-platform guarantee.
Compare new results only with the same fixture, build mode, and machine, and retain raw samples under
the ignored `profiles/` directory.
