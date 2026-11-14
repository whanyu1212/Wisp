---
title: MCP servers & codemode
description: Connect and authenticate MCP servers, control tool discovery and approvals, and use codemode to batch read-only calls without flooding model context.
route: guide/mcp.html
section: using
order: 8
layout: docs
listed: true
searchable: true
navTitle: MCP & codemode
---

# MCP servers & codemode

MCP connects Wisp to external tools. **Discovery** decides which tool definitions the model sees;
**codemode** lets it orchestrate read-only calls in a sandboxed Python script. Neither changes
what a server is allowed to do or grants extra permissions.

Start by [adding a server](#add-a-server), then choose [direct or on-demand discovery](#finding-mcp-tools-on-demand).
For larger catalogs, see [MCP with codemode](#mcp-with-codemode) for setup, an example, and common pitfalls.

## Add a server

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

## Signing in with OAuth

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

## Settings and tool safety

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

## Connection limits

Startup is failure-isolated: an unavailable or malformed server produces a sanitized error event
while healthy servers and built-in tools remain available. Wisp accepts at most 16 configured
servers, 64 discovery pages per server, 256 MCP tools overall, 1 MiB of definitions per server,
4 MiB overall, and 2 MiB per protocol frame before parsing. One server may bring any number of tools
within those limits. With direct exposure, every tool's definition is sent with every model request,
so a large catalog adds to each request's size and cost; see
[Finding MCP tools on demand](#finding-mcp-tools-on-demand). Connection and discovery have a
10-second per-server deadline. A server's catalog is registered atomically, so invalid definitions,
duplicate names, collisions, or limit violations expose none of that server's tools.

Set `"enabled": false` on a server, or run `wisp mcp disable NAME`, to keep its settings without
starting it.

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
[Signing in with OAuth](#signing-in-with-oauth).

Adding, enabling, and removing end in a confirmation docked above the composer that shows exactly
what will run. Like an approval, it has no default: choose with `1`/`2`, or highlight with `↑`/`↓`
and press `Enter`; a key pressed before the panel was drawn does nothing. Confirming saves the change
and reconnects, and the list then shows the server's new status and tools. Changes wait until the
current prompt finishes. "Allow for this session" answers for MCP tools are cleared on every
reconnect.

## Finding MCP tools on demand

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

### What on-demand discovery saves

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
restarting Wisp; see [Managing MCP servers](#managing-mcp-servers). Adding or enabling a server
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

## Asking Wisp to set up a server

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

## MCP with codemode

On-demand discovery saves sending a full catalog on every request. Codemode solves a different
problem: making the model spend a turn on every lookup and read a large result each time. The model
writes a short Python script to batch or chain calls, then filters the results inside the script.
Only printed output and the last expression return as result content, plus a tool-call summary;
raw nested results do not enter the model's conversation. Calls still run on the MCP server and
may incur that service's costs. Codemode is not an MCP server, an alternative transport, or a way
to run arbitrary local Python.

### Enable it and check availability

Install the optional extra in the same environment as Wisp. For a persistent uv tool installation:

```bash
uv tool install --force --python 3.12 'wisp-ai[codemode]'
```

This adds [monty](https://github.com/pydantic/monty), the sandbox scripts run in. Restart Wisp after
installation. A plain `wisp-ai` install does not include it. For source development, use
`uv sync --extra codemode` instead. No server is installed or granted permissions by adding the extra.

The following table assumes the extra is installed and the run has eligible read-only tools:

| `mcp_tool_exposure` | `codemode` omitted or `null` (automatic) | `codemode: true` | `codemode: false` |
|---|---|---|---|
| `auto` | Offered only when this run chooses on-demand discovery | Offered | Not offered |
| `on_demand` | Offered when this run has an on-demand MCP catalog | Offered | Not offered |
| `direct` | Not offered | Offered | Not offered |

Both settings are user-only. `WISP_MCP_TOOL_EXPOSURE` and `WISP_CODEMODE` override their settings-file
values at startup. In the TUI, `/codemode` shows the setting, extra availability, and whether the
next prompt would offer the tool; choose automatic, always, or never there. `/codemode always` and
`/codemode never` apply those choices directly. Settings changes affect the next prompt, not a script
already running. RPC/SDK clients use `get_codemode` and `set_codemode` for the same information.

**Offered does not mean used.** The model chooses whether to call codemode. A single lookup often
needs no script; fan-out queries, dependent lookups, and filtering large answers benefit more.
On-demand discovery with codemode was the cheapest setup measured on Wisp's orchestration evals;
those task-specific results are not a promise that every workload will be cheaper or faster.

### Make only verified read tools scriptable

MCP tools default to `command` safety, even if their names sound read-only or the server advertises
read-only annotations. Codemode does not infer safety. Inspect the server's implementation and
contract before setting `tool_safety` for individual remote tool names; do not mark an entire
write-capable catalog read-only just to enable scripts. A `read` classification also permits calls
without the ordinary command approval, so it is a security decision, not a performance switch.

For example, suppose you operate a server named `inventory` at the placeholder endpoint below. It
has a verified read-only remote tool `get_item(item_id: string)` returning JSON text with `id` and
`stock` fields. Merge this into your user `~/.wisp/settings.json`, substituting your actual endpoint
and exact remote name (not the `mcp__inventory__`-prefixed name):

```json
{
  "mcp_tool_exposure": "on_demand",
  "mcp_servers": {
    "inventory": {
      "url": "https://inventory.example/mcp",
      "tool_safety": {"get_item": "read"}
    }
  }
}
```

Reconnect with `c` in `/mcp` after editing. This example leaves `codemode` automatic; with the extra
installed, the on-demand catalog and read-only tool make it available. Other tools on that server
remain `command` unless separately classified.

### From discovery to a filtered result

Ask Wisp, for example: “Check inventory items A and B and report only those with no stock. Use
codemode to avoid bringing full records into the conversation.” The model can:

1. Use `mcp_search` to find the inventory tool and `mcp_describe` to inspect its parameters.
2. Call codemode with a script using the full discovered tool name as an async function.
3. Return the filtered answer; make any requested write as a separate normal tool call.

For the hypothetical tool above, this is the script body passed in codemode's `code` argument,
not a command to paste into your shell. Both calls return text; `json.loads` is appropriate only
because this example server promises JSON text:

```python
import asyncio
import json

raw = await asyncio.gather(
    mcp__inventory__get_item(item_id="A"),
    mcp__inventory__get_item(item_id="B"),
)
items = [json.loads(text) for text in raw]
[(item["id"], item["stock"]) for item in items if item["stock"] == 0]
```

Tool functions take keyword arguments and return the same text as a normal call. A failed call
raises `RuntimeError`; the script can catch it to continue with other work. Independent calls can
use `asyncio.gather`; Wisp still serializes tools not marked parallel-safe. Scripts may also call
the run's read-only local tools, but only through their provided functions, not by opening files.

With on-demand discovery, read-only catalog tools need not appear in codemode's description to be
callable by full name. The name and all parameter names must be Python identifiers and not keywords.
For other schemas, use the provided `mcp_call(name=..., arguments=...)` function in the script;
its resolved target must still be read-only. Inspect the actual schema rather than assuming every
MCP tool returns JSON or has Python-compatible parameters.

### Why a script cannot call a tool

| Symptom | Check or next step |
|---|---|
| Codemode is missing | Check the installed extra, `/codemode`, environment overrides, and the discovery mode for this run. Automatic mode does not offer it for a small catalog sent directly. |
| A tool is missing or not read-only | Check tool selection, plan mode, the exact `tool_safety` remote name, and reconnect after changes. Otherwise call it normally; do not loosen safety just to make the script work. |
| A write or command call is refused | Expected: scripts cannot call non-read tools, even under YOLO, `--yes`, a session grant, or through `mcp_call`. Use a normal direct call or top-level `mcp_call`, under its usual approval policy. |
| A read tool requires confirmation | A script cannot open an approval prompt. Without an existing applicable grant, a confirmation-required call fails. |
| Script type-check fails | Describe the tool and correct its name, parameter types, and keyword arguments. Scripts are checked before any calls run. |
| A server fails or needs sign-in | Fix the server connection or authentication, not codemode. See [MCP troubleshooting](#troubleshooting). |

## Scripting tool calls with codemode (experimental)

Codemode's safety, audit, and resource limits apply regardless of discovery mode:

- **Only read-only tools.** A script can call the read-only tools the run already offers: `read`,
  `ls`, `grep`, `find`, and MCP tools whose `tool_safety` you set to `"read"`. Tool selection and
  plan mode apply as for direct calls. When [MCP tools are found on demand](#finding-mcp-tools-on-demand),
  a script can also call a read-only tool `mcp_search` finds by its full name, when that name and
  its parameters are Python identifiers, without the codemode description listing them. Other
  tools stay reachable through `mcp_call`.
- **Each call is checked on its own.** Every call a script makes passes the same checks as a direct
  call. A script cannot ask for an approval, so a call that would need one fails, and the script can
  catch the error. Approving or allowing the script grants nothing to the calls it makes.
- **Each call is recorded.** The script's `tool.execution.ended` and `tool.result` events list every
  call it made in `nested_calls`, with each call's arguments, outcome and timing. Refused calls
  are listed too, and so are calls the script ended before they finished. The session saves that
  list. While the script runs, RPC and SDK clients and `--mode json` also receive transient
  `tool.nested.started` and `tool.nested.ended` events, on a best-effort basis. The model still
  reads only what the script prints and returns. A run cancelled while a script is running
  records that script as interrupted, with no calls listed; its live events are then the only
  trace. The terminal UI shows each script as one card that lists these calls when opened, and
  a script's optional `title` names it there.
- **A sandbox, not a shell.** Scripts run in a separate process with no files, network, environment
  or third-party packages, in a subset of Python. They are type-checked against the tools'
  declarations before anything runs.
- **Limits.** 200 tool calls, 2 minutes, 10 seconds of the script's own computing, 128 MiB and
  1 MiB of printed output per script. A script that reaches one fails with the limit named. A
  script cannot start another script, and cancelling a run kills it along with the calls it has
  running.

## Troubleshooting

Start in `/mcp`: check the server's status and sanitized error, confirm needed environment variables
were present when Wisp started, and sign in with `l` if required. `r` only refreshes status; `c`
reloads settings and reconnects, clearing session grants. Stdio servers need their command installed;
HTTP servers need a supported final endpoint and valid authentication. Codemode cannot repair a
missing server or bypass its access controls.

See [MCP connection troubleshooting](troubleshooting.md#an-mcp-server-will-not-start-or-authenticate)
for a symptom checklist and safe diagnostic sharing. For a connected server whose tools cannot be
scripted, use [Why a script cannot call a tool](#why-a-script-cannot-call-a-tool).
