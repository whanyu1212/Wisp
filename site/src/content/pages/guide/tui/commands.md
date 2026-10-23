---
title: TUI commands
description: Slash commands, provider and model selection, permissions, and MCP management.
route: guide/tui/commands.html
section: using
order: 1
layout: docs
listed: true
searchable: true
navTitle: Commands
parent: guide/tui.md
---

# TUI commands {#tui-commands}

## Connecting providers

`/connect` opens a provider connection panel. Use arrow keys to select a provider,
`Enter` to start its available API-key or device-code flow, `d` to disconnect stored credentials,
`r` to refresh, and `Escape` to close or cancel. API-key entry docks above the composer, shows a
cursor after the masked key and a character count so a paste visibly lands, and says when an
environment variable such as `OPENAI_API_KEY` is set, because that variable takes precedence over a
saved key. Device login displays the
short-lived user code and verification URL but never stores them in prompt or session history.



## Choosing a model

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



## Permissions

Unlike print mode, **the TUI exposes the full tool registry by default**. Mutating and
command tools still pause for approval (MCP server proposals offer only Confirm or Deny; see
[Asking Wisp to set up a server](.././tools-and-safety#asking-wisp-to-set-up-a-server)): approve once,
allow that tool for the session, choose a saved
project YOLO default, or deny.

Use `/permissions` in Rust to inspect and change the project default, or `/permissions ask` and
`/permissions yolo`. The panel docks above the composer and marks the current mode; choose with
`1`/`2`, or highlight with `↑`/`↓` and press `Enter` (there is no default highlight, because
saving a mode also clears temporary grants). Saved defaults apply to subsequent launches in the same canonical
project directory and live in user-owned `~/.wisp/permissions/` files, outside the repository.
Allow-once and tool-session choices do not persist; temporary session grants expire on `/new` or
switching to another session. Changing the default clears temporary grants. Startup `--yes` alone
is not saved. These choices do not change project trust or protected paths. In the home folder or a
folder above it, YOLO applies for the session only and the panel says so; Ask is still saved.

## Managing MCP servers

`/mcp` lists the configured MCP servers with their status. `↑`/`↓` select a server; below the list
you see what it runs (or the URL it connects to), the environment variables it sets or forwards, the
headers it sends (naming each variable a header sends and the host that receives it), any startup
error, and its tools. `PgUp`/`PgDn` scroll those details.

| Key | Action |
|---|---|
| `a` | Add a server by pasting its config snippet |
| `e` | Disable the selected server, or enable it after confirming what it runs |
| `d` | Remove the selected server, after confirming |
| `l` | Sign in to the selected HTTP server with OAuth, in your browser |
| `o` | Sign out of the selected server |
| `c` | Reconnect: reload servers from your settings file and restart them |
| `r` | Refresh the status without reconnecting |
| `Esc` | Close, step back out of a panel, or cancel a sign-in in progress |

To add a server, press `a` and paste the `{"mcpServers": {...}}` block from the server's README, one
server at a time (use `wisp mcp import` for several). Wisp reads the snippet without saving it. For
each environment variable the server needs, choose whether to forward it from Wisp's environment or
enter a value; then enter each HTTP header value the snippet sets. Entered values are masked, never
shown again, and saved to `~/.wisp/settings.json`. The snippet's own values are ignored, since
published snippets usually hold placeholders; a header written as `"Bearer ${VAR}"` forwards that
variable instead and needs no value.

A server that needs you to sign in shows "sign in required". Press `l`: Wisp opens the server's
sign-in page in your browser and waits up to five minutes. The list shows the page's address in case
no browser opens; open it in a browser on the same machine as Wisp, because the page returns to a
local address. `Esc` cancels. Once you finish, the server reconnects
with its tools; the list shows "signed in", and `o` signs out. See
[Signing in with OAuth](.././tools-and-safety#signing-in-with-oauth).

Adding, enabling, and removing end in a confirmation docked above the composer that shows exactly
what will run. Like an approval, it has no default: choose with `1`/`2`, or highlight with `↑`/`↓`
and press `Enter`; a key pressed before the panel was drawn does nothing. Confirming saves the change
and reconnects, and the list then shows the server's new status and tools. Changes wait until the
current prompt finishes. "Allow for this session" answers for MCP tools are cleared on every
reconnect.

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
/queue                      inspect and manage steering and follow-up queues
/plan                       switch to read-only planning mode
/build                      switch to normal build mode
/history                    search prompts submitted in this TUI run
/theme [name]               preview or select a curated color theme
/logo [name]                preview or select a startup logo
/update [check|install]     check immediately or explicitly install an update
/skills                     inspect loaded skills and discovery diagnostics
/mcp                        manage MCP servers: add, remove, enable, disable, reconnect
/quit, /exit
```

`/init` asks the active model to inspect repository documentation, manifests, CI configuration, and
source layout before creating project-specific guidance. It only works in build mode, uses the normal
project-trust and write-approval flow, and refuses to replace an existing `AGENTS.md` or `AGENTS.MD`.
The final write is create-only, so a file that appears during inspection is preserved. The Rust TUI
offers `/init` only when the backend includes it in command discovery and delegates the complete
workflow to that backend.

## Context, skills and MCP browsers

The header shows context usage. `/context` reports the budget, usage, cost and compaction details;
during a run, session totals show the last refreshed snapshot. When idle, `/context auto on|off`
changes automatic compaction for the current process, and `/compact [instructions]` compacts the
selected session. `Ctrl+C` cancels a manual compaction.

`/skills` lists skill descriptions and sources, and `d` shows discovery diagnostics. `Enter` inserts
the selected `/skill:name ` before your draft, ready to edit and submit. Slash completion lists
commands first, then skills, and typing `/skill:` narrows it to skills; `Tab` or `Enter` accepts a
suggestion, then `Enter` submits. `/mcp` shows server status, registered tools and connection
errors. In both browsers `r` refreshes and `Esc` or `Ctrl+C` closes; both stay available while a
prompt is running.
