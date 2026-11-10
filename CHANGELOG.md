# Changelog

Notable changes to Wisp, with an emphasis on the **event contract** that JSON, RPC, and SDK
consumers integrate against.

`WispEvent` payloads carry no per-event version, and a backend and frontend must be the same Wisp
release. `schemas/live-rpc/` describes the current release's wire contract; the typed models live in
the `src/wisp/events/` package. Event changes are recorded under the release that makes them.

The `## Live RPC protocol vN` and `## Schema vN` headings below are historical records from before
this policy.

## Unreleased

### Sessions

- A session can move its working directory to another folder: the new `change_directory {path}`
  RPC command, and `RpcController.change_directory()` in Python. The path may be absolute, `~`, or
  relative to the current folder, and must exist and be a directory. The command waits for running
  work and replies with `session.directory_changed` when it saved the move, then `rpc.workspace`.
  File and shell tools, the prompt's project context, Git status, and the file picker use the new
  folder from the next operation. `CodingSession.change_directory()` moves within the session's
  project only. (HY-476)
- A folder outside the project root enters the project that contains it, rerunning what startup
  does for it. Its settings layer, skills, instruction files, saved permission mode, and file-tool
  boundary replace the old project's, and the old project's settings, including a project
  `auth_path` and provider settings, never carry over; in-session `/model` and similar choices do.
  A project trusted by a saved decision, an environment override, or an earlier decision in the same
  process applies at once; an undecided one is untrusted until the next prompt asks, as at
  startup. Temporary tool approvals are cleared; background processes keep running. A folder inside
  the project stays in it even when it holds a repository of its own. The reply also carries
  `project.config.applied` when the configuration changed, `skill.catalog.updated`, and
  `rpc.permissions`. (HY-477)
- A move is saved as a `session.directory_changed` event entry on the active branch, not as a
  message. The next user message the model sees starts with
  `[The working directory has been changed to <folder>.]`; the saved prompt stays as typed, and a
  later replay rebuilds the same text, so the prompt cache still matches after the first turn. A
  separate note message would have put two user messages in a row, which some chat templates
  reject.
- Selecting, cloning, forking, navigating, or resuming a session restores the folder its active
  path last recorded; a session that records none, including every session saved before this
  release, works from the folder Wisp started in, as before. A recorded folder in another project
  enters that project, as `change_directory` would; one that is gone or unreadable falls back to
  the launch folder with a notice. A new session started after a move
  records its starting folder, and a session continued from another folder (`wisp -p --resume`)
  records that move. Session files keep `schema_version` 6, and 0.7 reads a session with moves; it
  ignores the event entries, so it works from its launch folder.
- The Rust TUI has `/cd <path>`. It completes folder names from the local file system as you type
  the path: `Tab` fills in the selected folder, and `Enter` moves once the path names one. Like
  other configuration, `/cd` is refused until the session is idle rather than queued behind a run.
  Each move draws a `── Working in <folder> ──` divider below the last reply, unless it ends where
  the moves before it started, in the live session and when a session is resumed; the footer, side
  panel, and permission badge follow the new folder and project. A move before any reply shows only
  in the footer. (HY-478)

### Events

- `session.directory_changed` (`SessionDirectoryChanged`) is new: `session_id`, `previous_cwd`
  (`null` for the folder a new session started in), and `cwd`. `change_directory` is a new
  command. `rpc.workspace` is now also sent after a move and after a session becomes active. The
  schema bundle and conformance fixture are regenerated; the same-release rule applies.
- `rpc.messages` message snapshots carry `moved_to`: the folder the session moved to after that
  message and before the next one, where the moves in between ended, or `null` when there were none
  or they ended where they started. A session's starting-folder record is not a move. The TUI draws
  it as the divider. (HY-478)
- `/cd` is in the `get_commands` catalog, with one required `path` argument.

### Tools

- An experimental `codemode` tool, off unless `"codemode": true` or `WISP_CODEMODE=1`: the model
  writes a Python script that calls the run's read-only tools, and only what the script prints and
  returns comes back. Scripts run in [monty](https://github.com/pydantic/monty)'s sandbox, from the
  new `codemode` extra, with no files, network or environment, and are type-checked before they
  run. Each call a script makes passes the same checks as a direct call; a call that would need an
  approval fails, so approving the script grants nothing. Limits: 200 calls, 2 minutes, 10 seconds
  of computing and 128 MiB per script. The tool-use evals gain `--codemode python`. (HY-495)
- The file and search tools' boundary is the project root in a trusted project, instead of the
  working directory, so a session started in a subfolder can read, edit and search the rest of its
  project. Relative paths are still taken from the working directory, protected paths stay
  protected wherever they are, and symbolic links must still resolve inside the boundary. An
  untrusted session keeps the working directory as the boundary, as does one whose project root
  does not contain the working directory or is a broad folder (the home folder, a folder above it,
  a filesystem root, or a temporary folder), including a `project_context_root` an SDK caller
  supplies. `grep` and `find` show files above the working directory as `../` paths, which globs
  can match, and a search inside the working directory returns the same files whether or not the
  project is trusted. A refused path now reads `Path is outside the workspace`, and its hint names
  the boundary. `ToolContext` gains `workspace_root` and `boundary()`. (HY-475)

<!-- Keeps links to the pre-release "0.7.0 — Unreleased" heading working. -->
<a id="070--unreleased"></a>

## 0.7.0 — 2026-11-10

0.7.0 lets models see images: pasted or dragged into the TUI composer, attached through RPC or the
SDK, or returned by `read` for an image file. It also finds MCP tools on demand when a catalog is
large, shows edit diffs with the file's real line numbers, previews long `bash` output as its start
and its end, and adds `$EDITOR` editing, clickable links, terminal title and notification signals,
and a colour fallback to the TUI. Commands and events gain fields, so the backend and every
frontend must come from the same release. 0.7.0 reads sessions 0.6.x wrote, but 0.6.x refuses a
0.7.0 session that holds a successful edit or a proxied MCP call, and drops the images of one that
holds images.

### Images

- Sessions keep images in a content-addressed store, an `images/` folder beside the session files
  keyed by SHA-256, and messages reference them as `ImagePart`s (`media_type`,
  `sha256`, `byte_size`, `width`, `height`) instead of holding base64. An image is stored before any
  message refers to it, and the bytes sent to a provider are the stored bytes, so a resumed request
  sends the same image. Wisp accepts PNG, JPEG, GIF and WebP, detected from the file's content, up
  to 7.5 MB and 8,000 px a side; it reads the dimensions from the header and does not resize. A
  message with no images is written as before. (#937)
- The `prompt`, `steer` and `follow_up` commands, and the matching `RpcController` methods, accept up
  to six `images` per message, each a base64 image file or the `ImagePart` of an image already in
  the session. A model that does not take images, or images larger than one request can carry, is
  refused before anything is stored. (#938, #941)
- Each adapter sends user-message images in its provider's format: Anthropic image blocks,
  Responses and Codex `input_image` parts, Chat Completions `image_url` parts, and Gemini inline
  data. A request carries at most 24 MB of images (12 MB for Gemini), newest first; older images,
  images whose bytes are missing, and every image for a text-only model go as a short
  `[image not sent: image/png, 640x480]` note. The catalog's `image_input_models` lists the models
  that take images. (#939)
- `read` returns an image file as an image: the model gets a line naming the file, its format, size
  and bytes, and the image itself. BMP, TIFF, HEIC and AVIF files are refused with a message that
  names the format. (#944)
- Each adapter also sends tool-result images: inside Anthropic tool results and Responses and Codex
  `function_call_output`, in one user message after the tool messages for Chat Completions (which
  accepts images only from the user), and inside Gemini 3's function responses or alongside earlier
  Gemini models' responses. Tool images share the run's image budget with the prompt's and with
  queued messages'; a queued message whose images no longer fit is refused. Providers that replay
  earlier tool calls as a summary (Anthropic, Gemini, DeepSeek) name the images in it rather than
  resending them. (#945)
- `scripts/live_image_smoke.py` checks the formats against the live APIs. On 2026-11-10 they passed
  for the OpenAI Responses API, Codex, xAI (Responses and Chat Completions) and Anthropic, and
  DeepSeek got notes as a text-only model should. Gemini has not been checked live yet. (#947)

### Events

- `ImagePart` is new. User-message `rpc.messages` snapshots and `queue.message.injected` gain
  `images`; `queue.updated` and `queue.items.removed` gain `steering_images` and
  `follow_up_images`; `rpc.session.forked` gains `selected_images` and `rpc.session.tree.navigated`
  `editor_images`, so a message that returns to the composer brings its images; and
  `tool.execution.ended` and `tool.result` gain `images` (at most six). The model selection in
  `rpc.model_catalog` gains `image_input` and `max_request_image_bytes`. Sessions
  save message images (optional, so no `schema_version` change). The schema bundle and conformance
  fixture are regenerated; the same-release rule applies. (#937–#945)

- `tool.execution.ended` and `tool.result` gain `edit_locations`: for a successful `edit`, each
  edit's first line in the file before the call and in the file it produced, in call order (empty
  for other tools). Sessions save them with the result (optional, so no `schema_version` change),
  and `rpc.messages` snapshots carry them as `tool_result.edit_locations`. The schema bundle and
  conformance fixture are regenerated; the same-release rule applies.

- `tool.execution.started`, `tool.approval.requested`, `tool.approval.resolved`,
  `tool.execution.ended` and `tool.result` gain an optional `target`: for a proxied MCP call
  (`mcp_call`, see MCP below) it names the MCP tool that runs, while `name` stays `mcp_call`. It is
  `null` for every other call. The schema bundle and conformance fixture are regenerated; the
  same-release rule applies.
  Sessions save the target with a proxied call's result (an optional field, so no
  `schema_version` change), and `rpc.messages` snapshots carry it as `tool_result.target`.

- `agent.started` gains an optional `setup` (`RunSetup`): the names of the tools offered to the
  model, in offered order, and SHA-256 hashes of the tool specs and of the system messages the run
  starts with. It lets an eval or a log reader confirm which setup a run actually had. The system
  messages include project context and Git status, so their hash also changes with the repository.
  `agent.started` is not saved in session files. The schema bundle and conformance fixture are
  regenerated; the same-release rule applies.

### MCP

- New user-only setting `mcp_tool_exposure` and `WISP_MCP_TOOL_EXPOSURE`: `"auto"` (the default),
  `"direct"` or `"on_demand"`. On demand, the model gets `mcp_search`, `mcp_describe` and `mcp_call`
  instead of every MCP tool's definition. `auto` finds tools on demand when a run's MCP definitions
  come to at least 2,000 estimated tokens, and sends them directly below that. `mcp_call` reaches only the MCP tools direct mode would
  offer that run, and each call goes through that tool's own safety, approval and session grant.
  The TUI and CLI approval prompts name the tool that runs. In the tool-use evals, on demand cut
  tokens per run by 76–77% on a multi-step task with a 102-tool catalog and by 28–46% with 50 small
  tools, with every run still passing; with 6 tools it cost 20–34% more. A sweep from 6 to 50 tools
  found the break-even at about 1,800 estimated tokens of definitions, which sets `auto`'s threshold.
  A call missing a required parameter is refused before approval; types are left to the server.
- In the TUI, an `mcp_call` card shows the MCP tool it runs and that tool's own arguments, as a
  direct call's card does, once the backend has resolved the call, in resumed sessions too. A call
  interrupted before its result, or split across two history pages, still shows `mcp_call`.

### CLI

- `wisp -p` gains `--timeout SECONDS`. At the deadline the run is asked to stop as a cancellation
  does: running tools and commands are stopped, and the run saves its session and still emits
  `agent.completed` with outcome `cancelled` and its totals. It reports why it stopped, and Wisp
  exits with status 124. A run already finishing after its last turn completes normally; one that
  neither stops nor finishes 10 seconds later is stopped outright, and if cleanup still holds it
  open 10 seconds after that, Wisp exits at once. No event or session format changes. `CodingSession.cancel_run()` is the new public way to ask an active run to stop like
  this; a run it stops between turns reports `cancelled`, not the last turn's `completed`.

### TUI

- Ctrl+O (`prompt.external_editor`) opens the draft in `$VISUAL`, `$EDITOR` or `vi` and makes the
  saved text the new draft, as one undoable edit. The editor runs as the terminal's foreground job,
  so Ctrl+C cancels only the edit, and a running prompt's events keep being handled meanwhile. A
  custom `tui_keybindings` that already binds Ctrl+O to another action now conflicts with this
  default, which restores the default keymap with a warning; bind `prompt.external_editor` to
  another key, or to `[]`, to keep yours.
- While browsing cards, Ctrl+O opens the file the selected card names in the same editor, at the
  card's line: a read's start, a search's first match, or the top of an edited or written file.
- Links in replies and the file path on read, edit and write cards are OSC 8 hyperlinks, so
  terminals that support them make them clickable. Only `http` and `https` reply links are linked.
- While a run works, the indicator beside "Working" breathes, and a tool card whose call just
  finished glows briefly. The breath blends colour on truecolor terminals and steps between bold and
  dim at lower colour depths; the glow needs truecolor. `WISP_TUI_MOTION=off`, `NO_COLOR`, and a
  terminal in the background keep everything still.
- An edit card's diff shows the file's real line numbers, live and in resumed sessions, and each
  edit's header names the line it starts at. Edits saved before this release keep numbering each
  fragment from 1.
- Paste a screenshot, or paste or drag an image file's path, into the composer to attach it; each
  shows as a chip such as `Image 1 · 640×480`, Backspace in an empty draft removes the newest
  one, and the sent prompt shows a `[image 640×480 png]` line for each. Attaching is refused with a
  notice when the model does not take images or the images would not fit one request. A queued
  message popped back into the composer, or a prompt forked to edit, returns with its images. A tool
  card whose result returned images ends its action row with the same label, or `[2 images]`.
  (#942, #943, #948)
- The TUI detects the terminal's colour depth and converts each frame to 256 or 16 colours where
  truecolor is missing, such as older macOS Terminal.app and the Linux console, moving text that
  would become hard to read to the nearest readable colour. `WISP_TUI_COLORS` (`auto`,
  `truecolor`, `256` or `16`) overrides the detection. (#925)
- The terminal title follows the run (`Wisp · working · <folder>`, `needs approval`, `needs trust`,
  `error`, or `Wisp · <folder>`) and is restored on exit. When a run finishes, fails, or waits for
  approval or trust while the terminal is in the background, Wisp sends a notification (OSC 9);
  `WISP_TUI_NOTIFY=bell` rings the bell instead and `off` sends nothing. A pending approval's border
  breathes while it waits. (#927)

### Prompt

- When neither the project root nor any folder down to the working directory has an `AGENTS.md`
  or `CLAUDE.md`, the project context now says so and tells the model not to search those folders
  or any above the project root; a subfolder below may still have its own. Without it, models
  went looking for `AGENTS.md` in the parent folder, outside the working directory, and hit a
  tool error in about half of the tool-use eval runs.

### Tools

- A finished `bash` command's long output is now shown as its first 64 lines (up to 4,000 bytes)
  and its last 192 lines (up to 8,000 bytes), with a `[truncated: N bytes omitted after the first
  K lines]` line between them, instead of its last 50,000 bytes. A build's first error, often
  printed early, is now visible without `read_output`. In the tool-use evals this cut tokens per
  run by 46–75% on the two long-output tasks, with every run still passing. Wisp's sessions save
  the full output for `read_output` whenever a result leaves anything out, which now happens from
  about 12 KB; an embedding that builds `BashTool` without an output store has no saved copy.
  Polls of started processes and timeouts keep showing the end. The result's `stdout` and `stderr`
  data fields and the `ToolResultReady` event are unchanged.
- An `edit` that replaces the start of a file with a UTF-8 byte-order mark keeps the mark. `read`
  shows the mark as part of the first line, so an `oldText` copied from it matched the mark and a
  `newText` without it removed it. Matching is unchanged, and a U+FEFF elsewhere is still text.
  (#946)

### Build

- The Rust TUI moves to ratatui 0.30.2 and crossterm 0.29, and building it from source needs Rust
  1.88 or later (was 1.85). Published wheels are unaffected. (#922)

### Skills

- The bundled `wisp-development` skill, now version 1.6, covers MCP servers (user-only settings,
  default `command` safety, on-demand exposure, and confirmed proposals), the `ask`/`yolo`
  permission modes and plan mode, the stale-write check, the full set of always-protected files,
  and where each layer's code lives. It also names `ProviderResponseLifecycle` by its current name.

<!-- Keeps links to the pre-release "0.6.1 — Unreleased" heading working. -->
<a id="061--unreleased"></a>

## 0.6.1 — 2026-11-08

0.6.1 is a patch release on 0.6.0 for the terminal UI. It shows the effort level a model uses by
default, scrolls footer notices that do not fit, and gives each fact one place on screen instead of
repeating it in the main column and the side panel. It adds one optional field to the model catalog
report and changes no command, handshake, session, settings, credential, or trust format, so 0.6.0
and 0.6.1 read each other's sessions. The backend and frontend must come from the same release.

### Events

- `RpcModelCatalogEntry` (in the `rpc.model_catalog` event) gains an optional `default_effort`: the
  effort level the provider applies when a request names none, for models whose documentation states
  it. Wisp still sends no effort unless one is chosen. The schema bundle and conformance fixture are
  regenerated; the same-release rule applies, and a 0.6.0 reader rejects the new field. (#908)

### Providers

- The provider catalog gains an optional per-model `[providers.default_effort]` table, filled from
  each provider's documentation and checked against the model's `effort_levels` when the catalog
  loads. (#908)

### Terminal UI

- With no effort chosen, the composer and footer show the model's documented default effort,
  muted, or `default` when the model has effort levels but no documented default. (#908)
- A footer notice wider than its row, such as the warning that YOLO and trust are not saved in the
  home folder, scrolls as a ticker instead of being cut off. (#908)
- The docked side panel no longer repeats what the main column shows: its "Now" section and
  idle/running label are gone, and the run status line leaves out `ctx` while the panel shows
  context, unless context is running low. The welcome screen drops two lines that repeated the
  composer placeholder and the command hint. (#909)
- YOLO shows on the composer line, so it stays visible when the panel is hidden, and the panel's
  key hints join the main footer's single row. (#910)
- The side panel folds healthy MCP servers and skills to their heading. MCP opens by itself when a
  server disconnects or the list can't be read, and skills open once one is used or the catalog
  reports diagnostics. Recent turns leaves out the latest prompt, which the transcript pins above
  its reply. (#911)
- A turn's summary row names the mode, model, and effort only on the first turn and when they
  change, instead of repeating the composer line on every turn. (#912)

<!-- Keeps links to the pre-release "0.6.0 — Unreleased" heading working. -->
<a id="060--unreleased"></a>

## 0.6.0 — 2026-11-07

0.6.0 is a minor release about long-running commands: `bash` now saves the output its result has
to cut, so the model can read an early error instead of running the command again, and the
terminal UI shows a command's output while it runs. It adds one transient event,
`tool.execution.progress`, and one read-only tool, `read_output`. It changes no command,
handshake, session, settings, credential, or trust format, so 0.5.x and 0.6.0 read each other's
sessions. The backend and frontend must come from the same release, which matters more than usual
here: a 0.5 terminal UI does not recognise the new event.

### Events

- New transient event `tool.execution.progress` (`ToolExecutionProgress`): a snapshot of a running
  call's output, with a per-call `sequence`, `elapsed_ms`, the last 4 KiB and 20 lines of stdout and
  stderr, and byte totals. It arrives only between the call's `tool.execution.started` and
  `tool.execution.ended`; each snapshot replaces the previous one, so consumers may skip any of
  them. It is not persisted. JSON output mode, RPC, and the SDK include it; text print mode does
  not show it. `CodingSession.run()` takes an optional `live_event_sink` to receive it. The schema
  bundle and conformance fixture are regenerated; the same-release rule applies.

### Tools

- A one-shot `bash` command that runs longer than half a second now reports its output while it
  runs. The TUI shows the latest line beside the running card and the last lines of both streams
  when it is expanded. Updates come at most four times a second and only when output changed; a
  frontend that falls behind skips to the newest output instead of slowing the command. Progress
  never reaches the model and is not saved in the session. (HY-432)
- When a `bash` result has to cut output, the full stdout and stderr are saved and the result
  names an `output_id`. The new read-only `read_output` tool pages through saved output, so the
  model no longer reruns a command to see an error printed early. A poll of a started process
  that dropped output, and a timed-out command, report saved output the same way; a timeout error
  also shows the last lines of output. Saved output is private to the user, bounded (8 MiB per
  stream, 64 MiB and 32 streams per runtime), and deleted when Wisp exits. `read_output` is
  exposed whenever `bash` is. No event, command, or session format changes. (HY-434)

### Terminal UI

- When a reply gets shorter, for example when the final text replaces a longer streamed draft, the
  view no longer jumps to the reply's first row; it stays on the last row. (#896)

### Python API

- `CodingSession.run()` gains an optional `live_event_sink` that receives `ToolExecutionProgress`
  events, and `ToolContext` gains an optional `progress` handle that tools report through.
  `ProcessSupervisor` gains an `output_store` (and an optional `output_store=` argument), and
  `builtin_tools()` includes `ReadOutputTool`. Existing calls keep working unchanged. (#905, #906)

<!-- Keeps links to the pre-release "0.5.0 — Unreleased" heading working. -->
<a id="050--unreleased"></a>

## 0.5.0 — 2026-10-27

0.5.0 is a minor release that hardens the built-in tools: it closes two ways credentials or
protected files could reach the model, stops commands Wisp starts from outliving it, and bounds
searches and file operations on very large trees and files. It changes no command, event,
handshake model, or session format, so 0.4.x and 0.5.0 read each other's sessions; the backend and
frontend must still come from the same release. It is a minor release because tool behavior
changes in ways callers can observe; the most visible are listed under Tools.

### Security

- Protected-path matching follows case-insensitive filesystems, which fold more than lowercasing
  does: on APFS `credentialſ.json` opens `credentials.json`, and `read` no longer reaches it.
  Composed and decomposed accents count as the same name, and `ls` now hides protected names as
  `grep` and `find` already did. (#849)
- `bash` commands no longer inherit the environment variables Wisp reads provider API keys from,
  including a configured OpenAI-compatible endpoint's variable, so a command cannot print them
  into the transcript. Your other exported variables still reach commands. (#850)

### Tools

- `write` and `edit` refuse an existing file the model has not read in this conversation, or one
  that changed since it did, so a hand edit or a regenerated file is no longer silently
  overwritten. The model is told to read the file first; new files need no read. (#856)
- Leaving a session (a new session, or selecting, cloning, or forking another one) stops the
  background processes started with `bash operation=start`. (#864)
- `grep`, `find`, and `ls` stop after 30 seconds and return what they found, marked incomplete.
  Searches enter at most 100 directory levels and say how many deeper directories they skipped.
  (#858, #863)
- `bash` bounds its waits: a one-shot `timeout` of at most an hour, `lifetime_seconds` of at most a
  day, and `yield_seconds` and `wait_seconds` of at most 10 minutes. When every process slot is
  taken, the error lists the processes holding them so the model can poll or cancel them. (#857)
- `edit` refuses files over 16 MiB and suggests `bash` instead. `read` no longer loads a whole long
  line before applying its output limit, so reading a huge single-line file stays small in memory.
  (#863)
- `write` refuses a target that is not a regular file, such as a FIFO, instead of blocking or
  replacing it. A replacement for an existing file is created private (`0600`) until it takes the
  file's mode, and the directory is synced after the rename so the write survives a crash. A path
  containing a NUL byte is an argument error in every file and search tool. (#859)
- `grep` skips files it cannot read, such as iCloud placeholders or files without read permission,
  and files with a line over 1,000,000 characters, listing them in a notice instead of failing the
  whole search. Searching such a file directly names it in the error. (#852, #860)
- `grep` and `find` started in a subfolder of a repository apply the repository's ignore files
  from its root down, as Git does. A repository at the home folder or above is never used. (#851)

### Processes

- A command that daemonizes (double-forks, calls `setsid`, and redirects its output) is stopped
  when the command ends, as other background processes already were, and no longer keeps `cancel`,
  lifetime expiry, or shutdown waiting on its open pipes. (#853, #854)
- The backend stops its background processes when it receives SIGHUP or SIGTERM, such as when the
  terminal closes. If it is killed outright or crashes, a watchdog process stops the commands it
  was running. (#855, #862)

### Terminal UI

- The terminal UI and the SDK wait up to 30 seconds, instead of 5, for the backend to answer the
  handshake, so a slow first start no longer fails. (#848)
- A key pressed on a prompt-history choice is no longer ignored when the background reads that
  follow a prompt finish just before it. (#869)

### Python API

- `ToolContext` gains `withheld_environment`, the variables `bash` leaves out of a command's
  environment, and `seen_files`, the record that guards `write` and `edit`. A bare
  `ToolContext(cwd=...)` withholds the provider key variables but leaves `seen_files` unset, so
  tools an embedding calls directly keep their earlier write behavior;
  `ToolContext.from_config` enables both. (#850, #856)

<!-- Keeps links to the pre-release "0.4.0 — Unreleased" heading working. -->
<a id="040--unreleased"></a>

## 0.4.0 — 2026-10-25

0.4.0 is a minor release focused on the built-in tools: faster `grep` and `find`, file tools that
cope better with real files, and a more dependable `bash`. It changes no command, event, handshake
model, or session format, so 0.3.x and 0.4.0 read each other's sessions; the backend and frontend
must still come from the same release. Code that sets `ToolContext`'s operation write fields must
migrate; see Python API below.

### Tools

- `bash` runs commands in bash (`/bin/bash`, then `bash` on `PATH`, falling back to `/bin/sh`)
  instead of `/bin/sh`, so `[[ ]]`, arrays, and `pipefail` work on Debian and Ubuntu. (#827)
- One-shot `bash` keeps the end of stdout and stderr separately instead of letting long stdout push
  out stderr, starts commands with stdin from `/dev/null`, returns shortly after the shell exits,
  and tells the model when it stopped background processes that still held the output. (#819,
  #822)
- Process cleanup on a busy machine no longer gives up before killing the process group, which had
  ended runs as "Failed to terminate process tree" and could leave processes running. (#830)
- `edit` matches LF edits in CRLF files, tolerates trailing whitespace the model did not copy,
  names the failing edit and whether it matched nothing or several places, reports indentation
  mismatches, and rejects edits that change nothing. (#821, #826)
- `read` ends a cut-short read with the offset to continue from, rejects an offset past the end,
  and suggests how to get past a line longer than the output limit. (#819)
- `read`, `grep`, `find`, and `ls` follow a symbolic link whose target stays inside the workspace
  and passes the protected-path check, which fixes monorepo and pnpm layouts; a symlink loop no
  longer crashes. `write` and `edit` still refuse links and name the target to write instead.
  Links found while walking a directory are still skipped. (#829)
- `grep` clips matched and context lines to 500 characters, and `grep` and `find` say how to see
  more when they stop at `max_results`. (#823)
- An ignore-file line that cannot be translated, such as `foo\`, no longer fails the whole search,
  and an invalid glob argument is reported as an argument error. (#839)
- The MCP credential file stays protected even when `protected_paths` is emptied. The docs now
  state that protected paths bind only the file tools, not `bash`, MCP, or extension tools. (#825)

### Performance

- With the native extension, `grep` and `find` walk directories in Rust, including gitignore
  matching ported from pathspec, and run in parallel within a tool batch. `find` on Wisp's own
  repository drops from about 114 ms to 29 ms. (#840, #841, #842)
- Python `grep` (regex, ignore-case, and literal without the native scanner) searches each block
  of lines once before its individual lines, splits lines with `str.splitlines`, and calls the
  regex module more cheaply; the last two changes alone take a regex search with no match over
  Wisp's `src/` from about 120 ms to 45 ms. Concurrent regex greps no longer slow each other
  down several times over. (#833, #842, #843, #844)
- The walk checks protected paths once per directory, `find` no longer resolves each file again,
  and the native literal scanner searches whole chunks instead of each line. (#834, #836, #837)
- A `bash` call on macOS skips a final job sample that cannot find anything, about 16 ms per
  call. (#835)

### Python API

- `ToolContext`'s `allowed_write_paths`, `conflicting_write_paths`,
  `require_create_only_writes`, `require_non_empty_writes`, and `create_only_write_receipt` are
  replaced by one `write_constraint`, a `WriteConstraint` from `wisp.tools.context` with
  `allowed_paths`, `conflicting_paths`, `create_only`, `non_empty`, and `receipt`. The fields
  were undocumented and only `/init` set them, so they are removed without a deprecation period;
  see the [0.4 compatibility notes](site/src/content/pages/reference/compatibility.md#wisp-04-compatibility).
  A `ToolContext` built from a working directory, budgets, and protected paths is unaffected.
  (#845)
- `wisp.agent.harness`, which is not a supported namespace, renames `SimpleCancellationToken` to
  `RunCancellationToken` and `queue_full_error` to `queue_full_message`, moves
  `ToolResultProcessingError` to `wisp.coding.tool_execution`, and no longer re-exports
  `QueueKind`; import it from `wisp.events`. (#815)

### Documentation

- Reorganized navigation and refreshed styling on the documentation site, and the README banner
  and homepage now show the terminal UI. (#820, #838)

<!-- Keeps links to the pre-release "0.3.1 — Unreleased" heading working. -->
<a id="031--unreleased"></a>

## 0.3.1 — 2026-10-23

0.3.1 is a patch release on 0.3.0 with one terminal UI fix and the documentation site cutover.
It changes no command, event, handshake model, or session format, so 0.3.0 and 0.3.1 read each
other's sessions; the backend and frontend must still come from the same release.

### Terminal UI

- The run status line shows "Compacting context" while an automatic compaction runs, whether the
  context reached the compaction threshold or overflowed. Previously it kept showing "Working" or
  "Thinking" until compaction finished; manual `/compact` already showed it. (#810)

### Documentation

- The documentation site now builds with Astro instead of mdBook, with Pagefind search, user-first
  navigation, refreshed homepage and section entry pages, a published changelog, and an archive of
  maintainer release records. The TUI guide is split into focused pages, and SDK task guides are
  separate from the API reference. The `/Wisp/` base path, print page, and older page URLs still
  work. (#803, #804, #805, #806, #807, #808, #809)

<!-- Keeps links to the pre-release "0.3.0 — Unreleased" heading working. -->
<a id="030--unreleased"></a>

## 0.3.0 — 2026-10-22

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
[0.3 upgrade guide](site/src/content/pages/guide/upgrading-to-0.3.md) for commands and
compatibility details.

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
