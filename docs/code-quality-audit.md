# Code Quality Audit — July 2026

This audit captures the current Wisp hotspots before ramping up more TUI work. It is intentionally scoped to code organization and reviewability, not feature behavior.

## Snapshot

Largest files by line count:

| Area | File | Lines | Notes |
| --- | --- | ---: | --- |
| TUI tests | `tests/test_tui.py` | ~1820 | Renderer, live input, shell state, run_tui integration, and CLI env tests in one module. |
| CLI tests | `tests/test_cli.py` | ~1470 | Print, JSON, RPC, approval policy, tool exposure, and provider errors in one module. |
| Tools | `src/wisp/tools/builtin.py` | ~1330 | Read/write/edit/bash/grep/find/ls plus subprocess/rg helpers in one module. |
| CLI | `src/wisp/cli/__init__.py` | ~990 | Typer callback, print mode, JSON mode, RPC server, and stdin readers remain in the package root after helper extraction. |
| TUI rendering | `src/wisp/tui/rendering.py` | ~525 | Renderer protocol, line renderer, fullscreen renderer, shared state. |
| TUI shell | `src/wisp/tui/shell.py` | ~485 | Shell input loop, RPC event loop, signal handling, and renderer synchronization after app split. |

Largest implementation objects by simple AST span:

| Symbol | Span | Concern |
| --- | ---: | --- |
| `TuiShell` | ~400 lines | Multiple responsibilities: input, state transitions, command dispatch, renderer sync. |
| `LiveFullscreenTui` | ~280 lines | prompt-toolkit app construction, input semantics, paste handling, rendering adapter. |
| `Agent` | ~280 lines | Core loop and provider/tool/session orchestration. Stable enough to defer. |
| `FullscreenTuiRenderer` | ~250 lines | Layout state, transcript rendering, event rendering. |
| `cli_callback()` | ~180 lines | User-facing option resolution and dispatch. |

## Quality assessment

Overall, the codebase is still healthy for an early agent project: behavior is covered by deterministic tests, fake provider remains available, and major flows are separated at a coarse package level. The main issue is reviewability: several modules now mix many subdomains, so small changes require scanning long files and large test modules.

The highest-risk production areas are TUI input/state transitions and RPC orchestration. The recent Codex feedback clustered around subtle live-input races, which suggests we should reduce TUI surface area before adding more interaction features.

## Refactor priorities

### 1. Split oversized tests first

This PR starts with test decomposition because it is behavior-preserving and lowers review friction immediately. Test groups should mirror product boundaries:

- TUI rendering/layout
- Live fullscreen adapter
- TUI shell state machine
- TUI run/CLI integration
- CLI print/JSON modes
- CLI RPC mode and stdin readers
- CLI tool policy/error behavior

### 2. Continue splitting the `wisp.cli` package

Completed first boundaries:

- `wisp.cli` — compatibility package root, Typer app, print mode, and RPC state machine
- `wisp.cli.options` — env/default resolution helpers
- `wisp.cli.output` — JSONL/text event rendering helpers
- `wisp.cli.tools` — tool exposure, approval policy, and session-selection helpers
- `wisp.cli.types` — shared CLI mode/error types

Recommended follow-up boundaries:

- `wisp.cli.print_mode` — print/JSON prompt execution
- `wisp.cli.rpc` — JSONL RPC server loop and stdin readers

Acceptance criteria: no behavior changes, CLI tests remain green, public command surface unchanged.

### 3. Continue splitting the `wisp.tui` package

Completed first boundaries:

- `wisp.tui.app` — `run_tui()` wiring plus compatibility aliases for existing imports/tests
- `wisp.tui.launch` — `TuiOptions`, preflight validation, subprocess command/env construction, and stdio detection
- `wisp.tui.state` — `TuiStatus`, interaction/view state, input signal dataclasses, and input-mode helpers
- `wisp.tui.shell` — `TuiShell` state machine and controller-facing event loop

Recommended follow-up boundaries:

- `wisp.tui.shell` — smaller input, approval/cancel, and RPC-event handler helpers if the class keeps growing
- `wisp.tui.rendering` — separate fullscreen layout rendering from renderer protocols/shared transcript types

Acceptance criteria: no behavior changes; TUI shell tests map to state/input/RPC concerns.

### 4. Split `src/wisp/tools/builtin.py`

Recommended boundaries:

- `wisp.tools.file_ops` — read/write/edit and path-safe file helpers
- `wisp.tools.search` — grep/find/ls and ripgrep parsing/truncation
- `wisp.tools.shell` — bash execution and subprocess limits
- `wisp.tools.builtin` — small registry/factory module

Acceptance criteria: tool schemas/names/outputs unchanged.

## Near-term recommendation

Before adding more TUI features, complete these low-risk organization PRs:

1. **PR #29** — audit report plus TUI/CLI test split.
2. **PR #30** — extract low-risk CLI helper modules.
3. **PR #31** — migrate CLI helpers into a `wisp.cli` subpackage.
4. **PR #32** — split `tui/app.py` around shell state and launch wiring.
5. **PR #33** — split `tools/builtin.py` by tool family.

After that, resume live fullscreen usability work, especially transcript scrollback and clearer status/approval surfaces.
