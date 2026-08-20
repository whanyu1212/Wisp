---
title: Interfaces
---

# Interfaces

Every interface drives the same RPC command host, `CodingSession`, `AgentHarness`, and
provider-neutral loop. Choose a surface based on who supplies input and how output needs to be
consumed—not because it has a different agent implementation.

| Interface | Start it | Output | Best for |
|---|---|---|---|
| Textual TUI | `wisp` or `wisp tui` | Fullscreen terminal UI | Interactive repository work |
| Line TUI | `wisp tui --line` | Incremental terminal text | Simple terminals and debugging |
| Print | `wisp -p "PROMPT"` | Assistant text on stdout; events on stderr | One-shot prompts and scripts |
| JSON | `wisp -p "PROMPT" --mode json` | One `WispEvent` JSON object per line | Typed one-shot automation |
| JSONL RPC | `wisp --mode rpc` | Commands on stdin; typed events/results on stdout | Long-lived clients and custom UIs |
| Python SDK | Import `InProcessWisp` | Typed async Python API | In-process applications and tests |

## Shared semantics, different controls

Session persistence, tool safety, approval decisions, cancellation, provider behavior, and event
ordering are shared. Input capabilities depend on the transport:

- RPC and SDK clients can steer an active run, queue follow-ups, edit queue state, cancel commands,
  and answer approvals.
- The TUI can queue follow-up prompts, cancel the active command, and answer approvals, but it does
  not currently expose the steering queue as a separate user action.
- Print and JSON modes execute one prompt and exit. They cannot accept steering, follow-up, or an
  approval response after the run starts; pass `--yes` only when unattended unsafe execution is
  intentional.

Use [Staying in sync](./staying-in-sync) for queue and cancellation behavior, the
[Python SDK guide](./sdk) for embedding lifecycle and examples, [CLI](../reference/cli) for flags
and stream contracts, and [Architecture](../architecture/) for the shared runtime boundaries.
