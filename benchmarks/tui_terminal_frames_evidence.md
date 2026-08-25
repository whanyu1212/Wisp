# TUI Terminal-Frame Evidence

**Date:** 2026-08-25
**POC:** Hanyu Wu
**Issue:** #443

## TL;DR

A deterministic three-run PTY comparison on Textual 8.2.8 found that positive synchronized-output
negotiation encloses every measured Wisp payload write in exactly one balanced CSI 2026 pair.
Unsupported runs emitted no CSI 2026 controls and retained their unsynchronized write behavior.
This supports keeping Textual as the frame owner, but the native emulator matrix and manual flicker
observations remain required before deciding the production policy in the next PR.

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

## Native emulator matrix

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

The final evidence update must include one row for each of the 15 samples, retaining capability,
observed-frame, exact-pair, unbalanced-frame, inside/outside-write, complete-layout, partial-update,
and source-completeness fields. The summary matrix remains pending until those individual rows exist:

| Emulator | Version | Host / multiplexer | Capability detected | Exact pairs | Unbalanced | Payload outside sync | Source complete | Manual flicker | Cleanup |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| iTerm2 | pending | macOS / direct | pending | pending | pending | pending | pending | pending | pending |
| Kitty or WezTerm | pending | direct | pending | pending | pending | pending | pending | pending | pending |
| Apple Terminal | pending | macOS / direct | expected no | expected 0 | expected 0 | pending | pending | pending | pending |
| tmux | pending | representative host | pending | pending | pending | pending | pending | pending | pending |
| Windows Terminal | pending | Windows / direct | pending | pending | pending | pending | pending | pending | pending |

For supported samples, require exact pairs to equal observed driver frames, unbalanced frames and
payload writes outside synchronization to equal zero, at least one full layout and partial update, and
complete source. Unsupported samples must detect no capability and emit no synchronization controls.
Treat mixed capability across repeated runs as a failure. Manual observations cover the cold full
layout, paced partial updates, and normal restoration of the alternate screen, cursor, keyboard input,
and shell prompt; they remain separate from automated framing counts.

The native matrix cannot be automated by the headless benchmark runner because visual flicker and the
actual emulator capability response are properties of the user's interactive terminal.

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

This recommendation remains provisional until the native emulator matrix is complete.
