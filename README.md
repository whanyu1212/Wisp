# Wisp

A Python, Pi-inspired coding agent experiment.

## Development

```bash
uv sync
uv run wisp -p "hello"
uv run pytest
```

By default, Wisp uses a deterministic fake provider so the agent core, CLI, and JSONL sessions can be tested without API keys.

## Configuration

Copy `.env.example` to `.env` for local settings:

```bash
cp .env.example .env
```

Available environment variables:

```env
WISP_PROVIDER=fake
WISP_MODEL=
WISP_MODE=
WISP_TUI_RENDERER=
WISP_SESSION_DIR=
WISP_AUTH_FILE=~/.wisp/auth.json
OPENAI_API_KEY=
```

CLI flags override environment variables:

```bash
uv run wisp -p "hello" --provider fake
uv run wisp -p "hello" --provider openai --model gpt-5.5
uv run wisp -p "hello" --provider openai-codex --model gpt-5.5
```

`OPENAI_API_KEY` is required only when using the `openai` provider. To use ChatGPT Plus/Pro subscription access, run `uv run wisp auth login openai-codex` and select `WISP_PROVIDER=openai-codex`; OAuth credentials are stored in `WISP_AUTH_FILE` (default `~/.wisp/auth.json`) with private permissions. Sessions default to OS temp storage; set `WISP_SESSION_DIR` or pass `--session-dir` to keep them somewhere durable. Set `WISP_MODE=tui` to make bare `wisp` launch the TUI, and `WISP_TUI_RENDERER=fullscreen` to make TUI launches use the live fullscreen renderer by default. `wisp -p "hello"` still uses text mode unless `--mode` is passed explicitly. Never commit `.env`, auth files, or real API keys.

## Default prompt and project context

Each agent turn sends a small default coding-agent system prompt plus a bounded project context message before the user prompt. The context includes:

- current working directory
- git branch and a capped short status summary when available
- detected root project files such as `pyproject.toml`, `package.json`, or `README.md`
- the tools currently exposed to the model, or that no tools are exposed

These prompt/context messages are persisted in the JSONL session so the provider-visible input is auditable. Context is informational only: it does not change the print-mode tool exposure policy described below.

## Session continuation

Print mode can continue an existing JSONL session:

```bash
uv run wisp -p "continue the work" --continue
uv run wisp -p "continue the work" --resume path/to/session.jsonl
uv run wisp -p "continue the work" --resume <session-id-prefix>
```

By default, Wisp stores sessions under a private, non-precreatable OS temp directory (`<tmp>/wisp-<user>-*/sessions`) created for the current process. `--continue` resumes the newest session in the active session directory. `--resume` accepts a JSONL path, filename, full session id, or unique id prefix. Use `--session-dir` or `WISP_SESSION_DIR` for durable session storage and cross-invocation `--continue`. Wisp rebuilds the current prompt/context for the new turn and reuses prior non-system conversation messages as history, so stale project context from earlier turns is not replayed as instructions.

Session JSONL files contain provider-facing `message` entries plus selected structured `event` entries for audit/debugging. Wisp persists tool calls, approvals, tool execution start/end, and errors, but not `token.delta` events. Session continuation reads only message entries, so audit events do not become model-visible history.

## Local tools

Wisp registers built-in local tools through the extension API:

- `read`
- `write`
- `edit`
- `bash`
- `grep`
- `find`
- `ls`

The tool registry is available to runtime/extensions and the agent tool loop.
File tools are sandboxed to the tool context working directory by default.

Print-mode CLI does not expose tools to the model unless explicitly requested:

```bash
uv run wisp -p "list files" --provider openai --allow-read-tools
uv run wisp -p "run tests" --provider openai --allow-tool bash --yes
```

Read tools (`read`, `grep`, `find`, `ls`) can be enabled together with
`--allow-read-tools`. Mutating tools (`write`, `edit`) and command execution
(`bash`) require explicit `--allow-tool <name>` opt-in.

Because print mode is non-interactive, mutating and command tools are still
blocked at execution time unless you also pass `--yes` (alias:
`--allow-unsafe-tool-execution`). Without that override, the model receives a
clear tool error instead of Wisp executing the operation.

Print mode keeps assistant text on stdout and writes operational events to
stderr, including tool calls, approval decisions, tool result summaries, and the
saved session path. Stderr may include spacing for terminal readability, while
stdout remains assistant-only and pipe-friendly.

For machine-readable integrations, use JSONL event output:

```bash
uv run wisp -p "hello" --mode json
```

JSON mode writes each `WispEvent` as one JSON object per line on stdout,
including `token.delta`, tool lifecycle events, errors, and `session.saved`.
Assistant text is not written as raw text in this mode.

For long-lived integrations, use JSONL RPC mode over stdin/stdout:

```bash
printf '{"type":"prompt","prompt":"hello"}\n{"type":"shutdown"}\n' | uv run wisp --mode rpc
```

RPC mode currently supports sequential commands:

- `{"id":"cmd-1","type":"prompt","prompt":"..."}` runs one agent turn and streams `WispEvent` JSONL.
- `{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}` requests cancellation of the running prompt.
- `{"id":"approval-1","type":"approval","call_id":"call-1","approved":true}` approves or denies a pending tool approval request.
- `{"id":"cmd-2","type":"shutdown"}` exits cleanly.

The `id` field is optional; Wisp generates one when omitted. Each command emits
`rpc.command.started` and `rpc.command.finished` events so clients can group the
agent events that occur between them. Prompt commands run sequentially; `cancel`
and `approval` commands are handled while a prompt is running, and other commands
wait for the current prompt to finish. When an allowed mutating/command tool needs
approval, Wisp emits `tool.approval.requested` with the tool `call_id`; clients
respond with an `approval` command using that `call_id`, a boolean `approved`, and
an optional denial `reason`. Cancellation is best-effort for providers/tools.
Provider, model, tool exposure, approval, session, and max-iteration CLI flags
apply to the whole RPC process.

Python integrations that do not want to hand-roll JSONL can use the typed RPC
client/controller helpers:

```python
from wisp.events import RpcCommandFinished
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController

transport = await JsonlSubprocessRpcTransport.start()
controller = RpcController(transport)
prompt_id = await controller.prompt("hello")
shutdown_id = None
try:
    async for event in controller.events():
        ...
        if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
            shutdown_id = await controller.shutdown()
        elif (
            isinstance(event, RpcCommandFinished)
            and shutdown_id is not None
            and event.command_id == shutdown_id
        ):
            break
finally:
    await controller.close()
```

`RpcController` exposes typed `prompt`, `cancel`, `approve`, and `shutdown`
methods and yields parsed `WispEvent` objects. This is intended as the stable
integration layer for future TUI work.

## TUI MVP

Wisp also includes a minimal terminal UI shell built on the RPC controller:

```bash
uv run wisp --mode tui --provider fake
uv run wisp --mode tui --provider fake --tui-renderer fullscreen
uv run wisp --mode tui --provider openai --allow-read-tools
uv run wisp --mode tui --provider openai --allow-tool bash
```

For a shorter daily launch, configure `.env`:

```env
WISP_PROVIDER=openai-codex
WISP_MODE=tui
WISP_TUI_RENDERER=fullscreen
WISP_SESSION_DIR=~/.wisp/sessions
WISP_AUTH_FILE=~/.wisp/auth.json
```

Then run:

```bash
uv run wisp
```

If you only set provider and renderer defaults, `uv run wisp --mode tui` is enough.

The default line-oriented TUI currently provides streamed assistant text, basic
tool call/result rendering, queued follow-up input while a prompt is running,
interactive approval prompts for mutating/command tools, Ctrl-C interrupt
handling, Ctrl-D shutdown, and `/help` plus `/quit` commands. On interactive
terminals, opt-in `--tui-renderer fullscreen` uses a live prompt-toolkit screen
with transcript/status/input regions that owns the input line. For piped input,
embedded tests, or explicit prompt readers, fullscreen falls back to the
line-oriented input path while preserving the same renderer state model.

Wisp does not cap model/tool rounds by default, matching Pi's permissive agent
loop. If you want a non-interactive fuse for a run, pass
`--max-tool-iterations <n>`.
