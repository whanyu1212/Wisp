# Static extension example

[`static_extension.py`](static_extension.py) is a deterministic example for Python embedders. It
registers one read-only tool, one frontend-neutral command descriptor, and one event handler through
`ExtensionAPI`. Both synchronous and asynchronous activation factories are shown.

The example is intentionally static: Wisp does not discover or import Python files from
`examples/`, user directories, or projects. An embedding application supplies factories explicitly:

```python
from wisp.runtime.extensions import activate_extensions

from examples.extensions.static_extension import activate

await activate_extensions(runtime.api, (activate,))
```

Register factories before using the runtime so provider-visible tool schemas and command discovery
see a stable catalog. The `example-status` descriptor contributes metadata only; command execution
remains owned by the RPC command host and is not added by this example.

The `example_greeting` tool remains subject to normal tool exposure and policy. Its `read` safety
category means it does not require unsafe-tool approval, but registration alone does not expose or
execute it.

Run the focused deterministic tests from a source checkout:

```bash
uv run pytest tests/test_static_extension_example.py
```
