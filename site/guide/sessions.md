---
title: Sessions
---

# Sessions

Wisp persists each run as a JSONL session and can continue an existing one:

```bash
wisp -p "continue the work" --continue
wisp -p "continue the work" --resume path/to/session.jsonl
wisp -p "continue the work" --resume <session-id-prefix>
```

- `--continue` resumes the newest session in the active session directory.
- `--resume` accepts a JSONL path, filename, full session id, or unique id prefix.
- Sessions live under `~/.wisp/sessions`; override with `--session-dir` or `WISP_SESSION_DIR`.

## What a session file contains

Session files contain provider-facing `message` entries plus selected structured `event` entries
(tool calls, approvals, tool start/end, errors) for audit. They do **not** persist `message.delta`
events. Continuation replays only the selected path's messages and compactions, so audit events never
become model-visible history.

That split is what makes the transcript useful for both purposes at once: the model sees a clean
conversation, while you keep the full record of what actually ran.

## Durability

Wisp treats a JSONL record as committed only when it is newline-terminated. A successful append also
synchronizes the session file before returning. Appends are serialized across cooperating Wisp
processes and rolled back to the previous committed size if writing or synchronization fails.

On the next read, Wisp discards any unterminated final bytes left by an interrupted writer — even if
those bytes happen to form valid JSON — while preserving all newline-terminated records. A malformed
newline-terminated record remains a session error rather than being silently removed.

Session files first created by an append, and recovery deletions, also synchronize the parent
directory on supported POSIX systems. Operations that remove a session suffix stage and validate a
complete replacement before atomically publishing it, so a failed rewrite does not truncate the last
committed history.

This is the mechanism behind the cancellation guarantee in
[Staying in sync](./staying-in-sync): an interrupted run leaves a valid, resumable file rather than a
half-written one.

## Branching

Records form a parent-linked tree, and an append-only active-leaf record selects the root-to-leaf
path used by continuation — abandoned or cancelled work stays in the audit log without entering model
context. Legacy unversioned and v1 linear session files remain readable and are never rewritten on
load. Current files use session-entry schema v6, while embedded event payloads and compaction
records keep their own independent versions. See [Compatibility & versioning](../reference/compatibility)
for the complete readable ranges and migration guarantees.

The typed session API can derive a new session without rewriting its source:

- A **clone** copies the complete active path.
- A **fork** copies the path before a selected user message and returns that prompt for editing.

Copied entries retain stable IDs, parent links, timestamps, and accounting metadata under a new
session ID. These are available to RPC clients via `clone_session` / `fork_session`; direct CLI and
TUI commands are not yet exposed.

::: warning Deprecated
`wisp.agent.messages.SessionEntry(...)` remains available as a factory. New integrations should
import the concrete entry models from `wisp.sessions`.
:::
