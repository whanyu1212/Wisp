# Python SDK examples

These examples exercise Wisp's supported public Python namespaces with deterministic fake providers.
They need no API keys and make no model-provider network calls.

Run them from a source checkout:

```bash
uv run python -m examples.sdk.minimal
uv run python -m examples.sdk.safety_requests
uv run python -m examples.sdk.control_requests
uv run python -m examples.sdk.persisted_sessions
uv run python -m examples.sdk.subprocess_rpc
```

## Examples

- [`minimal.py`](minimal.py) starts `InProcessWisp`, consumes its one ordered typed event stream,
  correlates a command through `RpcCommandFinished`, and closes runtime resources with the async
  context manager. `prompt()` currently returns an accepted command ID, not the command's result;
  awaitable completion and independent subscriptions are tracked in
  [#400](https://github.com/whanyu1212/Wisp/issues/400).
- [`safety_requests.py`](safety_requests.py) resolves `TrustRequested` and
  `ToolApprovalRequested` while a prompt is active. The example denies both by default. A real
  application should approve only from caller-owned policy or user input; model output must never
  grant trust or tool permission.
- [`control_requests.py`](control_requests.py) uses the public `RpcTransport` protocol to show the
  typed steering, follow-up, cancellation, and compaction requests sent by `RpcController`.
- [`persisted_sessions.py`](persisted_sessions.py) creates and resumes a JSONL session, then uses
  public snapshot events to clone and fork it. True in-memory sessions and atomic active-session
  replacement are tracked in [#404](https://github.com/whanyu1212/Wisp/issues/404).
- [`subprocess_rpc.py`](subprocess_rpc.py) runs the same controller contract across
  `JsonlSubprocessRpcTransport`. Prefer subprocess RPC when process isolation, non-asyncio callers,
  or independent lifecycle failure boundaries matter; use `InProcessWisp` when the application
  deliberately owns Wisp in its asyncio process.

Static custom tool/provider composition is demonstrated by
[`examples/extensions`](../extensions/README.md). Supplying a caller-owned runtime is tracked in
[#402](https://github.com/whanyu1212/Wisp/issues/402), and typed system-prompt, context, skill, and
template overrides are tracked in [#403](https://github.com/whanyu1212/Wisp/issues/403). Model,
credential, and settings management is tracked in
[#405](https://github.com/whanyu1212/Wisp/issues/405); long-running health, restart, and recovery
primitives are tracked in [#406](https://github.com/whanyu1212/Wisp/issues/406).

Tests import and run these same example functions rather than maintaining separate test-only copies:

```bash
uv run pytest tests/test_sdk_examples.py
```
