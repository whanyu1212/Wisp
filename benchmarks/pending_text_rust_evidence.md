# Rust Pending-Text Kernel Evidence

This report records the September 16, 2026 prototype of Wisp's bounded managed-process output
retention kernel. It uses the `53d50a2` `main` baseline on Apple Silicon with macOS 27.0, CPython
3.12.2, and a release-mode Rust build. Each direct-kernel condition consumes 1 MiB in 8 KiB chunks
with the production limits of 50,000 encoded bytes and 2,000 logical lines. Values are means across
five iterations on the same machine and are comparison evidence, not portable performance limits.

## Commands

```bash
uv run python -m benchmarks.process_output \
  --sizes 1048576 --iterations 5 \
  --output profiles/process-output-python-pending-text.json
cargo run --release -p wisp-process-text \
  --example process_output_benchmark -- \
  --size 1048576 --chunk 8192 --iterations 5
uv run python -m benchmarks.managed_process_output \
  --sizes 1048576 --iterations 1 \
  --output profiles/managed-process-python.json
```

Raw benchmark output remains ignored under `profiles/`.

## Direct kernel comparison

The Rust benchmark uses the same workload byte patterns, chunk size, retention limits, final flush,
and accounting assertion as the Python benchmark. The Rust values divide total measured time by five
to produce comparable per-iteration wall times.

| Workload | Python ms | Rust ms | Speedup | Rust throughput MiB/s |
|---|---:|---:|---:|---:|
| ASCII lines | 212.66 | 12.04 | 17.7x | 83.08 |
| Unicode | 980.41 | 25.01 | 39.2x | 39.99 |
| Short lines | 1,317.32 | 58.25 | 22.6x | 17.17 |
| Long line | 260.69 | 12.18 | 21.4x | 82.10 |
| Mixed newlines | 566.70 | 36.33 | 15.6x | 27.53 |
| Invalid UTF-8 | 1,379.56 | 32.89 | 42.0x | 30.41 |

All conditions preserve `retained_source_bytes + dropped_bytes == input_bytes`. The largest gains
occur where Python performs the most per-character decoding and provenance bookkeeping. Even the
smallest observed gain is large enough to justify integrating this bounded kernel.

## End-to-end Python baseline

The managed-process benchmark runs an actual child through `ProcessSupervisor.start`, pipe readers,
repeated polls, terminal-state observation, and cleanup. It provides the baseline for the later PyO3
integration rather than a Rust comparison in this prototype PR.

| Workload | Wall ms | Throughput MiB/s | Polls | Maximum poll ms |
|---|---:|---:|---:|---:|
| ASCII lines | 584.45 | 1.71 | 20 | 102.25 |
| Unicode | 1,248.40 | 0.80 | 20 | 100.61 |
| Short lines | 1,585.84 | 0.63 | 20 | 101.84 |
| Long line | 571.97 | 1.75 | 20 | 100.33 |
| Mixed newlines | 888.47 | 1.13 | 20 | 102.26 |
| Invalid UTF-8 | 1,650.37 | 0.61 | 20 | 107.85 |

The direct-kernel cost is a substantial share of the measured lifecycle for Unicode, short-line,
mixed-newline, and invalid-byte output. Child startup, pipe scheduling, and the 100 ms polling window
remain visible in the faster ASCII and long-line conditions. The benchmark sums delivered retained
and dropped bytes across polls because each poll drains the incremental output snapshot.

## Semantic evidence

Python and Rust execute the same 27-case JSON fixture. It covers exact byte and line caps, every
logical line separator supported by Python `splitlines`, CRLF and Unicode scalars divided across
chunks, malformed and incomplete UTF-8, multibyte trimming, replacement-character accounting,
zero caps, repeated drains, and pending decoder state across drains. Each drain compares retained
text, dropped source bytes, retained source bytes, and the source-byte length represented by every
decoded character. Existing process-manager tests continue to cover stdout/stderr isolation,
cancellation, timeouts, and process cleanup around the Python implementation.

## Boundary decision

The native boundary should contain only incremental UTF-8 decoding, logical-line recognition,
bounded tail retention, and source-byte provenance. Python should continue to own process creation,
pipe tasks, polling, cancellation, timeouts, cleanup, policy, and `ProcessUpdate` construction.

This PR deliberately leaves `_PendingText` as the production implementation. The next PR should add
a PyO3 wrapper and wheel packaging, retain the Python fallback, and run the existing process-manager
suite against both implementations. It should then repeat the managed-process benchmark with each
backend selected in the same installed build. The direct speedup establishes the value of the
kernel; that end-to-end comparison will measure the cost of crossing the extension boundary and the
actual user-visible gain before Rust becomes the preferred backend.

This evidence covers a macOS arm64 development machine and synthetic stdout workloads. It does not
measure stderr contention, sustained concurrent children, cancellation during continuous output,
other platforms, extension-call overhead, or packaged-wheel behavior. Those remain integration and
release gates rather than reasons to widen the Rust ownership boundary.
