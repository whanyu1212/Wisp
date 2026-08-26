---
title: Compatibility & versioning
---

# Compatibility & versioning

Wisp has separate version domains for the Python package, streamed events, and persisted session
records. They do not reset or advance together. In particular, a future `wisp-ai` 1.0 release may
emit an event schema much newer than v34.

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
`schemas/live-rpc/v1/`. Once runtime negotiation lands, ordinary command envelopes will inherit the
selected connection version rather than carrying their own version. Events continue to carry the
independently negotiated `schema_version` described below.

The v1 schema bundle contains client and server handshake messages, the complete typed-client command
output union, and the complete current live event output union. Command schemas describe payloads
produced by `RpcCommandModel.to_json_line()`; the backend may continue accepting a documented
superset. Event schemas describe the exact current serialized shape, including required defaulted and
nullable fields. Stateful lifecycle invariants remain model-level protocol requirements rather than
JSON Schema constraints.

JSON Schema cannot compare two properties or express that one array is a subset of another. The
handshake artifacts therefore record ordered ranges, selected-version containment, and the client
required-capability subset rule in `x-wisp-cross-field-invariants`; every implementation must enforce
those rules during decoding.

The live event artifact includes only the version emitted by the current package even though Python
retains historical event parsing for persisted sessions. The handshake negotiates protocol and event
compatibility independently, advertises a fixed pre-negotiation frame ceiling, and reports directional
application-frame limits. The manifest records both version domains, transport ceilings, and SHA-256
hashes for every schema.

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

Schema bundles are repository build inputs and versioned GitHub release assets named
`wisp-live-rpc-v<version>.tar.gz`; they are not part of the Python wheel API. The checked-in handshake
models define the contract for the optional Rust frontend experiment, but the currently shipped
JSONL-RPC adapter does not perform negotiation until the remaining work in
[#458](https://github.com/whanyu1212/Wisp/issues/458) lands.

## Deprecation and removal

A public API may be removed only when all of these conditions are met:

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

The deprecated `wisp.agent.messages.SessionEntry(...)` factory demonstrates the normal transition:
it warns when called while the concrete `wisp.sessions` entry models remain available as the
replacement.

## Event schemas

Every JSON, JSONL-RPC, and SDK `WispEvent` carries an integer `schema_version`. The installed package
emits only `EVENT_SCHEMA_VERSION`, currently **v34**. There is no transport-level negotiation or
supported down-level emission mode.

The typed parsers currently read **v5 through v34**:

```python
from wisp.events import EVENT_SCHEMA_VERSION, wisp_event_from_json

event = wisp_event_from_json(line)
assert event.schema_version <= EVENT_SCHEMA_VERSION
```

`wisp_event_from_json()` and `wisp_event_from_dict()` reject non-integer, pre-v5, and future schema
versions. They also enforce introduction versions for many later event types and fields, but they are
not a complete historical-conformance checker: some early additions are structurally valid under an
older readable version. Wisp itself emits only the current schema, so it does not originate such
mixed-version payloads. Consumers auditing third-party or hand-written events should use the
[event-schema history](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#schema-v34--current)
as the authoritative introduction record. For example, `rpc.messages` starts at v17 and its forward
cursor starts at v34.

A wire-visible change requires the next monotonically increasing event schema when it:

- adds or removes an event type or serialized field;
- adds an enum or discriminator value;
- changes field type, requiredness, default-on-the-wire behavior, meaning, or lifecycle ordering; or
- drops a previously readable event version.

Internal refactors, documentation, rendering changes, and behavior that leaves the serialized
contract unchanged do not bump the event schema. Schema numbers are never recycled; a future
incompatible protocol redesign would use a distinct protocol generation instead of resetting this
sequence. A schema change must update
`EVENT_SCHEMA_VERSION`, add a named breadcrumb in `src/wisp/events.py`, add consumer-focused history
to the changelog, and include JSON round-trip and compatibility tests.

Consumers should:

- parse untrusted events with Wisp's parser functions instead of dispatching on `type` manually;
- use `EVENT_SCHEMA_VERSION` from the installed package rather than hard-coding the current maximum;
- handle every known event type they need and deliberately ignore known types they do not use;
- reject a future schema and upgrade Wisp rather than guessing at its meaning; and
- consult the [event-schema history](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#schema-v34--current)
  for the action required by each version.

Dropping a readable event version follows the public deprecation window above and must occur at a
breaking package boundary. The release must identify affected events and persisted sessions and
provide migration guidance before support is removed.

## Persisted session schemas

A session file contains several independently versioned layers:

| Layer | Current writes | Readable history |
|---|---:|---:|
| Session entry | v6 | unversioned and v1–v6 |
| Persisted event envelope | v1 | v1 |
| Event payload inside the envelope | v34 | v5–v34 for typed access |
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
