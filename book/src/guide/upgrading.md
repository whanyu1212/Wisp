# Upgrading from Wisp 0.1 to 0.2

Wisp 0.2 is a minor release with announced breaking changes. What you need to do depends on how you
use Wisp:

- **At the terminal**, the interactive interface is now a Rust TUI that ships in the native wheels.
- **From Python**, a few deprecated names are gone and some internal modules moved.
- **Over JSONL-RPC**, clients must handshake first and connect only to a backend from the same
  release.

The [changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) lists every change,
release candidate by release candidate.

## Upgrade

Back up `~/.wisp/sessions` (or your configured session directory), then install 0.2:

```bash
uv tool install --force --python 3.12 wisp-ai
wisp --version       # wisp 0.2.3
wisp-tui --version   # wisp-tui 0.2.3, on platforms with a native wheel
```

Keep `--python 3.12` (or any newer Python). Without it, uv resolves for its default interpreter
first; if that is older than 3.12, uv selects the pure wheel and the install has no Rust TUI. Use
this command rather than `wisp update` from 0.1.0, which predates the native wheels. See
[Installation](./installation#the-tui-does-not-start-after-installing) if the TUI still doesn't
start.

## What changes for terminal users

`wisp`, `wisp tui`, and `wisp --mode tui` launch the Rust TUI. Native wheels for macOS arm64 and
Linux glibc 2.28+ x86_64 include it. Every other platform, including Intel macOS, gets a pure wheel:
print, JSON, RPC, and SDK use work as before, but interactive commands stop with instructions for
getting a native wheel or building a matching binary. For a source checkout, build the binary and
set its absolute path in `WISP_RUST_TUI_BINARY`; see
[Development setup](../contributing/development#rust-tui-scaffold).

The Python terminal renderers are gone, along with the options that chose between them: the
`textual`, `fullscreen`, and `line` renderers, `--line`, `--tui-renderer`, and
`wisp tui --renderer`. Python still runs the agent, providers, tools, permissions, and saved
sessions; the [TUI guide](./tui) covers the new interface.

Your 0.1 settings, credentials, and trust decisions carry over. Two things need attention:

- A `~/.wisp/catalog.toml` overlay must declare `schema_version = 2`.
- 0.2 never saves trust or YOLO for your home folder or a folder above it. A decision 0.1 saved for
  such a folder is ignored but kept; see [Tools & safety](./tools-and-safety#home-folder-and-broad-folders).

0.2 opens sessions saved by 0.1.0, since both write the same session format (entry schema v6).
Session files from releases before 0.1.0 that use an older format no longer load; 0.2 rejects them
rather than converting them.

0.2 also adds MCP server management from `/mcp`, `wisp mcp`, or by asking Wisp (see
[Tools & safety](./tools-and-safety#mcp-tools)), and the providers' own web search, which `/search`
toggles in the TUI.

## Python integrations

This is a minor release with an announced breaking API cleanup, not a patch release.

- The deprecated `wisp.agent.messages.SessionEntry(...)` factory has been removed as an explicit
  exception to the normal deprecation window. Use `MessageSessionEntry`, `EventSessionEntry`, or
  `CompactionSessionEntry` from `wisp.sessions`.
- For event entries, pass `PersistedEventEnvelope(payload=raw_event)` rather than a raw dictionary
  as the `event` value. This changes Python construction, not existing JSONL files.
- Code importing agent internals must update removed module paths. Use `wisp.agent.harness` and
  `wisp.agent.loop` as package entry points.
  Prompt building moved from `wisp.agent.prompt` to `wisp.coding.prompt` with no compatibility
  re-export, so update imports such as `build_prompt_messages` and `resolve_project_context_root`.
  `wisp.agent.mode` is also gone: import `AgentMode` and `DEFAULT_AGENT_MODE` from `wisp.events`,
  and `PLAN_MODE_SYSTEM_PROMPT` from `wisp.coding.prompt`; `is_agent_mode` is removed.
  Message records moved from `wisp.agent.messages` to `wisp.messages` (for example
  `from wisp.messages import Message`), and history helpers from `wisp.agent.history` to
  `wisp.messages.history`, again with no re-export.
- The SDK rejects a backend from a different Wisp release when it connects.
- Prefer the documented [Python SDK import surface](../reference/sdk) when embedding Wisp.
  See [Compatibility & versioning](../reference/compatibility) for the public API boundary and
  the early-removal exception.

## External JSONL-RPC clients

Update external clients together with the backend:

1. Send `rpc.handshake.request` as the first frame, before ordinary commands.
2. Connect only to a backend from the same Wisp release. Events carry no schema version and there
   is no cross-release event contract. Wait for `rpc.handshake.accepted` before sending
   commands; handle rejection as a connection failure rather than attempting legacy fallback.
3. Honor negotiated directional frame limits and strict UTF-8, LF-terminated JSON framing.
4. Use backend-owned model and connection catalogs. Credential mutations belong to backend RPC;
   frontends must not read or write Wisp credential files themselves.

Use the checked-in `schemas/live-rpc/` bundle and the typed Python transport as implementation
references. The schema bundle is a release asset, not part of the Python wheel API.

The in-process Python SDK has no serialization boundary and does not perform a wire handshake.
The Rust TUI requires the exact Python package release. Source builds use the matching
checkout; native wheels are built and published in lockstep with the Python release.

## Going back to 0.1.0

Exit Wisp, back up your sessions, and prepare your settings first:

- **Hosted MCP servers.** 0.1.0 ignores the whole `~/.wisp/settings.json`, with a warning, if it
  lists an MCP server that uses `url`. Copy those entries somewhere safe and remove them.
- **Home-folder trust.** 0.1.0 applies trust or YOLO that it saved for your home folder again.
  While still on 0.2, run `wisp trust forget ~`, and choose Ask in `/permissions` from your home
  folder, to remove those records.

Then install the exact earlier version:

```bash
uv tool install --force --python 3.12 "wisp-ai==0.1.0"
wisp --version
```

0.1.0 can open a session 0.2 wrote only if it holds no record type added during 0.2. It reports a
session as malformed if it contains a response from a provider that sends a prompt-cache key (such
as Codex or OpenAI), a web search result, or the Git status snapshot a trusted project saves. Start
new sessions after going back, and never delete or hand-edit session files as a rollback step.
