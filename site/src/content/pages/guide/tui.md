---
title: TUI basics
description: Start Wisp in your terminal, compose prompts, steer work, and customize its appearance.
route: guide/tui.html
section: using
order: 22
layout: docs
listed: true
searchable: true
---

# TUI basics {#tui}

Choose a focused guide for common terminal tasks:

- [TUI keys and keybindings](./tui/keybindings.md) — Keyboard shortcuts, editing keys, and user keybinding configuration.
- [TUI commands](./tui/commands.md) — Slash commands, provider and model selection, permissions, and MCP management.
- [TUI sessions and scrollback](./tui/sessions.md) — Resume sessions, navigate branches, browse tool details, and search prompt history.

```bash
wisp
```

The Rust TUI uses the Python RPC backend for agent behavior, tools, permissions, and sessions.

> [!NOTE]
> **TUI availability**
>
> `wisp`, `wisp tui`, and `wisp --mode tui` launch Rust.
> Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the binary. Pure-wheel installs
> retain print, JSON, RPC, and SDK use, but interactive startup reports how to obtain a native wheel
> or build a matching binary. Source checkouts set the absolute `WISP_RUST_TUI_BINARY` path after
> building Rust; see [Development setup](../contributing/development#rust-tui-scaffold).

## TUI {#rust-tui}

Rust is the only interactive terminal renderer.

Rust provides model selection, command discovery, context/compaction, skills/MCP, prompt history,
overlays, file completion, themes, mouse navigation, configurable bindings, keyboard selection,
undo/redo, composer clipboard actions, and compact paste presentation. The
[architecture guide](../architecture/rust-tui-boundary#subsystem-ownership) describes which process
owns each part of the interface.
Rust uses external update instructions, as described below.

During a backend output burst, Rust waits up to five seconds for inbound queue capacity while
continuing to give input and redraws turns. Sustained transport stalls and failed command writes
end the session with a diagnostic and terminal cleanup. A choice that changed while a key or mouse
input was waiting must be selected again; a redraw cannot apply that input to a replacement choice.

```bash
wisp tui
wisp --mode tui
```

The conversation uses the terminal width with modest side margins. User turns have a subtle
background with padding above the speaker label and below the message; assistant prose and collapsed tool rows stay open on the transcript background.
The transcript has space at the top and above the composer, collapsing on short terminals.
The composer shares the transcript background, with a thin rounded frame, a `›` prompt, and a hint when
empty. Long logical lines soft-wrap at word boundaries, falling back to safe hard wrapping for long
tokens, without adding newlines to the submitted prompt. The composer grows to its bounded height, then
keeps the cursor's wrapped row visible. The frame
collapses on short terminals to preserve editing space. The footer separates
the keys for the current workflow on the left from status (`idle`, `working`, `approval`, `trust`),
mode, model, and context on the right, as space permits.
The backend version stays in Ctrl+G help. An empty transcript shows a centered startup
logo with the installed package version, invites a prompt or `/` commands, and points at `/resume`,
`/connect`, and `@` when there is room. It collapses to compact artwork and copy on short or narrow
terminals.

Wisp's default instructions favor the answer or outcome first, short paragraphs, and a few useful
bullets. Progress updates focus on findings, blockers, and next steps; final replies summarize related
checks while retaining failures and uncertainty. Ask for a detailed explanation or walkthrough when
you need more depth. These writing defaults apply wherever Wisp uses its built-in prompt, including
the TUI and print mode; they do not truncate responses or rewrite existing history.

Assistant replies render Markdown during streaming and when loading session history: headings,
emphasis, links, lists, checklists, quotes, fenced code with syntax highlighting and continuous
backgrounds, and tables with
aligned columns, borders, and bold headers. Descriptions wrap at word boundaries inside their cells;
very narrow layouts stack cells within each row. Ordinary prose wraps at word boundaries within
an 88-column reading width. Paragraphs and headings have one blank line between them; top-level
list items have the same breathing room, while nested lists stay compact. Wrapped list text aligns
beneath its content, including numbered items and checkboxes. Code and tables retain the available
transcript width. These are display changes: the stored response retains its original Markdown, while mouse
selection copies the painted rows, including their spacing.
Very large unfinished blocks temporarily display as plain text and are formatted when the reply
completes. User prompts and tool output retain their literal text.

Providers send replies in bursts, so the TUI reveals streamed text at an even pace instead of as
each burst arrives. Text shows a word at a time, about a tenth of a second behind the stream, and
an unfinished `**bold**`, `` `code` `` or `~~strike~~` span, table row, or code fence line waits
until it is complete, so raw markers do not flash before the formatting applies. When the stream
pauses or the reply ends, waiting text is shown, and a tool card or prompt that follows the reply,
the end of the run, or a closed connection first shows all of it. More than 16 KiB of waiting
text is shown at once rather than animated. Set `WISP_TUI_STREAM_PACING=0` to show text as it arrives. To measure a
provider's stream, set `WISP_TUI_STREAM_TRACE` to a file path: each text delta appends a
tab-separated line of milliseconds since the first delta, milliseconds since the previous one,
and its size in bytes.

Replies that omit a table header are also supported: a paragraph beginning with at least two
complete, pipe-enclosed rows with the same number of columns renders as a table without a header.
Single rows, mismatched columns, and pipe syntax inside code remain literal.



## Approvals and trust

A pending tool approval docks a panel above the footer in place of the composer, so the
conversation that led to it stays visible; your draft is kept. The title names the tool, the
safety category sits at the top right, and the body shows the built-in `bash` tool's command as
`$ command`, with any other arguments after it. Every other tool, including MCP tools with a
`command` argument, shows its complete arguments. It offers four choices: `1` allow
once, `2` allow that tool for this session, `3` YOLO for this project (YOLO for this session in
the home folder or a folder above it, where YOLO is never saved), or `4` deny. The
`y`/`t`/`a`/`n` aliases also work, or highlight a choice with `↑`/`↓` and press `Enter`. There is
no default choice: `Enter` only confirms a highlighted choice that has been drawn, so an `Enter`
meant for the composer cannot answer a request. `Esc` denies. Project trust uses the same panel
with `y` trust and `n` deny. Below 11 rows the panel fills the screen so every choice stays
visible down to 30×8.



## Panels and popups

Rust pickers, help, context, skills/MCP, prompt history, and retained tool details open over the
conversation. Output continues updating behind the popup; closing it preserves the draft and
scroll intent. Keys and paste go to the focused popup, and its editor owns the cursor. Escape closes
the popup (and cancels a device login when one is active). Existing Ctrl+C behavior remains: it
closes logo, theme, help, history, context, discovery, model and connection views, while
session/tree/detail views retain the normal run-cancellation or idle-exit behavior.

Approvals and project-trust requests take precedence over popups. A refreshed selection must be
drawn before it can be activated. Popups are centered and capped at 100×28, except `/permissions`
and the `/mcp` add, enable, and remove panels, which dock above the composer; at the minimum 30×8
terminal size they can fill the screen. Short decision layouts prioritize readable controls while
retaining conversation state. Device-login URLs and codes wrap; use arrow/Page keys or Home/End to
read longer challenges. Pointer navigation requires the opt-in described below.



## Project file references

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
smart-case and deterministic. Queries over 4096 bytes
must be shortened. Discovery failures preserve the draft; close and reopen to retry.

The file popup is painted over the transcript, capped at 100 columns and 12 rows. In short terminals
it can cover the header or upper composer rows rather than rearranging the conversation. Approval and
trust controls take precedence. Below 30×8 no hidden selection can be inserted. Mouse selection is
opt-in, and modified submission shortcuts retain their existing meanings.



## Themes and appearance

Rust supports `/theme` and `/theme <name>` with curated Vapor, Glass, Orchid, Ember, Storm,
Grove, Wave, Paper, and Dawn palettes. Paper is the main light theme (`wisp-light` internally), with Dawn as a warmer light alternative. Each palette uses coordinated diff backgrounds and word highlights; user panels and tool chips stay subdued. Glass leaves the main canvas on the terminal's
default background, with smoky graphite surfaces and luminous ice, lilac, and mint accents. Configure
opacity, wallpaper, and blur in the terminal emulator; Wisp does not simulate those effects. The
picker previews with `Up`/`Down`, `PageUp`/`PageDown`,
or `Home`/`End`; `Enter` applies the displayed choice, while `Escape` or `Ctrl+C` restores the
committed theme. Streaming continues behind it. A new approval, trust request, or presented workflow
cancels the preview without saving it. The `theme.toggle` binding (unbound by default) switches
between Paper and the last committed dark theme, including when starting from Dawn; it is ignored
while the theme picker owns a preview.
These are local presentation actions, never prompts or runtime configuration commands.

Run `/logo` to preview and select Random, Classic WISP, Wisp Braille, Adal Braille, or Adal Mark
Braille. You can also select one directly, such as `/logo wisp-braille` or
`/logo adal-mark-braille`. Arrow, Page, Home, and End keys move through the
picker; `Enter` applies the displayed logo, and `Escape` or `Ctrl+C` closes it. The choice updates an
empty welcome screen immediately and applies to later new-session welcome screens. Random chooses
one named logo once per process, so terminal redraws and resizes do not change it. The Adal variants
render as pink artwork over the terminal background.

Rust stores theme preferences in `~/.wisp/tui.json` (`theme` and `last_dark_theme`).
It preserves unrelated keys and writes
atomically. Missing, unknown, or unusable preferences fall back to Vapor; unreadable, non-UTF-8,
non-regular, or over-64-KiB documents are not overwritten. A save failure leaves the live selection
active and reports a warning; critical approval/cancellation recovery notices retain priority.
Presentation preferences never enter `settings.json`, RPC, or session history.
Rust stores the startup-logo choice as `startup_logo` in this file; a missing or unknown value uses
Random.

Set `NO_COLOR` before launching Rust for deterministic grayscale, including code, diffs, and popups.
The conversion uses Rec.709 grayscale and minimally adjusts native foregrounds when
needed to retain a 4.5:1 contrast ratio against their rendered backgrounds. Selection uses reverse
video as well as a marker; status labels, approval action words, shape-coded tool markers, and diff
`+`/`-` signs remain visible without hue. The theme choice can still be changed and remembered while monochrome is active.



## Startup errors

A missing or non-executable binary, unsupported platform, package-version mismatch,
negotiation failure, or non-zero Rust exit is reported as an error. For source checkouts, see
[Development setup](../contributing/development#rust-tui-scaffold). Print, JSON, RPC, and SDK
interfaces remain available on pure-wheel installs.

The daily-use acceptance inventory for [#467](https://github.com/whanyu1212/Wisp/issues/467) is
recorded in the parity matrix.

### large pastes {#rust-large-pastes}

A paste over 2,000 Unicode characters is displayed as a numbered marker with character, line, and
byte counts. Ordinary text around it remains editable. Moving into the hidden range or editing at
its boundary first expands it; repeat the action to edit the revealed text. Mouse cursor placement
uses the displayed marker. At most 64 folds are retained; further pastes stay inline.

The raw draft remains subject to the existing 1 MiB and 10,000-line limits. Submission, steering,
follow-up, history search, and queue recovery use exact raw text. Compact live transcript echoes are
local and bounded (32 presentations / 4 MiB each for queued and transcript metadata); eviction or
historical replay displays raw content. Markers are never written into session history.

### updates {#rust-updates}

`/update`, `/update check`, and `/update install` open scrollable external instructions and preserve
the draft. They do not check the network, install, quit, or restart. Finish work and quit, then run
`wisp update --check` in a shell. Eligible uv tool installations can use `wisp update`; source
installations should follow the [development guide](../contributing/development#rust-tui-scaffold).
Rebuild or select a Rust binary matching the updated Python package before relaunching.
Automatic notices, binary installation, rollback, and coordinated restart remain distribution work
under [#469](https://github.com/whanyu1212/Wisp/issues/469).


## Steering and follow-ups

The composer remains active while a prompt runs:

- `Enter` sends a steering message for the active run. It is injected at the next safe request
  boundary, after any current assistant/tool batch.
- `Alt+Enter` queues follow-up work that starts when the active run would otherwise finish.
- `Alt+Up` removes the newest queued steering or follow-up message and restores it ahead of the
  current draft, after the shared runtime confirms the queue change.
- `Escape` or `Ctrl+C` cancels the active prompt. Cancellation does not discard runtime-owned
  queued messages.

A bounded queue panel previews up to three items and labels them `steer` or `later`; an omitted-item
count indicates when more are queued. Rust shows steering and follow-up totals in its footer and
composer. Failed submissions remain recoverable drafts: `Alt+Up` restores one ahead of the current
draft. The Rust TUI clears a submitted draft only after the JSONL writer flushes it, refreshes queue
state after startup and session changes, and reports queued or recovering text as unsent if the
transport closes.

Use `/queue` to inspect both queues, their FIFO previews, and their drain modes. The optional
`queue.manage` action can be bound without replacing the current composer draft, for example
`{"queue.manage": ["F7"]}` in the existing keybinding configuration. The overlay supports arrows,
Home/End, Enter, and opt-in mouse navigation. Escape closes it; Ctrl+C retains cancellation behavior.

- **One at a time** drains one message per eligible boundary. **All** drains the batch selected at
  that boundary; messages added later wait for a subsequent boundary.
- **Restore newest** acts on the selected queue, unlike Alt+Up's cross-queue newest-item selection.
  Restored text is prepended to the current draft, never automatically submitted or silently truncated.
- **Clear this queue** and **Clear both queues** require a scoped confirmation, defaulting to Cancel.
  A changed snapshot invalidates the old choice. Modes and contents change only after backend events.
- Mutations require an active editable run and a backend snapshot token. Retained queues after a run
  are inspectable but read-only. A stale request fails without removing a replacement item; refresh
  and review the new state before retrying. Missing token support never falls back to an unguarded pop.

Opening the overlay does not change queue policy or pause draining. These queues are process-local,
not persistent work across restarts. Predictable frame-limit failures preserve queued text; connection
loss can leave a command outcome unknown, so destructive operations are not retried automatically.

## Completions and the file picker

Type `/` to filter commands inline. Type `@` to reference a project file. The picker starts in fuzzy
mode and matches loosely, so `@rust_launcher` finds
`src/wisp/cli/native_tui/rust_launcher.py`; press `Tab` to switch to a project tree without changing
the draft or query, and press `Tab` again to return.

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

Run `/theme` to preview Vapor, Glass, Orchid, Ember, Storm, Grove, Wave, Paper, and Dawn, or pass one
of those names directly. A bound `theme.toggle` switches between Paper and the most recently selected
dark palette; from Dawn it returns to that dark palette too. The choice is written to `~/.wisp/tui.json` and
restored on the next run. It is presentation state owned by the TUI client, so it is kept out of
`settings.json` and never reaches the agent subprocess; an unreadable or unrecognized value falls
back to Vapor rather than failing to start.


### Side panel

On a terminal at least 148 columns wide, the Reading sidebar shows the session name and status,
folder and branch, compact context usage, named MCP servers, skills, changed files, and the three
most recent turns. The latest turn is highlighted; permissions stay at the bottom. Model and effort
remain in the composer. Context's **Breakdown** expands source totals and costs; `/context` opens
full details.

**MCP servers** and **Skills** start expanded. MCP rows show connection status and expand to tool
counts, errors, and `/mcp` management. Skill rows show **available** or **used**, and expand to their
description and source. Used means a skill was successfully loaded by the skill tool or an explicit
`/skill:name` invocation on the current session branch. Usage refreshes with session statistics after
an operation, survives resume and compaction, and excludes failed calls and sibling-only usage.
Project skills remain subject to trust. Empty, loading, and unavailable inventories are distinguished.
Click either section heading to collapse it, or a member row to expand its details.

The panel starts at 44 columns and grows to at most 52 automatically. Drag its left border to resize
it while leaving at least 104 columns for the conversation; double-click the border to restore the
automatic width. The chosen width lasts until Wisp exits. `Ctrl+B` hides or shows it. On narrower
terminals, `Ctrl+B` opens a 44-column overlay above the composer.

`F8` focuses the sidebar at either width. `Tab` and `Shift+Tab` move between sections, arrow keys
move among actionable rows, and `Enter` or `Space` expands details or opens the selected view.
`Esc` returns to the composer without changing its draft. `PageUp`/`PageDown`, or the mouse wheel
over the panel, scroll the panel independently. Keyboard navigation brings offscreen rows into view.
The bottom-right position indicator appears when the panel has more content below or above.

Changed files show line counts and retain `◆` for files Wisp edited on this branch. Selecting an
edited file jumps to its edit card; other files open `/diff`. File snapshots refresh when the panel
appears and after each run, except while `/diff` is open. The Recent turns heading opens `/tree`;
`Ctrl+T` also opens the session tree. While working or awaiting approval, the current activity and
queue lead the panel. On terminals at least 160 columns wide, Edit and Write cards still show old
and new content side by side.

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
wisp --mode tui --provider anthropic     # top-level flags such as --provider also work
```

Rust loads the complete saved active path at startup and on `/resume`. Set `NO_COLOR` to request
grayscale presentation.

`wisp --mode tui` launches the same TUI and accepts the top-level options, including `--provider`
and `--model`.

### Provider-native web search

Use `/search` to show the current setting and `/search on` or `/search off` to
change it for this running session. Wait for the current operation to finish
before changing it. The toggle applies to subsequent prompts, does not save user
defaults, and does not change separately configured MCP search tools such as Exa.
Persistent defaults remain available through the user-only `web_search` setting
or `WISP_WEB_SEARCH`.

When enabled, supported providers can choose to search; not every request will
search. Google Search grounding is not yet supported. Search queries, opened pages,
within-page searches, and failures appear with the reply, with bounded source
titles and hosts. This metadata is restored when loading saved replies.

Reply costs marked `cost ≥…` exclude provider search fees and are lower bounds,
not complete prices. An unavailable token estimate is shown as `cost unknown`,
except on responses containing only tool calls and no text, where the label is
omitted to avoid a standalone placeholder before the tools. Usage and cost
metadata are still recorded.

<section data-pagefind-ignore="" aria-label="Moved TUI sections"><h2 id="more-tui-tasks">More TUI tasks</h2><ul><li><a href="/Wisp/guide/tui/keybindings.html">TUI keys and keybindings</a></li><li><a href="/Wisp/guide/tui/commands.html">TUI commands</a></li><li><a href="/Wisp/guide/tui/sessions.html">TUI sessions and scrollback</a></li></ul><p>Previously linked sections now live on focused pages:</p><ul><li id="rust-keybinding-preferences"><a data-tui-destination="" href="/Wisp/guide/tui/keybindings.html#keybinding-preferences">keybinding preferences</a></li><li id="managing-mcp-servers"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#managing-mcp-servers">Managing MCP servers</a></li><li id="slash-commands"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#slash-commands">Slash commands</a></li><li id="keybindings"><a data-tui-destination="" href="/Wisp/guide/tui/keybindings.html#keybindings">Keybindings</a></li><li id="resuming-long-sessions"><a data-tui-destination="" href="/Wisp/guide/tui/sessions.html#resuming-long-sessions">Resuming long sessions</a></li><li id="prompt-history"><a data-tui-destination="" href="/Wisp/guide/tui/sessions.html#prompt-history">Prompt history</a></li><li id="context-skills-and-mcp-browsers"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#context-skills-and-mcp-browsers">Context, skills and MCP browsers</a></li></ul></section>
