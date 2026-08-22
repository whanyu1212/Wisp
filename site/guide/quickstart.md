---
title: Quickstart
---

# Quickstart

Wisp runs from the project you want it to understand. It requires Python 3.12 or newer; the
recommended persistent installation is:

```bash
uv tool install "wisp-ai==0.1.0rc6"
wisp --version
```

See [Installation](./installation) for `uvx`, updates, and troubleshooting.

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
masked API keys for OpenAI, Anthropic, Google, and configured OpenAI-compatible providers. Provider
credentials default to Wisp's private `~/.wisp/auth.json` file. Before connecting in a repository
you trust, inspect its `.wisp/settings.json`: the project may set `auth_path`, including a relative
path inside the repository. Pass `--auth-file` or set `WISP_AUTH_FILE` to override that choice, and
never commit the selected credential file.

Now enter a request such as:

```text
explain the architecture of this repository
```

Tool reads can run directly. Writes, edits, and shell commands pause for your approval unless you
explicitly pre-approved them. See [Tools & safety](./tools-and-safety) for the complete policy.

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

Next, learn how to [steer, cancel, and approve while Wisp works](./staying-in-sync), or browse the
[TUI guide](./tui) and [CLI reference](../reference/cli).
