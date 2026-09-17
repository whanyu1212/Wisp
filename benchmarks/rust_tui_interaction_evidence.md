# Rust and Textual interaction evidence

Captured on 2026-09-17 from base commit `7223eaf` with the benchmark in this change. The Rust
frontend was built with `cargo build --release -p wisp-tui`; the source-checkout CLI used Python
3.12.2 on macOS arm64. Each renderer ran three times at 100×24 in fresh sessions and with 10,000
saved messages (5.77 MiB of JSONL). Renderer and history order alternated. Each session sent five
unique typing probes while idle and five during a paced, 400-word fake response, then PageUp and
PageDown while streaming. A short prompt avoided large PTY paste limits; the fake provider generated
the response words itself, waited 20 ms per word, and emitted response-only start and completion
sentinels. Each saved-history sample observed the history sentinel before probing. All
samples observed the complete reply, clean exit, terminal restoration, and nonzero process-tree
resource samples.

```bash
uv run python -m benchmarks.rust_tui_interaction \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --response-words 400 --stream-interval-ms 20 --input-probes 5 \
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
| Rust | 0 | 17.6 / 19.9 | 11.6 / 19.9 | 1103.1 / 1103.7 | 1366 / 1366 | 164.6 / 166.6 |
| Textual | 0 | 5.3 / 13.3 | 6.1 / 21.4 | 1279.0 / 1286.5 | 2190 / 2210 | 157.0 / 157.3 |
| Rust | 10,000 | 17.7 / 23.1 | 16.2 / 20.7 | 2641.1 / 2676.0 | 3332 / 3765 | 285.5 / 286.8 |
| Textual | 10,000 | 5.6 / 42.5 | 7.2 / 16.4 | 1589.7 / 1761.0 | 4088 / 4166 | 229.3 / 230.3 |

The complete response was observed after a median 8.44 s in fresh Rust, 8.48 s in fresh Textual,
8.98 s in saved-history Rust, and 9.07 s in saved-history Textual. These exceed the nominal
8-second provider pacing, consistent with the response-only completion check. Median first-response
times were 73 ms, 85 ms, 611 ms, and 674 ms respectively in the same order. All 12 sessions
completed; the smallest process-tree sample count was 336.

PageUp/PageDown observations are recorded in the raw JSON but excluded from the performance verdict.
The next PTY write after a navigation key ranged from 0.2 to 748.5 ms in these runs. A concurrent
stream update can cause that write, so the metric does not establish that navigation changed the
viewport or isolate navigation work.

## Interpretation and limits

Both renderers kept typing responsive in the long-history condition: every streaming input marker
appeared in PTY output within 24 ms. This does not support moving more of the Python agent runtime
to Rust for interactive latency. Rust used less observed process-tree CPU in both conditions (about
38% less fresh and 18% less with history), but 10,000-message launch-to-ready was slower (2.64 s
versus 1.59 s) and observed peak tree RSS was higher (286 versus 229 MiB). The next useful
optimization investigation is the Rust `/resume` handoff and transcript hydration path, including
copies retained across the Python backend and Rust frontend, before considering more Python-to-Rust
ports. These measurements locate a symptom; they do not identify a specific allocation or stage.

CPU is the sum of the highest sampled counters for each process seen under the source CLI. Peak RSS
is the highest simultaneous sum across sampled descendants. The 20 ms sampler can miss short-lived
processes or peaks between observations. PTY output is a paint proxy, not perceived latency, and
three runs on one macOS arm64 machine are a local baseline rather than a cross-platform guarantee.
Compare new results only with the same fixture, build mode, and machine, and retain raw samples under
the ignored `profiles/` directory.
