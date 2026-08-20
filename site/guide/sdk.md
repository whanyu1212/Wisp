---
title: Python SDK
---

# Python SDK

Use the Python SDK when an asyncio application should own Wisp in its process and consume the same
typed command/event contract as the CLI and JSONL RPC interfaces. The SDK is presentation-free: the
embedding application renders events, decides trust and approvals, and owns shutdown.

Wisp currently ships the runtime and client in the single `wisp-ai` distribution. A possible
lightweight client package is still being evaluated in
[#409](https://github.com/whanyu1212/Wisp/issues/409); do not install or depend on a separate SDK
package today.

## Install

Wisp requires Python 3.12 or newer. Add the release candidate to an application environment rather
than installing it only as a command-line tool:

```bash
uv add "wisp-ai==0.1.0rc4"
```

The supported embedding imports start here:

```python
from wisp.config import WispConfig
from wisp.events import KnownWispEvent, RpcCommandFinished
from wisp.rpc import RpcController
from wisp.sdk import InProcessOptions, InProcessWisp
```

See the [SDK API reference](../reference/sdk) for supported namespaces, signatures, and command
groups.

## Minimal offline prompt

This example uses Wisp's deterministic fake provider. It needs no credentials and makes no model
provider network calls:

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import anyio

from wisp.config import WispConfig
from wisp.events import MessageDelta, RpcCommandFinished
from wisp.sdk import InProcessOptions, InProcessWisp


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wisp-sdk-") as temporary_directory:
        root = Path(temporary_directory)
        workspace = root / "workspace"
        workspace.mkdir()

        controller = await InProcessWisp.start(
            WispConfig(
                provider="fake",
                session_dir=root / "sessions",
                update_check_enabled=False,
            ),
            options=InProcessOptions(
                # Safe here because this application created the empty workspace.
                startup_trusted=True,
                project_context_root=workspace,
            ),
        )
        async with controller:
            events = controller.events()
            prompt_id = await controller.prompt("hello from the SDK")
            async for event in events:
                if isinstance(event, MessageDelta) and event.content_kind == "text":
                    print(event.delta, end="", flush=True)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    if not event.ok:
                        raise RuntimeError(event.error or "Prompt failed")
                    break
        print()


if __name__ == "__main__":
    anyio.run(main)
```

The maintained version is
[`examples/sdk/minimal.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/sdk/minimal.py),
and automated tests execute that same function.

## Choose a startup path

### Explicit configuration

`InProcessWisp.start(config, options=...)` uses the supplied `WispConfig`. It does not discover a
project settings layer on its own. Use it when the application owns configuration and can state its
trust decision explicitly.

`startup_trusted=True` is a security decision, not a convenience flag. Set it only after the caller
has trusted the resolved `project_context_root`. Trust enables project settings, instructions, and
skills. It does not approve unsafe tools.

### Environment and saved settings

`InProcessWisp.from_environment(...)` applies the same environment, user settings, and project trust
boundary as standalone RPC mode. If trust is undecided, the first prompt emits `TrustRequested`.
When the application trusts the project, Wisp rebuilds the project configuration before starting
that prompt.

Prefer this path when the embedder should honor the user's existing Wisp configuration. Explicit
arguments such as `provider`, `model`, `session_dir`, and `auth_path` still override lower-precedence
settings. See [Configuration](../reference/configuration) for precedence.

## Own one event consumer

Call `events()` exactly once and drain it continuously while the controller is running. In-process
events are ordered and bounded; when the consumer stops reading, streamed production eventually
backpressures. Do not create one iterator per command or let rendering block the only consumer
indefinitely.

Command methods submit typed requests and return their command IDs. They do **not** currently return
the command result. Events from commands may interleave, so match command-scoped reports and
`RpcCommandFinished` by `command_id`:

```python
prompt_id = await controller.prompt("inspect the failure")
stats_id = await controller.get_session_stats()
pending = {prompt_id, stats_id}

async for event in controller.events():
    if getattr(event, "command_id", None) == stats_id:
        # Handle the stats report and its lifecycle events.
        ...
    if isinstance(event, RpcCommandFinished) and event.command_id in pending:
        if not event.ok:
            raise RuntimeError(event.error or f"Command {event.command_id} failed")
        pending.remove(event.command_id)
        if not pending:
            break
```

Some streamed agent events, including message deltas, describe the active run without carrying a
command ID. Use command lifecycle events as the terminal correlation contract; do not infer
completion from the last text delta or `AgentCompleted` alone.

For an interactive or concurrent application, keep one long-lived consumer that routes events to
application state keyed by command ID. Awaitable command results and independent subscriptions are
tracked in [#400](https://github.com/whanyu1212/Wisp/issues/400). Direct settled-state accessors are
tracked in [#401](https://github.com/whanyu1212/Wisp/issues/401); current snapshot methods submit a
command and return their report through the event stream.

## Handle trust and approvals re-entrantly

Trust and unsafe tool execution pause the active command. Resolve their typed requests from the same
consumer while continuing to drain events:

```python
from wisp.events import ToolApprovalRequested, TrustRequested

async for event in controller.events():
    if isinstance(event, TrustRequested):
        await controller.trust(
            event.request_id,
            trusted=False,
            transient=True,
            reason="The application did not trust this project",
        )
    elif isinstance(event, ToolApprovalRequested):
        await controller.approve(
            event.call_id,
            approved=False,
            reason="The application did not authorize this tool call",
        )
```

Default deny is the safe fallback. An application may expose an approval prompt or apply its own
policy, but model output must never grant project trust or tool permission. Approval scopes are
`once`, `tool_session`, and `all_session`; broader scopes remain caller decisions.

`InProcessOptions` controls which tools the model can see. Tool exposure and approval are separate:
`all_tools=True` or `allowed_tools=(...)` does not bypass approval for mutating or command tools.
`approve_unsafe_tools=True` deliberately pre-approves them and should be reserved for a trusted,
caller-controlled environment.

Read [Tools & safety](./tools-and-safety) for protected paths, trust storage, and MCP policy. The
complete deny-by-default handler is in
[`examples/sdk/safety_requests.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/sdk/safety_requests.py).

## Live control

The controller can modify an active run without replacing its transcript:

```python
prompt_id = await controller.prompt("inspect the project")
await controller.steer("focus on the failing test")
await controller.follow_up("summarize the final diff")
await controller.cancel(prompt_id)
await controller.compact("retain decisions and test results")
```

- `steer()` injects text at the next eligible boundary in the active run.
- `follow_up()` queues text for when the run would otherwise stop.
- `cancel(target_id)` cooperatively cancels an active prompt or compaction.
- `compact()` is a sequential command and reports completion through events.
- Queue inspection and editing use `get_queue_state()`, `set_queue_mode()`, `pop_queue()`, and
  `clear_queue()`.

Submit control commands while the single event consumer remains active. Each method returns its own
command ID, so correlate its acceptance or failure independently. See [Staying in sync](./staying-in-sync)
and the deterministic
[`control_requests.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/sdk/control_requests.py)
example.

## Persist and resume sessions

SDK sessions use the same append-only JSONL store as every other interface. Select startup behavior
with `InProcessOptions`:

```python
options = InProcessOptions(resume="SESSION_ID_OR_PATH")
# Or: InProcessOptions(continue_latest=True)
```

`resume` and `continue_latest` are mutually exclusive. During a running controller, use the session
command methods to list, select, name, clone, fork, and navigate persisted sessions. Their typed
result events include the originating command ID.

Transcript reads are bounded. Page with `get_messages(limit=..., before_entry_id=...)` or
`after_entry_id=...`; use the cursors from `RpcMessagesReported` rather than assuming the entire
session fits in one response.

The runnable
[`persisted_sessions.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/sdk/persisted_sessions.py)
example creates, resumes, inspects, clones, and forks a session using public events. True in-memory
sessions and atomic active-session replacement are not shipped; they are tracked in
[#404](https://github.com/whanyu1212/Wisp/issues/404). Read [Sessions](./sessions) for durability and
branching semantics.

## Clean up explicitly

Prefer the async context manager after startup:

```python
controller = await InProcessWisp.start(config, options=options)
async with controller:
    ...
```

It calls `aclose()` even when the body raises. If ownership cannot be lexical, call `await
controller.aclose()` in `finally`. Cleanup stops command processing and releases runtime-owned
provider, MCP, and process resources. Do not rely on garbage collection.

`shutdown()` is a protocol command intended to ask an RPC host to exit; `aclose()`/`close()` is the
client-side resource cleanup contract.

## In-process or subprocess RPC?

Both choices expose `RpcController` and the same typed events:

| Choose | When |
|---|---|
| `InProcessWisp` | The application uses asyncio, deliberately shares a process with Wisp, and owns runtime cleanup. |
| `JsonlSubprocessRpcTransport` | You need process isolation, a non-asyncio parent, language-neutral JSONL, or a separate failure/restart boundary. |

In-process Wisp currently requires AnyIO's asyncio backend. From Trio or another runtime, place Wisp
behind JSONL RPC. The public subprocess adapter starts `wisp --mode rpc`, serializes typed commands,
and parses stdout into `KnownWispEvent` values:

```python
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController

transport = await JsonlSubprocessRpcTransport.start(cwd=workspace, env=child_environment)
controller = RpcController(transport)
try:
    ...
finally:
    await controller.close()
```

The caller owns `child_environment`. Isolate `HOME` and remove inherited `WISP_*` overrides when a
run must be deterministic and offline. See the tested
[`subprocess_rpc.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/sdk/subprocess_rpc.py)
example.

## Composition and current limits

Static source-checkout extension composition is demonstrated in
[`examples/extensions`](https://github.com/whanyu1212/Wisp/tree/main/examples/extensions). The
current SDK does not accept an arbitrary caller-owned runtime or provider instance; that API belongs
to [#402](https://github.com/whanyu1212/Wisp/issues/402).

Other planned APIs are intentionally not presented as available:

- typed system-prompt, context, skill, and template overrides —
  [#403](https://github.com/whanyu1212/Wisp/issues/403)
- model, credential, and settings management —
  [#405](https://github.com/whanyu1212/Wisp/issues/405)
- health, restart, recovery, and long-running observability primitives —
  [#406](https://github.com/whanyu1212/Wisp/issues/406)

Use public namespaces only. Private names such as `_InProcessTransport` are implementation details
and may change without notice.

## Next steps

- [SDK API reference](../reference/sdk) — exact public controllers, options, commands, and events.
- [Canonical examples](https://github.com/whanyu1212/Wisp/tree/main/examples/sdk) — deterministic,
  executable workflows covered by tests.
- [Interfaces](./interfaces) — compare SDK behavior with TUI, print, JSON, and RPC modes.
