---
title: MCP servers & codemode
description: Connect and authenticate MCP servers, control tool discovery and approvals, and use codemode to orchestrate tool workflows with per-call approvals.
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
**codemode** lets it batch and filter tool calls inside a sandboxed Python script. Neither grants
extra permissions—every remote tool defaults to `command` safety and requires approval unless you
explicitly mark it `"read"` in `tool_safety`.

Start by [adding a server](#add-a-server), choose [direct or on-demand discovery](#finding-mcp-tools-on-demand),
and see [MCP with codemode](#mcp-with-codemode) for batching multi-call workflows. For exact byte,
timeout, and storage caps across MCP and codemode, see
[Configuration: Tool, MCP, codemode, and subagent limits](../reference/configuration.md#tool-mcp-codemode-and-subagent-limits).

## Add a server

MCP servers live only in your user settings file (`~/.wisp/settings.json`)—never in project files.
Use `wisp mcp` from your shell (or press `a` in `/mcp` inside the TUI):

```text
wisp mcp add github --env-from GITHUB_TOKEN -- npx -y @modelcontextprotocol/server-github
wisp mcp add openai-developer-docs --url https://developers.openai.com/mcp
wisp mcp add linear --url https://mcp.linear.app/mcp --header-from "Authorization=Bearer LINEAR_API_KEY"
wisp mcp import snippet.json
wisp mcp list
```

Before saving a stdio server, `wisp mcp` shows the exact command it will run locally as your user on
every launch and asks for confirmation (`--yes` skips confirmation for scripts). Prefer `--env-from`
and `--header-from` so secrets stay in your environment; `--env NAME` and `--header NAME` prompt
with masked input and store values in `~/.wisp/settings.json`. See the
[CLI reference](../reference/cli.md#mcp-servers) for all flags.

You can also edit `~/.wisp/settings.json` directly:

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

Use one transport per server: `command` for local stdio processes, or `url` (HTTPS, or HTTP on
loopback) for Streamable HTTP. `headers_from` forwards an environment variable only to the
configured URL's origin and never logs or shows header values to the model.

## Signing in with OAuth

For hosted servers that use OAuth (such as Linear), add the URL and sign in via your browser:

```text
wisp mcp add linear --url https://mcp.linear.app/mcp
wisp mcp login linear
```

In the TUI, select the server in `/mcp` and press `l` to sign in or `o` to sign out (`wisp mcp logout NAME`
in the CLI). Tokens live in private `~/.wisp/mcp-auth.json` (separate from provider credentials in
`~/.wisp/auth.json`, protected from file tools, bound to the server's exact URL/origin, and
refreshed automatically). Wisp never opens a browser on its own.

## Settings and tool safety

- **Local process isolation:** Stdio servers run from `$HOME` with only a minimal safe environment
  plus your configured `env`/`env_from` values (missing `env_from` skips that server) and suppressed
  stderr. Local file-tool protections do not sandbox an MCP server.
- **Explicit safety classification:** Discovered tools are named `mcp__<server>__<tool>` and
  default to `command` safety (requiring approval). Only your user-owned `tool_safety` map (keyed by
  the remote tool name) can mark a tool `"read"` or `"mutating"`.
- **Untrusted server hints:** Wisp never auto-trusts MCP `readOnlyHint` annotations. In `/mcp`,
  unclassified hinted tools show a `hint` badge; press `h` to review and confirm copying them into
  `tool_safety` as `"read"` before reconnecting (`c`).

## Connection limits

Startup is failure-isolated: one broken server reports a sanitized error while healthy servers and
built-in tools stay available, and each server's catalog registers atomically. Set `"enabled": false`
or run `wisp mcp disable NAME` to keep a server's configuration without starting it. For exact
server, tool, definition-size, frame, and timeout caps (16 servers, 256 tools, 10 s deadline), see
[Configuration: MCP servers, OAuth, and discovery](../reference/configuration.md#mcp-servers-oauth-and-discovery).

> [!TIP]
> **Why 16 servers**
>
> Every stdio server is a separate local process, so startup time and memory use grow with the
> number and implementation of the configured servers. Wisp keeps this cap until local servers can
> be started lazily on demand.

Current MCP support covers stdio and Streamable HTTP tool discovery plus bounded text results
(resources, prompts, dynamic `tools/list_changed`, legacy HTTP+SSE, and pre-registered OAuth clients
are not yet supported).

## Managing MCP servers

Open `/mcp` in the TUI to inspect server commands/URLs, forwarded variables, headers, errors, and
tools (`↑`/`↓` select, `PgUp`/`PgDn` scroll details):

| Key | Action |
|---|---|
| `a` | Add a server by pasting its `{"mcpServers": {...}}` snippet (prompts for secret values rather than saving placeholders) |
| `e` | Disable the selected server, or enable it after confirming what it runs |
| `d` | Remove the selected server, after confirming |
| `l` / `o` | Sign in with OAuth in your browser / sign out |
| `h` | Review and confirm marking server-hinted `readOnlyHint` tools as `"read"` |
| `c` | Reconnect: reload `~/.wisp/settings.json`, restart servers, and clear MCP session grants |
| `r` | Refresh status without reconnecting |
| `Esc` | Close, step back, or cancel an active sign-in |

Adding, enabling, and removing always dock a confirmation showing exactly what will run or connect,
with no pre-selected default. Changes apply once the current prompt finishes.

## Finding MCP tools on demand

When a run's eligible MCP catalog is large, Wisp replaces the individual definitions with three
compact tools: `mcp_search` (query by keywords), `mcp_describe` (full schema for one tool), and
`mcp_call` (invoke by name). The system prompt still names each server and tool count so the model
knows what is available.

Control exposure with user-only `mcp_tool_exposure` in `~/.wisp/settings.json` or
`WISP_MCP_TOOL_EXPOSURE`:

| Value | When MCP definitions reach the model |
|---|---|
| `"auto"` (default) | On demand at ≥ 2,000 estimated tokens of eligible definitions; direct below that |
| `"direct"` | Every definition on every request |
| `"on_demand"` | Always via `mcp_search`, `mcp_describe`, and `mcp_call` |

`mcp_call` resolves the target tool before policy checks, so tool selection, plan mode, approvals,
and session grants apply to the underlying tool exactly as in direct mode.

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

On a single question, on-demand discovery breaks even around 1,800 estimated tokens of definitions;
for small catalogs (6–12 tools) it costs **more** tokens and extra latency, which is why `auto`
stays `direct` below 2,000 tokens. Committed baselines live in `evals/tool_use/baselines/`
(`2026-11-08-mcp-*` and `2026-11-09-mcp-sweep-*`; compare with `uv run python -m evals.tool_use compare`).

## Asking Wisp to set up a server

You can ask Wisp to add, disable, or remove a server (for example, "add the Linear MCP server").
Because `~/.wisp/settings.json` is protected from file tools, Wisp uses `list_mcp_servers`,
`propose_mcp_server`, and `propose_mcp_server_change`:

- Every proposal opens a mandatory **Confirm** / **Deny** prompt showing the exact command or URL,
  forwarded `env_from` variables, and target host for any `headers_from` variable. No permission
  mode (`YOLO`, `--yes`, or session grant) can auto-approve a proposal, and print/JSON modes refuse
  them.
- Secrets never pass through the model—arguments or URLs that look like tokens are rejected before
  prompting (use `/mcp` or `wisp mcp add --env` for literal secrets). Confirming restarts MCP
  servers after the reply completes.

## MCP with codemode

Codemode lets the model batch and filter tool calls in a short sandboxed Python script instead of
spending a model turn and context window on every lookup. Only printed output, the last expression,
and a tool-call summary return to the conversation.

### Enable it and check availability

Install the optional `codemode` extra (which adds [monty](https://github.com/pydantic/monty)) and
restart Wisp:

```bash
uv tool install --force --python 3.12 'wisp-ai[codemode]'
```

For source checkouts, run `uv sync --extra codemode`. With the extra installed, control availability
and presentation via `/codemode` in the TUI, user-only settings (`codemode`,
`codemode_presentation`), or `WISP_CODEMODE` / `WISP_CODEMODE_PRESENTATION`:

- **Availability (`codemode`):** `null`/omitted (**automatic**) offers codemode whenever the run has
  at least one eligible MCP tool; `true` also enables local-only workflows; `false` disables it.
- **Presentation (`codemode_presentation`):** `direct_and_codemode` (default) exposes direct tools
  alongside codemode; `codemode_only` hides direct schemas and exposes `codemode`, `tool_search`,
  and `tool_describe` (falling back to direct tools whenever codemode is unavailable).

### Classify MCP tools accurately

Inspect a server's contract before marking a remote tool `"read"` in `tool_safety`—that skips
per-call command approval, so it is a security decision, not a performance switch. Command and
mutating tools can still be scripted under their normal approval prompts.

Example configuration for a verified read-only `get_item` tool on an `inventory` server (merge into
`~/.wisp/settings.json` and reconnect with `c` in `/mcp`):

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

### From discovery to a filtered result

Inside a script, callable tools are async keyword-argument functions (use `mcp_call(name=..., arguments=...)`
for non-Python identifiers or parameter names, and `await gather_settled(...)` to collect partial
outcomes without aborting on one tool error). MCP tools that declare an output schema return typed
dicts validated before execution:

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

### Inspect, calculate, edit, and test

With `codemode: true`, scripts can combine eligible local tools too. Inspect first:

```python
print(await read(path="settings.py"))
```

Then edit, write, and test (each mutating or `bash` child follows its normal policy and approval
checks; failed tests do not roll back completed file edits):

```python
retry_seconds = 2 * 2
print(await edit(path="settings.py", edits=[{
    "oldText": "RETRY_SECONDS = 2",
    "newText": "RETRY_SECONDS = " + str(retry_seconds),
}]))
print(await write(path="retry-note.txt", content="Retry interval: " + str(retry_seconds) + " seconds\n"))
print(await bash(operation="run", command="uv run pytest tests/test_settings.py"))
```

Foreground `bash(operation="run", ...)` children end with the script; use `bash(operation="start", ...)`
and `poll`/`cancel` for processes that outlive the script (inspect them in `/ps`).

### Keeping data across scripts

Use `store`, `load`, and `delete` for branch-local JSON state that survives restarts and compaction
without filling model context:

```python
await store("inventory", {"A": 3, "B": 0})
items = await load("inventory")
print([name for name, count in items.items() if count == 0])
await delete("inventory")
```

Staged state writes commit only when the script succeeds and saves—errors or cancellation discard
them, while file, shell, and MCP side effects are never rolled back (Wisp disables automatic
overflow replay for codemode so side effects are not repeated).

### Why a script cannot call a tool

| Symptom | Check or next step |
|---|---|
| Codemode is missing | Check the `wisp-ai[codemode]` extra, `/codemode`, and `WISP_CODEMODE`. Automatic mode needs an eligible MCP tool; set `codemode: true` for local-only workflows. |
| A tool is missing | Check tool selection, plan mode, and schema, and reconnect MCP servers (`c` in `/mcp`). |
| A write or command call is refused | Check the child call's policy, plan mode, protected paths, and approval outcome. Allowing codemode never approves its children. |
| A child tool requires confirmation | Child approvals serialize over the live event channel; without one (e.g. non-interactive without `--yes`), the call fails. |
| Script type-check fails | Inspect the tool with `tool_describe` / `mcp_describe` and fix argument names or types; scripts are checked before any calls run. |
| A server fails or needs sign-in | Fix the server connection or OAuth sign-in in `/mcp`. See [Troubleshooting](#troubleshooting). |

## Scripting tool calls with codemode (experimental)

- **Isolated sandbox, normal policy:** Scripts run in a separate `monty` process with no direct
  filesystem, network, environment, or third-party package access. Every child tool call goes
  through Wisp's normal policy, protected paths, plan mode, and per-call approval prompt (child
  approvals serialize; approving codemode grants nothing to children; scripts cannot invoke
  `codemode` or `spawn_agent`).
- **Full audit trail & recoverable output:** Every child call (including refused or interrupted
  calls) is saved in `nested_calls` and shown inside the TUI's script card. Oversized script output
  is saved to the shared process-local `read_output` store so the model can page through it without
  rerunning the script.
- **Resource & state limits:** Exact execution, nested-result, discovery, `store`/`load`/`delete`,
  and `read_output` caps live in
  [Configuration: Codemode and subagents](../reference/configuration.md#codemode-and-subagents).

## Troubleshooting

Open `/mcp` to check server status and sanitized errors, confirm required environment variables were
set at startup, and sign in with `l` if prompted (`r` refreshes status; `c` reloads settings and
reconnects, clearing MCP session grants). See
[MCP connection troubleshooting](troubleshooting.md#an-mcp-server-will-not-start-or-authenticate)
for a symptom checklist and [Why a script cannot call a tool](#why-a-script-cannot-call-a-tool) for
script issues.
