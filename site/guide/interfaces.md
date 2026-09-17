# Interfaces

Every interface drives the same RPC command host, `CodingSession`, `AgentHarness`, and
provider-neutral loop. Choose a surface based on who supplies input and how output needs to be
consumed—not because it has a different agent implementation.

| Interface | Start it | Output | Best for |
|---|---|---|---|
| Rust TUI | `wisp` or `wisp tui` | Fullscreen terminal UI | Native-wheel installations |
| Print | `wisp -p "PROMPT"` | Assistant text on stdout; events on stderr | One-shot prompts and scripts |
| JSON | `wisp -p "PROMPT" --mode json` | One `WispEvent` JSON object per line | Typed one-shot automation |
| JSONL RPC | `wisp --mode rpc` | Commands on stdin; typed events/results on stdout | Long-lived clients and custom UIs |
| Python SDK | Import `InProcessWisp` | Typed async Python API | In-process applications and tests |

Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 include the Rust TUI and Python backend.
Pure-wheel installs retain print, JSON, RPC, and SDK, but an interactive TUI command reports that no
Rust binary is available. Source checkouts can build the binary and set `WISP_RUST_TUI_BINARY` to its
absolute path; see [Development setup](../contributing/development#rust-tui-scaffold). All interfaces
use the same Python runtime, permissions, providers, and saved sessions.

## Shared semantics, different controls

Session persistence, tool safety, approval decisions, cancellation, provider behavior, and event
ordering are shared. Input capabilities depend on the transport:

- RPC and SDK clients can steer an active run, queue follow-ups, edit queue state, cancel commands,
  and answer approvals.
- The Rust TUI exposes steering and follow-up as separate actions:
  while a prompt runs, `Enter` steers, `Alt+Enter` queues a follow-up, and `Alt+Up` restores the
  newest queued item to the composer. It also shows authoritative queue state and can cancel the
  active command or answer approvals.
- Print and JSON modes execute one prompt and exit. They cannot accept steering, follow-up, or an
  approval response after the run starts; pass `--yes` only when unattended unsafe execution is
  intentional.

Use [Staying in sync](./staying-in-sync) for queue and cancellation behavior, the
[Python SDK guide](./sdk) for embedding lifecycle and examples, [CLI](../reference/cli) for flags
and stream contracts, and [Architecture](../architecture/) for the shared runtime boundaries.
