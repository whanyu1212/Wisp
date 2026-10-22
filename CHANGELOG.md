# Changelog

Notable changes to Wisp, with an emphasis on the **event contract** that JSON, RPC, and SDK
consumers integrate against.

`WispEvent` payloads carry no per-event version, and a backend and frontend must be the same Wisp
release. `schemas/live-rpc/` describes the current release's wire contract; the typed models live in
the `src/wisp/events/` package. Event changes are recorded under the release that makes them.

The `## Live RPC protocol vN` and `## Schema vN` headings below are historical records from before
this policy.

## 0.3.0 — Unreleased

0.3.0 is a feature release centered on reading, reviewing, and following work in the terminal.
Native platforms remain macOS arm64 and Linux glibc 2.28+ x86_64, with a pure wheel for other
platforms. Python 3.12+ remains required. The Python backend and Rust frontend must be upgraded
together to exactly the same release.

### Terminal UI

- A docked, resizable sidebar on wide terminals and a Ctrl+B overlay on smaller terminals show
  the workspace, context budget, recent turns, changed files, and visible MCP/skill inventories.
  Context details remain expandable; running and approval states have dedicated presentations.
  (#773, #776, #778, #779, #786, #790, #791, #797)
- `/diff` reviews the working tree against HEAD, including staged and unstaged tracked changes,
  with untracked files listed separately. It identifies files edited by Wisp in the session.
  Retained edit/write previews use a full-width unified layout, a compact gutter, and explicit
  omission counts; the review overlay keeps its unified/split controls. (#769, #789, #795, #799)
- Assistant prose has an 88-column reading width, paragraph and list spacing, and aligned wrapped
  list items. The composer, footer, turn rail, and streamed activity have clearer spacing and
  status. Shift+Enter is distinguished from Enter when the terminal supports the kitty keyboard
  protocol. (#771, #774, #775, #777, #781, #782, #798)
- Tool activity shows concise command/MCP subjects, grouped call counts, execution times, and
  named process outcomes. Shell and process output has a readable surface and independently
  expandable earlier lines. Hidden retained output and unavailable output have separate counts;
  previews keep normal contrast without fading. (#770, #772, #787, #788, #792, #795, #800)
- The default agent prompt leads with outcomes, keeps progress updates brief, and groups related
  check results while retaining failures and uncertainty. Requested detail remains available;
  there is no hard response-length limit. (#801)

### RPC, SDK, and context

- Add `get_git_diff`, `RpcController.get_git_diff()`, and the `rpc.git_diff` event for a bounded,
  backend-owned working-tree review. Add `rpc.workspace` for the current folder and branch.
  External clients must use the matching 0.3.0 schema bundle. (#769, #773, #789)
- Session stats expose a source-attributed context breakdown, including messages, tools,
  instructions, per-turn contributions, and the largest items. Estimated splits remain identified
  as estimates; `/context` and the sidebar show the same backend data. (#784, #786)
- Tool and process timing and completed-run totals now survive reopening a session. RPC history
  snapshots expose the corresponding timing and run-completion metadata. (#772, #787, #788)

### Compatibility and rollback

0.3.0 reads 0.2.4 sessions without rewriting them. Entry schema v6, event-envelope v1, and
compaction v4 remain unchanged: the additions are defaulted metadata, not a migration.
**Backward readability is not guaranteed.** 0.2.4 may reject sessions containing the new tool
presentation fields, and its typed event reader rejects new event fields. Back up sessions before
upgrading; use a pre-upgrade backup or new sessions if returning to 0.2.4. Keep sessions written by
0.3.0 for reopening with 0.3.0; do not remove fields from JSONL files to force a downgrade.
Settings, credentials, and trust formats are unchanged. See the
[0.3 upgrade guide](book/src/guide/upgrading-to-0.3.md) for commands and compatibility details.

### Development and documentation

- CI selects checks from the complete change set, runs independent jobs concurrently, and
  retains a required final gate. PTY checks handle incremental repainting and resize boundaries
  more reliably. (#785, #794, #796, #800)
- Astro documentation gains content/navigation foundations, responsive layouts, light/dark/system
  themes, accessible code/diagram presentation, and built-site browser checks. Production docs
  still deploy through mdBook; search and the final cutover are follow-up work. (#780, #783)

## 0.2.4 — 2026-10-20

0.2.4 is a patch release on 0.2.3 with Codex transport diagnostics and terminal UI improvements.
It changes no command, event, handshake model, or session format. Sessions remain compatible;
the backend and frontend must still come from the same release.

### Terminal UI

- Retained edit/write review supports unified and side-by-side layouts, hunk navigation with
  `[` / `]`, edit navigation with Tab / Shift+Tab, and retained-context toggling with `c`.
  Press `s` for split review; narrow terminals use unified review. This is review of one retained
  tool edit, not a cumulative working-tree diff against HEAD. (#767)
- Inline diffs use old/new gutters, syntax colors, stronger changed-word highlights, and coherent
  preview folding. Edit line numbers are fragment-relative. Omission counts include changes
  beyond the retained-detail limit. (#767)
- Routine tool rows use quieter spacing, relative paths, and conservative command summaries.
  Expanded rows retain original arguments and process diagnostics. Process cards use task names
  where available, and the footer is shorter. All nine existing themes have coordinated diff
  colors and softer panels/chips; Paper remains the main light theme. (#767)

### Codex provider

- Transport failures retain safe diagnostics: exception type, timeout phase and configured limit,
  attempt number, and current response ID when available. Session history preserves these
  diagnostics without transport exception text, request URLs, or headers. Started responses are
  not automatically replayed; completed tool results and partial replies remain available when
  resuming the session. (#766)
- Configure read inactivity with the user-only `codex_read_timeout_seconds` setting,
  `WISP_CODEX_READ_TIMEOUT_SECONDS`, or SDK configuration. The default stays at 300 seconds;
  values must be positive and finite. This is an idle timeout, not a total response deadline.
  Caller-injected HTTP clients keep their own timeout policy. (#766)

## 0.2.3 — 2026-10-20

0.2.3 is a patch release on 0.2.2 with two terminal UI changes. It changes no command, event, or
handshake model and no session format, so 0.2.2 and 0.2.3 read each other's sessions; the backend
and frontend must still come from the same release, which the handshake enforces.

### Terminal UI

- Consecutive calls to the same MCP tool collapse into one row with a count, such as
  `linear/get_issue ×8  id=HY-270 … id=HY-278`, showing the first and last call's arguments. While a
  call in the run is open, the row shows that call with `· 7 of 8 done`, and a call awaiting
  approval keeps the `?` marker with `· awaiting approval`. Click, `Right`, or `Enter` expands the
  row as for `explored N files`. Bash, edit, and write cards never group. A blank column now
  separates every card's status marker from its tool chip. (#763)
- Streamed replies appear at an even pace, a word at a time and about a tenth of a second behind the
  stream, instead of in the provider's bursts. An unfinished `**bold**`, `` `code` ``, or
  `~~strike~~` span, table row, or code fence line waits until it is complete, so raw markers do not
  flash. A pause in the stream, the end of the run, or a tool card or prompt that follows shows any
  waiting text, and more than 16 KiB waiting is shown at once. Set `WISP_TUI_STREAM_PACING=0` to show
  text as it arrives, or `WISP_TUI_STREAM_TRACE=<path>` to record when each text delta arrives.
  (#764)

## 0.2.2 — 2026-10-19

0.2.2 is a patch release on 0.2.1 with one terminal UI change. It changes no command, event, or
handshake model and no session format, so 0.2.1 and 0.2.2 read each other's sessions; the backend
and frontend must still come from the same release, which the handshake enforces.

### Terminal UI

- Back-to-back collapsed tool cards are separated by a thin rule instead of touching, so their
  chips no longer merge into one block in a run of one-line calls. A chip is now only as wide as its
  name, with plain padding keeping commands and paths in one column. A blank row separates your
  prompt's highlighted surface from the reply or cards below it. A card showing its results keeps a
  blank row before the next card, as before. Each rule costs a row, so a run of five collapsed cards
  takes nine rows. (#761)

## 0.2.1 — 2026-10-19

0.2.1 is a patch release on 0.2.0. It changes no command, event, or handshake model and no session
format, so 0.2.0 and 0.2.1 read each other's sessions; the backend and frontend must still come
from the same release, which the handshake enforces. Two fixes change which events a consumer sees
in failure paths; they are listed under the event contract below.

### Terminal UI

- Tool cards are easier to scan. Each card leads with a short tool name in a tinted chip (`Bash`,
  `Read`, `Search`, `List`, `Edit`, `Write`, `Process`, or `MCP` with `server/tool`) in place of
  verb phrases such as `Ran` or `Awaiting approval to read`. States the status marker can't show,
  such as `awaiting approval` or `denied`, end the row as a muted note. The first result row starts
  with a `└ ` connector, a selected card shows a muted `(→ to expand)` hint instead of a solid bar,
  a click on a card's action line, an `explored N files` row, or a thought header expands or
  collapses it, and back-to-back cards stack without blank rows. (#759)

### Event contract

- When a provider raises an ordinary exception after its response started (for example a
  connection reset), the open message now closes with `message.completed` (`finish_reason="error"`)
  carrying the partial text, before `error` and the turn's terminal events. This matches what a typed
  provider failure already produced; the harness keeps the partial reply in the transcript. (#753,
  HY-190)
- `context.overflow` is published only while a turn is open. When Wisp's own boundary check refuses
  to build the next request after a completed tool turn, only `error` is published; it no longer
  names the completed turn. Provider rejections inside a turn are unchanged. (#758, HY-201)

### Fixes

- A response that repeats a tool-call id now fails the turn with a provider protocol error, so a
  mutating tool can't run twice and the next request can't carry two results with one id. (#756,
  HY-191)
- Over JSONL-RPC, the 101st `steer` or `follow_up` sent before a prompt run is ready gets the same
  `Agent queue is full (maximum 100 pending messages)` error as one sent after, instead of an RPC
  command-queue error that depended on timing. `wisp.agent.harness` now also exports
  `DEFAULT_MAX_PENDING_QUEUE_MESSAGES` and `queue_full_error`. (#751)
- Development: TUI end-to-end tests reap failed PTY children with a deadline and no longer wait for
  footer redraws that never come, with narrower macOS timeouts (#750, #752, #754, #757); a loop test
  checks that every opened message closes once (#755, HY-195).

## 0.2.0 — 2026-10-19

0.2.0 is the second stable release. It promotes the 0.2 release candidates below (RC1 through RC13)
and adds two small changes since RC13. It changes no command, event, handshake, or session format
since RC13, so RC13 and 0.2.0 read each other's sessions; the backend and frontend must still come
from the same release, which the handshake enforces. Read the
[upgrade guide](book/src/guide/upgrading.md) before upgrading Python integrations or external
JSONL-RPC clients from 0.1.

### Since 0.1.0

The RC sections below have the details and pull-request numbers.

- **Rust terminal UI.** `wisp` opens a Rust TUI, now the only interactive interface; the Textual
  renderers are gone (RC3). Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 include it.
  Other platforms, including Intel macOS, get a pure wheel that keeps print, JSON, RPC, and SDK use.
- **MCP servers.** Manage them from `/mcp`, with `wisp mcp`, or by asking Wisp, without editing
  `settings.json`. Hosted servers can authenticate with headers or OAuth (RC8, RC9).
- **Web search.** Wisp can use the providers' own web search, which the TUI shows and `/search`
  toggles (RC8, RC11).
- **Trust and YOLO** are never saved for your home folder or a folder above it (RC10).
- **Prompt caching** for Codex, xAI, and Anthropic keeps more of each conversation cached between
  prompts (RC4).
- **Faster tools.** Native wheels use Rust for literal grep and for retaining managed-process output,
  and bash, `grep`, and `find` have less local overhead (RC3, RC11).

### Breaking changes since 0.1.0

- Interactive use needs a native wheel or a source-built `wisp-tui`. The `textual`, `fullscreen`,
  and `line` renderers, `--line`, `--tui-renderer`, and `wisp tui --renderer` are removed (RC3,
  RC4).
- External JSONL-RPC clients must send `rpc.handshake.request` first and connect only to a backend
  from the same Wisp release. Events no longer carry a per-event `schema_version`, and the Python
  SDK rejects a backend from a different release (RC4).
- The deprecated `wisp.agent.messages.SessionEntry(...)` factory and the remaining compatibility
  shims are removed (RC1, RC4), and several internal modules moved with no re-export, including
  `wisp.agent.prompt` to `wisp.coding.prompt` and `wisp.agent.messages` to `wisp.messages` (RC13).
- Session files load only in the current format (entry schema v6, compaction record v4), which is
  what 0.1.0 writes; files from earlier prereleases no longer load (RC4).
- `~/.wisp/catalog.toml` overlays must declare `schema_version = 2` (RC4).

### Since RC13

- Wisp no longer bundles the `github-pr-delivery` skill; `wisp skills`, `/skills`, and the RPC skill
  catalog list only `wisp-development`, now version 1.5. A user or project skill with that name is
  unaffected. (#748)
- The TUI's startup logo and the brand images use the refreshed glass mascot. (#740)
- Development: the book moved to `book/` and its CI fails on mdBook errors such as a missing include
  (#742); benchmark evidence moved to `benchmarks/evidence/` (#743); the native-wheel build hook moved
  to `packaging/hatch_build.py`, with a new "How Wisp is built" page (#744); duplicate images were
  removed and a test now rejects them (#745); and the repository gained `CONTRIBUTING.md` and
  `SECURITY.md` (#746) and a rewritten README (#747).

## 0.2.0rc13 — 2026-10-18

RC13 finishes a cleanup of the agent runtime's Python packages, stops a reply from flickering when
it finishes streaming, and loads long sessions a little faster. It changes no command, event, or
handshake model. Sessions a trusted RC13 run writes save their Git status snapshot in a new field,
which **RC12 and earlier reject**: they list such a session but report it as malformed when
resuming it. Several internal Python modules moved with no compatibility re-export. As always, the
backend and frontend must come from the same release, which the handshake enforces.

### Rust TUI

- A reply no longer flickers when it finishes streaming: the transcript keeps its layout until the
  command finishes, instead of briefly bringing back the working row. (#726)

### Sessions

- A trusted run saves the Git status snapshot its project context used with that system message
  (`repository_status` on the message entry), and a resumed session reuses it from there instead
  of re-reading it from the prompt text. A resume now also keeps the cached prompt when Git was
  unavailable or the project context was cut short. Sessions saved before RC13 read Git once on
  their next resume. The entry `schema_version` stays 6, since earlier files still load. (#732)
- Loading a session parses each saved entry once instead of twice. On a 10,000-message session,
  parsing every entry took about 30% less time, and resuming it in the TUI reached a ready screen
  about 50 ms sooner (local macOS measurement). (#736)

### Prompts

- A custom `prompt_messages` passed to `CodingSession` now also gets the web-search guidance when
  search is on, after the skill index and before the plan-mode instructions, as the default prompt
  does. Only code that builds `CodingSession` directly can set it; the CLI, RPC, and SDK never do.
  (#733)

### Python API

These modules moved with no compatibility re-export. None is in the documented SDK surface.

- `wisp.agent.prompt` is now `wisp.coding.prompt`. (#730) Its `recover_repository_status` is
  removed. (#732)
- `wisp.agent.mode` is removed: import `AgentMode` and `DEFAULT_AGENT_MODE` from `wisp.events`
  and `PLAN_MODE_SYSTEM_PROMPT` from `wisp.coding.prompt`. `is_agent_mode` is removed. (#734)
- `wisp.agent.messages` is now `wisp.messages` (`from wisp.messages import Message`), and
  `wisp.agent.history` is now `wisp.messages.history`. (#737)
- New: `wisp.sessions.RepositoryStatusSnapshot` (#732), and `PROJECT_CONTEXT_HEADER` (#732) and
  `extend_custom_prompt_messages` (#733) in `wisp.coding.prompt`.

### Development

- The agent harness names its cancellation phases (#727) and keeps its message queues in
  `harness/queues.py` (#728). `prompt/project_context.py` is split into `git.py`,
  `project_root.py`, and `repository_status.py` (#731). Behavior is unchanged.
- The agent README defines the continuation terms and the request-boundary rules (#729).
- The Rust TUI benchmarks run again: they passed the removed `--renderer` flag. CI now runs their
  PTY tests, which skip without a built `wisp-tui`. (#735)

## 0.2.0rc12 — 2026-10-17

RC12 is a Rust TUI candidate. Scrolling back no longer loses prompts, dragging over the transcript
copies it, the conversation scrolls while an approval or trust prompt waits, and a label above the
composer jumps back to the latest output. It changes no command, event, or session format; as
always, the backend and frontend must come from the same release, which the handshake enforces.
RC11 resumes, lists, and reads RC12 sessions.

### Rust TUI

- Scrolling back no longer blanks the view when an older history page loads just as the row
  above an earlier prompt reaches the top. (#722)
- The latest prompt stays pinned above its reply while you scroll back through that reply; before,
  the first wheel tick up dropped it from view. (#722)
- Dragging over transcript text highlights it, and releasing copies it to the clipboard (natively
  and through OSC 52) with a "Copied N chars" notice. A plain click copies nothing; wheel scrolling,
  a resize, or an approval or popup appearing ends the selection. Mouse capture now also requests
  button-drag reports (`?1002h`), never all-motion tracking. The terminal's own selection still
  works with its bypass modifier, such as Shift-drag in Ghostty, or with `WISP_TUI_MOUSE=0`. (#722)
- While an approval or trust prompt is docked, the wheel and the transcript scroll keys (`PgUp`/
  `PgDn`, `Ctrl+Up`/`Ctrl+Down`, `Ctrl+Home`/`Ctrl+End`) move the conversation. Clicks are still
  ignored, and a custom binding never takes a key the prompt uses. (#722)
- When scrolled back, a `↓ latest · Ctrl+End` label (`↓ new output` once output arrives) sits on
  the blank row above the composer; clicking it returns to the latest output. Terminals too short
  for that row keep the footer's `scrolled` / `new ↓` marker. (#722)
- The footer no longer shows the session name or path. (#722)
- Tool-call responses without text no longer show a standalone `cost unknown` label, live or after
  reload; priced estimates and saved cost data are unchanged. (#721)

## 0.2.0rc11 — 2026-10-17

RC11 shows and controls the providers' own web search in the TUI, and cuts local overhead from
tool calls: bash returns sooner after a command exits, large output decodes faster, and the Codex
provider keeps one connection across turns. It adds a command field and two event fields, so an
RC11 backend and frontend must be paired. Session files keep their format: RC10 resumes, lists, and
reads RC11 sessions, including with `read_typed_events()`.

### Rust TUI

- `/search` shows whether provider-native web search is on, and `/search on` or `/search off`
  changes it for the running session. The toggle applies to later prompts; it saves no default and
  does not change MCP search tools. The `web_search` setting and `WISP_WEB_SEARCH` still set the
  default. (HY-265, #710)
- Replies show the searches, opened pages, within-page searches, and failures behind them, with
  bounded source titles and hosts, also when a saved session is loaded. A reply whose cost excludes
  provider search fees shows `cost ≥…` as a lower bound; one without a token estimate shows
  `cost unknown`. (HY-265, #710)
- Scrolled-back prompts keep a blank row between the previous reply and their highlighted block;
  the reply's last line no longer touches it. (#711)

### Tool execution

- A bash command that exits cleanly with nothing left running returns in about 40 ms on macOS
  instead of about 360 ms: Wisp skips the post-exit process scans and the termination grace period
  when no process survived. Survivors are still found and killed as before. (#712)
- Bash output that is valid UTF-8 decodes several times faster; 1 MiB of short lines went from about
  790 ms to 150 ms end to end. Invalid bytes are replaced exactly as before. (#716)
- The core instructions ask the model to request independent read-only lookups together in one
  response instead of one per turn. The prompt-cache prefix changes once. (#713)
- `grep` and `find` calls in one batch now run one at a time: their scans contend for Python's GIL,
  so running them together was up to 1.9× slower (3.5× for case-insensitive grep). `read` and `ls`
  batches still run in parallel; a batch mixing them with `grep` or `find` runs serially. (#718)
- A call rejected before it runs (unknown tool, invalid arguments, policy denial, denied approval)
  no longer forces its batch to run serially. Results stay in call order. (#714)
- A `grep` regex that exceeds its 50 ms per-line limit on a busy machine gets one retry from a
  1-second budget per search before the pattern is reported as too slow, so a benign regex is no
  longer rejected because the thread was starved. Catastrophic patterns still fail, after about a
  second. (#719)

### Providers

- The ChatGPT/Codex provider reuses one HTTP client for the life of the runtime and reads each
  response to its end (for at most 0.25 s after completion) so the connection is kept. Requests
  within httpx's 5-second keep-alive window reuse it; later ones reconnect as before. (#717)

### Event contract

- `ConfigureCommand.web_search` turns provider-native search on or off for later prompts;
  `RpcController.configure(web_search=...)` sends it. `CodingSessionState.web_search` (`rpc.state`,
  default `false`) reports the setting, and `RpcMessageSnapshot.web_searches` (`rpc.messages`,
  default empty) carries the bounded search evidence of an assistant message. Neither is written to
  session files, which already held `web.search` events. (HY-265, #710)

### Development

- `benchmarks/builtin_tools.py` measures tool batches against a serial baseline, searches a real
  tree with `--root`, compares with `rg`, and times bash end to end. (#715)

## 0.2.0rc10 — 2026-10-16

RC10 stops YOLO and trust from carrying to every project under your home folder, and fixes
trackpad scrolling that did nothing with the pointer over the composer. It adds two event fields,
so an RC10 backend and frontend must be paired.

### Trust and permissions

- YOLO and project trust are never saved for your home folder, any folder above it (including
  `/`), a drive root, or the system's temporary folder. Wisp still starts there; trust is asked each
  session, and YOLO chosen there (approval choice 3 or `/permissions yolo`) lasts for the session.
  `wisp trust allow` and `revoke` exit 1 for such a folder. Records an earlier release saved for one
  are ignored, not deleted, and a startup notice names the folder and how to remove them. (HY-252,
  #707)
- The project folder is no longer found by walking up into your home folder: a Git repository or a
  marker such as `package.json` or `AGENTS.md` there no longer makes the home folder the project
  for everything below it. A folder below home with no repository or marker is now its own project,
  so `~/AGENTS.md` no longer loads as its project context. (HY-252, #707)

### Rust TUI

- Wheel and trackpad scrolling moves the conversation wherever the pointer is, including over the
  composer, and right after a resize; before, such gestures were dropped. (HY-246, #708)
- In a long session, scrolling into history that must be reloaded while a reply runs now shows
  "Earlier history loads when this reply finishes." instead of stopping silently. (HY-246, #708)

### Event contract

- `ToolApprovalRequested.session_only_yolo` and `PermissionState.session_only_yolo` (default
  `false`) say that choosing YOLO lasts for the session only. Sessions keep approval requests, so
  RC9's `read_typed_events()` rejects an RC10 session in which one was made; resuming, listing, and
  reading messages are unaffected. (#707)

## 0.2.0rc9 — 2026-10-16

RC9 fixes two problems found while dogfooding RC8 with Linear's hosted MCP server. It changes no
session, event, protocol, or settings format, so RC8 and RC9 read each other's sessions and
settings.

- Fixed MCP servers with more than 64 tools being rejected whole: Linear's 68 tools showed as "the
  server returned an invalid tool catalog". The per-server tool count is removed; the limits on
  definition size per server and overall, discovery pages, and 256 MCP tools overall remain. A
  rejected catalog now reports its real reason instead of the generic message. (#705)
- Asking Wisp to add a hosted MCP server now proposes its URL and Wisp's own OAuth sign-in, preferred
  over API keys. A proposal that wraps a Streamable HTTP URL in a local bridge such as `mcp-remote`,
  which waits for its own sign-in and timed out at startup, is refused with the URL to propose
  instead; a bridge to a legacy SSE-only server is still allowed. After you confirm an HTTP server,
  Wisp tells you to press `l` in `/mcp` if it needs a sign-in. (#705)
- `l` in `/mcp` on a command-based server explains how to add the server by URL to sign in, and on a
  server with its own `Authorization` header why it cannot. (#705)

## 0.2.0rc8 — 2026-10-16

RC8 makes MCP servers manageable without editing `settings.json`, lets Wisp set one up on request,
adds header and OAuth authentication for hosted MCP servers, and offers the providers' own web
search. It changes the live RPC contract (new commands, events, and fields), so an RC8 backend and
frontend must be paired.

### MCP servers

- `wisp mcp list`, `add`, `import`, `remove`, `enable`, `disable`, `login`, and `logout` edit
  `mcp_servers` in `~/.wisp/settings.json` through a locked, validated writer that keeps every other
  setting. Adding or enabling a server shows exactly what it runs, or where it connects and which
  variables it sends there, and asks first. (#695)
- `/mcp` in the TUI adds (by pasting a server's config snippet), enables, disables, removes,
  reconnects, signs in to, and signs out of servers without restarting Wisp. Values you enter are
  masked and never shown again. (#696, #697, #698, #703)
- Ask Wisp to "set up the GitHub MCP server": it proposes the change with `propose_mcp_server` or
  `propose_mcp_server_change`, and you confirm it. Only Confirm or Deny is offered; no permission
  mode, `--yes`, or session grant approves a proposal, and one that seems to carry a secret, or a
  field the confirmation does not show, is refused. (#699)
- HTTP servers can authenticate with `headers` (stored values) and `headers_from` (a header built
  from an environment variable, such as `"Authorization": "Bearer ${LINEAR_API_KEY}"`). Headers go
  only to the server's own origin, and every confirmation names each variable and the host it is sent
  to. Pasted snippets may set `headers`. (#701, #703)
- OAuth sign-in for hosted servers such as Linear's: `wisp mcp login NAME` or `l` in `/mcp` opens
  the sign-in page and waits for the browser. Tokens are saved in `~/.wisp/mcp-auth.json` (private,
  protected from file tools), bound to the server's URL, refreshed automatically, and never shown or
  sent to the model. A server that needs a new sign-in shows "sign in required". (#702, #703)

### Web search

- New user-only `web_search` setting (or `WISP_WEB_SEARCH`), off by default, offers the provider's
  built-in web search to the model: Anthropic (at most 5 searches per reply), OpenAI, xAI, and the
  Codex login. Google, DeepSeek, and OpenAI-compatible endpoints are not supported yet; the model is
  told it cannot search. Providers bill searches on top of tokens, so a reply that searched shows its
  cost as a lower bound (`cost ≥…`). (#700)
- Anthropic `pause_turn` is now resumed inside the adapter as one response. (#700)

### Event and protocol contract

- New commands: `add_mcp_server`, `remove_mcp_server`, `set_mcp_server_enabled`, `reconnect_mcp`,
  `preview_mcp_snippet`, `login_mcp_server`, and `logout_mcp_server`.
- New events: `web.search` (`WebSearchPerformed`, also saved in sessions), `rpc.mcp.preview`
  (`RpcMcpSnippetPreviewed`), and `rpc.mcp.login` (`RpcMcpLoginStarted`).
- `rpc.mcp` server entries gain `launch` (what the server runs or connects to, values never
  included) and `sign_in`. Tool approval requests gain `grants_allowed`. `UsageCost` gains
  `web_searches`, present only when a reply searched.
- New `McpStartupDiagnostic` codes: `invalid-header` and `login-required`.

### Compatibility

- RC7 reads RC8 sessions in which no web search ran. A session where one did (which records
  `web_searches` and a `web.search` event) is reported as malformed by RC7.
- RC7 cannot load a `settings.json` in which any MCP server uses `headers` or `headers_from`: it
  ignores the whole file. Remove those entries before rolling back.

## 0.2.0rc7 — 2026-10-15

RC7 fixes a dead end found while dogfooding RC6: a prompt that failed, for example because the
selected provider had no credentials, locked the TUI's composer until exit. It changes no session,
event, or protocol format, so RC4 through RC7 read each other's sessions.

- Fixed the Rust TUI locking the composer after a failed prompt, which left "The prompt failed.
  Ctrl-C exits." as the only way out and made `/connect` unreachable without credentials. A failed
  prompt now returns to idle, and its reason stays as a red `✗ Prompt failed: …` row under the
  transcript until the next command starts or the session is replaced. The backend's
  missing-credential messages name `/connect`. A closed backend connection still ends the session.
  (#693)
- The `/connect` API-key entry docks above the composer, shows a cursor after the masked key and a
  character count, and says when an environment variable already supplies the key, since that
  variable takes precedence over a saved one. (#693)
- Refreshed the bundled `wisp-development` and `github-pr-delivery` skills' runtime guidance and
  review-evidence rules. (#692)

## 0.2.0rc6 — 2026-10-14

RC6 refreshes how the Rust TUI presents approvals, diffs, tool progress, and work in progress, and
fixes dropped trackpad scrolling found while dogfooding RC5. It changes no session, event, or
protocol format, so RC4, RC5, and RC6 read each other's sessions.

- Fixed trackpad and wheel scrolling being silently dropped while the agent was working: tool output
  queued ahead of the gesture invalidated it, so earlier turns looked unreachable. Scrolling while
  idle was unaffected. (#685)
- Tool approval and project-trust requests now dock as one panel above the footer in place of the
  composer instead of a centered box, keeping the conversation visible and the draft intact.
  Choices can be picked with `↑`/`↓` and `Enter` as well as the existing keys; there is no default
  choice, so an `Enter` meant for the composer cannot answer a request. The built-in `bash` tool's
  command shows as `$ command` with its other arguments; other tools show all arguments.
  `/permissions` docks above the composer with the same selection rules. (#686)
- `edit` and `write` cards show a quieter diff: change counts on the action line, `⋯` between
  hunks, no "No newline" notes unless the final newline changed, a line-number gutter for `write`,
  and the changed words of a replaced line in bold and underline. The Enter-details view still shows
  the full unified diff. (#687)
- Tool cards mark their state by shape (`✓` done, `✗` failed, `⊘` denied or cancelled, `?` awaiting
  approval) and running cards show a braille spinner. A collapsed card keeps its action line to one
  row, ending in `…`. The browse hint moved from every card to the footer. (#689)
- The working row reads like `⠹ Thinking · 12s · Esc to interrupt`: the phase, how long the current
  command has run, and the interrupt key. (#690)
- Refreshed the provider model catalog with current OpenAI, Codex, Claude 5.5, Gemini 4 Argon,
  DeepSeek V4.1 Flash, and Grok 4.7 models and prices. (#684)
- Redrew the Braille startup logo for terminal legibility. (#688)

## 0.2.0rc5 — 2026-10-14

RC5 fixes two problems found in the published RC4: stdio MCP servers failing to start in public
installs, and `wisp update` dropping the Rust TUI. It changes no session, event, or protocol format,
so RC4 and RC5 read each other's sessions.

- Fixed stdio MCP servers failing to start in public installs ("MCP server … is unavailable", with
  a `coroutine '_get_executable_command' was never awaited` warning). Wisp called a private `mcp`
  helper that became async in `mcp` 2.3.0; it now resolves the server command itself, using the
  SDK's public Windows helper. Affects 0.2.0rc3 and 0.2.0rc4 installs that resolved `mcp` 2.3.0.
- Fixed a stalled HTTP MCP session termination no longer being bounded with `mcp` 2.2+, which sends
  the DELETE through a different client path. The shutdown deadline now applies to every send path.
- Fixed `wisp update` installing the pure wheel, and dropping the Rust TUI, when uv's default Python
  is older than 3.12: it now passes the running Python version to `uv tool install`. The 0.2
  install and rollback instructions add `--python 3.12` for the same reason, and the installation
  guide explains how to recover an install that has no `wisp-tui`.
- Added a scheduled CI workflow that runs the test suite against the newest dependency versions
  `pyproject.toml` allows, since `uv.lock` pins older versions than public installs receive.

## 0.2.0rc4 — 2026-10-14

RC4 collects the changes since RC3: the live event contract without per-event versions, removal of
the remaining compatibility shims, prompt-cache improvements for Codex, xAI, and Anthropic, and
fixes found while dogfooding RC3.

**Rolling back to RC3:** RC3 cannot open a session in which RC4 saved a response with a
`prompt_cache_key` (responses from providers that send cache keys, such as Codex and OpenAI); it
reports a malformed session entry. Back up sessions before trying RC4, and restore the backup rather
than opening RC4-touched sessions with RC3.

- **Breaking:** the live RPC handshake no longer carries a protocol version. Requests drop
  `min_protocol_version`/`max_protocol_version`, accepted responses drop `protocol_version` and the
  range, and the `protocol_version_mismatch` rejection code is gone; frontends rely on the
  `backend_package_version` check instead. The manifest no longer records `live_protocol_version` or
  `schema_format_version`.
- **Breaking:** removed the TUI renderer selector: `--tui-renderer`, `wisp tui --renderer`,
  `WISP_TUI_RENDERER`, and `wisp.cli.TuiFrontendKind`. The TUI is always the Rust frontend.
- **Breaking:** `~/.wisp/catalog.toml` overlays must declare `schema_version = 2`.
- **Breaking:** removed remaining compatibility shims from the public Python API:
  `defer_context_overflow_errors` on `AgentLoopConfig` and the harness `prompt`,
  `prompt_message`, and `continue_` methods (deprecated since #634); the `TuiRendererKind` alias
  (use `TuiFrontendKind`); `persist_user_effort` (use `try_persist_user_model_selection`);
  `SessionReplay.entry_ids` (use `context_entry_ids`); and `ProjectSnapshot.paths` (use
  `entries`). `replay_session_entries` no longer chains tree entries that omit `parent_id`;
  link them explicitly. Runtime factories patched in for `build_runtime` must accept every
  keyword argument `build_runtime_for_config` passes. These removals are a documented
  early-removal exception in the compatibility policy.
- **Breaking:** session files are read only in the current format (entry schema v6, compaction
  record v4). Unversioned and v1–v5 session files, and events stamped with a pre-v9
  `schema_version`, now fail to load instead of being upgraded in memory.
- **Breaking:** `WispEvent` payloads no longer carry a per-event `schema_version`. The current
  `schemas/live-rpc/` bundle is the single event contract, and a backend and frontend must be the
  same Wisp release.
- **Breaking (SDK):** the Python SDK now rejects a backend from a different Wisp release at connect
  time with `RpcHandshakeError` (code `"backend_version_mismatch"`), as the Rust TUI already did.
  Previously a mismatched backend failed mid-stream on the first event it could not decode.
- The Anthropic adapter now requests prompt caching. Every Messages request sends top-level
  `cache_control`, so tools, system prompt, and resent history are read from cache on the next
  request. Before, nothing was cached and every request was billed as uncached input. In a live
  check, follow-up prompts went from 0% to 88–98% of input read from cache.
- Sessions hosted by `RpcHost` (the TUI's backend, `wisp --mode rpc`, and the in-process SDK) ask
  Anthropic for a 1-hour cache lifetime, so a pause of more than five minutes between prompts no
  longer re-writes the whole conversation to cache. One-off print and JSON runs keep the 5-minute
  default. Cache writes cost 2× input at 1 hour (1.25× at 5 minutes).
- The Codex adapter sends Wisp's prompt-cache key as a `session_id` header, which the Codex
  backend needs before it reliably reuses cached prompt prefixes.
- The project-context git status is read once per session and reused when a session resumes, so
  the system prompt stays byte-identical across prompts and restarts and the provider's prompt
  cache keeps matching.
- Each response's `ContextObservation` records the `prompt_cache_key` its request carried (a new
  optional field; the schema bundle is regenerated in place).
- The Rust TUI's session picker searches and pages the full saved-session catalog (debounced
  name/ID search, newest first, refresh and retry) instead of showing a fixed 50 sessions.
  `get_sessions` accepts optional `query` and `cursor` parameters over RPC and the SDK.
- The Rust TUI adds `/queue` and a configurable `queue.manage` action to review, restore, and clear
  queued steering and follow-up messages. Mutations are guarded against stale queue snapshots.
- Fixed clone and fork failing with `Projected session validation failed` for any session that
  had run a prompt in the current process.
- Fixed a caller's own cancellation (an asyncio `task.cancel()` or an anyio timeout) being swallowed
  while a tool batch ran and reported as an ordinary cancelled turn. It now propagates; only the
  run's own cancellation token settles the batch.
- The Rust event decoder fills omitted fields that have a schema default, exactly as the Python
  reader does, so both frontends accept the same live events.
- The Codex adapter resends each earlier response's own output items (including encrypted
  reasoning) when a new prompt rebuilds history, and sessions save those items on the response's
  row in a new optional `Message.native_output` field. The prompt cache now reaches the end of the
  previous run, including after a restart or `/resume`. The field is not sent over RPC.
- The xAI adapter does the same: fresh requests replay each earlier response's output items and
  request encrypted reasoning (`include: ["reasoning.encrypted_content"]`). The OpenAI adapter keeps
  the portable rebuild until its backend has been checked.
- Fixed the RPC backend aborting at shutdown (`Fatal Python error: _enter_buffered_busy`, exit
  code -6) whenever the frontend kept stdin open, as the Rust TUI does. The handshake is now read
  from the stdin descriptor without buffering later commands, so POSIX backends read stdin without
  a thread and the Windows reader thread no longer holds the stdin buffer lock.
- Fixed the Rust TUI on Linux occasionally ignoring a key pressed at the same moment the
  terminal was resized until the next keypress. crossterm now reads input through its poll-based
  `/dev/tty` source (`use-dev-tty`) instead of edge-triggered mio.
- Dropped live RPC version history: the historical `schemas/live-rpc/v1`–`v8` bundles, their
  immutability checks, and the pre-v9 handshake rejection shape. The current bundle now lives
  directly in `schemas/live-rpc/`, and the release asset is `wisp-live-rpc.tar.gz`.
- Removed the compatibility alias modules `wisp.tools.process`, `wisp.tools.file_ops`,
  `wisp.tools.paths`, `wisp.tools.process_manager`, `wisp.tools.secure_fs`, and
  `wisp.providers.retry`. Import from the defining modules instead: `wisp.tools.shell.process`,
  `wisp.tools.files.operations`, `wisp.tools.files.paths`, `wisp.tools.shell.supervisor`,
  `wisp.tools.files.secure_fs`, and `wisp.retry` respectively.

## 0.2.0rc3 — 2026-09-17

RC3 makes Rust the sole terminal interface. The Python runtime remains authoritative for agent
behavior, providers, tools, permissions, and saved sessions.

- Removed the Textual and Python fullscreen renderers and their `prompt-toolkit` dependency. `wisp`,
  `wisp tui`, and `wisp --mode tui` now select Rust. Native wheels for macOS arm64 and Linux glibc
  2.28+ x86_64 bundle the TUI; pure/source installs retain print, JSON, RPC, and SDK, while
  interactive startup explains how to obtain or build a matching binary. The legacy `textual`,
  `fullscreen`, and `line` renderer choices and `--line` flag are no longer available. RC3 also
  drops RC2's Intel macOS native wheel.
- Moved native TUI launch and binary resolution into the Python CLI, retaining the `auto` and `rust`
  selector spellings for compatibility and exact Python/Rust version checks.
- Added selectable Rust startup logos and native Rust pending-text handling in packaged wheels.
- Improved Rust TUI full-history hydration without capping saved transcript content. The matched
  streaming and long-session benchmarks remain workload observations, not general performance
  guarantees.
- Replaced the VitePress documentation site with an mdBook, preserving the published guide,
  reference, architecture, and contributor routes while removing the Node and TypeScript toolchain.
- Added an optional Rust scanner for case-sensitive literal `grep` searches in native-wheel
  installations, while keeping repository traversal, filesystem policy, regex matching, and a
  pure-Python fallback in Python.
- Avoided duplicate protected-path matching when a file resolves to its lexical path, reducing
  post-scanner `find` and literal `grep` time while retaining symlink-target checks.
- Reused authorized directory descriptors when opening files for `grep`, avoiding a second
  component-by-component filesystem walk while preserving no-follow and replacement checks.
- Coalesced adjacent compatible `message.delta` events at the JSONL-RPC output boundary, reducing
  framing and parsing work for the Rust TUI while preserving bounded streaming latency and exact
  text, thinking, turn, content-index, and event-order boundaries.
- Added a matched end-to-end PTY benchmark for the Rust and Textual frontends, covering source-CLI
  startup, fake-provider streaming, visible response progress, clean shutdown, and terminal restore.
- Extended matched TUI evidence to paced streaming and long saved sessions, with interactive input
  latency and sampled process-tree CPU and memory observations for both renderers.

## 0.2.0rc2 — 2026-09-16

RC2 trialed Rust as the default on native-wheel installations and was published with three native
wheels, including Intel macOS.
The notes below describe that candidate's behavior; RC3 removes its Python TUI fallback.

- Added automatic renderer selection shared by `wisp`, `wisp tui`, and `wisp --mode tui`.
  macOS/Linux installations with a declared native binary prefer Rust; pure/source installations
  and other platforms use Textual. Explicit flags and `WISP_TUI_RENDERER` override this choice.
- Retained `wisp tui --renderer textual` as a maintained fallback. Rust startup/runtime failures
  remain visible errors with recovery guidance, never a silent renderer switch.
- Integrated verified native wheels for macOS arm64/x86_64 and Linux glibc 2.28+ x86_64 with the
  release workflow, including installation, native/pure replacement, offline reinstall and cleanup.
- Added Rust composer keyboard selection, word/line editing, undo/redo, clipboard actions, alongside backend-owned project initialization through `/init`.
- Preserved package/native version lockstep at Python `0.2.0rc2` / Cargo `0.2.0-rc.2`.

- Added the shared Glass TUI theme, which exposes the terminal's default background for real
  emulator-controlled opacity or blur while retaining readable graphite surfaces and luminous accents.
- Refreshed both bundled Agent Skills with current dual-frontend, generated-catalog, verification,
  and post-merge follow-up guidance.
- Restored visible working activity after the final tool result while the next model step is pending,
  including parallel tools, delayed stream paints, and still-running background processes.
- Rust TUI: match the composer background to the transcript and provide a four-choice permission
  dialog. `/permissions` manages project defaults saved outside the repository; once/session
  approvals remain temporary.
- Added bounded, theme-derived semantic coloring across the Rust composer and transcript: headings,
  lists, inline/fenced code, tool labels, statuses, commands, paths, and generic tool arguments now
  share a restrained palette while preserving exact editable, submitted, and restored text.
- Rust TUI: load the complete saved transcript on session open and retain all conversation entries.
  History transfers preserve message text and tool arguments instead of exhausting a shared preview
  budget; transport batches load automatically. Large conversation messages remain readable as plain
  text, with bounded rendering caches and compact tool previews.

## 0.2.0rc1 — 2026-09-10

Release candidate for the 0.2 minor line; not a new stable release. Textual remains the default TUI.
See the [0.2 upgrade guide](site/guide/upgrading.md) before updating Python integrations or external
JSONL-RPC clients. Stable installation instructions remain pinned to 0.1.0 until 0.2.0 is published.

### Textual responsiveness and correctness

- Preserved streamed output order when scrolling away from and returning to the live tail.
- Prioritized pending input over Markdown drains and adopted capability-gated synchronized terminal
  output to reduce partial-frame flicker on supported terminals.
- Reused stable paragraph wrapping during streaming and lazily rebuilt file-picker presentations,
  avoiding hidden-tree work and repeated option construction for unchanged queries.
- Fixed bare `@` reopening with stale filtered results and stale expansion markers for directories
  with no visible indexed children.
- Used conservative full parsing when control characters or unusual line separators make incremental
  Markdown source mapping unsafe, preserving complete final output.
- Added reproducible responsiveness and rendering benchmarks. Results and measurement limitations
  are recorded in `benchmarks/tui_responsiveness_evidence.md`; these are not a universal latency claim.

### Python API and runtime

- Hardened harness startup and cleanup so failures release run state and permit reuse. Detached
  message snapshots and completion projections prevent caller or event-observer mutations from
  changing retained transcripts, queues, or cached session messages.
- Fixed duplicate cancellation errors when cancelling immediately after a completed tool turn.
  Added deterministic interruption and recovery checks at every emitted boundary of six harness
  workflows, including tool settlement, queues, and transcript transitions.
- **Breaking Python API cleanup:** removed the deprecated `wisp.agent.messages.SessionEntry(...)`
  factory as an explicit early-removal exception to the normal deprecation window. Construct
  `MessageSessionEntry`, `EventSessionEntry`, or `CompactionSessionEntry` from `wisp.sessions` instead;
  wrap raw event dictionaries in `PersistedEventEnvelope(payload=...)` for `EventSessionEntry`.
  Existing JSONL session files and supported event schemas remain readable without migration.
- Reorganized agent internals into focused prompt and harness packages. Internal imports now use
  `wisp.agent.validation`, `context_budget`, `transcript_repair`, `tool_contracts`, and
  `request_boundary`; history normalization helpers live in `wisp.agent.history`. The old flat
  module paths and history-helper re-exports from `wisp.agent.messages` have been removed.
- Clarified harness/loop ownership, strengthened runtime invariant tests, hardened coding-agent
  instructions, and refreshed the provider model catalog.

### External frontends and live RPC

- Added backend-owned connection-catalog, API-key, disconnect, and device-code RPC so Textual and
  external frontends never read or write credential files. The catalog advertises only available
  provider endpoints, and mutation completion remains authoritative if the secondary status refresh
  fails. Protocol v1–v3 artifacts remain immutable.
- Added the enforced live JSONL-RPC v4 handshake, bounded strict framing, immutable generated schemas,
  generated Rust Serde types, and Python/Rust conformance fixtures for every current command and event.
- Added an authoritative, bounded model-catalog RPC used by Textual and external clients for provider,
  model, availability, lifecycle, and effort discovery. Protocol v1 and v2 artifacts remain immutable.
- External JSONL-RPC clients must handshake before sending commands and support live RPC v6 with
  event schema v37. Persisted session compatibility is separate and is not reset by this release.
- Recorded the #470 renderer decision: Textual remains the default TUI; the Rust frontend stays
  experimental source-build opt-in on macOS and Linux. Explicit Rust selection does not fall back to
  Textual, and Python distributions still do not bundle the binary.
- Kept the experimental Rust frontend in exact package-version lockstep, translating Cargo's
  `0.2.0-rc.1` spelling to Python's `0.2.0rc1` without accepting other release versions.

## 0.1.0 — 2026-08-23

- Added active-run steering and follow-up controls to the TUI, including queue previews,
  restoration, cancellation, and session-transition handling through the shared typed RPC runtime.
- Deferred provider construction until first use and tightened provider ownership across trusted
  configuration refreshes, substantially reducing startup time while preserving registration and
  cleanup behavior.
- Reduced tool-card update flicker by avoiding full-width separator repaints, coalescing lifecycle
  style changes, and limiting terminal writes to changed cells while retaining fail-open rendering.
- Made the bundled GitHub PR delivery workflow use structured, state-change-only monitoring for CI
  and review waits, reducing repeated tool output while preserving exact-head readiness and targeted
  failure diagnostics.
- Clarified how the bundled Wisp development skill composes with GitHub PR delivery while keeping
  local verification separate from remote merge-readiness evidence.

## 0.1.0rc6 — 2026-08-22

- Added bounded, structured TUI presentation for `read`, `grep`, and `find` results, including
  path-aware summaries, line metadata, syntax highlighting, truncation indicators, and expandable
  detail while preserving literal fallback rendering for ambiguous or malformed tool output.

## 0.1.0rc5 — 2026-08-22

- Loaded provider and MCP SDKs only when selected or configured, reducing CLI and TUI startup time
  without changing the typed provider exports, registration order, or runtime behavior.
- Mounted a bounded history window when resuming long sessions and discarded unreachable scroll
  targets after transcript content shrinks, keeping complete retained history navigable while
  avoiding long mount stalls and frozen wheel input.
- Coalesced Textual presentation timers and settled display updates, and centralized tool and process
  card identity across live and historical views, reducing redundant transcript work.
- Reused completed Markdown blocks and closed code-fence highlighting while assistant responses
  stream, reducing repeated parsing and syntax-highlighting work without changing final rendering.
- Added privacy-safe, opt-in TUI hotpath diagnostics and reproducible benchmark probes for Markdown
  drain cost, display update shape, duplicate-cell suppression, fail-open repaint paths, history
  prepend suppression, pending tool-card pressure, and covered-history reveal.
- Added a capability audit pinned to Pi SDK v0.84.2, recording shipped Wisp behavior, intentional
  safety and protocol differences, and explicit ownership for remaining SDK roadmap gaps.
- Defined package, Python API, event-schema, session-migration, deprecation, and removal policies;
  completed the consumer-focused event history from the first versioned contract through v34.
- Recorded `wisp.agent.messages.SessionEntry(...)` as deprecated. Migrate to
  `MessageSessionEntry`, `EventSessionEntry`, or `CompactionSessionEntry` from `wisp.sessions`; the
  compatibility factory remains available and emits `DeprecationWarning` when called.
- Added a dedicated Python SDK guide and API reference covering startup, event consumption,
  command correlation, safety requests, live control, persisted sessions, subprocess RPC, and
  cleanup.
- Added deterministic, tested Python SDK examples for prompting, safety requests, live control,
  persisted-session workflows, and process-isolated JSONL RPC.
- Defined and wheel-verified the supported Python SDK import surface, including the typed project
  trust command used by external RPC clients.
- Limited routine Textual transcript scrolling to partial viewport updates, preventing unchanged
  composer and status rows from flickering during wheel and keyboard navigation.
- Kept complete resumed Textual histories covered until composer layout and tail positioning settle,
  preventing a transient viewport jump when the loading surface closes.

## 0.1.0rc4 — 2026-08-20

- Preserved accepted and pending TUI input across RPC handoff, startup, failure, and cancellation,
  keeping early drafts editable instead of silently queueing or losing them.
- Added an animated startup notice above the composer and hid provider-facing system prompts from
  TUI history while preserving persistence, RPC, provider context, and complete-history pagination.
- Excluded `cryptography` versions before 50.0.0 from runtime dependency resolution to avoid the
  PKCS#7 decryption vulnerability fixed in 50.0.0.

## 0.1.0rc3 — 2026-08-19

- Added bounded Markdown structure, inline-code, and fenced-code highlighting to the Textual prompt
  editor without changing submitted prompt text.
- Kept complete Textual conversation and tool history reachable through wheel, PageUp/PageDown,
  Home, and End navigation during long and tool-heavy turns without unbounded widget mounting.
- Added first-class DeepSeek support with streamed thinking, reasoning-preserving tool
  continuations, API-key authentication, automatic cache telemetry, and model catalog metadata.
- Added first-class xAI Grok support through the stateful Responses API, including streamed
  reasoning, native tool-call continuation, API-key authentication, and model catalog metadata.
- Preserved RPC command ordering when changing providers or models so configuration waits for
  active session work and takes effect before later prompts.
- Rejected ambiguous bare model selections when the active provider cannot disambiguate them,
  preventing cataloged models from being silently applied to an unrelated provider.

## 0.1.0rc2 — 2026-08-18

- Stabilized streamed TUI output and resumable process-card lifecycles across polling, replay,
  denial, interruption, concurrent calls, pagination, reload, and bounded retention.
- Added an asynchronous TUI update prompt that defaults to updating and restarting supported
  persistent installs while preserving explicit checks, skip/later choices, and safe relaunch state.
- Removed obsolete synchronous and CLI-owned RPC compatibility paths so queueing, cancellation,
  trust, approvals, and shutdown consistently use the shared async command host.
- Centralized context-budget construction in the shared accounting policy and strengthened CI with
  a required production fault-injection partition plus broader platform-sensitive coverage.
- Added a VitePress documentation site, reduced the README to a focused entry point, and added
  release, safety, provider, session, interface, and contributor references.
- Refined Vapor, Orchid, Ember, and Paper; added Grove, Wave, and Dawn; and made semantic transcript
  surfaces meet normal-text contrast requirements across every curated theme.

## 0.1.0rc1 — 2026-08-17

- Kept compaction, steering, follow-up, provider continuation, and overflow recovery inside one
  long-lived agent loop with typed request-boundary transitions.
- Separated provider thinking from assistant content, made provider failures terminal stream events,
  and improved context-budget accounting and actionable tool-failure feedback.
- Prevented truncated or inconsistent tool-call batches from reaching execution and added bounded
  concurrency for explicitly parallel-safe batches while preserving deterministic approvals,
  source-ordered results, sequential fallback, and complete cancellation.
- Preserved validated historical tool call/result pairs for native replay, with deterministic safe
  fallbacks for provider switches, opaque provider state, malformed history, and interrupted calls.
- Hardened filesystem tools and skill loading against races and path escapes, bounded read/search
  work, and fixed tool working-directory behavior for selected workspaces.
- Added richer diff layouts, transcript recovery, prompt highlighting, project-tree file selection,
  approval safety surfaces, and additional curated TUI themes.
- Fixed tool-heavy historical session replay repeatedly reloading its latest page, flickering the
  Textual screen and preventing normal interaction.

## 0.1.0b4 — 2026-08-12

- Added the package-owned `wisp-development` skill with progressively loaded architecture,
  extension API, safety, authoring, and verification guidance, plus a deterministic static
  extension example for Python embedders.
- Added named OpenAI-compatible providers with provider-scoped authentication, model metadata,
  reasoning effort, and private CA support, and improved Codex recovery from context overflow while
  preserving tool context.
- Added session-scoped prompt-cache keys, explicit stable-prefix cache boundaries for supported
  OpenAI models, and cache read/write telemetry in context usage views.
- Replaced TUI tool-card chrome with compact, expandable action and result trees that retain
  lifecycle state, bounded previews, structured diffs, and restored-session presentation.

## 0.1.0b3 — 2026-08-12

- Added configurable OpenAI-compatible endpoints for using provider APIs beyond the built-in OpenAI
  service.
- Added a project initialization command and refined TUI themes, permission prompts, transcript
  surfaces, streamed Markdown, diffs, and startup presentation.
- Fixed resumed and completed assistant response rendering, working-indicator visibility, truthful
  RPC command lifecycles, and recovery from incomplete session JSONL appends.

## 0.1.0b2 — 2026-08-11

- Added typed MCP status RPC snapshots and a TUI `/mcp` command that reports configured servers,
  registered tools, live connection state, and sanitized startup failures without reconnecting
  servers.

## 0.1.0b1 — 2026-08-10

- Reduced the installed-build PyPI update cache from 24 hours to six hours so new releases are
  announced sooner without checking on every TUI launch.
- Added immediate manual update checks through `wisp update --check` and `/update`, with confirmed
  installation for persistent `uv tool` environments through `wisp update` or `/update install`.
- Isolated self-updates from project-controlled uv configuration and indexes, kept provenance checks
  cancellable, and shielded active environment replacement from interruption.
- Added bounded, user-only configuration for multiple MCP stdio servers, including explicit
  arguments, environment forwarding, and exact per-tool safety overrides.
- Added MCP tool adaptation through the official SDK with deterministic namespacing, bounded schemas
  and text results, and command safety by default through Wisp's existing approval policy.
- Added concurrent MCP startup, paginated tool discovery, atomic catalog registration, sanitized
  failure diagnostics, and runtime-owned connection cleanup across CLI, RPC, SDK, and TUI modes.
- Added pre-parse MCP frame limits and aggregate server, tool, definition, and discovery limits so
  malformed or oversized servers fail independently without exposing payloads or credentials.

## 0.1.0a3 — 2026-08-08

- Added `wisp --version` to print the installed package version without starting the TUI.
- Added strict, trust-aware Agent Skills metadata discovery from user and project locations, with a
  `wisp skills` command for inspecting the catalog and isolated diagnostics.
- Added a bounded model-facing Agent Skills index and read-only `skill` tool for progressively
  loading instructions and contained supporting resources without weakening tool policy.
- Added explicit `/skill:<name>` invocation with typed replay evidence for prompts, steering, and
  follow-ups.
- Added typed skill-catalog RPC snapshots, deterministic `/skill:` TUI completion, cached `/skills`
  inspection, trust-refresh updates, and compact live and historical invocation presentation.
- Added an opt-in `wisp-code-review` example skill with user/project installation instructions and
  a progressively loaded Wisp review checklist.

## 0.1.0a2 — 2026-08-07

- Replaced TUI `/login` with an OpenCode-style `/connect` panel for ChatGPT subscription access and
  masked OpenAI, Anthropic, and Google API-key entry; `/disconnect` removes stored credentials.
- Removed the standalone browser-based `wisp auth login` command and its unusable localhost callback
  flow. ChatGPT subscription authentication now uses device-code OAuth exclusively.
- Added request-time stored API-key resolution for OpenAI, Anthropic, and Google while preserving
  explicit and environment credential precedence.
- Added a cached, non-blocking TUI notice when a newer applicable Wisp release is available on PyPI.

## 0.1.0a1 — 2026-08-07

Initial PyPI alpha release of Wisp's shared CLI, JSON, RPC, SDK, and Textual TUI runtime.

- Installs the `wisp` command through the `wisp-ai` package.
- Launches the fullscreen TUI from a bare interactive `wisp` invocation.
- Includes provider integrations, local tools, persistent sessions, compaction, project trust,
  protected paths, and explicit unsafe-tool approvals.
- Publishes provider-neutral lifecycle events at schema v27.

## Live RPC protocol v9

- Added Rust `/queue` and the configurable `queue.manage` action for FIFO previews, drain modes,
  newest-item restoration, and scoped clear confirmations. Existing Alt+Up uses guarded restoration.
- Queue snapshots add optional `token` and `command_id`; mode/pop/clear commands add optional
  `expected_token`. Stale requests and predictable oversized removal responses fail before mutation.
  Legacy unguarded callers remain supported; retained idle queues remain read-only. This additive
  change regenerates v9 in place without changing removal/snapshot/completion ordering.

- Events no longer carry a per-event `schema_version`. The live RPC protocol bundle under
  `schemas/live-rpc/` is the single compatibility contract: additive event changes regenerate the
  current bundle in place, and breaking changes bump the protocol version. The handshake no longer
  exchanges `event_schema_version`, and the `event_schema_version_mismatch` rejection code is gone.
- Live JSONL-RPC now negotiates protocol v9. Published v1–v8 bundles remain immutable. Persisted
  sessions written by earlier releases still load: the session reader drops a legacy
  `schema_version` key from event payloads before typed validation.
- Frontends speaking protocol v8 or earlier are rejected with `protocol_version_mismatch`. The
  backend ignores the removed `min_event_schema_version` / `max_event_schema_version` request fields
  a conforming v8 client still sends, so the rejection names the real cause.

## Schema v39

- Added nullable `message_entry_id` to `agent.started`, `message.completed`,
  `queue.message.injected`, `tool.execution.ended`, and `tool.result`. The coding session
  publishes assigned message IDs so frontends can reconcile live output with persisted history.
  An assigned ID does not guarantee a successful write; history reads verify availability.
- Live JSONL-RPC now negotiates protocol v8 with event schema v39. Published v1–v7 bundles remain
  immutable, and persisted event schemas v5 through v39 remain readable.
- Rust TUI scrolling recovers evicted live entries through bounded history pages, preserving
  surviving rows, pending scroll movement, and composer drafts.

## Schema v38

- Added `rpc.permissions` with effective mode, saved mode, and project path, plus `get_permissions`
  and `set_permissions` commands.
- Added the explicit `all_project` approval scope for saved project YOLO. Existing `all_session`
  remains temporary.
- Live JSONL-RPC now negotiates protocol v7 with event schema v38. Published v1–v6 bundles remain
  immutable; durable sessions retain backward-compatible parsing.

## Schema v37

- Added `get_project_files`, returning non-persisted `rpc.project_files` snapshots with relative
  file/directory metadata, request correlation, a policy generation, and explicit truncation.
- Added `project_files.invalidated` to discard earlier snapshots during trust/configuration changes.
  Python owns guarded traversal, protected paths, bounded work, cancellation, and publication;
  clients can derive trees and rank the sanitized snapshot locally.
- Live JSONL-RPC now negotiates protocol v6 with event schema v37. The v1–v5 schema bundles remain
  immutable; persisted event compatibility is unchanged.

## Schema v36

Adds `rpc.connection_catalog`, `rpc.device_code`, and `rpc.device_code.progress` for correlated,
backend-owned provider connection workflows. Connection catalogs expose only sanitized status;
API keys and OAuth tokens are never returned or persisted in session JSONL. Device user codes and
verification URLs are intentionally frontend-visible login instructions and must not be logged or
persisted.

## Schema v35

Adds `rpc.model_catalog`, a correlated snapshot of the current model selection and the ordered,
runtime-available provider/model catalog. Successful provider, model, and effort configuration emits
this authoritative result before command completion when the catalog fits the wire bounds; an
oversized catalog fails discovery explicitly without blocking typed configuration. Discovery never
constructs deferred providers.

## Schema v34

Adds `next_after_entry_id` to `rpc.messages` results so clients can paginate forward from a known
transcript entry. Consumers using forward pagination must accept the cursor; older pagination logic
can continue using `next_before_entry_id`.

## Schema v33

Adds optional `failure_code`, `retryable`, and `recovery_hint` metadata to failed tool results so
clients can present structured recovery guidance. Consumers that copy tool-result fields should
accept the new optional metadata.

## Schema v32

Adds `trailing_estimated_tokens`, `effective_tokens`, and `accounting_method` to context budget
snapshots, plus the provider-derived `context_observation` on `message.completed`. Clients can use
these fields to distinguish provider usage from local estimates; context displays should prefer
`effective_tokens` when present instead of assuming the raw estimate is authoritative.

## Schema v31

Adds `package:wisp` as a skill catalog source for package-owned skills and
`utf8_bytes_div_4_v2` as a Unicode-aware `ContextEstimate.method`. Consumers that exhaustively
validate skill sources or context-estimation methods must accept the new values.

## Schema v30

Adds the `rpc.mcp` event carrying configured server connection status, registered tool names, and
sanitized startup failures. RPC clients can issue `get_mcp_status`; consumers that exhaustively
match event types must handle or deliberately ignore `rpc.mcp`.

## Schema v29

Adds `rpc.skills` and `skill.catalog.updated` events carrying typed skill descriptors, isolated
discovery diagnostics, and project-trust state. RPC clients can issue `get_skills` for the active
immutable catalog; clients should replace their cached snapshot when `skill.catalog.updated`
arrives.

## Schema v28

Adds `skill.invoked` and typed skill-invocation evidence to queue updates and persisted RPC message
snapshots. Consumers can present the original directive and request without parsing the bounded
provider-visible expansion.

## Schema v27

Adds `mode` (`"plan" | "build"`, default `"build"`) to `CodingSessionState`, so RPC `get_state`
reports the active agent mode. Consumers that model state exhaustively must accept both values; the
field is stripped when serializing an older schema.

## Schema v26

Adds automatic-compaction policy to project-configuration and session-statistics events. Consumers
that present configuration or statistics should accept the optional compaction policy fields.

## Schema v25

Adds managed-process state, captured streams, truncation flags, dropped-byte counts, and process
errors to `tool.execution.ended` and `tool.result`. Tool consumers should use this metadata for
resumable process presentation and must not attach it to pre-v25 payloads.

## Schema v24

Adds `rpc.session.tree.unreverted` for undoing the latest session-tree navigation. Also adds
`output_has_exit_status` to tool execution/result events and persisted tool-result snapshots so
consumers can distinguish Wisp's synthetic completion envelope from genuine output. Exhaustive event
consumers must handle or deliberately ignore the new result event and accept the provenance flag.

## Schema v23

Adds `rpc.commands`, a typed catalog of commands supported by the active RPC host. Clients can issue
`get_commands` instead of maintaining an interface-specific command list.

## Schema v22

Adds optional tool-result presentation snapshots to messages returned by `rpc.messages`. Transcript
consumers should use the structured result metadata instead of reconstructing historical tool state
from text.

## Schema v21

Adds durable session names to state, session summaries, selection and derivation results, plus the
`rpc.session.name_changed` event. Consumers that copy these snapshots should accept nullable name
fields and the new event.

## Schema v20

Adds `rpc.session.tree` and `rpc.session.tree.navigated` events for inspecting and selecting the
active path through append-only session history. Consumers that expose branching should preserve
entry IDs and parent relationships from these events.

## Schema v19

Adds `rpc.session.cloned` and `rpc.session.forked` results. Session clients should correlate these
results by `command_id` and use the returned source and destination identities.

## Schema v18

Adds `rpc.sessions` and `rpc.session.selected` for cataloging and selecting persisted sessions.
Consumers should treat the selected session identity and path as authoritative host state.

## Schema v17

Adds paginated `rpc.messages` transcript snapshots, including optional structured `tool_calls` on
message entries. Clients should consume the typed message entries and pagination metadata rather
than reading an active session file concurrently.

## Schema v16

Adds `rpc.state`, reporting the active provider, model, session, run state, and pending queue counts.
Long-lived clients should refresh their local state from this snapshot after startup or uncertain
transitions.

## Schema v15

Adds `queue.items.removed`, the result of removing pending steering or follow-up work. Queue clients
must handle the result separately from unsolicited queue snapshots.

## Schema v14

Adds `queue.message.injected` when steering or a follow-up enters the active run. Consumers that show
live input should distinguish injected work from pending queue contents.

## Schema v13

Adds `queue.updated` snapshots with steering and follow-up contents and drain modes. Queue-aware
clients should replace their cached queue state with each snapshot.

## Schema v12

Adds list-price `cost` accounting to completed messages, compactions, and session statistics.
Consumers should treat cost as optional and retain the accompanying currency and model provenance.

## Schema v11

Adds overflow-triggered compaction and retry metadata. Compaction consumers must accept `overflow`
as a reason and use `will_retry` and the completion explanation when deciding what happens next.

## Schema v10

Adds threshold-triggered automatic compaction and its trigger budget. Compaction consumers must
accept `threshold` as a reason and preserve the budget snapshot that caused it.

## Schema v9

Adds `context.estimated` and `session.stats` events with context budgets and aggregate usage.
Consumers that need these reports must not expect them on v8 or older payloads.

## Schema v8

Adds manual `compaction.started` and `compaction.completed` lifecycle events. Consumers must handle
or deliberately ignore both event types and correlate completed compactions by session.

## Schema v7

Adds `context.pressure` and `context.overflow` events. Consumers that surface context limits should
handle warning and terminal-overflow signals separately.

## Schema v6

Adds optional provider-reported token usage to `message.completed`. Usage consumers must tolerate
missing values because not every provider response reports every token category.

## Schema v5 — oldest readable

Adds `model.provider_auto_switched` so RPC clients can observe a model selection that also changes
the provider, and adds optional `effort` to `project.config.applied`. Consumers must update both
provider and model when the switch event is emitted and accept the effective effort after trusted
project configuration is applied.

## Schema v4 — archival, unsupported

Adds bounded presentation metadata to `tool.execution.ended` and `tool.result`: `exit_code`,
`before_text`, `created`, `summary`, and `truncated`. Current typed parsers no longer accept v4
payloads.

## Schema v3 — archival, unsupported

Adds `provider.retrying` before response streaming starts, carrying attempt, delay, reason, and
optional status code. Current typed parsers no longer accept v3 payloads.

## Schema v2 — first versioned contract, archival, unsupported

Replaces the unversioned `token.delta` and `assistant.message` stream with explicit turn, message,
and agent lifecycle events; adds `tool.call` before execution. Current typed parsers no longer accept
v2 payloads. Events before this contract had no `schema_version`; there was no merged schema v1.

Sessions containing these events are no longer readable.

---

### Adding an entry

When you change a command, event, or handshake model, regenerate `schemas/live-rpc/` with
`uv run python -m wisp.rpc.protocol_schema --write`. Record every change under the current
release-preparation heading, or under `## Unreleased` after that release is published. Say what a
consumer must do differently — new fields, changed meanings, dropped compatibility — not the
implementation detail.
