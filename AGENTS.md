# AGENTS.md

## Project Mission

Wisp is a Python port of Pi (`@earendil-works/pi-coding-agent`) with deliberate, opinionated modifications.

The goal is **not** to line-for-line clone Pi. The goal is to preserve Pi's useful core ideas—minimal coding-agent harness, strong CLI ergonomics, tool use, sessions, JSON/RPC integration, and extensibility—while adapting them into a Python-first architecture that is easier to embed, inspect, test, and evolve toward a long-running personal assistant gateway.

Use Pi as the behavioral reference, but treat Wisp's architecture and safety model as authoritative.

## Upstream Reference

Pi is installed locally at:

```text
/Users/hanyuwu/.nvm/versions/node/v22.22.1/lib/node_modules/@earendil-works/pi-coding-agent
```

Important upstream files:

```text
/Users/hanyuwu/.nvm/versions/node/v22.22.1/lib/node_modules/@earendil-works/pi-coding-agent/package.json
/Users/hanyuwu/.nvm/versions/node/v22.22.1/lib/node_modules/@earendil-works/pi-coding-agent/README.md
```

Pi package identity:

- npm package: `@earendil-works/pi-coding-agent`
- binary: `pi`
- upstream repository: `https://github.com/earendil-works/pi.git`
- package directory: `packages/coding-agent`
- config directory: `.pi`

Pi's core product shape:

- terminal coding harness
- interactive mode
- print / JSON mode
- RPC mode
- SDK embedding
- session management
- extensions
- skills
- prompt templates
- themes
- default tools: `read`, `write`, `edit`, `bash`

## Wisp Direction

Wisp should become the Python-native equivalent of Pi's coding-agent experience, while taking these opinionated positions:

1. **Event-driven core**
   - Agent behavior should surface through typed `WispEvent` streams.
   - Interfaces should consume events instead of duplicating core logic.

2. **Safety-conscious tool execution**
   - Tools must have explicit safety categories.
   - Read-only tools can run directly.
   - Mutating tools and shell execution should require approval unless the caller has explicitly opted into trust.

3. **Async-first runtime**
   - Prefer async APIs and `anyio`-compatible designs.
   - Avoid blocking the event loop in core agent/runtime paths.

4. **Python-first extensibility**
   - Port Pi's extension/package ideas into Python idioms.
   - Favor typed registration APIs for providers, tools, commands, hooks, and lifecycle events.

5. **CLI/core first, gateway later**
   - Keep the CLI and core agent loop solid before expanding into daemon, assistant gateway, or channel adapters.
   - Future gateway work should build on the same event/session/tool primitives rather than fork behavior.

## Current Wisp Architecture

Key areas in the repository:

```text
src/wisp/agent/loop.py        # async agent loop and tool-calling coordination
src/wisp/tools/               # built-in tool definitions and execution
src/wisp/sessions/            # JSONL session persistence and resume/continue support
src/wisp/runtime/             # runtime registration and extension-facing APIs
src/wisp/cli.py               # command-line entry point and modes
README.md                     # current user-facing project overview
WISP_ROADMAP.local.md         # roadmap and Pi parity / port backlog
```

Expected current capabilities include:

- OpenAI provider/config support
- built-in tools similar to Pi:
  - `read`
  - `write`
  - `edit`
  - `bash`
  - `grep`
  - `find`
  - `ls`
- real tool-calling agent loop
- append-only JSONL sessions
- resume/continue flows
- print mode
- JSONL RPC mode
- minimal TUI / fullscreen TUI foundation

## Roadmap Priorities

When choosing what to build next, prefer work that advances the Pi-port roadmap in this order:

1. **Pi parity for core loops**
   - interactive CLI loop polish
   - print mode polish
   - JSON event mode
   - RPC mode compatibility
   - robust tool-call/result cycles

2. **Session model improvements**
   - resume by ID/name/file
   - branch/fork/clone semantics
   - session import/export
   - compaction support
   - durable metadata

3. **Tool parity and safety**
   - match Pi-like behavior where useful
   - preserve Wisp's explicit approval model
   - keep tool results structured and event-friendly
   - add tests for truncation, errors, path handling, and permissions

4. **TUI foundation**
   - line-oriented TUI first
   - fullscreen renderer second
   - keep UI as a consumer of RPC/events
   - do not bury agent logic in the UI layer

5. **Extension system**
   - provider registration
   - tool registration
   - lifecycle/event hooks
   - custom commands/UI hooks where appropriate
   - eventually Python package-based sharing

6. **Assistant gateway future**
   - split reusable core from long-running daemon
   - channel adapters: Telegram, Discord, Slack, etc.
   - connectors: GitHub, Calendar, Notion, etc.
   - memory scopes: personal, project, daily
   - authentication/pairing flows for remote channels

## Design Rules for Agents

When working in this repository:

- Treat `WISP_ROADMAP.local.md` as the strategic source of truth.
- Treat Pi as a reference implementation for behavior and product shape.
- Do not copy Pi internals blindly.
- Prefer small, well-tested Python modules over large ports.
- Keep the core independent from any one UI.
- Keep provider-specific behavior behind provider interfaces.
- Keep tool execution separate from model/provider code.
- Prefer structured data and typed events over parsing terminal strings.
- Preserve existing public behavior unless the requested change explicitly alters it.
- Avoid broad rewrites when a surgical port or compatibility layer is enough.

## Coding Standards

Use the existing project style:

- Python with type hints.
- Keep code compatible with the project's configured Python version.
- Use `uv` for dependency and environment workflows.
- Run formatting/linting/type checks before considering work complete.
- Keep public APIs typed and documented enough for future extension authors.
- Add tests for new behavior and bug fixes.
- Prefer deterministic tests over tests that require live model/API access.

Before committing substantial changes, run the relevant checks, typically:

```bash
uv run ruff check .
uv run mypy .
uv run pytest
```

If a command differs in the repository docs or `pyproject.toml`, follow the repository configuration.

## Repository Workflow Reliability

For Wisp repository work:

- When a user asks for a branch from current or refreshed `main`, fetch `origin`, verify
  `origin/main`, and base the branch on that verified remote state before describing it as current.
- If fetching fails because of network, authentication, or remote errors, report the failure rather
  than silently falling back to stale local refs.
- Preserve unrelated dirty or untracked files when switching branches or preparing commits.
- A timed-out verification command is inconclusive. Do not report it as passing; retry with a
  suitable timeout or focused command when practical, otherwise mark it unverified.
- Record the completed result before claiming `uv run ruff check .`, the configured mypy command,
  or `uv run pytest` passed. Partial output is not a successful result.
- Do not run pytest against anything under `refs/`; reference repositories are not part of Wisp's
  test suite. Scope pytest runs to Wisp's own tests (for example, `uv run pytest tests`).
- Always finish implementation work with a concise summary of changes, passed/failed/timed-out/not
  run checks, and any remaining blockers or uncertainty.

## Tool and Safety Expectations

All built-in tools should have clear behavior and safety classification.

General expectations:

- `read`, `grep`, `find`, `ls` are read-only.
- `write` and `edit` are mutating.
- `bash` is command execution and should be treated as high-risk unless explicitly approved.
- Tool calls should produce structured results suitable for CLI, TUI, RPC, and tests.
- Tool errors should be explicit and recoverable.
- Avoid silent failure.

For filesystem tools:

- Validate paths carefully.
- Avoid accidental writes outside the intended working directory.
- Preserve existing files unless overwrite behavior is explicit.
- Return useful truncation/line metadata for large outputs.

For shell execution:

- Avoid interactive commands.
- Surface command, exit code, stdout/stderr, and truncation state.
- Keep approval and trust decisions outside the model's direct control.

## Interface Expectations

Wisp should support multiple frontends over one core:

- CLI print mode
- interactive CLI/TUI
- JSONL RPC
- future SDK/embedding
- future daemon/gateway channels

Do not implement separate agent loops per interface. Add interface behavior by consuming events and sending user/tool approval responses through well-defined APIs.

## Porting Guidance from Pi

When porting a Pi feature, document the answer to these questions in the change or PR:

1. What Pi behavior is being matched?
2. Where is the relevant Pi source or README section?
3. What is intentionally different in Wisp?
4. How is the behavior exposed through Wisp events/RPC?
5. What tests prove the behavior?

Acceptable differences from Pi include:

- stronger approval requirements
- more explicit typing
- Pythonic APIs instead of TypeScript APIs
- event-first architecture
- simpler MVP versions before full UI parity
- omission of features that do not fit Wisp's roadmap

## Documentation Expectations

Keep docs aligned with the roadmap and implementation.

When adding a user-visible feature:

- update `README.md` if users need to know it exists
- update roadmap notes if a milestone is completed or changed
- add examples for new modes, tools, or extension APIs
- keep `AGENTS.md` focused on contributor/agent guidance rather than full user docs

## Definition of Done

A change is not done until:

- it implements the requested behavior
- it is scoped to the roadmap or clearly justified
- tests cover the new or changed behavior
- lint/type checks pass where applicable
- docs are updated when user-facing behavior changes
- safety/approval implications are considered
- no unrelated refactors or formatting churn are included

## Non-Goals

Avoid these unless explicitly requested:

- rewriting the project from scratch
- copying Pi source wholesale
- adding large framework dependencies without a clear need
- making the TUI own core agent behavior
- bypassing approval flows for convenience
- implementing remote assistant/channel features before the local core is stable
