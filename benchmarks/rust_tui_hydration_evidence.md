# Rust TUI saved-history startup

Measured 2026-09-17 on the same macOS arm64 host and Python 3.12.2 source checkout, using
release `wisp-tui` binaries and the startup-only PTY harness. The baseline binary differs from
the candidate only by requesting 75 instead of 200 messages for each older page during
full-history hydration. Both conditions retained every saved message and used the same Python
source. Each condition used three fresh processes at 100×24, alternating zero and 10,000 saved
messages (5.77 MiB JSONL). The harness waited for the saved-history sentinel and ready composer,
then exited cleanly. Raw samples are ignored under `profiles/`.

```bash
uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 --no-profile \
  --output profiles/rust-tui-hydration-wall.json

uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --output profiles/rust-tui-hydration-stages.json
```

The startup and resource figures below come from runs with profiling disabled, so per-page
profile file writes cannot affect the wall-clock comparison. Page counts and the stage table come
from separate profiled runs with identical fixtures and binaries.

| Condition | 10k launch to ready, median / p95 | History RPC pages | 10k observed peak tree RSS | 10k observed tree CPU |
| --- | ---: | ---: | ---: | ---: |
| Baseline, 75-message older pages | 2663 / 2669 ms | 132 | 259.6 MiB | 1800 ms |
| 200-message hydration pages | 1773 / 1783 ms | 51 | 259.2 MiB | 1740 ms |

The median 10,000-message startup fell by 890 ms (33%) while observed peak tree RSS stayed
within 0.4 MiB. Fresh-session medians were 1110 ms before and 1089 ms after. These are local
observations, not portable timing thresholds.

| Measured stage, 10k median total | Baseline | After |
| --- | ---: | ---: |
| Python page read and snapshot | 97.6 ms | 78.0 ms |
| Python selected-session refresh and replay | 20.9 ms | 20.5 ms |
| Python report frame check and publication | 36.2 ms | 32.0 ms |
| Rust page projection | 12.2 ms | 11.2 ms |
| Rust source-page clone | 8.1 ms | 7.7 ms |
| Rust final chronological projection | 14.8 ms | 14.5 ms |
| Rust history installation | 2.7 ms | 2.4 ms |

The measured work above totaled far less than the 10,000-message startup time. Together with the
drop from 132 to 51 serial page requests, this supports RPC pagination overhead as the principal
cost in this fixture. It does not isolate one round trip's scheduling, JSON transport, or first
draw. The profiled runs include synchronous per-page file writes and are used only for stage
attribution; their launch-to-ready times are excluded from the performance comparison. The 20 ms
process sampler can miss short peaks. The ready marker is observed in PTY output, not displayed
pixels. The larger page size applies only while loading the complete saved transcript; normal
older/newer navigation retains 75-message requests. Oversize reports remain bounded by the live
RPC frame limit and are split at whole-message boundaries by the Python backend.
