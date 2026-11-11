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
at most 600. When every process slot is taken, the error lists the processes holding them, so the
model can poll or cancel one.

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

Wisp can connect to user-configured [Model Context Protocol](https://modelcontextprotocol.io/)
servers over stdio or Streamable HTTP. Servers live only in the user settings file at
`~/.wisp/settings.json`. The `wisp mcp` commands edit it for you:

```text
wisp mcp add github --env-from GITHUB_TOKEN -- npx -y @modelcontextprotocol/server-github
wisp mcp add openai-developer-docs --url https://developers.openai.com/mcp
wisp mcp add linear --url https://mcp.linear.app/mcp --header-from "Authorization=Bearer LINEAR_API_KEY"
wisp mcp import snippet.json
wisp mcp list
```

Before saving a stdio server, `wisp mcp` shows the exact command it will run on every launch and
asks for confirmation; `--yes` skips the question for scripted setup. `--env NAME` prompts for the
value with masked input, and `import` asks again for each `env` value in a pasted
`{"mcpServers": {...}}` snippet, because published snippets usually hold placeholders. Leave an
answer empty to forward that variable from your environment instead. Changes apply the next time
Wisp starts. See the [CLI reference](../reference/cli.md#mcp-servers) for every command.

You can also edit the file directly:

```json
{
  "mcp_servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"READ_ONLY": "1"},
      "env_from": ["GITHUB_TOKEN"],
      "tool_safety": {"search_repositories": "read"}
    },
    "openai-developer-docs": {
      "url": "https://developers.openai.com/mcp",
      "tool_safety": {"search_openai_docs": "read", "fetch_openai_doc": "read"}
    },
    "linear": {
      "url": "https://mcp.linear.app/mcp",
      "headers_from": {"Authorization": "Bearer ${LINEAR_API_KEY}"}
    }
  }
}
```

Configure exactly one transport per server: `command` starts a stdio process, while `url` connects
directly over Streamable HTTP. Remote URLs require HTTPS except for loopback development endpoints
and cannot contain credentials, query strings, or fragments. HTTP entries do not accept stdio-only
`args`, `env`, or `env_from` fields. Configure the final endpoint URL: redirects are not followed.
HTTP responses must be uncompressed; JSON bodies and streamed events have bounded sizes.

For HTTP servers that need authentication, `headers` holds literal header values, stored like `env`,
and `headers_from` builds a header from an environment variable each time Wisp connects. A
`headers_from` value is `"${VAR}"` or a scheme word and the variable, such as
`"Bearer ${LINEAR_API_KEY}"`; nothing else, so the visible part can never hold a credential. If the
variable is unset, or its value is empty, starts or ends with a space, or contains a line break or
control character, the server is skipped.
Headers go only to the configured URL's origin (scheme, host, and port), never to another host, and
cannot replace the headers the transport manages (such as `Host`, `Accept-Encoding`, or
`Mcp-Session-Id`). Header values are never shown, logged, or sent to the model. A server's
`headers_from` sends that variable's value to the server, so every confirmation names the variable
and the host it goes to. A pasted snippet's `headers` work the same way: a value written as
`"Bearer ${VAR}"` forwards that variable, and any other value is asked for again (masked), because
published snippets usually hold placeholders.

#### Signing in with OAuth

Many hosted servers, such as Linear's, use OAuth instead of a token you paste. Add the server by URL,
then sign in:

```text
wisp mcp add linear --url https://mcp.linear.app/mcp
wisp mcp login linear
```

`login` opens the server's sign-in page in your browser and waits up to five minutes for the
browser to return to Wisp, on a one-time `http://127.0.0.1` address. Wisp follows the MCP
authorization spec: it discovers the server's authorization server, registers itself as a client,
and uses PKCE with state and issuer checks. It opens the sign-in page only if it is HTTPS (or HTTP on
a loopback address). Nothing is saved unless the whole sign-in succeeds.

The tokens are saved in `~/.wisp/mcp-auth.json`, readable only by you, separate from `auth.json`
and protected from Wisp's file tools like it. Each sign-in is bound to the server's URL: if the URL
changes, the tokens are never sent to the new one. The access token goes only to the server's own
origin, never to another host, and is never shown, logged, or sent to the model. Wisp refreshes it
automatically. When a server refuses Wisp, or a refresh fails, the server shows as unavailable with
"sign in with `wisp mcp login NAME`"; Wisp never opens a browser on its own. In the TUI, press `l`
on the server in `/mcp` instead. `wisp mcp logout NAME` (or `o` in `/mcp`) forgets a sign-in, and removing a server forgets it too. A server that sends its own
`Authorization` header does not sign in. Servers that require a pre-registered client, rather than
registering Wisp themselves, are not supported yet.

For stdio servers, `env` contains literal user-owned values. `env_from` forwards only the named
variables from Wisp's process environment; if one is missing, that server is skipped. Server
processes otherwise receive only the MCP SDK's small safe environment baseline, run from the user's
home directory rather than the active project, and have stderr suppressed. Commands, arguments,
environment values, URLs, stderr, and transport errors are never included in MCP startup diagnostics.

Discovered tools are named `mcp__<server>__<tool>`, with deterministic normalization and hashing when
needed. They follow the same exposure flags as built-ins: use `--allow-tool <name>` or `--all-tools`,
while `--allow-read-tools` also includes MCP tools explicitly assigned `read` safety. Remote tools
default to `command` safety and require approval; server-provided annotations cannot weaken this
policy. `tool_safety` is the only way to assign `read` or `mutating` safety and matches the remote
tool name exactly.

Startup is failure-isolated: an unavailable or malformed server produces a sanitized error event
while healthy servers and built-in tools remain available. Wisp accepts at most 16 configured
servers, 64 discovery pages per server, 256 MCP tools overall, 1 MiB of definitions per server,
4 MiB overall, and 2 MiB per protocol frame before parsing. One server may bring any number of tools
within those limits. Every tool's definition is sent with every model request, so a large catalog
(Linear's 68 tools are about 100 KB) adds to each request's size and cost; see
[Finding MCP tools on demand](#finding-mcp-tools-on-demand). Connection and discovery have a
10-second per-server deadline. A server's catalog is registered atomically, so invalid definitions,
duplicate names, collisions, or limit violations expose none of that server's tools.

Set `"enabled": false` on a server, or run `wisp mcp disable NAME`, to keep its settings without
starting it.

### Finding MCP tools on demand

When a run's MCP tool definitions are large, the model is not sent each one. It gets three small
tools instead:

| Tool | What it does |
|---|---|
| `mcp_search` | Finds MCP tools by query and lists each match's server, summary and parameters |
| `mcp_describe` | Returns one tool's full description and input schema |
| `mcp_call` | Runs one of those tools by name, with its arguments |

The system prompt names each server and how many tools it has, so the model knows what to search.

`"mcp_tool_exposure"` in `~/.wisp/settings.json`, or `WISP_MCP_TOOL_EXPOSURE` for one process,
chooses when this happens:

| Value | MCP tools reach the model |
|---|---|
| `"auto"` (default) | On demand when the run's MCP tool definitions come to at least 2,000 tokens, by the estimate `/stats` uses; directly below that |
| `"direct"` | Every definition, on every request |
| `"on_demand"` | Always through `mcp_search`, `mcp_describe` and `mcp_call` |

`auto` decides for each run from the MCP tools that run may use, so `--allow-tool` or plan mode can
bring a large catalog under the threshold. 2,000 tokens is roughly 20 small tools or a handful of
real-server ones. The setting is user-only: a project cannot change what the model sees or what
each request costs.

`mcp_call` reaches only the MCP tools direct mode would have offered that run: tool selection
(`--allow-tool`, `--all-tools`), plan mode and the run's own tool limits all apply, and built-in tools
are refused. Wisp resolves each `mcp_call` to the tool it names before any check, so that tool's own
safety decides: a `command` tool asks for approval exactly as a direct call does, the approval and
"allow for this session" name that tool, and a grant applies to that tool alone. A call missing a
required parameter is rejected before you are asked; everything else, including types, is left to
the server to validate, exactly as for a direct call. Events for a proxied call keep `name: "mcp_call"`
and add `target` with the tool that runs.

#### What on-demand discovery saves

Measured with Wisp's tool-use evals (`evals/tool_use/`), 5 runs of each task in each mode on
`gpt-6.1-sol` and `gpt-5.6-luna` through Codex. Every run passed in both modes, and 1 of 68 proxied
calls failed, on an argument the model then corrected.

| Catalog | Task | Tokens per run, direct → on demand | Tool calls |
|---|---|---|---|
| 102 tools, real-server-sized definitions (about 25k tokens) | multi-step triage | 124k → 28k (−77%), 153k → 37k (−76%) | about double |
| 50 small tools (about 3k tokens) | pick one tool | 16k → 12k (−28%), 17k → 9k (−46%) | 2 → 3–4 |
| 6 tools | pick one tool | 7k → 10k (+34%), 8k → 9k (+20%) | 2 → 3–4 |

The two figures in each row are `gpt-5.6-luna` and `gpt-6.1-sol`. Counting only input not served
from the provider's prompt cache, the triage task still fell 60–72%. Runs took 10–35% longer, from
the extra search and describe turns.

To place `auto`'s threshold, a sweep ran the one-question inventory task with 6 to 50 of its tools,
again 5 runs a side on both models, all passing. Sizes here are Wisp's own estimate, the one `auto`
uses, which runs about 1.4 times the provider's count:

| Inventory tools | Definitions (Wisp's estimate) | Tokens per run, on demand against direct |
|---|---|---|
| 6 | about 560 | +45%, +8% |
| 12 | about 1,100 | +36%, +3% |
| 20 | about 1,800 | +6%, −8% |
| 30 | about 2,700 | −9%, −32% |
| 50 | about 4,400 | −34%, −49% |

The 50-tool row is the same task as in the first table, measured again in a separate run, so its
figures differ a little from the first table's. On a single question, on demand breaks even at
about 1,800 tokens of definitions. Over tasks of
several steps it saves more, since each request would otherwise resend every definition. Below the
threshold, `auto` keeps `direct`, which is also faster.

The committed baselines in `evals/tool_use/baselines/` hold every run:
`2026-11-08-mcp-direct.json` and `2026-11-08-mcp-on-demand.json` for the table above, and
`2026-11-09-mcp-sweep-direct.json` and `2026-11-09-mcp-sweep-on-demand.json` for the sweep. Compare
them with `uv run python -m evals.tool_use compare`.

Run `/mcp` in the TUI to inspect server status, what each server runs, registered tool names, and
sanitized startup failures, and to add, remove, enable, disable, or reconnect servers without
restarting Wisp; see [Managing MCP servers](tui.md#managing-mcp-servers). Adding or enabling a server
always shows exactly what it runs and asks for confirmation. SDK and RPC clients can make the same
changes; see the [SDK reference](../reference/sdk.md). Changes made with `wisp mcp` apply the next time
Wisp starts, or after a reconnect (`c` in `/mcp`).

> [!TIP]
> **Why 16 servers**
>
> Every stdio server is a separate local process, so startup time and memory use grow with the number
> and implementation of the configured servers. Wisp may revisit this limit when it can avoid eagerly
> starting every local server, rather than raising it without a lifecycle or lazy-start solution.

Current MCP support covers stdio and Streamable HTTP tool discovery plus bounded text results.
Resources, prompts, dynamic `tools/list_changed` updates, legacy HTTP+SSE, and OAuth clients that
must be registered in advance are not yet supported.

### Asking Wisp to set up a server

You can ask Wisp to add, remove, or disable an MCP server, for example "set up the GitHub MCP
server". Saving a server means running its command every time Wisp starts, so Wisp proposes the
change instead of making it: `~/.wisp/settings.json` is protected from its file tools, and it calls
one of these tools. (Shell commands are not checked against protected paths, so under YOLO or an
"allow bash for this session" grant a shell command could still edit the file, as it could any of
your files. Keep `bash` on ask when that matters.)

| Tool | Safety | What it does |
|---|---|---|
| `list_mcp_servers` | read | Lists saved servers: the executable or URL origin and the environment variable names; never arguments, URL paths, or values |
| `propose_mcp_server` | command | Proposes adding (or replacing) a server: name, command and arguments or URL, `env_from` names, and `headers_from` for an HTTP server's token |
| `propose_mcp_server_change` | command | Proposes removing or disabling a saved server |

With [web search](providers.md#web-search) on, Wisp can look up a server's command or URL itself,
for example "add the Linear MCP server". What it finds is still only a proposal. For a hosted server,
Wisp proposes its URL and [signs in with OAuth](#signing-in-with-oauth) itself, which it prefers
over API keys; a proposal that wraps a Streamable HTTP URL in a local bridge such as `mcp-remote` is
refused with a pointer to the URL instead. A bridge is still allowed for a server that only offers a
legacy SSE endpoint, which Wisp cannot reach directly. After you confirm one, press `l` in `/mcp` if it shows "sign in required".

Every proposal opens a confirmation that shows exactly what the server would run or connect to and
which variables it forwards. A header built from a variable is shown with the host that receives
it, such as "sends $LINEAR_API_KEY to mcp.linear.app in header Authorization". A proposal with any
field the confirmation does not show cannot be confirmed. Only **Confirm** or **Deny** is offered: no permission mode, `--yes`,
or "allow for this session" answer ever approves a proposal, and a confirmation cannot be accepted
while part of it is hidden. In print and JSON modes a proposal is always refused. Confirming saves
the change from exactly the arguments you were shown; MCP servers then restart when the reply ends,
so a new server's tools are available from your next message.

Secrets never pass through the model. A proposal forwards environment variables by name with
`env_from`. Arguments or URLs that look like they carry a secret (known token formats,
`--api-key VALUE`, `TOKEN=…`, `scheme://user:password@…`, an `Authorization` header, an opaque
token in a URL path) are rejected before you are asked, as are line breaks and control characters.
This is a heuristic, so read every proposal before confirming. To store a value in the settings file instead, add the server from
`/mcp` or with `wisp mcp add --env`.

## Scripting tool calls with codemode (experimental)

With `"codemode": true` in `~/.wisp/settings.json`, or `WISP_CODEMODE=1` for one process, the model
also gets a `codemode` tool. It writes a short Python script that calls the run's read-only tools as
async functions, and only what the script prints and its last expression come back. One script can
run many calls at once with `asyncio.gather`, chain calls on earlier answers, and filter large results
before the model reads them. Install the `codemode` extra first (`pip install 'wisp-ai[codemode]'`);
it adds [monty](https://github.com/pydantic/monty), the sandbox scripts run in. monty needs Python 3.12
or newer; on 3.11 the extra installs nothing and the model is not offered `codemode`.

- **Only read-only tools.** A script can call the read-only tools the run already offers: `read`,
  `ls`, `grep`, `find`, and MCP tools whose `tool_safety` you set to `"read"`. Tool selection and
  plan mode apply as for direct calls. When [MCP tools are found on demand](#finding-mcp-tools-on-demand),
  a script can also call a read-only tool `mcp_search` finds by its full name, when that name and
  its parameters are Python identifiers, without the codemode description listing them. Other
  tools stay reachable through `mcp_call`.
- **Each call is checked on its own.** Every call a script makes passes the same checks as a direct
  call. A script cannot ask for an approval, so a call that would need one fails, and the script can
  catch the error. Approving or allowing the script grants nothing to the calls it makes.
- **A sandbox, not a shell.** Scripts run in a separate process with no files, network, environment
  or third-party packages, in a subset of Python. They are type-checked against the tools'
  declarations before anything runs.
- **Limits.** 200 tool calls, 2 minutes, 10 seconds of the script's own computing and 128 MiB per
  script. A script cannot start another script, and cancelling a run kills it.

It is off by default while its effect is measured on the tool-use evals (HY-431).

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
