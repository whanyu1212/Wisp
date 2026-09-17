# Case study: Hardening the tool boundary

The [chapter 2 checkpoint](../02-tools.md) assumes a small trusted fixture and a
single writer. Wisp operates inside developer repositories, runs long-lived
commands, and accepts calls from models and custom executors. This case study
connects those pressures to the mechanisms around its built-in tools.

## Files: a path check is not enough

A file can change between checking its path and opening it. An entry that was a
regular file can become a symlink; another process can replace the target while
an edit is being prepared. These are time-of-check/time-of-use races.

Wisp's `src/wisp/tools/files/secure_fs.py` uses component-by-component,
descriptor-relative access with `O_NOFOLLOW` on POSIX and a junction-aware Windows
path. File operations check identity and version information rather than relying
only on a previously validated string.

On top of that, `src/wisp/tools/files/operations.py` supplies:

- **Read:** 1-indexed offset/limit slicing inside the secured open, avoiding a
  whole-file load for a small page.
- **Write:** a same-directory temporary file and atomic replacement in the common
  case, preserving metadata and checking for concurrent replacement. Prior text
  snapshots are capped so diffs do not flood the event wire.
- **Edit:** every `oldText` must match exactly once; replacements must not overlap;
  a detected concurrent modification aborts rather than silently merging.

“Atomic write” is not a universal guarantee here. When the destination has
multiple hard links, or a permission fallback requires it, Wisp writes in place
to preserve the inode. Other readers may observe a partial write, and hard-link
aliases see the change. That is a meaningful compatibility trade-off to document,
not hide behind an atomic-write label.

Descriptor handling, metadata preservation, and platform differences explain why
file tools are much larger than their schemas. A short schema is a model-facing
interface, not a measure of implementation complexity.

## Shell: output and lifetime need owners

`BashTool` in `src/wisp/tools/shell/tool.py` supports `run`, `start`, `poll`, and
`cancel` through a shared `ProcessSupervisor`. A bare `run` has a 30-second default
timeout. `start` retains a managed process with a lifetime cap, and `poll` returns
bounded increments of output.

Results report process state, separate stdout/stderr truncation information, and
dropped-byte counts. An exit code is available only once the process terminates.
This distinguishes “still running,” “finished,” and “I saw only part of its log.”

The chapter's `subprocess.run(..., capture_output=True)` only truncates after
capture. Wisp retains bounded process output as it arrives. Resource cleanup
also needs to handle process trees, not just the immediate child. These concerns
belong beside the process supervisor rather than in each frontend.

The output-retention kernel is partly native; [the Rust case study](rust-boundary.md)
explains the measurement behind that boundary.

## Search: two engines, one policy

Recursive search must handle hidden files, ignore rules, huge directories,
binary input, oversized lines, and expensive regular expressions. Otherwise a
read-only call can still exhaust memory or tie up execution.

Wisp's `GrepTool` and `FindTool` in `src/wisp/tools/search/tools.py` traverse with
open directory descriptors, skip hidden names and symlinks, and honor
`.gitignore`, `.ignore`, `.rgignore`, and `.git/info/exclude`. The traversal caps
directory entries and ignore-file size and pattern count. Scanning adds binary
detection, incremental UTF-8 decoding, a per-line ceiling, and regex timeouts.

`LsTool` has a narrower contract: a single-directory listing, optional hidden
entries, and no recursive ignore filtering. Sharing a tool family does not imply
identical traversal semantics.

Literal, case-sensitive grep can send an already-open descriptor to the optional
`wisp-search` Rust scanner. Regex and case-insensitive matching remain in Python;
unsupported native inputs or sandbox failures fall back. Protected-path filtering
applies to emitted records regardless of engine and fails closed on ambiguous
path parses. An acceleration path must not become a different permissions path.

## Execution: prepare approvals before side effects

Wisp separates `ToolPolicy` (“may this operation run?”) from `ToolApprovalPolicy`
(“must the user confirm it?”). `ToolContext` carries host-owned working directory,
output budgets, protected paths, and write scopes. Model arguments cannot grant
approval or change a tool's safety category.

The lifecycle validator in `src/wisp/agent/loop/tool_execution.py` checks ordering
and identity: approval request before resolution, one terminal execution result,
and no success after denial or events after termination. Invalid executor
sequences raise `ToolExecutionProtocolError`; they are not ordinary tool failures
to feed back as though execution had succeeded.

The prepared-executor path in `src/wisp/agent/loop/prepared_tools.py` first prepares
the calls, surfacing approvals without performing their side effects. It then
executes the prepared batch:

- If every call is parallel-safe, execution is concurrent in bounded groups, with
  results published in source order.
- If any call is not parallel-safe, execution is sequential.

That scheduling rule preserves a write-before-test dependency. Turn-based
batching alone would not. Truncated model responses with finish reason `length`
do not execute their calls; synthetic retryable results ask the model to reissue
complete arguments. Arguments are detached at boundaries so an executor cannot
rewrite the retained call record through a shared mutable dictionary.

## What these choices cost—and leave open

The small local tool surface is easier to describe and audit, but it does not
remove every workflow limitation:

- **No multi-file transaction or first-class undo.** Several edits can partially
  succeed. A before-snapshot for display is not a rollback system.
- **Exact-match edits.** Refusing ambiguous replacements is predictable but can
  require extra calls for broad mechanical changes.
- **Approval granularity.** Write scopes and tool grants do not automatically
  provide arbitrary path-pattern approval rules.
- **Unranked search.** Source-ordered, capped results can omit the most relevant
  match. Retrieval quality is a separate problem from safe traversal.
- **Process accounting.** Separate supervisors and local limits do not constitute
  one global process or memory budget.
- **Measurement.** `ToolResult` text/data/truncation is not itself a complete
  timing or cost telemetry model.

These are current limitations with possible remedies, not proof that every agent
should make the same trade-offs. Choose additional operations and guarantees
based on the workflows your users actually need.

## Evidence and further reading

For observable behavior, begin with
[`tests/coding/test_tool_execution.py`](https://github.com/whanyu1212/Wisp/blob/main/tests/coding/test_tool_execution.py)
and the interruption matrix described in
[Testing](../../contributing/testing.md#harness-interruption-and-recovery).
Tests exercise concrete invariants; their presence is not a claim of complete
coverage across platforms or every concurrent interleaving.

The user-facing configuration belongs in [Tools and safety](../../guide/tools-and-safety.md).
Return to [chapter 2](../02-tools.md) for the runnable teaching implementation.
