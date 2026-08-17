---
title: Tools & safety
---

# Tools & safety

Wisp includes built-in local tools for reading files, editing files, searching projects, and running
shell commands. File tools are sandboxed to the tool context's working directory.

| Category | Tools | Approval |
|----------|-------|----------|
| **Read** | `read` · `grep` · `find` · `ls` | Runs directly |
| **Mutating** | `write` · `edit` | Required |
| **Command** | `bash` | Required |

Approval decisions stay outside the model's control — see [Staying in sync](./staying-in-sync) for
why that boundary matters, and the [safety model](../architecture/safety) for how it is enforced.

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
- `WISP_TRUST_FILE` may relocate the global trust store, but only to an absolute path outside the
  repository. A relative value is rejected.

## MCP tools

Wisp can connect to user-configured [Model Context Protocol](https://modelcontextprotocol.io/)
servers over stdio. Add servers only to the user settings file at `~/.wisp/settings.json`:

```json
{
  "mcp_servers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"READ_ONLY": "1"},
      "env_from": ["GITHUB_TOKEN"],
      "tool_safety": {"search_repositories": "read"}
    }
  }
}
```

`env` contains literal user-owned values. `env_from` forwards only the named variables from Wisp's
process environment; if one is missing, that server is skipped. Server processes otherwise receive
only the MCP SDK's small safe environment baseline, run from the user's home directory rather than
the active project, and have stderr suppressed. Commands, arguments, environment values, stderr, and
transport errors are never included in MCP startup diagnostics.

Discovered tools are named `mcp__<server>__<tool>`, with deterministic normalization and hashing when
needed. They follow the same exposure flags as built-ins: use `--allow-tool <name>` or `--all-tools`,
while `--allow-read-tools` also includes MCP tools explicitly assigned `read` safety. Remote tools
default to `command` safety and require approval; server-provided annotations cannot weaken this
policy. `tool_safety` is the only way to assign `read` or `mutating` safety and matches the remote
tool name exactly.

Startup is failure-isolated: an unavailable or malformed server produces a sanitized error event
while healthy servers and built-in tools remain available. Wisp accepts at most 16 configured
servers, 64 discovery pages and 64 tools per server, 256 MCP tools overall, 1 MiB of definitions per
server, 4 MiB overall, and 2 MiB per protocol frame before parsing. Connection and discovery have a
10-second per-server deadline. A server's catalog is registered atomically, so invalid definitions,
duplicate names, collisions, or limit violations expose none of that server's tools.

Run `/mcp` in the TUI to inspect configured server status, registered tool names, and sanitized
startup failures. The command reads the current runtime snapshot and does not reconnect servers.

::: tip Why 16 servers
Every stdio server is a separate local process, so startup time and memory use grow with the number
and implementation of the configured servers. Wisp may revisit this limit when it can avoid eagerly
starting every local server, rather than raising it without a lifecycle or lazy-start solution.
:::

Current MCP support covers stdio tool discovery and bounded text results. Resources, prompts, dynamic
`tools/list_changed` updates, HTTP/SSE transports, OAuth, and interactive authentication are not yet
supported.
