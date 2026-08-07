# Event reference

Wisp exposes all agent activity as typed `WispEvent` objects. The same event stream drives print
mode, JSON mode, RPC, the TUI, and the in-process SDK — there is no interface-specific event set.

## Schema versioning

Every outbound event includes a `schema_version` field.

| | |
|---|---|
| Emitted version | **27** |
| Accepted on read | 5 – 27 |

Consumers should branch on `schema_version` and reject versions they do not support. Wisp's typed
RPC client (`wisp.rpc.RpcController`) does this automatically.

Fields introduced by a newer schema are stripped when an event is read at an older version, so an
older consumer sees a valid event rather than an unknown key. See [`CHANGELOG.md`](../../CHANGELOG.md)
for what each version added.

## Prompt lifecycle

A successful prompt emits events in this order. Tool events repeat within a turn whenever the model
requests tools.

```text
agent.started
  turn.started
    context.estimated
    provider.retrying *
    message.started
    message.delta *
    message.completed
    context.pressure ?
    tool.call -> tool.execution.started -> approval events -> tool.execution.ended -> tool.result
  turn.completed
queue.message.injected -> queue.updated ?
compaction.started ?
session.saved
compaction.completed ?
agent.completed
```

`*` = zero or more · `?` = optional

### Message events

`message.delta` distinguishes ordinary output from reasoning with `content_kind` (`text` or
`thinking`). `message.completed` carries the assembled content, finish reason, response id, and
completed tool calls.

### Failure paths

A failed provider response or tool loop emits `error`, a failed `turn.completed`, and a failed
`agent.completed`. It does **not** emit `message.completed` for an incomplete response.

The one exception is successful overflow recovery (schema v11+). After `context.overflow`, Wisp
emits overflow compaction lifecycle events, then the failed `turn.completed`, and continues once.
That compaction's `will_retry=true` marks the failed turn as nonterminal, so no `error` or
intermediate `agent.completed` is emitted unless compaction or retry setup itself fails.

## Provider stream contract

Provider adapters yield typed events from `wisp.providers` in a strictly validated order:

```text
provider.retrying *  ->  ProviderResponseStarted  (exactly one)
                     ->  text / thinking deltas, completed tool calls  *
                     ->  ProviderResponseCompleted | ProviderResponseFailed  (exactly one)
```

The agent rejects retries after start, missing or duplicate terminal boundaries, post-terminal
events, and mismatched tool-call summaries. Configuration and request-opening failures may raise
before the start event; once a response has started, adapters normalize expected transport and
provider failures into a failed terminal event.

Use `ScriptedProvider` (`wisp.providers.fake`) to exercise deterministic multi-turn and failure
cases without a live model.

## Persistence

Events reach disk by two independent paths:

- **Messages** — provider-visible history, written as message entries.
- **Raw events** — any event whose type is in `PERSISTED_SESSION_EVENT_TYPES` is persisted verbatim
  (this includes `tool.execution.ended`, `tool.call`, approval events, `context.pressure`,
  `context.overflow`, and `error`).

Durable records version independently of the event stream:

| Record | Current | Readable |
|---|---|---|
| Session entry (`SESSION_ENTRY_SCHEMA_VERSION`) | 5 | 1 – 5 |
| Compaction record | 4 | 1 – 4 |

Session entry v5 records whether an active-leaf transition came from navigation, unrevert, or
internal system recovery; v4 and older entries are read as system transitions. Compaction records
gained threshold metadata at v2, overflow at v3, and cost snapshots at v4.
