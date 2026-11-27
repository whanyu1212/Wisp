---
title: Environment variables
description: Runtime, provider credential, path, and terminal environment variables recognized by Wisp.
route: reference/environment.html
section: reference
order: 3
layout: docs
listed: true
searchable: true
---

# Environment variables

| Variable | Purpose |
|----------|---------|
| `WISP_PROVIDER` | Provider name: `openai-codex`, `openai`, `xai`, `deepseek`, `openai-compatible`, `anthropic`, `google`, or `fake` |
| `WISP_MODEL` | Model override; blank uses the provider default |
| `WISP_MODE` | Default mode for invocations without `--prompt`; prompt runs require explicit `--mode` |
| `WISP_RUST_TUI_BINARY` | Absolute executable path to a source-built `wisp-tui`; used when launching the TUI |
| `WISP_TUI_MOUSE` | TUI mouse navigation and selection; enabled when unset or set to `1`, `true`, or `on` (case-insensitive, surrounding whitespace ignored); any other value disables it |
| `WISP_TUI_COLORS` | Override TUI color depth: `auto` (default), `truecolor`, `256`, or `16`; other values are ignored |
| `WISP_TUI_IMAGES` | Override terminal image protocol: `auto` (default), `kitty-unicode`, `kitty`, `iterm2`, `sixel`, or `halfblock`; overrides `tui_images` |
| `WISP_TUI_MOTION` | Live indicator/glow/startup-logo motion; set to `off`, `0`, or `false` to disable |
| `WISP_TUI_NOTIFY` | Background-terminal attention alerts: OSC 9 desktop notifications by default, `bell` for terminal bell, or `off` |
| `WISP_TUI_STREAM_PACING` | Word-paced assistant text reveal; set to `0` to render streamed deltas immediately |
| `WISP_BROWSER_DEFAULT` | Preferred browser when both Wisp's live browser and a browser MCP server are available: `live` (default), `mcp`, or `none`; overrides `browser_default` |
| `NO_COLOR` | Deterministic Rec.709 grayscale TUI rendering with a 4.5:1 contrast floor and reverse-video selections |
| `WISP_SESSION_DIR` | Session storage directory; defaults to `~/.wisp/sessions` |
| `WISP_AUTH_FILE` | Auth file path; defaults to `~/.wisp/auth.json` |
| `WISP_OPENAI_COMPATIBLE_CONFIG` | JSON object configuring one OpenAI-compatible endpoint; overrides the user-settings `openai_compatible` object |
| `WISP_TRUST` | Trust the current project for one process: `1` to opt in, `0` to force untrusted |
| `WISP_TRUST_FILE` | Relocate the global trust store; must be absolute, but is otherwise accepted as supplied |
| `WISP_EFFORT` | Reasoning effort override |
| `WISP_RETRY_MAX_RETRIES` | Provider retry count; defaults to `2`, set `0` to disable |
| `WISP_RETRY_BASE_DELAY_SECONDS` | Initial retry delay; defaults to `0.5` |
| `WISP_RETRY_MAX_DELAY_SECONDS` | Maximum retry delay; defaults to `30` |
| `WISP_CODEX_READ_TIMEOUT_SECONDS` | Read-idle timeout for Wisp-owned `openai-codex` connections; defaults to `300`; positive finite seconds, e.g. `600` |
| `WISP_CONTEXT_RESERVE_TOKENS` | Minimum tokens reserved outside estimated input context; defaults to `16384` |
| `WISP_AUTO_COMPACTION` | Automatic threshold compaction and overflow recovery; defaults to `true` |
| `WISP_WAKE_ON_PROCESS_EXIT` | Start a run when a background process started with `notify_on_exit` stops while the session is idle, at most three times between prompts; defaults to `true` |
| `WISP_WEB_SEARCH` | Offer the provider's built-in [web search](../guide/providers.md#web-search) to the model; defaults to `false` |
| `WISP_MCP_TOOL_EXPOSURE` | MCP discovery mode: `auto` (default), `direct`, or `on_demand`; see [MCP discovery](../guide/mcp.md#finding-mcp-tools-on-demand) |
| `WISP_CODEMODE_PRESENTATION` | `direct_and_codemode` (default) or `codemode_only`; controls model-facing schemas when codemode is offered, independently of `WISP_CODEMODE` |
| `WISP_CODEMODE` | Offer experimental [codemode](../guide/mcp.md#mcp-with-codemode) whenever eligible (`1`) or never (`0`); unset, automatic mode requires an eligible MCP tool, including command and mutating tools. `1` also enables local-only workflows. Requires the extra; each child follows normal tool policy and approvals |
| `WISP_SUBAGENTS` | Offer the experimental [`spawn_agent`](../guide/tools-and-safety.md#delegating-to-a-subagent-experimental) tool, which hands a read-only task to a subagent; defaults to `false` |
| `WISP_SUBAGENT_MODEL` | Model a [subagent](../guide/tools-and-safety.md#delegating-to-a-subagent-experimental) that starts fresh uses, on the session's provider; unset, subagents use the session's model |

For terminal-native selection instead of Wisp's mouse handling, launch with:

```bash
WISP_TUI_MOUSE=0 wisp tui
```

See [Mouse navigation and scrollback](../guide/tui/sessions.md#mouse-navigation-and-scrollback)
for supported interactions and terminal-specific overrides. To customize keyboard controls, use
[`tui_keybindings`](configuration.md#structured-user-settings), not an environment variable.

## Provider credentials

| Variable | Provider |
|---|---|
| `OPENAI_API_KEY` | `openai` |
| `XAI_API_KEY` | `xai` |
| `DEEPSEEK_API_KEY` | `deepseek` |
| `ANTHROPIC_API_KEY` | `anthropic` |
| `GOOGLE_API_KEY` · `GEMINI_API_KEY` | `google` |
| `<CUSTOM_PROVIDER>_API_KEY` | A custom OpenAI-compatible provider; hyphens become underscores |
| `OPENAI_COMPATIBLE_API_KEY` | Fallback for custom OpenAI-compatible providers |

Each is required only for the matching provider. See [Providers & auth](../guide/providers.md) for
storage and precedence details.

`WISP_OPENAI_COMPATIBLE_CONFIG` accepts `provider_name`, `base_url`, `default_model`, optional
`requires_api_key`, and optional absolute `ca_bundle` fields. The value must be a JSON object; invalid
JSON or unknown fields fail configuration instead of being ignored. It overrides the structured
endpoint in `~/.wisp/settings.json`, while an explicit SDK configuration value overrides the
environment.

`WISP_TRUST` and `WISP_TRUST_FILE` are read only from the real process environment, never from
project files, and `WISP_TRUST` is never persisted — see
[Tools & safety](../guide/tools-and-safety.md#project-trust).

`WISP_MODE` applies only when neither a mode nor a prompt is supplied on the command line. For
example, `WISP_MODE=json wisp -p "hello"` still uses text output; write
`wisp -p "hello" --mode json` for a machine-readable prompt run.

Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the Rust TUI. Pure-wheel installs
retain print, JSON, RPC, and SDK use; an interactive TUI command instead reports how to obtain a
native wheel or build a matching Rust binary from source. There is no Python terminal renderer.

`WISP_RUST_TUI_BINARY` is a source-development override, not a general executable search path. It
must name an existing, executable absolute path and is removed from the environment passed to the
Python RPC backend. Without an override, native-wheel installations resolve `wisp-tui` only
from the active Python environment's scripts directory; Wisp never searches `PATH`. See
[Development setup](../contributing/development.md#rust-tui-scaffold).

## Terminal UI rendering, compatibility, and limits

The Rust TUI stores local presentation preferences (`theme`, `last_dark_theme`, `startup_logo`, and
`verbosity`) atomically in `~/.wisp/tui.json`, preserving unrelated keys. Missing or invalid
preferences fall back to `Vapor`, `Random`, and `medium`; unreadable, non-UTF-8, non-regular, or
over-64-KiB files are left untouched with a warning. Presentation preferences never enter
`settings.json`, RPC, or session history.

### Layout, paste, and transport thresholds

| Surface | Limit or threshold |
|---|---|
| Minimum usable terminal size | `30×9` columns × rows (`30×8` for the model picker); below `11` rows, docked approval/trust panels fill the screen |
| Assistant prose measure | `100` columns; code blocks and tables use the available transcript width |
| Side panel auto-dock | Docks when terminal width is at least `148` columns (`44`-column base width, auto-growing to a `76`-column cap while keeping at least `104` columns for the transcript); narrower terminals use a `44`-column overlay |
| Side panel manual drag | Dragged left border can widen the docked panel up to `60%` of terminal width while leaving at least `60` columns for the transcript; double-click resets auto width (`400 ms`) |
| Side panel swimlane waterfall | Horizontal Gantt waterfall when the panel is at least `72` columns wide (`f` expands into a centered overlay); narrower widths show compact duration bars |
| Two-row telemetry deck footer | Renders two rows when the conversation column is at least `60` columns wide and `16` rows tall; collapses to one row below that |
| Centered popups & file picker | Popups cap at `100×28`; composer file picker caps at `100×12`, `30` fuzzy matches, and `4096`-byte queries |
| Composer drafts & folded pastes | Pastes over `2,000` Unicode characters fold into numbered markers (up to `64` folds retained); raw drafts cap at `1 MiB` and `10,000` lines; compact live transcript/queue paste echoes cap at `32` entries / `4 MiB` each |
| Stream pacing & backpressure | Word-paced reveal flushes immediately if more than `16 KiB` waits; inbound burst queue waits up to `5 s` while keeping input and redraws responsive |

### Terminal-specific color, motion, and notification behavior

- **Color depth & `tmux`:** Themes are authored in 24-bit RGB and converted down to 256 colors on
  macOS Terminal.app (unless `COLORTERM` advertises truecolor) or 16 ANSI colors on 8/16-color
  terminals, keeping foreground contrast at or above `4.5:1` and separating background tiers. At 16
  colors, selections use reverse video, diffs use high-contrast green/red with bold+underlined
  changed words, and pickers keep their cursor arrow. Inside `tmux`, Wisp emits 24-bit color by
  default; if the outer terminal lacks truecolor, set `WISP_TUI_COLORS=256` so Wisp runs its
  contrast-preserving conversion.
- **`NO_COLOR`:** Converts the active theme to Rec.709 grayscale with a `4.5:1` contrast floor,
  reverse-video selections, and monochrome diffs while keeping shape-coded markers and allowing
  theme changes to be saved.
- **Notifications & terminal title:** While unfocused, Wisp sends OSC 9 desktop notifications
  (supported by iTerm2, Ghostty, WezTerm, etc.) on run completion, failure, approval/trust waits,
  and autonomous background-process exits. In `tmux`, enable `focus-events` and allow OSC 9
  passthrough, or set `WISP_TUI_NOTIFY=bell` (also useful for macOS Terminal.app). Terminals
  without a title stack (including `tmux`) retain Wisp's last title until a shell prompt resets it.
- **Motion, links, and cursors:** Breathing indicators, finished-card glows, and the ~2 s startup
  logo animation pause automatically in the background, under `NO_COLOR`, or with
  `WISP_TUI_MOTION=off` (at 256/16 colors or transparent themes, indicators step between bold and
  dim without glow). Clickable `http`/`https` links and file paths use OSC 8 hyperlinks and turn
  off under `TERM=dumb` or `linux`. The side-panel border shows a resize pointer in kitty, Ghostty,
  foot, and Rex (not inside `tmux`).

## Internal development controls

The following names are implementation details for Wisp's launcher, tests, or benchmarks, not
supported user configuration. They may change without compatibility guarantees; leave them unset
for normal use.

| Name | Internal purpose |
|---|---|
| `WISP_RUST_TUI_BINDINGS_JSON` | Launcher-to-frontend handoff of resolved keybindings. The Python launcher overwrites it from user settings; configure `tui_keybindings` instead. |
| `WISP_TUI_STREAM_TRACE` | Diagnostic stream-timing log path; each streamed text delta appends a tab-separated line of milliseconds since first delta, milliseconds since previous delta, and byte length. |
| `WISP_VISUAL_OUTPUT` | Output directory for the explicitly requested, ignored Rust visual-capture test; not a runtime rendering option. |
| `WISP_HYDRATION_PROFILE_DIR` | Opt-in Python/Rust saved-history benchmark timings, appended as per-process JSONL files in an existing directory. |
| `WISP_FAKE_*` | Fake-provider test controls: `WISP_FAKE_STREAM_INTERVAL_MS`, `WISP_FAKE_RESPONSE_WORDS`, `WISP_FAKE_RESPONSE_PREFIX`, and `WISP_FAKE_RESPONSE_SUFFIX`. These tune synthetic responses, not real providers. |

The historical `WISP_BRAILLE_*` controls are no longer read from the environment. The current
`WISP_BRAILLE_MINI`, `WISP_BRAILLE_NORMAL`, and `WISP_BRAILLE_FULL` names are Rust logo constants,
not environment variables.
