# Current Extension API

`wisp.runtime.api.ExtensionAPI` is the supported registration surface. It currently provides:

- `register_provider(provider, replace=True)`
- `register_provider_factory(name, factory, replace=True)`
- `register_tool(tool, execution=None, prompt=None, replace=True)`
- `register_command(descriptor, replace=False)`
- `on(event_type, handler)`
- access to the runtime-owned `ProcessSupervisor` when bound

Providers implement the provider protocol. Use `register_provider_factory` for providers whose
construction or vendor SDK import should remain lazy. Tools implement the async `Tool` protocol and
declare a safety category; optional execution and prompt metadata describe runtime behavior without
granting access or approval. Commands are frontend-neutral descriptors; execution remains owned by
the RPC command host. Event handlers may be synchronous or asynchronous and run in registration
order.

Built-in capabilities are registered in `wisp.extensions.builtin.activate()`. Runtime construction
in `wisp.runtime.extensions` supports ordered synchronous or asynchronous static factories. Use
these existing seams for built-ins and Python embedders rather than importing capabilities into a
frontend.

## MCP servers

MCP servers are how users add external tools today. They are configured in user settings only:
`mcp_servers` and `mcp_tool_exposure` are user-only fields, so a project `.wisp/settings.json`
cannot add a server or change how its tools are offered.

- `wisp.mcp.runtime` owns the connections; `wisp.runtime.extensions.restart_mcp_servers()`
  replaces every connection and its tools when the server settings change, while no run is using
  them.
- `wisp.mcp.tool` adapts each remote tool to the local `Tool` protocol as
  `mcp__<server>__<tool>`. Its safety is `command` unless the server's `tool_safety` mapping
  declares otherwise, so remote tools need approval by default.
- `mcp_tool_exposure` is `auto` (the default), `direct`, or `on_demand`. On demand, the model gets
  `mcp_search`, `mcp_describe`, and `mcp_call` from `wisp.mcp.discovery` instead of every MCP
  schema. The executor resolves `mcp_call` to its target before any check, so the target's own
  policy, plan-mode rule, approval, and execution metadata apply as for a direct call, and the
  catalog holds only tools direct mode would have offered that run.
- The agent never edits MCP settings itself. The proposal tools in `wisp.tools.mcp_servers` set
  `confirm_every_call`, so each proposal needs a one-time user confirmation that no permission
  mode or session grant skips; `wisp.rpc.handlers.mcp` applies exactly the arguments the user saw.
- HTTP servers may sign in with OAuth. Tokens live in `~/.wisp/mcp-auth.json`, each bound to the
  server URL it was issued for.

## Current limitations

Wisp does not yet discover or import arbitrary user or project Python extensions. It has no public
extension identity/provenance model, ownership-based unregister operation, lifecycle hook suite,
code hot reload, or package management workflow. Project trust gates are necessary but not by
themselves sufficient for executable extension loading.

Do not describe static factory support as dynamic plugin loading. Do not place extension execution
or policy in a terminal client.
