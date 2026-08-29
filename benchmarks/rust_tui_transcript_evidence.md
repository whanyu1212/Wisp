# Rust TUI transcript performance evidence

Development measurement captured 2026-08-29 for issue #465. Raw JSON and sampling output remain
ignored under `profiles/`.

## Reproducibility

| Field | Value |
| --- | --- |
| Base tree | `origin/main` at `2394cae82ee2c792b1c2f74d3f19c6a2533366a9` |
| Local benchmark report commit | `8a96eeddc28acff9c3583003dddeadbe1ab8cc7b` |
| Worktree | Clean |
| Host | Mac14,15, Apple M2, arm64 |
| OS | macOS 26.5.2 / Darwin 25.5.0 |
| Rust | `rustc 1.85.0 (4d91de4e4 2025-02-17)` |
| Build | `--release`, 100x24 viewport, five rotated runs |
| Conditions | 1,000 / 10,000 / 100,000 transcript entries; identical 512-entry rich suffix |

Command:

```bash
cargo +1.85.0 run --release -p wisp-tui \
  --features transcript-benchmark --example transcript_benchmark -- \
  --entries 1000,10000,100000 --runs 5 \
  --output profiles/rust-tui-transcript.json
```

## Results

Values are medians across the five per-condition samples. Timing columns other than cold frame and
stream maximum are medians of each sample's p95 distribution.

| Entries | Cold frame ms | Warm p95 ms | Navigation p95 ms | Resize p95 ms | Stream p95 ms | Stream max ms | Stream CPU / 100 updates ms | Detail p95 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.188 | 0.091 | 0.101 | 0.102 | 0.130 | 0.293 | 10.958 | 0.081 |
| 10,000 | 0.181 | 0.090 | 0.101 | 0.101 | 0.130 | 0.191 | 11.053 | 0.087 |
| 100,000 | 0.200 | 0.092 | 0.101 | 0.102 | 0.130 | 0.156 | 11.023 | 0.084 |

The first syntax-highlight call took 15.869 ms in the standalone fresh-process run. This includes
Syntect initialization, parsing, and highlighting the first fence; it is reported separately from
warm stream frames and has no portable threshold.

100k / 1k ratios:

| Metric | Ratio |
| --- | ---: |
| Warm p95 | 1.018 |
| Navigation p95 | 1.002 |
| Resize p95 | 1.002 |
| Stream p95 | 0.996 |
| Stream process CPU | 1.006 |
| Detail p95 | 1.031 |

All ratios are below the 1.25 same-machine scaling gate. Warm, navigation, resize, stream, and detail
p95 values are below 16 ms; measured stream maxima are below 32 ms.

## Deterministic evidence

- `scaling_work_independent`: `true` for cold, warm, navigation, resize, and stream work.
- `all_correctness_checks_passed`: `true` for all 15 samples.
- Warm draws built zero rows and parsed/highlighted zero source bytes.
- Streaming retained exact source, stayed at the tail without unseen output, reused stable Markdown,
  performed no full reparse, and stayed within the configured parse/highlight work bounds.
- Visible rows remained viewport-bounded; the structured diff saturated the 400-row retention
  budget and rendered known old/new rows through its bounded cache.
- A default-suite regression independently compares 1,000 and 100,000-entry transcripts without
  using timing assertions.

## Profile

A two-second macOS `sample` capture observed 500 repeated, correctness-passing 100k-entry workloads.
The most frequent production draw samples were Ratatui line reflow/grapheme traversal and buffer
diffing. Wisp viewport samples remained in the bounded `visible_rows` → `last_anchor` / `row_at`
path; Markdown and Syntect samples were attributable to the deliberately changing visible stream.
Fixture-only samples included bounded `similar` diff construction and transcript population.

No transcript-length-growing renderer hotspot appeared, and the 100k timing/work evidence is flat.
No production hardening change is justified by this profile; adding a speculative cache or alternate
renderer would increase code without improving a measured limit.

## Limits

- `TestBackend` measures in-process Ratatui frame construction and diffing, not PTY writes, emulator
  paint, perceptual latency, or event-loop scheduling.
- Absolute timings apply only to this machine and build. CI asserts deterministic work and
  correctness, never these timings.
- History hydration, prepending, and exact historical detail fetch belong to #466 and are not
  exercised here.
- This table was captured from the clean benchmark implementation commit shown above. The ignored
  raw JSON is rerun once more from the final PR head after this evidence-only commit.
