# Protocol fuzzing

This harness advances #468 with coverage-guided protocol decoding and stable
framing properties. It does not cover terminal rendering, Unicode layout, secret
cleanup, lifecycle races, dependency auditing, or whole-session memory retention.
The production workspace continues to use Rust 1.85; this nested workspace has
its own lockfile and uses a pinned nightly for AddressSanitizer instrumentation.

## Targets and corpus

`client_wire` tries command and handshake-request decoding; `server_wire` tries
event and handshake-response decoding. Both use the same raw-byte oracle as the
stable protocol tests. Successful decoding must survive a canonical round trip.
Invalid inputs may return errors; panics, sanitizer failures, timeouts and memory
limit failures fail the campaign. Generic JSON parsing before typed decoding is
deliberately avoided because it would erase duplicate keys.

The seed preparer reads every canonical command/event conformance fixture from
the current v6 schemas and copies the committed `seeds/` files byte for byte.
`valid` and `invalid` directories specify stable test expectations: valid inputs
must decode in their named family, while invalid inputs must fail both decoders
in that family. A protocol-level `rpc.handshake.rejected` response is valid wire
data. Generated and mutated corpora are disposable and ignored by Git. The
preparer requires a new destination so it cannot overwrite an existing campaign.

## Local smoke run

Run from the repository root on a supported cargo-fuzz host with a C++ compiler:

```bash
rustup toolchain install nightly-2026-09-11 --profile minimal --component rustfmt
cargo +nightly-2026-09-11 install cargo-fuzz --version 0.13.2 --locked
cargo +nightly-2026-09-11 fetch --locked --manifest-path fuzz/Cargo.toml
python3 scripts/prepare_fuzz_corpus.py fuzz/corpus
export CARGO_NET_OFFLINE=true
cargo +nightly-2026-09-11 fuzz build --sanitizer address
for target in client_wire server_wire; do
  cargo +nightly-2026-09-11 fuzz run "$target" --sanitizer address -- \
    -seed=468 -runs=0 -max_len=65536 -timeout=3 -rss_limit_mb=1024
  cargo +nightly-2026-09-11 fuzz run "$target" --sanitizer address -- \
    -seed=468 -runs=10000 -max_total_time=60 -max_len=65536 \
    -timeout=3 -rss_limit_mb=1024 -print_final_stats=1
done
```

`cargo-fuzz 0.13.2` does not forward `--locked`. Fetch with `--locked` first,
then build/run offline; CI additionally rejects any harness lockfile change.
Keep the nested lockfile's shared dependencies aligned with the production
lockfile when updating dependencies.

The PR workflow runs each target in a separate Linux job, with at most 10,000
executions or 60 seconds of mutation and a 20-minute overall job timeout. Initial
corpus replay is a separate step. The 64 KiB input cap is a campaign budget, not
a protocol limit; large-frame pressure tests remain separate evidence.

The fixed seed, pinned tools, initial corpus and dependency lockfile make a run
easier to reproduce. Time-limited mutation is not guaranteed identical across
machines. Stable replay of an exact failing input is the authoritative regression.
Stable property tests use fixed seeds, 256 cases, and bounded shrinking; framing
properties compare the actual reader with an independent whole-buffer splitter,
including deterministic cancellation/resumption. They check logical buffering
and read sizes, not total heap allocation or retained transcript memory.

## Extended campaigns and failure replay

Manually dispatch **Rust protocol fuzzing** with 5, 15 (default), or 60 minutes per
target. Manual jobs have an 80-minute overall timeout. No runs are scheduled.
For a local 15-minute campaign, replace `-runs=10000 -max_total_time=60` with
`-max_total_time=900`, retaining the other limits.

CI retains logs, revision/toolchain/lockfile metadata and reproducing artifacts
for 14 days, including on failure. To replay and minimize a downloaded artifact:

```bash
cargo +nightly-2026-09-11 fuzz run client_wire /path/to/crash -- \
  -timeout=3 -rss_limit_mb=1024
cargo +nightly-2026-09-11 fuzz tmin client_wire /path/to/crash -- \
  -max_total_time=60 -timeout=3 -rss_limit_mb=1024
```

Use the target named in the failed job. After fixing the cause, copy the minimized
raw bytes into that family's committed `valid/` or `invalid/` directory, according
to the protocol contract, with a descriptive filename. Add a focused regression
when the generic decoding oracle cannot express the bug. Never automatically
commit campaign output. Run the stable seed replay and the bounded campaign again.

Framing counterexamples belong in the private framing tests; malformed JSON and
invalid UTF-8 are rejected by protocol decoding, not by the byte-oriented framer.
