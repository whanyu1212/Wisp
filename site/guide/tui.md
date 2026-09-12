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

::: info Two terminal frontends
Wisp currently ships two terminal clients over the same Python runtime. Textual is the default and
supported product TUI. The Rust client is an experimental opt-in for presentation performance:
source-build, macOS and Linux, with no silent fallback. It is not a rewrite of the agent, and it is
not the default until a later explicit decision. See the
[terminal frontend boundary](../architecture/rust-tui-boundary).
:::

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

The two frontends coexist during this period. Features may land in Rust first without changing the
default. [#470](https://github.com/whanyu1212/Wisp/issues/470) closed with Textual as the supported
product TUI; Rust stays an experimental opt-in on macOS and Linux, source-build only, with no
fallback and no stage-3 supported-opt-in claim. [#467](https://github.com/whanyu1212/Wisp/issues/467),
[#468](https://github.com/whanyu1212/Wisp/issues/468), and
[#469](https://github.com/whanyu1212/Wisp/issues/469) remain the blockers for that later stage. A
default switch requires a new explicit issue.

The [feature-parity matrix](../architecture/rust-tui-boundary#feature-parity-matrix) records delivered
slices through #548: model selection, command discovery, context/compaction, skills/MCP, prompt
history, overlays, file completion, themes, and opt-in mouse navigation. Remaining readiness work
includes configurable bindings, full layout/focus acceptance, hardening, and binary distribution.
Rust also lacks Textual's `/update` notice/install/restart flow; its delivery or an explicit
alternative remains part of the workflow and distribution decisions.

```bash
wisp tui --renderer rust
wisp --mode tui --tui-renderer rust
WISP_TUI_RENDERER=rust wisp
```

The Rust TUI negotiates and validates live RPC v6/event schema v37, supports prompts, approvals,
project trust, cancellation, steering and follow-up queues, a virtual Markdown/tool/diff transcript,
and bounded session history.
`/resume` opens a picker for up to 50 persisted sessions (or accepts
one exact session ID); `/new` deselects the current session and clears the local transcript after the
backend confirms it. Startup and resumed history install the newest 200-message page atomically;
reaching an edge loads additional 75-message pages while retaining at most 1,200 logical transcript
rows. An omission row marks history that remains outside the retained window.

`/connect` opens a provider connection panel. Use arrow keys to select a provider,
`Enter` to start its available API-key or device-code flow, `d` to disconnect stored credentials,
`r` to refresh, and `Escape` to close or cancel. API-key entry is masked. Device login displays the
short-lived user code and verification URL but never stores them in prompt or session history.

The Rust TUI also exposes direct persisted-session workflows: `/name <display name>` and
`/name --clear`, `/clone`, `/tree`, and `/unrevert`. The `/tree` picker uses `Up`/`Down`,
`PageUp`/`PageDown`, `Home`/`End`, `Enter` to navigate, `f` to fork a selected user-message node,
and `Escape` to close. It requests 200 append-ordered nodes per page and retains only the newest two
pages (400 nodes); an omission row appears after the oldest page is evicted, and reopening `/tree`
starts again from the first page. Forking restores the selected prompt after the fork's authoritative
history loads. Navigating to a user-message node likewise restores its editable prompt after loading;
prompts that exceed the editor limit are rejected explicitly rather than truncated.

This is still experimental and source-build only: current Python distributions do not include the Rust
binary. Native text selection/copy and transcript search remain unavailable; mouse navigation is
experimental opt-in as described below.
Textual does not currently expose Rust's direct naming, clone, tree-navigation, or unrevert commands.
Textual's model picker is hydrated from the backend's authoritative ordered catalog before input is
enabled. It disables unavailable providers, passes typed `/model` values through unchanged, and only
persists the selection reported by the backend. If discovery fails, prompts and typed `/model`
commands remain available while the bare picker reports the catalog as unavailable.

Rust also supports `/model` while idle. The picker groups models by provider, disables unavailable
providers, and labels preview and legacy models. Use `Up`/`Down`, `PageUp`/`PageDown`, or `Home`/`End`
to select a model, `Left`/`Right` to choose its reasoning effort (including the provider default),
`Enter` to apply, `r` to refresh, and `Escape` or `Ctrl+C` to close. Navigating does not change the
runtime. Closing after submitting a selection does not cancel its application. The picker requires
at least 30 columns and 8 rows; a smaller terminal cannot apply a hidden selection.

For direct selection, use `/model <model> [effort|-]` or
`/model <provider>::<model> [effort|-]`. Custom model names pass through to the backend. `-` clears
an explicit effort. `/provider <name>` switches providers and restores that provider's defaults;
bare `/provider` reports the current provider. These commands are rejected while a run is active,
so they cannot become steering or follow-up messages.

Successful Rust selections update the live session and save user defaults for later launches.
Existing environment, CLI, and project settings keep their normal precedence over saved defaults.
If saving fails, the applied live selection remains active and a warning is shown. Catalog discovery
runs in the background; a failed catalog does not prevent prompts or typed model commands. If a
successful configuration cannot report its selection, the header marks the last confirmed selection
until a fresh catalog succeeds. Other command-workflow parity remains tracked in
[#467](https://github.com/whanyu1212/Wisp/issues/467).

Rust pickers, help, context, skills/MCP, prompt history, and retained tool details open over the
conversation. Output continues updating behind the popup; closing it preserves the draft and
scroll intent. Keys and paste go to the focused popup, and its editor owns the cursor. Escape closes
the popup (and cancels a device login when one is active). Existing Ctrl+C behavior remains: it
closes theme, help, history, context, discovery, model and connection views, while session/tree/detail views
retain the normal run-cancellation or idle-exit behavior.

Approvals and project-trust requests take precedence over popups. A refreshed selection must be
drawn before it can be activated. Popups are centered and capped at 100×28; at the minimum 30×8
terminal size they can fill the screen. Short decision layouts prioritize readable controls while
retaining conversation state. Device-login URLs and codes wrap; use arrow/Page keys or Home/End to
read longer challenges. Pointer navigation requires the opt-in described below.

The Rust composer supports `@` project-file references while idle or streaming. Type `@` at a token
boundary to open a composer-anchored popup, then type to fuzzy-filter the paths. `Up`/`Down` select;
`Enter` inserts a reference without submitting. `Tab` switches to a project tree without changing the
draft or query; `Left`/`Right` collapse/expand folders, and `Enter` toggles folders or inserts files.
Fuzzy mode also permits directory references. `Escape` closes the picker first, without cancelling
the run; `Tab` at a dismissed reference reopens it and refreshes discovery.

Only reference text is inserted: `@"src/example file.py"` for paths requiring JSON quoting, otherwise
`@src/main.rs`. No file content is read or inlined by Rust. Python supplies one bounded, protected-path-aware
snapshot per opening; typing and tree navigation use that snapshot locally. A policy change clears
the old choices before refresh, and late responses cannot reopen a dismissed picker. A limited-snapshot
cue means paths were omitted, not that a folder is empty. Fuzzy results are capped at 30; matching is
smart-case and deterministic, but ranking need not be identical to Textual. Queries over 4096 bytes
must be shortened. Discovery failures preserve the draft; close and reopen to retry.

The file popup is painted over the transcript, capped at 100 columns and 12 rows. In short terminals
it can cover the header or upper composer rows rather than rearranging the conversation. Approval and
trust controls take precedence. Below 30×8 no hidden selection can be inserted. Mouse selection is
opt-in, and modified submission shortcuts retain their existing meanings.

Rust supports `/theme` and `/theme <name>` with the same curated Vapor, Orchid, Ember, Storm, Grove,
Wave, Paper, and Dawn palettes as Textual. The picker previews with `Up`/`Down`, `PageUp`/`PageDown`,
or `Home`/`End`; `Enter` applies the displayed choice, while `Escape` or `Ctrl+C` restores the
committed theme. Streaming continues behind it. A new approval, trust request, or presented workflow
cancels the preview without saving it. `Ctrl+T` switches between Paper and the last committed dark
theme, including when starting from Dawn; it is ignored while the theme picker owns a preview.
These are local presentation actions, never prompts or runtime configuration commands.

Both frontends share `~/.wisp/tui.json` (`theme` and `last_dark_theme`). Choosing a theme in Rust also
sets the next Textual launch's preference, and vice versa. Rust preserves unrelated keys and writes
atomically. Missing, unknown, or unusable preferences fall back to Vapor; unreadable, non-UTF-8,
non-regular, or over-64-KiB documents are not overwritten. A save failure leaves the live selection
active and reports a warning; critical approval/cancellation recovery notices retain priority.
Presentation preferences never enter `settings.json`, RPC, or session history.

Set `NO_COLOR` before launching Rust for deterministic grayscale, including code, diffs, and popups.
The conversion starts with Textual's Rec.709 grayscale and minimally adjusts native foregrounds when
needed to retain a 4.5:1 contrast ratio against their rendered backgrounds. Selection uses reverse
video as well as a marker; status labels, approval action words, and diff `+`/`-` signs remain visible
without hue. The theme choice can still be changed and remembered while monochrome is active.

Rust mouse navigation is **off by default**. Enable it for a launch with:

```bash
WISP_TUI_MOUSE=1 wisp tui --renderer rust
```

`1`, `true`, and `on` enable capture (case-insensitive); unset, `0`, and other values leave it off.
This is Rust-local presentation state, not a backend setting or persisted preference.

- Wheel/trackpad reports over the conversation scroll three lines without moving composer focus.
  Reading earlier content remains anchored while output streams; paging uses the existing bounded
  history requests. Over an open popup, the wheel moves its selection or scrolls its report instead.
  It does not scroll the conversation behind the popup. The session tree requests its next page
  when wheeling past the last retained node.
- Left-click a visible picker row to select it. **Clicks do not activate choices**: use `Enter` to
  apply a model/theme, insert a file/skill reference, or navigate a session. Model selection remains
  locked while an application is pending. In the file tree, select a directory and use `Enter` or
  `Right` to expand it.
- A left click outside a popup dismisses it like `Escape`, including cancelling an active device
  login or rolling back a theme preview. That click is consumed, never passed to the background.
- With no popup open, click in the main composer to position its cursor at a grapheme boundary.
  Tabs, wide/combining characters, and horizontal/vertical editor scrolling retain their source
  positions. At the minimum 30×8 size, a tall draft keeps one editable row; header details may be
  omitted. Stale coordinates after resize, text replacement, or catalog refresh cannot select a
  new unseen target.

Approvals and project-trust decisions remain **keyboard-only**; mouse input is ignored during those
decisions and when a failed cancellation response requires keyboard recovery. Drag selection,
clipboard copy, horizontal wheel actions, and middle/right-click actions
are not implemented. Capture can interfere with terminal-native text selection: leave it off for
that workflow, or use your terminal's documented modifier override where supported. Only button and
SGR mouse reports are requested, not all-motion tracking; native and launcher cleanup restore the
terminal after exit or failure.

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
