# TUI Terminal-Frame Evidence

**Date:** 2026-08-26
**POC:** Hanyu Wu
**Issue:** #443

## TL;DR

A deterministic three-run PTY comparison on Textual 8.2.8 found that positive synchronized-output
negotiation encloses every measured Wisp payload write in exactly one balanced CSI 2026 pair, with a
process-wide maximum depth of one through restored shutdown. A second three-run comparison proved the
Wisp opt-out emits no CSI 2026 controls even when the terminal reports support. Fresh direct native
runs confirmed balanced teardown in WezTerm and unsupported fallback in Apple Terminal; neither
environment showed a perceptible default/opt-out difference or cleanup problem. Earlier tmux evidence
remains valid but was not repeated for the opt-out comparison. Windows Terminal is unavailable, is not
a blocker for this scoped policy, and receives no compatibility claim.

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

| Mode | Run | Order | Full layouts | Chops updates | Observed frames | Exact pairs | Payload writes inside | Payload writes outside | Process begin/end | Max depth |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| unsupported | 1 | 1 | 1 | 10 | 11 | 0 | 0 | 11 | 0 / 0 | 0 |
| supported | 1 | 2 | 1 | 10 | 11 | 11 | 11 | 0 | 16 / 16 | 1 |
| supported | 2 | 1 | 1 | 9 | 10 | 10 | 10 | 0 | 15 / 15 | 1 |
| unsupported | 2 | 2 | 1 | 10 | 11 | 0 | 0 | 11 | 0 / 0 | 0 |
| unsupported | 3 | 1 | 1 | 9 | 10 | 0 | 0 | 10 | 0 / 0 | 0 |
| supported | 3 | 2 | 1 | 10 | 11 | 11 | 11 | 0 | 15 / 15 | 1 |

Observed automated properties:

- Textual emitted its real `CSI ? 2026 $ p` query in all six samples.
- Only supported samples received `CSI ? 2026 ; 1 $ y`.
- All 32 supported measured frames had exactly one begin and one end.
- All 32 supported payload writes occurred inside synchronization; none escaped.
- Unsupported samples emitted no begin/end controls.
- Process-wide begin/end totals remained balanced through application teardown.
- Process-wide synchronization depth never exceeded one.
- Every sample included a complete layout and partial updates.
- Wisp's display-cache suppression remained active during warm updates.
- Reports retained no terminal payload or escape-sequence text.

Frame counts vary slightly because paced Textual refreshes may coalesce; synchronization invariants
do not depend on a fixed refresh count.

The same command with `--disable-synchronized-output` completed all six samples and retained the same
full-layout, partial-update, cache, and source-completion behavior. All six samples reported zero
capability activation, exact pairs, process begin/end controls, and maximum depth. Three samples still
received the synthetic positive terminal response, proving the Wisp policy rather than terminal
non-support caused the fallback.

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

# Repeat with the same label and viewport for the visual fallback comparison.
uv run python -m benchmarks.tui_terminal_frames --mode native --runs 3 \
  --disable-synchronized-output \
  --messages 20 --retained-history 10 --stream-chunks 12 \
  --stream-interval-seconds 0.03 --width 100 --height 24 \
  --pending-tool-cards 2 \
  --emulator-label "<emulator version / host OS / direct or multiplexer version / disabled>" \
  --output profiles/tui-terminal-frames-<environment>-disabled.json
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
state. Raw reports remain machine-local. Windows Terminal rows are recorded as unavailable rather
than pending; they do not support a compatibility claim:

| Emulator | Run | Capability | Full layouts | Chops updates | Observed frames | Exact pairs | Unbalanced | Writes inside | Writes outside | Source complete |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| WezTerm 20240203-110809-5046fc22 | 1 | yes | 1 | 8 | 9 | 9 | 0 | 9 | 0 | yes |
| WezTerm 20240203-110809-5046fc22 | 2 | yes | 1 | 10 | 11 | 11 | 0 | 11 | 0 | yes |
| WezTerm 20240203-110809-5046fc22 | 3 | yes | 1 | 11 | 12 | 12 | 0 | 12 | 0 | yes |
| Apple Terminal 2.15 | 1 | no | 1 | 11 | 12 | 0 | 0 | 0 | 12 | yes |
| Apple Terminal 2.15 | 2 | no | 1 | 12 | 13 | 0 | 0 | 0 | 13 | yes |
| Apple Terminal 2.15 | 3 | no | 1 | 13 | 13 | 0 | 0 | 0 | 13 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 1 | yes | 1 | 11 | 12 | 12 | 0 | 12 | 0 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 2 | yes | 1 | 11 | 11 | 11 | 0 | 11 | 0 | yes |
| tmux 3.7b / WezTerm 20240203-110809-5046fc22 | 3 | yes | 1 | 11 | 12 | 12 | 0 | 12 | 0 | yes |
| Windows Terminal | 1 | unavailable | — | — | — | — | — | — | — | — |
| Windows Terminal | 2 | unavailable | — | — | — | — | — | — | — | — |
| Windows Terminal | 3 | unavailable | — | — | — | — | — | — | — | — |

Fresh explicit opt-out results:

| Emulator | Capability detected | Observed frames | Exact pairs | Process begin/end | Max depth | Source complete |
|---|---:|---:|---:|---:|---:|---:|
| WezTerm direct | 0 / 3 | 32 | 0 | 0 / 0 | 0 | 3 / 3 |
| Apple Terminal direct | 0 / 3 | 32 | 0 | 0 / 0 | 0 | 3 / 3 |
| tmux on WezTerm | not rerun | — | — | — | — | — |
| Windows Terminal | unavailable | — | — | — | — | — |

Current summary:

| Emulator | Version | Host / multiplexer | Capability detected | Exact pairs | Unbalanced | Payload outside sync | Source complete | Process-wide balance | Manual flicker | Cleanup |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|
| WezTerm | 20240203-110809-5046fc22 | macOS 26.5.2 / direct | 3 / 3 | 32 / 32 frames | 0 | 0 | 3 / 3 | 45 / 45; max depth 1 | no perceptible difference vs opt-out | prompt and input restored; no cleanup issue reported |
| Apple Terminal | 2.15 | macOS 26.5.2 / direct | 0 / 3 | 0 / 38 frames | 0 | 38 | 3 / 3 | 0 / 0; max depth 0 | no perceptible difference vs opt-out | prompt, cursor, and input restored |
| tmux | 3.7b | WezTerm 20240203-110809-5046fc22 | 3 / 3 | 35 / 35 frames | 0 | 0 | 3 / 3 | not freshly measured | opt-out comparison skipped | earlier 100x24 run restored the shell prompt |
| Windows Terminal | unavailable | Windows / direct | — | — | — | — | — | — | — | not tested; no compatibility claim |

For supported samples, require exact pairs to equal observed driver frames, unbalanced frames and
payload writes outside synchronization to equal zero, at least one full layout and partial update, and
complete source. Unsupported samples must detect no capability and emit no synchronization controls.
Treat mixed capability across repeated runs as a failure. Manual observations cover the cold full
layout, paced partial updates, and normal restoration of the alternate screen, cursor, keyboard input,
and shell prompt; they remain separate from automated framing counts.

The native matrix cannot be automated by the headless benchmark runner because visual flicker and the
actual emulator capability response are properties of the user's interactive terminal.

Native mode now keeps its observer attached through Textual's terminal restoration and records
`process_sync_begin_count`, `process_sync_end_count`, `process_sync_balanced`, and
`process_sync_max_depth`. Fresh direct reports verify those fields; the earlier tmux report predates
the instrumentation and was not rerun. A normal process exit or restored shell prompt remains cleanup
evidence, not proof of balanced CSI 2026 controls across teardown.

The fresh direct samples satisfy their automated gates: WezTerm detected capability in every default
run, wrapped all 32 observed frames exactly once, emitted no payload outside synchronization, balanced
45 process-wide pairs at maximum depth one, and emitted zero controls when disabled. Apple Terminal
consistently retained unsynchronized fallback behavior in both modes. Manual comparison found no
perceptible difference and no cleanup issue. The older tmux samples still show 35/35 exact per-frame
pairs, but no fresh opt-out or process-wide claim is made. Windows Terminal is an accepted unavailable
environment.

## Production decision

Use Textual-owned synchronization if native evidence confirms:

1. supported terminals detect capability without a Wisp override;
2. every eligible frame has exactly one balanced begin/end pair;
3. measured payload writes remain inside that pair;
4. begin/end totals remain balanced after terminal restoration;
5. unsupported terminals emit no CSI 2026; and
6. supporting emulators show a repeatable visual improvement or no intermediate-frame exposure.

The policy implementation adds a conservative Wisp opt-out and adversarial lifecycle coverage without
duplicating Textual's frame wrapper. It does not add force-on behavior. Direct native runs verify the
remaining visual and teardown items; tmux and Windows limitations remain explicit rather than inferred.

## Current recommendation

The automated evidence favors Textual ownership and positive capability detection:

- default only after the terminal positively reports support;
- explicit Wisp opt-out;
- no force-on mode;
- no synchronization for inline, headless, line, print, JSONL-RPC, or other noninteractive output.

Adopt this policy for the scoped production change. Automated evidence proves intermediate payload
writes are not exposed on a supporting terminal, while manual checks found no perceptible regression.
The recommendation does not extend to Windows Terminal, and tmux retains only its earlier per-frame
claim because the fresh comparison was intentionally skipped.
