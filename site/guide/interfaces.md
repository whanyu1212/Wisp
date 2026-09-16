---
title: Interfaces
---

# Interfaces

Every interface drives the same RPC command host, `CodingSession`, `AgentHarness`, and
provider-neutral loop. Choose a surface based on who supplies input and how output needs to be
consumed—not because it has a different agent implementation.

| Interface | Start it | Output | Best for |
|---|---|---|---|
| Rust TUI | `wisp` or `wisp tui` | Fullscreen terminal UI | Default in RC2 native-wheel installations |
| Textual TUI | `wisp tui --renderer textual` | Fullscreen terminal UI | Maintained fallback; default without a native binary |
| Line TUI | `wisp tui --line` | Incremental terminal text | Simple terminals and debugging |
| Print | `wisp -p "PROMPT"` | Assistant text on stdout; events on stderr | One-shot prompts and scripts |
| JSON | `wisp -p "PROMPT" --mode json` | One `WispEvent` JSON object per line | Typed one-shot automation |
| JSONL RPC | `wisp --mode rpc` | Commands on stdin; typed events/results on stdout | Long-lived clients and custom UIs |
| Python SDK | Import `InProcessWisp` | Typed async Python API | In-process applications and tests |

In 0.2.0rc2, `wisp`, `wisp tui`, and `wisp --mode tui` use `auto`: prefer the Rust frontend
on macOS/Linux when the active installation declares its native binary, otherwise use Textual.
Native wheels cover macOS arm64 and Linux glibc 2.28+ x86_64. Pure/source installs, Intel macOS, and
other platforms keep Textual. Explicit CLI selection takes precedence over `WISP_TUI_RENDERER`,
which takes precedence over `auto`. `WISP_RUST_TUI_BINARY` also selects Rust in auto mode on
macOS/Linux for source development. Missing or damaged declared binaries and Rust launch/runtime
failures report an error; they never silently switch frontends.

Use `wisp tui --renderer textual` or `WISP_TUI_RENDERER=textual` for the maintained Python
fallback. Textual keeps compatibility and critical fixes; new frontend work prioritizes Rust.
Both clients use the same Python runtime, permissions, providers, and saved sessions.

## Shared semantics, different controls

Session persistence, tool safety, approval decisions, cancellation, provider behavior, and event
ordering are shared. Input capabilities depend on the transport:

- RPC and SDK clients can steer an active run, queue follow-ups, edit queue state, cancel commands,
  and answer approvals.
- The Textual and prompt-toolkit fullscreen TUIs expose steering and follow-up as separate actions:
  while a prompt runs, `Enter` steers, `Alt+Enter` queues a follow-up, and `Alt+Up` restores the
  newest queued item to the composer. They also show authoritative queue state and can cancel the
  active command or answer approvals.
- The line TUI accepts follow-up work while a prompt runs, but its line-oriented input does not
  provide the fullscreen steering and queue-restoration keybindings.
- Print and JSON modes execute one prompt and exit. They cannot accept steering, follow-up, or an
  approval response after the run starts; pass `--yes` only when unattended unsafe execution is
  intentional.

Use [Staying in sync](./staying-in-sync) for queue and cancellation behavior, the
[Python SDK guide](./sdk) for embedding lifecycle and examples, [CLI](../reference/cli) for flags
and stream contracts, and [Architecture](../architecture/) for the shared runtime boundaries.
