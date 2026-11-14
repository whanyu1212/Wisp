---
title: Troubleshooting & FAQ
description: Recover from installation, provider, MCP, trust, and terminal problems, and find session diagnostics safely.
route: guide/troubleshooting.html
section: using
order: 12
layout: docs
listed: true
searchable: true
---

# Troubleshooting & FAQ

Start with the symptom below. Before sharing diagnostics, remove credentials, private code, and
personal paths: session files and command output can contain sensitive data.

## Wisp is installed, but the TUI will not start

If your shell cannot find `wisp`, run `uv tool update-shell` and restart the shell. If
`wisp --version` works but interactive startup reports that no Rust TUI binary is available, you
may have installed the pure wheel, which supports print, JSON, RPC, and SDK use but has no TUI.

Native wheels bundle the TUI only for macOS arm64 and Linux glibc 2.28+ x86_64. Even on those
platforms, uv can choose the pure wheel when its default Python is older than Wisp's 3.11 minimum.
Reinstall with an explicit interpreter:

```bash
uv tool install --force --python 3.12 wisp-ai
wisp --version
wisp-tui --version
```

On other platforms, changing Python does not supply a native wheel. See
[Installation](installation.md#the-tui-does-not-start-after-installing) for the wheel-selection
explanation and [Development setup](../contributing/development.md#rust-tui-scaffold) for building
a matching Rust binary. A frontend/backend version mismatch also requires a matching release;
do not point `WISP_RUST_TUI_BINARY` at an older build.

To check the Python installation without credentials, a network model call, or the TUI:

```bash
wisp -p "hello" --provider fake
```

## A provider reports missing or invalid credentials

Check which provider you selected. The default `openai-codex` uses ChatGPT subscription sign-in;
an `OPENAI_API_KEY` configures the separate `openai` API provider, not that subscription connection.
In the TUI, open `/connect` and select the intended connection method. For one-shot runs, configure
the matching provider's environment variable or stored credential before retrying.

Environment API keys take precedence over stored keys. If updating a key through `/connect` seems
to do nothing, check whether a stale key is still exported in the shell launching Wisp; do not
print or paste its value. A provider rejection can also reflect account/model access rather than
a missing key. See [Providers & auth](providers.md) for the credential table and
[credential storage](providers.md#credential-storage) for precedence and auth-file locations.

Never put a key in a prompt. Before connecting in a trusted repository, inspect its
`.wisp/settings.json`: `auth_path` may redirect stored credentials into the repository. Use
`--auth-file` or `WISP_AUTH_FILE` for a deliberate override, and never commit the selected file.

## A prompt failed or the connection dropped: can I continue?

Fix the reported credential, network, or provider problem first. In the TUI, send a new prompt in
the same session. After exiting, resume the newest session or select the failed session explicitly:

```bash
wisp --continue
wisp --resume <session-id-prefix>
```

`--continue` selects the newest session in the active session directory, which may not be the
failed one if you have started another. Use `/resume` in the TUI to find the intended session.
If startup failed before a session was saved, rerun the original command after fixing the cause.

Wisp retries eligible failures only before streaming starts. An interrupted stream is not
automatically replayed; partial replies and completed tool results are retained. Review those
results and any changed files before requesting a repeat: resuming does not guarantee that the
model will never request the same action again. See [Retry behavior](providers.md#retry-behavior)
for retry limits and the configurable Codex read-idle timeout.

## An MCP server will not start or authenticate

Open `/mcp` to inspect the selected server's status, sanitized startup error, and registered tools.
Then check the relevant cause:

- **Local stdio server:** confirm its executable and arguments are correct and installed, and that
  variables named by `env_from` are present in the environment that launched Wisp. Review the
  command before running it yourself; starting a server executes code as your user.
- **HTTP server:** use its final Streamable HTTP endpoint. Wisp does not follow redirects or
  support legacy HTTP+SSE. If using `headers_from`, its configured environment variable must be
  available in Wisp's process; unset or invalid values cause the server to be skipped.
- **Sign-in required:** select the HTTP server and press `l`, or run `wisp mcp login NAME`. Complete
  OAuth in a browser on the same machine as Wisp so it can return to the local callback address.
  Provider `/connect` credentials do not sign you into an MCP server.
- **Changed settings:** press `c` in `/mcp` to reload settings and reconnect. `r` refreshes status
  only. CLI `wisp mcp` changes apply at the next launch or reconnect.

See [MCP tools](tools-and-safety.md#mcp-tools) for supported transports, configuration examples,
authentication, and limits, and [Managing MCP servers](tui/commands.md#managing-mcp-servers) for
panel controls. Do not share settings containing literal tokens or authentication headers.

## Why will trust or YOLO not stay enabled in my home folder?

Wisp deliberately does not save trust or YOLO for your home folder, its ancestors, filesystem or
drive roots, or the system temporary folder. A decision there could otherwise affect unrelated
projects. Wisp still runs there, but trust is asked for each session and YOLO lasts only for that
session; `wisp trust allow` and `revoke` refuse broad folders.

Work from the specific project directory instead, and inspect it before granting trust. Trust
controls project-local configuration and instructions; it is not permission to run every tool.
See [Project trust](tools-and-safety.md#project-trust) and
[Home folder and broad folders](tools-and-safety.md#home-folder-and-broad-folders).

## Scrolling or text selection behaves differently in my terminal

Wisp captures mouse input by default. Wheel or trackpad input scrolls the conversation; dragging
transcript text copies it when you release. To use your terminal's native selection instead:

```bash
WISP_TUI_MOUSE=0 wisp tui
```

Keyboard transcript controls still work: `PgUp`/`PgDn` scroll pages and `Ctrl+End` returns to the
live tail. Some terminals provide a mouse-capture bypass modifier, such as `Shift`-drag in Ghostty.
Clipboard forwarding over SSH uses OSC 52; inside tmux it needs `set-clipboard on`. Terminal support
and policy can affect clipboard delivery.

In a long session, earlier saved history loads only while Wisp is idle. If you see “Earlier history
loads when this reply finishes”, wait for the reply to end. See
[Mouse navigation and scrollback](tui/sessions.md#mouse-navigation-and-scrollback) for supported
interactions and [Keybinding preferences](tui/keybindings.md#keybinding-preferences) if your terminal
does not send a shortcut distinctly.

## Where are session files and logs?

- **Saved sessions:** `~/.wisp/sessions` by default. `--session-dir`, `WISP_SESSION_DIR`, or the
  `session_dir` setting can change it; use the same directory when resuming. Session JSONL contains
  messages and selected audit events, including errors, but not every live streaming update. See
  [Sessions](sessions.md#what-a-session-file-contains).
- **One-shot diagnostics:** text-mode lifecycle events and errors go to stderr, while assistant
  text goes to stdout. `--mode json` emits structured events on stdout. Wisp does not configure a
  persistent application log file by default; retain relevant terminal output when reporting a
  failure. See [Interfaces](interfaces.md).
- **Truncated shell output:** the tool result includes an `output_id` when full output was saved.
  Ask Wisp to inspect it with `read_output` before exiting. These bounded artifacts live under
  `~/.wisp/tool-output/`, last only while Wisp runs, and are not restored with a saved session.
  See [Saved command output](tools-and-safety.md#output-a-result-cuts-is-saved).

For a useful bug report, include `wisp --version`, your OS/architecture and terminal, the exact
error, and minimal reproduction steps. Share only the relevant redacted excerpts, not your entire
session, auth file, or settings file.
