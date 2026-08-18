---
title: CLI
---

# CLI

The `wisp` executable selects an interface from its arguments and terminal state. Run
`wisp --help` or any command with `--help` for the installed version's generated help.

## Invocation modes

| Command | Behavior |
|---|---|
| `wisp` | Launch the Textual TUI when stdin and stdout are interactive |
| `wisp tui` | Launch the dedicated Textual TUI command |
| `wisp tui --line` | Launch the simple line renderer |
| `wisp -p "PROMPT"` | Run one prompt and print assistant text |
| `wisp -p "PROMPT" --mode json` | Emit one typed `WispEvent` JSON object per line |
| `wisp --mode rpc` | Start the long-lived JSONL RPC command host |
| `wisp --mode tui --tui-renderer RENDERER` | Compatibility TUI entry point (`line`, `fullscreen`, or `textual`) |

A prompt is invalid with `--mode rpc` or `--mode tui`. A non-interactive invocation with neither a
prompt nor an explicit RPC/TUI mode prints help and exits.

## Top-level options

These options configure bare TUI, print, JSON, RPC, and compatibility TUI invocations. Options for
the dedicated `wisp tui` command are listed separately below.

| Option | Meaning | Environment equivalent |
|---|---|---|
| `--version` | Print `wisp VERSION` and exit | — |
| `-p`, `--prompt TEXT` | Run one prompt and exit | — |
| `--provider NAME` | Select a provider such as `openai-codex`, `deepseek`, `anthropic`, or `fake` | `WISP_PROVIDER` |
| `--model NAME` | Override the selected provider's model | `WISP_MODEL` |
| `--session-dir PATH` | Store and resolve JSONL sessions in this directory | `WISP_SESSION_DIR` |
| `--auth-file PATH` | Use this private provider credential file | `WISP_AUTH_FILE` |
| `--mode text\|json\|rpc\|tui` | Select the output/interface mode | `WISP_MODE` (only without `--prompt`) |
| `--tui-renderer line\|fullscreen\|textual` | Renderer for `--mode tui` | `WISP_TUI_RENDERER` |
| `--all-tools`, `--no-all-tools` | Expose or withhold the full tool registry; TUI modes default on, other modes off | — |
| `--allow-read-tools`, `--no-allow-read-tools` | Expose sandboxed read-only tools | — |
| `--allow-tool NAME` | Expose one named tool; repeat for multiple tools | — |
| `--resume SESSION` | Continue by JSONL path, filename, session id, or unique id prefix | — |
| `--continue` | Continue the newest session in the selected session directory | — |
| `--yes`, `--allow-unsafe-tool-execution` | Pre-approve mutating and command tools | — |
| `--max-tool-iterations N` | Cap model/tool rounds; omitted means uncapped | — |
| `--help` | Show generated help and exit | — |

`WISP_MODE` supplies a default only when the invocation has neither an explicit `--mode` nor
`-p`/`--prompt`. Prompt invocations keep text mode unless `--mode json` is passed explicitly; for
example, `WISP_MODE=json wisp -p "hello"` does not select JSON output.

Explicit command-line values override their environment and settings-file equivalents. `--resume`
and `--continue` are mutually exclusive, and `--max-tool-iterations` must be zero or greater.

Tool exposure and tool approval are separate. Exposing a mutating or command tool does not approve
it; without `--yes`, Wisp asks in interactive modes and blocks unsafe execution in non-interactive
modes. See [Tools & safety](../guide/tools-and-safety).

## `wisp tui`

`wisp tui` defaults to the Textual renderer and the full tool registry.

| Option | Meaning |
|---|---|
| `--line` | Use the simple line renderer instead of Textual |
| `--session-dir PATH` | Override the JSONL session directory |
| `--auth-file PATH` | Override the provider auth file |
| `--all-tools`, `--no-all-tools` | Expose or withhold the full tool registry |
| `--allow-read-tools`, `--no-allow-read-tools` | Expose sandboxed read-only tools |
| `--allow-tool NAME` | Expose one named tool; repeatable |
| `--resume SESSION` | Continue a selected session |
| `--continue` | Continue the newest session |
| `--yes`, `--allow-unsafe-tool-execution` | Pre-approve mutating and command tools |
| `--max-tool-iterations N` | Cap model/tool rounds |

Provider and model defaults for the dedicated command come from configuration and
`WISP_PROVIDER`/`WISP_MODEL`. Use the compatibility `--mode tui` form when you need top-level
`--provider` or `--model` flags.

## Maintenance and inspection commands

### Updates

```text
wisp update [--check] [--yes|-y]
```

Without `--check`, Wisp offers to install the latest compatible release. `--check` reports status
without installing; `--yes` accepts installation without confirmation. Installation is supported
only for persistent `uv tool` installs. Check and installation failures exit with status 1.

### Credentials

```text
wisp auth status [PROVIDER] [--auth-file PATH]
wisp auth logout PROVIDER [--auth-file PATH]
```

`status` never prints secrets. `logout` removes the selected stored credential.

### Project trust

```text
wisp trust status [PROJECT]
wisp trust allow [PROJECT]
wisp trust revoke [PROJECT]
wisp trust forget [PROJECT]
```

The project defaults to the current directory. `allow` and `revoke` persist a decision; `forget`
returns it to undecided. `WISP_TRUST=1` or `0` overrides the decision for one process without
persisting it.

### Skills

```text
wisp skills [PROJECT]
```

Lists valid Agent Skills and isolated discovery diagnostics. Project skills are skipped unless the
project is trusted; bundled and user skills remain discoverable.

## Output streams

| Mode | stdout | stderr |
|---|---|---|
| Text/print | Final assistant text | Lifecycle events, trust prompts, diagnostics, and errors |
| JSON | One `WispEvent` JSON object per line | Trust prompts and process-level diagnostics |
| RPC | JSONL commands are read from stdin; typed events and command results are written to stdout | Process-level diagnostics |
| TUI | Terminal UI | Startup failures before the UI takes control |

Do not parse text-mode stderr as a stable protocol. Use JSON mode, RPC, or the SDK for typed
integration contracts.

## Exit status

| Status | Meaning |
|---|---|
| `0` | Normal completion, help/version output, user-declined update, or graceful TUI/RPC shutdown |
| `1` | Configuration, provider, session, tool, update, or runtime failure |
| `2` | Command-line syntax or type error reported by Typer/Click |

External termination may produce a shell-specific signal status; that is not a versioned Wisp exit
code. JSON mode emits an `error` event before status 1 when its typed output contract can still be
honored.
