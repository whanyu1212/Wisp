---
title: Project file discovery
description: Contract for the get_project_files RPC command and bounded, protected-path-aware rpc.project_files reports.
route: reference/project-files.html
section: reference
order: 4
layout: docs
listed: true
searchable: true
---

# Project file discovery

`get_project_files` discovers names under the active tool working directory using
the backend's resolved protection policy. The command accepts an optional
correlation `id` and two options.

| Options | Reports |
| --- | --- |
| No options | One fresh, bounded full-project snapshot. |
| `directory: ""` | One report listing the root's direct children. |
| `directory: "src/example"` | One report listing the selected directory's direct children. |
| `progressive: true` | A bounded full-project scan that can report intermediate snapshots and always sends a final snapshot on success. |

Directory scopes are display-safe relative POSIX paths. A directory scope and
`progressive: true` cannot be combined. All reports appear between
`rpc.command.started` and `rpc.command.finished`.

Each `rpc.project_files` report includes `command_id`, a process-local policy
`generation`, `entries` with project-relative POSIX `path` and `kind` (`file` or
`directory`), `truncated`, and the standard event `timestamp`. The report's
`directory` echoes the requested scope and is null for a full-project scan.
`in_progress` is true only for intermediate full-project snapshots. An
intermediate snapshot contains the accumulated entries within the report budget,
so clients can apply their search to it directly. A successful scan ends with an
`in_progress: false` report even if its entries match the most recent intermediate report.

A refresh rescans the filesystem. The backend has no watcher, snapshot cache,
pagination, or guarantee that an entry still exists after discovery.

## Bounds and ordering

A scan can return up to 10,000 files and directories, explore up to 12 levels,
and examine up to 50,000 raw directory entries, including rejected names. A
directory at the depth limit can appear in its parent's listing while its own
listing is truncated. Entry, depth, work, and byte limits set `truncated`.
Each report fits within 1 MiB, including the encoded event and JSONL newline.
Retained paths are sorted with parents before descendants in full-project reports.
An individual directory that exceeds the enumeration budget is omitted in full.

Directory listings and full-project requests without progress have a
2.5-second deadline. Progressive requests have a 30-second deadline and
can report progress between directories up to eight times. Those updates start
250 milliseconds apart and become less frequent during long scans.
Cancellation, timeout, or another scan failure ends the
request without a final snapshot. A client can retain earlier usable snapshots
while they belong to the current policy generation.

Directories are opened with guarded filesystem helpers. Symlinks and special
files are omitted. A selected branch must also pass the protected-path and
ignored-directory checks for each of its components. Paths are advisory names
and later tool operations perform their own access checks. Reports omit denied
names, absolute roots, file contents, raw filesystem errors, and names that
cannot be displayed safely.

Each host has one physical worker slot each for directory listings, full-project
scans, and Git diff reads. Each slot remains occupied
until its worker exits, including when a blocked filesystem call outlasts
cancellation of the request. Another request for that slot receives a generic
busy failure. Discovery shares command IDs, cancellation, and bounded outstanding
accounting with auxiliary reads. EOF and shutdown cancel discovery and wait
for its terminal command event.

## Policy transitions and clients

Before applying trusted configuration, the host advances the policy generation,
cancels earlier discovery requests, and publishes `project_files.invalidated`.
Clients must clear earlier snapshots and discard reports for an older generation
or an obsolete request ID. Requests received during the transition can wait for
policy settlement and remain cancellable.

The backend reserves candidate protected paths before adopting the new provider
configuration. It retains every protection learned during a host's lifetime,
including after failed or cancelled adoption. The backend checks generation and
cancellation under its output lock before publishing each partial or final
report. If a report has begun publication, it finishes and any subsequent
invalidation tells clients to discard it.

## Frontend transition

The Files sidebar fetches the root and expanded directories independently.
Global filtering and `@` completion share the full-project catalog and rank it
locally.

The contract is part of the current release's live RPC bundle. The backend and
frontend must be the same Wisp release.
