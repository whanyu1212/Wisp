# Compatibility & versioning

Wisp versions the Python package and persisted session records separately. The live RPC protocol
has no compatibility promise of its own: a backend and a frontend must come from the same Wisp
release.

## Python package versions

`wisp-ai` is the only published Wisp distribution today. Package splitting is planned in
[#409](https://github.com/whanyu1212/Wisp/issues/409); names or compatibility promises for those
future packages are not defined here.

Package releases use semantic versioning expressed with Python's PEP 440 spelling. For example,
`0.1.0` is the first stable release in the `0.1` minor line.

- Patch releases preserve the documented public API. They may correct behavior that contradicts a
  documented contract; consumers that relied on the defect may observe the correction.
- While Wisp is below 1.0, a later minor release may make an announced breaking change. After 1.0,
  breaking changes require a major release.
- Alpha, beta, and release-candidate suffixes identify prereleases; changing only the prerelease
  suffix is not, by itself, a compatibility boundary for removing a public API.
- Persistence schemas advance only when their own contracts change. A package release does not
  reset them.

The supported Python API is the import surface documented in [Python SDK](./sdk). Imports not listed
there, and modules or names marked internal, are not covered by this compatibility policy. Additive
exports, optional parameters with defaults, and new event models are compatible changes. Removing or
renaming a supported export, adding a required argument, narrowing an accepted value, or changing
documented behavior is a breaking change.

Capabilities tracked in [#400](https://github.com/whanyu1212/Wisp/issues/400) through
[#406](https://github.com/whanyu1212/Wisp/issues/406) are planned work, not current API promises.
Package organization beyond `wisp-ai` remains tracked by
[#409](https://github.com/whanyu1212/Wisp/issues/409).

## Live JSONL-RPC protocol

The external frontend protocol is generated from the Python models, which remain its source of
truth. `schemas/live-rpc/` holds one deterministic bundle describing the current release: handshake
request and response messages, the complete typed-client command output union, the complete live
event output union, conformance fixtures, and validation-only projections consumed by Rust type
generation. Command schemas describe payloads produced by `RpcCommandModel.to_json_line()`; the
backend may continue accepting a documented superset. Event schemas describe the exact serialized
shape, including required defaulted and nullable fields. Stateful lifecycle invariants remain
model-level protocol requirements rather than JSON Schema constraints.

**A backend and a frontend must be the same Wisp release.** Both readers reject unknown fields, so
two releases can disagree about any event. The Rust TUI and the Python SDK transport compare the
handshake's `backend_package_version` with their own release and refuse to connect on a mismatch.
The handshake carries no protocol version, and there are no historical bundles.

JSON Schema cannot compare two properties or express that one array is a subset of another. The
client handshake artifact therefore records the required-capability subset rule in
`x-wisp-cross-field-invariants`; every implementation must enforce it during decoding.

Regenerate or verify the artifacts from the repository root with:

```bash
uv run python -m wisp.rpc.protocol_schema --write
uv run python -m wisp.rpc.protocol_schema --check
```

Generated schema files must not be edited manually. CI rejects stale, missing, obsolete, and
hash-mismatched artifacts.

The external JSONL adapter requires `rpc.handshake.request` as its first frame and emits
exactly one `rpc.handshake.accepted` or `rpc.handshake.rejected` response before ordinary events.
Capabilities and directional frame limits are negotiated before the RPC host is constructed. The
in-process Python SDK does not negotiate because it has no serialization boundary.

Handshake frames are limited to 64 KiB. Negotiated application frames are limited to the directional
limits in the accepted response, currently 64 MiB. Frames are UTF-8 JSON objects terminated by LF;
duplicate object fields, invalid UTF-8, oversized frames, and incomplete final lines are rejected.
Clean EOF on a frame boundary closes input normally. Unknown commands receive the ordinary typed
command error lifecycle, while unknown events are fatal to clients.

The schema bundle is a repository build input and a GitHub release asset named
`wisp-live-rpc.tar.gz`; it is not part of the Python wheel API.

## Deprecation and removal

Except for an explicit exception documented below, a public API may be removed only when all of
these conditions are met:

1. The deprecation is recorded in the changelog and reference documentation with a supported
   replacement and required migration.
2. Wisp emits `DeprecationWarning` when use can be detected at runtime.
3. At least 90 days and one intervening minor release line have passed after the first released
   deprecation. Both conditions apply.
4. Removal occurs at a breaking package boundary: a later minor release before 1.0, or a later major
   release after 1.0.

A security, data-loss, legal, or ecosystem failure that cannot be mitigated may require faster
removal. Such an exception must be called out prominently in release notes with the safest available
migration or containment advice.

> [!WARNING]
> **0.2 agent API cleanup exception**
>
> The agent module reorganization removes the deprecated `wisp.agent.messages.SessionEntry(...)`
> factory before the normal deprecation window, at the 0.2 minor-release boundary. This is a specific
> early-removal exception for the agent API cleanup; the normal policy continues to apply to other
> public APIs. The [0.2 upgrade guide](../guide/upgrading) tracks candidate availability and migration.
>
> Construct `MessageSessionEntry`, `EventSessionEntry`, or `CompactionSessionEntry` from `wisp.sessions`
> instead. For event entries, wrap raw event dictionaries in `PersistedEventEnvelope(payload=...)`.
> The removal changes Python construction only.

> [!WARNING]
> **0.2 compatibility-shim cleanup exception**
>
> The 0.2 minor-release boundary also removes these compatibility shims before the normal
> deprecation window:
>
> - `defer_context_overflow_errors` on `AgentLoopConfig` and the harness `prompt`,
>   `prompt_message`, and `continue_` methods. Return `ContextOverflowFailure` from the overflow
>   hook to supply the error message; the loop publishes overflow terminals itself.
> - `wisp.cli.TuiRendererKind` and `wisp.cli.TuiFrontendKind`, the `--tui-renderer` and
>   `wisp tui --renderer` options, and `WISP_TUI_RENDERER`. The TUI is always the Rust frontend;
>   drop the option.
> - `wisp.config.settings.persist_user_effort`; use `try_persist_user_model_selection`.
> - `SessionReplay.entry_ids`; use `context_entry_ids`.
> - `ProjectSnapshot.paths`; use `entries`.
> - Chaining of tree entries that omit `parent_id` in `replay_session_entries`; set `parent_id`
>   explicitly.
>
> This exception covers only the names listed here; the normal policy continues to apply to other
> public APIs.

## Event schemas

`WispEvent` payloads carry no per-event version. The installed package emits only the shapes in
`schemas/live-rpc/`, and the typed parsers read exactly that shape.

```python
from wisp.events import wisp_event_from_json

event = wisp_event_from_json(line)
```

`wisp_event_from_json()` and `wisp_event_from_dict()` reject unknown event types and unknown fields.

Consumers should:

- parse untrusted events with Wisp's parser functions instead of dispatching on `type` manually;
- handle every known event type they need and deliberately ignore known types they do not use; and
- upgrade the backend and frontend together, treating a `backend_version_mismatch` handshake error as
  a signal to do so.

## Persisted session schemas

A session file contains several independently versioned layers:

| Layer | Current version |
|---|---:|
| Session entry | v6 |
| Persisted event envelope | v1 |
| Event payload inside the envelope | unversioned (current event shape) |
| Compaction record | v4 |

The session reader accepts only these versions. Files written in an earlier entry schema, or
without a `schema_version`, fail to load with `UnsupportedSessionEntryVersionError`; they are not
upgraded. Loading a session never rewrites it.

Persisted event envelopes retain their payload as raw JSON. `read_events()` can therefore expose a
payload for inspection without claiming to understand it. Typed access through `read_typed_events()`
rejects unknown event types and fields. Malformed committed records remain errors rather than being
silently discarded.
