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

Unlike print mode, **the TUI exposes the full tool registry by default** — otherwise it would be a
chatbot that can't read files or run commands. Mutating and command tools still pause for approval:
approve once, allow that tool for the session, YOLO all mutating/command tools for the process (never
persisted), or deny.

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

## Keybindings

| Key | Action |
|---|---|
| `Enter` | Submit, or activate the selected slash/file-picker item |
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
| `Home` / `End` | Jump to the oldest loaded content / return to the latest output |
| `Escape` | Dismiss nearest menu or overlay, then cancel an active prompt |
| `Ctrl+C` | Copy selection; otherwise press twice within 1.5s to quit |
| `Ctrl+D` | Delete right; EOF only from an empty editor |

The Textual transcript has no visible scrollbar, but all transcript scrolling remains available
through the controls above. When new output arrives while you are reading earlier content, select the
`↓ new` indicator to return to the live tail.

`Ctrl+T` switches between Wisp's dark and light palettes. The choice is written to `~/.wisp/tui.json`
and restored on the next run. It is presentation state owned by the TUI client, so it is kept out of
`settings.json` and never reaches the agent subprocess; an unreadable or unrecognized value falls
back to the dark theme rather than failing to start.

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
```

On `--continue` or `--resume`, the TUI hydrates up to 500 active-path persisted messages through the
same RPC `get_messages` command available to other frontends before accepting input.

The Textual TUI targets truecolor terminals and degrades gracefully — 256-color and 16-color
terminals are handled by Textual's own detection. Setting `NO_COLOR` switches to deterministic
grayscale.

The legacy `--mode tui` entrypoint remains for compatibility and honors
`--tui-renderer line|fullscreen|textual` plus `WISP_TUI_RENDERER`.
