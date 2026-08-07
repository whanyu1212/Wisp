# RPC reference

For long-lived integrations, drive Wisp over JSONL-RPC on stdin/stdout:

```bash
printf '{"type":"prompt","prompt":"hello"}\n{"type":"shutdown"}\n' | uv run wisp --mode rpc
```

Commands go in as one JSON object per line; `WispEvent` objects come out the same way. The `id`
field is optional — Wisp generates one when omitted.

Every command emits `rpc.command.started` and `rpc.command.finished`, so clients can group the
events between them.

Provider, model, tool-exposure, approval, session, and max-iteration flags apply to the whole RPC
process.

## Commands

### Prompting and context

| Command | Effect |
|---|---|
| `{"type":"prompt","prompt":"…"}` | Run one agent turn, streaming `WispEvent` JSONL |
| `{"type":"compact","instructions":"…"}` | Compact older context in the active session |
| `{"type":"cancel","target_id":"cmd-1"}` | Request cancellation of a running or queued operation |
| `{"type":"shutdown"}` | Exit cleanly |

A prompt may emit threshold compaction events before its `rpc.command.finished`. This does not
create a nested `compact` command.

Cancellation is best-effort.

### State and discovery

| Command | Effect |
|---|---|
| `{"type":"get_state"}` | Immediate in-memory `rpc.state` snapshot |
| `{"type":"get_commands"}` | Immediate in-memory `rpc.commands` descriptor snapshot |
| `{"type":"get_session_stats"}` | Derived `session.stats` snapshot |
| `{"type":"get_messages","limit":200}` | Bounded active transcript page |

`get_state` reports coherent in-memory state only — use `get_session_stats` for persisted entry,
message, usage, context, and cost statistics, and `get_messages` for transcript pages.

`get_commands` reads the in-memory runtime command registry, including extension-registered
descriptors. It never reads session files or executes a handler.

`get_messages` defaults to the selected session and returns an empty page with null session fields
before any session is selected. Pass `session_id`, `limit` (`1..500`), and `before_entry_id` to read
a specific page without switching the selection.

### Sessions

| Command | Effect |
|---|---|
| `{"type":"get_sessions","limit":50}` | Bounded persisted session catalog |
| `{"type":"new_session"}` | Deselect the active session; the next prompt creates one lazily |
| `{"type":"select_session","session_id":"…"}` | Select a persisted session for later commands |
| `{"type":"clone_session"}` | Clone the selected active path and select the clone |
| `{"type":"fork_session","entry_id":"…"}` | Fork before a user message, select it, return its prompt |
| `{"type":"set_session_name","name":"…"}` | Rename the selected session; empty names clear it |

`get_sessions` accepts `limit` (`0..200`, default `50`) and never switches the selection.

`select_session` accepts a non-empty session id, path, or prefix, and preserves the previous
selection on failure.

`clone_session` and `fork_session` require a selected session and atomically replace it only after
the derived session validates. The source stays append-only and unchanged. Cancellation is honored
before the durable store operation begins; once publication starts the operation completes rather
than reporting a cancelled command that already created a session.

`set_session_name` targets the selected session unless an explicit `session_id` is given. Names are
normalized (CR/LF runs become spaces, surrounding whitespace trimmed, capped at 256 UTF-8 bytes).

### Session tree

| Command | Effect |
|---|---|
| `{"type":"get_session_tree","limit":200}` | Bounded append-order page of the selected session tree |
| `{"type":"navigate_session_tree","entry_id":"…"}` | Navigate in-file, optionally restoring a prompt for editing |
| `{"type":"unrevert_session_tree"}` | Reverse the latest eligible explicit tree navigation |

`get_session_tree` accepts `limit` (`1..500`, default `200`) and `after_entry_id`. Previews are
capped at 512 UTF-8-safe bytes; event previews expose only the event type, tool arguments are never
included, and compaction previews contain only the bounded summary.

`navigate_session_tree` requires a selected persisted session. Selecting the current active node is
a successful no-op; selecting another user message activates its parent and returns the exact,
untruncated prompt as `editor_text`. Failures leave coordinator history and the prior active leaf
unchanged. `rpc.session.tree.navigated` is emitted only after refreshed history is active, so later
reads and prompts immediately use the selected path.

`unrevert_session_tree` reverses only the most recent changed navigation, with the same
selected-session, optimistic-leaf, cancellation, and append-only guarantees.

### Queues

| Command | Effect |
|---|---|
| `{"type":"steer","content":"…"}` | Queue text after the active assistant/tool batch |
| `{"type":"follow_up","content":"…"}` | Queue text for when the active run would otherwise stop |
| `{"type":"get_queue_state"}` | Active or retained `queue.updated` snapshot |
| `{"type":"set_queue_mode","kind":"steering","mode":"all"}` | Set one active queue's drain mode |
| `{"type":"pop_queue","kind":"steering"}` | Remove the latest item from one active queue |
| `{"type":"clear_queue","kind":"follow_up"}` | Clear one queue; omit `kind` to clear both |

`get_queue_state` is safe while idle. **Queue mutations require an active run that is still
accepting messages**, and otherwise fail with `CodingSession has no active agent run`.

Successful mutations emit the authoritative `queue.updated`. `pop_queue` and `clear_queue` first
emit `queue.items.removed` with the exact removed text. Pop removes the latest item for an
edit-and-requeue workflow; clear preserves FIFO order.

The harness caps the combined steering and follow-up backlog at **100 pending messages**;
additional enqueue commands fail without changing either queue.

Wisp uses its native `content`, unified queue-kind commands, and `one_at_a_time` spelling. Pi
compatibility aliases are not accepted.

### Approval and trust

| Command | Effect |
|---|---|
| `{"type":"approval","call_id":"…","approved":true,"scope":"tool_session"}` | Approve or deny a pending tool request |
| `{"type":"trust","request_id":"…","trusted":true}` | Answer a project-trust request |

When an allowed mutating or command tool needs approval, Wisp emits `tool.approval.requested` with
a `call_id`. Respond with an `approval` command carrying that `call_id`, a boolean `approved`, and
an optional `scope`:

| Scope | Meaning |
|---|---|
| `once` | This call only (default) |
| `tool_session` | This exact tool name, for this RPC process |
| `all_session` | All mutating/command tools, for this RPC process |

Scoped denials are rejected. An optional `reason` describes a denial.

When an undecided project needs trust, Wisp emits `trust.requested` with a `request_id`. Respond
with a `trust` command carrying that `request_id`, a boolean `trusted`, and an optional denial
`reason`. **Denials are remembered** unless the command includes `"transient": true` — for example,
a UI closing before the user answered.

## Concurrency

Commands fall into two groups:

**Sequential** — prompts, compactions, statistics reads, transcript reads, session catalog reads,
session selection, cloning, forking, tree reads, tree navigation, tree unrevert, and renaming.

**Handled during an active operation** — `get_state`, `get_commands`, queue commands, `cancel`,
`approval`, and `trust`.

`get_state` and `get_commands` preserve the active command and any queued commands, including
during prompt startup, compaction, statistics reads, transcript reads, session operations,
approval/trust waits, and after cancellation is requested. During prompt startup, queue commands
buffered before readiness are projected into the reported queue modes and pending counts without
draining the buffer.

## Typed Python client

Python integrations can skip hand-rolling JSONL and use the typed controller — the intended stable
integration layer:

```python
transport = await JsonlSubprocessRpcTransport.start()
controller = RpcController(transport)
```

`RpcController` exposes typed `prompt`, `compact`, `get_session_stats`, `get_state`,
`get_commands`, `get_messages`, `get_sessions`, `new_session`, `select_session`, `clone_session`,
`fork_session`, `get_session_tree`, `navigate_session_tree`, `set_session_name`, `steer`,
`follow_up`, `get_queue_state`, `set_queue_mode`, `pop_queue`, `clear_queue`, `cancel`, `approve`,
`configure`, and `shutdown` methods, and yields parsed `WispEvent` objects.

## In-process Python SDK

Python hosts that do not need process isolation can drive the same command/event contract directly:

```python
from wisp.config import WispConfig
from wisp.events import RpcCommandFinished
from wisp.sdk import InProcessOptions, InProcessWisp

# `startup_trusted=True` is an explicit trusted decision made by this host.
controller = await InProcessWisp.start(
    WispConfig(provider="fake"),
    options=InProcessOptions(startup_trusted=True, allow_read_tools=True),
)
prompt_id = await controller.prompt("hello")
shutdown_id = None
try:
    async for event in controller.events():
        render(event)
        if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
            shutdown_id = await controller.shutdown()
        elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
            break
finally:
    await controller.aclose()
```

`InProcessWisp` has the same typed command methods and `WispEvent` stream as `RpcController`. It
uses the same command host, agent loop, JSONL sessions, approval policy, project-trust gate, and
runtime cleanup as RPC, and does not import terminal/TUI code or expose mutable `CodingSession`
internals.

Constraints:

- Requires AnyIO's `asyncio` backend, because built-in process tools use asyncio subprocesses. Use
  JSONL RPC from other async backends.
- Consume `events()` from exactly one task, and drain it while commands run.
- Tools are not exposed by default. `allow_read_tools`, `allowed_tools`, or `all_tools` control
  exposure; mutating and command tools still require `approve()` unless `approve_unsafe_tools=True`.

For normal environment/settings resolution, use `InProcessWisp.from_environment(...)`. It applies
only pre-existing safe trust decisions at startup; an undecided project emits `trust.requested`,
which the host answers with `trust()`, before project-local configuration is applied.
