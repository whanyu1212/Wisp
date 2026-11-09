---
title: Inside Wisp's built-in tools
description: How Wisp's read, write, edit, bash, grep, find, ls, and read_output tools work at the operating-system level, from path checks to process cleanup.
route: crafting-agents/02-tools/built-in-tools.html
section: learn
order: 1
layout: course
listed: true
searchable: true
navTitle: Inside the built-in tools
parent: crafting-agents/02-tools.md
---

# Inside Wisp's built-in tools

[Chapter 2](../02-tools.md) built three narrow tools for a trusted fixture. Wisp's
eight built-ins (`read`, `write`, `edit`, `bash`, `grep`, `find`, `ls`, and
`read_output`, which pages through output `bash` saved) take several thousand lines of Python, plus optional Rust kernels. Very little of that
is the schema the model sees. Most of it answers questions a schema cannot: which
file a path really names, what happens if the file changes while we read it, and
who stops a process that a command left running in the background.

This page follows each tool from the call the model sends to the observation it
gets back. It is optional on a first read: [chapter 3](../03-context.md) does not
depend on it. Sections 1 and 2 cover machinery every tool shares; the remaining
sections can be read in any order. Each tool section opens with that tool's calls
from chapter 2's built-in tools [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), so you can see what it returns before
reading how it works.

> **About the code on this page.** Unlike the chapters, whose listings are
> included from runnable checkpoint files, the snippets here are **shortened
> excerpts** of Wisp's source under `src/wisp/tools/`. Error handling, Windows
> branches, and bookkeeping are left out, and `# ...` marks omitted code. They
> are written to explain the mechanism and are not meant to run on their own.
> Each section links the full source; where an excerpt and the source differ, the
> source is authoritative.

## 1. What every tool shares

### The protocol

Every tool, built-in or not, implements one small protocol from
[`tools/base.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/base.py):

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: Mapping[str, object]  # JSON Schema sent to the provider
    safety: ToolSafety                  # "read", "mutating", or "command"

    async def run(self, arguments: Mapping[str, object], context: ToolContext) -> ToolResult: ...
```

`name`, `description`, and `input_schema` form the model-facing contract from
[chapter 2](../02-tools.md#1-a-tool-has-two-contracts). `safety` and `run` are host-facing: the model never sees the safety
category and cannot change it. `builtin_tools()` in `tools/builtin.py` simply
returns one instance of each class:

| Tool | Safety | May run in parallel with other calls | Where the blocking work happens |
| --- | --- | --- | --- |
| `read` | `read` | Yes | Worker thread |
| `ls` | `read` | Yes | Worker thread with a deadline |
| `grep`, `find` | `read` | Only when the native directory walk is installed | Worker thread with a deadline |
| `write`, `edit` | `mutating` | No | Worker thread |
| `bash` | `command` | No | Child processes, watched by asyncio |
| `read_output` | `read` | Yes | Worker thread |

Parallel safety is registered next to the tools, in `extensions/builtin.py`, not
in the schema. `grep` and `find` only read, but the Python directory walk holds
Python's global interpreter lock (GIL), so a batch of them ran *slower* in
parallel than one at a time. The Rust walk releases the lock, so they are marked
parallel-safe only when it is available.

All filesystem work runs through `anyio.to_thread.run_sync`. A system call such
as `open` or `read` blocks the thread that makes it, and blocking the event-loop
thread would freeze event streaming to the TUI and every other concurrent task.

### What happens before `run`

`ConfiguredToolExecutor.prepare` in `coding/tool_execution.py` runs the checks
from chapter 2 in a fixed order. None of them call the tool:

```python
if tool_call.parse_error is not None:  # the arguments were not valid JSON
    outcome = _failure_outcome(..., failure_code="invalid_arguments", retryable=True)
else:
    tool = self._registry.get(tool_call.name)  # an unknown name becomes "unknown_tool"
    if not self._policy.allows(tool):          # ToolPolicy: may this tool run at all?
        outcome = _failure_outcome(..., failure_code="policy_denied")
    elif self._approval_policy.requires_approval(tool) and not self._approval_policy.approves(tool):
        yield ToolApprovalRequested(...)       # the TUI or RPC client asks the user
        decision = await self._approval_policy.await_approval(...)
        yield ToolApprovalResolved(...)
        if decision.approved:
            yield self._prepare_tool(tool_call, tool, arguments)  # wraps tool.run for later
            return
        outcome = _failure_outcome(..., failure_code="approval_denied")
    else:
        yield self._prepare_tool(tool_call, tool, arguments)
        return
yield self._prepare_outcome(tool_call, outcome)
```

By default, `mutating` and `command` tools require approval and `read` tools do
not. `_prepare_tool` returns a closure rather than running the tool, so a whole
batch of calls can be approved before any of them has a side effect.

### Arguments are checked again inside the tool

The JSON Schema asks the provider for a shape, but not every provider enforces it,
and custom executors can call tools directly. Each tool therefore checks the
values it reads with small helpers from `tools/common.py`:

```python
def _optional_int(arguments, name, *, default=None):
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):  # bool is a subclass of int
        raise ToolArgumentError(f"{name} must be an integer")
    return value
```

The `bool` check matters: in Python `isinstance(True, int)` is true, so without it
`{"offset": true}` would read from line 1. `ToolArgumentError` carries the
`invalid_arguments` failure code, `retryable=True`, and a recovery hint.

### Results, errors, and output budgets

A tool returns a `ToolResult(text, data, truncated, images)`. `text` is what the
model reads. `data` holds structured fields for interfaces, such as an exit code or
the text a file had before a write, which the TUI uses to draw a diff. `images`
holds complete image files, such as the one `read` returns for a PNG. The executor
validates each one and stores it in the session's image store before the result is
published, so the result carries only a reference (`ImagePart`) that already
resolves.

A tool reports an expected failure by raising `ToolError`. The executor turns it
into an observation with the message, failure code, and recovery hint. Any
*other* exception becomes the fixed text `Tool execution failed`:

```python
try:
    result = await tool.run(arguments, self._context)
except Exception as exc:
    return _tool_exception_outcome(exc)  # ToolError: its message; anything else: a generic one
```

That keeps an unexpected exception message, which might contain internal paths
or state, out of the provider request.

Output is bounded by `ToolContext.max_output_bytes` and `max_output_lines`
(50,000 bytes and 2,000 lines by default). `tools/truncation.py` has two
helpers that count both limits and fit a `[truncated]` marker inside the budget:

- `truncate_text` keeps the **head**. It suits files and listings, where the
  model continues from where the output stopped.
- `truncate_text_tail` keeps the **tail**. It suits process output, where the
  error, traceback, or summary usually comes last.

`ToolContext` is the host-owned half of each call: the working directory, output
budgets, protected-path patterns, optional write constraints, environment
variables to withhold from commands, and the record of which files the model has
seen. None of these values come from the model.

## 2. From a path string to an open file

Every file and search tool turns the model's `path` string into an open file in
the same two stages, both in
[`tools/files/secure_fs.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/files/secure_fs.py):
a policy check on the string, then an open that cannot be redirected.

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), both stages stop a read of the project's `.env` file before
anything is opened:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:read-protected}}
```

### Stage 1: check the string

```python
def secure_tool_path(selected, context, *, default=".", write=False, follow_links=False):
    raw = selected if selected is not None else default
    if "\0" in raw:
        raise ToolArgumentError(...)  # the C open() call would silently cut the path here
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    # Lexical: collapses "a/../b" without asking the filesystem about links.
    absolute = Path(os.path.abspath(os.path.normpath(candidate)))
    if not context.allow_outside_cwd:
        absolute.relative_to(cwd)  # ValueError becomes "path_outside_workspace"
    if is_protected_path(absolute, context):
        raise ToolError(f"Access to protected path denied: {raw}")
    # ... a write constraint may limit writes to an exact list of paths
    if (follow_links or write) and os.name != "nt":
        resolved = Path(os.path.realpath(absolute))
        if resolved != absolute:  # some component is a symbolic link
            _check_resolved_target(resolved, raw, cwd, context)  # same two checks again
            if write:
                raise ToolError(f"Path goes through a symbolic link: {raw}", ...)
            absolute = resolved
    return SecureToolPath(absolute, raw, display)
```

Both the path as written and the path after following links must stay inside the
working directory and avoid protected patterns. A link named `notes.txt` that
points at `~/.ssh/id_rsa` fails the second check. Reads continue with the
resolved path. Writes refuse links, with a hint naming the target: the atomic
replace described in section 4 would replace the link itself rather than the file it
points to.

Protected patterns from `tools/files/paths.py` come in two forms. A bare pattern
such as `.env` or `*.key` matches a file name at any depth. A pattern containing
`/`, such as `.wisp/auth.json`, matches the end of the path. Matching ignores
case using Unicode case folding and normalization, because case-insensitive
filesystems such as APFS open `.ENV` as `.env`.

### Stage 2: open without following links

A checked string is not yet safe to pass to `open()`. Between the check and the
open, another process can replace `src` with a symbolic link to `/etc`, and a
plain `open("/repo/src/app.py")` would follow it. This is a
**time-of-check to time-of-use** (TOCTOU) race. Wisp closes it by opening one path
component at a time, each relative to the directory it already holds open:

```python
_TRAVERSAL_FLAGS = O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC  # O_RDONLY without O_PATH

@contextmanager
def open_parent(path, *, create=False):
    current = os.open("/", _TRAVERSAL_FLAGS)
    try:
        for part in parts[:-1]:  # every directory, not the final name
            if create:
                os.mkdir(part, 0o777, dir_fd=current)  # FileExistsError is ignored
            child = os.open(part, _TRAVERSAL_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        yield OpenParent(path, current, parts[-1])  # a held directory plus the leaf name
    finally:
        os.close(current)
```

Each flag does one job:

- `dir_fd=` makes Python call `openat(2)`, which looks up `part` inside an
  already-open directory. A directory descriptor keeps referring to the same
  directory even if it is renamed, so earlier components cannot be swapped.
- `O_NOFOLLOW` makes the open fail with `ELOOP` when the component is a symbolic
  link. `O_DIRECTORY` makes it fail with `ENOTDIR` when it is not a directory.
- `O_PATH` (Linux) opens a directory only for lookups, without needing read
  permission. `O_CLOEXEC` keeps the descriptor from leaking into commands that
  `bash` starts later.

The leaf is then opened with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` relative to the
parent, and `fstat` must report a regular file. `O_NONBLOCK` matters because
opening a named pipe (FIFO) for reading would otherwise wait forever for a
writer. On platforms without these primitives Wisp denies access rather than
falling back to plain path opens; Windows instead holds a handle on every ancestor
directory and rejects junctions.

### File versions: read before you write

To notice that a file changed, Wisp compares five fields from `stat`:

```python
def file_version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
```

Device and inode numbers identify the file itself, so replacing it with a new file
changes them. Writing to it changes the size or modification time. The change
time (`ctime`) is updated by the kernel on any change and, unlike `mtime`, cannot
be set back with `touch`.

`SeenFiles` in `tools/seen_files.py` maps each file's real path to the version the
model last read or wrote. `write` and `edit` call `require_current` before
changing an existing file. A file never read fails with `Read <path> before
changing it`; a file whose version differs fails with `<path> changed since it was
last read`. Both use the retryable `stale_input` code. This prevents the model from
overwriting a change made by the user or a formatter that it has not seen.
Creating a new file needs no prior read.

## 3. `read`

Arguments: `path`, plus optional 1-indexed `offset` and `limit` in lines.

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project):

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:read-page}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:read}}
```

The tool resolves the path with `follow_links=True` and reserves part of the
output budget for a continuation notice. Then, in a worker thread, it opens the
file as in section 2, records its version, reads the requested slice, and records the
version again. If the two versions differ, the file changed during the read and
the text may mix both versions, so the read is not recorded as seen. A partial
read does count as seen.

The slice is read by `_read_line_slice` in
[`tools/files/operations.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/files/operations.py):

```python
line_limit = max_bytes + 1  # never hold more than one budget's worth of a line
with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as file:
    while line := file.readline(line_limit):
        if not line.endswith("\n") and not (line.endswith("\r") and len(line) < line_limit):
            _read_past_line(file, line, line_limit)  # discard the rest of a huge line
        scanned_count += 1
        if scanned_count < offset:
            continue  # stream past earlier lines without keeping them
        if limit is not None and len(selected_parts) >= limit:
            return stop_before_eof(truncated=False)  # one line of lookahead: more follows
        if len(selected_parts) >= max_lines:
            return stop_before_eof(truncated=True)
        encoded_line = line.encode("utf-8")
        if selected_bytes + len(encoded_line) > max_bytes:
            if selected_parts:
                return stop_before_eof(truncated=True)  # the next page starts here
            return _partial_line_slice(line, scanned_count, max_bytes - notice_bytes)
        selected_parts.append(line)
        selected_bytes += len(encoded_line)
return _ReadSlice("".join(selected_parts), scanned_count, len(selected_parts), False)
```

Some details are easy to miss:

- `os.dup` gives the text wrapper its own copy of the descriptor, so closing the
  wrapper leaves the secured descriptor for the caller to close.
- `newline=""` turns off Python's newline translation, so `\r\n` line endings
  reach the model as they are in the file.
- `readline(line_limit)` stops after `line_limit` characters. A one-line minified
  bundle is never loaded whole; only one budget's worth is kept.
- Reading from `offset=5000` still reads the first 4,999 lines, but discards each
  as it goes. Time grows with the offset; memory stays bounded by the page.
- `stop_before_eof` drops trailing lines until the continuation notice fits, so a
  final truncation can never cut off the instruction that says where to resume.

The notice depends on why the read stopped:

```text
[truncated] Output limit reached. Use offset=1834 to continue.
[More lines follow. Use offset=201 to continue.]
[truncated] Line 12 is longer than the output limit. Use offset=13 to skip it, or read it in parts with bash, for example: sed -n '12p' bundle.min.js | cut -c1-49731
```

The total `line_count` is reported only when the scan reached the end of the file.
An offset past the end fails with the valid range, and a file that is not valid
UTF-8, including most binary files, fails with `File is not valid UTF-8`.

### Image files

Before decoding anything as text, `read` looks at the file's first 16 bytes on the
same secured descriptor. A PNG, JPEG, GIF or WebP signature makes it an image read:

- The tool reads at most one byte past the 7.5 MB image limit, so a huge file costs
  no more than that, and refuses a file over the limit.
- `wisp.images.inspect_image` checks the format, the header's dimensions (at most
  8000 px a side) and the file's structure without decoding any pixels. A file that
  declares a huge size to exhaust memory when decoded is refused from its header.
- The text the model reads names the file, its format, its size in pixels and its
  bytes. The image itself travels as `ToolResult.images`. `offset` and `limit` do
  not apply and are ignored, with a note saying so.
- A BMP, TIFF, HEIC or AVIF file is refused with a message naming its format and
  suggesting a conversion, rather than failing as text that is not UTF-8.

Wisp does not resize images. Validating headers needs no image library, which keeps
installation free of native dependencies such as Pillow, and the providers that
take images scale large ones down themselves. The 8000 px and 7.5 MB bounds are
the tightest limits among those providers.

How the image reaches the model depends on the provider. Anthropic, the OpenAI
Responses API, Codex and xAI take it inside the tool result. Chat Completions APIs
accept images only from the user, so the image follows the batch's tool messages in
one user message that says which tool call returned it and that it is tool output,
not an instruction. Gemini 3 and later take it inside the function response; earlier
Gemini models get it as a part of the same turn. A model that does not accept
images, or a request whose image budget is spent, gets a short note naming the
image instead. When a later session replays the call as a summary rather than as
native tool history (Anthropic and Gemini do), the summary names the image but does
not carry it; read the file again to see it.

## 4. `write`

Arguments: `path`, `content`, and `overwrite` (default `true`; `false` fails if
the file exists).

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), `write` creates `docs/` on the way, then refuses to replace the
file it just made:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:write}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:write-exists}}
```

Writing to a file in place has a flaw: a reader, or a crash, can catch the file
half-written. `write` instead builds the new content in a temporary file beside
the target and swaps it in with one `rename`. This is `_atomic_write` on POSIX:

```python
with open_parent(path, create=True) as parent:  # creates missing directories as it walks
    initial = stat_leaf(parent)  # lstat relative to the parent; None when absent
    if initial is not None and stat.S_ISLNK(initial.st_mode):
        raise ToolError(f"Symbolic links are not allowed: {path.selected}")
    if not overwrite and initial is not None:
        raise ToolError(f"File already exists: {path.selected}")
    if initial is not None and not stat.S_ISREG(initial.st_mode):
        raise ToolError(f"Not a regular file: {path.display}")  # FIFO, socket, device
    if initial is not None:
        descriptor = _open_existing(parent, initial)  # O_NOFOLLOW; must match the lstat
        metadata = _snapshot_replacement_metadata(descriptor, opened)  # mode, owner, xattrs
        before_text = _snapshot_descriptor(descriptor)  # for the diff; at most 1M characters
        initial_version = file_version(opened)
        context.seen_files.require_current(path.path, initial_version, display=path.display)
        if link_count > 1:
            return _write_existing_in_place(...)  # keep the inode other names share

    temporary, written = _write_temporary(parent, content, metadata=metadata)
    try:
        current = stat_leaf(parent)
        if (None if current is None else file_version(current)) != initial_version:
            raise ToolError(f"File changed while writing: {path.selected}")
        if overwrite:
            os.replace(temporary, parent.leaf, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
        else:
            os.link(temporary, parent.leaf, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
        published = True
    finally:
        _cleanup_temporary(parent, temporary, published=published)  # remove the temporary name
    _sync_directory(parent)
```

`_write_temporary` creates the file it writes to:

```python
flags = O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC
mode = 0o600 if replacing_existing else 0o666
descriptor = os.open(f".wisp-write-{uuid4().hex}", flags, mode, dir_fd=parent.fd)
# ... write the content, flush, copy owner, mode, and extended attributes, then:
os.fsync(file.fileno())
```

Each step closes a specific gap:

1. **Same directory.** `rename(2)` is atomic only within one filesystem. A
   temporary file in the target's own directory is always on the same one.
2. **`O_CREAT | O_EXCL`** fails if the name already exists, so Wisp never writes
   into a file someone planted under the temporary name. A random name is retried
   up to ten times.
3. **Mode `0600` first.** When replacing a private file, the new content is
   readable only by its owner until the original permissions are copied over.
4. **`fsync` before the rename.** Without it, a crash could leave the target name
   pointing at a file whose data never reached the disk.
5. **Version check, then publish.** If another process changed the target since
   it was read, the write fails rather than overwriting that change. This narrows
   the window to the moment between the check and the rename, but does not remove it.
6. **`os.replace`** calls `rename(2)`: another reader sees either the old file or
   the new one, never a mixture. For `overwrite=false`, **`os.link`** gives the
   temporary file the target name only if that name is still free; it fails with
   `EEXIST` otherwise, so two writers cannot both create the file.
7. **`fsync` on the directory.** The rename changes a directory entry, so the
   directory itself is synced to make the new name survive a crash. Filesystems
   that cannot sync a directory are skipped.

There is a deliberate exception. If the file has several hard links, replacing it
would detach this name from the others, so Wisp truncates and rewrites the
existing inode in place. It does the same when permissions prevent creating the
temporary file. That fallback is not atomic, which the
[tool-boundary case study](../case-studies/tool-boundary.md) records as a trade-off.

Afterward, the new version is recorded as seen only if the file at the path still
has the device, inode, size, and modification time of the file just written.
Another process may have replaced it immediately after the rename. The result is
`Wrote 42 bytes to src/app.py`, with `created` and `before_text` in `data`.

## 5. `edit`

Arguments: `path` and a non-empty list of `edits`, each with `oldText` and
`newText`.

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), the first call fails because the file has not been read, the
second because its `oldText` is ambiguous, and the third succeeds:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:edit-unread}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:edit-ambiguous}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:edit}}
```

`_atomic_edit` opens the existing file as `write` does, requires the seen version,
and refuses files larger than 16 MiB, because the old text, the new text, and
their encodings are all held in memory at once. It reads the whole file with
`newline=""`, computes the replacement, and publishes it with the same
temporary-file and rename sequence. The interesting part is `_apply_edits`:

```python
replacements = []
for index, (old_text, new_text) in enumerate(edits):
    old_text, new_text = _match_line_endings(original, old_text, new_text)  # LF to CRLF if needed
    start, end, new_text = _locate_edit(original, old_text, new_text, index)
    replacements.append((start, end, new_text, index))
replacements.sort(key=lambda replacement: replacement[0])
for previous, current in pairwise(replacements):
    if current[0] < previous[1]:
        raise ToolArgumentError(f"edits[{first}] and edits[{second}] overlap")
parts, cursor = [], 0
for start, end, new_text, _index in replacements:
    parts.extend((original[cursor:start], new_text))  # splice once, left to right
    cursor = end
parts.append(original[cursor:])
edited = "".join(parts)
if edited == original:
    raise ToolError("The edits leave the file unchanged", ...)
```

Every edit is located in the **original** text, not in the result of earlier
edits. Order in the list therefore does not matter, and one edit cannot match text
that another just inserted. A file with Windows `\r\n` endings accepts edits
written with `\n`, since the model cannot see the difference, and lines outside
the edits keep their exact bytes.

`_locate_edit` tries progressively looser matches, but loosens only what is safe:

```python
occurrences = _find_occurrences(original, old_text)  # every start, overlapping ones too
if len(occurrences) == 1:
    return occurrences[0], occurrences[0] + len(old_text), new_text
if len(occurrences) > 1:
    raise _ambiguous_edit_error(index, len(occurrences))  # "matches 3 places; it must match once"

# Whole lines that match once trailing whitespace is ignored.
windows = _matching_line_windows(original, file_lines, old_lines, str.rstrip)
if len(windows) > 1:
    raise _ambiguous_edit_error(index, len(windows))
if windows:
    # ... replace those whole lines
    return matched[0].start, end, new_text

# Ignoring indentation is only used to explain the failure, never to apply an edit.
indented = _matching_line_windows(original, file_lines, old_lines, str.strip)
if indented:
    raise ToolError(f"edits[{index}].oldText matches line {indented[0] + 1} only if "
                    "indentation is ignored", failure_code="stale_input", ...)
raise ToolError(f"edits[{index}].oldText was not found in the file", failure_code="stale_input", ...)
```

`_find_occurrences` resumes each search one character after the previous match,
so `"aa"` is found twice in `"aaa"` and the ambiguity is reported. Trailing
whitespace is invisible in most output, so forgiving it is cheap. Indentation is
not: in Python or YAML it changes meaning, so a mismatch is reported with a line
number and a hint to copy the exact indentation.

## 6. `bash`

Arguments: `operation` (`run`, `start`, `poll`, or `cancel`; default `run`),
`command`, `timeout` (1 to 3,600 seconds, default 30), `process_id`, and timing
fields for long-running processes.

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), a failing test run reports its exit code, and a started process
returns its first output while it keeps running. A later `poll` reports the exit
code once it has finished:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:bash-fail}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:bash-start}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:bash-poll}}
```

The code lives in
[`tools/shell/`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/shell/):
`tool.py` handles arguments and results, `supervisor.py` owns running processes,
and `process.py` spawns, captures, and stops them. Starting a process is easy;
most of this code handles the processes a command leaves behind.

### `operation=run`, step by step

1. Validate `timeout`. Work longer than an hour belongs in `operation=start`.
2. Build the environment: a copy of Wisp's own, minus the variables in
   `ToolContext.withheld_environment`. That set holds Wisp's provider API keys,
   because a command such as `env` would otherwise print them into the transcript
   and the next provider request.
3. Ask the shared `ProcessSupervisor` to run the command. It refuses when its
   process slots (eight in the runtime) are full.
4. Spawn the process and capture its output under `asyncio.wait_for(timeout)`.
5. On timeout, stop the whole process tree and return a retryable `timeout`
   error. It names where the output so far was saved and shows its last lines;
   its hint says the result is inconclusive.
6. After a normal exit, *still* stop the process tree, in case the command left
   background processes running.
7. Decode the output as UTF-8, replacing invalid bytes, and format the result.
   If the result cannot show all of it, save the full streams and say where.

If the tool call itself is cancelled, for example because the user interrupted
the run, the same tree cleanup runs, shielded from the cancellation, before the
cancellation propagates.

### Spawning a command

This is `_create_shell_process` on Linux, shortened:

```python
command_token = secrets.token_hex(16)
env = {**env, "WISP_COMMAND_TOKEN": command_token}  # inherited by every descendant
jobs_file = tempfile.NamedTemporaryFile(prefix="wisp-jobs-", delete=False)  # background PIDs
exit_pipe_read_fd, exit_pipe_write_fd = _open_posix_exit_pipe()
command = _wrap_posix_shell_command(command, jobs_file)

process = await asyncio.create_subprocess_exec(
    sys.executable, "-c", _POSIX_SUBREAPER_HELPER, command,  # becomes a subreaper, execs /bin/sh
    cwd=str(cwd),
    env=env,
    start_new_session=True,  # setsid(): a new session and process group
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    pass_fds=(jobs_fd, exit_pipe_write_fd),
)
for fd in (jobs_fd, exit_pipe_write_fd):
    os.close(fd)  # from now on only the command's processes hold these
watch_command(WatchedCommand(pid=process.pid, token=command_token, ...))
```

Other POSIX systems use `create_subprocess_shell` with the same session, stdin,
and descriptor settings. The mechanisms, from simplest to most involved:

- **`stdin=DEVNULL`.** In RPC mode, Wisp's standard input carries the JSONL command
  stream; a command that read it would consume messages meant for Wisp. A command
  that prompts for input also sees end-of-file at once instead of hanging.
- **A new process group.** `start_new_session=True` calls `setsid()`, which makes
  the shell the leader of a new process group whose ID equals its PID.
  `os.killpg(pid, signal)` then reaches every process in the group, including each
  stage of a pipeline and the children they start.
- **A child subreaper (Linux).** When a process exits, its children are normally
  re-parented to PID 1 and can no longer be found by walking down from the shell.
  `prctl(PR_SET_CHILD_SUBREAPER)` makes the outer shell adopt them instead.
- **A wrapper script.** The command runs inside a generated wrapper whose shape
  is roughly:

  ```sh
  __wisp_record_loop &   # every 0.25 s: append background and child PIDs to the jobs file
  /bin/bash -c '<signal traps>; eval "<the model'\''s command>"'
  __wisp_status=$?
  kill "$__wisp_recorder_pid"
  exit "$__wisp_status"
  ```

  The command runs under `bash` when it is installed, because models write bash
  (`[[`, arrays, `pipefail`) and `/bin/sh` is `dash` on Debian and Ubuntu. The
  recorder catches processes that leave the process group, such as daemons that
  call `setsid` themselves, which `killpg` would miss.
- **A command token.** Every descendant inherits `WISP_COMMAND_TOKEN`. During
  cleanup, Wisp checks a candidate process's environment for that token, through
  `/proc/<pid>/environ` on Linux or `ps -E` on macOS, before signalling it. A
  fully detached daemon can still be found, and an unrelated process that happens
  to reuse a PID is left alone.
- **An exit pipe.** Every descendant inherits the write end of a pipe that nothing
  ever writes to. A pipe's read end reports end-of-file only after every copy of
  its write end is closed, so end-of-file proves no descendant is still alive
  holding it. In the common case this lets cleanup skip a slow scan of every
  process's open files.
- **A watchdog.** The tree is in its own session, so if the Wisp backend is killed
  outright, nothing would stop it. A small watchdog process, described in
  `tools/shell/watchdog.py`, receives each command's registration over a pipe. When
  the backend dies, the watchdog reads end-of-file and stops every command still
  registered.

### Capturing output with bounded memory

`process.communicate()` would buffer all output in memory, and a command that
prints gigabytes would exhaust it. Wisp reads each stream in 8 KiB chunks and keeps
only the tail:

```python
class _StreamTail:
    def append(self, chunk: bytes) -> None:
        self._retained += chunk
        excess = len(self._retained) - self._max_bytes
        if excess <= 0:
            return
        # Start on a UTF-8 character boundary so the kept tail decodes cleanly.
        while excess < len(self._retained) and self._retained[excess] & 0xC0 == 0x80:
            excess += 1
        del self._retained[:excess]
        self._dropped_bytes += excess
```

UTF-8 continuation bytes have the bit pattern `10xxxxxx`, so `byte & 0xC0 == 0x80`
identifies them. Skipping past them ensures the kept tail never starts in the
middle of a character. Stdout and stderr have separate tails, so a noisy stdout
cannot push the error message out of stderr.

Waiting for the command to finish needs similar care:

```python
readers = (
    asyncio.create_task(_read_stream_tail(process.stdout, stdout_tail)),
    asyncio.create_task(_read_stream_tail(process.stderr, stderr_tail)),
)
pending = set(readers)
while pending and process.returncode is None:  # poll the exit status every 50 ms
    done, pending = await asyncio.wait(pending, timeout=_EXIT_POLL_INTERVAL_SECONDS)
if pending:  # the shell exited but a pipe is still open
    done, still_open = await asyncio.wait(pending, timeout=_OUTPUT_DRAIN_GRACE_SECONDS)
```

asyncio's `Process.wait()` also waits for the pipes to close. A command such as
`npm run dev &` leaves a background process holding stdout, so `wait()` would
never return. Wisp polls the shell's exit status instead, gives the readers 0.2
seconds to drain, and treats a pipe still open after that as a background process.
Cleanup stops it, and the result tells the model to use `operation=start` for
processes that should keep running.

### Stopping the process tree

```python
async def _terminate_posix_process_tree(process):
    group_term = await asyncio.to_thread(_signal_posix_process_group, process, signal.SIGTERM)
    if group_term is _PosixGroupSignal.FAILED:
        return False
    if group_term is _PosixGroupSignal.SENT:
        await asyncio.sleep(_POSIX_TERMINATION_GRACE_SECONDS)  # 50 ms to exit cleanly
    return await asyncio.to_thread(_kill_posix_process_tree, process)

def _kill_posix_process_tree(process):
    descendants_recorded = _record_posix_descendant_pids(process)  # a final scan for children
    _signal_posix_detached_processes(process, signal.SIGKILL)      # token-tagged daemons
    group_kill = _signal_posix_process_group(process, signal.SIGKILL)  # killpg
    recorded_kill_succeeded = _signal_posix_recorded_jobs(process, signal.SIGKILL)
    # ... success only if every step could account for the whole tree
```

`SIGTERM` asks processes to exit and can be handled; `SIGKILL` cannot be. Recorded
PIDs are signalled only if they still belong to the command: they are still
descendants, or still hold its jobs-file descriptor. On Linux, Wisp opens a
`pidfd`, a descriptor bound to one specific process, before checking, so the signal
cannot reach a different process that received a recycled PID. If cleanup cannot
account for every process, the call fails with `Failed to terminate process tree`
and tracking is kept so a later attempt can retry it.

### The result

The model-facing text starts with a fixed status prefix, followed by stdout and
then stderr. A failing test run might return:

```text
Command exited with code 1: F
FAIL: test_add (test_calculator.AddTests.test_add)
AssertionError: -1 != 5
```

When the output does not fit, it is cut from the front: `[truncated]` follows the
prefix, then the tail of the output. The status prefix sits outside the output budget, so even a tiny budget never
hides whether the command succeeded. As in the chapter's teaching tool, a nonzero exit code
is an ordinary result rather than a `ToolError`. `data` carries the exit code,
separate stdout and stderr tails, truncation flags, how many bytes were dropped
from each stream, and the id of each stream that was saved.

### Saving what the result cut

A tail is the right default, but some failures print their cause first: a build
that reports one missing header and then thousands of follow-on errors. Without the
beginning, the model's only way back to the cause is to run the command again,
which is slow for a test suite and unsafe for a migration or a deploy. So each
stream's reader also feeds a `SavedStream` from
[`tools/output_store.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/output_store.py):

```python
async def append(self, chunk: bytes) -> None:
    with self._lock:  # a threading lock: file writes happen in worker threads
        # ... count bytes past the 8 MiB stream cap as omitted
        self._buffer += chunk
        if self._fd is not None:
            flush_due = len(self._buffer) >= _WRITE_BATCH_BYTES  # 64 KiB batches
        else:
            flush_due = len(self._buffer) > self._spill_threshold  # the tail budget
    if flush_due:
        await _run_in_thread(self._flush)  # creates the file on first use
```

Output that fits the budget stays in memory and never touches the disk. When a
stream outgrows the budget, or the result is cut for another reason such as the
line limit, the tool saves both streams and ends its text with one line:

```text
[Full output saved: stdout output_id=3f9c0a1b2c3d4e5f (2481533 bytes, 31007 lines). Read it with read_output.]
```

`read_output` takes that id with the same `offset` and `limit` paging as `read`.
The model never passes a path, so it cannot use the tool to reach any other file.
The streams are kept separate: two pipes read concurrently arrive in no reliable
order, so a combined log would claim an interleaving Wisp does not know.

The store bounds itself and cleans up after itself:

- **Limits.** 8 MiB per stream, keeping the beginning, since the result already
  shows the end. At most 64 MiB and 32 streams per runtime; when either is
  reached, the oldest finished stream is deleted, as is a running stream whose
  id no result has reported yet. Once a stream loses bytes to a limit, saving
  stops for good, so what is saved is always an unbroken prefix.
- **Lifetime.** Files live in a private directory, `~/.wisp/tool-output/runtime-*`
  (mode 0700, files 0600), which closing the runtime deletes. Reads go through a
  descriptor opened when the file was created, never through its path. Output does not
  survive a restart, like a `process_id`. Each runtime holds an exclusive lock on
  a file in its directory, and the first runtime to save output deletes any
  directory whose lock it can take, which removes what a crashed runtime left.
- **Failures.** A disk error never changes the command's result. That stream is
  reported as not saved, with the reason, and its partial file is deleted.
- **Cancellation.** A write in progress finishes before a cancelled reader
  continues, so the file is never closed under a worker thread. A run that is
  cancelled or fails discards its streams, because no result will name them.

### Progress while a command runs

A build can run for minutes, and without progress a frontend shows only that it
is running. While a one-shot command runs, the supervisor samples the same tails
every 250 ms and, when either stream has grown, reports them to the call's
progress handle (`ToolContext.progress`). A tool called without a handle, from
an SDK test for example, reports nothing.

Delivery belongs to the coding layer, in
[`coding/tool_progress.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/coding/tool_progress.py).
The handle is a one-slot buffer: each report replaces the last, and a forwarder
task sends whatever is newest when the frontend can take it:

```python
async with anyio.create_task_group() as task_group:
    task_group.start_soon(forward)  # waits 0.5 s, then sends at most every 0.25 s
    try:
        result = await run(slot)    # the tool reports into the slot as it likes
    finally:
        slot.close()                # stops the forwarder before the result is returned
return result
```

Three properties follow from that shape:

- **Nothing queues up.** Reporting never waits, so reading the command's pipes
  never stalls behind a slow frontend; the frontend just sees fewer snapshots.
  Each `tool.execution.progress` event is a whole snapshot rather than a delta,
  which is what makes skipping safe.
- **Progress stays inside the call.** The runner closes the slot and waits for a
  delivery in flight before it returns `tool.execution.ended`, so no progress
  event can follow it.
- **The agent loop is unchanged.** The runner is a plain coroutine, so it may own
  the forwarder's task group. Merging progress into the loop's event generator
  would mean holding a task group open across `yield`, which anyio does not allow.

Progress is not persisted and never reaches the model. `CodingSession.run()`
takes the frontend's sink as `live_event_sink`; the RPC host passes its own
event rendering, so progress reaches the TUI and SDK in order with the run's
other events, and JSON mode passes its writer.

### `start`, `poll`, and `cancel`

A dev server or a long build should not block one tool call. `operation=start`
spawns the command the same way but returns a `process_id` instead of waiting.
The supervisor then keeps three tasks per process: one reader for each stream and
one completion task that enforces `lifetime_seconds` (default 300, up to a day).
Readers append into a buffer bounded by bytes and lines that drops the oldest
output first, and into a `SavedStream`, so a poll that reports dropped output can
also say where the full output was saved. An optional Rust implementation of that buffer lives in
`rust/wisp-process-text`.

`poll` returns only the output produced since the previous poll:

```python
async def poll(self, process_id: str, *, wait_seconds: float = 0) -> ProcessUpdate:
    managed = await self._get(process_id)
    if wait_seconds > 0:
        # ... unless output is already waiting or the process has ended:
        await asyncio.wait_for(managed.changed.wait(), timeout=wait_seconds)
    async with managed.operation_lock:
        return self._snapshot(managed)  # drains both buffers
```

Readers set `managed.changed`, an `asyncio.Event`, whenever output arrives or the
process ends. A poll with `wait_seconds` is a long poll: it returns as soon as
something happens, without repeatedly checking in a loop. `cancel` stops the tree
and marks the process `cancelled`. A process reports one of five states:
`running`, `completed`, `timed_out`, `cancelled`, or `failed`. An exit code appears
only for `completed`, so "still running" is never confused with "exited".

## 7. `grep`

Arguments: `pattern`, plus optional `path` (default `.`), `glob`, `ignore_case`,
`literal`, `context` lines, and `max_results` (default 100).

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), `build/greeting.py` also contains `TODO` but is excluded by
`.gitignore`. With `context`, nearby lines use `-` instead of `:`:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:grep}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:grep-context}}
```

`grep`, `find`, and `ls` all live in
[`tools/search/tools.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/search/tools.py).
A `grep` call validates its arguments, chooses a scanner, and runs the search in a
worker thread with a deadline. The search walks the tree, scans each file, and
assembles bounded output.

### A deadline for a thread that cannot be killed

Python cannot forcibly stop a thread. Searches therefore check a
`threading.Event` between files and between lines, and `_run_search_worker`
controls those events:

```python
async def stop_at_deadline() -> None:
    await anyio.sleep(_SEARCH_DEADLINE_SECONDS)        # 30 s
    deadline_expired.set()                             # "return what you have"
    stop()                                             # sets cancel_event
    await anyio.sleep(_SEARCH_DEADLINE_GRACE_SECONDS)  # 5 s more
    worker_scope.cancel()                              # give up waiting for the thread

async with anyio.create_task_group() as tasks:
    tasks.start_soon(stop_at_deadline)
    with worker_scope:
        result = await anyio.to_thread.run_sync(search, abandon_on_cancel=True)
    tasks.cancel_scope.cancel()
```

At the deadline the worker returns its partial results, marked incomplete. It only
fails to stop if it is blocked inside a system call, for example on an
unresponsive network mount. After the grace period the call returns a `timeout`
error and abandons the thread.

### Walking the tree

`grep` and `find` share one walk. Only the search root may be a symbolic link,
resolved as for `read`; links found during the walk are skipped. This is
`_walk_directory`, shortened:

```python
ignore_specs += _read_ignore_specs(descriptor, path, context, include_gitignore=in_git_repository)
retained = _bounded_sorted_directory_entries(descriptor, path)  # at most 100,000, sorted by name
for entry in retained:
    info = entry.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        continue
    if stat.S_ISDIR(info.st_mode):
        if _is_hidden(name) or (
            _is_ignored(candidate, is_directory=True, ignore_specs=ignore_specs)
            and not _may_reinclude_descendant(candidate, ignore_specs)
        ):
            continue
        if depth >= _MAX_WALK_DEPTH:  # 100 levels, one open descriptor per level
            report.deep_directories_skipped += 1
            continue
        child = os.open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, dir_fd=descriptor)
        try:
            yield from _walk_directory(child, candidate, context, ..., depth=depth + 1)
        finally:
            os.close(child)
    elif stat.S_ISREG(info.st_mode) and not hidden_or_ignored and not protected:
        yield _WalkedFile(path=candidate, parent_descriptor=descriptor, leaf=name,
                          expected_identity=(info.st_dev, info.st_ino))
```

The walk is a generator, so memory grows with directory depth and the size of one
directory, not with the size of the tree. Sorting each directory keeps results in
a stable order from run to run. Each yielded file carries its open parent and the
device and inode seen during the listing. Opening it later uses `openat` with
`O_NOFOLLOW` and compares the identity again, so a file replaced in the meantime is
skipped. Protected files are dropped during the walk, so neither their names nor
their contents reach the model.

Ignore rules follow Git: `.gitignore` applies inside a Git repository (a `.git`
directory, or a `.git` file containing `gitdir:` in a worktree), as do
`.git/info/exclude` and the ignore files of directories between the repository
root and the search root. `.ignore` and `.rgignore` apply everywhere. Rules are
matched with the `pathspec` library, including `!` negations, which is why an
ignored directory is still entered when a negation could re-include something
inside it. Each ignore file is capped at 1 MB and 10,000 patterns, because a
repository is untrusted input too.

When the optional native module is installed, the Rust crate `rust/wisp-search`
performs the same walk while releasing the GIL.

### Scanning one file

Files are read in 64 KiB binary chunks and decoded incrementally:

```python
decoder = codecs.getincrementaldecoder("utf-8")()
with os.fdopen(os.dup(descriptor), "rb") as file:
    while chunk := file.read(_PYTHON_GREP_CHUNK_BYTES):
        decoded = decoder.decode(chunk)  # holds back a character split across chunks
        if "\0" in decoded:
            raise _BinaryFileDetected   # the file is skipped
        # ... split into lines; a line over 1,000,000 characters skips the file
        yield block
```

A chunk boundary can fall in the middle of a multi-byte character, or between the
`\r` and `\n` of a line ending. The incremental decoder and a carried-over `\r`
handle both. A NUL byte marks a binary file, which is skipped without comment. A
file with an enormous line, such as a minified bundle, is skipped and named in a
notice. Invalid UTF-8 also skips the file.

Before testing each line, `grep` joins the chunk's lines with `\n` and searches
the whole block once. If the block cannot contain a match, every line in it is
skipped. This shortcut is only used for patterns where a match within one line is
guaranteed to be a match within the block; patterns with string anchors or
lookarounds are searched line by line.

Regular expressions use the third-party `regex` module rather than `re`, because
it supports a timeout:

```python
def regex_matcher(line: str) -> bool:
    try:
        return search(line, None, None, False, False, _REGEX_TIMEOUT_SECONDS) is not None  # 50 ms
    except TimeoutError as exc:
        if retry_budget <= 0:
            raise time_limit_error() from exc
    # A line that only lost the CPU will finish; catastrophic backtracking will not.
    started = time.monotonic()
    try:
        return expression.search(line, timeout=retry_budget) is not None
    except TimeoutError as exc:
        raise time_limit_error() from exc
    finally:
        retry_budget -= time.monotonic() - started  # one second shared by the whole call
```

A pattern such as `(a+)+$` can take exponential time on a line of `a`s followed by
another character. The timeout turns that into an `invalid_pattern` error
suggesting `literal=true`, instead of a stuck search.

Matches are written as `path:line:text` and context lines as `path-line-text`,
the format of command-line `grep`. When context is shown, results from different
files are separated by `--`. A `deque` holds the preceding lines, and overlapping context groups merge so no line
is printed twice. Long lines are clipped to 500 characters, and `context` is capped
so that one match and its context fit in the line budget. The search stops at the
output budget or at `max_results`. It looks one match past the limit, so it can
report that more results exist.

For a case-sensitive `literal=true` search, each file can be scanned by the Rust
function `scan_literal_fd` instead. It receives the descriptor the walk already
opened and applies the same budgets with the GIL released. The secured walk still
decides which files are read, so the faster path cannot read a file the Python
path would refuse. If the native scanner cannot read a descriptor, for example in
a sandbox without `/proc`, that file falls back to the Python scanner.

## 8. `find`

Arguments: optional `path`, `pattern` (default `*`), and `max_results` (default
100).

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), `build/greeting.py` is left out because `.gitignore` excludes it:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:find}}
```

`find` reuses the walk from section 7, so it honors the same links, ignore, hidden, and
protected-path rules. Each file's path relative to the working directory is
matched against a gitignore-style glob: `*.py` matches at any depth, `src/**/*.ts`
matches below `src`, `{ts,tsx}` expands into alternatives (at most 256), and a
leading `!` inverts the match.

Sorting needs one trick. The walk sorts each directory, but descends into `a/`
before visiting `a-c`. Globally, `a-c` sorts first because `-` precedes `/`. To
return the first results in true sorted order without holding every path:

```python
matches = heapq.nsmallest(max_results + 1, matching_display_paths())
```

`heapq.nsmallest` consumes the stream while keeping a heap of only
`max_results + 1` items. The extra item shows whether the result was cut off.

## 9. `ls`

Arguments: optional `path` and `all` (include hidden entries).

In the [tour](../02-tools.md#watch-the-built-ins-work-on-a-project), `all` reveals `.git/` and `.gitignore`, but never the protected
`.env`:

```text
{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:ls}}

{{#include ../../../../../../examples/crafting_agents/builtin_tools_tour.txt:ls-all}}
```

`ls` lists one directory, with no recursion and no ignore rules:

```python
def eligible_entries(descriptor):
    with os.scandir(descriptor) as entries:  # list through the secured descriptor
        for entry in entries:
            if not include_hidden and entry.name.startswith("."):
                continue
            if _is_protected_walked_file(secure_path.path / entry.name,
                                         real_directory / entry.name, context):
                continue
            entry_count += 1
            yield entry.name, entry.is_dir(follow_symlinks=False)

with open_directory(secure_path) as descriptor:
    retained = heapq.nsmallest(retained_limit + 1, eligible_entries(descriptor),
                               key=lambda entry: entry[0].lower())
names = [f"{name}/" if is_directory else name for name, is_directory in retained]
```

`open_directory` walks components with `O_NOFOLLOW` like `open_parent`, and
`os.scandir` accepts a directory descriptor on POSIX. Protected names are hidden,
checked against both the listed path and the directory's real path. Because
`is_dir(follow_symlinks=False)` does not follow links, a link to a directory is
listed without a trailing `/`. Entries sort case-insensitively, and only as many as
the output budget could show are kept in memory, while `entry_count` still counts
every eligible entry.

## Where to go next

- [Hardening the tool boundary](../case-studies/tool-boundary.md) steps back from
  the mechanisms to the pressures behind them and the limitations that remain.
- [Earning a Rust boundary](../case-studies/rust-boundary.md) explains how
  measurements decided which parts of search and output retention moved to Rust.
- [Chapter 5: Controlling side effects](../05-side-effects.md) builds the teaching
  agent's own exposure, policy, approval, and process boundaries.

Continue the course with [chapter 3: Giving the model useful context](../03-context.md).
