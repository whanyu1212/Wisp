---
title: Project file discovery
---

# Project file discovery

`get_project_files` returns one fresh, bounded `rpc.project_files` report between
`rpc.command.started` and `rpc.command.finished`. The command accepts an optional
correlation `id`; Python chooses the active tool working directory and resolved
protection policy. It does not accept client paths, queries, or policy overrides.

The report contains `command_id`, a process-local policy `generation`, `entries`
with relative POSIX `path` and `kind` (`file` or `directory`), and `truncated`.
Standard event version and timestamp fields also apply. No absolute root,
credential paths, denied-entry counts, file contents, or raw filesystem errors
are returned. Paths are advisory names; later tool operations still perform their
own access checks. Names that cannot be represented safely in UTF-8 or contain
controls, bidirectional overrides, or ambiguous backslashes are omitted.

Clients derive hierarchy from path components and rank the snapshot locally.
This supports tree browsing and fuzzy completion without a round trip for each
keystroke or cross-language highlight-index conversions. Match offsets belong
to the client renderer. A new request rescans the filesystem; there is no watcher,
server snapshot cache, pagination, or filesystem freshness guarantee after a scan.

## Bounds and ordering

The default scan accepts at most 10,000 files/directories, traverses at most 12
levels, examines at most 50,000 raw directory entries (including rejected names),
and has a cooperative 2.5-second deadline. Reports including their JSONL newline
fit within 1 MiB, measured with the actual escaping and event envelope.

Directories are opened with the shared guarded filesystem helpers. Symlinks and
special files are never scanned. Each directory is materialized within the work
budget before sorting; a directory that cannot be enumerated within that budget
is omitted in full. Entry/depth/work/byte limits produce a sorted truncated
snapshot with parents retained before descendants. Cancellation or timeout returns
no snapshot. Thus successful snapshot selection is deterministic for an unchanged
accessible filesystem; timeouts do not expose timing-dependent prefixes.

Checks run between filesystem operations. The RPC also stops awaiting the scan
at the deadline. A blocked OS call itself cannot be interrupted, but it runs off
the command loop. Only one physical scan can run per
host: even after its awaiting task is cancelled, its admission slot remains held
until the worker exits. Additional requests receive a generic busy failure.
Discovery shares command IDs, cancellation, and bounded outstanding accounting
with auxiliary reads, but does not hold up prompts or session operations.
EOF and shutdown cancel discovery and wait for its terminal command event.

## Policy transitions and clients

Before trusted configuration is applied, the host serializes
`project_files.invalidated` with an advanced generation and cancels old discovery.
Clients must clear earlier snapshots and reject reports for an older generation
or a request ID they no longer want. A request arriving during the transition may
wait for policy settlement; it remains cancellable. Additional requests are busy.

Candidate protected paths are reserved before provider adoption, including the
new credential path. All protection learned during a host's lifetime is retained
for discovery, including after failed or cancelled adoption. Success and failure
both settle waiters against the active policy plus these reserved protections.
The host checks generation and cancellation again under its output lock before
committing a report. A report already committed to output finishes publication;
a subsequent invalidation tells clients to discard it.

## Frontend transition

Textual uses the same scanner and snapshot types through `wisp.project_files`,
while retaining its existing UI worker, local matching, and stale-request handling.
External frontends use the RPC capability. Rust's typed protocol client can request
and decode snapshots; the Rust picker UI is a separate change. Agent grep/find
behavior and authorization are unchanged.

The contract uses live protocol v6 and event schema v37. Historical v1–v5 protocol
bundles stay immutable, and supported persisted event schemas remain readable.
