# Wisp Review Checklist

Use the relevant sections only. A review does not need to mention every item.

## Architecture

- Is the behavior implemented at the narrowest layer that owns it?
- Does the pure agent loop remain independent of sessions, persistence, and frontends?
- Do CLI, TUI, RPC, and SDK adapters render or transport behavior rather than reimplement it?
- Are providers, tools, and commands registered through the runtime extension API?

## Events And Persistence

- Do event fields survive `model_dump_json()` and `wisp_event_from_json()`?
- Was `EVENT_SCHEMA_VERSION` bumped with a named breadcrumb for a changed event contract?
- Were both message persistence and raw persisted-event paths considered?
- Are bounded scalar fields used instead of opaque or unbounded payloads?
- Do replay and historical rendering consume typed metadata rather than parsed display text?

## Security

- Are project-local inputs ignored until project trust is granted?
- Can any relative or project-controlled path redirect a security-critical file?
- Are canonical containment, protected paths, symlinks, and traversal handled fail-closed?
- Do mutating and command tools still pass through approval policy?
- Can metadata, configuration, or model output accidentally grant authority?

## Agent And Tool Contracts

- Does each provider stream have one start and one terminal event in the required order?
- Does each tool execution yield exactly one terminal result?
- Are errors explicit, bounded, and recoverable rather than silently ignored?
- Are context estimates, compaction, cancellation, and queued messages kept consistent?

## Textual TUI

- Does required state cross the real JSON RPC subprocess boundary?
- Is untrusted text rendered literally rather than through Markdown or terminal markup?
- Are asynchronous `Markdown.update()` calls awaited before measurement or scrolling?
- Are stale input and tail-follow races tested at their actual event boundary?
- Does the regression test fail against the pre-fix implementation without relying on
  `pilot.pause()` to prove ordering?

## Verification

- Is the new behavior covered by a focused deterministic test?
- Do format, lint, mypy, and the full `tests` suite pass?
- Were user-visible behavior and limitations documented?
- Is the diff free of unrelated refactors, generated churn, and stale comments?
