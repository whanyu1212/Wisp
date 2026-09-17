# Rust TUI saved-history startup

Measured 2026-09-17 on the same macOS arm64 host and Python 3.12.2 source checkout, using a
release `wisp-tui` binary and the new startup-only PTY harness. Baseline was commit `4f45174` plus
the benchmark's opt-in timing probes; the after condition also requested 200 messages for each
older page during full-history hydration. Both conditions retained every saved message. Each
condition used three fresh processes at 100×24, alternating zero and 10,000 saved messages
(5.77 MiB JSONL). The harness waited for the saved-history sentinel and ready composer, then
exited cleanly. Raw profiles are ignored under `profiles/`.

```bash
uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 \
  --output profiles/rust-tui-hydration.json
```

| Condition | 10k launch to ready, median / p95 | History RPC pages | 10k observed peak tree RSS | 10k observed tree CPU |
| --- | ---: | ---: | ---: | ---: |
| Baseline, 75-message older pages | 2612 / 2671 ms | 132 | 259.5 MiB | 1835 ms |
| 200-message hydration pages | 1778 / 1810 ms | 51 | 259.4 MiB | 1770 ms |

The median 10,000-message startup fell by 835 ms (32%) while the observed peak tree RSS stayed
within 0.1 MiB. Fresh-session medians were 1058 ms before and 1085 ms after. These are local
observations, not portable timing thresholds.

| Measured stage, 10k median total | Baseline | After |
| --- | ---: | ---: |
| Python page read and snapshot | 97.7 ms | 79.5 ms |
| Python report frame check and publication | 36.1 ms | 31.9 ms |
| Rust page projection | 12.5 ms | 11.3 ms |
| Rust source-page clone | 8.2 ms | 7.9 ms |
| Rust final chronological projection | 14.9 ms | 14.9 ms |
| Rust history installation | 2.6 ms | 2.4 ms |

The measured work above totaled far less than the 10,000-message startup time. Together with the
drop from 132 to 51 serial page requests, this supports RPC pagination overhead as the principal
cost in this fixture. It does not isolate one round trip's scheduling, JSON transport, or first
draw, and the timing probes themselves add small I/O outside the timed sections. The 20 ms
process sampler can miss short peaks. The ready marker is observed in PTY output, not displayed
pixels. The larger page size applies only while loading the complete saved transcript; normal
older/newer navigation retains 75-message requests. Oversize reports remain bounded by the live
RPC frame limit and are split at whole-message boundaries by the Python backend.
