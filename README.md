<p align="center">
  <img src="https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner-v2.png" alt="Wisp: redirect it while it works, approve what it changes." width="100%">
</p>

<h1 align="center">Wisp</h1>

<p align="center">
  A terminal coding agent you can steer while it works.<br>
  It asks before it edits files or runs commands, and records every step it takes.
</p>

<p align="center">
  <a href="https://pypi.org/project/wisp-ai/"><img src="https://img.shields.io/pypi/v/wisp-ai?label=PyPI" alt="PyPI version"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12+"></a>
  <a href="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml"><img src="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License"></a>
</p>

<p align="center">
  <a href="https://whanyu1212.github.io/Wisp/">Documentation</a> ·
  <a href="#install">Install</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="https://whanyu1212.github.io/Wisp/guide/sdk.html">Python SDK</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

Wisp reads your project, edits files and runs commands on your behalf. You can correct its course
mid-task without starting over, and anything that writes a file or runs a shell command waits for
your approval. Each run is saved as a typed event log that you can resume, branch or audit later.

The terminal UI, the one-shot CLI, a JSONL RPC process and an in-process Python SDK all drive the
same runtime, so sessions, tools, approvals and cancellation behave the same way in each of them.
Wisp works with OpenAI (including a ChatGPT subscription), Anthropic, Google, xAI and DeepSeek.

## Install

Wisp needs Python 3.12 or newer and runs on macOS and Linux. Windows is best-effort until it has
dedicated CI coverage.

```bash
uv tool install --python 3.12 wisp-ai
```

On macOS arm64 and Linux x86_64 (glibc 2.28+), the native wheel includes the Rust terminal UI. Other
platforms get a pure wheel, which keeps print, JSON, RPC and SDK use but has no terminal UI. Keep
`--python 3.12` (or newer): without it, uv may resolve for an older default Python and pick the pure
wheel. Run `uv tool update-shell` once if `wisp` is not on your `PATH`.

The [installation guide](https://whanyu1212.github.io/Wisp/guide/installation.html) covers updates
and troubleshooting. Coming from 0.1? Read the
[upgrade guide](https://whanyu1212.github.io/Wisp/guide/upgrading.html) first.

## Quickstart

Start Wisp in the project you want it to work on:

```bash
cd path/to/project
wisp
```

Type `/connect` to choose a provider. **OpenAI → ChatGPT Plus/Pro** signs in with your subscription
through a device code; the same panel takes API keys for OpenAI, Anthropic, Google, xAI and
DeepSeek. Then ask for something:

```text
explain the architecture of this repository
```

For scripts and one-off questions, use print mode. The `fake` provider runs offline without any
credentials, which is a quick way to check an install:

```bash
wisp -p "summarize the current changes"
wisp -p "hello" --provider fake
```

## Working with Wisp

- **Steering.** A correction sent during a run is applied at the next safe point between model
  requests, and the tool work already done is kept. In the TUI, `Enter` steers the active run and
  `Alt+Enter` queues a follow-up for when it finishes; RPC and SDK clients have the same controls.
- **Approvals.** Reads run directly. Writes, edits and shell commands stop and ask, and the model
  cannot change that policy. Print mode refuses them unless you pass `--yes`.
- **Cancellation.** Cancelling unwinds in-flight tool work and leaves the session valid, so
  `--continue` resumes from the last committed state.
- **Transcripts.** Every action is a typed `WispEvent`, appended to a JSONL session file in a fixed
  order. Sessions can be resumed, branched and inspected after the run.
- **Project trust.** Wisp ignores a project's local settings, `AGENTS.md` instructions and skills
  until you trust it, and never saves trust or YOLO mode for your home folder.

[Staying in sync](https://whanyu1212.github.io/Wisp/guide/staying-in-sync.html) and
[Tools & safety](https://whanyu1212.github.io/Wisp/guide/tools-and-safety.html) explain how these
guarantees are enforced.

## Interfaces

| Mode | Command | Output | Use it for |
|------|---------|--------|------------|
| TUI | `wisp` | Fullscreen Rust terminal UI | Interactive work |
| Print | `wisp -p "…"` | Assistant text on stdout, events on stderr | One-shot prompts and scripts |
| JSON | `wisp -p "…" --mode json` | One `WispEvent` JSON object per line | Machine-readable automation |
| RPC | `wisp --mode rpc` | Typed JSONL commands and events | Long-lived integrations and other frontends |

The [TUI guide](https://whanyu1212.github.io/Wisp/guide/tui.html) covers commands, keybindings,
themes, prompt history, MCP servers and skills.

## Python SDK

The SDK runs the same runtime in your own process and streams typed events:

```python
import anyio

from wisp.config import WispConfig
from wisp.events import MessageDelta, RpcCommandFinished
from wisp.sdk import InProcessOptions, InProcessWisp


async def main() -> None:
    wisp = await InProcessWisp.start(
        WispConfig(provider="fake"),  # offline provider; no credentials needed
        options=InProcessOptions(startup_trusted=True),
    )
    async with wisp:
        events = wisp.events()
        command_id = await wisp.prompt("hello from the SDK")
        async for event in events:
            if isinstance(event, MessageDelta) and event.content_kind == "text":
                print(event.delta, end="", flush=True)
            elif isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                print()
                break


anyio.run(main)
```

More examples, covering approvals, saved sessions and running Wisp as a subprocess, are in
[`examples/sdk/`](examples/sdk/). The
[SDK guide](https://whanyu1212.github.io/Wisp/guide/sdk.html) and
[reference](https://whanyu1212.github.io/Wisp/reference/sdk.html) document the full surface.

## How it's built

Every interface is a thin adapter over one event-driven runtime:

```text
CLI / JSONL-RPC / SDK adapters → RPC command host → CodingSession → AgentHarness → run_agent_loop
```

The agent loop runs the provider and tool cycle and knows nothing about sessions or frontends. The
harness keeps the conversation in memory, `CodingSession` adds persistence and safety policy, and
interfaces consume typed events. The Rust TUI is an RPC client of this runtime, not a second agent
loop, so it gets the same guarantees as everything else.

The [architecture guide](https://whanyu1212.github.io/Wisp/architecture/) describes each layer.
[Crafting Coding Agents](https://whanyu1212.github.io/Wisp/crafting-agents/) is a tutorial that walks
through the engineering decisions behind a coding agent, with runnable checkpoints.

## Documentation

| | |
|---|---|
| [Guide](https://whanyu1212.github.io/Wisp/guide/) | Installation, providers, tools, sessions, skills and the TUI |
| [Python SDK](https://whanyu1212.github.io/Wisp/guide/sdk.html) | Embedding Wisp, typed events, safety requests and sessions |
| [Reference](https://whanyu1212.github.io/Wisp/reference/) | CLI, SDK, configuration and environment variables |
| [Architecture](https://whanyu1212.github.io/Wisp/architecture/) | Runtime layers and ownership boundaries |
| [Contributing](https://whanyu1212.github.io/Wisp/contributing/) | Development setup, testing and how Wisp is built |

## Contributing

Bug reports, fixes and documentation improvements are welcome. [CONTRIBUTING.md](CONTRIBUTING.md)
lists the checks to run before opening a pull request. The test suite runs against deterministic
fake providers, so it needs no API keys or network access. Please report security issues privately
as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
