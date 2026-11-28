---
title: Tools & safety
description: Understand read, mutating, and command tool policies, project trust, protected paths, saved permissions, and MCP boundaries.
route: guide/tools-and-safety.html
section: using
order: 7
layout: docs
listed: true
searchable: true
---

# Tools & safety

Wisp separates **what tools the model can see**, **whether you trust a project's files**, and
**whether a specific edit or command may run**. Each boundary stays under host and user control—see
[Staying in sync](staying-in-sync.md) for how approvals pause active runs.

## Built-in tools and workspace scope

File tools stay scoped to the workspace (the project root in a trusted project, or the current
working directory otherwise). On macOS and Linux, read tools follow a named symlink whose target
stays inside the workspace and is not protected; `write` and `edit` refuse symlinks and require the
model to read an existing file before changing it (refusing stale writes if the file changed since).

| Category | Tools | Default approval |
|---|---|---|
| **Read** | `read` · `grep` · `find` · `ls` · `read_output` | Runs directly |
| **Mutating** | `write` · `edit` | Required |
| **Command** | `bash` | Required |

`bash` runs locally **as your user** for both one-shot commands and long-running managed processes
(`operation=start|poll|cancel`). Wisp withholds provider API key environment variables from `bash`
so `env` cannot leak them into transcripts, while other tools' variables pass through. In the TUI,
press `Ctrl+B` to move a running foreground command into the background; Wisp notifies the agent
when background processes exit (waking an idle session automatically unless you disable
`wake_on_process_exit`) and stops managed and detached processes when a command, session, or Wisp
exits.

### Output a result cuts is saved

When a `bash` result exceeds the model-facing output preview, Wisp keeps both the start and end of
the output, saves the full stdout and stderr to a private, process-local directory
(`~/.wisp/tool-output/`), and lets the model page through it via `read_output` without rerunning
the command. Saved output is deleted when Wisp exits and is not persisted in session JSONL.

For exact search/shell timeouts, auto-wake caps, preview line/byte budgets, and `read_output`
storage limits, see [Configuration: File, search, and shell tools](../reference/configuration.md#file-search-and-shell-tools).

## Protected paths

File tools never open common secret files: `.env` variants, keys/certificates (`*.pem`, `*.key`,
`*.p12`, `*.pfx`, SSH keys), `credentials.json`, `.netrc`, `.pgpass`, and Wisp's own `auth.json`,
`mcp-auth.json`, and `settings.json` (checked case- and symlink-target-insensitively). Customize
the list with user-only `protected_paths` in `~/.wisp/settings.json`; Wisp's own credential,
settings, permission, and browser-profile files stay protected even if you set `protected_paths` to
`[]`.

**Protected paths are not a shell sandbox.** Outside macOS sandboxed `Auto commands`, they apply
only to Wisp's file tools—not to unrestricted `bash` (which runs as your user), MCP tools, or
hardlink aliases. Keep `bash` on Ask when secrets are within reach, and use a container or VM for
untrusted repositories.

## Print mode exposes no tools unless you ask

Non-interactive print mode withholds all tools until you opt in, and blocks mutating/command
execution unless you also pass `--yes` (`--allow-unsafe-tool-execution`):

```bash
wisp -p "list files" --allow-read-tools
wisp -p "run tests"  --allow-tool bash --yes
```

Pass `--max-tool-iterations <n>` or `--timeout <seconds>` to bound unattended runs; see
[CLI reference: Exit status](../reference/cli.md#exit-status).

## Tool prompt metadata

Extensions may attach descriptive `ToolPromptMetadata` when registering a tool. Wisp includes it
only when that tool is exposed for the run; it cannot weaken tool policy, protected paths, or
approval requirements.

## Project trust

Trusting a project lets Wisp load its local `.wisp/settings.json`, `AGENTS.md` / `CLAUDE.md`
instructions, and project skills. Trust **does not** approve mutating or command tools, and
untrusted projects stay usable with those local resources disabled. Before connecting a provider in
a trusted repository, check its `.wisp/settings.json` for `auth_path` (override with `--auth-file`
or `WISP_AUTH_FILE` if needed).

Decisions are saved globally in `~/.wisp/trust.json`:

```bash
wisp trust status [path]   # trusted, untrusted, or undecided
wisp trust allow [path]    # persistently trust a project
wisp trust revoke [path]   # persistently mark a project untrusted
wisp trust forget [path]   # remove the decision so Wisp prompts again
```

Non-interactive runs default to untrusted unless you pass `WISP_TRUST=1` in the process environment
(`WISP_TRUST=0` forces untrusted; never persisted). See
[Environment variables](../reference/environment.md) for `WISP_TRUST_FILE`.

### Home folder and broad folders

Trust and `YOLO` are never persisted for `$HOME`, any ancestor (including `/`), a drive root, or
the system temporary directory. Wisp asks for trust each session there, `wisp trust allow`/`revoke`
refuse those folders, and `Auto edits`/`Auto commands`/`YOLO` apply for the session only. Project
discovery stops below `$HOME` so a stray marker or dotfiles repo cannot trust every folder beneath
it.

## MCP tools

Configure external servers in the [MCP servers & codemode guide](mcp.md). Every MCP tool defaults
to `command` safety and requires approval until you explicitly mark it `"read"` in `tool_safety`;
server annotations never weaken approval policy, and file-tool protections do not sandbox MCP
servers.

#### Signing in with OAuth

See [Signing in with OAuth](mcp.md#signing-in-with-oauth) for browser sign-in and `~/.wisp/mcp-auth.json`
storage (kept separate from provider credentials in `~/.wisp/auth.json`).

### Finding MCP tools on demand

See [Finding MCP tools on demand](mcp.md#finding-mcp-tools-on-demand) for `mcp_tool_exposure`
(`auto`, `direct`, `on_demand`). Discovery changes what the model sees, not its permissions.

#### What on-demand discovery saves

See [measurements and trade-offs](mcp.md#what-on-demand-discovery-saves) for when on-demand
discovery saves input tokens vs. when small catalogs are cheaper and faster in direct mode.

### Asking Wisp to set up a server

See [Asking Wisp to set up a server](mcp.md#asking-wisp-to-set-up-a-server). Server proposals always
require explicit **Confirm** or **Deny** (even under `YOLO`) and are refused in print/JSON modes.

## Scripting tool calls with codemode (experimental)

See [MCP with codemode](mcp.md#mcp-with-codemode) for setup and runnable examples, and
[Execution limits](mcp.md#scripting-tool-calls-with-codemode-experimental) for sandbox boundaries.
Every child call inside a script passes normal tool policy, plan mode, protected paths, and its own
approval checks; scripts cannot invoke `spawn_agent` or another `codemode` script.

## Delegating to a subagent (experimental)

Enable user-only `"subagents": true` in `~/.wisp/settings.json` (or `WISP_SUBAGENTS=1`, or
`/subagents` in the TUI) to expose `spawn_agent` for parallel read-only investigations without
crowding your main context:

- **Fresh vs. forked context:** Fresh subagents start with only the task and can use a cheaper
  user-configured `subagent_model` (`WISP_SUBAGENT_MODEL`) on the same provider. Forked subagents
  (`context: "fork"`) reuse the session's model and conversation prefix cache so they can reference
  prior findings, at the cost of rereading the conversation per task.
- **Strictly read-only and billed:** Subagents receive only the run's read-only tools (`read`,
  `ls`, `grep`, `find`, and MCP tools marked `"read"`), cannot request approvals, return text only,
  record every nested call in the transcript, and bill their model usage to session totals.

See [Configuration: Codemode and subagents](../reference/configuration.md#codemode-and-subagents)
for task concurrency, round, and report-size limits.

## Saved project permissions

When an edit or command pauses for approval in the TUI, you choose the exact scope:

- **Allow once** (`1`) or **Allow tool for this session** (`2`) — temporary grants that clear on
  `/new`, session switch, or changing the project default.
- **Always allow `<prefix>` (saved)** (`2` / `p` on eligible simple single `bash` commands inside a
  project) — fail-closed per-project command-prefix allow rule. Never offered for shell chaining,
  pipelines, subshells, redirections, or broad folders (`$HOME`). Revoke saved rules anytime in
  `/permissions`.
- **Project permission defaults (`/permissions`):** Saved per canonical project path in user-owned
  `~/.wisp/permissions/` (outside the repository; never saved for broad folders):
  - **`Ask`** (default) — ask before every mutating or command tool call.
  - **`Auto edits`** (`/permissions auto`) — auto-approves eligible built-in workspace and
    session-scratch edits on macOS/Linux (atomic replacement only; still protects Git metadata,
    agent-control files, protected paths, and stale-write checks; still asks for commands).
  - **`Auto commands`** (`/permissions auto-commands`, macOS Seatbelt opt-in) — keeps `Auto edits`
    and runs supported local test/build commands inside a Seatbelt sandbox with no TCP/UDP network;
    fast-path test runners run with a **read-only workspace** and disposable `TMPDIR`/`HOME`, while
    other eligible simple local commands go to an independent no-tools model reviewer (which can
    incur model charges) and fall back to Ask on any ambiguity. Pass
    `--auto-command-read-dir /abs/path` at launch to allow read-only `/bin/ls` and `/bin/cat` on one
    external directory for that session. **Known limitation:** Seatbelt is containment, not a VM—a
    pre-existing hardlink alias under an allowed name can still reach a protected or external file.
  - **`YOLO`** (`/permissions yolo`, or startup `--yes` for one run) — skips tool approval prompts;
    project trust, tool exposure, protected file paths, and mandatory MCP-proposal confirmations
    still apply.

For exact Seatbelt rules, fast-path command forms, and reviewer budgets, see
[Configuration: macOS `Auto commands`](../reference/configuration.md#macos-auto-commands-seatbelt--reviewer).
