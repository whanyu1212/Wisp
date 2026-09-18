# Compatibility & versioning

Wisp has separate version domains for the Python package, the live RPC protocol, and persisted
session records. They do not reset or advance together. In particular, a future `wisp-ai` 1.0
release may speak a live RPC protocol much newer than v9.

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
- Event and persistence schemas advance only when their own contracts change. A package release does
  not reset them.

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

The proposed external frontend protocol is a separate compatibility domain. Python models remain
its semantic source of truth, and deterministic current-version artifacts are checked in under
`schemas/live-rpc/v9/`. Ordinary command and event envelopes inherit the selected connection
version rather than carrying their own version; there is no per-event `schema_version`.

The v9 schema bundle contains handshake request and response messages, the complete typed-client
command output union, the complete current live event output union, deterministic conformance
fixtures, and validation-only projections consumed by Rust type generation. Command schemas describe payloads
produced by `RpcCommandModel.to_json_line()`; the backend may continue accepting a documented
superset. Event schemas describe the exact current serialized shape, including required defaulted and
nullable fields. Stateful lifecycle invariants remain model-level protocol requirements rather than
JSON Schema constraints.

JSON Schema cannot compare two properties or express that one array is a subset of another. The
handshake artifacts therefore record ordered ranges, selected-version containment, and the client
required-capability subset rule in `x-wisp-cross-field-invariants`; every implementation must enforce
those rules during decoding.

The live event artifact describes only the shapes emitted by the current package even though Python
still loads persisted events written before protocol v9. The handshake negotiates the protocol
version, advertises a fixed pre-negotiation frame ceiling, and reports directional application-frame
limits; there is no separate event-version negotiation. The manifest records the protocol version,
transport ceilings, and SHA-256 hashes for every schema.

Regenerate or verify the artifacts from the repository root with:

```bash
uv run python -m wisp.rpc.protocol_schema --write
uv run python -m wisp.rpc.protocol_schema --check
```

Generated schema files must not be edited manually. CI rejects changed, missing, obsolete,
cross-version, and hash-mismatched artifacts. A new protocol version writes a new immutable version
directory rather than replacing an older bundle. Before a protocol bump, the previous manifest's
SHA-256 digest must be added to `HISTORICAL_PROTOCOL_MANIFEST_SHA256`; that digest transitively pins
the old schemas and metadata outside their version directory. CI also compares committed version
artifacts with the trusted base revision and rejects modifications, deletions, or renames; new
protocol directories may only be added. A separate `pull_request_target` guard performs the same
check from default-branch workflow code without checking out or executing pull-request code.

The external JSONL adapter requires `rpc.handshake.request` as its first frame and emits
exactly one `rpc.handshake.accepted` or `rpc.handshake.rejected` response before ordinary events.
Protocol version, capabilities, and directional frame limits are negotiated before the RPC host is
constructed. The in-process Python SDK does not negotiate because it has no
serialization boundary.

Handshake frames are limited to 64 KiB. Negotiated application frames are limited to the directional
limits in the accepted response, currently 64 MiB. Frames are UTF-8 JSON objects terminated by LF;
duplicate object fields, invalid UTF-8, oversized frames, and incomplete final lines are rejected.
Clean EOF on a frame boundary closes input normally. Unknown commands receive the ordinary typed
command error lifecycle, while unknown events are fatal to clients for an already-negotiated version.

Schema bundles are repository build inputs and versioned GitHub release assets named
`wisp-live-rpc-v<version>.tar.gz`; they are not part of the Python wheel API. The checked-in handshake
models and compile-time generated Serde crate define the contract for external frontends. Protocol
v1 remains immutable historical design input; v2 is the first runtime-enforced negotiated version,
v3 adds authoritative model-catalog discovery, v4 adds backend-owned connection workflows,
v5 adds opt-in persistence for model configuration, and v6 adds
[bounded project file discovery](./project-files). Protocol v7 adds project permission settings
and the explicit `all_project` approval scope; `all_session` remains temporary. Protocol v8 adds
assigned message origins to live events for transcript recovery. All earlier bundles remain immutable.

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
> The removal changes Python construction only: existing JSONL session files and supported event
> schemas remain readable without migration.

## Event schemas

`WispEvent` payloads carry no per-event version. The live RPC protocol bundle under
`schemas/live-rpc/` is the single compatibility contract for streamed events: the installed package
emits only the shapes in the current bundle, currently **protocol v9**, and the typed parsers read
exactly that shape.

```python
from wisp.events import wisp_event_from_json

event = wisp_event_from_json(line)
```

`wisp_event_from_json()` and `wisp_event_from_dict()` reject unknown event types and unknown fields,
including the legacy `schema_version` key that events carried before protocol v9. Persisted sessions
are the exception: the session reader drops that key from stored event payloads before typed
validation, so history written by earlier releases still loads. Consumers auditing third-party or
hand-written events should use the
[event history](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#live-rpc-protocol-v9--current)
as the authoritative introduction record.

A wire-visible event change:

- **regenerates the current bundle in place** when it is additive — a new event type, a new optional
  field, or a new enum value that existing consumers can ignore; and
- **bumps `LIVE_RPC_PROTOCOL_VERSION`** when it is breaking — removing or renaming an event type or
  field, changing a field's type, requiredness, or default-on-the-wire behavior, or changing lifecycle
  ordering.

Internal refactors, documentation, rendering changes, and behavior that leaves the serialized
contract unchanged do not touch the bundle. Protocol numbers are never recycled. A protocol bump
must pin the previous manifest hash in `src/wisp/rpc/protocol_schema.py`, regenerate the new
`schemas/live-rpc/vN/` directory, add consumer-focused history to the changelog, and include JSON
round-trip and conformance tests.

Consumers should:

- parse untrusted events with Wisp's parser functions instead of dispatching on `type` manually;
- handle every known event type they need and deliberately ignore known types they do not use;
- treat a `protocol_version_mismatch` handshake rejection as a signal to upgrade rather than guessing
  at the newer contract; and
- consult the [event history](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#live-rpc-protocol-v9--current)
  for the action required by each version.

## Persisted session schemas

A session file contains several independently versioned layers:

| Layer | Current writes | Readable history |
|---|---:|---:|
| Session entry | v6 | unversioned and v1–v6 |
| Persisted event envelope | v1 | v1 |
| Event payload inside the envelope | unversioned (protocol v9 shape) | pre-v9 payloads with a legacy `schema_version` key |
| Compaction record | v4 | v1–v4 |

Historical session entries are normalized to current typed models in memory. Loading a session does
not rewrite it; later appends use the current entry schema while preserving committed historical
records. Legacy linear entries receive their parent relationships during decoding, without changing
the source file.

Persisted event envelopes retain their payload as raw JSON. `read_events()` can therefore expose a
future event payload for inspection without claiming to understand it. Typed access through
`read_typed_events()` rejects an unsupported future event version. Malformed committed records remain
errors rather than being silently discarded.

Any future on-disk migration must preserve append-only history, stable entry IDs, parent links,
timestamps, active-branch meaning, and provider-visible message order. A migration must be explicit
and recoverable; merely opening an older session must not destructively upgrade it.
