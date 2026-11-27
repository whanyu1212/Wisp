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

## HY-289: read-only startup preview

Five paired release-mode blocks on macOS arm64 with Python 3.12.12, against `0d9b1d6ea`
(the merged compaction replay fix). Each block ran fresh, 10k and 50k; variant and size
order alternated. Both checkouts used this harness and their own source CLI and release TUI:

```sh
uv run python -m benchmarks.rust_tui_hydration --rust-binary "$PWD/target/release/wisp-tui" \
  --no-profile --first-reply --runs 1 --history-messages 0,10000,50000
```

Medians in milliseconds, **base → preview**. “Visible” ends at the first complete PTY frame
showing the fixture’s last message; “composer” uses the existing ready/context redraw check.
Submit is the PTY write after startup context is measured, confirmed by the fake provider’s reply.
It is not the time of an internal backend acknowledgement.

| History | Newest visible | Composer | Submit | Submit → reply | Launch → reply |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fresh | — | 881 → 887 | 897 → 905 | 88 → 87 | 981 → 989 |
| 10k | 1300 → 907 | 1300 → 1382 | 1587 → 1675 | 538 → 545 | 2124 → 2221 |
| 50k | 3408 → 992 | 3408 → 3648 | 4986 → 5232 | 2833 → 2832 | 7819 → 8062 |

At 50k, the median newest frame was 105 ms later than the fresh composer. Four of the five
paired gaps were within 200 ms (67, 58, 104 and 152 ms); the remaining launch took 4169 ms
to show history. The fast route is best effort, not a latency guarantee. The preview is **not**
an interaction speedup: median accepted submit was 246 ms later at 50k. Sampled process-tree
CPU through reply and shutdown rose from 12.29 to 13.04 s; median peak tree RSS was 881 → 875 MiB.
All 30 runs received the expected reply, exited cleanly and restored the terminal.

A separate single pair included an inactive branch, turn events and a manual 40k-ID compaction
(62,504 entries). Newest frame: 3525 → 1042 ms; composer: 3525 → 3739 ms; submit: 4573 →
4780 ms; submit → reply: 1086 → 1085 ms. Both exited cleanly and restored the terminal.
These are local measurements on disposable fixtures, not displayed pixels or CI thresholds.

## HY-289: cached run snapshots and fast-path entry reads

Measured 2026-11-27 on macOS arm64 (Python 3.12.12, release `wisp-tui` built from `928fa24d6`)
after caching `SessionRunSnapshot` and `SessionTreeState` on `JsonlSession`, reusing
`read_run_snapshot()` across `updated_rpc_session_state` and `rpc_selected_session_state`,
and fast-pathing unchanged `(generation, FileSignature)` checks ahead of tail recovery.

Three unprofiled first-reply runs each on 0, 10k, and 50k using the same `--no-profile --first-reply`
command above (medians in ms):

| History | Newest visible | Composer | Submit | Submit → reply | Launch → reply | Tree CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fresh | — | 854 | 874 | 77 | 957 | 861 ms |
| 10k | 882 | 1305 | 1540 | 532 | 2072 | 2.83 s |
| 50k | 906 | 3020 | 4292 | 2535 | 6853 | 11.22 s |

Compared against the `#1158` preview medians in the previous section on the same machine class
(not paired in one run): at 50k, median composer fell **3648 → 3020 ms**, submit fell
**5232 → 4292 ms**, launch → reply fell **8062 → 6853 ms**, and process-tree CPU fell
**13.04 → 11.22 s**.

One profiled startup run (`--runs 1 --history-messages 10000,50000`) recorded `python.session_refresh`
dropping to **1.18 ms** at 10k (down from ~18–20.5 ms previously) and **2.10 ms** at 50k, with
`python.page_read` across all 251 pages totaling **420.6 ms** at 50k (`105.4 ms` across 51 pages at
10k). The remaining 50k ready/submit cost is dominated by per-frame Rust JSON schema validation
and event projection (`HY-287`). These remain local measurements on disposable fixtures, not
portable CI thresholds.
