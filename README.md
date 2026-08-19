<p align="center">
  <img src="https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner-v2.png" alt="Wisp — A coding agent that stays in sync, illustrated by a glowing spectral companion linked to a terminal." width="100%">
</p>

# Wisp

<p align="center">
  <strong>A coding agent that stays in sync with you.</strong><br>
  Redirect it while it works. Approve what it changes. Inspect everything it did.
</p>

<p align="center">
  <a href="#install">Install</a>
  ·
  <a href="#quickstart">Quickstart</a>
  ·
  <a href="#staying-in-sync">Staying in sync</a>
  ·
  <a href="#documentation">Docs</a>
  ·
  <a href="https://pypi.org/project/wisp-ai/">PyPI</a>
  ·
  <a href="https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/wisp-ai/"><img src="https://img.shields.io/pypi/v/wisp-ai?label=PyPI" alt="PyPI version" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12+" /></a>
  <a href="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml"><img src="https://github.com/whanyu1212/Wisp/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://github.com/whanyu1212/Wisp/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License" /></a>
</p>

> **Release candidate status:** Wisp is approaching its first stable release. Interfaces may still
> change while the `0.1.0` candidate is validated.

## What is Wisp?

Most coding agents ask you to choose between watching and working: you either sit and wait, or you
walk away and audit a diff afterwards. Wisp is built for the middle — you can redirect it mid-run,
you approve anything that touches your machine, and every action it takes is a typed event on an
inspectable transcript.

Underneath is a single event-driven runtime. The CLI, the fullscreen TUI, the JSONL RPC process, and
the in-process SDK all drive the same command host and agent loop rather than reimplementing it.
They share session, tool, approval, and cancellation semantics, while each frontend exposes the
controls its input model supports: RPC and SDK clients have live steering and queue APIs, the TUI
accepts interactive follow-ups, and print/JSON modes run one prompt without a mid-run input channel.

## Install

Wisp is published on PyPI as `wisp-ai`, installs a `wisp` command, and requires Python 3.12+. Linux
and macOS are supported; Windows is best-effort until it has dedicated CI coverage.

```bash
uv tool install "wisp-ai==0.1.0rc3"   # request the prerelease explicitly
wisp --version
```

To run it without installing: `uvx --from "wisp-ai==0.1.0rc3" wisp`. If `wisp` is not on your
`PATH`, run `uv tool update-shell` once and restart your shell.

See [Installation](https://whanyu1212.github.io/Wisp/guide/installation) for the update policy and
troubleshooting.

## Quickstart

Run Wisp from the project you want it to work on:

```bash
cd path/to/project
wisp
```

Wisp defaults to OpenAI Codex subscription access. Type `/connect` to open the provider panel, pick
**OpenAI → ChatGPT Plus/Pro**, and complete the device-code flow. The same panel accepts masked API
keys for OpenAI, xAI, DeepSeek, Anthropic, and Google. Then ask for something:

```text
explain the architecture of this repository
```

For one-shot prompts and scripts, use print mode — or run entirely offline to try it without
credentials:

```bash
wisp -p "summarize the current changes"
wisp -p "hello" --provider fake
```

## Staying in sync

This is the part worth knowing before anything else.

**Steer without starting over.** RPC and SDK clients can queue a course correction for the active
run. Wisp injects it at the next safe request boundary without discarding completed tool work or
rewriting the transcript. A follow-up waits until the run would otherwise finish. The TUI queues
text entered during a run as follow-up work; print and JSON invocations are intentionally one-shot.

**Cancel cleanly.** Cancellation is cooperative and leaves the session resumable rather than
half-written. In-flight tool work is unwound, the JSONL record stays valid, and `--continue` picks
up from the last committed state.

**Approve what matters.** Tools are classified `read`, `mutating`, or `command`. Reads run
directly; writes, edits, and shell commands stop and ask. The decision lives outside the model's
reach — no prompt can talk Wisp into skipping it — and print mode blocks unsafe execution entirely
unless you pass `--yes`.

**Nothing happens off-screen.** Every action is a typed `WispEvent` in an enforced order, persisted
to an append-only JSONL session you can read, resume, branch, or audit long after the run.

→ [How it stays in sync](https://whanyu1212.github.io/Wisp/guide/staying-in-sync)

## Interfaces

| Mode | Command | Output | Best for |
|------|---------|--------|----------|
| **TUI** | `wisp` (or `wisp tui`) | Fullscreen Textual UI | Interactive development |
| **Print** | `wisp -p "…"` | Assistant text on stdout, events on stderr | One-shot prompts and scripts |
| **JSON** | `wisp -p "…" --mode json` | One `WispEvent` JSON object per line | Machine-readable automation |
| **RPC** | `wisp --mode rpc` | Typed JSONL commands and events | Long-lived integrations |

RPC mode and the in-process SDK expose the same command, event, session, trust, and approval
contracts the built-in interfaces use. See
[Interfaces](https://whanyu1212.github.io/Wisp/guide/interfaces).

## Architecture

One event-driven runtime, shared by every interface:

```text
CLI / JSONL-RPC / SDK adapters → RPC command host → CodingSession → AgentHarness → run_agent_loop
```

Each layer adds exactly one concern. The provider/tool cycle knows nothing about sessions or
frontends; the harness owns in-memory conversation state; the coding session adds persistence and
safety policy; interfaces consume typed events. The TUI is an RPC client, not a second agent loop —
which is why it cannot drift from the guarantees above.

→ [Architecture](https://whanyu1212.github.io/Wisp/architecture/)

## Documentation

| | |
|---|---|
| [Guide](https://whanyu1212.github.io/Wisp/guide/) | Installation, quickstart, providers, tools, sessions, skills, TUI |
| [Staying in sync](https://whanyu1212.github.io/Wisp/guide/staying-in-sync) | Steering, cancellation, approvals, transcripts |
| [Reference](https://whanyu1212.github.io/Wisp/reference/) | CLI flags, configuration, and environment variables |
| [Architecture](https://whanyu1212.github.io/Wisp/architecture/) | Runtime layers and ownership boundaries |
| [Contributing](https://whanyu1212.github.io/Wisp/contributing/) | Development setup and testing |

## Contributing

```bash
uv sync                                                              # install
uv run ruff format --check . && uv run ruff check . && uv run mypy   # quality gates
uv run pytest tests                                                  # complete suite
```

The suite runs entirely against deterministic fake and scripted providers, so the agent core, CLI,
and JSONL sessions are exercised without API keys or network calls. Run the complete command before
considering a change verified, and preserve the layer boundaries described above.

See [Contributing](https://whanyu1212.github.io/Wisp/contributing/) for development setup and CI
policy. Issues and pull requests are welcome at
[github.com/whanyu1212/Wisp/issues](https://github.com/whanyu1212/Wisp/issues).

## License

MIT — see [LICENSE](https://github.com/whanyu1212/Wisp/blob/main/LICENSE).
