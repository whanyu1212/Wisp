# Tools & safety

Wisp includes built-in local tools for reading files, editing files, searching projects, and running
shell commands. File tools are sandboxed to the tool context's working directory.

| Category | Tools | Approval |
|----------|-------|----------|
| **Read** | `read` · `grep` · `find` · `ls` | Runs directly |
| **Mutating** | `write` · `edit` | Required |
| **Command** | `bash` | Required |

Approval decisions stay outside the model's control — see [Staying in sync](./staying-in-sync) for
why that boundary matters and how the shared runtime enforces it.

`bash` defaults to one-shot execution and reports stdout, stderr, truncation state, and exit code.
It also accepts `operation=start|poll|cancel` for commands needing a retained process handle; those
return a `process_id`, process state, incremental output, and per-stream truncation metadata under
the same safety category and approval policy.

## Print mode exposes no tools unless you ask

Read tools are enabled as a group; mutating and command tools require per-tool opt-in:

```bash
wisp -p "list files" --allow-read-tools
wisp -p "run tests"  --allow-tool bash --yes
```

Because print mode is non-interactive, mutating and command tools are also blocked at execution time
unless you pass `--yes` (alias `--allow-unsafe-tool-execution`). Without it the model receives a
clear tool error instead of Wisp executing the operation.

Wisp does not cap model/tool rounds by default. Pass `--max-tool-iterations <n>` for a
non-interactive fuse.

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
Wisp starts. See the [CLI reference](../reference/cli#mcp-servers) for every command.

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
(Linear's 68 tools are about 100 KB) adds to each request's size and cost. Connection and discovery have a
10-second per-server deadline. A server's catalog is registered atomically, so invalid definitions,
duplicate names, collisions, or limit violations expose none of that server's tools.

Set `"enabled": false` on a server, or run `wisp mcp disable NAME`, to keep its settings without
starting it.

Run `/mcp` in the TUI to inspect server status, what each server runs, registered tool names, and
sanitized startup failures, and to add, remove, enable, disable, or reconnect servers without
restarting Wisp; see [Managing MCP servers](./tui#managing-mcp-servers). Adding or enabling a server
always shows exactly what it runs and asks for confirmation. SDK and RPC clients can make the same
changes; see the [SDK reference](../reference/sdk). Changes made with `wisp mcp` apply the next time
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

With [web search](./providers#web-search) on, Wisp can look up a server's command or URL itself,
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

## Saved project permissions

The Rust permission dialog offers allow once, allow this tool for the session, saved project YOLO,
and deny. `/permissions` shows the current default; `/permissions ask` or `/permissions yolo`
changes it. Defaults are stored outside the repository in `~/.wisp/permissions/`, keyed by the
canonical project directory, and survive restarts. Changing the default clears temporary grants.
One-time and session grants are not saved, and starting or switching sessions expires session
grants. YOLO allows mutating and command tools without approval; protected paths, project trust,
and tool availability still apply. `--yes` alone remains a temporary startup choice.
