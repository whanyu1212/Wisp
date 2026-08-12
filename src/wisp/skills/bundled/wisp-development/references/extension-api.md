# Current Extension API

`wisp.runtime.api.ExtensionAPI` is the supported registration surface. It currently provides:

- `register_provider(provider, replace=True)`
- `register_tool(tool, prompt=None, replace=True)`
- `register_command(descriptor, replace=False)`
- `on(event_type, handler)`
- access to the runtime-owned `ProcessSupervisor` when bound

Providers implement the provider protocol. Tools implement the async `Tool` protocol and declare a
safety category. Commands are frontend-neutral descriptors; execution remains owned by the RPC
command host. Event handlers may be synchronous or asynchronous and run in registration order.

Built-in capabilities are registered in `wisp.extensions.builtin.activate()`. Runtime construction
in `wisp.runtime.extensions` supports ordered synchronous or asynchronous static factories. Use
these existing seams for built-ins and Python embedders rather than importing capabilities into a
frontend.

## Current limitations

Wisp does not yet discover or import arbitrary user or project Python extensions. It has no public
extension identity/provenance model, ownership-based unregister operation, lifecycle hook suite,
code hot reload, or package management workflow. Project trust gates are necessary but not by
themselves sufficient for executable extension loading.

Do not describe static factory support as dynamic plugin loading. Do not place extension execution
or policy in the Textual client.
