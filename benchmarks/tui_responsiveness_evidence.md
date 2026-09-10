# Python TUI responsiveness evidence

These measurements cover the Python/Textual frontend, not the Rust rewrite.
Local environment: macOS ARM64, Python 3.12.2, Textual 8.2.8. Times are indicative,
not portable CI thresholds. Reproduction commands below run from the repository root.

## Correctness gates

The regression tests cover:

- arrival-order buffering when following changes before a scheduled stream drain;
- incremental/full Markdown equivalence at every chunk, including Unicode separators,
  carriage returns, controls, reference links, lists, and fences;
- immediate empty-query results when reopening an `@` mention;
- unchanged option identities on caret-only picker updates, lazy hidden-tree rendering,
  snapshot replacement, and resize invalidation;
- streamed Markdown selection, cache reuse by width, and invalidation on theme changes,
  replacement, parser fallback, and settlement.

## File picker

The synthetic picker experiment used a mounted `FileSuggest` at 100×24 with
100, 1,000, and 10,000 flat root-level files named `file_00000.py`, etc.
After installing each snapshot and displaying `@file_0`, five identical
`show_for("@file_0", 7)` calls were timed synchronously. This excludes later layout and paint.
The baseline implementation was loaded from `origin/main` at `8650f1a`; the changed
implementation was `999c0ff`. Both ran in the same process with separate app instances.

| Paths | Before median | After median | Hidden tree options before / after |
| --- | ---: | ---: | ---: |
| 100 | 1.029 ms | 0.004 ms | 100 / 0 |
| 1,000 | 2.320 ms | 0.010 ms | 1,000 / 0 |
| 10,000 | 19.001 ms | 0.013 ms | 10,000 / 0 |

The deterministic regression asserts option reuse and zero hidden tree options,
not these timings. Opening the tree still constructs the requested rows; this change
removes that cost from fuzzy-mode typing rather than claiming the tree is virtualized.

## Stable Markdown paragraphs

```bash
uv run python -m benchmarks.tui_markdown_blocks --runs 3 --updates 100
```

The paired microbenchmark rotates cached/uncached order between runs and reports
preparation separately from visual height measurement. It retains Rich's own
paragraph construction and inter-block spacing; the cache avoids repeating wrapping
and segment generation for stable paragraphs.

| Workload | Uncached measurement median | Cached measurement median | Paragraph wraps before / after |
| --- | ---: | ---: | ---: |
| 100 prose updates | 823.9 ms | 366.9 ms | 5,050 / 199 |
| 100 open-fence updates | 602.1 ms | 610.9 ms | 0 / 0 |

Prose preparation was 53.9 / 52.7 ms. Final height was identical in both modes
(399 rows for prose, 99 for the open fence), and source completeness held in every sample.
An open code fence remains mutable; the small timing difference there is not an improvement.

## Paced streaming

```bash
uv run python -m benchmarks.tui_stream_hotpaths \
  --messages 1000 --retained-history 60 --runs 3 --stream-chunks 100
```

Before was `17dbfa7`, with stream-order and source-map fixes but without paragraph
render caching. After includes the paragraph cache. These are sequential local
before/after captures, not an interleaved controlled experiment.

| Metric | Before median | After median |
| --- | ---: | ---: |
| Stream process CPU | 597.6 ms | 551.3 ms |
| Layout total | 464.3 ms | 417.7 ms |
| Event-loop delay p95 | 14.3 ms | 12.6 ms |
| Stream elapsed | 2,303.0 ms | 2,252.1 ms |
| Source characters processed | 8,956 | 8,956 |

Timing-dependent frame counts differed (30 versus 29), so this is supporting evidence,
not an exact fixed-work comparison. The synchronous microbenchmark above isolates the
wrapping reduction. Neither benchmark establishes perceived typing latency.

## Input and terminal-driver smoke checks

```bash
uv run python -m benchmarks.tui_input_latency --runs 1
uv run python -m benchmarks.tui_terminal_frames --mode paired --runs 1 \
  --messages 100 --retained-history 60 --stream-chunks 40
```

The input benchmark completed all idle/streaming action categories. This is a smoke
check, not a before/after latency claim. The PTY-backed driver check preserved complete
source in supported and unsupported synchronized-output modes, with no out-of-band
writes or unbalanced synchronization frames.

No native terminal-emulator visual assessment was performed. The PTY check exercises
the real driver and negotiation, not human-perceived flicker.

## Remaining limits

- Rich still traverses the combined token stream, and Textual still measures/crops the
  whole message. This is not full stable-strip reuse or visible-row virtualization.
- A single growing paragraph, list, or code fence can remain expensive.
- Uncertain source maps use full parsing; correctness takes priority over reuse.
- Pacing still uses preparation cost, not total frame cost. This PR does not change
  cadence based on the small initial timing samples.
- Cached segments retain one width per stable token and are released on settlement.
  This trades bounded per-message memory for less repeated wrapping.
