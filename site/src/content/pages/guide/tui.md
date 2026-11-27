---
title: TUI basics
description: Work in Wisp's Rust terminal workspace—editing turns, per-turn /diff, Session | Files | Browser side panel, local ! shell, docked approvals, and live steering.
route: guide/tui.html
section: using
order: 4
layout: docs
listed: true
searchable: true
navGroup: Terminal UI
capture: working
---

# TUI basics {#tui}

`wisp` launches the Rust terminal workspace over Wisp's shared Python RPC runtime:

```bash
wisp
```

Jump to a focused TUI page for full command and key tables:

- [TUI keys and keybindings](./tui/keybindings.md) — Keyboard shortcuts, composer editing keys, external editor (`Ctrl+O`), and `tui_keybindings` customization.
- [TUI commands](./tui/commands.md) — Slash commands, `/connect`, `/model`, `/permissions`, `/codemode`, `/subagents`, `/mcp`, and live `/browser` takeover.
- [TUI sessions and scrollback](./tui/sessions.md) — Inline and working-tree `/diff`, tool detail browsing (`F6`), `/resume`, `/tree`, `/cd`, mouse controls, and prompt history.

## An editing turn in the workspace {#rust-tui}

Start Wisp in a project (`wisp`, `wisp tui`, or `wisp --mode tui`). On an empty session, the
welcome card lets you pick `Connect a provider`, `Resume a session`, `Browse commands`, or
`Mention a project file` with arrows or a click; start typing at any time to write directly in the
composer.

When you ask Wisp to make a change, the workspace keeps the turn readable end to end:

- **Composer and live transcript:** Your prompt sits in a rounded `›` composer whose bottom border
  shows the active model, reasoning effort, and mode (`claude-opus-5-5 (high) · build`; amber in
  `plan` mode, and a warning label when `YOLO` is active). Assistant Markdown streams into a
  `100`-column reading measure while code blocks and tables use the full transcript width, with
  clickable OSC 8 file paths and `http`/`https` links.
- **Calm stream pacing:** Providers deliver tokens in uneven bursts, so Wisp reveals text at a
  steady word pace and holds back unfinished inline Markdown markers (like `**bold**`, `` `code` ``,
  table rows, or code fences) until complete. That prevents jitter and raw syntax flashes, then
  flushes immediately on pauses, tool cards, or turn completion (`WISP_TUI_STREAM_PACING=0` disables
  pacing).
- **Truthful tool labels:** Every tool call reads as a concise action line with a status mark (`•`
  done, `✗` failed, `⊘` denied/cancelled, or a spinner while running) that distinguishes what was
  requested from what actually happened—for example `Searched filenames for *.rs in src` vs.
  `Searched text for "TODO" in src`, `Proposed disabling MCP server linear` (a request, not an
  applied change), or `Edit failed: src/main.rs · stale input`. Consecutive reads, repeated
  commands, and repeated MCP calls fold cleanly, while failures and retries stay explicit instead of
  being hidden by a later success. Codemode scripts and subagent delegations keep their task title
  and nested call counts (`F6` + `→` or a click expands any card; `Enter` opens full detail).
- **Per-turn change summary and `delta`-style `/diff` (`Alt+G`):** Every `edit` or `write` card
  shows a syntax-highlighted inline diff preview, and the bottom of the turn rail summarizes the
  turn's net change (`╰ +N −M in K files`). Press `Alt+G` (or click / press `Enter` on the turn
  summary row) to open `/diff` filtered to that turn's edits—complete with hunk headers, syntax and
  word-level highlights, folded unchanged regions, and side-by-side split view on wide terminals.
  Run `/diff` directly for the full working tree against `HEAD`.
- **Two-row telemetry deck footer:** Below the composer, a two-row deck tracks live state at a
  glance:
  - **Row 1:** run state badge (`• idle`, `● working`, `● approval`, `● trust`, or `✗ error`),
    current folder and Git branch, MCP health (`2 MCP` or `1 MCP off`), active background processes
    and subagents (`1 bg · 1 sub`), plus queue counts or active notices on the right.
  - **Row 2:** context window meter with token ratio, session cost, non-default transcript
    verbosity (`verbosity: low` / `verbosity: high`), and contextual keycap hints.

## Docked approvals and project trust

Read-only tools run immediately. When Wisp proposes a file edit, shell command, or MCP mutation, an
approval panel docks above the footer in place of the composer—keeping the conversation visible and
your draft intact:

- **Inline previews:** `edit` and `write` approvals render a syntax-highlighted inline diff; `bash`
  approvals render the wrapped syntax-highlighted `$ command`; other tools show their full
  arguments.
- **Deliberate confirmation (keyboard or mouse):** Press `1` allow once, `2` allow tool for this
  session, `3` YOLO for this project (session-only in `$HOME` or broader folders), or `4` deny (or
  the `y`/`t`/`a`/`n` aliases, `↑`/`↓` + `Enter`, `Esc` to deny, or click a painted choice
  directly). There is no pre-selected default, so a stray `Enter` meant for the composer cannot
  approve a tool call.
- **Fail-closed saved command-prefix allow rules:** For eligible simple single `bash` commands
  inside a project (never shell chaining, pipelines, subshells, redirections, or broad folders), the
  approval dock offers an extra `2` / `p` choice: **Always allow `<prefix>` (saved)** (shifting
  tool-session, YOLO, and deny to `3`/`4`/`5`). Inspect or revoke saved rules and switch permission
  modes (`Ask`, `Auto edits`, macOS `Auto commands`, `YOLO`) in `/permissions`—see
  [TUI commands: Permissions](./tui/commands.md#permissions).

Project trust uses the same docked panel (`y` trust, `n` deny, keyboard-only) and stays separate
from per-call tool approval.

## Side panel: Session, Files, and Browser {#side-panel}

A one-row top bar keeps the session name on the left and the **`Session`**, **`Files`**, and
**`Browser`** tabs on the right. Click any tab to open or close the panel, press `Ctrl+P` to toggle
it, `F8` to focus it (`[` / `]` switch tabs, `Esc` returns to the composer), or `f` to expand the
active tab into a centered overlay.

On wide terminals, the side panel docks beside the transcript—growing automatically up to a
`76`-column cap while preserving the `100`-column prose measure—and its left border can be dragged
wider (double-click to reset). Narrow terminals open it as a `44`-column overlay.

- **`Session`:** Live run activity, context gauge and token-growth sparkline, auto-expanding MCP
  server and skill inventories, session-touched files (`◆`, click to jump to the edit card),
  running background processes (`/ps`), and recent turns (`Ctrl+T` opens `/tree`). Press `t` (or
  expand with `f`) to open the **parallel turn and subagent swimlane waterfall timeline**, showing
  wall-clock Gantt spans for parallel tool batches, nested subagent calls, and codemode scripts.
- **`Files`:** Session-touched files above a lazy-loaded project file tree with Git status and
  Material Design icons (use a [Nerd Fonts](https://www.nerdfonts.com/) v3 Mono font). Click a file
  or press `Enter` for a read-only inline preview, press `e` to open it in `$VISUAL` / `$EDITOR` /
  `vi` (returning with an updated preview on save), `/` for a project-wide filename filter, and `r`
  to refresh.
- **`Browser`:** Built-in live Chrome/Chromium tab in trusted projects with an address bar, search,
  and recent addresses, plus automatic previews when Wisp renders an HTML mockup or a browser MCP
  server captures a page. Click the viewport or run `/browser interact` (`i` when focused) for
  interactive takeover. See
  [Browser viewport and interactive takeover](./tui/commands.md#browser-viewport-and-interactive-takeover).

## Local shell mode (`!`) and background processes

Prefix a composer line with `!` to run a shell command locally without spending a model turn:

```text
!git status
!uv run pytest tests/docs
```

Output renders inline with a `local · Alt+S to attach` footer—your local runs are **not** sent to
the model automatically. Press `Alt+S` to attach that command's output as a `[! …]` chip on your
next prompt (or press `Alt+S` again, or `Backspace` on an empty draft, to detach it).

When the agent runs a long command in the background (`Ctrl+B` moves a running command to the
background), it appears in the `Session` side panel (and a compact strip below the composer when the
panel is hidden). Open it with `/ps` or `↓` from an empty composer to peek at live output, jump to
its transcript card (`Enter`), or stop it (`x` then `y`, or `/kill <number|id>`).

## Steering and follow-ups {#steering-and-follow-ups}

The composer stays live while a run is active so you never have to cancel just to redirect work:

- `Enter` sends a **steering** message injected at the next safe request boundary (after the current
  tool batch finishes).
- `Alt+Enter` queues a **follow-up** message for when the current run would otherwise stop.
- `Alt+Up` pops the newest queued steering or follow-up message back into your draft (and recovers
  failed submissions).
- `Escape` or `Ctrl+C` cancels the active run without discarding queued messages.

Run `/queue` (or bind `queue.manage` in `tui_keybindings`) to inspect both queues, switch drain
modes (**One at a time** vs. **All** per boundary), restore the newest item from a specific queue,
or clear queues with confirmation. Queues are process-local and validated with snapshot tokens so
stale edits never drop a newer message.

## Completions and the file picker {#completions-and-the-file-picker}

<div id="project-file-references"></div>

- Type `/` to filter slash commands and `/skill:` entries inline; `Tab` or `Enter` accepts.
- Type `@` at a word boundary (while idle or streaming) to open the composer-anchored file picker
  in fuzzy mode (`@rust_launcher` matches `src/wisp/cli/native_tui/rust_launcher.py`). Press `Tab`
  to switch between fuzzy search and a collapsible project tree (`Left`/`Right` collapse/expand,
  `Enter` inserts `@path` or `@"path with spaces"`). Wisp inserts the path reference rather than
  inlining file contents, and respects `protected_paths`.

The composer also highlights recognized commands, project paths, and basic Markdown structure as a
presentation aid while keeping the exact raw text editable.

### Large pastes {#rust-large-pastes}

Pastes over 2,000 characters fold into a compact numbered marker showing character, line, and byte
counts so surrounding text stays easy to edit. Moving into or editing at a marker expands it first;
submission, steering, history search, and queue recovery always use the exact raw text, and markers
are never written into session history.

## Themes, verbosity, and appearance

- `/theme [name]` previews and applies curated themes (`Vapor`, `Glass`, `Orchid`, `Ember`, `Storm`,
  `Grove`, `Wave`, and light themes `Paper` and `Dawn`); `theme.toggle` can be bound in
  `tui_keybindings`.
- `/logo [name]` chooses the startup welcome logo (`Random`, `Classic WISP`, `Wisp Braille`,
  `Adal Braille`, or `Adal Mark Braille`).
- `/verbosity [low|medium|high]` switches transcript detail live without touching saved history:
  `low` hides completed thoughts and inline diffs and collapses consecutive successful tools;
  `medium` (default) groups exploration calls with compact diff previews; `high` expands thoughts,
  `bash` output, and full retained diffs inline.

Local presentation preferences save atomically to `~/.wisp/tui.json` (never `settings.json`, RPC,
or session files). Set `NO_COLOR` for contrast-safe monochrome rendering. For `WISP_TUI_*`
environment variables (`WISP_TUI_COLORS`, `WISP_TUI_IMAGES`, `WISP_TUI_MOTION`, `WISP_TUI_NOTIFY`,
`WISP_TUI_MOUSE`, `WISP_TUI_STREAM_PACING`), `tmux` notes, and exact layout/paste thresholds, see
[Environment variables: Terminal UI rendering, compatibility, and limits](../reference/environment.md#terminal-ui-rendering-compatibility-and-limits).

## Modes {#modes}

- **`/plan`** switches future prompts in this process to read-only planning mode (`write`, `edit`,
  `bash`, and non-read tools stay unavailable). Use **`/build`** to return to editing. Mode is not
  persisted in session JSONL.
- **`/new`** preserves the current session for `/resume`, clears the screen, and starts a fresh
  session lazily while keeping your provider, model, effort, mode, permissions, and trust settings.

## Flags and availability {#flags-and-renderers}

```bash
wisp tui --continue
wisp tui --resume <session-id-prefix>
wisp tui --no-all-tools                  # opt-in tool filter instead of the full registry
wisp tui --yes                           # auto-approve mutating/command tools for this run
wisp --mode tui --provider anthropic     # top-level flags such as --provider and --model work
```

Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle `wisp-tui` alongside the Python
backend. Pure-wheel installs keep print, JSON, RPC, and SDK modes, and report how to obtain a
native wheel or build a matching Rust binary when launched interactively. From a source checkout,
build the binary and set its absolute path in `WISP_RUST_TUI_BINARY`—see
[Development setup](../contributing/development.md#rust-tui-scaffold) and
[Rust terminal frontend boundary](../architecture/rust-tui-boundary.md#subsystem-ownership).

### Provider-native web search {#provider-native-web-search}

Run `/search`, `/search on`, or `/search off` while idle to toggle provider-native web search for
the current session (set `web_search` in `~/.wisp/settings.json` or `WISP_WEB_SEARCH` for a
persistent default). This does not affect separate MCP search servers such as Exa. Google Search
grounding is not yet supported. Replies that used provider search show bounded source titles/hosts
and mark cost as `cost ≥…` to indicate provider search fees are excluded.

### Updates {#rust-updates}

`/update`, `/update check`, and `/update install` display update instructions without leaving your
draft. Quit Wisp and run `wisp update --check` or `wisp update` in your shell for persistent
`uv tool` installations; source checkouts should pull and rebuild the matching Rust binary per
[Development setup](../contributing/development.md#rust-tui-scaffold).

<section class="moved-tui-sections" data-pagefind-ignore="" aria-label="Moved TUI sections"><strong id="more-tui-tasks" class="eyebrow">Topics on dedicated TUI pages</strong><ul><li id="rust-keybinding-preferences"><a data-tui-destination="" href="/Wisp/guide/tui/keybindings.html#keybinding-preferences">Keybinding preferences</a></li><li id="keybindings"><a data-tui-destination="" href="/Wisp/guide/tui/keybindings.html#keybindings">Keybindings</a></li><li id="slash-commands"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#slash-commands">Slash commands</a></li><li id="managing-mcp-servers"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#managing-mcp-servers">Managing MCP servers</a></li><li id="context-skills-and-mcp-browsers"><a data-tui-destination="" href="/Wisp/guide/tui/commands.html#context-skills-and-mcp-browsers">Context, skills, and MCP browsers</a></li><li id="resuming-long-sessions"><a data-tui-destination="" href="/Wisp/guide/tui/sessions.html#resuming-long-sessions">Resuming long sessions</a></li><li id="prompt-history"><a data-tui-destination="" href="/Wisp/guide/tui/sessions.html#prompt-history">Prompt history</a></li></ul></section>
