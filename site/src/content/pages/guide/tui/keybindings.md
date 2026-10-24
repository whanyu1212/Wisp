---
title: TUI keys and keybindings
description: Keyboard shortcuts, composer editing keys, and custom keybinding configuration in ~/.wisp/settings.json.
route: guide/tui/keybindings.html
section: using
order: 0
layout: docs
listed: true
searchable: true
navTitle: Keys
parent: guide/tui.md
---

# TUI keys and keybindings {#tui-keybindings}

<div id="rust-keybinding-preferences"></div>

## Keybinding preferences {#keybinding-preferences}

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
| `queue.manage` | Unbound (`/queue`) | Inspect queues, change drain modes, restore or clear |
| `history.open` | Ctrl+R | Search submitted prompts |
| `theme.toggle` | Unbound (`/theme`) | Toggle Paper / last dark theme |
| `panel.toggle` | Ctrl+B | Show or hide the side panel |
| `panel.focus` | F8 | Focus the side panel; Esc returns to the composer |
| `session.tree` | Ctrl+T | Open the session tree to switch branches (`/tree`) |
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
custom application bindings. A bound theme toggle stays global except during theme preview. Binding
`theme.toggle` to Ctrl+T also needs `"session.tree": []`, since two actions cannot share a chord.
Ctrl+G opens read-only help for the current workflow, with resolved keys; Ctrl+G or Escape closes it
without closing the underlying workflow. Ctrl+C keeps that workflow's cancellation behavior.
Approval/trust requests preempt help, and positive decisions require the request to be visible.
Scroll help with arrows, PageUp/PageDown, Home/End, including at 30×8.

Only user settings supply this preference; project files are ignored even after trust. There is no
public environment or CLI keybinding override. The launcher resolves user settings once and passes a
private snapshot to Rust; the backend child does not inherit it. Bindings change frontend input only,
not runtime policy. Limits are 64 KiB of configuration, 64 entries, eight chords per action, and 64
characters per chord.

## Keybindings

| Key | Action |
|---|---|
| `Enter` | Submit; while a prompt runs, steer it; or activate the selected slash/file-picker item |
| `Alt+Enter` | While a prompt runs, queue a follow-up; otherwise insert a newline |
| `Alt+Up` | While a prompt runs, restore the newest queued item to the composer |
| `Ctrl+J` | Insert newline when a terminal cannot distinguish `Shift+Enter` from `Enter` |
| `Shift+Enter` | Insert newline in Rust when the terminal reports the modified key |
| `Tab` | Switch fuzzy/tree for an active file picker; complete an active slash command |
| `Up` / `Down` | Move through an active suggestion menu |
| `Left` / `Right` | Collapse/expand the selected directory in tree mode |
| `Shift+Tab` | Toggle plan/build mode |
| `Ctrl+T` | Open the session tree to switch branches (`/tree`) |
| `Ctrl+B` | Hide or show the side panel on a wide terminal; open or close it as an overlay otherwise |
| `F8` | Focus the side panel for keyboard navigation |
| `Ctrl+G` | Toggle contextual help for the focused Rust surface |
| `Ctrl+R` | Search prompt history for this TUI run |
| Mouse wheel / trackpad | Scroll the transcript without moving editor focus |
| `PageUp` / `PageDown` | Scroll the transcript by one page |
| `Ctrl+Home` / `Ctrl+End` | Traverse to the session beginning / return to the latest output |
| `Escape` | Dismiss nearest menu or overlay, then cancel an active prompt |
| `Ctrl+C` | Cancel the active workflow or quit when idle |
| `Ctrl+D` | Delete right; EOF only from an empty editor |


`Ctrl+G` and `/help` open the same native contextual guide. It follows focus across the editor, tool
cards, pickers, context reports, and safety decisions; its key reference is derived from live
bindings. The panel moves below the conversation on narrow terminals and never runs a tool, changes
the session, or resolves an approval.
