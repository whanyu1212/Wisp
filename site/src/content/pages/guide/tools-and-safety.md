---
title: Tools & safety
description: Understand read, mutating, and command tool policies, project trust, protected paths, and MCP server approvals.
route: guide/tools-and-safety.html
section: using
order: 7
layout: docs
listed: true
searchable: true
---

# Tools & safety

Wisp includes built-in local tools for reading files, editing files, searching projects, and running
shell commands. File tools are sandboxed to the workspace. In a trusted project the workspace is
the project root, so a session started in `src/` can still read and edit `tests/`. Otherwise, or
when no project root contains the working directory, it is the working directory itself. Relative
paths are always taken from the working directory. On macOS and Linux, `read`, `grep`, `find`, and
`ls` follow a symbolic link you name when its target is also inside the workspace and not
protected; `write` and `edit` refuse links and name the target to use
instead. Links found while searching a directory are skipped.

| Category | Tools | Approval |
|----------|-------|----------|
| **Read** | `read` · `grep` · `find` · `ls` · `read_output` | Runs directly |
| **Mutating** | `write` · `edit` | Required |
| **Command** | `bash` | Required |

Approval decisions stay outside the model's control — see [Staying in sync](staying-in-sync.md) for
why that boundary matters and how the shared runtime enforces it.

A `grep`, `find`, or `ls` call stops after 30 seconds and returns what it has found so far, with a
note saying the results are incomplete. If a filesystem call is still blocked 5 seconds later, for
example on an unresponsive network mount, the call fails with a timeout instead of waiting for it.
`grep` and `find` search at most 100 directory levels below where they start, and say how many
deeper directories they skipped. `edit` refuses files larger than 16 MiB; change those with `bash`.

`write` and `edit` change an existing file only if the model has read it in the current
conversation and it has not changed since. A file you edited, reformatted, or regenerated after the
model read it is refused with a request to read it again, so your changes are never overwritten
unseen. Changes the model makes through `bash` count as changes too, and new files need no read.

`bash` defaults to one-shot execution and reports stdout, stderr, truncation state, and exit code.
It also accepts `operation=start|poll|cancel` for commands needing a retained process handle; those
return a `process_id`, process state, incremental output, and per-stream truncation metadata under
the same safety category and approval policy. A one-shot `timeout` is at most 3,600 seconds, a
started process's `lifetime_seconds` at most 86,400 (a day), and `yield_seconds` and `wait_seconds`
at most 600. A poll that waits returns at the process's next output; with `until=exit` it waits
through output until the process stops or the wait ends, so a watcher that prints progress, such as
`gh pr checks --watch`, does not cut the wait short. A process started with `notify_on_exit=true`
tells the agent when it stops, so the agent can end its turn instead of waiting; the notice names
the process and how it ended, and the agent polls once to read its output. If the session is idle
when such a process stops, or becomes idle with the exit still unreported, Wisp starts a run by
itself that carries only the notice, and the TUI shows it as a run like any other, which `Esc`
cancels. At most three such wake runs start between your prompts, so a process that keeps
restarting itself cannot keep the agent working unattended; after that, Wisp says auto-wake has
paused, and exits wait for your next prompt, which tells the agent they waited. A wake run uses the session's permission mode and asks for approvals as usual. Set
`wake_on_process_exit` to `false` in `~/.wisp/settings.json`, or `WISP_WAKE_ON_PROCESS_EXIT=0`,
to have every notice wait for your next prompt instead. Waking is on by default in the CLI and TUI,
and off by default for applications that embed Wisp through the in-process SDK. Every exit also appears in the TUI as it
happens. When every process slot is taken, the error lists the processes holding them, so the model
can poll or cancel one.

While the agent waits on a shell command in the foreground, such as a test suite or a dev server,
press `Ctrl+B` in the TUI to move it to the background. The command keeps running with all its
output, the agent's call ends at once with the command's new `process_id` and its output so far,
and the turn goes on; the agent is told when the command stops, as if it had started it with
`notify_on_exit=true`. A moved command may run for up to an hour from then, and keeps its process
slot. Embedders send `background_tool_call` with the call's id.

### Watching a command while it runs

A one-shot `bash` command that runs longer than half a second shows its output as it arrives. In the
TUI, the running card shows the latest output line beside the command, and expanding the card shows
the last lines of both streams. The view updates at most four times a second, and only when the
output changed, so a quiet command shows nothing until it finishes. When the result arrives, it
replaces the live view.

Progress is for watching only. It never reaches the model, which gets the same result it always
did, and it is not saved in the session. A frontend that falls behind skips to the newest output
instead of slowing the command down. SDK clients receive the same updates as
`ToolExecutionProgress` events, and `--mode json` includes them as `tool.execution.progress` lines.

### Output a result cuts is saved

A finished `bash` command's result shows long output as its start and its end: the first 64 lines
(up to 4,000 bytes) and the last 192 lines (up to 8,000 bytes), with a line such as
`[truncated: 81,204 bytes omitted after the first 64 lines]` between them. A build's first error is
often near the start and its summary at the end. The start comes from stdout, or from stderr when
stdout is empty, and the marker's line count is an offset into that stream (it names the stream
when both have output). Output that fits is shown whole. A smaller tool output budget (50,000 bytes
and 2,000 lines by default) scales both parts down, and below about 1,000 bytes or 8 lines a
result keeps only the end. When a result leaves anything out, Wisp saves the full stdout and
stderr and ends the result with a line such as
`[Full output saved: stdout output_id=3f9c0a1b2c3d4e5f (2481533 bytes, 31007 lines). Read it with read_output.]`.
The model pages through a saved stream with `read_output`, using the same `offset` and `limit` as
`read`, instead of running the command again. This also applies to a poll of a started process
that dropped output, and to a timed-out command, whose error names the saved output and shows its
last lines.

- `read_output` is a read tool and needs no approval. It accepts only ids Wisp issued, never a
  path, so it cannot reach other files. It is exposed whenever `bash` is, including with
  `--allow-tool bash`.
- Each stream keeps its first 8 MiB; the result already shows how it ended. A runtime keeps at most
  64 MiB and 32 saved streams, deleting the oldest finished one to make room. A stream cut short by
  either limit stops there, so saved output never skips a part of the stream. stdout and stderr are
  saved separately, because Wisp cannot know how they interleaved.
- Saved output lasts while Wisp runs. It lives in a directory only you can read,
  `~/.wisp/tool-output/`, which is deleted when Wisp exits; after a crash, the next Wisp process to
  save output removes it. Saved output is not stored with the session, so a session resumed in a
  later Wisp process cannot read it.
- Command output can contain secrets the command printed. Saving it keeps them on disk, readable
  only by you, until Wisp exits, in addition to the part the transcript already holds.
- If the output cannot be saved, for example because the disk is full, the command's result is
  unchanged and says why the output was not saved.

Commands inherit Wisp's environment except the variables Wisp reads provider API keys from:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
`XAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, and the key variable of a configured OpenAI-compatible
endpoint, such as `OPENROUTER_API_KEY`. A command's output reaches the model and the transcript, so
`env` would otherwise publish them. Other variables, including tokens your own tools need, pass
through unchanged.

A one-shot command does not leave processes behind. When it exits, Wisp stops everything it
started, including a daemon that detached itself, such as `redis-server --daemonize yes`, and tells
the model it did. For a process that should keep running, use `operation=start` and run it in the
foreground: a managed command's detached processes are also stopped when it ends. To recognise a
detached process, Wisp gives each command a `WISP_COMMAND_TOKEN` environment variable with a random
value. Some processes cannot be found this way: one that clears its environment or runs as another
user, and, on macOS, a protected system program such as those in `/bin` and `/usr/bin`, whose
environment macOS keeps private.

Processes started with `operation=start` belong to the session that started them: they stop when
you start a new session or switch to another one, and when Wisp shuts down, including when you close
the terminal (SIGHUP) or a supervisor sends SIGTERM. A stopped process still answers a poll, as
cancelled. On macOS and Linux they also stop if Wisp is
killed outright (SIGKILL) or crashes: a small watchdog process, started with Wisp's first command,
notices that Wisp has gone and stops every command still running, including detached processes it
can recognise by their `WISP_COMMAND_TOKEN`. On Windows, a crash still leaves them running.

## Protected paths

The file tools never open files that commonly hold secrets: `.env` and its variants, keys and
certificates (`*.pem`, `*.key`, `*.p12`, `*.pfx`, and SSH keys such as `id_rsa`),
`credentials.json`, `.netrc`, `.pgpass`, and Wisp's own `auth.json`, `mcp-auth.json`, and
`settings.json`. Matching ignores case and accent composition the way case-insensitive filesystems
do, so `credentialſ.json` (long s) counts as `credentials.json`, and it checks both the path as
written and the file a symlink points to. `read`, `write`, and `edit` refuse a protected path, and
`grep`, `find`, and `ls` leave protected files out of their results.

Protection goes by path. A hard link is another path to the same file, so a hard link inside the
workspace to a protected file, or to a file outside it, is read and changed like any other
file. Git does not store hard links, so one has to be created on your machine, usually by a `bash`
command you approve.

Change the list with the `protected_paths` setting in your user settings; a project's settings
cannot change it. Wisp's credential files, including MCP sign-ins, and its settings and permission
files stay protected even if you set the list to empty.

Protected paths are not a sandbox. They apply only to Wisp's file tools. A `bash` command runs as
your user, so `cat .env`, or a script that reads it, is not blocked, and neither are MCP or extension
tools. For those, approval is the safeguard: keep `bash` on ask when secrets are within reach, read
each command before allowing it, and run Wisp in a container or virtual machine for repositories you
do not trust. YOLO and "allow this tool for the session" grants skip that review.

## Print mode exposes no tools unless you ask

Read tools are enabled as a group; mutating and command tools require per-tool opt-in. Allowing
`bash` also exposes `read_output`, which reads the output `bash` saves:

```bash
wisp -p "list files" --allow-read-tools
wisp -p "run tests"  --allow-tool bash --yes
```

Because print mode is non-interactive, mutating and command tools are also blocked at execution time
unless you pass `--yes` (alias `--allow-unsafe-tool-execution`). Without it the model receives a
clear tool error instead of Wisp executing the operation.

Wisp does not cap model/tool rounds by default. Pass `--max-tool-iterations <n>` for a
non-interactive fuse, and `--timeout <seconds>` to bound a run's wall-clock time, including a
single tool call that hangs. See [Exit status](../reference/cli.md#exit-status).

## Tool prompt metadata

Extensions may attach optional `ToolPromptMetadata` when calling `ExtensionAPI.register_tool(...)`.
Wisp adds that guidance only when the tool is actually exposed for the current run, de-duplicates and
bounds it, and keeps it separate from the provider-facing tool schema. The metadata is descriptive —
it cannot alter tool policy, sandboxing, protected paths, or approval requirements.

## Project trust

Project-local settings, context files (`AGENTS.md` / `CLAUDE.md`), and skills are loaded only after
the project is trusted. Untrusted projects remain fully usable — Wisp simply ignores their local
configuration and instructions. Project-authored executable extensions are not currently loaded.

The first run in an untrusted directory asks `Do you trust the files in /path/to/project?`. Answer
yes and the decision is remembered globally in `~/.wisp/trust.json`, keyed by resolved path.

```bash
wisp trust status [path]   # trusted, untrusted, or undecided
wisp trust allow [path]    # persistently trust a project
wisp trust revoke [path]   # persistently mark a project untrusted
wisp trust forget [path]   # remove the decision so Wisp can prompt again
```

- **Non-interactive runs** (CI, scripts, standalone RPC) default to untrusted. The interactive TUI
  asks before entering the interface. Set `WISP_TRUST=1` to opt in for one process, or `WISP_TRUST=0`
  to force untrusted mode.
- `WISP_TRUST` is read only from the real process environment, never from project files, and is never
  persisted.
- `WISP_TRUST_FILE` may relocate the global trust store only to an absolute path. Relative values
  are rejected in favor of `~/.wisp/trust.json`; an absolute path is otherwise accepted even when
  it points inside the repository, so choose a user-controlled location.

### Home folder and broad folders

Trust and YOLO are never saved for your home folder, any folder above it (including `/`), a drive
root, or the system's temporary folder: a decision saved there would carry to every project below
it. Wisp still starts there, but asks for trust each session, `wisp trust allow` and `revoke` refuse
such a folder, and YOLO chosen there lasts for the session only. The TUI, RPC, and SDK show a
startup notice naming the folder.
Decisions an earlier release saved for such a folder are ignored but kept; `wisp trust forget`
removes a trust record, and choosing Ask in `/permissions` replaces a saved YOLO.

Wisp finds the project folder from a Git repository or a marker file such as `pyproject.toml` or
`AGENTS.md`, searching upward from the working directory. That search stops below the home folder,
so a stray marker or a dotfiles repository in your home folder does not make it the project for
every folder beneath it.

## MCP tools

Connect external tools using the [MCP servers & codemode guide](mcp.md). It covers CLI setup,
TUI management, authentication, settings examples, limits, and tool safety. MCP tools default to
`command` safety: only your explicit `tool_safety` settings can mark one read-only. Server
annotations do not grant permissions, and local file-tool protections do not sandbox an MCP server.

#### Signing in with OAuth

See [Signing in with OAuth](mcp.md#signing-in-with-oauth) for browser sign-in, token storage, and
server URL binding. Provider `/connect` credentials are separate from MCP authentication.

### Finding MCP tools on demand

See [Finding MCP tools on demand](mcp.md#finding-mcp-tools-on-demand) for `mcp_tool_exposure`,
`mcp_search`, `mcp_describe`, and `mcp_call`. Discovery changes what the model sees, not its permissions.

#### What on-demand discovery saves

The [measurements and tradeoffs](mcp.md#what-on-demand-discovery-saves) compare direct and on-demand
exposure; large catalogs can save input tokens at the cost of extra discovery turns.

### Asking Wisp to set up a server

See [Asking Wisp to set up a server](mcp.md#asking-wisp-to-set-up-a-server). Proposals always need a
separate user confirmation, even under YOLO; they are refused in print and JSON modes.

## Scripting tool calls with codemode (experimental)

See [MCP with codemode](mcp.md#mcp-with-codemode) for configuration and a worked example, and
[Codemode safety and limits](mcp.md#scripting-tool-calls-with-codemode-experimental) for the sandbox,
audit records, and cancellation behavior. Scripts reach only the run's read-only tools; they cannot
approve a call or turn a command tool into a read-only one.

## Delegating to a subagent (experimental)

With `"subagents": true` in `~/.wisp/settings.json`, or `WISP_SUBAGENTS=1`, the model also gets a
`spawn_agent` tool. It hands a list of tasks to subagents, one each: a subagent is a second agent on
the same model that starts from its task alone, works with the run's read-only tools, and returns
only its final message. The model is told to do the work itself by default and to delegate only a
broad search or investigation whose raw output would crowd its context.

- **Fresh context by default.** A subagent sees the session prompt and the task the model wrote.
  It sees none of the conversation, and nothing it reads reaches the conversation except its
  report.
- **Or a fork of the conversation.** With `context: "fork"`, each subagent starts from the
  conversation so far and then gets its task, so the task can refer to what was already found or
  decided. A fork is sent the run's own prompt and tool list, so a provider can reuse what it
  cached for the conversation up to the current prompt; the tool rounds of the current run are
  sent again. Every forked subagent rereads the whole conversation, which costs its length again
  for each task, so the model is told to fork only when restating the context would be long. A
  fork is shown every tool the run has and is still refused all but the read-only ones.
- **Only read-only tools.** A subagent is offered the read-only tools the run already offers: `read`,
  `ls`, `grep`, `find`, and MCP tools whose `tool_safety` you set to `"read"`. Tool selection and
  plan mode apply as for direct calls. It gets neither `spawn_agent` nor `codemode`. With
  [web search](providers.md#web-search) on, it can search the web as the run can; its searches
  are not listed in the transcript, and they mark the session's cost as a lower bound.
  It reads text only: when a tool returns an image, such as `read` on a picture, the result says
  the image was left out.
- **Each call is checked on its own.** Every call a subagent makes passes the same checks as a
  direct call. A subagent cannot ask for an approval, so a call that would need one fails. What it
  reads does not count as seen by the run, so the model still reads a file itself before editing it.
- **Each call is recorded.** As for a codemode script, the `spawn_agent` result lists every call
  the subagent made in `nested_calls`, and clients receive `tool.nested.started` and
  `tool.nested.ended` while it works. The terminal UI shows one card that lists the task and
  these calls when opened.
- **Its model calls are billed.** A subagent makes its own model requests. The result's
  `model_usage` lists each response's tokens and cost, the session saves it, and session usage and
  cost totals include it. A run cancelled while subagents are working records the call as
  interrupted and still lists the responses they had completed, so that spend stays in the
  totals. A response still being written when the run was cancelled reports no usage and is not
  counted.
- **Several at once.** One call takes up to eight tasks and runs their subagents at the same
  time, four at once, the rest waiting their turn. The result gives each report under a
  `## Task N` heading; a subagent that fails says why in its place and the others still report.
  The run continues once all of them have. A tool that is not safe to run in parallel still runs
  for one subagent at a time, though it can overlap a parallel-safe call.
- **Short reports.** A subagent is asked for a report of at most 500 words. A report longer than
  8,000 bytes, or than its share of the result when seven or eight tasks report at once, is cut
  and marked, so one subagent cannot fill the context the model delegated to protect.
- **Limits.** A subagent works for at most 63 tool rounds; one that reaches the limit returns
  what it has, after a note saying so. Cancelling a run stops its subagents.

The setting is user-only, because a subagent spends on your provider account. A run that an
operation limits to named tools, such as `/init`, is not offered the tool. In the terminal UI,
`/subagents` shows and saves the setting, chooses the subagent model, and describes what each
kind of subagent gets; RPC and SDK clients use `get_subagents`, `set_subagents` and
`set_subagent_model`.

**A cheaper model for subagents.** The Model row of `/subagents`, `"subagent_model"` in
`~/.wisp/settings.json`, or `WISP_SUBAGENT_MODEL` names the model a subagent that starts fresh uses, on the same provider
as the session. Subagents mostly read and search, so a smaller model often does. The session's
reasoning effort carries over when that model supports it, and otherwise the model's default
applies. If the model catalog lists the current provider without that model, as after switching
provider, subagents use the session's model; a run warns about it, once per conversation, and
`/subagents` says so. On a provider the catalog
does not list, such as an OpenAI-compatible endpoint, the setting is used as given. A forked subagent always uses the session's model,
because only that model can reuse what the provider cached for the conversation. The setting is
user-only, like `subagents`.

## Saved project permissions

The Rust permission dialog offers allow once, allow this tool for the session, saved project YOLO,
and deny. `/permissions` shows the current default; `/permissions ask` or `/permissions yolo`
changes it. Defaults are stored outside the repository in `~/.wisp/permissions/`, keyed by the
canonical project directory, and survive restarts. Changing the default clears temporary grants.
One-time and session grants are not saved, and starting or switching sessions expires session
grants. YOLO allows mutating and command tools without approval; project trust, tool availability,
and the file tools' [protected paths](#protected-paths) still apply. `--yes` alone remains a
temporary startup choice. In the home folder or a folder above it, YOLO is not saved and lasts for
the session; see
[Home folder and broad folders](#home-folder-and-broad-folders).
