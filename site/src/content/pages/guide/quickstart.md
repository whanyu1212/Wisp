---
title: Quickstart
description: Trust a repository, connect a model provider, run your first interactive or one-shot prompt, and resume saved work.
route: guide/quickstart.html
section: using
order: 2
layout: docs
listed: true
searchable: true
capture: approval
---

# Quickstart

Wisp runs from the project you want it to understand. It requires Python 3.11 or newer; the
recommended persistent installation is:

```bash
uv tool install --python 3.12 wisp-ai
wisp --version
```

See [Installation](installation.md) for `uvx`, updates, and troubleshooting.

## Start in a project

```bash
cd path/to/project
wisp
```

A first run asks whether you trust the project. Trusting allows Wisp to load project-controlled
configuration, context and instruction files, and skills. If you decline—or if a non-interactive
invocation cannot ask—Wisp still runs, but those project-local resources stay disabled. Executable
project extensions are not currently discovered. You can manage the persisted decision explicitly:

```bash
wisp trust status .
wisp trust allow .
```

## Connect a provider

Wisp defaults to OpenAI Codex subscription access. In the TUI, enter:

```text
/connect
```

Choose **OpenAI → ChatGPT Plus/Pro** and complete the device-code flow. The same panel accepts
masked API keys for OpenAI, xAI, DeepSeek, Anthropic, Google, and configured OpenAI-compatible
providers. Provider credentials default to Wisp's private `~/.wisp/auth.json` file. Before
connecting in a repository you trust, inspect its `.wisp/settings.json`: the project may set
`auth_path`, including a relative path inside the repository. Pass `--auth-file` or set
`WISP_AUTH_FILE` to override that choice, and never commit the selected credential file.

## Run your first editing turn

Start with a question or a small edit—use `@` to reference a file without pasting its contents:

```text
explain the architecture of this repository
```

- **Approvals stay explicit:** Read-only tools run directly. Edits, writes, and shell commands dock
  an approval panel above the footer with an inline diff or syntax-highlighted `$ command` preview.
  Approve once (`1`), allow that tool for the session, save a fail-closed command-prefix allow rule
  for simple `bash` commands inside the project (`2` / `p`), or deny (`Esc`). Manage saved rules and
  modes anytime in `/permissions`; see [Tools & safety](tools-and-safety.md) for the full policy.
- **Review changes per turn:** Every editing turn ends with a change summary (`+N −M in K files`).
  Press `Alt+G` (or click the summary row) to open `/diff` filtered to that turn's edits, or run
  `/diff` for the full working tree against `HEAD`.
- **Inspect the workspace side panel:** Click **`Session`**, **`Files`**, or **`Browser`** in the
  top bar (or press `Ctrl+P`) to view live context, the turn/subagent waterfall (`t`), inline file
  previews (`e` to edit), or the built-in browser.
- **Run quick local checks inline:** Prefix any line with `!` (such as `!git status` or `!pytest`)
  to run a shell command locally without spending a model turn. Output stays local unless you press
  `Alt+S` to attach it to your next prompt.

See the [TUI guide](tui.md) for steering active runs, `/verbosity`, themes, and shortcuts.

## One-shot and offline runs

Print mode runs one prompt and exits:

```bash
wisp -p "summarize the current changes"
```

To verify the installation without credentials or a network model call, select the deterministic
fake provider:

```bash
wisp -p "hello" --provider fake
```

Assistant text is written to stdout and lifecycle events to stderr. For machine-readable JSONL,
add `--mode json`.

## Continue where you left off

Sessions are append-only JSONL files under `~/.wisp/sessions` by default. Resume the newest session
or select one by path, filename, id, or id prefix:

```bash
wisp --continue
wisp --resume <session-id-prefix>
```

Next, learn how to [steer, cancel, and approve while Wisp works](staying-in-sync.md), or browse the
[TUI guide](tui.md) and [CLI reference](../reference/cli.md). If something goes wrong, start with
[Troubleshooting & FAQ](troubleshooting.md).

To connect external services, follow [MCP servers & codemode](mcp.md): add and authenticate a server,
then choose how its tools are discovered and whether read-only calls can be batched in scripts.
