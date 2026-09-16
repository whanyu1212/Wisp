# Wisp 0.2.0rc2 release decision and checklist

Status: release preparation, September 16, 2026. This PR prepares the candidate; it does not create a
tag, publish packages, or remove Textual. Stable remains 0.1.0; the latest published candidate at
preparation time is 0.2.0rc1.

## Renderer decision

The release owner approved an RC trial of Rust as the native-install default, superseding the
September 3 experimental-default hold in #470 for this candidate. This is an explicit product
decision under #456, not an inference from completed packaging or feature issues.

- `wisp`, `wisp tui`, and `wisp --mode tui` default to `auto`.
- On macOS/Linux, an installed distribution declaring `wisp-tui` selects Rust. A pure wheel or
  source installation without that declaration selects Textual. No executable is discovered via PATH.
- Native release targets are macOS arm64 and Linux glibc 2.28+ x86_64. Intel macOS, Linux arm64,
  musl/Alpine, and Windows retain the Python route; Windows remains best-effort, not CI-certified.
- Explicit CLI flags override `WISP_TUI_RENDERER`; the environment overrides `auto`.
  `--line` remains explicit. A development `WISP_RUST_TUI_BINARY` override selects Rust in auto mode
  on macOS/Linux and is validated normally.
- A declared but missing, non-executable, corrupt, incompatible, or failing Rust binary remains an
  actionable error. There is no fallback after Rust selection. Users select Textual explicitly with
  `wisp tui --renderer textual` or `WISP_TUI_RENDERER=textual`.
- Textual remains installed, tested, and maintained for compatibility and critical fixes. New
  frontend features prioritize Rust. Removal and stable promotion require separate decisions.

Python retains runtime, safety, authentication, and persistence authority. Print, JSON, RPC, and SDK
contracts are unchanged by renderer selection. Package versions remain exactly paired:
Python `0.2.0rc2`, Cargo `0.2.0-rc.2`.

## Accepted RC differences and promotion blockers

Rust does not yet implement transcript search, built-in transcript copying/drag selection, clickable
Markdown links, or Textual's integrated update/restart dialog. Terminal-native copying is available
subject to mouse-capture settings. Composer clipboard support is separate. The Textual fallback
remains available when these differences affect a workflow.

Any reproducible loss of session content, incorrect approval/trust/cancellation behavior, startup
failure on a claimed native target, or terminal/process cleanup regression blocks release acceptance.
Full history is retained without a transcript cap: startup cost and memory grow with the corpus.
Large messages may use plain-text rendering. Rust's implementation language is not performance proof.

Before stable promotion, record representative terminal and multiplexer feedback, copying and
accessibility limitations, support burden, and matched Textual/Rust input-to-frame, CPU, startup, and
memory measurements. The installed RC smoke below is a narrower release check, not that comparison.

## Prepublication checks

- [ ] Exact PR head has terminal green CI and no actionable review threads.
- [ ] Python format/lint/mypy, generated themes, immutable protocol schemas, and full tests pass.
- [ ] Rust formatting, Clippy, full workspace tests, build, and Python/Rust handoff tests pass.
- [ ] Pure wheel, sdist, and native candidate wheels pass metadata/version/content verification.
- [ ] Native-wheel lifecycle passes on every claimed target: automatic selection, explicit Textual,
  native-to-pure replacement, corrupt/non-executable failure, offline reinstall, and uninstall.
- [ ] Each native target records installed startup with empty history and 10,001 saved messages,
  submits a new fake-provider prompt after hydration, exits, and restores terminal attributes.
- [ ] Docs build passes; release notes explain renderer selection, platform scope, and rollback.

The wheel CI runs `scripts/verify_rust_tui_install_lifecycle.py`. Its JSON evidence includes an empty
session and `long_history` measurements. The synthetic large fixture contains 2,500 complete
user/assistant/read-tool/result cycles (10,000 records) plus a final readiness sentinel. It is generated
outside the startup timing. `ready_frame_seconds` measures observed readiness through the installed
launcher; `max_rss_bytes` is the maximum reported child-process RSS, not the sum or concurrent peak of
the process tree. Timings depend on machine and load and are observations, not universal thresholds.

To reproduce a long-history measurement in an isolated installed native environment:

```bash
/path/to/environment/bin/python scripts/smoke_installed_rust_tui.py \
  --wisp /path/to/environment/bin/wisp \
  --session-dir /tmp/wisp-rc2-history-probe \
  --history-messages 10000
```

Use a disposable session directory. This submits only a fake-provider prompt; no API credentials are
needed. Candidate CI artifacts are prepublication evidence, not proof of PyPI availability.

## Local candidate evidence

Measured September 16 on macOS arm64 with Python 3.12.2, using the installed native candidate wheel
outside the source package. The full Python test suite was running concurrently; these are single
observations under load, not a controlled renderer comparison.

| Workload | Saved JSONL bytes | Observed ready frame | Maximum reported child RSS |
| --- | ---: | ---: | ---: |
| Empty session | 0 | 4.066 s | 106,692,608 bytes |
| 10,001 messages, including 2,500 read calls/results | 6,047,246 | 4.757 s | 174,014,464 bytes |

Both workloads submitted a fake-provider prompt, received its response, exited successfully, and
restored terminal attributes. The complete local lifecycle also passed default selection on native
and pure installs, explicit Textual routing, absent Rust after pure replacement, non-executable and
corrupt Rust failures, native restoration, offline reinstall, and uninstall. Unit tests separately
cover a native distribution declaring a missing executable. Wheel metadata/content parity verification passed.
The native wheel measured 5,703,473 bytes; its stripped binary measured 11,938,416 bytes.

The PR's candidate-wheel CI supplies Linux x86_64 evidence using the same script.
Published PyPI installation and cross-version downgrade remain postpublication work.

## Publication and postpublication acceptance

After the release PR merges, publishing requires a separately authorized `v0.2.0rc2` tag through the
existing release workflow. Do not manually upload partial artifacts or substitute source-build tests.

- [ ] Verify exactly one sdist, one pure fallback wheel, and two native wheels were published.
- [ ] Verify checksums, SBOMs, provenance attestations, package/native versions, and protocol identity.
- [ ] Install `wisp-ai==0.2.0rc2` from PyPI without Cargo on each claimed target; repeat default-launch,
  prompt, session resume, explicit Textual, and lifecycle checks outside the source checkout.
- [ ] Record immutable artifact links and measurements; update the upgrade guide's publication status.
- [ ] Dogfood the published candidate and record remaining terminal/accessibility issues under #456.

For a frontend regression, select Textual explicitly and retain the session files for diagnosis.
Reverting the default requires another reviewed change; downgrading the entire package is not needed
just to switch frontends. Cross-version native rollback evidence remains separate from this PR's
same-version native/pure replacement checks. Never delete or migrate user sessions as a rollback step.
