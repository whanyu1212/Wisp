---
title: TUI
---

# TUI

```bash
wisp
```

A fullscreen Textual TUI built on the same RPC controller other integrations use. While a command is
active, a spinning `Working…` row stays at the live transcript tail as assistant output and tool
cards appear, and changes labels for retries, approvals, trust, or compaction.

The footer shows the working directory plus plan/queued state on the left, the active shortcut in the
center, and the model, billing route, and context percentage on the right. At narrow widths it
progressively drops the shortcut, model, and working directory while preserving plan/queued state and
compact billing and context fields.

- `context 53%` is a current provider observation; `context ~53%` is an estimate. Narrow layouts
  shorten these to `53%` and `~53%`.
- Billing shows the active route as `ChatGPT plan` for subscription-backed Codex, `offline` for the
  fake provider, or `API` for a direct provider. Once usage is recorded, its session-wide cumulative
  estimate is labeled independently as `session $0.042`, `session ≥$0.042` when partially priced, or
  `session unpriced` when no request can be priced. This keeps earlier usage honest after switching
  providers. Estimates are not invoices.

## Experimental Rust TUI

Textual remains Wisp's default and full-featured TUI. An experimental Rust frontend is available as
an explicit opt-in on macOS and Linux:

```bash
wisp tui --renderer rust
wisp --mode tui --tui-renderer rust
WISP_TUI_RENDERER=rust wisp
```

The Rust TUI negotiates and validates live RPC v3/event schema v35, supports prompts, approvals,
project trust, cancellation, steering and follow-up queues, a virtual Markdown/tool/diff transcript,
and bounded session history.
`/resume` opens a keyboard-only picker for up to 50 persisted sessions (or accepts
one exact session ID); `/new` deselects the current session and clears the local transcript after the
backend confirms it. Startup and resumed history install the newest 200-message page atomically;
reaching an edge loads additional 75-message pages while retaining at most 1,200 logical transcript
rows. An omission row marks history that remains outside the retained window.

The Rust TUI also exposes direct persisted-session workflows: `/name <display name>` and
`/name --clear`, `/clone`, `/tree`, and `/unrevert`. The `/tree` picker uses `Up`/`Down`,
`PageUp`/`PageDown`, `Home`/`End`, `Enter` to navigate, `f` to fork a selected user-message node,
and `Escape` to close. It requests 200 append-ordered nodes per page and retains only the newest two
pages (400 nodes); an omission row appears after the oldest page is evicted, and reopening `/tree`
starts again from the first page. Forking restores the selected prompt after the fork's authoritative
history loads. Navigating to a user-message node likewise restores its editable prompt after loading;
prompts that exceed the editor limit are rejected explicitly rather than truncated.

This is still experimental and source-build only: current Python distributions do not include the Rust
binary, and it intentionally omits Textual features such as transcript search and mouse controls.
Textual does not currently expose Rust's direct naming, clone, tree-navigation, or unrevert commands.
Textual's model picker is hydrated from the backend's authoritative ordered catalog before input is
enabled. It disables unavailable providers, passes typed `/model` values through unchanged, and only
persists the selection reported by the backend. If discovery fails, prompts and typed `/model`
commands remain available while the bare picker reports the catalog as unavailable. Rust validates
the same model-catalog contract but leaves picker interaction to
[#467](https://github.com/whanyu1212/Wisp/issues/467).
Selecting Rust never falls back to Textual. A missing/non-executable binary,
unsupported platform, package-version mismatch,
negotiation failure, or non-zero Rust exit is reported as an error. See
[Development setup](../contributing/development#rust-tui-scaffold), or select Textual explicitly with
`wisp tui --renderer textual`.

Unlike print mode, **the Textual TUI exposes the full tool registry by default** — otherwise it would
be a chatbot that can't read files or run commands. Mutating and command tools still pause for
approval: approve once, allow that tool for the session, YOLO all mutating/command tools for the
process (never persisted), or deny.

## Steering and follow-ups

The composer remains active while a prompt runs. In the Textual, prompt-toolkit fullscreen, and
experimental Rust TUIs:

- `Enter` sends a steering message for the active run. It is injected at the next safe request
  boundary, after any current assistant/tool batch.
- `Alt+Enter` queues follow-up work that starts when the active run would otherwise finish.
- `Alt+Up` removes the newest queued steering or follow-up message and restores it ahead of the
  current draft, after the shared runtime confirms the queue change.
- `Escape` cancels the active prompt in the Python fullscreen TUIs. The Rust TUI accepts either
  `Escape` or `Ctrl+C`. Cancellation does not discard runtime-owned queued messages.

A bounded queue panel previews up to three items and labels them `steer` or `later`; an omitted-item
count indicates when more are queued. Python fullscreen TUIs report separate steering and follow-up
totals in the footer; Rust shows them in its header and composer. Python returns failed submissions to
the composer. Rust retains them as recoverable drafts: `Alt+Up` restores one ahead of the current
draft. The Rust TUI clears a submitted draft only after the JSONL writer flushes it, refreshes queue
state after startup and session changes, and reports queued or recovering text as unsent if the
transport closes.

The line renderer accepts text entered during a run as follow-up work, but does not expose the
fullscreen steering and restoration keybindings.

## Slash commands

```text
/help                       show help
/init                       inspect the project and create a root AGENTS.md
/auth [provider]            show credential status
/connect [provider]         connect a provider or open the provider panel
/disconnect [provider]      remove stored credentials (`/logout` alias)
/provider [provider]        switch provider (resets model to default)
/model [model] [effort]     switch model and optional reasoning effort
/new                        start a fresh session and clear the screen
/resume [session-id]        browse or resume a persisted session
/compact [instructions]     summarize older context while preserving the JSONL audit
/context [auto on|off]      show or toggle compaction policy
/plan                       switch to read-only planning mode
/build                      switch to normal build mode
/history                    search prompts submitted in this TUI run
/theme [name]               preview or select a curated color theme
/update [check|install]     check immediately or explicitly install an update
/skills                     inspect loaded skills and discovery diagnostics
/mcp                        show configured MCP servers and registered tools
/quit, /exit
```

`/init` asks the active model to inspect repository documentation, manifests, CI configuration, and
source layout before creating project-specific guidance. It only works in build mode, uses the normal
project-trust and write-approval flow, and refuses to replace an existing `AGENTS.md` or `AGENTS.MD`.
The final write is create-only, so a file that appears during inspection is preserved.

## Completions and the file picker

Type `/` to filter commands inline. Type `@` to reference a project file. The picker starts in fuzzy
mode and matches loosely, so `@tuiapp` finds `src/wisp/tui/textual_app.py`; press `Tab` to switch to a
project tree without changing the draft or query, and press `Tab` again to return.

`Up`/`Down` move the selection. In tree mode, `Left`/`Right` collapse or expand a directory, while
`Enter` (or a click) expands/collapses directories and inserts files. Fuzzy mode retains directory
insertion for compatibility. `Escape` dismisses the picker without changing the draft.

Only the path is inserted; Wisp does not inline file contents, and the shared snapshot honors the same
`protected_paths` policy, so secrets are never offered. A visible limit cue means the indexed snapshot
omitted paths rather than proving a directory is empty.

The prompt editor highlights recognized commands and project paths alongside common Markdown
structure: headings, list markers, inline code, and fenced code blocks. Highlighting is a bounded,
presentation-only aid rather than a Markdown preview; the exact editable source remains the prompt
submitted to the agent, and incomplete Markdown stays editable.

## Keybindings

| Key | Action |
|---|---|
| `Enter` | Submit; while a prompt runs, steer it; or activate the selected slash/file-picker item |
| `Alt+Enter` | While a prompt runs, queue a follow-up; otherwise insert a newline |
| `Alt+Up` | While a prompt runs, restore the newest queued item to the composer |
| `Shift+Enter` / `Ctrl+J` | Insert newline (`Ctrl+J` in the live fullscreen renderer) |
| `Tab` | Switch fuzzy/tree for an active file picker; complete an active slash command |
| `Up` / `Down` | Move through an active suggestion menu |
| `Left` / `Right` | Collapse/expand the selected directory in tree mode |
| `Shift+Tab` | Toggle plan/build mode |
| `Ctrl+T` | Switch between the light and dark themes (remembered across runs) |
| `Ctrl+G` | Toggle contextual help for the focused Textual surface |
| `Ctrl+R` | Search prompt history for this TUI run |
| Mouse wheel / trackpad | Scroll the transcript without moving editor focus |
| `PageUp` / `PageDown` | Scroll the transcript by one page |
| `Home` / `End` | Traverse to the session beginning / return to the latest output |
| `Escape` | Dismiss nearest menu or overlay, then cancel an active prompt |
| `Ctrl+C` | Copy selection; otherwise press twice within 1.5s to quit |
| `Ctrl+D` | Delete right; EOF only from an empty editor |

The Textual transcript has no visible scrollbar, but all persisted conversation and tool activity
remains reachable through the controls above. Older and newer pages load transparently at the
mounted window edges. When new output arrives while you are reading earlier content, your viewport
stays anchored; select the `↓ new` indicator or press `End` to return to the live tail.

### Resuming long sessions

In Textual, selecting a session from the `/resume` picker, or running `/resume <session-id>`, loads the
complete active-path transcript before revealing the replacement. The experimental Rust TUI installs
the latest page first, then loads older history with `PageUp` or `Ctrl+Home`; `PageDown` or `Ctrl+End`
returns through an evicted tail to the latest page. Plain `Home` remains available to the prompt
editor. Paging preserves surviving viewport anchors; older-page and exact-detail requests can run
while a prompt is active. Once backend selection commits, the old transcript is cleared before the
selected session's page is installed. A
failed or stale page leaves an explicit error instead of mislabeling old or partially loaded history.

Historical file-tool cards keep bounded previews. Press `F6` to browse visible cards and `Enter` to
open detail; when a persisted preview was clipped, the Rust TUI fetches that one exact result on
demand and releases it when the detail view closes. It does not cache historical detail or read JSONL
directly, and cannot recover bytes that the tool truncated before persistence.

Every persisted message row is represented, but representation is logical rather than one widget per
JSONL row. A tool request and its result share one tool card. Repeated process start, poll, cancel, and
completion rows for the same process share one process card; its header reports both the poll count
and represented row count. Focus and expand that card with `Enter` or `Space`, then use `p`/`n` to
move through its bounded update timeline and `l` to load the selected row's exact persisted output.
The timeline keeps transcript layout stable, while exact output is fetched only when requested.

This deliberately trades `/resume` cold-start time and metadata memory for reliable upward scrolling:
the TUI no longer has to mount older page boundaries while a reader is traversing a long resumed
session. Output bodies and tool arguments still use bounded previews during the initial load, so the
same transcript is not held twice in memory. An on-demand detail load returns the exact text stored in
JSONL; it cannot recover bytes that the tool itself truncated before persistence, and those cards stay
marked as truncated.

Run `/theme` to preview Vapor, Orchid, Ember, Storm, Grove, Wave, Paper, and Dawn, or pass one of
those names directly. `Ctrl+T` switches between Paper and the most recently selected dark palette;
from Dawn it returns to that dark palette too. The choice is written to `~/.wisp/tui.json` and
restored on the next run. It is presentation state owned by the TUI client, so it is kept out of
`settings.json` and never reaches the agent subprocess; an unreadable or unrecognized value falls
back to Vapor rather than failing to start.

`Ctrl+G` and `/help` open the same native contextual guide. It follows focus across the editor, tool
cards, pickers, context reports, and safety decisions; its key reference is derived from live
bindings. The panel moves below the conversation on narrow terminals and never runs a tool, changes
the session, or resolves an approval. Line and fallback fullscreen modes keep their textual `/help`
summary.

The searchable prompt-history index holds up to 100 unique prompts and is memory-only; `/history`
does not create a separate on-disk cache. Submitted user messages still become part of the active
session's persistent JSONL transcript under the configured session directory. Do not put secrets in
prompts, and delete or protect session files according to their contents.

## Modes

**Plan mode** applies to future prompts in the current process. It exposes only read-only tools that
were already authorized at startup; `write`, `edit`, `bash`, and non-read extension tools are
unavailable. Use `/build` to restore. The mode is not persisted in session JSONL.

**`/new`** preserves the current JSONL session for `/resume`, clears the transcript and screen, and
creates the next session lazily. Provider, model, effort, mode, tool permissions, trust, and
compaction settings are retained.

## Flags and renderers

```bash
wisp tui --continue
wisp tui --resume <session-id-prefix>
wisp tui --no-all-tools                  # opt-in tool filter instead of the full registry
wisp tui --yes                           # auto-approve mutating/command tools
wisp tui --line                          # simple line renderer, for fallback/debugging
wisp tui --renderer rust                 # experimental source-build Rust TUI
wisp tui --no-synchronized-output        # disable atomic Textual frame presentation
```

At process startup, `--continue` or `--resume` hydrates at most 500 active-path persisted messages
through the same RPC `get_messages` command available to other frontends before accepting input. This
bounded, silent startup path avoids delaying the first frame. Complete hydration begins only after an
explicit interactive `/resume` selection in the Textual TUI; line and fallback renderers retain
bounded paging behavior.

The Textual TUI targets truecolor terminals and degrades gracefully — 256-color and 16-color
terminals are handled by Textual's own detection. Setting `NO_COLOR` switches to deterministic
grayscale.

Textual also queries the terminal for synchronized-output support. A positive response lets Textual
present each display update atomically; unsupported terminals retain ordinary output. If a terminal
or multiplexer shows rendering artifacts, retry with `--no-synchronized-output`. The flag affects only
the Textual TUI and has no environment-variable equivalent; line, print, JSONL-RPC, and SDK output do
not use synchronized frames.

The legacy `--mode tui` entrypoint remains for compatibility and honors
`--tui-renderer line|fullscreen|textual|rust` plus `WISP_TUI_RENDERER`.
