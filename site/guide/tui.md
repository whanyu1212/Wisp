# TUI

```bash
wisp
```

Wisp's fullscreen terminal clients share the same Python RPC controller.

> [!NOTE]
> **Frontend selection**
>
> `wisp`, `wisp tui`, and `wisp --mode tui` use `auto`: they select Rust when a native binary is
> installed on macOS/Linux, and the prompt-toolkit fullscreen renderer otherwise. Native wheels
> cover macOS arm64 and Linux glibc 2.28+ x86_64. Pure/source installs, Intel macOS, and other
> platforms use prompt-toolkit fullscreen by default. Explicit CLI selection takes precedence over
> `WISP_TUI_RENDERER`, which takes precedence over `auto`. `WISP_RUST_TUI_BINARY` also selects Rust
> in auto mode on macOS/Linux for source development. Missing or damaged declared binaries and Rust
> launch/runtime failures report an error; they never silently switch frontends.
>
> Use `wisp tui --renderer fullscreen` or `WISP_TUI_RENDERER=fullscreen` to select the Python
> fullscreen renderer explicitly. Both frontends use the same Python runtime, permissions,
> providers, and saved sessions.

## Rust TUI

Rust is the default for native-wheel installs. The prompt-toolkit fullscreen renderer remains
available on pure/source installs and by explicit selection.

Rust provides model selection, command discovery, context/compaction, skills/MCP, prompt history,
overlays, file completion, themes, mouse navigation, configurable bindings, keyboard selection,
undo/redo, composer clipboard actions, and compact paste presentation. The
[architecture guide](../architecture/rust-tui-boundary#subsystem-ownership) describes which process
owns each part of the interface.
Rust uses external update instructions, as described below.
Published-artifact validation and terminal/accessibility feedback remain release acceptance work;
the RC trial does not establish stable promotion.

During a backend output burst, Rust waits up to five seconds for inbound queue capacity while
continuing to give input and redraws turns. Sustained transport stalls and failed command writes
end the session with a diagnostic and terminal cleanup. A choice that changed while a key or mouse
input was waiting must be selected again; a redraw cannot apply that input to a replacement choice.

```bash
wisp tui --renderer rust
wisp --mode tui --tui-renderer rust
WISP_TUI_RENDERER=rust wisp
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
mode, model, context, and the selected session on the right, as space permits.
Live RPC and event-schema versions stay in Ctrl+G help. An empty transcript shows a centered startup
logo with the installed package version, invites a prompt or `/` commands, and points at `/resume`,
`/connect`, and `@` when there is room. It collapses to compact artwork and copy on short or narrow
terminals.

Assistant replies render Markdown during streaming and when loading session history: headings,
emphasis, links, lists, checklists, quotes, fenced code with syntax highlighting and continuous
backgrounds, and tables with
aligned columns, borders, and bold headers. Descriptions wrap at word boundaries inside their cells;
very narrow layouts stack cells within each row. Ordinary prose also wraps at word boundaries.
Very large unfinished blocks temporarily display as plain text and are formatted when the reply
completes. User prompts and tool output retain their literal text.

Replies that omit a table header are also supported: a paragraph beginning with at least two
complete, pipe-enclosed rows with the same number of columns renders as a table without a header.
Single rows, mismatched columns, and pipe syntax inside code remain literal.

Ctrl+G lists every resolved binding. Successful `edit` and `write` cards show a bounded inline diff
preview with the file, change counts, stable `+`/`-` gutters, and themed changed-row bands. Other tool
and process previews stay collapsed to an action line; consecutive `read` / `grep` / `find` / `ls`
cards group as `explored N files`. Thinking streams as a collapsed `thought` row. F6 browses visible
card rows: Right expands, Left collapses, Enter opens retained
detail. While following the tail, the latest user prompt stays pinned at the top of the conversation
pane until you scroll away. The live view contains only that prompt and the replies and tools that
follow it; older turns remain in scrollback. A clipped assistant reply keeps its `wisp` label visible.
Before reply text arrives, a working row animates below the transcript. Once text starts
streaming, the working row and spinner disappear. The `wisp` label stays visible at the live tail
during intervening tool calls, and the working row returns after the final result while another
model step is pending. A completed poll whose background process remains alive does not suppress
this separate model activity.
Compaction keeps its separate activity row. Active tool cards use a prominent solid-dot marker that
pulses gently in brightness and active shell calls say `Running`;
completed, failed, denied, cancelled, and approval-waiting calls stay steady. In monochrome mode,
active markers alternate normal and dim intensity. The footer keeps a static status label. A scrollbar on the right shows
the approximate position in retained history without laying out every offscreen row. Keyboard
scrolling and wheel/trackpad scrolling update it.
A pending tool approval opens a rounded dialog with four choices: `1` allow once, `2` allow
that tool for this session, `3` YOLO for this project, or `4` deny. The `y`/`t`/`a`/`n`
aliases also work. Project trust remains a compact card at the bottom of the pane
with `y`/`n` choices; the composer stays a short waiting
strip instead of a five-row args panel.

The Rust TUI negotiates and validates live RPC v8/event schema v39, supports prompts, approvals,
project trust, cancellation, steering and follow-up queues, a virtual Markdown/tool/diff transcript,
and complete saved session history.
`/resume` opens a picker for up to 50 persisted sessions (or accepts
one exact session ID); `/new` deselects the current session and clears the local transcript after the
backend confirms it. Startup and resumed history collect every transport page and build the
complete transcript once before enabling input. Conversation entries are retained without a history
cap; rendering caches and compact tool previews remain bounded. Large messages render in full as
plain text. Very large sessions therefore require more startup time and memory.

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

RC2 prepares native wheels for the two packaged targets. Transcript search and arbitrary drag
selection remain follow-ups; composer selection and clipboard actions are keyboard-driven.
See the mouse controls below; transcript copying still relies on terminal-native selection.

Rust also supports `/model` while idle. The picker groups models by provider, disables unavailable
providers, and labels preview and legacy models. Use `Up`/`Down`, `PageUp`/`PageDown`, or `Home`/`End`
to select a model, `Left`/`Right` to choose its reasoning effort (including the provider default).
The effort control stays within the model panel's border. Use `Enter` to apply, `r` to refresh, and
`Escape` or `Ctrl+C` to close. Navigating does not change the
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
successful configuration cannot report its selection, the footer marks the last confirmed selection
until a fresh catalog succeeds.

Rust pickers, help, context, skills/MCP, prompt history, and retained tool details open over the
conversation. Output continues updating behind the popup; closing it preserves the draft and
scroll intent. Keys and paste go to the focused popup, and its editor owns the cursor. Escape closes
the popup (and cancels a device login when one is active). Existing Ctrl+C behavior remains: it
closes logo, theme, help, history, context, discovery, model and connection views, while
session/tree/detail views retain the normal run-cancellation or idle-exit behavior.

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
smart-case and deterministic. Queries over 4096 bytes
must be shortened. Discovery failures preserve the draft; close and reopen to retry.

The file popup is painted over the transcript, capped at 100 columns and 12 rows. In short terminals
it can cover the header or upper composer rows rather than rearranging the conversation. Approval and
trust controls take precedence. Below 30×8 no hidden selection can be inserted. Mouse selection is
opt-in, and modified submission shortcuts retain their existing meanings.

Rust supports `/theme` and `/theme <name>` with curated Vapor, Glass, Orchid, Ember, Storm,
Grove, Wave, Paper, and Dawn palettes. Glass leaves the main canvas on the terminal's
default background, with smoky graphite surfaces and luminous ice, lilac, and mint accents. Configure
opacity, wallpaper, and blur in the terminal emulator; Wisp does not simulate those effects. The
picker previews with `Up`/`Down`, `PageUp`/`PageDown`,
or `Home`/`End`; `Enter` applies the displayed choice, while `Escape` or `Ctrl+C` restores the
committed theme. Streaming continues behind it. A new approval, trust request, or presented workflow
cancels the preview without saving it. `Ctrl+T` switches between Paper and the last committed dark
theme, including when starting from Dawn; it is ignored while the theme picker owns a preview.
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
video as well as a marker; status labels, approval action words, and diff `+`/`-` signs remain visible
without hue. The theme choice can still be changed and remembered while monochrome is active.

Rust mouse navigation is **on by default**, so wheel and trackpad scrolling can reach earlier
turns even though the live view starts at the current prompt. Disable it for a launch with:

```bash
WISP_TUI_MOUSE=0 wisp tui --renderer rust
```

Unset enables capture. `1`, `true`, and `on` also enable it (case-insensitive); `0`, `false`, `off`,
an empty value, and unknown values disable it. This is Rust-local presentation state, not a backend
setting or persisted preference.

- Wheel/trackpad reports over the conversation scroll three lines without moving composer focus.
  Reading earlier content remains anchored while output streams; paging uses the existing bounded
  history requests. Over an open popup, the wheel moves its selection or scrolls its report instead.
  It does not scroll the conversation behind the popup. The session tree requests its next page
  when wheeling past the last retained node.
- Left-click a visible picker row to select it. **Clicks do not activate choices**: use `Enter` to
  apply a model/theme/logo, insert a file/skill reference, or navigate a session. Model selection remains
  locked while an application is pending. In the file tree, select a directory and use `Enter` or
  `Right` to expand it.
- A left click outside a popup dismisses it like `Escape`, including cancelling an active device
  login or rolling back a theme preview. That click is consumed, never passed to the background.
- With no popup open, click in the main composer to position its cursor at a grapheme boundary.
  Tabs, wide/combining characters, and horizontal/vertical editor scrolling retain their source
  positions. At the minimum 30×8 size, a tall draft keeps one editable row; footer details may be
  omitted. Stale coordinates after resize, text replacement, or catalog refresh cannot select a
  new unseen target.

Approvals and project-trust decisions remain **keyboard-only**; mouse input is ignored during those
decisions and when a failed cancellation response requires keyboard recovery. Drag selection,
transcript clipboard copy, horizontal wheel actions, and middle/right-click actions are not implemented.
Capture can interfere with terminal-native text selection: set
`WISP_TUI_MOUSE=0` for that workflow, or use your terminal's documented modifier override where
supported. Only button and
SGR mouse reports are requested, not all-motion tracking; native and launcher cleanup restore the
terminal after exit or failure.

Selecting Rust never falls back to Python fullscreen. A missing/non-executable binary,
unsupported platform, package-version mismatch,
negotiation failure, or non-zero Rust exit is reported as an error. See
[Development setup](../contributing/development#rust-tui-scaffold), or select Python fullscreen
explicitly with `wisp tui --renderer fullscreen`.

The daily-use acceptance inventory for [#467](https://github.com/whanyu1212/Wisp/issues/467) is
recorded in the parity matrix.

### Rust keybinding preferences

Add `tui_keybindings` to your user `~/.wisp/settings.json`, alongside existing settings:

```json
{
  "tui_keybindings": {
    "prompt.submit": ["F3", "Ctrl+Enter"],
    "history.open": ["F4"],
    "transcript.browse": []
  }
}
```

Restart Wisp to apply changes. Missing actions inherit defaults; an array replaces all keys for that
one action. `[]` disables an optional action. `prompt.submit` and `prompt.newline` must each retain
at least one key. To recover, remove `tui_keybindings` and restart. Invalid shapes, unknown action
IDs, malformed chords, duplicate keys, and overlapping action bindings produce a warning and restore
the entire default keymap; other valid user settings still apply.

| Action ID | Default keys | Behavior |
|---|---|---|
| `prompt.submit` | Enter | Send a prompt; steer while running |
| `prompt.alternate_submit` | Alt+Enter | Newline while idle; follow-up while running |
| `prompt.newline` | Shift+Enter, Ctrl+J | Insert newline |
| `queue.restore` | Alt+Up | Restore newest queued draft |
| `history.open` | Ctrl+R | Search submitted prompts |
| `theme.toggle` | Ctrl+T | Toggle Paper / last dark theme |
| `transcript.browse` | F6 | Select transcript cards |
| `transcript.page_up`, `transcript.page_down` | PgUp, PgDn | Scroll one page |
| `transcript.home`, `transcript.tail` | Ctrl+Home, Ctrl+End | Oldest content / live tail |
| `transcript.line_up`, `transcript.line_down` | Ctrl+Up, Ctrl+Down | Scroll one line |

Chords accept case-insensitive `Ctrl`, `Alt` and `Shift`, an ASCII character, Enter, navigation keys,
or F1–F12, for example `Ctrl+F`, `Alt+Enter`, or `Ctrl+Shift+F3`. Printable keys require Ctrl or Alt.
Escape, Ctrl+C, Ctrl+G, Ctrl+A/E, editor selection, clipboard, and word/line editing keys listed below,
unmodified editor arrows/Home/End, and Tab/BackTab/Backspace/Delete are reserved.
Default Ctrl+Enter submits; other combined Enter modifiers preserve legacy newline
behavior. Default Ctrl+J and Ctrl+navigation accept extra modifiers, except that Shift+navigation
selects text while editing the composer. Replacing an action removes
these inherited aliases too. Some terminals cannot distinguish every modified chord; use a function
key if your chosen combination does not arrive distinctly.

The Rust composer supports these fixed editing keys:

| Keys | Behavior |
|---|---|
| Shift+arrows, Shift+Home/End | Extend selection by character, line, or to a line edge |
| Ctrl+Shift+Home/End | Extend selection to the start/end of the draft |
| Alt+A | Select the whole draft; Ctrl+A retains line-start behavior |
| Alt+Home/End | Move to the start/end of the draft |
| Ctrl/Alt+Left/Right, Alt+B/F | Move across a Unicode word or punctuation segment, skipping whitespace; add Shift to select |
| Ctrl+W, Ctrl/Alt+Backspace | Delete backward to the word boundary |
| Alt+D, Ctrl/Alt+Delete | Delete forward to the word boundary |
| Ctrl+U/K | Delete to line start/end; at that edge, remove the adjacent newline |
| Ctrl+Z | Undo the latest edit group |
| Ctrl+Y, Ctrl+Shift+Z | Redo the latest undone edit group |
| Ctrl+C, Ctrl+Insert | Copy selected composer text |
| Ctrl+X, Shift+Delete | Cut selected composer text as one undoable edit |
| Ctrl+V, Shift+Insert | Paste system-clipboard text, replacing the selection |

Undo and redo retain at most 100 states and 4 MiB of draft text in each direction. Consecutive
typing and repeated character deletion are grouped; paste, completion, file insertion, prompt-history
restoration, and queued-draft restoration are individual steps. Sending or clearing a draft starts a
new editing session and clears this process-local history.

Typing, pasting, Tab, and newline insertion replace the selection. Backspace and Delete remove it.
Unmodified Left/Right collapse the selection to its start/end; a composer click clears it.
Explicitly selecting a folded paste selects its exact underlying text, and replacement removes that
selected text in one edit. Without a selection, a destructive edit into a paste marker expands it first.
Rejected oversized edits preserve the draft and selection. Submission sends the entire draft.
Ctrl+C copies when the composer owns a non-empty selection; without one it retains run-cancellation
and idle-exit behavior. A failed cut preserves the selected draft, and clipboard paste follows the
same control-character filtering, size limits, compact-fold handling, and undo behavior as bracketed
paste. Native desktop clipboard access supports copy, cut, and paste; copy also emits an OSC52 fallback
for remote terminals. Mouse drag selection and transcript clipboard copy remain separate work.

Focused controls retain their local keys: picker arrows/Enter, completion Tab/Enter, card browsing
Tab/Shift+Tab/Enter/Space, decision approval/denial, and editor navigation. They take precedence over
custom application bindings. The existing Ctrl+T toggle remains global except during theme preview.
Ctrl+G opens read-only help for the current workflow, with resolved keys; Ctrl+G or Escape closes it
without closing the underlying workflow. Ctrl+C keeps that workflow's cancellation behavior.
Approval/trust requests preempt help, and positive decisions require the request to be visible.
Scroll help with arrows, PageUp/PageDown, Home/End, including at 30×8.

Only user settings supply this preference; project files are ignored even after trust. There is no
public environment or CLI keybinding override. The launcher resolves user settings once and passes a
private snapshot to Rust; the backend child does not inherit it. Bindings change frontend input only,
not runtime policy. Limits are 64 KiB of configuration, 64 entries, eight chords per action, and 64
characters per chord. The Python fullscreen renderer keeps its own keybindings.

### Rust large pastes

A paste over 2,000 Unicode characters is displayed as a numbered marker with character, line, and
byte counts. Ordinary text around it remains editable. Moving into the hidden range or editing at
its boundary first expands it; repeat the action to edit the revealed text. Mouse cursor placement
uses the displayed marker. At most 64 folds are retained; further pastes stay inline.

The raw draft remains subject to the existing 1 MiB and 10,000-line limits. Submission, steering,
follow-up, history search, and queue recovery use exact raw text. Compact live transcript echoes are
local and bounded (32 presentations / 4 MiB each for queued and transcript metadata); eviction or
historical replay displays raw content. Markers are never written into session history.

### Rust updates

`/update`, `/update check`, and `/update install` open scrollable external instructions and preserve
the draft. They do not check the network, install, quit, or restart. Finish work and quit, then run
`wisp update --check` in a shell. Eligible uv tool installations can use `wisp update`; source
installations should follow the [development guide](../contributing/development#rust-tui-scaffold).
Rebuild or select a Rust binary matching the updated Python package before relaunching.
Automatic notices, binary installation, rollback, and coordinated restart remain distribution work
under [#469](https://github.com/whanyu1212/Wisp/issues/469).

Unlike print mode, **fullscreen TUI modes expose the full tool registry by default**. Mutating and
command tools still pause for approval: approve once, allow that tool for the session, choose a saved
project YOLO default, or deny.

Use `/permissions` in Rust to inspect and change the project default, or `/permissions ask` and
`/permissions yolo` in either TUI. Saved defaults apply to subsequent launches in the same canonical
project directory and live in user-owned `~/.wisp/permissions/` files, outside the repository.
Allow-once and tool-session choices do not persist; temporary session grants expire on `/new` or
switching to another session. Changing the default clears temporary grants. Startup `--yes` alone
is not saved. These choices do not change project trust or protected paths.

## Steering and follow-ups

The composer remains active while a prompt runs. In the prompt-toolkit fullscreen and Rust TUIs:

- `Enter` sends a steering message for the active run. It is injected at the next safe request
  boundary, after any current assistant/tool batch.
- `Alt+Enter` queues follow-up work that starts when the active run would otherwise finish.
- `Alt+Up` removes the newest queued steering or follow-up message and restores it ahead of the
  current draft, after the shared runtime confirms the queue change.
- `Escape` cancels the active prompt in the Python fullscreen TUIs. The Rust TUI accepts either
  `Escape` or `Ctrl+C`. Cancellation does not discard runtime-owned queued messages.

A bounded queue panel previews up to three items and labels them `steer` or `later`; an omitted-item
count indicates when more are queued. Python fullscreen TUIs report separate steering and follow-up
totals in the footer; Rust shows them in its footer and composer. Python returns failed submissions to
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
/logo [name]                preview or select a startup logo
/update [check|install]     check immediately or explicitly install an update
/skills                     inspect loaded skills and discovery diagnostics
/mcp                        show configured MCP servers and registered tools
/quit, /exit
```

`/init` asks the active model to inspect repository documentation, manifests, CI configuration, and
source layout before creating project-specific guidance. It only works in build mode, uses the normal
project-trust and write-approval flow, and refuses to replace an existing `AGENTS.md` or `AGENTS.MD`.
The final write is create-only, so a file that appears during inspection is preserved. The Rust TUI
offers `/init` only when the backend includes it in command discovery and delegates the complete
workflow to that backend.

## Completions and the file picker

Type `/` to filter commands inline. Type `@` to reference a project file. The picker starts in fuzzy
mode and matches loosely, so `@rust` finds `src/wisp/tui/rust_launcher.py`; press `Tab` to switch to a
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
| `Shift+Enter` | Insert newline |
| `Tab` | Switch fuzzy/tree for an active file picker; complete an active slash command |
| `Up` / `Down` | Move through an active suggestion menu |
| `Left` / `Right` | Collapse/expand the selected directory in tree mode |
| `Shift+Tab` | Toggle plan/build mode |
| `Ctrl+T` | Switch between the light and dark themes (remembered across runs) |
| `Ctrl+G` | Toggle contextual help for the focused Rust surface |
| `Ctrl+R` | Search prompt history for this TUI run |
| Mouse wheel / trackpad | Scroll the transcript without moving editor focus |
| `PageUp` / `PageDown` | Scroll the transcript by one page |
| `Ctrl+Home` / `Ctrl+End` | Traverse to the session beginning / return to the latest output |
| `Escape` | Dismiss nearest menu or overlay, then cancel an active prompt |
| `Ctrl+C` | Cancel the active workflow or quit when idle |
| `Ctrl+D` | Delete right; EOF only from an empty editor |

The Rust transcript retains the complete saved conversation. When new output arrives while you are
reading earlier content, the viewport stays anchored; press `Ctrl+End` to return to the live tail.

### Resuming long sessions

Selecting a session from `/resume`, or running `/resume <session-id>`, loads the complete saved
active-path transcript before input resumes. `PageUp` and `PageDown` scroll through the retained
conversation; no history cap clips older messages. A failed or stale history response reports an
error rather than presenting a partial replacement as complete.

Historical file-tool cards keep bounded previews. Press `F6` to browse visible cards and `Enter` to
open detail; when a persisted preview was clipped, the Rust TUI fetches that one exact result on
demand and releases it when the detail view closes. It does not cache historical detail or read JSONL
directly, and cannot recover bytes that the tool truncated before persistence.

Every persisted message row is represented, but representation is logical rather than one widget per
JSONL row. A tool request and its result share one tool card. Repeated process start, poll, cancel,
and completion rows for the same process share one process card; its header reports the poll count.
Use `F6` to browse visible cards, `Left`/`Right` to collapse or expand one, and `Enter` to open its
retained detail.

This deliberately trades `/resume` cold-start time and metadata memory for reliable upward scrolling.
Output bodies and tool arguments use bounded previews during initial loading. An on-demand detail
load returns the exact text stored in JSONL; it cannot recover bytes truncated before persistence.

Run `/theme` to preview Vapor, Glass, Orchid, Ember, Storm, Grove, Wave, Paper, and Dawn, or pass one
of those names directly. `Ctrl+T` switches between Paper and the most recently selected dark palette;
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
wisp tui --renderer rust                 # explicitly select Rust
wisp tui --renderer fullscreen           # explicitly select Python fullscreen
```

Rust loads the complete saved active path at startup and on `/resume`. The Python fullscreen and line
renderers retain their own paging behavior. Set `NO_COLOR` to request grayscale presentation.

The legacy `--mode tui` entrypoint remains for compatibility and honors
`--tui-renderer line|fullscreen|rust` plus `WISP_TUI_RENDERER`.
