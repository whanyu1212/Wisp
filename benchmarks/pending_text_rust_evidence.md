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

The prototype PR deliberately left `_PendingText` as the production implementation. The follow-up
integration described below adds the PyO3 wrapper and wheel packaging, retains the Python fallback,
runs the process-manager suite against both implementations, and repeats the managed-process
benchmark through an installed native wheel before selecting Rust automatically when available.

The direct-kernel evidence covers a macOS arm64 development machine and synthetic stdout workloads.
It does not measure stderr contention, sustained concurrent children, cancellation during continuous
output, or other platforms. Those remain release evidence limits rather than reasons to widen the
Rust ownership boundary.

## Installed extension integration

The follow-up integration used the packaged `cp312-abi3` extension from an installed macOS arm64
candidate wheel and repeated the complete managed-process benchmark with `--iterations 3`. Both
backends ran through the same Python supervisor, child process, 8 KiB pipe reads, polling, source-byte
accounting, and cleanup. These measurements were recorded on September 16, 2026 on the same macOS
arm64 machine with CPython 3.12.2.

| Workload | Python wall ms | Native wall ms | Wall speedup | Python CPU ms | Native CPU ms | CPU reduction |
|---|---:|---:|---:|---:|---:|---:|
| ASCII lines | 408.76 | 411.76 | 0.99x | 249.16 | 29.10 | 8.6x |
| Unicode | 1,177.90 | 403.60 | 2.92x | 999.68 | 38.66 | 25.9x |
| Short lines | 1,484.66 | 406.95 | 3.65x | 1,323.06 | 69.51 | 19.0x |
| Long line | 400.39 | 411.58 | 0.97x | 235.03 | 27.02 | 8.7x |
| Mixed newlines | 738.61 | 413.00 | 1.79x | 579.21 | 51.05 | 11.3x |
| Invalid UTF-8 | 1,558.22 | 405.14 | 3.85x | 1,396.77 | 46.16 | 30.3x |

The native backend removes most retention CPU cost. Wall time becomes approximately 400 ms across
the matrix because child startup, pipe scheduling, and the benchmark's 100 ms polling window become
the remaining floor. ASCII and long-line wall time therefore stay flat even though their CPU cost
falls sharply. The Unicode, short-line, mixed-newline, and invalid-byte conditions improve end to
end because retention previously exceeded that floor.

The native wheel now selects this backend automatically. Pure wheels and unsupported platforms keep
the Python implementation. Native-wheel CI runs the shared conformance corpus and complete process
manager suite against the installed extension, then verifies that replacing it with the pure wheel
removes `wisp._native` and restores the Python fallback.
