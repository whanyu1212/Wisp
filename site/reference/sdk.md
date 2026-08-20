---
title: Python SDK
---

# Python SDK reference

The Python SDK is part of the `wisp-ai` distribution and carries a `py.typed` marker. It requires
Python 3.12+. The package-boundary evaluation in
[#409](https://github.com/whanyu1212/Wisp/issues/409) has not produced a separate client or SDK
distribution.

For lifecycle guidance and complete examples, start with the [Python SDK guide](../guide/sdk).

## Supported namespaces

| Namespace | Supported surface |
|---|---|
| `wisp.sdk` | In-process controller and startup options |
| `wisp.rpc` | High-level controller, transport protocol, subprocess transport, and typed command models |
| `wisp.events` | Typed event models, event schema constant, and JSON/dict parsers |
| `wisp.config` | Immutable runtime configuration |
| `wisp.sessions` | JSONL session store, entries, replay models, and typed session errors |
| `wisp.runtime` | Static extension/runtime contracts and registries |
| `wisp.providers` | Provider contracts, provider events, built-in providers, and deterministic fake providers |
| `wisp.tools` | Tool, context, result, safety, approval, and policy contracts |

Import supported names from these namespaces, not private implementation modules. Package-level
`__all__` lists are verified from the built wheel. `wisp.events` is a model namespace rather than a
curated re-export package; the stable entry points used by SDK consumers are described below.

## `wisp.sdk`

### `InProcessWisp`

```python
class InProcessWisp(RpcController):
    @classmethod
    async def start(
        cls,
        config: WispConfig,
        *,
        options: InProcessOptions | None = None,
    ) -> InProcessWisp: ...

    @classmethod
    async def from_environment(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        session_dir: Path | None = None,
        auth_path: Path | None = None,
        options: InProcessOptions | None = None,
    ) -> InProcessWisp: ...

    async def aclose(self) -> None: ...
```

`InProcessWisp` inherits every command, event, and `close()` method from `RpcController`. It is an
async context manager; `__aexit__` calls `aclose()`.

The active AnyIO backend must be asyncio. Startup on another backend raises `RuntimeError` before
runtime resources are retained.

`events()` returns the controller's only `AsyncIterator[KnownWispEvent]`. A second call raises
`RuntimeError`.

### `InProcessOptions`

`InProcessOptions` is an immutable dataclass.

| Field | Type | Default | Meaning |
|---|---|---:|---|
| `all_tools` | `bool` | `False` | Expose the complete registered tool set to the model. |
| `allow_read_tools` | `bool` | `False` | Expose tools classified as read-only. |
| `allowed_tools` | `tuple[str, ...]` | `()` | Expose selected tool names. |
| `resume` | `str \| None` | `None` | Resume by session path, filename, ID, or unique prefix. |
| `continue_latest` | `bool` | `False` | Resume the newest session in the configured store. |
| `approve_unsafe_tools` | `bool` | `False` | Pre-approve mutating and command tools. |
| `max_tool_iterations` | `int \| None` | `None` | Optional non-negative model/tool round limit. |
| `startup_trusted` | `bool` | `False` | Caller-supplied initial project trust decision. |
| `project_context_root` | `Path \| None` | `None` | Root for trust, project settings, skills, and instructions. |
| `cwd` | `Path \| None` | `None` | Working directory for built-in file and process tools. |

`resume` and `continue_latest` cannot both be set. A negative `max_tool_iterations` is rejected.
When `cwd` is omitted and `project_context_root` is supplied, the project root also becomes the tool
working directory.

Tool visibility does not imply tool approval. Unsafe tools still request approval unless
`approve_unsafe_tools=True`.

## `wisp.rpc`

### `RpcTransport`

A custom transport implements this public protocol:

```python
class RpcTransport(Protocol):
    async def send(self, command: RpcCommand) -> None: ...
    def events(self) -> AsyncIterator[KnownWispEvent]: ...
    async def close(self) -> None: ...
```

Transport implementations preserve typed command submission and one ordered event stream. Runtime
policy remains in the shared command host, not in the transport.

### `RpcController`

```python
RpcController(
    transport: RpcTransport,
    *,
    command_id_factory: Callable[[str], str] | None = None,
)
```

Every command method returns `str`, the selected command ID, after the transport accepts the typed
request. Completion and results arrive through `events()`.

#### Prompt, lifecycle, configuration, and snapshots

| Method | Signature after `self` | Result event or effect |
|---|---|---|
| `prompt` | `(prompt: str, *, command_id: str \| None = None)` | Agent/message/tool events; terminal `RpcCommandFinished` |
| `init` | `(*, command_id: str \| None = None)` | Initialize project guidance |
| `compact` | `(instructions: str \| None = None, *, command_id: str \| None = None)` | Compaction events |
| `configure` | `(*, provider=None, model=None, effort=None, clear_effort=False, auto_compaction_enabled=None, mode=None, command_id=None)` | Configuration events and terminal status |
| `get_session_stats` | `(*, command_id: str \| None = None)` | `SessionStatsReported` |
| `get_state` | `(*, command_id: str \| None = None)` | `RpcStateReported` |
| `get_commands` | `(*, command_id: str \| None = None)` | `RpcCommandsReported` |
| `get_skills` | `(*, command_id: str \| None = None)` | `RpcSkillsReported` |
| `get_mcp_status` | `(*, command_id: str \| None = None)` | `RpcMcpStatusReported` |
| `shutdown` | `(*, command_id: str \| None = None)` | Request host shutdown |

`configure()` accepts `provider: str | None`, `model: str | None`, `effort: str | None`,
`auto_compaction_enabled: bool | None`, and `mode: AgentMode | None`. `effort=None` leaves the
current setting untouched; use `clear_effort=True` to restore the provider default.

#### Live queues and cancellation

| Method | Signature after `self` |
|---|---|
| `steer` | `(content: str, *, command_id: str \| None = None)` |
| `follow_up` | `(content: str, *, command_id: str \| None = None)` |
| `get_queue_state` | `(*, command_id: str \| None = None)` |
| `set_queue_mode` | `(kind: QueueKind, mode: QueueMode, *, command_id: str \| None = None)` |
| `pop_queue` | `(kind: QueueKind, *, command_id: str \| None = None)` |
| `clear_queue` | `(kind: QueueKind \| None = None, *, command_id: str \| None = None)` |
| `cancel` | `(target_id: str, *, command_id: str \| None = None)` |

`QueueKind` is `"steering" | "follow_up"`. `QueueMode` is `"one_at_a_time" | "all"`.
`cancel()` targets a running prompt or compaction command ID.

#### Trust and approval

| Method | Signature after `self` |
|---|---|
| `trust` | `(request_id: str, *, trusted: bool, reason: str \| None = None, transient: bool = False, command_id: str \| None = None)` |
| `approve` | `(call_id: str, *, approved: bool = True, reason: str \| None = None, scope: ApprovalScope \| None = None, command_id: str \| None = None)` |

`ApprovalScope` is `"once" | "tool_session" | "all_session"`. Match `request_id` from
`TrustRequested` and `call_id` from `ToolApprovalRequested`. These methods are re-entrant control
commands and may be submitted while a prompt is paused.

#### Persisted sessions

| Method | Signature after `self` | Primary result event |
|---|---|---|
| `get_sessions` | `(*, limit: int = 50, command_id: str \| None = None)` | `RpcSessionsReported` |
| `new_session` | `(*, command_id: str \| None = None)` | Deselect; next prompt creates a session |
| `select_session` | `(session_id: str, *, command_id: str \| None = None)` | `RpcSessionSelected` |
| `set_session_name` | `(name: str, *, session_id: str \| None = None, command_id: str \| None = None)` | `RpcSessionNameChanged` |
| `clone_session` | `(*, command_id: str \| None = None)` | `RpcSessionCloned` |
| `fork_session` | `(entry_id: str, *, command_id: str \| None = None)` | `RpcSessionForked` |
| `get_session_tree` | `(*, limit: int = 200, after_entry_id: str \| None = None, command_id: str \| None = None)` | `RpcSessionTreeReported` |
| `navigate_session_tree` | `(entry_id: str, *, command_id: str \| None = None)` | `RpcSessionTreeNavigated` |
| `unrevert_session_tree` | `(*, command_id: str \| None = None)` | `RpcSessionTreeUnreverted` |

`get_sessions()` accepts `limit` from 0 through 200. Session tree pages accept 1 through 500 nodes.

Transcript pages use:

```python
async def get_messages(
    *,
    session_id: str | None = None,
    limit: int = 200,
    before_entry_id: str | None = None,
    after_entry_id: str | None = None,
    entry_ids: tuple[str, ...] = (),
    complete_structure: bool = False,
    full_content: bool = False,
    allow_during_prompt: bool = False,
    command_id: str | None = None,
) -> str: ...
```

`limit` is 1 through 500. Forward and backward cursors are mutually exclusive. Exact `entry_ids`
cannot be combined with cursors; at most 16 IDs are accepted. `full_content=True` requires exactly
one explicit entry ID. The result is `RpcMessagesReported`, including bounded message snapshots,
`truncated`, and continuation cursors.

#### Event and cleanup methods

```python
def events(self) -> AsyncIterator[KnownWispEvent]: ...
async def close(self) -> None: ...
```

The controller delegates stream ownership and cleanup to its transport. `InProcessWisp.aclose()` is
an alias for its client cleanup path.

### `JsonlSubprocessRpcTransport`

```python
await JsonlSubprocessRpcTransport.start(
    command: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stderr: int | None = subprocess.DEVNULL,
) -> JsonlSubprocessRpcTransport
```

The default command is the current Python interpreter running `-m wisp --mode rpc`. Commands are
newline-delimited JSON on stdin; stdout is parsed into typed events. Stderr defaults to `DEVNULL` so
an undrained pipe cannot deadlock the event stream. If `stderr=subprocess.PIPE` is selected, the
caller must drain it concurrently.

`close()` closes stdin, waits for bounded graceful exit, then terminates and kills if needed. It is
idempotent and re-raises a retained close failure.

### Typed command models

`wisp.rpc` exports `RpcCommand` and one Pydantic model for each command:

- work/configuration: `PromptCommand`, `InitCommand`, `CompactCommand`, `ConfigureCommand`,
  `ShutdownCommand`
- snapshots: `GetSessionStatsCommand`, `GetStateCommand`, `GetCommandsCommand`, `GetSkillsCommand`,
  `GetMcpStatusCommand`
- queue/control: `SteerCommand`, `FollowUpCommand`, `GetQueueStateCommand`, `SetQueueModeCommand`,
  `PopQueueCommand`, `ClearQueueCommand`, `CancelCommand`
- safety: `ApprovalCommand`, `TrustCommand`
- sessions: `GetMessagesCommand`, `GetSessionsCommand`, `NewSessionCommand`,
  `SelectSessionCommand`, `SetSessionNameCommand`, `CloneSessionCommand`, `ForkSessionCommand`,
  `GetSessionTreeCommand`, `NavigateSessionTreeCommand`, `UnrevertSessionTreeCommand`

Each model is frozen, discriminated by `type`, accepts an optional `id`, and provides
`to_json_line()`. Prefer `RpcController` unless an integration is implementing a lower-level
transport.

## `wisp.events`

### Core types and parsers

| Name | Contract |
|---|---|
| `WispEvent` | Frozen Pydantic base model with `type` and `schema_version`. Unknown fields are rejected. |
| `KnownWispEvent` | Discriminated union of every event model understood by this version. |
| `EVENT_SCHEMA_VERSION` | Schema version emitted by the installed package. |
| `wisp_event_from_json(line)` | Validate one JSON event string and return `KnownWispEvent`. |
| `wisp_event_from_dict(data)` | Validate one event dictionary and return `KnownWispEvent`. |

Use the parser functions at protocol boundaries rather than selecting a model from an untrusted
`type` value manually. Unsupported future schemas raise `ValueError`. The complete readable-history
and deprecation policy is being documented separately as part of
[#407](https://github.com/whanyu1212/Wisp/issues/407).

### Event groups

| Group | Important models |
|---|---|
| Command lifecycle | `RpcCommandStarted`, `RpcCommandFinished` |
| Assistant output | `MessageStarted`, `MessageDelta`, `MessageCompleted` |
| Run lifecycle | `AgentStarted`, `TurnStarted`, `TurnCompleted`, `AgentCompleted`, `ErrorEvent` |
| Safety | `TrustRequested`, `TrustResolved`, `ToolApprovalRequested`, `ToolApprovalResolved` |
| Tools | `ToolCallRequested`, `ToolExecutionStarted`, `ToolExecutionEnded`, `ToolResultReady` |
| Compaction/context | `ContextEstimated`, `ContextPressure`, `ContextOverflow`, `CompactionStarted`, `CompactionCompleted` |
| Queues | `QueueUpdated`, `QueueItemsRemoved`, `QueueMessageInjected` |
| Snapshots | `SessionStatsReported`, `RpcStateReported`, `RpcCommandsReported`, `RpcSkillsReported`, `RpcMcpStatusReported` |
| Sessions | `SessionSaved`, `RpcMessagesReported`, `RpcSessionsReported`, `RpcSessionSelected`, `RpcSessionCloned`, `RpcSessionForked`, `RpcSessionNameChanged`, `RpcSessionTreeReported`, `RpcSessionTreeNavigated`, `RpcSessionTreeUnreverted` |

`RpcCommandFinished` is the terminal command correlation event:

```python
class RpcCommandFinished(WispEvent):
    command_id: str
    command_type: str
    ok: bool
    error: str | None = None
```

A failed command has `ok=False` and an optional sanitized error. Domain result events normally
precede the matching successful terminal event and carry the same `command_id`. Streamed run events
are ordered but do not all have command IDs.

## `wisp.config`

`WispConfig` is a frozen Pydantic model. Its primary fields are:

| Field | Type | Default behavior |
|---|---|---|
| `provider` | `str` | `openai-codex` |
| `model` | `str \| None` | Provider default |
| `effort` | `str \| None` | Provider/user default |
| `session_dir` | `Path` | `~/.wisp/sessions` |
| `auth_path` | `Path` | `~/.wisp/auth.json` |
| `protected_paths` | `tuple[str, ...]` | Built-in protected globs plus sensitive settings/auth paths |
| `retry_policy` | `RetryPolicy` | Bounded default retry policy |
| `context_reserve_tokens` | `int` | `16384` |
| `auto_compaction_enabled` | `bool` | `True` |
| `update_check_enabled` | `bool` | `True` |
| `mcp_servers` | `tuple[McpServerConfig, ...]` | `()` |
| `openai_compatible` | `OpenAICompatibleSettings \| None` | `None` |

`WispConfig.from_env(...)` applies explicit arguments over environment, trusted project settings,
user settings, and defaults. For SDK startup, prefer `InProcessWisp.from_environment()` when the
project trust transition must remain re-entrant; it resolves trust before building initial project
configuration.

See [Configuration](./configuration) for every persisted field and precedence rule.

## Related public contracts

- `wisp.sessions.JsonlSessionStore` and `JsonlSession` provide direct typed access to append-only
  session storage. RPC/SDK command methods are preferred when the active runtime must change session.
- `wisp.runtime.ExtensionAPI` and `WispRuntime` describe static extension composition. The current
  `InProcessWisp` startup methods do not accept a caller-built runtime; see
  [#402](https://github.com/whanyu1212/Wisp/issues/402).
- `wisp.providers.FakeProvider` and `ScriptedProvider` are deterministic provider implementations.
  They are public for tests and examples, but arbitrary provider injection into `InProcessWisp` is
  not yet public.
- `wisp.tools.Tool`, `ToolContext`, and `ToolResult` are the core custom-tool contracts. Tool
  registration is demonstrated in the
  [static extension example](https://github.com/whanyu1212/Wisp/tree/main/examples/extensions).

## Current limitations

These APIs are tracked but are not part of the current reference:

- awaitable command results and independent event subscriptions —
  [#400](https://github.com/whanyu1212/Wisp/issues/400)
- direct settled lifecycle/state primitives — [#401](https://github.com/whanyu1212/Wisp/issues/401)
- caller-owned runtime/provider injection — [#402](https://github.com/whanyu1212/Wisp/issues/402)
- typed prompt/context/skill/template overrides —
  [#403](https://github.com/whanyu1212/Wisp/issues/403)
- in-memory sessions and atomic active-session replacement —
  [#404](https://github.com/whanyu1212/Wisp/issues/404)
- model, authentication, and settings management —
  [#405](https://github.com/whanyu1212/Wisp/issues/405)
- long-running health, restart, recovery, and observability —
  [#406](https://github.com/whanyu1212/Wisp/issues/406)
