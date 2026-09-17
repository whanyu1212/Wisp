# Rust TUI saved-transcript memory

Measured 2026-09-17 from the merged #605 tree (`f568de6`) on macOS arm64 with
Python 3.12.2 and a release Rust TUI. The saved-history fixture contains complete
four-message turns and is 5.77 MiB for 10,000 messages or 28.9 MiB for 50,000.
The PTY harness waits for the saved-history sentinel and ready composer, samples
each live process, leaves the TUI without input for one second, samples again, then checks
clean exit and terminal restoration. The opt-in stage profiler was disabled.
Raw per-run JSON is under ignored `profiles/rust-tui-memory-checkpoints.json` and
`profiles/rust-tui-memory-50k.json`.

```bash
uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 3 --history-messages 0,10000 --no-profile \
  --ready-hold-seconds 1 --output profiles/rust-tui-memory-checkpoints.json

uv run python -m benchmarks.rust_tui_hydration \
  --rust-binary "$PWD/target/release/wisp-tui" \
  --runs 2 --history-messages 50000 --no-profile \
  --ready-hold-seconds 1 --timeout-seconds 180 \
  --output profiles/rust-tui-memory-50k.json
```

The local environment denied process enumeration in the default sandbox, so
these runs used the existing `.venv/bin/python` and local process-inspection
access. No provider or network request was involved.

| Saved messages | Runs | Launch to ready, median | Launcher RSS at settled | Python RPC RSS at settled | Rust TUI RSS at settled | Process-tree peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 3 | 1.09 s | 77.1 MiB | 62.6 MiB | 20.7 MiB | 160.5 MiB |
| 10,000 | 3 | 1.79 s | 77.3 MiB | 107.5 MiB | 74.4 MiB | 259.1 MiB |
| 50,000 | 2 | 5.00 s | 77.4 MiB | 269.8 MiB | 280.5 MiB | 636.4 MiB |

At 10,000 messages, settled RSS grew by about 54 MiB in Rust and 45 MiB in
the Python RPC backend relative to a fresh session. At 50,000 messages, the
corresponding growth was about 260 and 207 MiB. The launcher stayed near 77
MiB in every condition. The backend's RSS continued rising after the ready frame:
its 10,000-message RSS was 94.0 MiB at ready and 107.5 MiB one second later;
at 50,000 it rose from about 218 to 270 MiB. Settled RSS is therefore more
representative of retained memory than the first ready-frame snapshot.

The matched Rust/Textual interaction benchmark was rerun after #605 with three
runs per renderer and 0/10,000-message conditions. Both renderers used the
same fake provider, 400-word paced response, and PTY workload. Raw results are
in ignored `profiles/rust-tui-interaction-post-hydration.json`.

| Renderer | Saved messages | Launch to ready, median | Process-tree peak RSS, median | Observed tree CPU, median | Streaming input visible, median / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Rust | 0 | 1.08 s | 161.3 MiB | 1,337 ms | 18.0 / 21.6 ms |
| Textual | 0 | 1.30 s | 157.3 MiB | 2,179 ms | 5.6 / 19.7 ms |
| Rust | 10,000 | 1.78 s | 282.1 MiB | 3,273 ms | 16.2 / 21.0 ms |
| Textual | 10,000 | 1.58 s | 229.0 MiB | 4,127 ms | 9.1 / 22.5 ms |

Rust is faster to the ready frame in a fresh session and uses less observed
process-tree CPU, but it is still about 0.20 s slower and has a 53 MiB higher
observed peak RSS than Textual for the 10,000-message interaction workload.
Both keep streaming input responsive on this machine. The CPU and RSS figures
include the whole process tree; the Rust-only RSS above comes from the separate
idle hydration run, not the interaction run.

These results attribute resident memory to processes, not to Rust allocation
sites or redundant transcript copies. Python and Rust both need their own
representation under the current RPC architecture; this evidence does not
show that either can be removed without changing behavior. RSS includes
allocator-retained pages and libraries. The 20 ms process-tree sampler can
miss short peaks, and the ready frame is PTY output rather than displayed
pixels. Three or two local runs are a directional baseline, not a portable
performance guarantee. The next optimization needs allocation-site evidence
before changing transcript ownership or storage.
