# Rust and Textual end-to-end TUI evidence

Captured on 2026-09-17 from base commit `b74c094` with the benchmark introduced by this change.
The Rust frontend was built with `cargo build --release -p wisp-tui`. The source-checkout Python CLI
used Python 3.12.2 on macOS arm64. Each renderer ran three times at 100x24 with alternating order and
a 64-word deterministic fake-provider prompt.

```bash
PYTHONPATH="$PWD/src" python -m benchmarks.rust_tui_e2e \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --prompt-words 64 \
  --output profiles/rust-tui-e2e.json
```

## Results

Times are milliseconds. Each cell is median / interpolated p95 across the three raw samples.
Launch-to-ready includes the forced one-column redraw used to capture a complete hydrated frame.
First response is observation of the complete fake-provider response prefix and opening sentinel.

| Renderer | Launch to ready | Prompt echo | First response | Final response | Settled composer | Total lifecycle |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Rust | 1059.6 / 1069.8 | 17.2 / 17.3 | 52.5 / 55.1 | 52.6 / 55.3 | 52.6 / 55.3 | 1302.0 / 1317.1 |
| Textual | 1245.0 / 1280.2 | 167.7 / 182.3 | 314.2 / 319.5 | 314.7 / 320.1 | 915.7 / 918.3 | 3332.5 / 3348.5 |

| Renderer | PTY output bytes, median / p95 | Exact markers | Clean exits | Terminal restored |
| --- | ---: | ---: | ---: | ---: |
| Rust | 11,216 / 11,221 | 3/3 | 3/3 | 3/3 |
| Textual | 32,062 / 36,430 | 3/3 | 3/3 | 3/3 |

In this matched workload, the Rust path reduced median launch-to-ready time by about 15%, first and
final visible-response time by about 83%, settled-composer time by about 94%, total measured lifecycle
time by about 61%, and PTY output bytes by about 65%. The raw samples retained in the JSON report
showed the same direction in every run, including when renderer order reversed.

## Interpretation and limits

This closes the main measurement gap left by the in-process transcript benchmark: it covers the real
source CLI, provider and session setup, Rust JSONL-RPC subprocess boundary and production delta
coalescer, event admission, and terminal writes. The Textual condition uses the same source CLI and
fake provider through the maintained Python frontend. The result supports the current Rust-default,
Python-runtime boundary; it does not identify another Python subsystem that should be ported.

PTY output is a visibility proxy and does not include terminal-emulator parsing, compositing, or
display latency. Three samples on one machine establish a local baseline, not a cross-platform timing
guarantee. The benchmark records `wait4` CPU and maximum RSS observations, but the Rust condition's
direct child is the Python supervisor of separate Rust and RPC processes while Textual runs in that
Python process. Those resource values are intentionally excluded from the comparison above and must
not be treated as whole-process-tree CPU or memory evidence.

The 64-word fixture keeps both response sentinels visible in a 24-row terminal. An exploratory
128-word run scrolled the leading Rust response sentinel outside the viewport before the full frame
was emitted, so the harness correctly timed out rather than inventing a first-visible timestamp.
Larger transcript and off-screen response costs remain covered by the in-process transcript benchmark.
