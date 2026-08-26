# TUI Terminal-Frame Evidence

**Date:** 2026-08-26
**POC:** Hanyu Wu
**Issue:** #443

## TL;DR

A deterministic three-run PTY comparison on Textual 8.2.8 found that positive synchronized-output
negotiation encloses every measured Wisp payload write in exactly one balanced CSI 2026 pair.
Nine native samples now confirm the same behavior in WezTerm directly and through tmux, while Apple
Terminal retains the unsupported fallback without emitting CSI 2026 controls. This evidence pass is
explicitly scoped to Apple Terminal, WezTerm, tmux on WezTerm, and Windows Terminal; Windows results
and manual flicker observations remain required before deciding the production policy.

## Automated paired PTY run

Command:

```bash
uv run python -m benchmarks.tui_terminal_frames --mode paired --runs 3 \
  --emulator-label deterministic-pty \
  --output profiles/tui-terminal-frames-paired.json
```

Environment:

- macOS 26.5.2, arm64
- Python 3.12.2
- Textual 8.2.8
- 100×24 pseudo-terminal
- 20 fixture messages, 10 retained history entries
- 12 paced stream chunks and 2 pending tool cards per sample
- alternating unsupported/supported order across three runs

The workload measures one cold full-layout frame followed by normal warm partial updates. Every
sample completed the expected source stream.

| Mode | Run | Order | Full layouts | Chops updates | Observed frames | Exact pairs | Payload writes inside | Payload writes outside | Process begin/end |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| unsupported | 1 | 1 | 1 | 10 | 10 | 0 | 0 | 10 | 0 / 0 |
| supported | 1 | 2 | 1 | 11 | 12 | 12 | 12 | 0 | 16 / 16 |
| supported | 2 | 1 | 1 | 10 | 11 | 11 | 11 | 0 | 16 / 16 |
| unsupported | 2 | 2 | 1 | 10 | 10 | 0 | 0 | 10 | 0 / 0 |
| unsupported | 3 | 1 | 1 | 10 | 11 | 0 | 0 | 11 | 0 / 0 |
| supported | 3 | 2 | 1 | 9 | 10 | 10 | 10 | 0 | 15 / 15 |

Observed automated properties:

- Textual emitted its real `CSI ? 2026 $ p` query in all six samples.
- Only supported samples received `CSI ? 2026 ; 1 $ y`.
- All 33 supported measured frames had exactly one begin and one end.
- All 33 supported payload writes occurred inside synchronization; none escaped.
- Unsupported samples emitted no begin/end controls.
- Process-wide begin/end totals remained balanced through application teardown.
- Every sample included a complete layout and partial updates.
- Wisp's display-cache suppression remained active during warm updates.
- Reports retained no terminal payload or escape-sequence text.

Frame counts vary slightly because paced Textual refreshes may coalesce; synchronization invariants
do not depend on a fixed refresh count.

## Scoped native emulator matrix

Run native mode three times in each environment before the production policy PR. Use the explicit
fixture below in every terminal so later default changes cannot alter the comparison:

```bash
uv run python -m benchmarks.tui_terminal_frames --mode native --runs 3 \
  --messages 20 --retained-history 10 --stream-chunks 12 \
  --stream-interval-seconds 0.03 --width 100 --height 24 \
  --pending-tool-cards 2 \
  --emulator-label "<emulator version / host OS / direct or multiplexer version>" \
  --output profiles/tui-terminal-frames-<environment>.json
```

Resize the usable terminal or multiplexer pane to exactly 100x24 before running the command. Native
mode now rejects a mismatched real viewport and records the validated dimensions as
`terminal_columns` and `terminal_lines`. Keep raw JSON reports local under ignored `profiles/`.

This evidence pass intentionally revises the original five-environment plan to a four-environment
matrix: Apple Terminal and WezTerm directly on macOS, tmux hosted by WezTerm, and Windows Terminal.
iTerm2 is excluded because it is unavailable in the test environment, rather than being counted as
completed or pending evidence. Conclusions from this matrix are representative of only these scoped
environments, not exhaustive native-terminal coverage.

Recorded native environment:

- macOS 26.5.2, arm64
- Python 3.12.2
- Textual 8.2.8
- validated 100x24 terminal or multiplexer pane
- 20 fixture messages, 10 retained history entries
- 12 paced stream chunks and 2 pending tool cards per sample

Every recorded row came from a real terminal stream without assigning Textual's private capability
state. Raw reports remain machine-local. Three rows are still pending from the unavailable Windows
Terminal environment:

| Emulator | Run | Capability | Full layouts | Chops updates | Observed frames | Exact pairs | Unbalanced | Writes inside | Writes outside | Source complete |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| WezTerm 20240203-110809-5046fc22 | 1 | yes | 1 | 11 | 11 | 11 | 0 | 11 | 0 | yes |
| WezTerm 20240203-110809-5046fc22 | 2 | yes | 1 | 8 | 9 | 9 | 0 | 9 | 0 | yes |
| WezTerm 20240203-110809-5046fc22 | 3 | yes | 1 | 10 | 11 | 11 | 0 | 11 | 0 | yes |
| Apple Terminal 2.15 | 1 | no | 1 | 12 | 13 | 0 | 0 | 0 | 13 | yes |
| Apple Terminal 2.15 | 2 | no | 1 | 11 | 12 | 0 | 0 | 0 | 12 | yes |
| Apple Terminal 2.15 | 3 | no | 1 | 11 | 12 | 0 | 0 | 0 | 12 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 1 | yes | 1 | 11 | 12 | 12 | 0 | 12 | 0 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 2 | yes | 1 | 11 | 11 | 11 | 0 | 11 | 0 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 3 | yes | 1 | 11 | 12 | 12 | 0 | 12 | 0 | yes |
| Windows Terminal | 1 | pending | pending | pending | pending | pending | pending | pending | pending | pending |
| Windows Terminal | 2 | pending | pending | pending | pending | pending | pending | pending | pending | pending |
| Windows Terminal | 3 | pending | pending | pending | pending | pending | pending | pending | pending | pending |

Current summary:

| Emulator | Version | Host / multiplexer | Capability detected | Exact pairs | Unbalanced | Payload outside sync | Source complete | Manual flicker | Cleanup |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| WezTerm | 20240203-110809-5046fc22 | macOS 26.5.2 / direct | 3 / 3 | 31 / 31 frames | 0 | 0 | 3 / 3 | pending | process exited normally; cursor/input not manually checked |
| Apple Terminal | 2.15 | macOS 26.5.2 / direct | 0 / 3 | 0 / 37 frames | 0 | 37 | 3 / 3 | pending | shell prompt restored; cursor/input not manually checked |
| tmux | 3.7b | WezTerm 20240203-110809-5046fc22 | 3 / 3 | 35 / 35 frames | 0 | 0 | 3 / 3 | pending | 100x24 pane and shell prompt restored; cursor/input not manually checked |
| Windows Terminal | pending | Windows / direct | pending | pending | pending | pending | pending | pending | pending |

For supported samples, require exact pairs to equal observed driver frames, unbalanced frames and
payload writes outside synchronization to equal zero, at least one full layout and partial update, and
complete source. Unsupported samples must detect no capability and emit no synchronization controls.
Treat mixed capability across repeated runs as a failure. Manual observations cover the cold full
layout, paced partial updates, and normal restoration of the alternate screen, cursor, keyboard input,
and shell prompt; they remain separate from automated framing counts.

The native matrix cannot be automated by the headless benchmark runner because visual flicker and the
actual emulator capability response are properties of the user's interactive terminal.

The nine completed samples satisfy their automated gates: supporting environments detected capability
in every run, wrapped every observed frame exactly once, and emitted no payload outside
synchronization; Apple Terminal consistently retained unsynchronized fallback behavior. This is not
yet the production decision because Windows Terminal and all manual flicker checks
remain pending.

## Decision gate for the next PR

Use Textual-owned synchronization if native evidence confirms:

1. supported terminals detect capability without a Wisp override;
2. every eligible frame has exactly one balanced begin/end pair;
3. measured payload writes remain inside that pair;
4. begin/end totals remain balanced after terminal restoration;
5. unsupported terminals emit no CSI 2026; and
6. supporting emulators show a repeatable visual improvement or no intermediate-frame exposure.

If those conditions hold, the next PR should add a conservative Wisp opt-out and adversarial lifecycle
coverage without duplicating Textual's frame wrapper. Do not add force-on behavior.

If native evidence finds eligible writes escaping Textual's pair, the next PR should instead be an
opt-in Wisp-owned wrapper prototype with nested-frame and cleanup protection.

## Current recommendation

The automated evidence favors Textual ownership and positive capability detection:

- default only after the terminal positively reports support;
- explicit Wisp opt-out in the next PR;
- no force-on mode;
- no synchronization for inline, headless, line, print, JSONL-RPC, or other noninteractive output.

This recommendation remains provisional until Windows Terminal and manual flicker evidence finish
this scoped evidence pass.
